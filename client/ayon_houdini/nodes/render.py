import hou
import logging
import math
import sys
from datetime import datetime

# --------------------------------------------------
# LOGGER SETUP (Deadline-safe)
# --------------------------------------------------

def _configure_unbuffered_stdio():
    """Best effort: force line-buffered output for farm log streaming."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass


def _flush_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


_configure_unbuffered_stdio()

_LOGGER = logging.getLogger("BMFX.FileCache")

if not _LOGGER.handlers:
    # Use stdout so Deadline's Houdini plugin sees log lines as render output.
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)

_LOGGER.setLevel(logging.INFO)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

_EPSILON = 1e-8


def _iter_context_nodes():
    """Yield current callback node and its parents."""
    node = hou.pwd()
    while node:
        yield node
        node = node.parent()


def _find_parm(*parm_names):
    """Return first matching parm found in callback context."""
    for node in _iter_context_nodes():
        for name in parm_names:
            parm = node.parm(name)
            if parm:
                return parm
    return None


def _get_render_node():
    """Resolve the parent render HDA node when callbacks run in internal ROPs."""
    node = hou.pwd()
    if not node:
        return None

    parent = node.parent()
    grand_parent = parent.parent() if parent else None
    return grand_parent or node


def _format_frame(frame):
    """Return compact frame string that avoids trailing decimals for ints."""
    if abs(frame - int(frame)) <= _EPSILON:
        return str(int(frame))
    return f"{frame:g}"


def _resolve_frame_range():
    """Resolve render frame range from local ROP/HDA parms."""
    current_frame = float(hou.frame())

    trange_parm = _find_parm("trange", "frame_range")
    if trange_parm:
        mode = str(trange_parm.evalAsString()).strip().lower()
        if mode in {"off", "0"}:
            return current_frame, current_frame, 1.0

    start_parm = _find_parm("f1", "fx", "frame_start", "start_frame")
    end_parm = _find_parm("f2", "fy", "frame_end", "end_frame")
    step_parm = _find_parm("f3", "fz", "inc", "frame_inc", "step")

    start = float(start_parm.eval()) if start_parm else current_frame
    end = float(end_parm.eval()) if end_parm else current_frame
    step = abs(float(step_parm.eval())) if step_parm else 1.0
    if step <= 0:
        step = 1.0

    return start, end, step


def _frame_count(start, end, step):
    return max(1, int(math.floor((abs(end - start) / step) + _EPSILON)) + 1)


def _completed_frames(frame, start, end, step):
    if end >= start:
        offset = frame - start
    else:
        offset = start - frame
    return int(math.floor((offset / step) + _EPSILON)) + 1


def _emit_deadline_progress(percent):
    """Emit Deadline/Alfred-compatible progress line."""
    percent = max(0, min(100, int(round(percent))))
    print(f"ALF_PROGRESS {percent}%", flush=True)
    _flush_stdio()


# --------------------------------------------------
# CALLBACKS
# --------------------------------------------------

def on_prerender():
    """Pre-render callback."""
    render_node = _get_render_node()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _LOGGER.info("=" * 50)
    _LOGGER.info("JOB START")
    _LOGGER.info("Time : %s", timestamp)
    _LOGGER.info("Node : %s", render_node.path() if render_node else "<unknown>")
    _LOGGER.info("=" * 50)
    _emit_deadline_progress(0)


def on_preframe():
    """Pre-frame callback (per frame start)."""
    render_node = _get_render_node()
    frame = float(hou.frame())

    _LOGGER.info(
        "Rendering '%s' | Frame %s",
        render_node.path() if render_node else "<unknown>",
        _format_frame(frame)
    )
    _flush_stdio()


def on_postframe():
    """Post-frame callback (per frame end)."""
    render_node = _get_render_node()
    frame = float(hou.frame())
    start, end, step = _resolve_frame_range()
    total_frames = _frame_count(start, end, step)
    done_frames = max(0, min(total_frames, _completed_frames(frame, start, end, step)))
    percent = (float(done_frames) / float(total_frames)) * 100.0

    _emit_deadline_progress(percent)

    _LOGGER.info(
        "Finished '%s' | Frame %s (%d/%d | %d%%)",
        render_node.path() if render_node else "<unknown>",
        _format_frame(frame),
        done_frames,
        total_frames,
        int(round(percent))
    )
    _flush_stdio()


def on_postrender():
    """Post-render callback."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _emit_deadline_progress(100)
    _LOGGER.info("=" * 50)
    _LOGGER.info("JOB FINISHED")
    _LOGGER.info("Time : %s", timestamp)
    _LOGGER.info("=" * 50)
