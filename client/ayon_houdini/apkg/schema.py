"""BMFX APKG manifest construction, migration and validation."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable, Mapping

from .models import PackageDependency, PackageResource


APKG_MANIFEST_SCHEMA = "bmfx.apkg/1.0"
APKG_SUMMARY_SCHEMA = "bmfx.apkg.summary/1.0"
LEGACY_PACKAGE_SCHEMAS = frozenset({"bmfx.package/2.0"})
_FORBIDDEN_TIMING_KEYS = frozenset({
    "frame_range", "frameRange", "frameStart", "frameEnd", "handles",
})


class ManifestValidationError(ValueError):
    """Raised when an APKG manifest violates the interchange contract."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _dependency_mapping(value):
    if isinstance(value, PackageDependency):
        return value.to_mapping()
    return PackageDependency.from_mapping(value).to_mapping()


def _resource_mapping(value, index=None):
    try:
        if isinstance(value, PackageResource):
            return value.to_mapping()
        return PackageResource.from_mapping(value).to_mapping()
    except (TypeError, ValueError) as exc:
        label = "resources" if index is None else "resources[{}]".format(index)
        raise ManifestValidationError(["{}: {}".format(label, exc)]) from exc


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Return a stable fingerprint, excluding the fingerprint itself."""
    value = copy.deepcopy(dict(manifest))
    integrity = value.get("integrity") or {}
    if isinstance(integrity, dict):
        integrity.pop("manifestFingerprint", None)
        if integrity:
            value["integrity"] = integrity
        else:
            value.pop("integrity", None)
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_manifest(
    *,
    package_uid: str,
    project_name: str,
    folder_id: str,
    task_id: str,
    department: str,
    role: str,
    slot: str,
    contribution_type: str = "",
    product_id: str = "",
    version_id: str = "",
    previous_package_uid: str = "",
    entrypoint: Mapping[str, Any] | None = None,
    dependencies: Iterable[PackageDependency | Mapping[str, Any]] = (),
    resources: Iterable[PackageResource | Mapping[str, Any]] = (),
    exports: Iterable[str] = (),
    requires: Iterable[str] = (),
    capabilities: Iterable[str] = (),
    creator: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical APKG manifest.

    Shot timing is deliberately absent. AYON's shot/folder entity remains the
    sole source of truth for frame range, handles, FPS and resolution.
    """
    manifest = {
        "schema": APKG_MANIFEST_SCHEMA,
        "kind": "contribution",
        "packageUid": str(package_uid),
        "context": {
            "project": str(project_name),
            "folderId": str(folder_id),
            "taskId": str(task_id),
            "department": str(department).strip().lower(),
        },
        "source": {
            "productId": str(product_id),
            "versionId": str(version_id),
            "previousPackageUid": str(previous_package_uid),
        },
        "identity": {
            "role": str(role),
            "slot": str(slot),
            "contributionType": str(contribution_type),
        },
        "entrypoint": dict(entrypoint or {}),
        "dependencies": [_dependency_mapping(item) for item in dependencies],
        "resources": [
            _resource_mapping(item, index)
            for index, item in enumerate(resources)
        ],
        "interface": {
            "exports": sorted(set(str(item) for item in exports if item)),
            "requires": sorted(set(str(item) for item in requires if item)),
            "capabilities": sorted(
                set(str(item) for item in capabilities if item)
            ),
        },
        "creator": dict(creator or {}),
        "diagnostics": dict(diagnostics or {}),
    }
    manifest["integrity"] = {
        "manifestFingerprint": manifest_fingerprint(manifest)
    }
    validate_manifest(manifest)
    return manifest


