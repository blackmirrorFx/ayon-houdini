import hou
from ayon_core.pipeline import get_current_context
from ayon_api import (
    get_folder_by_path,
    get_last_version_by_product_id,
    get_products,
    get_representations,
)

# ==================================================
# PIPELINE CONFIG
# ==================================================

# Map task → product name
TASK_TO_PRODUCT = {
    "matchmove": "usdMatchMove",
    "layout": "usdLayout",
    "animation": "usdAnim",
}

# Define upstream order (important for chaining)
TASK_HIERARCHY = ["matchmove", "layout", "animation"]


# ==================================================
# UTILS
# ==================================================

def _find_parm(node, keywords):
    for parm in node.parms():
        for key in keywords:
            if key.lower() in parm.name().lower():
                return parm
    return None


def _set_parm(node, keywords, value):
    parm = _find_parm(node, keywords)
    if parm:
        parm.set(value)


def _press_button(node, keywords):
    parm = _find_parm(node, keywords)
    if parm:
        parm.pressButton()


def _has_published_usd_version(project_name, product_id):
    """Return whether product has a latest version with a USD representation."""
    latest_version = get_last_version_by_product_id(
        project_name=project_name,
        product_id=product_id,
    )
    if not latest_version:
        return False

    reps = get_representations(
        project_name=project_name,
        version_ids=[latest_version["id"]],
    )
    return any(rep.get("name") == "usd" for rep in reps)


def _get_published_usd_products(project_name, folder_path):
    """Return published product names in folder that have USD output."""
    folder_entity = get_folder_by_path(project_name, folder_path)
    if not folder_entity:
        return []

    products = get_products(project_name, folder_ids=[folder_entity["id"]])
    published_products = []
    for product in products:
        if _has_published_usd_version(project_name, product["id"]):
            published_products.append(product["name"])
    return published_products


# ==================================================
# MAIN ENTRY FUNCTION
# ==================================================

def load_upstream_usd(hda_node, node_type="ayon::load_shot::1.0", y_step=3.0):

    # --------------------------------------------------
    # 1. AYON CONTEXT
    # --------------------------------------------------
    ctx = get_current_context()
    if not ctx:
        hou.ui.displayMessage("No AYON context found.")
        return

    project_name = ctx["project_name"]
    folder_path = ctx["folder_path"]
    current_task = ctx["task_name"]

    # --------------------------------------------------
    # 2. TARGET NETWORK (inside HDA)
    # --------------------------------------------------
    parent_net = hda_node.node("Dive") or hda_node

    created_nodes = []
    newly_created_node_names = []
    y_pos = 0.0

    # --------------------------------------------------
    # 3. DETERMINE UPSTREAM TASKS
    # --------------------------------------------------
    needed_tasks = []

    if current_task in TASK_HIERARCHY:
        current_index = TASK_HIERARCHY.index(current_task)
        needed_tasks = TASK_HIERARCHY[:current_index]

    elif current_task in ("fx", "lighting"):
        needed_tasks = TASK_HIERARCHY.copy()

    published_products = _get_published_usd_products(project_name, folder_path)
    published_product_set = set(published_products)

    # --------------------------------------------------
    # 4. FX SPLITS (lighting only)
    # --------------------------------------------------
    fx_products = []
    if current_task == "lighting":
        fx_products = [
            product_name
            for product_name in published_products
            if product_name.startswith("usdFxsplit")
        ]

    # Combine upstream tasks + fx splits
    all_to_load = [
        (task_name, TASK_TO_PRODUCT.get(task_name))
        for task_name in needed_tasks
        if TASK_TO_PRODUCT.get(task_name) in published_product_set
    ]
    all_to_load += [(f, f) for f in fx_products]

    # --------------------------------------------------
    # 5. CREATE / UPDATE LOADERS
    # --------------------------------------------------
    for identifier, product_name in all_to_load:
        if not product_name:
            continue

        node_name = f"load_{identifier}"
        node = parent_net.node(node_name)

        if not node:
            node = parent_net.createNode(node_type, node_name=node_name)
            newly_created_node_names.append(node_name)

        node.setPosition(hou.Vector2(0, y_pos))
        y_pos -= y_step

        _set_parm(node, ["project"], project_name)
        _set_parm(node, ["folder"], folder_path)
        _set_parm(node, ["product"], product_name)
        _set_parm(node, ["version"], "latest")

        _press_button(node, ["reload", "refresh", "update"])

        created_nodes.append(node)

    # --------------------------------------------------
    # 6. CHAIN LOADERS
    # --------------------------------------------------
    for i in range(1, len(created_nodes)):
        created_nodes[i].setInput(0, created_nodes[i - 1])

    # --------------------------------------------------
    # 7. CONNECT TO OUTPUT
    # --------------------------------------------------
    if created_nodes:
        last_loader = created_nodes[-1]
        last_pos = last_loader.position()

        # Find existing output node
        output_node = None
        for n in parent_net.children():
            if n.type().name() == "output":
                output_node = n
                break

        if not output_node:
            output_node = parent_net.createNode("output", "OUT")

        output_node.setPosition(
            hou.Vector2(last_pos.x(), last_pos.y() - 3)
        )

        output_node.setInput(0, last_loader)
        output_node.setDisplayFlag(True)

    synced_node_names = [node.name() for node in created_nodes]
    created_label = ", ".join(newly_created_node_names) if newly_created_node_names else "none"
    synced_label = ", ".join(synced_node_names) if synced_node_names else "none"
    print(
        f"created: {created_label}."
    )
    return created_nodes
