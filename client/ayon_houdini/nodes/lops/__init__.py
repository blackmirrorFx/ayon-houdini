"""
AYON Houdini LOP Nodes Package.

This package contains LOP (Lightweight Object Protocol) nodes and utilities
for Houdini, including the Material Builder for USD material creation.
"""

# Import material builder components for easy access
try:
    from ayon_houdini.nodes.lops.material_builder import (
        MaterialBuilder,
        MaterialChannel,
        MaterialChannelLibrary,
        UDIMHandler,
    )
    __all__ = [
        "MaterialBuilder",
        "MaterialChannel",
        "MaterialChannelLibrary",
        "UDIMHandler",
    ]
except ImportError:
    pass

__version__ = "1.0.0"
