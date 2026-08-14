"""USD-level regression tests for the automatic light LPE AOV asset."""

from __future__ import absolute_import

import unittest
import importlib.util
import os

from pxr import Sdf, Usd, UsdLux, UsdRender


MODULE_PATH = os.path.join(
    os.path.dirname(__file__),
    "light_lpe_aovs.py",
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "light_lpe_aovs", MODULE_PATH
)
light_lpe_aovs = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(light_lpe_aovs)


class LightLpeAovTests(unittest.TestCase):

    def setUp(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.key = UsdLux.SphereLight.Define(
            self.stage, Sdf.Path("/World/Lights/Key_Light")
        ).GetPrim()
        self.fill = UsdLux.RectLight.Define(
            self.stage, Sdf.Path("/World/Lights/Fill")
        ).GetPrim()

        self.ci = UsdRender.Var.Define(
            self.stage, Sdf.Path("/Render/Products/Vars/Ci")
        )
        self.product = UsdRender.Product.Define(
            self.stage, Sdf.Path("/Render/Products/beauty")
        )
        self.product.CreateOrderedVarsRel().SetTargets([
            self.ci.GetPath()
        ])

    def _cook(self):
        return light_lpe_aovs.author_light_lpe_aovs(self.stage)

    def test_tags_beauty_aovs_and_product_binding(self):
        result = self._cook()

        self.assertEqual(
            [item["tag"] for item in result["lights"]],
            ["Fill", "Key_Light"],
        )
        for item in result["lights"]:
            prim = self.stage.GetPrimAtPath(item["path"])
            self.assertEqual(
                prim.GetAttribute(
                    light_lpe_aovs.RENDERMAN_LPE_ATTRIBUTE
                ).Get(),
                item["tag"],
            )
            self.assertEqual(
                prim.GetAttribute(
                    light_lpe_aovs.KARMA_LPE_ATTRIBUTE
                ).Get(),
                item["tag"],
            )

        targets = [
            path.pathString
            for path in self.product.GetOrderedVarsRel().GetTargets()
        ]
        self.assertEqual(targets, [self.ci.GetPath().pathString] + result["aovs"])
        self.assertEqual(result["aovs"], [
            "/Render/Products/Vars/beauty_Fill",
            "/Render/Products/Vars/beauty_Key_Light",
        ])

        expressions = [
            self.stage.GetPrimAtPath(path).GetAttribute("sourceName").Get()
            for path in result["aovs"]
        ]
        self.assertEqual(expressions, [
            "C[DS]*<L.'Fill'>",
            "C[DS]*<L.'Key_Light'>",
        ])
        channel_names = [
            self.stage.GetPrimAtPath(path).GetAttribute(
                "driver:parameters:aov:name"
            ).Get()
            for path in result["aovs"]
        ]
        self.assertEqual(channel_names, [
            "C_Fill",
            "C_Key_Light",
        ])
        formats = [
            self.stage.GetPrimAtPath(path).GetAttribute(
                "driver:parameters:aov:format"
            ).Get()
            for path in result["aovs"]
        ]
        self.assertEqual(formats, ["float3", "float3"])
        data_types = [
            self.stage.GetPrimAtPath(path).GetAttribute("dataType").Get()
            for path in result["aovs"]
        ]
        self.assertEqual(data_types, ["float3", "float3"])
        husk_names = [
            self.stage.GetPrimAtPath(path).GetAttribute(
                "driver:parameters:aov:husk:name"
            ).Get()
            for path in result["aovs"]
        ]
        self.assertEqual(husk_names, ["C_Fill", "C_Key_Light"])
        for path in result["aovs"]:
            prim = self.stage.GetPrimAtPath(path)
            self.assertIn("KarmaRenderVarAPI", prim.GetAppliedSchemas())
            self.assertIn("HuskRenderVarAPI", prim.GetAppliedSchemas())
            self.assertEqual(
                prim.GetAttribute(
                    "driver:parameters:aov:husk:channel_prefix"
                ).Get(),
                prim.GetAttribute("driver:parameters:aov:husk:name").Get(),
            )
            self.assertTrue(
                prim.GetAttribute(
                    "driver:parameters:aov:husk:multiSampled"
                ).Get()
            )

    def test_recook_replaces_managed_aovs_without_duplicates(self):
        first = self._cook()
        second = self._cook()
        self.assertEqual(first["aovs"], second["aovs"])

        targets = self.product.GetOrderedVarsRel().GetTargets()
        self.assertEqual(len(targets), 3)
        self.assertEqual(targets[0], self.ci.GetPath())

    def test_removed_light_removes_its_managed_aov(self):
        self._cook()
        self.stage.RemovePrim(self.fill.GetPath())
        result = self._cook()

        self.assertEqual(len(result["lights"]), 1)
        self.assertEqual(len(result["aovs"]), 1)
        targets = self.product.GetOrderedVarsRel().GetTargets()
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0], self.ci.GetPath())

    def test_valid_existing_tag_is_reused(self):
        self.key.CreateAttribute(
            light_lpe_aovs.RENDERMAN_LPE_ATTRIBUTE,
            Sdf.ValueTypeNames.String,
        ).Set("heroKey")
        result = self._cook()
        by_path = {item["path"]: item["tag"] for item in result["lights"]}
        self.assertEqual(by_path[self.key.GetPath().pathString], "heroKey")

    def test_selected_karma_split_aovs_share_the_product(self):
        result = light_lpe_aovs.author_light_lpe_aovs(
            self.stage,
            enabled_aovs=(
                "beauty",
                "directdiffuse",
                "indirectglossyreflection",
                "visiblelights",
            ),
        )
        self.assertEqual(len(result["aovs"]), 8)
        by_channel = {}
        for path in result["aovs"]:
            prim = self.stage.GetPrimAtPath(path)
            by_channel[
                prim.GetAttribute("driver:parameters:aov:husk:name").Get()
            ] = prim.GetAttribute("sourceName").Get()

        self.assertEqual(by_channel["C_Fill"], "C[DS]*<L.'Fill'>")
        self.assertEqual(
            by_channel["directdiffuse_Fill"],
            "C<RD><L.'Fill'>",
        )
        self.assertEqual(
            by_channel["indirectglossyreflection_Key_Light"],
            "C<RG>.+<L.'Key_Light'>",
        )
        self.assertEqual(
            by_channel["visiblelights_Key_Light"],
            "C<L.'Key_Light'>",
        )
        targets = self.product.GetOrderedVarsRel().GetTargets()
        self.assertEqual(len(targets), 9)


if __name__ == "__main__":
    unittest.main()
