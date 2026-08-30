"""Automatic per-light LPE tags and beauty RenderVars for Solaris.

The :func:`cook_light_lpe_aovs` entry point is intended for the Python LOP in
the ``bmfx_auto_light_lpe_aovs`` HDA.  It discovers USD lights on the incoming
stage, gives each light a deterministic LPE tag for both RenderMan and Karma,
creates one beauty-contribution RenderVar per tag and appends those variables
to existing RenderProducts.

USD imports are deliberately local so this module remains importable by lint
and test tools outside Houdini.
"""

from __future__ import absolute_import

import re

try:
    import hou
except ImportError:  # pragma: no cover - used only outside Houdini.
    hou = None


RENDERMAN_LPE_ATTRIBUTE = (
    "primvars:ri:attributes:identifier:lpegroup"
)
KARMA_LPE_ATTRIBUTE = "inputs:karma:light:lpetag"
MANAGED_AOV_KEY = "ayon:autoLightLpeAov"
SOURCE_LIGHT_KEY = "ayon:sourceLight"
# Production-tested per-light beauty expression.  Keep this exact form: the
# bracketed light-or-emission variant (``[<L.'tag'>O]``) produces a black
# result with the BMFX RenderMan lighting setup.
RENDERMAN_LIGHT_BEAUTY_LPE = "C[DS]*<L.'{}'>"

# Mirrors the AOVs for which Karma Render Settings exposes "Split per LPE
# Tag".  Beauty deliberately retains the production-approved BMFX expression
# above; the remaining expressions follow Karma's standard LPE definitions.
KARMA_SPLIT_LPE_AOVS = (
    ("beauty", "C", "C.*[LO]"),
    ("beautyunshadowed", "beautyunshadowed", "unoccluded;C.*[LO]"),
    ("shadow", "shadow", "shadow;C.*[LO]"),
    ("combineddiffuse", "combineddiffuse", "C<RD>.*L"),
    ("directdiffuse", "directdiffuse", "C<RD>L"),
    ("indirectdiffuse", "indirectdiffuse", "C<RD>.+L"),
    (
        "combineddiffuseunshadowed",
        "combineddiffuseunshadowed",
        "unoccluded;C<RD>.*L",
    ),
    (
        "directdiffuseunshadowed",
        "directdiffuseunshadowed",
        "unoccluded;C<RD>L",
    ),
    (
        "indirectdiffuseunshadowed",
        "indirectdiffuseunshadowed",
        "unoccluded;C<RD>.+L",
    ),
    ("combineddiffuseshadow", "combineddiffuseshadow", "shadow;C<RD>.*L"),
    ("directdiffuseshadow", "directdiffuseshadow", "shadow;C<RD>L"),
    ("indirectdiffuseshadow", "indirectdiffuseshadow", "shadow;C<RD>.+L"),
    (
        "combinedglossyreflection",
        "combinedglossyreflection",
        "C<RG>.*L",
    ),
    ("directglossyreflection", "directglossyreflection", "C<RG>L"),
    ("indirectglossyreflection", "indirectglossyreflection", "C<RG>.+L"),
    ("glossytransmission", "glossytransmission", "C<TG>.*L"),
    ("visiblelights", "visiblelights", "CL"),
    ("combinedvolume", "combinedvolume", "CV.*L"),
    ("directvolume", "directvolume", "CVL"),
    ("indirectvolume", "indirectvolume", "CV.+L"),
    ("coat", "coat", "C<...'coat'>.*L"),
    ("sss", "sss", "C<TD>.*L"),
)
KARMA_SPLIT_LPE_AOV_MAP = {
    key: (channel, expression)
    for key, channel, expression in KARMA_SPLIT_LPE_AOVS
}

_INVALID_IDENTIFIER = re.compile(r"[^A-Za-z0-9_]+")
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_identifier(value, fallback="light"):
    """Return a renderer-safe LPE/USD identifier."""
    value = _INVALID_IDENTIFIER.sub("_", str(value or "").strip())
    value = value.strip("_") or fallback
    if not value:
        return ""
    if value[0].isdigit():
        value = "_{}".format(value)
    return value


def _parm_string(node, name, default=""):
    parm = node.parm(name) if node else None
    if parm is None:
        return default
    try:
        return parm.evalAsString().strip()
    except Exception:
        return default


def _parm_bool(node, name, default=False):
    parm = node.parm(name) if node else None
    if parm is None:
        return bool(default)
    try:
        return bool(parm.eval())
    except Exception:
        return bool(default)


def _is_below(path, root):
    if root == "/":
        return True
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def discover_lights(stage, scope_root="/"):
    """Return active USD light prims below ``scope_root`` in path order."""
    from pxr import UsdLux

    root = str(scope_root or "/").strip() or "/"
    if not root.startswith("/"):
        raise ValueError("Light Scope must be an absolute USD path")

    lights = []
    for prim in stage.Traverse():
        if not prim or not prim.IsValid() or not prim.IsActive():
            continue
        path = prim.GetPath().pathString
        if _is_below(path, root) and prim.HasAPI(UsdLux.LightAPI):
            lights.append(prim)
    return sorted(lights, key=lambda prim: prim.GetPath().pathString)


