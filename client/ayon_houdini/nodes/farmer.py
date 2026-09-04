import logging
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

import hou
from ayon_core.pipeline import get_current_context

try:
    from ayon_houdini.api.file_permissions import read_only_source
except ImportError:  # Direct source-tree tests and embedded HDA loading.
    import importlib.util

    _permissions_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "api",
        "file_permissions.py",
    )
    _permissions_spec = importlib.util.spec_from_file_location(
        "ayon_houdini_file_permissions", _permissions_path
    )
    _permissions_module = importlib.util.module_from_spec(_permissions_spec)
    _permissions_spec.loader.exec_module(_permissions_module)
    read_only_source = _permissions_module.read_only_source


_LOGGER = logging.getLogger("BMFX.Farmer")
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

_DEADLINE_MACHINE_LIST_PARM = "machine_list"
_DEADLINE_MACHINE_DENYLIST_PARM = "machine_list_is_deny"
_DISCORD_NOTIFY_PARM = "discord_notify"
_LEGACY_EMAIL_PARMS = ("email_farm_report", "email_report_recipients")

_OUTPUT_PARM_NAMES = (
    "vm_picture",
    "picture",
    "outputimage",
    "sopoutput",
    "lopoutput",
    "filename",
    "file",
    "output",
)
_CONTROL_DRIVER_TYPES = {
    "batch",
    "fetch",
    "merge",
    "null",
    "ropnet",
    "switch",
}


def _append_parm_template(group, template, after=None):
    """Append a spare parameter, optionally beside an existing parameter."""
    try:
        if after and group.find(after):
            group.insertAfter(after, template)
        else:
            group.append(template)
    except hou.OperationFailed:
        group.append(template)


def ensure_deadline_machine_list_parms(node):
    """Add universal Deadline controls missing from older Farmer HDAs."""

    try:
        group = node.parmTemplateGroup()
    except (hou.PermissionError, hou.OperationFailed) as e:
        _LOGGER.error("Cannot access parameter template group: %s", str(e))
        return

    changed = False

    # Migrate existing Farmer nodes away from the retired email controls. The
    # Discord bot token and user ID are never stored in the HDA or HIP file.
    for legacy_name in _LEGACY_EMAIL_PARMS:
        if group.find(legacy_name):
            try:
                group.remove(legacy_name)
                changed = True
            except hou.OperationFailed:
                _LOGGER.warning(
                    "Could not remove legacy Farmer parameter %s",
                    legacy_name,
                )

    if not group.find(_DEADLINE_MACHINE_DENYLIST_PARM):
        deny_template = hou.ToggleParmTemplate(
            _DEADLINE_MACHINE_DENYLIST_PARM,
            "Machine List Is Deny List",
            default_value=False,
        )
        try:
            if group.find("single_machine"):
                group.insertAfter("single_machine", deny_template)
                _LOGGER.info("Inserted deny list parameter after single_machine")
            else:
                group.append(deny_template)
                _LOGGER.info("Appended deny list parameter")
        except hou.OperationFailed as e:
            _LOGGER.error("Failed to insert deny list: %s", str(e))
            group.append(deny_template)
        changed = True

    universal_templates = (
        (
            "priority",
            hou.IntParmTemplate(
                "priority", "Priority", 1, default_value=(50,), min=0, max=100
            ),
            _DEADLINE_MACHINE_LIST_PARM,
        ),
        (
            "pool",
            hou.StringParmTemplate("pool", "Pool", 1, default_value=("houdini",)),
            "priority",
        ),
        (
            _DISCORD_NOTIFY_PARM,
            hou.ToggleParmTemplate(
                _DISCORD_NOTIFY_PARM,
                "Discord Personal Message",
                default_value=True,
            ),
            "pool",
        ),
    )
    for parm_name, template, after in universal_templates:
        if group.find(parm_name):
            continue
        _append_parm_template(group, template, after=after)
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
                "import importlib, ayon_houdini.nodes.farmer as farmer; "
                "farmer = importlib.reload(farmer); "
                "farmer.pick_deadline_machine_list(kwargs)"
            ),
            "script_action_icon": "BUTTONS_gear",
            "script_action_help": "Select Deadline machines",
            "script_action_language": "python",
        })
        
        # Always append at the end since this is easier and more reliable
        group.append(list_template)
        _LOGGER.info("Appended machine list parameter")
        changed = True

    if changed:
        try:
            node.setParmTemplateGroup(group)
            _LOGGER.info("Successfully updated parameter template group on %s", node.path())
        except (hou.PermissionError, hou.OperationFailed) as e:
            _LOGGER.error("Failed to set parameter template group: %s", str(e))
            return


