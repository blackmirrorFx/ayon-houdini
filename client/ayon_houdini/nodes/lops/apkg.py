"""Houdini controllers for BMFX APKG Begin and End LOPs.

AYON is queried only from explicit UI actions.  Cooking consumes the resolved,
immutable selection stored on the Begin HDA, so network/database availability
cannot make an existing HIP cook differently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import replace

try:
    import hou
except Exception:  # pragma: no cover - importable in unit tests
    hou = None

try:
    from qtpy import QtCore, QtWidgets
except Exception:  # pragma: no cover - headless/farm process
    QtCore = None
    QtWidgets = None

try:
    from ayon_houdini.apkg.shot_builder import (
        SHOT_DEPARTMENTS,
        SHOT_DEPARTMENT_LABELS,
        build_payload_plan,
        department_index,
        resolve_department,
        sanitize_node_name,
        sort_departments,
        upstream_departments,
    )
except ModuleNotFoundError:  # Direct source-tree unit tests.
    from apkg.shot_builder import (
        SHOT_DEPARTMENTS,
        SHOT_DEPARTMENT_LABELS,
        build_payload_plan,
        department_index,
        resolve_department,
        sanitize_node_name,
        sort_departments,
        upstream_departments,
    )


SELECTION_SCHEMA = "bmfx.apkg.selection/1.0"
MANAGED_SELECTION_USER_DATA = "bmfx_apkg_selection"
MANAGED_VIEWPORT_LOAD_PATHS_USER_DATA = "bmfx_apkg_viewport_load_paths"
BUILDER_ID_USER_DATA = "bmfx_apkg_builder_id"
PAIRED_END_USER_DATA = "bmfx_apkg_end_path"
PAIRED_BEGIN_USER_DATA = "bmfx_apkg_begin_path"
LOADER_USER_DATA = "bmfx_apkg_loader_path"
PUBLISHER_USER_DATA = "bmfx_apkg_publisher_path"
APKG_KIND_USER_DATA = "bmfx_apkg_kind"
GENERATED_NODE_USER_DATA = "bmfx_shot_builder_generated"
GENERATED_BUILDER_USER_DATA = "bmfx_shot_builder_id"
GENERATED_KIND_USER_DATA = "bmfx_shot_builder_kind"
GENERATED_KEY_USER_DATA = "bmfx_shot_builder_key"
BASE_INPUT_USER_DATA = "bmfx_shot_builder_base_input"
GRAPH_MODE_USER_DATA = "bmfx_shot_builder_graph_mode"
PAYLOAD_WRAPPER_USER_DATA = "bmfx_apkg_payload_wrapper"
PREVIOUS_SELECTION_USER_DATA = "bmfx_apkg_previous_selection"
PAYLOAD_GRAPH_MODE = "payload"
PAYLOAD_NODE_TYPE = "bmfx_apkg_payload::1.0"

log = logging.getLogger("bmfx.shot_builder")


def _interruptable_operation(label):
    if hou is None or not hou.isUIAvailable():
        return nullcontext()
    return hou.InterruptableOperation(
        label, open_interrupt_dialog=True
    )

_BEGIN_CONTEXT_CACHE = {}
_VERSION_PREVIEW_CACHE = {}
_VERSION_PREVIEW_TTL = 15.0

# Every department consumes all available departments before it. Missing
# departments are skipped by discovery and never produce empty compartments.
DEFAULT_TASK_PROFILES = {
    department: {
        "automatic": upstream_departments(department),
        "optional": (),
    }
    for department in SHOT_DEPARTMENTS
}

CFX_DEPARTMENTS = frozenset({"cfx", "crowd"})
SPLIT_DEPARTMENTS = frozenset({"fx", "cfx", "crowd"})
CFX_KINDS = ("hair", "cloth", "crowd", "crowdcfx")

GENERIC_USD_PUBLISH_FAMILIES = frozenset({
    "usd", "usdrop", "usdLayer", "usdAsset"
})

DEPENDENCY_MODE_LABELS = {
    "inherit": "Required Dependency",
    "context": "Context Only",
    # Legacy selections may contain this value. It historically published
    # through exactly like inherit, so present it honestly as required.
    "optional": "Required Dependency",
    "disabled": "Disabled",
    "overlay": "Overlay",
    "replace": "Replace",
}

class ApkgNodeError(RuntimeError):
    pass


def isolated_publish_families(values=()):
    """Return APKG-only families without AYON generic USD contributions."""
    families = set(values or ())
    families.difference_update(GENERIC_USD_PUBLISH_FAMILIES)
    families.add("apkg")
    return sorted(families)


def dependency_mode_label(value):
    """Return an artist-facing label without changing stored APKG metadata."""
    normalized = str(value or "inherit").strip().lower()
    return DEPENDENCY_MODE_LABELS.get(normalized, normalized.replace("_", " ").title())


def selection_uses_current_context(selection_data, task_name=None):
    """Return whether a locked build contains its department as context."""
    task = resolve_department(
        task_name or selection_data.get("task")
    ) or str(task_name or selection_data.get("task") or "").strip().lower()
    return bool(task) and any(
        resolve_department(item.get("department")) == task
        and str(item.get("mode") or "").strip().lower() == "context"
        for item in selection_data.get("packages") or ()
    )


def selection_uses_downstream_lighting(selection_data, task_name=None):
    """Return whether a locked non-Lighting build uses Lighting as context."""
    task = resolve_department(
        task_name or selection_data.get("task")
    ) or str(task_name or selection_data.get("task") or "").strip().lower()
    return bool(task and task != "lighting") and any(
        resolve_department(item.get("department")) == "lighting"
        and str(item.get("mode") or "").strip().lower() == "context"
        for item in selection_data.get("packages") or ()
    )


def _set_parm(node, name, value):
    parm = node.parm(name)
    if parm is not None:
        parm.set(value)


def _eval(node, name, default=""):
    parm = node.parm(name)
    return parm.eval() if parm is not None else default


def _lines(value):
    return tuple(
        part.strip()
        for part in re.split(r"[\r\n,;]+", str(value or ""))
        if part.strip()
    )


def _payload_export_paths(items):
    """Return stable USD branches whose APKG payloads must be visible."""
    return tuple(sorted({
        str(path).strip()
        for item in items or ()
        if item.get("mode") != "disabled"
        for path in item.get("exports") or ()
        if str(path).strip().startswith("/")
        and str(path).strip() != "/HoudiniLayerInfo"
    }))


def _sync_viewport_payloads(node, items):
    """Load only the payload branches managed by this APKG Begin node."""
    if hou is None:
        return
    network = node.parent()
    if not hasattr(network, "viewportLoadMasks"):
        return
    try:
        masks = network.viewportLoadMasks()
        previous = json.loads(node.userData(
            MANAGED_VIEWPORT_LOAD_PATHS_USER_DATA
        ) or "[]")
        for path in previous:
            masks.removeLoadPath(str(path), remove_children=False)

        managed = []
        if not masks.loadAll():
            for path in _payload_export_paths(items):
                # An explicitly loaded ancestor already covers this branch.
                if not masks.isPathLoaded(path, exact_match=False):
                    masks.addLoadPath(path)
                    managed.append(path)
        network.setViewportLoadMasks(masks)
        node.setUserData(
            MANAGED_VIEWPORT_LOAD_PATHS_USER_DATA,
            json.dumps(managed, separators=(",", ":")),
        )
    except (AttributeError, TypeError, ValueError, hou.Error):
        # Payload visibility is a viewport convenience. It must never prevent
        # the immutable USD selection from being stored or cooked.
        return


def ensure_builder_id(node):
    value = str(_eval(node, "builder_id") or node.userData(
        BUILDER_ID_USER_DATA
    ) or "").strip()
    if not value:
        value = str(uuid.uuid4())
    _set_parm(node, "builder_id", value)
    node.setUserData(BUILDER_ID_USER_DATA, value)
    return value


def use_split_value(node, task_name=None):
    """Return whether Split is part of the public APKG identity."""
    task = resolve_department(
        task_name or _current_context()["task_name"]
    ) or str(task_name or _current_context()["task_name"]).strip().lower()
    if task not in SPLIT_DEPARTMENTS:
        return False
    use_split = node.parm("use_split")
    # Legacy End nodes had no toggle and always authored a split token.
    return bool(use_split.eval()) if use_split is not None else True


def split_value(node, task_name=None):
    if not use_split_value(node, task_name):
        return "main"
    value = re.sub(
        r"[^A-Za-z0-9]+", "_", str(_eval(node, "split", "main")).strip()
    ).strip("_").lower()
    if not value:
        raise ApkgNodeError("Split is required (for example smoke, dust or debris)")
    return value


def cfx_kind_value(node, task_name=None):
    """Return the required CFX/Crowd contribution classification."""
    task = resolve_department(
        task_name or _current_context()["task_name"]
    ) or str(task_name or _current_context()["task_name"]).strip().lower()
    if task not in CFX_DEPARTMENTS:
        return ""
    try:
        index = int(_eval(node, "cfx_kind", 0))
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(CFX_KINDS):
        raise ApkgNodeError("Invalid CFX type on APKG End")
    return CFX_KINDS[index]


def sync_end_context_interface(end):
    """Show task-specific End controls from the immutable AYON context."""
    task = resolve_department(_current_context()["task_name"]) or str(
        _current_context()["task_name"]
    ).strip().lower()
    cfx_enabled = task in CFX_DEPARTMENTS
    split_enabled = task in SPLIT_DEPARTMENTS
    _set_parm(end, "cfx_kind_enabled", int(cfx_enabled))
    _set_parm(end, "split_enabled", int(split_enabled))
    if not split_enabled:
        _set_parm(end, "use_split", 0)
    if cfx_enabled and end.parm("cfx_kind") is not None:
        # Crowd defaults to Crowd; CFX defaults to Hair. Once the artist has
        # chosen a valid value, interface refreshes preserve that choice.
        initialized = end.userData("bmfx_apkg_cfx_kind_initialized") == "1"
        value = int(_eval(end, "cfx_kind", 0) or 0)
        if not initialized:
            _set_parm(end, "cfx_kind", 2 if task == "crowd" else 0)
            end.setUserData("bmfx_apkg_cfx_kind_initialized", "1")
        elif value < 0 or value >= len(CFX_KINDS):
            _set_parm(end, "cfx_kind", 2 if task == "crowd" else 0)
    return split_enabled


def _block_parent(kwargs):
    pane = kwargs.get("pane") or kwargs.get("networkeditor")
    if pane is not None:
        try:
            parent = pane.pwd()
            if parent and parent.childTypeCategory() == hou.lopNodeTypeCategory():
                return parent, pane
        except Exception:
            pass
    parent = hou.node("/stage")
    if parent is None:
        raise ApkgNodeError("The Houdini scene has no /stage network")
    return parent, pane


def _install_begin_interface(node):
    """Copy the APKG HDA controls onto a native Solaris block-begin node."""
    source_type = hou.nodeType(
        hou.lopNodeTypeCategory(), "bmfx_apkg_begin::1.0"
    )
    if source_type is None:
        raise ApkgNodeError("BMFX APKG Begin HDA is not installed")
    source = source_type.parmTemplateGroup().find("build")
    if source is None:
        raise ApkgNodeError("BMFX APKG Begin interface is unavailable")
    group = node.parmTemplateGroup()
    existing = group.find("build")
    if existing is None:
        group.append(source)
    else:
        group.replace("build", source)
    node.setParmTemplateGroup(group)


def _install_end_interface(node):
    """Hide native controls and expose only APKG publish identity controls."""
    source_type = hou.nodeType(
        hou.lopNodeTypeCategory(), "bmfx_apkg_end::1.0"
    )
    if source_type is None:
        raise ApkgNodeError("BMFX APKG End HDA is not installed")
    source = source_type.parmTemplateGroup().find("identity")
    if source is None:
        raise ApkgNodeError("BMFX APKG End interface is unavailable")
    group = node.parmTemplateGroup()
    for template in group.entries():
        if template.name() == "identity":
            continue
        hidden = template.clone()
        hidden.hide(True)
        group.replace(template.name(), hidden)
    existing = group.find("identity")
    if existing is None:
        group.append(source)
    else:
        group.replace("identity", source)
    node.setParmTemplateGroup(group)


def is_apkg_begin(node):
    if node is None:
        return False
    type_name = node.type().name().split("::", 1)[0]
    return (
        type_name in {"bmfx_apkg_begin", "bmfx_apkg_block_begin"}
        or node.userData(APKG_KIND_USER_DATA) == "begin"
    )


def is_apkg_end(node):
    if node is None:
        return False
    type_name = node.type().name().split("::", 1)[0]
    return (
        type_name in {"bmfx_apkg_end", "bmfx_apkg_block_end"}
        or node.userData(APKG_KIND_USER_DATA) == "end"
    )


def is_apkg_publisher(node):
    if node is None:
        return False
    type_name = node.type().name().split("::", 1)[0]
    return (
        type_name == "bmfx_apkg_publish"
        or node.userData(APKG_KIND_USER_DATA) == "publisher"
    )


def create_publisher(end, name="apkg_publish"):
    """Create the APKG-specific Publisher LOP directly after End."""
    parent = end.parent()
    type_name = "bmfx_apkg_publish::1.0"
    if hou.nodeType(hou.lopNodeTypeCategory(), type_name) is None:
        addon_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        asset_path = os.path.join(
            addon_root, "startup", "otls", "lop_bmfx_apkg_publish.1.0.hda"
        )
        if not os.path.isfile(asset_path):
            raise ApkgNodeError(
                "BMFX APKG Publisher HDA is not installed: {}".format(
                    asset_path
                )
            )
        hou.hda.installFile(asset_path)
    publisher = parent.createNode(type_name, name)
    publisher.setInput(0, end)
    publisher.setPosition(end.position() + hou.Vector2(0, -1.7))
    publisher.setUserData(APKG_KIND_USER_DATA, "publisher")
    publisher.setUserData(PAIRED_END_USER_DATA, end.path())
    end.setUserData(PUBLISHER_USER_DATA, publisher.path())
    return publisher


def _publisher_end(publisher):
    end = hou.node(publisher.userData(PAIRED_END_USER_DATA) or "")
    if end is None and publisher.inputs():
        end = publisher.inputs()[0]
    if not is_apkg_end(end):
        raise ApkgNodeError(
            "BMFX APKG Publisher is not connected to an APKG End"
        )
    return end


def publisher_product_name(publisher):
    """Return the canonical live product identity for a Publisher LOP."""
    from ayon_houdini.apkg.naming import apkg_product_name

    end = _publisher_end(publisher)
    task = resolve_department(_current_context()["task_name"]) or str(
        _current_context()["task_name"]
    ).strip().lower()
    split = split_value(end, task)
    classification = cfx_kind_value(end, task)
    return apkg_product_name(
        task, split, split, classification=classification,
        use_split=use_split_value(end, task),
    )


def next_publish_version(product_name, force=False):
    """Return AYON's next available version for this shot product."""
    import ayon_api

    context = _current_context()
    cache_key = (
        context["project_name"], context["folder_path"], product_name
    )
    now = time.monotonic()
    cached = _VERSION_PREVIEW_CACHE.get(cache_key)
    if not force and cached and now - cached[0] < _VERSION_PREVIEW_TTL:
        return cached[1]

    version = None
    try:
        folder = ayon_api.get_folder_by_path(
            project_name=context["project_name"],
            folder_path=context["folder_path"],
        )
        if folder:
            product = ayon_api.get_product_by_name(
                project_name=context["project_name"],
                product_name=product_name,
                folder_id=folder["id"],
            )
            if not product:
                version = 1
            else:
                latest = ayon_api.get_last_version_by_product_id(
                    project_name=context["project_name"],
                    product_id=product["id"],
                )
                version = int(latest["version"]) + 1 if latest else 1
    except Exception:
        version = None
    _VERSION_PREVIEW_CACHE[cache_key] = (now, version)
    return version


