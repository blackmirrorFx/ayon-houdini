"""Deterministic APKG version selection policies."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence, TypeVar

from .models import PackageRecord


DEFAULT_STATUS_PRIORITY = ("approved", "available", "pending")
T = TypeVar("T")

_STATUS_ALIASES = {
    "approved": "approved",
    "available": "available",
    "pending": "pending",
    "pendingreview": "pending",
    "review": "pending",
}


def _status_tier(value: object) -> str:
    normalized = "".join(
        character for character in str(value or "").strip().lower()
        if character.isalnum()
    )
    return _STATUS_ALIASES.get(normalized, normalized)


def _normalized_tiers(
    status_priority: Sequence[str] | Mapping[str, Iterable[str]],
) -> tuple[tuple[str, frozenset[str]], ...]:
    if isinstance(status_priority, Mapping):
        return tuple(
            (
                str(tier).strip().lower(),
                frozenset(_status_tier(value) for value in values),
            )
            for tier, values in status_priority.items()
        )
    return tuple(
        (_status_tier(status), frozenset({_status_tier(status)}))
        for status in status_priority
    )


def select_preferred_versions(
    records: Iterable[PackageRecord],
    status_priority: Sequence[str] | Mapping[str, Iterable[str]] = (
        DEFAULT_STATUS_PRIORITY
    ),
) -> dict[str, PackageRecord]:
    """Select one record per contribution using status tier before version.

    An older approved version intentionally wins over a newer pending version.
    Records with statuses outside all configured tiers are not selected.
    """

    tiers = _normalized_tiers(status_priority)
    grouped: dict[str, list[PackageRecord]] = defaultdict(list)
    for record in records:
        grouped[record.contribution_key].append(record)

    output = {}
    for key, versions in grouped.items():
        by_status = defaultdict(list)
        for version in versions:
            by_status[_status_tier(version.status)].append(version)
        for _tier, statuses in tiers:
            candidates = [
                version for status in statuses for version in by_status[status]
            ]
            if candidates:
                output[key] = max(
                    candidates,
                    key=lambda item: (item.version, item.version_id),
                )
                break
    return output
