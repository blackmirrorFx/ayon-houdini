"""Finalize APKG representation IDs on the integrated AYON version."""

from __future__ import annotations

import ayon_api
import pyblish.api

from ayon_houdini.api import plugin


class IntegrateAPKGMetadata(plugin.HoudiniInstancePlugin):
    label = "Integrate BMFX APKG Metadata"
    order = pyblish.api.IntegratorOrder + 0.48
    families = ["apkg"]

    def process(self, instance):
        version = instance.data.get("versionEntity") or {}
        version_id = version.get("id")
        if not version_id:
            self.log.debug("APKG version was not integrated; skipping metadata")
            return
        project_name = instance.context.data["projectName"]
        representations = list(ayon_api.get_representations(
            project_name,
            version_ids={version_id},
            fields={"id", "name"},
        ))
        representation_ids = {
            representation["name"]: representation["id"]
            for representation in representations
        }
        current_data = dict(version.get("data") or {})
        summary = dict(
            current_data.get("bmfxApkg")
            or (instance.data.get("versionData") or {}).get("bmfxApkg")
            or {}
        )
        summary["entrypointRepresentationId"] = representation_ids.get(
            "usd", ""
        )
        summary["manifestRepresentationId"] = representation_ids.get(
            "apkg", ""
        )
        current_data.update({
            "bmfxApkg": summary,
            "bmfxApkgSchema": "bmfx.apkg/1.0",
            "bmfxKind": "apkg",
        })
        ayon_api.update_version(
            project_name, version_id, data=current_data
        )
        version["data"] = current_data
