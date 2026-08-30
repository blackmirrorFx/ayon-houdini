"""Immutable, UI-independent APKG records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


DEPENDENCY_MODES = frozenset({
    "inherit", "context", "optional", "overlay", "replace", "disabled"
})
LOAD_STRATEGIES = frozenset({
    "sublayer", "reference", "payload", "value_clips", "sop_import",
    "metadata", "none",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class PackageDependency:
    package_uid: str
    version_id: str = ""
    product_id: str = ""
    mode: str = "inherit"
    required: bool = True
    publish_through: bool = True
    order: int = 0
    purpose: str = ""

    def __post_init__(self):
        if not self.package_uid:
            raise ValueError("APKG dependency requires package_uid")
        if self.mode not in DEPENDENCY_MODES:
            raise ValueError("Unsupported APKG dependency mode: {}".format(
                self.mode
            ))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]):
        mode = _text(value.get("mode")) or "inherit"
        publish_through = value.get("publishThrough")
        if publish_through is None:
            publish_through = mode not in {"context", "disabled"}
        return cls(
            package_uid=_text(
                value.get("packageUid") or value.get("package_uid")
            ),
            version_id=_text(
                value.get("versionId") or value.get("version_id")
            ),
            product_id=_text(
                value.get("productId") or value.get("product_id")
            ),
            mode=mode,
            required=bool(value.get("required", True)),
            publish_through=bool(publish_through),
            order=int(value.get("order") or 0),
            purpose=_text(value.get("purpose")),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "packageUid": self.package_uid,
            "versionId": self.version_id,
            "productId": self.product_id,
            "mode": self.mode,
            "required": self.required,
            "publishThrough": self.publish_through,
            "order": self.order,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class PackageResource:
    role: str
    representation_id: str = ""
    representation_name: str = ""
    extension: str = ""
    resolver_uri: str = ""
    load_strategy: str = "reference"
    purpose: str = "render"
    default_state: str = "loaded"
    paths: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.role:
            raise ValueError("APKG resource requires a semantic role")
        if self.load_strategy not in LOAD_STRATEGIES:
            raise ValueError("Unsupported APKG load strategy: {}".format(
                self.load_strategy
            ))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]):
        if not isinstance(value, Mapping):
            raise TypeError("APKG resource must be a mapping")
        return cls(
            role=_text(value.get("role")),
            representation_id=_text(
                value.get("representationId")
                or value.get("representation_id")
                or value.get("id")
            ),
            representation_name=_text(
                value.get("representationName")
                or value.get("representation_name")
                or value.get("name")
            ),
            extension=_text(value.get("extension") or value.get("ext")),
            resolver_uri=_text(
                value.get("resolverUri") or value.get("resolver_uri")
            ),
            load_strategy=_text(
                value.get("loadStrategy") or value.get("load_strategy")
            ) or "reference",
            purpose=_text(value.get("purpose")) or "render",
            default_state=_text(
                value.get("defaultState") or value.get("default_state")
            ) or "loaded",
            paths=tuple(_text(path) for path in value.get("paths") or []),
            tags=tuple(_text(tag) for tag in value.get("tags") or []),
        )

    def to_mapping(self) -> dict[str, Any]:
        output = {
            "role": self.role,
            "representationId": self.representation_id,
            "representationName": self.representation_name,
            "extension": self.extension,
            "resolverUri": self.resolver_uri,
            "loadStrategy": self.load_strategy,
            "purpose": self.purpose,
            "defaultState": self.default_state,
            "tags": list(self.tags),
        }
        # Paths are useful in exported/offline manifests. AYON representation
        # ids remain authoritative and are preferred by live clients.
        if self.paths:
            output["paths"] = list(self.paths)
        return output


@dataclass(frozen=True)
class PackageRecord:
    package_uid: str
    project_name: str
    folder_id: str
    product_id: str
    version_id: str
    version: int
    status: str
    department: str
    role: str
    slot: str
    contribution_type: str = ""
    product_name: str = ""
    task_id: str = ""
    author: str = ""
    entrypoint_representation_id: str = ""
    entrypoint_path: str = ""
    dependencies: tuple[PackageDependency, ...] = ()
    resources: tuple[PackageResource, ...] = ()
    exports: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def contribution_key(self) -> str:
        """Stable key used to group independently versioned contributions."""
        return self.product_id or "{}:{}:{}".format(
            self.department, self.role, self.slot
        )

    @classmethod
    def from_entities(
        cls,
        project_name: str,
        product: Mapping[str, Any],
        version: Mapping[str, Any],
        representations: Iterable[Mapping[str, Any]] = (),
    ):
        data = version.get("data") or {}
        summary = data.get("bmfxApkg") or {}
        resources = []
        entrypoint_id = _text(summary.get("entrypointRepresentationId"))
        entrypoint_path = ""
        for representation in representations:
            representation_data = representation.get("data") or {}
            role = _text(representation_data.get("bmfxApkgRole"))
            name = _text(representation.get("name"))
            extension = _text(
                representation.get("extension")
                or (representation.get("context") or {}).get("ext")
            )
            if not role:
                role = "entrypoint" if name == "usd" else "resource.{}".format(
                    name or extension or "unknown"
                )
            paths = []
            for file_info in representation.get("files") or []:
                if isinstance(file_info, Mapping) and file_info.get("path"):
                    paths.append(_text(file_info["path"]))
                elif isinstance(file_info, str):
                    paths.append(file_info)
            resource = PackageResource(
                role=role,
                representation_id=_text(representation.get("id")),
                representation_name=name,
                extension=extension,
                load_strategy=_text(
                    representation_data.get("bmfxLoadStrategy")
                ) or ("payload" if name == "usd" else "reference"),
                purpose=_text(representation_data.get("bmfxPurpose"))
                or "render",
                paths=tuple(paths),
                tags=tuple(representation.get("tags") or ()),
            )
            resources.append(resource)
            is_entrypoint = (
                resource.representation_id == entrypoint_id
                or not entrypoint_id and name == "usd"
            )
            if is_entrypoint and not entrypoint_id:
                entrypoint_id = resource.representation_id
            if is_entrypoint and paths:
                entrypoint_path = paths[0]

        represented_roles = {resource.role for resource in resources}
        for value in summary.get("resources") or []:
            resource = PackageResource.from_mapping(value)
            if resource.role not in represented_roles:
                resources.append(resource)
                represented_roles.add(resource.role)

        interface = summary.get("interface") or {}
        dependencies = tuple(
            PackageDependency.from_mapping(item)
            for item in summary.get("dependencies") or []
            if item.get("packageUid") or item.get("package_uid")
        )
        return cls(
            package_uid=_text(data.get("bmfxPackageId")),
            project_name=project_name,
            folder_id=_text(product.get("folderId")),
            product_id=_text(product.get("id")),
            version_id=_text(version.get("id")),
            version=int(version.get("version") or 0),
            status=_text(version.get("status")).lower(),
            department=_text(summary.get("department")).lower(),
            role=_text(summary.get("role")),
            slot=_text(summary.get("slot")),
            contribution_type=_text(
                summary.get("contributionType") or summary.get("effectType")
            ),
            product_name=_text(product.get("name")),
            task_id=_text(version.get("taskId")),
            author=_text(version.get("author")),
            entrypoint_representation_id=entrypoint_id,
            entrypoint_path=entrypoint_path,
            dependencies=dependencies,
            resources=tuple(resources),
            exports=tuple(interface.get("exports") or ()),
            requires=tuple(interface.get("requires") or ()),
            capabilities=tuple(interface.get("capabilities") or ()),
            raw={"product": dict(product), "version": dict(version)},
        )
