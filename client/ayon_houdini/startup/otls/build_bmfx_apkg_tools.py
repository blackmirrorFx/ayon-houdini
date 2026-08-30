"""Build the BMFX APKG Begin, End and Publish HDAs with SideFX ``hotl``.

The build is license-free and repeatable: it expands known-good Houdini assets,
edits their textual sections and compiles the resulting libraries.
"""

from __future__ import absolute_import

import os
import shutil
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OTL_DIR = os.path.join(ROOT, "startup", "otls")
LOP_TEMPLATE = os.path.join(OTL_DIR, "lop_bmfx_configure_departments.1.0.hda")
LAYERBREAK_TEMPLATE = os.path.join(OTL_DIR, "lop_import_asset_layer.1.0.hda")
ROP_TEMPLATE = os.path.join(OTL_DIR, "driver_farmer.1.0.hda")
HOTL = "/opt/hfs21.0/bin/hotl"


def _read(path):
    with open(path, "r") as stream:
        return stream.read()


def _write(path, value):
    with open(path, "w") as stream:
        stream.write(value)


def _replace(path, replacements):
    value = _read(path)
    for old, new in replacements:
        value = value.replace(old, new)
    _write(path, value)


def _operator_dir(expanded):
    return next(
        os.path.join(expanded, item)
        for item in os.listdir(expanded)
        if os.path.isdir(os.path.join(expanded, item))
    )


def _button(name, label, callback, invisible=False):
    hidden = "\n        invisible" if invisible else ""
    return """
    parm {{
        name    "{name}"
        label   "{label}"
        type    button
        default {{ "0" }}{hidden}
        parmtag {{ "script_callback" "{callback}" }}
        parmtag {{ "script_callback_language" "python" }}
    }}""".format(
        name=name,
        label=label,
        hidden=hidden,
        callback=callback.replace('"', '\\"'),
    )


def _string(
    name, label, default="", invisible=False, editor=False, help_text="",
    callback="", hidewhen="", disablewhen="",
):
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    string",
        '        default {{ "{}" }}'.format(default.replace('"', '\\"')),
    ]
    if invisible:
        lines.append("        invisible")
    if editor:
        lines.extend([
            '        parmtag { "editor" "1" }',
            '        parmtag { "editorlines" "8" }',
        ])
    if help_text:
        lines.append('        help    "{}"'.format(help_text.replace('"', '\\"')))
    if hidewhen:
        lines.append('        hidewhen "{}"'.format(hidewhen.replace('"', '\\"')))
    if disablewhen:
        lines.append(
            '        disablewhen "{}"'.format(
                disablewhen.replace('"', '\\"')
            )
        )
    if callback:
        lines.extend([
            '        parmtag {{ "script_callback" "{}" }}'.format(
                callback.replace('"', '\\"')
            ),
            '        parmtag { "script_callback_language" "python" }',
        ])
    lines.append("    }")
    return "\n".join(lines)


def _python_string(name, label, expression, editor_lines=5, help_text=""):
    """Build a read-only string parameter driven by a Python return value."""
    escaped = (
        expression.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    string",
        '        default {{ [ "{}" python ] }}'.format(escaped),
        '        parmtag { "editor" "1" }',
        '        parmtag {{ "editorlines" "{}" }}'.format(editor_lines),
        '        disablewhen "{ information_locked == 1 }"',
    ]
    if help_text:
        lines.append('        help    "{}"'.format(help_text.replace('"', '\\"')))
    lines.append("    }")
    return "\n".join(lines)


def _python_value(name, label, expression, lock_parm, help_text=""):
    """Build a compact read-only string driven by a Python expression."""
    escaped = (
        expression.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    string",
        '        default {{ [ "{}" python ] }}'.format(escaped),
        '        disablewhen "{{ {} == 1 }}"'.format(lock_parm),
    ]
    if help_text:
        lines.append('        help    "{}"'.format(help_text.replace('"', '\\"')))
    lines.append("    }")
    return "\n".join(lines)


def _toggle(
    name, label, default=False, callback="", help_text="", invisible=False,
    hidewhen="",
):
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    toggle",
        '        default {{ "{}" }}'.format(1 if default else 0),
    ]
    if invisible:
        lines.append("        invisible")
    if hidewhen:
        lines.append('        hidewhen "{}"'.format(hidewhen))
    if help_text:
        lines.append('        help    "{}"'.format(help_text.replace('"', '\\"')))
    if callback:
        lines.extend([
            '        parmtag {{ "script_callback" "{}" }}'.format(
                callback.replace('"', '\\"')
            ),
            '        parmtag { "script_callback_language" "python" }',
        ])
    lines.append("    }")
    return "\n".join(lines)


