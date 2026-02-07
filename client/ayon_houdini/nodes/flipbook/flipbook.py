import hou
from ayon_core.pipeline import (
    get_current_project_name,
    get_current_context
)
from ayon_api import get_folder_by_name
import os
import re

def get_next_version(path):
    if not os.path.exists(path):
        return 1

    versions = []
    for f in os.listdir(path):
        m = re.search(r"v(\d+)", f)
        if m:
            versions.append(int(m.group(1)))

    return max(versions) + 1 if versions else 1


def get_flipbook_path(ext="mov"):
    ctx = get_current_context()
    project = get_current_project_name()

    folder = ctx.get("folder", {}).get("name", "unknown_asset")
    task = ctx.get("task", {}).get("name", "fx")

    base_dir = f"/projects/{project}/publish/flipbook/{folder}/{task}"

    version = get_next_version(base_dir)
    version_str = f"v{version:03d}"

    out_dir = os.path.join(base_dir, version_str)
    os.makedirs(out_dir, exist_ok=True)

    file_name = f"{folder}_{task}_{version_str}.{ext}"
    return os.path.join(out_dir, file_name)


def create_ayon_flipbook(
    camera="/obj/cam1",
    start=1001,
    end=1100,
    resolution=(1920, 1080),
    aa=4,
    ext="mov"
):
    scene_viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if not scene_viewer:
        hou.ui.displayMessage("No Scene Viewer found")
        return

    flipbook = scene_viewer.flipbookSettings()

    # Camera
    flipbook.setUseCamera(True)
    flipbook.setCamera(hou.node(camera))

    # Frame range
    flipbook.setFrameRange((start, end))
    flipbook.setFrameIncrement(1)

    # Resolution
    flipbook.setResolution(resolution)
    flipbook.setCropOutMaskOverlay(True)

    # Anti-alias
    flipbook.setAntialias(aa)

    # Output
    output_path = get_flipbook_path(ext)
    flipbook.setOutput(output_path)
    flipbook.setOutputToMPlay(False)

    print(f"[AYON FLIPBOOK] Output: {output_path}")

    scene_viewer.flipbook(scene_viewer.curViewport(), flipbook)
