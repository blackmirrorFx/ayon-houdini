import hou
import logging
import os
import re
from datetime import datetime
from ayon_core.pipeline import get_current_context
import json
import tempfile
import subprocess
import shutil
from pxr import Usd

# --------------------------------------------------
# LOGGER SETUP (Deadline-safe)
# --------------------------------------------------

_LOGGER = logging.getLogger("BMFX.FileCache")

if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)

_LOGGER.setLevel(logging.INFO)

_FROZEN_WRITE_VERSION = None

_DEADLINE_MACHINE_LIST_PARM = "machine_list"
_DEADLINE_MACHINE_DENYLIST_PARM = "machine_list_is_deny"


def _normalized_machine_list(raw_value):
    if raw_value is None:
        return ""
    tokens = [part.strip() for part in re.split(r"[,\s;]+", str(raw_value)) if part.strip()]
    if not tokens:
        return ""
    deduped = list(dict.fromkeys(tokens))
    return ",".join(deduped)


def _parse_deadline_machine_names(raw_output):
    names = []
    for part in re.split(r"[\r\n,]+", raw_output or ""):
        value = part.strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered.startswith(("result", "success", "error", "warning", "info")):
            continue
        if "=" in value:
            prefix = value.split("=", 1)[0].strip().lower()
            if prefix in {"result", "success", "error", "warning", "info"}:
                continue
        names.append(value)
    return list(dict.fromkeys(names))


def _query_deadline_machine_names():
    for command in ("-GetWorkerNames", "-GetSlaveNames"):
        try:
            output = subprocess.check_output(
                ["deadlinecommand", command],
                stderr=subprocess.STDOUT,
            ).decode()
        except (subprocess.CalledProcessError, OSError):
            continue
        names = _parse_deadline_machine_names(output)
        if names:
            return names
    return []


def pick_deadline_machine_list(kwargs):
    node = None
    if isinstance(kwargs, dict):
        node = kwargs.get("node")
    elif isinstance(kwargs, hou.Node):
        node = kwargs
    if not node:
        return

    ensure_deadline_machine_list_parms(node)
    machine_list_parm = node.parm(_DEADLINE_MACHINE_LIST_PARM)
    if machine_list_parm is None:
        return

    machine_names = _query_deadline_machine_names()
    if not machine_names:
        hou.ui.displayMessage(
            "No Deadline workers found. Check Deadline client connection.",
            title="Deadline",
        )
        return

    selected_now = _normalized_machine_list(machine_list_parm.evalAsString()).split(",")
    selected_now = [name for name in selected_now if name]
    selected_set = set(selected_now)
    default_indices = [
        index for index, name in enumerate(machine_names) if name in selected_set
    ]

    picked = hou.ui.selectFromList(
        machine_names,
        default_choices=default_indices,
        exclusive=False,
        title="Select Deadline Machines",
        message="Choose machines for the Deadline machine list.",
        clear_on_cancel=False,
    )
    if picked is None:
        return

    selected = [machine_names[index] for index in picked]
    machine_list_parm.set(",".join(selected))


def ensure_deadline_machine_list_parms(node):
    if (
        node.parm(_DEADLINE_MACHINE_LIST_PARM)
        and node.parm(_DEADLINE_MACHINE_DENYLIST_PARM)
    ):
        return

    try:
        group = node.parmTemplateGroup()
    except (hou.PermissionError, hou.OperationFailed):
        return

    changed = False

    if not group.find(_DEADLINE_MACHINE_DENYLIST_PARM):
        deny_template = hou.ToggleParmTemplate(
            _DEADLINE_MACHINE_DENYLIST_PARM,
            "Machine List Is Deny List",
            default_value=False,
        )
        try:
            if group.find("single_machine"):
                group.insertAfter("single_machine", deny_template)
            else:
                group.append(deny_template)
        except hou.OperationFailed:
            group.append(deny_template)
        changed = True

    if not group.find(_DEADLINE_MACHINE_LIST_PARM):
        list_template = hou.StringParmTemplate(
            _DEADLINE_MACHINE_LIST_PARM,
            "Machine List",
            1,
            default_value=("",),
        )
        list_template.setHelp(
            "Comma-separated Deadline worker names used for whitelist or deny list."
        )
        list_template.setTags({
            "script_action": (
                "import importlib, ayon_houdini.nodes.lops.filecache as fc; "
                "fc = importlib.reload(fc); "
                "fc.pick_deadline_machine_list(kwargs)"
            ),
            "script_action_icon": "BUTTONS_gear",
            "script_action_help": "Select Deadline machines",
            "script_action_language": "python",
        })
        # Pick a sensible anchor inside the "Farm Settings" area so the
        # Machine List appears near other farm-related params. Try several
        # common parameter names and fall back to appending if nothing
        # matches.
        anchor_parm = None
        if group.find(_DEADLINE_MACHINE_DENYLIST_PARM):
            anchor_parm = _DEADLINE_MACHINE_DENYLIST_PARM
        else:
            for cand in (
                "single_machine",
                "chunks",
                "priority",
                "machine_limit",
                "cache_path",
                "cachepath",
                "cache",
            ):
                if group.find(cand):
                    anchor_parm = cand
                    break

        try:
            if anchor_parm:
                group.insertAfter(anchor_parm, list_template)
            else:
                group.append(list_template)
        except hou.OperationFailed:
            group.append(list_template)
        changed = True

    if changed:
        try:
            node.setParmTemplateGroup(group)
        except (hou.PermissionError, hou.OperationFailed):
            return

def get_ayon_launcher_env():
    return {
        k: v for k, v in os.environ.items()
        if k.startswith(("AYON_", "OPENPYPE_", "AVALON_"))
    }

def get_all_houdini_vars():
    raw = hou.hscript("set")[0]
    data = {}

    for line in raw.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = hou.expandString(v.strip())

    return data

def filter_custom_houdini_vars(vars_dict):
    ALLOWED_PREFIXES = (
        "AYON_",
        "OPENPYPE_",
        "AVALON_",
        "TH_",
        "RES",
        "PIX_",
    )
    ALLOWED_EXACT = {
        "JOB",
        "SEQ",
        "SHOT",
        "APP",
        "LIB",
    }
    return {
        k: v for k, v in vars_dict.items()
        if k in ALLOWED_EXACT or k.startswith(ALLOWED_PREFIXES)
    }

def ayon_context_json_path(node, version):
    ver_dir = version_dir(node, version, create=True)
    return os.path.join(ver_dir, ".ayon_vars.json")

def get_pipeline_env():
    """
    Collect critical pipeline paths so farm can import pipeline code.
    """
    keys = (
        "PYTHONPATH",
        "HOUDINI_PATH",
        "HOUDINI_OTLSCAN_PATH",
    )
    env = {}
    for k in keys:
        val = os.environ.get(k)
        if val:
            env[k] = val

    return env

