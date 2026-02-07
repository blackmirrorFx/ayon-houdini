import hou
import logging
import os
import re
import shutil
from datetime import datetime
from ayon_core.pipeline import get_current_context
import json
import tempfile
import subprocess

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

    print("[AYON] Context JSON saved")
    print(f"       {path}")

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

def cache_file(node, version=None):
    """
    Resolves the final file path. Handles the Frameless toggle.
    """
    if version is None:
        version = active_version(node)
    ext = node.parm("file_format").evalAsString()
    base_dir = version_dir(node, version)
    if node.parm("frameless").eval():
        filename = f"main.{ext}"
    else:
        filename = f"main.$F4.{ext}"
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
    ver_dir = version_dir(node, version)
    if ext == "abc":
        return os.path.join(ver_dir, "main.abc")
    if node.parm("frameless").eval() and node.parm("frame_range").evalAsString() == "off":
        filename = f"main.{ext}"
    else:
        filename = f"main.$F4.{ext}"
    return os.path.join(ver_dir, filename)

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
    fmt = node.parm("file_format").evalAsString()
    
    # Check if we are in a LOP (USD) context
    if fmt in ["usd", "usdc", "usda"]:
        # Find the USD export node inside your HDA's ropnet
        rop = node.node("ropnet/usd")
    elif fmt == "abc":
        rop = node.node("ropnet/alembic")
    else:
        rop = node.node("ropnet/geometry")
        
    if not rop:
        raise RuntimeError(f"Required ROP not found inside {node.name()}")
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
    original_path = hou.hipFile.path()
    if not original_path or original_path == "untitled.hip":
        raise RuntimeError("Please save the HIP file before caching.")
    version_path = version_dir(node, version, create=True)
    hip_dir = os.path.join(version_path, "hip")
    os.makedirs(hip_dir, exist_ok=True)
    dst = os.path.join(hip_dir, "source.hip")
    hou.hipFile.save(file_name=dst)
    hou.hipFile.setName(original_path)
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
        return cache_file(node, v)
    except:
        return ""

def frozen_version(node):
    p = node.parm("write_version")
    if p:
        val = p.evalAsString()
        if val:
            return int(val)
    return None

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
        rop.render()
        if "BMFX_FROZEN_VERSION" not in os.environ:
            node.parm("version").set(str(_FROZEN_WRITE_VERSION))
        set_status(node, "CACHED")
        update_cache_status(node)

    finally:
        #  Always clear after render
        _FROZEN_WRITE_VERSION = None

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

        return cache_file(node, v)
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

    # If it's an Alembic or Frameless, check for a single file
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

def submit_cache_to_deadline(node, dependent_job_id=None, batch_name=None):
    """
    Submit this FileCache node to Deadline.
    Supports Batch grouping and Job dependencies.
    """
    version = active_version(node)
    _LOGGER.info("=" * 60)
    _LOGGER.info(
        "DEADLINE SUBMIT | Node: %s | Version: v%03d",
        node.path(),
        version,
    )

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

    _LOGGER.info("BatchName: %s", batch_name)
    if dependent_job_id:
        _LOGGER.info("Dependency JobID: %s", dependent_job_id)
    _LOGGER.info(
        "Frames: %s-%s | ChunkSize: %s | Pool: houdini | Priority: %s | SingleMachine: %s",
        f1,
        f2,
        chunk_size,
        user_priority,
        single_machine,
    )
    _LOGGER.info("Context JSON: %s", json_path)
    _LOGGER.info("HIP saved: %s", hip_path)

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

    # --- PLUGIN INFO ---
    rop = get_active_rop(node)
    _LOGGER.info("OutputDriver: %s", rop.path())
    plugin_info = {
        "SceneFile": hip_path,
        "OutputDriver": rop.path(),
        "Version": "21.0",
        "Build": "64bit",
        "IgnoreSceneFileFrameRange": True,
        "StartFrame": f1,
        "EndFrame": f2,
    }

    # Submission logic using subprocess to capture JobID
    job_file = tempfile.NamedTemporaryFile(delete=False, suffix="_job.info")
    plugin_file = tempfile.NamedTemporaryFile(delete=False, suffix="_plugin.info")

    try:
        with open(job_file.name, 'wb') as f:
            for k, v in job_info.items():
                f.write(f"{k}={v}\n".encode())

        with open(plugin_file.name, 'wb') as f:
            for k, v in plugin_info.items():
                f.write(f"{k}={v}\n".encode())

        cmd = ["deadlinecommand", job_file.name, plugin_file.name]
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        _LOGGER.debug("Deadline output:\n%s", result.strip())
        
        match = re.search(r"JobID=([\w\d]+)", result)
        if match:
            new_job_id = match.group(1)
            node.parm("version").set(str(version))
            update_cache_status(node)
            _LOGGER.info("DEADLINE SUCCESS | JobID: %s", new_job_id)
            return new_job_id 

        _LOGGER.error(
            "DEADLINE ERROR | Job submitted but JobID not found in output:\n%s",
            result.strip(),
        )
        return None
            
    except subprocess.CalledProcessError as e:
        _LOGGER.error(
            "DEADLINE ERROR | Submission failed:\n%s",
            e.output.decode().strip(),
        )
        return None
    finally:
        for f in [job_file.name, plugin_file.name]:
            if os.path.exists(f): os.remove(f)

def update_cache_status(node):
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
