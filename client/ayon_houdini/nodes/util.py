import importlib
import re

import hou
import ayon_api

from ayon_core.pipeline import get_current_context
from ayon_houdini.nodes import asset as ayon_assets


# ---------------------------------------------------------
# LOW-LEVEL UTIL
# ---------------------------------------------------------

_DEFAULT_TASK_ORDER = ["modeling", "rigging", "groom", "lookdev"]


def _get_asset_module(reload_module=False):
    """Return asset module, optionally reloaded."""
    if not reload_module:
        return ayon_assets

    try:
        return importlib.reload(ayon_assets)
    except Exception:
        return ayon_assets


def _get_asset_builder_module():
    """Return module that provides gather_* build functions."""
    asset_module = _get_asset_module(reload_module=True)
    if hasattr(asset_module, "gather_single_asset"):
        return asset_module

    try:
        from ayon_houdini.nodes import build_asset as legacy_build_asset
        legacy_build_asset = importlib.reload(legacy_build_asset)
        if hasattr(legacy_build_asset, "gather_single_asset"):
            return legacy_build_asset
    except Exception:
        pass

    return asset_module

def rebuild_menu(parm):
    """Force Houdini to rebuild a dynamic menu and return tokens."""
    if not parm:
        return []
    try:
        return parm.menuItems()
    except hou.OperationFailed:
        return []


def _set_first_menu_item(parm):
    """Set parm to first available menu token and return it."""
    items = rebuild_menu(parm)
    if not items:
        return None
    parm.set(items[0])
    return items[0]


def _node_from_arg(node_or_kwargs):
    """Return node from either node or kwargs dict."""
    if isinstance(node_or_kwargs, dict):
        return node_or_kwargs.get("node")
    return node_or_kwargs


def get_latest_version_token(node):
    """Resolve latest version token (v###) for current node selection."""
    asset_module = _get_asset_module()
    category = node.evalParm("category") if node.parm("category") else ""
    asset_name = node.evalParm("asset") if node.parm("asset") else ""
    department = node.evalParm("department") if node.parm("department") else ""

    if not all((category, asset_name, department)):
        return None

    return asset_module.get_latest_version_token(
        category,
        asset_name,
        department,
    )


def sync_version_to_latest(node):
    """Set `version` parm to latest available token for current selection."""
    version_parm = node.parm("version")
    if not version_parm:
        return None

    token = get_latest_version_token(node)
    if token:
        version_parm.set(token)
        node.cook(force=True)
        return token

    version_parm.set("")
    menu_token = _set_first_menu_item(version_parm)
    node.cook(force=True)
    return menu_token


# ---------------------------------------------------------
# IMPORT
# ---------------------------------------------------------

def run_import(node):
    """Synchronize UI and build a single asset import network."""
    category = node.evalParm("category") if node.parm("category") else ""
    asset_parm = node.parm("asset")

    if not asset_parm:
        hou.ui.displayMessage(
            "Asset parameter is missing on this node.",
            severity=hou.severityType.Warning,
        )
        return

    asset_name = asset_parm.eval()
    if not asset_name:
        _set_first_menu_item(asset_parm)
        asset_name = asset_parm.eval()

    if not category or not asset_name:
        hou.ui.displayMessage(
            "Selection incomplete. Please select Category and Asset.",
            severity=hou.severityType.Warning,
        )
        return

    try:
        cb_import_asset_import(node)
        print(f"[AYON] Import triggered for asset: {asset_name}")
    except Exception as exc:
        print(f"[AYON] Build Error: {exc}")


# ---------------------------------------------------------
# CALLBACKS
# ---------------------------------------------------------

def on_category_changed(node):
    """Category -> Asset -> Department -> Version token chain."""
    asset_module = _get_asset_module()
    category = node.evalParm("category") if node.parm("category") else ""
    assets_by_category = asset_module.get_assets_by_category()

    asset_parm = node.parm("asset")
    dept_parm = node.parm("department")
    version_parm = node.parm("version")

    current_asset = asset_parm.eval() if asset_parm else ""
    if dept_parm:
        dept_parm.set("")
    if version_parm:
        version_parm.set("")

    if asset_parm:
        category_assets = _get_assets_for_category(assets_by_category, category)
        category_assets = sorted(
            category_assets,
            key=lambda asset: (asset.get("label") or asset["name"]).lower(),
        )
        available_assets = [asset["name"] for asset in category_assets]

        if current_asset in available_assets:
            asset_parm.set(current_asset)
        elif available_assets:
            asset_parm.set(available_assets[0])
        else:
            asset_parm.set("")

    if dept_parm:
        _set_first_menu_item(dept_parm)

    sync_version_to_latest(node)


