"""USD render-pass authoring for the Configure Render Pass HDA.

Put a Python Script LOP inside ``configure_render_pass`` and use its Script
parameter to call::

    from ayon_houdini.nodes.lops.render_pass import cook_configure_render_pass
    cook_configure_render_pass(hou.pwd(), hou.pwd().parent())

The HDA stays responsible for its UI.  This module reads its existing
``passname`` range and ``folder0`` multiparm, then authors one ``RenderPass``
per configured render layer on the Python LOP's editable USD stage.
"""

from __future__ import absolute_import

import re

try:
    import hou
except ImportError:  # Allows importing the module in non-Houdini test tools.
    hou = None


_PASS_RANGE_RE = re.compile(r"^L(\d+)-L(\d+)$", re.IGNORECASE)
_PASS_NUMBER_RE = re.compile(r"^L(\d+)(?:\D|$)", re.IGNORECASE)
_PATH_SPLIT_RE = re.compile(r"[\s,;]+")
PASS_DEPARTMENT_RANGES = (
    (10, 90, "Characters"),
    (100, 190, "Environment"),
    (200, 290, "FX"),
    (300, 390, "Vehicles"),
    (400, 490, "Props"),
    (500, 590, "Matte / Holdout"),
    (900, 990, "Utility"),
)
_COLLECTION_NAMES = {
    "render": "renderVisibility",
    "camera": "cameraVisibility",
    "prune": "prune",
    "matte": "matte",
}

_DEFAULT_PASS_RANGE = "L010-L090"
_LAYER_TARGET_PARMS = (
    "beautytarget{}",
    "excludetarget{}",
    "light{}",
    "lightexclude{}",
    "phantomtarget{}",
    "mattetarget{}",
)


def _callback_node(node_or_kwargs):
    if isinstance(node_or_kwargs, dict):
        return node_or_kwargs.get("node")
    return node_or_kwargs


def initialize_node(node_or_kwargs=None):
    """Initialize a newly created Configure Render Pass HDA safely.

    Pass Name is assigned before the first multiparm row is created. This is
    important because creating a row immediately cooks the internal Python
    LOP; an empty Pass Name at that moment causes a new node to error.

    The function is idempotent so it can also be run manually on an existing
    node without replacing any values the artist has already authored.
    """
    if hou is None:
        return False
    node = _callback_node(node_or_kwargs)
    if node is None:
        return False

    pass_name_parm = node.parm("passname")
    layer_count_parm = node.parm("folder0")
    if pass_name_parm is None or layer_count_parm is None:
        return False

    try:
        if not pass_name_parm.evalAsString().strip():
            pass_name_parm.set(_DEFAULT_PASS_RANGE)

        preset_parm = node.parm("rpreset")
        if preset_parm is not None:
            try:
                preset_parm.eval()
            except Exception:
                preset_parm.set(0)

        element_parm = node.parm("elemtarget")
        if element_parm is not None:
            try:
                element_parm.evalAsString()
            except Exception:
                element_parm.set("")

        layer_count = max(0, int(layer_count_parm.eval()))
        if layer_count < 1:
            layer_count_parm.set(1)
            layer_count = 1

        for index in range(1, layer_count + 1):
            for name_pattern in _LAYER_TARGET_PARMS:
                parm = node.parm(name_pattern.format(index))
                if parm is None:
                    continue
                try:
                    parm.evalAsString()
                except Exception:
                    parm.set("")
        return True
    except Exception:
        # An OnCreated callback must never leave the new node in an error
        # state. Houdini can briefly expose an incomplete parameter interface
        # while an HDA definition is being installed or synchronized.
        return False


def on_created(node_or_kwargs=None):
    """HDA OnCreated entry point."""
    return initialize_node(node_or_kwargs)


def pass_department(pass_name):
    """Return the department assigned to an ``L###`` render-pass number."""
    match = _PASS_NUMBER_RE.match(str(pass_name or "").strip())
    if not match:
        return "Custom"
    number = int(match.group(1))
    for start, end, label in PASS_DEPARTMENT_RANGES:
        if start <= number <= end:
            return label
    return "Custom"


def render_pass_name(pass_name):
    """Return the canonical user-facing name authored by this HDA."""
    raw_name = str(pass_name or "").strip()
    department = pass_department(raw_name)
    if department == "Custom":
        return raw_name
    department = re.sub(r"\s*/\s*", "-", department)
    return "{}-{}".format(raw_name, department)


def _parm_value(node, name, default=""):
    """Return a UI parameter without making optional HDA fields mandatory."""
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return parm.evalAsString().strip()
    except Exception:
        try:
            return str(parm.eval()).strip()
        except Exception:
            return default


def _parm_int(node, name, default=0):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return int(parm.eval())
    except Exception:
            return default


