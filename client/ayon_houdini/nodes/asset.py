import hou
from collections import defaultdict

from ayon_api import (
    get_folders,
    get_products,
    get_representations,
    get_tasks,
    get_versions,
)
from ayon_core.pipeline import get_current_context, get_current_project_name


def _resolve_asset_name(category, asset_value):
    """Convert asset UI label to internal AYON asset name."""
    if not asset_value:
        return None

    assets = get_assets_by_category().get(category, [])
    for asset in assets:
        if asset_value == asset["name"] or asset_value == asset["label"]:
            return asset["name"]

    return None


TASK_PRODUCT_KEYWORDS = {
    "modeling": ["model"],
    "lookdev": ["look"],
    "texture": ["tex", "texture"],
}


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

        # Use the ID 'name' as the token and 'label' for the UI
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

    folder_entity = None
    for f in get_folders(project_name=project, folder_types=["Asset"]):
        parts = f["path"].strip("/").split("/")
        if len(parts) < 3:
            continue

        _, cat, asset = parts
        if cat.lower() == category.lower() and asset.lower() == asset_name.lower():
            folder_entity = f
            break

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
    """Resolve USD file path from AYON 0.9.x representation dict."""
    # Preferred location (AYON 0.9.x)
    attrib = rep.get("attrib", {})
    path = attrib.get("path")
    if path:
        return path

    # Fallback: first file entry
    files = rep.get("files", [])
    if files and isinstance(files, list):
        return files[0].get("path")

    return None


def get_latest_usd_for_asset(category, asset_name, task_name):
    project = get_current_project_name()
    if not project:
        return None

    folder_entity = None
    for f in get_folders(project_name=project, folder_types=["Asset"]):
        parts = f["path"].strip("/").split("/")
        if len(parts) < 3:
            continue

        _, cat, asset = parts
        if cat.lower() == category.lower() and asset.lower() == asset_name.lower():
            folder_entity = f
            break

    if not folder_entity:
        return None

    folder_id = folder_entity["id"]

    products = list(
        get_products(
            project_name=project,
            folder_ids=[folder_id],
        )
    )

    keywords = TASK_PRODUCT_KEYWORDS.get(task_name, [])
    task_products = []

    for p in products:
        name_l = p["name"].lower()
        if name_l.startswith("workfile"):
            continue
        if any(k in name_l for k in keywords):
            task_products.append(p)

    if not task_products:
        return None

    latest_version = None
    latest_version_id = None
    latest_product = None

    for product in task_products:
        versions = list(
            get_versions(
                project_name=project,
                product_ids=[product["id"]]
            )
        )

        for v in versions:
            if latest_version is None or v["version"] > latest_version:
                latest_version = v["version"]
                latest_version_id = v["id"]
                latest_product = product

    if not latest_version_id:
        return None

    reps = list(
        get_representations(
            project_name=project,
            version_ids=[latest_version_id],
        )
    )

    for r in reps:
        if r["name"] == "usd":
            usd_path = get_usd_path_from_rep(r)
            if not usd_path:
                continue

            return {
                "version": latest_version,
                "usd_path": usd_path,
                "product": latest_product["name"],
            }

    return None


def get_versions_for_asset(category, asset_name, task_name):
    project = get_current_project_name()
    if not project:
        return []

    folder_entity = None
    for f in get_folders(project_name=project, folder_types=["Asset"]):
        parts = f["path"].strip("/").split("/")
        if len(parts) < 3:
            continue

        _, cat, asset = parts
        if cat.lower() == category.lower() and asset.lower() == asset_name.lower():
            folder_entity = f
            break

    if not folder_entity:
        return []

    products = list(
        get_products(
            project_name=project,
            folder_ids=[folder_entity["id"]],
        )
    )

    keywords = TASK_PRODUCT_KEYWORDS.get(task_name, [])
    task_products = []

    for p in products:
        name_l = p["name"].lower()
        if name_l.startswith("workfile"):
            continue
        if any(k in name_l for k in keywords):
            task_products.append(p)

    if not task_products:
        return []

    versions = set()
    for product in task_products:
        for v in get_versions(
            project_name=project,
            product_ids=[product["id"]],
        ):
            versions.add(v["version"])

    return sorted(versions)


