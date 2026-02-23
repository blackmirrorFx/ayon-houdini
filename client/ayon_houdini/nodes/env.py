import hou
import os
import json
from ayon_houdini.nodes import filecache
# --------------------------------------------------
# AYON CONTEXT SERIALIZATION
# --------------------------------------------------

def get_ayon_launcher_env():
    return {
        k: v for k, v in os.environ.items()
        if k.startswith(("AYON_", "OPENPYPE_", "AVALON_"))
    }


def get_all_houdini_vars():
    raw = hou.hscript("set")[0]
    data = {}

    for line in raw.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = hou.expandString(v.strip())

    return data


def filter_custom_houdini_vars(vars_dict):
    ALLOWED_PREFIXES = (
        "AYON_",
        "OPENPYPE_",
        "AVALON_",
        "TH_",
        "RES",
        "PIX_",
    )

    ALLOWED_EXACT = {
        "JOB",
        "SEQ",
        "SHOT",
        "APP",
        "LIB",
    }

    return {
        k: v for k, v in vars_dict.items()
        if k in ALLOWED_EXACT or k.startswith(ALLOWED_PREFIXES)
    }


def ayon_context_json_path(node, version):
    ver_dir = version_dir(node, version, create=True)
    return os.path.join(ver_dir, ".ayon_vars.json")


def save_ayon_context_for_node(node, version):
    path = ayon_context_json_path(node, version)

    data = {
        "launcher_env": get_ayon_launcher_env(),
        "houdini_vars": filter_custom_houdini_vars(
            get_all_houdini_vars()
        ),
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=4)

    print("[AYON] Context JSON saved")
    print(f"       {path}")
