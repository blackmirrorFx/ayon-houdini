"""Batched AYON discovery for shot contribution APKGs."""

from __future__ import annotations

from collections import defaultdict

from .models import PackageRecord
from .paths import attach_resolved_paths


def _connection(connection=None):
    if connection is not None:
        return connection
    import ayon_api
    return ayon_api.get_server_api_connection()


def discover_packages(
    project_name,
    folder_id,
    *,
    connection=None,
    active=True,
):
    """Return all APKG versions in a shot using three batched AYON queries."""
    connection = _connection(connection)
    products = list(connection.get_products(
        project_name,
        folder_ids={folder_id},
        active=active,
        fields={"id", "name", "folderId", "productBaseType", "productType"},
    ))
    if not products:
        return []
    products_by_id = {product["id"]: product for product in products}
    versions = list(connection.get_versions(
        project_name,
        product_ids=set(products_by_id),
        hero=True,
        standard=True,
        active=active,
        fields={
            "id", "version", "productId", "taskId", "status", "author",
            "data", "attrib",
        },
    ))
    apkg_versions = []
    for version in versions:
        data = version.get("data") or {}
        if data.get("bmfxApkg") or data.get("bmfxApkgSchema"):
            apkg_versions.append(version)
    if not apkg_versions:
        return []

    representations = list(connection.get_representations(
        project_name,
        version_ids={version["id"] for version in apkg_versions},
        active=active,
    ))
    roots_query = getattr(connection, "get_project_roots_by_site_id", None)
    try:
        roots = roots_query(project_name) if roots_query else {}
    except Exception:
        roots = {}

    representations_by_version = defaultdict(list)
    for representation in representations:
        representations_by_version[representation.get("versionId")].append(
            attach_resolved_paths(representation, roots)
        )

    records = []
    for version in apkg_versions:
        product = products_by_id.get(version.get("productId"))
        if not product:
            continue
        records.append(PackageRecord.from_entities(
            project_name,
            product,
            version,
            representations_by_version.get(version["id"], ()),
        ))
    return records