def _query_deadline_machine_names():
    """Query available Deadline worker names."""
    for command in ("-GetWorkerNames", "-GetSlaveNames"):
        try:
            output = subprocess.check_output(
                ["deadlinecommand", command],
                stderr=subprocess.STDOUT,
            ).decode()
        except (subprocess.CalledProcessError, OSError):
            continue
        names = []
        for part in re.split(r"[\r\n,]+", output or ""):
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
        deduped = list(dict.fromkeys(names))
        if deduped:
            return deduped
    return []


def _normalized_machine_list(raw_value):
    """Normalize machine list string."""
    if raw_value is None:
        return ""
    tokens = [part.strip() for part in re.split(r"[,\s;]+", str(raw_value)) if part.strip()]
    if not tokens:
        return ""
    deduped = list(dict.fromkeys(tokens))
    return ",".join(deduped)


def pick_deadline_machine_list(kwargs):
    """Show UI to pick Deadline machines for the farmer node."""
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


def on_node_created(node):
    """
    Initialize a Farmer node with Deadline machine list parameters.
    Call this from the Farmer HDA's OnCreated callback.
    """
    try:
        ensure_deadline_machine_list_parms(node)
    except Exception as e:
        _LOGGER.error("Failed to add machine list parameters: %s", str(e))


def _get_hda_module(node):
    try:
        return node.hdaModule()
    except (AttributeError, hou.PermissionError):
        return None


def _is_generic_rop_or_cache(node):
    """Return whether Deadline's Houdini plugin can render this node directly."""
    if _has_submit_hook(node):
        return False

    try:
        base_type = node.type().name().split("::", 1)[0].lower()
        if base_type in _CONTROL_DRIVER_TYPES or "farmer" in base_type:
            return False
        category = node.type().category().name()
        # Houdini calls this category "Driver"; some test doubles and older
        # APIs expose it as "ROP".
        return category in {"Driver", "ROP"}
    except Exception:
        return False


def _has_submit_hook(node):
    module = _get_hda_module(node)
    return bool(
        module
        and (
            hasattr(module, "submit_wedges_to_deadline")
            or hasattr(module, "submit_cache_to_deadline")
        )
    )


def _get_fetch_source(node):
    try:
        if node.type().name().split("::", 1)[0] != "fetch":
            return None
    except Exception:
        return None

    source_parm = node.parm("source")
    source_path = source_parm.evalAsString().strip() if source_parm else ""
    if not source_path:
        return None
    if source_path.startswith("op:"):
        source_path = source_path[3:]
    try:
        return node.node(source_path) or hou.node(source_path)
    except AttributeError:
        return hou.node(source_path)


def _is_submittable_node(node):
    """Check if node can be submitted (has submit hook or is generic ROP)."""
    return _has_submit_hook(node) or _is_generic_rop_or_cache(node)


def _upstream_nodes(node):
    nodes = [item for item in node.inputs() if item]
    fetch_source = _get_fetch_source(node)
    if fetch_source and fetch_source not in nodes:
        nodes.append(fetch_source)
    return nodes


