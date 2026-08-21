"""Collect durable Houdini/USD dependency provenance for BMFX publishes.

The collector deliberately returns plain JSON data.  It can therefore be
stored in a farm submission manifest and in AYON version data without
requiring Houdini or USD when the publish is inspected later.
"""

from __future__ import annotations

import os
from pathlib import Path


_USD_EXTENSIONS = {"usd", "usda", "usdc", "usdz"}
_CACHE_EXTENSIONS = {
    "abc", "bgeo", "bgeo.sc", "geo", "vdb", "ass", "ass.gz",
}
_MODEL_EXTENSIONS = {"fbx", "obj", "gltf", "glb", "dae", "ply"}
_TEXTURE_EXTENSIONS = {
    "exr", "tx", "tex", "rat", "png", "jpg", "jpeg", "tif", "tiff",
    "bmp", "tga", "hdr", "pic",
}
_SCENE_EXTENSIONS = {"hip", "hiplc", "hipnc"}
_MULTI_EXTENSIONS = ("bgeo.sc", "ass.gz")


def _extension(path):
    lowered = str(path or "").lower().split("?", 1)[0]
    for extension in _MULTI_EXTENSIONS:
        if lowered.endswith("." + extension):
            return extension
    return os.path.splitext(lowered)[1].lstrip(".")


def dependency_kind(path, product_type="", representation_name=""):
    """Classify a dependency using AYON semantics first, extension second."""
    value = "{} {}".format(product_type or "", representation_name or "").lower()
    extension = _extension(path)
    if "model" in value:
        return "model"
    if "usd" in value or extension in _USD_EXTENSIONS:
        return "usd"
    if any(token in value for token in ("cache", "pointcache", "alembic", "vdb")):
        return "cache"
    if extension in _CACHE_EXTENSIONS:
        return "cache"
    if extension in _MODEL_EXTENSIONS:
        return "model"
    if extension in _TEXTURE_EXTENSIONS:
        return "texture"
    if extension in _SCENE_EXTENSIONS:
        return "scene"
    return "reference"


def _clean_path(path, hou_module=None):
    value = str(path or "").strip()
    if not value or value.startswith("anon:"):
        return ""
    if hou_module is not None:
        try:
            value = hou_module.expandString(value)
        except Exception:
            pass
    # USD identifiers may have resolver arguments after a question mark.
    if not value.startswith(("http://", "https://", "ayon://")):
        value = os.path.normpath(value).replace("\\", "/")
    return value


def _record_key(record):
    return (
        record.get("representation_id") or "",
        record.get("version_id") or "",
        record.get("path") or "",
        record.get("node_path") or "",
    )


def _collect_containers(project_name):
    try:
        from ayon_houdini.api.pipeline import ls
    except Exception:
        try:
            from api.pipeline import ls
        except Exception:
            return []

    containers = []
    try:
        containers = list(ls())
    except Exception:
        return []
    representation_ids = {
        str(container.get("representation") or "")
        for container in containers
        if container.get("representation")
    }
    representations = {}
    versions = {}
    products = {}
    if representation_ids and project_name:
        try:
            import ayon_api
            representations = {
                item["id"]: item
                for item in ayon_api.get_representations(
                    project_name,
                    representation_ids=representation_ids,
                    fields={"id", "name", "versionId", "attrib", "data"},
                )
            }
            version_ids = {
                item.get("versionId") for item in representations.values()
                if item.get("versionId")
            }
            versions = {
                item["id"]: item
                for item in ayon_api.get_versions(
                    project_name,
                    version_ids=version_ids,
                    fields={"id", "version", "productId"},
                )
            }
            product_ids = {
                item.get("productId") for item in versions.values()
                if item.get("productId")
            }
            products = {
                item["id"]: item
                for item in ayon_api.get_products(
                    project_name,
                    product_ids=product_ids,
                    fields={"id", "name", "productType", "folderId"},
                )
            }
        except Exception:
            # Container identity is still useful even if AYON is temporarily
            # unreachable during collection.
            representations = {}

    output = []
    for container in containers:
        representation_id = str(container.get("representation") or "")
        representation = representations.get(representation_id) or {}
        version = versions.get(representation.get("versionId")) or {}
        product = products.get(version.get("productId")) or {}
        attrib = representation.get("attrib") or {}
        path = attrib.get("path") or container.get("path") or ""
        node = container.get("node")
        try:
            node_path = node.path() if node is not None else ""
        except Exception:
            node_path = str(container.get("objectName") or "")
        product_name = product.get("name") or container.get("name") or "AYON asset"
        version_number = version.get("version")
        label = product_name
        if isinstance(version_number, int):
            label = "{} v{:03d}".format(product_name, version_number)
        output.append({
            "label": label,
            "kind": dependency_kind(
                path,
                product.get("productType"),
                representation.get("name"),
            ),
            "source": "AYON container",
            "path": _clean_path(path),
            "node_path": node_path,
            "representation_id": representation_id,
            "version_id": str(representation.get("versionId") or ""),
            "product_name": product_name,
            "product_type": str(product.get("productType") or ""),
            "representation_name": str(representation.get("name") or ""),
        })
    return output


