"""Shared Houdini menu callbacks for LOP HDAs.

The Configure Render Pass HDA keeps only tiny import-and-return expressions in
its parameter menu scripts.  Keeping the implementation here lets callbacks
be updated without editing the asset definition for every menu change.
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
from ayon_houdini.nodes.lops.render_pass import PASS_DEPARTMENT_RANGES


def _stage(node):
    """Get a LOP node's composed stage without breaking the parameter UI."""
    try:
        return node.stage() if node else None
    except Exception:
        return None


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
