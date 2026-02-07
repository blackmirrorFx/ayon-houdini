from ayon_api import get_folders, get_tasks
from ayon_core.pipeline import (
    get_current_project_name,
    get_current_folder_path,
)


def get_tasks_for_current_shot():
    # --------------------------------------------------
    # 1. Context
    # --------------------------------------------------
    project = get_current_project_name()
    folder_path = get_current_folder_path()

    if not project or not folder_path:
        raise RuntimeError("Not in a valid AYON context")

    # --------------------------------------------------
    # 2. Resolve folder entity (generator → single item)
    # --------------------------------------------------
    folder_entity = next(
        get_folders(
            project_name=project,
            folder_paths=[folder_path]
        ),
        None
    )

    if not folder_entity:
        raise RuntimeError(f"Folder not found in AYON DB: {folder_path}")

    folder_id = folder_entity["id"]

    # --------------------------------------------------
    # 3. Get tasks (GENERATOR!)
    # --------------------------------------------------
    task_entities = get_tasks(
        project_name=project,
        folder_ids=[folder_id]
    )

    # --------------------------------------------------
    # 4. Convert generator → dict
    # --------------------------------------------------
    tasks = {}
    for task in task_entities:
        tasks[task["name"]] = task

    return tasks


# --------------------------------------------------
# USAGE
# --------------------------------------------------
tasks = get_tasks_for_current_shot()

# print("Tasks available for this shot:")
# for task_name, task_data in tasks.items():
#     print(f"  - {task_name} ({task_data['taskType']})")