def _ordinal(
    name, label, items, default=0, callback="", help_text="",
    hidewhen="",
):
    lines = [
        "    parm {",
        '        name    "{}"'.format(name),
        '        label   "{}"'.format(label),
        "        type    ordinal",
        '        default {{ "{}" }}'.format(int(default)),
        "        menu {",
    ]
    for token, item_label in items:
        lines.append(
            '            "{}" "{}"'.format(token, item_label)
        )
    lines.append("        }")
    if hidewhen:
        lines.append('        hidewhen "{}"'.format(hidewhen))
    if help_text:
        lines.append('        help    "{}"'.format(help_text.replace('"', '\\"')))
    if callback:
        lines.extend([
            '        parmtag {{ "script_callback" "{}" }}'.format(
                callback.replace('"', '\\"')
            ),
            '        parmtag { "script_callback_language" "python" }',
        ])
    lines.append("    }")
    return "\n".join(lines)


def _begin_dialog():
    callback = "from ayon_houdini.nodes.lops import apkg; apkg.{}(kwargs['node'])"
    return "\n".join([
        "# BMFX APKG Begin",
        "{",
        "    name\tbmfx_apkg_begin::1.0",
        "    script\tbmfx_apkg_begin::1.0",
        '    label\t"BMFX APKG Begin"',
        '    inputlabel\t1\t"Optional Input Stage"',
        "    group {",
        '        name "build"',
        '        label "Shot Builder"',
        _button(
            "open_builder", "Open Shot Builder...",
            callback.format("open_shot_builder"),
        ),
        _toggle("information_locked", "Information Locked", True, invisible=True),
        _string("selection_json", "Selection Lock", "", invisible=True),
        _python_string(
            "ayon_context_display", "AYON Context",
            "from ayon_houdini.nodes.lops import apkg\n"
            "return apkg.begin_context_display(hou.pwd())",
            editor_lines=5,
            help_text="Live values returned from the AYON database.",
        ),
        _python_string(
            "loaded_apkgs_display", "Loaded APKGs",
            "from ayon_houdini.nodes.lops import apkg\n"
            "return apkg.begin_loaded_apkgs_display(hou.pwd())",
            editor_lines=8,
            help_text=(
                "Exact APKG products, versions and statuses locked by this "
                "Begin node."
            ),
        ),
        # Retain legacy callback parms invisibly so old scripts and shelf tools
        # continue to work. All artist-facing actions live in the modern UI.
        _button("clear_all", "Clear All", callback.format("clear_all"), True),
        _button("build_auto", "Build", callback.format("build_automatic"), True),
        _button("build_manual", "Build Manual", callback.format("build_manual"), True),
        _button(
            "refresh_info", "Refresh Information",
            callback.format("refresh_build_information"),
            True,
        ),
        _string("build_summary", "State", "No APKGs loaded", invisible=True),
        _string(
            "build_information", "Shot Builder Information",
            "Shot Builder is ready. Build or load APKGs to see details.",
            editor=True,
            help_text=(
                "Read-only snapshot of the AYON context and every exact APKG "
                "version currently loaded by this block."
            ),
            disablewhen="{ information_locked == 1 }",
            invisible=True,
        ),
        "    }",
        _string("status_priority", "Status Priority", "approved,available,pending", invisible=True),
        _string("automatic_departments", "Automatic Departments", "", invisible=True),
        _string("optional_departments", "Optional Departments", "", invisible=True),
        _toggle(
            "include_downstream_lighting", "Include Downstream Lighting",
            False, invisible=True,
        ),
        _toggle(
            "merge_current_department", "Merge Current Context",
            False, invisible=True,
        ),
        _toggle(
            "remove_obsolete_apkgs", "Remove Obsolete APKGs",
            False, invisible=True,
        ),
        _string("builder_id", "Builder ID", "", invisible=True),
        "}",
        "",
    ])


