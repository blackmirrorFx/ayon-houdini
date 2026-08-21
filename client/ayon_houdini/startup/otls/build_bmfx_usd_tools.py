"""Build the BMFX Python-based USD utility HDA suite with SideFX ``hotl``."""

from __future__ import absolute_import

import os
import shutil
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OTL_DIR = os.path.join(ROOT, "startup", "otls")
TEMPLATE = os.path.join(OTL_DIR, "lop_bmfx_configure_departments.1.0.hda")
HOTL = "/opt/hfs21.0/bin/hotl"
OLD_NAME = "bmfx_configure_departments"
OLD_LABEL = "BMFX Configure Departments"


def parm(
    name,
    label,
    parm_type="string",
    default="",
    menu_call=None,
    help_text=None,
    callback=None,
):
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        '        type    {}'.format(parm_type),
        '        default {{ "{}" }}'.format(default),
        '        parmtag { "cook_dependent" "1" }',
    ]
    if menu_call:
        lines.extend([
            "        menu {",
            '            [ "import importlib" ]',
            '            [ "from ayon_houdini.nodes.lops import usd_toolkit" ]',
            '            [ "importlib.reload(usd_toolkit)" ]',
            '            [ "return usd_toolkit.{}(kwargs[\\"node\\"])" ]'.format(menu_call),
            "            language python",
            "        }",
        ])
    if help_text:
        lines.append('        help    "{}"'.format(help_text))
    if callback:
        lines.extend([
            '        parmtag {{ "script_callback" "{}" }}'.format(callback),
            '        parmtag { "script_callback_language" "python" }',
        ])
    lines.append("    }")
    return "\n".join(lines)


def payload_multiparm():
    return "\n".join([
        "    multiparm {",
        '        name    "payloadcount"',
        '        label   "Payloads"',
        "        default 1",
        '        parmtag { "cook_dependent" "1" }',
        "        parm {",
        '            name    "payloadpath#"',
        '            label   "Payload #"',
        "            type    string",
        '            default { "" }',
        '            parmtag { "cook_dependent" "1" }',
        '            parmtag { "script_callback" "kwargs[\'node\'].cook(force=True)" }',
        '            parmtag { "script_callback_language" "python" }',
        "            menu {",
        '                [ "import importlib" ]',
        '                [ "from ayon_houdini.nodes.lops import usd_toolkit" ]',
        '                [ "importlib.reload(usd_toolkit)" ]',
        '                [ "return usd_toolkit.payload_menu(kwargs[\\"node\\"])" ]',
        "                language python",
        "            }",
        "        }",
        "        parm {",
        '            name    "loadpayload#"',
        '            label   "Load Payload #"',
        "            type    toggle",
        '            default { "1" }',
        '            parmtag { "cook_dependent" "1" }',
        '            parmtag { "script_callback" "kwargs[\'node\'].cook(force=True)" }',
        '            parmtag { "script_callback_language" "python" }',
        "        }",
        "    }",
    ])


def button(name, label, callback):
    return "\n".join([
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    button",
        '        default { "0" }',
        '        parmtag {{ "script_callback" "{}" }}'.format(callback),
        '        parmtag { "script_callback_language" "python" }',
        "    }",
    ])


def log_parm():
    return "\n".join([
        "    parm {",
        '        name    "log"',
        '        label   "Log"',
        "        type    string",
        '        default { "Press Validate to inspect the input stage." }',
        '        parmtag { "editor" "1" }',
        '        parmtag { "editorlines" "12" }',
        "    }",
    ])


RELOAD_PREFIX = (
    "import importlib; from ayon_houdini.nodes.lops import usd_toolkit; "
    "importlib.reload(usd_toolkit); "
)


