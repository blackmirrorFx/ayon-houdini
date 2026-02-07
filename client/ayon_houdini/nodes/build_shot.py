import hou
from ayon_core.pipeline import get_current_context
from ayon_api import get_products, get_folder_by_path

# ==================================================
# PIPELINE CONFIG
# ==================================================
TASK_TO_PRODUCT = {
    "layout": "usdLayout",
    "animation": "usdAnim",
}


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

    # if hda_node.isLocked():
    #     hou.ui.displayMessage(
    #         f"HDA '{hda_node.name()}' is locked.\n"
    #         "Allow Editing of Contents to create nodes."
    #     )
    #     return

    created_nodes = []
    y_pos = 0.0

    # --------------------------------------------------
    # 3. TASK LOGIC
    # --------------------------------------------------
    needed_tasks = []
    if current_task == "animation":
        needed_tasks = ["layout"]
    elif current_task in ("fx", "lighting"):
        needed_tasks = ["layout", "animation"]

    # --------------------------------------------------
    # 4. FX SPLITS (lighting only)
    # --------------------------------------------------
    fx_products = []
    if current_task == "lighting":
        folder_entity = get_folder_by_path(project_name, folder_path)
        if folder_entity:
            products = get_products(project_name, folder_ids=[folder_entity["id"]])
            fx_products = [
                p["name"] for p in products
                if p["name"].startswith("usdFxsplit")
            ]

    all_to_load = [(t, TASK_TO_PRODUCT.get(t)) for t in needed_tasks]
    all_to_load += [(f, f) for f in fx_products]

    # --------------------------------------------------
    # 5. CREATE / UPDATE NODES
    # --------------------------------------------------
    for identifier, product_name in all_to_load:
        if not product_name:
            continue

        node_name = f"load_{identifier}"
        node = parent_net.node(node_name)

        if not node:
            node = parent_net.createNode(node_type, node_name=node_name)

        node.setPosition(hou.Vector2(0, y_pos))
        y_pos -= y_step

        _set_parm(node, ["project"], project_name)
        _set_parm(node, ["folder"], folder_path)
        _set_parm(node, ["product"], product_name)
        _set_parm(node, ["version"], "latest")

        _press_button(node, ["reload", "refresh", "update"])

        created_nodes.append(node)

    # --------------------------------------------------
    # 6. WIRING
    # --------------------------------------------------
    # --------------------------------------------------
    # 6. WIRING (chain loaders)
    # --------------------------------------------------
    for i in range(1, len(created_nodes)):
        created_nodes[i].setInput(0, created_nodes[i - 1])

    # --------------------------------------------------
    # 7. CONNECT TO OUTPUT NODE
    # --------------------------------------------------
    if created_nodes:
        last_loader = created_nodes[-1]
        last_pos = last_loader.position()

        # Find output node in the same LOP network
        output_node = None
        for n in parent_net.children():
            if n.type().name() == "output":
                output_node = n
                break

        if not output_node:
            output_node = parent_net.createNode("output", "OUT")

            # Position output node (to the right of last loader)
        output_node.setPosition(
            hou.Vector2(last_pos.x() + 0, last_pos.y()-3)
        )

        # Wire output
        output_node.setInput(0, last_loader)

        # Flags
        output_node.setDisplayFlag(True)
        # output_node.setRenderFlag(True)

    print(f"[AYON] Successfully synced {len(created_nodes)} nodes.")
    return created_nodes