def publisher_publish_name_display(publisher):
    """Return the canonical product plus its next AYON version."""
    from ayon_houdini.apkg.naming import apkg_version_name

    product_name = publisher_product_name(publisher)
    version = next_publish_version(product_name)
    if version is None:
        return "{}_v???".format(product_name)
    return apkg_version_name(product_name, version)


def end_publish_name_display(end):
    """Python-expression value for End's read-only publish-name logger."""
    publisher = hou.node(end.userData(PUBLISHER_USER_DATA) or "")
    if publisher is None:
        return "No APKG Publisher connected"
    try:
        return publisher_publish_name_display(publisher)
    except ApkgNodeError as exc:
        return "Invalid APKG identity: {}".format(exc)


def update_publisher_identity(publisher):
    """Synchronize existing AYON imprint fields with visible node controls."""
    product_name = publisher_product_name(publisher)
    end_data_value = end_data(_publisher_end(publisher))
    apkg_name = (
        end_data_value["split"] if end_data_value["useSplit"] else "main"
    )
    for parm_name, value in (
        ("apkgName", apkg_name),
        ("variant", apkg_name),
        ("AYON_productName", product_name),
    ):
        _set_parm(publisher, parm_name, value)
    publisher.setUserData("bmfx_apkg_product_name", product_name)
    return product_name


def clean_publisher_instance_interface(publisher):
    """Hide AYON storage parms and remove its redundant publish button."""
    group = publisher.parmTemplateGroup()
    changed = False
    extra = group.findFolder("Extra")
    if extra is not None:
        hidden = extra.clone()
        hidden.hide(True)
        group.replace(extra.name(), hidden)
        changed = True
    if group.find("ayon_self_publish") is not None:
        group.remove("ayon_self_publish")
        changed = True
    if changed:
        publisher.setParmTemplateGroup(group)
    return changed


def publish_from_node(publisher):
    """Create/update the AYON instance and publish directly from the LOP."""
    if hou is None or not hou.isUIAvailable():
        raise ApkgNodeError("APKG node publishing requires Houdini UI")
    if not is_apkg_publisher(publisher):
        raise ApkgNodeError("Select a BMFX APKG Publisher node")

    from ayon_core.pipeline import registered_host
    from ayon_core.pipeline.create import CreateContext
    from ayon_houdini.api import lib

    product_name = update_publisher_identity(publisher)
    # Refresh the preview immediately before publishing. AYON still assigns
    # the authoritative version if another publish wins a concurrent race.
    next_publish_version(product_name, force=True)
    end_data_value = end_data(_publisher_end(publisher))
    apkg_name = (
        end_data_value["split"] if end_data_value["useSplit"] else "main"
    )
    context = CreateContext(registered_host(), reset=True)
    instance = next((
        item for item in context.instances
        if item.get("instance_node") == publisher.path()
    ), None)
    if instance is None:
        context.create(
            creator_identifier="io.ayon.creators.houdini.apkg",
            variant=apkg_name,
            pre_create_data={
                "apkg_publisher_path": publisher.path(),
                "render_target": "local",
            },
        )
    else:
        instance["variant"] = apkg_name
        instance["productName"] = product_name
        instance["apkgName"] = apkg_name
        instance["apkgTask"] = end_data_value["task"]
        instance["apkgSplit"] = end_data_value["split"]
        instance["apkgClassification"] = end_data_value.get(
            "classification", ""
        )
        instance["apkgUseSplit"] = end_data_value["useSplit"]
        instance["active"] = True
        context.save_changes()

    clean_publisher_instance_interface(publisher)
    publisher.setUserData("bmfx_apkg_product_name", product_name)
    return lib.self_publish(publisher)


def publish_from_node_callback(publisher):
    """Run node publishing with an artist-facing error instead of traceback."""
    try:
        return publish_from_node(publisher)
    except Exception as exc:
        if hou is not None and hou.isUIAvailable():
            hou.ui.displayMessage(
                "APKG publish could not start:\n{}".format(exc),
                severity=hou.severityType.Error,
                title="BMFX APKG Publisher",
            )
            return None
        raise


def check_department_usd(end_node):
    """Verify local department USD survives into the APKG Publisher stage."""
    if is_apkg_publisher(end_node):
        end_node = _publisher_end(end_node)
    if not is_apkg_end(end_node):
        raise ApkgNodeError("Select a BMFX APKG End node")

    data = end_data(end_node)
    stage = end_node.stage(apply_viewport_overrides=False)
    if stage is None:
        raise ApkgNodeError("APKG End has no valid USD stage")
    layers = _layers_below_break(end_node, stage)
    authored_paths = sorted(
        path for path in _authored_prim_paths(layers)
        if path not in {"/World", "/HoudiniLayerInfo"}
    )
    if not authored_paths:
        raise ApkgNodeError(
            "No department USD was authored inside the APKG block"
        )
    exports = _derived_exports(
        stage, layers, data["task"], data["split"]
    )
    if not exports:
        raise ApkgNodeError(
            "Department USD exists, but no APKG export prims were resolved"
        )

    publisher = hou.node(end_node.userData(PUBLISHER_USER_DATA) or "")
    if not is_apkg_publisher(publisher):
        raise ApkgNodeError("APKG Publisher is missing or disconnected")
    publisher_stage = publisher.stage(apply_viewport_overrides=False)
    if publisher_stage is None:
        raise ApkgNodeError("APKG Publisher has no valid USD stage")
    missing = [
        path for path in exports if not publisher_stage.GetPrimAtPath(path)
    ]
    if missing:
        raise ApkgNodeError(
            "Department USD is missing from the Publisher stage: {}".format(
                ", ".join(missing)
            )
        )
    has_marker = any(
        bool((getattr(layer, "customLayerData", None) or {}).get(
            "bmfx:apkgPublisher"
        ))
        for layer in publisher_stage.GetLayerStack(
            includeSessionLayers=False
        )
    )
    if not has_marker:
        raise ApkgNodeError(
            "USD reaches the Publisher, but the APKG publisher marker is "
            "missing"
        )
    return {
        "department": data["department"],
        "authoredPaths": tuple(authored_paths),
        "exports": tuple(exports),
        "message": "Ready: {} USD export{} injected".format(
            len(exports), "" if len(exports) == 1 else "s"
        ),
    }


def check_department_usd_callback(end_node):
    """Run the department USD check and show a concise persistent result."""
    try:
        result = check_department_usd(end_node)
        message = result["message"]
        details = "{}\n\nExports:\n{}".format(
            message, "\n".join(result["exports"])
        )
        severity = hou.severityType.Message
    except Exception as exc:
        message = "Missing: {}".format(exc)
        details = message
        severity = hou.severityType.Error
    _set_parm(end_node, "department_usd_status", message)
    if hou is not None and hou.isUIAvailable():
        hou.ui.displayMessage(
            details,
            severity=severity,
            title="BMFX APKG Department USD Check",
        )
    return message


def migrate_end_to_native(end, begin):
    """Replace a stock/legacy End while preserving its graph connections."""
    native_type = "bmfx_apkg_block_end"
    if end.type().name().split("::", 1)[0] == native_type:
        end.setUserData(APKG_KIND_USER_DATA, "end")
        end.setUserData(PAIRED_BEGIN_USER_DATA, begin.path())
        begin.setUserData(PAIRED_END_USER_DATA, end.path())
        return end
    if hou.nodeType(hou.lopNodeTypeCategory(), native_type) is None:
        raise ApkgNodeError(
            "BMFX APKG native End DSO is not loaded. Restart Houdini once "
            "after installing this addon build."
        )

    parent = end.parent()
    upstream = end.input(0)
    consumers = [
        (connection.outputNode(), connection.inputIndex())
        for connection in tuple(end.outputConnections())
    ]

    with hou.undos.group("Upgrade BMFX APKG End"):
        replacement = parent.createNode(native_type, "__apkg_end_upgrade")
        _install_end_interface(replacement)
        for source_parm in end.parms():
            target_parm = replacement.parm(source_parm.name())
            if target_parm is None:
                continue
            try:
                target_parm.set(source_parm.eval())
            except (hou.Error, TypeError):
                pass
        for key, value in end.userDataDict().items():
            replacement.setUserData(key, value)

        replacement.setPosition(end.position())
        replacement.setColor(end.color())
        if upstream is not None:
            replacement.setInput(0, upstream)
        for consumer, input_index in consumers:
            consumer.setInput(input_index, replacement)

        old_name = end.name()
        end.setName("__legacy_apkg_end", unique_name=True)
        replacement.setName(old_name)
        replacement.setUserData(APKG_KIND_USER_DATA, "end")
        replacement.setUserData(PAIRED_BEGIN_USER_DATA, begin.path())
        begin.setUserData(PAIRED_END_USER_DATA, replacement.path())
        publisher = hou.node(
            replacement.userData(PUBLISHER_USER_DATA) or ""
        )
        if publisher is not None:
            publisher.setUserData(PAIRED_END_USER_DATA, replacement.path())
        end.destroy()
    return replacement


