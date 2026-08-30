"""Shared Houdini menu callbacks for LOP HDAs.

HDA parameter menus keep only tiny import-and-return expressions.  Keeping the
implementations here lets callbacks be updated without editing each asset
definition for every menu change.
"""

from __future__ import absolute_import


_SHOT_ROOT = "/World/Shot"
_LIGHTS_ROOT = "/World/Shot/lights"
_ELEMENT_CATEGORIES = (
    "character",
    "crowd",
    "env",
    "fx",
    "prop",
    "vehicle",
)
_MATERIAL_TARGET_PATHS = (
    "/World/Assets",
    "/World/Shot/Assets",
    "/World/Shot/character",
    "/World/Shot/crowd",
    "/World/Shot/env",
    "/World/Shot/fx",
    "/World/Shot/layout",
    "/World/Shot/lidar",
    "/World/Shot/lights",
    "/World/Shot/prop",
    "/World/Shot/vehicle",
)
from ayon_houdini.nodes.lops.render_pass import PASS_DEPARTMENT_RANGES


def _stage(node):
    """Get a LOP node's composed stage without breaking the parameter UI."""
    try:
        return node.stage() if node else None
    except Exception:
        return None


def _input_stage(node):
    """Get the stage connected to a LOP HDA's first input.

    Material-assignment menus must inspect the stage before the HDA authors
    bindings.  Falling back to the node's own stage also keeps the callbacks
    useful for unlocked/test nodes that do not expose an input connection.
    """
    try:
        input_node = node.input(0) if node else None
        if input_node is not None:
            return input_node.stage()
    except Exception:
        pass
    return _stage(node)


def _prim_type_menu(stage, type_name):
    """Return a deterministic token/label menu for a USD schema type."""
    if stage is None:
        return []

    paths = []
    try:
        for prim in stage.Traverse():
            if prim.GetTypeName() == type_name:
                paths.append(prim.GetPath().pathString)
    except Exception:
        return []

    menu = []
    for path in sorted(set(paths)):
        # Use the complete path as the label because mesh and material names
        # are commonly repeated in separate asset namespaces.
        menu.extend((path, path))
    return menu


def mesh_prim_menu(node):
    """Return every Mesh prim on the stage connected to ``node``."""
    return _prim_type_menu(_input_stage(node), "Mesh")


def material_prim_menu(node):
    """Return bindable Material prims with short, artist-friendly labels.

    Some material exporters author a Material-typed container with the real
    materials below it.  When that occurs, omit the container and expose its
    Material descendants instead.
    """
    typed_menu = _prim_type_menu(_input_stage(node), "Material")
    material_paths = [
        typed_menu[index] for index in range(0, len(typed_menu), 2)
    ]
    leaf_paths = [
        path for path in material_paths
        if not any(
            other.startswith(path.rstrip("/") + "/")
            for other in material_paths
            if other != path
        )
    ]
    names = [path.rstrip("/").rsplit("/", 1)[-1] for path in leaf_paths]

    menu = []
    for path, name in zip(leaf_paths, names):
        label = name
        if names.count(name) > 1:
            parent_path = path.rstrip("/").rsplit("/", 1)[0] or "/"
            label = "{} ({})".format(name, parent_path)
        menu.extend((path, label))
    return menu


def scope_prim_menu(node):
    """Return valid material parents as Material Library path prefixes.

    Structural Scopes such as ``/World`` and ``/World/Shot`` are intentionally
    excluded. Asset material containers may be Scopes or upstream Xforms.
    """
    stage = _input_stage(node)
    paths = []
    if stage is not None:
        try:
            for prim in stage.Traverse():
                type_name = prim.GetTypeName()
                path = prim.GetPath().pathString
                if path in _MATERIAL_TARGET_PATHS and type_name in (
                    "Scope",
                    "Xform",
                ):
                    paths.append(path)
        except Exception:
            return []
    paths = sorted(set(paths))
    names = [path.rstrip("/").rsplit("/", 1)[-1] for path in paths]

    menu = []
    for path, name in zip(paths, names):
        label = name
        if names.count(name) > 1:
            parent_path = path.rstrip("/").rsplit("/", 1)[0] or "/"
            label = "{} ({})".format(name, parent_path)
        menu.extend((path.rstrip("/") + "/", label))
    return menu


