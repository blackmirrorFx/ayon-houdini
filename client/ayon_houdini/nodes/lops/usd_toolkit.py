"""Shared implementation for the BMFX USD utility LOP HDA suite."""

from __future__ import absolute_import

import re

try:
    import hou
except ImportError:
    hou = None

from ayon_houdini.nodes.lops.department_config import (
    ASSET_DEPARTMENT_PATHS,
    SCOPE_PATHS,
    configure_asset_kinds,
    configure_department_scopes,
)


DEPARTMENT_NAMES = (
    "Assets", "character", "crowd", "env", "fx", "layout", "lidar",
    "lights", "prop", "vehicle",
)


def _input_stage(node):
    try:
        input_node = node.input(0)
        return input_node.stage() if input_node else node.stage()
    except Exception:
        return None


def _string(node, name, default=""):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return parm.evalAsString().strip()
    except Exception:
        return default


def _bool(node, name, default=False):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return bool(parm.eval())
    except Exception:
        return default


def _int(node, name, default=0):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return int(parm.eval())
    except Exception:
        return default


def _float(node, name, default=0.0):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return float(parm.eval())
    except Exception:
        return default


def _short_path_menu(paths):
    paths = sorted(set(str(path) for path in paths if path))
    names = [path.rstrip("/").rsplit("/", 1)[-1] for path in paths]
    menu = []
    for path, name in zip(paths, names):
        label = name
        if names.count(name) > 1:
            label = "{} ({})".format(name, path.rsplit("/", 1)[0] or "/")
        menu.extend((path, label))
    return menu


def asset_menu(node):
    stage = _input_stage(node)
    paths = []
    if stage:
        for department_path in ASSET_DEPARTMENT_PATHS:
            prim = stage.GetPrimAtPath(department_path)
            if not prim or not prim.IsValid():
                continue
            paths.extend(
                child.GetPath().pathString
                for child in prim.GetChildren()
                if child.GetTypeName() in ("Xform", "Scope")
            )
    return _short_path_menu(paths)


def camera_menu(node):
    stage = _input_stage(node)
    paths = []
    if stage:
        paths = [
            prim.GetPath().pathString for prim in stage.Traverse()
            if prim.GetTypeName() == "Camera"
        ]
    return _short_path_menu(paths)


def payload_menu(node):
    """Return only prims that actually author one or more payload arcs."""
    stage = _input_stage(node)
    paths = []
    if stage:
        paths = [
            prim.GetPath().pathString
            for prim in stage.TraverseAll()
            if prim.HasPayload()
        ]
    return _short_path_menu(paths)


def prim_menu(node):
    stage = _input_stage(node)
    paths = []
    if stage:
        paths = [prim.GetPath().pathString for prim in stage.Traverse()]
    return _short_path_menu(paths)


def default_prim_menu(node):
    """Return only root prims that are valid USD defaultPrim targets."""
    stage = _input_stage(node)
    paths = []
    if stage:
        paths = [
            prim.GetPath().pathString
            for prim in stage.GetPseudoRoot().GetChildren()
            if prim.IsValid() and prim.GetName() != "HoudiniLayerInfo"
        ]
    return _short_path_menu(paths)


def variant_prim_menu(node):
    """Return only prims that have one or more composed variant sets."""
    stage = _input_stage(node)
    paths = []
    if stage:
        paths = [
            prim.GetPath().pathString
            for prim in stage.TraverseAll()
            if prim.GetVariantSets().GetNames()
        ]
    return _short_path_menu(paths)


def variant_set_menu(node):
    stage = _input_stage(node)
    path = _string(node, "primpath")
    prim = stage.GetPrimAtPath(path) if stage and path else None
    names = prim.GetVariantSets().GetNames() if prim and prim.IsValid() else []
    return [item for name in names for item in (name, name)]


def variant_menu(node):
    stage = _input_stage(node)
    path = _string(node, "primpath")
    set_name = _string(node, "variantset")
    prim = stage.GetPrimAtPath(path) if stage and path else None
    if not prim or not prim.IsValid() or not set_name:
        return []
    variants = prim.GetVariantSets().GetVariantSet(set_name).GetVariantNames()
    return [item for name in variants for item in (name, name)]


def sync_variant_menus(node):
    """Populate dependent variant parameters after an upstream choice."""
    stage = _input_stage(node)
    path = _string(node, "primpath")
    prim = stage.GetPrimAtPath(path) if stage and path else None
    set_names = (
        prim.GetVariantSets().GetNames()
        if prim and prim.IsValid() else []
    )

    set_parm = node.parm("variantset") if node else None
    variant_parm = node.parm("variant") if node else None
    if set_parm is None or variant_parm is None:
        return

    set_name = _string(node, "variantset")
    if set_name not in set_names:
        set_name = set_names[0] if set_names else ""
        set_parm.set(set_name)

    variants = []
    if set_name:
        variants = prim.GetVariantSets().GetVariantSet(
            set_name
        ).GetVariantNames()
    if _string(node, "variant") not in variants:
        variant_parm.set(variants[0] if variants else "")

    try:
        node.cook(force=True)
    except Exception:
        pass


