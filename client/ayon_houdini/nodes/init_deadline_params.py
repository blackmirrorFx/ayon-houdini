"""
Initialize Deadline machine list parameters on existing nodes in the current Houdini scene.

Usage (run in Houdini Python shell):

from ayon_houdini.nodes import init_deadline_params
init_deadline_params.setup_all_nodes()

What it does:
- For every node in the scene it will try these in order:
  1) If the node's HDA module exposes `on_node_created(node)` it will call it.
  2) If the node type name contains `filecache` it will call
     `ayon_houdini.nodes.filecache.ensure_deadline_machine_list_parms(node)`
  3) If the node type name contains `farmer` it will call
     `ayon_houdini.nodes.farmer.on_node_created(node)` if available.

This should make the `machine_list` and `machine_list_is_deny` parameters appear
on existing nodes without editing the HDA files.
"""

import hou
import importlib

from . import filecache as fc_module
from . import farmer as farmer_module


def _safe_call(func, node):
    try:
        func(node)
        return True
    except Exception as exc:
        try:
            print(f"init_deadline_params: call failed on {node.path()}: {exc}")
        except Exception:
            pass
        return False


def setup_all_nodes():
    """Initialize all relevant nodes in the current Houdini session."""
    root = hou.node("/")
    if not root:
        print("No Houdini root node found.")
        return

    count = 0
    touched = []

    for node in root.allSubChildren():
        # 1) If HDA module has on_node_created, call it
        try:
            module = None
            try:
                module = node.hdaModule()
            except Exception:
                module = None

            if module and hasattr(module, "on_node_created"):
                try:
                    module.on_node_created(node)
                    touched.append(node.path())
                    count += 1
                    continue
                except Exception:
                    pass
        except Exception:
            pass

        # 2) FileCache nodes (SOP or LOP)
        try:
            tname = node.type().name().lower()
        except Exception:
            tname = ""

        if "filecache" in tname or "bmfx" in tname:
            if hasattr(fc_module, "ensure_deadline_machine_list_parms"):
                if _safe_call(fc_module.ensure_deadline_machine_list_parms, node):
                    touched.append(node.path())
                    count += 1
                    continue

        # 3) Farmer nodes
        if "farmer" in tname or "farm" in tname:
            if hasattr(farmer_module, "on_node_created"):
                if _safe_call(farmer_module.on_node_created, node):
                    touched.append(node.path())
                    count += 1
                    continue

    print(f"init_deadline_params: initialized {count} nodes")
    if touched:
        print("Touched nodes:\n" + "\n".join(touched))


if __name__ == "__main__":
    setup_all_nodes()