def _existing_tag(prim):
    for name in (RENDERMAN_LPE_ATTRIBUTE, KARMA_LPE_ATTRIBUTE):
        attribute = prim.GetAttribute(name)
        if not attribute:
            continue
        try:
            value = str(attribute.Get() or "").strip()
        except Exception:
            value = ""
        if _VALID_IDENTIFIER.match(value):
            return value
    return ""


def _unique_tag(candidate, used):
    base = candidate
    index = 2
    while candidate in used:
        candidate = "{}_{}".format(base, index)
        index += 1
    used.add(candidate)
    return candidate


def assign_light_tags(lights, prefix="", preserve_existing=True):
    """Return deterministic ``(prim, tag)`` pairs with unique tags."""
    used = set()
    assignments = []
    safe_prefix = sanitize_identifier(prefix, fallback="") if prefix else ""
    if safe_prefix and prefix.endswith("_") and not safe_prefix.endswith("_"):
        safe_prefix += "_"

    for prim in lights:
        existing = _existing_tag(prim) if preserve_existing else ""
        base = existing or "{}{}".format(
            safe_prefix, sanitize_identifier(prim.GetName())
        )
        assignments.append((prim, _unique_tag(base, used)))
    return assignments


def _author_light_tag(prim, tag):
    from pxr import Sdf

    for name in (RENDERMAN_LPE_ATTRIBUTE, KARMA_LPE_ATTRIBUTE):
        prim.CreateAttribute(
            name, Sdf.ValueTypeNames.String, custom=False
        ).Set(tag)


def _remove_managed_render_vars(stage, aov_root):
    paths = []
    for prim in stage.Traverse():
        path = prim.GetPath().pathString
        if not _is_below(path, aov_root):
            continue
        try:
            managed = bool(prim.GetCustomDataByKey(MANAGED_AOV_KEY))
        except Exception:
            managed = False
        if managed:
            paths.append(prim.GetPath())

    # Remove deepest paths first in case a future version creates sub-prims.
    for path in sorted(paths, key=lambda item: len(item.pathString), reverse=True):
        stage.RemovePrim(path)
    return {path.pathString for path in paths}


def _render_products(stage):
    return sorted(
        (
            prim for prim in stage.Traverse()
            if prim.GetTypeName() == "RenderProduct"
        ),
        key=lambda prim: prim.GetPath().pathString,
    )


def _available_render_var_path(stage, aov_root, name):
    from pxr import Sdf

    base = sanitize_identifier(name)
    candidate = Sdf.Path("{}/{}".format(aov_root.rstrip("/"), base))
    index = 2
    while stage.GetPrimAtPath(candidate).IsValid():
        candidate = Sdf.Path(
            "{}/{}_{}".format(aov_root.rstrip("/"), base, index)
        )
        index += 1
    return candidate


def _split_lpe_expression(aov_key, expression, tag):
    """Return Karma-style tag substitution with the approved beauty LPE."""
    if aov_key == "beauty":
        return RENDERMAN_LIGHT_BEAUTY_LPE.format(tag)
    light_expression = "<L.'{}'>".format(tag)
    if "[LO]" in expression:
        return expression.replace("[LO]", light_expression)
    return expression.replace("L", light_expression)


