"""Validate the APKG contract before any files are written."""

from __future__ import annotations

import os

import pyblish.api
import hou

from ayon_core.pipeline import PublishValidationError
from ayon_houdini.api import plugin


class ValidateAPKG(plugin.HoudiniInstancePlugin):
    label = "Validate BMFX APKG"
    order = pyblish.api.ValidatorOrder - 0.35
    families = ["apkg"]

    def process(self, instance):
        data = instance.data.get("apkgData") or {}
        errors = []
        for key in ("department", "role", "slot", "builderId"):
            if not data.get(key):
                errors.append("Missing APKG {}".format(key))
        exports = tuple(data.get("exports") or ())
        if not exports:
            errors.append("APKG must own at least one exported USD prim")
        if len(exports) != len(set(exports)):
            errors.append("APKG export paths must be unique")

        selection = data.get("selection") or {}
        if selection.get("schema") != "bmfx.apkg.selection/1.0":
            errors.append("Invalid or missing APKG Begin selection")
        for item in selection.get("packages") or []:
            if item.get("mode") == "context" and item.get("publishThrough"):
                errors.append(
                    "Context package {} cannot publish through".format(
                        item.get("packageUid")
                    )
                )

        # Creator instances do not guarantee that the Houdini node is stored
        # as a Pyblish list member. The imprinted instance path is the stable,
        # authoritative reference for both LOP and Driver publishers.
        publisher = hou.node(instance.data.get("instance_node") or "")
        output_parm = publisher.parm("lopoutput") if publisher else None
        output = output_parm.evalAsString() if output_parm else ""
        if not output:
            errors.append("APKG output path is empty")
        elif os.path.splitext(output)[1].lower() not in {
            ".usd", ".usda", ".usdc"
        }:
            errors.append("APKG entrypoint must use a USD extension")

        if errors:
            raise PublishValidationError(
                "\n".join(errors),
                title=self.label,
                description=(
                    "APKG publishes must have a valid Begin/End block, stable "
                    "identity, owned USD prim interface and safe dependencies."
                ),
            )
