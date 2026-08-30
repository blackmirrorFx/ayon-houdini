"""RenderMan AOV presets for the BMFX Render Setting HDA."""

from __future__ import absolute_import

try:
    import hou
except ImportError:  # Allows importing the module outside Houdini for tests.
    hou = None


# Exact RenderMan parameter names from the rendermanrendervars node's parm.ds.
# The generated suffix is part of the parameter name, so keeping this mapping
# explicit also makes a RenderMan/Houdini definition change fail clearly.
CORE_BEAUTY_AOVS = {
    "beauty": "xn__Cienable_zoa",
    "alpha": "xn__aenable_cna",
}

RECONSTRUCTION_AOVS = {
    "directDiffuse": "xn__directDiffuseenable_76a",
    "indirectDiffuse": "xn__indirectDiffuseenable_jbb",
    "subsurface": "xn__subsurfaceenable_81a",
    "directSpecular": "xn__directSpecularenable_v8a",
    "indirectSpecular": "xn__indirectSpecularenable_6cb",
    "singleScatter": "xn__transmissiveSingleScatterLobeenable_qyb",
    "glassTransmission": "xn__transmissiveGlassLobeenable_hlb",
    "emissive": "xn__emissiveenable_xya",
}

UTILITY_AOVS = {
    "opacity": "xn__Oienable_zoa",
    "positionWS": "xn____Pworldenable_xya",
    "normalWS": "xn____Nworldenable_xya",
    "normalCamera": "xn__Nnenable_zoa",
    "normalGeometric": "xn__Ngnenable_nqa",
    "depth": "xn__zenable_cna",
    "uv": "xn____stenable_bsa",
    "motionFore": "xn__motionForeenable_81a",
    "motionBack": "xn__motionBackenable_81a",
    "velocity3D": "xn__dPdtimeenable_9wa",
    "velocityCamera": "xn__dPcameradtimeenable_76a",
    "sampleCount": "xn__sampleCountenable_w3a",
    "cpuTime": "xn__cpuTimeenable_9wa",
    "curvature": "xn__curvatureenable_l0a",
    "id": "xn__idenable_zoa",
    "rawId": "xn__rawIdenable_yta",
}

LOOKDEV_LOBE_AOVS = {
    "directSpecularPrimary": "xn__directSpecularPrimaryLobeenable_3rb",
    "indirectSpecularPrimary": "xn__indirectSpecularPrimaryLobeenable_fvb",
    "directRoughSpecular": "xn__directSpecularRoughLobeenable_sob",
    "indirectRoughSpecular": "xn__indirectSpecularRoughLobeenable_3rb",
    "directClearcoat": "xn__directSpecularClearcoatLobeenable_fvb",
    "indirectClearcoat": "xn__indirectSpecularClearcoatLobeenable_qyb",
    "directIridescence": "xn__directSpecularIridescenceLobeenable_qyb",
    "indirectIridescence": "xn__indirectSpecularIridescenceLobeenable_11b",
    "directFuzz": "xn__directSpecularFuzzLobeenable_4mb",
    "indirectFuzz": "xn__indirectSpecularFuzzLobeenable_gqb",
    "directGlassReflection": "xn__directSpecularGlassLobeenable_sob",
    "indirectGlassReflection": "xn__indirectSpecularGlassLobeenable_3rb",
    "singleScatter": "xn__transmissiveSingleScatterLobeenable_qyb",
    "glassTransmission": "xn__transmissiveGlassLobeenable_hlb",
}

MATTE_HOLDOUT_AOVS = {
    "occluded": "xn__occludedenable_xya",
    "unoccluded": "xn__unoccludedenable_81a",
    "shadow": "xn__shadowenable_mva",
    "MatteID0": "xn__MatteID0enable_xya",
    "MatteID1": "xn__MatteID1enable_xya",
    "MatteID2": "xn__MatteID2enable_xya",
    "MatteID3": "xn__MatteID3enable_xya",
    "MatteID4": "xn__MatteID4enable_xya",
    "MatteID5": "xn__MatteID5enable_xya",
    "MatteID6": "xn__MatteID6enable_xya",
    "MatteID7": "xn__MatteID7enable_xya",
}