def create_light_lpe_aovs(stage, assignments, aov_root, aov_prefix,
                          enabled_aovs=("beauty",), bind_products=True):
    """Create renderer-compatible split RenderVars for enabled Karma AOVs."""
    from pxr import Sdf, UsdRender

    aov_root = str(aov_root or "/Render/Products/Vars").rstrip("/")
    if not aov_root.startswith("/"):
        raise ValueError("RenderVar Root must be an absolute USD path")

    removed_paths = _remove_managed_render_vars(stage, aov_root)
    products = _render_products(stage) if bind_products else []

    # Preserve the ordering of all non-managed AOVs already on each product.
    product_targets = {}
    for product_prim in products:
        relationship = product_prim.GetRelationship("orderedVars")
        targets = list(relationship.GetTargets()) if relationship else []
        product_targets[product_prim.GetPath().pathString] = [
            target for target in targets
            if target.pathString not in removed_paths
        ]

    enabled_aovs = [
        key for key in enabled_aovs if key in KARMA_SPLIT_LPE_AOV_MAP
    ]
    created_paths = []
    for aov_key in enabled_aovs:
        base_channel, base_expression = KARMA_SPLIT_LPE_AOV_MAP[aov_key]
        for light_prim, tag in assignments:
            if aov_key == "beauty":
                prim_name = "{}{}".format(aov_prefix, tag)
            else:
                prim_name = "{}_{}".format(aov_key, tag)
            channel_name = "{}_{}".format(base_channel, tag)
            var_path = _available_render_var_path(stage, aov_root, prim_name)
            render_var = UsdRender.Var.Define(stage, var_path)
            render_var.CreateDataTypeAttr().Set("float3")
            render_var.CreateSourceTypeAttr().Set("lpe")
            render_var.CreateSourceNameAttr().Set(
                _split_lpe_expression(aov_key, base_expression, tag)
            )
            var_prim = render_var.GetPrim()
            var_prim.AddAppliedSchema("KarmaRenderVarAPI")
            var_prim.AddAppliedSchema("HuskRenderVarAPI")
            var_prim.CreateAttribute(
                "driver:parameters:aov:name",
                Sdf.ValueTypeNames.String,
                custom=False,
            ).Set(channel_name)
            var_prim.CreateAttribute(
                "driver:parameters:aov:format",
                Sdf.ValueTypeNames.Token,
                custom=False,
            ).Set("float3")
            var_prim.CreateAttribute(
                "driver:parameters:aov:husk:name",
                Sdf.ValueTypeNames.String,
                custom=False,
            ).Set(channel_name)
            var_prim.CreateAttribute(
                "driver:parameters:aov:husk:format",
                Sdf.ValueTypeNames.Token,
                custom=False,
            ).Set("float3")
            var_prim.CreateAttribute(
                "driver:parameters:aov:husk:channel_prefix",
                Sdf.ValueTypeNames.String,
                custom=False,
            ).Set(channel_name)
            var_prim.CreateAttribute(
                "driver:parameters:aov:husk:clearValue",
                Sdf.ValueTypeNames.Int,
                custom=False,
            ).Set(0)
            var_prim.CreateAttribute(
                "driver:parameters:aov:husk:multiSampled",
                Sdf.ValueTypeNames.Bool,
                custom=False,
            ).Set(True)
            var_prim.SetCustomDataByKey(MANAGED_AOV_KEY, True)
            var_prim.SetCustomDataByKey(
                SOURCE_LIGHT_KEY, light_prim.GetPath().pathString
            )
            created_paths.append(var_path)

    for product_prim in products:
        targets = product_targets[product_prim.GetPath().pathString]
        product = UsdRender.Product(product_prim)
        product.CreateOrderedVarsRel().SetTargets(targets + created_paths)

    return [path.pathString for path in created_paths], [
        prim.GetPath().pathString for prim in products
    ]


def create_light_beauty_aovs(stage, assignments, aov_root, aov_prefix,
                             bind_products=True):
    """Backward-compatible wrapper for the original beauty-only API."""
    return create_light_lpe_aovs(
        stage,
        assignments,
        aov_root,
        aov_prefix,
        enabled_aovs=("beauty",),
        bind_products=bind_products,
    )


def author_light_lpe_aovs(stage, scope_root="/", tag_prefix="",
                          aov_root="/Render/Products/Vars",
                          aov_prefix="beauty_",
                          preserve_existing=True, bind_products=True,
                          enabled_aovs=("beauty",)):
    """Author tags and beauty AOVs and return a compact cook summary."""
    if stage is None:
        raise RuntimeError("An editable USD stage is required")

    lights = discover_lights(stage, scope_root)
    assignments = assign_light_tags(
        lights, prefix=tag_prefix, preserve_existing=preserve_existing
    )
    for prim, tag in assignments:
        _author_light_tag(prim, tag)

    aovs, products = create_light_lpe_aovs(
        stage,
        assignments,
        aov_root=aov_root,
        aov_prefix=aov_prefix,
        enabled_aovs=enabled_aovs,
        bind_products=bind_products,
    )
    return {
        "lights": [
            {"path": prim.GetPath().pathString, "tag": tag}
            for prim, tag in assignments
        ],
        "aovs": aovs,
        "products": products,
    }


def cook_light_lpe_aovs(python_lop=None, settings_node=None):
    """Cook entry point used by the BMFX Auto Light LPE AOVs HDA."""
    if hou is None:
        raise RuntimeError("This operation must run inside Houdini")

    python_lop = python_lop or hou.pwd()
    settings_node = settings_node or python_lop.parent()
    if not _parm_bool(settings_node, "enabled", True):
        return {"lights": [], "aovs": [], "products": []}

    stage = python_lop.editableStage()
    if stage is None:
        raise RuntimeError("Auto Light LPE AOVs could not acquire an editable stage")

    return author_light_lpe_aovs(
        stage,
        scope_root=_parm_string(settings_node, "scope_root", "/"),
        tag_prefix=_parm_string(settings_node, "tag_prefix", ""),
        aov_root=_parm_string(
            settings_node, "aov_root", "/Render/Products/Vars"
        ),
        # This is intentionally fixed instead of exposed in the HDA UI.  It
        # follows Karma's beauty_<tag> RenderVar prim naming convention.
        aov_prefix="beauty_",
        preserve_existing=_parm_bool(
            settings_node, "preserve_existing_tags", True
        ),
        bind_products=_parm_bool(settings_node, "bind_products", True),
        enabled_aovs=tuple(
            key for key, _channel, _expression in KARMA_SPLIT_LPE_AOVS
            if _parm_bool(
                settings_node,
                "split_{}".format(key),
                key == "beauty",
            )
        ),
    )
