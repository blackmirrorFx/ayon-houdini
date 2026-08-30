"""Modern Qt interface for the BMFX APKG Shot Builder."""

from __future__ import annotations

from collections import defaultdict

from qtpy import QtCore, QtGui, QtWidgets


_WINDOWS = {}


STYLE = """
QDialog#BmfxShotBuilder {
    background: #171b22;
    color: #dce4ef;
}
QFrame#Header {
    background: #1d232d;
    border-bottom: 1px solid #343d4b;
}
QLabel#AppMark {
    background: #e6922e;
    border-radius: 8px;
    color: #171b22;
    font-size: 18px;
    font-weight: 800;
    padding: 7px 10px;
}
QLabel#Title {
    color: #f2f6fb;
    font-size: 21px;
    font-weight: 650;
}
QLabel#Subtitle, QLabel#Muted {
    color: #8693a5;
    font-size: 11px;
}
QLabel#StatusReady {
    background: #183b2c;
    border: 1px solid #2d7653;
    border-radius: 10px;
    color: #70d89c;
    font-weight: 650;
    padding: 4px 10px;
}
QLabel#StatusError {
    background: #48252a;
    border: 1px solid #88404a;
    border-radius: 10px;
    color: #ff8d99;
    font-weight: 650;
    padding: 4px 10px;
}
QFrame#Card {
    background: #202630;
    border: 1px solid #343d4b;
    border-radius: 7px;
}
QLabel#SectionNumber {
    background: #2b3441;
    border: 1px solid #4b5769;
    border-radius: 9px;
    color: #dce4ef;
    font-size: 10px;
    font-weight: 700;
    min-width: 18px;
    min-height: 18px;
    max-width: 18px;
    max-height: 18px;
}
QLabel#SectionTitle {
    color: #e9eef5;
    font-size: 12px;
    font-weight: 700;
}
QLabel#ContextKey {
    color: #8793a4;
    font-size: 11px;
}
QLabel#ContextValue {
    color: #e0e6ef;
    font-size: 12px;
    font-weight: 550;
}
QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
    background: #181d24;
    border: 1px solid #374151;
    border-radius: 5px;
    color: #dce4ef;
    selection-background-color: #356c51;
}
QLineEdit, QComboBox {
    min-height: 30px;
    padding: 0 9px;
}
QLineEdit:focus, QComboBox:focus, QTableWidget:focus {
    border-color: #57936f;
}
QComboBox::drop-down {
    border: 0;
    width: 24px;
}
QComboBox[tableEditor="true"] {
    background: #181d24;
    border: none;
    border-radius: 0;
    margin: 0;
    min-height: 0;
    padding: 0 4px 0 10px;
}
QComboBox[tableEditor="true"]::drop-down {
    border: none;
    width: 18px;
}
QComboBox[tableEditor="true"]:hover,
QComboBox[tableEditor="true"]:focus {
    background: #252d38;
    border: none;
}
QTableWidget {
    alternate-background-color: #1d232b;
    gridline-color: #303846;
    outline: 0;
}
QTableWidget::item {
    border-bottom: 1px solid #2c3440;
    padding: 7px;
}
QTableWidget::item:selected {
    background: #315b49;
}
QHeaderView::section {
    background: #252c37;
    border: 0;
    border-right: 1px solid #343d4b;
    border-bottom: 1px solid #3b4554;
    color: #9facbd;
    font-size: 10px;
    font-weight: 700;
    padding: 8px;
}
QScrollBar:vertical {
    background: #1a1f27;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #465162;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QPushButton {
    background: #2a323e;
    border: 1px solid #465162;
    border-radius: 5px;
    color: #dce4ef;
    font-weight: 600;
    min-height: 34px;
    padding: 0 15px;
}
QPushButton:hover {
    background: #343e4c;
    border-color: #617086;
}
QPushButton:pressed {
    background: #222a34;
}
QPushButton#Primary {
    background: #4d842a;
    border-color: #69a63d;
    color: white;
    min-width: 150px;
}
QPushButton#Primary:hover {
    background: #5a9632;
}
QPushButton#Danger {
    color: #ef9a9a;
}
QSplitter::handle {
    background: transparent;
    width: 7px;
    height: 7px;
}
QToolTip {
    background: #262e39;
    border: 1px solid #526075;
    color: #e8edf4;
    padding: 5px;
}
"""