def save_ayon_context_for_node(node, version):
    path = ayon_context_json_path(node, version)
    data = {
        "launcher_env": get_ayon_launcher_env(),
        "houdini_vars": filter_custom_houdini_vars(
            get_all_houdini_vars()
        ),
        "pipeline_env": get_pipeline_env(),
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def project_root():
    """Resolves the $JOB root with fallbacks."""
    root = os.environ.get("JOB")
    if not root or root == "$JOB":
        root = hou.text.expandString("$JOB")
    if not root or root == "$JOB":
        root = hou.expandString("$HIP")
    return root

def node_cache_root(node):
    """Constructs the base directory for the specific node."""
    ctx = get_current_context()
    proj = ctx.get("project_name", "unknown_project")
    fold = (ctx.get("folder_path") or "unknown_folder").strip("/")
    task = ctx.get("task_name", "unknown_task")
    return os.path.join(project_root(), fold, "houdini", task, "cache", node.name())

def existing_versions(node):
    """Finds all folders matching 'v###' and returns sorted integers."""
    base = node_cache_root(node)
    if not os.path.exists(base):
        return []
    versions = []
    for d in os.listdir(base):
        m = re.match(r"v(\d+)", d)  # FIX: Match 'v' prefix consistently
        if m:
            versions.append(int(m.group(1)))
    return sorted(versions)

def active_version(node):
    """
    Determines the version number to use.
    Strips 'v' prefix if manual mode provides a string like 'v022'.
    """
    mode = node.parm("version_mode").evalAsString()

    if mode == "manual":
        # Get the raw string: e.g., "v022"
        raw_val = node.parm("version").evalAsString()

        # Remove the 'v' if it exists
        clean_val = raw_val.lstrip('v')

        try:
            return int(clean_val)
        except ValueError:
            return 1
    versions = existing_versions(node)
    return versions[-1] + 1 if versions else 1

def version_dir(node, version=None, create=False):
    """
    Constructs the path to the version folder (e.g., v001).
    """
    if version is None:
        version = active_version(node)
    path = os.path.join(node_cache_root(node), f"v{int(version):03d}")
    if create:
        os.makedirs(path, exist_ok=True)
    return path

def is_frameless(node):
    p_frameless = node.parm("frameless")
    p_mode = node.parm("frame_range")
    return bool(
        p_frameless
        and p_mode
        and p_frameless.eval()
        and p_mode.evalAsString() == "off"
    )

def cache_file(node, version=None):
    """
    Resolves the USD sequence cache path.
    """
    if version is None:
        version = active_version(node)
    ext = node.parm("file_format").evalAsString()
    if ext not in {"usd", "usdc", "usda"}:
        ext = "usdc"
    base_dir = version_dir(node, version)
    filename = f"sequence.{ext}" if is_frameless(node) else f"sequence.$F4.{ext}"
    return os.path.join(base_dir, filename)

def active_version(node):
    """
    Determines the version number.
    Farm-safe: respects frozen version from Deadline.
    """
    frozen = os.environ.get("BMFX_FROZEN_VERSION")
    if frozen is not None:
        try:
            return int(frozen)
        except ValueError:
            pass  # fall through if malformed
    mode = node.parm("version_mode").evalAsString()
    if mode == "manual":
        raw_val = node.parm("version").evalAsString()
        clean_val = raw_val.lstrip("v")
        try:
            return int(clean_val)
        except ValueError:
            return 1
    versions = existing_versions(node)
    return versions[-1] + 1 if versions else 1

def cache_file(node, version=None):
    if version is None:
        version = active_version(node)
    ext = node.parm("file_format").evalAsString()
    if ext not in {"usd", "usdc", "usda"}:
        ext = "usdc"
    ver_dir = version_dir(node, version)
    filename = f"sequence.{ext}" if is_frameless(node) else f"sequence.$F4.{ext}"
    return os.path.join(ver_dir, filename)

def main_cache_file(node, version=None):
    if version is None:
        version = active_version(node)
    # Read helpers should always resolve to a stable USD wrapper file.
    return os.path.join(version_dir(node, version), "master.usdc")

def frame_range(node):
    if isinstance(node, str):
        node = hou.node(node)
    if node is None:
        raise ValueError("Invalid node")
    p_frameless = node.parm("frameless")
    p_mode = node.parm("frame_range")
    if p_frameless and p_mode:
        if p_frameless.eval() and p_mode.evalAsString() == "off":
            f = int(hou.frame())
            return f, f
    if p_mode and p_mode.evalAsString() in ("normal", "on"):
        p_f1 = node.parm("f1") or node.parm("fx")
        p_f2 = node.parm("f2") or node.parm("fy")
        if p_f1 and p_f2:
            return int(p_f1.eval()), int(p_f2.eval())
    f = int(hou.frame())
    return f, f


def get_active_rop(node):
    rop = node.node("ropnet/usd")
    if not rop:
        raise RuntimeError(f"USD ROP not found inside {node.name()}")
    return rop

def resolve_output_path(node):
    global _FROZEN_WRITE_VERSION
    if _FROZEN_WRITE_VERSION is not None:
        return cache_file(node, _FROZEN_WRITE_VERSION)
    if node.parm("read_latest") and node.parm("read_latest").eval():
        versions = existing_versions(node)
        v = versions[-1] if versions else 1
    else:
        v = active_version(node)
    return cache_file(node, v)
def resolve_f1(node):
    return frame_range(node)[0]

def resolve_f2(node):
    return frame_range(node)[1]



def save_hip_to_version_dir(node, version):
    version_path = version_dir(node, version, create=True)
    hip_dir = os.path.join(version_path, "hip")
    os.makedirs(hip_dir, exist_ok=True)
    dst = os.path.join(hip_dir, "source.hip")

    # Save current in-memory scene directly to snapshot HIP
    # without renaming/saving the artist's main scene file.
    try:
        hscript_dst = dst.replace("\\", "/").replace('"', '\\"')
        hou.hscript(f'mwrite -n "{hscript_dst}"')
    except hou.OperationFailed as exc:
        raise RuntimeError(
            f"Failed to save snapshot HIP to {dst}: {exc}"
        ) from exc
    return dst

def get_path_for_ui(node):
    """
    This is the function you will call inside the Houdini Parameter.
    It returns the path for the currently selected version/mode.
    """
    try:
        # Determine which version the user is currently 'looking' at
        if node.parm("read_latest") and node.parm("read_latest").eval():
            versions = existing_versions(node)
            v = versions[-1] if versions else 1
        else:
            v = active_version(node)
        return main_cache_file(node, v)
    except:
        return ""

def frozen_version(node):
    p = node.parm("write_version")
    if p:
        val = p.evalAsString()
        if val:
            return int(val)
    return None
def execute_render_and_wrap(node):
    """
    Called by both the 'Write Cache' button and the 
    Deadline Post-Render script.
    """
    global _FROZEN_WRITE_VERSION
    
    # 1. Determine the version to use
    v = _FROZEN_WRITE_VERSION if _FROZEN_WRITE_VERSION else active_version(node)
    
    _LOGGER.info("[USD] Generating master wrapper for v%03d", v)
    create_usd_master_wrapper(node, v)

def write_cache(node):
    global _FROZEN_WRITE_VERSION
    try:
        # Freeze version ONCE per render
        _FROZEN_WRITE_VERSION = active_version(node)

        print(f"[BMFX] Rendering v{_FROZEN_WRITE_VERSION:03d}")
        save_ayon_context_for_node(node, _FROZEN_WRITE_VERSION)
        json_path = ayon_context_json_path(node, _FROZEN_WRITE_VERSION)
        output_path = cache_file(node, _FROZEN_WRITE_VERSION)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # Save HIP snapshot
        save_hip_to_version_dir(node, _FROZEN_WRITE_VERSION)
        rop = get_active_rop(node)
        try:
            rop.render()
        finally:
            execute_render_and_wrap(node)

        if "BMFX_FROZEN_VERSION" not in os.environ:
            node.parm("version").set(str(_FROZEN_WRITE_VERSION))
        set_status(node, "CACHED")
        update_cache_status(node)

    finally:
        #  Always clear after render
        _FROZEN_WRITE_VERSION = None

def _count_prims_under(stage, root_path):
    """
    Count the total number of prims under *root_path* in *stage*
    (including root itself).  Used as a proxy for geometry completeness:
    a frame with more prims has a more complete hierarchy (more parts, LODs,
    etc.) and is therefore a better topology source.

    Cheap — never reads attribute values, only traverses prim metadata.
    """
    from pxr import Usd
    root_prim = stage.GetPrimAtPath(root_path)
    if not root_prim or not root_prim.IsValid():
        return 0
    return sum(1 for _ in Usd.PrimRange(root_prim))


#: How many evenly-spaced frames to open when searching for the topology frame.
#: Higher = more accurate but slower wrapper creation.
_TOPO_SAMPLE_COUNT = 10


def _find_topology_frame(ver_dir, seq_candidates):
    """
    Return ``(clip_name, root_path, root_type)`` for the frame in
    *seq_candidates* that has the **most complete prim hierarchy**.

    Why "most prims" instead of "first non-empty"?
    -----------------------------------------------
    For a prop with multiple geometry layers (inside, outside, cork, pipe)
    the very first frame might only have *one* layer authored (e.g. only
    ``outside`` exists on frame 1001) while the rest appear on later frames.
    Referencing that first frame as the topology source means the wrapper
    stage never knows about the missing prims — they simply don't exist in
    the composed stage regardless of what the clip files contain.

    By sampling frames across the full range and choosing the one with the
    highest prim count, we capture the most complete geometry description:

    - **FX / particles**: early frames have 0 prims (nothing born yet),
      later frames have the full point cloud → later frame wins.
    - **Props / characters**: all frames should have all parts, but some
      parts may only be authored on certain frames → the most complete
      frame wins.

    Sampling strategy
    -----------------
    Opens at most ``_TOPO_SAMPLE_COUNT`` frames, evenly spaced across the
    sequence (always including the first and last).  If the sequence is
    shorter than the sample count, every frame is checked.

    Falls back to the very first readable frame if nothing has any prims.
    """
    from pxr import Usd

    if not seq_candidates:
        return None, None, None

    total = len(seq_candidates)
    if total <= _TOPO_SAMPLE_COUNT:
        sample = seq_candidates
    else:
        # Evenly spaced, always include index 0 and index total-1.
        indices = set()
        indices.add(0)
        indices.add(total - 1)
        for k in range(1, _TOPO_SAMPLE_COUNT - 1):
            idx = int(round(k * (total - 1) / (_TOPO_SAMPLE_COUNT - 1)))
            indices.add(idx)
        sample = [seq_candidates[i] for i in sorted(indices)]

    # best = (prim_count, frame_num, clip_name, root_path, root_type)
    best = None
    fallback = None  # first readable frame, regardless of prim count

    for item in sample:
        _, frame_num, frame_str, clip_base, clip_ext, clip_name = item
        clip_path = os.path.join(ver_dir, clip_name).replace("\\", "/")

        stage = Usd.Stage.Open(clip_path)
        if not stage:
            continue

        # Determine root prim path and type.
        default_prim = stage.GetDefaultPrim()
        if default_prim and default_prim.IsValid():
            root_path = default_prim.GetPath().pathString
            root_type = default_prim.GetTypeName() or "Xform"
        else:
            roots = stage.GetPseudoRoot().GetChildren()
            if not roots:
                del stage
                continue
            root_path = roots[0].GetPath().pathString
            root_type = roots[0].GetTypeName() or "Xform"

        if fallback is None:
            fallback = (clip_name, root_path, root_type)

        prim_count = _count_prims_under(stage, root_path)
        del stage

        if best is None or prim_count > best[0]:
            best = (prim_count, frame_num, clip_name, root_path, root_type)

    if best and best[0] > 0:
        prim_count, frame_num, clip_name, root_path, root_type = best
        _LOGGER.info(
            "[USD] Topology frame: %s (frame %s) — %d prims (most complete in sample).",
            clip_name, frame_num, prim_count,
        )
        return clip_name, root_path, root_type

    if fallback:
        _LOGGER.warning(
            "[USD] No frame with geometry prims found in %s; "
            "using first readable frame as topology source.",
            ver_dir,
        )
        return fallback

    return None, None, None


def _sanitize_usd_prim_name(name):
    """
    Convert an arbitrary string into a valid USD prim name.
    USD prim names must match [A-Za-z_][A-Za-z0-9_]*.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized or "fx_cache"


def _unwrap_container_prim(stage, root_path):
    """
    Unwrap only known export containers such as /Geometry.

    Do NOT unwrap scene roots like /World.
    """
    from pxr import Usd

    # Only these roots are allowed to be removed.
    UNWRAPPABLE_ROOTS = {
        "/Geometry",
        "/ROOT",
        "/Root",
    }

    if root_path not in UNWRAPPABLE_ROOTS:
        return root_path, None

    prim = stage.GetPrimAtPath(root_path)
    if not (prim and prim.IsValid()):
        return root_path, None

    if prim.GetTypeName() not in ("", "Xform", "Scope"):
        return root_path, None

    if prim.GetAuthoredAttributes():
        return root_path, None

    children = prim.GetChildren()
    if len(children) != 1:
        return root_path, None

    child = children[0]
    return child.GetPath().pathString, child.GetTypeName() or "Xform"


def _should_preserve_shallow_root_path(root_path, stage=None):
    """
    Keep meaningful shallow scene roots like ``/World`` intact.

    Generic cache export roots like ``/Geometry`` should still be remapped to
    a node-name wrapper so stacked caches do not collide on ClipsAPI metadata.
    """
    parts = [part for part in root_path.strip("/").split("/") if part]
    if len(parts) != 1:
        return False

    root_name = parts[0]
    if root_name.lower() in {"geometry", "geo"}:
        return False

    if stage is not None:
        prim = stage.GetPrimAtPath(root_path)
        if prim and prim.IsValid():
            if prim.GetChildren():
                return True
            if prim.GetAuthoredAttributes():
                return True

    return True


def _resolve_wrapper_prim_path(clip_root_path, node_name, topo_stage=None, node=None):
    """
    Determine how the master wrapper should map the clip hierarchy into the
    composed stage.

    Returns
    -------
    wrapper_prim_path : str
        Where to define the root prim inside ``master.usdc``.
    default_prim_name : str
        Root component of ``wrapper_prim_path`` for ``stage.SetDefaultPrim()``.
    clip_ref_path : str
        Path **inside the clip file** to use for ``AddReference()``  — gives
        the prim its schema/type/children (topology).
    clip_anim_path : str
        Path **inside each frame file** to set as ``SetClipPrimPath()`` —
        tells USD where to read time-sampled attribute values from.

    Priority order
    --------------
    1. Explicit node parameter (``usd_scene_path`` / ``primpath`` / etc.).
    2. Deep clip path (> 1 component) — already unique; preserved as-is.
    3. Unwrapped shallow container — strips the container prefix so the scene
       hierarchy is preserved without double-nesting.
    4. Preserve meaningful shallow scene roots like ``/World``.
    5. Node-name fallback — renames generic roots like ``/Geometry`` to
       ``/{node_name}`` to prevent ClipsAPI collision when multiple FX caches
       are sublayered.

    Unwrap example
    --------------
    Clip files export with ``/Geometry`` as defaultPrim, but inside is::

        /Geometry          <- empty Xform container (clip_root_path)
          /Geometry/World  <- sole child (inner_abs_path)
            /Shot/fx/droplets/Droplet_G

    Result::

        wrapper_prim_path = "/World"             (inner suffix, strip /Geometry)
        clip_ref_path     = "/Geometry/World"    (AddReference target in clip)
        clip_anim_path    = "/Geometry/World"    (SetClipPrimPath in frames)

    This makes ``master.usdc`` present ``/World/Shot/fx/droplets/Droplet_G``
    without any double-nesting.
    """
    # 1. Explicit override from a node parameter.
    if node is not None:
        for parm_name in ("usd_scene_path", "usd_prim_path", "primpath", "rootprim"):
            p = node.parm(parm_name)
            if p:
                val = p.evalAsString().strip()
                if val and val.startswith("/") and len(val) > 1:
                    parts = [x for x in val.strip("/").split("/") if x]
                    # Reference and animate from the original clip root.
                    return val, parts[0], clip_root_path, clip_root_path

    parts = [p for p in clip_root_path.strip("/").split("/") if p]

    # 2. Deep path — already unique; preserved as-is.
    if len(parts) > 1:
        return clip_root_path, parts[0], clip_root_path, clip_root_path

    # 3. Shallow container — try to unwrap.
    if topo_stage is not None:
        inner_abs, inner_type = _unwrap_container_prim(topo_stage, clip_root_path)
        if inner_abs != clip_root_path:
            # Strip the container prefix to get the scene-correct wrapper path.
            # e.g.  inner_abs="/Geometry/World"  clip_root_path="/Geometry"
            #       wrapper = "/World"  (the suffix after the container)
            wrapper = inner_abs[len(clip_root_path):]  # e.g. "/World"
            w_parts = [p for p in wrapper.strip("/").split("/") if p]
            if w_parts:
                _LOGGER.info(
                    "[USD] Unwrapped container %s → wrapper=%s  ref/anim=%s",
                    clip_root_path, wrapper, inner_abs,
                )
                # clip_ref_path = clip_anim_path = inner_abs (the ACTUAL path
                # inside clip files where the prim lives).
                return wrapper, w_parts[0], inner_abs, inner_abs

    # 4. Preserve meaningful shallow scene roots like /World.
    if _should_preserve_shallow_root_path(clip_root_path, stage=topo_stage):
        return clip_root_path, parts[0], clip_root_path, clip_root_path

    # 5. Node-name fallback — rename generic roots to avoid ClipsAPI collision.
    return f"/{node_name}", node_name, clip_root_path, clip_root_path


def create_usd_master_wrapper(node, version=None):
    """
    Build a ``master.usdc`` wrapper that is safe to stack with other FX
    caches via SublayerLOP, ReferenceLOP, or PayloadLOP.

    Multi-FX stacking problem (and fix)
    ------------------------------------
    All FX caches written by a SOP Import → USD ROP share the same root prim
    path (``/Geometry``).  When you sublayer two or more ``master.usdc``
    files that both define ClipsAPI on ``/Geometry``, USD's composition rules
    discard every ClipsAPI but the strongest (topmost sublayer).  The other
    caches appear frozen on their topology frame.

    Fix: the wrapper defines its root prim at ``/{node_name}`` (e.g.
    ``/droplets_pts``) — unique per node — so each cache lives at a different
    path in the composed stage and ClipsAPI metadata never collides.

    Topology still comes from the first non-empty clip frame, but via a
    **Reference arc** (``AddReference``) that maps the clip's ``/Geometry``
    into ``/{node_name}``.  ``SetClipPrimPath`` still points to ``/Geometry``
    inside the per-frame files so time-varying attribute values resolve
    correctly across the full frame range.

    Structure of master.usdc
    ------------------------
    ::

        master.usdc
          defaultPrim = {node_name}
          startTimeCode / endTimeCode
          /{node_name}  [type = Points / Geometry / …]
            Reference → ./sequence.TOPO.usdc @ /Geometry   (topology + schema)
            ClipsAPI:
              templateAssetPath  = sequence.####.usdc
              templateStart/End/Stride
              clipPrimPath       = /Geometry   ← path INSIDE each clip file
    """
    if version is None:
        version = active_version(node)

    from pxr import Usd, Sdf

    # Node name sanitized for USD — used as fallback prim name for generic paths.
    node_name = _sanitize_usd_prim_name(node.name())

    ver_dir = version_dir(node, version)
    main_path = os.path.join(ver_dir, "master.usdc").replace("\\", "/")

    f1, f2 = frame_range(node)

    # ------------------------------------------------------------------
    # 1. Find clip files on disk
    # ------------------------------------------------------------------
    seq_rx = re.compile(r"^(sequence|main)\.(\d+)\.(usd|usdc|usda)$")
    single_rx = re.compile(r"^(sequence|main)\.(usd|usdc|usda)$")
    seq_candidates = []
    single_candidates = []

    for name in os.listdir(ver_dir):
        seq_match = seq_rx.match(name)
        if seq_match:
            base, frame_str, ext = seq_match.groups()
            preference = 0 if base == "sequence" else 1
            seq_candidates.append(
                (preference, int(frame_str), frame_str, base, ext, name)
            )
            continue

        single_match = single_rx.match(name)
        if single_match:
            base, ext = single_match.groups()
            preference = 0 if base == "sequence" else 1
            single_candidates.append((preference, base, ext, name))

    is_sequence = bool(seq_candidates)
    if is_sequence:
        seq_candidates.sort(key=lambda item: (item[0], item[1]))
        _, first_frame, first_frame_str, clip_base, clip_ext, _ = seq_candidates[0]
        pad = len(first_frame_str)

        # Find the first frame that actually has geometry data.
        # FX caches often have empty early frames before particles are born.
        topo_clip_name, clip_root_path, clip_root_type = _find_topology_frame(
            ver_dir, seq_candidates
        )
        if topo_clip_name is None:
            _LOGGER.error("[USD] No readable clip files found in %s", ver_dir)
            return None

    elif single_candidates:
        single_candidates.sort(key=lambda item: item[0])
        _, clip_base, clip_ext, topo_clip_name = single_candidates[0]
        first_frame = f1
        pad = 1

        topo_path_abs = os.path.join(ver_dir, topo_clip_name).replace("\\", "/")
        clip_stage = Usd.Stage.Open(topo_path_abs)
        if not clip_stage:
            _LOGGER.error("[USD] Could not open clip file: %s", topo_path_abs)
            return None
        default_prim = clip_stage.GetDefaultPrim()
        if default_prim and default_prim.IsValid():
            clip_root_path = default_prim.GetPath().pathString
            clip_root_type = default_prim.GetTypeName() or "Xform"
        else:
            roots = clip_stage.GetPseudoRoot().GetChildren()
            if not roots:
                _LOGGER.error("[USD] No root prim in clip: %s", topo_path_abs)
                return None
            clip_root_path = roots[0].GetPath().pathString
            clip_root_type = roots[0].GetTypeName() or "Xform"
        del clip_stage
    else:
        _LOGGER.error(
            "[USD] No clip files found in %s (expected sequence.*.<usd/usdc/usda>)",
            ver_dir,
        )
        return None

    # ------------------------------------------------------------------
    # 3. Resolve wrapper prim path
    # ------------------------------------------------------------------
    # Open the topology frame (read-only) so the resolver can peek inside
    # shallow container prims (e.g. /Geometry wrapping /World/Shot/fx/...).
    topo_path_abs = os.path.join(ver_dir, topo_clip_name).replace("\\", "/")
    _topo_stage_for_peek = Usd.Stage.Open(topo_path_abs)

    wrapper_prim_path, default_prim_name, clip_ref_path, clip_anim_path = (
        _resolve_wrapper_prim_path(
            clip_root_path,
            node_name,
            topo_stage=_topo_stage_for_peek,
            node=node,
        )
    )
    del _topo_stage_for_peek

    _LOGGER.info(
        "[USD] clip_root=%s → wrapper=%s  ref=%s  anim=%s",
        clip_root_path, wrapper_prim_path, clip_ref_path, clip_anim_path,
    )

    # ------------------------------------------------------------------
    # 4. Write master.usdc
    # ------------------------------------------------------------------
    if os.path.exists(main_path):
        try:
            os.remove(main_path)
        except OSError:
            pass

    stage = Usd.Stage.CreateNew(main_path)
    stage.SetStartTimeCode(f1)
    stage.SetEndTimeCode(f2)

    # Define the prim at wrapper_prim_path.
    # Use effective_type = type of the prim at clip_ref_path (may differ from
    # clip_root_type when the container was unwrapped).
    #
    # For unwrapped paths we open the topo stage again briefly to get the
    # inner prim's type; for all other cases clip_root_type is correct.
    effective_type = clip_root_type
    if clip_ref_path != clip_root_path:
        _ts = Usd.Stage.Open(topo_path_abs)
        if _ts:
            _inner_prim = _ts.GetPrimAtPath(clip_ref_path)
            if _inner_prim and _inner_prim.IsValid():
                effective_type = _inner_prim.GetTypeName() or clip_root_type
            del _ts

    root_prim = stage.DefinePrim(wrapper_prim_path, effective_type)

    # defaultPrim must be a ROOT-level prim (single component).
    default_prim = stage.GetPrimAtPath("/" + default_prim_name)
    if default_prim and default_prim.IsValid():
        stage.SetDefaultPrim(default_prim)

    # Reference the topology frame using clip_ref_path.
    # For unwrapped paths this points to the INNER prim (e.g. /Geometry/World)
    # so children like /Shot/fx/droplets appear directly under our wrapper prim
    # without double-nesting.
    root_prim.GetReferences().AddReference(
        f"./{topo_clip_name}",
        clip_ref_path,
    )

    if is_sequence:
        clips = Usd.ClipsAPI(root_prim)
        clip_template = f"{clip_base}.{'#' * pad}.{clip_ext}"
        clips.SetClipTemplateAssetPath(clip_template)
        clips.SetClipTemplateStartTime(f1)
        clips.SetClipTemplateEndTime(f2)
        clips.SetClipTemplateStride(1.0)
        # clip_anim_path = path INSIDE each per-frame file where this prim's
        # time-sampled attributes live.  For unwrapped paths this is the inner
        # absolute path (e.g. /Geometry/World), NOT the container root.
        clips.SetClipPrimPath(clip_anim_path)
    else:
        pass  # Frameless: reference above gives us the static data.

    stage.GetRootLayer().Save()

    _LOGGER.info(
        "[USD] Master wrapper created: %s | wrapperPrim: %s | clipPrimPath: %s | topo: %s",
        main_path,
        wrapper_prim_path,
        clip_anim_path,
        topo_clip_name,
    )







def read_version(node):
    if node.parm("read_latest") and node.parm("read_latest").eval():
        versions = existing_versions(node)
        return versions[-1] if versions else None

    p = node.parm("version")
    if not p:
        return None

    val = p.evalAsString()  # e.g. "v012"
    try:
        return int(val.lstrip("v"))
    except Exception:
        return None

def eval_read_cache_path(node):
    try:
        v = read_version(node)
        if not v:
            return ""

        return main_cache_file(node, v)
    except Exception:
        return ""


def expected_frame_range(node):
    # If 'off', we only expect the current frame
    p_mode = node.parm("frame_range")
    if p_mode and p_mode.evalAsString() == "off":
        f = int(hou.frame())
        return f, f

    # Check fx and fy
    p_f1 = node.parm("fx")
    p_f2 = node.parm("fy")

    if p_f1 and p_f2:
        return int(p_f1.eval()), int(p_f2.eval())

    return None


def missing_frames_in_range(node, version):
    path = cache_file(node, version)

    # For USD frame sequences, ensure each expected frame file exists.
    if "$F" not in path:
        expanded = hou.expandString(path)
        return [] if os.path.exists(expanded) else ["FILE_MISSING"]

    fr = expected_frame_range(node)
    if not fr:
        return []

    start, end = fr
    missing = []
    m = re.search(r"\$F(\d*)", path)
    pad = int(m.group(1)) if m and m.group(1) else 1

    for f in range(start, end + 1):
        # Manually swap $F4 with the actual frame number
        frame_str = str(f).zfill(pad)
        frame_path = path.replace(m.group(0), frame_str)
        full_path = hou.expandString(frame_path)

        if not os.path.exists(full_path):
            missing.append(f)

    return missing

def set_status(node, state):
    ensure_deadline_machine_list_parms(node)
    COLORS = {
        "LIVE":          (0.3, 0.3, 0.3),
        "CACHED":        (0.1, 0.6, 0.1),
        "OLDER CACHE":   (0.9, 0.6, 0.1),
        "MISSING CACHE": (0.8, 0.3, 0.1),
        "ERROR":         (0.8, 0.1, 0.1),
    }

    node.setColor(hou.Color(COLORS.get(state, COLORS["LIVE"])))
    if state == "LIVE":
        node.setComment(">> LIVE")
    elif state == "MISSING CACHE":
        node.setComment("! MISSING CACHE")
    elif state == "OLDER CACHE":
        node.setComment("! USING OLDER CACHE")
    else:
        node.setComment(f"Status: {state}")

    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)

def _build_default_batch_name(node, version):
    hip_name = os.path.splitext(hou.hipFile.basename())[0] or "untitled"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{hip_name} | {node.name()} | v{int(version):03d} | {timestamp}"


def _write_deadline_info_file(data, suffix):
    info_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    with open(info_file.name, "wb") as stream:
        for key, value in data.items():
            stream.write(f"{key}={value}\n".encode())
    return info_file.name


def _submit_deadline_files(job_info, plugin_info):
    job_file = _write_deadline_info_file(job_info, "_job.info")
    plugin_file = _write_deadline_info_file(plugin_info, "_plugin.info")

    try:
        result = subprocess.check_output(
            ["deadlinecommand", job_file, plugin_file],
            stderr=subprocess.STDOUT,
        ).decode()
        _LOGGER.debug("Deadline output:\n%s", result.strip())

        match = re.search(r"JobID=([\w\d]+)", result)
        if match:
            return match.group(1)

        _LOGGER.error(
            "DEADLINE ERROR | Job submitted but JobID not found in output:\n%s",
            result.strip(),
        )
        return None

    except subprocess.CalledProcessError as exc:
        _LOGGER.error(
            "DEADLINE ERROR | Submission failed:\n%s",
            exc.output.decode().strip(),
        )
        return None
    finally:
        for path in (job_file, plugin_file):
            if os.path.exists(path):
                os.remove(path)


def _resolve_hython_executable():
    exe_name = "hython.exe" if os.name == "nt" else "hython"

    hfs = hou.getenv("HFS") or os.environ.get("HFS")
    if hfs:
        return os.path.join(hfs, "bin", exe_name)

    found = shutil.which(exe_name)
    if found:
        return found

    try:
        major, minor = hou.applicationVersion()[:2]
        if os.name != "nt":
            return f"/opt/hfs{major}.{minor}/bin/hython"
    except Exception:
        pass

    return exe_name


def _master_wrapper_job_script_source(ver_dir, f1, f2, node_name):
    ver_dir_literal = json.dumps(ver_dir.replace("\\", "/"))
    node_name_literal = json.dumps(node_name)
    return f'''import os
import re
import sys
from pxr import Usd, Sdf

VER_DIR = {ver_dir_literal}
NODE_NAME = {node_name_literal}
F1 = {int(f1)}
F2 = {int(f2)}
MAIN_PATH = os.path.join(VER_DIR, "master.usdc").replace("\\\\", "/")


def _log(message):
    print("[BMFX USD Wrapper] " + message, flush=True)


def _pick_clip_files():
    seq_rx = re.compile(r"^(sequence|main)\\.(\\d+)\\.(usd|usdc|usda)$")
    single_rx = re.compile(r"^(sequence|main)\\.(usd|usdc|usda)$")
    seq_candidates = []
    single_candidates = []

    for name in os.listdir(VER_DIR):
        seq_match = seq_rx.match(name)
        if seq_match:
            base, frame_str, ext = seq_match.groups()
            preference = 0 if base == "sequence" else 1
            seq_candidates.append(
                (preference, int(frame_str), frame_str, base, ext, name)
            )
            continue

        single_match = single_rx.match(name)
        if single_match:
            base, ext = single_match.groups()
            preference = 0 if base == "sequence" else 1
            single_candidates.append((preference, base, ext, name))

    if seq_candidates:
        seq_candidates.sort(key=lambda item: (item[0], item[1]))
        return True, seq_candidates[0]

    if single_candidates:
        single_candidates.sort(key=lambda item: item[0])
        return False, single_candidates[0]

    raise RuntimeError(
        "No clip files found in {{}} (expected sequence.*.<usd/usdc/usda>)".format(
            VER_DIR
        )
    )


def _count_prims_under(stage, root_path):
    root_prim = stage.GetPrimAtPath(root_path)
    if not root_prim or not root_prim.IsValid():
        return 0
    return sum(1 for _ in Usd.PrimRange(root_prim))


TOPO_SAMPLE_COUNT = 10


def _find_topology_frame(seq_candidates):
    """
    Return (topo_clip_name, root_path, root_type) for the frame with the
    most complete prim hierarchy.

    Samples up to TOPO_SAMPLE_COUNT evenly-spaced frames and picks the one
    with the highest prim count.  This handles both:
    - FX caches: early frames are empty; later frames have geometry.
    - Props: early frames may only have partial geometry (one layer); the
      frame with the most prims (inside + outside + cork + pipe, etc.) wins.
    """
    if not seq_candidates:
        return None, None, None

    total = len(seq_candidates)
    if total <= TOPO_SAMPLE_COUNT:
        sample = seq_candidates
    else:
        indices = set()
        indices.add(0)
        indices.add(total - 1)
        for k in range(1, TOPO_SAMPLE_COUNT - 1):
            idx = int(round(k * (total - 1) / (TOPO_SAMPLE_COUNT - 1)))
            indices.add(idx)
        sample = [seq_candidates[i] for i in sorted(indices)]

    best = None      # (prim_count, frame_num, clip_name, root_path, root_type)
    fallback = None  # first readable frame regardless of prim count

    for item in sample:
        _, frame_num, frame_str, clip_base, clip_ext, clip_name = item
        clip_path = os.path.join(VER_DIR, clip_name).replace("\\\\", "/")

        stage = Usd.Stage.Open(clip_path)
        if not stage:
            continue

        default_prim = stage.GetDefaultPrim()
        if default_prim and default_prim.IsValid():
            root_path = default_prim.GetPath().pathString
            root_type = default_prim.GetTypeName() or "Xform"
        else:
            roots = stage.GetPseudoRoot().GetChildren()
            if not roots:
                del stage
                continue
            root_path = roots[0].GetPath().pathString
            root_type = roots[0].GetTypeName() or "Xform"

        if fallback is None:
            fallback = (clip_name, root_path, root_type)

        prim_count = _count_prims_under(stage, root_path)
        del stage

        if best is None or prim_count > best[0]:
            best = (prim_count, frame_num, clip_name, root_path, root_type)

    if best and best[0] > 0:
        prim_count, frame_num, clip_name, root_path, root_type = best
        _log("Topology frame: " + clip_name + " (frame " + str(frame_num) + ") — " + str(prim_count) + " prims (most complete in sample).")
        return clip_name, root_path, root_type

    if fallback:
        _log("WARNING: no frame with geometry prims found; using first readable frame as topology source.")
        return fallback

    return None, None, None


def _clip_root_single(clip_name):
    clip_path = os.path.join(VER_DIR, clip_name).replace("\\\\", "/")
    clip_stage = Usd.Stage.Open(clip_path)
    if not clip_stage:
        raise RuntimeError("Could not open clip file: " + clip_path)

    default_prim = clip_stage.GetDefaultPrim()
    if default_prim and default_prim.IsValid():
        return default_prim.GetPath().pathString, default_prim.GetTypeName() or "Xform"

    roots = clip_stage.GetPseudoRoot().GetChildren()
    if not roots:
        raise RuntimeError("No root prim found in clip: " + clip_path)

    return roots[0].GetPath().pathString, roots[0].GetTypeName() or "Xform"


def _should_preserve_shallow_root_path(root_path, stage=None):
    parts = [part for part in root_path.strip("/").split("/") if part]
    if len(parts) != 1:
        return False

    root_name = parts[0]
    if root_name.lower() in ("geometry", "geo"):
        return False

    if stage is not None:
        prim = stage.GetPrimAtPath(root_path)
        if prim and prim.IsValid():
            if prim.GetChildren():
                return True
            if prim.GetAuthoredAttributes():
                return True

    return True


def main():
    if not os.path.isdir(VER_DIR):
        raise RuntimeError("Version directory does not exist: " + VER_DIR)

    # Sanitize node name — used as fallback prim name for generic/shallow paths.
    node_name_safe = re.sub(r"[^A-Za-z0-9_]", "_", NODE_NAME)
    if node_name_safe and node_name_safe[0].isdigit():
        node_name_safe = "_" + node_name_safe
    node_name_safe = node_name_safe or "fx_cache"

    is_sequence, clip_data = _pick_clip_files()
    if is_sequence:
        _, first_frame, first_frame_str, clip_base, clip_ext, _ = clip_data
        pad = len(first_frame_str)

        # Rebuild full sorted seq_candidates list to scan for first non-empty frame.
        seq_rx = re.compile(r"^(sequence|main)\\.(\\d+)\\.(usd|usdc|usda)$")
        all_seq = []
        for name in os.listdir(VER_DIR):
            m = seq_rx.match(name)
            if m:
                base, fstr, ext = m.groups()
                pref = 0 if base == "sequence" else 1
                all_seq.append((pref, int(fstr), fstr, base, ext, name))
        all_seq.sort(key=lambda x: (x[0], x[1]))

        topo_clip_name, clip_root_path, clip_root_type = _find_topology_frame(all_seq)
        if topo_clip_name is None:
            raise RuntimeError("No readable clip files found in " + VER_DIR)
    else:
        _, clip_base, clip_ext, topo_clip_name = clip_data
        first_frame = F1
        pad = 1
        clip_root_path, clip_root_type = _clip_root_single(topo_clip_name)

    # ------------------------------------------------------------------
    # Resolve wrapper prim path (mirrors local _resolve_wrapper_prim_path).
    # Returns: wrapper_prim_path, default_prim_name, clip_ref_path, clip_anim_path
    # ------------------------------------------------------------------
    parts = [p for p in clip_root_path.strip("/").split("/") if p]
    clip_ref_path = clip_root_path    # default: reference the clip root
    clip_anim_path = clip_root_path   # default: animate from clip root
    effective_type = clip_root_type

    if len(parts) > 1:
        # Deep path — already unique; preserve it.
        wrapper_prim_path = clip_root_path
        default_prim_name = parts[0]
    else:
        # Shallow path — try to unwrap single-child Xform container first.
        topo_abs = os.path.join(VER_DIR, topo_clip_name).replace("\\\\", "/")
        _peek_stage = Usd.Stage.Open(topo_abs)
        inner_abs = clip_root_path
        inner_type = clip_root_type
        if _peek_stage and clip_root_path in ("/Geometry", "/ROOT", "/Root"):
            _root = _peek_stage.GetPrimAtPath(clip_root_path)
            if _root and _root.IsValid() and _root.GetTypeName() in ("", "Xform", "Scope"):
                if not _root.GetAuthoredAttributes():
                    _children = _root.GetChildren()
                    if len(_children) == 1:
                        _child = _children[0]
                        inner_abs = _child.GetPath().pathString
                        inner_type = _child.GetTypeName() or "Xform"
                        _log("Unwrapped container " + clip_root_path + " to " + inner_abs)

        if inner_abs != clip_root_path:
            # Strip the container prefix to get the scene-correct wrapper path.
            # e.g. inner_abs="/Geometry/World" clip_root_path="/Geometry"
            #      wrapper = "/World"
            wrapper_path = inner_abs[len(clip_root_path):]
            w_parts = [p for p in wrapper_path.strip("/").split("/") if p]
            if w_parts:
                wrapper_prim_path = wrapper_path
                default_prim_name = w_parts[0]
                clip_ref_path = inner_abs      # reference the INNER prim in clip
                clip_anim_path = inner_abs     # animate from the INNER prim path
                effective_type = inner_type
                _log("wrapper=" + wrapper_prim_path + "  ref/anim=" + clip_ref_path)
            else:
                wrapper_prim_path = "/" + node_name_safe
                default_prim_name = node_name_safe
        else:
            if _should_preserve_shallow_root_path(clip_root_path, stage=_peek_stage):
                wrapper_prim_path = clip_root_path
                default_prim_name = parts[0]
            else:
                # Pure FX geometry prim — rename to /{node_name} to avoid collision.
                wrapper_prim_path = "/" + node_name_safe
                default_prim_name = node_name_safe
        del _peek_stage

    _log("clip_root=" + clip_root_path + "  wrapper=" + wrapper_prim_path + "  ref=" + clip_ref_path + "  anim=" + clip_anim_path)

    if os.path.exists(MAIN_PATH):
        os.remove(MAIN_PATH)

    stage = Usd.Stage.CreateNew(MAIN_PATH)
    stage.SetStartTimeCode(F1)
    stage.SetEndTimeCode(F2)

    root_prim = stage.DefinePrim(wrapper_prim_path, effective_type)

    # defaultPrim must be a root-level prim.
    default_prim = stage.GetPrimAtPath("/" + default_prim_name)
    if default_prim and default_prim.IsValid():
        stage.SetDefaultPrim(default_prim)

    # Reference clip_ref_path (the INNER prim for unwrapped cases) so children
    # appear directly under wrapper_prim_path without double-nesting.
    root_prim.GetReferences().AddReference("./{{}}".format(topo_clip_name), clip_ref_path)

    if is_sequence:
        clips = Usd.ClipsAPI(root_prim)
        clip_template = "{{}}.{{}}.{{}}".format(clip_base, "#" * pad, clip_ext)
        clips.SetClipTemplateAssetPath(clip_template)
        clips.SetClipTemplateStartTime(F1)
        clips.SetClipTemplateEndTime(F2)
        clips.SetClipTemplateStride(1.0)
        # clip_anim_path = inner absolute path in each frame file for time samples.
        clips.SetClipPrimPath(clip_anim_path)

    stage.GetRootLayer().Save()

    if not os.path.exists(MAIN_PATH):
        raise RuntimeError("Save completed but master.usdc was not found: " + MAIN_PATH)

    _log("Created " + MAIN_PATH + " | wrapperPrim: " + wrapper_prim_path + " | clipPrimPath: " + clip_anim_path + " | topo: " + topo_clip_name)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log("ERROR: " + str(exc))
        raise
'''


def _write_master_wrapper_job_script(node, version, f1, f2):
    ver_dir = version_dir(node, version, create=True)
    node_name = _sanitize_usd_prim_name(node.name())
    script_path = os.path.join(ver_dir, ".create_master_wrapper.py")
    with open(script_path, "w") as stream:
        stream.write(_master_wrapper_job_script_source(ver_dir, f1, f2, node_name))
    return script_path


def _submit_master_wrapper_job(
    node,
    version,
    f1,
    f2,
    batch_name,
    priority,
    dependency_job_id,
    json_path,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=False,
):
    script_path = _write_master_wrapper_job_script(node, version, f1, f2)
    hython_path = _resolve_hython_executable()

    job_info = {
        "Plugin": "CommandLine",
        "BatchName": batch_name,
        "Name": f"{node.name()} | v{version:03d} | master.usdc",
        "Frames": "0",
        "ChunkSize": 1,
        "Pool": "houdini",
        "Priority": priority,
        "JobDependency0": dependency_job_id,
        "EnvironmentKeyValue0": f"AYON_CONTEXT_JSON={json_path}",
        "EnvironmentKeyValue1": f"BMFX_FROZEN_VERSION={version}",
    }

    if machine_limit:
        job_info["MachineLimit"] = machine_limit
    if machine_list:
        key = "Blacklist" if machine_list_is_deny else "Whitelist"
        job_info[key] = machine_list

    plugin_info = {
        "Executable": hython_path,
        "Arguments": f'"{script_path}"',
        "StartupDirectory": version_dir(node, version),
        "ShellExecute": False,
        "SingleFramesOnly": True,
    }

    wrapper_job_id = _submit_deadline_files(job_info, plugin_info)
    if wrapper_job_id:
        _LOGGER.info(
            "DEADLINE WRAPPER SUCCESS | Render JobID: %s | Wrapper JobID: %s",
            dependency_job_id,
            wrapper_job_id,
        )
    return wrapper_job_id


def submit_cache_to_deadline(
    node,
    dependent_job_id=None,
    batch_name=None,
    machine_limit=None,
    machine_list=None,
    machine_list_is_deny=None,
):
    """
    Submit this FileCache node to Deadline.
    Supports Batch grouping and Job dependencies.
    """
    version = active_version(node)
    ensure_deadline_machine_list_parms(node)
    save_ayon_context_for_node(node, version)
    json_path = ayon_context_json_path(node, version)
    hip_path = save_hip_to_version_dir(node, version)

    f1, f2 = frame_range(node)
    
    # Priority and Chunk Logic
    user_priority = node.parm("priority").eval() if node.parm("priority") else 50
    user_chunks = node.parm("chunks").eval() if node.parm("chunks") else 10
    single_machine = node.parm("single_machine").eval() if node.parm("single_machine") else False
    
    chunk_size = (f2 - f1) + 1 if single_machine else user_chunks

    # Use the provided batch_name (from Farmer) or create a default
    if not batch_name:
        batch_name = _build_default_batch_name(node, version)

    if machine_limit is None and node.parm("machine_limit"):
        machine_limit = node.parm("machine_limit").eval()

    normalized_machine_limit = None
    if machine_limit is not None:
        try:
            normalized_machine_limit = int(machine_limit)
        except (TypeError, ValueError):
            normalized_machine_limit = None
        if normalized_machine_limit is not None and normalized_machine_limit < 1:
            normalized_machine_limit = None

    if machine_list is None:
        machine_list_parm = node.parm(_DEADLINE_MACHINE_LIST_PARM)
        machine_list = machine_list_parm.evalAsString() if machine_list_parm else ""
    normalized_machine_list = _normalized_machine_list(machine_list)

    if machine_list_is_deny is None:
        denylist_parm = node.parm(_DEADLINE_MACHINE_DENYLIST_PARM)
        machine_list_is_deny = bool(denylist_parm.eval()) if denylist_parm else False

    # --- JOB INFO ---
    job_info = {
        "Plugin": "Houdini",
        "BatchName": batch_name, # Grouping key
        "Name": f"{node.name()} | v{version:03d}", # Sub-branch name
        "Frames": f"{f1}-{f2}",
        "ChunkSize": chunk_size,
        "Pool": "houdini",
        "Priority": user_priority,
        "EnvironmentKeyValue0": f"AYON_CONTEXT_JSON={json_path}",
        "EnvironmentKeyValue1": f"BMFX_FROZEN_VERSION={version}",
    }

    if dependent_job_id:
        job_info["JobDependency0"] = dependent_job_id
    if normalized_machine_limit:
        job_info["MachineLimit"] = normalized_machine_limit
    if normalized_machine_list:
        key = "Blacklist" if machine_list_is_deny else "Whitelist"
        job_info[key] = normalized_machine_list

    # --- PLUGIN INFO ---
    rop = get_active_rop(node)
    plugin_info = {
        "SceneFile": hip_path,
        "OutputDriver": rop.path(),
        "Version": "21.0",
        "Build": "64bit",
        "IgnoreSceneFileFrameRange": True,
        "StartFrame": f1,
        "EndFrame": f2,
    }

    new_job_id = _submit_deadline_files(job_info, plugin_info)
    if not new_job_id:
        return None

    wrapper_job_id = _submit_master_wrapper_job(
        node,
        version,
        f1,
        f2,
        batch_name,
        user_priority,
        new_job_id,
        json_path,
        machine_limit=normalized_machine_limit,
        machine_list=normalized_machine_list,
        machine_list_is_deny=machine_list_is_deny,
    )

    node.parm("version").set(str(version))
    update_cache_status(node)
    return wrapper_job_id or new_job_id


def on_node_created(node):
    """
    Initialize the LOP FileCache node with Deadline machine list parameters.
    Call this from the node's OnCreated callback.
    """
    ensure_deadline_machine_list_parms(node)


def update_cache_status(node):
    ensure_deadline_machine_list_parms(node)
    node.setGenericFlag(hou.nodeFlag.DisplayComment, False)
    versions = existing_versions(node)
    mode_parm = node.parm("mode")
    if mode_parm and mode_parm.evalAsString() == "live":
        set_status(node, "LIVE")
        return

    if not versions:
        set_status(node, "LIVE")
        return

    latest = versions[-1]
    if node.parm("read_latest") and node.parm("read_latest").eval():
        selected = latest
    else:
        p_version = node.parm("version")
        try:
            selected = int(p_version.evalAsString().lstrip("v"))
        except (ValueError, AttributeError):
            set_status(node, "LIVE")
            return

    missing = missing_frames_in_range(node, selected)

    if missing:
        set_status(node, "MISSING CACHE")
        preview = ", ".join(map(str, missing[:3]))
        suffix = "..." if len(missing) > 3 else ""
        node.setComment(f"! MISSING FRAMES: {preview}{suffix}")
    elif selected < latest:
        set_status(node, "OLDER CACHE")
    else:
        set_status(node, "CACHED")

    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
