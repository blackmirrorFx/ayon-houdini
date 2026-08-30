"""Collect BMFX APKG authoring block and dependency metadata."""

from __future__ import annotations

import pyblish.api
import hou

from ayon_core.pipeline import KnownPublishError
from ayon_houdini.api import plugin
from ayon_houdini.apkg.graph import publish_through_representation_ids
from ayon_houdini.apkg.naming import apkg_product_name
from ayon_houdini.nodes.lops import apkg as apkg_nodes


class CollectAPKG(plugin.HoudiniInstancePlugin):
    label = "Collect BMFX APKG"
    order = pyblish.api.CollectorOrder - 0.3
    families = ["apkg"]

    def process(self, instance):
        rop_node = hou.node(instance.data["instance_node"])
        if rop_node is None:
            raise KnownPublishError("APKG publish ROP does not exist")
        if apkg_nodes.is_apkg_publisher(rop_node):
            end_node = hou.node(
                rop_node.userData(apkg_nodes.PAIRED_END_USER_DATA) or ""
            ) or (rop_node.inputs()[0] if rop_node.inputs() else None)
        else:
            # Legacy /out APKG Publish HDA support.
            source_parm = rop_node.parm("loppath")
            end_node = source_parm.evalAsNode() if source_parm else None
        if end_node is None or not isinstance(end_node, hou.LopNode):
            raise KnownPublishError(
                "APKG Source must point to a BMFX APKG End LOP"
            )
        try:
            data = apkg_nodes.validate_end(end_node, show_message=False)
        except apkg_nodes.ApkgNodeError as exc:
            raise KnownPublishError(str(exc))

        # Export through the dedicated Publisher LOP so its authored APKG
        # marker layer is part of the resulting entrypoint. Legacy /out
        # instances continue to export directly from End.
        instance.data["output_node"] = (
            rop_node if apkg_nodes.is_apkg_publisher(rop_node)
            else end_node
        )
        instance.data["apkgData"] = data
        # Recompute identity from the current End controls. This keeps the
        # product canonical when CFX Type or Split changed after creation.
        apkg_name = data["split"] if data["useSplit"] else "main"
        product_name = apkg_product_name(
            data["task"], data["split"], apkg_name,
            classification=data.get("classification"),
            use_split=data["useSplit"],
        )
        instance.data["productName"] = product_name
        instance.data["apkgTask"] = data["task"]
        instance.data["apkgSplit"] = data["split"]
        instance.data["apkgClassification"] = data.get(
            "classification", ""
        )
        instance.data["apkgUseSplit"] = data["useSplit"]
        data["productName"] = product_name
        instance.data["family"] = "apkg"
        # APKG owns its USD extraction and composition contract. Advertising
        # the generic `usd` family makes AYON Core's standard contribution
        # collector generate usdShot_<department> and usdShot products, which
        # are a separate workflow and must never be spawned by APKG.
        instance.data["families"] = apkg_nodes.isolated_publish_families(
            instance.data.get("families")
        )

        # Reuse AYON's normal generative input-link integration. Context-only
        # packages are intentionally absent from outgoing publish links.
        dependencies = [
            item for item in data["selection"].get("packages") or []
            if item.get("publishThrough")
            and item.get("mode") not in {"context", "disabled"}
        ]
        instance.data["inputRepresentations"] = (
            publish_through_representation_ids(dependencies)
        )
        instance.data["apkgDependencies"] = dependencies

        version_data = instance.data.setdefault("versionData", {})
        version_data["bmfxKind"] = "apkg"
        version_data["bmfxApkgSchema"] = "bmfx.apkg/1.0"


class CollectAPKGIdentity(plugin.HoudiniInstancePlugin):
    """Finalize queryable APKG version metadata after UID collection."""

    label = "Collect BMFX APKG Identity"
    order = pyblish.api.CollectorOrder + 0.495
    families = ["apkg"]

    def process(self, instance):
        data = instance.data["apkgData"]
        package_uid = instance.data.get("bmfxPackageId")
        if not package_uid:
            raise KnownPublishError("BMFX package UID was not collected")
        dependencies = []
        for item in instance.data.get("apkgDependencies") or []:
            dependencies.append({
                "packageUid": item["packageUid"],
                "versionId": item.get("versionId", ""),
                "productId": item.get("productId", ""),
                "mode": item.get("mode", "inherit"),
                "required": True,
                "publishThrough": True,
                "order": int(item.get("order") or 0),
                "purpose": "",
            })
        summary = {
            "schema": "bmfx.apkg.summary/1.0",
            "kind": "contribution",
            "department": data["department"],
            "classification": data.get("classification", ""),
            "effectType": data["effectType"],
            "contributionType": data["effectType"],
            "role": data["role"],
            "slot": data["slot"],
            "entrypointRepresentationId": "",
            "dependencies": dependencies,
            "interface": {
                "exports": list(data["exports"]),
                "requires": list(data["requires"]),
                "capabilities": list(data["capabilities"]),
            },
        }
        version_data = instance.data.setdefault("versionData", {})
        version_data["bmfxApkg"] = summary
        version_data["bmfxPackageId"] = package_uid

        # Houdini's generic CollectUpstreamInputs runs at +0.4 and replaces
        # inputRepresentations with scene-container inputs. APKG dependency
        # locks are authoritative, so restore them here immediately before
        # AYON Core converts representation IDs to generative version links
        # at +0.499.
        representation_ids = publish_through_representation_ids(
            instance.data.get("apkgDependencies")
        )
        instance.data["inputRepresentations"] = representation_ids
        instance.data.pop("inputVersions", None)
        self.log.debug(
            "Final APKG input representations: %s", representation_ids
        )
