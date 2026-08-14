import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

import hou
from ayon_core.lib import get_ffmpeg_tool_args
from qtpy import QtCore, QtGui, QtWidgets

log = logging.getLogger("ayon.flipbook")
if not log.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    log.addHandler(handler)
log.setLevel(logging.INFO)


def _sanitize_token(value, fallback="x"):
    text = str(value or "").strip()
    if not text:
        return fallback
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


_VERSION_DIR_PATTERN = re.compile(r"^v(\d+)$", re.IGNORECASE)
_TRAILING_VERSION_PATTERN = re.compile(r"_v\d+$", re.IGNORECASE)


class FlipbookShelfTool(QtWidgets.QDialog):
    VIEWPORT_CAMERA_TOKEN = "__viewport_camera__"
    DEFAULT_BUTTON_STYLE = ""
    DIRECT_OK_BUTTON_STYLE = (
        "QPushButton {"
        "background-color: #2d7a42;"
        "color: #ffffff;"
        "border: 1px solid #3c9c56;"
        "}"
    )
    DEADLINE_OK_BUTTON_STYLE = (
        "QPushButton {"
        "background-color: #2b5f9e;"
        "color: #ffffff;"
        "border: 1px solid #3b79c4;"
        "}"
    )
    WARNING_BUTTON_STYLE = (
        "QPushButton {"
        "background-color: #8a5b1f;"
        "color: #ffffff;"
        "border: 1px solid #b1782c;"
        "}"
    )
    RESOLUTION_PRESETS = [
        ("Camera", "camera"),
        ("Viewport", None),
        ("1280x720", (1280, 720)),
        ("1920x1080", (1920, 1080)),
        ("2048x858", (2048, 858)),
        ("3840x2160", (3840, 2160)),
        ("Custom", "custom"),
    ]
    ANTIALIAS_PRESETS = [
        ("Use Viewport", "viewport"),
        ("Off", "off"),
        ("2x", "2x"),
        ("4x", "4x"),
        ("8x", "8x"),
        ("16x", "16x"),
        ("32x", "32x"),
        ("64x", "64x"),
    ]
    BURNIN_PRESETS = [
        ("Auto (Task)", "auto"),
        ("Compact", "compact"),
        ("Detailed", "detailed"),
        ("Minimal", "minimal"),
        ("Off", "off"),
    ]
    OUTPUT_PROFILES = [
        ("HQ Review", "hq"),
        ("Fast Preview", "fast"),
        ("No Burnin", "noburnin"),
        ("Image Sequence Only", "sequence_only"),
    ]

    def __init__(self, parent=None):
        super(FlipbookShelfTool, self).__init__(parent)
        self.scene_viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
        self.last_flipbook = None
        self.last_publish_result = None
        self._last_upload_retry_payload = None
        self._version_preview_cache = {}
        self._active_operation = None
        self._cancel_requested = False

        self.setWindowTitle("Flipbook Tool")
        self.setMinimumWidth(540)
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowCloseButtonHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.WindowMinimizeButtonHint
        )

        self._build_ui()
        self.refresh_camera_list()
        self._refresh_label_dropdown()
        self._on_resolution_changed()
        self._update_name_preview()
        self._set_progress_idle()

    # --------------------------------------------------
    # UI
    # --------------------------------------------------
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        layout.addLayout(form)

        self.name_preview = QtWidgets.QLabel("")
        self.name_preview.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.label_input = QtWidgets.QComboBox()
        self.label_input.setEditable(True)
        self.label_input.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.label_input.setDuplicatesEnabled(False)
        self.label_input.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        if self.label_input.lineEdit():
            self.label_input.lineEdit().setPlaceholderText("main")
        self.refresh_label_btn = QtWidgets.QToolButton()
        self.refresh_label_btn.setToolTip("Refresh Labels")
        self.refresh_label_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload)
        )
        self.refresh_label_btn.setAutoRaise(True)
        self.refresh_label_btn.setFixedSize(22, 22)
        self.refresh_label_btn.setStyleSheet(
            "QToolButton {"
            "border: 1px solid #4a4a4a;"
            "border-radius: 11px;"
            "padding: 0px;"
            "}"
            "QToolButton:hover {"
            "background-color: rgba(255, 255, 255, 28);"
            "}"
        )
        label_wrap = QtWidgets.QHBoxLayout()
        label_wrap.addWidget(self.label_input, 1)
        label_wrap.addWidget(self.refresh_label_btn)
        label_widget = QtWidgets.QWidget()
        label_widget.setLayout(label_wrap)

        self.resolution_combo = QtWidgets.QComboBox()
        for label, value in self.RESOLUTION_PRESETS:
            self.resolution_combo.addItem(label, value)
        camera_res_index = self.resolution_combo.findData("camera")
        if camera_res_index >= 0:
            self.resolution_combo.setCurrentIndex(camera_res_index)
        self.res_width = QtWidgets.QSpinBox()
        self.res_width.setRange(64, 16384)
        self.res_width.setValue(1920)
        self.res_height = QtWidgets.QSpinBox()
        self.res_height.setRange(64, 16384)
        self.res_height.setValue(1080)
        res_custom_wrap = QtWidgets.QHBoxLayout()
        res_custom_wrap.addWidget(self.res_width)
        res_custom_wrap.addWidget(QtWidgets.QLabel("x"))
        res_custom_wrap.addWidget(self.res_height)
        res_custom_wrap.addStretch(1)
        res_custom_widget = QtWidgets.QWidget()
        res_custom_widget.setLayout(res_custom_wrap)

        self.aa_combo = QtWidgets.QComboBox()
        for label, value in self.ANTIALIAS_PRESETS:
            self.aa_combo.addItem(label, value)

        self.camera_combo = QtWidgets.QComboBox()
        self.refresh_camera_btn = QtWidgets.QPushButton("Refresh Cameras")
        self.lock_camera_checkbox = QtWidgets.QCheckBox("Lock Camera")
        self.lock_camera_checkbox.setChecked(True)
        camera_wrap = QtWidgets.QHBoxLayout()
        camera_wrap.addWidget(self.camera_combo, 1)
        camera_wrap.addWidget(self.refresh_camera_btn)
        camera_wrap.addWidget(self.lock_camera_checkbox)
        camera_widget = QtWidgets.QWidget()
        camera_widget.setLayout(camera_wrap)

        self.burnin_preset_combo = QtWidgets.QComboBox()
        for label, value in self.BURNIN_PRESETS:
            self.burnin_preset_combo.addItem(label, value)
        auto_index = self.burnin_preset_combo.findData("auto")
        if auto_index >= 0:
            self.burnin_preset_combo.setCurrentIndex(auto_index)
        self.output_profile_combo = QtWidgets.QComboBox()
        for label, value in self.OUTPUT_PROFILES:
            self.output_profile_combo.addItem(label, value)

        self.intent_input = QtWidgets.QLineEdit()
        self.intent_input.setPlaceholderText("Optional intent")
        self.comment_input = QtWidgets.QPlainTextEdit()
        self.comment_input.setPlaceholderText("Comment")
        self.comment_input.setMaximumHeight(56)

        start, end = self.get_frame_range()
        self.range_label = QtWidgets.QLabel("Range: {} - {}".format(start, end))
        self.override_range_btn = QtWidgets.QPushButton("Override")
        self.override_range_btn.setCheckable(True)
        self.override_range_btn.setFixedHeight(22)
        self.override_range_btn.setMinimumWidth(74)

        self.range_start_input = QtWidgets.QSpinBox()
        self.range_start_input.setRange(-10000000, 10000000)
        self.range_start_input.setValue(start)
        self.range_start_input.setEnabled(False)

        self.range_end_input = QtWidgets.QSpinBox()
        self.range_end_input.setRange(-10000000, 10000000)
        self.range_end_input.setValue(end)
        self.range_end_input.setEnabled(False)

        range_wrap = QtWidgets.QHBoxLayout()
        range_wrap.addWidget(self.range_label, 1)
        range_wrap.addWidget(self.override_range_btn)
        range_wrap.addWidget(self.range_start_input)
        range_wrap.addWidget(QtWidgets.QLabel("-"))
        range_wrap.addWidget(self.range_end_input)
        range_widget = QtWidgets.QWidget()
        range_widget.setLayout(range_wrap)

        form.addRow("Product Name:", self.name_preview)
        form.addRow("Label:", label_widget)
        form.addRow("Resolution:", self.resolution_combo)
        form.addRow("Custom Resolution:", res_custom_widget)
        form.addRow("Anti Alias:", self.aa_combo)
        form.addRow("Camera:", camera_widget)
        form.addRow("Burnin Preset:", self.burnin_preset_combo)
        form.addRow("Output Profile:", self.output_profile_combo)
        form.addRow("Intent:", self.intent_input)
        form.addRow("Comment:", self.comment_input)
        form.addRow("Frame Range:", range_widget)

        self.btn_flip = QtWidgets.QPushButton("Flipbook")
        self.btn_publish_direct = QtWidgets.QPushButton("Publish")
        self.btn_publish_deadline = QtWidgets.QPushButton("Publish Deadline")
        self.btn_publish_direct.setEnabled(False)
        self.btn_publish_deadline.setEnabled(False)
        for button in (
            self.btn_flip,
            self.btn_publish_direct,
            self.btn_publish_deadline,
        ):
            button.setFixedHeight(24)

        actions_layout = QtWidgets.QHBoxLayout()
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.btn_flip, 1)
        actions_layout.addWidget(self.btn_publish_direct, 1)
        actions_layout.addWidget(self.btn_publish_deadline, 1)
        layout.addLayout(actions_layout)

        progress_layout = QtWidgets.QHBoxLayout()
        self.progress_label = QtWidgets.QLabel("Idle")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_cancel_btn = QtWidgets.QPushButton("Cancel")
        self.progress_cancel_btn.setEnabled(False)
        self.progress_cancel_btn.setFixedHeight(22)
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        progress_layout.addWidget(self.progress_cancel_btn)
        layout.addLayout(progress_layout)

        quick_layout = QtWidgets.QHBoxLayout()
        self.btn_open_folder = QtWidgets.QPushButton("Open Folder")
        self.btn_copy_path = QtWidgets.QPushButton("Copy Path")
        self.btn_open_ayon = QtWidgets.QPushButton("Open AYON")
        self.btn_retry_upload = QtWidgets.QPushButton("Retry Upload")
        for button in (
            self.btn_open_folder,
            self.btn_copy_path,
            self.btn_open_ayon,
            self.btn_retry_upload,
        ):
            button.setEnabled(False)
            button.setFixedHeight(22)
        quick_layout.addWidget(self.btn_open_folder)
        quick_layout.addWidget(self.btn_copy_path)
        quick_layout.addWidget(self.btn_open_ayon)
        quick_layout.addWidget(self.btn_retry_upload)
        layout.addLayout(quick_layout)

        self.label_input.currentTextChanged.connect(self._update_name_preview)
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        self.refresh_label_btn.clicked.connect(self._refresh_label_dropdown)
        self.refresh_camera_btn.clicked.connect(self.refresh_camera_list)
        self.override_range_btn.toggled.connect(self._on_override_range_toggled)
        self.range_start_input.valueChanged.connect(self._on_override_range_changed)
        self.range_end_input.valueChanged.connect(self._on_override_range_changed)
        self.burnin_preset_combo.currentIndexChanged.connect(
            self._on_burnin_preset_changed
        )
        self.btn_flip.clicked.connect(self.do_flipbook)
        self.btn_publish_direct.clicked.connect(self.do_publish_direct)
        self.btn_publish_deadline.clicked.connect(self.do_publish_deadline)
        self.progress_cancel_btn.clicked.connect(self._request_cancel)
        self.btn_open_folder.clicked.connect(self._open_last_folder)
        self.btn_copy_path.clicked.connect(self._copy_last_folder)
        self.btn_open_ayon.clicked.connect(self._open_last_publish_in_ayon)
        self.btn_retry_upload.clicked.connect(self.do_retry_upload)

    def _on_override_range_toggled(self, state):
        enabled = bool(state)
        self.range_start_input.setEnabled(enabled)
        self.range_end_input.setEnabled(enabled)
        if enabled:
            start, end = self.get_frame_range()
            self.range_start_input.setValue(start)
            self.range_end_input.setValue(end)
        self._sync_range_from_timeline()

    def _on_override_range_changed(self, _value):
        if self.override_range_btn.isChecked():
            self._sync_range_from_timeline()

    def _sync_range_from_timeline(self):
        if self.override_range_btn.isChecked():
            start, end = self._selected_frame_range()
            self.range_label.setText("Range: {} - {}".format(start, end))
            return

        start, end = self.get_frame_range()
        self.range_label.setText("Range: {} - {}".format(start, end))
        self.range_start_input.setValue(start)
        self.range_end_input.setValue(end)

    def _selected_frame_range(self):
        if not self.override_range_btn.isChecked():
            return self.get_frame_range()

        start = int(self.range_start_input.value())
        end = int(self.range_end_input.value())
        if end < start:
            start, end = end, start
        return start, end

    def _on_burnin_preset_changed(self, _index):
        # Reserved for future per-preset UI state changes.
        return

    def _default_burnin_preset(self):
        task_name = (self._get_ayon_context().get("task_name") or "").strip().lower()
        if task_name in {"fx", "simulation"}:
            return "detailed"
        if task_name in {"lighting", "lookdev"}:
            return "compact"
        if task_name in {"comp", "compositing"}:
            return "minimal"
        return "compact"

    def _resolved_burnin_preset(self):
        value = self.burnin_preset_combo.currentData()
        if value == "auto":
            return self._default_burnin_preset()
        return value or "compact"

    def _pump_events(self):
        app = QtWidgets.QApplication.instance()
        if app:
            app.processEvents()

    def _set_progress_idle(self):
        self._active_operation = None
        self._cancel_requested = False
        self.progress_label.setText("Idle")
        self.progress_bar.setValue(0)
        self.progress_cancel_btn.setEnabled(False)
        self._pump_events()

    def _start_progress(self, operation_name):
        self._active_operation = operation_name
        self._cancel_requested = False
        self.progress_label.setText(str(operation_name))
        self.progress_bar.setValue(0)
        self.progress_cancel_btn.setEnabled(True)
        self._pump_events()

    def _set_progress(self, percent, label=None):
        if label:
            self.progress_label.setText(str(label))
        self.progress_bar.setValue(max(0, min(100, int(percent))))
        self._pump_events()

    def _request_cancel(self):
        if not self._active_operation:
            return
        self._cancel_requested = True
        self.progress_label.setText("Cancelling...")
        self._pump_events()

    def _check_cancelled(self):
        if self._cancel_requested:
            raise RuntimeError("Operation cancelled by user.")

    def _reset_publish_button_colors(self):
        self.btn_publish_direct.setStyleSheet(self.DEFAULT_BUTTON_STYLE)
        self.btn_publish_deadline.setStyleSheet(self.DEFAULT_BUTTON_STYLE)

    def _set_direct_publish_state(self, success=True):
        if success:
            self.btn_publish_direct.setStyleSheet(self.DIRECT_OK_BUTTON_STYLE)
        else:
            self.btn_publish_direct.setStyleSheet(self.WARNING_BUTTON_STYLE)

    def _set_deadline_publish_state(self, success=True):
        if success:
            self.btn_publish_deadline.setStyleSheet(self.DEADLINE_OK_BUTTON_STYLE)
        else:
            self.btn_publish_deadline.setStyleSheet(self.WARNING_BUTTON_STYLE)

    def _set_quick_actions_enabled(
        self,
        folder_enabled=False,
        ayon_enabled=False,
        retry_enabled=False,
    ):
        self.btn_open_folder.setEnabled(folder_enabled)
        self.btn_copy_path.setEnabled(folder_enabled)
        self.btn_open_ayon.setEnabled(ayon_enabled)
        self.btn_retry_upload.setEnabled(retry_enabled)

    def _on_resolution_changed(self):
        value = self.resolution_combo.currentData()
        is_custom = value == "custom"
        self.res_width.setEnabled(is_custom)
        self.res_height.setEnabled(is_custom)

    # --------------------------------------------------
    # Context / Naming
    # --------------------------------------------------
    def _get_ayon_context(self):
        context = {}
        try:
            from ayon_core.pipeline import get_current_context
            context = get_current_context() or {}
        except Exception:
            context = {}

        project_name = context.get("project_name") or os.getenv("AYON_PROJECT_NAME")
        folder_path = context.get("folder_path") or os.getenv("AYON_FOLDER_PATH")
        task_name = context.get("task_name") or os.getenv("AYON_TASK_NAME")
        author = os.getenv("AYON_USERNAME") or os.getenv("USER") or "artist"

        return {
            "project_name": project_name,
            "folder_path": folder_path,
            "task_name": task_name,
            "author": author,
        }

    def _get_shot_and_task_tokens(self):
        context = self._get_ayon_context()
        folder_path = context.get("folder_path") or ""
        task_name = context.get("task_name") or "task"

        shot_name = ""
        folder_parts = [part for part in folder_path.split("/") if part]
        if folder_parts:
            shot_name = folder_parts[-1]
        if not shot_name:
            shot_name = os.path.splitext(hou.hipFile.basename())[0] or "shot"

        return _sanitize_token(shot_name, "shot"), _sanitize_token(task_name, "task")

    def _label_prefix(self):
        shot_name, task_name = self._get_shot_and_task_tokens()
        return "P_{}_{}_".format(shot_name, task_name)

    def _collect_db_labels(self):
        context = self._get_ayon_context()
        project_name = context.get("project_name")
        folder_path = context.get("folder_path")
        if not project_name or not folder_path:
            return []

        try:
            from ayon_api import get_folder_by_path, get_products

            folder_entity = get_folder_by_path(
                project_name=project_name,
                folder_path=folder_path,
                fields={"id"},
            )
            if not folder_entity:
                return []

            products = get_products(
                project_name=project_name,
                folder_ids=[folder_entity["id"]],
                product_types=["review"],
            )
        except Exception:
            log.warning("Could not query AYON labels for flipbook.", exc_info=True)
            return []

        prefix = self._label_prefix().lower()
        labels = set()
        for product in products:
            product_name = str(product.get("name") or "")
            if not product_name:
                continue
            if not product_name.lower().startswith(prefix):
                continue
            label = product_name[len(prefix):]
            label = _TRAILING_VERSION_PATTERN.sub("", label)
            if label:
                labels.add(label)

        return sorted(labels, key=lambda item: item.lower())

    def _set_label_text(self, value):
        text = str(value or "").strip()
        if not text:
            text = "main"
        current_index = self.label_input.findText(text)
        if current_index >= 0:
            self.label_input.setCurrentIndex(current_index)
        else:
            self.label_input.setEditText(text)

    def _refresh_label_dropdown(self):
        current_text = self.label_input.currentText().strip()
        labels = self._collect_db_labels()
        if "main" not in labels:
            labels.insert(0, "main")

        self.label_input.blockSignals(True)
        self.label_input.clear()
        self.label_input.addItems(labels)
        self._set_label_text(current_text or "main")
        self.label_input.blockSignals(False)
        self._version_preview_cache.clear()
        self._update_name_preview()

    def build_product_name(self):
        label = _sanitize_token(self.label_input.currentText(), "main")
        return "{}{}".format(self._label_prefix(), label)

    def _resolve_preview_version(self, product_name):
        cached = self._version_preview_cache.get(product_name)
        if cached is not None:
            return int(cached)

        version_number = self._next_ayon_version(product_name)
        if version_number is None:
            hip_path = hou.hipFile.path()
            hip_dir = os.path.dirname(hip_path) if hip_path else ""
            product_root = os.path.join(hip_dir, "flipbook", product_name)
            version_number = self._next_local_version(product_root)

        self._version_preview_cache[product_name] = int(version_number)
        return int(version_number)

    def _update_name_preview(self):
        product_name = self.build_product_name()
        preview_version = self._resolve_preview_version(product_name)
        self.name_preview.setText(
            "{}_v{:03d}".format(product_name, int(preview_version))
        )

    # --------------------------------------------------
    # Camera
    # --------------------------------------------------
    def _find_scene_cameras(self):
        output = []
        obj = hou.node("/obj")
        if not obj:
            return output
        for node in obj.allSubChildren():
            try:
                if node.type().category() != hou.objNodeTypeCategory():
                    continue
                if node.type().name() != "cam":
                    continue
            except Exception:
                continue
            output.append(node)
        output.sort(key=lambda item: item.path().lower())
        return output

    def refresh_camera_list(self):
        current_value = self.camera_combo.currentData()
        self.camera_combo.clear()
        viewport_camera = None
        viewport_camera_path = None
        try:
            if self.scene_viewer:
                viewport_camera = self._get_viewport_camera(
                    self.scene_viewer.curViewport()
                )
        except Exception:
            viewport_camera = None

        viewport_label = (
            viewport_camera.path()
            if viewport_camera
            else "<Viewport: Perspective>"
        )
        if viewport_camera:
            viewport_camera_path = viewport_camera.path()
        self.camera_combo.addItem(viewport_label, self.VIEWPORT_CAMERA_TOKEN)
        for camera in self._find_scene_cameras():
            if viewport_camera_path and camera.path() == viewport_camera_path:
                continue
            self.camera_combo.addItem(camera.path(), camera.path())

        if current_value and current_value != self.VIEWPORT_CAMERA_TOKEN:
            index = self.camera_combo.findData(current_value)
            if index >= 0:
                self.camera_combo.setCurrentIndex(index)
                return
            if viewport_camera_path and current_value == viewport_camera_path:
                self.camera_combo.setCurrentIndex(0)
                return
        self.camera_combo.setCurrentIndex(0)

    def _apply_selected_camera(self, viewport):
        if not self.lock_camera_checkbox.isChecked():
            return self._get_viewport_camera(viewport)

        camera_path = self.camera_combo.currentData()
        if camera_path == self.VIEWPORT_CAMERA_TOKEN or not camera_path:
            return self._get_viewport_camera(viewport)

        cam = hou.node(camera_path)
        if not cam:
            raise RuntimeError("Selected camera not found: {}".format(camera_path))
        if hasattr(viewport, "setCamera"):
            viewport.setCamera(cam)
        return cam

    def _get_viewport_camera(self, viewport):
        getter = getattr(viewport, "camera", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _get_camera_resolution(self, camera_node):
        if not camera_node:
            return None
        resx_parm = camera_node.parm("resx")
        resy_parm = camera_node.parm("resy")
        if not resx_parm or not resy_parm:
            return None

        try:
            width = int(resx_parm.eval())
            height = int(resy_parm.eval())
        except Exception:
            return None

        if width <= 0 or height <= 0:
            return None
        return width, height

    # --------------------------------------------------
    # Flipbook options
    # --------------------------------------------------
    def get_frame_range(self):
        start = int(hou.playbar.frameRange()[0])
        end = int(hou.playbar.frameRange()[1])
        return start, end

    def _get_resolution(self, viewport=None):
        value = self.resolution_combo.currentData()
        if value == "camera":
            cam = self._get_viewport_camera(viewport) if viewport else None
            if not cam:
                camera_path = self.camera_combo.currentData()
                if camera_path:
                    cam = hou.node(camera_path)
            return self._get_camera_resolution(cam)

        if value is None:
            return None
        if value == "custom":
            return int(self.res_width.value()), int(self.res_height.value())
        return value

    def _apply_resolution(self, settings, viewport=None):
        resolution = self._get_resolution(viewport=viewport)
        if resolution is None:
            if hasattr(settings, "useResolution"):
                settings.useResolution(False)
            return

        if hasattr(settings, "useResolution"):
            settings.useResolution(True)
        if hasattr(settings, "resolution"):
            settings.resolution(resolution)

    def _resolve_antialias_enum(self, mode_key):
        enum_obj = getattr(hou, "flipbookAntialias", None)
        if enum_obj is None or mode_key == "viewport":
            return None

        candidate_map = {
            "off": ["off", "none", "disabled", "noantialiasing", "0"],
            "2x": ["2x", "x2", "2", "low", "fast"],
            "4x": ["4x", "x4", "4", "medium", "normal"],
            "8x": ["8x", "x8", "8", "high", "good"],
            "16x": ["16x", "x16", "16", "veryhigh", "best", "production"],
            "32x": ["32x", "x32", "32", "extreme", "best", "production"],
            "64x": ["64x", "x64", "64", "ultra", "ultraHD"],
        }
        attrs = {
            attr.lower(): attr
            for attr in dir(enum_obj)
            if not attr.startswith("_")
        }
        for candidate in candidate_map.get(mode_key, []):
            attr_name = attrs.get(candidate.lower())
            if attr_name:
                return getattr(enum_obj, attr_name)
        return None

    def _apply_antialias(self, settings):
        mode_key = self.aa_combo.currentData()
        if mode_key == "viewport":
            return

        value = self._resolve_antialias_enum(mode_key)
        if value is None:
            try:
                value = int(str(mode_key).rstrip("x"))
            except Exception:
                value = None

        for method_name in (
            "antialias",
            "antialiasing",
            "setAntialias",
            "setAntialiasing",
        ):
            method = getattr(settings, method_name, None)
            if not callable(method):
                continue

            for arg in (value, int(value) if value is not None else None):
                if arg is None:
                    continue
                try:
                    method(arg)
                    return
                except Exception:
                    continue

            if mode_key == "off":
                for arg in (0, False):
                    try:
                        method(arg)
                        return
                    except Exception:
                        continue

    # --------------------------------------------------
    # Output path
    # --------------------------------------------------
    def _list_local_versions(self, product_root):
        if not os.path.isdir(product_root):
            return []

        versions = []
        for name in os.listdir(product_root):
            match = _VERSION_DIR_PATTERN.match(name)
            if not match:
                continue
            versions.append(int(match.group(1)))
        versions.sort()
        return versions

    def _next_local_version(self, product_root):
        versions = self._list_local_versions(product_root)
        if not versions:
            return 1
        return int(versions[-1]) + 1

    def _next_ayon_version(self, product_name):
        context = self._get_ayon_context()
        project_name = context.get("project_name")
        folder_path = context.get("folder_path")
        if not project_name or not folder_path:
            return None

        try:
            from ayon_api import (
                get_folder_by_path,
                get_last_version_by_product_id,
                get_product_by_name,
            )

            folder_entity = get_folder_by_path(
                project_name=project_name,
                folder_path=folder_path,
            )
            if not folder_entity:
                return None

            product_entity = get_product_by_name(
                project_name=project_name,
                product_name=product_name,
                folder_id=folder_entity["id"],
            )
            if not product_entity:
                return 1

            last_version = get_last_version_by_product_id(
                project_name=project_name,
                product_id=product_entity["id"],
            )
            if not last_version:
                return 1

            return int(last_version["version"]) + 1
        except Exception:
            log.warning(
                "Could not resolve next AYON version for flipbook path.",
                exc_info=True,
            )
            return None

    def _resolve_flipbook_version(self, product_name, product_root):
        local_next = self._next_local_version(product_root)
        ayon_next = self._next_ayon_version(product_name)
        if ayon_next is None:
            return local_next
        return max(int(ayon_next), int(local_next))

    def _save_hip_to_version_dir(
        self,
        version_dir,
        version_number,
    ):
        original_path = hou.hipFile.path()
        if not original_path or original_path == "untitled.hip":
            raise RuntimeError("Please save scene before running.")

        hip_dir = os.path.join(version_dir, "hip")
        os.makedirs(hip_dir, exist_ok=True)
        dst = os.path.join(hip_dir, "source.v{:03d}.hip".format(int(version_number)))
        try:
            hscript_dst = dst.replace("\\", "/").replace('"', '\\"')
            hou.hscript('mwrite -n "{}"'.format(hscript_dst))
        except hou.OperationFailed as exc:
            raise RuntimeError(
                "Failed to save snapshot HIP to {}: {}".format(dst, exc)
            ) from exc
        return dst

    def get_output_path(self, product_name, version_number=None):
        hip_path = hou.hipFile.path()
        hip_dir = os.path.dirname(hip_path)
        product_root = os.path.join(hip_dir, "flipbook", product_name)
        if version_number is None:
            version_number = self._resolve_flipbook_version(product_name, product_root)
        version_number = int(version_number)
        version_token = "v{:03d}".format(version_number)
        folder = os.path.join(product_root, version_token)
        os.makedirs(folder, exist_ok=True)
        return folder, version_number

    def _assert_folder_writable(self, folder_path):
        os.makedirs(folder_path, exist_ok=True)
        test_path = os.path.join(folder_path, ".ayon_write_test.tmp")
        with open(test_path, "w") as stream:
            stream.write("ok")
        if os.path.exists(test_path):
            os.remove(test_path)

    def _run_flipbook_preflight(self, product_name, version_number):
        start, end = self._selected_frame_range()
        if end < start:
            raise RuntimeError("Frame range is invalid.")

        if self.lock_camera_checkbox.isChecked():
            selected = self.camera_combo.currentData()
            viewport = self.scene_viewer.curViewport() if self.scene_viewer else None
            if selected and selected != self.VIEWPORT_CAMERA_TOKEN:
                if not hou.node(selected):
                    raise RuntimeError(
                        "Selected camera does not exist: {}".format(selected)
                    )
            else:
                cam = self._get_viewport_camera(viewport) if viewport else None
                if cam is None:
                    raise RuntimeError(
                        "Viewport is not locked to a camera. "
                        "Choose a camera or disable 'Lock Camera'."
                    )

        folder, _ = self.get_output_path(
            product_name=product_name,
            version_number=version_number,
        )
        self._assert_folder_writable(folder)

    def _run_publish_preflight(self, for_deadline=False):
        if not self.last_flipbook:
            raise RuntimeError("Run flipbook first.")

        if self._selected_frame_range()[1] < self._selected_frame_range()[0]:
            raise RuntimeError("Frame range is invalid.")

        if not os.path.isdir(self.last_flipbook["folder"]):
            raise RuntimeError("Flipbook output folder does not exist.")

        preview_file = self.last_flipbook["pattern"].replace("$F4", str(
            int(self.last_flipbook["frame_start"])
        ).zfill(4))
        if not os.path.exists(preview_file):
            raise RuntimeError(
                "Flipbook frames are missing. Run flipbook again before publish."
            )

        profile = self.output_profile_combo.currentData()
        if profile != "sequence_only":
            try:
                get_ffmpeg_tool_args("ffmpeg")
            except Exception as exc:
                raise RuntimeError(
                    "FFmpeg could not be resolved through AYON."
                ) from exc
        if for_deadline and not shutil.which("deadlinecommand"):
            raise RuntimeError("deadlinecommand was not found in PATH.")

    def _build_publish_options(self):
        comment = self.comment_input.toPlainText().strip()
        intent = self.intent_input.text().strip()
        version_data = {}
        version_attrib = {}
        if comment:
            version_data["comment"] = comment
            # AYON UIs commonly display version description from attrib.
            version_attrib["description"] = comment
        if intent:
            version_data["intent"] = intent

        output_profile = self.output_profile_combo.currentData()
        burnin_preset = self._resolved_burnin_preset()
        burnin_mode = None
        encode_profile = "hq"
        create_online_reviewable = True
        if output_profile == "fast":
            encode_profile = "fast"
        elif output_profile == "noburnin":
            burnin_mode = "none"
        elif output_profile == "sequence_only":
            create_online_reviewable = False

        if burnin_preset == "off":
            burnin_mode = "none"

        return {
            "version_data": version_data or None,
            "version_attrib": version_attrib or None,
            "create_online_reviewable": create_online_reviewable,
            "burnin_mode": burnin_mode,
            "encode_profile": encode_profile,
            "burnin_preset": burnin_preset,
            "allow_version_fallback": True,
        }

    # --------------------------------------------------
    # Flipbook
    # --------------------------------------------------
    def do_flipbook(self):
        if not self.scene_viewer:
            log.error("Flipbook aborted: no Scene Viewer found.")
            return

        try:
            self._reset_publish_button_colors()
            self._set_quick_actions_enabled(
                folder_enabled=False,
                ayon_enabled=False,
                retry_enabled=False,
            )
            self.last_publish_result = None
            self._last_upload_retry_payload = None

            self._start_progress("Flipbook preflight")
            product_name = self.build_product_name()
            preview_version = self._resolve_preview_version(product_name)
            self._run_flipbook_preflight(
                product_name=product_name,
                version_number=preview_version,
            )
            self._check_cancelled()
            self._set_progress(20, "Preparing output")

            folder, local_version = self.get_output_path(
                product_name,
                version_number=preview_version,
            )
            hip_snapshot = self._save_hip_to_version_dir(
                folder,
                local_version,
            )
            pattern = os.path.join(folder, "{}.$F4.jpg".format(product_name))
            start, end = self._selected_frame_range()
            self.range_label.setText("Range: {} - {}".format(start, end))

            settings = self.scene_viewer.flipbookSettings().stash()
            settings.frameRange((start, end))
            settings.output(pattern)
            settings.outputToMPlay(True)
            self._apply_antialias(settings)

            self._check_cancelled()
            self._set_progress(55, "Rendering flipbook")
            viewport = self.scene_viewer.curViewport()
            self._apply_selected_camera(viewport)
            self._apply_resolution(settings, viewport=viewport)
            self.scene_viewer.flipbook(viewport, settings)

            self.last_flipbook = {
                "product_name": product_name,
                "folder": folder,
                "local_version": local_version,
                "pattern": pattern,
                "hip_snapshot": hip_snapshot,
                "frame_start": start,
                "frame_end": end,
            }
            self.btn_publish_direct.setEnabled(True)
            self.btn_publish_deadline.setEnabled(True)
            self._set_quick_actions_enabled(
                folder_enabled=True,
                ayon_enabled=False,
                retry_enabled=False,
            )
            self._sync_range_from_timeline()
            self._set_progress(100, "Flipbook complete")

        except Exception as exc:
            log.exception("Flipbook error: %s", exc)
            self._set_progress(0, "Flipbook failed")
        finally:
            self._set_progress_idle()

    # --------------------------------------------------
    # Publish payload
    # --------------------------------------------------
    def _build_publish_payload(self):
        if not self.last_flipbook:
            raise RuntimeError("Run flipbook first.")

        context = self._get_ayon_context()
        payload = {
            "product_name": self.last_flipbook["product_name"],
            "path_or_paths": self.last_flipbook["pattern"],
            "frame_start": self.last_flipbook["frame_start"],
            "frame_end": self.last_flipbook["frame_end"],
            "representation_name": "jpg",
            "project_name": context.get("project_name"),
            "folder_path": context.get("folder_path"),
            "task_name": context.get("task_name"),
            "author": context.get("author"),
            "version": int(self.last_flipbook["local_version"]),
        }
        payload.update(self._build_publish_options())
        return payload

    # --------------------------------------------------
    # Direct publish
    # --------------------------------------------------
    def do_publish_direct(self):
        try:
            self._start_progress("Direct publish preflight")
            self._run_publish_preflight(for_deadline=False)
            self._check_cancelled()
            self._set_progress(30, "Publishing")
            from ayon_houdini.nodes import direct_publish

            payload = self._build_publish_payload()
            payload["raise_on_upload_error"] = False
            publish_result = direct_publish.publish_review_sequence(**payload)
            self.last_publish_result = publish_result
            self._version_preview_cache.pop(publish_result["product_name"], None)
            self._update_name_preview()

            if publish_result.get("upload_error"):
                self._last_upload_retry_payload = dict(publish_result)
                self._set_direct_publish_state(success=False)
                self._set_quick_actions_enabled(
                    folder_enabled=True,
                    ayon_enabled=True,
                    retry_enabled=True,
                )
                log.warning(
                    "Published version, but reviewable upload failed: %s",
                    publish_result["upload_error"],
                )
                self._set_progress(100, "Publish done (retry upload)")
            else:
                self._last_upload_retry_payload = None
                self._set_direct_publish_state(success=True)
                self._set_quick_actions_enabled(
                    folder_enabled=True,
                    ayon_enabled=True,
                    retry_enabled=False,
                )
                log.info(
                    "Published successfully: %s v%03d",
                    publish_result["product_name"],
                    int(publish_result["version"]),
                )
                self._set_progress(100, "Publish complete")

        except Exception as exc:
            log.exception("Direct publish failed: %s", exc)
            self._set_direct_publish_state(success=False)
            self._set_progress(0, "Publish failed")
        finally:
            self._set_progress_idle()

    def do_retry_upload(self):
        if not self._last_upload_retry_payload:
            log.warning("No failed upload found to retry.")
            return

        try:
            self._start_progress("Retrying upload")
            self._check_cancelled()
            from ayon_houdini.nodes import direct_publish

            retry_result = direct_publish.upload_online_reviewable_for_result(
                result=dict(self._last_upload_retry_payload),
                burnin_mode=self._build_publish_options().get("burnin_mode"),
                encode_profile=self._build_publish_options().get("encode_profile"),
                burnin_preset=self._build_publish_options().get("burnin_preset"),
                raise_on_error=False,
            )
            if retry_result.get("upload_error"):
                self._last_upload_retry_payload = dict(retry_result)
                self._set_direct_publish_state(success=False)
                self._set_quick_actions_enabled(
                    folder_enabled=True,
                    ayon_enabled=True,
                    retry_enabled=True,
                )
                log.warning(
                    "Retry upload failed: %s",
                    retry_result["upload_error"],
                )
                self._set_progress(0, "Retry failed")
                return

            self.last_publish_result = retry_result
            self._last_upload_retry_payload = None
            self._set_direct_publish_state(success=True)
            self._set_quick_actions_enabled(
                folder_enabled=True,
                ayon_enabled=True,
                retry_enabled=False,
            )
            log.info(
                "Retry upload succeeded: %s v%03d",
                retry_result["product_name"],
                int(retry_result["version"]),
            )
            self._set_progress(100, "Upload retry complete")

        except Exception as exc:
            log.exception("Retry upload failed: %s", exc)
            self._set_direct_publish_state(success=False)
            self._set_progress(0, "Retry failed")
        finally:
            self._set_progress_idle()

    def _open_last_folder(self):
        folder = ""
        if self.last_flipbook:
            folder = self.last_flipbook.get("folder") or ""
        if not folder:
            log.warning("No folder available.")
            return

        url = QtCore.QUrl.fromLocalFile(folder)
        if not QtGui.QDesktopServices.openUrl(url):
            log.warning("Could not open folder: %s", folder)

    def _copy_last_folder(self):
        folder = ""
        if self.last_flipbook:
            folder = self.last_flipbook.get("folder") or ""
        if not folder:
            log.warning("No folder available.")
            return
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard:
            clipboard.setText(folder)
            log.info("Copied folder path: %s", folder)

    def _open_last_publish_in_ayon(self):
        if not self.last_publish_result:
            log.warning("No publish result available.")
            return

        project_name = self.last_publish_result.get("project_name")
        if not project_name:
            log.warning("Cannot resolve project name for AYON URL.")
            return
        server_url = (
            os.getenv("AYON_SERVER_URL")
            or os.getenv("AYON_SERVER")
            or ""
        ).strip()
        if not server_url:
            log.warning("AYON server URL env is not set.")
            return
        url = "{}/projects/{}".format(server_url.rstrip("/"), project_name)
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            log.warning("Could not open AYON URL: %s", url)

    # --------------------------------------------------
    # Deadline publish
    # --------------------------------------------------
    def _collect_deadline_env(self):
        env = {}
        for key, value in os.environ.items():
            if not value:
                continue
            if key.startswith(("AYON_", "OPENPYPE_", "AVALON_")):
                env[key] = value
                continue
            if key in {
                "PYTHONPATH",
                "PATH",
                "HOUDINI_PATH",
                "HOUDINI_OTLSCAN_PATH",
                "HFS",
            }:
                env[key] = value
        return env

    def _build_deadline_script(self, script_path):
        script = r'''import importlib.util
import json
import os
import sys
import traceback


def _import_module_from_path(module_name, module_path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    payload_path = sys.argv[1]
    with open(payload_path, "r") as stream:
        payload = json.load(stream)

    for key, value in (payload.get("env") or {}).items():
        if value is not None:
            os.environ[str(key)] = str(value)

    direct_publish_path = payload["direct_publish_path"]
    direct_publish = _import_module_from_path(
        "ayon_direct_publish_job",
        direct_publish_path
    )

    kwargs = payload["publish_kwargs"]
    result = direct_publish.publish_review_sequence(**kwargs)
    print(
        "AYON flipbook publish done: {} v{:03d}".format(
            result["product_name"], int(result["version"])
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
'''
        with open(script_path, "w") as stream:
            stream.write(script)

    def _submit_deadline_publish(self, publish_kwargs):
        deadline_command = shutil.which("deadlinecommand")
        if not deadline_command:
            raise RuntimeError("deadlinecommand was not found in PATH.")

        script_dir = os.path.join(self.last_flipbook["folder"], ".deadline_publish")
        os.makedirs(script_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        direct_publish_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "direct_publish.py")
        )
        payload_path = os.path.join(script_dir, "publish_{}.json".format(stamp))
        script_path = os.path.join(script_dir, "run_publish_{}.py".format(stamp))

        payload = {
            "direct_publish_path": direct_publish_path,
            "publish_kwargs": publish_kwargs,
            "env": self._collect_deadline_env(),
        }
        with open(payload_path, "w") as stream:
            json.dump(payload, stream, indent=4)
        self._build_deadline_script(script_path)

        context = self._get_ayon_context()
        folder_path = (context.get("folder_path") or "").strip("/")
        shot_name = folder_path.split("/")[-1] if folder_path else "shot"
        task_name = context.get("task_name") or "task"
        batch_name = "Flipbook Publish | {} | {} | {}".format(
            context.get("project_name") or "project",
            shot_name,
            task_name,
        )

        job_info = {
            "Plugin": "CommandLine",
            "Name": "AYON Flipbook Publish | {}".format(
                publish_kwargs["product_name"]
            ),
            "BatchName": batch_name,
            "Pool": "houdini",
            "Priority": 50,
            "Frames": "0-0",
            "ChunkSize": 1,
            "Comment": "Auto-submit from AYON Flipbook Tool",
        }

        env_index = 0
        for key, value in sorted(payload["env"].items()):
            if value is None:
                continue
            if "\n" in str(value):
                continue
            job_info["EnvironmentKeyValue{}".format(env_index)] = "{}={}".format(
                key, value
            )
            env_index += 1

        plugin_info = {
            "Executable": sys.executable,
            "Arguments": "\"{}\" \"{}\"".format(script_path, payload_path),
            "ExecuteInShell": "False",
        }

        job_file = tempfile.NamedTemporaryFile(delete=False, suffix="_job.info")
        plugin_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix="_plugin.info"
        )

        try:
            with open(job_file.name, "w") as stream:
                for key, value in job_info.items():
                    stream.write("{}={}\n".format(key, value))

            with open(plugin_file.name, "w") as stream:
                for key, value in plugin_info.items():
                    stream.write("{}={}\n".format(key, value))

            result = subprocess.check_output(
                [deadline_command, job_file.name, plugin_file.name],
                stderr=subprocess.STDOUT,
            ).decode()
            log.info("Deadline submit output:\n%s", result.strip())

            match = re.search(r"JobID=([\w\d]+)", result)
            if not match:
                raise RuntimeError(
                    "Deadline submit finished but JobID was not found.\n{}".format(
                        result.strip()
                    )
                )
            return match.group(1)

        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "Deadline submit failed:\n{}".format(
                    (exc.output or b"").decode().strip()
                )
            ) from exc
        finally:
            for path in (job_file.name, plugin_file.name):
                if os.path.exists(path):
                    os.remove(path)

    def do_publish_deadline(self):
        try:
            self._start_progress("Deadline submit preflight")
            self._run_publish_preflight(for_deadline=True)
            self._check_cancelled()
            self._set_progress(35, "Submitting to Deadline")
            publish_kwargs = self._build_publish_payload()
            job_id = self._submit_deadline_publish(publish_kwargs)
            log.info("Submitted to Deadline. JobID: %s", job_id)
            self._set_deadline_publish_state(success=True)
            self._set_quick_actions_enabled(
                folder_enabled=True,
                ayon_enabled=bool(self.last_publish_result),
                retry_enabled=bool(self._last_upload_retry_payload),
            )
            self._set_progress(100, "Sent to farm")
        except Exception as exc:
            log.exception("Deadline publish failed: %s", exc)
            self._set_deadline_publish_state(success=False)
            self._set_progress(0, "Deadline submit failed")
        finally:
            self._set_progress_idle()


# --------------------------------------------------
# Launch Safely
# --------------------------------------------------
def _close_existing_tool():
    if not hasattr(hou.session, "ayon_fb_tool"):
        return
    try:
        hou.session.ayon_fb_tool.close()
        hou.session.ayon_fb_tool.deleteLater()
    except Exception:
        pass


def launch():
    if hou.hipFile.path() == "untitled.hip":
        log.warning("Please save scene before running.")
        return

    _close_existing_tool()
    hou.session.ayon_fb_tool = FlipbookShelfTool(parent=hou.qt.mainWindow())
    hou.session.ayon_fb_tool.show()


launch()