def on_asset_changed(node):
    """Asset change should refresh department and auto-pick latest version."""
    dept_parm = node.parm("department")
    if dept_parm:
        _set_first_menu_item(dept_parm)

    sync_version_to_latest(node)


def on_department_changed(node):
    """Department change should auto-pick latest version token."""
    sync_version_to_latest(node)


def force_refresh_hda(node):
    """Forces full parameter refresh on import layer controls."""
    for name in ("category", "asset", "department", "version"):
        parm = node.parm(name)
        if parm:
            rebuild_menu(parm)


def get_current_ayon_folder_label():
    """Return current AYON folder display name for active context."""
    ctx = get_current_context()
    project_name = ctx.get("project_name")
    folder_path = ctx.get("folder_path")

    if not project_name or not folder_path:
        raise RuntimeError("AYON context not available.")

    folder_entity = ayon_api.get_folder_by_path(project_name, folder_path)
    if not folder_entity:
        raise RuntimeError("Folder entity not found in AYON.")

    return folder_entity.get("label") or folder_entity.get("name")


# ---------------------------------------------------------
# MENU HELPERS
# ---------------------------------------------------------

def _flatten_menu(items):
    """Convert [(token, label), ...] to Houdini menu flat list."""
    result = []
    for token, label in items:
        result.extend([token, label])
    return result


def _extract_index_from_name(parm_name, prefixes):
    """Return trailing numeric index from parm name for given prefixes."""
    for prefix in prefixes:
        match = re.match(rf"^{re.escape(prefix)}(\d+)$", parm_name or "")
        if match:
            return match.group(1)
    return None


def _get_multiparm_index(kwargs, prefixes):
    """Resolve multiparm index from callback/menu kwargs."""
    parm = kwargs.get("parm")
    if parm:
        index = _extract_index_from_name(parm.name(), prefixes)
        if index:
            return index

    raw_index = kwargs.get("script_multiparm_index")
    if raw_index is None:
        return None

    try:
        return str(int(raw_index))
    except Exception:
        match = re.search(r"\d+", str(raw_index))
        return match.group(0) if match else None


def _get_first_existing_parm(node, names):
    """Return first existing parm from provided parm names."""
    for name in names:
        parm = node.parm(name)
        if parm:
            return parm
    return None


def _get_assets_for_category(assets_by_category, category):
    """Return assets for category with case-insensitive fallback."""
    category_key = _resolve_category_token(assets_by_category, category)
    if category_key is not None:
        return assets_by_category.get(category_key, [])
    return []


def _resolve_category_token(assets_by_category, category):
    """Return canonical category token with case-insensitive fallback."""
    if category in assets_by_category:
        return category

    category_l = (category or "").strip().lower()
    if not category_l:
        return None

    for key in assets_by_category:
        if str(key).strip().lower() == category_l:
            return key
    return None


def _sync_single_asset_from_category(node, asset_module=None):
    """Ensure single-node category/asset parms have valid tokens."""
    if not node:
        return

    asset_module = asset_module or _get_asset_module()
    category_parm = node.parm("category")
    asset_parm = node.parm("asset")
    if not category_parm or not asset_parm:
        return

    assets_by_category = asset_module.get_assets_by_category()
    categories = sorted(assets_by_category.keys())

    current_category = category_parm.eval()
    current_asset = asset_parm.eval()
    category_key = _resolve_category_token(assets_by_category, current_category)
    if category_key is None and categories:
        category_key = categories[0]
        if current_category != category_key:
            category_parm.set(category_key)

    category_assets = _get_assets_for_category(assets_by_category, category_key or "")
    category_assets = sorted(
        category_assets,
        key=lambda asset: (asset.get("label") or asset["name"]).lower(),
    )
    available_assets = [asset["name"] for asset in category_assets]

    if current_asset in available_assets:
        return
    if available_assets:
        asset_parm.set(available_assets[0])
    else:
        asset_parm.set("")