def _end_dialog():
    callback = (
        "from ayon_houdini.nodes.lops import apkg; "
        "apkg.update_end_identity(kwargs['node'])"
    )
    usd_check_callback = (
        "from ayon_houdini.nodes.lops import apkg; "
        "apkg.check_department_usd_callback(kwargs['node'])"
    )
    return "\n".join([
        "# BMFX APKG End",
        "{",
        "    name\tbmfx_apkg_end::1.0",
        "    script\tbmfx_apkg_end::1.0",
        '    label\t"BMFX APKG End"',
        '    inputlabel\t1\t"APKG Contribution"',
        "    group {",
        '        name "identity"',
        '        label "Package Identity"',
        _toggle(
            "publish_info_locked", "Publish Information Locked", True,
            invisible=True,
        ),
        _toggle(
            "cfx_kind_enabled", "CFX Type Enabled", False,
            invisible=True,
        ),
        _toggle(
            "split_enabled", "Split Enabled", False,
            invisible=True,
        ),
        _ordinal(
            "cfx_kind", "CFX Type",
            (
                ("hair", "Hair"),
                ("cloth", "Cloth"),
                ("crowd", "Crowd"),
                ("crowdcfx", "Crowd CFX"),
            ),
            callback=callback,
            hidewhen="{ cfx_kind_enabled == 0 }",
            help_text=(
                "Classifies CFX/Crowd output and becomes part of the APKG "
                "product name."
            ),
        ),
        _toggle(
            "use_split", "Use Split", False, callback=callback,
            hidewhen="{ split_enabled == 0 }",
            help_text=(
                "Enable only when this task publishes independent packages "
                "such as smoke, dust, debris or destruction."
            ),
        ),
        _string(
            "split", "Split", "main", callback=callback,
            hidewhen="{ use_split == 0 }",
            help_text=(
                "Optional package split used in the canonical APKG product "
                "name."
            ),
        ),
        _python_value(
            "published_name_display", "Published Name",
            "from ayon_houdini.nodes.lops import apkg\n"
            "return apkg.end_publish_name_display(hou.pwd())",
            "publish_info_locked",
            help_text=(
                "Canonical APKG name with the next AYON database version. "
                "AYON assigns the authoritative version during publishing."
            ),
        ),
        _button(
            "check_department_usd", "Check Department USD",
            usd_check_callback,
        ),
        _string(
            "department_usd_status", "Department USD", "Not checked",
            disablewhen="{ publish_info_locked == 1 }",
        ),
        "    }",
        "}",
        "",
    ])


def _publisher_dialog():
    publish_callback = (
        "from ayon_houdini.nodes.lops import apkg; "
        "apkg.publish_from_node_callback(kwargs['node'])"
    )
    return "\n".join([
        "# BMFX APKG Publisher LOP",
        "{",
        "    name\tbmfx_apkg_publish::1.0",
        "    script\tbmfx_apkg_publish::1.0",
        '    label\t"BMFX APKG Publisher"',
        '    inputlabel\t1\t"APKG End"',
        "    group {",
        '        name "publish"',
        '        label "APKG Publish"',
        _toggle(
            "publish_info_locked", "Publish Information Locked", True,
            invisible=True,
        ),
        _python_value(
            "published_name_display", "Published Name",
            "from ayon_houdini.nodes.lops import apkg\n"
            "return apkg.publisher_publish_name_display(hou.pwd())",
            "publish_info_locked",
            help_text=(
                "Next available AYON version at preview time. AYON assigns "
                "the authoritative version during publishing."
            ),
        ),
        _button("publish_apkg", "Publish APKG", publish_callback),
        "    }",
        _string("lopoutput", "Staging USD", "$HIP/ayon/apkg.usd", invisible=True),
        "}",
        "",
    ])


def _payload_dialog():
    return "\n".join([
        "# BMFX APKG Payload",
        "{",
        "    name\tbmfx_apkg_payload::1.0",
        "    script\tbmfx_apkg_payload::1.0",
        '    label\t"BMFX APKG Payload"',
        '    inputlabel\t1\t"Incoming Shot Stage"',
        "    group {",
        '        name "payload"',
        '        label "APKG Payload"',
        _toggle(
            "load_payload", "Load Payload", True,
            help_text=(
                "Mute or restore this exact APKG payload without deleting "
                "its locked build record."
            ),
        ),
        _toggle("information_locked", "Information Locked", True, invisible=True),
        _string(
            "department_display", "Department", "--",
            disablewhen="{ information_locked == 1 }",
        ),
        _string(
            "package_display", "Package", "--",
            disablewhen="{ information_locked == 1 }",
        ),
        _string(
            "status_display", "Status", "--",
            disablewhen="{ information_locked == 1 }",
        ),
        "    }",
        _string("payload_spec_json", "Payload Lock", "", invisible=True),
        "}",
        "",
    ])