def migrate_begin_to_native(begin):
    """Replace legacy APKG boundaries with the compiled native pair."""
    native_type = "bmfx_apkg_block_begin"
    if begin.type().name().split("::", 1)[0] == native_type:
        begin.setUserData(LOADER_USER_DATA, begin.path())
        end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
        if not is_apkg_end(end):
            raise ApkgNodeError(
                "Could not find this block's paired APKG End node"
            )
        migrate_end_to_native(end, begin)
        return begin
    if hou.nodeType(hou.lopNodeTypeCategory(), native_type) is None:
        raise ApkgNodeError(
            "BMFX APKG native Begin DSO is not loaded. Restart Houdini once "
            "after installing this addon build."
        )

    end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
    if not is_apkg_end(end):
        raise ApkgNodeError("Could not find this block's paired APKG End node")

    parent = begin.parent()
    old_loader = hou.node(begin.userData(LOADER_USER_DATA) or "")
    upstream = (
        old_loader.input(0)
        if old_loader is not None and old_loader != begin
        else begin.input(0)
    )
    consumers = [
        (connection.outputNode(), connection.inputIndex())
        for connection in tuple(begin.outputConnections())
    ]

    with hou.undos.group("Upgrade BMFX APKG Begin"):
        replacement = parent.createNode(native_type, "__apkg_begin_upgrade")
        _install_begin_interface(replacement)
        for source_parm in begin.parms():
            target_parm = replacement.parm(source_parm.name())
            if target_parm is None:
                continue
            try:
                target_parm.set(source_parm.eval())
            except (hou.Error, TypeError):
                pass
        for key, value in begin.userDataDict().items():
            replacement.setUserData(key, value)

        replacement.setPosition(begin.position())
        replacement.setColor(begin.color())
        if upstream is not None:
            replacement.setInput(0, upstream)
        for consumer, input_index in consumers:
            consumer.setInput(input_index, replacement)

        old_name = begin.name()
        begin.setName("__legacy_apkg_begin", unique_name=True)
        replacement.setName(old_name)
        replacement.setUserData(APKG_KIND_USER_DATA, "begin")
        replacement.setUserData(PAIRED_END_USER_DATA, end.path())
        replacement.setUserData(LOADER_USER_DATA, replacement.path())
        end.setUserData(PAIRED_BEGIN_USER_DATA, replacement.path())

        begin.destroy()
        if old_loader is not None and old_loader != begin:
            old_loader.destroy()
        _sync_loader(replacement)
    migrate_end_to_native(end, replacement)
    return replacement


def _sync_loader(begin):
    """Mirror the visible Begin state to its internal APKG composition."""
    loader = hou.node(begin.userData(LOADER_USER_DATA) or "")
    if loader is None and begin.type().name().split("::", 1)[0] == (
        "bmfx_apkg_block_begin"
    ):
        loader = begin
    if loader is None:
        return None
    for name in ("builder_id", "selection_json"):
        _set_parm(loader, name, _eval(begin, name))
    payload = begin.userData(MANAGED_SELECTION_USER_DATA)
    if payload:
        loader.setUserData(MANAGED_SELECTION_USER_DATA, payload)
    else:
        loader.destroyUserData(
            MANAGED_SELECTION_USER_DATA, must_exist=False
        )
    # New builds sublayer one lightweight, generated composition layer inside
    # Begin. That layer payloads the actual APKG entrypoints; no package loader
    # nodes are exposed in the artist network. Legacy HIP files keep their old
    # direct sublayer behavior until explicitly rebuilt.
    if begin.userData(GRAPH_MODE_USER_DATA) == PAYLOAD_GRAPH_MODE:
        wrapper = str(begin.userData(PAYLOAD_WRAPPER_USER_DATA) or "").strip()
        if selection(begin).get("packages") and not wrapper:
            raise ApkgNodeError("APKG Begin has no internal payload layer")
        paths = [wrapper] if wrapper else []
    else:
        paths = composition_paths(selection(begin))
    _set_parm(loader, "apkg_sublayer_paths", "\n".join(paths))
    try:
        loader.cook(force=True)
    except hou.Error as exc:
        diagnostics = _cook_diagnostics(loader)
        detail = "\n".join(diagnostics) or str(exc)
        raise ApkgNodeError(
            "APKG Begin failed to compose the payload graph\n{}".format(
                detail
            )
        ) from exc
    return loader


def _payload_asset_path():
    addon_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(
        addon_root, "startup", "otls", "lop_bmfx_apkg_payload.1.0.hda"
    )


def _ensure_payload_node_type():
    """Install and return the generated APKG Payload LOP type."""
    asset_path = _payload_asset_path()
    if not os.path.isfile(asset_path):
        raise ApkgNodeError(
            "BMFX APKG Payload HDA is not installed: {}".format(asset_path)
        )
    # Always refresh this small generated definition. This fixes an already
    # running Houdini session after an addon update without requiring a
    # restart, and ensures its 0-or-1 input contract is current.
    try:
        hou.hda.installFile(asset_path, force_use_assets=True)
    except TypeError:
        hou.hda.installFile(asset_path)
    if hou.nodeType(hou.lopNodeTypeCategory(), PAYLOAD_NODE_TYPE) is None:
        raise ApkgNodeError("BMFX APKG Payload LOP is unavailable")
    return PAYLOAD_NODE_TYPE


def _is_generated_for(begin, item):
    return bool(
        item is not None
        and item.userData(GENERATED_NODE_USER_DATA) == "1"
        and item.userData(GENERATED_BUILDER_USER_DATA)
        == ensure_builder_id(begin)
    )


def _generated_nodes(begin):
    return tuple(
        child for child in begin.parent().children()
        if _is_generated_for(begin, child)
    )


def _generated_boxes(begin):
    output = []
    for box in begin.parent().networkBoxes():
        try:
            generated = box.userData(GENERATED_NODE_USER_DATA) == "1"
            builder_id = box.userData(GENERATED_BUILDER_USER_DATA)
        except (AttributeError, hou.Error):
            generated = False
            builder_id = ""
        if generated and builder_id == ensure_builder_id(begin):
            output.append(box)
    return tuple(output)


def _tag_generated(item, begin, kind, key=""):
    item.setUserData(GENERATED_NODE_USER_DATA, "1")
    item.setUserData(GENERATED_BUILDER_USER_DATA, ensure_builder_id(begin))
    item.setUserData(GENERATED_KIND_USER_DATA, str(kind))
    item.setUserData(GENERATED_KEY_USER_DATA, str(key))


def _payload_spec(package):
    return {
        "schema": "bmfx.apkg.payload/1.0",
        "stableKey": package.stable_key,
        "packageUid": package.package_uid,
        "department": package.department,
        "productName": package.product_name,
        "productId": package.product_id,
        "versionId": package.version_id,
        "representationId": package.representation_id,
        "version": package.version,
        "status": package.status,
        "mode": package.mode,
        "loadStrategy": "payload",
        "arcs": [
            {
                "assetPath": arc.asset_path,
                "targetPrim": arc.target_prim,
                "sourcePrim": arc.source_prim,
            }
            for arc in package.arcs
        ],
    }


def _unique_node_name(parent, requested):
    base = re.sub(r"[^A-Za-z0-9_]+", "_", str(requested or "")).strip("_")
    base = re.sub(r"_+", "_", base) or "apkg"
    if base[0].isdigit():
        base = "apkg_" + base
    if parent.node(base) is None:
        return base
    index = 2
    while parent.node("{}_{}".format(base, index)) is not None:
        index += 1
    return "{}_{}".format(base, index)


def _set_payload_node(node, begin, package):
    """Update one generated node without replacing the artist's node item."""
    spec = _payload_spec(package)
    _set_parm(node, "payload_spec_json", json.dumps(
        spec, sort_keys=True, separators=(",", ":")
    ))
    _set_parm(node, "package_display", "{} · v{:03d}".format(
        package.product_name, package.version
    ))
    _set_parm(node, "department_display", SHOT_DEPARTMENT_LABELS[
        package.department
    ])
    _set_parm(node, "status_display", package.status.title())
    if node.parm("load_payload") is not None and not node.userData(
        "bmfx_payload_initialized"
    ):
        _set_parm(node, "load_payload", 1)
        node.setUserData("bmfx_payload_initialized", "1")
    _tag_generated(node, begin, "payload", package.stable_key)
    for key, value in (
        ("bmfx_department", package.department),
        ("ayon_product", package.product_name),
        ("ayon_version", str(package.version)),
        ("ayon_product_id", package.product_id),
        ("ayon_version_id", package.version_id),
        ("ayon_representation_id", package.representation_id),
        ("ayon_source_path", package.arcs[0].asset_path),
        ("bmfx_package_uid", package.package_uid),
        ("bmfx_dependency_mode", package.mode),
    ):
        node.setUserData(key, str(value or ""))
    node.setComment("{}\n{} · v{:03d} · {}\nPayload · {} export{}".format(
        SHOT_DEPARTMENT_LABELS[package.department],
        package.product_name,
        package.version,
        package.status.title(),
        len(package.arcs),
        "" if len(package.arcs) == 1 else "s",
    ))
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    return node


def _validate_payload_sources(plan):
    """Validate local APKG entrypoints and declared source prims."""
    errors = []
    try:
        from pxr import Usd
    except Exception:
        Usd = None
    stages = {}
    for package in plan.packages:
        path = package.arcs[0].asset_path
        # Resolver URIs and tokenized paths are validated by their resolver at
        # cook time. Absolute/local paths must already exist for a safe build.
        if os.path.isabs(path) and not os.path.isfile(path):
            errors.append("{} entrypoint does not exist: {}".format(
                package.product_name, path
            ))
            continue
        if Usd is None or not os.path.isfile(path):
            continue
        stage = stages.get(path)
        if stage is None:
            try:
                stage = Usd.Stage.Open(path, load=Usd.Stage.LoadNone)
            except Exception as exc:
                errors.append("{} could not be opened: {}".format(path, exc))
                stages[path] = False
                continue
            stages[path] = stage or False
        if not stage:
            errors.append("{} is not a readable USD stage".format(path))
            continue
        for arc in package.arcs:
            if not stage.GetPrimAtPath(arc.source_prim):
                errors.append(
                    "{} does not contain payload source prim {}".format(
                        package.product_name, arc.source_prim
                    )
                )
    if errors:
        raise ApkgNodeError("Payload validation failed:\n" + "\n".join(errors))


def _payload_wrapper_directory():
    """Return a persistent work location for internal payload layers."""
    candidates = []
    if hou is not None:
        try:
            hip_path = str(hou.hipFile.path() or "")
        except (AttributeError, hou.Error):
            hip_path = ""
        if hip_path and not os.path.basename(hip_path).lower().startswith(
            "untitled"
        ):
            candidates.append(os.path.dirname(os.path.abspath(hip_path)))
    candidates.extend(
        os.environ.get(name, "")
        for name in ("AYON_WORKDIR", "AVALON_WORKDIR")
    )
    candidates.append(tempfile.gettempdir())
    failures = []
    for root in candidates:
        if not root:
            continue
        directory = os.path.join(
            os.path.abspath(os.path.expandvars(root)),
            ".ayon", "apkg_payloads",
        )
        try:
            os.makedirs(directory, exist_ok=True)
            return directory
        except OSError as exc:
            failures.append("{}: {}".format(directory, exc))
    raise ApkgNodeError(
        "Could not create the APKG internal payload directory\n{}".format(
            "\n".join(failures)
        )
    )


def _payload_wrapper_lock(plan):
    return {
        "compositionSchema": "bmfx.apkg.internal-payload/3",
        "packages": [
            {
                "packageUid": package.package_uid,
                "representationId": package.representation_id,
                "version": package.version,
                "arcs": [
                    (arc.asset_path, arc.target_prim, arc.source_prim)
                    for arc in package.arcs
                ],
            }
            for package in plan.packages
        ],
    }


def _copy_package_scope_shell(target_stage, package, Usd):
    """Recreate published context scopes without making them payloads."""
    asset_path = package.arcs[0].asset_path
    try:
        source_stage = Usd.Stage.Open(asset_path, load=Usd.Stage.LoadNone)
    except Exception as exc:
        raise ApkgNodeError(
            "Could not inspect {} namespace: {}".format(
                package.product_name, exc
            )
        ) from exc
    if source_stage is None:
        raise ApkgNodeError(
            "Could not inspect {} namespace: {}".format(
                package.product_name, asset_path
            )
        )
    copied = set()
    for prim in source_stage.TraverseAll():
        path = str(prim.GetPath())
        if (
            not path
            or path in {"/", "/HoudiniLayerInfo"}
            or str(prim.GetTypeName()) != "Scope"
        ):
            continue
        scope = target_stage.DefinePrim(path, "Scope")
        kind = prim.GetMetadata("kind")
        if kind:
            scope.SetMetadata("kind", kind)
        copied.add(path)

    # A contribution-only APKG may contain only over prims. Always create the
    # structural ancestors required to place its declared export cleanly.
    for arc in package.arcs:
        parent = arc.target_prim.rsplit("/", 1)[0]
        while parent:
            if parent != "/":
                target_stage.DefinePrim(parent, "Scope")
                copied.add(parent)
            parent = parent.rsplit("/", 1)[0]
    return tuple(sorted(copied))