def _collect_usd_stage(stage, hou_module=None):
    if stage is None:
        return []
    paths = set()
    try:
        layers = list(stage.GetUsedLayers())
    except Exception:
        layers = []
    for layer in layers:
        layer_path = _clean_path(getattr(layer, "realPath", ""), hou_module)
        for value in (
            layer_path,
            getattr(layer, "identifier", ""),
        ):
            path = _clean_path(value, hou_module)
            if path:
                paths.add(path)
        try:
            references = layer.GetExternalReferences()
        except Exception:
            references = []
        try:
            references = list(references) + list(
                layer.GetExternalAssetDependencies()
            )
        except Exception:
            references = list(references)
        for value in references:
            path = _clean_path(value, hou_module)
            if (
                path
                and layer_path
                and not os.path.isabs(path)
                and not path.startswith(("http://", "https://", "ayon://"))
            ):
                path = _clean_path(
                    os.path.join(os.path.dirname(layer_path), path), hou_module
                )
            if path:
                paths.add(path)

    # ComputeAllDependencies also discovers payloads and asset-valued
    # attributes (commonly textures) that are not guaranteed to appear in
    # GetUsedLayers. Keep this optional for older Houdini USD builds.
    try:
        from pxr import UsdUtils
        root_layer = stage.GetRootLayer()
        dependency_result = UsdUtils.ComputeAllDependencies(
            root_layer.identifier
        )
    except Exception:
        dependency_result = ()
    for collection in dependency_result:
        if not isinstance(collection, (list, tuple, set)):
            continue
        for value in collection:
            path = _clean_path(
                getattr(value, "realPath", "")
                or getattr(value, "path", "")
                or getattr(value, "identifier", "")
                or value,
                hou_module,
            )
            if path:
                paths.add(path)

    output = []
    for path in sorted(paths):
        # Anonymous/session layers and Houdini operator identifiers are not
        # durable external dependencies.
        if path.startswith(("op:", "opdef:", "memory:")):
            continue
        output.append({
            "label": Path(path.split("?", 1)[0]).name or path,
            "kind": dependency_kind(path),
            "source": "USD stage",
            "path": path,
            "node_path": "",
            "representation_id": "",
            "version_id": "",
            "product_name": "",
            "product_type": "",
            "representation_name": "",
        })
    return output


def _collect_file_references(hou_module):
    if hou_module is None:
        return []
    try:
        references = hou_module.fileReferences()
    except Exception:
        return []
    output = []
    for parm, raw_path in references:
        try:
            tags = parm.parmTemplate().tags() if parm is not None else {}
        except Exception:
            tags = {}
        chooser_mode = str(
            tags.get("filechooser_mode")
            or tags.get("filechooserMode")
            or ""
        ).lower()
        if chooser_mode in {"write", "save", "export"}:
            continue
        path = _clean_path(raw_path, hou_module)
        if not path or not _extension(path):
            continue
        try:
            node_path = parm.node().path() if parm is not None else ""
        except Exception:
            node_path = ""
        output.append({
            "label": Path(path.split("?", 1)[0]).name or path,
            "kind": dependency_kind(path),
            "source": "Houdini file reference",
            "path": path,
            "node_path": node_path,
            "representation_id": "",
            "version_id": "",
            "product_name": "",
            "product_type": "",
            "representation_name": "",
        })
    return output


def collect_scene_dependencies(project_name="", stage=None, hou_module=None):
    """Return deduplicated dependencies and AYON input version payloads."""
    if hou_module is None:
        try:
            import hou as hou_module
        except Exception:
            hou_module = None
    records = []
    records.extend(_collect_containers(project_name))
    records.extend(_collect_usd_stage(stage, hou_module))
    records.extend(_collect_file_references(hou_module))

    deduplicated = []
    seen = set()
    for record in records:
        key = _record_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    deduplicated.sort(key=lambda item: (
        item.get("kind") or "", item.get("label") or "", item.get("path") or ""
    ))
    version_ids = sorted({
        record["version_id"] for record in deduplicated
        if record.get("version_id")
    })
    return {
        "schema_version": 1,
        "records": deduplicated,
        "input_version_ids": version_ids,
        "input_versions": [
            {"version_id": version_id, "data": {}}
            for version_id in version_ids
        ],
    }