def _rop_dialog():
    return "\n".join([
        "# BMFX APKG Publish ROP",
        "{",
        "    name\tbmfx_apkg_publish::1.0",
        "    script\tbmfx_apkg_publish::1.0",
        '    label\t"BMFX APKG Publish"',
        "    parm {",
        '        name "execute"',
        "        baseparm",
        '        label "Render"',
        "        invisible",
        "        export none",
        "    }",
        _string("loppath", "APKG End", ""),
        _string("lopoutput", "Staging USD", "$HIP/ayon/apkg.usd"),
        "}",
        "",
    ])


def _block_tool_shelf():
    return """<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <tool name="$HDA_DEFAULT_TOOL" label="BMFX APKG Block" icon="$HDA_ICON">
    <toolMenuContext name="viewer">
      <contextNetType>LOP</contextNetType>
    </toolMenuContext>
    <toolMenuContext name="network">
      <contextOpType>$HDA_TABLE_AND_NAME</contextOpType>
    </toolMenuContext>
    <toolSubmenu>BMFX</toolSubmenu>
    <script scriptType="python"><![CDATA[import importlib
from ayon_houdini.nodes.lops import apkg
importlib.reload(apkg)
apkg.create_block(kwargs)]]></script>
  </tool>
</shelfDocument>
"""


def _add_layerbreak(op_dir, layerbreak_source):
    content_dir = os.path.join(op_dir, "Contents.dir")
    target_root = os.path.join(content_dir, "hdaroot")
    source_root = os.path.dirname(layerbreak_source)
    for extension in ("init", "def", "parm", "userdata"):
        source = os.path.join(source_root, "layerbreak." + extension)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(target_root, "layerbreak." + extension))

    layer_def = os.path.join(target_root, "layerbreak.def")
    value = _read(layer_def).replace("sublayer", "configure_departments")
    _write(layer_def, value)
    output_def = os.path.join(target_root, "output0.def")
    _write(output_def, _read(output_def).replace(
        "configure_departments", "layerbreak"
    ))
    _write(os.path.join(target_root, "hdaroot.order"),
           "3\nconfigure_departments\nlayerbreak\noutput0\n")

    contents = os.path.join(op_dir, "Contents.contents")
    lines = _read(contents).splitlines()
    marker = "hdaroot/output0.init"
    index = lines.index(marker)
    additions = [
        "hdaroot/layerbreak.init", "hdaroot/layerbreak.def",
        "hdaroot/layerbreak.parm", "hdaroot/layerbreak.userdata",
    ]
    lines[index:index] = additions
    _write(contents, "\n".join(lines) + "\n")


def _build_lop(
    work, layerbreak_source, name, label, dialog, cook_function,
    begin=False, icon=None, optional_input=False,
):
    expanded = os.path.join(work, name)
    subprocess.check_call([HOTL, "-X", expanded, LOP_TEMPLATE])
    op_dir = _operator_dir(expanded)
    replacements = (
        ("bmfx_configure_departments", name),
        ("BMFX Configure Departments", label),
    )
    for path in (
        os.path.join(expanded, "INDEX__SECTION"),
        os.path.join(expanded, "Sections.list"),
        os.path.join(op_dir, "CreateScript"),
        os.path.join(op_dir, "Help"),
        os.path.join(op_dir, "Tools.shelf"),
    ):
        _replace(path, replacements)
    tools_path = os.path.join(op_dir, "Tools.shelf")
    if begin:
        _write(tools_path, _block_tool_shelf())
    else:
        # End is an implementation detail created only by the APKG Block tool.
        _write(tools_path, "<shelfDocument/>\n")
    _write(os.path.join(op_dir, "DialogScript"), dialog)
    script_path = os.path.join(
        op_dir, "Contents.dir", "hdaroot", "configure_departments.parm"
    )
    script = (
        "from ayon_houdini.nodes.lops import apkg\n"
        "apkg.{}(hou.pwd())"
    ).format(cook_function)
    _write(
        script_path,
        "{\nversion 0.8\npython\t[ 0\tlocks=0 ]\t(\t\""
        + script
        + "\"\t)\nmaintainstate\t[ 0\tlocks=0 ]\t(\t\"off\"\t)\n}\n",
    )
    if begin or optional_input:
        index_path = os.path.join(expanded, "INDEX__SECTION")
        _write(index_path, _read(index_path).replace(
            "Inputs:       1 to 1", "Inputs:       0 to 1"
        ))
    if begin:
        _add_layerbreak(op_dir, layerbreak_source)
    if icon:
        index_path = os.path.join(expanded, "INDEX__SECTION")
        index_lines = _read(index_path).splitlines()
        index_lines = [
            "Icon:         {}".format(icon)
            if line.startswith("Icon:") else line
            for line in index_lines
        ]
        _write(index_path, "\n".join(index_lines) + "\n")
    _write(os.path.join(op_dir, "Help"), "= {} =\n\n{}\n".format(label, label))
    output = os.path.join(OTL_DIR, "lop_{}.1.0.hda".format(name))
    subprocess.check_call([HOTL, "-C", expanded, output])
    return output


