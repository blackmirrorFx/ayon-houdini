"""Create color-managed MOV and MP4 review files on a farm worker."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile


def _frame_path(template, frame, padding):
    return template.replace("#" * int(padding), str(int(frame)).zfill(int(padding)))


def _emit_progress(percent):
    print("Progress: {}%".format(max(0, min(100, int(percent)))), flush=True)


def _run(command, environment=None, progress_start=0, progress_end=100, duration=0.0):
    print("Running: {}".format(" ".join(command)), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=environment,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        print(line, flush=True)
        match = re.match(r"out_time_ms=(\d+)", line)
        if match and duration > 0:
            seconds = float(match.group(1)) / 1000000.0
            fraction = min(1.0, seconds / duration)
            _emit_progress(progress_start + fraction * (progress_end - progress_start))
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            "Command failed with exit code {}: {}".format(
                return_code, " ".join(command)
            )
        )
    _emit_progress(progress_end)


def _validate_inputs(template, start, end, padding):
    missing = [
        _frame_path(template, frame, padding)
        for frame in range(start, end + 1)
        if not os.path.isfile(_frame_path(template, frame, padding))
    ]
    if missing:
        raise RuntimeError(
            "Review input sequence is incomplete ({} missing):\n{}".format(
                len(missing), "\n".join(missing[:10])
            )
        )


def create_movies(
    ffmpeg,
    ocio_config,
    input_colorspace,
    display,
    view,
    output_primaries,
    output_transfer,
    output_matrix,
    input_template,
    mov_path,
    mp4_path,
    start,
    end,
    fps,
    padding=4,
):
    """Apply the OCIO display transform, then encode delivery-safe movies."""
    start = int(start)
    end = int(end)
    padding = int(padding)
    fps = float(fps)
    if end < start or fps <= 0:
        raise ValueError("Invalid frame range or FPS.")
    if not os.path.isfile(ffmpeg):
        raise RuntimeError("Required executable was not found: {}".format(ffmpeg))
    if not ocio_config or not os.path.isfile(ocio_config):
        raise RuntimeError("OCIO configuration was not found: {}".format(ocio_config))
    if not all((
        input_colorspace,
        display,
        view,
        output_primaries,
        output_transfer,
        output_matrix,
    )):
        raise RuntimeError("OCIO transform and video color metadata are required.")
    _validate_inputs(input_template, start, end, padding)

    for path in (mov_path, mp4_path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    work_root = tempfile.mkdtemp(prefix="bmfx_review_", dir=os.path.dirname(mov_path))
    environment = os.environ.copy()
    environment["OCIO"] = ocio_config
    try:
        frame_count = end - start + 1
        staged_template = os.path.join(work_root, "review.%0{}d.tif".format(padding))
        _emit_progress(0)
        for index, frame in enumerate(range(start, end + 1), 1):
            source = _frame_path(input_template, frame, padding)
            staged = os.path.join(
                work_root, "review.{}.tif".format(str(frame).zfill(padding))
            )
            # 16-bit TIFF retains sufficient precision for ProRes 422 HQ.
            import OpenImageIO as oiio

            source_buf = oiio.ImageBuf(source)
            if source_buf.has_error:
                raise RuntimeError(source_buf.geterror())
            channel_names = list(source_buf.spec().channelnames)
            rgb_indices = []
            for candidates in (("R", "Ci.R", "Ci.r"), ("G", "Ci.G", "Ci.g"), ("B", "Ci.B", "Ci.b")):
                index = next(
                    (channel_names.index(name) for name in candidates if name in channel_names),
                    None,
                )
                if index is None:
                    raise RuntimeError(
                        "No RGB/Ci beauty channels were found in {}.".format(source)
                    )
                rgb_indices.append(index)
            beauty_buf = oiio.ImageBufAlgo.channels(
                source_buf, tuple(rgb_indices), ("R", "G", "B")
            )
            display_buf = oiio.ImageBufAlgo.ociodisplay(
                beauty_buf,
                display,
                view,
                fromspace=input_colorspace,
                colorconfig=ocio_config,
            )
            if display_buf.has_error:
                raise RuntimeError(display_buf.geterror())
            display_buf.set_write_format(oiio.UINT16)
            if not display_buf.write(staged):
                raise RuntimeError(
                    "Could not write color-managed frame {}: {}".format(
                        staged, display_buf.geterror()
                    )
                )
            _emit_progress(index * 55.0 / frame_count)

        duration = frame_count / fps
        common = [
            ffmpeg, "-y", "-nostats", "-loglevel", "error",
            "-progress", "pipe:1", "-framerate", str(fps),
            "-start_number", str(start), "-i", staged_template,
            "-frames:v", str(frame_count), "-an", "-map_metadata", "-1",
            "-color_primaries", output_primaries,
            "-color_trc", output_transfer,
            "-colorspace", output_matrix,
            "-color_range", "tv",
        ]
        mov_encoded = mov_path + ".partial_encode.mov"
        mov_temp = mov_path + ".partial.mov"
        mp4_temp = mp4_path + ".partial.mp4"
        _run(
            common + [
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "prores_ks", "-profile:v", "3",
                "-pix_fmt", "yuv422p10le", mov_encoded,
            ],
            environment=environment,
            progress_start=55,
            progress_end=73,
            duration=duration,
        )
        # A stream-copy pass writes both the ProRes frame metadata and the MOV
        # colr atom. Some ProRes encoders do not propagate all three tags while
        # initially encoding, even when the codec context is configured.
        _run(
            [
                ffmpeg, "-y", "-nostats", "-loglevel", "error",
                "-progress", "pipe:1", "-i", mov_encoded, "-c", "copy",
                "-bsf:v",
                "prores_metadata=color_primaries={}:color_trc={}:colorspace={}".format(
                    output_primaries, output_transfer, output_matrix
                ),
                "-color_primaries", output_primaries,
                "-color_trc", output_transfer,
                "-colorspace", output_matrix,
                "-movflags", "+write_colr", mov_temp,
            ],
            environment=environment,
            progress_start=73,
            progress_end=78,
            duration=duration,
        )
        os.unlink(mov_encoded)
        _run(
            common + [
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
                "-x264-params", "colorprim={}:transfer={}:colormatrix={}:fullrange=off".format(
                    output_primaries, output_transfer, output_matrix
                ),
                "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart+write_colr", mp4_temp,
            ],
            environment=environment,
            progress_start=78,
            progress_end=100,
            duration=duration,
        )
        os.replace(mov_temp, mov_path)
        os.replace(mp4_temp, mp4_path)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ocio-config", required=True)
    parser.add_argument("--input-colorspace", required=True)
    parser.add_argument("--display", required=True)
    parser.add_argument("--view", required=True)
    parser.add_argument("--output-primaries", required=True)
    parser.add_argument("--output-transfer", required=True)
    parser.add_argument("--output-matrix", required=True)
    parser.add_argument("--input", required=True, dest="input_template")
    parser.add_argument("--mov", required=True, dest="mov_path")
    parser.add_argument("--mp4", required=True, dest="mp4_path")
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", required=True, type=int)
    parser.add_argument("--fps", required=True, type=float)
    parser.add_argument("--padding", type=int, default=4)
    args = parser.parse_args(argv)
    create_movies(**vars(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("REVIEW MEDIA ERROR: {}".format(exc), file=sys.stderr, flush=True)
        raise
