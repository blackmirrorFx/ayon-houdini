import hou
import json
import logging
import os
import random
import re
import glob
import shutil
import subprocess
import tempfile
import sys
from datetime import datetime

from ayon_core.pipeline import get_current_context


_LOGGER = logging.getLogger("BMFX.Wedge")
if not _LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    _LOGGER.addHandler(_handler)
_LOGGER.setLevel(logging.INFO)

_DEADLINE_MACHINE_LIST_PARM = "machine_list"
_DEADLINE_MACHINE_DENYLIST_PARM = "machine_list_is_deny"

_ATTR_COUNT_PARMS = (
    "wedge_attr",
    "wedge_attributes",
    "wedgeattrs",
    "wedgeattribs",
    "wedge_attr_count",
    "num_attributes",
)
_WEDGE_COUNT_PARMS = (
    "nwedge",
    "wedge_count",
    "wedgecount",
    "num_wedges",
    "wedges",
)
_SEED_PARMS = ("seed", "random_seed")
_FRAME_RANGE_TUPLE_PARMS = ("frame_range", "framerange")
_FRAME_MODE_PARMS = ("trange", "frame_range_mode", "range_mode", "frame_mode")
_FRAME_START_PARMS = (
    "f1",
    "fx",
    "rangex",
    "start_frame",
    "fstart",
    "frange1",
    "frame_start",
)
_FRAME_END_PARMS = (
    "f2",
    "fy",
    "rangey",
    "end_frame",
    "fend",
    "frange2",
    "frame_end",
)
_FRAME_STEP_PARMS = (
    "f3",
    "rangez",
    "step_frame",
    "fstep",
    "frange3",
    "frame_step",
)

_TARGET_STEMS = ("target_parameter", "targetparm", "target_parm", "parameter")
_TYPE_STEMS = ("attribute_type", "attributetype", "attr_type", "type")
_WEDGE_TYPE_STEMS = ("wedge_type", "wedgetype", "distribution", "mode")
_START_STEMS = ("start", "min", "from", "rangex", "minvalue", "range_min")
_END_STEMS = ("end", "max", "rangey", "maxvalue", "range_max")
_RANGE2_STEMS = (
    "floatrange",
    "start_end",
    "startend",
    "range2",
    "range",
    "valuerange",
)
_CACHE_ROOT_PARMS = ("cache_path", "cachepath")
_VERSION_MODE_PARMS = ("version_mode", "versionmode")
_VERSION_PARMS = ("version", "write_version")
_WEDGE_COUNT_INTERNAL_PARMS = ("wedge_attr", "wedge_attributes", "nwedge")
_READ_LATEST_PARMS = ("read_latest", "readlatest")
_READ_WEDGE_INDEX_PARMS = (
    "read_wedge_index",
    "wedge_index",
    "single_wedge",
    "wedgeid",
    "preview_wedge",
)
_READ_ALL_WEDGES_PARMS = (
    "load_all_wedge",
    "load_all_wedges",
    "read_all_wedges",
    "all_wedges",
)
_SUBMIT_REVIEW_JOB_PARMS = (
    "doflipbook",
    "submit_review_job",
    "submit_flipbook_job",
    "submit_wedge_flipbook",
    "submit_review_merge",
)
_REVIEW_CAMERA_PARMS = (
    "flipbook_camera",
    "review_camera",
    "camera_path",
    "camera",
    "cam_path",
    "cam",
)
_REVIEW_RENDERER_PARMS = (
    "flipbook_renderer",
    "review_renderer",
    "renderer_mode",
)


def _submitted_versions_path(node):
    return os.path.join(node_cache_root(node), ".wedge_submitted_versions.json")


def _read_submitted_versions(node):
    versions = set(existing_versions(node))
    path = _submitted_versions_path(node)
    if os.path.exists(path):
        try:
            with open(path, "r") as stream:
                data = json.load(stream)
            versions.update(int(v) for v in (data or []) if int(v) > 0)
        except Exception:
            pass
    return sorted(v for v in versions if v > 0)


def _mark_version_submitted(node, version):
    version = max(1, int(version))
    versions = _read_submitted_versions(node)
    if version not in versions:
        versions.append(version)
    versions = sorted(set(versions))

    os.makedirs(node_cache_root(node), exist_ok=True)
    with open(_submitted_versions_path(node), "w") as stream:
        json.dump(versions, stream, indent=4)


def _normalized_machine_list(raw_value):
    if raw_value is None:
        return ""
    tokens = [
        part.strip()
        for part in re.split(r"[,\s;]+", str(raw_value))
        if part.strip()
    ]
    return ",".join(dict.fromkeys(tokens))


def _first_parm(node, names):
    for name in names:
        parm = node.parm(name)
        if parm:
            return parm
    return None


def _indexed_parm(node, idx, stems):
    direct_names = []
    for stem in stems:
        direct_names.extend(
            (
                f"{stem}{idx}",
                f"{stem}_{idx}",
                f"{stem}{idx:02d}",
                f"{stem}_{idx:02d}",
            )
        )

    for name in direct_names:
        parm = node.parm(name)
        if parm:
            return parm

    suffix = str(idx)
    for parm in node.parms():
        pname = parm.name().lower()
        if not pname.endswith(suffix):
            continue
        if any(stem in pname for stem in stems):
            return parm
    return None


def _indexed_parm_tuple(node, idx, stems):
    direct_names = []
    for stem in stems:
        direct_names.extend(
            (
                f"{stem}{idx}",
                f"{stem}_{idx}",
                f"{stem}{idx:02d}",
                f"{stem}_{idx:02d}",
            )
        )

    for name in direct_names:
        parm_tuple = node.parmTuple(name)
        if parm_tuple and len(parm_tuple) >= 2:
            return parm_tuple

    suffix = str(idx)
    for parm_tuple in node.parmTuples():
        pname = parm_tuple.name().lower()
        if not pname.endswith(suffix):
            continue
        if any(stem in pname for stem in stems) and len(parm_tuple) >= 2:
            return parm_tuple
    return None


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_path_string(path):
    """
    Keep cache paths stable across mixed slash / drive formats.
    Example fixes:
    - r://library/... -> r:/library/...
    - r//library/...  -> r:/library/...
    """
    path = str(path or "").strip()
    if not path:
        return ""

    path = path.replace("\\", "/")

    drive_with_colon = re.match(r"^([A-Za-z]):/{2,}(.*)$", path)
    if drive_with_colon:
        drive = drive_with_colon.group(1)
        tail = drive_with_colon.group(2)
        path = f"{drive}:/{tail.lstrip('/')}"
    else:
        drive_without_colon = re.match(r"^([A-Za-z])/{2,}(.*)$", path)
        if drive_without_colon:
            drive = drive_without_colon.group(1)
            tail = drive_without_colon.group(2)
            path = f"{drive}:/{tail.lstrip('/')}"

    if path.startswith("//"):
        path = "//" + re.sub(r"/{2,}", "/", path[2:])
    else:
        path = re.sub(r"/{2,}", "/", path)

    return path


def _expand_path_at_frame(path, frame=None):
    """
    Expand env vars and force $F token replacement to a concrete frame path.
    """
    path = str(path or "").strip()
    if not path:
        return ""

    try:
        frame_value = int(round(float(frame if frame is not None else hou.frame())))
    except Exception:
        frame_value = int(hou.frame())

    try:
        expanded = hou.expandStringAtFrame(path, float(frame_value))
    except Exception:
        expanded = hou.expandString(path)

    token_match = re.search(r"\$F(\d*)", expanded)
    if token_match:
        pad = int(token_match.group(1)) if token_match.group(1) else 1
        expanded = expanded.replace(
            token_match.group(0),
            str(frame_value).zfill(pad),
        )

    return _normalize_path_string(expanded)


def _parse_version_value(raw_value):
    token = str(raw_value or "").strip().lower().lstrip("v")
    parsed = _safe_int(token, 0)
    return parsed if parsed > 0 else None


def _selected_version_from_parm(version_parm):
    if not version_parm:
        return None
    try:
        return _parse_version_value(version_parm.evalAsString())
    except Exception:
        return None


def _resolve_mode_token(value, mode_parm=None):
    token = str(value or "").strip()
    lowered = token.lower()
    if lowered in {"auto", "manual"}:
        return lowered
    if "manual" in lowered:
        return "manual"
    if "auto" in lowered:
        return "auto"

    # Numeric or custom tokens: derive meaning from menu labels when available.
    if mode_parm:
        try:
            template = mode_parm.parmTemplate()
            if isinstance(template, hou.MenuParmTemplate):
                menu_items = [str(item) for item in template.menuItems()]
                menu_labels = [str(label).strip().lower() for label in template.menuLabels()]
                if token in menu_items:
                    idx = menu_items.index(token)
                    if idx < len(menu_labels):
                        label = menu_labels[idx]
                        if "manual" in label:
                            return "manual"
                        if "auto" in label:
                            return "auto"
        except Exception:
            pass

    # Fallback for common numeric menu token setups.
    if lowered == "0":
        return "manual"
    if lowered == "1":
        return "auto"
    return "auto"


def _is_manual_mode(value, mode_parm=None):
    return _resolve_mode_token(value, mode_parm=mode_parm) == "manual"


def _is_auto_mode(value, mode_parm=None):
    return _resolve_mode_token(value, mode_parm=mode_parm) == "auto"


def _set_version_parm_value(version_parm, version, prefer_prefixed=True):
    """
    Write version values in a format that works for string/int/menu parms.
    """
    if not version_parm:
        return False

    version = max(1, _safe_int(version, 1))
    prefixed = f"v{int(version):03d}"
    numeric = str(int(version))
    candidates = [prefixed, numeric] if prefer_prefixed else [numeric, prefixed]

    # Int parms prefer numeric strings first.
    try:
        if isinstance(version_parm.parmTemplate(), hou.IntParmTemplate):
            candidates = [numeric, prefixed]
    except Exception:
        pass

    for candidate in dict.fromkeys(candidates):
        try:
            version_parm.set(candidate)
            read_back = version_parm.evalAsString().strip().lower().lstrip("v")
            if _safe_int(read_back, 0) == version:
                return True
        except Exception:
            continue

    try:
        version_parm.set(int(version))
        return True
    except Exception:
        return False


def _normalize_attr_type(raw):
    token = str(raw or "").strip().lower()
    if token in {"1", "int", "integer", "interger"} or "int" in token:
        return "int"
    if token in {"2", "bool", "boolean", "toggle"}:
        return "bool"
    return "float"


def _normalize_wedge_type(raw):
    token = str(raw or "").strip().lower().replace(" ", "_")
    if token in {"0", "random"} or "rand" in token:
        return "random"
    return "range"


def _cast_value(value, attr_type):
    if attr_type == "int":
        return int(round(value))
    if attr_type == "bool":
        return 1 if float(value) >= 0.5 else 0
    return float(value)


