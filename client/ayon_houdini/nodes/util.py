import importlib
import hou

from ayon_houdini.nodes import asset as ayon_assets
from ayon_houdini.nodes import build_asset


# ---------------------------------------------------------
# LOW-LEVEL UTIL
# ---------------------------------------------------------

def rebuild_menu(parm):
    """Force Houdini to rebuild a dynamic menu."""
    if parm:
        return parm.menuItems()
    return []


# ---------------------------------------------------------
# IMPORT
# ---------------------------------------------------------

def run_import(node):
    """
    Synchronizes UI and runs import safely.
    """
    category = node.parm("category").eval()
    asset_parm = node.parm("asset")
    asset = asset_parm.eval()

    # Ensure asset token is valid
    if not asset:
        items = rebuild_menu(asset_parm)
        if items:
            asset_parm.set(items[0])
            asset = asset_parm.eval()

    if not asset:
        hou.ui.displayMessage(
            "Selection incomplete. Please select an Asset.",
            severity=hou.severityType.Warning
        )
        return

    importlib.reload(ayon_assets)
    importlib.reload(build_asset)

    try:
        build_asset.gather_single_asset(node)
        print(f"[AYON] Import triggered for asset: {asset}")
    except Exception as e:
        print(f"[AYON] Build Error: {e}")


# ---------------------------------------------------------
# CATEGORY CALLBACK
# ---------------------------------------------------------

def on_category_changed(node):
    """
    Category → Asset → Department → Version
    """
    category = node.evalParm("category")
    assets_dict = ayon_assets.get_assets_by_category()

    asset_parm = node.parm("asset")
    dept_parm = node.parm("department")
    ver_parm = node.parm("version")

    asset_parm.set("")
    dept_parm.set("")
    ver_parm.set("")

    if category in assets_dict and assets_dict[category]:
        first_asset = assets_dict[category][0]["name"]
        asset_parm.set(first_asset)

        # rebuild downstream
        rebuild_menu(dept_parm)
        rebuild_menu(ver_parm)
    else:
        rebuild_menu(asset_parm)
        rebuild_menu(dept_parm)
        rebuild_menu(ver_parm)


# ---------------------------------------------------------
# MULTIPARM CATEGORY CALLBACK
# ---------------------------------------------------------

def on_category_changed(node):
    """Refreshes asset list and automatically selects the first one."""
    category = node.evalParm('category')
    # Fetch your assets dictionary from your AYON core tool
    assets_dict = ayon_assets.get_assets_by_category()

    if category in assets_dict and assets_dict[category]:
        # Grab the first asset name (e.g., 'Angel' or 'Kamaz')
        first_asset = assets_dict[category][0]['name']
        node.parm('asset').set(first_asset)

        # --- THE TRIGGER ---
        # Now that asset has a value, force the Department to refresh
        node.parm("department").eval()
    else:
        node.parm('asset').set("")
        node.parm("department").set("")

    node.cook(force=True)

# ---------------------------------------------------------
# ASSET CALLBACK
# ---------------------------------------------------------

def on_asset_changed(node):
    """
    Triggers when the asset is changed.
    It forces the Department to update and then tells the Version to refresh.
    """
    # 1. Evaluate the asset to lock in the token
    node.parm("asset").eval()

    # 2. Force the Department menu to rebuild based on the new asset
    node.parm("department").eval()

    # 3. AUTO-SELECT: Grab the first available task (e.g., 'modeling')
    dept_menu = node.parm("department").menuItems()
    if dept_menu:
        # Programmatically set the department so the Version menu knows what to query
        node.parm("department").set(dept_menu[0])

        # 4. Trigger the NEXT link in the chain (Version refresh)
        # This ensures the Version doesn't stay 'stuck' on the old asset's data
        on_department_changed(node)
    else:
        node.parm("department").set("")
        node.parm("version").set("")

    # 5. Refresh the HDA internal state
    node.cook(force=True)
# ---------------------------------------------------------
# DEPARTMENT CALLBACK
# ---------------------------------------------------------

def on_department_changed(node):
    """Aggressively forces the Version UI to sync and redraw labels."""
    version_parm = node.parm("version")

    # 1. HARD RESET: Set to an impossible value to clear the 'ghost' label
    version_parm.set("")

    # 2. FORCE MENU REBUILD: Re-run the Menu Script logic
    version_parm.eval()

    # 3. RE-INDEX: Get the fresh list from the menu script
    version_items = version_parm.menuItems()

    if version_items:
        # 4. PUSH ACTUAL TOKEN: Set to the first item (e.g., v002)
        target_version = version_items[0]
        version_parm.set(target_version)

        # 5. UI NOTIFY: Force the parameter to tell the UI it is 'dirty'
        # This triggers the interface to look at the token and update the label
        version_parm.set(target_version)
        version_parm.pressButton()

        # 6. Final cook to ensure internal paths match the new version
    node.cook(force=True)

def force_refresh_hda(node):
    """
    Forces full parameter refresh (not UI hacks).
    """
    for name in ("category", "asset", "department", "version"):
        p = node.parm(name)
        if p:
            rebuild_menu(p)
