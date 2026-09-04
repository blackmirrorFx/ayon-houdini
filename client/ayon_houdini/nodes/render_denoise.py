"""Farm-safe wrapper around RenderMan's offline ``denoise_batch`` tool."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys


def _frame_path(template, frame, padding):
    token = "#" * int(padding)
    return template.replace(token, str(int(frame)).zfill(int(padding)))


def _emit_progress(percent):
    percent = max(0, min(100, int(float(percent))))
    print("Progress: {}%".format(percent), flush=True)


def _validate_inputs(template, start, end, padding):
    missing = [
        _frame_path(template, frame, padding)
        for frame in range(int(start), int(end) + 1)
        if not os.path.isfile(_frame_path(template, frame, padding))
    ]
    if missing:
        preview = "\n".join(missing[:10])
        remainder = len(missing) - min(10, len(missing))
        if remainder:
            preview += "\n... and {} more".format(remainder)
        raise RuntimeError(
            "Denoise input sequence is incomplete ({} missing):\n{}".format(
                len(missing), preview
            )
        )


def _collect_denoised_outputs(input_template, output_dir, start, end, padding):
    """Resolve and validate the complete staged sequence before replacement."""
    resolved = []
    for frame in range(int(start), int(end) + 1):
        original = _frame_path(input_template, frame, padding)
        basename = os.path.basename(original)
        matches = []
        for root, _dirs, files in os.walk(output_dir):
            if basename in files:
                matches.append(os.path.join(root, basename))
        if len(matches) != 1:
            raise RuntimeError(
                "Expected one staged denoised result for {}, found {}.".format(
                    original, len(matches)
                )
            )
        candidate = matches[0]
        if os.path.getsize(candidate) <= 0:
            raise RuntimeError("Denoised output is empty: {}".format(candidate))
        resolved.append((candidate, original))
    return resolved


def finalize_denoise(input_template, output_dir, start, end, padding=4):
    """Replace the raw sequence only after every staged frame validates."""
    if int(end) < int(start):
        raise ValueError("Invalid denoise finalize frame range.")
    _emit_progress(0)
    _validate_inputs(input_template, start, end, padding)
    _emit_progress(10)
    resolved = _collect_denoised_outputs(
        input_template, output_dir, start, end, padding
    )
    _emit_progress(30)
    total = float(len(resolved))
    for index, (candidate, original) in enumerate(resolved, 1):
        os.replace(candidate, original)
        print("Replaced source render: {}".format(original), flush=True)
        _emit_progress(30 + index * 65.0 / total)
    _emit_progress(98)
    shutil.rmtree(output_dir, ignore_errors=True)
    _emit_progress(100)


def run_denoise(
    executable,
    input_template,
    output_dir,
    start,
    end,
    padding=4,
    sequence_start=None,
    sequence_end=None,
    handles=2,
):
    """Denoise one target chunk using overlapping raw temporal context."""
    if not os.path.isfile(executable):
        raise RuntimeError("RenderMan denoise_batch was not found: {}".format(executable))
    start = int(start)
    end = int(end)
    sequence_start = start if sequence_start is None else int(sequence_start)
    sequence_end = end if sequence_end is None else int(sequence_end)
    handles = max(0, int(handles))
    if end < start or sequence_end < sequence_start:
        raise ValueError("Invalid denoise target or sequence frame range.")
    if start < sequence_start or end > sequence_end:
        raise ValueError("Denoise target range is outside the sequence range.")

    _emit_progress(0)
    is_animation = sequence_end > sequence_start
    effective_handles = handles if is_animation else 0
    context_start = max(sequence_start, start - effective_handles)
    context_end = min(sequence_end, end + effective_handles)
    _validate_inputs(input_template, context_start, context_end, padding)
    _emit_progress(3)
    os.makedirs(output_dir, exist_ok=True)
    _emit_progress(5)

    command = [executable]
    if is_animation:
        command.extend(("--crossframe", "--flow"))
    command.extend((
        "--progress",
        "--output", output_dir,
    ))
    if is_animation:
        command.extend((
            "--frame-include", "{}-{}".format(start, end),
        ))
    command.extend((
        input_template,
        "{}-{}".format(context_start, context_end),
    ))
    print(
        "Denoise target {}-{} with context {}-{} ({} handles).".format(
            start, end, context_start, context_end, effective_handles
        ),
        flush=True,
    )
    print("RenderMan denoise command: {}".format(" ".join(command)), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    last_progress = -1
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        print(line, flush=True)
        match = re.search(
            r"(?:ALF_PROGRESS|Progress:?|R90000)"
            r"[^0-9]*([0-9]+(?:\.[0-9]+)?)%?",
            line,
            re.IGNORECASE,
        )
        if match:
            progress = int(float(match.group(1)))
            if progress != last_progress:
                # Reserve the first 5% for validation and the final 1% for
                # successful process completion. This keeps Deadline moving
                # during setup without ever reporting success too early.
                _emit_progress(5 + progress * 0.94)
                last_progress = progress
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            "RenderMan denoise_batch failed with exit code {}".format(return_code)
        )
    _emit_progress(100)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable")
    parser.add_argument("--input", required=True, dest="input_template")
    parser.add_argument("--output", required=True, dest="output_dir")
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", required=True, type=int)
    parser.add_argument("--padding", type=int, default=4)
    parser.add_argument("--sequence-start", type=int)
    parser.add_argument("--sequence-end", type=int)
    parser.add_argument("--handles", type=int, default=2)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args(argv)
    if args.finalize:
        finalize_denoise(
            args.input_template,
            args.output_dir,
            args.start,
            args.end,
            padding=args.padding,
        )
        return 0
    if not args.executable:
        parser.error("--executable is required unless --finalize is used")
    run_denoise(
        args.executable,
        args.input_template,
        args.output_dir,
        args.start,
        args.end,
        padding=args.padding,
        sequence_start=args.sequence_start,
        sequence_end=args.sequence_end,
        handles=args.handles,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("DENOISE ERROR: {}".format(exc), file=sys.stderr, flush=True)
        raise
