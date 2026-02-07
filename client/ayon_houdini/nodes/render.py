import hou
import logging
from datetime import datetime

# --------------------------------------------------
# LOGGER SETUP (Deadline-safe)
# --------------------------------------------------

_LOGGER = logging.getLogger("BMFX.FileCache")

if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)

_LOGGER.setLevel(logging.INFO)


# --------------------------------------------------
# CALLBACKS
# --------------------------------------------------

def on_prerender():
    """Pre-render callback."""
    rop_node = hou.pwd()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _LOGGER.info("=" * 50)
    _LOGGER.info("JOB START")
    _LOGGER.info("Time : %s", timestamp)
    _LOGGER.info("Node : %s", rop_node.path())
    _LOGGER.info("=" * 50)


def on_preframe():
    """Pre-frame callback (per frame start)."""
    rop_node = hou.pwd().parent().parent()
    frame = int(hou.frame())

    # Deadline progress update
    # print(f"ALF_PROGRESS {frame}")

    _LOGGER.info(
        "Rendering '%s' | Frame %s",
        rop_node.path(),
        frame
    )


def on_postframe():
    """Post-frame callback (per frame end)."""
    rop_node = hou.pwd().parent().parent()
    frame = int(hou.frame())

    _LOGGER.info(
        "Finished '%s' | Frame %s",
        rop_node.path(),
        frame
    )


def on_postrender():
    """Post-render callback."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _LOGGER.info("=" * 50)
    _LOGGER.info("JOB FINISHED")
    _LOGGER.info("Time : %s", timestamp)
    _LOGGER.info("=" * 50)
