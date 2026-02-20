"""
Houdini 21+ Solaris MaterialX Builder
Fully working version - builds inside Material Library LOP
"""

import re
from pathlib import Path

try:
    import hou
except ImportError:
    hou = None


# ------------------------------------------------------------
# BUILDER CLASS
# ------------------------------------------------------------

class SolarisMaterialBuilder:

    def __init__(self, material_name, create_preview=False):
        self.material_name = material_name.replace(" ", "_")
        self.textures = {}
        self.is_udim = False
        self.create_preview = create_preview

    # --------------------------------------------------------
    # ADD TEXTURE
    # --------------------------------------------------------

    def add_texture(self, channel, filepath):

        if not filepath:
            return

        filepath = Path(filepath).as_posix()

        # UDIM detect (1001-1999 range)
        if re.search(r"1\d{3}", filepath):
            self.is_udim = True

        self.textures[channel.lower()] = filepath

    # --------------------------------------------------------
    # BUILD MATERIAL
    # --------------------------------------------------------

    def build(self):
        if not hou:
            raise RuntimeError("Run inside Houdini")

        stage = hou.node("/stage")
        if not stage:
            raise RuntimeError("/stage not found")

        # Find or create a Material Library LOP
        matlib = None
        matlib_name = self.material_name + "_MATLIB"
        for node in stage.children():
            if node.type().name() == "materiallibrary":
                matlib = node
                break
        if matlib is None:
            matlib = stage.createNode("materiallibrary", matlib_name)
        matlib.parm("matpathprefix").set(f"/materials/{self.material_name}")

        # Ensure internal materialnetwork exists
        matnet = matlib.node("materialnetwork")
        if matnet is None:
            matnet = matlib.createNode("materialnetwork", "materialnetwork")

        # Remove any existing subnet with the same name to avoid duplicates
        existing = matnet.node(self.material_name)
        if existing:
            existing.destroy()

        # Create subnet for material
        builder = matnet.createNode("subnet", self.material_name)

        shader = builder.createNode("mtlxstandard_surface", "standard_surface")
        material = builder.createNode("mtlxsurfacematerial", "surface_material")
        material.setInput(0, shader)
        output = builder.createNode("suboutput", "OUT_material")
        output.setInput(0, material)

        # Connect textures
        self._connect_textures(builder, shader, material)

        builder.layoutChildren()
        matnet.layoutChildren()
        matlib.layoutChildren()

        if self.create_preview:
            self._create_preview_geo(matlib)

        return matlib

    # --------------------------------------------------------
    # CREATE TEXTURE NODE
    # --------------------------------------------------------

    def _create_texture_node(self, parent, channel, path):

        if self.is_udim:
            path = re.sub(r"(1\d{3})", "<UDIM>", path)

        tex = parent.createNode("mtlximage", f"tex_{channel}")
        tex.parm("file").set(path)

        raw_channels = [
            "normal", "roughness", "metallic",
            "ao", "spec", "displacement",
            "height", "opacity"
        ]

        if channel in raw_channels:
            try:
                tex.parm("colorspace").set("raw")
            except:
                pass

        return tex

    # --------------------------------------------------------
    # CONNECT TEXTURES
    # --------------------------------------------------------

    def _connect_textures(self, parent, shader, material):

        base_channels = ["diffuse", "albedo", "basecolor"]

        # ---- Base + AO Handling ----

        base_key = next(
            (c for c in base_channels if c in self.textures),
            None
        )

        if base_key:

            base_tex = self._create_texture_node(
                parent, base_key, self.textures[base_key]
            )

            if "ao" in self.textures:

                ao_tex = self._create_texture_node(
                    parent, "ao", self.textures["ao"]
                )

                convert = parent.createNode("mtlxconvert")
                convert.parm("outtype").set("color3")
                convert.setInput(0, ao_tex)

                mult = parent.createNode("mtlxmultiply")
                mult.setInput(0, base_tex)
                mult.setInput(1, convert)

                shader.setInput(
                    shader.inputIndex("base_color"),
                    mult
                )

            else:
                shader.setInput(
                    shader.inputIndex("base_color"),
                    base_tex
                )

        # ---- Other Channels ----

        for channel, path in self.textures.items():

            if channel in base_channels or channel == "ao":
                continue

            if channel == "roughness":
                tex = self._create_texture_node(parent, channel, path)
                shader.setInput(
                    shader.inputIndex("specular_roughness"),
                    tex
                )

            elif channel in ["metallic", "metalness"]:
                tex = self._create_texture_node(parent, channel, path)
                shader.setInput(
                    shader.inputIndex("metalness"),
                    tex
                )

            elif channel == "normal":
                tex = self._create_texture_node(parent, channel, path)
                normal = parent.createNode("mtlxnormalmap")
                normal.setInput(0, tex)
                shader.setInput(
                    shader.inputIndex("normal"),
                    normal
                )

            elif channel in ["displacement", "height"]:
                tex = self._create_texture_node(parent, channel, path)
                disp = parent.createNode("mtlxdisplacement")
                disp.setInput(0, tex)
                disp.parm("scale").set(0.05)
                material.setInput(1, disp)

            elif channel == "opacity":
                tex = self._create_texture_node(parent, channel, path)
                shader.setInput(
                    shader.inputIndex("opacity"),
                    tex
                )

            elif channel == "emissive":
                tex = self._create_texture_node(parent, channel, path)
                shader.setInput(
                    shader.inputIndex("emission_color"),
                    tex
                )

            elif channel == "spec":
                tex = self._create_texture_node(parent, channel, path)
                shader.setInput(
                    shader.inputIndex("specular"),
                    tex
                )

    # --------------------------------------------------------
    # PREVIEW SPHERE
    # --------------------------------------------------------

    def _create_preview_geo(self, matlib):

        obj = hou.node("/obj")
        geo = obj.createNode("geo",
                             self.material_name + "_preview")

        sphere = geo.createNode("sphere")
        sphere.parm("type").set(1)

        mat_assign = geo.createNode("material")

        mat_assign.parm("shop_materialpath1").set(
            matlib.parm("matpathprefix").eval()
        )

        mat_assign.setInput(0, sphere)

        geo.layoutChildren()


