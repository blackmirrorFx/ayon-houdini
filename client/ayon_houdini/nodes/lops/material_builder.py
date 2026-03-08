"""
LOPs MaterialX builder for Solaris.

Features:
- Creates a Material Library LOP in /stage.
- Creates a MaterialX subnet inside the material library.
- Builds a MaterialX network from texture files.
- Supports UDIM token conversion (<UDIM>) via UI toggle.
"""

import re
import random
from dataclasses import dataclass
from functools import partial
from pathlib import Path

try:
    import hou
except ImportError:
    hou = None


SUPPORTED_IMAGE_EXTENSIONS = {
    ".bmp",
    ".cin",
    ".dpx",
    ".exr",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".png",
    ".psd",
    ".rat",
    ".sgi",
    ".tga",
    ".tif",
    ".tiff",
    ".tx",
    ".webp",
}

TEXTURE_NODE_COLORS = {
    "base_color": (0.90, 0.72, 0.18),
    "ao": (0.45, 0.45, 0.45),
    "roughness": (0.93, 0.56, 0.22),
    "glossiness": (0.95, 0.76, 0.30),
    "metalness": (0.62, 0.70, 0.82),
    "normal": (0.36, 0.62, 0.95),
    "displacement": (0.82, 0.48, 0.70),
    "opacity": (0.84, 0.84, 0.84),
    "emission_color": (0.22, 0.92, 0.42),
    "specular": (0.62, 0.68, 0.96),
    "transmission": (0.32, 0.88, 0.88),
    "translucency": (0.44, 0.82, 0.74),
    "subsurface": (0.96, 0.48, 0.36),
}


@dataclass(frozen=True)
class MaterialChannel:
    name: str
    aliases: tuple
    raw_colorspace: bool = True


class UDIMHandler:
    _udim_pattern = re.compile(r"(?<!\d)(1\d{3})(?!\d)")

    @classmethod
    def has_udim(cls, path):
        return "<udim>" in path.lower() or bool(cls._udim_pattern.search(path))

    @classmethod
    def to_udim_token(cls, path):
        if "<udim>" in path.lower():
            return re.sub(r"(?i)<udim>", "<UDIM>", path)
        return cls._udim_pattern.sub("<UDIM>", path, count=1)