def _write_payload_wrapper(begin, plan):
    """Write one internal USD layer that payloads all locked APKGs.

    The wrapper is the only layer consumed by the native Begin LOP. APKG
    entrypoints remain payload arcs and no generated loader nodes appear in
    the parent Solaris network.
    """
    if not plan.packages:
        return ""
    try:
        from pxr import Sdf, Usd
    except Exception as exc:
        raise ApkgNodeError(
            "OpenUSD is unavailable; cannot build the APKG payload layer"
        ) from exc

    locked = _payload_wrapper_lock(plan)
    serialized = json.dumps(
        locked, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    builder_id = sanitize_node_name(ensure_builder_id(begin), "builder")
    output_path = os.path.join(
        _payload_wrapper_directory(),
        "bmfx_apkg_{}_{}.usda".format(builder_id, digest),
    )
    if os.path.isfile(output_path):
        return output_path.replace("\\", "/")

    temporary_path = "{}.{}.tmp.usda".format(output_path, uuid.uuid4().hex)
    try:
        stage = Usd.Stage.CreateNew(temporary_path)
        if stage is None:
            raise ApkgNodeError(
                "Could not create internal APKG payload layer: {}".format(
                    temporary_path
                )
            )
        # Preserve the entrypoint's scene-graph context as ordinary local
        # scopes. These provide /World, /Render and the sibling department
        # paths but deliberately carry no payload arcs.
        for package in plan.packages:
            _copy_package_scope_shell(stage, package, Usd)

        # USD list arcs are strongest first. The plan is dependency-first
        # (Layout before Animation before FX), so author it in reverse to let
        # each downstream department override its upstream inputs.
        for package in reversed(plan.packages):
            for arc in package.arcs:
                prim = stage.OverridePrim(arc.target_prim)
                if not prim or not prim.GetPayloads().AddPayload(Sdf.Payload(
                    arc.asset_path, Sdf.Path(arc.source_prim)
                )):
                    raise ApkgNodeError(
                        "Could not author {} payload at {}".format(
                            package.product_name, arc.target_prim
                        )
                    )
        layer = stage.GetRootLayer()
        layer.customLayerData = {
            "bmfx:apkgBuilderId": ensure_builder_id(begin),
            "bmfx:apkgComposition": "payload",
            "bmfx:apkgCompositionSchema": "bmfx.apkg.internal-payload/3",
            "bmfx:apkgPackageCount": len(plan.packages),
            "bmfx:apkgSelectionHash": digest,
        }
        if not layer.Save():
            raise ApkgNodeError(
                "Could not save internal APKG payload layer: {}".format(
                    temporary_path
                )
            )
        # Release USD handles before the atomic rename, especially on Windows.
        del stage
        del layer
        os.replace(temporary_path, output_path)
    except Exception as exc:
        try:
            if os.path.isfile(temporary_path):
                os.unlink(temporary_path)
        except OSError:
            pass
        if isinstance(exc, ApkgNodeError):
            raise
        raise ApkgNodeError(
            "Could not write the APKG internal payload layer: {}".format(exc)
        ) from exc
    return output_path.replace("\\", "/")


def _base_input(begin):
    current = begin.input(0)
    if current is not None and not _is_generated_for(begin, current):
        begin.setUserData(BASE_INPUT_USER_DATA, current.path())
        return current
    stored = str(begin.userData(BASE_INPUT_USER_DATA) or "").strip()
    return hou.node(stored) if stored else None


def _destroy_generated_boxes(begin):
    for box in _generated_boxes(begin):
        try:
            box.removeAllItems()
            box.destroy()
        except hou.Error:
            log.warning("Could not remove generated network box %s", box)


def _cook_diagnostics(node):
    messages = []
    candidates = (node,) + tuple(node.allSubChildren())
    for candidate in candidates:
        for method_name in ("errors", "warnings"):
            method = getattr(candidate, method_name, None)
            if method is None:
                continue
            try:
                values = method() or ()
            except hou.Error:
                continue
            for value in values:
                message = "{}: {}".format(candidate.path(), value)
                if message not in messages:
                    messages.append(message)
    return tuple(messages)


def _cook_payload_node(node, package):
    try:
        node.cook(force=True)
    except hou.Error as exc:
        diagnostics = _cook_diagnostics(node)
        detail = "\n".join(diagnostics) or str(exc)
        raise ApkgNodeError(
            "{} v{:03d} payload failed to cook\n{}".format(
                package.product_name, package.version, detail
            )
        ) from exc


def _create_department_box(begin, department, nodes):
    if not nodes:
        return None
    parent = begin.parent()
    box = parent.createNetworkBox()
    try:
        _tag_generated(box, begin, "department", department)
    except AttributeError:
        pass
    box.setComment(SHOT_DEPARTMENT_LABELS[department])
    box.setColor(hou.Color((0.22, 0.27, 0.34)))
    for node in nodes:
        box.addItem(node)
    box.fitAroundContents()
    return box


def sync_payload_graph(begin, selection_data, remove_obsolete=False):
    """Compose the locked APKG selection entirely inside Begin."""
    if hou is None:
        raise ApkgNodeError("Houdini is unavailable")
    plan = build_payload_plan(selection_data)
    if plan.errors:
        raise ApkgNodeError("Payload build plan is invalid:\n{}".format(
            "\n".join(message.message for message in plan.errors)
        ))
    _validate_payload_sources(plan)
    builder_id = ensure_builder_id(begin)
    wrapper_path = _write_payload_wrapper(begin, plan)

    # Remove payload nodes created by the short-lived exposed-graph workflow.
    # The Begin boundary now owns composition and the artist network remains
    # exactly Begin -> department work -> End -> Publisher.
    base = _base_input(begin)
    generated = list(_generated_nodes(begin))
    if begin.input(0) in generated:
        begin.setInput(0, base)
    _destroy_generated_boxes(begin)
    for node in reversed(generated):
        try:
            node.destroy()
        except hou.Error:
            log.warning("Could not remove generated APKG node %s", node)

    if wrapper_path:
        begin.setUserData(PAYLOAD_WRAPPER_USER_DATA, wrapper_path)
        begin.setUserData(GRAPH_MODE_USER_DATA, PAYLOAD_GRAPH_MODE)
    else:
        begin.destroyUserData(PAYLOAD_WRAPPER_USER_DATA, must_exist=False)
        begin.destroyUserData(GRAPH_MODE_USER_DATA, must_exist=False)

    _sync_loader(begin)
    report = {
        "created": 0,
        "updated": 0,
        "unchanged": len(plan.packages),
        "obsolete": len(generated),
        "removed": len(generated),
        "departments": tuple(
            department.department for department in plan.departments
        ),
        "payloadLayer": wrapper_path,
        "warnings": tuple(message.message for message in plan.warnings),
    }
    begin.setUserData(
        "bmfx_shot_builder_report",
        json.dumps(report, sort_keys=True, separators=(",", ":")),
    )
    log.info(
        "Internal payload build %s: %d APKGs, %d exposed nodes removed",
        builder_id, len(plan.packages), len(generated),
    )
    return report


def clear_payload_graph(begin):
    """Clear internal payload composition and any obsolete generated items."""
    if hou is None:
        return 0
    base = _base_input(begin)
    generated = list(_generated_nodes(begin))
    if begin.input(0) in generated:
        begin.setInput(0, base)
    _destroy_generated_boxes(begin)
    for node in reversed(generated):
        try:
            node.destroy()
        except hou.Error:
            log.warning("Could not remove generated node %s", node.path())
    begin.destroyUserData(GRAPH_MODE_USER_DATA, must_exist=False)
    begin.destroyUserData(PAYLOAD_WRAPPER_USER_DATA, must_exist=False)
    begin.destroyUserData(BASE_INPUT_USER_DATA, must_exist=False)
    begin.destroyUserData("bmfx_shot_builder_report", must_exist=False)
    return len(generated)


def cook_payload(python_lop_node):
    """Author locked APKG entrypoints as payload arcs, never sublayers."""
    hda_node = python_lop_node.parent()
    if hda_node.parm("load_payload") is not None and not bool(
        hda_node.evalParm("load_payload")
    ):
        return
    raw = str(hda_node.evalParm("payload_spec_json") or "").strip()
    if not raw:
        return
    try:
        spec = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ApkgNodeError("Invalid APKG payload specification: {}".format(exc))
    if spec.get("schema") != "bmfx.apkg.payload/1.0":
        raise ApkgNodeError("Unsupported APKG payload specification")

    from pxr import Sdf
    stage = python_lop_node.editableStage()
    for arc in spec.get("arcs") or ():
        asset_path = str(arc.get("assetPath") or "").replace("\\", "/")
        target_prim = str(arc.get("targetPrim") or "")
        source_prim = str(arc.get("sourcePrim") or target_prim)
        if not asset_path or not Sdf.Path.IsValidPathString(target_prim):
            raise ApkgNodeError("Invalid APKG payload arc")
        prim = stage.OverridePrim(target_prim)
        prim.GetPayloads().AddPayload(Sdf.Payload(
            asset_path, Sdf.Path(source_prim)
        ))

    layer = stage.GetEditTarget().GetLayer()
    custom_data = dict(layer.customLayerData)
    custom_data.update({
        "bmfx:apkgPayload": True,
        "bmfx:apkgPackageUid": str(spec.get("packageUid") or ""),
        "bmfx:apkgRepresentationId": str(
            spec.get("representationId") or ""
        ),
        "bmfx:apkgDepartment": str(spec.get("department") or ""),
    })
    layer.customLayerData = custom_data


def create_block(kwargs=None):
    """Create a real Solaris block hull with APKG authoring semantics."""
    if hou is None:
        raise ApkgNodeError("Houdini is unavailable")
    kwargs = kwargs or {}
    parent, pane = _block_parent(kwargs)
    try:
        previous_display = parent.displayNode()
    except Exception:
        previous_display = None
    try:
        previous_current = pane.currentNode() if pane is not None else None
    except Exception:
        previous_current = None
    # Fail early when Houdini was not launched in a valid AYON task context.
    _current_context()
    node_color = hou.Color((0.95, 0.32, 0.04))
    with hou.undos.group("Create BMFX APKG Block"):
        # These native node types are what make Houdini draw its non-selectable
        # Context Options Block hull. A Network Box cannot reproduce it.
        begin_type = "bmfx_apkg_block_begin"
        end_type = "bmfx_apkg_block_end"
        missing_types = [
            type_name for type_name in (begin_type, end_type)
            if hou.nodeType(hou.lopNodeTypeCategory(), type_name) is None
        ]
        if missing_types:
            raise ApkgNodeError(
                "BMFX APKG native block DSO is not loaded ({}). Restart "
                "Houdini once after installing this addon build.".format(
                    ", ".join(missing_types)
                )
            )
        begin = parent.createNode(begin_type, "apkg_begin")
        end = parent.createNode(end_type, "apkg_end")
        publisher = create_publisher(end)
        _install_begin_interface(begin)
        _install_end_interface(end)
        sync_end_context_interface(end)
        _set_parm(begin, "layerbreak", 1)
        end.setInput(0, begin)
        try:
            origin = pane.cursorPosition()
        except Exception:
            origin = hou.Vector2(0, 0)
        # Match Houdini's compact paired-node spacing. Artists insert their
        # department contribution nodes into this wire.
        begin.setPosition(origin + hou.Vector2(0, 0.65))
        end.setPosition(origin + hou.Vector2(0, -1.05))
        publisher.setPosition(origin + hou.Vector2(0, -2.75))
        begin.setColor(node_color)
        end.setColor(node_color)
        begin.setUserData(PAIRED_END_USER_DATA, end.path())
        end.setUserData(PAIRED_BEGIN_USER_DATA, begin.path())
        begin.setUserData(LOADER_USER_DATA, begin.path())
        begin.setUserData(APKG_KIND_USER_DATA, "begin")
        end.setUserData(APKG_KIND_USER_DATA, "end")
        ensure_builder_id(begin)
        update_block_label(begin)
        refresh_build_information(begin)
        _sync_loader(begin)
        clear_block_node_flags(begin)
        if previous_display is not None:
            try:
                previous_display.setDisplayFlag(True)
            except hou.Error:
                pass
        if previous_current is not None:
            try:
                previous_current.setCurrent(True, clear_all_selected=False)
            except hou.Error:
                pass
    return begin, end, publisher


def clear_block_node_flags(node):
    """Remove Houdini selection/current/display halos from an APKG block."""
    begin = (
        hou.node(node.userData(PAIRED_BEGIN_USER_DATA) or "")
        if is_apkg_end(node) else node
    )
    if not is_apkg_begin(begin):
        return False
    end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
    loader = hou.node(begin.userData(LOADER_USER_DATA) or "")
    publisher = (
        hou.node(end.userData(PUBLISHER_USER_DATA) or "")
        if end is not None else None
    )
    for item in (begin, end, loader, publisher):
        if item is None:
            continue
        try:
            item.setSelected(False)
        except hou.Error:
            pass
        try:
            item.setCurrent(False, clear_all_selected=False)
        except hou.Error:
            pass
        try:
            item.setDisplayFlag(False)
        except hou.Error:
            pass
    return True


def _remove_legacy_backdrops(begin, end):
    """Remove old APKG backdrops without deleting any contained nodes."""
    removed = 0
    for backdrop in tuple(begin.parent().networkBoxes()):
        items = tuple(backdrop.items())
        if begin not in items and end not in items:
            continue
        # Only migrate a box belonging to this APKG pair. This avoids touching
        # an artist-created box that happens to contain one of the markers.
        comment = str(backdrop.comment() or "")
        if not comment.startswith("APKG "):
            continue
        backdrop.removeAllItems()
        backdrop.destroy()
        removed += 1
    return removed


def update_block_label(begin):
    """Store the pair label as metadata; never create a visible backdrop."""
    task = resolve_department(_current_context()["task_name"]) or str(
        _current_context()["task_name"]
    ).strip().lower()
    end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
    split = split_value(end, task) if end is not None else "main"
    cfx_kind = cfx_kind_value(end, task) if end is not None else ""
    use_split = use_split_value(end, task) if end is not None else False
    identity = (
        " · ".join(value for value in (cfx_kind, split) if value)
        if use_split else ""
    )
    label = "APKG {}".format(task.upper())
    if identity:
        label += " · {}".format(identity)
    begin.setUserData("bmfx_apkg_label", label)
    if end is not None:
        end.setUserData("bmfx_apkg_label", label)
        _remove_legacy_backdrops(begin, end)
    _sync_loader(begin)
    return label


def update_end_identity(end):
    """Refresh the block identity after changing End's split controls."""
    sync_end_context_interface(end)
    begin = hou.node(end.userData(PAIRED_BEGIN_USER_DATA) or "")
    if not is_apkg_begin(begin):
        return None
    label = update_block_label(begin)
    publisher = hou.node(end.userData(PUBLISHER_USER_DATA) or "")
    if is_apkg_publisher(publisher):
        clean_publisher_instance_interface(publisher)
        update_publisher_identity(publisher)
    return label


def upgrade_block_interfaces(begin):
    """Upgrade an existing native APKG pair without losing its selection."""
    if not is_apkg_begin(begin):
        raise ApkgNodeError("Select a BMFX APKG Begin node")
    begin = migrate_begin_to_native(begin)
    end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
    if not is_apkg_end(end):
        raise ApkgNodeError("Could not find this block's paired APKG End node")

    legacy_split = str(_eval(begin, "split", "main") or "main")
    # Always refresh the copied native-node interface. This also updates button
    # callbacks during in-place development without recreating the block.
    _install_begin_interface(begin)
    locked_selection = begin.userData(MANAGED_SELECTION_USER_DATA) or ""
    if locked_selection:
        _set_parm(begin, "selection_json", locked_selection)
    had_end_interface = end.parm("use_split") is not None
    old_use_split = int(_eval(end, "use_split", 0) or 0)
    old_split = str(_eval(end, "split", "main") or "main")
    old_cfx_kind = _eval(end, "cfx_kind", None)
    _install_end_interface(end)
    if had_end_interface:
        _set_parm(end, "use_split", old_use_split)
        _set_parm(end, "split", old_split)
        if old_cfx_kind is not None:
            _set_parm(end, "cfx_kind", old_cfx_kind)
    else:
        normalized = re.sub(
            r"[^A-Za-z0-9]+", "_", legacy_split.strip()
        ).strip("_").lower()
        if normalized and normalized != "main":
            _set_parm(end, "use_split", 1)
            _set_parm(end, "split", normalized)
    sync_end_context_interface(end)
    publisher = hou.node(end.userData(PUBLISHER_USER_DATA) or "")
    if is_apkg_publisher(publisher):
        clean_publisher_instance_interface(publisher)
    refresh_build_information(begin)
    update_block_label(begin)
    return begin, end


def compact_block(node):
    """Remove a legacy backdrop and restore compact paired-node layout."""
    if is_apkg_end(node):
        begin = hou.node(node.userData(PAIRED_BEGIN_USER_DATA) or "")
    else:
        begin = node
    if begin is None:
        raise ApkgNodeError("Could not find this block's APKG Begin node")
    end = hou.node(begin.userData(PAIRED_END_USER_DATA) or "")
    if end is None:
        raise ApkgNodeError("Could not find this block's paired APKG End node")
    with hou.undos.group("Compact BMFX APKG Block"):
        end.setPosition(begin.position() + hou.Vector2(0, -1.7))
        publisher = hou.node(end.userData(PUBLISHER_USER_DATA) or "")
        if publisher is not None:
            publisher.setPosition(end.position() + hou.Vector2(0, -1.7))
        begin.setColor(hou.Color((0.95, 0.32, 0.04)))
        end.setColor(hou.Color((0.95, 0.32, 0.04)))
        return _remove_legacy_backdrops(begin, end)


def _current_context():
    from ayon_core.pipeline import get_current_context
    context = get_current_context() or {}
    required = ("project_name", "folder_path", "task_name")
    missing = [key for key in required if not context.get(key)]
    if missing:
        raise ApkgNodeError(
            "AYON context is missing: {}".format(", ".join(missing))
        )
    return context


def _entity_label(entity, fallback="--"):
    entity = entity or {}
    return str(
        entity.get("label") or entity.get("name") or fallback
    ).strip()


def _database_begin_context():
    """Return one cached AYON database snapshot for the current shot task."""
    import ayon_api

    context = _current_context()
    cache_key = (
        context["project_name"], context["folder_path"], context["task_name"]
    )
    cached = _BEGIN_CONTEXT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    project = ayon_api.get_project(context["project_name"]) or {}
    folder = ayon_api.get_folder_by_path(
        context["project_name"], context["folder_path"]
    ) or {}
    task = {}
    if folder.get("id"):
        task = ayon_api.get_task_by_name(
            context["project_name"], folder["id"], context["task_name"]
        ) or {}

    # Resolve the sequence from database parents instead of assuming a fixed
    # path depth. The immediate parent remains a safe fallback for projects
    # whose folder type is not explicitly named "Sequence".
    sequence = None
    parent_fallback = None
    parent_id = folder.get("parentId")
    visited = set()
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = ayon_api.get_folder_by_id(
            context["project_name"], parent_id
        ) or {}
        if parent_fallback is None:
            parent_fallback = parent
        folder_type = str(parent.get("folderType") or "").strip().lower()
        if "sequence" in folder_type:
            sequence = parent
            break
        parent_id = parent.get("parentId")

    path_parts = [
        part for part in str(folder.get("path") or context["folder_path"])
        .strip("/").split("/") if part
    ]
    sequence_name = _entity_label(sequence or parent_fallback, "")
    if not sequence_name:
        sequence_name = path_parts[-2] if len(path_parts) > 1 else "--"

    task_attributes = dict(task.get("attrib") or {})
    folder_attributes = dict(folder.get("attrib") or {})
    frame_start = task_attributes.get("frameStart")
    frame_end = task_attributes.get("frameEnd")
    if frame_start is None:
        frame_start = folder_attributes.get("frameStart")
    if frame_end is None:
        frame_end = folder_attributes.get("frameEnd")
    frame_range = (
        "{} - {}".format(frame_start, frame_end)
        if frame_start is not None and frame_end is not None
        else "--"
    )

    result = {
        "show": _entity_label(project, context["project_name"]),
        "sequence": sequence_name,
        "shot": _entity_label(
            folder, path_parts[-1] if path_parts else context["folder_path"]
        ),
        "task": _entity_label(task, context["task_name"]),
        "frameRange": frame_range,
    }
    _BEGIN_CONTEXT_CACHE[cache_key] = result
    return result


def begin_context_display(_node=None):
    """Python-expression return value for Begin's AYON context display."""
    try:
        data = _database_begin_context()
    except Exception as exc:
        return "AYON database unavailable\n{}".format(exc)
    return "\n".join((
        "Show        : {}".format(data["show"]),
        "Sequence    : {}".format(data["sequence"]),
        "Shot        : {}".format(data["shot"]),
        "Task        : {}".format(data["task"]),
        "Frame Range : {}".format(data["frameRange"]),
    ))


def loaded_apkgs_display(selection_data):
    """Format every exact APKG lock for compact Begin display."""
    packages = list((selection_data or {}).get("packages") or [])
    if not packages:
        return "No APKGs loaded"
    values = [
        (
            str(item.get("department") or "unknown").title(),
            str(item.get("productName") or item.get("role") or "APKG"),
            "v{:03d}".format(int(item.get("version") or 0)),
            str(item.get("status") or "unknown").title(),
        )
        for item in packages
    ]
    index_width = max(2, len(str(len(values))))
    department_width = max(12, *(len(value[0]) for value in values))
    product_width = max(24, *(len(value[1]) for value in values))
    version_width = max(4, *(len(value[2]) for value in values))
    row_template = (
        "{{:0{}d}}  {{:<{}}} | {{:<{}}} | {{:<{}}} | {{}}"
    ).format(
        index_width, department_width, product_width, version_width
    )
    rows = [
        row_template.format(index, *value)
        for index, value in enumerate(values, 1)
    ]
    return "\n".join(rows)


def begin_loaded_apkgs_display(node):
    """Python-expression return value for Begin's locked APKG display."""
    # Evaluating this parameter establishes a Houdini dependency so changing
    # the lock dirties and refreshes this expression automatically.
    _eval(node, "selection_json")
    try:
        return loaded_apkgs_display(selection(node))
    except ApkgNodeError as exc:
        return "Invalid APKG selection: {}".format(exc)


def build_information_text(node, context=None, selection_data=None):
    """Build the persistent, read-only Begin dashboard from locked data."""
    context = context or _current_context()
    selection_data = selection_data or selection(node)
    packages = list(selection_data.get("packages") or [])
    build_mode = str(selection_data.get("buildMode") or "not built")
    locked_task = str(selection_data.get("task") or "")
    context_task = str(context.get("task_name") or "")
    context_state = "READY"
    if packages and locked_task and locked_task.lower() != context_task.lower():
        context_state = "CONTEXT MISMATCH"

    lines = [
        "SHOT BUILDER  [{}]".format(context_state),
        "Project : {}".format(context.get("project_name") or "--"),
        "Folder  : {}".format(context.get("folder_path") or "--"),
        "Task    : {}".format(context_task or "--"),
        "Mode    : {}".format(build_mode.upper()),
        "Loaded  : {} APKG{}".format(
            len(packages), "" if len(packages) == 1 else "s"
        ),
        "",
        "LOADED APKG VERSIONS",
    ]
    if not packages:
        lines.append("[NOT SELECTED] No upstream APKGs are loaded.")
        return "\n".join(lines)

    for index, item in enumerate(packages, 1):
        path = str(item.get("entrypointPath") or "")
        health = "READY" if path else "MISSING"
        version = int(item.get("version") or 0)
        lines.extend([
            "{:02d}. [{}] {} | {} | v{:03d} | {} | {}".format(
                index,
                health,
                item.get("department") or "unknown",
                item.get("productName") or item.get("role") or "APKG",
                version,
                str(item.get("status") or "unknown").upper(),
                dependency_mode_label(item.get("mode")),
            ),
            "    {}".format(path or "-- no USD entrypoint --"),
        ])
    raw_report = str(node.userData("bmfx_shot_builder_report") or "")
    try:
        report = json.loads(raw_report) if raw_report else {}
    except (TypeError, ValueError):
        report = {}
    if report:
        lines.extend([
            "",
            "PAYLOAD GRAPH",
            "Departments : {}".format(
                ", ".join(
                    SHOT_DEPARTMENT_LABELS.get(value, value.title())
                    for value in report.get("departments") or ()
                ) or "--"
            ),
            "Created     : {}".format(report.get("created", 0)),
            "Updated     : {}".format(report.get("updated", 0)),
            "Unchanged   : {}".format(report.get("unchanged", 0)),
            "Obsolete    : {}".format(report.get("obsolete", 0)),
        ])
        for warning in report.get("warnings") or ():
            lines.append("Warning     : {}".format(warning))
    return "\n".join(lines)


def refresh_build_information(node):
    """Refresh Begin's dashboard without changing its locked selection."""
    value = build_information_text(node)
    _set_parm(node, "build_information", value)
    return value


def open_shot_builder(node):
    """Open the modern Shot Builder interface for this Begin node."""
    if QtWidgets is None or hou is None or not hou.isUIAvailable():
        raise ApkgNodeError("Shot Builder requires Houdini's interactive UI")
    node, _end = upgrade_block_interfaces(node)
    from ayon_houdini.nodes.lops.apkg_ui import show_shot_builder
    return show_shot_builder(node)


def _status_priority(node):
    values = _lines(_eval(node, "status_priority", "approved,available,pending"))
    return values or ("approved", "available", "pending")


def _profile(node, task_name):
    automatic = _lines(_eval(node, "automatic_departments"))
    optional = _lines(_eval(node, "optional_departments"))
    if automatic or optional:
        return {"automatic": automatic, "optional": optional}
    department = resolve_department(task_name)
    return DEFAULT_TASK_PROFILES.get(
        department, {"automatic": (), "optional": ()}
    )


def allowed_upstream_departments(
    node, task_name, merge_current=None, include_lighting=None
):
    """Return the ordered department allow-list for this shot task."""
    profile = _profile(node, task_name)
    output = []
    for value in tuple(profile["automatic"]) + tuple(profile["optional"]):
        department = str(value or "").strip().lower()
        if department and department not in output:
            output.append(department)
    task = resolve_department(task_name) or str(
        task_name or ""
    ).strip().lower()
    if merge_current is None:
        merge_current = bool(_eval(node, "merge_current_department", 0))
    if include_lighting is None:
        include_lighting = bool(_eval(
            node, "include_downstream_lighting", 0
        ))
    if (
        task
        and merge_current
        and task not in output
    ):
        output.append(task)
    if (
        task != "lighting"
        and include_lighting
        and "lighting" not in output
    ):
        output.append("lighting")
    return tuple(output)


def filter_upstream_records(
    node, task_name, records, merge_current=None, include_lighting=None
):
    """Remove current and downstream departments from an APKG catalog."""
    allowed = set(allowed_upstream_departments(
        node, task_name,
        merge_current=merge_current,
        include_lighting=include_lighting,
    ))
    return tuple(
        record for record in records
        if resolve_department(record.department) in allowed
    )


def discover_for_node(
    node, merge_current=None, include_lighting=None
):
    import ayon_api
    from ayon_houdini.apkg.catalog import discover_packages

    context = _current_context()
    folder = ayon_api.get_folder_by_path(
        context["project_name"], context["folder_path"],
        fields={"id", "name", "path", "folderType"},
    )
    if not folder:
        raise ApkgNodeError(
            "AYON folder does not exist: {}".format(context["folder_path"])
        )
    folder_type = str(folder.get("folderType") or "").strip().lower()
    if folder_type and "shot" not in folder_type:
        raise ApkgNodeError(
            "APKG Shot Builder requires an AYON Shot folder; current folder "
            "type is '{}'".format(folder.get("folderType"))
        )
    records = discover_packages(
        context["project_name"], folder["id"], active=True
    )
    unsupported = [
        record for record in records
        if resolve_department(record.department) is None
    ]
    for record in unsupported:
        log.warning(
            "Skipping unsupported APKG department: task=%s product=%s v%03d",
            record.department or "unknown", record.product_name,
            record.version,
        )
    records = filter_upstream_records(
        node, context["task_name"], records,
        merge_current=merge_current,
        include_lighting=include_lighting,
    )
    log.info(
        "Discovered %d allowed APKG versions for %s %s",
        len(records), context["folder_path"], context["task_name"],
    )
    return context, folder, records


def _selection_item(record, mode="inherit", order=0):
    return {
        "packageUid": record.package_uid,
        "productId": record.product_id,
        "productName": record.product_name,
        "versionId": record.version_id,
        "version": record.version,
        "status": record.status,
        "department": record.department,
        "role": record.role,
        "slot": record.slot,
        "contributionType": record.contribution_type,
        "mode": mode,
        "publishThrough": mode not in {"context", "disabled"},
        "order": int(order),
        "entrypointRepresentationId": record.entrypoint_representation_id,
        "entrypointPath": record.entrypoint_path,
        "loadStrategy": "payload",
        "exports": list(record.exports),
    }


def _resolve_required_dependencies(selected, records):
    """Add exact required dependencies while preserving explicit modes."""
    by_uid = {record.package_uid: record for record in records}
    output = {item["packageUid"]: dict(item) for item in selected}
    explicit_uids = set(output)
    selected_products = {
        str(item.get("productId") or ""): item["packageUid"]
        for item in output.values()
        if item.get("productId")
    }
    queue = list(output)
    while queue:
        package_uid = queue.pop(0)
        record = by_uid.get(package_uid)
        if record is None:
            continue
        # Current-department APKGs are merged as context over the explicitly
        # selected upstream shot. Do not add their historical dependency lock
        # (for example FX v001's Animation v004) beside a newer explicit
        # selection (Animation v005).
        if output[package_uid].get("mode") == "context":
            continue
        for dependency in record.dependencies:
            if not dependency.required or dependency.mode == "disabled":
                continue
            if dependency.package_uid in output:
                continue
            dependency_record = by_uid.get(dependency.package_uid)
            if dependency_record is None:
                raise ApkgNodeError(
                    "{} requires unavailable package {}".format(
                        record.product_name or record.package_uid,
                        dependency.package_uid,
                    )
                )
            dependency_product = str(
                dependency.product_id or dependency_record.product_id or ""
            )
            selected_uid = selected_products.get(dependency_product)
            if selected_uid and selected_uid != dependency.package_uid:
                # A direct version choice in the Shot Builder intentionally
                # overrides an older transitive lock of the same APKG product.
                # Keep one version only; _validate_selection retargets the
                # in-memory graph edge so department ordering remains valid.
                if selected_uid in explicit_uids:
                    continue
                raise ApkgNodeError(
                    "Conflicting required versions of APKG product {}: {} "
                    "and {}".format(
                        dependency_product,
                        selected_uid,
                        dependency.package_uid,
                    )
                )
            parent_mode = output[package_uid].get("mode", "inherit")
            dependency_mode = (
                "context" if parent_mode == "context" else dependency.mode
            )
            output[dependency.package_uid] = _selection_item(
                dependency_record,
                mode=dependency_mode,
                order=dependency.order,
            )
            if dependency_product:
                selected_products[dependency_product] = (
                    dependency.package_uid
                )
            queue.append(dependency.package_uid)
    return list(output.values())


def _validate_selection(items, records):
    try:
        from ayon_houdini.apkg.graph import PackageGraph
    except ModuleNotFoundError:  # Direct source-tree unit tests.
        from apkg.graph import PackageGraph

    selected_uids = {item["packageUid"] for item in items}
    selected_by_product = {
        str(item.get("productId") or ""): item["packageUid"]
        for item in items
        if item.get("productId")
    }
    selected_version_ids = {
        item["packageUid"]: str(item.get("versionId") or "")
        for item in items
    }
    selected_modes = {
        item["packageUid"]: str(item.get("mode") or "inherit")
        for item in items
    }
    graph_records = []
    for record in records:
        if record.package_uid not in selected_uids:
            continue
        if selected_modes.get(record.package_uid) == "context":
            graph_records.append(replace(record, dependencies=()))
            continue
        dependencies = []
        for dependency in record.dependencies:
            replacement_uid = selected_by_product.get(
                str(dependency.product_id or "")
            )
            if (
                dependency.package_uid not in selected_uids
                and replacement_uid
                and replacement_uid != record.package_uid
            ):
                dependency = replace(
                    dependency,
                    package_uid=replacement_uid,
                    version_id=selected_version_ids.get(
                        replacement_uid, dependency.version_id
                    ),
                )
            dependencies.append(dependency)
        graph_records.append(replace(
            record, dependencies=tuple(dependencies)
        ))
    graph = PackageGraph(graph_records)
    graph.validate()

    missing_paths = [
        item["productName"] or item["packageUid"]
        for item in items
        if not item.get("entrypointPath")
    ]
    if missing_paths:
        raise ApkgNodeError(
            "APKG entrypoint is missing for: {}".format(
                ", ".join(sorted(missing_paths))
            )
        )
    by_uid = {item["packageUid"]: dict(item) for item in items}
    ordered = []
    for index, record in enumerate(graph.topological_order(), 1):
        item = by_uid[record.package_uid]
        item["order"] = index * 10
        ordered.append(item)
    return ordered


def _store_selection(node, context, folder_id, items, build_mode):
    task_name = resolve_department(context["task_name"]) or str(
        context["task_name"] or ""
    ).strip().lower()
    items = [dict(item) for item in items]
    # Current-department and downstream Lighting packages are scene context,
    # never publish-through dependencies. This prevents self-dependencies and
    # downstream-to-upstream dependency cycles.
    for item in items:
        department = str(
            item.get("department") or ""
        ).strip().lower()
        if department == task_name or (
            task_name != "lighting" and department == "lighting"
        ):
            item["mode"] = "context"
            item["publishThrough"] = False
    if selection_uses_current_context(
        {"task": task_name, "packages": items}
    ):
        # Persist the UI preference as well as recording it in the immutable
        # build lock. Reopening can recover from either source.
        _set_parm(node, "merge_current_department", 1)
    if selection_uses_downstream_lighting(
        {"task": task_name, "packages": items}
    ):
        _set_parm(node, "include_downstream_lighting", 1)
    allowed = set(allowed_upstream_departments(
        node, task_name
    ))
    # The UI may explicitly expose the current department for this build even
    # when the native Begin spare parameter has not refreshed yet. A selected
    # current-department row is always normalized to context-only above, so it
    # is safe and correct to accept it here. The toggle controls discovery;
    # the locked row controls final validation.
    if any(
        str(item.get("department") or "").strip().lower() == task_name
        and item.get("mode") == "context"
        for item in items
    ):
        allowed.add(task_name)
    if selection_uses_downstream_lighting(
        {"task": task_name, "packages": items}
    ):
        allowed.add("lighting")
    blocked = sorted({
        str(item.get("department") or "unknown").strip().lower()
        for item in items
        if str(item.get("department") or "").strip().lower() not in allowed
    })
    if blocked:
        raise ApkgNodeError(
            "{} may only load upstream APKGs from: {}. Blocked: {}".format(
                task_name,
                ", ".join(sorted(allowed)) if allowed else "none",
                ", ".join(blocked),
            )
        )
    builder_id = ensure_builder_id(node)
    ordered = sorted(
        items,
        key=lambda item: (
            int(item.get("order") or 0),
            item.get("department") or "",
            item.get("role") or "",
            item.get("slot") or "",
            item.get("packageUid") or "",
        ),
    )
    data = {
        "schema": SELECTION_SCHEMA,
        "builderId": builder_id,
        "project": context["project_name"],
        "folderId": folder_id,
        "folderPath": context["folder_path"],
        "task": context["task_name"],
        "buildMode": build_mode,
        "packages": ordered,
    }
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    previous_payload = str(_eval(node, "selection_json") or node.userData(
        MANAGED_SELECTION_USER_DATA
    ) or "")
    if previous_payload and previous_payload != payload:
        node.setUserData(PREVIOUS_SELECTION_USER_DATA, previous_payload)
    _set_parm(node, "selection_json", payload)
    node.setUserData(MANAGED_SELECTION_USER_DATA, payload)
    try:
        with _interruptable_operation("Loading APKG payloads inside Begin"):
            graph_report = sync_payload_graph(
                node,
                data,
                remove_obsolete=bool(
                    _eval(node, "remove_obsolete_apkgs", 0)
                ),
            )
    except Exception:
        _set_parm(node, "selection_json", previous_payload)
        if previous_payload:
            node.setUserData(MANAGED_SELECTION_USER_DATA, previous_payload)
        else:
            node.destroyUserData(
                MANAGED_SELECTION_USER_DATA, must_exist=False
            )
        try:
            if previous_payload:
                sync_payload_graph(node, json.loads(previous_payload))
            else:
                clear_payload_graph(node)
                _sync_loader(node)
        except Exception:
            log.exception("Could not restore the previous APKG payload build")
        raise
    summary = "Loaded {} APKG{}".format(
        len(ordered), "" if len(ordered) == 1 else "s"
    )
    _set_parm(node, "build_summary", summary)
    _set_parm(
        node, "build_information",
        build_information_text(node, context=context, selection_data=data),
    )
    details = [summary]
    details.extend(
        "{} · v{:03d} · {} · {}".format(
            item.get("productName") or item.get("role") or "APKG",
            int(item.get("version") or 0),
            item.get("status") or "unknown",
            item.get("mode") or "inherit",
        )
        for item in ordered
    )
    node.setComment("\n".join(details))
    node.setGenericFlag(hou.nodeFlag.DisplayComment, bool(ordered))
    _sync_viewport_payloads(node, ordered)
    node.cook(force=True)
    if hou is not None and hou.isUIAvailable():
        hou.ui.setStatusMessage(
            "APKG payload build: {} created, {} updated, {} unchanged".format(
                graph_report["created"], graph_report["updated"],
                graph_report["unchanged"],
            ),
            severity=hou.severityType.Message,
        )
    return data


def rollback_build(node):
    """Restore the immediately previous exact APKG selection and graph."""
    raw = str(node.userData(PREVIOUS_SELECTION_USER_DATA) or "").strip()
    if not raw:
        raise ApkgNodeError("No previous APKG build is available")
    try:
        previous = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ApkgNodeError("Previous APKG build lock is invalid: {}".format(exc))
    if previous.get("schema") != SELECTION_SCHEMA:
        raise ApkgNodeError("Previous APKG build uses an unsupported schema")
    context = _current_context()
    if (
        previous.get("project") != context["project_name"]
        or previous.get("folderPath") != context["folder_path"]
    ):
        raise ApkgNodeError(
            "Previous APKG build belongs to another AYON shot"
        )
    with hou.undos.group("Rollback BMFX APKG Shot"):
        return _store_selection(
            node,
            context,
            previous.get("folderId") or "",
            previous.get("packages") or [],
            "rollback",
        )


def selection(node):
    raw = str(_eval(node, "selection_json") or node.userData(
        MANAGED_SELECTION_USER_DATA
    ) or "").strip()
    if not raw:
        return {"schema": SELECTION_SCHEMA, "packages": []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ApkgNodeError("Stored APKG selection is invalid: {}".format(exc))
    if data.get("schema") != SELECTION_SCHEMA:
        raise ApkgNodeError("Unsupported APKG selection schema")
    return data


def _selection_item_from_resolved(package, mode="inherit", order=0):
    """Convert a Browser resolver result into a locked Begin selection."""
    summary = (package.get("version_data") or {}).get("bmfxApkg") or {}
    entrypoint_id = str(summary.get("entrypointRepresentationId") or "")
    entrypoint = next((
        resource for resource in package.get("representations") or []
        if str(resource.get("id") or "") == entrypoint_id
    ), None)
    if entrypoint is None:
        entrypoint = next((
            resource for resource in package.get("representations") or []
            if str(resource.get("name") or "").lower() == "usd"
            or str(resource.get("extension") or "").lower()
            in {"usd", "usda", "usdc"}
        ), {})
    paths = entrypoint.get("paths") or []
    normalized_mode = {
        "reference": "inherit",
        "import": "inherit",
    }.get(str(mode).lower(), str(mode).lower())
    if normalized_mode not in {
        "inherit", "context", "optional", "overlay", "replace", "disabled"
    }:
        raise ApkgNodeError("Unsupported APKG load mode: {}".format(mode))
    return {
        "packageUid": str(package.get("package_uid") or ""),
        "productId": str(package.get("product_id") or ""),
        "productName": str(package.get("product_name") or "APKG"),
        "versionId": str(package.get("version_id") or ""),
        "version": int(package.get("version") or 0),
        "status": str(package.get("status") or ""),
        "department": str(summary.get("department") or "").lower(),
        "role": str(summary.get("role") or ""),
        "slot": str(summary.get("slot") or ""),
        "contributionType": str(
            summary.get("contributionType") or summary.get("effectType") or ""
        ),
        "mode": normalized_mode,
        "publishThrough": normalized_mode not in {"context", "disabled"},
        "order": int(order),
        "entrypointRepresentationId": str(entrypoint.get("id") or ""),
        "entrypointPath": str(paths[0] if paths else ""),
        "loadStrategy": "payload",
        "exports": list((summary.get("interface") or {}).get("exports") or ()),
    }


def find_or_create_begin():
    """Return the unambiguous APKG Begin in /stage, creating one if absent."""
    if hou is None:
        raise ApkgNodeError("Houdini is unavailable")
    stage = hou.node("/stage")
    if stage is None:
        raise ApkgNodeError("The Houdini scene has no /stage network")
    selected = [
        node for node in hou.selectedNodes()
        if is_apkg_begin(node)
    ]
    if len(selected) == 1:
        upgrade_block_interfaces(selected[0])
        return selected[0]
    candidates = [
        node for node in stage.allSubChildren()
        if is_apkg_begin(node)
    ]
    if len(candidates) > 1:
        raise ApkgNodeError(
            "Multiple APKG Begin nodes exist. Select the target Begin and retry."
        )
    if candidates:
        upgrade_block_interfaces(candidates[0])
        return candidates[0]
    begin, _end, _publisher = create_block({})
    return begin


def add_resolved_packages(node, packages):
    """Add Browser-resolved APKGs without performing another AYON query.

    ``packages`` contains ``(resolver_result, dependency_mode, order)`` rows.
    Existing versions of the same product are atomically replaced.
    """
    if node.parm("build_information") is None or node.parm("split") is not None:
        upgrade_block_interfaces(node)
    context = _current_context()
    current = selection(node)
    current_project = current.get("project") or context["project_name"]
    if any(package.get("project_name") != current_project
           for package, _mode, _order in packages):
        raise ApkgNodeError("APKGs from another project cannot enter this builder")
    folder_ids = {
        str(package.get("folder_id") or "")
        for package, _mode, _order in packages
        if package.get("folder_id")
    }
    existing_folder_id = str(current.get("folderId") or "")
    if existing_folder_id and folder_ids - {existing_folder_id}:
        raise ApkgNodeError("APKGs from another shot cannot enter this builder")

    items = list(current.get("packages") or [])
    for package, mode, order in packages:
        item = _selection_item_from_resolved(package, mode, order)
        if not item["packageUid"] or not item["entrypointPath"]:
            raise ApkgNodeError(
                "{} has no resolvable APKG USD entrypoint".format(
                    item["productName"]
                )
            )
        contribution = (
            item["productId"] or
            "{}:{}:{}".format(
                item["department"], item["role"], item["slot"]
            )
        )
        items = [
            value for value in items
            if (value.get("productId") or "{}:{}:{}".format(
                value.get("department", ""), value.get("role", ""),
                value.get("slot", "")
            )) != contribution
        ]
        items.append(item)
    folder_id = existing_folder_id or next(iter(folder_ids), "")
    with hou.undos.group("Load BMFX APKG from Browser"):
        return _store_selection(node, context, folder_id, items, "browser")


def clear_all(node):
    """Remove only this Begin node's managed package selection."""
    if node.parm("build_information") is None or node.parm("split") is not None:
        upgrade_block_interfaces(node)
    count = len(selection(node).get("packages") or [])
    if count and hou is not None and hou.isUIAvailable():
        choice = hou.ui.displayMessage(
            "Remove {} APKG{} from this Shot Builder?\n\n"
            "Published files and artist-authored nodes will not be removed.".format(
                count, "" if count == 1 else "s"
            ),
            buttons=("Clear All", "Cancel"),
            default_choice=1,
            close_choice=1,
            severity=hou.severityType.Warning,
        )
        if choice != 0:
            return False
    with hou.undos.group("Clear BMFX APKG Shot Builder"):
        current_payload = str(_eval(node, "selection_json") or node.userData(
            MANAGED_SELECTION_USER_DATA
        ) or "")
        if current_payload:
            node.setUserData(PREVIOUS_SELECTION_USER_DATA, current_payload)
        removed = clear_payload_graph(node)
        _set_parm(node, "selection_json", "")
        _set_parm(node, "build_summary", "No APKGs loaded")
        node.destroyUserData(
            MANAGED_SELECTION_USER_DATA, must_exist=False
        )
        _sync_viewport_payloads(node, ())
        node.destroyUserData(
            MANAGED_VIEWPORT_LOAD_PATHS_USER_DATA, must_exist=False
        )
        node.setComment("")
        node.setGenericFlag(hou.nodeFlag.DisplayComment, False)
        refresh_build_information(node)
        _sync_loader(node)
        node.cook(force=True)
        log.info("Cleared %d generated APKG shot-builder nodes", removed)
    return True


def build_automatic(node):
    from ayon_houdini.apkg.status import select_preferred_versions

    if node.parm("build_information") is None or node.parm("split") is not None:
        upgrade_block_interfaces(node)
    context, folder, records = discover_for_node(node)
    profile = _profile(node, context["task_name"])
    departments = set(profile["automatic"])
    merge_current = bool(_eval(node, "merge_current_department", 0))
    current_department = resolve_department(context["task_name"]) or str(
        context["task_name"]
    ).strip().lower()
    candidates = [
        record for record in records
        if record.department in departments
        or (merge_current and record.department == current_department)
    ]
    preferred = select_preferred_versions(candidates, _status_priority(node))
    items = [
        _selection_item(
            record,
            mode=(
                "context"
                if record.department == current_department
                else "inherit"
            ),
            order=index * 10,
        )
        for index, record in enumerate(preferred.values(), 1)
    ]
    items = _resolve_required_dependencies(items, records)
    items = _validate_selection(items, records)
    with hou.undos.group("Build BMFX APKG Shot"):
        return _store_selection(node, context, folder["id"], items, "automatic")


if QtWidgets is not None:
    class PackageSelectionDialog(QtWidgets.QDialog):
        """Compact Browser-compatible APKG version selection dialog."""

        HEADERS = (
            "Load", "Department", "Package", "Version", "Status", "Mode",
            "Author",
        )

        def __init__(
            self, records, recommended, optional_departments,
            current_selection=(), parent=None,
        ):
            super().__init__(parent)
            self.setWindowTitle("Build BMFX APKG Shot")
            self.resize(1050, 650)
            self._records = tuple(records)
            self._recommended = recommended
            self._optional_departments = set(optional_departments)
            self._current_by_key = {
                (
                    item.get("productId") or
                    "{}:{}:{}".format(
                        item.get("department", ""), item.get("role", ""),
                        item.get("slot", ""),
                    )
                ): item
                for item in current_selection
            }
            self._rows = []

            layout = QtWidgets.QVBoxLayout(self)
            filter_layout = QtWidgets.QHBoxLayout()
            self.search = QtWidgets.QLineEdit()
            self.search.setPlaceholderText(
                "Search package, role, slot, artist or department"
            )
            self.department = QtWidgets.QComboBox()
            self.department.addItem("All departments", "")
            for value in sort_departments(
                record.department for record in records
            ):
                self.department.addItem(value.title(), value)
            filter_layout.addWidget(self.search, 1)
            filter_layout.addWidget(self.department)
            layout.addLayout(filter_layout)

            self.table = QtWidgets.QTableWidget(0, len(self.HEADERS))
            self.table.setHorizontalHeaderLabels(self.HEADERS)
            self.table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows
            )
            self.table.setAlternatingRowColors(True)
            self.table.verticalHeader().setVisible(False)
            layout.addWidget(self.table, 1)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Cancel
                | QtWidgets.QDialogButtonBox.Ok
            )
            buttons.button(QtWidgets.QDialogButtonBox.Ok).setText(
                "Build Selected"
            )
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self.search.textChanged.connect(self._filter)
            self.department.currentIndexChanged.connect(self._filter)
            self._populate()

        def _populate(self):
            grouped = defaultdict(list)
            for record in self._records:
                grouped[record.contribution_key].append(record)
            for key in sorted(
                grouped,
                key=lambda value: (
                    department_index(grouped[value][0].department),
                    (grouped[value][0].product_name or value).lower(),
                    value,
                ),
            ):
                versions = sorted(
                    grouped[key], key=lambda item: item.version, reverse=True
                )
                recommended = self._recommended.get(key)
                locked = self._current_by_key.get(key)
                current = next((
                    version for version in versions
                    if locked and version.version_id == locked.get("versionId")
                ), None) or recommended or versions[0]
                row = self.table.rowCount()
                self.table.insertRow(row)

                check = QtWidgets.QTableWidgetItem()
                check.setFlags(check.flags() | QtCore.Qt.ItemIsUserCheckable)
                check.setCheckState(
                    QtCore.Qt.Checked
                    if locked or recommended else QtCore.Qt.Unchecked
                )
                self.table.setItem(row, 0, check)
                for column, text in (
                    (1, current.department.title()),
                    (2, current.product_name or current.role),
                    (4, current.status.title()),
                    (6, current.author),
                ):
                    self.table.setItem(row, column, QtWidgets.QTableWidgetItem(text))

                version_combo = QtWidgets.QComboBox()
                for version in versions:
                    version_combo.addItem(
                        "v{:03d} — {}".format(version.version, version.status.title()),
                        version.version_id,
                    )
                selected_index = next(
                    (
                        index for index, version in enumerate(versions)
                        if version.version_id == current.version_id
                    ),
                    0,
                )
                version_combo.setCurrentIndex(selected_index)
                version_combo.currentIndexChanged.connect(
                    lambda _index, r=row: self._version_changed(r)
                )
                self.table.setCellWidget(row, 3, version_combo)

                mode = QtWidgets.QComboBox()
                mode.addItems(("inherit", "context", "optional"))
                if locked and locked.get("mode") in {
                    "inherit", "context", "optional"
                }:
                    mode.setCurrentText(locked["mode"])
                elif current.department in self._optional_departments:
                    mode.setCurrentText("context")
                self.table.setCellWidget(row, 5, mode)
                self._rows.append((versions, check, version_combo, mode))

            header = self.table.horizontalHeader()
            header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
            for column in (0, 1, 3, 4, 5, 6):
                header.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeToContents
                )

        def _version_changed(self, row):
            versions, _check, combo, _mode = self._rows[row]
            version_id = combo.currentData()
            record = next(item for item in versions if item.version_id == version_id)
            self.table.item(row, 4).setText(record.status.title())
            self.table.item(row, 6).setText(record.author)

        def _filter(self):
            search = self.search.text().strip().lower()
            department = self.department.currentData()
            for row, (versions, _check, _combo, _mode) in enumerate(self._rows):
                record = versions[0]
                haystack = " ".join((
                    record.department, record.product_name, record.role,
                    record.slot, record.author,
                )).lower()
                visible = (not search or search in haystack) and (
                    not department or department == record.department
                )
                self.table.setRowHidden(row, not visible)

        def selected_items(self):
            output = []
            for order, (versions, check, combo, mode) in enumerate(self._rows, 1):
                if check.checkState() != QtCore.Qt.Checked:
                    continue
                version_id = combo.currentData()
                record = next(
                    item for item in versions if item.version_id == version_id
                )
                output.append(_selection_item(
                    record, mode=mode.currentText(), order=order * 10
                ))
            return output


