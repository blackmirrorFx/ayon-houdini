"""
Debug script to inspect node parameters and their structure.

Usage (run in Houdini Python shell):

from ayon_houdini.nodes import debug_node_params
# Select a node in Houdini, then:
debug_node_params.print_node_params()
# Or pass a node path:
debug_node_params.print_node_params("/path/to/node")
"""

import hou


def print_node_params(node_path=None):
    """Print all parameters in a node to help debug parameter placement."""
    if node_path is None:
        # Try to get selected node
        try:
            node = hou.selectedNodes()[0]
        except (IndexError, AttributeError):
            print("No node selected. Pass a node path or select a node.")
            return
    else:
        node = hou.node(node_path)
        if not node:
            print(f"Node not found: {node_path}")
            return

    print(f"\n=== Parameters in {node.path()} ===\n")
    
    try:
        group = node.parmTemplateGroup()
    except Exception as e:
        print(f"Error getting parm group: {e}")
        return
    
    def print_parms(ptg, indent=0):
        for parm_template in ptg.parmTemplates():
            name = parm_template.name()
            label = parm_template.label()
            ptype = type(parm_template).__name__
            prefix = "  " * indent
            print(f"{prefix}{name:30s} | {label:30s} | {ptype}")
            
            # If it's a folder, print contents
            if isinstance(parm_template, hou.FolderParmTemplate):
                print(f"{prefix}  [FOLDER CONTENTS:]")
                print_parms(parm_template, indent + 2)
    
    print_parms(group)
    print("\n")


if __name__ == "__main__":
    print_node_params()