def _collect_upstream_submit_graph(farmer_node):
    """Return topologically sorted targets and their nearest job parents."""
    collected = []
    visited = set()

    def _walk(node):
        if not node:
            return

        node_path = node.path()
        if node_path in visited:
            return
        visited.add(node_path)

        for upstream in _upstream_nodes(node):
            _walk(upstream)

        if _is_submittable_node(node):
            collected.append(node)

    for upstream in farmer_node.inputs():
        _walk(upstream)

    targets_by_path = {node.path(): node for node in collected}
    nearest_cache = {}
    resolving = set()

    def _nearest_targets(node):
        path = node.path()
        if path in nearest_cache:
            return nearest_cache[path]
        if path in resolving:
            _LOGGER.warning("Cycle detected while inspecting Farmer inputs at %s", path)
            return []
        resolving.add(path)
        found = []
        for upstream in _upstream_nodes(node):
            upstream_path = upstream.path()
            if upstream_path in targets_by_path:
                found.append(upstream_path)
            else:
                found.extend(_nearest_targets(upstream))
        nearest_cache[path] = list(dict.fromkeys(found))
        resolving.discard(path)
        return nearest_cache[path]

    dependencies = {
        node.path(): [
            path for path in _nearest_targets(node) if path != node.path()
        ]
        for node in collected
    }
    return collected, dependencies


def _collect_upstream_submit_nodes(farmer_node):
    """Backward-compatible node-only collector."""
    return _collect_upstream_submit_graph(farmer_node)[0]


def _job_id_list(result):
    if not result:
        return []
    if isinstance(result, (list, tuple, set)):
        return [str(item) for item in result if item]
    return [str(result)]


def _submit_cache_job(
    module,
    node,
    batch_name,
    dependency_ids,
    options,
    scene_dependencies=None,
    dependency_manifest_path=None,
):
    """Call a specialized cache/wedge hook, retaining legacy compatibility."""
    dependency_ids = _job_id_list(dependency_ids)
    submitter = getattr(module, "submit_wedges_to_deadline", None)
    if submitter is None:
        submitter = module.submit_cache_to_deadline

    kwargs = {
        "batch_name": batch_name,
        "machine_limit": options["machine_limit"],
        "machine_list": options["machine_list"],
        "machine_list_is_deny": options["machine_list_is_deny"],
        "scene_dependencies": scene_dependencies,
        "dependency_manifest_path": dependency_manifest_path,
    }
    if dependency_ids:
        # In-repository hooks accept a comma-separated dependency value via
        # JobDependencies. Legacy hooks still receive their usual first ID.
        kwargs["dependent_job_id"] = ",".join(dependency_ids)
    try:
        signature = inspect.signature(submitter)
    except (TypeError, ValueError):
        # Python functions exposed through some HDA module proxies do not
        # publish a signature. The in-repository hooks accept these options.
        return _job_id_list(submitter(node, **kwargs))

    accepts_kwargs = any(
        parm.kind == inspect.Parameter.VAR_KEYWORD
        for parm in signature.parameters.values()
    )
    if accepts_kwargs:
        supported = kwargs
    else:
        supported = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters
        }
    return _job_id_list(submitter(node, **supported))


def _get_ayon_launcher_env():
    """Get all AYON/OpenPype environment variables."""
    return {
        k: v for k, v in os.environ.items()
        if k.startswith(("AYON_", "OPENPYPE_", "AVALON_"))
    }


def _get_all_houdini_vars():
    """Get all Houdini environment variables."""
    raw = hou.hscript("set")[0]
    data = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = hou.expandString(v.strip())
    return data


def _filter_custom_houdini_vars(vars_dict):
    """Filter Houdini vars to keep only relevant ones."""
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


def _get_pipeline_env():
    """Collect critical pipeline paths."""
    keys = (
        "PYTHONPATH",
        "HOUDINI_PATH",
        "HOUDINI_OTLSCAN_PATH",
        # Carry Discord notification configuration with the private AYON
        # context so Deadline jobs receive it automatically.
        "BMFX_DISCORD_USER_ID",
        "BMFX_DISCORD_BOT_TOKEN",
        "BMFX_DISCORD_API_BASE",
    )
    env = {}
    for k in keys:
        val = os.environ.get(k)
        if val:
            env[k] = val
    return env