def build_manual(node):
    if QtWidgets is None or hou is None or not hou.isUIAvailable():
        raise ApkgNodeError("Build Manual requires Houdini's interactive UI")
    from ayon_houdini.apkg.status import select_preferred_versions
    from ayon_houdini.api import lib

    if node.parm("build_information") is None or node.parm("split") is not None:
        upgrade_block_interfaces(node)
    context, folder, records = discover_for_node(node)
    profile = _profile(node, context["task_name"])
    # Discovery has already applied the strict department policy. Manual mode
    # only chooses versions from that allowed catalog.
    candidates = list(records)
    merge_current = bool(_eval(node, "merge_current_department", 0))
    current_department = resolve_department(context["task_name"]) or str(
        context["task_name"]
    ).strip().lower()
    recommended_candidates = [
        record for record in candidates
        if record.department in set(profile["automatic"])
        or (merge_current and record.department == current_department)
    ]
    recommended = select_preferred_versions(
        recommended_candidates, _status_priority(node)
    )
    optional_departments = list(profile["optional"])
    if bool(_eval(node, "merge_current_department", 0)):
        optional_departments.append(current_department)
    if bool(_eval(node, "include_downstream_lighting", 0)):
        optional_departments.append("lighting")
    dialog = PackageSelectionDialog(
        candidates,
        recommended,
        optional_departments,
        current_selection=selection(node).get("packages") or [],
        parent=lib.get_main_window(),
    )
    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return None
    items = _resolve_required_dependencies(dialog.selected_items(), records)
    items = _validate_selection(items, records)
    with hou.undos.group("Build BMFX APKG Shot Manually"):
        return _store_selection(node, context, folder["id"], items, "manual")