def get_usd_for_asset_version(category, asset_name, task_name, version_number):
    project = get_current_project_name()
    if not project:
        return None

    folder_entity = None
    for f in get_folders(project_name=project, folder_types=["Asset"]):
        parts = f["path"].strip("/").split("/")
        if len(parts) < 3:
            continue

        _, cat, asset = parts
        if cat.lower() == category.lower() and asset.lower() == asset_name.lower():
            folder_entity = f
            break

    if not folder_entity:
        return None

    folder_id = folder_entity["id"]

    products = list(
        get_products(
            project_name=project,
            folder_ids=[folder_id],
        )
    )

    keywords = TASK_PRODUCT_KEYWORDS.get(task_name, [])
    task_products = []

    for p in products:
        name_l = p["name"].lower()
        if name_l.startswith("workfile"):
            continue
        if any(k in name_l for k in keywords):
            task_products.append(p)

    if not task_products:
        return None

    version_id = None
    product_name = None

    for product in task_products:
        versions = list(
            get_versions(
                project_name=project,
                product_ids=[product["id"]]
            )
        )

        for v in versions:
            if v["version"] == int(version_number):
                version_id = v["id"]
                product_name = product["name"]
                break

        if version_id:
            break

    if not version_id:
        return None

    reps = list(
        get_representations(
            project_name=project,
            version_ids=[version_id],
        )
    )

    for r in reps:
        if r["name"] == "usd":
            attrib = r.get("attrib", {})
            usd_path = attrib.get("path")
            if usd_path:
                return {
                    "version": int(version_number),
                    "usd_path": usd_path,
                    "product": product_name,
                }

    return None


def on_created(node: hou.Node):
    """AYON OnCreated hook for import_asset_layer HDA."""
    if node is None:
        return

    # Adjust this string if your HDA name is different
    if node.type().name() != "import_asset_layer":
        return

    ctx = get_current_context()
    if not ctx:
        return

    folder_path = ctx.get("folder_path")  # e.g. /assets/prop/chair
    task_name = ctx.get("task_name")  # e.g. Modeling

    if not folder_path:
        return

    parts = folder_path.strip("/").split("/")

    # Basic safety check for path depth
    if len(parts) < 3:
        return

    try:
        idx = parts.index("assets")
        category = parts[idx + 1]
        asset_name = parts[idx + 2]
    except (ValueError, IndexError):
        return

    # We set these first so the menu scripts have context
    if node.parm("category"):
        node.parm("category").set(category)

    if node.parm("asset"):
        node.parm("asset").set(asset_name)

    if node.parm("department") and task_name:
        node.parm("department").set(task_name.lower())

    try:
        import_latest_usd(node)
    except Exception as e:
        print(f"Initial import failed on creation: {e}")


_CACHE_VERSIONS = {}


def list_versions_for_asset(category, asset_name, task_name):
    """Return sorted version tokens for an AYON asset task."""
    project = get_current_project_name()
    if not project:
        return []

    cache_key = (project, category, asset_name, task_name)
    if cache_key in _CACHE_VERSIONS:
        return _CACHE_VERSIONS[cache_key][:]

    folder_entity = None
    for f in get_folders(project_name=project, folder_types=["Asset"]):
        parts = f["path"].strip("/").split("/")
        if len(parts) >= 3 and parts[1] == category and parts[2] == asset_name:
            folder_entity = f
            break

    if not folder_entity:
        return []

    versions = set()

    products = list(
        get_products(
            project_name=project,
            folder_ids=[folder_entity["id"]],
        )
    )

    for product in products:
        for v in get_versions(
            project_name=project,
            product_ids=[product["id"]],
        ):
            versions.add(v["version"])

    version_tokens = [f"v{v:03d}" for v in sorted(versions)]
    _CACHE_VERSIONS[cache_key] = version_tokens
    return version_tokens[:]


def get_latest_version_token(category, asset_name, task_name):
    versions = list_versions_for_asset(category, asset_name, task_name)
    if not versions:
        return None
    return versions[-1]  # v002, v003, etc.


def import_latest_usd(node):
    node.cook(force=True)
    category = node.evalParm("category")
    asset = node.evalParm("asset")
    task = node.evalParm("department")

    data = get_latest_usd_for_asset(category, asset, task)

    if not data:
        hou.ui.displayMessage("NO DATA FOUND!!")
        return

    usd_path = data["usd_path"]

    lopnet = node
    sublayer = lopnet.node("sublayer")
    if not sublayer:
        sublayer = lopnet.createNode("sublayer", "sublayer1")

    sublayer.parm("filepath1").set(usd_path)
    sublayer.cook(force=True)
    latest_token = get_latest_version_token(category, asset, task)
    if latest_token:
        node.parm("version").set(str(data["version"]))  # MUST be string

