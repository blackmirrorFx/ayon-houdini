"""Canonical APKG product and representation naming."""

from __future__ import annotations

import re


_INVALID_TOKEN = re.compile(r"[^A-Za-z0-9]+")
SPLIT_DEPARTMENTS = frozenset({"fx", "cfx", "crowd"})


def naming_token(value, fallback):
    token = _INVALID_TOKEN.sub("_", str(value or "").strip()).strip("_")
    return (token or fallback).lower()


def apkg_product_name(
        task, split, apkg_name, classification="", use_split=None):
    """Return the stable AYON product name without a version suffix.

    Older callers do not provide ``use_split``. For those, ``main`` is
    treated as the unsplit sentinel while any other split remains explicit.
    """
    normalized_task = naming_token(task, "task")
    normalized_split = naming_token(split, "main")
    if use_split is None:
        use_split = normalized_split != "main"
    use_split = bool(use_split) and normalized_task in SPLIT_DEPARTMENTS
    tokens = [
        "{}split".format(normalized_task)
        if use_split else normalized_task
    ]
    if use_split and str(classification or "").strip():
        tokens.append(naming_token(classification, "main"))
    if use_split:
        tokens.append(normalized_split)
    return "APKG_{}".format("_".join(tokens))


def apkg_version_name(product_name, version):
    """Return the file/package stem with a canonical v### suffix."""
    value = str(product_name or "APKG_task").strip()
    value = re.sub(r"_v\d+$", "", value, flags=re.IGNORECASE)
    return "{}_v{:03d}".format(value, int(version or 1))
