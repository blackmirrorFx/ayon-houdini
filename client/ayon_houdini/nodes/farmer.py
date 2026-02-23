import logging
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime

import hou
from ayon_core.pipeline import get_current_context


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


def ensure_deadline_machine_list_parms(node):
    """Add Deadline machine list parameters to farmer node in Farm Settings section."""
    if (
        node.parm(_DEADLINE_MACHINE_LIST_PARM)
        and node.parm(_DEADLINE_MACHINE_DENYLIST_PARM)
    ):
        _LOGGER.info("Machine list parameters already exist on %s", node.path())
        return

    try:
        group = node.parmTemplateGroup()
    except (hou.PermissionError, hou.OperationFailed) as e:
        _LOGGER.error("Cannot access parameter template group: %s", str(e))
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
                _LOGGER.info("Inserted deny list parameter after single_machine")
            else:
                group.append(deny_template)
                _LOGGER.info("Appended deny list parameter")
        except hou.OperationFailed as e:
            _LOGGER.error("Failed to insert deny list: %s", str(e))
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
    """Check if node is a generic ROP or cache node (not an HDA with submit hook)."""
    if _has_submit_hook(node):
        return False
    
    try:
        node_type = node.type().name()
        # Check if it's a ROP node (rop manager type contains 'rop')
        category = node.type().category().name()
        if category == "ROP":
            return True
        # Check for common cache/render node patterns
        if any(x in node_type.lower() for x in ["rop", "cache", "render", "bgeo", "usd"]):
            return True
    except Exception:
        pass
    
    return False


def _has_submit_hook(node):
    module = _get_hda_module(node)
    return bool(module and hasattr(module, "submit_cache_to_deadline"))


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

    return hou.node(source_path)


def _is_submittable_node(node):
    """Check if node can be submitted (has submit hook or is generic ROP)."""
    return _has_submit_hook(node) or _is_generic_rop_or_cache(node)


def _collect_upstream_submit_nodes(farmer_node):
    collected = []
    visited = set()

    def _walk(node):
        if not node:
            return

        node_path = node.path()
        if node_path in visited:
            return
        visited.add(node_path)

        for upstream in node.inputs():
            _walk(upstream)

        fetch_source = _get_fetch_source(node)
        if fetch_source:
            _walk(fetch_source)

        if _is_submittable_node(node):
            collected.append(node)

    for upstream in farmer_node.inputs():
        _walk(upstream)

    deduped = []
    seen = set()
    for node in collected:
        path = node.path()
        if path in seen:
            continue
        seen.add(path)
        deduped.append(node)

    return deduped


def _submit_cache_job(module, node, batch_name):
    # Keep jobs independent (parallel) by not setting dependency ids.
    try:
        return module.submit_cache_to_deadline(
            node,
            dependent_job_id=None,
            batch_name=batch_name,
        )
    except TypeError:
        try:
            return module.submit_cache_to_deadline(node, batch_name=batch_name)
        except TypeError:
            return module.submit_cache_to_deadline(node)


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
        hou.hscript(f'mwrite -n "{hscript_dst}"')
        _LOGGER.info("Saved hip file for submission: %s", hip_path)
        return hip_path
    except (hou.OperationFailed, Exception) as exc:
        _LOGGER.error("Failed to save hip file: %s", str(exc))
        return None


