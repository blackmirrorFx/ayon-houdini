import hou
import re
from collections import defaultdict

from ayon_api import (
    get_folders,
    get_products,
    get_representations,
    get_tasks,
    get_versions,
)
from ayon_core.pipeline import get_current_context, get_current_project_name


TASK_ORDER = ["modeling", "rigging", "groom", "lookdev"]

TASK_PRODUCT_KEYWORDS = {
    "modeling": ["model"],
    "rigging": ["rig"],
    "groom": ["groom", "fur", "hair"],
    "lookdev": ["look", "surf", "shade"],
    "texture": ["tex", "texture"],
}

DESTINATION_BY_CATEGORY = {
    "character": "/World/Shot/character",
    "vehicle": "/World/Shot/vehicle",
    "prop": "/World/Shot/env",
}


def _resolve_asset_name(category, asset_value):
    """Convert asset UI label to internal AYON asset name."""
    if not asset_value:
        return None

    assets = get_assets_by_category().get(category, [])
    for asset in assets:
        if asset_value in (asset["name"], asset["label"]):
            return asset["name"]

    return asset_value


def _get_asset_folder(project_name, category, asset_value):
    """Return AYON folder entity for a given category/asset pair."""
    asset_name = _resolve_asset_name(category, asset_value)
    if not asset_name:
        return None

    for folder in get_folders(project_name=project_name, folder_types=["Asset"]):
        parts = folder["path"].strip("/").split("/")
        if len(parts) < 3:
            continue

        _, cat, asset = parts[:3]
        if cat.lower() == category.lower() and asset.lower() == asset_name.lower():
            return folder

    return None


def _get_task_products(project_name, folder_id, task_name):
    """Return products matching the given task by name keywords or product type."""
    task_name = (task_name or "").lower()
    keywords = TASK_PRODUCT_KEYWORDS.get(task_name, [])
    if not keywords and task_name:
        keywords = [task_name]

    products = list(
        get_products(
            project_name=project_name,
            folder_ids=[folder_id],
        )
    )

    task_products = []
    for product in products:
        name_l = product["name"].lower()
        if name_l.startswith("workfile"):
            continue
        if not keywords:
            task_products.append(product)
            continue
        # Match by product name keywords OR product type
        # e.g. product named "props_bldg_lg_f" with productType="model"
        # should still match the "modeling" task (keyword "model")
        product_type = (product.get("productType") or "").lower()
        if any(keyword in name_l for keyword in keywords) or any(
            keyword in product_type for keyword in keywords
        ):
            task_products.append(product)

    return task_products


def _get_latest_version_for_products(project_name, products):
    """Return latest version entity and product from a product list."""
    latest_version = None
    latest_product = None

    for product in products:
        versions = get_versions(
            project_name=project_name,
            product_ids=[product["id"]],
        )
        for version in versions:
            if (
                latest_version is None
                or version["version"] > latest_version["version"]
            ):
                latest_version = version
                latest_product = product

    return latest_version, latest_product


def _is_usd_representation(rep_name):
    if not rep_name:
        return False
    rep_name = rep_name.lower()
    return rep_name == "usd" or rep_name.startswith("usd")


def get_assets_by_category():
    project = get_current_project_name()
    if not project:
        return {}

    assets = list(get_folders(project_name=project, folder_types=["Asset"]))

    result = defaultdict(list)
    for asset in assets:
        parts = asset["path"].strip("/").split("/")
        if len(parts) < 3:
            continue
        category = parts[1]

        result[category].append(
            {
                "name": asset["name"],
                "label": asset.get("label") or asset["name"],
            }
        )

    return dict(result)


def get_tasks_for_asset(category, asset_name):
    project = get_current_project_name()
    if not project:
        return []

    folder_entity = _get_asset_folder(project, category, asset_name)
    if not folder_entity:
        return []

    tasks = list(
        get_tasks(
            project_name=project,
            folder_ids=[folder_entity["id"]],
        )
    )

    return [
        {
            "value": task["name"],
            "label": task["label"],
        }
        for task in tasks
    ]


def get_usd_path_from_rep(rep):
    """Resolve USD file path from AYON representation dict."""
    attrib = rep.get("attrib", {})
    path = attrib.get("path")
    if path:
        return path

    files = rep.get("files", [])
    if files and isinstance(files, list):
        return files[0].get("path")

    return None


