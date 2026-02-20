"""
Setup script to initialize FileCache nodes with Deadline machine list parameters.

This can be run from Houdini's Python panel or as a startup script.
It adds the deadline machine list parameters to all existing FileCache nodes.
"""

import hou
from . import filecache


def setup_all_filecache_nodes():
    """
    Finds all FileCache nodes in the current hip and initializes their
    deadline machine list parameters.
    """
    for node in hou.node("/").allSubChildren():
        if node.type().name() == "Sop_FileCache" or "filecache" in node.type().name().lower():
            try:
                filecache.on_node_created(node)
                print(f"✓ Initialized deadline parameters on: {node.path()}")
            except Exception as e:
                print(f"✗ Failed to initialize {node.path()}: {e}")


if __name__ == "__main__":
    setup_all_filecache_nodes()