def get_ayon_launcher_env():
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("AYON_", "OPENPYPE_", "AVALON_"))
    }


def get_all_houdini_vars():
    raw = hou.hscript("set")[0]
    data = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = hou.expandString(value.strip())
    return data


def filter_custom_houdini_vars(vars_dict):
    prefixes = ("AYON_", "OPENPYPE_", "AVALON_", "TH_", "RES", "PIX_")
    exact = {"JOB", "SEQ", "SHOT", "APP", "LIB"}
    return {
        key: value
        for key, value in vars_dict.items()
        if key in exact or key.startswith(prefixes)
    }


def get_pipeline_env():
    keys = ("PYTHONPATH", "HOUDINI_PATH", "HOUDINI_OTLSCAN_PATH")
    data = {}
    for key in keys:
        value = os.environ.get(key)
        if value:
            data[key] = value
    return data


def project_root():
    root = os.environ.get("JOB")
    if not root or root == "$JOB":
        root = hou.text.expandString("$JOB")
    if not root or root == "$JOB":
        root = hou.expandString("$HIP")
    if not root:
        root = os.path.dirname(hou.hipFile.path())
    return _normalize_path_string(root)


def _default_node_cache_root(node):
    try:
        context = get_current_context() or {}
    except Exception:
        context = {}

    folder = (context.get("folder_path") or "").strip("/")
    task = context.get("task_name") or "task"
    base = project_root()

    if folder:
        return os.path.join(base, folder, "houdini", task, "cache", node.name())
    return os.path.join(base, "cache", node.name())


def node_cache_root(node):
    cache_root_parm = _first_parm(node, _CACHE_ROOT_PARMS)
    if cache_root_parm:
        raw = cache_root_parm.evalAsString().strip()
        if raw:
            return _normalize_path_string(hou.expandString(raw))
    return _normalize_path_string(_default_node_cache_root(node))


def ensure_cache_and_version_parms(node):
    """
    Add versioning controls as spare parms when missing:
    - version_mode (auto/manual)
    - version (manual value)
    Cache root override is optional and only used if a `cache_path` parm
    already exists on the HDA.
    """
    try:
        group = node.parmTemplateGroup()
    except Exception:
        return

    changed = False

    if not group.find("version_mode"):
        version_mode = hou.MenuParmTemplate(
            "version_mode",
            "Version Mode",
            menu_items=("auto", "manual"),
            menu_labels=("Auto", "Manual"),
            default_value=0,
        )
        group.append(version_mode)
        changed = True

    if not group.find("version"):
        version = hou.StringParmTemplate(
            "version",
            "Version",
            1,
            default_value=("v001",),
        )
        group.append(version)
        changed = True

    if changed:
        try:
            node.setParmTemplateGroup(group)
        except Exception:
            return

    cache_root = _first_parm(node, _CACHE_ROOT_PARMS)
    if cache_root and not cache_root.evalAsString().strip():
        cache_root.set(_default_node_cache_root(node))


def existing_versions(node):
    base = node_cache_root(node)
    if not os.path.exists(base):
        return []
    versions = []
    for entry in os.listdir(base):
        match = re.match(r"v(\d+)$", entry)
        if match:
            versions.append(int(match.group(1)))
    return sorted(versions)


def active_version(node):
    frozen = os.environ.get("BMFX_FROZEN_VERSION")
    if frozen:
        parsed = _safe_int(frozen, 0)
        if parsed > 0:
            return parsed

    mode_parm = _first_parm(node, _VERSION_MODE_PARMS)
    version_parm = _first_parm(node, _VERSION_PARMS)
    if mode_parm and version_parm and _is_manual_mode(
        mode_parm.evalAsString(),
        mode_parm=mode_parm,
    ):
        raw = version_parm.evalAsString().strip().lower().lstrip("v")
        return max(1, _safe_int(raw, 1))

    versions = _read_submitted_versions(node)
    return versions[-1] + 1 if versions else 1


def version_dir(node, version=None, create=False):
    version = active_version(node) if version is None else int(version)
    path = os.path.join(node_cache_root(node), f"v{version:03d}")
    if create:
        os.makedirs(path, exist_ok=True)
    return _normalize_path_string(path)


def _latest_known_version(node):
    versions = _read_submitted_versions(node)
    return versions[-1] if versions else 1


def _display_version(node):
    mode_parm = _first_parm(node, _VERSION_MODE_PARMS)
    version_parm = _first_parm(node, _VERSION_PARMS)
    selected_version = _selected_version_from_parm(version_parm)
    if mode_parm and version_parm and _is_manual_mode(
        mode_parm.evalAsString(),
        mode_parm=mode_parm,
    ):
        return max(1, selected_version or 1)

    # In auto mode, allow selecting old versions for cache viewing.
    known_versions = _read_submitted_versions(node)
    if selected_version and (not known_versions or selected_version in known_versions):
        return selected_version
    return _latest_known_version(node)


def cache_path_pattern(node, version=None):
    version = _display_version(node) if version is None else int(version)
    return _normalize_path_string(os.path.join(
        version_dir(node, version),
        "w$WEDGE_ID",
        "main.$F4.bgeo.sc",
    ))


def get_path_for_ui(node):
    try:
        version = read_version(node) or _display_version(node)
        if _read_all_wedges_enabled(node):
            return _ensure_bgeo_sc(
                cache_path_pattern(node, version=version).replace("$WEDGE_ID", "*")
            )
        wedge_index = read_wedge_index(node, prefer_env=False)
        return _cache_file_path(node, version, wedge_index, create=False)
    except Exception:
        return ""


def resolve_output_path(node_or_kwargs, frame=None):
    """
    FileCache-like helper for internal ROP output parms.
    Returns frame-expanded path (e.g. main.1001.bgeo.sc) for the
    currently evaluated frame.
    """
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ""
    try:
        version = active_version(node)
        wedge_index = read_wedge_index(node, prefer_env=True)
        path = _cache_file_path(node, version, wedge_index, create=False)
        return _expand_path_at_frame(path, frame=frame)
    except Exception:
        return ""