class MaterialChannelLibrary:
    CHANNELS = (
        MaterialChannel(
            "base_color",
            (
                "basecolor",
                "base_color",
                "basecolour",
                "albedo",
                "diffuse",
                "diff",
                "color",
                "colour",
                "col",
            ),
            raw_colorspace=False,
        ),
        MaterialChannel("ao", ("ao", "occlusion", "ambientocclusion", "ambient_occlusion", "occ")),
        MaterialChannel("roughness", ("roughness", "rough")),
        MaterialChannel("glossiness", ("glossiness", "gloss", "sharpness")),
        MaterialChannel("metalness", ("metalness", "metallic", "metal")),
        MaterialChannel(
            "normal",
            ("normal", "nrm", "nor", "normalgl", "normaldx", "opengl", "dx", "normal-ogl", "nrmmaya"),
        ),
        MaterialChannel("displacement", ("displacement", "disp", "height", "bump")),
        MaterialChannel("opacity", ("opacity", "alpha", "transparency")),
        MaterialChannel("emission_color", ("emission", "emissive", "emit")),
        MaterialChannel("specular", ("specular", "spec")),
        MaterialChannel("transmission", ("refrac", "refraction", "transmission")),
        MaterialChannel("translucency", ("translucency", "translucent", "trans")),
        MaterialChannel("subsurface", ("sss", "subsurface", "scattering")),
    )

    _alias_to_channel = {
        alias: channel.name
        for channel in CHANNELS
        for alias in channel.aliases
    }

    _channel_by_name = {channel.name: channel for channel in CHANNELS}

    def __init__(self, texture_path):
        self.texture_path = (texture_path or "").strip()

    @classmethod
    def normalize_channel_name(cls, channel):
        key = (channel or "").strip().lower()
        if not key:
            return None
        if key in cls._channel_by_name:
            return key
        return cls._alias_to_channel.get(key)

    @classmethod
    def channel_from_filename(cls, filename):
        stem = Path(filename).stem.lower()
        tokens = [token for token in re.split(r"[^a-z0-9]+", stem) if token]
        normalized_stem = "".join(tokens)

        best_match = None
        best_alias_len = -1

        for alias, channel in cls._alias_to_channel.items():
            alias_len = len(alias)
            if alias in tokens or alias.replace("_", "") in normalized_stem:
                if alias_len > best_alias_len:
                    best_alias_len = alias_len
                    best_match = channel

        return best_match

    @classmethod
    def is_supported_image(cls, path_obj):
        return path_obj.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

    def _iter_texture_files(self):
        source = Path(self.texture_path).expanduser()
        if not self.texture_path:
            raise ValueError("Texture path is empty.")
        if not source.exists():
            raise ValueError("Texture path does not exist.")

        if source.is_file():
            return [source]

        return [child for child in sorted(source.iterdir()) if child.is_file()]

    def discover_textures(self):
        return self.discover_textures_from_paths(self._iter_texture_files())

    @classmethod
    def discover_textures_from_paths(cls, paths):
        textures = {}
        for texture_file in cls.expand_paths_to_files(paths):
            channel_name = cls.channel_from_filename(texture_file.name)
            if not channel_name:
                continue
            textures.setdefault(channel_name, texture_file.as_posix())
        return textures

    @classmethod
    def _material_key_from_file(cls, file_path):
        tokens = cls._clean_name_tokens(file_path.stem)
        if tokens:
            return cls._sanitize_material_name("_".join(tokens))
        return cls._sanitize_material_name(file_path.parent.name or file_path.stem)

    @classmethod
    def discover_material_groups_from_paths(cls, paths):
        groups_by_name = {}
        for texture_file in cls.expand_paths_to_files(paths):
            channel_name = cls.channel_from_filename(texture_file.name)
            if not channel_name:
                continue

            group_name = cls._material_key_from_file(texture_file)
            groups_by_name.setdefault(group_name, {})
            groups_by_name[group_name].setdefault(channel_name, texture_file.as_posix())

        groups = []
        for group_name in sorted(groups_by_name.keys()):
            textures = groups_by_name[group_name]
            if not textures:
                continue
            groups.append(
                {
                    "suggested_name": group_name,
                    "textures": textures,
                }
            )
        return groups

    @classmethod
    def expand_paths_to_files(cls, paths):
        if not paths:
            return []

        collected = []
        seen = set()

        for raw_path in paths:
            if not raw_path:
                continue
            path_obj = Path(str(raw_path)).expanduser()
            if not path_obj.exists():
                continue

            if path_obj.is_file():
                posix = path_obj.as_posix()
                if posix not in seen:
                    seen.add(posix)
                    collected.append(path_obj)
                continue

            if path_obj.is_dir():
                for child in sorted(path_obj.iterdir()):
                    if not child.is_file():
                        continue
                    posix = child.as_posix()
                    if posix in seen:
                        continue
                    seen.add(posix)
                    collected.append(child)

        return collected

    def suggest_material_name(self):
        source = Path(self.texture_path).expanduser()
        if source.is_file():
            base = source.stem
        else:
            base = source.name

        if not base:
            return "Material"

        tokens = [token for token in re.split(r"[^a-zA-Z0-9]+", base) if token]
        if tokens:
            last = tokens[-1].lower()
            if last in self._alias_to_channel:
                tokens = tokens[:-1]
        if tokens:
            base = "_".join(tokens)

        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_")
        return sanitized or "Material"

    @classmethod
    def channel(cls, name):
        return cls._channel_by_name.get(name)

    @classmethod
    def channel_names(cls):
        return [channel.name for channel in cls.CHANNELS]

    @staticmethod
    def _sanitize_material_name(value):
        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "")).strip("_")
        return sanitized or "Material"

    @classmethod
    def _clean_name_tokens(cls, stem):
        tokens = [token for token in re.split(r"[^a-zA-Z0-9]+", stem) if token]
        cleaned = []
        for token in tokens:
            lowered = token.lower()
            alias_key = lowered.replace("_", "")

            if lowered in cls._alias_to_channel or alias_key in cls._alias_to_channel:
                continue
            if re.fullmatch(r"1\d{3}", lowered):  # UDIM like 1001
                continue
            if re.fullmatch(r"\d+k", lowered):  # 1k/2k/4k/8k
                continue
            if re.fullmatch(r"\d+x\d+", lowered):  # 2048x2048
                continue
            if re.fullmatch(r"v\d+", lowered):  # v001 style
                continue
            if lowered in {"tex", "texture", "map", "udim"}:
                continue

            cleaned.append(token)

        return cleaned

    @classmethod
    def suggest_material_name_from_paths(cls, paths):
        files = cls.expand_paths_to_files(paths)
        if not files:
            return "Material"

        token_lists = []
        for file_path in files:
            tokens = cls._clean_name_tokens(file_path.stem)
            if tokens:
                token_lists.append(tokens)

        if token_lists:
            common_prefix = token_lists[0]
            for tokens in token_lists[1:]:
                index = 0
                while (
                    index < len(common_prefix)
                    and index < len(tokens)
                    and common_prefix[index].lower() == tokens[index].lower()
                ):
                    index += 1
                common_prefix = common_prefix[:index]
                if not common_prefix:
                    break

            if common_prefix:
                return cls._sanitize_material_name("_".join(common_prefix))

            return cls._sanitize_material_name("_".join(token_lists[0]))

        fallback = files[0].parent.name or files[0].stem
        return cls._sanitize_material_name(fallback)