BEAUTY_UTILITY_NAMES = {
    "positionWS",
    "normalWS",
    "normalCamera",
    "normalGeometric",
    "depth",
    "motionFore",
    "motionBack",
}


def _combine_aovs(*groups):
    combined = {}
    for group in groups:
        combined.update(group)
    return combined


RENDERMAN_AOV_PRESETS = {
    "beauty": _combine_aovs(
        CORE_BEAUTY_AOVS,
        RECONSTRUCTION_AOVS,
        {
            name: parm_name
            for name, parm_name in UTILITY_AOVS.items()
            if name in BEAUTY_UTILITY_NAMES
        },
    ),
    "utility": UTILITY_AOVS,
    "lookdev": _combine_aovs(
        CORE_BEAUTY_AOVS,
        RECONSTRUCTION_AOVS,
        LOOKDEV_LOBE_AOVS,
        UTILITY_AOVS,
    ),
    "matte": MATTE_HOLDOUT_AOVS,
    "hero": _combine_aovs(
        CORE_BEAUTY_AOVS,
        RECONSTRUCTION_AOVS,
        UTILITY_AOVS,
        LOOKDEV_LOBE_AOVS,
        MATTE_HOLDOUT_AOVS,
    ),
}

PRESET_DESCRIPTIONS = {
    "beauty": (
        "Final Ci, alpha, lighting reconstruction, normals, position, "
        "motion and depth."
    ),
    "utility": "Position, normals, depth, motion, IDs and diagnostics.",
    "lookdev": "Beauty, utilities and detailed PxrSurface material lobes.",
    "matte": "Holdout, shadow and MatteID channels.",
    "hero": "Production plus detailed lobes and matte/holdout channels.",
}


def _selected_preset(node, preset):
    if preset is None:
        parm = node.parm("varType")
        preset = parm.eval() if parm is not None else 0

    preset_key = {
        "0": "beauty",
        "1": "utility",
        "2": "lookdev",
        "3": "matte",
        "4": "hero",
        "beauty": "beauty",
        "utility": "utility",
        "lookdev": "lookdev",
        "matte": "matte",
        "hero": "hero",
    }.get(str(preset).strip().lower())
    if preset_key is None:
        raise ValueError("Unknown RenderMan AOV preset: {!r}".format(preset))
    return preset_key


def set_renderman_aov_preset(kwargs=None, node=None, preset=None):
    """Enable the selected AOV set and disable every other RenderMan AOV."""
    kwargs = kwargs or {}
    node = node or kwargs.get("node")
    if node is None:
        if hou is None:
            raise RuntimeError("A BMFX Render Setting node is required")
        node = hou.pwd()

    preset_key = _selected_preset(node, preset)
    enabled_aovs = RENDERMAN_AOV_PRESETS[preset_key]
    render_vars = node.node("rendermanrendervars1")
    if render_vars is None:
        raise RuntimeError(
            "BMFX Render Setting is missing its rendermanrendervars1 node"
        )

    # Use RenderMan's own button callback to clear every category, including
    # AOVs that may be added by a future RenderMan version.
    disable_all = render_vars.parm("none")
    if disable_all is None:
        raise RuntimeError(
            "RenderMan Render Vars node has no Disable All AOVs parameter"
        )
    disable_all.pressButton()

    found = set()
    missing = set()
    for aov_name, parm_name in enabled_aovs.items():
        parm = render_vars.parm(parm_name)
        if parm is None:
            missing.add("{} ({})".format(aov_name, parm_name))
            continue
        parm.set(1)
        if int(parm.eval()) != 1:
            raise RuntimeError(
                "RenderMan AOV did not enable: {} ({})".format(
                    aov_name, parm_name
                )
            )
        found.add(aov_name)

    if missing:
        raise RuntimeError(
            "RenderMan Render Vars node does not provide: {}".format(
                ", ".join(sorted(missing))
            )
        )

    # RenderMan owns the exact variance/MSE support set required by its
    # offline denoiser.  Use its native button instead of duplicating fragile
    # generated parameter names here.  Utility/Data passes remain untouched.
    if preset_key == "beauty":
        enable_denoiser = render_vars.parm("denoiser")
        if enable_denoiser is None:
            raise RuntimeError(
                "RenderMan Render Vars has no Enable Denoiser AOVs control"
            )
        enable_denoiser.pressButton()
    return sorted(found)