def composition_paths(selection_data):
    """Return USD sublayers in strongest-to-weakest composition order."""
    dependency_first = [
        item["entrypointPath"].replace("\\", "/")
        for item in selection_data.get("packages") or []
        if item.get("entrypointPath") and item.get("mode") != "disabled"
    ]
    # Stored order is topological (dependencies before consumers). USD
    # subLayerPaths are strongest-to-weakest, so consumers must be reversed
    # ahead of the packages they are allowed to override.
    return list(reversed(dependency_first))


def cook_begin(python_lop_node):
    """Legacy Begin HDA cook using the same internal payload composition."""
    hda_node = python_lop_node.parent()
    data = selection(hda_node)
    if hda_node.userData(GRAPH_MODE_USER_DATA) == PAYLOAD_GRAPH_MODE:
        wrapper = str(
            hda_node.userData(PAYLOAD_WRAPPER_USER_DATA) or ""
        ).strip()
        paths = [wrapper] if wrapper else []
    else:
        paths = composition_paths(data)
    stage = python_lop_node.editableStage()
    layer = stage.GetEditTarget().GetLayer()
    layer.subLayerPaths = paths
    custom_data = dict(layer.customLayerData)
    custom_data.update({
        "bmfx:apkgBuilderId": data.get("builderId", ""),
        "bmfx:apkgSelectionSchema": data.get("schema", SELECTION_SCHEMA),
        "bmfx:apkgPackageCount": len(paths),
    })
    layer.customLayerData = custom_data