class MaterialBuilder:
    def __init__(self, material_name, use_udim=False):
        self.material_name = self._sanitize_name(material_name)
        self.use_udim = bool(use_udim)
        self.textures = {}

    @staticmethod
    def _sanitize_name(name):
        clean = re.sub(r"[^a-zA-Z0-9_]+", "_", (name or "").strip())
        return clean or "Material"

    def add_texture(self, channel, filepath):
        if not filepath:
            return

        canonical = MaterialChannelLibrary.normalize_channel_name(channel)
        if not canonical:
            return

        path = Path(filepath).as_posix()
        if self.use_udim and UDIMHandler.has_udim(path):
            path = UDIMHandler.to_udim_token(path)

        self.textures[canonical] = path

    def build(self):
        if not hou:
            raise RuntimeError("This tool must run inside Houdini.")
        if not self.textures:
            raise RuntimeError("No recognized textures were provided.")

        stage = hou.node("/stage")
        if stage is None:
            raise RuntimeError("Could not find /stage network.")

        matlib, created_new_matlib = self._find_or_create_materiallibrary(stage)
        if created_new_matlib:
            matpath_prefix = matlib.parm("matpathprefix")
            if matpath_prefix:
                matpath_prefix.set("/materials/")

        material_parent = self._resolve_material_parent(matlib)

        target_name = self.material_name
        if material_parent.node(target_name) is not None:
            try:
                target_name = material_parent.uniqueName(target_name)
            except Exception:
                suffix = 1
                while material_parent.node(f"{target_name}_{suffix}") is not None:
                    suffix += 1
                target_name = f"{target_name}_{suffix}"

        material_subnet = self._create_node_with_fallback(
            material_parent,
            target_name,
            ("subnet", "subnetvop"),
        )
        try:
            material_subnet.setMaterialFlag(True)
        except Exception:
            pass
        self._set_random_node_color(material_subnet)
        self._remove_default_subnet_io(material_subnet)

        shader = self._create_node_with_fallback(
            material_subnet,
            "standard_surface",
            ("mtlxstandard_surface", "mtlxstandardsurface"),
        )

        surface_output = self._create_subnet_connector(
            material_subnet,
            node_name="surface_output",
            parm_name="surface",
            parm_label="Surface",
            parm_type="surface",
        )
        self._connect_output(surface_output, shader)

        displacement_output = self._create_subnet_connector(
            material_subnet,
            node_name="displacement_output",
            parm_name="displacement",
            parm_label="Displacement",
            parm_type="displacement",
        )

        self._connect_textures(material_subnet, shader, displacement_output)

        material_subnet.layoutChildren()
        if material_parent != matlib:
            material_parent.layoutChildren()
        matlib.layoutChildren()
        matlib.moveToGoodPosition()

        return matlib, material_subnet

    @classmethod
    def _find_or_create_materiallibrary(cls, stage):
        for child in stage.children():
            try:
                if child.type().name() == "materiallibrary":
                    return child, False
            except Exception:
                continue

        matlib = cls._create_node(stage, "materiallibrary", "Materials")
        return matlib, True

    @staticmethod
    def _create_node(parent, node_type, node_name):
        try:
            return parent.createNode(node_type, node_name)
        except Exception as exc:
            parent_path = parent.path() if hasattr(parent, "path") else str(parent)
            raise RuntimeError(
                f"Failed to create node type '{node_type}' in '{parent_path}'. "
                f"Houdini error: {exc}"
            ) from exc

    @staticmethod
    def _remove_default_subnet_io(subnet_node):
        for child in list(subnet_node.children()):
            try:
                type_name = child.type().name()
            except Exception:
                continue
            if type_name not in {"subinput", "suboutput"}:
                continue
            try:
                child.destroy()
            except Exception:
                continue

    @classmethod
    def _create_node_with_fallback(cls, parent, node_name, node_types):
        last_exc = None
        for node_type in node_types:
            try:
                return cls._create_node(parent, node_type, node_name)
            except RuntimeError as exc:
                last_exc = exc
                continue
        parent_path = parent.path() if hasattr(parent, "path") else str(parent)
        tried = ", ".join(node_types)
        raise RuntimeError(
            f"Failed to create node '{node_name}' in '{parent_path}'. Tried: {tried}. "
            f"Last error: {last_exc}"
        )

    @staticmethod
    def _has_vop_children(node):
        try:
            category = node.childTypeCategory()
        except Exception:
            return False
        return bool(category and category.name() == "Vop")

    @classmethod
    def _resolve_material_parent(cls, matlib):
        def _can_host_materials(node):
            probe_types = ("mtlximage", "subnetvop", "subnet")
            for node_type in probe_types:
                try:
                    probe = node.createNode(node_type, "__ayon_material_probe__")
                    try:
                        category_name = probe.type().category().name()
                    except Exception:
                        category_name = None
                    probe.destroy()
                    if category_name == "Vop":
                        return True
                except Exception:
                    continue
            return False

        for name in ("materialnetwork", "matnet"):
            child = matlib.node(name)
            if child is not None and _can_host_materials(child):
                return child

        if _can_host_materials(matlib):
            return matlib

        for child in matlib.children():
            try:
                node_type_name = child.type().name()
            except Exception:
                continue

            if node_type_name in {"materialnetwork", "matnet", "vopnet"}:
                if _can_host_materials(child):
                    return child

        for child in matlib.children():
            try:
                if hasattr(child, "isMaterialFlagSet") and child.isMaterialFlagSet():
                    continue
            except Exception:
                pass
            if _can_host_materials(child):
                return child

        raise RuntimeError(
            "Could not find an editable VOP material parent inside "
            f"{matlib.path()}."
        )

    @classmethod
    def _create_subnet_connector(
        cls,
        parent,
        node_name,
        parm_name,
        parm_label,
        parm_type,
    ):
        connector = cls._create_node_with_fallback(
            parent,
            node_name,
            ("subnetconnector", "suboutput"),
        )
        connectorkind = connector.parm("connectorkind")
        if connectorkind is not None:
            connectorkind.set("output")
        for parm, value in (
            ("parmname", parm_name),
            ("parmlabel", parm_label),
            ("parmtype", parm_type),
        ):
            p = connector.parm(parm)
            if p is not None:
                p.set(value)
        return connector

    @staticmethod
    def _connect_output(output_node, source_node):
        if output_node is None or source_node is None:
            return
        try:
            output_node.setNamedInput("suboutput", source_node, "out")
            return
        except Exception:
            pass
        try:
            output_node.setInput(0, source_node, 0)
        except Exception:
            pass

    @staticmethod
    def _set_input_by_name(node, input_name, source_node):
        index = node.inputIndex(input_name)
        if index >= 0:
            node.setInput(index, source_node)

    @classmethod
    def _set_input_by_names(cls, node, input_names, source_node):
        for input_name in input_names:
            index = node.inputIndex(input_name)
            if index >= 0:
                node.setInput(index, source_node)
                return True
        return False

    @staticmethod
    def _set_raw_colorspace(tex_node):
        for parm_name in ("colorspace", "filecolorspace"):
            parm = tex_node.parm(parm_name)
            if parm is None:
                continue
            for raw_value in ("raw", "Raw", "Utility - Raw"):
                try:
                    parm.set(raw_value)
                    return
                except Exception:
                    continue

    @staticmethod
    def _set_first_available_parm(node, parm_names, value):
        for parm_name in parm_names:
            parm = node.parm(parm_name)
            if parm is None:
                continue
            try:
                parm.set(value)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _set_node_color(node, channel_name):
        if not hou:
            return
        rgb = TEXTURE_NODE_COLORS.get(channel_name)
        if not rgb:
            return
        try:
            node.setColor(hou.Color(rgb))
        except Exception:
            pass

    @staticmethod
    def _set_random_node_color(node):
        if not hou:
            return
        rgb = (
            random.uniform(0.25, 0.95),
            random.uniform(0.25, 0.95),
            random.uniform(0.25, 0.95),
        )
        try:
            node.setColor(hou.Color(rgb))
        except Exception:
            pass

    def _create_texture_node(self, parent, channel_name, texture_path):
        tex_node = self._create_node_with_fallback(
            parent,
            "tex_" + channel_name,
            ("mtlxtiledimage", "mtlximage"),
        )
        if not self._set_first_available_parm(
            tex_node,
            ("file", "filename", "tex0", "map"),
            texture_path,
        ):
            raise RuntimeError(
                "Could not set texture path on node '{0}' ({1}).".format(
                    tex_node.path(),
                    tex_node.type().name(),
                )
            )
        self._set_node_color(tex_node, channel_name)

        channel = MaterialChannelLibrary.channel(channel_name)
        if channel and channel.raw_colorspace:
            self._set_raw_colorspace(tex_node)

        return tex_node

    def _connect_base_and_ao(self, parent, shader):
        base_path = self.textures.get("base_color")
        ao_path = self.textures.get("ao")

        if not base_path:
            return

        base_tex = self._create_texture_node(parent, "base_color", base_path)
        if not ao_path:
            self._set_input_by_name(shader, "base_color", base_tex)
            return

        ao_tex = self._create_texture_node(parent, "ao", ao_path)

        convert = self._create_node(parent, "mtlxconvert", "ao_to_color3")
        self._set_first_available_parm(convert, ("outtype", "type", "signature"), "color3")
        convert.setInput(0, ao_tex)

        multiply = self._create_node(parent, "mtlxmultiply", "base_ao_multiply")
        multiply.setInput(0, base_tex)
        multiply.setInput(1, convert)

        self._set_input_by_name(shader, "base_color", multiply)

    def _connect_textures(self, parent, shader, displacement_output):
        self._connect_base_and_ao(parent, shader)

        roughness_path = self.textures.get("roughness")
        if roughness_path:
            roughness_tex = self._create_texture_node(parent, "roughness", roughness_path)
            self._set_input_by_name(shader, "specular_roughness", roughness_tex)
        else:
            gloss_path = self.textures.get("glossiness")
            if gloss_path:
                gloss_tex = self._create_texture_node(parent, "glossiness", gloss_path)
                invert_gloss = self._create_node(parent, "mtlxremap", "invert_gloss")
                outlow = invert_gloss.parm("outlow")
                outhigh = invert_gloss.parm("outhigh")
                if outlow is not None:
                    outlow.set(1.0)
                if outhigh is not None:
                    outhigh.set(0.0)
                invert_gloss.setInput(0, gloss_tex)
                self._set_input_by_name(shader, "specular_roughness", invert_gloss)

        metalness_path = self.textures.get("metalness")
        if metalness_path:
            metalness_tex = self._create_texture_node(parent, "metalness", metalness_path)
            self._set_input_by_name(shader, "metalness", metalness_tex)

        normal_path = self.textures.get("normal")
        if normal_path:
            normal_tex = self._create_texture_node(parent, "normal", normal_path)
            normal_map = self._create_node(parent, "mtlxnormalmap", "normal_map")
            normal_map.setInput(0, normal_tex)
            self._set_input_by_name(shader, "normal", normal_map)

        displacement_path = self.textures.get("displacement")
        if displacement_path:
            displacement_tex = self._create_texture_node(parent, "displacement", displacement_path)
            displacement = self._create_node(parent, "mtlxdisplacement", "displacement")
            displacement.setInput(0, displacement_tex)
            scale_parm = displacement.parm("scale")
            if scale_parm:
                scale_parm.set(0.05)
            self._connect_output(displacement_output, displacement)

        opacity_path = self.textures.get("opacity")
        if opacity_path:
            opacity_tex = self._create_texture_node(parent, "opacity", opacity_path)
            self._set_input_by_name(shader, "opacity", opacity_tex)

        emission_path = self.textures.get("emission_color")
        if emission_path:
            emission_tex = self._create_texture_node(parent, "emission_color", emission_path)
            self._set_input_by_names(shader, ("emission_color", "emission"), emission_tex)

        specular_path = self.textures.get("specular")
        if specular_path:
            specular_tex = self._create_texture_node(parent, "specular", specular_path)
            self._set_input_by_name(shader, "specular", specular_tex)

        transmission_path = self.textures.get("transmission")
        if transmission_path:
            transmission_tex = self._create_texture_node(parent, "transmission", transmission_path)
            self._set_input_by_name(shader, "transmission", transmission_tex)

        translucency_path = self.textures.get("translucency")
        if translucency_path:
            translucency_tex = self._create_texture_node(parent, "translucency", translucency_path)
            self._set_input_by_names(
                shader,
                ("transmission_color", "transmission"),
                translucency_tex,
            )

        subsurface_path = self.textures.get("subsurface")
        if subsurface_path:
            subsurface_tex = self._create_texture_node(parent, "subsurface", subsurface_path)
            self._set_input_by_name(shader, "subsurface", subsurface_tex)