def resolve_f1(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return int(hou.frame())
    try:
        return int(_frame_range(node)[0])
    except Exception:
        return int(hou.frame())


def resolve_f2(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return int(hou.frame())
    try:
        return int(_frame_range(node)[1])
    except Exception:
        return int(hou.frame())


def resolve_f3(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return 1
    try:
        return max(1, int(_frame_range(node)[2]))
    except Exception:
        return 1


def read_version(node):
    mode_parm = _first_parm(node, _VERSION_MODE_PARMS)
    mode_value = mode_parm.evalAsString() if mode_parm else "auto"
    read_latest_parm = _first_parm(node, _READ_LATEST_PARMS)
    versions = _read_submitted_versions(node)

    if read_latest_parm and bool(read_latest_parm.eval()):
        return versions[-1] if versions else None

    version_parm = _first_parm(node, _VERSION_PARMS)
    selected_version = _selected_version_from_parm(version_parm)
    if selected_version:
        if _is_auto_mode(mode_value, mode_parm=mode_parm) and versions:
            if selected_version in versions:
                return selected_version
            return versions[-1]
        return selected_version

    return versions[-1] if versions else None


def _read_all_wedges_enabled(node):
    parm = _first_parm(node, _READ_ALL_WEDGES_PARMS)
    if not parm:
        return False
    try:
        return bool(parm.eval())
    except Exception:
        return False


def _all_wedge_paths_for_frame(node, version, frame=None, only_existing=False):
    paths = []
    for wedge_index in _version_wedge_indices(node, version):
        path = _cache_file_path(node, version, wedge_index, create=False)
        resolved = _expand_path_at_frame(path, frame=frame)
        if only_existing and not os.path.exists(resolved):
            continue
        paths.append(resolved)
    return paths


def _format_read_files_value(paths):
    # File SOP "Read Files" accepts a whitespace-separated list.
    return " ".join(path for path in paths if path)


def _cache_frame_file_name(frame=None):
    return os.path.basename(_expand_path_at_frame("main.$F4.bgeo.sc", frame=frame))


def eval_read_cache_root(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ""
    version = read_version(node)
    if not version:
        return ""
    root = _normalize_path_string(version_dir(node, version, create=False))
    if not root.endswith("/"):
        root = f"{root}/"
    return root


def eval_read_cache_object_mask(node_or_kwargs, frame=None):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ""
    file_name = _cache_frame_file_name(frame=frame)
    return file_name


def read_wedge_index(node, prefer_env=False):
    wedge_parm = _first_parm(node, _READ_WEDGE_INDEX_PARMS)
    parm_index = _safe_int(wedge_parm.eval(), 0) if wedge_parm else 0
    if parm_index > 0 and not prefer_env:
        return parm_index

    env_index = _safe_int(os.environ.get("BMFX_WEDGE_INDEX"), 0)
    if env_index > 0:
        return env_index

    if parm_index > 0:
        return parm_index

    if wedge_parm:
        return max(1, _safe_int(wedge_parm.eval(), 1))
    return 1


def _is_wedge_node(node):
    if not isinstance(node, hou.Node):
        return False
    if node.parm("submittodeadline"):
        return True
    if _first_parm(node, _WEDGE_COUNT_INTERNAL_PARMS):
        return True
    return False


def _resolve_wedge_node(node_or_kwargs):
    node = _kwargs_node(node_or_kwargs)
    if not node:
        return None
    cursor = node
    while cursor:
        if _is_wedge_node(cursor):
            return cursor
        cursor = cursor.parent()
    return node


def expected_frame_range(node):
    try:
        f1, f2, step = _frame_range(node)
        return int(f1), int(f2), max(1, int(step))
    except Exception:
        return None


def missing_frames_in_range(node, version, wedge_index=None):
    if wedge_index is None:
        wedge_index = read_wedge_index(node)
    path = _cache_file_path(node, version, wedge_index, create=False)

    # Single-file caches (no frame token)
    if "$F" not in path:
        expanded = hou.expandString(path)
        return [] if os.path.exists(expanded) else ["FILE_MISSING"]

    fr = expected_frame_range(node)
    if not fr:
        return []

    start, end, step = fr
    token_match = re.search(r"\$F(\d*)", path)
    if not token_match:
        expanded = hou.expandString(path)
        return [] if os.path.exists(expanded) else ["FILE_MISSING"]
    pad = int(token_match.group(1)) if token_match.group(1) else 1

    missing = []
    for frame in range(start, end + 1, max(1, step)):
        frame_str = str(int(frame)).zfill(pad)
        frame_path = path.replace(token_match.group(0), frame_str)
        full_path = hou.expandString(frame_path)
        if not os.path.exists(full_path):
            missing.append(int(frame))
    return missing


def _version_wedge_indices(node, version):
    ver_dir = version_dir(node, version=version, create=False)
    indices = []
    if os.path.isdir(ver_dir):
        for entry in os.listdir(ver_dir):
            entry_path = os.path.join(ver_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            match = re.match(r"w(\d+)$", entry)
            if match:
                indices.append(int(match.group(1)))

    if indices:
        return sorted(set(indices))

    # Fallback when wedge folders are not present yet.
    count_parm = _first_parm(node, _WEDGE_COUNT_PARMS)
    fallback_count = max(1, _safe_int(count_parm.eval(), 1)) if count_parm else 1
    return list(range(1, fallback_count + 1))


def missing_frames_for_version(node, version):
    missing_by_wedge = {}
    for wedge_index in _version_wedge_indices(node, version):
        missing = missing_frames_in_range(node, version, wedge_index=wedge_index)
        if missing:
            missing_by_wedge[int(wedge_index)] = missing
    return missing_by_wedge


def set_status(node, state):
    colors = {
        "LIVE": (0.3, 0.3, 0.3),
        "CACHED": (0.1, 0.6, 0.1),
        "OLDER CACHE": (0.9, 0.6, 0.1),
        "MISSING CACHE": (0.8, 0.3, 0.1),
        "ERROR": (0.8, 0.1, 0.1),
    }

    node.setColor(hou.Color(colors.get(state, colors["LIVE"])))
    if state == "LIVE":
        node.setComment(">> LIVE")
    elif state == "MISSING CACHE":
        node.setComment("! MISSING CACHE")
    elif state == "OLDER CACHE":
        node.setComment("! USING OLDER CACHE")
    else:
        node.setComment(f"Status: {state}")
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)


def update_cache_status(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return

    node.setGenericFlag(hou.nodeFlag.DisplayComment, False)
    versions = _read_submitted_versions(node)

    mode_parm = node.parm("mode")
    if mode_parm and mode_parm.evalAsString() == "live":
        set_status(node, "LIVE")
        return

    if not versions:
        set_status(node, "LIVE")
        return

    latest = versions[-1]
    selected = read_version(node)
    if not selected:
        set_status(node, "LIVE")
        return

    missing_by_wedge = missing_frames_for_version(node, selected)

    if missing_by_wedge:
        set_status(node, "MISSING CACHE")
        parts = []
        for wedge_index in sorted(missing_by_wedge)[:3]:
            missing = missing_by_wedge[wedge_index]
            if missing == ["FILE_MISSING"]:
                preview = "FILE_MISSING"
            else:
                preview = ", ".join(map(str, missing[:2]))
                if len(missing) > 2:
                    preview = f"{preview}..."
            parts.append(f"w{int(wedge_index)}: {preview}")
        suffix = " | ..." if len(missing_by_wedge) > 3 else ""
        node.setComment(f"! MISSING FRAMES {', '.join(parts)}{suffix}")
    elif selected < latest:
        set_status(node, "OLDER CACHE")
    else:
        set_status(node, "CACHED")

    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)


def eval_read_cache_path(node_or_kwargs, frame=None):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ""

    try:
        version = read_version(node)
        if not version:
            return ""
        if _read_all_wedges_enabled(node):
            return eval_read_cache_root(node)

        wedge_index = read_wedge_index(node, prefer_env=False)
        path = _cache_file_path(node, version, wedge_index, create=False)
        return _expand_path_at_frame(path, frame=frame)
    except Exception:
        return ""


def eval_read_cache_path_expanded(node_or_kwargs, frame=None):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ""
    version = read_version(node)
    if not version:
        return ""
    if _read_all_wedges_enabled(node):
        return eval_read_cache_root(node)

    wedge_index = read_wedge_index(node, prefer_env=False)
    path = _cache_file_path(node, version, wedge_index, create=False)
    if not path:
        return ""
    return _resolve_existing_cache_path(path, frame=frame)


def _resolve_existing_cache_path(path, frame=None):
    if not path:
        return ""

    token_match = re.search(r"\$F(\d*)", path)

    if frame is None:
        frame = int(hou.frame())
    expanded = _expand_path_at_frame(path, frame=frame)

    if expanded and os.path.exists(expanded):
        return expanded
    if "$F" not in path and os.path.exists(path):
        return _normalize_path_string(path)

    if token_match:
        pad = int(token_match.group(1)) if token_match.group(1) else 1
        frame_value = int(frame if frame is not None else hou.frame())
        frame_str = str(frame_value).zfill(pad)
        concrete_path = path.replace(token_match.group(0), frame_str)
        concrete_expanded = _normalize_path_string(hou.expandString(concrete_path))
        if os.path.exists(concrete_expanded):
            return concrete_expanded

        pattern = _normalize_path_string(hou.expandString(path.replace(token_match.group(0), "*")))
        matches = sorted(glob.glob(pattern))
        if matches:
            # Ignore invalid literal token files like "main.$F4.bgeo.sc".
            numeric_matches = [
                match
                for match in matches
                if "$F" not in os.path.basename(match)
            ]
            if numeric_matches:
                return _normalize_path_string(numeric_matches[0])
        # Even when cache is missing, show concrete frame path instead of raw $F token.
        return concrete_expanded

    return expanded or _normalize_path_string(path)


def eval_version_string(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return "v001"
    try:
        version = max(1, int(_display_version(node)))
    except Exception:
        version = 1
    return f"v{version:03d}"


def eval_version_int(node_or_kwargs):
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return 1
    try:
        return max(1, int(_display_version(node)))
    except Exception:
        return 1


def version_menu_items(node_or_kwargs):
    """
    Dynamic menu items for the `version` parameter.
    Returns [token1, label1, token2, label2, ...].
    """
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ["v001", "v001"]

    items = []
    versions = _read_submitted_versions(node)

    for version in reversed(versions):
        token = f"v{int(version):03d}"
        if token in items:
            continue
        items.extend([token, token])

    if not items:
        token = f"v{max(1, active_version(node)):03d}"
        items.extend([token, token])

    return items


def wedge_index_menu_items(node_or_kwargs):
    """
    Dynamic menu items for single/read wedge selector.
    Returns [token1, label1, token2, label2, ...].
    """
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return ["1", "1"]

    version = read_version(node) or _display_version(node) or active_version(node)
    indices = _version_wedge_indices(node, version)
    if not indices:
        count_parm = _first_parm(node, _WEDGE_COUNT_PARMS)
        fallback_count = max(1, _safe_int(count_parm.eval(), 1)) if count_parm else 1
        indices = list(range(1, fallback_count + 1))

    items = []
    for wedge_index in sorted(set(int(i) for i in indices if int(i) > 0)):
        token = str(int(wedge_index))
        label = token
        items.extend([token, label])

    if not items:
        items = ["1", "1"]
    return items


def ayon_context_json_path(node, version):
    return os.path.join(version_dir(node, version, create=True), ".ayon_vars.json")


def save_ayon_context_for_node(node, version):
    data = {
        "launcher_env": get_ayon_launcher_env(),
        "houdini_vars": filter_custom_houdini_vars(get_all_houdini_vars()),
        "pipeline_env": get_pipeline_env(),
    }
    out_path = ayon_context_json_path(node, version)
    with open(out_path, "w") as stream:
        json.dump(data, stream, indent=4)
    return out_path


def _attr_count(node):
    count_parm = _first_parm(node, _ATTR_COUNT_PARMS)
    if count_parm:
        return max(0, _safe_int(count_parm.eval(), 0))

    found = 0
    for parm in node.parms():
        name = parm.name().lower()
        if not any(stem in name for stem in _TARGET_STEMS):
            continue
        match = re.search(r"(\d+)$", name)
        if match:
            found = max(found, int(match.group(1)))
    return found


def _wedge_count(node, attributes):
    parm = _first_parm(node, _WEDGE_COUNT_PARMS)
    if parm:
        return max(1, _safe_int(parm.eval(), 1))

    # Reasonable fallback when count parm does not exist.
    if any(attr["wedge_type"] == "random" for attr in attributes):
        return 10
    return 1


def _seed_value(node):
    parm = _first_parm(node, _SEED_PARMS)
    if parm:
        return _safe_int(parm.eval(), 1234)
    return 1234


def _frame_range(node):
    mode_parm = _first_parm(node, _FRAME_MODE_PARMS)
    mode_token = ""
    if mode_parm:
        try:
            mode_token = str(mode_parm.evalAsString()).strip().lower()
        except Exception:
            mode_token = ""

    if not mode_token:
        frame_range_parm = _first_parm(node, ("frame_range", "framerange"))
        if frame_range_parm:
            try:
                frame_range_template = frame_range_parm.parmTemplate()
                if isinstance(frame_range_template, hou.MenuParmTemplate):
                    mode_token = str(frame_range_parm.evalAsString()).strip().lower()
            except Exception:
                mode_token = ""

    tuple_vals = None
    for name in (*_FRAME_RANGE_TUPLE_PARMS, "range"):
        parm_tuple = node.parmTuple(name)
        if parm_tuple and len(parm_tuple) >= 3:
            tuple_vals = parm_tuple.eval()
            break

    p_f1 = _first_parm(node, _FRAME_START_PARMS)
    p_f2 = _first_parm(node, _FRAME_END_PARMS)
    p_st = _first_parm(node, _FRAME_STEP_PARMS)

    if not mode_token or not p_f1 or not p_f2:
        try:
            rop = _get_active_rop(node)
        except Exception:
            rop = None
        if rop:
            if not mode_token:
                rop_mode = rop.parm("trange") or rop.parm("frame_range")
                if rop_mode:
                    try:
                        mode_token = str(rop_mode.evalAsString()).strip().lower()
                    except Exception:
                        mode_token = ""
            if not p_f1:
                p_f1 = rop.parm("f1") or rop.parm("fx") or rop.parm("frange1")
            if not p_f2:
                p_f2 = rop.parm("f2") or rop.parm("fy") or rop.parm("frange2")
            if not p_st:
                p_st = rop.parm("f3") or rop.parm("fstep") or rop.parm("frange3")

    if mode_token in {"off", "none", "single", "current", "0"}:
        frame = int(hou.frame())
        return frame, frame, 1

    if tuple_vals:
        f1, f2, step = int(tuple_vals[0]), int(tuple_vals[1]), int(tuple_vals[2])
    else:
        f1 = _safe_int(p_f1.eval(), int(hou.frame())) if p_f1 else int(hou.frame())
        f2 = _safe_int(p_f2.eval(), f1) if p_f2 else f1
        step = _safe_int(p_st.eval(), 1) if p_st else 1

    if f2 < f1:
        f1, f2 = f2, f1
    step = max(1, step)
    return f1, f2, step


def _frames_for_deadline(f1, f2, step):
    if step <= 1:
        return f"{f1}-{f2}"
    return f"{f1}-{f2}x{step}"


def _resolve_target_parm(owner_node, target_path):
    target = (target_path or "").strip()
    if not target:
        return None

    if target.startswith("/"):
        return hou.parm(target)

    # Relative/explicit node path + parm name.
    if "/" in target:
        node_path, parm_name = target.rsplit("/", 1)
        node = owner_node.node(node_path) or hou.node(node_path)
        if node:
            return node.parm(parm_name)

    # Local parm name.
    return owner_node.parm(target)


def _collect_wedge_attributes(node):
    count = _attr_count(node)
    if count < 1:
        raise RuntimeError("No wedge attributes found. Add at least one attribute row.")

    attributes = []
    for index in range(1, count + 1):
        target_parm = _indexed_parm(node, index, _TARGET_STEMS)
        type_parm = _indexed_parm(node, index, _TYPE_STEMS)
        wedge_type_parm = _indexed_parm(node, index, _WEDGE_TYPE_STEMS)
        start_parm = _indexed_parm(node, index, _START_STEMS)
        end_parm = _indexed_parm(node, index, _END_STEMS)
        range2_parm = _indexed_parm(node, index, _RANGE2_STEMS)
        range2_tuple = _indexed_parm_tuple(node, index, _RANGE2_STEMS)

        target_path = target_parm.evalAsString().strip() if target_parm else ""
        if not target_path:
            continue

        resolved_target = _resolve_target_parm(node, target_path)
        if not resolved_target:
            raise RuntimeError(
                f"Invalid target parameter in wedge row {index}: '{target_path}'"
            )

        attr_type = _normalize_attr_type(type_parm.evalAsString() if type_parm else "")
        wedge_type = _normalize_wedge_type(
            wedge_type_parm.evalAsString() if wedge_type_parm else ""
        )
        start_val = _safe_float(start_parm.eval() if start_parm else 0.0, 0.0)
        end_val = _safe_float(end_parm.eval() if end_parm else start_val, start_val)
        if range2_tuple:
            vals = range2_tuple.eval()
            start_val = _safe_float(vals[0], start_val)
            end_val = _safe_float(vals[1], end_val)
        elif range2_parm and range2_parm.tuple() and len(range2_parm.tuple()) >= 2:
            vals = range2_parm.tuple().eval()
            start_val = _safe_float(vals[0], start_val)
            end_val = _safe_float(vals[1], end_val)

        attributes.append(
            {
                "row_index": index,
                "target_path": target_path,
                "target_parm": resolved_target,
                "attr_type": attr_type,
                "wedge_type": wedge_type,
                "start": start_val,
                "end": end_val,
            }
        )

    if not attributes:
        raise RuntimeError("No valid wedge attribute rows found.")
    return attributes


def _build_wedge_values(attributes, wedge_count, seed):
    all_values = []
    for wedge_index in range(1, wedge_count + 1):
        wedge_values = []
        for attr_index, attr in enumerate(attributes):
            start = attr["start"]
            end = attr["end"]

            mode = attr["wedge_type"]
            if mode == "random":
                attr_seed = seed + (wedge_index * 1009) + (attr_index * 37)
                rng = random.Random(attr_seed)
                low = min(start, end)
                high = max(start, end)
                raw = rng.uniform(low, high)
            else:
                if wedge_count <= 1:
                    raw = start
                else:
                    t = float(wedge_index - 1) / float(wedge_count - 1)
                    raw = start + ((end - start) * t)

            value = _cast_value(raw, attr["attr_type"])
            wedge_values.append(
                {
                    "target_path": attr["target_path"],
                    "target_parm": attr["target_parm"],
                    "value": value,
                    "row_index": attr["row_index"],
                }
            )
        all_values.append(wedge_values)
    return all_values


def _get_active_rop(node):
    candidates = (
        "ropnet/geometry",
        "ropnet/cache",
        "ropnet/rop_geometry1",
        "rop_geometry1",
        "geometry",
        "cache",
    )
    for rel_path in candidates:
        rop = node.node(rel_path)
        if rop and rop.type().category().name() == "Driver":
            return rop

    ropnet = node.node("ropnet")
    if ropnet:
        for child in ropnet.children():
            if child.type().category().name() == "Driver":
                return child

    for child in node.allSubChildren():
        if child.type().category().name() == "Driver":
            return child

    raise RuntimeError(f"No internal ROP driver found inside {node.path()}.")


def _get_output_parm(node, rop):
    # Prefer promoted/top-level parms first to avoid writing directly to
    # internal parms on locked HDAs.
    for name in ("output_path", "sopoutput", "output", "filename", "file", "lopoutput"):
        parm = node.parm(name)
        if parm:
            return parm

    for name in ("sopoutput", "output", "filename", "file", "lopoutput"):
        parm = rop.parm(name)
        if parm:
            return parm

    return None


def _set_parm_safe(parm, value, context="parameter"):
    if not parm:
        return False
    try:
        parm.set(value)
        return True
    except hou.PermissionError:
        _LOGGER.warning(
            "Permission denied while setting %s: %s",
            context,
            parm.path(),
        )
        return False
    except Exception:
        _LOGGER.warning(
            "Failed setting %s: %s",
            context,
            parm.path(),
        )
        return False


def _should_set_output_path_on_submit(node):
    """
    Output path is expression-driven in this wedge setup, so default to
    not overriding it during submission.
    Opt-in options:
    - node parm: `set_output_path_on_submit` (toggle/int)
    - env var : BMFX_WEDGE_SET_OUTPUT_PARM=1
    """
    toggle = node.parm("set_output_path_on_submit")
    if toggle:
        try:
            return bool(toggle.eval())
        except Exception:
            pass

    token = str(os.environ.get("BMFX_WEDGE_SET_OUTPUT_PARM", "")).strip().lower()
    return token in {"1", "true", "yes", "on"}


def _wedge_dir(node, version, wedge_index, create=False):
    path = os.path.join(version_dir(node, version, create=create), f"w{int(wedge_index)}")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _ensure_bgeo_sc(path):
    base = os.path.basename(path)
    if "$F" in base:
        match = re.search(r"(\$F\d*)(?:\.[^./\\]+(?:\.[^./\\]+)?)?$", base)
        if match:
            token = match.group(1)
            base = re.sub(r"(\$F\d*)(?:\.[^./\\]+(?:\.[^./\\]+)?)?$", f"{token}.bgeo.sc", base)
            return os.path.join(os.path.dirname(path), base)
    if not path.endswith(".bgeo.sc"):
        path = re.sub(r"(?:\.[^./\\]+(?:\.[^./\\]+)?)?$", ".bgeo.sc", path)
    return path


def _cache_file_path(node, version, wedge_index, create=False):
    path = cache_path_pattern(node, version=version).replace(
        "$WEDGE_ID",
        str(int(wedge_index)),
    )
    if create:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return _normalize_path_string(_ensure_bgeo_sc(path))


def _cache_file(node, version, wedge_index):
    return _cache_file_path(node, version, wedge_index, create=True)


def _values_json_path(node, version, wedge_index):
    return os.path.join(_wedge_dir(node, version, wedge_index, create=True), "wedge_values.json")


def _save_hip_to_wedge_dir(node, version, wedge_index):
    wedge_root = _wedge_dir(node, version, wedge_index, create=True)
    hip_dir = os.path.join(wedge_root, "hip")
    os.makedirs(hip_dir, exist_ok=True)
    dst = os.path.join(hip_dir, "source.hip")
    escaped = dst.replace("\\", "/").replace('"', '\\"')
    try:
        hou.hscript(f'mwrite -n "{escaped}"')
    except hou.OperationFailed as exc:
        raise RuntimeError(f"Failed to save wedge HIP snapshot: {dst}") from exc
    return dst


def _write_values_json(node, version, wedge_index, wedge_values):
    serialized = {}
    for entry in wedge_values:
        serialized[entry["target_path"]] = entry["value"]

    out_path = _values_json_path(node, version, wedge_index)
    with open(out_path, "w") as stream:
        json.dump(serialized, stream, indent=4)
    return out_path


def _build_job_name(node, wedge_index, version):
    hip_name = os.path.splitext(hou.hipFile.basename())[0] or "untitled"
    return f"{hip_name} | {node.name()} | v{int(version):03d} | w{int(wedge_index)}"


def _build_batch_name(node):
    hip_name = os.path.splitext(hou.hipFile.basename())[0] or "untitled"
    return f"{hip_name} | {node.name()}"


def _get_node_priority(node):
    parm = node.parm("priority")
    if parm:
        return max(1, _safe_int(parm.eval(), 50))
    return 50


def _get_chunk_size(node, f1, f2, step):
    chunk_parm = node.parm("chunks")
    single_machine = (
        bool(node.parm("single_machine").eval())
        if node.parm("single_machine")
        else False
    )
    user_chunks = _safe_int(chunk_parm.eval(), 10) if chunk_parm else 10
    user_chunks = max(1, user_chunks)

    if single_machine:
        # Match FileCache behavior ("all frames in one task") while honoring step.
        return max(1, int(((f2 - f1) / float(step)) + 1))
    return user_chunks


def _set_rop_frame_range_for_submission(rop, f1, f2, step):
    """
    Force frame-range parms on the ROP snapshot used for Deadline so
    $F tokens are expanded to concrete frame numbers during render.
    Returns a map of parm_path -> original_value for restoration.
    """
    saved = {}

    def _save_and_set(parm, *values):
        if not parm:
            return
        try:
            saved[parm.path()] = parm.evalAsString()
        except Exception:
            return

        for value in values:
            try:
                parm.set(value)
                return
            except Exception:
                continue

    trange_parm = rop.parm("trange") or rop.parm("frame_range")
    _save_and_set(trange_parm, "normal", "on", 1)

    p_f1 = rop.parm("f1") or rop.parm("fx") or rop.parm("frange1")
    p_f2 = rop.parm("f2") or rop.parm("fy") or rop.parm("frange2")
    p_f3 = rop.parm("f3") or rop.parm("fstep") or rop.parm("frange3")
    _save_and_set(p_f1, int(f1))
    _save_and_set(p_f2, int(f2))
    _save_and_set(p_f3, max(1, int(step)))

    return saved


def _restore_parm_values(saved):
    if not saved:
        return
    for parm_path, raw_value in saved.items():
        parm = hou.parm(parm_path)
        if not parm:
            continue
        try:
            parm.set(raw_value)
        except Exception:
            continue


def _deadline_command():
    command = shutil.which("deadlinecommand")
    if command:
        return command
    return "deadlinecommand"


def _submit_job_to_deadline(job_info, plugin_info):
    deadline_cmd = _deadline_command()
    job_file = tempfile.NamedTemporaryFile(delete=False, suffix="_job.info")
    plugin_file = tempfile.NamedTemporaryFile(delete=False, suffix="_plugin.info")

    try:
        with open(job_file.name, "w") as stream:
            for key, value in job_info.items():
                stream.write(f"{key}={value}\n")

        with open(plugin_file.name, "w") as stream:
            for key, value in plugin_info.items():
                stream.write(f"{key}={value}\n")

        result = subprocess.check_output(
            [deadline_cmd, job_file.name, plugin_file.name],
            stderr=subprocess.STDOUT,
        ).decode()
        _LOGGER.debug("Deadline output:\n%s", result.strip())

        match = re.search(r"JobID=([\w\d]+)", result)
        if match:
            return match.group(1)
        raise RuntimeError(f"Deadline submission succeeded but JobID not found.\n{result.strip()}")
    except subprocess.CalledProcessError as exc:
        msg = (exc.output or b"").decode().strip()
        raise RuntimeError(f"Deadline submission failed:\n{msg}") from exc
    finally:
        for path in (job_file.name, plugin_file.name):
            if os.path.exists(path):
                os.remove(path)


def _collect_deadline_env():
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


def _should_submit_review_job(node):
    toggle = _first_parm(node, _SUBMIT_REVIEW_JOB_PARMS)
    if toggle:
        try:
            return bool(toggle.eval())
        except Exception:
            pass

    token = str(os.environ.get("BMFX_WEDGE_SUBMIT_REVIEW_JOB", "1")).strip().lower()
    return token not in {"0", "false", "off", "no"}


def _is_obj_camera(node):
    if not node:
        return False
    try:
        return (
            node.type().category() == hou.objNodeTypeCategory()
            and node.type().name() == "cam"
        )
    except Exception:
        return False


def _resolve_review_camera(node):
    camera_parm = _first_parm(node, _REVIEW_CAMERA_PARMS)
    camera_path = camera_parm.evalAsString().strip() if camera_parm else ""

    if camera_path:
        candidate = None
        if camera_path.startswith("/"):
            candidate = hou.node(camera_path)
        else:
            candidate = node.node(camera_path) or hou.node(camera_path)
            if not candidate and not camera_path.startswith(".."):
                candidate = hou.node(f"/obj/{camera_path}")
        if _is_obj_camera(candidate):
            return candidate

    obj_root = hou.node("/obj")
    if obj_root:
        for child in obj_root.allSubChildren():
            if _is_obj_camera(child):
                return child
    return None


def _review_camera_payload(node):
    camera = _resolve_review_camera(node)
    if not camera:
        return {}

    data = {"path": camera.path()}
    try:
        data["world_matrix"] = [float(v) for v in camera.worldTransform().asTuple()]
    except Exception:
        pass

    def _read_float(*names):
        for name in names:
            parm = camera.parm(name)
            if not parm:
                continue
            try:
                return float(parm.eval())
            except Exception:
                continue
        return None

    def _read_int(*names):
        value = _read_float(*names)
        if value is None:
            return None
        return int(round(value))

    focal = _read_float("focal")
    aperture = _read_float("aperture", "apert")
    near = _read_float("near", "nearclip")
    far = _read_float("far", "farclip")
    resx = _read_int("resx")
    resy = _read_int("resy")

    if focal is not None:
        data["focal"] = focal
    if aperture is not None:
        data["aperture"] = aperture
    if near is not None:
        data["near"] = near
    if far is not None:
        data["far"] = far
    if resx is not None:
        data["resx"] = resx
    if resy is not None:
        data["resy"] = resy

    return data


def _review_ocio_colorspace(node):
    if not os.getenv("OCIO"):
        return ""

    colorspace_parm = _first_parm(
        node,
        (
            "ociocolorspace",
            "review_color_space",
            "review_colorspace",
            "colorspace",
        ),
    )
    if colorspace_parm:
        try:
            value = str(colorspace_parm.evalAsString()).strip()
            if value:
                return value
        except Exception:
            pass

    try:
        from ayon_houdini.api.colorspace import get_default_display_view_colorspace

        value = str(get_default_display_view_colorspace() or "").strip()
        if value:
            return value
    except Exception:
        pass

    return ""


def _review_renderer_mode(node):
    """
    Wedge review is flipbook-node only.
    """
    return "flipbook"


def _hython_executable():
    candidates = []
    hfs = str(os.environ.get("HFS", "")).strip()
    if hfs:
        candidates.append(os.path.join(hfs, "bin", "hython"))
    which_hython = shutil.which("hython")
    if which_hython:
        candidates.append(which_hython)
    if sys.executable:
        candidates.append(sys.executable)

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.exists(candidate):
            return candidate
    return "hython"


def _xvfb_executable():
    """
    Resolve xvfb-run to an absolute path when available.
    Returns None when xvfb is not found.
    """
    override = str(os.environ.get("BMFX_XVFB_RUN", "")).strip()
    if override:
        if os.path.isabs(override) and os.path.exists(override):
            return override
        found = shutil.which(override)
        if found:
            return found

    found = shutil.which("xvfb-run")
    if found:
        return found

    for candidate in ("/usr/bin/xvfb-run", "/usr/local/bin/xvfb-run", "/bin/xvfb-run"):
        if os.path.exists(candidate):
            return candidate
    return None


def _build_wedge_flipbook_images_deadline_script(script_path):
    script = r'''import json
import os
import re
import sys
import traceback

hou = None


def _norm(path):
    return os.path.normpath(path).replace("\\", "/")


def _set_first_parm(node, names, *values):
    if not node:
        return False
    for name in names:
        parm = node.parm(name)
        if not parm:
            continue
        for value in values:
            try:
                parm.set(value)
                return True
            except Exception:
                continue
    return False


def _set_flipbook_color_settings(flipbook_rop, desired_ocio_colorspace=""):
    if not flipbook_rop:
        return

    _set_first_parm(flipbook_rop, ("colorcorrect",), "ocio")

    available_spaces = []
    try:
        available_spaces = [str(value) for value in hou.Color.ocio_spaces()]
    except Exception:
        available_spaces = []

    candidates = []
    if desired_ocio_colorspace:
        candidates.append(str(desired_ocio_colorspace).strip())

    for env_key in ("BMFX_WEDGE_OCIO_COLORSPACE", "AYON_REVIEW_COLOR_SPACE"):
        env_value = str(os.environ.get(env_key, "")).strip()
        if env_value:
            candidates.append(env_value)

    candidates.extend(("aces", "scene_linear", "ACES - ACEScg", "ACEScg"))

    for candidate in candidates:
        color_space = str(candidate or "").strip()
        if not color_space:
            continue
        if available_spaces and color_space not in available_spaces:
            continue
        if _set_first_parm(flipbook_rop, ("ociocolorspace",), color_space):
            return

    if available_spaces:
        _set_first_parm(flipbook_rop, ("ociocolorspace",), available_spaces[0])


def _expand_frame_token(path_pattern, frame):
    token_match = re.search(r"\$F(\d*)", str(path_pattern or ""))
    if not token_match:
        return _norm(path_pattern)
    token = token_match.group(0)
    pad = int(token_match.group(1) or 1)
    frame_token = str(int(frame)).zfill(pad)
    return _norm(str(path_pattern).replace(token, frame_token))


def _build_scene(camera_payload, ociocolorspace=""):
    try:
        hou.hipFile.clear(suppress_save_prompt=True)
    except TypeError:
        hou.hipFile.clear()

    obj = hou.node("/obj")
    out = hou.node("/out")
    if not obj or not out:
        raise RuntimeError("Missing /obj or /out context for flipbook generation.")

    geo = obj.createNode("geo", "wedge_flipbook_geo")
    for child in list(geo.children()):
        child.destroy()
    file_sop = geo.createNode("file", "IN_CACHE")
    file_sop.setDisplayFlag(True)
    file_sop.setRenderFlag(True)
    geo.layoutChildren()

    cam = obj.createNode("cam", "wedge_flipbook_cam")
    world_matrix = camera_payload.get("world_matrix") or []
    if isinstance(world_matrix, (list, tuple)) and len(world_matrix) == 16:
        try:
            cam.setWorldTransform(hou.Matrix4(tuple(float(v) for v in world_matrix)))
        except Exception:
            pass

    _set_first_parm(cam, ("focal",), camera_payload.get("focal"))
    _set_first_parm(cam, ("aperture", "apert"), camera_payload.get("aperture"))
    _set_first_parm(cam, ("near", "nearclip"), camera_payload.get("near"))
    _set_first_parm(cam, ("far", "farclip"), camera_payload.get("far"))
    _set_first_parm(cam, ("resx",), camera_payload.get("resx"))
    _set_first_parm(cam, ("resy",), camera_payload.get("resy"))

    flipbook_rop = None
    try:
        flipbook_rop = out.createNode("flipbook", "wedge_flipbook_node")
        _set_first_parm(flipbook_rop, ("source",), "objects", "Objects", 1)
        _set_first_parm(flipbook_rop, ("camera", "cam"), cam.path())
        _set_first_parm(
            flipbook_rop,
            ("scenepath", "scenepath", "scene_path"),
            "/obj",
        )
        _set_first_parm(flipbook_rop, ("soppath", "sop_path"), geo.path())
        _set_first_parm(
            flipbook_rop,
            ("candidateobjects", "candidate_objects", "candidate"),
            geo.path(),
        )
        _set_first_parm(
            flipbook_rop,
            ("forceobjects", "force_objects"),
            geo.path(),
        )
        _set_flipbook_color_settings(
            flipbook_rop,
            desired_ocio_colorspace=ociocolorspace,
        )
    except Exception:
        flipbook_rop = None

    obj.layoutChildren()
    out.layoutChildren()
    if not flipbook_rop:
        raise RuntimeError("Could not create /out/flipbook node for wedge review.")
    return file_sop, flipbook_rop


def _set_scene_frame_range(f1, f2):
    start = float(f1)
    end = float(f2)
    try:
        hou.playbar.setFrameRange(start, end)
    except Exception:
        pass
    try:
        hou.playbar.setPlaybackRange(start, end)
    except Exception:
        pass
    try:
        hou.setFrame(start)
    except Exception:
        pass


def _set_flipbook_frame_range(flipbook_rop, f1, f2, step):
    _set_first_parm(flipbook_rop, ("trange", "frame_range", "range"), "normal", "on", 1)
    _set_first_parm(flipbook_rop, ("f1", "frange1", "rangex", "frame_start"), int(f1))
    _set_first_parm(flipbook_rop, ("f2", "frange2", "rangey", "frame_end"), int(f2))
    _set_first_parm(flipbook_rop, ("f3", "frange3", "rangez", "frame_step"), max(1, int(step)))


def _save_debug_hip(debug_scene_dir, label):
    if not debug_scene_dir:
        return
    try:
        os.makedirs(debug_scene_dir, exist_ok=True)
        out_path = os.path.join(debug_scene_dir, "flipbook_{}.hip".format(str(label)))
        hou.hipFile.save(out_path)
        print("Saved wedge flipbook debug hip: {}".format(_norm(out_path)))
    except Exception as exc:
        print("Warning: failed to save debug hip '{}': {}".format(label, exc))


def _render_single_frame(rop, frame, frame_output):
    _set_first_parm(
        rop,
        ("picture", "vm_picture", "output", "outputfile", "filename", "output_path"),
        frame_output,
    )
    hou.setFrame(float(frame))
    try:
        rop.render(frame_range=(float(frame), float(frame)), verbose=True, output_progress=False)
    except TypeError:
        try:
            rop.render(frame_range=(float(frame), float(frame)), verbose=True)
        except TypeError:
            rop.render(verbose=True)


def _render_wedge(
    file_sop,
    flipbook_rop,
    cache_pattern,
    output_pattern,
    f1,
    f2,
    step,
):
    _set_first_parm(file_sop, ("file", "filename"), cache_pattern)
    reload_parm = file_sop.parm("reload")
    if reload_parm:
        try:
            reload_parm.pressButton()
        except Exception:
            pass

    output_dir = os.path.dirname(_expand_frame_token(output_pattern, f1))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if not flipbook_rop:
        raise RuntimeError("Flipbook node is unavailable.")

    _set_flipbook_frame_range(flipbook_rop, f1, f2, step)
    first_output = _expand_frame_token(output_pattern, int(f1))
    _set_first_parm(
        flipbook_rop,
        ("picture", "vm_picture", "output", "outputfile", "filename", "output_path"),
        first_output,
    )

    for frame in range(int(f1), int(f2) + 1, max(1, int(step))):
        frame_output = _expand_frame_token(output_pattern, int(frame))
        try:
            _render_single_frame(flipbook_rop, frame, frame_output)
        except Exception as exc:
            raise RuntimeError(
                "Flipbook node render failed at frame {}: {}".format(int(frame), exc)
            )


def main():
    global hou
    payload_path = sys.argv[1]
    with open(payload_path, "r") as stream:
        payload = json.load(stream)

    renderer_mode = "flipbook"

    for key, value in (payload.get("env") or {}).items():
        if value is not None:
            os.environ[str(key)] = str(value)

    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("HOUDINI_OGL_SOFTWARE", "0")
    for key in (
        "QT_OPENGL",
        "LIBGL_ALWAYS_SOFTWARE",
        "MESA_GL_VERSION_OVERRIDE",
        "MESA_GLSL_VERSION_OVERRIDE",
    ):
        os.environ.pop(key, None)

    os.environ.setdefault("HOUDINI_NO_SPLASH", "1")
    os.environ.setdefault("HOUDINI_DSO_ERROR", "2")

    import hou as _hou
    hou = _hou

    wedge_count = int(payload.get("wedge_count") or 1)
    f1 = int(payload.get("f1") or 1)
    f2 = int(payload.get("f2") or f1)
    step = max(1, int(payload.get("step") or 1))
    cache_patterns = payload.get("cache_patterns") or {}
    image_patterns = payload.get("image_patterns") or {}
    camera_payload = payload.get("camera") or {}
    ociocolorspace = str(payload.get("ociocolorspace") or "").strip()
    debug_scene_dir = payload.get("debug_scene_dir") or ""
    print("Wedge flipbook renderer mode: {}".format(renderer_mode))
    print("Wedge flipbook camera: {}".format(camera_payload.get("path") or "<generated>"))
    print("Wedge flipbook OCIO colorspace: {}".format(ociocolorspace or "<auto>"))

    file_sop, flipbook_rop = _build_scene(camera_payload, ociocolorspace=ociocolorspace)
    _set_scene_frame_range(f1, f2)
    _set_flipbook_frame_range(flipbook_rop, f1, f2, step)
    _save_debug_hip(debug_scene_dir, "scene_setup")

    rendered = 0
    for wedge_index in range(1, wedge_count + 1):
        key = str(int(wedge_index))
        cache_pattern = cache_patterns.get(key)
        image_pattern = image_patterns.get(key)
        if not cache_pattern or not image_pattern:
            print("Skip w{}: missing cache/image pattern payload.".format(wedge_index))
            continue

        first_cache = _expand_frame_token(cache_pattern, f1)
        if first_cache and not os.path.exists(first_cache):
            print("Skip w{}: cache not found at {}".format(wedge_index, first_cache))
            continue

        print(
            "Render wedge flipbook w{} | cache={} | out={}".format(
                wedge_index, cache_pattern, image_pattern
            )
        )
        _set_first_parm(file_sop, ("file", "filename"), cache_pattern)
        _save_debug_hip(debug_scene_dir, "w{:03d}".format(int(wedge_index)))
        _render_wedge(
            file_sop=file_sop,
            flipbook_rop=flipbook_rop,
            cache_pattern=cache_pattern,
            output_pattern=image_pattern,
            f1=f1,
            f2=f2,
            step=step,
        )
        rendered += 1

    if rendered < 1:
        raise RuntimeError("No wedge flipbook images rendered.")

    print("Rendered wedge flipbook image sequences for {} wedge(s).".format(rendered))


if __name__ == "__main__":
    _exit_code = 0
    try:
        main()
    except Exception:
        traceback.print_exc()
        _exit_code = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_exit_code)
'''
    with open(script_path, "w") as stream:
        stream.write(script)


def _submit_wedge_flipbook_images_job(
    node,
    version,
    wedge_count,
    f1,
    f2,
    step,
    batch_name,
    pool_name,
    priority,
    dependency_job_ids,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=False,
):
    if not dependency_job_ids:
        return None

    version_root = version_dir(node, version=version, create=True)
    script_dir = os.path.join(version_root, ".deadline_review")
    os.makedirs(script_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload_path = os.path.join(script_dir, f"flipbook_payload_{stamp}.json")
    script_path = os.path.join(script_dir, f"run_flipbook_{stamp}.py")
    debug_scene_dir = os.path.join(script_dir, f"flipbook_debug_hip_{stamp}")

    cache_patterns = {}
    image_patterns = {}
    for wedge_index in range(1, int(wedge_count) + 1):
        cache_patterns[str(int(wedge_index))] = _cache_file_path(
            node,
            version=version,
            wedge_index=wedge_index,
            create=False,
        )
        image_pattern = os.path.join(
            _wedge_dir(node, version, wedge_index, create=True),
            "review",
            "main.$F4.jpg",
        )
        image_patterns[str(int(wedge_index))] = _normalize_path_string(image_pattern)

    # Use a minimal, stable Houdini env for farm-side image generation.
    # This avoids loading third-party DSOs/DS scripts (for example broken HTOA).
    empty_packages_dir = os.path.join(script_dir, "empty_packages")
    os.makedirs(empty_packages_dir, exist_ok=True)

    env = {
        "HOUDINI_PATH": "&",
        "HOUDINI_OTLSCAN_PATH": "&",
        "HOUDINI_NO_SPLASH": "1",
        "HOUDINI_DSO_ERROR": "2",
        "HOUDINI_NO_ENV_FILE": "1",
        "HOUDINI_PACKAGE_DIR": _normalize_path_string(empty_packages_dir),
    }
    for key in ("PATH", "HFS", "LD_LIBRARY_PATH"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    camera_payload = _review_camera_payload(node)
    ociocolorspace = _review_ocio_colorspace(node)
    if not camera_payload:
        _LOGGER.warning(
            "No valid review camera found on %s. Using default generated camera.",
            node.path(),
        )
    renderer_mode = "flipbook"
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    env.setdefault("HOUDINI_OGL_SOFTWARE", "0")
    _LOGGER.info(
        "Wedge flipbook renderer mode on %s: %s",
        node.path(),
        renderer_mode,
    )
    _LOGGER.info(
        "Wedge flipbook debug hip output on %s: %s",
        node.path(),
        _normalize_path_string(debug_scene_dir),
    )

    payload = {
        "wedge_count": int(wedge_count),
        "f1": int(f1),
        "f2": int(f2),
        "step": max(1, int(step)),
        "renderer_mode": renderer_mode,
        "cache_patterns": {
            key: _normalize_path_string(value)
            for key, value in cache_patterns.items()
        },
        "image_patterns": image_patterns,
        "camera": camera_payload,
        "ociocolorspace": str(ociocolorspace or ""),
        "debug_scene_dir": _normalize_path_string(debug_scene_dir),
        "env": env,
    }
    with open(payload_path, "w") as stream:
        json.dump(payload, stream, indent=4)
    _build_wedge_flipbook_images_deadline_script(script_path)

    hip_name = os.path.splitext(hou.hipFile.basename())[0] or "untitled"
    job_info = {
        "Plugin": "CommandLine",
        "BatchName": batch_name,
        "Name": f"{hip_name} | {node.name()} | v{int(version):03d} | wedge_flipbook_images",
        "Frames": "0-0",
        "ChunkSize": 1,
        "Pool": pool_name or "houdini",
        "Priority": int(priority),
        "Comment": "Auto-submit wedge flipbook image generation job",
        "JobDependencies": ",".join(str(job_id) for job_id in dependency_job_ids if job_id),
    }

    if machine_limit:
        job_info["MachineLimit"] = int(machine_limit)
    if machine_list:
        key = "Blacklist" if machine_list_is_deny else "Whitelist"
        job_info[key] = machine_list

    env_index = 0
    for key, value in sorted(env.items()):
        if value is None:
            continue
        value_text = str(value)
        if "\n" in value_text:
            continue
        job_info[f"EnvironmentKeyValue{env_index}"] = f"{key}={value_text}"
        env_index += 1

    hython_exec = _hython_executable()
    plugin_info = {
        "Executable": hython_exec,
        "Arguments": f"\"{script_path}\" \"{payload_path}\"",
        "ExecuteInShell": "False",
    }

    return _submit_job_to_deadline(job_info, plugin_info)


def _build_wedge_review_deadline_script(script_path):
    script = r'''import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".exr", ".bmp", ".tif", ".tiff", ".tga")


def _norm(path):
    return os.path.normpath(path).replace("\\", "/")


def _escape_drawtext(value):
    text = str(value or "")
    text = text.replace("\\", "\\\\")
    text = text.replace(":", r"\:")
    text = text.replace("'", r"\'")
    text = text.replace(",", r"\,")
    text = text.replace("%", r"\%")
    text = text.replace("[", r"\[")
    text = text.replace("]", r"\]")
    return text


def _format_value(value):
    if isinstance(value, float):
        return "{:.3f}".format(value)
    return str(value)


def _burnin_text(wedge_index, values_json_path):
    values = {}
    if values_json_path and os.path.exists(values_json_path):
        try:
            with open(values_json_path, "r") as stream:
                values = json.load(stream) or {}
        except Exception:
            values = {}

    entries = []
    for key in sorted(values.keys()):
        short_key = str(key).split("/")[-1]
        entries.append("{}={}".format(short_key, _format_value(values[key])))

    details = ", ".join(entries[:6])
    if len(entries) > 6:
        details += ", ..."

    if details:
        text = "w{} | {}".format(int(wedge_index), details)
    else:
        text = "w{}".format(int(wedge_index))

    if len(text) > 220:
        text = text[:217] + "..."
    return text


def _detect_sequence(wedge_dir):
    if not os.path.isdir(wedge_dir):
        return None

    grouped = {}
    for name in sorted(os.listdir(wedge_dir)):
        lower = name.lower()
        if not any(lower.endswith(ext) for ext in _IMAGE_EXTS):
            continue
        match = re.match(r"^(.*?)(\d+)(\.[^.]+)$", name)
        if not match:
            continue
        prefix, frame_str, suffix = match.groups()
        key = (prefix, suffix, len(frame_str))
        grouped.setdefault(key, []).append(int(frame_str))

    if not grouped:
        return None

    def _score(item):
        (prefix, _suffix, _padding), frames = item
        return (
            len(frames),
            1 if prefix.lower().startswith("main.") else 0,
            1 if prefix.lower().startswith("main_") else 0,
            -len(prefix),
        )

    (prefix, suffix, _padding), frames = max(grouped.items(), key=_score)
    frames = sorted(set(frames))
    if not frames:
        return None

    glob_pattern = os.path.join(wedge_dir, "{}*{}".format(prefix, suffix))
    return {
        "glob_pattern": _norm(glob_pattern),
        "start_frame": int(frames[0]),
        "end_frame": int(frames[-1]),
    }


def _run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "Unknown command error").strip()
        raise RuntimeError(
            "Command failed: {}\n{}".format(" ".join(cmd), error)
        )


def _encode_wedge_video(ffmpeg_bin, sequence, output_path, fps, burnin_text):
    filter_graph = (
        "drawbox=x=0:y=0:w=iw:h=64:color=black@0.45:t=fill,"
        "drawtext=text='{}':x=20:y=20:fontsize=26:fontcolor=white"
    ).format(_escape_drawtext(burnin_text))

    cmd = [
        ffmpeg_bin,
        "-y",
        "-framerate",
        str(float(fps)),
        "-pattern_type",
        "glob",
        "-i",
        sequence["glob_pattern"],
        "-vf",
        filter_graph,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        output_path,
    ]
    _run(cmd)


def _merge_grid(ffmpeg_bin, inputs, output_path, tile_width, tile_height):
    if len(inputs) == 1:
        shutil.copy2(inputs[0], output_path)
        return

    cols = int(math.ceil(math.sqrt(len(inputs))))
    rows = int(math.ceil(float(len(inputs)) / float(cols)))

    parts = []
    layout = []
    for index, _path in enumerate(inputs):
        parts.append(
            "[{0}:v]scale={1}:{2}:force_original_aspect_ratio=decrease,"
            "pad={1}:{2}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{0}]".format(
                index, int(tile_width), int(tile_height)
            )
        )
        x = int(index % cols) * int(tile_width)
        y = int(index // cols) * int(tile_height)
        layout.append("{}_{}".format(x, y))

    filter_graph = (
        ";".join(parts)
        + ";"
        + "".join("[v{}]".format(i) for i in range(len(inputs)))
        + "xstack=inputs={}:layout={}[vout]".format(
            len(inputs),
            "|".join(layout),
        )
    )

    cmd = [ffmpeg_bin, "-y"]
    for path in inputs:
        cmd.extend(["-i", path])
    cmd.extend(
        [
            "-filter_complex",
            filter_graph,
            "-map",
            "[vout]",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            output_path,
        ]
    )
    _run(cmd)


def main():
    payload_path = sys.argv[1]
    with open(payload_path, "r") as stream:
        payload = json.load(stream)

    for key, value in (payload.get("env") or {}).items():
        if value is not None:
            os.environ[str(key)] = str(value)

    ffmpeg_bin = payload.get("ffmpeg_bin") or shutil.which("ffmpeg") or "ffmpeg"
    version_dir = payload["version_dir"]
    review_dir = payload["review_dir"]
    output_path = payload["output_path"]
    wedge_count = int(payload.get("wedge_count") or 1)
    fps = float(payload.get("fps") or 24.0)
    tile_width = int(payload.get("tile_width") or 960)
    tile_height = int(payload.get("tile_height") or 540)
    wedge_values_files = payload.get("wedge_values_files") or {}

    os.makedirs(review_dir, exist_ok=True)

    wedge_videos = []
    for wedge_index in range(1, wedge_count + 1):
        wedge_dir = os.path.join(version_dir, "w{}".format(int(wedge_index)))
        wedge_review_dir = os.path.join(wedge_dir, "review")
        sequence = _detect_sequence(wedge_review_dir)
        if not sequence:
            sequence = _detect_sequence(wedge_dir)
        if not sequence:
            print(
                "Skip w{}: no numbered image sequence found in {}".format(
                    wedge_index, wedge_review_dir
                )
            )
            continue

        burnin_text = _burnin_text(
            wedge_index=wedge_index,
            values_json_path=wedge_values_files.get(str(wedge_index)),
        )
        wedge_video = os.path.join(
            review_dir,
            "w{:03d}.burnin.mp4".format(int(wedge_index)),
        )
        _encode_wedge_video(
            ffmpeg_bin=ffmpeg_bin,
            sequence=sequence,
            output_path=wedge_video,
            fps=fps,
            burnin_text=burnin_text,
        )
        wedge_videos.append(_norm(wedge_video))
        print("Built wedge burnin video: {}".format(wedge_video))

    if not wedge_videos:
        raise RuntimeError(
            "No wedge image sequences found. "
            "Expected numbered images in {}/w*/review/".format(version_dir)
        )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _merge_grid(
        ffmpeg_bin=ffmpeg_bin,
        inputs=wedge_videos,
        output_path=output_path,
        tile_width=tile_width,
        tile_height=tile_height,
    )
    print("Built merged wedge review mp4: {}".format(output_path))


if __name__ == "__main__":
    main()
'''
    with open(script_path, "w") as stream:
        stream.write(script)


def _submit_wedge_review_job(
    node,
    version,
    wedge_count,
    batch_name,
    pool_name,
    priority,
    dependency_job_ids,
    wedge_values_files,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=False,
):
    if not dependency_job_ids:
        return None

    version_root = version_dir(node, version=version, create=True)
    review_root = os.path.join(version_root, "review")
    script_dir = os.path.join(version_root, ".deadline_review")
    os.makedirs(script_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload_path = os.path.join(script_dir, f"review_payload_{stamp}.json")
    script_path = os.path.join(script_dir, f"run_review_{stamp}.py")
    output_path = os.path.join(
        review_root,
        f"{node.name()}.v{int(version):03d}.wedges.mp4",
    )

    try:
        fps_value = float(hou.fps())
    except Exception:
        fps_value = 24.0

    env = _collect_deadline_env()
    payload = {
        "version_dir": _normalize_path_string(version_root),
        "review_dir": _normalize_path_string(review_root),
        "output_path": _normalize_path_string(output_path),
        "wedge_count": int(wedge_count),
        "fps": float(fps_value),
        "tile_width": 960,
        "tile_height": 540,
        "wedge_values_files": {
            str(int(index)): _normalize_path_string(path)
            for index, path in (wedge_values_files or {}).items()
            if path
        },
        "ffmpeg_bin": "ffmpeg",
        "env": env,
    }
    with open(payload_path, "w") as stream:
        json.dump(payload, stream, indent=4)
    _build_wedge_review_deadline_script(script_path)

    hip_name = os.path.splitext(hou.hipFile.basename())[0] or "untitled"
    job_info = {
        "Plugin": "CommandLine",
        "BatchName": batch_name,
        "Name": f"{hip_name} | {node.name()} | v{int(version):03d} | wedge_flipbook_merge",
        "Frames": "0-0",
        "ChunkSize": 1,
        "Pool": pool_name or "houdini",
        "Priority": int(priority),
        "Comment": "Auto-submit wedge flipbook + merge job",
        "JobDependencies": ",".join(str(job_id) for job_id in dependency_job_ids if job_id),
    }

    if machine_limit:
        job_info["MachineLimit"] = int(machine_limit)
    if machine_list:
        key = "Blacklist" if machine_list_is_deny else "Whitelist"
        job_info[key] = machine_list

    env_index = 0
    for key, value in sorted(env.items()):
        if value is None:
            continue
        value_text = str(value)
        if "\n" in value_text:
            continue
        job_info[f"EnvironmentKeyValue{env_index}"] = f"{key}={value_text}"
        env_index += 1

    plugin_info = {
        "Executable": sys.executable,
        "Arguments": f"\"{script_path}\" \"{payload_path}\"",
        "ExecuteInShell": "False",
    }

    return _submit_job_to_deadline(job_info, plugin_info)


def _collect_machine_options(node, machine_limit, machine_list, machine_list_is_deny):
    if machine_limit is None and node.parm("machine_limit"):
        machine_limit = node.parm("machine_limit").eval()

    if machine_list is None:
        list_parm = node.parm(_DEADLINE_MACHINE_LIST_PARM)
        machine_list = list_parm.evalAsString() if list_parm else ""

    if machine_list_is_deny is None:
        deny_parm = node.parm(_DEADLINE_MACHINE_DENYLIST_PARM)
        machine_list_is_deny = bool(deny_parm.eval()) if deny_parm else False

    normalized_limit = None
    if machine_limit is not None:
        limit = _safe_int(machine_limit, 0)
        normalized_limit = limit if limit > 0 else None

    normalized_list = _normalized_machine_list(machine_list)
    return normalized_limit, normalized_list, bool(machine_list_is_deny)


def submit_wedges_to_deadline(
    node,
    dependent_job_id=None,
    batch_name=None,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=None,
):
    if hou.hipFile.path() == "untitled.hip":
        raise RuntimeError("Please save the Houdini scene before submitting wedges.")

    ensure_cache_and_version_parms(node)

    attributes = _collect_wedge_attributes(node)
    wedge_count = _wedge_count(node, attributes)
    seed = _seed_value(node)
    f1, f2, step = _frame_range(node)

    version = active_version(node)
    json_path = save_ayon_context_for_node(node, version)
    rop = _get_active_rop(node)
    output_parm = _get_output_parm(node, rop)
    set_output_path_on_submit = _should_set_output_path_on_submit(node)
    wedge_values_all = _build_wedge_values(attributes, wedge_count, seed)
    pool_parm = node.parm("pool")
    pool_name = pool_parm.evalAsString() if pool_parm else "houdini"
    priority = _get_node_priority(node)
    chunk_size = _get_chunk_size(node, f1, f2, step)
    frames = _frames_for_deadline(f1, f2, step)

    batch_name = batch_name or _build_batch_name(node)
    (
        normalized_machine_limit,
        normalized_machine_list,
        deny_list_mode,
    ) = _collect_machine_options(node, machine_limit, machine_list, machine_list_is_deny)

    original_values = {}
    for attr in attributes:
        target = attr["target_parm"]
        if target.path() in original_values:
            continue
        try:
            original_values[target.path()] = target.eval()
        except Exception:
            original_values[target.path()] = None

    original_output = (
        output_parm.evalAsString()
        if (output_parm and set_output_path_on_submit)
        else None
    )
    rop_frame_parm_backup = _set_rop_frame_range_for_submission(rop, f1, f2, step)

    submitted_ids = []
    values_json_by_wedge = {}
    app_version = hou.applicationVersion()
    houdini_version = f"{app_version[0]}.{app_version[1]}"

    try:
        for wedge_index, wedge_values in enumerate(wedge_values_all, start=1):
            for item in wedge_values:
                item["target_parm"].set(item["value"])

            cache_path = _cache_file(node, version, wedge_index)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            if output_parm and set_output_path_on_submit:
                if not _set_parm_safe(
                    output_parm,
                    cache_path,
                    context="submission output path",
                ):
                    raise RuntimeError(
                        "Cannot set output path parameter during wedge submission. "
                        "The parameter may be locked by HDA/take permissions."
                    )

            values_json = _write_values_json(node, version, wedge_index, wedge_values)
            values_json_by_wedge[int(wedge_index)] = values_json
            hip_path = _save_hip_to_wedge_dir(node, version, wedge_index)

            job_info = {
                "Plugin": "Houdini",
                "BatchName": batch_name,
                "Name": _build_job_name(node, wedge_index, version=version),
                "Frames": frames,
                "ChunkSize": chunk_size,
                "Pool": pool_name,
                "Priority": priority,
                "EnvironmentKeyValue0": f"AYON_CONTEXT_JSON={json_path}",
                "EnvironmentKeyValue1": f"BMFX_WEDGE_INDEX={wedge_index}",
                "EnvironmentKeyValue2": f"BMFX_WEDGE_VALUES_JSON={values_json}",
                "EnvironmentKeyValue3": f"BMFX_FROZEN_VERSION={version}",
            }

            if dependent_job_id:
                job_info["JobDependency0"] = dependent_job_id
            if normalized_machine_limit:
                job_info["MachineLimit"] = normalized_machine_limit
            if normalized_machine_list:
                key = "Blacklist" if deny_list_mode else "Whitelist"
                job_info[key] = normalized_machine_list

            plugin_info = {
                "SceneFile": hip_path,
                "OutputDriver": rop.path(),
                "Version": houdini_version,
                "Build": "64bit",
                "IgnoreSceneFileFrameRange": True,
                "StartFrame": f1,
                "EndFrame": f2,
            }
            if step > 1:
                plugin_info["FrameStep"] = step

            job_id = _submit_job_to_deadline(job_info, plugin_info)
            submitted_ids.append(job_id)
            _LOGGER.info(
                "WEDGE SUBMIT | Node: %s | Wedge: w%d | JobID: %s",
                node.path(),
                wedge_index,
                job_id,
            )
    finally:
        for parm_path, original in original_values.items():
            parm = hou.parm(parm_path)
            if not parm:
                continue
            if original is not None:
                _set_parm_safe(parm, original, context="wedge target restore")
        if output_parm and set_output_path_on_submit and original_output is not None:
            _set_parm_safe(output_parm, original_output, context="output path restore")
        _restore_parm_values(rop_frame_parm_backup)

    # Only mark version as submitted if we successfully submitted jobs
    if submitted_ids:
        if _should_submit_review_job(node):
            review_dependency_ids = list(submitted_ids)
            try:
                flipbook_job_id = _submit_wedge_flipbook_images_job(
                    node=node,
                    version=version,
                    wedge_count=wedge_count,
                    f1=f1,
                    f2=f2,
                    step=step,
                    batch_name=batch_name,
                    pool_name=pool_name,
                    priority=priority,
                    dependency_job_ids=submitted_ids,
                    machine_limit=normalized_machine_limit,
                    machine_list=normalized_machine_list,
                    machine_list_is_deny=deny_list_mode,
                )
                if flipbook_job_id:
                    review_dependency_ids = [flipbook_job_id]
                    _LOGGER.info(
                        "WEDGE FLIPBOOK SUBMIT | Node: %s | Version: v%03d | JobID: %s",
                        node.path(),
                        version,
                        flipbook_job_id,
                    )
            except Exception as exc:
                _LOGGER.warning(
                    "Wedge flipbook image job submission failed on %s: %s",
                    node.path(),
                    exc,
                )

            try:
                review_job_id = _submit_wedge_review_job(
                    node=node,
                    version=version,
                    wedge_count=wedge_count,
                    batch_name=batch_name,
                    pool_name=pool_name,
                    priority=priority,
                    dependency_job_ids=review_dependency_ids,
                    wedge_values_files=values_json_by_wedge,
                    machine_limit=normalized_machine_limit,
                    machine_list=normalized_machine_list,
                    machine_list_is_deny=deny_list_mode,
                )
                if review_job_id:
                    _LOGGER.info(
                        "WEDGE REVIEW SUBMIT | Node: %s | Version: v%03d | JobID: %s",
                        node.path(),
                        version,
                        review_job_id,
                    )
            except Exception as exc:
                _LOGGER.warning(
                    "Wedge review job submission failed on %s: %s",
                    node.path(),
                    exc,
                )

        _mark_version_submitted(node, version)
        
        # Update version parm AFTER marking submitted
        version_parm = _first_parm(node, _VERSION_PARMS)
        mode_parm = _first_parm(node, _VERSION_MODE_PARMS)
        
        if version_parm and mode_parm:
            mode_value = mode_parm.evalAsString()
            _set_version_parm_value(version_parm, version, prefer_prefixed=True)
            if _is_auto_mode(mode_value, mode_parm=mode_parm):
                # In auto mode show the version that was just submitted.
                _LOGGER.info(
                    "AUTO VERSION | Node: %s | Submitted: v%03d",
                    node.path(),
                    version,
                )

        update_cache_status(node)

    return submitted_ids


def submit_cache_to_deadline(
    node,
    dependent_job_id=None,
    batch_name=None,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=None,
):
    """
    Farmer-compatible entry point.
    Returns first JobID to keep existing farmer integration stable.
    """
    job_ids = submit_wedges_to_deadline(
        node,
        dependent_job_id=dependent_job_id,
        batch_name=batch_name,
        machine_limit=machine_limit,
        machine_list=machine_list,
        machine_list_is_deny=machine_list_is_deny,
    )
    return job_ids[0] if job_ids else None


def _kwargs_node(kwargs_or_node):
    if isinstance(kwargs_or_node, hou.Node):
        return kwargs_or_node
    if isinstance(kwargs_or_node, dict):
        node = kwargs_or_node.get("node")
        if isinstance(node, hou.Node):
            return node
    return None


def submit_to_deadline(kwargs):
    node = _kwargs_node(kwargs)
    if not node:
        raise RuntimeError("Could not resolve wedge node from callback kwargs.")
    ensure_cache_and_version_parms(node)
    job_ids = submit_wedges_to_deadline(node)
    
    # Sync UI after successful submission
    if job_ids:
        sync_version_ui(node)
    
    hou.ui.displayMessage(
        f"Submitted {len(job_ids)} wedge job(s) to Deadline.",
        title="BMFX Wedge",
    )


def sync_version_ui(kwargs_or_node):
    """
    Optional callback for version/version_mode parms to keep UI value aligned.
    """
    node = _resolve_wedge_node(kwargs_or_node)
    if not node:
        return
    version_parm = _first_parm(node, _VERSION_PARMS)
    mode_parm = _first_parm(node, _VERSION_MODE_PARMS)
    if not version_parm or not mode_parm:
        return
    if _is_auto_mode(mode_parm.evalAsString(), mode_parm=mode_parm):
        current_version = _selected_version_from_parm(version_parm)
        known_versions = _read_submitted_versions(node)
        if current_version and (not known_versions or current_version in known_versions):
            # Keep user-selected version for old cache viewing in auto mode.
            update_cache_status(node)
            return
        _set_version_parm_value(
            version_parm,
            max(1, _latest_known_version(node)),
            prefer_prefixed=True,
        )

    update_cache_status(node)


def refresh_status(kwargs_or_node):
    """
    Button callback helper to force-refresh wedge status/comment/color.
    """
    node = _resolve_wedge_node(kwargs_or_node)
    if not node:
        return

    ensure_cache_and_version_parms(node)
    update_cache_status(node)


def clear_wedge_attributes(kwargs):
    node = _kwargs_node(kwargs)
    if not node:
        return
    count_parm = _first_parm(node, _ATTR_COUNT_PARMS)
    if count_parm:
        count_parm.set(0)


def _update_status_deferred(node_path):
    node = hou.node(node_path)
    if node:
        update_cache_status(node)


def on_node_created(node_or_kwargs):
    """
    Optional OnCreated callback hook for the wedge HDA.
    Accepts either `node` or Houdini `kwargs`.
    """
    node = _resolve_wedge_node(node_or_kwargs)
    if not node:
        return

    ensure_cache_and_version_parms(node)
    update_cache_status(node)

    # Some HDAs finish parm/menu initialization after OnCreated.
    # Defer one more status refresh so comment/color is visible.
    try:
        import hdefereval
        hdefereval.executeDeferred(
            lambda path=node.path(): _update_status_deferred(path)
        )
    except Exception:
        pass


def get_wedge_preview(node):
    """
    Utility for UI/debug: returns wedge values for each index without submission.
    """
    attrs = _collect_wedge_attributes(node)
    count = _wedge_count(node, attrs)
    seed = _seed_value(node)
    values = _build_wedge_values(attrs, count, seed)
    compact = []
    for wedge_index, wedge_values in enumerate(values, start=1):
        row = {"wedge_index": wedge_index, "values": {}}
        for entry in wedge_values:
            row["values"][entry["target_path"]] = entry["value"]
        compact.append(row)
    return compact


def show_wedge_preview(kwargs_or_node):
    """
    Button callback helper: compute wedge preview and show it in a UI dialog.
    """
    node = _resolve_wedge_node(kwargs_or_node)
    if not node:
        hou.ui.displayMessage(
            "Could not resolve wedge node for preview.",
            title="BMFX Wedge Preview",
            severity=hou.severityType.Error,
        )
        return []

    try:
        preview = get_wedge_preview(node)
        pretty = json.dumps(preview, indent=2)
        message = (
            f"Generated preview for {len(preview)} wedge(s).\n\n"
            f"{pretty}"
        )
        hou.ui.displayMessage(
            message,
            title="BMFX Wedge Preview",
        )
        print(pretty)
        return preview
    except Exception as exc:
        hou.ui.displayMessage(
            f"Preview failed:\n{exc}",
            title="BMFX Wedge Preview",
            severity=hou.severityType.Error,
        )
        return []
