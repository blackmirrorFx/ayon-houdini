"""Creator for BMFX contribution APKG publishes."""

from __future__ import annotations

import inspect

import hou

from ayon_core.pipeline import CreatedInstance, CreatorError
from ayon_houdini.api import plugin
from ayon_houdini.api import lib
from ayon_houdini.apkg.naming import apkg_product_name
from ayon_houdini.nodes.lops import apkg as apkg_nodes


class CreateAPKG(plugin.HoudiniCreator):
    """Publish one independently versioned department contribution."""

    identifier = "io.ayon.creators.houdini.apkg"
    label = "BMFX Department APKG"
    product_base_type = "usd"
    product_type = "usd"
    icon = "package"
    description = "Publish an APKG End contribution and manifest"
    enabled = True
    default_variants = ["Main"]
    render_target = "local"
    # The Publisher HDA owns its one-click Publish APKG button.
    add_publish_button = False

    def create(self, product_name, instance_data, pre_create_data):
        # The publish instance is the APKG Publisher LOP directly below End.
        # Artists may select either boundary node or the publisher itself.
        selected_nodes = hou.selectedNodes()
        publisher = hou.node(
            pre_create_data.get("apkg_publisher_path") or ""
        )
        if not apkg_nodes.is_apkg_publisher(publisher):
            publisher = next((
                node for node in selected_nodes
                if isinstance(node, hou.LopNode)
                and apkg_nodes.is_apkg_publisher(node)
            ), None)
        selected_end = next((
            node for node in selected_nodes
            if isinstance(node, hou.LopNode)
            and apkg_nodes.is_apkg_end(node)
        ), None)
        if publisher is not None:
            selected_end = hou.node(
                publisher.userData(apkg_nodes.PAIRED_END_USER_DATA) or ""
            ) or (publisher.inputs()[0] if publisher.inputs() else None)
        if selected_end is None:
            raise RuntimeError(
                "Select APKG End or its BMFX APKG Publisher node first."
            )
        if publisher is None:
            publisher = hou.node(
                selected_end.userData(apkg_nodes.PUBLISHER_USER_DATA) or ""
            )
        if publisher is None:
            publisher = apkg_nodes.create_publisher(selected_end)
        block_data = apkg_nodes.end_data(selected_end)
        apkg_name = (
            block_data["split"] if block_data["useSplit"] else "main"
        )
        product_name = apkg_product_name(
            block_data["task"], block_data["split"], apkg_name,
            classification=block_data.get("classification"),
            use_split=block_data["useSplit"],
        )
        instance_data["apkgName"] = str(apkg_name)
        instance_data["apkgTask"] = block_data["task"]
        instance_data["apkgSplit"] = block_data["split"]
        instance_data["apkgClassification"] = block_data.get(
            "classification", ""
        )
        instance_data["apkgUseSplit"] = block_data["useSplit"]
        creator_attributes = instance_data.setdefault("creator_attributes", {})
        creator_attributes["render_target"] = pre_create_data.get(
            "render_target", self.render_target
        )
        try:
            self.customize_node_look(publisher)
            instance_data["instance_node"] = publisher.path()
            instance_data["instance_id"] = publisher.path()
            instance_data["families"] = self.get_publish_families()
            instance = CreatedInstance(
                product_type=self.product_type,
                product_base_type=self.product_base_type,
                product_name=product_name,
                data=instance_data,
                creator=self,
            )
            if self.enable_staging_path_management:
                staging = self.get_staging_dir(instance).directory
                if self.expand_staging_dir:
                    with hou.ScriptEvalContext(publisher):
                        staging = lib.expand_houdini_string(staging)
                self.set_node_staging_dir(
                    publisher, staging, instance, pre_create_data
                )
            self._add_instance_to_context(instance)
            self.imprint(publisher, instance.data_to_store())
            if self.add_publish_button:
                lib.add_self_publish_button(publisher)
            apkg_nodes.clean_publisher_instance_interface(publisher)
            return instance
        except hou.Error as exc:
            raise CreatorError("Creator error: {}".format(exc)) from exc

    def set_node_staging_dir(
        self, node, staging_dir, instance, pre_create_data
    ):
        node.parm("lopoutput").set("{}/{}.usd".format(
            staging_dir, instance.product_name
        ))

    def get_network_categories(self):
        return [hou.lopNodeTypeCategory()]

    def get_publish_families(self):
        # Deliberately not `usdrop`: APKG has a dedicated extractor that also
        # creates and validates the package manifest.
        return ["apkg", "publish.hou"]

    def get_detail_description(self):
        return inspect.cleandoc("""
            Publish a contribution prepared by a BMFX APKG End LOP. Upstream
            inherited packages become exact version dependencies; context-only
            packages remain visible in the workfile but are not published
            through. Shot frame range and FPS remain owned by AYON.
        """)