def get_latest_usd_for_asset(category, asset_name, task_name):
    project = get_current_project_name()
    if not project:
        return None

    folder_entity = _get_asset_folder(project, category, asset_name)
    if not folder_entity:
        return None

    task_products = _get_task_products(project, folder_entity["id"], task_name)
    if not task_products:
        return None

    latest_version, latest_product = _get_latest_version_for_products(
        project,
        task_products,
    )
    if not latest_version:
        return None

    reps = get_representations(
        project_name=project,
        version_ids=[latest_version["id"]],
    )

    for rep in reps:
        if not _is_usd_representation(rep.get("name")):
            continue
        usd_path = get_usd_path_from_rep(rep)
        if not usd_path:
            continue

        return {
            "version": latest_version["version"],
            "usd_path": usd_path,
            "product": latest_product["name"],
        }

    return None


def get_versions_for_asset(category, asset_name, task_name):
    project = get_current_project_name()
    if not project:
        return []

    folder_entity = _get_asset_folder(project, category, asset_name)
    if not folder_entity:
        return []

    task_products = _get_task_products(project, folder_entity["id"], task_name)
    if not task_products:
        return []

    versions = set()
    for product in task_products:
        for version in get_versions(
            project_name=project,
            product_ids=[product["id"]],
        ):
            versions.add(version["version"])

    return sorted(versions)


def get_usd_for_asset_version(category, asset_name, task_name, version_number):
    project = get_current_project_name()
    if not project:
        return None

    folder_entity = _get_asset_folder(project, category, asset_name)
    if not folder_entity:
        return None

    task_products = _get_task_products(project, folder_entity["id"], task_name)
    if not task_products:
        return None

    target_version = int(version_number)
    version_id = None
    product_name = None

    for product in task_products:
        for version in get_versions(
            project_name=project,
            product_ids=[product["id"]],
        ):
            if version["version"] == target_version:
                version_id = version["id"]
                product_name = product["name"]
                break

        if version_id:
            break

    if not version_id:
        return None

    reps = get_representations(
        project_name=project,
        version_ids=[version_id],
    )

    for rep in reps:
        if not _is_usd_representation(rep.get("name")):
            continue
        usd_path = get_usd_path_from_rep(rep)
        if usd_path:
            return {
                "version": target_version,
                "usd_path": usd_path,
                "product": product_name,
            }

    return None


def on_created(node: hou.Node):
    """AYON OnCreated hook for import_asset_layer HDA."""
    if node is None:
        return

    if node.type().name() != "import_asset_layer":
        return

    ctx = get_current_context()
    if not ctx:
        return

    folder_path = ctx.get("folder_path")
    task_name = ctx.get("task_name")

    if not folder_path:
        return

    parts = folder_path.strip("/").split("/")
    if len(parts) < 3:
        return

    try:
        idx = parts.index("assets")
        category = parts[idx + 1]
        asset_name = parts[idx + 2]
    except (ValueError, IndexError):
        return

    if node.parm("category"):
        node.parm("category").set(category)

    if node.parm("asset"):
        node.parm("asset").set(asset_name)

    if node.parm("department") and task_name:
        node.parm("department").set(task_name.lower())

    try:
        import_latest_usd(node)
    except Exception as exc:
        print(f"Initial import failed on creation: {exc}")


_CACHE_VERSIONS = {}


def list_versions_for_asset(category, asset_name, task_name):
    """Return sorted version tokens for an AYON asset task."""
    project = get_current_project_name()
    if not project:
        return []

    cache_key = (project, category, asset_name, task_name)
    if cache_key in _CACHE_VERSIONS:
        return _CACHE_VERSIONS[cache_key][:]

    versions = get_versions_for_asset(category, asset_name, task_name)
    version_tokens = [f"v{version:03d}" for version in versions]

    # Only cache non-empty results — an empty list from an early call
    # (e.g. before department parm is resolved) must not permanently
    # block future valid queries for the same key.
    if version_tokens:
        _CACHE_VERSIONS[cache_key] = version_tokens
    return version_tokens[:]


def get_latest_version_token(category, asset_name, task_name):
    versions = list_versions_for_asset(category, asset_name, task_name)
    if not versions:
        return None
    return versions[-1]