def _get_farmer_submission_root(farmer_node_name, job_prefix=""):
    """Constructs the submission directory for farmer in AYON structure.
    
    Returns: <project_root>/<folder_path>/houdini/<task>/.farm/<farmer_node>_<jobprefix>_<HH_MM_SS>/
    """
    ctx = get_current_context() or {}
    fold = (ctx.get("folder_path") or "unknown_folder").strip("/")
    task = ctx.get("task_name", "unknown_task")
    
    # Get project root (with fallbacks like filecache.py)
    root = os.environ.get("JOB")
    if not root or root == "$JOB":
        root = hou.text.expandString("$JOB")
    if not root or root == "$JOB":
        root = hou.expandString("$HIP")
    
    # Create timestamp in HH_MM_SS format
    timestamp = datetime.now().strftime("%H_%M_%S")
    
    # Build folder name with job prefix if provided
    if job_prefix.strip():
        folder_name = f"{farmer_node_name}_{job_prefix}_{timestamp}"
    else:
        folder_name = f"{farmer_node_name}_{timestamp}"
    
    submission_root = os.path.join(root, fold, "houdini", task, ".farm", folder_name)
    os.makedirs(submission_root, exist_ok=True)
    return submission_root


def _save_hip_file_for_submission(submission_root):
    """Save current hip file to submission directory for Deadline."""
    try:
        hip_path = os.path.join(submission_root, "source.hip")
        
        hscript_dst = hip_path.replace("\\", "/").replace('"', '\\"')
        with read_only_source(hip_path):
            hou.hscript(f'mwrite -n "{hscript_dst}"')
        _LOGGER.info("Saved hip file for submission: %s", hip_path)
        return hip_path
    except (hou.OperationFailed, Exception) as exc:
        _LOGGER.error("Failed to save hip file: %s", str(exc))
        return None


