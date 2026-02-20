import hou
from qtpy import QtWidgets, QtCore

from ayon_houdini.nodes import direct_publish


_OUTPUT_PARMS = (
    "filename",
    "sopoutput",
    "sopoutput1",
    "filepath",
    "file",
    "output",
)


def _guess_output_from_selection():
    selected = hou.selectedNodes()
    if not selected:
        return ""

    node = selected[0]
    for parm_name in _OUTPUT_PARMS:
        parm = node.parm(parm_name)
        if not parm:
            continue
        try:
            value = parm.unexpandedString() or parm.evalAsString()
        except Exception:
            continue
        if value:
            return value
    return ""


class PointCachePublishTool(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super(PointCachePublishTool, self).__init__(parent)
        self.setWindowTitle("AYON Point Cache Publish")
        self.setMinimumWidth(520)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)

        start, end = [int(v) for v in hou.playbar.frameRange()]

        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel("Product Name:"))
        self.product_input = QtWidgets.QLineEdit("pointcacheMain")
        layout.addWidget(self.product_input)

        layout.addWidget(QtWidgets.QLabel("File / Sequence Pattern:"))
        path_layout = QtWidgets.QHBoxLayout()
        self.path_input = QtWidgets.QLineEdit(_guess_output_from_selection())
        self.browse_btn = QtWidgets.QPushButton("Browse")
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.browse_btn)
        layout.addLayout(path_layout)

        range_layout = QtWidgets.QHBoxLayout()
        range_layout.addWidget(QtWidgets.QLabel("Start:"))
        self.start_input = QtWidgets.QSpinBox()
        self.start_input.setRange(-1000000, 1000000)
        self.start_input.setValue(start)
        range_layout.addWidget(self.start_input)

        range_layout.addWidget(QtWidgets.QLabel("End:"))
        self.end_input = QtWidgets.QSpinBox()
        self.end_input.setRange(-1000000, 1000000)
        self.end_input.setValue(end)
        range_layout.addWidget(self.end_input)
        layout.addLayout(range_layout)

        self.publish_btn = QtWidgets.QPushButton("Publish Point Cache")
        layout.addWidget(self.publish_btn)

        self.browse_btn.clicked.connect(self.on_browse)
        self.publish_btn.clicked.connect(self.on_publish)

    def on_browse(self):
        start_path = self.path_input.text() or hou.expandString("$HIP")
        selected = hou.ui.selectFile(
            title="Select Cache File",
            start_directory=start_path,
            chooser_mode=hou.fileChooserMode.Read
        )
        if not selected:
            return
        self.path_input.setText(selected)

    def on_publish(self):
        try:
            product_name = self.product_input.text().strip()
            path = self.path_input.text().strip()
            if not product_name:
                raise RuntimeError("Product name cannot be empty.")
            if not path:
                raise RuntimeError("File path/pattern cannot be empty.")

            publish_result = direct_publish.publish_pointcache(
                product_name=product_name,
                path_or_paths=path,
                frame_start=int(self.start_input.value()),
                frame_end=int(self.end_input.value()),
            )
            hou.ui.displayMessage(
                "Published Successfully!\n"
                f"{publish_result['product_name']} "
                f"v{publish_result['version']:03d}"
            )
            self.close()
        except Exception as exc:
            hou.ui.displayMessage(f"Publish Failed:\n{exc}")


def launch():
    if hasattr(hou.session, "ayon_pointcache_publish_tool"):
        try:
            hou.session.ayon_pointcache_publish_tool.close()
            hou.session.ayon_pointcache_publish_tool.deleteLater()
        except Exception:
            pass

    hou.session.ayon_pointcache_publish_tool = PointCachePublishTool(
        parent=hou.qt.mainWindow()
    )
    hou.session.ayon_pointcache_publish_tool.show()


if __name__ == "__main__":
    launch()