def normalize_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the current schema or adapt a Browser v2 package manifest."""
    manifest = copy.deepcopy(dict(value))
    schema = manifest.get("schema")
    if schema == APKG_MANIFEST_SCHEMA:
        return manifest
    if schema not in LEGACY_PACKAGE_SCHEMAS:
        raise ManifestValidationError(["Unsupported schema: {}".format(schema)])

    # Legacy Browser manifests describe a flat package. Preserve all legacy
    # resources and expose the record as a non-authoritative legacy APKG.
    department = str(manifest.get("task") or "legacy").strip().lower()
    resources = []
    for resource in manifest.get("resources") or []:
        item = dict(resource)
        item.setdefault("role", "resource.{}".format(
            item.get("name") or item.get("extension") or "unknown"
        ))
        item.setdefault("loadStrategy", "reference")
        resources.append(item)
    return {
        "schema": APKG_MANIFEST_SCHEMA,
        "kind": "legacy",
        "packageUid": manifest.get("package_uid") or "",
        "context": {
            "project": manifest.get("project") or "",
            "folderId": "",
            "taskId": "",
            "department": department,
        },
        "source": {"productId": "", "versionId": manifest.get("version_id") or ""},
        "identity": {
            "role": "legacy.{}".format(department),
            "slot": manifest.get("product") or "legacy",
        },
        "entrypoint": {},
        "dependencies": list(manifest.get("dependencies") or []),
        "resources": resources,
        "interface": {"exports": [], "requires": [], "capabilities": []},
        "creator": {},
        "diagnostics": {"legacy": True},
    }


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    errors = []
    if manifest.get("schema") != APKG_MANIFEST_SCHEMA:
        errors.append("schema must be {}".format(APKG_MANIFEST_SCHEMA))
    if not str(manifest.get("packageUid") or "").strip():
        errors.append("packageUid is required")

    for key in _FORBIDDEN_TIMING_KEYS:
        if key in manifest:
            errors.append(
                "{} belongs to the AYON shot entity, not APKG".format(key)
            )

    context = manifest.get("context") or {}
    for key in ("project", "folderId", "taskId", "department"):
        if not str(context.get(key) or "").strip():
            errors.append("context.{} is required".format(key))

    identity = manifest.get("identity") or {}
    for key in ("role", "slot"):
        if not str(identity.get(key) or "").strip():
            errors.append("identity.{} is required".format(key))

    dependency_uids = set()
    for index, value in enumerate(manifest.get("dependencies") or []):
        try:
            dependency = PackageDependency.from_mapping(value)
        except (TypeError, ValueError) as exc:
            errors.append("dependencies[{}]: {}".format(index, exc))
            continue
        if dependency.package_uid == manifest.get("packageUid"):
            errors.append("package cannot depend on itself")
        if dependency.package_uid in dependency_uids:
            errors.append("duplicate dependency {}".format(
                dependency.package_uid
            ))
        dependency_uids.add(dependency.package_uid)

    resource_roles = set()
    for index, value in enumerate(manifest.get("resources") or []):
        try:
            resource = PackageResource.from_mapping(value)
        except (TypeError, ValueError) as exc:
            errors.append("resources[{}]: {}".format(index, exc))
            continue
        if resource.role in resource_roles:
            errors.append("duplicate resource role {}".format(resource.role))
        resource_roles.add(resource.role)

    exports = list((manifest.get("interface") or {}).get("exports") or [])
    for path in exports:
        if not str(path).startswith("/"):
            errors.append("export path must be absolute: {}".format(path))
    requires = list((manifest.get("interface") or {}).get("requires") or [])
    for path in requires:
        if not str(path).startswith("/"):
            errors.append("required path must be absolute: {}".format(path))

    if errors:
        raise ManifestValidationError(errors)


def summary_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, server-queryable version data summary."""
    normalized = normalize_manifest(manifest)
    validate_manifest(normalized)
    context = normalized["context"]
    identity = normalized["identity"]
    return {
        "schema": APKG_SUMMARY_SCHEMA,
        "kind": normalized.get("kind", "contribution"),
        "department": context["department"],
        "role": identity["role"],
        "slot": identity["slot"],
        "contributionType": identity.get("contributionType", ""),
        "entrypointRepresentationId": (
            normalized.get("entrypoint") or {}
        ).get("representationId", ""),
        "dependencies": list(normalized.get("dependencies") or []),
        "resources": list(normalized.get("resources") or []),
        "interface": dict(normalized.get("interface") or {}),
        "manifestFingerprint": manifest_fingerprint(normalized),
    }
