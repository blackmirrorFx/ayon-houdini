import hou

from ayon_houdini.nodes import asset as ayon_assets

TASK_ORDER = ["modeling", "rigging", "groom", "lookdev"]


def gather_single_asset(node):
    """Rebuild the internal LOP network for a single asset."""

    category = node.evalParm("category")
    asset = node.evalParm("asset")
    exclude_task = node.evalParm("exclude_department")

    if not category or not asset:
        hou.ui.displayMessage("Category or Asset missing")
        return

    container = node.node("dive")
    if not container:
        hou.ui.displayMessage("Dive network not found inside HDA")
        return

    for child in container.children():
        child.destroy()

    # Get tasks
    tasks_data = node.hdaModule().get_tasks_for_asset(category, asset)
    available_tasks = {t["value"] for t in tasks_data}

    ordered_tasks = [
        task
        for task in TASK_ORDER
        if task in available_tasks and task != exclude_task
    ]

    if not ordered_tasks:
        hou.ui.displayMessage("Nothing to import")
        return

    previous_node = None
    created_nodes = []

    for task in ordered_tasks:
        layer_node = container.createNode(
            "import_asset_layer",
            f"{asset}_{task}_layer",
        )

        layer_node.parm("category").set(category)
        layer_node.parm("asset").set(asset)
        layer_node.parm("department").set(task)

        ayon_assets.import_latest_usd(layer_node)

        if previous_node:
            layer_node.setInput(0, previous_node)

        previous_node = layer_node
        created_nodes.append(layer_node)

    output_node = container.createNode("output", "output0")
    if previous_node:
        output_node.setInput(0, previous_node)

    x, y = 0.0, 0.0
    for created_node in created_nodes:
        created_node.setPosition(hou.Vector2(x, y))
        y -= 1.6
    output_node.setPosition(hou.Vector2(x, y))

def gather_assets(node):
    """Create multiple import_asset nodes and connect them into a single USD stage."""

    container = node.node("dive")
    if not container:
        hou.ui.displayMessage("Dive network not found")
        return

    for child in container.children():
        child.destroy()

    asset_count = node.evalParm("asset_count")

    if asset_count == 0:
        hou.ui.displayMessage("No assets specified")
        return

    previous_node = None
    created_nodes = []

    for i in range(1, asset_count + 1):
        category = node.evalParm(f"asset_category{i}")
        asset = node.evalParm(f"asset_name{i}")

        if not category or not asset:
            continue

        asset_node = container.createNode(
            "import_asset",
            f"{asset}_import",
        )

        asset_node.parm("category").set(category)
        asset_node.parm("asset").set(asset)

        asset_node.hdaModule().gather_asset(asset_node)

        if previous_node:
            asset_node.setInput(0, previous_node)

        previous_node = asset_node
        created_nodes.append(asset_node)

    output_node = container.createNode("output", "output0")
    if previous_node:
        output_node.setInput(0, previous_node)

    x, y = 0.0, 0.0
    for created_node in created_nodes:
        created_node.setPosition(hou.Vector2(x, y))
        y -= 2.0
    output_node.setPosition(hou.Vector2(x, y))

    container.layoutChildren()