def _find_begin(end_node):
    # Current APKG blocks have an explicit, stable boundary link. Prefer it
    # over cook-graph traversal because native Context Options blocks can
    # alter which ancestors Houdini reports during evaluation.
    begin = hou.node(end_node.userData(PAIRED_BEGIN_USER_DATA) or "")
    if is_apkg_begin(begin):
        return begin

    # Legacy block fallback for scenes created before explicit pairing.
    candidates = []
    for ancestor in end_node.inputAncestors(
        include_ref_inputs=True, follow_subnets=True
    ):
        type_name = ancestor.type().name().split("::", 1)[0]
        if type_name == "bmfx_apkg_begin" or ancestor.parm("selection_json"):
            candidates.append(ancestor)
    # Prefer the closest ancestor returned by Houdini.
    if not candidates:
        raise ApkgNodeError("APKG End is not connected to an APKG Begin")
    return candidates[0]


def end_data(end_node):
    begin = _find_begin(end_node)
    selected = selection(begin)
    context = _current_context()
    task = resolve_department(context["task_name"]) or str(
        context["task_name"]
    ).strip().lower()
    locked_task = str(selected.get("task") or "").strip().lower()
    if selected.get("packages") and locked_task and locked_task != task:
        raise ApkgNodeError(
            "This APKG Block was built in task '{}', but Houdini is running "
            "in task '{}'. Clear or rebuild the block.".format(
                locked_task, task
            )
        )
    split = split_value(end_node, task)
    use_split = use_split_value(end_node, task)
    cfx_kind = cfx_kind_value(end_node, task)
    contribution_type = cfx_kind or split
    role_parts = [task]
    if cfx_kind:
        role_parts.append(cfx_kind)
    role_parts.append(split)
    return {
        "beginNode": begin.path(),
        "builderId": ensure_builder_id(begin),
        "department": task,
        "task": task,
        "split": split,
        "useSplit": use_split,
        "classification": cfx_kind,
        "effectType": contribution_type,
        "slot": split,
        "role": ".".join(role_parts),
        "exports": (),
        "requires": (),
        "capabilities": (
            "provides:{}".format(task),
            "split:{}".format(split),
            *(
                ("classification:{}".format(cfx_kind),)
                if cfx_kind else ()
            ),
        ),
        "selection": selected,
    }