def _build_rop(work):
    name = "bmfx_apkg_publish"
    label = "BMFX APKG Publish"
    # Keep this legacy Driver expansion separate from the current LOP HDA,
    # which intentionally uses the same operator name in another category.
    expanded = os.path.join(work, name + "_legacy_driver")
    subprocess.check_call([HOTL, "-X", expanded, ROP_TEMPLATE])
    op_dir = _operator_dir(expanded)
    replacements = (("farmer", name), ("BMFX Farmer", label))
    for path in (
        os.path.join(expanded, "INDEX__SECTION"),
        os.path.join(op_dir, "CreateScript"),
        os.path.join(op_dir, "Help"),
        os.path.join(op_dir, "Tools.shelf"),
    ):
        _replace(path, replacements)
    tools_path = os.path.join(op_dir, "Tools.shelf")
    # Retain the Driver definition only for opening legacy scenes. New APKG
    # publishing adopts the native USD ROP LOP created in /stage, so the old
    # /out node must not be offered in the Tab menu.
    _write(
        tools_path,
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<shelfDocument/>\n',
    )
    # Keep the encoded on-disk section key (Driver_1farmer_8_81.0) because
    # that is the extracted directory name; change only its operator mapping.
    sections_path = os.path.join(expanded, "Sections.list")
    _write(
        sections_path,
        _read(sections_path).replace(
            "Driver/farmer::1.0", "Driver/{}::1.0".format(name)
        ),
    )
    on_created = os.path.join(op_dir, "OnCreated")
    if os.path.exists(on_created):
        _write(on_created, "")
    _write(os.path.join(op_dir, "DialogScript"), _rop_dialog())
    index_path = os.path.join(expanded, "INDEX__SECTION")
    index_data = _read(index_path)
    # The earlier global operator-name replacement also changes the index
    # label. Replace that generated value explicitly with the human label.
    index_data = index_data.replace(
        "Label:        " + name, "Label:        " + label
    )
    index_data = index_data.replace("Inputs:       0 to 1", "Inputs:       0 to 0")
    index_data = index_data.replace("Icon:         opdef:/Driver/{}::1.0?IconSVG".format(name), "Icon:         LOP_usd_rop")
    _write(index_path, index_data)
    output = os.path.join(OTL_DIR, "driver_{}.1.0.hda".format(name))
    subprocess.check_call([HOTL, "-C", expanded, output])
    return output


def build_all():
    work = tempfile.mkdtemp(prefix="bmfx_apkg_hdas_")
    try:
        layer_expanded = os.path.join(work, "layer_source")
        subprocess.check_call([HOTL, "-X", layer_expanded, LAYERBREAK_TEMPLATE])
        layer_op_dir = _operator_dir(layer_expanded)
        layerbreak_source = os.path.join(
            layer_op_dir, "Contents.dir", "hdaroot", "layerbreak.init"
        )
        outputs = [
            _build_lop(
                work, layerbreak_source,
                "bmfx_apkg_begin", "BMFX APKG Begin", _begin_dialog(),
                "cook_begin", begin=True,
            ),
            _build_lop(
                work, layerbreak_source,
                "bmfx_apkg_end", "BMFX APKG End", _end_dialog(),
                "cook_end", begin=False,
            ),
            _build_lop(
                work, layerbreak_source,
                "bmfx_apkg_publish", "BMFX APKG Publisher",
                _publisher_dialog(), "cook_publish", begin=False,
                icon="LOP_usd_rop",
            ),
            _build_lop(
                work, layerbreak_source,
                "bmfx_apkg_payload", "BMFX APKG Payload",
                _payload_dialog(), "cook_payload", begin=False,
                icon="LOP_payload", optional_input=True,
            ),
            _build_rop(work),
        ]
        return outputs
    finally:
        shutil.rmtree(work)


if __name__ == "__main__":
    for path in build_all():
        print(path)