def open_material_creator(node):
    """Open the material builder for a BMFX Material Creator instance."""
    try:
        import hou
    except ImportError:
        raise RuntimeError("This function must run inside Houdini")

    def show_error(message):
        try:
            hou.ui.displayMessage(message, severity=hou.severityType.Error)
        except Exception:
            pass
        return None

    if node is None:
        return show_error("BMFX Material Creator node is unavailable.")

    target_parm = node.parm("target")
    target = target_parm.evalAsString().strip() if target_parm else ""
    if not target:
        return show_error("Select a Target before building a material.")

    target_path = target.rstrip("/") or "/"
    target_prim = _valid_prim(_input_stage(node), target_path)
    target_type = target_prim.GetTypeName() if target_prim is not None else ""
    valid_target = (
        target_path in _MATERIAL_TARGET_PATHS
        and target_type in ("Scope", "Xform")
    )
    if not valid_target:
        return show_error(
            "Target must be a Scope or supported shot department: {}".format(
                target_path
            )
        )

    # Material Library concatenates this prefix with the material name.
    # Normalize manually entered Scope paths as well as dropdown selections.
    normalized_target = target_path.rstrip("/") + "/"
    if target != normalized_target:
        target_parm.set(normalized_target)

    material_library = node.node("material")
    if material_library is None:
        return show_error("The internal Material Library node is missing.")

    from ayon_houdini.nodes.lops import material_builder

    try:
        ui_parent = hou.qt.mainWindow()
    except Exception:
        ui_parent = None
    return material_builder.show_ui(
        parent=ui_parent,
        material_library=material_library,
    )


def _valid_prim(stage, path):
    if stage is None:
        return None
    prim = stage.GetPrimAtPath(path)
    return prim if prim and prim.IsValid() else None


def pass_name_menu():
    """Return the render-layer number ranges used by Pass Name."""
    menu = []
    for start, end, label in PASS_DEPARTMENT_RANGES:
        token = "L{:03d}-L{:03d}".format(start, end)
        menu.extend((token, "{} - {}".format(token, label)))
    return menu


def element_target_menu(node):
    """Return top-level shot element categories."""
    root = _valid_prim(_stage(node), _SHOT_ROOT)
    if root is None:
        return []
    menu = []
    for prim in root.GetChildren():
        name = prim.GetName()
        if name in _ELEMENT_CATEGORIES:
            menu.extend((prim.GetPath().pathString, name.capitalize()))
    return menu


def beauty_target_menu(node):
    """Return Xform children of the selected Element Target."""
    stage = _stage(node)
    try:
        target_path = node.evalParm("elemtarget")
    except Exception:
        return []
    root = _valid_prim(stage, target_path)
    if root is None:
        return []
    menu = []
    for prim in root.GetChildren():
        if prim.GetTypeName() == "Xform":
            menu.extend((prim.GetPath().pathString, prim.GetName()))
    return menu


def scene_target_menu(node):
    """Return all assets below supported shot element categories."""
    root = _valid_prim(_stage(node), _SHOT_ROOT)
    if root is None:
        return []
    menu = []
    for category in root.GetChildren():
        category_name = category.GetName()
        if category_name not in _ELEMENT_CATEGORIES:
            continue
        for prim in category.GetChildren():
            menu.extend((
                prim.GetPath().pathString,
                "{} ({})".format(prim.GetName(), category_name),
            ))
    return menu


def light_menu(node):
    """Return concrete lights for both Lights and Light Exclude menus."""
    root = _valid_prim(_stage(node), _LIGHTS_ROOT)
    if root is None:
        return []
    menu = []
    for prim in root.GetChildren():
        if prim.GetTypeName() in ("Scope", "Xform"):
            continue
        menu.extend((prim.GetPath().pathString, prim.GetName()))
    return menu