def _layers_below_break(end_node, stage):
    try:
        inherited = set(end_node.layersAboveLayerBreak(
            use_last_cook_context_options=False
        ))
    except TypeError:
        inherited = set(end_node.layersAboveLayerBreak())
    return tuple(
        layer for layer in stage.GetLayerStack(includeSessionLayers=False)
        if layer.identifier not in inherited
    )


def _authored_prim_paths(layers):
    output = set()
    for layer in layers:
        stack = list(getattr(layer, "rootPrims", ()) or ())
        while stack:
            spec = stack.pop()
            path = str(spec.path)
            if path and path != "/HoudiniLayerInfo":
                output.add(path)
            children = getattr(spec, "nameChildren", {})
            try:
                stack.extend(children.values())
            except AttributeError:
                stack.extend(children)
    return output


def _derived_exports(stage, layers, task, split):
    authored = _authored_prim_paths(layers)
    expected = "/World/{}/{}".format(task, split)
    if any(
        path == expected
        or path.startswith(expected + "/")
        or expected.startswith(path + "/")
        for path in authored if path != "/World"
    ) and stage.GetPrimAtPath(expected):
        return (expected,)

    # Prefer defining prim specs over structural ancestor overs. A payload or
    # component such as /World/Shot/env/Gnd is department-owned; the /World,
    # /World/Shot and /World/Shot/env overs needed to reach it are not.
    defining = set()
    stack = []
    for layer in layers:
        stack.extend(getattr(layer, "rootPrims", ()))
    while stack:
        spec = stack.pop()
        path = str(getattr(spec, "path", ""))
        specifier = str(getattr(spec, "specifier", "")).lower()
        type_name = str(getattr(spec, "typeName", "")).lower()
        if (
            path
            and path != "/HoudiniLayerInfo"
            and "def" in specifier
            and type_name != "scope"
            and stage.GetPrimAtPath(path)
        ):
            defining.add(path)
        children = getattr(spec, "nameChildren", {})
        try:
            stack.extend(children.values())
        except AttributeError:
            stack.extend(children)
    if defining:
        roots = []
        for path in sorted(defining, key=lambda item: (item.count("/"), item)):
            if not any(
                path == root or path.startswith(root + "/") for root in roots
            ):
                roots.append(path)
        return tuple(roots)

    # Override-only contributions (commonly Animation) author ``over`` specs
    # down to the actual component. Shared ancestors such as /World and
    # /World/Shot are traversal scaffolding, so export only authored leaves.
    candidates = {
        path for path in authored
        if path not in {"/", "/World", "/Render", "/HoudiniLayerInfo"}
        and not any(
            other != path and other.startswith(path.rstrip("/") + "/")
            for other in authored
        )
    }
    return tuple(sorted(
        path for path in candidates if stage.GetPrimAtPath(path)
    ))


def _local_external_resources(layers, selection_data):
    """Return assets authored below Begin's Layer Break.

    This records VDB, BGEO, Alembic, texture and USD dependencies referenced
    by the department contribution without treating inherited layers as owned
    resources.
    """
    selected_paths = {
        os.path.normpath(item.get("entrypointPath") or "")
        for item in selection_data.get("packages") or []
        if item.get("entrypointPath")
    }
    context_paths = {
        os.path.normpath(item.get("entrypointPath") or "")
        for item in selection_data.get("packages") or []
        if item.get("entrypointPath") and item.get("mode") == "context"
    }
    output = set()
    for layer in layers:
        references = getattr(layer, "externalReferences", None)
        if references is None:
            references = layer.GetExternalReferences()
        for reference in references or ():
            if not reference or str(reference).startswith("anon:"):
                continue
            try:
                resolved = layer.ComputeAbsolutePath(reference)
            except Exception:
                resolved = str(reference)
            normalized = os.path.normpath(str(resolved))
            if normalized in context_paths:
                raise ApkgNodeError(
                    "Context-only APKG was referenced below the Layer Break: "
                    "{}".format(resolved)
                )
            if normalized not in selected_paths:
                output.add(str(resolved).replace("\\", "/"))
    return tuple(sorted(output))


def validate_end(end_node, show_message=True):
    errors = []
    try:
        data = end_data(end_node)
    except ApkgNodeError as exc:
        # Identity/connectivity failures make all later stage-interface checks
        # invalid. Preserve and report the original actionable error instead
        # of continuing with a partial dictionary.
        message = str(exc)
        _set_parm(end_node, "validation_log", message)
        if show_message and hou is not None and hou.isUIAvailable():
            hou.ui.displayMessage(
                message, severity=hou.severityType.Error
            )
        raise ApkgNodeError(message) from exc
    for key in ("department", "effectType", "slot", "role"):
        if not data.get(key):
            errors.append("{} is required".format(key))

    stage = end_node.stage(apply_viewport_overrides=False)
    if stage is None:
        errors.append("APKG End has no valid USD stage")
        layers = ()
    else:
        layers = _layers_below_break(end_node, stage)
        data["exports"] = _derived_exports(
            stage, layers, data["task"], data["split"]
        )
    exports = data.get("exports") or ()
    requires = data.get("requires") or ()
    if stage is not None:
        if not exports:
            errors.append(
                "No USD prims were authored inside the APKG block"
            )
        for path in exports:
            if not stage.GetPrimAtPath(path):
                errors.append("Export prim does not exist: {}".format(path))
        for path in requires:
            if not stage.GetPrimAtPath(path):
                errors.append("Required upstream prim does not exist: {}".format(path))
    if stage is not None and data:
        try:
            data["externalResources"] = _local_external_resources(
                layers, data["selection"]
            )
        except ApkgNodeError as exc:
            errors.append(str(exc))

    message = "APKG is valid" if not errors else "\n".join(errors)
    _set_parm(end_node, "validation_log", message)
    if show_message and hou is not None and hou.isUIAvailable():
        hou.ui.displayMessage(
            message,
            severity=(
                hou.severityType.Message if not errors
                else hou.severityType.Error
            ),
        )
    if errors:
        raise ApkgNodeError(message)
    return data


def cook_end(python_lop_node):
    hda_node = python_lop_node.parent()
    try:
        data = end_data(hda_node)
    except ApkgNodeError:
        return
    stage = python_lop_node.editableStage()
    layer = stage.GetEditTarget().GetLayer()
    custom_data = dict(layer.customLayerData)
    custom_data.update({
        "bmfx:apkgBlockId": data["builderId"],
        "bmfx:apkgDepartment": data["department"],
        "bmfx:apkgSplit": data["split"],
        "bmfx:apkgRole": data["role"],
        "bmfx:apkgSlot": data["slot"],
        "bmfx:apkgProductHint": "{}.{}".format(
            data["task"], data["split"]
        ),
        "bmfx:apkgExports": list(data["exports"]),
    })
    layer.customLayerData = custom_data


def cook_publish(python_lop_node):
    """Pass the APKG stage through and identify the dedicated publish node."""
    stage = python_lop_node.editableStage()
    layer = stage.GetEditTarget().GetLayer()
    custom_data = dict(layer.customLayerData)
    custom_data["bmfx:apkgPublisher"] = True
    layer.customLayerData = custom_data