TOOLS = (
    {
        "name": "bmfx_usd_validator",
        "label": "BMFX USD Validator",
        "icon": "LOP_usd_rop",
        "tool": "usd_validator",
        "parms": [log_parm(), button("validate", "Validate Stage", RELOAD_PREFIX + "usd_toolkit.validate_stage(kwargs['node'])")],
        "help": "Validate required hierarchy Scopes and missing mesh material bindings.",
    },
    {
        "name": "bmfx_material_validator",
        "label": "BMFX Material Validator",
        "icon": "LOP_materiallibrary",
        "tool": "material_validator",
        "parms": [log_parm(), button("validate", "Validate Materials", RELOAD_PREFIX + "usd_toolkit.validate_materials(kwargs['node'])")],
        "help": "Report unassigned meshes and materials without surface shaders.",
    },
    {
        "name": "bmfx_department_visibility",
        "label": "BMFX Department Visibility",
        "icon": "LOP_prune",
        "tool": "department_visibility",
        "parms": [parm("show_" + name.lower(), name, "toggle", "1") for name in (
            "assets", "character", "crowd", "env", "fx", "layout", "lidar", "lights", "prop", "vehicle"
        )],
        "help": "Author inherited or invisible visibility on BMFX department roots.",
    },
    {
        "name": "bmfx_asset_isolator",
        "label": "BMFX Asset Isolator",
        "icon": "LOP_prune",
        "tool": "asset_isolator",
        "parms": [parm("asset", "Asset", menu_call="asset_menu"), parm("isolate", "Isolate", "toggle", "1")],
        "help": "Isolate one department asset while preserving the complete stage.",
    },
    {
        "name": "bmfx_collection_builder",
        "label": "BMFX Collection Builder",
        "icon": "LOP_collection",
        "tool": "collection_builder",
        "parms": [parm("collectionroot", "Collection Root", default="/World/Collections")],
        "help": "Create expandable USD collections for every BMFX department.",
    },
    {
        "name": "bmfx_payload_controller",
        "label": "BMFX Payload Controller",
        "icon": "LOP_payload",
        "tool": "payload_controller",
        "parms": [payload_multiparm()],
        "help": "Load or unload multiple payloads. Unloaded roots are deactivated so Hydra removes them from the viewport.",
    },
    {
        "name": "bmfx_variant_manager",
        "label": "BMFX Variant Manager",
        "icon": "LOP_setvariant",
        "tool": "variant_manager",
        "parms": [
            parm("primpath", "Prim", menu_call="variant_prim_menu", callback=RELOAD_PREFIX + "usd_toolkit.sync_variant_menus(kwargs['node'])"),
            parm("variantset", "Variant Set", menu_call="variant_set_menu", callback=RELOAD_PREFIX + "usd_toolkit.sync_variant_menus(kwargs['node'])"),
            parm("variant", "Variant", menu_call="variant_menu"),
            parm("createmissing", "Create Missing Variant", "toggle", "1"),
        ],
        "help": "Select a composed USD variant set and variant on any prim.",
    },
    {
        "name": "bmfx_camera_configurator",
        "label": "BMFX Camera Configurator",
        "icon": "LOP_camera",
        "tool": "camera_configurator",
        "parms": [
            parm("camera", "Camera", menu_call="camera_menu"),
            parm("focallength", "Focal Length", "float", "50"),
            parm("aperture", "Horizontal Aperture", "float", "36"),
            parm("nearclip", "Near Clip", "float", "0.1"),
            parm("farclip", "Far Clip", "float", "100000"),
        ],
        "help": "Configure focal length, aperture, and clipping on a USD camera.",
    },
    {
        "name": "bmfx_asset_renamer",
        "label": "BMFX Asset Renamer",
        "icon": "LOP_namespaceedit",
        "tool": "asset_renamer",
        "parms": [
            parm("source", "Asset", menu_call="asset_menu"),
            parm("newname", "New Name"),
            parm("enable", "Apply Rename", "toggle", "0", help_text="Copies the complete composed asset subtree to the new path and deactivates the old path."),
        ],
        "help": "Rename an assembled asset without modifying its upstream source layer.",
    },
    {
        "name": "bmfx_publish_prep",
        "label": "BMFX Publish Prep",
        "icon": "LOP_usd_rop",
        "tool": "publish_prep",
        "parms": [
            parm("defaultprim", "Default Prim", default="/World", menu_call="default_prim_menu"),
            parm("configurehierarchy", "Configure BMFX Hierarchy", "toggle", "1"),
            button("validate", "Validate Before Publish", RELOAD_PREFIX + "usd_toolkit.validate_stage(kwargs['node'])"),
        ],
        "help": "Set the default prim, normalize BMFX hierarchy metadata, and validate before publish.",
    },
)


