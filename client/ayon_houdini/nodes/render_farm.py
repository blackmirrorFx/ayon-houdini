"""Launch a Houdini ROP on Deadline with reliable native progress output."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys


_PROGRESS_PATTERNS = (
    ("alf", re.compile(
        r"ALF_PROGRESS\s+([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE
    )),
    ("rman", re.compile(
        r"RMAN_PROGRESS\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE
    )),
    # RenderMan 26/27 emits this marker from the renderer process. It is the
    # primary progress source for HdPrman/USD renders.
    ("r90000", re.compile(
        r"R90000\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE
    )),
    ("houdini_done", re.compile(
        r"([0-9]+(?:\.[0-9]+)?)%\s+done", re.IGNORECASE
    )),
    ("houdini", re.compile(
        r"\[render progress\]\s*-+\s*([0-9]+(?:\.[0-9]+)?)\s+percent",
        re.IGNORECASE,
    )),
    ("wrapper", re.compile(
        r"Progress:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE
    )),
)


def _emit_progress(percent):
    percent = max(0, min(100, int(float(percent))))
    print("Progress: {}%".format(percent), flush=True)


class _TaskProgress:
    """Convert per-frame renderer percentages into monotonic task progress."""

    def __init__(self, start, end, step):
        step = abs(float(step)) or 1.0
        self.frame_count = max(1, int(abs(float(end) - float(start)) / step) + 1)
        self.completed_frames = 0
        self.active_source = None
        self.last_frame_progress = None
        self.last_overall_progress = 0

    def update(self, source, percent):
        percent = max(0.0, min(100.0, float(percent)))
        if source == "wrapper":
            overall = int(percent)
            self.last_overall_progress = max(
                self.last_overall_progress, overall
            )
            return self.last_overall_progress

        # Husk and RenderMan can both print percentage formats. Use the first
        # native source detected so duplicate streams cannot create fake frame
        # boundaries.
        if self.active_source is None:
            self.active_source = source
        elif source != self.active_source:
            return None

        if self.frame_count == 1:
            overall = int(percent)
        else:
            # ALF progress starts again at zero for every frame. A significant
            # high-to-low transition means the previous frame completed.
            if (
                self.last_frame_progress is not None
                and self.last_frame_progress >= 10.0
                and percent <= 5.0
            ):
                self.completed_frames = min(
                    self.frame_count - 1,
                    self.completed_frames + 1,
                )
            overall = int(
                100.0
                * (self.completed_frames + percent / 100.0)
                / self.frame_count
            )
            # The worker's final marker owns 100%; renderer progress may still
            # be followed by image writing and process shutdown.
            overall = min(99, overall)

        self.last_frame_progress = percent
        overall = max(self.last_overall_progress, overall)
        self.last_overall_progress = overall
        return overall


def _apply_context_file(path):
    """Restore the AYON/Houdini environment saved beside the farm HIP."""
    if not path:
        return
    with open(path, "r") as stream:
        payload = json.load(stream)
    for section_name in ("launcher_env", "pipeline_env", "houdini_vars"):
        for key, value in (payload.get(section_name) or {}).items():
            if value is not None:
                os.environ[str(key)] = str(value)


def _houdini_output_template(value):
    """Return a Houdini frame template from a shell-safe hash template."""
    value = str(value or "")
    return re.sub(
        r"(#+)",
        lambda match: "$F{}".format(len(match.group(1))),
        value,
    )


def _concrete_frame_output(value, frame):
    """Resolve Houdini ``$F`` tokens before handing a path to RenderMan USD.

    PxrCryptomatte receives a plain USD string and therefore does not expand
    Houdini variables. Each Cryptomatte Deadline task owns exactly one frame,
    so author the concrete padded filename directly on the session layer.
    """
    value = _houdini_output_template(value)
    numeric_frame = float(frame)
    integral_frame = int(round(numeric_frame))
    if abs(numeric_frame - integral_frame) > 1e-6:
        raise RuntimeError(
            "Cryptomatte requires integral frames; received {}.".format(frame)
        )

    def replace(match):
        width = int(match.group(1) or 1)
        return ("{:0" + str(width) + "d}").format(integral_frame)

    return re.sub(r"\$F(\d*)", replace, value)


def _rop_output_template(rop, requested_output=""):
    """Set and verify the USD Render ROP output override."""
    parm = rop.parm("outputimage")
    if parm is None:
        raise RuntimeError("USD Render ROP has no outputimage parameter.")

    output_template = _houdini_output_template(requested_output)
    if not output_template:
        try:
            output_template = parm.unexpandedString()
        except Exception:
            output_template = parm.evalAsString()
    if not output_template:
        raise RuntimeError("USD Render ROP has no output path.")

    try:
        parm.deleteAllKeyframes()
    except Exception:
        pass
    parm.set(output_template)
    try:
        saved_value = parm.unexpandedString()
    except Exception:
        saved_value = parm.evalAsString()
    if saved_value != output_template:
        raise RuntimeError(
            "Failed to set USD Render ROP output. Expected {!r}, got {!r}.".format(
                output_template, saved_value
            )
        )
    return output_template


def _task_frames(start, end, step):
    """Return the integral frame numbers owned by this Deadline task."""
    start = int(round(float(start)))
    end = int(round(float(end)))
    step = max(1, int(round(abs(float(step)))))
    direction = 1 if end >= start else -1
    return range(start, end + direction, direction * step)


def _expected_outputs(hou_module, output_template, start, end, step):
    return [
        hou_module.expandStringAtFrame(output_template, frame)
        for frame in _task_frames(start, end, step)
    ]


def _validate_render_outputs(output_paths):
    """Reject a renderer success exit when it did not produce usable images."""
    missing = []
    empty = []
    for path in output_paths:
        if not os.path.isfile(path):
            missing.append(path)
        elif os.path.getsize(path) <= 0:
            empty.append(path)
    if missing or empty:
        details = []
        if missing:
            details.append("missing: {}".format(", ".join(missing)))
        if empty:
            details.append("empty: {}".format(", ".join(empty)))
        raise RuntimeError(
            "Render process exited successfully but expected output validation "
            "failed ({})".format("; ".join(details))
        )


def _prepare_render_rop(rop, requested_output="", manage_output=True):
    """Prepare progress/callbacks and optionally manage the ROP output."""
    output_template = (
        _rop_output_template(rop, requested_output) if manage_output else ""
    )
    progress_parm = rop.parm("alfprogress") or rop.parm("vm_alfprogress")
    if progress_parm is None:
        raise RuntimeError(
            "USD Render ROP has no Alfred progress parameter."
        )
    progress_parm.set(1)

    delegate_products_parm = rop.parm("delegateproducts")
    if delegate_products_parm is not None:
        delegate_products_parm.set(1)

    for event_name in ("prerender", "preframe", "postframe", "postrender"):
        toggle = rop.parm("t{}".format(event_name))
        script = rop.parm(event_name)
        if toggle is not None:
            toggle.set(0)
        if script is not None:
            script.set("")
    return output_template


def _rop_parm_string(rop, names):
    for name in names:
        parm = rop.parm(name)
        if parm is None:
            continue
        try:
            value = parm.evalAsString().strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _prepare_deep_render_product(
    rop, output_template, replace_products=True
):
    """Author a minimal RIS Deep EXR product on the active RenderSettings.

    ``replace_products`` preserves the old dedicated-deep mode for previously
    submitted jobs. New dispatcher jobs append Deep to the existing Beauty
    products so one HdPrman render invocation writes both outputs.
    """
    import hou
    from pxr import Sdf, Usd, UsdRender

    lop_path = _rop_parm_string(rop, ("loppath", "lop_path", "lopnode"))
    lop_node = hou.node(lop_path) if lop_path else None
    if lop_node is None:
        raise RuntimeError(
            "Cannot configure Deep EXR because the USD Render ROP LOP path "
            "is invalid: {}".format(lop_path or "<empty>")
        )
    stage = lop_node.stage()
    if stage is None:
        raise RuntimeError("Cannot configure Deep EXR without a USD stage.")

    settings_path = _rop_parm_string(
        rop, ("rendersettings", "render_settings", "rendersettingsprim")
    )
    settings_prim = stage.GetPrimAtPath(settings_path) if settings_path else None
    if not settings_prim or not settings_prim.IsValid():
        candidates = [
            prim for prim in stage.Traverse()
            if prim.GetTypeName() == "RenderSettings"
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                "Cannot identify one RenderSettings prim for the Deep EXR job."
            )
        settings_prim = candidates[0]
        settings_path = settings_prim.GetPath().pathString

    settings = UsdRender.Settings(settings_prim)
    source_products = list(settings.GetProductsRel().GetTargets())
    if not source_products:
        source_products = [
            prim.GetPath() for prim in stage.Traverse()
            if prim.GetTypeName() == "RenderProduct"
        ]
    if not source_products:
        raise RuntimeError("No RenderProduct is available for the Deep EXR job.")

    ordered_vars = []
    for product_path in source_products:
        product_prim = stage.GetPrimAtPath(product_path)
        if not product_prim or not product_prim.IsValid():
            continue
        for target in UsdRender.Product(product_prim).GetOrderedVarsRel().GetTargets():
            if target not in ordered_vars:
                ordered_vars.append(target)

    # A Nuke holdout does not need a separate flat Z or lighting AOVs.
    # RenderMan's DeepEXR driver attaches deep.front/deep.back to a sampled
    # color/opacity payload, so retain Ci and alpha; an alpha-only product can
    # finish quickly while containing no usable deep samples.
    # The UI selection filters orderedVars on the flat products. Deep still
    # needs Ci and alpha, so reuse matching RenderVars already present on the
    # stage without adding them back to the Beauty product.
    candidate_vars = list(ordered_vars)
    for prim in stage.Traverse():
        if prim.GetTypeName() != "RenderVar":
            continue
        path = prim.GetPath()
        if path not in candidate_vars:
            candidate_vars.append(path)

    wanted_vars = []
    matched_roles = set()
    for var_path in candidate_vars:
        var_prim = stage.GetPrimAtPath(var_path)
        if not var_prim or not var_prim.IsValid():
            continue
        source_attr = var_prim.GetAttribute("sourceName")
        source_name = source_attr.Get() if source_attr else ""
        names = {
            str(var_prim.GetName()).strip("_").lower(),
            str(source_name or "").strip("_").lower(),
        }
        role = None
        if "ci" in names:
            role = "ci"
        elif names & {"a", "alpha"}:
            role = "alpha"
        if role and role not in matched_roles:
            wanted_vars.append(var_path)
            matched_roles.add(role)
    has_ci = "ci" in matched_roles
    has_alpha = "alpha" in matched_roles
    if not (has_ci and has_alpha):
        missing = []
        if not has_ci:
            missing.append("Ci")
        if not has_alpha:
            missing.append("alpha")
        raise RuntimeError(
            "Deep Holdout requires Ci and alpha RenderVars on the selected "
            "RenderProduct (missing: {}).".format(", ".join(missing))
        )

    session_layer = stage.GetSessionLayer()
    with Usd.EditContext(stage, session_layer):
        deep_path = Sdf.Path(settings_path).AppendChild("BMFXDeepHoldout")
        deep_product = UsdRender.Product.Define(stage, deep_path)
        deep_product.CreateProductNameAttr().Set(output_template)
        # HdPrman treats productType as its display-driver token. Using the
        # generic USD token ``deepRaster`` makes it search for the nonexistent
        # d_deepRaster.so; RenderMan's installed driver is d_deepexr.so.
        deep_product.CreateProductTypeAttr().Set("deepexr")
        deep_product.CreateOrderedVarsRel().SetTargets(wanted_vars)

        deep_prim = deep_product.GetPrim()
        deep_prim.CreateAttribute(
            "ri:productType", Sdf.ValueTypeNames.Token
        ).Set("deepexr")
        deep_prim.CreateAttribute(
            "ri:displayDriver:asrgba", Sdf.ValueTypeNames.Bool
        ).Set(True)
        deep_prim.CreateAttribute(
            "ri:displayDriver:storage", Sdf.ValueTypeNames.Token
        ).Set("scanline")
        deep_prim.CreateAttribute(
            "ri:displayDriver:type", Sdf.ValueTypeNames.Token
        ).Set("half")
        deep_prim.CreateAttribute(
            "ri:displayDriver:compression", Sdf.ValueTypeNames.Token
        ).Set("zips")
        if replace_products:
            product_targets = [deep_path]
        else:
            product_targets = [
                path for path in source_products if path != deep_path
            ]
            product_targets.append(deep_path)
        settings.GetProductsRel().SetTargets(product_targets)

    print(
        "Deep EXR product: {} (RenderVars: {})".format(
            deep_path.pathString,
            ", ".join(path.pathString for path in wanted_vars),
        ),
        flush=True,
    )


def _prepare_cryptomatte_outputs(rop, mapping_path, frame):
    """Override creator-authored Cryptomatte filenames for this farm version."""
    if not mapping_path:
        return []
    with open(mapping_path, "r") as stream:
        specs = (json.load(stream) or {}).get("cryptomattes") or []
    if not specs:
        return []

    import hou
    from pxr import Usd

    lop_path = _rop_parm_string(rop, ("loppath", "lop_path", "lopnode"))
    lop_node = hou.node(lop_path) if lop_path else None
    if lop_node is None or lop_node.stage() is None:
        raise RuntimeError(
            "Cannot configure Cryptomatte because the USD Render ROP LOP "
            "path is invalid: {}".format(lop_path or "<empty>")
        )
    stage = lop_node.stage()
    output_templates = []
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for spec in specs:
            prim_path = str(spec.get("prim_path") or "")
            output_template = _concrete_frame_output(
                spec.get("output_path") or "", frame
            )
            prim = stage.GetPrimAtPath(prim_path) if prim_path else None
            if (
                not prim
                or not prim.IsValid()
                or "cryptomatte" not in str(prim.GetTypeName() or "").lower()
            ):
                raise RuntimeError(
                    "Cryptomatte creator prim was not found: {}".format(
                        prim_path or "<empty>"
                    )
                )
            if not output_template:
                raise RuntimeError(
                    "Cryptomatte output path is empty for {}.".format(prim_path)
                )
            filename_attr = None
            for attr_name in (
                "inputs:ri:filename", "ri:filename",
                "inputs:filename", "filename",
            ):
                attr = prim.GetAttribute(attr_name)
                if attr and attr.IsValid():
                    filename_attr = attr
                    break
            if filename_attr is None:
                raise RuntimeError(
                    "Cryptomatte prim {} has no filename input.".format(prim_path)
                )
            filename_attr.Set(output_template)
            output_templates.append(output_template)
            print(
                "Cryptomatte output: {} -> {}".format(
                    prim_path, output_template
                ),
                flush=True,
            )
    return output_templates


def _worker(
    scene,
    rop_path,
    context_path,
    output,
    start,
    end,
    step,
    deep=False,
    deep_output="",
    cryptomatte_map="",
):
    _apply_context_file(context_path)
    import hou

    hou.hipFile.load(
        scene,
        suppress_save_prompt=True,
        ignore_load_warnings=True,
    )
    rop = hou.node(rop_path)
    if rop is None:
        raise RuntimeError("USD Render ROP was not found: {}".format(rop_path))
    # A dedicated Cryptomatte job writes only through the Creator HDA's
    # PxrCryptomatte filename. Its USD Render ROP outputimage must remain
    # untouched and must not be treated as an expected farm deliverable.
    manage_rop_output = bool(output) or not bool(cryptomatte_map)
    output_template = _prepare_render_rop(
        rop, output, manage_output=manage_rop_output
    )
    if deep:
        _prepare_deep_render_product(rop, output_template)
    output_templates = [output_template] if output_template else []
    if deep_output:
        deep_output_template = _houdini_output_template(deep_output)
        _prepare_deep_render_product(
            rop, deep_output_template, replace_products=False
        )
        output_templates.append(deep_output_template)
    if cryptomatte_map and abs(float(end) - float(start)) > 1e-6:
        raise RuntimeError(
            "Cryptomatte farm tasks must contain exactly one frame. "
            "Set Deadline Chunk Size to 1."
        )
    output_templates.extend(
        _prepare_cryptomatte_outputs(rop, cryptomatte_map, start)
    )
    expected_outputs = []
    for template in output_templates:
        expected_outputs.extend(
            _expected_outputs(hou, template, start, end, step)
        )
    for path in expected_outputs:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    print(
        "ROP output override: {}".format(
            output_template if output_template else "<not set>"
        ),
        flush=True,
    )
    print(
        "Expected task output{}: {}".format(
            "s" if len(expected_outputs) != 1 else "",
            ", ".join(expected_outputs),
        ),
        flush=True,
    )

    _emit_progress(0)
    # Deadline's stock hrender_dl.py omits these two keyword arguments. They
    # are what ask Houdini to emit native ALF_PROGRESS lines during a render.
    rop.render(
        (float(start), float(end), float(step)),
        ignore_inputs=False,
        verbose=True,
        output_progress=True,
    )
    _validate_render_outputs(expected_outputs)
    _emit_progress(100)
    return 0


def _parent(hython, script_path, args):
    _apply_context_file(args.context)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    command = [
        hython,
        script_path,
        "--worker",
        "--scene", args.scene,
        "--rop", args.rop_path,
        "--context", args.context,
        "--output", args.output,
        "--start", str(args.start),
        "--end", str(args.end),
        "--step", str(args.step),
    ]
    if args.deep:
        command.append("--deep")
    if args.deep_output:
        command.extend(("--deep-output", args.deep_output))
    if args.cryptomatte_map:
        command.extend(("--cryptomatte-map", args.cryptomatte_map))
    print("Houdini render command: {}".format(" ".join(command)), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=environment,
    )
    sampler = None
    if args.metrics:
        try:
            try:
                from ayon_houdini.nodes.farm_report import ResourceSampler
            except ImportError:
                from farm_report import ResourceSampler
            sampler = ResourceSampler(
                process.pid,
                args.metrics,
                metadata={
                    "job_id": os.getenv("DEADLINE_JOB_ID", ""),
                    "task_id": os.getenv("DEADLINE_TASK_ID", ""),
                    "frame_start": args.start,
                    "frame_end": args.end,
                },
            ).start()
        except Exception as exc:
            print("Farm telemetry unavailable: {}".format(exc), flush=True)
    assert process.stdout is not None
    task_progress = _TaskProgress(args.start, args.end, args.step)
    last_progress = None
    for line in process.stdout:
        line = line.rstrip("\r\n")
        is_progress_line = False
        for source, pattern in _PROGRESS_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            is_progress_line = True
            progress = task_progress.update(source, match.group(1))
            # Never forward native ALF/RMAN/Husk markers to Deadline. Multiple
            # renderer phases can restart those markers at zero, causing the
            # Monitor to show a false second render. Publish only this
            # monotonic wrapper stream, including the child worker's markers.
            if progress is not None and progress != last_progress:
                _emit_progress(progress)
            if progress is not None:
                last_progress = progress
            break
        if not is_progress_line:
            print(line, flush=True)
    return_code = process.wait()
    if sampler is not None:
        try:
            sampler.stop()
        except Exception as exc:
            print("Could not write farm telemetry: {}".format(exc), flush=True)
    if return_code:
        raise RuntimeError(
            "Houdini render failed with exit code {}.".format(return_code)
        )
    if last_progress != 100:
        _emit_progress(100)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--hython")
    parser.add_argument("--scene", required=True)
    parser.add_argument("--rop", required=True, dest="rop_path")
    parser.add_argument("--context", required=True)
    parser.add_argument(
        "--output",
        default="",
        help="Expected output template; hashes are converted to Houdini $F tokens.",
    )
    parser.add_argument("--start", required=True, type=float)
    parser.add_argument("--end", required=True, type=float)
    parser.add_argument("--step", type=float, default=1.0)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Render a minimal RenderMan RIS Deep EXR holdout product.",
    )
    parser.add_argument(
        "--deep-output",
        default="",
        help=(
            "Append a Deep EXR product to Beauty in the same render; hashes "
            "are converted to Houdini $F tokens."
        ),
    )
    parser.add_argument(
        "--cryptomatte-map",
        default="",
        help="JSON mapping of PxrCryptomatte prims to versioned EXR outputs.",
    )
    parser.add_argument(
        "--metrics",
        default="",
        help="Write CPU/RAM/GPU task telemetry to this JSON path.",
    )
    args = parser.parse_args(argv)
    if args.worker:
        return _worker(
            args.scene,
            args.rop_path,
            args.context,
            args.output,
            args.start,
            args.end,
            args.step,
            deep=args.deep,
            deep_output=args.deep_output,
            cryptomatte_map=args.cryptomatte_map,
        )
    if not args.hython or not os.path.isfile(args.hython):
        raise RuntimeError("Hython executable was not found: {}".format(args.hython))
    return _parent(args.hython, os.path.abspath(__file__), args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("HOUDINI FARM RENDER ERROR: {}".format(exc), file=sys.stderr, flush=True)
        raise