def import_latest_usd(node, show_message=True):
    node.cook(force=True)

    category = node.evalParm("category")
    asset = node.evalParm("asset")
    task = node.evalParm("department")

    data = get_latest_usd_for_asset(category, asset, task)
    if not data:
        if show_message:
            hou.ui.displayMessage("No published USD found for current selection.")
        return False

    sublayer = node.node("sublayer")
    if not sublayer:
        sublayer = node.createNode("sublayer", "sublayer1")

    sublayer.parm("filepath1").set(data["usd_path"])
    sublayer.cook(force=True)

    version_parm = node.parm("version")
    if version_parm:
        latest_token = get_latest_version_token(category, asset, task)
        if latest_token:
            version_parm.set(latest_token)
        else:
            version_parm.set(str(data["version"]))
    return True


def _clear_container(container):
    for child in container.children():
        child.destroy()


def _get_first_existing_parm(node, names):
    """Return first existing parm from provided names."""
    for name in names:
        parm = node.parm(name)
        if parm:
            return parm
    return None


def _collect_batch_rows(node):
    """Collect batch rows from either asset_*# or plain category#/asset# parms."""
    indices = set()
    prefixes = ("asset_category", "category", "asset_name", "asset")

    for parm in node.parms():
        parm_name = parm.name()
        for prefix in prefixes:
            match = re.match(rf"^{re.escape(prefix)}(\d+)$", parm_name)
            if match:
                indices.add(int(match.group(1)))
                break

    if not indices:
        count_parm = _get_first_existing_parm(node, ["asset_count", "folder0"])
        if count_parm:
            try:
                count = int(count_parm.eval())
            except Exception:
                count = 0
            indices.update(range(1, count + 1))

    rows = []
    for index in sorted(indices):
        category_parm = _get_first_existing_parm(
            node,
            [f"asset_category{index}", f"category{index}"],
        )
        asset_parm = _get_first_existing_parm(
            node,
            [f"asset_name{index}", f"asset{index}"],
        )
        if not category_parm or not asset_parm:
            continue

        rows.append((index, category_parm.eval(), asset_parm.eval()))

    return rows


def _get_batch_instances_value(node, index):
    """Return instance count for batch row index."""
    parm = _get_first_existing_parm(
        node,
        [f"instances{index}", f"instance{index}"],
    )
    if not parm:
        return 1

    try:
        return max(1, int(parm.eval()))
    except Exception:
        return 1


def _get_asset_label(category, asset_name):
    """Return display label for asset token in category."""
    assets_by_category = get_assets_by_category()
    assets = assets_by_category.get(category, [])
    if not assets:
        category_l = (category or "").strip().lower()
        for key, value in assets_by_category.items():
            if str(key).strip().lower() == category_l:
                assets = value
                break

    for asset in assets:
        if asset_name in (asset.get("name"), asset.get("label")):
            return asset.get("label") or asset.get("name") or asset_name
    return asset_name


def _sanitize_token(value):
    value = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or ""))
    return value.strip("_") or "item"


def _get_destination_path(category):
    """Resolve destination prim path from category."""
    category_l = (category or "").strip().lower()
    if category_l in DESTINATION_BY_CATEGORY:
        return DESTINATION_BY_CATEGORY[category_l]
    if category_l:
        return f"/World/Shot/{category_l}"
    return "/World/Shot"


def gather_single_asset(node):
    """Rebuild the internal LOP network for a single asset."""
    category = node.evalParm("category")
    asset_value = node.evalParm("asset")
    asset_name = _resolve_asset_name(category, asset_value)

    if not category or not asset_name:
        hou.ui.displayMessage("Category or Asset missing")
        return []

    container = node.node("dive") or node.node("Dive")
    if not container:
        hou.ui.displayMessage("Dive network not found inside HDA")
        return []

    _clear_container(container)

    tasks_data = get_tasks_for_asset(category, asset_name)
    available_tasks = [task["value"].lower() for task in tasks_data]

    ordered_tasks = [
        task
        for task in TASK_ORDER
        if task in available_tasks
    ]

    extra_tasks = [
        task
        for task in sorted(available_tasks)
        if task not in TASK_ORDER
    ]
    ordered_tasks.extend(extra_tasks)

    if not ordered_tasks:
        hou.ui.displayMessage("Nothing to import")
        return []

    previous_node = None
    created_nodes = []
    imported_tasks = []

    for task in ordered_tasks:
        layer_node = container.createNode(
            "import_asset_layer",
            f"{asset_name}_{task}_layer",
        )

        if layer_node.parm("category"):
            layer_node.parm("category").set(category)
        if layer_node.parm("asset"):
            layer_node.parm("asset").set(asset_name)
        if layer_node.parm("department"):
            layer_node.parm("department").set(task)

        loaded = import_latest_usd(layer_node, show_message=False)
        if not loaded:
            layer_node.destroy()
            continue

        if previous_node:
            layer_node.setInput(0, previous_node)

        previous_node = layer_node
        created_nodes.append(layer_node)
        imported_tasks.append(task)

    if not created_nodes:
        hou.ui.displayMessage(
            "Nothing to import. No published USD found on any department."
        )
        return []

    output_node = container.createNode("output", "output0")
    if previous_node:
        output_node.setInput(0, previous_node)

    x_pos, y_pos = 0.0, 0.0
    for created_node in created_nodes:
        created_node.setPosition(hou.Vector2(x_pos, y_pos))
        y_pos -= 1.6
    output_node.setPosition(hou.Vector2(x_pos, y_pos))

    return created_nodes


