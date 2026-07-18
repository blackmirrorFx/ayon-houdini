"""
LOPs material builder for Solaris.

Features:
- Creates a Material Library LOP in /stage.
- Creates MaterialX or PXR shader networks from texture files.
- Supports UDIM token conversion (<UDIM>) via UI toggle.
"""

import os
import re
import random
import shutil
import subprocess
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

MATERIAL_BUILDERS = (
    ("materialx", "MaterialX"),
    ("pxr", "PXR"),
)

MATERIAL_BUILDER_LABELS = {key: label for key, label in MATERIAL_BUILDERS}


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


class TextureConverter:
    @staticmethod
    def _rman_tree():
        if hou:
            try:
                rman_tree = hou.getenv("RMANTREE")
                if rman_tree:
                    return rman_tree
            except Exception:
                pass
        return os.environ.get("RMANTREE")

    @classmethod
    def find_converter(cls):
        candidates = []

        rman_tree = cls._rman_tree()
        if rman_tree:
            candidates.append(("txmake", Path(rman_tree) / "bin" / "txmake"))

        txmake_path = shutil.which("txmake")
        if txmake_path:
            candidates.append(("txmake", Path(txmake_path)))

        maketx_path = shutil.which("maketx")
        if maketx_path:
            candidates.append(("maketx", Path(maketx_path)))

        for tool_name, tool_path in candidates:
            try:
                if tool_path.exists():
                    return tool_name, tool_path.as_posix()
            except Exception:
                continue

        return None, None

    @staticmethod
    def _ocio_config():
        """Return the active OCIO config path, or None."""
        if hou:
            try:
                cfg = hou.getenv("OCIO")
                if cfg:
                    return cfg
            except Exception:
                pass
        return os.environ.get("OCIO")

    @classmethod
    def _run_converter(cls, cmd):
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raw_error = (result.stderr or result.stdout or "").strip()
            error_line = raw_error.splitlines()[-1] if raw_error else "unknown error"
            raise RuntimeError(error_line)

    @classmethod
    def convert_to_tex(cls, source_path):
        source = Path(str(source_path)).expanduser()
        if not source.exists() or not source.is_file():
            raise RuntimeError(f"Texture file not found: {source.as_posix()}")

        if source.suffix.lower() == ".tex":
            return source.as_posix(), False

        destination = source.with_suffix(".tex")
        try:
            if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
                return destination.as_posix(), False
        except Exception:
            pass

        tool_name, executable = cls.find_converter()
        if not executable:
            raise RuntimeError(
                "Could not find txmake/maketx. Ensure RenderMan is installed and RMANTREE is set."
            )

        if tool_name == "maketx":
            cmd = [executable, source.as_posix(), "-o", destination.as_posix()]
        else:
            cmd = [executable, source.as_posix(), destination.as_posix()]

        try:
            cls._run_converter(cmd)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed converting '{source.name}' with {tool_name}: {exc}"
            ) from exc

        if not destination.exists():
            raise RuntimeError(
                f"Conversion finished but .tex output was not found: {destination.as_posix()}"
            )

        return destination.as_posix(), True

    @classmethod
    def convert_to_tex_aces(
        cls,
        source_path,
        raw_channel=False,
        src_colorspace="sRGB",
        dst_colorspace="ACES - ACEScg",
    ):
        """Convert a texture to .tex with optional ACES colorspace transform.

        Args:
            source_path: Source texture file path.
            raw_channel: If True the texture is a data map (normal, roughness,
                metalness, etc.) and no colorspace conversion is applied.
                If False (color map) the src->dst colorspace conversion is baked in.
            src_colorspace: Source colorspace name (maketx / OCIO).
            dst_colorspace: Target colorspace name (maketx / OCIO).

        Returns:
            (destination_path, was_converted) tuple.
        """
        source = Path(str(source_path)).expanduser()
        if not source.exists() or not source.is_file():
            raise RuntimeError(f"Texture file not found: {source.as_posix()}")

        if source.suffix.lower() == ".tex":
            return source.as_posix(), False

        destination = source.with_suffix(".tex")
        try:
            if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
                return destination.as_posix(), False
        except Exception:
            pass

        tool_name, executable = cls.find_converter()
        if not executable:
            raise RuntimeError(
                "Could not find txmake/maketx. Ensure RenderMan is installed and RMANTREE is set."
            )

        if tool_name == "maketx":
            cmd = [executable, source.as_posix(), "-o", destination.as_posix()]
            if raw_channel:
                cmd += ["--nocolorconvert"]
            else:
                cmd += ["--colorconvert", src_colorspace, dst_colorspace]
                ocio = cls._ocio_config()
                if ocio:
                    cmd += ["--ocioconfig", ocio]
        else:
            # txmake (RenderMan) — colorspace is managed at render time by
            # RenderMan's OCIO integration via the PxrTexture node's colorspace
            # parameter (set by _apply_colorspace). txmake does not accept
            # OpenImageIO-style --colorconvert flags, so we do a plain conversion.
            cmd = [executable, source.as_posix(), destination.as_posix()]

        try:
            cls._run_converter(cmd)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed ACES converting '{source.name}' with {tool_name}: {exc}"
            ) from exc

        if not destination.exists():
            raise RuntimeError(
                f"ACES conversion finished but .tex output was not found: {destination.as_posix()}"
            )

        return destination.as_posix(), True

    @classmethod
    def convert_to_tx(cls, source_path):
        """Convert a texture to .tx (Arnold / OpenImageIO maketx).

        Always uses ``maketx``; txmake cannot produce .tx files.
        Returns (destination_path, was_converted).
        """
        source = Path(str(source_path)).expanduser()
        if not source.exists() or not source.is_file():
            raise RuntimeError(f"Texture file not found: {source.as_posix()}")

        if source.suffix.lower() == ".tx":
            return source.as_posix(), False

        destination = source.with_suffix(".tx")
        try:
            if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
                return destination.as_posix(), False
        except Exception:
            pass

        maketx_path = shutil.which("maketx")
        if not maketx_path:
            raise RuntimeError(
                "maketx not found. Install OpenImageIO (oiiotool/maketx) to convert to .tx."
            )

        cmd = [maketx_path, source.as_posix(), "-o", destination.as_posix()]
        try:
            cls._run_converter(cmd)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed converting '{source.name}' to .tx with maketx: {exc}"
            ) from exc

        if not destination.exists():
            raise RuntimeError(
                f"Conversion finished but .tx output was not found: {destination.as_posix()}"
            )

        return destination.as_posix(), True

    @classmethod
    def convert_to_tx_aces(
        cls,
        source_path,
        raw_channel=False,
        src_colorspace="sRGB",
        dst_colorspace="ACES - ACEScg",
    ):
        """Convert a texture to .tx with optional ACES colorspace transform.

        Always uses maketx (OpenImageIO). raw_channel skips colorspace conversion.
        Returns (destination_path, was_converted).
        """
        source = Path(str(source_path)).expanduser()
        if not source.exists() or not source.is_file():
            raise RuntimeError(f"Texture file not found: {source.as_posix()}")

        if source.suffix.lower() == ".tx":
            return source.as_posix(), False

        destination = source.with_suffix(".tx")
        try:
            if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
                return destination.as_posix(), False
        except Exception:
            pass

        maketx_path = shutil.which("maketx")
        if not maketx_path:
            raise RuntimeError(
                "maketx not found. Install OpenImageIO (oiiotool/maketx) to convert to .tx."
            )

        cmd = [maketx_path, source.as_posix(), "-o", destination.as_posix()]
        if raw_channel:
            cmd += ["--nocolorconvert"]
        else:
            cmd += ["--colorconvert", src_colorspace, dst_colorspace]
            ocio = cls._ocio_config()
            if ocio:
                cmd += ["--ocioconfig", ocio]

        try:
            cls._run_converter(cmd)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed ACES converting '{source.name}' to .tx with maketx: {exc}"
            ) from exc

        if not destination.exists():
            raise RuntimeError(
                f"ACES conversion finished but .tx output was not found: {destination.as_posix()}"
            )

        return destination.as_posix(), True


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
        best_token_idx = -1  # rightmost position wins; channel suffix is always at the end

        for alias, channel in cls._alias_to_channel.items():
            alias_len = len(alias)
            alias_clean = alias.replace("_", "")

            # Find the rightmost token that matches this alias.
            token_idx = -1
            for i, token in enumerate(tokens):
                if token == alias or token == alias_clean:
                    token_idx = i  # keep updating — we want the rightmost hit

            if token_idx >= 0:
                # A direct token match: prefer rightmost position, break ties by alias length.
                if token_idx > best_token_idx or (
                    token_idx == best_token_idx and alias_len > best_alias_len
                ):
                    best_token_idx = token_idx
                    best_alias_len = alias_len
                    best_match = channel
            elif best_token_idx < 0 and alias_clean in normalized_stem:
                # Normalised-stem fallback only when no direct token match found at all.
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
            if lowered in {"tex", "texture", "map", "udim", "gl", "ogl"}:
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
    MATERIALX = "materialx"
    PXR = "pxr"

    def __init__(self, material_name, use_udim=False, builder_type=MATERIALX, use_aces=False):
        self.material_name = self._sanitize_name(material_name)
        self.use_udim = bool(use_udim)
        self.use_aces = bool(use_aces)
        self.builder_type = self._normalize_builder_type(builder_type)
        self.textures = {}

    @classmethod
    def _normalize_builder_type(cls, builder_type):
        normalized = str(builder_type or "").strip().lower()
        if normalized == cls.PXR:
            return cls.PXR
        return cls.MATERIALX

    @staticmethod
    def _sanitize_name(name):
        clean = re.sub(r"[^a-zA-Z0-9_]+", "_", (name or "").strip())
        return clean or "Material"

    @staticmethod
    def _normalize_lookup_key(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

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

        if self.builder_type == self.PXR:
            material_subnet_types = ("pxrmaterialbuilder",)
        else:
            material_subnet_types = ("subnet", "subnetvop")

        material_subnet = self._create_node_with_fallback(
            material_parent,
            target_name,
            material_subnet_types,
        )
        try:
            material_subnet.setMaterialFlag(True)
        except Exception:
            pass
        self._set_random_node_color(material_subnet)

        if self.builder_type == self.PXR:
            # Keep default PXR builder I/O so RenderMan outputs remain valid.
            shader = self._create_surface_shader(material_subnet)
            self._connect_pxr_outputs(material_subnet, surface_source=shader)
            self._connect_textures(material_subnet, shader, None)
        else:
            self._remove_default_subnet_io(material_subnet)

            shader = self._create_surface_shader(material_subnet)

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

    @staticmethod
    def _node_type_base_name(node_type_name):
        return str(node_type_name or "").split("::", 1)[0]

    @staticmethod
    def _node_type_sort_key(node_type_name):
        parts = str(node_type_name or "").split("::", 1)
        if len(parts) < 2:
            return ((), "")
        suffix = parts[1]
        tokens = tuple(int(token) for token in re.findall(r"\d+", suffix))
        return (tokens, suffix.lower())

    @classmethod
    def _resolve_node_type_name(cls, parent, node_type):
        try:
            category = parent.childTypeCategory()
        except Exception:
            return None
        if not category:
            return None

        try:
            node_types = category.nodeTypes()
        except Exception:
            return None

        if not node_types:
            return None

        node_type_str = str(node_type)
        if node_type_str in node_types:
            return node_type_str

        target_base = cls._node_type_base_name(node_type_str).lower()
        matches = [
            name
            for name in node_types.keys()
            if cls._node_type_base_name(name).lower() == target_base
        ]
        if not matches:
            return None

        matches.sort(key=cls._node_type_sort_key, reverse=True)
        return matches[0]

    @classmethod
    def _find_child_by_type_base(cls, parent, type_bases):
        expected = {cls._normalize_lookup_key(item) for item in type_bases}
        for child in parent.children():
            try:
                type_name = child.type().name()
            except Exception:
                continue
            base_name = cls._normalize_lookup_key(cls._node_type_base_name(type_name))
            if base_name in expected:
                return child
        return None

    @classmethod
    def _create_node_with_fallback(cls, parent, node_name, node_types):
        tried_types = []
        last_exc = None
        for requested_type in node_types:
            candidate_types = [requested_type]
            resolved_type = cls._resolve_node_type_name(parent, requested_type)
            if resolved_type and resolved_type not in candidate_types:
                candidate_types.append(resolved_type)

            for node_type in candidate_types:
                if node_type in tried_types:
                    continue
                tried_types.append(node_type)
                try:
                    return cls._create_node(parent, node_type, node_name)
                except RuntimeError as exc:
                    last_exc = exc
                    continue

        parent_path = parent.path() if hasattr(parent, "path") else str(parent)
        tried = ", ".join(str(node_type) for node_type in (tried_types or node_types))
        raise RuntimeError(
            f"Failed to create node '{node_name}' in '{parent_path}'. Tried: {tried}. "
            f"Last error: {last_exc}"
        )

    @classmethod
    def _try_create_node_with_fallback(cls, parent, node_name, node_types):
        try:
            return cls._create_node_with_fallback(parent, node_name, node_types)
        except Exception:
            return None

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
            try:
                index = node.inputIndex(input_name)
            except Exception:
                continue
            if index >= 0:
                node.setInput(index, source_node)
                return True
        return False

    @classmethod
    def _set_input_by_keywords(cls, node, keywords, source_node):
        normalized_keywords = [
            cls._normalize_lookup_key(keyword)
            for keyword in keywords
            if cls._normalize_lookup_key(keyword)
        ]
        if not normalized_keywords:
            return False

        try:
            input_names = node.inputNames()
        except Exception:
            return False

        for index, input_name in enumerate(input_names):
            normalized_input = cls._normalize_lookup_key(input_name)
            if not normalized_input:
                continue
            for keyword in normalized_keywords:
                if keyword in normalized_input:
                    try:
                        node.setInput(index, source_node)
                        return True
                    except Exception:
                        continue
        return False

    def _connect_shader_input(self, shader, source_node, input_names=(), keyword_names=()):
        if shader is None or source_node is None:
            return False
        if input_names and self._set_input_by_names(shader, input_names, source_node):
            return True
        keywords = keyword_names or input_names
        if keywords:
            return self._set_input_by_keywords(shader, keywords, source_node)
        return False

    @staticmethod
    def _input_names(node):
        try:
            return tuple(node.inputNames())
        except Exception:
            return tuple()

    @classmethod
    def _node_has_input_keywords(cls, node, keywords):
        normalized_keywords = [
            cls._normalize_lookup_key(keyword)
            for keyword in keywords
            if cls._normalize_lookup_key(keyword)
        ]
        if not normalized_keywords:
            return False

        for input_name in cls._input_names(node):
            normalized_input = cls._normalize_lookup_key(input_name)
            if not normalized_input:
                continue
            for keyword in normalized_keywords:
                if keyword in normalized_input:
                    return True
        return False

    @classmethod
    def _find_pxr_output_node(cls, parent):
        named_collect = parent.node("output_collect")
        if named_collect is not None:
            return named_collect

        named = parent.node("suboutput1")
        if named is not None:
            return named

        type_priority = (
            ("collect", "collectvop", "output_collect", "pxrmaterialoutput"),
            ("suboutput", "subnetconnector"),
        )

        for type_bases in type_priority:
            for child in parent.children():
                try:
                    type_name = child.type().name()
                except Exception:
                    continue
                base_name = cls._normalize_lookup_key(cls._node_type_base_name(type_name))
                expected = {cls._normalize_lookup_key(item) for item in type_bases}
                if base_name not in expected:
                    continue
                if "collect" in base_name:
                    return child
                if cls._node_has_input_keywords(
                    child,
                    ("bxdf", "surface", "ri:bxdf", "displacement", "ri:displacement"),
                ):
                    return child

        for child in parent.children():
            if cls._node_has_input_keywords(
                child,
                ("bxdf", "surface", "ri:bxdf", "displacement", "ri:displacement"),
            ):
                return child

        # Last-resort fallback: some builders may not expose a pre-created output node.
        try:
            return cls._create_node_with_fallback(parent, "suboutput1", ("suboutput", "subnetconnector"))
        except Exception:
            return None

    @staticmethod
    def _is_input_connected(target_node, source_node):
        try:
            for connected in target_node.inputs():
                if connected == source_node:
                    return True
        except Exception:
            return False
        return False

    def _connect_pxr_outputs(self, parent, surface_source=None, displacement_source=None):
        output_node = self._find_pxr_output_node(parent)
        if output_node is None:
            return False

        connected = False
        try:
            output_base = self._normalize_lookup_key(
                self._node_type_base_name(output_node.type().name())
            )
        except Exception:
            output_base = ""
        is_collect_output = "collect" in output_base

        def _output_index(node, output_names):
            for output_name in output_names:
                try:
                    index = node.outputIndex(output_name)
                except Exception:
                    continue
                if index >= 0:
                    return index
            return 0

        if surface_source is not None:
            connected_surface = False
            source_output_names = ("bxdf_out", "bxdf", "surface", "out", "Shader")
            surface_output_index = _output_index(
                surface_source,
                source_output_names,
            )
            surface_input_names = (
                "shader1",
                "shader",
                "material1",
                "material",
                "bxdf",
                "surface",
                "ri:bxdf",
                "input1",
                "input",
            )
            for input_name in surface_input_names:
                for output_name in source_output_names:
                    try:
                        output_node.setNamedInput(input_name, surface_source, output_name)
                        if self._is_input_connected(output_node, surface_source):
                            connected_surface = True
                            break
                    except Exception:
                        continue
                if connected_surface:
                    break
            if not connected_surface:
                index_candidates = (0, 1, 2) if is_collect_output else (0, 2)
                for input_index in index_candidates:
                    try:
                        output_node.setInput(input_index, surface_source, surface_output_index)
                        if self._is_input_connected(output_node, surface_source):
                            connected_surface = True
                            break
                    except Exception:
                        continue
            try:
                if not connected_surface:
                    connected_surface = self._connect_shader_input(
                        output_node,
                        surface_source,
                        (
                            "shader1",
                            "shader",
                            "material1",
                            "bxdf",
                            "surface",
                            "ri:bxdf",
                            "input1",
                        ),
                        (
                            "shader1",
                            "shader",
                            "material1",
                            "bxdf",
                            "surface",
                            "ribxdf",
                            "input1",
                        ),
                    )
            except Exception:
                connected_surface = False
            connected = connected_surface or connected

        if displacement_source is not None:
            connected_displacement = False
            source_output_names = ("displace_out", "displacement", "out")
            displacement_output_index = _output_index(
                displacement_source,
                source_output_names,
            )
            for input_name in ("displacement", "displace", "ri:displacement", "input2"):
                for output_name in source_output_names:
                    try:
                        output_node.setNamedInput(input_name, displacement_source, output_name)
                        if self._is_input_connected(output_node, displacement_source):
                            connected_displacement = True
                            break
                    except Exception:
                        continue
                if connected_displacement:
                    break
            if not connected_displacement:
                for input_index in (1, 3):
                    try:
                        output_node.setInput(input_index, displacement_source, displacement_output_index)
                        if self._is_input_connected(output_node, displacement_source):
                            connected_displacement = True
                            break
                    except Exception:
                        continue
            try:
                if not connected_displacement:
                    connected_displacement = self._connect_shader_input(
                        output_node,
                        displacement_source,
                        ("displacement", "displace", "ri:displacement", "input2"),
                        ("displacement", "displace", "ridisplacement", "input2"),
                    )
            except Exception:
                connected_displacement = False
            connected = connected_displacement or connected

        return connected

    @staticmethod
    def _try_set_colorspace_parm(parm, value):
        """Set a colorspace parm by token string or menu index (handles ordinal menus).

        PxrTexture uses an ordinal (integer) menu, so parm.set(string) raises.
        We fall back to scanning menuItems() and setting by index.
        Returns True if the value was applied.
        """
        # Try direct string assignment (works for string-menu parms like mtlximage)
        try:
            parm.set(value)
            return True
        except Exception:
            pass
        # Fall back: find the token index in the menu and set by integer
        try:
            items = parm.menuItems()
            for i, token in enumerate(items):
                if token.lower() == value.lower():
                    parm.set(i)
                    return True
        except Exception:
            pass
        return False

    @classmethod
    def _set_raw_colorspace(cls, tex_node):
        for parm_name in ("filename_colorspace", "colorspace", "filecolorspace"):
            parm = tex_node.parm(parm_name)
            if parm is None:
                continue
            # PxrTexture 3.x token first, then OCIO/MaterialX fallbacks
            for raw_value in ("data", "raw", "Raw", "Utility - Raw", "linear"):
                if cls._try_set_colorspace_parm(parm, raw_value):
                    return

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

    def _create_surface_shader(self, parent):
        if self.builder_type == self.PXR:
            existing_shader = self._find_child_by_type_base(parent, ("pxrsurface", "pxrdisney"))
            if existing_shader is not None:
                return existing_shader
            return self._create_node_with_fallback(
                parent,
                "surface_shader",
                ("pxrsurface", "pxrdisney"),
            )

        return self._create_node_with_fallback(
            parent,
            "standard_surface",
            ("mtlxstandard_surface", "mtlxstandardsurface"),
        )

    def _create_texture_node(self, parent, channel_name, texture_path):
        if self.builder_type == self.PXR:
            return self._create_pxr_texture_node(parent, channel_name, texture_path)
        return self._create_materialx_texture_node(parent, channel_name, texture_path)

    # ACES colorspace strings for color map inputs on MaterialX image nodes.
    _ACES_COLOR_COLORSPACE = "ACES - ACEScg"
    # Candidates for the raw (data) colorspace parm value.
    # PxrTexture 3.x uses "data"; OCIO configs use "raw" / "Utility - Raw".
    _RAW_COLORSPACE_VALUES = ("data", "raw", "Raw", "Utility - Raw", "linear")
    # Candidates for the ACES working-space colorspace parm value.
    # PxrTexture 3.x uses "rendering"; OCIO configs use "ACES - ACEScg".
    _ACES_COLORSPACE_VALUES = ("rendering", "ACES - ACEScg", "acescg", "ACEScg")
    # Candidates for sRGB color maps (non-ACES mode).
    # PxrTexture 3.x uses "srgb_texture".
    _SRGB_COLORSPACE_VALUES = ("srgb_texture", "sRGB", "srgb", "Utility - sRGB - Texture")

    def _apply_colorspace(self, tex_node, channel_name):
        """Set the appropriate colorspace on *tex_node* based on channel type and ACES mode."""
        channel = MaterialChannelLibrary.channel(channel_name)
        is_raw = channel.raw_colorspace if channel else True

        if is_raw:
            # Data maps (roughness, normal, metalness, AO, etc.) → data / raw
            self._set_raw_colorspace(tex_node)
        else:
            # Color maps (base color, emission, specular…)
            candidates = self._ACES_COLORSPACE_VALUES if self.use_aces else self._SRGB_COLORSPACE_VALUES
            for parm_name in ("filename_colorspace", "colorspace", "filecolorspace"):
                parm = tex_node.parm(parm_name)
                if parm is None:
                    continue
                for value in candidates:
                    if self._try_set_colorspace_parm(parm, value):
                        return

    def _create_materialx_texture_node(self, parent, channel_name, texture_path):
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
        self._apply_colorspace(tex_node, channel_name)
        return tex_node

    # Channels that should have Linearize enabled when reading gamma-encoded files.
    _LINEARIZE_CHANNELS = frozenset(("base_color", "emission_color"))
    # Formats stored in gamma/sRGB space that benefit from linearization at read time.
    _LINEARIZE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".tex"))

    def _apply_linearize(self, tex_node, channel_name, texture_path):
        """Enable the Linearize checkbox on *tex_node* for color channels
        read from gamma-encoded image formats (.jpg / .png)."""
        if channel_name not in self._LINEARIZE_CHANNELS:
            return
        ext = Path(str(texture_path)).suffix.lower()
        if ext not in self._LINEARIZE_EXTENSIONS:
            return
        parm = tex_node.parm("linearize")
        if parm is not None:
            try:
                parm.set(1)
            except Exception:
                pass

    def _create_pxr_texture_node(self, parent, channel_name, texture_path):
        tex_node = self._create_node_with_fallback(
            parent,
            "tex_" + channel_name,
            ("pxrtexture",),
        )
        if not self._set_first_available_parm(
            tex_node,
            ("filename", "filename0", "file", "tex0", "map"),
            texture_path,
        ):
            raise RuntimeError(
                "Could not set texture path on node '{0}' ({1}).".format(
                    tex_node.path(),
                    tex_node.type().name(),
                )
            )
        self._set_node_color(tex_node, channel_name)
        self._apply_colorspace(tex_node, channel_name)
        self._apply_linearize(tex_node, channel_name, texture_path)
        return tex_node

    def _create_pxr_to_float(self, parent, node_name, source_node):
        to_float = self._create_node_with_fallback(
            parent,
            node_name,
            ("pxrtofloat",),
        )
        try:
            to_float.setNamedInput("input", source_node, "resultRGB")
        except Exception:
            to_float.setInput(0, source_node, 0)
        return to_float

    @classmethod
    def _create_pxr_tile_manifold(cls, parent):
        """Create a single PxrTileManifold node to drive all PXR texture nodes."""
        return cls._try_create_node_with_fallback(
            parent,
            "tile_manifold",
            ("pxrtilemanifold",),
        )

    @classmethod
    def _connect_manifold_to_all_textures(cls, parent, manifold_node):
        """Wire manifold_node.result -> manifold input of every PxrTexture child."""
        if manifold_node is None:
            return
        for child in parent.children():
            try:
                base_type = cls._node_type_base_name(child.type().name()).lower()
            except Exception:
                continue
            if "pxrtexture" not in base_type:
                continue

            # Resolve the actual index of the 'manifold' input on this node.
            # We MUST check this first — setNamedInput on VOP nodes does NOT
            # raise for unknown input names; it silently connects to index 0
            # and would clobber existing shader-facing connections.
            manifold_idx = -1
            try:
                manifold_idx = child.inputIndex("manifold")
            except Exception:
                pass

            if manifold_idx < 0:
                # Try iterating the input names list as a fallback
                try:
                    for i, iname in enumerate(child.inputNames()):
                        if "manifold" in iname.lower():
                            manifold_idx = i
                            break
                except Exception:
                    pass

            if manifold_idx < 0:
                continue

            # Connect using the resolved index so we never touch the wrong slot.
            for out_name in ("result", "resultS", "out"):
                try:
                    child.setInput(manifold_idx, manifold_node, 0)
                    break
                except Exception:
                    continue


    @classmethod
    def _connect_to_pxr_normal_input(cls, normal_map_node, texture_node):
        output_names = ("resultRGB", "result", "out", "rgb")
        input_names = ("inputRGB", "input", "in")

        for input_name in input_names:
            for output_name in output_names:
                try:
                    normal_map_node.setNamedInput(input_name, texture_node, output_name)
                    return True
                except Exception:
                    continue

        if cls._set_input_by_names(normal_map_node, input_names, texture_node):
            return True

        # Last fallback for node variants that only expose positional inputs.
        try:
            normal_map_node.setInput(1, texture_node, 0)
            return True
        except Exception:
            pass
        try:
            normal_map_node.setInput(0, texture_node, 0)
            return True
        except Exception:
            return False

    def _connect_base_and_ao_materialx(self, parent, shader):
        base_path = self.textures.get("base_color")
        ao_path = self.textures.get("ao")

        if not base_path:
            return

        base_tex = self._create_texture_node(parent, "base_color", base_path)
        if not ao_path:
            self._connect_shader_input(shader, base_tex, ("base_color",))
            return

        ao_tex = self._create_texture_node(parent, "ao", ao_path)

        convert = self._create_node(parent, "mtlxconvert", "ao_to_color3")
        self._set_first_available_parm(convert, ("outtype", "type", "signature"), "color3")
        convert.setInput(0, ao_tex)

        multiply = self._create_node(parent, "mtlxmultiply", "base_ao_multiply")
        multiply.setInput(0, base_tex)
        multiply.setInput(1, convert)

        self._connect_shader_input(shader, multiply, ("base_color",))

    def _connect_base_and_ao_pxr(self, parent, shader):
        base_path = self.textures.get("base_color")
        if base_path:
            base_tex = self._create_texture_node(parent, "base_color", base_path)
            self._connect_shader_input(
                shader,
                base_tex,
                ("baseColor", "base_color", "diffuseColor"),
                ("basecolor", "diffusecolor"),
            )

        ao_path = self.textures.get("ao")
        if ao_path:
            ao_tex = self._create_texture_node(parent, "ao", ao_path)
            self._connect_shader_input(
                shader,
                ao_tex,
                ("ambientOcclusion", "occlusion", "ao"),
                ("ambientocclusion", "occlusion", "ao"),
            )

    def _connect_textures(self, parent, shader, displacement_output):
        if self.builder_type == self.PXR:
            self._connect_textures_pxr(parent, shader, displacement_output)
            return
        self._connect_textures_materialx(parent, shader, displacement_output)

    def _connect_textures_materialx(self, parent, shader, displacement_output):
        self._connect_base_and_ao_materialx(parent, shader)

        roughness_path = self.textures.get("roughness")
        if roughness_path:
            roughness_tex = self._create_texture_node(parent, "roughness", roughness_path)
            self._connect_shader_input(shader, roughness_tex, ("specular_roughness",))
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
                self._connect_shader_input(shader, invert_gloss, ("specular_roughness",))

        metalness_path = self.textures.get("metalness")
        if metalness_path:
            metalness_tex = self._create_texture_node(parent, "metalness", metalness_path)
            self._connect_shader_input(shader, metalness_tex, ("metalness",))

        normal_path = self.textures.get("normal")
        if normal_path:
            normal_tex = self._create_texture_node(parent, "normal", normal_path)
            normal_map = self._create_node(parent, "mtlxnormalmap", "normal_map")
            normal_map.setInput(0, normal_tex)
            self._connect_shader_input(shader, normal_map, ("normal",))

        displacement_path = self.textures.get("displacement")
        if displacement_path:
            displacement_tex = self._create_texture_node(parent, "displacement", displacement_path)
            displacement = self._create_node(parent, "mtlxdisplacement", "displacement")
            displacement.setInput(0, displacement_tex)
            scale_parm = displacement.parm("scale")
            if scale_parm:
                scale_parm.set(0.001)
            self._connect_output(displacement_output, displacement)

        opacity_path = self.textures.get("opacity")
        if opacity_path:
            opacity_tex = self._create_texture_node(parent, "opacity", opacity_path)
            self._connect_shader_input(shader, opacity_tex, ("opacity",))

        emission_path = self.textures.get("emission_color")
        if emission_path:
            emission_tex = self._create_texture_node(parent, "emission_color", emission_path)
            self._connect_shader_input(
                shader,
                emission_tex,
                ("emission_color", "emission"),
                ("emissioncolor", "emission"),
            )

        specular_path = self.textures.get("specular")
        if specular_path:
            specular_tex = self._create_texture_node(parent, "specular", specular_path)
            self._connect_shader_input(shader, specular_tex, ("specular",))

        transmission_path = self.textures.get("transmission")
        if transmission_path:
            transmission_tex = self._create_texture_node(parent, "transmission", transmission_path)
            self._connect_shader_input(shader, transmission_tex, ("transmission",))

        translucency_path = self.textures.get("translucency")
        if translucency_path:
            translucency_tex = self._create_texture_node(parent, "translucency", translucency_path)
            self._connect_shader_input(
                shader,
                translucency_tex,
                ("transmission_color", "transmission"),
                ("transmissioncolor", "transmission"),
            )

        subsurface_path = self.textures.get("subsurface")
        if subsurface_path:
            subsurface_tex = self._create_texture_node(parent, "subsurface", subsurface_path)
            self._connect_shader_input(shader, subsurface_tex, ("subsurface",))

    def _create_pxr_metallic_workflow(self, parent, base_tex, metalness_to_float, specular_tex, shader):
        """Create a PxrMetallicWorkflow node and wire it into the surface shader.

        Inputs (specular is optional):
          baseColor ← base_color texture (RGB)
          metallic  ← metalness_to_float (scalar)
          specular  ← specular texture (RGB, optional)

        Outputs wired to PxrSurface:
          resultDiffuse           → diffuseColor
          resultSpecularEdgeColor → specularEdgeColor
          resultSpecularFaceColor → specularFaceColor
        """
        metallic_workflow = self._try_create_node_with_fallback(
            parent,
            "pxrmetallicworkflow1",
            ("pxrmetallicworkflow",),
        )
        if metallic_workflow is None:
            return False

        def _resolve_input_idx(node, name):
            """Return the index of a named input, or -1 if not found."""
            try:
                idx = node.inputIndex(name)
                if idx >= 0:
                    return idx
            except Exception:
                pass
            # scan input names as fallback
            try:
                for i, iname in enumerate(node.inputNames()):
                    if name.lower() == iname.lower():
                        return i
            except Exception:
                pass
            return -1

        def _resolve_output_idx(node, name, positional_fallback):
            """Return the index of a named output, or positional_fallback."""
            try:
                idx = node.outputIndex(name)
                if idx >= 0:
                    return idx
            except Exception:
                pass
            try:
                for i, oname in enumerate(node.outputNames()):
                    if name.lower() == oname.lower():
                        return i
            except Exception:
                pass
            return positional_fallback

        def _wire_to_workflow(wf_input_name, source_node, out_candidates):
            """Connect source_node → metallic_workflow input by resolved index."""
            if source_node is None:
                return
            idx = _resolve_input_idx(metallic_workflow, wf_input_name)
            if idx < 0:
                return
            for out_name in out_candidates:
                out_idx = _resolve_output_idx(source_node, out_name, -1)
                if out_idx < 0:
                    continue
                try:
                    metallic_workflow.setInput(idx, source_node, out_idx)
                    return
                except Exception:
                    continue
            # last resort: connect output 0
            try:
                metallic_workflow.setInput(idx, source_node, 0)
            except Exception:
                pass

        # ── Wire inputs into the workflow ─────────────────────────────────────
        _wire_to_workflow("baseColor", base_tex,            ("resultRGB", "result", "out"))
        _wire_to_workflow("metallic",  metalness_to_float,  ("resultF",   "result", "out"))
        _wire_to_workflow("specular",  specular_tex,        ("resultRGB", "result", "out"))

        # ── Wire workflow outputs → PxrSurface inputs ─────────────────────────
        # (wf_output_name, positional_fallback_index, shader_input_name)
        _output_map = (
            ("resultDiffuse",           0, "diffuseColor"),
            ("resultSpecularEdgeColor", 1, "specularEdgeColor"),
            ("resultSpecularFaceColor", 2, "specularFaceColor"),
        )
        for wf_out_name, wf_out_fallback, shader_in_name in _output_map:
            wf_out_idx  = _resolve_output_idx(metallic_workflow, wf_out_name, wf_out_fallback)
            shader_in_idx = _resolve_input_idx(shader, shader_in_name)
            if shader_in_idx < 0:
                continue
            try:
                shader.setInput(shader_in_idx, metallic_workflow, wf_out_idx)
            except Exception:
                pass

        return True

    def _connect_textures_pxr(self, parent, shader, displacement_output):
        # Create a single tile manifold that will drive UV tiling for all textures.
        manifold = self._create_pxr_tile_manifold(parent)

        metalness_path = self.textures.get("metalness")

        # ── Base colour / AO ──────────────────────────────────────────────────
        # When a metalness texture is present we delay wiring base_color into
        # the surface shader; it will be fed through PxrMetallicWorkflow instead.
        base_path = self.textures.get("base_color")
        base_tex = None
        if base_path:
            base_tex = self._create_texture_node(parent, "base_color", base_path)

        ao_path = self.textures.get("ao")
        if ao_path:
            ao_tex = self._create_texture_node(parent, "ao", ao_path)
            self._connect_shader_input(
                shader,
                ao_tex,
                ("ambientOcclusion", "occlusion", "ao"),
                ("ambientocclusion", "occlusion", "ao"),
            )

        # ── Roughness / Glossiness ────────────────────────────────────────────
        roughness_path = self.textures.get("roughness")
        if roughness_path:
            roughness_tex = self._create_texture_node(parent, "roughness", roughness_path)
            roughness_to_float = self._create_pxr_to_float(parent, "roughness_to_float", roughness_tex)
            self._connect_shader_input(
                shader,
                roughness_to_float,
                ("roughness", "specularRoughness"),
                ("roughness", "specularroughness"),
            )
        else:
            gloss_path = self.textures.get("glossiness")
            if gloss_path:
                gloss_tex = self._create_texture_node(parent, "glossiness", gloss_path)
                roughness_source = gloss_tex
                invert_gloss = self._try_create_node_with_fallback(
                    parent,
                    "invert_gloss",
                    ("pxrinvert", "mtlxremap", "invert"),
                )
                if invert_gloss is not None:
                    outlow = invert_gloss.parm("outlow")
                    outhigh = invert_gloss.parm("outhigh")
                    if outlow is not None:
                        outlow.set(1.0)
                    if outhigh is not None:
                        outhigh.set(0.0)
                    invert_gloss.setInput(0, gloss_tex)
                    roughness_source = invert_gloss
                roughness_to_float = self._create_pxr_to_float(
                    parent,
                    "glossiness_to_float",
                    roughness_source,
                )
                self._connect_shader_input(
                    shader,
                    roughness_to_float,
                    ("roughness", "specularRoughness"),
                    ("roughness", "specularroughness"),
                )

        # ── Metalness / PxrMetallicWorkflow ───────────────────────────────────
        if metalness_path:
            metalness_tex = self._create_texture_node(parent, "metalness", metalness_path)
            metalness_to_float = self._create_pxr_to_float(parent, "metalness_to_float", metalness_tex)

            # Specular texture is consumed by the workflow node (optional).
            specular_tex_for_workflow = None
            specular_path_wf = self.textures.get("specular")
            if specular_path_wf:
                specular_tex_for_workflow = self._create_texture_node(
                    parent, "specular", specular_path_wf
                )

            used_workflow = self._create_pxr_metallic_workflow(
                parent, base_tex, metalness_to_float, specular_tex_for_workflow, shader
            )

            if not used_workflow:
                # Fallback: connect directly without the workflow node.
                if base_tex is not None:
                    self._connect_shader_input(
                        shader,
                        base_tex,
                        ("baseColor", "base_color", "diffuseColor"),
                        ("basecolor", "diffusecolor"),
                    )
                self._connect_shader_input(
                    shader,
                    metalness_to_float,
                    ("metalness", "metallic"),
                    ("metalness", "metallic"),
                )
        else:
            # No metalness — connect base_color directly (legacy path).
            if base_tex is not None:
                self._connect_shader_input(
                    shader,
                    base_tex,
                    ("baseColor", "base_color", "diffuseColor"),
                    ("basecolor", "diffusecolor"),
                )

        normal_path = self.textures.get("normal")
        if normal_path:
            normal_tex = self._create_texture_node(parent, "normal", normal_path)
            normal_source = normal_tex
            normal_map = self._try_create_node_with_fallback(
                parent,
                "normal_map",
                ("pxrnormalmap", "normalmap"),
            )
            if normal_map is not None:
                self._connect_to_pxr_normal_input(normal_map, normal_tex)
                normal_source = normal_map
            self._connect_shader_input(
                shader,
                normal_source,
                ("normal", "bumpNormal"),
                ("normal", "bumpnormal"),
            )

        displacement_path = self.textures.get("displacement")
        if displacement_path:
            displacement_tex = self._create_texture_node(parent, "displacement", displacement_path)
            displacement_to_float = self._create_pxr_to_float(
                parent,
                "displacement_to_float",
                displacement_tex,
            )
            displacement_source = displacement_to_float
            disp_transform = self._try_create_node_with_fallback(
                parent,
                "pxr_disp_transform",
                ("pxrdisptransform",),
            )
            if disp_transform is not None:
                try:
                    disp_transform.setNamedInput("dispScalar", displacement_to_float, "resultF")
                except Exception:
                    disp_transform.setInput(0, displacement_to_float, 0)
                self._set_first_available_parm(disp_transform, ("dispRemapMode",), 2)
                displacement_source = disp_transform
            displacement_node = self._try_create_node_with_fallback(
                parent,
                "displacement",
                ("pxrdisplace",),
            )
            if displacement_node is not None:
                try:
                    displacement_node.setNamedInput("dispScalar", displacement_source, "resultF")
                except Exception:
                    displacement_node.setInput(0, displacement_source, 0)
                self._set_first_available_parm(
                    displacement_node,
                    ("dispAmount", "displacementAmount", "scale", "amount"),
                    0.001,
                )
                displacement_source = displacement_node
            if displacement_output is not None:
                self._connect_output(displacement_output, displacement_source)
            else:
                if not self._connect_pxr_outputs(
                    parent,
                    displacement_source=displacement_source,
                ):
                    self._connect_shader_input(
                        shader,
                        displacement_source,
                        ("displacement",),
                        ("displacement", "disp"),
                    )

        opacity_path = self.textures.get("opacity")
        if opacity_path:
            opacity_tex = self._create_texture_node(parent, "opacity", opacity_path)
            opacity_to_float = self._create_pxr_to_float(parent, "opacity_to_float", opacity_tex)
            self._connect_shader_input(
                shader,
                opacity_to_float,
                ("presence", "opacity"),
                ("presence", "opacity"),
            )

        emission_path = self.textures.get("emission_color")
        if emission_path:
            emission_tex = self._create_texture_node(parent, "emission_color", emission_path)
            self._connect_shader_input(
                shader,
                emission_tex,
                ("emitColor", "emissionColor", "emission"),
                ("emitcolor", "emissioncolor", "emission"),
            )

        specular_path = self.textures.get("specular")
        # Specular is consumed by PxrMetallicWorkflow when metalness is present.
        # Only wire it standalone when there is no metalness texture.
        if specular_path and not metalness_path:
            specular_tex = self._create_texture_node(parent, "specular", specular_path)
            self._connect_shader_input(
                shader,
                specular_tex,
                ("specular", "specularFaceColor"),
                ("specularfacecolor", "specular"),
            )

        transmission_path = self.textures.get("transmission")
        if transmission_path:
            transmission_tex = self._create_texture_node(parent, "transmission", transmission_path)
            self._connect_shader_input(
                shader,
                transmission_tex,
                ("transmission", "refractionGain"),
                ("transmission", "refraction"),
            )

        translucency_path = self.textures.get("translucency")
        if translucency_path:
            translucency_tex = self._create_texture_node(parent, "translucency", translucency_path)
            self._connect_shader_input(
                shader,
                translucency_tex,
                ("transmissionColor", "subsurfaceColor"),
                ("transmissioncolor", "subsurfacecolor"),
            )

        subsurface_path = self.textures.get("subsurface")
        if subsurface_path:
            subsurface_tex = self._create_texture_node(parent, "subsurface", subsurface_path)
            self._connect_shader_input(
                shader,
                subsurface_tex,
                ("subsurfaceColor", "subsurface"),
                ("subsurfacecolor", "subsurface"),
            )

        # Wire the tile manifold to every PxrTexture node in the subnet.
        self._connect_manifold_to_all_textures(parent, manifold)


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
            self._converted_texture_paths = {}
            self._build_ui()
            self._apply_style()
            self._on_mode_toggled(self.auto_detect_check.isChecked())
            self._on_auto_name_toggled(self.auto_name_check.isChecked())
            self._on_builder_type_changed(0)  # sets create-button text for initial builder type
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

            builder_row = QtWidgets.QHBoxLayout()
            builder_row.setContentsMargins(0, 0, 0, 0)
            builder_row.setSpacing(8)
            builder_label = QtWidgets.QLabel("Builder Type")
            builder_label.setMinimumWidth(95)
            builder_row.addWidget(builder_label)
            self.builder_type_combo = QtWidgets.QComboBox()
            for builder_key, builder_label in MATERIAL_BUILDERS:
                self.builder_type_combo.addItem(builder_label, builder_key)
            self.builder_type_combo.currentIndexChanged.connect(self._on_builder_type_changed)
            builder_row.addWidget(self.builder_type_combo, 1)
            settings_layout.addLayout(builder_row, 1, 0, 1, 4)

            self.auto_name_check = QtWidgets.QCheckBox("Auto detect material name")
            self.auto_name_check.setChecked(True)
            self.auto_name_check.toggled.connect(self._on_auto_name_toggled)

            self.udim_check = QtWidgets.QCheckBox("Convert Texture to <UDIM>")
            self.udim_check.setChecked(False)

            self.aces_check = QtWidgets.QCheckBox("ACES Colorspace")
            self.aces_check.setChecked(False)
            self.aces_check.setToolTip(
                "Apply ACES colorspace to color maps (base color, emission).\n"
                "Data maps (roughness, normal, etc.) are always kept raw.\n"
                "When converting to TEX, bakes the colorspace transform into the file."
            )

            self.auto_detect_check = QtWidgets.QCheckBox("Auto detect textures from selected files")
            self.auto_detect_check.setChecked(True)
            self.auto_detect_check.toggled.connect(self._on_mode_toggled)

            self.auto_convert_check = QtWidgets.QCheckBox("Auto-convert textures")
            self.auto_convert_check.setChecked(True)
            self.auto_convert_check.setToolTip(
                "Automatically convert textures to the chosen format\n"
                "(derived from Builder Type) before creating the material."
            )

            toggles_row = QtWidgets.QHBoxLayout()
            toggles_row.setContentsMargins(0, 0, 0, 0)
            toggles_row.setSpacing(18)
            toggles_row.addStretch(1)
            toggles_row.addWidget(self.auto_name_check)
            toggles_row.addWidget(self.udim_check)
            toggles_row.addWidget(self.aces_check)
            toggles_row.addWidget(self.auto_detect_check)
            toggles_row.addWidget(self.auto_convert_check)
            toggles_row.addStretch(1)
            settings_layout.addLayout(toggles_row, 2, 0, 1, 4)

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

            self.create_btn = QtWidgets.QPushButton("Create Material in LOPs")
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

        def _selected_builder_type(self):
            return MaterialBuilder._normalize_builder_type(self.builder_type_combo.currentData())

        def _selected_builder_label(self):
            return MATERIAL_BUILDER_LABELS.get(self._selected_builder_type(), "MaterialX")

        def _on_builder_type_changed(self, _index):
            self._update_create_button_text()

        def _selected_tex_format(self):
            """Return '.tex' for PXR/RenderMan, '.tx' for MaterialX/Arnold."""
            return ".tex" if self._selected_builder_type() == "pxr" else ".tx"

        def _update_create_button_text(self):
            self.create_btn.setText("Create {0} in LOPs".format(self._selected_builder_label()))

        @staticmethod
        def _normalize_texture_path(path_value):
            return Path(str(path_value or "")).expanduser().as_posix()

        def _resolve_texture_path_for_build(self, texture_path):
            normalized = self._normalize_texture_path(texture_path)
            converted = self._converted_texture_paths.get(normalized)
            if converted:
                return converted

            if self._selected_builder_type() == MaterialBuilder.PXR:
                tex_candidate = Path(normalized).with_suffix(".tex")
                if tex_candidate.exists():
                    return tex_candidate.as_posix()

            return normalized

        def _collect_texture_paths_for_conversion(self):
            texture_paths = []

            if self.auto_detect_check.isChecked():
                for group in self._collect_auto_material_groups():
                    for texture_path in group["textures"].values():
                        texture_paths.append(texture_path)
            else:
                texture_paths.extend(self._collect_manual_textures().values())

            unique_paths = []
            seen = set()
            for texture_path in texture_paths:
                normalized = self._normalize_texture_path(texture_path)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                unique_paths.append(normalized)

            return unique_paths

        def _replace_manual_texture_path(self, source_path, converted_path):
            source_norm = self._normalize_texture_path(source_path)
            converted_norm = self._normalize_texture_path(converted_path)
            for line_edit in self.manual_fields.values():
                current = line_edit.text().strip()
                if not current:
                    continue
                if self._normalize_texture_path(current) == source_norm:
                    line_edit.setText(converted_norm)

        def _convert_textures_to_tex(self):
            self._set_busy(True)
            try:
                texture_paths = self._collect_texture_paths_for_conversion()
                if not texture_paths:
                    self._set_progress(0, 1, "No textures found")
                    self._set_info(
                        "No textures found to convert. Add textures first.",
                        "error",
                    )
                    return

                target_format = self._selected_tex_format()
                use_tx = target_format == ".tx"

                tool_name, _tool_path = TextureConverter.find_converter()
                if use_tx:
                    # .tx always needs maketx; check for it specifically
                    import shutil as _shutil
                    if not _shutil.which("maketx"):
                        self._set_progress(1, 1, "maketx not found")
                        self._set_info(
                            "maketx not found. Install OpenImageIO to convert to .tx.",
                            "error",
                        )
                        return
                    tool_name = "maketx"
                elif not tool_name:
                    self._set_progress(1, 1, "Converter not found")
                    self._set_info(
                        "txmake/maketx not found. Ensure RenderMan is installed and RMANTREE is set.",
                        "error",
                    )
                    return

                total = len(texture_paths)
                converted_count = 0
                skipped_count = 0
                failed = []

                self._set_progress(0, total, f"Converting 0/{total} with {tool_name}")

                for index, source_path in enumerate(texture_paths, start=1):
                    label = Path(source_path).name
                    self._set_progress(
                        index - 1,
                        total,
                        "Converting {0}/{1}: {2}".format(index - 1, total, label),
                    )
                    try:
                        channel_name = None
                        for grp in self._collect_auto_material_groups():
                            for ch, tp in grp["textures"].items():
                                if self._normalize_texture_path(tp) == self._normalize_texture_path(source_path):
                                    channel_name = ch
                                    break
                            if channel_name:
                                break
                        if channel_name is None:
                            for ch, tp in self._collect_manual_textures().items():
                                if self._normalize_texture_path(tp) == self._normalize_texture_path(source_path):
                                    channel_name = ch
                                    break
                        raw_ch = True
                        if channel_name:
                            ch_obj = MaterialChannelLibrary.channel(channel_name)
                            raw_ch = ch_obj.raw_colorspace if ch_obj else True
                        if use_tx:
                            if self.aces_check.isChecked():
                                converted_path, changed = TextureConverter.convert_to_tx_aces(
                                    source_path, raw_channel=raw_ch
                                )
                            else:
                                converted_path, changed = TextureConverter.convert_to_tx(source_path)
                        else:
                            if self.aces_check.isChecked():
                                converted_path, changed = TextureConverter.convert_to_tex_aces(
                                    source_path, raw_channel=raw_ch
                                )
                            else:
                                converted_path, changed = TextureConverter.convert_to_tex(source_path)
                        source_norm = self._normalize_texture_path(source_path)
                        converted_norm = self._normalize_texture_path(converted_path)
                        self._converted_texture_paths[source_norm] = converted_norm
                        self._replace_manual_texture_path(source_norm, converted_norm)
                        if changed:
                            converted_count += 1
                        else:
                            skipped_count += 1
                    except Exception as exc:
                        failed.append((source_path, str(exc)))

                    self._set_progress(
                        index,
                        total,
                        "Converting {0}/{1}".format(index, total),
                    )

                self._preview_textures()

                if failed and converted_count == 0 and skipped_count == 0:
                    first_failed_path, first_failed_error = failed[0]
                    self._set_info(
                        "Conversion failed ({0}): {1}".format(
                            Path(first_failed_path).name,
                            first_failed_error,
                        ),
                        "error",
                    )
                    return

                if failed:
                    first_failed_path, first_failed_error = failed[0]
                    self._set_info(
                        "Converted {0}, skipped {1}, failed {2}. First failure ({3}): {4}".format(
                            converted_count,
                            skipped_count,
                            len(failed),
                            Path(first_failed_path).name,
                            first_failed_error,
                        ),
                        "error",
                    )
                    return

                self._set_info(
                    "Texture conversion complete using {0}: converted {1}, up-to-date {2}.".format(
                        tool_name,
                        converted_count,
                        skipped_count,
                    ),
                    "success",
                )
            finally:
                self._set_busy(False)

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
            self.builder_type_combo.setCurrentIndex(0)
            self.auto_name_check.setChecked(True)
            self.udim_check.setChecked(True)
            self.auto_detect_check.setChecked(True)
            self.auto_paths_list.clear()
            self._converted_texture_paths = {}
            for field in self.manual_fields.values():
                field.clear()
            self._update_summary({})
            self._reset_progress()
            self._set_info("Reset complete.", "info")

        def _create_material(self):
            self._set_busy(True)
            try:
                # ── Auto-convert textures if the toggle is on ──────────────────
                if self.auto_convert_check.isChecked():
                    self._convert_textures_to_tex()
                    # _convert_textures_to_tex sets busy=False at the end;
                    # re-acquire busy state for the build step.
                    self._set_busy(True)

                builder_type = self._selected_builder_type()
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
                            builder_type=builder_type,
                            use_aces=self.aces_check.isChecked(),
                        )
                        for channel_name, texture_path in group["textures"].items():
                            builder.add_texture(
                                channel_name,
                                self._resolve_texture_path_for_build(texture_path),
                            )

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
                    builder_type=builder_type,
                    use_aces=self.aces_check.isChecked(),
                )
                for channel_name, texture_path in textures.items():
                    builder.add_texture(
                        channel_name,
                        self._resolve_texture_path_for_build(texture_path),
                    )

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
