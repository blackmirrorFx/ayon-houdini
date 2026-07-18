import os
import random
import re
import shutil

try:
    import hou
except Exception:
    hou = None

try:
    from qtpy import QtCore, QtGui, QtWidgets
except Exception:
    QtCore = None
    QtGui = None
    QtWidgets = None

try:
    from ayon_core.pipeline import get_current_context
except Exception:
    get_current_context = None

ayon_api = None

_PROTECTED_DIRS = {"review"}
_PROTECTED_SOURCE_FILES = {
    "source.hip",
    "source.hipnc",
    "source.hiplc",
}
_SWATCH_COLORS = (
    "#5aa4ff",
    "#f0b54a",
    "#66c46a",
    "#5d9dff",
    "#9fa6b2",
    "#a27bd9",
    "#9b9b9b",
    "#3ab4b1",
    "#d474c1",
)
_MAX_HOUDINI_DISCOVERY_DEPTH = 8


def _qt_item_flag(name):
    if QtCore is None:
        return None
    value = getattr(QtCore.Qt, name, None)
    if value is not None:
        return value
    item_flag_enum = getattr(QtCore.Qt, "ItemFlag", None)
    if item_flag_enum is not None:
        return getattr(item_flag_enum, name, None)
    return None


def _normalize(path):
    return os.path.normpath(os.path.abspath(path))


def _pump_qt_events():
    if QtCore is None or QtWidgets is None:
        return
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    try:
        app.processEvents(QtCore.QEventLoop.AllEvents, 5)
    except Exception:
        pass


def _project_root():
    root = os.environ.get("JOB")

    if hou is not None:
        if not root or root == "$JOB":
            try:
                root = hou.text.expandString("$JOB")
            except Exception:
                pass

        if not root or root == "$JOB":
            try:
                root = hou.expandString("$HIP")
            except Exception:
                pass

        if not root and hasattr(hou, "hipFile"):
            try:
                root = os.path.dirname(hou.hipFile.path())
            except Exception:
                pass

    if not root:
        root = os.getcwd()

    return _normalize(root)


def resolve_cache_root():
    context = {}
    if get_current_context is not None:
        try:
            context = get_current_context() or {}
        except Exception:
            context = {}

    folder = (context.get("folder_path") or "").strip("/")
    task = (context.get("task_name") or "").strip()
    base = _project_root()

    if folder and task:
        return _normalize(os.path.join(base, folder, "houdini", task, "cache"))

    return _normalize(os.path.join(base, "cache"))


def _current_project_name():
    context = {}
    if get_current_context is not None:
        try:
            context = get_current_context() or {}
        except Exception:
            context = {}

    project_name = (
        context.get("project_name")
        or os.environ.get("AYON_PROJECT_NAME")
        or os.environ.get("AVALON_PROJECT")
        or ""
    )
    return str(project_name).strip()