def _sync_layer_department_and_version(node, asset_module=None):
    """Ensure import_asset_layer department/version tokens are valid."""
    if not node:
        return

    asset_module = asset_module or _get_asset_module()
    department_parm = node.parm("department")
    version_parm = node.parm("version")
    if not department_parm:
        return

    category = node.evalParm("category") if node.parm("category") else ""
    asset_name = node.evalParm("asset") if node.parm("asset") else ""

    task_items = _ordered_task_items(category, asset_name)
    task_tokens = [token for token, _ in task_items]

    current_department = department_parm.eval()
    if current_department not in task_tokens:
        if task_tokens:
            department_parm.set(task_tokens[0])
            current_department = task_tokens[0]
        else:
            department_parm.set("")
            current_department = ""

    if not version_parm:
        return

    version_tokens = asset_module.list_versions_for_asset(
        category,
        asset_name,
        current_department,
    )
    current_version = version_parm.eval()

    if current_version not in version_tokens:
        if version_tokens:
            version_parm.set(version_tokens[-1])
        else:
            version_parm.set("")


def _collect_batch_indices(node):
    """Collect multiparm indices for batch category/asset rows."""
    indices = set()
    for parm in node.parms():
        index = _extract_index_from_name(
            parm.name(),
            prefixes=["asset_category", "category", "asset_name", "asset"],
        )
        if index:
            indices.add(index)
    return sorted(indices, key=int)


def _sync_batch_asset_from_category(node, index, asset_module):
    """Set the batch asset token for a given row index."""
    category_parm = _get_first_existing_parm(
        node,
        [f"asset_category{index}", f"category{index}"],
    )
    asset_parm = _get_first_existing_parm(
        node,
        [f"asset_name{index}", f"asset{index}"],
    )
    if not category_parm or not asset_parm:
        return

    category = category_parm.eval()
    assets_by_category = asset_module.get_assets_by_category()
    assets = _get_assets_for_category(assets_by_category, category)
    assets = sorted(
        assets,
        key=lambda asset: (asset.get("label") or asset["name"]).lower(),
    )
    available_asset_names = [asset["name"] for asset in assets]

    current_asset = asset_parm.eval()
    if current_asset in available_asset_names:
        asset_parm.set(current_asset)
    elif available_asset_names:
        asset_parm.set(available_asset_names[0])
    else:
        asset_parm.set("")


def _sync_all_batch_assets(node, asset_module=None):
    """Sync all batch rows so each category row has a valid asset token."""
    if not node:
        return

    asset_module = asset_module or _get_asset_module()
    for index in _collect_batch_indices(node):
        _sync_batch_asset_from_category(node, index, asset_module)


def _ordered_task_items(category, asset_name):
    """Return ordered task tuples for current asset."""
    asset_module = _get_asset_module()
    tasks = asset_module.get_tasks_for_asset(category, asset_name)
    task_map = {task["value"].lower(): task for task in tasks}

    ordered = []
    for task_name in getattr(asset_module, "TASK_ORDER", _DEFAULT_TASK_ORDER):
        task = task_map.pop(task_name, None)
        if task:
            ordered.append((task["value"], task["label"]))

    for _, task in sorted(task_map.items(), key=lambda item: item[1]["label"].lower()):
        ordered.append((task["value"], task["label"]))

    return ordered


def menu_import_asset_category(_node=None):
    """Menu items for import_asset `category` parm."""
    asset_module = _get_asset_module()
    categories = sorted(asset_module.get_assets_by_category().keys())

    node = _node["node"] if isinstance(_node, dict) else _node
    if node:
        _sync_single_asset_from_category(node, asset_module)
        if node.parm("department"):
            _sync_layer_department_and_version(node, asset_module)

    return _flatten_menu([(cat, cat.title()) for cat in categories])


def menu_import_asset_asset(node):
    """Menu items for import_asset `asset` parm."""
    asset_module = _get_asset_module()
    _sync_single_asset_from_category(node, asset_module)
    if node.parm("department"):
        _sync_layer_department_and_version(node, asset_module)

    category = node.evalParm("category") if node.parm("category") else ""
    assets_by_category = asset_module.get_assets_by_category()
    assets = _get_assets_for_category(assets_by_category, category)
    assets = sorted(assets, key=lambda asset: (asset.get("label") or asset["name"]).lower())
    return _flatten_menu(
        [
            (asset["name"], asset.get("label") or asset["name"])
            for asset in assets
        ]
    )