def _save_ayon_context_to_file(submission_root):
    """Save AYON context to a JSON file in the submission directory."""
    try:
        path = os.path.join(submission_root, ".ayon_vars.json")
        data = {
            "launcher_env": _get_ayon_launcher_env(),
            "houdini_vars": _filter_custom_houdini_vars(
                _get_all_houdini_vars()
            ),
            "pipeline_env": _get_pipeline_env(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
        _LOGGER.info("Saved AYON context: %s", path)
        return path
    except Exception as exc:
        _LOGGER.error("Failed to save AYON context: %s", str(exc))
        return None


def _get_frame_range(rop_node, farmer_node):
    """Extract frame range based on farmer node's validfr mode.
    
    validfr modes:
    - 0: Use upstream node's frame range
    - 1: Use farmer's fx/fy/fz parameters
    - 2: Use custom frame list
    """
    try:
        validfr_parm = farmer_node.parm("validfr")
        if not validfr_parm:
            # Fallback to ROP node's frame range
            for param_name in ["f1", "frame_start", "start_frame"]:
                parm = rop_node.parm(param_name)
                if parm:
                    f1 = int(parm.eval())
                    break
            else:
                f1 = int(hou.frame())
            
            for param_name in ["f2", "frame_end", "end_frame"]:
                parm = rop_node.parm(param_name)
                if parm:
                    f2 = int(parm.eval())
                    break
            else:
                f2 = int(hou.frame())
            
            return f1, f2
        
        validfr_mode = int(validfr_parm.eval())
        
        if validfr_mode == 0:
            # Use upstream frame range from ROP node
            for param_name in ["f1", "frame_start", "start_frame"]:
                parm = rop_node.parm(param_name)
                if parm:
                    f1 = int(parm.eval())
                    break
            else:
                f1 = int(hou.frame())
            
            for param_name in ["f2", "frame_end", "end_frame"]:
                parm = rop_node.parm(param_name)
                if parm:
                    f2 = int(parm.eval())
                    break
            else:
                f2 = int(hou.frame())
        
        elif validfr_mode == 1:
            # Use farmer's fx/fy parameters
            fx_parm = farmer_node.parm("fx")
            fy_parm = farmer_node.parm("fy")
            f1 = int(fx_parm.eval()) if fx_parm else int(hou.frame())
            f2 = int(fy_parm.eval()) if fy_parm else int(hou.frame())
        
        elif validfr_mode == 2:
            # Custom frame list - for now treat as range (start to end)
            fx_parm = farmer_node.parm("fx")
            fy_parm = farmer_node.parm("fy")
            f1 = int(fx_parm.eval()) if fx_parm else int(hou.frame())
            f2 = int(fy_parm.eval()) if fy_parm else int(hou.frame())
        
        else:
            # Fallback
            f1 = int(hou.frame())
            f2 = int(hou.frame())
        
        return f1, f2
    except (TypeError, ValueError, AttributeError):
        current_frame = int(hou.frame())
        return current_frame, current_frame


def _submit_generic_rop_to_deadline(node, batch_name, farmer_node_name, farmer_node):
    """Submit a generic ROP or cache node to Deadline with AYON context."""
    try:
        # Ensure farmer node has machine list parameters available
        if ensure_deadline_machine_list_parms:
            try:
                ensure_deadline_machine_list_parms(farmer_node)
            except Exception:
                pass
        # Get job prefix from farmer node
        try:
            job_prefix = farmer_node.parm("jobprefix").evalAsString() if farmer_node.parm("jobprefix") else ""
        except (TypeError, AttributeError):
            job_prefix = ""
        
        # Create submission root directory
        submission_root = _get_farmer_submission_root(farmer_node_name, job_prefix)
        
        # Save hip file and AYON context in the same directory
        hip_path = _save_hip_file_for_submission(submission_root)
        if not hip_path:
            return None
        
        ayon_context_json = _save_ayon_context_to_file(submission_root)
        if not ayon_context_json:
            return None
        
        # Get frame range based on farmer node settings
        f1, f2 = _get_frame_range(node, farmer_node)
        node_name = node.name()
        
        # Get parameters from farmer node
        try:
            priority = int(farmer_node.parm("priority").eval()) if farmer_node.parm("priority") else 50
        except (TypeError, AttributeError):
            priority = 50
        
        try:
            chunk_size = int(farmer_node.parm("chunk_size").eval()) if farmer_node.parm("chunk_size") else 10
        except (TypeError, AttributeError):
            chunk_size = 10
        
        try:
            machine_limit = int(farmer_node.parm("machine_limit").eval()) if farmer_node.parm("machine_limit") else 0
            machine_limit = machine_limit if machine_limit > 0 else None
        except (TypeError, AttributeError):
            machine_limit = None
        
        try:
            single_machine = bool(farmer_node.parm("single_machine").eval()) if farmer_node.parm("single_machine") else False
        except (TypeError, AttributeError):
            single_machine = False
        
        # Adjust chunk size if single machine
        if single_machine:
            chunk_size = (f2 - f1) + 1
        
        # Get machine list parameters
        try:
            machine_list_parm = farmer_node.parm("machine_list")
            machine_list = machine_list_parm.evalAsString() if machine_list_parm else ""
        except (TypeError, AttributeError):
            machine_list = ""
        
        try:
            machine_list_is_deny_parm = farmer_node.parm("machine_list_is_deny")
            machine_list_is_deny = bool(machine_list_is_deny_parm.eval()) if machine_list_is_deny_parm else False
        except (TypeError, AttributeError):
            machine_list_is_deny = False
        
        # Build job name with optional prefix
        if job_prefix.strip():
            job_name = f"{job_prefix} | {node_name}"
        else:
            job_name = node_name
        
        # Job info for Deadline
        job_info = {
            "Plugin": "Houdini",
            "BatchName": batch_name,
            "Name": job_name,
            "Frames": f"{f1}-{f2}",
            "ChunkSize": chunk_size,
            "Pool": "houdini",
            "Priority": priority,
            "EnvironmentKeyValue0": f"AYON_CONTEXT_JSON={ayon_context_json}",
            "EnvironmentKeyValue1": "AYON_CONTEXT_INJECTED=1",
        }
        
        if machine_limit:
            job_info["MachineLimit"] = machine_limit
        
        # Add machine list/blacklist if provided
        if machine_list.strip():
            key = "Blacklist" if machine_list_is_deny else "Whitelist"
            job_info[key] = machine_list
        
        # Plugin info for Deadline - use the saved hip file
        plugin_info = {
            "SceneFile": hip_path,
            "OutputDriver": node.path(),
            "Version": "21.0",
            "Build": "64bit",
            "IgnoreSceneFileFrameRange": True,
            "StartFrame": f1,
            "EndFrame": f2,
        }
        
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
            # Write job info
            for key, value in job_info.items():
                job_file.write(f"{key}={value}\n")
            job_file.close()
            
            # Write plugin info
            for key, value in plugin_info.items():
                plugin_file.write(f"{key}={value}\n")
            plugin_file.close()
            
            # Submit to Deadline
            cmd = ["deadlinecommand", job_file.name, plugin_file.name]
            result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
            _LOGGER.debug("Deadline output:\n%s", result.strip())
            
            # Extract JobID
            match = re.search(r"JobID=([\w\d]+)", result)
            if match:
                job_id = match.group(1)
                _LOGGER.info("Generic ROP submitted | Node: %s | JobID: %s", node.path(), job_id)
                return job_id
            
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
    # Ensure farmer node has the machine list parameters available
    if ensure_deadline_machine_list_parms:
        try:
            ensure_deadline_machine_list_parms(farmer_node)
        except Exception:
            pass

    rops = _collect_upstream_submit_nodes(farmer_node)
    if not rops:
        hou.ui.displayMessage("No upstream cache/render nodes found.", title="Farmer Error")
        return

    context = get_current_context() or {}
    project = context.get("project_name", "project")
    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    batch_name = "Farm | {} | {} | {}".format(project, farmer_node.name(), submitted_at)

    submitted_job_ids = []
    for rop in rops:
        job_id = None
        
        # Try HDA-based submission first
        module = _get_hda_module(rop)
        if module and hasattr(module, "submit_cache_to_deadline"):
            job_id = _submit_cache_job(module, rop, batch_name=batch_name)
        # Fall back to generic submission
        elif _is_generic_rop_or_cache(rop):
            job_id = _submit_generic_rop_to_deadline(
                rop,
                batch_name=batch_name,
                farmer_node_name=farmer_node.name(),
                farmer_node=farmer_node
            )
        
        if job_id:
            submitted_job_ids.append(job_id)
            _LOGGER.info("%s", rop.path())

    if not submitted_job_ids:
        hou.ui.displayMessage("Deadline submission failed.", title="Farmer Error")
        return

    _LOGGER.info(
        "FARMER SUBMIT | Submitted: %d/%d | JobIDs: %s",
        len(submitted_job_ids),
        len(rops),
        ", ".join(submitted_job_ids),
    )
