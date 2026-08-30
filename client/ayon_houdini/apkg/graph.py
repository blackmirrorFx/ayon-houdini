"""Dependency and USD interface validation for APKG selections."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import PackageRecord


class PackageGraphError(ValueError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def publish_through_representation_ids(dependencies):
    """Return unique AYON representation IDs for exact APKG input links."""
    output = []
    seen = set()
    for item in dependencies or ():
        representation_id = str(
            item.get("entrypointRepresentationId") or ""
        ).strip()
        if (
            not representation_id
            or not item.get("publishThrough")
            or item.get("mode") in {"context", "disabled"}
            or representation_id in seen
        ):
            continue
        seen.add(representation_id)
        output.append(representation_id)
    return output


class PackageGraph:
    def __init__(self, records: Iterable[PackageRecord]):
        self.records = tuple(records)
        self.by_uid = {record.package_uid: record for record in self.records}
        if len(self.by_uid) != len(self.records):
            raise PackageGraphError(["Package UIDs must be unique"])

    def validate(self) -> None:
        errors = []
        exports = defaultdict(list)
        for record in self.records:
            if not record.package_uid:
                errors.append("{} has no package UID".format(
                    record.product_name or record.version_id
                ))
            for path in record.exports:
                exports[path].append(record.package_uid)
            for dependency in record.dependencies:
                if dependency.required and dependency.mode != "disabled":
                    if dependency.package_uid not in self.by_uid:
                        errors.append("{} requires missing package {}".format(
                            record.package_uid, dependency.package_uid
                        ))
        export_rows = sorted(
            (path.rstrip("/") or "/", owner)
            for path, owners in exports.items() for owner in owners
        )

        dependency_cache = {}

        def transitive_dependencies(package_uid):
            cached = dependency_cache.get(package_uid)
            if cached is not None:
                return cached
            output = set()
            pending = [package_uid]
            while pending:
                current_uid = pending.pop()
                current = self.by_uid.get(current_uid)
                if current is None:
                    continue
                for dependency in current.dependencies:
                    dependency_uid = dependency.package_uid
                    if (
                        dependency.mode != "disabled"
                        and dependency_uid in self.by_uid
                        and dependency_uid not in output
                    ):
                        output.add(dependency_uid)
                        pending.append(dependency_uid)
            dependency_cache[package_uid] = output
            return output

        reported = set()
        for index, (path, owner) in enumerate(export_rows):
            for other_path, other_owner in export_rows[index + 1:]:
                if owner == other_owner:
                    continue
                # Overlays along an exact dependency chain are intentional:
                # Animation may override Layout, CFX may override Animation,
                # and so on. Ownership collisions remain invalid between
                # unrelated/parallel packages.
                if (
                    other_owner in transitive_dependencies(owner)
                    or owner in transitive_dependencies(other_owner)
                ):
                    continue
                collision = (
                    path == other_path
                    or path == "/"
                    or other_path.startswith(path + "/")
                    or other_path == "/"
                    or path.startswith(other_path + "/")
                )
                if collision:
                    key = (path, owner, other_path, other_owner)
                    if key not in reported:
                        reported.add(key)
                        errors.append(
                            "USD export ownership overlaps: {} ({}) and {} ({})"
                            .format(path, owner, other_path, other_owner)
                        )
        try:
            self.topological_order()
        except PackageGraphError as exc:
            errors.extend(exc.errors)
        if errors:
            raise PackageGraphError(errors)

    def topological_order(self) -> tuple[PackageRecord, ...]:
        visiting = set()
        visited = set()
        output = []

        def visit(uid, chain):
            if uid in visited:
                return
            if uid in visiting:
                start = chain.index(uid) if uid in chain else 0
                cycle = chain[start:] + [uid]
                raise PackageGraphError([
                    "Circular APKG dependency: {}".format(" -> ".join(cycle))
                ])
            visiting.add(uid)
            record = self.by_uid[uid]
            dependencies = sorted(
                (
                    item for item in record.dependencies
                    if item.mode != "disabled" and item.package_uid in self.by_uid
                ),
                key=lambda item: (item.order, item.package_uid),
            )
            for dependency in dependencies:
                visit(dependency.package_uid, chain + [uid])
            visiting.remove(uid)
            visited.add(uid)
            output.append(record)

        for package_uid in sorted(self.by_uid):
            visit(package_uid, [])
        return tuple(output)