def install_render_pass_menu_callbacks(node):
    """Replace Configure Render Pass HDA menu scripts with utility imports.

    Run once in Houdini's Python Shell after saving this module.  The function
    edits and saves the asset definition belonging to ``node``; Houdini makes
    its standard HDA backup before changing the definition.
    """
    try:
        import hou
    except ImportError:
        raise RuntimeError("This function must run inside Houdini")

    if node is None:
        raise ValueError("Pass the Configure Render Pass HDA node")
    definition = node.type().definition()
    if definition is None:
        raise ValueError("{} is not an HDA node".format(node.path()))

    callbacks = {
        "passname": "pass_name_menu()",
        "elemtarget": "element_target_menu(hou.pwd())",
        "beautytarget#": "beauty_target_menu(hou.pwd())",
        "excludetarget#": "scene_target_menu(hou.pwd())",
        "phantomtarget#": "scene_target_menu(hou.pwd())",
        "mattetarget#": "scene_target_menu(hou.pwd())",
        "light#": "light_menu(hou.pwd())",
        "lightexclude#": "light_menu(hou.pwd())",
    }
    group = definition.parmTemplateGroup()
    for parm_name, call in callbacks.items():
        template = group.find(parm_name)
        if template is None:
            raise ValueError("HDA parameter not found: {}".format(parm_name))
        template.setItemGeneratorScript(
            "import hou\nfrom ayon_houdini.nodes.lops import utils\nreturn utils.{}".format(call)
        )
        template.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
        group.replace(parm_name, template)
    definition.setParmTemplateGroup(group)


def install_material_assigner_menu_callbacks(node):
    """Install the mesh and material menus on BMFX Material Assigner.

    Pass an instance of ``bmfx_material_assigner`` from Houdini's Python
    Shell.  The asset definition is updated, so all instances receive the
    dropdown callbacks.
    """
    try:
        import hou
    except ImportError:
        raise RuntimeError("This function must run inside Houdini")

    if node is None:
        raise ValueError("Pass the BMFX Material Assigner HDA node")
    definition = node.type().definition()
    if definition is None:
        raise ValueError("{} is not an HDA node".format(node.path()))

    callbacks = {
        "primpattern#": "mesh_prim_menu(kwargs['node'])",
        "matspecpath#": "material_prim_menu(kwargs['node'])",
    }
    group = definition.parmTemplateGroup()
    for parm_name, call in callbacks.items():
        template = group.find(parm_name)
        if template is None:
            raise ValueError("HDA parameter not found: {}".format(parm_name))
        template.setItemGeneratorScript(
            "from ayon_houdini.nodes.lops import utils\n"
            "return utils.{}".format(call)
        )
        template.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
        group.replace(parm_name, template)
    definition.setParmTemplateGroup(group)


def install_material_creator_callbacks(node):
    """Install the Scope menu and builder button on Material Creator."""
    try:
        import hou
    except ImportError:
        raise RuntimeError("This function must run inside Houdini")

    if node is None:
        raise ValueError("Pass the BMFX Material Creator HDA node")
    definition = node.type().definition()
    if definition is None:
        raise ValueError("{} is not an HDA node".format(node.path()))

    group = definition.parmTemplateGroup()
    target_template = group.find("target")
    button_template = group.find("buildmat")
    if target_template is None or button_template is None:
        raise ValueError("Material Creator requires target and buildmat parameters")

    target_template.setItemGeneratorScript(
        "from ayon_houdini.nodes.lops import utils\n"
        "return utils.scope_prim_menu(kwargs['node'])"
    )
    target_template.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
    group.replace("target", target_template)

    button_template.setScriptCallback(
        "from ayon_houdini.nodes.lops import utils; "
        "utils.open_material_creator(kwargs['node'])"
    )
    button_template.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    group.replace("buildmat", button_template)
    definition.setParmTemplateGroup(group)
