"""Compatibility wrappers for legacy references.

Core asset import/build logic now lives in `nodes/asset.py`.
"""

from ayon_houdini.nodes import asset as ayon_assets


TASK_ORDER = ayon_assets.TASK_ORDER


def gather_single_asset(node):
    return ayon_assets.gather_single_asset(node)


def gather_asset(node):
    return ayon_assets.gather_single_asset(node)


def gather_assets(node):
    return ayon_assets.gather_assets(node)