def _validation_issues(stage, materials_only=False):
    from pxr import UsdGeom, UsdShade

    issues = []
    if stage is None:
        return ["No input USD stage is connected."]

    if not materials_only:
        for path in SCOPE_PATHS:
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                issues.append("Missing required Scope: {}".format(path))
            elif prim.GetTypeName() != "Scope":
                issues.append("Expected Scope, found {}: {}".format(
                    prim.GetTypeName(), path
                ))

    material_paths = set()
    for prim in stage.Traverse():
        if prim.GetTypeName() == "Material":
            material_paths.add(prim.GetPath().pathString)
        if prim.GetTypeName() != "Mesh":
            continue
        material, _relationship = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if not material:
            issues.append("Mesh has no bound material: {}".format(prim.GetPath()))
            continue
        material_path = material.GetPath().pathString
        if material_path not in material_paths:
            # The material may occur later in traversal; validate after gathering.
            material_paths.add(material_path)

    if materials_only:
        for prim in stage.Traverse():
            if prim.GetTypeName() != "Material":
                continue
            material = UsdShade.Material(prim)
            if not material.ComputeSurfaceSource()[0]:
                issues.append("Material has no surface shader: {}".format(prim.GetPath()))
    return sorted(set(issues))


def _write_report(node, issues):
    """Write a numbered validation report into the HDA's Log parameter."""
    if issues:
        lines = ["FAILED - {} issue(s) found".format(len(issues)), ""]
        lines.extend(
            "{}. {}".format(index, issue)
            for index, issue in enumerate(issues, 1)
        )
        message = "\n".join(lines)
    else:
        message = "PASSED - No issues found."

    log_parm = node.parm("log") if node else None
    if log_parm is None:
        raise ValueError("Validator HDA is missing its Log parameter")
    log_parm.set(message)
    return issues


def validate_stage(node):
    return _write_report(node, _validation_issues(_input_stage(node)))


def validate_materials(node):
    return _write_report(
        node,
        _validation_issues(_input_stage(node), materials_only=True),
    )


def _set_visibility(prim, visible):
    from pxr import UsdGeom
    if prim and prim.IsValid() and prim.IsA(UsdGeom.Imageable):
        UsdGeom.Imageable(prim).CreateVisibilityAttr().Set(
            UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible
        )


def cook_department_visibility(stage, node):
    for path in ASSET_DEPARTMENT_PATHS:
        name = path.rsplit("/", 1)[-1].lower()
        parm_name = "show_{}".format(re.sub(r"[^a-z0-9]", "", name))
        _set_visibility(stage.GetPrimAtPath(path), _bool(node, parm_name, True))


def cook_asset_isolator(stage, node):
    selected = _string(node, "asset")
    enabled = _bool(node, "isolate", True)
    for department_path in ASSET_DEPARTMENT_PATHS:
        department = stage.GetPrimAtPath(department_path)
        if not department or not department.IsValid():
            continue
        for asset in department.GetChildren():
            path = asset.GetPath().pathString
            _set_visibility(asset, not enabled or path == selected)


def cook_collection_builder(stage, node):
    from pxr import Sdf, Usd
    root_path = _string(node, "collectionroot", "/World/Collections")
    root = stage.DefinePrim(root_path, "Scope")
    for department_path in ASSET_DEPARTMENT_PATHS:
        department = stage.GetPrimAtPath(department_path)
        if not department or not department.IsValid():
            continue
        name = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            department_path.strip("/"),
        )
        collection = Usd.CollectionAPI.Apply(root, name)
        collection.CreateExpansionRuleAttr().Set(Usd.Tokens.expandPrims)
        collection.CreateIncludesRel().SetTargets([Sdf.Path(department_path)])


def cook_payload_controller(stage, node):
    from pxr import Sdf, Usd

    for index in range(1, _int(node, "payloadcount") + 1):
        path = _string(node, "payloadpath{}".format(index))
        if not path:
            continue
        prim_path = Sdf.Path(path)
        if not prim_path.IsAbsolutePath() or not prim_path.IsPrimPath():
            continue

        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            continue

        # Do not edit the payload list here. In Houdini the composed payload
        # arc may be authored on the current edit target, so ClearPayloads()
        # can remove the source arc and leave only the asset's Xform behind.
        # Active state is reversible and reliably removes/restores the entire
        # payload subtree in Hydra.
        override = stage.OverridePrim(prim_path)
        if _bool(node, "loadpayload{}".format(index), True):
            override.SetActive(True)
            stage.Load(prim_path, Usd.LoadWithDescendants)
        else:
            override.SetActive(False)


