"""Guarantee that collected BMFX identifiers reach the AYON version entity."""

import ayon_api
import pyblish.api


class IntegrateBmfxPackageUid(pyblish.api.InstancePlugin):
    """Persist BMFX identifiers after AYON has created the version.

    Core AYON normally merges ``versionData`` during integration.  This final
    integration step is deliberately idempotent and protects installations
    where a host-specific publishing path creates the version without carrying
    custom version data across.
    """

    label = "Integrate BMFX Package UID"
    order = pyblish.api.IntegratorOrder + 0.49
    hosts = ["houdini"]
    families = ["*"]

    def process(self, instance):
        version_entity = instance.data.get("versionEntity")
        if not version_entity or not version_entity.get("id"):
            self.log.debug("No integrated version entity; skipping BMFX UID")
            return

        version_data = instance.data.get("versionData") or {}
        package_id = (
            version_data.get("bmfxPackageId")
            or instance.data.get("bmfxPackageId")
        )
        if not package_id:
            self.log.warning("No BMFX package UID was collected for %s", instance)
            return

        execution_id = (
            version_data.get("bmfxExecutionId")
            or instance.context.data.get("bmfxExecutionId")
        )
        current_data = dict(version_entity.get("data") or {})
        desired = {
            "bmfxPackageId": package_id,
            "bmfxUidSchema": version_data.get("bmfxUidSchema", 1),
        }
        if execution_id:
            desired["bmfxExecutionId"] = execution_id
        if all(current_data.get(key) == value for key, value in desired.items()):
            return

        current_data.update(desired)
        ayon_api.update_version(
            instance.context.data["projectName"],
            version_entity["id"],
            data=current_data,
        )
        version_entity["data"] = current_data
        self.log.info("Persisted BMFX package UID %s", package_id)
