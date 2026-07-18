"""Interactive USD render layer dispatcher for Houdini LOP/ROP nodes.

Primary goal:
- Launch all detected USD RenderSettings as separate submissions without manual
  per-layer knob changes.

This module is intentionally self-contained so it can be called from HDA
callbacks, shelf tools, or Python shell.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass

import hou

try:
    from qtpy import QtCore, QtWidgets
except Exception:
    try:
        from PySide6 import QtCore, QtWidgets
    except Exception:
        try:
            from PySide2 import QtCore, QtWidgets
        except Exception:
            QtCore = None
            QtWidgets = None


_LOGGER = logging.getLogger("BMFX.RenderDispatcher")
if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    _LOGGER.addHandler(handler)
_LOGGER.setLevel(logging.INFO)

_DEFAULT_RENDERSETTINGS_PATH = "/Render/rendersettings"
_DEFAULT_ROOT_PATH = "/World/Shot"
_DEFAULT_CAMERA_PATH = "/World/Shot/renderCam"
_DEFAULT_RENDER_CONFIG_PATH = "/Render"

_BEHAVIOR_TYPES = [
    "Normal",
    "Matte",
    "Phantom",
    "Holdout",
    "Pruned",
]

_PRESET_DIR = os.path.join(
    os.path.expanduser("~"),
    ".ayon",
    "houdini",
    "render_dispatcher",
)


@dataclass
class LayerEntry:
    path: str
    name: str
    scope: str
    enabled: bool = True
    behavior: str = "Normal"
    motion_blur: bool = True
    notes: str = ""


def _resolve_node(node_or_kwargs=None):
    """Resolve Houdini node from callback kwargs/path/node."""
    if isinstance(node_or_kwargs, dict):
        node_or_kwargs = node_or_kwargs.get("node")

    if node_or_kwargs is None:
        return hou.pwd()

    if isinstance(node_or_kwargs, hou.Node):
        return node_or_kwargs

    if isinstance(node_or_kwargs, str):
        return hou.node(node_or_kwargs)

    return None


def _resolve_stage(node):
    """Resolve stage from LOP node or render-ROP that references a LOP path."""
    if not node:
        return None

    stage_method = getattr(node, "stage", None)
    if callable(stage_method):
        try:
            stage = stage_method()
        except Exception:
            stage = None
        if stage:
            return stage

    for parm_name in ("loppath", "lop_path", "lopnode", "source_lop"):
        parm = node.parm(parm_name)
        if not parm:
            continue
        try:
            lop_node = parm.evalAsNode()
        except Exception:
            lop_node = None
        if not lop_node:
            continue
        stage_method = getattr(lop_node, "stage", None)
        if not callable(stage_method):
            continue
        try:
            stage = stage_method()
        except Exception:
            stage = None
        if stage:
            return stage

    try:
        inputs = node.inputs()
    except Exception:
        inputs = []

    for input_node in inputs:
        stage_method = getattr(input_node, "stage", None)
        if not callable(stage_method):
            continue
        try:
            stage = stage_method()
        except Exception:
            stage = None
        if stage:
            return stage

    return None


def _collect_scope_paths(stage, root_path):
    """Collect scope paths under root path."""
    if not stage:
        return []

    root_prim = stage.GetPrimAtPath(root_path)
    if not root_prim or not root_prim.IsValid():
        return []

    output = []
    stack = [root_prim]
    while stack:
        prim = stack.pop(0)
        if not prim or not prim.IsValid():
            continue

        type_name = prim.GetTypeName() or ""
        if type_name == "Scope":
            output.append(str(prim.GetPath()))

        try:
            children = prim.GetChildren()
        except Exception:
            children = []
        for child in children:
            stack.append(child)

    output = sorted(dict.fromkeys(output))
    return output


def _collect_render_settings_paths(stage, render_config_path=_DEFAULT_RENDER_CONFIG_PATH):
    """Collect RenderSettings prim paths, optionally filtered by config root."""
    if not stage:
        return []

    config_prefix = (render_config_path or "").strip()
    if config_prefix.endswith("/") and config_prefix != "/":
        config_prefix = config_prefix[:-1]

    output = []
    try:
        for prim in stage.Traverse():
            if not prim or not prim.IsValid():
                continue
            if prim.GetTypeName() != "RenderSettings":
                continue
            path = str(prim.GetPath())
            if config_prefix and config_prefix != "/":
                if not (path == config_prefix or path.startswith(config_prefix + "/")):
                    continue
            output.append(path)
    except Exception:
        return []

    output = sorted(dict.fromkeys(output))
    return output


def _expand_requested_paths(render_node, available_paths):
    """Resolve requested `rendersettings` paths to concrete stage paths."""
    if not available_paths:
        return []

    parm = render_node.parm("rendersettings")
    if not parm:
        return []

    try:
        raw_value = parm.evalAsString().strip()
    except Exception:
        raw_value = ""

    if not raw_value:
        raw_value = _DEFAULT_RENDERSETTINGS_PATH

    # Default + multi layers => fan out to all.
    if raw_value == _DEFAULT_RENDERSETTINGS_PATH and len(available_paths) > 1:
        return list(available_paths)

    tokens = [token for token in re.split(r"[,\s;]+", raw_value) if token]
    if not tokens:
        return []

    selected = []
    has_wildcards = any(any(ch in token for ch in "*?[]") for token in tokens)

    if has_wildcards:
        for token in tokens:
            for path in available_paths:
                if fnmatch.fnmatchcase(path, token):
                    selected.append(path)
    else:
        for token in tokens:
            if token in available_paths:
                selected.append(token)

    selected = list(dict.fromkeys(selected))
    return selected


def _find_submit_parm(node):
    """Pick the best submit/render button parameter on node."""
    candidates = (
        "submit",
        "submitjob",
        "deadline_submit",
        "executebackground",
        "execute",
        "render",
    )
    for name in candidates:
        parm = node.parm(name)
        if parm is not None:
            return parm
    return None


@contextlib.contextmanager
def _temporary_rendersettings(node, path):
    """Temporarily set `rendersettings` parm and restore it after."""
    parm = node.parm("rendersettings")
    if parm is None:
        yield
        return

    original = None
    try:
        original = parm.evalAsString()
    except Exception:
        original = None

    try:
        parm.set(path)
        yield
    finally:
        if original is not None:
            try:
                parm.set(original)
            except Exception:
                pass


def _infer_scope_from_layer_path(layer_path, scope_paths):
    """Guess best scope label for a layer path."""
    if not scope_paths:
        return "global"

    layer_name = layer_path.split("/")[-1].lower()

    # 1) Strong match by scope token in layer name.
    for scope_path in sorted(scope_paths, key=len, reverse=True):
        scope_name = scope_path.split("/")[-1].lower()
        if scope_name and scope_name in layer_name:
            return scope_path.split("/")[-1]

    # 2) Fallback to first non-root scope.
    if len(scope_paths) > 1:
        return scope_paths[1].split("/")[-1]

    return scope_paths[0].split("/")[-1]


def get_render_layers(node_or_kwargs=None):
    """Return render settings paths that would be dispatched."""
    node = _resolve_node(node_or_kwargs)
    if not node:
        return []

    stage = _resolve_stage(node)
    available = _collect_render_settings_paths(stage)
    selected = _expand_requested_paths(node, available)
    return selected


def dispatch_render_layers(
    node_or_kwargs=None,
    selected_paths=None,
    dry_run=False,
    show_messages=True,
):
    """Dispatch one submission per render layer.

    Args:
        node_or_kwargs: Houdini node, path or callback kwargs dict.
        selected_paths (list[str] | None): Explicit render settings paths.
        dry_run (bool): If True, no button press, logs only.
        show_messages (bool): Show Houdini popups for user feedback.

    Returns:
        list[str]: List of dispatched RenderSettings paths.
    """
    node = _resolve_node(node_or_kwargs)
    if not node:
        raise RuntimeError("Could not resolve render node.")

    stage = _resolve_stage(node)
    available = _collect_render_settings_paths(stage)

    if selected_paths is None:
        selected = _expand_requested_paths(node, available)
    else:
        selected = [path for path in selected_paths if path in available]

    if not selected:
        message = (
            f"No RenderSettings found for dispatch on node: {node.path()}\n"
            "Check stage input and `rendersettings` value."
        )
        _LOGGER.warning(message)
        if show_messages and hou.isUIAvailable():
            hou.ui.displayMessage(message, title="Render Dispatcher")
        return []

    submit_parm = _find_submit_parm(node)
    if submit_parm is None:
        raise RuntimeError(
            f"No submit/render button found on node: {node.path()}"
        )

    _LOGGER.info(
        "Dispatching %d render layer(s) from %s",
        len(selected),
        node.path(),
    )

    for path in selected:
        layer_name = path.split("/")[-1] or path
        _LOGGER.info("Layer -> %s (%s)", layer_name, path)
        if dry_run:
            continue
        with _temporary_rendersettings(node, path):
            submit_parm.pressButton()

    if show_messages and hou.isUIAvailable():
        hou.ui.displayMessage(
            f"Dispatched {len(selected)} render layer job(s).",
            title="Render Dispatcher",
        )

    return selected


class RenderDispatcherDialog(QtWidgets.QDialog if QtWidgets is not None else object):
    """UI panel for interactive scope/layer selection and dispatch."""

    def __init__(self, node, parent=None):
        if QtWidgets is None:
            raise RuntimeError("Qt is unavailable in this Houdini session.")
        super(RenderDispatcherDialog, self).__init__(parent)
        self.node = node
        self.stage = None
        self.scope_paths = []
        self.layer_entries = []

        self.setWindowTitle("Universal Render Pass Dispatcher")
        self.resize(1400, 900)

        self._build_ui()
        self._load_defaults_from_node()
        self.refresh_data()

    def _build_ui(self):
        root_layout = QtWidgets.QVBoxLayout(self)

        # Header row
        header_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Universal Render Pass Dispatcher")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        header_row.addWidget(title)
        header_row.addStretch(1)

        self.save_btn = QtWidgets.QPushButton("Save Preset")
        self.load_btn = QtWidgets.QPushButton("Load Preset")
        self.help_btn = QtWidgets.QPushButton("?")
        self.help_btn.setFixedWidth(28)
        header_row.addWidget(self.save_btn)
        header_row.addWidget(self.load_btn)
        header_row.addWidget(self.help_btn)
        root_layout.addLayout(header_row)

        self.tabs = QtWidgets.QTabWidget()
        root_layout.addWidget(self.tabs)

        self._build_context_tab()
        self._build_setup_tab()
        self._build_preview_tab()
        self._build_dispatch_tab()

        self.status_label = QtWidgets.QLabel("Ready")
        root_layout.addWidget(self.status_label)

        self.refresh_btn.clicked.connect(self.refresh_data)
        self.search_edit.textChanged.connect(self._filter_rows)
        self.save_btn.clicked.connect(self._save_preset)
        self.load_btn.clicked.connect(self._load_preset)
        self.help_btn.clicked.connect(self._show_help)
        self.preview_btn.clicked.connect(self._refresh_preview)
        self.dispatch_btn.clicked.connect(self._dispatch_from_ui)
        self.select_all_btn.clicked.connect(self._select_all_rows)
        self.clear_btn.clicked.connect(self._clear_all_rows)
        self.invert_btn.clicked.connect(self._invert_rows)

    def _build_context_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        form = QtWidgets.QFormLayout()
        self.root_path_edit = QtWidgets.QLineEdit(_DEFAULT_ROOT_PATH)
        self.camera_path_edit = QtWidgets.QLineEdit(_DEFAULT_CAMERA_PATH)
        self.render_config_edit = QtWidgets.QLineEdit(_DEFAULT_RENDER_CONFIG_PATH)
        form.addRow("USD Root Path", self.root_path_edit)
        form.addRow("Camera", self.camera_path_edit)
        form.addRow("Render Config Path", self.render_config_edit)
        layout.addLayout(form)

        row = QtWidgets.QHBoxLayout()
        self.auto_detect_check = QtWidgets.QCheckBox("Auto Detect Scopes & Passes")
        self.auto_detect_check.setChecked(True)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        row.addWidget(self.auto_detect_check)
        row.addStretch(1)
        row.addWidget(self.refresh_btn)
        layout.addLayout(row)

        self.scope_tree = QtWidgets.QTreeWidget()
        self.scope_tree.setHeaderLabels(["Scope / Layer", "Type", "Status"])
        self.scope_tree.setRootIsDecorated(True)
        layout.addWidget(self.scope_tree)

        self.tabs.addTab(tab, "1. Context & Scopes")

    def _build_setup_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search layer...")
        top.addWidget(self.search_edit)
        layout.addLayout(top)

        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Enable",
            "Layer",
            "Scope",
            "RenderSettings",
            "Behavior",
            "Motion Blur",
            "Notes",
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table)

        actions = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Select All")
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.invert_btn = QtWidgets.QPushButton("Invert Selection")
        actions.addWidget(self.select_all_btn)
        actions.addWidget(self.clear_btn)
        actions.addWidget(self.invert_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.tabs.addTab(tab, "2. Pass & Layer Setup")

    def _build_preview_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        self.preview_text = QtWidgets.QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        layout.addWidget(self.preview_text)

        row = QtWidgets.QHBoxLayout()
        self.preview_btn = QtWidgets.QPushButton("Build Preview")
        row.addStretch(1)
        row.addWidget(self.preview_btn)
        layout.addLayout(row)

        self.tabs.addTab(tab, "3. Build & Preview")

    def _build_dispatch_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        self.dispatch_summary = QtWidgets.QLabel("No jobs prepared")
        layout.addWidget(self.dispatch_summary)

        self.dry_run_check = QtWidgets.QCheckBox("Dry Run (log only, do not press submit)")
        layout.addWidget(self.dry_run_check)

        self.dispatch_btn = QtWidgets.QPushButton("Dispatch Render Layers")
        self.dispatch_btn.setMinimumHeight(44)
        layout.addWidget(self.dispatch_btn)

        layout.addStretch(1)
        self.tabs.addTab(tab, "4. Dispatch")

    def _load_defaults_from_node(self):
        if not self.node:
            return
        rs = self.node.parm("rendersettings")
        if rs:
            value = rs.evalAsString().strip()
            if value and value != _DEFAULT_RENDERSETTINGS_PATH:
                parent_path = "/".join(value.split("/")[:-1])
                if parent_path:
                    self.render_config_edit.setText(parent_path)

    def refresh_data(self):
        node = self.node
        if not node:
            self.status_label.setText("No node selected")
            return

        self.stage = _resolve_stage(node)
        if not self.stage:
            self.status_label.setText("Failed to resolve USD stage from node")
            self.scope_tree.clear()
            self.table.setRowCount(0)
            return

        root_path = self.root_path_edit.text().strip() or _DEFAULT_ROOT_PATH
        render_cfg = self.render_config_edit.text().strip() or _DEFAULT_RENDER_CONFIG_PATH

        self.scope_paths = _collect_scope_paths(self.stage, root_path)
        render_layers = _collect_render_settings_paths(self.stage, render_cfg)

        if not render_layers:
            # fallback to whole stage
            render_layers = _collect_render_settings_paths(self.stage, "")

        # Build entries
        previous = {entry.path: entry for entry in self.layer_entries}
        entries = []
        for layer_path in render_layers:
            layer_name = layer_path.split("/")[-1]
            scope_name = _infer_scope_from_layer_path(layer_path, self.scope_paths)

            old = previous.get(layer_path)
            if old:
                entries.append(old)
                continue

            entries.append(
                LayerEntry(
                    path=layer_path,
                    name=layer_name,
                    scope=scope_name,
                    enabled=True,
                    behavior="Normal",
                    motion_blur=True,
                )
            )

        self.layer_entries = entries

        self._populate_scope_tree()
        self._populate_table()
        self._refresh_preview()

        self.status_label.setText(
            f"Detected {len(self.scope_paths)} scopes, {len(self.layer_entries)} render layers"
        )

    def _populate_scope_tree(self):
        self.scope_tree.clear()

        grouped = {}
        for entry in self.layer_entries:
            grouped.setdefault(entry.scope, []).append(entry)

        for scope in sorted(grouped.keys()):
            scope_item = QtWidgets.QTreeWidgetItem([scope, "Scope", f"{len(grouped[scope])} Layers"])
            self.scope_tree.addTopLevelItem(scope_item)
            for entry in sorted(grouped[scope], key=lambda x: x.name.lower()):
                child = QtWidgets.QTreeWidgetItem([entry.name, "Layer", entry.path])
                child.setCheckState(0, QtCore.Qt.Checked if entry.enabled else QtCore.Qt.Unchecked)
                scope_item.addChild(child)
            scope_item.setExpanded(True)

    def _populate_table(self):
        self.table.setRowCount(0)
        self.table.setSortingEnabled(False)

        for row, entry in enumerate(self.layer_entries):
            self.table.insertRow(row)

            enable_item = QtWidgets.QTableWidgetItem()
            enable_item.setFlags(enable_item.flags() | QtCore.Qt.ItemIsUserCheckable)
            enable_item.setCheckState(QtCore.Qt.Checked if entry.enabled else QtCore.Qt.Unchecked)
            self.table.setItem(row, 0, enable_item)

            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.name))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.scope))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(entry.path))

            behavior_combo = QtWidgets.QComboBox()
            behavior_combo.addItems(_BEHAVIOR_TYPES)
            if entry.behavior in _BEHAVIOR_TYPES:
                behavior_combo.setCurrentText(entry.behavior)
            self.table.setCellWidget(row, 4, behavior_combo)

            motion_chk = QtWidgets.QCheckBox()
            motion_chk.setChecked(entry.motion_blur)
            motion_chk.setStyleSheet("margin-left: 10px;")
            self.table.setCellWidget(row, 5, motion_chk)

            notes_item = QtWidgets.QTableWidgetItem(entry.notes)
            self.table.setItem(row, 6, notes_item)

        self.table.setSortingEnabled(True)

    def _filter_rows(self, text):
        text = (text or "").strip().lower()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 1)
            path_item = self.table.item(row, 3)
            blob = ""
            if name_item:
                blob += name_item.text().lower() + " "
            if path_item:
                blob += path_item.text().lower()
            self.table.setRowHidden(row, text not in blob)

    def _sync_entries_from_table(self):
        updated = []
        for row in range(self.table.rowCount()):
            path_item = self.table.item(row, 3)
            name_item = self.table.item(row, 1)
            scope_item = self.table.item(row, 2)
            enable_item = self.table.item(row, 0)
            notes_item = self.table.item(row, 6)
            if not path_item or not name_item or not scope_item:
                continue

            behavior_combo = self.table.cellWidget(row, 4)
            motion_chk = self.table.cellWidget(row, 5)

            updated.append(
                LayerEntry(
                    path=path_item.text(),
                    name=name_item.text(),
                    scope=scope_item.text(),
                    enabled=(enable_item.checkState() == QtCore.Qt.Checked) if enable_item else True,
                    behavior=behavior_combo.currentText() if behavior_combo else "Normal",
                    motion_blur=motion_chk.isChecked() if motion_chk else True,
                    notes=notes_item.text() if notes_item else "",
                )
            )

        self.layer_entries = updated

    def _refresh_preview(self):
        self._sync_entries_from_table()
        selected = [entry for entry in self.layer_entries if entry.enabled]

        lines = [
            f"Node: {self.node.path() if self.node else '<None>'}",
            f"Total Layers: {len(self.layer_entries)}",
            f"Selected for Dispatch: {len(selected)}",
            "",
            "Jobs:",
        ]

        for index, entry in enumerate(selected, start=1):
            lines.append(
                f"{index:02d}. {entry.name} | {entry.path} | behavior={entry.behavior} | motion_blur={entry.motion_blur}"
            )

        if not selected:
            lines.append("(none selected)")

        self.preview_text.setPlainText("\n".join(lines))
        self.dispatch_summary.setText(f"Prepared {len(selected)} dispatch job(s)")

    def _dispatch_from_ui(self):
        self._sync_entries_from_table()
        selected_paths = [entry.path for entry in self.layer_entries if entry.enabled]

        if not selected_paths:
            hou.ui.displayMessage("No render layers selected.", title="Render Dispatcher")
            return

        dispatched = dispatch_render_layers(
            self.node,
            selected_paths=selected_paths,
            dry_run=self.dry_run_check.isChecked(),
            show_messages=True,
        )
        self.status_label.setText(f"Dispatched {len(dispatched)} layers")
        self._refresh_preview()

    def _select_all_rows(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(QtCore.Qt.Checked)
        self._refresh_preview()

    def _clear_all_rows(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(QtCore.Qt.Unchecked)
        self._refresh_preview()

    def _invert_rows(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            state = item.checkState()
            item.setCheckState(QtCore.Qt.Unchecked if state == QtCore.Qt.Checked else QtCore.Qt.Checked)
        self._refresh_preview()

    def _save_preset(self):
        self._sync_entries_from_table()
        os.makedirs(_PRESET_DIR, exist_ok=True)

        default_path = os.path.join(_PRESET_DIR, "dispatcher_preset.json")
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Render Dispatcher Preset",
            default_path,
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        payload = {
            "root_path": self.root_path_edit.text().strip(),
            "camera": self.camera_path_edit.text().strip(),
            "render_config": self.render_config_edit.text().strip(),
            "layers": [entry.__dict__ for entry in self.layer_entries],
        }

        with open(file_path, "w") as stream:
            json.dump(payload, stream, indent=4)

        self.status_label.setText(f"Preset saved: {file_path}")

    def _load_preset(self):
        os.makedirs(_PRESET_DIR, exist_ok=True)
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load Render Dispatcher Preset",
            _PRESET_DIR,
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        with open(file_path, "r") as stream:
            payload = json.load(stream)

        self.root_path_edit.setText(payload.get("root_path", _DEFAULT_ROOT_PATH))
        self.camera_path_edit.setText(payload.get("camera", _DEFAULT_CAMERA_PATH))
        self.render_config_edit.setText(payload.get("render_config", _DEFAULT_RENDER_CONFIG_PATH))

        self.refresh_data()

        # Apply layer overrides from preset by path.
        by_path = {entry.path: entry for entry in self.layer_entries}
        for row in payload.get("layers", []):
            path = row.get("path")
            if path not in by_path:
                continue
            existing = by_path[path]
            existing.enabled = bool(row.get("enabled", existing.enabled))
            existing.behavior = row.get("behavior", existing.behavior)
            existing.motion_blur = bool(row.get("motion_blur", existing.motion_blur))
            existing.notes = row.get("notes", existing.notes)

        self._populate_table()
        self._refresh_preview()
        self.status_label.setText(f"Preset loaded: {file_path}")

    def _show_help(self):
        message = (
            "Workflow:\n"
            "1. Set root/render paths and click Refresh.\n"
            "2. Choose layers in Pass & Layer Setup.\n"
            "3. Optional: Save/Load preset.\n"
            "4. Dispatch to submit one job per selected render layer.\n\n"
            "Notes:\n"
            "- Dispatch submits by temporarily setting the node's `rendersettings`\n"
            "  parm for each selected layer and pressing the node's submit/render button.\n"
            "- Render behavior columns are organizational in this version."
        )
        hou.ui.displayMessage(message, title="Render Dispatcher Help")


def show_dispatcher(node_or_kwargs=None):
    """Open the interactive render dispatcher UI."""
    if QtWidgets is None:
        raise RuntimeError("Qt is unavailable; cannot open dispatcher UI.")

    node = _resolve_node(node_or_kwargs)
    if not node:
        raise RuntimeError("Could not resolve node for dispatcher UI.")

    parent = hou.qt.mainWindow() if hasattr(hou, "qt") else None

    dlg = RenderDispatcherDialog(node=node, parent=parent)
    dlg.show()

    # Keep dialog alive in Houdini session.
    try:
        hou.session.ayon_render_dispatcher_dialog = dlg
    except Exception:
        pass

    return dlg


# Callback-friendly aliases

def dispatch(kwargs):
    """Non-UI callback entrypoint: direct dispatch from current node settings."""
    return dispatch_render_layers(kwargs)


def open_ui(kwargs=None):
    """UI callback entrypoint."""
    return show_dispatcher(kwargs)