def menu_import_asset_exclude_department(node):
    """Menu items for import_asset `exclude_department` parm."""
    _sync_single_asset_from_category(node)
    category = node.evalParm("category") if node.parm("category") else ""
    asset_name = node.evalParm("asset") if node.parm("asset") else ""
    return _flatten_menu(_ordered_task_items(category, asset_name))


def menu_import_asset_layer_department(node):
    """Menu items for import_asset_layer `department` parm."""
    asset_module = _get_asset_module()
    _sync_single_asset_from_category(node, asset_module)
    _sync_layer_department_and_version(node, asset_module)

    category = node.evalParm("category") if node.parm("category") else ""
    asset_name = node.evalParm("asset") if node.parm("asset") else ""
    return _flatten_menu(_ordered_task_items(category, asset_name))


def menu_import_asset_layer_version(node):
    """Menu items for import_asset_layer `version` parm."""
    if not node:
        return []

    asset_module = _get_asset_module()
    _sync_single_asset_from_category(node, asset_module)
    _sync_layer_department_and_version(node, asset_module)

    category = node.evalParm("category") if node.parm("category") else ""
    asset_name = node.evalParm("asset") if node.parm("asset") else ""
    department = node.evalParm("department") if node.parm("department") else ""

    versions = asset_module.list_versions_for_asset(category, asset_name, department)
    return _flatten_menu([(token, token) for token in reversed(versions)])


# ---------------------------------------------------------
# BATCH IMPORT MENU HELPERS
# ---------------------------------------------------------

def menu_import_asset_batch_category(kwargs):
    """Menu items for import_batch_asset multiparm `category#`."""
    node = kwargs["node"] if isinstance(kwargs, dict) else kwargs
    asset_module = _get_asset_module()
    categories = sorted(asset_module.get_assets_by_category().keys())

    if isinstance(kwargs, dict):
        index = _get_multiparm_index(
            kwargs,
            prefixes=["asset_category", "category"],
        )
        if index:
            category_parm = _get_first_existing_parm(
                node,
                [f"asset_category{index}", f"category{index}"],
            )
            if category_parm:
                current_category = category_parm.eval()
                if not current_category and categories:
                    category_parm.set(categories[0])
            _sync_batch_asset_from_category(node, index, asset_module)

    return _flatten_menu([(cat, cat.title()) for cat in categories])


def menu_import_asset_batch_asset(kwargs):
    """Menu items for import_batch_asset multiparm `asset#`."""
    if not isinstance(kwargs, dict):
        return []

    node = kwargs["node"]
    asset_module = _get_asset_module()

    index = _get_multiparm_index(kwargs, prefixes=["asset_name", "asset"])
    if not index:
        return []

    category_parm = _get_first_existing_parm(
        node,
        [f"asset_category{index}", f"category{index}"],
    )
    asset_parm = _get_first_existing_parm(
        node,
        [f"asset_name{index}", f"asset{index}"],
    )
    category = category_parm.eval() if category_parm else ""

    assets_by_category = asset_module.get_assets_by_category()
    assets = _get_assets_for_category(assets_by_category, category)
    assets = sorted(
        assets,
        key=lambda asset: (asset.get("label") or asset["name"]).lower(),
    )
    asset_names = [asset["name"] for asset in assets]

    # New multiparm rows often start with empty/invalid token until next
    # callback. Keep token valid as soon as menu evaluates.
    if asset_parm:
        current_asset = asset_parm.eval()
        if asset_names and current_asset not in asset_names:
            asset_parm.set(asset_names[0])
        elif not asset_names and current_asset:
            asset_parm.set("")

    return _flatten_menu(
        [
            (asset["name"], asset.get("label") or asset["name"])
            for asset in assets
        ]
    )


def menu_import_asset_layer_category(node):
    """Menu items for import_asset_layer `category` parm."""
    return menu_import_asset_category(node)


def menu_import_asset_layer_asset(node):
    """Menu items for import_asset_layer `asset` parm."""
    return menu_import_asset_asset(node)


# ---------------------------------------------------------
# CALLBACK WRAPPERS (ONE-LINER FRIENDLY)
# ---------------------------------------------------------