def gather_asset(node):
    """Compatibility alias used by older HDA scripts."""
    return gather_single_asset(node)


def gather_assets(node):
    """Build batch asset imports with duplicate/copy and merged output."""
    container = node.node("dive") or node.node("Dive")
    if not container:
        hou.ui.displayMessage("Dive network not found")
        return []

    _clear_container(container)

    rows = _collect_batch_rows(node)
    valid_rows = [
        (index, category, asset_name, _get_batch_instances_value(node, index))
        for index, category, asset_name in rows
        if category and asset_name
    ]

    if not valid_rows:
        hou.ui.displayMessage("No assets specified")
        return []

    include_layer_break = bool(
        node.parm("include_layer_break") and node.evalParm("include_layer_break")
    )

    merge_inputs = []
    copy_nodes = []
    import_nodes = []

    x_spacing = 5.0
    y_import = 0.0
    y_copy = -2.5
    y_merge = -5.5
    y_out = -8.0

    for idx, (_, category, asset_name, instances) in enumerate(valid_rows, start=1):
        asset_label = _get_asset_label(category, asset_name)
        safe_category = _sanitize_token(category)
        safe_asset = _sanitize_token(asset_name)

        x_pos = (idx - 1) * x_spacing

        import_name = f"import_asset_{safe_category}_{safe_asset}"
        asset_node = container.createNode("import_asset", import_name)
        asset_node.setPosition(hou.Vector2(x_pos, y_import))

        if asset_node.parm("category"):
            asset_node.parm("category").set(category)
        if asset_node.parm("asset"):
            asset_node.parm("asset").set(asset_name)
        if asset_node.parm("include_layer_break"):
            asset_node.parm("include_layer_break").set(int(include_layer_break))

        asset_module = asset_node.hdaModule()
        if hasattr(asset_module, "gather_single_asset"):
            asset_module.gather_single_asset(asset_node)
        elif hasattr(asset_module, "gather_asset"):
            asset_module.gather_asset(asset_node)
        else:
            gather_single_asset(asset_node)

        copy_node = container.createNode(
            "duplicate",
            f"{safe_category}_{safe_asset}_copy",
        )
        copy_node.setInput(0, asset_node)
        copy_node.setPosition(hou.Vector2(x_pos, y_copy))

        if copy_node.parm("ncy"):
            copy_node.parm("ncy").set(instances)

        if copy_node.parm("separatesource"):
            copy_node.parm("separatesource").set(1)

        if copy_node.parm("makeinstances"):
            copy_node.parm("makeinstances").set(0)

        if copy_node.parm("duplicatename"):
            copy_node.parm("duplicatename").set(
                "`@srcname`_`padzero(3, @copy + 1)`"
            )

        if copy_node.parm("sourceprims"):
            copy_node.parm("sourceprims").set(f"/World/Assets/{asset_label}")

        if copy_node.parm("destinationprims"):
            copy_node.parm("destinationprims").set(_get_destination_path(category))

        import_nodes.append(asset_node)
        copy_nodes.append(copy_node)
        merge_inputs.append(copy_node)

    if not merge_inputs:
        hou.ui.displayMessage("No valid assets to import")
        return []

    avg_x = (
        sum(copy_node.position().x() for copy_node in copy_nodes) / len(copy_nodes)
        if copy_nodes else 0.0
    )

    merge_node = container.createNode("merge", "merged_assets")
    merge_node.setPosition(hou.Vector2(avg_x, y_merge))
    for index, merge_input in enumerate(merge_inputs):
        merge_node.setInput(index, merge_input)

    output_node = container.createNode("output", "OUT")
    output_node.setInput(0, merge_node)
    output_node.setPosition(hou.Vector2(avg_x, y_out))

    container.layoutChildren()
    return import_nodes
