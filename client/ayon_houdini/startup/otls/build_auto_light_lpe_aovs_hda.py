"""Build the BMFX Auto Light LPE AOVs Solaris HDA.

Run with Houdini's Python, for example::

    hython startup/otls/build_auto_light_lpe_aovs_hda.py
"""

from __future__ import absolute_import

import os

import hou


TYPE_NAME = "bmfx_auto_lpe_aovs::1.0"
TYPE_LABEL = "BMFX Auto LPE AOVs"
FILE_NAME = "lop_bmfx_auto_lpe_aovs.1.0.hda"

SPLIT_AOV_GROUPS = (
    ("Beauty", (
        ("beauty", "Beauty"),
        ("beautyunshadowed", "Beauty Unshadowed"),
        ("shadow", "Shadow"),
    )),
    ("Diffuse", (
        ("combineddiffuse", "Combined Diffuse"),
        ("directdiffuse", "Direct Diffuse"),
        ("indirectdiffuse", "Indirect Diffuse"),
        ("combineddiffuseunshadowed", "Combined Diffuse Unshadowed"),
        ("directdiffuseunshadowed", "Direct Diffuse Unshadowed"),
        ("indirectdiffuseunshadowed", "Indirect Diffuse Unshadowed"),
        ("combineddiffuseshadow", "Combined Diffuse Shadow"),
        ("directdiffuseshadow", "Direct Diffuse Shadow"),
        ("indirectdiffuseshadow", "Indirect Diffuse Shadow"),
    )),
    ("Glossy Reflection", (
        ("combinedglossyreflection", "Combined Glossy Reflection"),
        ("directglossyreflection", "Direct Glossy Reflection"),
        ("indirectglossyreflection", "Indirect Glossy Reflection"),
    )),
    ("Glossy Transmission", (
        ("glossytransmission", "Glossy Transmission"),
    )),
    ("Volume", (
        ("combinedvolume", "Combined Volume"),
        ("directvolume", "Direct Volume"),
        ("indirectvolume", "Indirect Volume"),
    )),
    ("Other Lighting", (
        ("visiblelights", "Visible Lights"),
        ("coat", "Coat"),
        ("sss", "SSS"),
    )),
)

COOK_SCRIPT = """import importlib
from ayon_houdini.nodes.lops import light_lpe_aovs
importlib.reload(light_lpe_aovs)
light_lpe_aovs.cook_light_lpe_aovs(hou.pwd(), hou.pwd().parent())
"""

HELP = """= BMFX Auto Light LPE AOVs =

Detects every active USD light below **Light Scope** and creates a stable,
unique LPE tag from its prim name. The tag is authored for both RenderMan and
Karma. It mirrors every Karma lighting AOV that supports **Split per LPE Tag**.
Beauty remains enabled by default; all other split AOVs are opt-in. Generated
beauty RenderVar prims use Karma's fixed `beauty_<tag>` naming convention and
their image channels use `C_<tag>`. The naming is intentionally not exposed as
a user-editable prefix. Generated RenderVars are appended to every existing
RenderProduct.

RenderMan attribute:
`primvars:ri:attributes:identifier:lpegroup`

Karma attribute:
`inputs:karma:light:lpetag`

The production-tested per-light beauty expression is
`C[DS]*<L.'tag'>`. Each channel uses float3 storage and a float3 USD data type.
Existing ordered RenderVars are preserved. AOVs previously created by this node
are refreshed on every cook, so adding, removing or renaming a light
automatically updates the result.

Place this node after lights and after the node that creates RenderProducts.
"""


def _string(name, label, default, help_text):
    return hou.StringParmTemplate(
        name, label, 1, default_value=(default,), help=help_text
    )


def _toggle(name, label, default, help_text):
    return hou.ToggleParmTemplate(
        name, label, default_value=default, help=help_text
    )


def _parameter_templates():
    folder = hou.FolderParmTemplate(
        "lpe_settings", "Automatic Light LPE AOVs"
    )
    folder.addParmTemplate(_toggle(
        "enabled",
        "Enable",
        True,
        "Disable to make the asset a pass-through without authoring tags or AOVs.",
    ))
    folder.addParmTemplate(_string(
        "scope_root",
        "Light Scope",
        "/",
        "Only detect lights at or below this absolute USD prim path.",
    ))
    folder.addParmTemplate(_string(
        "tag_prefix",
        "LPE Tag Prefix",
        "",
        "Prefix for automatically generated renderer LPE tags.",
    ))
    folder.addParmTemplate(_toggle(
        "preserve_existing_tags",
        "Preserve Existing Tags",
        True,
        "Reuse valid existing RenderMan or Karma light tags when possible.",
    ))
    folder.addParmTemplate(_string(
        "aov_root",
        "RenderVar Root",
        "/Render/Products/Vars",
        "Absolute USD path below which per-light RenderVars are created.",
    ))
    folder.addParmTemplate(_toggle(
        "bind_products",
        "Add to Render Products",
        True,
        "Append generated AOVs to orderedVars on every existing RenderProduct.",
    ))
    for group_index, (group_label, aovs) in enumerate(SPLIT_AOV_GROUPS):
        group = hou.FolderParmTemplate(
            "split_group_{}".format(group_index),
            group_label,
            folder_type=hou.folderType.Collapsible,
        )
        for key, label in aovs:
            group.addParmTemplate(_toggle(
                "split_{}".format(key),
                "{}: Split per LPE Tag".format(label),
                key == "beauty",
                "Create one {} channel for each detected light LPE tag.".format(
                    label
                ),
            ))
        folder.addParmTemplate(group)
    return folder


def build(output_path=None):
    output_path = output_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), FILE_NAME
    )

    stage = hou.node("/stage")
    if stage is None:
        stage = hou.node("/").createNode("lopnet", "stage")

    subnet = stage.createNode("subnet", "bmfx_auto_light_lpe_aovs_build")
    python_lop = subnet.createNode("pythonscript", "author_light_lpe_aovs")
    python_lop.parm("python").set(COOK_SCRIPT)
    python_lop.setInput(0, subnet.indirectInputs()[0])

    output = subnet.node("output0")
    if output is None:
        output = subnet.createNode("output", "output0")
    output.setInput(0, python_lop)
    python_lop.setPosition(hou.Vector2(0, 0))
    output.setPosition(hou.Vector2(0, -1.5))

    asset = subnet.createDigitalAsset(
        name=TYPE_NAME,
        hda_file_name=output_path,
        description=TYPE_LABEL,
        min_num_inputs=1,
        max_num_inputs=1,
    )
    definition = asset.type().definition()

    parm_group = hou.ParmTemplateGroup()
    parm_group.append(_parameter_templates())
    definition.setParmTemplateGroup(parm_group)
    definition.addSection("Help", HELP)

    options = definition.options()
    options.setLockContents(True)
    options.setSaveSpareParms(True)
    definition.setOptions(options)
    definition.updateFromNode(asset)
    definition.save(output_path, asset, options)

    print("Built {} at {}".format(TYPE_NAME, output_path))
    hou.exit()


if __name__ == "__main__":
    build()