def _dialog(tool):
    return "\n".join([
        "# Dialog script for {}::1.0 automatically generated".format(tool["name"]),
        "",
        "{",
        "    name\t{}::1.0".format(tool["name"]),
        "    script\t{}::1.0".format(tool["name"]),
        '    label\t"{}"'.format(tool["label"]),
        "",
        "    help {",
        '\t"{}"'.format(tool["help"]),
        "    }",
        "",
        '    inputlabel\t1\t"Input Stage"',
        "",
        "\n".join(tool["parms"]),
        "}",
        "",
    ])


def _replace_file(path, old_name, new_name, old_label, new_label):
    with open(path, "r") as stream:
        data = stream.read()
    data = data.replace(old_name, new_name).replace(old_label, new_label)
    with open(path, "w") as stream:
        stream.write(data)


def build_all():
    work = tempfile.mkdtemp(prefix="bmfx_usd_tools_")
    try:
        template_dir = os.path.join(work, "template")
        subprocess.check_call([HOTL, "-X", template_dir, TEMPLATE])
        operator_dir = next(
            os.path.join(template_dir, item)
            for item in os.listdir(template_dir)
            if os.path.isdir(os.path.join(template_dir, item))
        )
        relative_operator = os.path.basename(operator_dir)

        outputs = []
        for tool in TOOLS:
            source = os.path.join(work, tool["name"])
            shutil.copytree(template_dir, source)
            op_dir = os.path.join(source, relative_operator)
            for relative in (
                "INDEX__SECTION", "Sections.list",
                os.path.join(relative_operator, "CreateScript"),
                os.path.join(relative_operator, "DialogScript"),
                os.path.join(relative_operator, "Help"),
                os.path.join(relative_operator, "Contents.dir", "hdaroot.init"),
            ):
                path = os.path.join(source, relative)
                _replace_file(path, OLD_NAME, tool["name"], OLD_LABEL, tool["label"])

            index_path = os.path.join(source, "INDEX__SECTION")
            with open(index_path, "r") as stream:
                index_data = stream.read()
            index_data = re_sub_icon(index_data, tool["icon"])
            with open(index_path, "w") as stream:
                stream.write(index_data)

            with open(os.path.join(op_dir, "DialogScript"), "w") as stream:
                stream.write(_dialog(tool))
            with open(os.path.join(op_dir, "Help"), "w") as stream:
                stream.write("= {} =\n\n{}\n".format(tool["label"], tool["help"]))

            parm_path = os.path.join(
                op_dir, "Contents.dir", "hdaroot", "configure_departments.parm"
            )
            script = (
                "import importlib\n"
                "from ayon_houdini.nodes.lops import usd_toolkit\n"
                "importlib.reload(usd_toolkit)\n"
                "usd_toolkit.cook_tool(hou.pwd(), hou.pwd().parent(), {!r})"
            ).format(tool["tool"])
            with open(parm_path, "w") as stream:
                stream.write(
                    '{{\nversion 0.8\npython\t[ 0\tlocks=0 ]\t(\t"{}"\t)\n'
                    'maintainstate\t[ 0\tlocks=0 ]\t(\t"off"\t)\n}}\n'.format(script)
                )

            output = os.path.join(OTL_DIR, "lop_{}.1.0.hda".format(tool["name"]))
            subprocess.check_call([HOTL, "-C", source, output])
            outputs.append(output)
        return outputs
    finally:
        shutil.rmtree(work)


def re_sub_icon(index_data, icon):
    lines = []
    for line in index_data.splitlines():
        if line.startswith("Icon:"):
            line = "Icon:         {}".format(icon)
        lines.append(line)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    for path in build_all():
        print(path)
