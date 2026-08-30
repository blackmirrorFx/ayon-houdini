"""Canonical APKG resource construction helpers."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .models import PackageResource


def external_resource(path):
    """Return a manifest-safe resource for a discovered USD dependency."""
    value = str(path or "").strip()
    if not value:
        raise ValueError("APKG external resource path is empty")
    clean_path = value.split("?", 1)[0]
    name = os.path.basename(clean_path)
    extension = "bgeo.sc" if name.lower().endswith(".bgeo.sc") else (
        os.path.splitext(name)[1].lower().lstrip(".")
    )
    stem = name[:-len(extension) - 1] if extension else name
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stem).strip("_.-")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    role = "resource.{}.{}.{}".format(
        extension or "file", stem or "unnamed", digest
    )
    return PackageResource(
        role=role,
        extension=extension,
        resolver_uri=value if value.startswith("ayon://") else "",
        load_strategy=(
            "sublayer" if extension in {"usd", "usda", "usdc", "usdz"}
            else "reference"
        ),
        paths=(value,),
        tags=("apkg", "external-resource"),
    )


def plan_resource_transfer(path, publish_dir):
    """Plan a durable AYON transfer for one APKG external resource.

    BMFX USD Cache resources are directory bundles: ``master.usdc`` may
    reference frame/topology files beside it. Their ``cache/<name>/v###``
    structure is preserved below the shot's published USD root so relative
    arcs remain valid after integration. Other resources are namespaced below
    the APKG version directory.
    """
    source = os.path.normpath(str(path or "").split("?", 1)[0])
    if not source:
        raise ValueError("APKG external resource path is empty")
    if str(path).startswith("ayon://"):
        return {
            "source": str(path),
            "destination": str(path),
            "transfers": (),
        }
    if not os.path.isfile(source):
        raise ValueError("APKG external resource does not exist: {}".format(
            source
        ))

    publish_dir = os.path.normpath(str(publish_dir or ""))
    if not publish_dir:
        raise ValueError("APKG publish directory is unavailable")
    parts = Path(source).parts
    cache_index = None
    for index, part in enumerate(parts):
        if (
            part.lower() == "cache"
            and index + 2 < len(parts)
            and re.match(r"^v\d+$", parts[index + 2], re.IGNORECASE)
        ):
            cache_index = index

    transfers = []
    if cache_index is not None:
        source_version_dir = str(Path(*parts[:cache_index + 3]))
        # publishDir is .../publish/usd/<product>/v###.
        publish_usd_root = os.path.dirname(os.path.dirname(publish_dir))
        destination_version_dir = os.path.join(
            publish_usd_root, *parts[cache_index:cache_index + 3]
        )
        for root, dirnames, filenames in os.walk(source_version_dir):
            dirnames[:] = [
                name for name in dirnames
                if not name.startswith(".") and name.lower() != "hip"
            ]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                source_file = os.path.join(root, filename)
                relative = os.path.relpath(source_file, source_version_dir)
                transfers.append((
                    source_file,
                    os.path.join(destination_version_dir, relative),
                ))
        destination = os.path.join(
            destination_version_dir,
            os.path.relpath(source, source_version_dir),
        )
    else:
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        destination = os.path.join(
            publish_dir, "resources", digest, os.path.basename(source)
        )
        transfers.append((source, destination))

    normalized = tuple(
        (os.path.normpath(src), os.path.normpath(dst))
        for src, dst in transfers if os.path.normpath(src) != os.path.normpath(dst)
    )
    return {
        "source": source,
        "destination": os.path.normpath(destination),
        "transfers": normalized,
    }
