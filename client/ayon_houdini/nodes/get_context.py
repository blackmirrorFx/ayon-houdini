from ayon_api import get_folder_by_name
import ayon_api
from ayon_core.pipeline import get_current_project_name, get_current_context


def get_database_path(node):
    project = get_current_project_name()
    # Get the asset name currently selected in your dropdown
    asset_name = node.evalParm("asset")

    if not project or not asset_name:
        return ""

    # Fetch the actual folder object from AYON
    folder = get_folder_by_name(project, asset_name)

    if folder:
        # folder["path"] returns the full string like /production/chars/Angel
        # exactly as it is stored in the database
        return folder.get("path", "")

    return "Asset not found in DB"



def set_initial_context(node):
    # 1. Get the current session context from AYON
    context = get_current_context()
    project = get_current_project_name()

    db_full_path = context.get("folder_path")  # e.g., /assets/character/Angel
    current_task = context.get("task_name")  # e.g., modeling

    if not db_full_path:
        return

    # 2. Extract Category and Asset from the DB path
    parts = db_full_path.strip("/").split("/")
    if len(parts) >= 2:
        # Assuming your structure is root/category/asset
        category_name = parts[-2]
        asset_name = parts[-1]

        # Set Category and Asset parameters
        if node.parm("category"):
            node.parm("category").set(category_name)

        if node.parm("asset"):
            node.parm("asset").menuItems()  # Force menu rebuild
            node.parm("asset").set(asset_name)


def get_current_task():
    context = get_current_context()
    return context.get("task_name", "")


import ayon_api
from ayon_core.pipeline import get_current_context, get_current_project_name
import hou



def get_world_asset_path_label():
    project = get_current_project_name()
    context = get_current_context()

    # Try multiple ways to identify the folder
    folder_id = context.get("folder_id")
    folder_path = context.get("folder_path")

    if not project:
        return "/World/Assets/No_Project"

    folder = None
    try:
        # Priority 1: Get by ID
        if folder_id:
            folder = ayon_api.get_folder_by_id(project, folder_id)

        # Priority 2: Get by Path if ID failed
        if not folder and folder_path:
            folder = ayon_api.get_folder_by_path(project, folder_path)

        if folder:
            # Strictly use the database label
            asset_label = folder.get("label")

            # Final fallback to technical name ONLY if label is None in DB
            if not asset_label:
                asset_label = folder.get("name")

            return f"/World/Assets/{asset_label}"

    except Exception as e:
        return f"/World/Assets/API_Error"

    return "/World/Assets/No_Context"