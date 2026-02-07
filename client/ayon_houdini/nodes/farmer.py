import hou
import os
import logging
from datetime import datetime
from ayon_core.pipeline import get_current_context
from ayon_houdini.nodes.filecache import active_version

_LOGGER = logging.getLogger("BMFX.Farmer")
if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)
_LOGGER.setLevel(logging.INFO)


def _collect_upstream_rops(node, collected=None):
    if collected is None:
        collected = []

    # 1. Look at all inputs of the current node
    for input_node in node.inputs():
        if not input_node:
            continue
            
        # 2. If it's a Fetch node, jump to the source target
        if input_node.type().name() == "fetch":
            target_path = input_node.parm("source").eval()
            target = hou.node(target_path)
            if target:
                # Recurse from the target of the fetch
                _collect_upstream_rops(target, collected)
        else:
            # Recurse from a standard direct input
            _collect_upstream_rops(input_node, collected)

    # 3. Check if THIS node (or its parent HDA) is a BMFX cache node
    # We check the parent because the 'target' of a fetch is often 
    # an internal ROP inside your HDA.
    hda_node = node if node.type().definition() else node.parent()
    
    try:
        # Check for our specific submission function to identify BMFX nodes
        if hasattr(hda_node.hdaModule(), "submit_cache_to_deadline"):
            if hda_node not in collected:
                collected.append(hda_node)
    except (AttributeError, hou.PermissionError):
        pass

    return collected


def submit_farmer_to_deadline(farmer_node):
    _LOGGER.info("FARMER SUBMIT | Node: %s", farmer_node.path())

    # 1. Collect all upstream BMFX nodes in order
    rops = _collect_upstream_rops(farmer_node)
    rops = list(dict.fromkeys(rops)) 

    if not rops:
        _LOGGER.warning(
            "FARMER SUBMIT | No upstream BMFX Cache nodes found for %s",
            farmer_node.path(),
        )
        hou.ui.displayMessage("No BMFX Cache nodes found upstream.", title="Farmer Error")
        return

    # 2. Generate a readable, unique Batch Name for Deadline Monitor
    ctx = get_current_context() or {}
    project = ctx.get("project_name", "project")
    folder_path = (ctx.get("folder_path") or "").strip("/")
    folder = folder_path.split("/")[-1] if folder_path else "folder"
    task = ctx.get("task_name", "task")
    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    batch_name = (
        f"Farm | {project} / {folder} / {task} | "
        f"{farmer_node.name()} | {submitted_at}"
    )
    _LOGGER.info(
        "FARMER SUBMIT | Batch: %s | Dependencies: %d",
        batch_name,
        len(rops),
    )
    
    last_job_id = None
    submitted_count = 0

    # 3. Iterate and submit
    for rop in rops:
        module = rop.hdaModule()
        if hasattr(module, "submit_cache_to_deadline"):
            # hou.setStatusMessage(f"Submitting {rop.name()}...")
            
            last_job_id = module.submit_cache_to_deadline(
                rop, 
                dependent_job_id=last_job_id,
                batch_name=batch_name
            )
            
            if last_job_id:
                submitted_count += 1

    if submitted_count == len(rops):
        _LOGGER.info(
            "FARMER SUBMIT | Completed | Submitted: %d | Last JobID: %s",
            submitted_count,
            last_job_id,
        )
    else:
        _LOGGER.warning(
            "FARMER SUBMIT | Partial submit | Submitted: %d/%d | Last JobID: %s",
            submitted_count,
            len(rops),
            last_job_id,
        )