def cook_variant_manager(stage, node):
    path = _string(node, "primpath")
    set_name = _string(node, "variantset")
    selection = _string(node, "variant")
    prim = stage.GetPrimAtPath(path) if path else None
    if not prim or not prim.IsValid() or not set_name or not selection:
        return
    variant_sets = prim.GetVariantSets()
    if set_name not in variant_sets.GetNames():
        if not _bool(node, "createmissing", True):
            raise ValueError("Unknown variant set: {}".format(set_name))
        variant_set = variant_sets.AddVariantSet(set_name)
    else:
        variant_set = variant_sets.GetVariantSet(set_name)
    if selection not in variant_set.GetVariantNames():
        if not _bool(node, "createmissing", True):
            raise ValueError("Unknown variant {} on {}".format(selection, set_name))
        variant_set.AddVariant(selection)
    variant_set.SetVariantSelection(selection)


def cook_camera_configurator(stage, node):
    from pxr import Gf, UsdGeom
    path = _string(node, "camera")
    prim = stage.GetPrimAtPath(path) if path else None
    if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return
    camera = UsdGeom.Camera(prim)
    camera.CreateFocalLengthAttr().Set(_float(node, "focallength", 50.0))
    camera.CreateHorizontalApertureAttr().Set(_float(node, "aperture", 36.0))
    near = max(0.0001, _float(node, "nearclip", 0.1))
    far = max(near, _float(node, "farclip", 100000.0))
    camera.CreateClippingRangeAttr().Set(Gf.Vec2f(near, far))


def cook_asset_renamer(stage, node):
    from pxr import Sdf, Usd

    if not _bool(node, "enable", False):
        return
    source = _string(node, "source")
    new_name = re.sub(r"[^A-Za-z0-9_]", "_", _string(node, "newname"))
    if not source or not new_name:
        return
    if new_name[0].isdigit():
        new_name = "_" + new_name

    source_path = Sdf.Path(source)
    source_prim = stage.GetPrimAtPath(source_path)
    if not source_prim or not source_prim.IsValid():
        raise ValueError("Rename source does not exist: {}".format(source_path))

    destination = source_path.GetParentPath().AppendChild(new_name)
    destination_prim = stage.GetPrimAtPath(destination)
    if destination_prim and destination_prim.IsValid():
        raise ValueError("Rename destination already exists: {}".format(
            destination
        ))

    layer = stage.GetEditTarget().GetLayer()
    mask = Usd.StagePopulationMask()
    mask.Add(source_path)
    masked_stage = Usd.Stage.OpenMasked(
        stage.GetRootLayer(),
        stage.GetSessionLayer(),
        mask,
        Usd.Stage.LoadAll,
    )
    flattened = masked_stage.Flatten()

    # Rename inside the isolated flattened layer first. This captures every
    # contributing layer beneath the asset, rather than copying only its root
    # spec and losing Mesh children authored elsewhere in the layer stack.
    namespace_edit = Sdf.BatchNamespaceEdit()
    namespace_edit.Add(source_path, destination)
    if not flattened.Apply(namespace_edit):
        raise ValueError("Could not rename {} to {}".format(
            source_path, destination
        ))

    # Sdf namespace edits move specs but do not rewrite relationship targets
    # or attribute connections. Remap paths that pointed inside the old asset.
    flattened_stage = Usd.Stage.Open(flattened)
    for prim in flattened_stage.Traverse():
        for relationship in prim.GetRelationships():
            targets = relationship.GetTargets()
            remapped = [
                target.ReplacePrefix(source_path, destination)
                if target.HasPrefix(source_path) else target
                for target in targets
            ]
            if remapped != targets:
                relationship.SetTargets(remapped)
        for attribute in prim.GetAttributes():
            connections = attribute.GetConnections()
            remapped = [
                connection.ReplacePrefix(source_path, destination)
                if connection.HasPrefix(source_path) else connection
                for connection in connections
            ]
            if remapped != connections:
                attribute.SetConnections(remapped)

    Sdf.CreatePrimInLayer(layer, destination.GetParentPath())
    if not Sdf.CopySpec(flattened, destination, layer, destination):
        raise ValueError("Could not copy {} to {}".format(
            source_path, destination
        ))

    # Keep the upstream namespace untouched; this node owns only the copied
    # destination and the opinion that hides the old assembly path.
    stage.OverridePrim(source_path).SetActive(False)


def cook_publish_prep(stage, node):
    default_path = _string(node, "defaultprim", "/World")
    prim = stage.GetPrimAtPath(default_path)
    if prim and prim.IsValid() and prim.GetPath().IsRootPrimPath():
        stage.SetDefaultPrim(prim)
    if _bool(node, "configurehierarchy", True):
        configure_department_scopes(stage)
        configure_asset_kinds(stage)


COOKERS = {
    "department_visibility": cook_department_visibility,
    "asset_isolator": cook_asset_isolator,
    "collection_builder": cook_collection_builder,
    "payload_controller": cook_payload_controller,
    "variant_manager": cook_variant_manager,
    "camera_configurator": cook_camera_configurator,
    "asset_renamer": cook_asset_renamer,
    "publish_prep": cook_publish_prep,
}


def cook_tool(python_lop, settings_node, tool_name):
    """Dispatch an HDA's internal Python LOP to its USD authoring function."""
    cooker = COOKERS.get(tool_name)
    if cooker is None:
        return None  # Validator HDAs are intentional pass-through nodes.
    return cooker(python_lop.editableStage(), settings_node)