def _card(number, title):
    frame = QtWidgets.QFrame()
    frame.setObjectName("Card")
    outer = QtWidgets.QVBoxLayout(frame)
    outer.setContentsMargins(14, 12, 14, 14)
    outer.setSpacing(10)
    heading = QtWidgets.QHBoxLayout()
    badge = QtWidgets.QLabel(str(number))
    badge.setObjectName("SectionNumber")
    badge.setAlignment(QtCore.Qt.AlignCenter)
    label = QtWidgets.QLabel(title.upper())
    label.setObjectName("SectionTitle")
    heading.addWidget(badge)
    heading.addWidget(label)
    heading.addStretch(1)
    outer.addLayout(heading)
    return frame, outer


class ShotBuilderDialog(QtWidgets.QDialog):
    HEADERS = (
        "Load", "Department", "APKG", "Version", "Status",
        "Dependency", "Publisher",
    )

    MODE_OPTIONS = (
        (
            "Required",
            "inherit",
            "Compose this APKG and publish its exact version as a required "
            "dependency.",
        ),
        (
            "Context only",
            "context",
            "Load for artist reference without publishing a dependency link.",
        ),
    )

    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self._records = ()
        self._rows = []
        self._context = {}
        self._folder = {}
        self._load_error = ""
        self.setObjectName("BmfxShotBuilder")
        self.setWindowTitle("BMFX Shot Builder")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.resize(1380, 820)
        self.setMinimumSize(1050, 650)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._load_catalog()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("Header")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(20, 13, 20, 13)
        mark = QtWidgets.QLabel("AP")
        mark.setObjectName("AppMark")
        mark.setAlignment(QtCore.Qt.AlignCenter)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(1)
        title = QtWidgets.QLabel("Shot Builder")
        title.setObjectName("Title")
        self.subtitle = QtWidgets.QLabel(self.node.path())
        self.subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        self.health = QtWidgets.QLabel("READY")
        self.health.setObjectName("StatusReady")
        header_layout.addWidget(mark)
        header_layout.addSpacing(8)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        header_layout.addWidget(self.health)
        root.addWidget(header)

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 10)
        body.setSpacing(12)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(12)
        context_card, context_layout = _card(1, "Context")
        self.context_grid = QtWidgets.QGridLayout()
        self.context_grid.setHorizontalSpacing(14)
        self.context_grid.setVerticalSpacing(9)
        self.context_values = {}
        for row, (key, label) in enumerate((
            ("project", "Project"),
            ("hierarchy", "Hierarchy"),
            ("shot", "Shot"),
            ("task", "Task (Department)"),
            ("mode", "Current Build"),
            ("count", "Loaded APKGs"),
        )):
            key_label = QtWidgets.QLabel(label)
            key_label.setObjectName("ContextKey")
            value_label = QtWidgets.QLabel("--")
            value_label.setObjectName("ContextValue")
            value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.context_grid.addWidget(key_label, row, 0)
            self.context_grid.addWidget(value_label, row, 1)
            self.context_values[key] = value_label
        self.context_grid.setColumnStretch(1, 1)
        context_layout.addLayout(self.context_grid)
        left.addWidget(context_card)

        selection_card, selection_layout = _card(2, "APKG Selection")
        filters = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search package, department or publisher...")
        self.department = QtWidgets.QComboBox()
        self.department.addItem("All departments", "")
        self.include_lighting = QtWidgets.QCheckBox(
            "Show downstream Lighting"
        )
        self.include_lighting.setToolTip(
            "Expose Lighting APKGs as a manual context-only exception. "
            "Other downstream departments remain hidden."
        )
        from ayon_houdini.nodes.lops import apkg
        locked_selection = apkg.selection(self.node)
        lighting_parm = self.node.parm("include_downstream_lighting")
        self.include_lighting.setChecked(
            (bool(lighting_parm.eval()) if lighting_parm is not None else False)
            or apkg.selection_uses_downstream_lighting(locked_selection)
        )
        self.merge_current = QtWidgets.QCheckBox("Merge Current Context")
        self.merge_current.setToolTip(
            "Load preferred APKGs from the current department as context "
            "only. They will not become publish dependencies."
        )
        merge_parm = self.node.parm("merge_current_department")
        self.merge_current.setChecked(
            (bool(merge_parm.eval()) if merge_parm is not None else False)
            or apkg.selection_uses_current_context(locked_selection)
        )
        filters.addWidget(self.search, 1)
        filters.addWidget(self.department)
        filters.addWidget(self.merge_current)
        filters.addWidget(self.include_lighting)
        selection_layout.addLayout(filters)

        self.table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setHorizontalScrollMode(
            QtWidgets.QAbstractItemView.ScrollPerPixel
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        selection_layout.addWidget(self.table, 1)
        left.addWidget(selection_card, 1)
        body.addLayout(left, 3)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(12)
        preview_card, preview_layout = _card(3, "Locked Build Preview")
        self.preview = QtWidgets.QTableWidget(0, 6)
        self.preview.setHorizontalHeaderLabels((
            "Department", "APKG", "Version", "Status", "Mode", "Path"
        ))
        self.preview.setAlternatingRowColors(True)
        self.preview.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.preview.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.preview.verticalHeader().setVisible(False)
        preview_layout.addWidget(self.preview)
        right.addWidget(preview_card, 3)

        info_card, info_layout = _card(4, "Detailed Information")
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(9)
        self.details.setFont(font)
        info_layout.addWidget(self.details)
        right.addWidget(info_card, 2)
        body.addLayout(right, 2)
        root.addLayout(body, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(14, 0, 14, 14)
        footer.setSpacing(8)
        self.clear_button = QtWidgets.QPushButton("Clear All")
        self.clear_button.setObjectName("Danger")
        self.refresh_button = QtWidgets.QPushButton("Refresh Catalog")
        self.rollback_button = QtWidgets.QPushButton("Rollback")
        self.rollback_button.setToolTip(
            "Restore the immediately previous exact APKG build lock."
        )
        self.remove_obsolete = QtWidgets.QCheckBox("Remove obsolete")
        self.remove_obsolete.setToolTip(
            "Delete disconnected Shot Builder payload nodes that no longer "
            "exist in this build. Leave off to preserve them muted."
        )
        remove_parm = self.node.parm("remove_obsolete_apkgs")
        self.remove_obsolete.setChecked(
            bool(remove_parm.eval()) if remove_parm is not None else False
        )
        close_button = QtWidgets.QPushButton("Close")
        self.auto_button = QtWidgets.QPushButton("Build Approved")
        self.build_button = QtWidgets.QPushButton("Build Selected")
        self.build_button.setObjectName("Primary")
        footer.addWidget(self.clear_button)
        footer.addWidget(self.refresh_button)
        footer.addWidget(self.rollback_button)
        footer.addWidget(self.remove_obsolete)
        footer.addStretch(1)
        footer.addWidget(close_button)
        footer.addWidget(self.auto_button)
        footer.addWidget(self.build_button)
        root.addLayout(footer)

        self.search.textChanged.connect(self._filter_rows)
        self.department.currentIndexChanged.connect(self._filter_rows)
        self.include_lighting.toggled.connect(
            self._toggle_downstream_lighting
        )
        self.merge_current.toggled.connect(self._toggle_merge_current)
        self.remove_obsolete.toggled.connect(self._toggle_remove_obsolete)
        self.clear_button.clicked.connect(self._clear)
        self.rollback_button.clicked.connect(self._rollback)
        self.refresh_button.clicked.connect(self._load_catalog)
        self.auto_button.clicked.connect(self._build_automatic)
        self.build_button.clicked.connect(self._build_selected)
        close_button.clicked.connect(self.close)

    def _set_health(self, text, error=False):
        self.health.setText(text.upper())
        self.health.setObjectName("StatusError" if error else "StatusReady")
        self.health.style().unpolish(self.health)
        self.health.style().polish(self.health)

    def _run_action(self, label, callback):
        try:
            self._set_health(label)
            QtWidgets.QApplication.processEvents()
            result = callback()
            self._set_health("Ready")
            return result
        except Exception as exc:
            self._set_health("Action Failed", error=True)
            QtWidgets.QMessageBox.critical(self, "BMFX Shot Builder", str(exc))
            return None

    def _load_catalog(self):
        from ayon_houdini.nodes.lops import apkg

        def load():
            self._context, self._folder, records = apkg.discover_for_node(
                self.node,
                merge_current=self.merge_current.isChecked(),
                include_lighting=self.include_lighting.isChecked(),
            )
            self._records = tuple(records)
            self._load_error = ""
            self.include_lighting.setVisible(
                apkg.resolve_department(self._context.get("task_name"))
                != "lighting"
            )
            self._populate_catalog()
            self._refresh_locked_preview()

        self._run_action("Refreshing", load)

    def _toggle_downstream_lighting(self, enabled):
        parm = self.node.parm("include_downstream_lighting")
        if parm is not None:
            parm.set(int(enabled))
        self._load_catalog()

    def _toggle_merge_current(self, enabled):
        parm = self.node.parm("merge_current_department")
        if parm is not None:
            parm.set(int(enabled))
        self._load_catalog()

    def _toggle_remove_obsolete(self, enabled):
        parm = self.node.parm("remove_obsolete_apkgs")
        if parm is not None:
            parm.set(int(enabled))

    def _selection_key(self, item):
        return item.get("productId") or "{}:{}:{}".format(
            item.get("department", ""), item.get("role", ""),
            item.get("slot", ""),
        )

    def _populate_catalog(self):
        from ayon_houdini.apkg.status import select_preferred_versions
        from ayon_houdini.nodes.lops import apkg

        locked_packages = apkg.selection(self.node).get("packages") or []
        locked_by_key = {
            self._selection_key(item): item for item in locked_packages
        }
        profile = apkg._profile(self.node, self._context["task_name"])
        automatic = set(profile["automatic"])
        current_department = str(
            apkg.resolve_department(self._context.get("task_name")) or
            self._context.get("task_name") or ""
        ).lower()
        if self.merge_current.isChecked() and current_department:
            automatic.add(current_department)
        preferred = select_preferred_versions(
            [record for record in self._records
             if record.department in automatic],
            apkg._status_priority(self.node),
        )

        grouped = defaultdict(list)
        for record in self._records:
            grouped[record.contribution_key].append(record)

        self.table.setRowCount(0)
        self._rows = []
        has_locked = bool(locked_packages)
        for key in sorted(
            grouped,
            key=lambda value: (
                apkg.department_index(grouped[value][0].department),
                (grouped[value][0].product_name or value).lower(),
                value,
            ),
        ):
            versions = sorted(
                grouped[key], key=lambda record: record.version, reverse=True
            )
            locked = locked_by_key.get(key)
            recommended = preferred.get(key)
            current = next((
                record for record in versions
                if locked and record.version_id == locked.get("versionId")
            ), None) or recommended or versions[0]

            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(check.flags() | QtCore.Qt.ItemIsUserCheckable)
            selected = bool(locked) if has_locked else bool(recommended)
            if (
                self.merge_current.isChecked()
                and current.department == current_department
                and recommended is not None
            ):
                selected = True
            check.setCheckState(
                QtCore.Qt.Checked if selected else QtCore.Qt.Unchecked
            )
            self.table.setItem(row, 0, check)

            for column, value in (
                (1, current.department.title()),
                (2, current.product_name or current.role),
                (4, current.status.title()),
                (6, current.author or "--"),
            ):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(
                    "Publisher: {}\nUSD: {}".format(
                        current.author or "--",
                        current.entrypoint_path or "--",
                    )
                )
                if column == 4:
                    color = {
                        "approved": "#6fd59a",
                        "available": "#e6b95c",
                        "pending": "#ef8d75",
                    }.get(str(current.status).lower(), "#b2bdca")
                    item.setForeground(QtGui.QColor(color))
                self.table.setItem(row, column, item)

            version_combo = QtWidgets.QComboBox()
            version_combo.setProperty("tableEditor", True)
            version_combo.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Expanding,
            )
            for record in versions:
                version_combo.addItem(
                    "v{:03d}".format(record.version), record.version_id
                )
            current_index = next((
                index for index, record in enumerate(versions)
                if record.version_id == current.version_id
            ), 0)
            version_combo.setCurrentIndex(current_index)
            version_combo.currentIndexChanged.connect(
                lambda _index, table_row=row: self._version_changed(table_row)
            )
            self.table.setCellWidget(row, 3, version_combo)

            locked_mode = str((locked or {}).get("mode") or "")
            if locked_mode == "optional":
                locked_mode = "inherit"
            mode_combo = QtWidgets.QComboBox()
            mode_combo.setProperty("tableEditor", True)
            mode_combo.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Expanding,
            )
            for mode_label, mode_value, tooltip in self.MODE_OPTIONS:
                mode_combo.addItem(mode_label, mode_value)
                mode_combo.setItemData(
                    mode_combo.count() - 1, tooltip, QtCore.Qt.ToolTipRole
                )
            dependency_mode = locked_mode if locked_mode in {
                "inherit", "context"
            } else (
                "context"
                if (
                    current.department in set(profile["optional"])
                    or (
                        self.merge_current.isChecked()
                        and current.department == current_department
                    )
                    or (
                        current.department == "lighting"
                        and apkg.resolve_department(
                            self._context.get("task_name")
                        ) != "lighting"
                    )
                )
                else "inherit"
            )
            mode_combo.setCurrentIndex(mode_combo.findData(dependency_mode))
            mode_combo.setToolTip(mode_combo.itemData(
                mode_combo.currentIndex(), QtCore.Qt.ToolTipRole
            ))
            mode_combo.currentIndexChanged.connect(
                lambda index, combo=mode_combo: combo.setToolTip(
                    combo.itemData(index, QtCore.Qt.ToolTipRole)
                )
            )
            self.table.setCellWidget(row, 5, mode_combo)
            self._rows.append((versions, check, version_combo, mode_combo))

        departments = apkg.sort_departments(
            record.department for record in self._records
        )
        current_department = self.department.currentData()
        self.department.blockSignals(True)
        self.department.clear()
        self.department.addItem("All departments", "")
        for value in departments:
            self.department.addItem(value.title(), value)
        index = self.department.findData(current_department)
        self.department.setCurrentIndex(max(index, 0))
        self.department.blockSignals(False)

        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(52)
        for column in range(len(self.HEADERS)):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.Fixed)
        for column, width in enumerate((52, 105, 210, 100, 135, 145, 115)):
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self._filter_rows()

    def _version_changed(self, row):
        versions, _check, combo, _mode = self._rows[row]
        version_id = combo.currentData()
        record = next(
            value for value in versions if value.version_id == version_id
        )
        self.table.item(row, 1).setText(record.department.title())
        self.table.item(row, 2).setText(record.product_name or record.role)
        self.table.item(row, 4).setText(record.status.title())
        status_color = {
            "approved": "#6fd59a",
            "available": "#e6b95c",
            "pending": "#ef8d75",
        }.get(str(record.status).lower(), "#b2bdca")
        self.table.item(row, 4).setForeground(QtGui.QColor(status_color))
        self.table.item(row, 6).setText(record.author or "--")

    def _filter_rows(self):
        search = self.search.text().strip().lower()
        department = self.department.currentData()
        for row, (versions, _check, combo, _mode) in enumerate(self._rows):
            version_id = combo.currentData()
            record = next((
                value for value in versions if value.version_id == version_id
            ), versions[0])
            haystack = " ".join(str(value or "") for value in (
                record.department, record.product_name, record.role,
                record.slot, record.author, record.entrypoint_path,
            )).lower()
            visible = (not search or search in haystack) and (
                not department or department == record.department
            )
            self.table.setRowHidden(row, not visible)

    def _selected_items(self):
        from ayon_houdini.nodes.lops import apkg

        output = []
        for order, (versions, check, combo, mode) in enumerate(self._rows, 1):
            if check.checkState() != QtCore.Qt.Checked:
                continue
            version_id = combo.currentData()
            record = next(
                value for value in versions if value.version_id == version_id
            )
            output.append(apkg._selection_item(
                record, mode=mode.currentData(), order=order * 10
            ))
        return output

    def _build_selected(self):
        from ayon_houdini.nodes.lops import apkg

        def build():
            items = apkg._resolve_required_dependencies(
                self._selected_items(), self._records
            )
            items = apkg._validate_selection(items, self._records)
            apkg._store_selection(
                self.node, self._context, self._folder["id"], items, "manual"
            )
            self._populate_catalog()
            self._refresh_locked_preview()

        self._run_action("Building", build)

    def _build_automatic(self):
        from ayon_houdini.nodes.lops import apkg

        def build():
            apkg.build_automatic(self.node)
            self._populate_catalog()
            self._refresh_locked_preview()

        self._run_action("Building", build)

    def _clear(self):
        from ayon_houdini.nodes.lops import apkg

        def clear():
            if apkg.clear_all(self.node):
                self._populate_catalog()
                self._refresh_locked_preview()

        self._run_action("Clearing", clear)

    def _rollback(self):
        from ayon_houdini.nodes.lops import apkg

        def rollback():
            apkg.rollback_build(self.node)
            self._populate_catalog()
            self._refresh_locked_preview()

        self._run_action("Rolling Back", rollback)

    def _refresh_locked_preview(self):
        from ayon_houdini.nodes.lops import apkg

        data = apkg.selection(self.node)
        packages = list(data.get("packages") or [])
        folder_path = self._context.get("folder_path") or data.get("folderPath") or ""
        path_parts = [value for value in folder_path.split("/") if value]
        values = {
            "project": self._context.get("project_name") or data.get("project") or "--",
            "hierarchy": "/".join(path_parts[:-1]) or "--",
            "shot": path_parts[-1] if path_parts else "--",
            "task": self._context.get("task_name") or data.get("task") or "--",
            "mode": str(data.get("buildMode") or "Not built").title(),
            "count": str(len(packages)),
        }
        for key, value in values.items():
            self.context_values[key].setText(value)

        self.preview.setRowCount(len(packages))
        for row, package in enumerate(packages):
            cells = (
                package.get("department") or "--",
                package.get("productName") or package.get("role") or "APKG",
                "v{:03d}".format(int(package.get("version") or 0)),
                str(package.get("status") or "unknown").title(),
                apkg.dependency_mode_label(package.get("mode")),
                package.get("entrypointPath") or "--",
            )
            for column, value in enumerate(cells):
                self.preview.setItem(
                    row, column, QtWidgets.QTableWidgetItem(str(value))
                )
        preview_header = self.preview.horizontalHeader()
        for column in range(5):
            preview_header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeToContents
            )
        preview_header.setSectionResizeMode(5, QtWidgets.QHeaderView.Stretch)
        self.details.setPlainText(apkg.build_information_text(
            self.node, context=self._context, selection_data=data
        ))
        self._set_health(
            "Ready · {} loaded".format(len(packages))
            if packages else "Ready · empty"
        )


def show_shot_builder(node):
    """Show one modeless Shot Builder window per Begin node."""
    from ayon_houdini.api import lib

    key = node.path()
    current = _WINDOWS.get(key)
    if current is not None:
        try:
            current.show()
            current.raise_()
            current.activateWindow()
            return current
        except RuntimeError:
            _WINDOWS.pop(key, None)

    window = ShotBuilderDialog(node, parent=lib.get_main_window())
    _WINDOWS[key] = window
    window.destroyed.connect(lambda *_args: _WINDOWS.pop(key, None))
    window.show()
    window.raise_()
    window.activateWindow()
    return window