def _parm_values(node, name):
    """Read either a regular menu or Houdini multi-select menu parameter."""
    parm = node.parm(name) if node else None
    if parm is None:
        return []
    try:
        value = parm.eval()
        if isinstance(value, (tuple, list)):
            return list(value)
    except Exception:
        pass
    return [_parm_value(node, name)]


def _as_path_list(values, label):
    """Return unique absolute USD paths from values read from an HDA."""
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values:
        if isinstance(value, (tuple, list)):
            value_parts = value
        else:
            value_parts = _PATH_SPLIT_RE.split(str(value or "").strip())
        for path in value_parts:
            path = str(path or "").strip()
            if not path:
                continue
            if not path.startswith("/"):
                raise ValueError("{} must be an absolute USD path: {!r}".format(label, path))
            if path not in result:
                result.append(path)
    return result


def _exclude_paths_not_hiding_targets(exclude_paths, target_paths):
    """Drop exclusions that would remove an explicitly assigned target.

    Target roles (beauty, phantom, or matte) take precedence over the generic
    Exclude Target field.  An ancestor exclusion must also be removed when a
    target below it is assigned, because USD collection exclusions apply to
    the whole subtree.
    """
    result = []
    for exclude_path in exclude_paths:
        prefix = exclude_path.rstrip("/") + "/"
        if any(
            target_path == exclude_path or target_path.startswith(prefix)
            for target_path in target_paths
        ):
            continue
        result.append(exclude_path)
    return result


def _path_is_below_any_target(path, target_paths):
    """Return whether ``path`` is a target or one of its descendants."""
    for target_path in target_paths:
        if path == target_path or path.startswith(target_path.rstrip("/") + "/"):
            return True
    return False


def _instance_root_paths_below_targets(stage, target_paths):
    """Return concrete native USD instance roots below selected targets.

    Render-pass collections commonly target an asset Xform and use
    ``expandPrims`` for its contents.  Some Hydra delegates do not reliably
    apply that inherited membership to native instance proxies, while they do
    honor membership authored directly on the instance root.  Normal stage
    traversal visits those roots (but intentionally does not enter their
    proxies), so add the concrete roots without trying to target illegal
    instance-proxy paths.
    """
    target_paths = _as_path_list(target_paths, "Instance Target")
    if not target_paths:
        return []

    result = []
    for prim in stage.Traverse():
        if not prim.IsInstance():
            continue
        path = prim.GetPath().pathString
        if _path_is_below_any_target(path, target_paths):
            result.append(path)
    return result


def _deinstance_native_instances_for_render_pass(stage):
    """Author local ``instanceable = false`` opinions for native instances.

    OpenUSD render-pass visibility currently does not work for native USD
    instances in Storm or RenderMan (OpenUSD issue #4122). Targeting instance
    roots and instance proxies in the collections does not fix the Hydra
    scene-index behavior. De-instancing at this downstream render-pass node is
    the supported working form and leaves referenced/published source layers
    untouched.

    Repeat because de-instancing an outer instance can reveal nested native
    instance roots which were not present in normal stage traversal before.
    """
    authored = []
    authored_set = set()
    while True:
        paths = [
            prim.GetPath().pathString
            for prim in stage.Traverse()
            if prim.IsInstance()
            and prim.GetPath().pathString not in authored_set
        ]
        if not paths:
            break
        for path in paths:
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.SetInstanceable(False):
                raise RuntimeError(
                    "Cannot de-instance native USD prim for RenderPass: {}".format(path)
                )
            authored.append(path)
            authored_set.add(path)
    return authored