def _reset_exclude_department(node):
    parm = node.parm("exclude_department")
    if parm:
        items = rebuild_menu(parm)
        if items:
            parm.set(items[0])


def cb_import_asset_category(node):
    """Callback for import_asset `category` parm."""
    on_category_changed(node)


def cb_import_asset_asset(node):
    """Callback for import_asset `asset` parm."""
    on_asset_changed(node)


def cb_import_asset_import(node):
    """Callback for import_asset Import button."""
    builder_module = _get_asset_builder_module()
    if hasattr(builder_module, "gather_single_asset"):
        return builder_module.gather_single_asset(node)
    if hasattr(builder_module, "gather_asset"):
        return builder_module.gather_asset(node)

    hda_module = node.hdaModule()
    if hasattr(hda_module, "gather_single_asset"):
        return hda_module.gather_single_asset(node)
    if hasattr(hda_module, "gather_asset"):
        return hda_module.gather_asset(node)

    raise AttributeError(
        "No gather function found in module '{}'.".format(
            getattr(builder_module, "__file__", "<unknown>")
        )
    )


def cb_import_asset_batch_category(kwargs):
    """Callback for import_batch_asset multiparm `category#`."""
    asset_module = _get_asset_module()
    if isinstance(kwargs, dict):
        node = kwargs["node"]
        index = _get_multiparm_index(
            kwargs,
            prefixes=["asset_category", "category"],
        )
    else:
        node = kwargs
        index = None

    if index:
        _sync_batch_asset_from_category(node, index, asset_module)
    else:
        _sync_all_batch_assets(node, asset_module)

    if node:
        node.cook(force=True)


def cb_import_asset_batch_asset(kwargs):
    """Callback for import_batch_asset multiparm `asset#`."""
    if isinstance(kwargs, dict):
        kwargs["node"].cook(force=True)


def cb_import_asset_batch_instances(kwargs):
    """Callback for import_batch_asset multiparm instance count (+/-/clear)."""
    node = kwargs["node"] if isinstance(kwargs, dict) else kwargs
    if not node:
        return

    _sync_all_batch_assets(node)
    node.cook(force=True)


def cb_import_asset_batch_import(node):
    """Callback for import_batch_asset Import button."""
    builder_module = _get_asset_builder_module()
    if hasattr(builder_module, "gather_assets"):
        return builder_module.gather_assets(node)

    hda_module = node.hdaModule()
    if hasattr(hda_module, "gather_assets"):
        return hda_module.gather_assets(node)

    raise AttributeError(
        "No gather_assets function found in module '{}'.".format(
            getattr(builder_module, "__file__", "<unknown>")
        )
    )


def cb_import_asset_layer_category(node):
    """Callback for import_asset_layer `category` parm."""
    on_category_changed(node)


def cb_import_asset_layer_asset(node):
    """Callback for import_asset_layer `asset` parm."""
    on_asset_changed(node)


def cb_import_asset_layer_department(node):
    """Callback for import_asset_layer `department` parm."""
    on_department_changed(node)


# ---------------------------------------------------------
# ON CREATED HELPERS
# ---------------------------------------------------------

def on_created_import_asset(node_or_kwargs):
    """OnCreated helper for import_asset HDA."""
    node = _node_from_arg(node_or_kwargs)
    if not node:
        return

    asset_module = _get_asset_module(reload_module=True)
    _sync_single_asset_from_category(node, asset_module)
    node.cook(force=True)


def on_created_import_asset_layer(node_or_kwargs):
    """OnCreated helper for import_asset_layer HDA."""
    node = _node_from_arg(node_or_kwargs)
    if not node:
        return

    asset_module = _get_asset_module(reload_module=True)
    try:
        if hasattr(asset_module, "on_created"):
            asset_module.on_created(node)
    except Exception:
        pass

    _sync_single_asset_from_category(node, asset_module)
    _sync_layer_department_and_version(node, asset_module)
    node.cook(force=True)


def on_created_import_asset_batch(node_or_kwargs):
    """OnCreated helper for import_assets_batch HDA."""
    node = _node_from_arg(node_or_kwargs)
    if not node:
        return

    asset_module = _get_asset_module(reload_module=True)
    _sync_all_batch_assets(node, asset_module)
    node.cook(force=True)