def show_ui(parent=None):
    from qtpy import QtCore, QtWidgets

    class DropPathListWidget(QtWidgets.QListWidget):
        pathsDropped = QtCore.Signal(list)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.setAcceptDrops(True)

        @staticmethod
        def _extract_paths(mime_data):
            paths = []
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    local = url.toLocalFile()
                    if local:
                        paths.append(local)
            elif mime_data.hasText():
                for chunk in mime_data.text().splitlines():
                    chunk = chunk.strip().strip('"')
                    if chunk:
                        paths.append(chunk)
            return paths

        def dragEnterEvent(self, event):
            if self._extract_paths(event.mimeData()):
                event.acceptProposedAction()
                return
            super().dragEnterEvent(event)

        def dragMoveEvent(self, event):
            if self._extract_paths(event.mimeData()):
                event.acceptProposedAction()
                return
            super().dragMoveEvent(event)

        def dropEvent(self, event):
            paths = self._extract_paths(event.mimeData())
            if paths:
                self.pathsDropped.emit(paths)
                event.acceptProposedAction()
                return
            super().dropEvent(event)

    class MaterialXBuilderDialog(QtWidgets.QDialog):
        def __init__(self, ui_parent=None):
            super().__init__(ui_parent)
            self.setWindowTitle("BMFX material builder")
            self.setWindowFlags(
                QtCore.Qt.Window
                | QtCore.Qt.WindowTitleHint
                | QtCore.Qt.WindowSystemMenuHint
                | QtCore.Qt.WindowMinimizeButtonHint
                | QtCore.Qt.WindowCloseButtonHint
            )
            self.setMinimumWidth(900)
            self.setMinimumHeight(680)

            self.manual_fields = {}
            self._build_ui()
            self._apply_style()
            self._on_mode_toggled(self.auto_detect_check.isChecked())
            self._on_auto_name_toggled(self.auto_name_check.isChecked())
            self._update_summary({})
            self._set_info("Ready.", "info")

        def _build_ui(self):
            root_layout = QtWidgets.QVBoxLayout(self)
            root_layout.setContentsMargins(12, 12, 12, 12)
            root_layout.setSpacing(10)

            settings_group = QtWidgets.QGroupBox("Material Settings")
            settings_layout = QtWidgets.QGridLayout(settings_group)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            settings_layout.setHorizontalSpacing(8)
            settings_layout.setVerticalSpacing(8)

            name_row = QtWidgets.QHBoxLayout()
            name_row.setContentsMargins(0, 0, 0, 0)
            name_row.setSpacing(8)
            name_label = QtWidgets.QLabel("Material Name")
            name_label.setMinimumWidth(95)
            name_row.addWidget(name_label)
            self.material_name_edit = QtWidgets.QLineEdit("Material")
            self.material_name_edit.setPlaceholderText("Material name")
            name_row.addWidget(self.material_name_edit, 1)
            settings_layout.addLayout(name_row, 0, 0, 1, 4)

            self.auto_name_check = QtWidgets.QCheckBox("Auto detect material name")
            self.auto_name_check.setChecked(True)
            self.auto_name_check.toggled.connect(self._on_auto_name_toggled)

            self.udim_check = QtWidgets.QCheckBox("Convert Texture to <UDIM>")
            self.udim_check.setChecked(False)

            self.auto_detect_check = QtWidgets.QCheckBox("Auto detect textures from selected files")
            self.auto_detect_check.setChecked(True)
            self.auto_detect_check.toggled.connect(self._on_mode_toggled)

            toggles_row = QtWidgets.QHBoxLayout()
            toggles_row.setContentsMargins(0, 0, 0, 0)
            toggles_row.setSpacing(18)
            toggles_row.addStretch(1)
            toggles_row.addWidget(self.auto_name_check)
            toggles_row.addWidget(self.udim_check)
            toggles_row.addWidget(self.auto_detect_check)
            toggles_row.addStretch(1)
            settings_layout.addLayout(toggles_row, 1, 0, 1, 4)

            root_layout.addWidget(settings_group)

            self.mode_stack = QtWidgets.QStackedWidget()
            self.auto_page = self._build_auto_page()
            self.manual_page = self._build_manual_page()
            self.mode_stack.addWidget(self.auto_page)
            self.mode_stack.addWidget(self.manual_page)
            root_layout.addWidget(self.mode_stack, 1)

            self.summary_label = QtWidgets.QLabel("")
            root_layout.addWidget(self.summary_label)

            self.progress_bar = QtWidgets.QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setFormat("Idle")
            root_layout.addWidget(self.progress_bar)

            buttons_layout = QtWidgets.QHBoxLayout()
            self.preview_btn = QtWidgets.QPushButton("Preview")
            self.preview_btn.clicked.connect(self._preview_textures)
            buttons_layout.addWidget(self.preview_btn)

            self.create_btn = QtWidgets.QPushButton("Create MaterialX in LOPs")
            self.create_btn.clicked.connect(self._create_material)
            buttons_layout.addWidget(self.create_btn)

            self.reset_btn = QtWidgets.QPushButton("Reset")
            self.reset_btn.clicked.connect(self._reset_ui)
            buttons_layout.addWidget(self.reset_btn)
            root_layout.addLayout(buttons_layout)

            self.info_label = QtWidgets.QLabel("")
            self.info_label.setWordWrap(True)
            root_layout.addWidget(self.info_label)

        def _build_auto_page(self):
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)

            group = QtWidgets.QGroupBox("Auto Detect")
            group_layout = QtWidgets.QVBoxLayout(group)
            group_layout.addWidget(
                QtWidgets.QLabel(
                    "Select any texture files or folders. Channels will be detected from file names."
                )
            )

            self.auto_paths_list = DropPathListWidget()
            self.auto_paths_list.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection
            )
            self.auto_paths_list.setMinimumHeight(250)
            self.auto_paths_list.pathsDropped.connect(self._auto_paths_dropped)
            group_layout.addWidget(self.auto_paths_list)

            row = QtWidgets.QHBoxLayout()
            add_files_btn = QtWidgets.QPushButton("Add Files")
            add_files_btn.clicked.connect(self._auto_add_files)
            row.addWidget(add_files_btn)

            add_folder_btn = QtWidgets.QPushButton("Add Folder")
            add_folder_btn.clicked.connect(self._auto_add_folder)
            row.addWidget(add_folder_btn)

            remove_selected_btn = QtWidgets.QPushButton("Remove Selected")
            remove_selected_btn.clicked.connect(self._auto_remove_selected)
            row.addWidget(remove_selected_btn)

            clear_btn = QtWidgets.QPushButton("Clear List")
            clear_btn.clicked.connect(self._auto_clear)
            row.addWidget(clear_btn)
            group_layout.addLayout(row)

            layout.addWidget(group)
            return page

        def _build_manual_page(self):
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)

            group = QtWidgets.QGroupBox("Manual Channel Assignment")
            group_layout = QtWidgets.QVBoxLayout(group)

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            content = QtWidgets.QWidget()
            content_layout = QtWidgets.QVBoxLayout(content)

            for channel_name in MaterialChannelLibrary.channel_names():
                row = QtWidgets.QHBoxLayout()
                label = QtWidgets.QLabel(self._channel_label(channel_name))
                label.setMinimumWidth(140)
                row.addWidget(label)

                line = QtWidgets.QLineEdit()
                line.setPlaceholderText("Select texture file")
                row.addWidget(line, 1)

                browse_btn = QtWidgets.QPushButton("Browse")
                browse_btn.clicked.connect(partial(self._browse_manual_channel, channel_name))
                row.addWidget(browse_btn)

                clear_btn = QtWidgets.QPushButton("Clear")
                clear_btn.clicked.connect(partial(self._clear_manual_channel, channel_name))
                row.addWidget(clear_btn)

                content_layout.addLayout(row)
                self.manual_fields[channel_name] = line

            content_layout.addStretch(1)
            scroll.setWidget(content)
            group_layout.addWidget(scroll)

            layout.addWidget(group)
            return page

        def _apply_style(self):
            self.setStyleSheet(
                """
                QDialog { background-color: #2b2b2b; }
                QGroupBox {
                    border: 1px solid #4a4a4a;
                    border-radius: 6px;
                    margin-top: 8px;
                    padding-top: 8px;
                    font-weight: bold;
                }
                QLabel { color: #dddddd; }
                QLineEdit, QListWidget, QScrollArea {
                    background-color: #1f1f1f;
                    border: 1px solid #555555;
                    border-radius: 4px;
                    padding: 4px;
                    color: #e8e8e8;
                }
                QProgressBar {
                    background-color: #1f1f1f;
                    border: 1px solid #555555;
                    border-radius: 4px;
                    min-height: 18px;
                    text-align: center;
                    color: #dddddd;
                }
                QProgressBar::chunk {
                    background-color: #4b7bec;
                    border-radius: 3px;
                }
                QLineEdit[readOnly="true"] {
                    color: #f4d35e;
                    border: 1px solid #8a6e00;
                }
                QPushButton {
                    background-color: #3f3f3f;
                    border: 1px solid #5f5f5f;
                    border-radius: 4px;
                    padding: 6px 10px;
                    color: #f0f0f0;
                }
                QPushButton:hover { background-color: #4a4a4a; }
                QPushButton:pressed { background-color: #333333; }
                QCheckBox { color: #e0e0e0; }
                """
            )

        @staticmethod
        def _channel_label(channel_name):
            return channel_name.replace("_", " ").title()

        @staticmethod
        def _image_dialog_filter():
            extensions = " ".join("*" + ext for ext in sorted(SUPPORTED_IMAGE_EXTENSIONS))
            return "Image Files ({0});;All Files (*)".format(extensions)

        def _on_mode_toggled(self, is_auto):
            self.mode_stack.setCurrentIndex(0 if is_auto else 1)
            if is_auto:
                self.preview_btn.setText("Preview Auto Detection")
            else:
                self.preview_btn.setText("Preview Manual Setup")
            self._update_material_name_auto()

        def _on_auto_name_toggled(self, enabled):
            self.material_name_edit.setReadOnly(enabled)
            self._update_material_name_auto()

        def _auto_list_paths(self):
            return [
                self.auto_paths_list.item(index).text()
                for index in range(self.auto_paths_list.count())
            ]

        def _material_name_source_paths(self):
            if self.auto_detect_check.isChecked():
                return self._auto_list_paths()

            manual_paths = []
            for line_edit in self.manual_fields.values():
                value = line_edit.text().strip()
                if value:
                    manual_paths.append(value)
            return manual_paths

        def _update_material_name_auto(self):
            if not self.auto_name_check.isChecked():
                return

            source_paths = self._material_name_source_paths()
            if not source_paths:
                self.material_name_edit.setText("Material")
                return

            suggested_name = MaterialChannelLibrary.suggest_material_name_from_paths(source_paths)
            self.material_name_edit.setText(suggested_name)

        def _auto_add_paths(self, paths):
            if not paths:
                return
            existing = set(self._auto_list_paths())
            for raw_path in paths:
                clean = str(raw_path).strip()
                if not clean or clean in existing:
                    continue
                self.auto_paths_list.addItem(clean)
                existing.add(clean)
            self._update_material_name_auto()

        def _auto_add_files(self):
            filepaths, _ = QtWidgets.QFileDialog.getOpenFileNames(
                self,
                "Select Texture Files",
                "",
                self._image_dialog_filter(),
            )
            self._auto_add_paths(filepaths)
            self._preview_textures()

        def _auto_add_folder(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "Select Texture Folder",
                "",
            )
            if folder:
                self._auto_add_paths([folder])
                self._preview_textures()

        def _auto_remove_selected(self):
            selected_items = list(self.auto_paths_list.selectedItems())
            for item in selected_items:
                row = self.auto_paths_list.row(item)
                self.auto_paths_list.takeItem(row)
            self._update_material_name_auto()
            self._preview_textures()

        def _auto_clear(self):
            self.auto_paths_list.clear()
            self._update_material_name_auto()
            self._preview_textures()

        def _auto_paths_dropped(self, paths):
            self._auto_add_paths(paths)
            self._preview_textures()

        def _browse_manual_channel(self, channel_name):
            target_line = self.manual_fields.get(channel_name)
            if target_line is None:
                return
            filepath, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select Texture File",
                "",
                self._image_dialog_filter(),
            )
            if filepath:
                target_line.setText(filepath)
                self._update_material_name_auto()
                if not self.auto_detect_check.isChecked():
                    self._preview_textures()

        def _clear_manual_channel(self, channel_name):
            target_line = self.manual_fields.get(channel_name)
            if target_line is None:
                return
            target_line.clear()
            self._update_material_name_auto()
            if not self.auto_detect_check.isChecked():
                self._preview_textures()

        def _collect_manual_textures(self):
            textures = {}
            for channel_name, line_edit in self.manual_fields.items():
                value = line_edit.text().strip()
                if not value:
                    continue
                textures[channel_name] = Path(value).as_posix()
            return textures

        def _collect_auto_material_groups(self):
            groups = []
            raw_paths = self._auto_list_paths()
            if not raw_paths:
                return groups

            expanded = []
            for raw_path in raw_paths:
                path_obj = Path(str(raw_path)).expanduser()
                if not path_obj.exists():
                    continue
                expanded.append(path_obj)

            selected_dirs = []
            selected_dirs_set = set()
            file_groups_by_parent = {}

            for path_obj in expanded:
                if path_obj.is_dir():
                    dir_path = path_obj.as_posix()
                    if dir_path in selected_dirs_set:
                        continue
                    selected_dirs_set.add(dir_path)
                    selected_dirs.append(dir_path)
                    continue

                if path_obj.is_file():
                    parent_path = path_obj.parent.as_posix()
                    if parent_path in selected_dirs_set:
                        continue
                    file_groups_by_parent.setdefault(parent_path, [])
                    file_groups_by_parent[parent_path].append(path_obj.as_posix())

            for dir_path in selected_dirs:
                source_paths = [dir_path]
                detected_groups = MaterialChannelLibrary.discover_material_groups_from_paths(
                    source_paths
                )
                for detected in detected_groups:
                    groups.append(
                        {
                            "source_paths": source_paths,
                            "textures": detected["textures"],
                            "suggested_name": detected["suggested_name"],
                        }
                    )

            for _parent_path, file_paths in file_groups_by_parent.items():
                detected_groups = MaterialChannelLibrary.discover_material_groups_from_paths(
                    file_paths
                )
                for detected in detected_groups:
                    groups.append(
                        {
                            "source_paths": list(file_paths),
                            "textures": detected["textures"],
                            "suggested_name": detected["suggested_name"],
                        }
                    )

            return groups

        def _collect_auto_textures(self):
            groups = self._collect_auto_material_groups()
            if not groups:
                return {}
            return groups[0]["textures"]

        def _collect_textures(self):
            if self.auto_detect_check.isChecked():
                return self._collect_auto_textures()
            return self._collect_manual_textures()

        @staticmethod
        def _summary_text(textures):
            if not textures:
                return "Detected textures: 0"
            ordered = ", ".join(sorted(textures.keys()))
            return "Detected textures: {0} ({1})".format(len(textures), ordered)

        def _update_summary(self, textures):
            self.summary_label.setText(self._summary_text(textures))

        def _preview_textures(self):
            if self.auto_detect_check.isChecked():
                groups = self._collect_auto_material_groups()
                if not groups:
                    self._update_summary({})
                elif len(groups) == 1:
                    self._update_summary(groups[0]["textures"])
                else:
                    total_textures = sum(len(group["textures"]) for group in groups)
                    names = [
                        group["suggested_name"]
                        for group in groups
                        if group["suggested_name"]
                    ]
                    name_text = ", ".join(names[:4])
                    if len(names) > 4:
                        name_text += ", ..."
                    self.summary_label.setText(
                        "Detected materials: {0} | total textures: {1} ({2})".format(
                            len(groups),
                            total_textures,
                            name_text or "multiple groups",
                        )
                    )
            else:
                textures = self._collect_manual_textures()
                self._update_summary(textures)
            self._update_material_name_auto()

        def _set_create_button_state(self, level):
            if level == "success":
                self.create_btn.setStyleSheet(
                    "background-color: #2e7d32; border: 1px solid #4caf50; color: #ffffff;"
                )
            elif level == "error":
                self.create_btn.setStyleSheet(
                    "background-color: #8b2f2f; border: 1px solid #cc4b4b; color: #ffffff;"
                )
            else:
                self.create_btn.setStyleSheet("")

        def _set_info(self, message, level="info"):
            if level == "success":
                color = "#7ddc7f"
            elif level == "error":
                color = "#ff9a9a"
            else:
                color = "#d0d0d0"
            self.info_label.setStyleSheet(f"color: {color};")
            self.info_label.setText(message)
            self._set_create_button_state(level if level in {"success", "error"} else "default")

        def _set_busy(self, is_busy):
            self.create_btn.setEnabled(not is_busy)
            self.preview_btn.setEnabled(not is_busy)
            self.reset_btn.setEnabled(not is_busy)
            if is_busy:
                self.setCursor(QtCore.Qt.WaitCursor)
            else:
                self.unsetCursor()

        def _set_progress(self, current, total, label):
            total = max(1, int(total))
            current = max(0, min(int(current), total))
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.progress_bar.setFormat(label)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 50)

        def _reset_progress(self):
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Idle")

        def _suggest_material_name(self, textures):
            source_paths = self._material_name_source_paths()
            if source_paths:
                return MaterialChannelLibrary.suggest_material_name_from_paths(source_paths)
            if textures:
                return MaterialChannelLibrary.suggest_material_name_from_paths(
                    list(textures.values())
                )
            return "Material"

        def _reset_ui(self):
            self.material_name_edit.setText("Material")
            self.auto_name_check.setChecked(True)
            self.udim_check.setChecked(True)
            self.auto_detect_check.setChecked(True)
            self.auto_paths_list.clear()
            for field in self.manual_fields.values():
                field.clear()
            self._update_summary({})
            self._reset_progress()
            self._set_info("Reset complete.", "info")

        def _create_material(self):
            self._set_busy(True)
            try:
                if self.auto_detect_check.isChecked():
                    groups = self._collect_auto_material_groups()
                    if not groups:
                        self._set_progress(0, 1, "No textures found")
                        self._set_info(
                            "No textures found. Add folders/files that contain recognizable maps.",
                            "error",
                        )
                        return

                    total_groups = len(groups)
                    self._set_progress(0, total_groups, "Creating 0/{0}".format(total_groups))

                    custom_base = self.material_name_edit.text().strip()
                    created_paths = []
                    failed = []

                    for index, group in enumerate(groups, start=1):
                        if self.auto_name_check.isChecked():
                            material_name = group["suggested_name"] or f"Material_{index:02d}"
                        else:
                            if not custom_base:
                                custom_base = "Material"
                            if total_groups == 1:
                                material_name = custom_base
                            else:
                                suffix = group["suggested_name"] or f"{index:02d}"
                                material_name = f"{custom_base}_{suffix}"

                        self._set_progress(
                            index - 1,
                            total_groups,
                            "Creating {0}/{1}: {2}".format(
                                index - 1,
                                total_groups,
                                material_name,
                            ),
                        )

                        builder = MaterialBuilder(
                            material_name=material_name,
                            use_udim=self.udim_check.isChecked(),
                        )
                        for channel_name, texture_path in group["textures"].items():
                            builder.add_texture(channel_name, texture_path)

                        try:
                            _matlib, subnet = builder.build()
                            created_paths.append(subnet.path())
                        except Exception as exc:
                            failed.append((material_name, str(exc)))

                        self._set_progress(
                            index,
                            total_groups,
                            "Creating {0}/{1}".format(index, total_groups),
                        )

                    if created_paths and not failed:
                        self._set_progress(
                            total_groups,
                            total_groups,
                            "Completed {0}/{1}".format(total_groups, total_groups),
                        )
                        info_lines = ["Created {0} materials:".format(len(created_paths))]
                        for path in created_paths:
                            info_lines.append(path)
                        self._set_info("\n".join(info_lines), "success")
                        return

                    if created_paths and failed:
                        self._set_progress(
                            total_groups,
                            total_groups,
                            "Completed with errors",
                        )
                        first_failed_name, first_failed_error = failed[0]
                        self._set_info(
                            "Created {0}, failed {1}. First failure ({2}): {3}".format(
                                len(created_paths),
                                len(failed),
                                first_failed_name,
                                first_failed_error,
                            ),
                            "error",
                        )
                        return

                    if failed:
                        self._set_progress(total_groups, total_groups, "Failed")
                        first_failed_name, first_failed_error = failed[0]
                        self._set_info(
                            "Build failed ({0}): {1}".format(
                                first_failed_name,
                                first_failed_error,
                            ),
                            "error",
                        )
                        return

                textures = self._collect_manual_textures()
                if not textures:
                    self._set_progress(0, 1, "No textures found")
                    self._set_info(
                        "No textures found. Set channels manually or enable auto detect.",
                        "error",
                    )
                    return

                material_name = self.material_name_edit.text().strip()
                if not material_name:
                    material_name = self._suggest_material_name(textures)
                    self.material_name_edit.setText(material_name)

                self._set_progress(0, 1, "Creating 0/1: {0}".format(material_name))
                builder = MaterialBuilder(
                    material_name=material_name,
                    use_udim=self.udim_check.isChecked(),
                )
                for channel_name, texture_path in textures.items():
                    builder.add_texture(channel_name, texture_path)

                try:
                    _matlib, subnet = builder.build()
                except Exception as exc:
                    self._set_progress(1, 1, "Failed")
                    self._set_info(f"Build failed: {exc}", "error")
                    return

                self._set_progress(1, 1, "Completed 1/1")
                self._set_info(
                    "Material Created: {0} ({1})".format(
                        subnet.path(),
                        subnet.name(),
                    ),
                    "success",
                )
            finally:
                self._set_busy(False)

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QtWidgets.QApplication([])
    qt_parent = parent

    global _BMFX_MATERIAL_BUILDER_DIALOG
    try:
        existing_dialog = _BMFX_MATERIAL_BUILDER_DIALOG
    except NameError:
        existing_dialog = None

    if existing_dialog is not None:
        try:
            if existing_dialog.isVisible():
                existing_dialog.raise_()
                existing_dialog.activateWindow()
                return existing_dialog
        except Exception:
            pass

    dialog = MaterialXBuilderDialog(qt_parent)
    dialog.setWindowModality(QtCore.Qt.NonModal)
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)

    def _clear_dialog_reference(*_):
        global _BMFX_MATERIAL_BUILDER_DIALOG
        if _BMFX_MATERIAL_BUILDER_DIALOG is dialog:
            _BMFX_MATERIAL_BUILDER_DIALOG = None

    dialog.destroyed.connect(_clear_dialog_reference)
    _BMFX_MATERIAL_BUILDER_DIALOG = dialog

    dialog.show()
    dialog.raise_()
    dialog.activateWindow()

    if owns_app:
        app.exec_()

    return dialog


if __name__ == "__main__":
    show_ui()