# ------------------------------------------------------------
# UI
# ------------------------------------------------------------

def show_ui():

    from qtpy import QtWidgets

    class MaterialDialog(QtWidgets.QDialog):

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Solaris MaterialX Builder")
            self.setMinimumWidth(600)

            layout = QtWidgets.QVBoxLayout(self)

            layout.addWidget(QtWidgets.QLabel("Material Name"))
            self.name_edit = QtWidgets.QLineEdit("Material")
            layout.addWidget(self.name_edit)

            self.preview_check = QtWidgets.QCheckBox(
                "Create Preview Sphere"
            )
            layout.addWidget(self.preview_check)

            self.texture_fields = {}

            channels = [
                "diffuse", "ao", "roughness", "metallic",
                "normal", "displacement",
                "opacity", "emissive", "spec"
            ]

            for ch in channels:
                row = QtWidgets.QHBoxLayout()
                row.addWidget(QtWidgets.QLabel(ch))
                line = QtWidgets.QLineEdit()
                row.addWidget(line)
                btn = QtWidgets.QPushButton("Browse")
                btn.clicked.connect(
                    lambda _, c=ch, l=line:
                    self.browse_texture(c, l)
                )
                row.addWidget(btn)
                layout.addLayout(row)
                self.texture_fields[ch] = line

            create_btn = QtWidgets.QPushButton("Create Material")
            create_btn.clicked.connect(self.create_material)
            layout.addWidget(create_btn)

        def browse_texture(self, channel, line):
            file, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                f"Select {channel} texture",
                "",
                "Images (*.exr *.jpg *.png *.tif)"
            )
            if file:
                line.setText(file)

        def create_material(self):
            # Collect all non-empty texture fields
            textures = {ch: field.text() for ch, field in self.texture_fields.items() if field.text()}
            if not textures:
                QtWidgets.QMessageBox.warning(self, "No Textures", "Please specify at least one texture.")
                return

            # For now, use the material name as a single set
            material_name = self.name_edit.text()
            builder = SolarisMaterialBuilder(
                material_name,
                create_preview=self.preview_check.isChecked()
            )
            for ch, path in textures.items():
                builder.add_texture(ch, path)
            builder.build()
            self.accept()

    app = QtWidgets.QApplication.instance()
    if not app:
        app = QtWidgets.QApplication([])

    dialog = MaterialDialog()
    dialog.exec_()


# ------------------------------------------------------------
# RUN
# ------------------------------------------------------------

if __name__ == "__main__":
    show_ui()
