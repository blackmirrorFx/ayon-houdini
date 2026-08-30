"""Add durable BMFX correlation identifiers to every Houdini publish."""

import uuid
from datetime import datetime, timezone

import pyblish.api


def _uid(label):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "BMFX-{}-{}-{}".format(
        label, stamp, uuid.uuid4().hex[:12].upper()
    )


class CollectBmfxPackageUid(pyblish.api.InstancePlugin):
    """Persist execution/package ids in AYON version data.

    All instances in one Publisher run share ``bmfxExecutionId``. Each
    individual AYON product version receives a unique ``bmfxPackageId``.
    Existing dispatcher-authored IDs always win.
    """

    label = "Collect BMFX Package UID"
    order = pyblish.api.CollectorOrder + 0.49
    hosts = ["houdini"]
    families = ["*"]

    def process(self, instance):
        context = instance.context
        execution_id = context.data.get("bmfxExecutionId")
        if not execution_id:
            execution_id = _uid("EXEC")
            context.data["bmfxExecutionId"] = execution_id

        version_data = instance.data.setdefault("versionData", {})
        package_id = (
            version_data.get("bmfxPackageId")
            or instance.data.get("bmfxPackageId")
            or _uid("PKG")
        )
        instance.data["bmfxPackageId"] = package_id
        version_data["bmfxPackageId"] = package_id
        version_data["bmfxExecutionId"] = execution_id
        version_data["bmfxUidSchema"] = 1

        self.log.info(
            "BMFX package %s (execution %s)", package_id, execution_id
        )