def _layer_pass_names(pass_range, count):
    """Expand ``L200-L290`` to L200, L210, ... for ``count`` layers."""
    value = (pass_range or "").strip()
    match = _PASS_RANGE_RE.match(value)
    if not match:
        if count > 1:
            raise ValueError(
                "Pass Name must be a range such as L200-L290 when using multiple Render Layers"
            )
        if not value:
            raise ValueError("Pass Name is required")
        return [value]

    start, end = (int(number) for number in match.groups())
    if end < start:
        raise ValueError("Pass Name range ends before it starts: {}".format(value))
    names = ["L{:03d}".format(start + index * 10) for index in range(count)]
    if int(names[-1][1:]) > end:
        raise ValueError(
            "{} supports {} render layers at increments of 10, but {} were configured".format(
                value, ((end - start) // 10) + 1, count
            )
        )
    return names


def _define_render_pass(stage, pass_path):
    from pxr import Sdf, UsdRender

    path = Sdf.Path(pass_path)
    define = getattr(UsdRender.Pass, "Define", None)
    if define:
        return define(stage, path)
    # Compatibility with USD bindings which expose only the schema wrapper.
    return UsdRender.Pass(stage.DefinePrim(path, "RenderPass"))


def _find_render_settings(stage):
    """Return the deterministic first RenderSettings prim under ``/Render``."""
    candidates = []
    for prim in stage.Traverse():
        if prim.GetTypeName() == "RenderSettings" and prim.GetPath().pathString.startswith("/Render/"):
            candidates.append(prim.GetPath())
    return sorted(candidates, key=lambda path: path.pathString)[0] if candidates else None


def _unselected_light_paths(stage, selected_light_paths):
    """Return concrete lights not below a selected light/menu target path."""
    from pxr import UsdLux

    selected_light_paths = _as_path_list(selected_light_paths, "Lights")
    result = []
    for prim in stage.Traverse():
        if not prim.HasAPI(UsdLux.LightAPI):
            continue
        path = prim.GetPath().pathString
        if not any(path == selected or path.startswith(selected + "/") for selected in selected_light_paths):
            result.append(path)
    return result


def _unassigned_renderable_paths(stage, assigned_paths):
    """Return renderable geometry outside the explicitly assigned targets.

    Isolation must operate on geometry prims instead of their asset ancestors.
    An unselected asset can own materials or other dependencies used by an
    assigned FX prim, and excluding the asset root would remove those too.
    ``UsdGeom.Boundable`` covers meshes, curves, volumes, point instancers, and
    renderer procedural geometry while lights are handled separately. Native
    USD instance roots must be handled explicitly: they are usually Xforms,
    and normal traversal does not descend into their boundable proxies.
    """
    from pxr import UsdGeom, UsdLux

    assigned_paths = _as_path_list(assigned_paths, "Render Target")
    result = []
    for prim in stage.Traverse():
        if prim.HasAPI(UsdLux.LightAPI):
            continue
        if not prim.IsInstance() and not prim.IsA(UsdGeom.Boundable):
            continue
        path = prim.GetPath().pathString
        if not _path_is_below_any_target(path, assigned_paths):
            result.append(path)
    return result


def _author_collection(pass_schema, collection_key, includes, excludes, include_root=False):
    """Author a RenderPass collection using explicit USD relationship targets."""
    from pxr import Sdf, Usd

    collection = Usd.CollectionAPI.Apply(
        pass_schema.GetPrim(), _COLLECTION_NAMES[collection_key]
    )
    collection.CreateIncludeRootAttr().Set(bool(include_root))
    # The HDA menus point at asset Xform prims.  ``explicitOnly`` would make
    # the collection contain just that transform, not its mesh descendants,
    # which produces an empty render.  Expand the selected asset hierarchy.
    collection.CreateExpansionRuleAttr().Set(Usd.Tokens.expandPrims)
    collection.CreateIncludesRel().SetTargets([Sdf.Path(path) for path in includes])
    collection.CreateExcludesRel().SetTargets([Sdf.Path(path) for path in excludes])


def create_render_pass(stage, pass_name, beauty_paths, exclude_paths=(), phantom_paths=(),
                       matte_paths=(), renderer="", preset="", render_source="",
                       light_paths=(), light_exclude_paths=()):
    """Create one renderer-independent USD ``RenderPass``.

    Beauty objects are camera visible.  Phantom objects remain render-visible
    but are excluded from camera visibility.  Matte objects are camera and
    render visible, then authored to the standard ``matte`` collection.
    Unassigned renderable geometry does not participate in the pass, while
    non-geometry dependencies such as material libraries remain available.
    Explicit target roles take precedence if the same object was also entered
    in Exclude Target.
    """
    if stage is None:
        raise RuntimeError("An editable USD stage is required")
    if not pass_name:
        raise ValueError("Pass Name is required")

    authored_name = render_pass_name(pass_name)
    if pass_name.startswith("/"):
        pass_path = pass_name
    else:
        # USD prim identifiers cannot contain hyphens.  Use an underscore in
        # the Scene Graph Tree while retaining the canonical hyphenated name
        # in ayon:renderName for UI, output and farm naming.
        prim_name = re.sub(r"[^A-Za-z0-9_]", "_", authored_name)
        pass_path = "/Render/Passes/{}".format(prim_name)
    if not pass_path.startswith("/Render/"):
        raise ValueError("Render pass must be below /Render: {}".format(pass_path))

    beauty_paths = _as_path_list(beauty_paths, "Beauty Target")
    exclude_paths = _as_path_list(exclude_paths, "Exclude Target")
    phantom_paths = _as_path_list(phantom_paths, "Phantom Target")
    matte_paths = _as_path_list(matte_paths, "Matte Target")
    light_paths = _as_path_list(light_paths, "Lights")
    light_exclude_paths = _as_path_list(light_exclude_paths, "Light Exclude")
    # Menu refreshes can briefly yield a placeholder or an empty resolved
    # target.  This is an incomplete UI row, not a cook failure.
    if not beauty_paths:
        return None

    deinstanced_paths = _deinstance_native_instances_for_render_pass(stage)

    pass_schema = _define_render_pass(stage, pass_path)
    pass_prim = pass_schema.GetPrim()
    pass_prim.SetCustomDataByKey("ayon:configureRenderPass", True)
    pass_prim.SetCustomDataByKey("ayon:renderer", renderer)
    pass_prim.SetCustomDataByKey("ayon:renderPreset", preset)
    pass_prim.SetCustomDataByKey(
        "ayon:deinstancedNativeInstanceCount",
        len(deinstanced_paths),
    )
    pass_prim.SetCustomDataByKey(
        "ayon:renderDepartment", pass_department(pass_name)
    )
    pass_prim.SetCustomDataByKey("ayon:renderName", authored_name)
    set_display_name = getattr(pass_prim, "SetDisplayName", None)
    if callable(set_display_name):
        set_display_name(authored_name)

    from pxr import Sdf
    source_path = render_source.strip() if render_source else ""
    if source_path:
        source_prim = stage.GetPrimAtPath(source_path)
        if not source_prim.IsValid() or source_prim.GetTypeName() != "RenderSettings":
            raise ValueError("Render Source is not a RenderSettings prim: {}".format(source_path))
        source_path = Sdf.Path(source_path)
    else:
        source_path = _find_render_settings(stage)
    if source_path:
        pass_schema.CreateRenderSourceRel().SetTargets([source_path])

    participating_paths = _as_path_list(
        beauty_paths + phantom_paths + matte_paths,
        "Render Target",
    )
    exclude_paths = _exclude_paths_not_hiding_targets(
        exclude_paths,
        participating_paths,
    )
    light_excludes = _as_path_list(
        _unselected_light_paths(stage, light_paths) + light_exclude_paths,
        "Light Exclude",
    )
    geometry_excludes = _as_path_list(
        _unassigned_renderable_paths(stage, participating_paths),
        "Unassigned Geometry",
    )
    # Start from the complete stage and remove only unassigned renderable
    # geometry.  This preserves material libraries and asset ancestors even
    # when an FX prim binds to a material owned by another department's asset.
    render_excludes = _as_path_list(
        geometry_excludes
        + exclude_paths
        + light_excludes,
        "Render Exclude",
    )
    _author_collection(
        pass_schema,
        "render",
        (),
        render_excludes,
        include_root=True,
    )
    # Camera visibility uses the same geometry isolation.  Matte objects stay
    # camera-visible so the matte collection can turn them into holdouts.
    phantom_excludes = _exclude_paths_not_hiding_targets(
        phantom_paths,
        beauty_paths + matte_paths,
    )
    _author_collection(
        pass_schema,
        "camera",
        (),
        geometry_excludes
        + phantom_excludes
        + exclude_paths
        + light_excludes,
        include_root=True,
    )
    _author_collection(pass_schema, "prune", render_excludes, ())
    _author_collection(pass_schema, "matte", matte_paths, ())
    return pass_path


def cook_configure_render_pass(python_lop=None, settings_node=None):
    """Cook entry point for the HDA's internal Python Script LOP.

    Rows without a Beauty Target are ignored.  Houdini temporarily clears
    dependent menu parameters while an artist changes Element Target, so
    treating that intermediate state as an error makes the HDA unnecessarily
    fail during normal UI edits.
    """
    if hou is None:
        raise RuntimeError("cook_configure_render_pass must run inside Houdini")
    python_lop = python_lop or hou.pwd()
    settings_node = settings_node or python_lop.parent()
    stage = python_lop.editableStage()

    layer_count = _parm_int(settings_node, "folder0", 0)
    if layer_count < 1:
        return []
    pass_names = _layer_pass_names(_parm_value(settings_node, "passname"), layer_count)
    renderer = _parm_value(settings_node, "renderer")
    preset = _parm_value(settings_node, "rpreset")
    render_source = _parm_value(settings_node, "rendersource")

    authored = []
    for index, pass_name in enumerate(pass_names, 1):
        beauty = _parm_values(settings_node, "beautytarget{}".format(index))
        exclude = _parm_values(settings_node, "excludetarget{}".format(index))
        phantom = _parm_values(settings_node, "phantomtarget{}".format(index))
        matte = _parm_values(settings_node, "mattetarget{}".format(index))
        lights = _parm_values(settings_node, "light{}".format(index))
        light_exclude = _parm_values(settings_node, "lightexclude{}".format(index))
        if not _as_path_list(beauty, "Beauty Target"):
            continue
        pass_path = create_render_pass(
            stage, pass_name, beauty, exclude, phantom, matte,
            renderer, preset, render_source, lights, light_exclude
        )
        if pass_path:
            authored.append(pass_path)
    return authored
