"""Pure APKG shot-build planning and department policy.

This module deliberately has no Houdini or AYON imports.  The catalog layer
produces :class:`PackageRecord` values, this module turns the locked selection
into a deterministic payload plan, and the Houdini controller projects that
plan into a LOP network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


SHOT_DEPARTMENTS = (
    "matchmove",
    "layout",
    "setdress",
    "animation",
    "crowd",
    "crowdcfx",
    "cfx",
    "fx",
    "lighting",
)

SHOT_DEPARTMENT_LABELS = {
    "matchmove": "Matchmove",
    "layout": "Layout",
    "setdress": "Setdress",
    "animation": "Animation",
    "crowd": "Crowd",
    "crowdcfx": "Crowdcfx",
    "cfx": "CFX",
    "fx": "FX",
    "lighting": "Lighting",
}

TASK_TYPE_ALIASES = {
    "matchmove": "matchmove",
    "tracking": "matchmove",
    "cameratracking": "matchmove",
    "layout": "layout",
    "shotlayout": "layout",
    "setdress": "setdress",
    "setdressing": "setdress",
    "environment": "setdress",
    "env": "setdress",
    "animation": "animation",
    "anim": "animation",
    "crowd": "crowd",
    "crowdsim": "crowd",
    "crowdsimulation": "crowd",
    "crowdcfx": "crowdcfx",
    "crowdfx": "crowdcfx",
    "cfx": "cfx",
    "techanim": "cfx",
    "technicalanimation": "cfx",
    "cloth": "cfx",
    "groomsim": "cfx",
    "fx": "fx",
    "effects": "fx",
    "lighting": "lighting",
    "light": "lighting",
    "lgt": "lighting",
}

UNSUPPORTED_ASSET_TASKS = frozenset({
    "art", "modeling", "texture", "lookdev", "rigging", "edit",
    "paint", "compositing", "roto",
})

_INVALID_NODE_TOKEN = re.compile(r"[^A-Za-z0-9_]+")


def normalize_identifier(value: Any) -> str:
    """Return a comparison-safe AYON task/department identifier."""
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def resolve_department(
    explicit_department: Any = "",
    task_type: Any = "",
    task_name: Any = "",
) -> Optional[str]:
    """Resolve a shot department using metadata-first priority."""
    for value in (explicit_department, task_type, task_name):
        normalized = normalize_identifier(value)
        if not normalized:
            continue
        department = TASK_TYPE_ALIASES.get(normalized)
        if department:
            return department
        if normalized in UNSUPPORTED_ASSET_TASKS:
            return None
    return None


def department_index(department: Any) -> int:
    """Return production order, sorting unsupported values last."""
    resolved = resolve_department(department)
    if resolved is None:
        return len(SHOT_DEPARTMENTS)
    return SHOT_DEPARTMENTS.index(resolved)


def sort_departments(departments: Iterable[Any]) -> list[str]:
    """Return unique supported departments in production order."""
    resolved = {
        value for value in (
            resolve_department(department) for department in departments
        ) if value
    }
    return sorted(resolved, key=department_index)


def upstream_departments(task: Any) -> tuple[str, ...]:
    """Return every department weaker than the supplied shot task."""
    department = resolve_department(task)
    if department is None:
        return ()
    return SHOT_DEPARTMENTS[:department_index(department)]


def sanitize_node_name(value: Any, fallback: str = "apkg") -> str:
    """Return a deterministic Houdini-safe node name."""
    token = _INVALID_NODE_TOKEN.sub("_", str(value or "").strip())
    token = re.sub(r"_+", "_", token).strip("_").lower()
    if not token:
        token = fallback
    if token[0].isdigit():
        token = "apkg_" + token
    return token


@dataclass(frozen=True)
class ValidationMessage:
    level: str
    code: str
    message: str
    product_name: str = ""


@dataclass(frozen=True)
class PayloadArc:
    asset_path: str
    target_prim: str
    source_prim: str


@dataclass(frozen=True)
class PayloadPackagePlan:
    stable_key: str
    package_uid: str
    department: str
    product_name: str
    product_id: str
    version_id: str
    representation_id: str
    version: int
    status: str
    mode: str
    order: int
    arcs: tuple[PayloadArc, ...]


@dataclass(frozen=True)
class DepartmentPlan:
    department: str
    label: str
    packages: tuple[PayloadPackagePlan, ...]


@dataclass(frozen=True)
class ShotBuildPlan:
    departments: tuple[DepartmentPlan, ...]
    messages: tuple[ValidationMessage, ...]

    @property
    def packages(self) -> tuple[PayloadPackagePlan, ...]:
        return tuple(
            package
            for department in self.departments
            for package in department.packages
        )

    @property
    def errors(self) -> tuple[ValidationMessage, ...]:
        return tuple(item for item in self.messages if item.level == "error")

    @property
    def warnings(self) -> tuple[ValidationMessage, ...]:
        return tuple(item for item in self.messages if item.level == "warning")


def _stable_key(item: Mapping[str, Any]) -> str:
    """Use product identity for updates and package identity as fallback."""
    return str(
        item.get("productId")
        or item.get("packageUid")
        or "{}:{}:{}".format(
            item.get("department", ""),
            item.get("role", ""),
            item.get("slot", ""),
        )
    ).strip()


def _payload_arcs(item: Mapping[str, Any]) -> tuple[PayloadArc, ...]:
    path = str(item.get("entrypointPath") or "").strip().replace("\\", "/")
    exports = sorted({
        str(value or "").strip()
        for value in item.get("exports") or ()
        if str(value or "").strip()
    })
    return tuple(
        PayloadArc(path, export, export) for export in exports
    )


def build_payload_plan(
    selection_data: Mapping[str, Any],
) -> ShotBuildPlan:
    """Build and validate a deterministic, payload-only shot plan.

    APKG entrypoints preserve their authored namespace. Every declared
    interface export becomes a payload arc from the same source prim to the
    same target prim. The Houdini adapter reconstructs the non-payload scope
    shell around these owned branches.
    """
    messages = []
    packages = []
    seen_representations = set()
    seen_paths = set()
    for sequence, item in enumerate(selection_data.get("packages") or (), 1):
        product_name = str(
            item.get("productName") or item.get("role") or "APKG"
        )
        if str(item.get("mode") or "inherit").lower() == "disabled":
            messages.append(ValidationMessage(
                "info", "package_disabled",
                "{} is disabled and will not be payloaded".format(product_name),
                product_name,
            ))
            continue

        department = resolve_department(item.get("department"))
        if department is None:
            messages.append(ValidationMessage(
                "warning", "unsupported_department",
                "{} has unsupported department '{}' and was skipped".format(
                    product_name, item.get("department") or "unknown"
                ),
                product_name,
            ))
            continue

        path = str(item.get("entrypointPath") or "").strip()
        representation_id = str(
            item.get("entrypointRepresentationId") or ""
        ).strip()
        if not path:
            messages.append(ValidationMessage(
                "error", "missing_entrypoint",
                "{} has no APKG USD entrypoint".format(product_name),
                product_name,
            ))
            continue
        normalized_path = path.replace("\\", "/")
        if representation_id and representation_id in seen_representations:
            messages.append(ValidationMessage(
                "error", "duplicate_representation",
                "{} uses an already selected representation and was skipped"
                .format(product_name), product_name,
            ))
            continue
        if normalized_path in seen_paths:
            messages.append(ValidationMessage(
                "error", "duplicate_entrypoint",
                "{} uses an already selected entrypoint and was skipped"
                .format(product_name), product_name,
            ))
            continue

        arcs = _payload_arcs(item)
        if not arcs:
            messages.append(ValidationMessage(
                "error", "missing_exports",
                "{} declares no payloadable USD exports".format(product_name),
                product_name,
            ))
            continue
        invalid_exports = [
            arc.target_prim for arc in arcs
            if not arc.target_prim.startswith("/") or arc.target_prim == "/"
        ]
        if invalid_exports:
            messages.append(ValidationMessage(
                "error", "invalid_export",
                "{} has invalid payload export{}: {}".format(
                    product_name,
                    "s" if len(invalid_exports) != 1 else "",
                    ", ".join(invalid_exports),
                ),
                product_name,
            ))
            continue

        if representation_id:
            seen_representations.add(representation_id)
        seen_paths.add(normalized_path)
        packages.append(PayloadPackagePlan(
            stable_key=_stable_key(item),
            package_uid=str(item.get("packageUid") or ""),
            department=department,
            product_name=product_name,
            product_id=str(item.get("productId") or ""),
            version_id=str(item.get("versionId") or ""),
            representation_id=representation_id,
            version=int(item.get("version") or 0),
            status=str(item.get("status") or "unknown"),
            mode=str(item.get("mode") or "inherit"),
            order=int(item.get("order") or sequence * 10),
            arcs=arcs,
        ))

    grouped = {}
    for package in packages:
        grouped.setdefault(package.department, []).append(package)
    departments = []
    for department in sort_departments(grouped):
        ordered = tuple(sorted(
            grouped[department],
            key=lambda value: (
                value.order,
                value.product_name.lower(),
                value.version,
                value.stable_key,
            ),
        ))
        departments.append(DepartmentPlan(
            department,
            SHOT_DEPARTMENT_LABELS[department],
            ordered,
        ))
    return ShotBuildPlan(tuple(departments), tuple(messages))
