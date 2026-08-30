"""Shared BMFX APKG data model and package-resolution helpers.

This package intentionally has no Houdini or Qt imports.  It is shared by
Houdini nodes, publish plug-ins, BMFX Browser and headless/farm processes.
"""

from .graph import PackageGraph, PackageGraphError
from .models import PackageDependency, PackageRecord, PackageResource
from .naming import apkg_product_name, apkg_version_name, naming_token
from .schema import (
    APKG_MANIFEST_SCHEMA,
    APKG_SUMMARY_SCHEMA,
    ManifestValidationError,
    build_manifest,
    manifest_fingerprint,
    normalize_manifest,
    validate_manifest,
)
from .status import DEFAULT_STATUS_PRIORITY, select_preferred_versions

__all__ = [
    "APKG_MANIFEST_SCHEMA",
    "APKG_SUMMARY_SCHEMA",
    "DEFAULT_STATUS_PRIORITY",
    "ManifestValidationError",
    "PackageDependency",
    "PackageGraph",
    "PackageGraphError",
    "PackageRecord",
    "PackageResource",
    "build_manifest",
    "apkg_product_name",
    "apkg_version_name",
    "manifest_fingerprint",
    "normalize_manifest",
    "naming_token",
    "select_preferred_versions",
    "validate_manifest",
]
