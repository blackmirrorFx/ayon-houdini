"""USD department-root configuration for the BMFX Configure Departments LOP."""

from __future__ import absolute_import

try:
    import hou
except ImportError:  # Allow import by non-Houdini tests.
    hou = None


SCOPE_PATHS = (
    "/Render",
    "/World",
    "/World/Assets",
    "/World/Shot",
    "/World/Shot/Assets",
    "/World/Shot/character",
    "/World/Shot/crowd",
    "/World/Shot/env",
    "/World/Shot/fx",
    "/World/Shot/layout",
    "/World/Shot/lidar",
    "/World/Shot/lights",
    "/World/Shot/prop",
    "/World/Shot/renderCam",
    "/World/Shot/vehicle",
)

ASSET_DEPARTMENT_PATHS = (
    "/World/Assets",
    "/World/Shot/Assets",
    "/World/Shot/character",
    "/World/Shot/crowd",
    "/World/Shot/env",
    "/World/Shot/fx",
    "/World/Shot/layout",
    "/World/Shot/lidar",
    "/World/Shot/lights",
    "/World/Shot/prop",
    "/World/Shot/vehicle",
)

MODEL_GROUP_PATHS = (
    "/World",
    "/World/Shot",
) + ASSET_DEPARTMENT_PATHS


def configure_department_scopes(stage):
    """Create or override the complete BMFX hierarchy as USD Scope prims.

    A stronger ``typeName = Scope`` opinion replaces an upstream Xform type
    without deleting children, metadata, references, or other authored data.
    Missing paths are created automatically; the HDA intentionally has no
    artist-facing configuration.
    """
    if stage is None:
        raise RuntimeError("An editable USD stage is required")

    changed = []
    for path in SCOPE_PATHS:
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid() and prim.GetTypeName() == "Scope":
            continue
        stage.DefinePrim(path, "Scope")
        changed.append(path)
    return changed


def configure_asset_kinds(stage):
    """Mark department assets as components and their meshes as subcomponents.

    A direct Xform child of an asset department is treated as an asset root.
    Mesh descendants retain their Mesh schema and receive ``subcomponent``
    kind metadata, producing a valid and useful USD model hierarchy.
    """
    if stage is None:
        raise RuntimeError("An editable USD stage is required")

    from pxr import Kind, Usd

    changed = []
    for group_path in MODEL_GROUP_PATHS:
        group_prim = stage.GetPrimAtPath(group_path)
        if not group_prim or not group_prim.IsValid():
            continue
        group_model = Usd.ModelAPI(group_prim)
        if group_model.GetKind() != Kind.Tokens.group:
            group_model.SetKind(Kind.Tokens.group)
            changed.append(group_path)

    for department_path in ASSET_DEPARTMENT_PATHS:
        department_prim = stage.GetPrimAtPath(department_path)
        if not department_prim or not department_prim.IsValid():
            continue

        for asset_prim in department_prim.GetChildren():
            if asset_prim.GetTypeName() != "Xform":
                continue

            asset_model = Usd.ModelAPI(asset_prim)
            if asset_model.GetKind() != Kind.Tokens.component:
                asset_model.SetKind(Kind.Tokens.component)
                changed.append(asset_prim.GetPath().pathString)

            for descendant in Usd.PrimRange(asset_prim):
                if descendant == asset_prim or descendant.GetTypeName() != "Mesh":
                    continue
                mesh_model = Usd.ModelAPI(descendant)
                if mesh_model.GetKind() == Kind.Tokens.subcomponent:
                    continue
                mesh_model.SetKind(Kind.Tokens.subcomponent)
                changed.append(descendant.GetPath().pathString)
    return changed


def cook_configure_departments(python_lop=None, settings_node=None):
    """Cook entry point for the HDA's internal Python Script LOP."""
    if hou is None:
        raise RuntimeError("cook_configure_departments must run inside Houdini")

    python_lop = python_lop or hou.pwd()
    stage = python_lop.editableStage()
    scope_changes = configure_department_scopes(stage)
    kind_changes = configure_asset_kinds(stage)
    return scope_changes + kind_changes
