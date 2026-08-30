"""Portable AYON representation path handling for APKG clients."""

from __future__ import annotations

import os
import re
from pathlib import PurePath


_ROOT_TOKEN = re.compile(r"\{root\[([^\]]+)\]\}", re.IGNORECASE)


def project_roots_from_environment():
    prefix = "AYON_PROJECT_ROOT_"
    return {
        key[len(prefix):].lower(): value
        for key, value in os.environ.items()
        if key.upper().startswith(prefix) and value
    }


def normalize_path(path, roots=None):
    if not path:
        return ""
    values = project_roots_from_environment()
    values.update({
        str(key).lower(): str(value)
        for key, value in (roots or {}).items()
        if value
    })

    def replace(match):
        return values.get(match.group(1).lower(), match.group(0))

    resolved = _ROOT_TOKEN.sub(replace, str(path))
    return os.path.normpath(os.path.expandvars(os.path.expanduser(resolved)))


def attach_resolved_paths(representation, roots=None):
    result = dict(representation)
    context = representation.get("context") or {}
    context_path = normalize_path(context.get("path") or "", roots)
    files = representation.get("files") or []
    if isinstance(files, str):
        files = [files]
    paths = []
    for value in files:
        if isinstance(value, dict):
            file_path = value.get("path")
            value = file_path or value.get("name")
            if value and not file_path and context_path:
                value = os.path.join(context_path, str(value))
        if value:
            paths.append(normalize_path(value, roots))
    if not paths and context_path:
        paths.append(context_path)
    result["files"] = [{"path": path} for path in paths]
    return result


def representation_extension(representation):
    context = representation.get("context") or {}
    data = representation.get("data") or {}
    extension = str(
        representation.get("extension")
        or representation.get("ext")
        or context.get("ext")
        or data.get("extension")
        or data.get("ext")
        or ""
    ).lower().lstrip(".")
    if extension:
        return extension
    files = representation.get("files") or []
    if not files:
        return ""
    value = files[0]
    if isinstance(value, dict):
        value = value.get("path") or value.get("name") or ""
    name = PurePath(str(value)).name.lower()
    return "bgeo.sc" if name.endswith(".bgeo.sc") else PurePath(name).suffix.lstrip(".")