def _iter_ayon_project_names():
    names = set()
    current = _current_project_name()
    if current:
        names.add(current)

    if ayon_api is None:
        return sorted(names)

    get_project_names = getattr(ayon_api, "get_project_names", None)
    if callable(get_project_names):
        project_name_results = None
        for kwargs in ({}, {"active": None}, {"active": True}, {"active": False}):
            try:
                project_name_results = get_project_names(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                project_name_results = None
                break

        if project_name_results is not None:
            try:
                for name in (project_name_results or []):
                    if name:
                        names.add(str(name).strip())
            except Exception:
                pass

    get_projects = getattr(ayon_api, "get_projects", None)
    if callable(get_projects):
        project_iter = None
        for kwargs in (
            {},
            {"active": None},
            {"active": True},
            {"active": False},
            {"fields": ["name"]},
            {"fields": {"name"}},
            {"active": None, "fields": ["name"]},
            {"active": None, "fields": {"name"}},
        ):
            try:
                project_iter = get_projects(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                project_iter = None
                break

        if project_iter is not None:
            try:
                for project in project_iter:
                    if isinstance(project, str):
                        if project.strip():
                            names.add(project.strip())
                        continue
                    if not isinstance(project, dict):
                        continue
                    name = (
                        project.get("name")
                        or project.get("project_name")
                        or project.get("projectName")
                        or project.get("project")
                        or ""
                    )
                    if name:
                        names.add(str(name).strip())
            except Exception:
                pass

    return sorted(name for name in names if name)


def _extract_path_strings(value):
    output = []
    if isinstance(value, str):
        if value.strip():
            output.append(value.strip())
        return output

    if isinstance(value, dict):
        for item in value.values():
            output.extend(_extract_path_strings(item))
        return output

    if isinstance(value, (list, tuple, set)):
        for item in value:
            output.extend(_extract_path_strings(item))
        return output

    return output


def _project_root_env_paths():
    paths = []
    for key, value in os.environ.items():
        if not value:
            continue
        if key == "AYON_PROJECT_ROOT" or key.startswith("AYON_PROJECT_ROOT_"):
            paths.append(value)
    return _extract_path_strings(paths)


def _project_root_paths_for_project(project_name):
    if ayon_api is None:
        return []
    get_roots_for_site = getattr(ayon_api, "get_project_roots_for_site", None)
    if not callable(get_roots_for_site):
        return []

    roots_payload = None
    try:
        roots_payload = get_roots_for_site(project_name=project_name)
    except TypeError:
        try:
            roots_payload = get_roots_for_site(project_name)
        except Exception:
            roots_payload = None
    except Exception:
        roots_payload = None

    return _extract_path_strings(roots_payload)


def _candidate_project_bases_for_name(
    project_name,
    raw_root_paths,
    current_project_root,
):
    candidates = []
    seen = set()
    project_key = str(project_name or "").strip().lower()

    def _add(path):
        if not path:
            return
        normalized = _normalize(path)
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    for raw_path in raw_root_paths:
        base = _normalize(raw_path)
        _add(base)

        # If root path points to shared storage (e.g. /projects), project usually
        # lives under root/<project_name>.
        if project_key and os.path.basename(base).strip().lower() != project_key:
            _add(os.path.join(base, project_name))

        # If root path points to a specific project, try sibling projects.
        parent = os.path.dirname(base)
        if parent and parent != base:
            _add(os.path.join(parent, project_name))

    current_root = _normalize(current_project_root)
    current_parent = os.path.dirname(current_root)
    if current_parent and current_parent != current_root:
        _add(os.path.join(current_parent, project_name))

    return candidates


def _discover_all_ayon_project_bases(default_base):
    records = []
    seen = set()
    discovered_project_names = set(_iter_ayon_project_names())
    env_root_paths = _project_root_env_paths()
    current_project_root = _project_root()

    def _add(project_name, base_path):
        if not base_path:
            return
        if project_name:
            discovered_project_names.add(str(project_name).strip())
        normalized = _normalize(base_path)
        key = normalized.lower()
        if key in seen:
            return
        if not os.path.isdir(normalized):
            return
        seen.add(key)
        records.append({
            "project_name": project_name or os.path.basename(normalized) or "project",
            "project_base": normalized,
        })

    for project_name in _iter_ayon_project_names():
        raw_root_paths = []
        raw_root_paths.extend(_project_root_paths_for_project(project_name))
        raw_root_paths.extend(env_root_paths)

        for candidate in _candidate_project_bases_for_name(
            project_name,
            raw_root_paths,
            current_project_root=current_project_root,
        ):
            _add(project_name, candidate)

    fallback_project = _current_project_name() or "current_project"
    _add(fallback_project, default_base)

    records.sort(key=lambda item: (item["project_name"].lower(), item["project_base"]))
    return records, sorted(name for name in discovered_project_names if name)


def _discover_project_cache_roots(project_base):
    roots = []
    seen = set()
    project_base = _normalize(project_base)
    base_depth = project_base.rstrip(os.sep).count(os.sep)

    def _add(path):
        normalized = _normalize(path)
        if normalized in seen:
            return
        if os.path.isdir(normalized):
            seen.add(normalized)
            roots.append(normalized)

    _add(os.path.join(project_base, "cache"))

    for root, dirs, _files in os.walk(project_base, topdown=True):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth > _MAX_HOUDINI_DISCOVERY_DEPTH:
            dirs[:] = []
            continue

        dirs[:] = [
            name for name in dirs
            if not name.startswith(".") and name != "__pycache__"
        ]

        if os.path.basename(root).strip().lower() == "houdini":
            for task_dir in list(dirs):
                _add(os.path.join(root, task_dir, "cache"))
            dirs[:] = []

    roots.sort(key=lambda path: os.path.relpath(path, project_base).replace("\\", "/"))
    return roots


def _cache_root_prefix(project_base, cache_root):
    rel = os.path.relpath(cache_root, project_base).replace("\\", "/")
    if rel.lower().endswith("/cache"):
        rel = rel[:-6]
    return rel.strip("/") or "cache"


def _discover_all_projects_cache_roots(default_base, default_cache_root):
    cache_root_records = []
    seen_cache_roots = set()
    project_records, all_project_names = _discover_all_ayon_project_bases(default_base)

    for project_record in project_records:
        project_name = project_record["project_name"]
        project_base = project_record["project_base"]
        cache_roots = _discover_project_cache_roots(project_base)

        for cache_root in cache_roots:
            normalized_cache = _normalize(cache_root)
            key = normalized_cache.lower()
            if key in seen_cache_roots:
                continue
            seen_cache_roots.add(key)

            relative = _cache_root_prefix(project_base, normalized_cache)
            display_prefix = "{}/{}".format(project_name, relative) if relative else project_name
            cache_root_records.append({
                "project_name": project_name,
                "project_base": project_base,
                "cache_root": normalized_cache,
                "display_prefix": display_prefix,
                "relative_prefix": relative,
            })

    if os.path.isdir(default_cache_root):
        normalized_default_cache = _normalize(default_cache_root)
        key = normalized_default_cache.lower()
        if key not in seen_cache_roots:
            fallback_project = _current_project_name() or "current_project"
            project_base = _normalize(default_base)
            relative = _cache_root_prefix(project_base, normalized_default_cache)
            display_prefix = "{}/{}".format(fallback_project, relative) if relative else fallback_project
            cache_root_records.append({
                "project_name": fallback_project,
                "project_base": project_base,
                "cache_root": normalized_default_cache,
                "display_prefix": display_prefix,
                "relative_prefix": relative,
            })
            seen_cache_roots.add(key)

    cache_root_records.sort(
        key=lambda item: (
            item["project_name"].lower(),
            item["display_prefix"].lower(),
            item["cache_root"],
        )
    )
    return cache_root_records, all_project_names


def _combined_disk_usage(paths):
    total = 0
    used = 0
    free = 0
    seen_devices = set()

    for path in paths:
        if not os.path.isdir(path):
            continue
        try:
            device = os.stat(path).st_dev
        except Exception:
            device = None

        if device is not None and device in seen_devices:
            continue

        try:
            p_total, p_used, p_free = shutil.disk_usage(path)
        except Exception:
            continue

        total += int(p_total)
        used += int(p_used)
        free += int(p_free)
        if device is not None:
            seen_devices.add(device)

    return int(total), int(used), int(free)


def _human_size(num_bytes):
    size = float(max(0, int(num_bytes)))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:,.2f} {unit}"
        size /= 1024.0
    return f"{size:,.2f} PB"


def _dir_size(path):
    total = 0
    stack = [path]
    scanned_entries = 0

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    scanned_entries += 1
                    if scanned_entries % 300 == 0:
                        _pump_qt_events()
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue

    return total


def _scan_main_folder(path):
    versions = []
    other_bytes = 0

    try:
        children = sorted(os.scandir(path), key=lambda entry: entry.name.lower())
    except OSError:
        return versions, 0

    for child in children:
        _pump_qt_events()
        try:
            if child.is_symlink():
                continue

            if child.is_dir(follow_symlinks=False):
                if re.match(r"^v\d+$", child.name, flags=re.IGNORECASE):
                    versions.append({
                        "name": child.name,
                        "path": _normalize(child.path),
                        "size": _dir_size(child.path),
                    })
                else:
                    other_bytes += _dir_size(child.path)

            elif child.is_file(follow_symlinks=False):
                other_bytes += child.stat(follow_symlinks=False).st_size
        except OSError:
            continue

    def _version_key(item):
        raw = item["name"].strip().lower().lstrip("v")
        try:
            return int(raw)
        except ValueError:
            return 0

    versions.sort(key=_version_key)
    return versions, int(other_bytes)


def _top_level_file_bytes(cache_root):
    total = 0
    try:
        with os.scandir(cache_root) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return int(total)


def _child_cache_entries(
    cache_root,
    display_prefix=None,
    project_name=None,
    full_prefix=None,
):
    entries = []
    try:
        names = sorted(os.listdir(cache_root))
    except OSError:
        return entries

    for name in names:
        _pump_qt_events()
        path = os.path.join(cache_root, name)
        if not os.path.isdir(path):
            continue

        versions, other_bytes = _scan_main_folder(path)
        version_total = sum(int(v["size"]) for v in versions)
        main_size = int(other_bytes + version_total)
        if display_prefix:
            entry_name = "{}/{}".format(display_prefix, name)
        else:
            entry_name = name
        if full_prefix:
            full_name = "{}/{}".format(full_prefix, name)
        else:
            full_name = entry_name

        entries.append({
            "name": entry_name,
            "full_name": full_name,
            "project_name": project_name or "",
            "path": _normalize(path),
            "size": main_size,
            "other_size": int(other_bytes),
            "versions": versions,
        })

    entries.sort(key=lambda item: item["size"], reverse=True)
    return entries


def build_report(cache_root, include_project=False, project_base=None):
    cache_root = _normalize(cache_root)
    _ = include_project  # Kept for backwards compatibility in external calls.
    project_base = _normalize(project_base or _project_root())

    cache_roots = [cache_root] if os.path.isdir(cache_root) else []
    scope_roots = list(cache_roots)
    current_project_name = _current_project_name()
    projects_with_cache = [current_project_name] if cache_roots and current_project_name else []
    project_names = [current_project_name] if current_project_name else []
    entries = _child_cache_entries(cache_root)
    top_level_bytes = _top_level_file_bytes(cache_root)
    scan_root = cache_root

    entries.sort(key=lambda item: item["size"], reverse=True)
    for index, item in enumerate(entries):
        item["color"] = _SWATCH_COLORS[index % len(_SWATCH_COLORS)]

    cache_size = int(top_level_bytes + sum(item["size"] for item in entries))
    if os.path.isdir(scan_root):
        disk_total, disk_used, disk_free = shutil.disk_usage(scan_root)
    else:
        disk_total, disk_used, disk_free = (0, 0, 0)

    return {
        "cache_root": cache_root,
        "scan_root": scan_root,
        "include_project": False,
        "cache_roots": cache_roots,
        "cache_roots_count": len(cache_roots),
        "scope_roots": scope_roots,
        "scope_roots_count": len(scope_roots),
        "project_names": project_names,
        "project_count": len(project_names),
        "projects_with_cache_count": len(projects_with_cache),
        "cache_size": cache_size,
        "top_level_bytes": top_level_bytes,
        "disk_total": int(disk_total),
        "disk_used": int(disk_used),
        "disk_free": int(disk_free),
        "entries": entries,
    }


def _ascii_bar(ratio, width=30):
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = int(round(ratio * width))
    return "#" * filled + "-" * (width - filled)


def _report_summary(report):
    total = float(report["disk_total"] or 1)
    used_ratio = report["disk_used"] / total
    free_ratio = report["disk_free"] / total
    cache_ratio = report["cache_size"] / total

    return "\n".join(
        [
            f"Cache Root: {report['cache_root']}",
            "",
            f"Cache Used: {_human_size(report['cache_size'])}",
            f"Disk Free: {_human_size(report['disk_free'])} / {_human_size(report['disk_total'])}",
            "",
            f"Disk Used  [{_ascii_bar(used_ratio)}] {used_ratio * 100:5.1f}%",
            f"Disk Free  [{_ascii_bar(free_ratio)}] {free_ratio * 100:5.1f}%",
            f"Cache Part [{_ascii_bar(cache_ratio)}] {cache_ratio * 100:5.1f}% of disk",
        ]
    )


def _report_details(report):
    lines = ["Top cache folders in this cache root:", ""]

    if not report["entries"]:
        lines.append("(No cache subfolders found)")
    else:
        for index, item in enumerate(report["entries"], start=1):
            lines.append(f"{index:02d}. {item['name']}  |  {_human_size(item['size'])}")
            for version in item.get("versions") or []:
                lines.append(f"    - {version['name']}  |  {_human_size(version['size'])}")

    return "\n".join(lines)


def _delete_confirmation_code():
    return f"{random.randint(100000, 999999)}"


def _path_has_protected_dir(path, target_root):
    rel = os.path.relpath(path, target_root)
    if rel in (".", ""):
        return False
    parts = [part.strip().lower() for part in rel.split(os.sep)]
    return any(part in _PROTECTED_DIRS for part in parts if part)


def _is_protected_source_file(path):
    return os.path.basename(path).strip().lower() in _PROTECTED_SOURCE_FILES


def _purge_cache_payload(target_root):
    removed_files = 0
    removed_dirs = 0
    errors = []

    for root, dirs, files in os.walk(target_root, topdown=False):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            if _path_has_protected_dir(file_path, target_root):
                continue
            if _is_protected_source_file(file_path):
                continue
            try:
                os.remove(file_path)
                removed_files += 1
            except Exception as exc:
                errors.append(f"{file_path} ({exc})")

        for dir_name in dirs:
            if dir_name.strip().lower() in _PROTECTED_DIRS:
                continue
            dir_path = os.path.join(root, dir_name)
            if _path_has_protected_dir(dir_path, target_root):
                continue
            try:
                if os.path.isdir(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    removed_dirs += 1
            except Exception as exc:
                errors.append(f"{dir_path} ({exc})")

    return removed_files, removed_dirs, errors


def _safe_delete(cache_root, targets):
    if isinstance(cache_root, (list, tuple, set)):
        scope_roots = [
            _normalize(os.path.realpath(path))
            for path in cache_root
            if isinstance(path, str) and os.path.isdir(path)
        ]
    else:
        scope_roots = [_normalize(os.path.realpath(cache_root))]

    deleted = []
    failed = []

    for item in targets:
        path = _normalize(os.path.realpath(item["path"]))

        in_scope = False
        for root_real in scope_roots:
            try:
                if os.path.commonpath([root_real, path]) == root_real:
                    in_scope = True
                    break
            except ValueError:
                continue
        if not in_scope:
            failed.append(f"{item['label']} (outside cache scope)")
            continue

        if os.path.basename(path).strip().lower() in _PROTECTED_DIRS:
            failed.append(f"{item['label']} (protected folder)")
            continue

        if not os.path.exists(path):
            failed.append(f"{item['label']} (not found)")
            continue

        try:
            removed_files, removed_dirs, errors = _purge_cache_payload(path)
            deleted.append(
                f"{item['label']} ({removed_files} files, {removed_dirs} empty dirs)"
            )
            if errors:
                failed.append(f"{item['label']} (partial, {len(errors)} error(s))")
        except Exception as exc:
            failed.append(f"{item['label']} ({exc})")

    return deleted, failed


def _show_basic_manager():
    cache_root = resolve_cache_root()
    if not os.path.isdir(cache_root):
        if hou is not None:
            hou.ui.displayMessage(f"Cache folder not found:\n{cache_root}", title="Cache Manager")
        else:
            print(f"Cache folder not found: {cache_root}")
        return

    report = build_report(cache_root)
    text = _report_summary(report) + "\n\n" + _report_details(report)
    if hou is not None:
        hou.ui.displayMessage(text, title="Cache Manager")
    else:
        print(text)


class UsageDonutWidget(QtWidgets.QWidget if QtWidgets is not None else object):
    def __init__(self, parent=None):
        super(UsageDonutWidget, self).__init__(parent)
        self._segments = []
        self._total = 0
        self.setMinimumSize(240, 240)

    def set_usage(self, segments, total):
        self._segments = list(segments or [])
        self._total = int(total or 0)
        self.update()

    def paintEvent(self, event):
        if QtGui is None:
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)

        painter.fillRect(self.rect(), QtGui.QColor("#0f1116"))

        area = self.rect().adjusted(10, 10, -10, -10)
        side = min(area.width(), area.height())
        if side <= 0:
            return
        circle = QtCore.QRectF(
            area.center().x() - (side / 2.0),
            area.center().y() - (side / 2.0),
            float(side),
            float(side),
        )

        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#1c212c"))
        painter.drawEllipse(circle)

        if self._total > 0 and self._segments:
            start_angle = -90 * 16
            remaining = 360 * 16
            for index, segment in enumerate(self._segments):
                if index == len(self._segments) - 1:
                    span = remaining
                else:
                    ratio = float(segment.get("size", 0)) / float(self._total)
                    span = max(0, int(round(ratio * 360 * 16)))
                    remaining -= span

                painter.setBrush(QtGui.QColor(segment.get("color", "#7f8a9d")))
                painter.drawPie(circle, start_angle, span)
                start_angle += span

        thickness = max(26, int(side * 0.24))
        if (side - (thickness * 2)) > 4:
            inner = circle.adjusted(thickness, thickness, -thickness, -thickness)
            painter.setBrush(QtGui.QColor("#0f1116"))
            painter.drawEllipse(inner)
        else:
            inner = circle

        painter.setPen(QtGui.QColor("#d4d9e3"))
        center_font = painter.font()
        center_font.setBold(True)
        center_font.setPointSize(10)
        painter.setFont(center_font)
        painter.drawText(inner, QtCore.Qt.AlignCenter, _human_size(self._total))


class CacheManagerDialog(QtWidgets.QDialog if QtWidgets is not None else object):
    def __init__(self, parent=None):
        super(CacheManagerDialog, self).__init__(parent)
        self.current_cache_root = resolve_cache_root()
        self.project_root = _project_root()
        self.delete_scope_root = self.current_cache_root
        self.report = None
        self._syncing_tree = False

        self.setWindowTitle("Cache Manager")
        self.setMinimumSize(1180, 680)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)

        self._build_ui()
        self._apply_style()
        QtCore.QTimer.singleShot(0, self.refresh_report)

    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(8)

        self.header_label = QtWidgets.QLabel("Cache Used: 0 B | Total Disk: 0 B")
        self.header_label.setObjectName("headerLabel")

        self.root_label = QtWidgets.QLabel("")
        self.root_label.setObjectName("rootLabel")
        self.root_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.cache_bar = QtWidgets.QProgressBar()
        self.cache_bar.setObjectName("cachePartBar")
        self.cache_bar.setRange(0, 1000)
        self.cache_bar.setTextVisible(False)

        disk_row = QtWidgets.QHBoxLayout()
        disk_row.setSpacing(8)

        self.disk_used_title = QtWidgets.QLabel("Disk Used:")
        self.disk_used_title.setObjectName("metricTitle")

        self.disk_bar = QtWidgets.QProgressBar()
        self.disk_bar.setObjectName("diskUsedBar")
        self.disk_bar.setRange(0, 1000)
        self.disk_bar.setTextVisible(False)

        self.disk_used_pct = QtWidgets.QLabel("0.0% (100.0% free)")
        self.disk_used_pct.setObjectName("metricValue")

        disk_row.addWidget(self.disk_used_title)
        disk_row.addWidget(self.disk_bar, 1)
        disk_row.addWidget(self.disk_used_pct)

        self.cache_part_label = QtWidgets.QLabel("Cache Part: 0 B")
        self.cache_part_label.setObjectName("cachePartLabel")

        self.content_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Select", "Folder", "Size"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setItemsExpandable(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.itemChanged.connect(self._on_tree_item_changed)

        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        tree_header.resizeSection(0, 72)
        tree_header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        tree_header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        tree_header.resizeSection(2, 150)

        left_layout.addWidget(self.tree)

        right_panel = QtWidgets.QWidget()
        right_panel.setMinimumWidth(300)
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        self.graph_title = QtWidgets.QLabel("Current Project Cache Usage")
        self.graph_title.setObjectName("graphTitle")
        self.graph_title.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)

        self.donut = UsageDonutWidget()

        self.legend_scroll = QtWidgets.QScrollArea()
        self.legend_scroll.setWidgetResizable(True)
        self.legend_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        self.legend_body = QtWidgets.QWidget()
        self.legend_layout = QtWidgets.QVBoxLayout(self.legend_body)
        self.legend_layout.setContentsMargins(2, 2, 2, 2)
        self.legend_layout.setSpacing(4)
        self.legend_layout.addStretch(1)

        self.legend_scroll.setWidget(self.legend_body)

        right_layout.addWidget(self.graph_title)
        right_layout.addWidget(self.donut, 0, QtCore.Qt.AlignHCenter)
        right_layout.addWidget(self.legend_scroll, 1)

        self.content_splitter.addWidget(left_panel)
        self.content_splitter.addWidget(right_panel)
        self.content_splitter.setStretchFactor(0, 5)
        self.content_splitter.setStretchFactor(1, 2)
        self.content_splitter.setSizes([820, 320])

        bottom_row = QtWidgets.QHBoxLayout()
        bottom_row.setSpacing(10)

        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.setObjectName("refreshButton")
        self.refresh_btn.clicked.connect(self.refresh_report)

        self.delete_btn = QtWidgets.QPushButton("Delete Selected")
        self.delete_btn.setObjectName("deleteButton")
        self.delete_btn.clicked.connect(self._on_delete_selected)

        self.total_label = QtWidgets.QLabel("Total Cache Size: 0 B")
        self.total_label.setObjectName("totalLabel")
        self.total_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        bottom_row.addWidget(self.refresh_btn)
        bottom_row.addWidget(self.delete_btn)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self.total_label)

        main_layout.addWidget(self.header_label)
        main_layout.addWidget(self.root_label)
        main_layout.addWidget(self.cache_bar)
        main_layout.addLayout(disk_row)
        main_layout.addWidget(self.cache_part_label)
        main_layout.addWidget(self.content_splitter, 1)
        main_layout.addLayout(bottom_row)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QDialog {
                background-color: #0f1116;
                color: #dde3ee;
            }

            QLabel {
                color: #cfd6e2;
                font-size: 13px;
            }

            QLabel#headerLabel {
                font-size: 28px;
                font-weight: 700;
                color: #f4f7ff;
                padding-top: 2px;
            }

            QLabel#rootLabel {
                font-size: 12px;
                color: #8090a9;
            }

            QLabel#metricTitle,
            QLabel#cachePartLabel,
            QLabel#graphTitle {
                font-size: 14px;
                font-weight: 600;
                color: #d8deea;
            }

            QLabel#metricValue {
                font-size: 13px;
                color: #9aa8bf;
                min-width: 160px;
            }

            QLabel#totalLabel {
                font-size: 20px;
                font-weight: 700;
                color: #8fb9ff;
            }

            QProgressBar {
                border: 1px solid #2f3642;
                border-radius: 3px;
                background: #181d25;
                min-height: 18px;
            }

            QProgressBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #8e939f,
                    stop: 1 #636a79
                );
            }

            QProgressBar#cachePartBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #66adff,
                    stop: 1 #2f6fc4
                );
            }

            QTreeWidget {
                border: 1px solid #2a303a;
                background: #12161e;
                alternate-background-color: #151a23;
                color: #e4ebf7;
                font-size: 14px;
                outline: none;
            }

            QTreeWidget::item {
                height: 28px;
            }

            QTreeWidget::item:hover {
                background: #1c2430;
            }

            QHeaderView::section {
                background: #1a212c;
                color: #9fb0ca;
                border: 1px solid #2d3643;
                padding: 5px;
                font-size: 12px;
                font-weight: 700;
            }

            QScrollArea {
                background: transparent;
            }

            QPushButton {
                border: 1px solid #323846;
                border-radius: 4px;
                min-height: 34px;
                min-width: 140px;
                font-size: 13px;
                font-weight: 700;
                color: #f0f4fb;
            }

            QPushButton#refreshButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #566072,
                    stop: 1 #3f4756
                );
            }

            QPushButton#deleteButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #c64d4d,
                    stop: 1 #8f2e2e
                );
                border-color: #6e2626;
            }

            QPushButton:pressed {
                padding-top: 1px;
            }
            """
        )

    def _color_icon(self, color_hex):
        pixmap = QtGui.QPixmap(10, 10)
        pixmap.fill(QtGui.QColor(color_hex))
        return QtGui.QIcon(pixmap)

    def _selected_payloads(self):
        payloads = []

        def _collect(item):
            if item.checkState(0) == QtCore.Qt.Checked:
                payload = item.data(0, QtCore.Qt.UserRole)
                if payload:
                    payloads.append(dict(payload))
            for idx in range(item.childCount()):
                _collect(item.child(idx))

        for index in range(self.tree.topLevelItemCount()):
            _collect(self.tree.topLevelItem(index))

        unique = {}
        for payload in payloads:
            real_path = _normalize(os.path.realpath(payload["path"]))
            payload["_real_path"] = real_path
            unique[real_path] = payload

        ordered = sorted(unique.values(), key=lambda row: len(row["_real_path"]))
        collapsed = []
        kept_roots = []

        for payload in ordered:
            path = payload["_real_path"]
            skip = False
            for root in kept_roots:
                try:
                    if os.path.commonpath([root, path]) == root:
                        skip = True
                        break
                except ValueError:
                    continue
            if skip:
                continue
            kept_roots.append(path)
            collapsed.append(payload)

        return collapsed

    def _selected_size(self):
        return int(sum(int(item.get("size") or 0) for item in self._selected_payloads()))

    def _clear_legend(self):
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _update_legend(self, segments, total):
        self._clear_legend()

        if total <= 0 or not segments:
            empty = QtWidgets.QLabel("No cache data")
            empty.setStyleSheet("color: #7c8798; font-size: 12px;")
            self.legend_layout.addWidget(empty)
            self.legend_layout.addStretch(1)
            return

        for segment in segments:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(6)

            dot = QtWidgets.QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(
                "background: {}; border: 1px solid #1f2430; border-radius: 2px;".format(
                    segment["color"]
                )
            )

            name = QtWidgets.QLabel(segment["name"])
            name.setStyleSheet("color: #d5deec; font-size: 12px;")

            pct = (float(segment["size"]) / float(total)) * 100.0
            value = QtWidgets.QLabel("{} ({:.1f}%)".format(_human_size(segment["size"]), pct))
            value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            value.setStyleSheet("color: #9aa9c2; font-size: 12px;")

            row_layout.addWidget(dot)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(value)

            self.legend_layout.addWidget(row)

        self.legend_layout.addStretch(1)

    def _update_total_label(self):
        total_cache = int((self.report or {}).get("cache_size") or 0)
        selected = self._selected_size()
        self.total_label.setText(
            "Total Cache Size: <b>{}</b> | Selected: <b>{}</b>".format(
                _human_size(total_cache), _human_size(selected)
            )
        )

    def _on_tree_item_changed(self, item, column):
        if self._syncing_tree:
            return
        self._update_total_label()

    def _populate_tree(self, report):
        previous_checked = set(
            item["_real_path"] for item in self._selected_payloads()
        )

        self._syncing_tree = True
        try:
            self.tree.clear()

            def _create_cache_item(entry, parent_widget):
                parent_item = QtWidgets.QTreeWidgetItem(parent_widget)
                parent_flags = parent_item.flags()
                flag_enabled = _qt_item_flag("ItemIsEnabled")
                flag_checkable = _qt_item_flag("ItemIsUserCheckable")
                flag_tristate = _qt_item_flag("ItemIsAutoTristate") or _qt_item_flag("ItemIsTristate")
                if flag_enabled is not None:
                    parent_flags |= flag_enabled
                if flag_checkable is not None:
                    parent_flags |= flag_checkable
                if flag_tristate is not None:
                    parent_flags |= flag_tristate
                parent_item.setFlags(parent_flags)
                parent_item.setCheckState(0, QtCore.Qt.Unchecked)
                parent_item.setText(1, entry["name"])
                parent_item.setText(2, _human_size(entry["size"]))
                parent_item.setTextAlignment(2, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                parent_item.setIcon(1, self._color_icon(entry.get("color", "#8b98af")))

                parent_payload = {
                    "name": entry["name"],
                    "label": entry.get("full_name") or entry["name"],
                    "path": entry["path"],
                    "size": int(entry["size"]),
                    "kind": "main",
                }
                parent_item.setData(0, QtCore.Qt.UserRole, parent_payload)

                parent_real = _normalize(os.path.realpath(entry["path"]))
                if parent_real in previous_checked:
                    parent_item.setCheckState(0, QtCore.Qt.Checked)

                for version in entry.get("versions") or []:
                    child_item = QtWidgets.QTreeWidgetItem(parent_item)
                    child_flags = child_item.flags()
                    if flag_enabled is not None:
                        child_flags |= flag_enabled
                    if flag_checkable is not None:
                        child_flags |= flag_checkable
                    child_item.setFlags(child_flags)
                    child_item.setCheckState(0, QtCore.Qt.Unchecked)
                    child_item.setText(1, version["name"])
                    child_item.setText(2, _human_size(version["size"]))
                    child_item.setTextAlignment(2, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                    child_item.setIcon(1, self._color_icon(entry.get("color", "#8b98af")))

                    child_payload = {
                        "name": version["name"],
                        "label": "{}/{}".format(
                            entry.get("full_name") or entry["name"],
                            version["name"],
                        ),
                        "path": version["path"],
                        "size": int(version["size"]),
                        "kind": "version",
                    }
                    child_item.setData(0, QtCore.Qt.UserRole, child_payload)

                    child_real = _normalize(os.path.realpath(version["path"]))
                    if child_real in previous_checked:
                        child_item.setCheckState(0, QtCore.Qt.Checked)

                if parent_item.childCount() > 0:
                    parent_item.setExpanded(True)
                return parent_item

            entries = report.get("entries") or []
            for entry in entries:
                _create_cache_item(entry, self.tree)

        finally:
            self._syncing_tree = False

    def _update_chart(self, report):
        entries = sorted(report.get("entries") or [], key=lambda item: item.get("size", 0), reverse=True)

        total = int(sum(int(item.get("size") or 0) for item in entries))
        if total <= 0:
            self.donut.set_usage([], 0)
            self._update_legend([], 0)
            return

        max_segments = 8
        segments = []
        remaining = 0

        for index, entry in enumerate(entries):
            if index < max_segments:
                segment_name = entry.get("name")
                segments.append({
                    "name": segment_name or entry.get("name") or "cache",
                    "size": int(entry["size"]),
                    "color": entry.get("color", "#8493aa"),
                })
            else:
                remaining += int(entry["size"])

        if remaining > 0:
            segments.append({"name": "Others", "size": remaining, "color": "#6f7683"})

        self.donut.set_usage(segments, total)
        self._update_legend(segments, total)

    def _apply_report(self, report):
        active_cache_root = report.get("scan_root") or self.current_cache_root
        self.report = report
        self.delete_scope_root = active_cache_root
        self.graph_title.setText("Current Project Cache Usage")

        total = float(report.get("disk_total") or 1)
        cache_ratio = max(0.0, min(1.0, float(report.get("cache_size") or 0) / total))
        used_ratio = max(0.0, min(1.0, float(report.get("disk_used") or 0) / total))
        free_ratio = max(0.0, min(1.0, float(report.get("disk_free") or 0) / total))

        self.root_label.setText(
            "Current Cache Root: {}".format(report.get("cache_root", self.current_cache_root))
        )
        self.header_label.setText(
            "Cache Used: <b>{}</b> | Total Disk: <b>{}</b>".format(
                _human_size(report.get("cache_size") or 0),
                _human_size(report.get("disk_total") or 0),
            )
        )
        self.cache_part_label.setText("Cache Part: {}".format(_human_size(report.get("cache_size") or 0)))
        self.disk_used_pct.setText(f"{used_ratio * 100:.1f}% ({free_ratio * 100:.1f}% free)")
        self.cache_bar.setValue(int(round(cache_ratio * 1000)))
        self.disk_bar.setValue(int(round(used_ratio * 1000)))

        self._populate_tree(report)
        self._update_chart(report)
        self._update_total_label()

    def _empty_report(self, active_cache_root):
        report = {
            "cache_root": active_cache_root,
            "scan_root": active_cache_root,
            "include_project": False,
            "cache_roots": [],
            "cache_roots_count": 0,
            "scope_roots": [],
            "scope_roots_count": 0,
            "project_names": [],
            "project_count": 0,
            "projects_with_cache_count": 0,
            "cache_size": 0,
            "disk_total": 0,
            "disk_used": 0,
            "disk_free": 0,
            "entries": [],
        }
        self._apply_report(report)
        self.root_label.setText("Current Cache Root: {} (not found)".format(active_cache_root))

    def refresh_report(self):
        active_cache_root = self.current_cache_root

        self.refresh_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.tree.setEnabled(False)
        self.header_label.setText("Scanning cache data...")
        self.root_label.setText("Scanning current cache root. Please wait...")
        self.cache_part_label.setText("Cache Part: calculating...")
        self.disk_used_pct.setText("Scanning...")
        self.cache_bar.setRange(0, 0)
        self.disk_bar.setRange(0, 0)
        _pump_qt_events()

        try:
            if not os.path.isdir(active_cache_root):
                self._empty_report(active_cache_root)
                return

            report = build_report(
                self.current_cache_root,
                include_project=False,
                project_base=self.project_root,
            )

            self.cache_bar.setRange(0, 1000)
            self.disk_bar.setRange(0, 1000)
            self._apply_report(report)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Cache Manager",
                "Failed to scan current project cache:\n{}".format(exc),
            )
            self._empty_report(active_cache_root)
        finally:
            self.refresh_btn.setEnabled(True)
            self.delete_btn.setEnabled(True)
            self.tree.setEnabled(True)
            self.cache_bar.setRange(0, 1000)
            self.disk_bar.setRange(0, 1000)

    def _confirm_delete(self, targets):
        code = _delete_confirmation_code()
        names = [f"- {item['label']}" for item in targets]
        if len(names) > 12:
            names = names[:12] + [f"... (+{len(targets) - 12} more)"]

        typed, ok = QtWidgets.QInputDialog.getText(
            self,
            "Confirm Cache Delete",
            "Type this confirmation code to purge selected cache data"
            "\n(keeps source.hip* and review folders):"
            f"\n\n{code}\n\n"
            "Selected:\n"
            + "\n".join(names),
            QtWidgets.QLineEdit.Normal,
            "",
        )

        if not ok:
            return False

        return str(typed).strip() == code

    def _on_delete_selected(self):
        targets = self._selected_payloads()
        if not targets:
            QtWidgets.QMessageBox.information(
                self,
                "Cache Manager",
                "Select at least one folder or version.",
            )
            return

        if not self._confirm_delete(targets):
            QtWidgets.QMessageBox.information(
                self,
                "Cache Manager",
                "Deletion cancelled. Confirmation code did not match.",
            )
            return

        deleted, failed = _safe_delete(self.delete_scope_root, targets)

        lines = []
        if deleted:
            lines.append("Deleted:")
            lines.extend(f"- {name}" for name in deleted)

        if failed:
            if lines:
                lines.append("")
            lines.append("Failed:")
            lines.extend(f"- {name}" for name in failed)

        if not lines:
            lines.append("No cache data was deleted.")

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Cache Manager")
        box.setText("\n".join(lines))
        box.setIcon(
            QtWidgets.QMessageBox.Warning if failed else QtWidgets.QMessageBox.Information
        )
        box.exec_()

        self.refresh_report()


def show_cache_manager(kwargs=None):
    if hou is None:
        raise RuntimeError("This function requires Houdini's hou module.")

    if QtWidgets is None:
        _show_basic_manager()
        return

    existing = getattr(hou.session, "ayon_cache_manager_tool", None)
    if existing is not None:
        try:
            existing.close()
            existing.deleteLater()
        except Exception:
            pass

    parent = hou.qt.mainWindow() if hasattr(hou, "qt") else None
    dialog = CacheManagerDialog(parent=parent)
    hou.session.ayon_cache_manager_tool = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def print_cache_report():
    cache_root = resolve_cache_root()
    if not os.path.isdir(cache_root):
        print(f"Cache folder not found: {cache_root}")
        return

    report = build_report(cache_root)
    print(_report_summary(report))
    print()
    print(_report_details(report))


if __name__ == "__main__":
    print_cache_report()