def _save_ayon_context_to_file(
    submission_root, scene_dependencies=None, dependency_manifest_path=None
):
    """Save AYON context to a JSON file in the submission directory."""
    try:
        path = os.path.join(submission_root, ".ayon_vars.json")
        data = {
            "launcher_env": _get_ayon_launcher_env(),
            "houdini_vars": _filter_custom_houdini_vars(
                _get_all_houdini_vars()
            ),
            "pipeline_env": _get_pipeline_env(),
            "bmfx_scene_dependencies": scene_dependencies or {},
            "bmfx_dependency_manifest": dependency_manifest_path or "",
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
        _LOGGER.info("Saved AYON context: %s", path)
        return path
    except Exception as exc:
        _LOGGER.error("Failed to save AYON context: %s", str(exc))
        return None


def _save_dependency_manifest(submission_root, farmer_node, context):
    """Freeze live scene provenance before any farm snapshot is submitted."""
    try:
        from ayon_houdini.nodes.scene_dependencies import (
            collect_scene_dependencies,
        )
    except ImportError:
        from nodes.scene_dependencies import collect_scene_dependencies

    stage = None
    candidates = [farmer_node] + list(farmer_node.inputs() or [])
    for candidate in candidates:
        stage_method = getattr(candidate, "stage", None)
        if not callable(stage_method):
            continue
        try:
            stage = stage_method()
        except Exception:
            stage = None
        if stage is not None:
            break
    dependencies = collect_scene_dependencies(
        project_name=context.get("project_name") or "",
        stage=stage,
        hou_module=hou,
    )
    path = os.path.join(submission_root, "scene_dependencies.json")
    with open(path, "w") as stream:
        json.dump(dependencies, stream, indent=2, sort_keys=True)
    _LOGGER.info(
        "FARMER DEPENDENCIES | %d resources | %d AYON versions | %s",
        len(dependencies.get("records") or []),
        len(dependencies.get("input_version_ids") or []),
        path,
    )
    return dependencies, path


def _parm_value(node, names, default=None, string=False):
    for name in names:
        parm = node.parm(name)
        if parm is None:
            continue
        try:
            return parm.evalAsString() if string else parm.eval()
        except Exception:
            continue
    return default


def _node_frame_range(node):
    mode = str(_parm_value(node, ("trange", "frame_range"), "", True)).lower()
    if mode in {"off", "single", "current", "0"}:
        frame = int(hou.frame())
        return frame, frame, 1
    start = int(_parm_value(node, ("f1", "fx", "frame_start", "start_frame"), hou.frame()))
    end = int(_parm_value(node, ("f2", "fy", "frame_end", "end_frame"), start))
    step = int(_parm_value(node, ("f3", "fz", "frame_step", "step"), 1) or 1)
    if end < start:
        start, end = end, start
    return start, end, max(1, abs(step))


def _get_frame_spec(rop_node, farmer_node):
    """Return Deadline frames and bounds for every Farmer range mode."""
    mode = int(_parm_value(farmer_node, ("validfr",), 0) or 0)
    if mode == 2:
        frames = str(_parm_value(farmer_node, ("frlist",), "", True)).strip()
        if not frames:
            raise ValueError("Custom frame list is empty.")
        frames = re.sub(r"\s+", "", frames)
        frame_token = r"-?\d+(?:--?\d+(?:[xX]\d+)?)?"
        if not re.fullmatch(r"{}(?:,{})*".format(frame_token, frame_token), frames):
            raise ValueError(
                "Use Deadline syntax such as 1001-1100,1200 or 1001-1100x2."
            )
        # A range dash is a separator, while a leading/double dash denotes a
        # negative frame. Exclude the increment in tokens such as ``x2``.
        numbers = [
            int(value)
            for value in re.findall(r"(?<![\dxX])-?\d+", frames)
        ]
        if not numbers:
            raise ValueError("Custom frame list contains no frame numbers.")
        return frames, min(numbers), max(numbers), 1
    if mode == 1:
        frame_tuple = farmer_node.parmTuple("f")
        if frame_tuple and len(frame_tuple) >= 3:
            values = frame_tuple.eval()
            start, end, step = int(values[0]), int(values[1]), int(values[2] or 1)
        else:
            start = int(_parm_value(farmer_node, ("fx",), hou.frame()))
            end = int(_parm_value(farmer_node, ("fy",), start))
            step = int(_parm_value(farmer_node, ("fz",), 1) or 1)
        if end < start:
            start, end = end, start
        step = max(1, abs(step))
    else:
        start, end, step = _node_frame_range(rop_node)
    frames = "{}-{}".format(start, end)
    if step > 1:
        frames += "x{}".format(step)
    return frames, start, end, step


def _get_frame_range(rop_node, farmer_node):
    """Backward-compatible frame-bound helper."""
    _frames, start, end, _step = _get_frame_spec(rop_node, farmer_node)
    return start, end


def _farmer_options(farmer_node):
    machine_limit = int(_parm_value(farmer_node, ("machine_limit",), 0) or 0)
    return {
        "priority": max(0, min(100, int(_parm_value(farmer_node, ("priority",), 50)))),
        "chunk_size": max(1, int(_parm_value(
            farmer_node, ("chunksize", "chunk_size"), 10
        ) or 1)),
        "single_machine": bool(_parm_value(farmer_node, ("single_machine",), False)),
        "machine_limit": machine_limit if machine_limit > 0 else None,
        "machine_list": _normalized_machine_list(_parm_value(
            farmer_node, (_DEADLINE_MACHINE_LIST_PARM,), "", True
        )),
        "machine_list_is_deny": bool(_parm_value(
            farmer_node, (_DEADLINE_MACHINE_DENYLIST_PARM,), False
        )),
        "pool": str(_parm_value(farmer_node, ("pool",), "houdini", True) or "houdini"),
        "discord_notify": bool(_parm_value(
            farmer_node, (_DISCORD_NOTIFY_PARM,), True
        )),
    }


def _node_messages(node, method_name):
    method = getattr(node, method_name, None)
    if not method:
        return []
    try:
        return [str(message) for message in (method() or []) if message]
    except Exception:
        return []


def _preflight(farmer_node, nodes, dependencies):
    """Collect blocking errors and warnings before any jobs are submitted."""
    errors = []
    warnings = []
    deadline_command = shutil.which("deadlinecommand")
    if not deadline_command:
        errors.append("deadlinecommand was not found in PATH.")
    else:
        try:
            repository_root = subprocess.check_output(
                [deadline_command, "-GetRepositoryRoot"],
                stderr=subprocess.STDOUT,
                timeout=15,
            ).decode(errors="replace").strip()
            if not repository_root or repository_root.lower().startswith("error"):
                errors.append("Deadline repository connection failed: {}".format(
                    repository_root or "empty response"
                ))
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            output = getattr(exc, "output", b"") or b""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            details = str(output).strip() or str(exc)
            errors.append("Deadline repository connection failed: {}".format(details))
    hip_path = hou.hipFile.path()
    if not hip_path or os.path.basename(hip_path).lower() == "untitled.hip":
        errors.append("Save the Houdini scene before submitting.")
    if not nodes:
        errors.append("No supported cache, wedge, or ROP nodes were found upstream.")
    try:
        _farmer_options(farmer_node)
    except (TypeError, ValueError) as exc:
        errors.append("Invalid Farmer setting: {}".format(exc))

    for node in nodes:
        path = node.path()
        try:
            if node.isBypassed():
                errors.append("{} is bypassed.".format(path))
        except Exception:
            pass
        errors.extend("{}: {}".format(path, msg) for msg in _node_messages(node, "errors"))
        warnings.extend("{}: {}".format(path, msg) for msg in _node_messages(node, "warnings"))
        try:
            _get_frame_spec(node, farmer_node)
        except (TypeError, ValueError) as exc:
            errors.append("{} has an invalid frame range: {}".format(path, exc))
        if _is_generic_rop_or_cache(node):
            output_parm = next(
                (node.parm(name) for name in _OUTPUT_PARM_NAMES if node.parm(name)),
                None,
            )
            if output_parm:
                try:
                    if not output_parm.evalAsString().strip():
                        errors.append("{} has an empty output path.".format(path))
                except Exception:
                    warnings.append("Could not evaluate the output path on {}.".format(path))

    graph_lines = []
    for node in nodes:
        parent_paths = dependencies.get(node.path(), [])
        suffix = " <- {}".format(", ".join(parent_paths)) if parent_paths else ""
        graph_lines.append("{}{}".format(node.path(), suffix))
    return errors, warnings, graph_lines


def _submit_generic_rop_to_deadline(
    node,
    batch_name,
    farmer_node,
    hip_path=None,
    ayon_context_json=None,
    dependency_ids=None,
    options=None,
    dependency_manifest_path=None,
):
    """Submit a generic ROP or cache node to Deadline with AYON context."""
    try:
        options = options or _farmer_options(farmer_node)
        if not hip_path or not ayon_context_json:
            job_prefix = str(_parm_value(
                farmer_node, ("jobprefix",), "", True
            )).strip()
            submission_root = _get_farmer_submission_root(
                farmer_node.name(), job_prefix
            )
            hip_path = hip_path or _save_hip_file_for_submission(submission_root)
            ayon_context_json = ayon_context_json or _save_ayon_context_to_file(
                submission_root
            )
        if not hip_path or not ayon_context_json:
            return None

        frames, f1, f2, step = _get_frame_spec(node, farmer_node)
        node_name = node.name()
        job_prefix = str(_parm_value(farmer_node, ("jobprefix",), "", True)).strip()
        job_name = "{} | {}".format(job_prefix, node_name) if job_prefix else node_name
        chunk_size = options["chunk_size"]
        if options["single_machine"]:
            chunk_size = max(1, int((f2 - f1) / float(step)) + 1)

        job_info = {
            "Plugin": "Houdini",
            "BatchName": batch_name,
            "Name": job_name,
            "Frames": frames,
            "ChunkSize": chunk_size,
            "Pool": options["pool"],
            "Priority": options["priority"],
            "EnvironmentKeyValue0": f"AYON_CONTEXT_JSON={ayon_context_json}",
            "EnvironmentKeyValue1": "AYON_CONTEXT_INJECTED=1",
        }
        if dependency_manifest_path:
            job_info["EnvironmentKeyValue2"] = (
                "BMFX_SCENE_DEPENDENCIES_JSON={}".format(
                    dependency_manifest_path
                )
            )

        dependency_ids = _job_id_list(dependency_ids)
        if dependency_ids:
            job_info["JobDependencies"] = ",".join(dependency_ids)
        if options["machine_limit"]:
            job_info["MachineLimit"] = options["machine_limit"]
        if options["machine_list"]:
            key = "Blacklist" if options["machine_list_is_deny"] else "Whitelist"
            job_info[key] = options["machine_list"]

        version = hou.applicationVersion()
        plugin_info = {
            "SceneFile": hip_path,
            "OutputDriver": node.path(),
            "Version": "{}.{}".format(version[0], version[1]),
            "Build": "64bit",
            "IgnoreSceneFileFrameRange": True,
            "IgnoreInputs": True,
            "StartFrame": f1,
            "EndFrame": f2,
        }
        if step > 1:
            plugin_info["FrameStep"] = step

        # Write temp files
        job_file = tempfile.NamedTemporaryFile(
            mode='w',
            delete=False,
            suffix='_job.info',
        )
        plugin_file = tempfile.NamedTemporaryFile(
            mode='w',
            delete=False,
            suffix='_plugin.info',
        )
        
        try:
            for key, value in job_info.items():
                job_file.write(f"{key}={value}\n")
            job_file.close()

            for key, value in plugin_info.items():
                plugin_file.write(f"{key}={value}\n")
            plugin_file.close()

            cmd = ["deadlinecommand", job_file.name, plugin_file.name]
            result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
            _LOGGER.debug("Deadline output:\n%s", result.strip())
            
            # Extract JobID
            match = re.search(r"JobID=([\w\d]+)", result)
            if match:
                job_id = match.group(1)
                _LOGGER.info("Generic ROP submitted | Node: %s | JobID: %s", node.path(), job_id)
                return [job_id]
            
            _LOGGER.error(
                "Deadline submission failed - JobID not found in output:\n%s",
                result.strip(),
            )
            return None
            
        except subprocess.CalledProcessError as e:
            _LOGGER.error(
                "Deadline submission failed:\n%s",
                e.output.decode().strip(),
            )
            return None
        finally:
            # Cleanup temp files
            try:
                os.unlink(job_file.name)
                os.unlink(plugin_file.name)
            except OSError:
                pass
    
    except Exception as e:
        _LOGGER.error("Error submitting generic ROP: %s", str(e))
        return None


def submit_farmer_to_deadline(farmer_node):
    """Preflight and submit any connected caches, wedges, and native ROPs."""
    ensure_deadline_machine_list_parms(farmer_node)
    nodes, dependencies = _collect_upstream_submit_graph(farmer_node)
    errors, warnings, graph_lines = _preflight(farmer_node, nodes, dependencies)

    if errors:
        message = "Preflight failed:\n\n- " + "\n- ".join(errors)
        if warnings:
            message += "\n\nWarnings:\n- " + "\n- ".join(warnings)
        hou.ui.displayMessage(message, title="Farmer Preflight")
        return []

    message = "Ready to submit {} node(s):\n\n{}".format(
        len(nodes), "\n".join(graph_lines)
    )
    if warnings:
        message += "\n\nWarnings:\n- " + "\n- ".join(warnings)
    try:
        if hou.isUIAvailable():
            choice = hou.ui.displayMessage(
                message,
                buttons=("Submit", "Cancel"),
                default_choice=0,
                close_choice=1,
                title="Farmer Preflight",
            )
            if choice != 0:
                return []
    except AttributeError:
        pass

    context = get_current_context() or {}
    project = context.get("project_name", "project")
    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    batch_name = "Farm | {} | {} | {}".format(project, farmer_node.name(), submitted_at)
    options = _farmer_options(farmer_node)

    job_prefix = str(_parm_value(
        farmer_node, ("jobprefix",), "", True
    )).strip()
    submission_root = _get_farmer_submission_root(
        farmer_node.name(), job_prefix
    )
    try:
        scene_dependencies, dependency_manifest_path = (
            _save_dependency_manifest(submission_root, farmer_node, context)
        )
    except Exception as exc:
        _LOGGER.exception("Could not collect Farmer scene dependencies")
        hou.ui.displayMessage(
            "Could not collect scene dependencies:\n{}".format(exc),
            title="Farmer Error",
        )
        return []
    hip_path = None
    context_path = None
    if any(_is_generic_rop_or_cache(node) for node in nodes):
        hip_path = _save_hip_file_for_submission(submission_root)
        context_path = _save_ayon_context_to_file(
            submission_root,
            scene_dependencies=scene_dependencies,
            dependency_manifest_path=dependency_manifest_path,
        )
        if not hip_path or not context_path:
            hou.ui.displayMessage(
                "Could not create the Farmer HIP snapshot or AYON context file.",
                title="Farmer Error",
            )
            return []

    submitted_job_ids = []
    job_ids_by_node = {}
    failed_nodes = []
    for node in nodes:
        parent_paths = dependencies.get(node.path(), [])
        failed_parents = [path for path in parent_paths if not job_ids_by_node.get(path)]
        if failed_parents:
            failed_nodes.append(
                "{} (dependency failed: {})".format(node.path(), ", ".join(failed_parents))
            )
            job_ids_by_node[node.path()] = []
            continue
        dependency_ids = []
        for parent_path in parent_paths:
            dependency_ids.extend(job_ids_by_node.get(parent_path, []))

        try:
            module = _get_hda_module(node)
            if _has_submit_hook(node):
                job_ids = _submit_cache_job(
                    module,
                    node,
                    batch_name=batch_name,
                    dependency_ids=dependency_ids,
                    options=options,
                    scene_dependencies=scene_dependencies,
                    dependency_manifest_path=dependency_manifest_path,
                )
            else:
                job_ids = _submit_generic_rop_to_deadline(
                    node,
                    batch_name=batch_name,
                    farmer_node=farmer_node,
                    hip_path=hip_path,
                    ayon_context_json=context_path,
                    dependency_ids=dependency_ids,
                    options=options,
                    dependency_manifest_path=dependency_manifest_path,
                )
        except Exception as exc:
            _LOGGER.exception("Submission failed for %s", node.path())
            job_ids = []
            failed_nodes.append("{} ({})".format(node.path(), exc))

        job_ids = _job_id_list(job_ids)
        job_ids_by_node[node.path()] = job_ids
        submitted_job_ids.extend(job_ids)
        if job_ids:
            _LOGGER.info("FARMER NODE | %s | %s", node.path(), ", ".join(job_ids))
        elif not any(item.startswith(node.path() + " (") for item in failed_nodes):
            failed_nodes.append("{} (Deadline returned no Job ID)".format(node.path()))

    if not submitted_job_ids:
        hou.ui.displayMessage("Deadline submission failed.", title="Farmer Error")
        return []

    if options["discord_notify"]:
        try:
            from ayon_houdini.nodes import farm_report

            report_layers = []
            for node in nodes:
                node_job_ids = job_ids_by_node.get(node.path(), [])
                for index, job_id in enumerate(node_job_ids):
                    label = node.name()
                    if len(node_job_ids) > 1:
                        label += "-{:02d}".format(index + 1)
                    report_layers.append({"name": label, "job_id": job_id})
            report_host_job_id = submitted_job_ids[-1]
            farm_report.attach_report_to_job(
                deadline_command=(
                    shutil.which("deadlinecommand") or "deadlinecommand"
                ),
                host_job_id=report_host_job_id,
                dependency_job_ids=submitted_job_ids,
                report_root=os.path.join(submission_root, "farm_report"),
                layers=report_layers,
                project=project,
                shot=context.get("folder_path", ""),
                task=context.get("task_name", ""),
            )
            _LOGGER.info(
                "FARMER DISCORD | Attached to JobID: %s",
                report_host_job_id,
            )
        except Exception as exc:
            _LOGGER.exception("Could not attach the Farmer Discord notifier")
            failed_nodes.append("Discord notification ({})".format(exc))

    _LOGGER.info(
        "FARMER SUBMIT | Submitted: %d/%d | JobIDs: %s",
        len(submitted_job_ids),
        len(nodes),
        ", ".join(submitted_job_ids),
    )
    result_message = "Submitted {} job(s) from {} node(s).".format(
        len(submitted_job_ids), len(nodes) - len(failed_nodes)
    )
    if failed_nodes:
        result_message += "\n\nNot submitted:\n- " + "\n- ".join(failed_nodes)
    hou.ui.displayMessage(result_message, title="Farmer")
    return submitted_job_ids
