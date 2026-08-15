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


def _prepare_render_rop(rop, requested_output=""):
    """Set the output, enable progress, and remove callback reporting."""
    output_template = _rop_output_template(rop, requested_output)
    progress_parm = rop.parm("alfprogress") or rop.parm("vm_alfprogress")
    if progress_parm is None:
        raise RuntimeError(
            "USD Render ROP has no Alfred progress parameter."
        )
    progress_parm.set(1)

    for event_name in ("prerender", "preframe", "postframe", "postrender"):
        toggle = rop.parm("t{}".format(event_name))
        script = rop.parm(event_name)
        if toggle is not None:
            toggle.set(0)
        if script is not None:
            script.set("")
    return output_template


def _worker(scene, rop_path, context_path, output, start, end, step):
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
    output_template = _prepare_render_rop(rop, output)
    expected_outputs = _expected_outputs(
        hou, output_template, start, end, step
    )
    for path in expected_outputs:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    print("ROP output override: {}".format(output_template), flush=True)
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
    print("Houdini render command: {}".format(" ".join(command)), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=environment,
    )
    assert process.stdout is not None
    task_progress = _TaskProgress(args.start, args.end, args.step)
    last_progress = None
    for line in process.stdout:
        line = line.rstrip("\r\n")
        print(line, flush=True)
        for source, pattern in _PROGRESS_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            progress = task_progress.update(source, match.group(1))
            if (
                progress is not None
                and progress != last_progress
                and source != "wrapper"
            ):
                _emit_progress(progress)
            if progress is not None:
                last_progress = progress
            break
    return_code = process.wait()
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
