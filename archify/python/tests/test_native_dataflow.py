"""Tests for the Dataflow SVG renderer."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from archify_runtime.common import DiagnosticError
from archify_runtime.dataflow import render_dataflow
from archify_runtime.flow_geometry import automatic_port_spread


ARCHIFY = Path(__file__).resolve().parents[2]
EXAMPLES = ARCHIFY / "examples"


class NativeDataflowTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="archify-native-dataflow-", dir=str(Path("/tmp").resolve()))
        self.tmp = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def read_example(self, name="product-analytics"):
        return json.loads((EXAMPLES / f"{name}.dataflow.json").read_text(encoding="utf-8"))

    def assert_svg_valid(self, diagram, quality=None):
        actual = render_dataflow(diagram, quality).strip()
        self.assertTrue(actual.startswith("<svg"), "Output does not start with <svg")
        self.assertTrue(actual.endswith("</svg>"), "Output does not end with </svg>")
        self.assertIn("viewBox=", actual)
        for node in diagram.get("nodes", []):
            self.assertIn(f'data-node-id="{node["id"]}"', actual)
        for flow in diagram.get("flows", []):
            self.assertIn(f'data-edge-from="{flow["from"]}"', actual)
            self.assertIn(f'data-edge-to="{flow["to"]}"', actual)
        return actual

    def assert_failure(self, diagram, code, quality=None):
        with self.assertRaises(DiagnosticError) as raised:
            render_dataflow(diagram, quality)
        matching = [entry for entry in raised.exception.diagnostics if entry["code"] == code]
        self.assertTrue(matching, f"Expected diagnostic code {code} in {raised.exception.diagnostics}")
        return raised.exception.diagnostics

    def test_checked_in_examples_render_valid_svg(self):
        for example in sorted(EXAMPLES.glob("*.dataflow.json")):
            with self.subTest(example=example.name):
                diagram = json.loads(example.read_text(encoding="utf-8"))
                self.assert_svg_valid(diagram)

    def test_brands_unicode_wrapped_headers_and_legend_overrides_match(self):
        diagram = self.read_example()
        diagram["meta"]["locale"] = "zh-CN"
        diagram["stages"][2]["label"] = "数据处理 👩‍💻 e\u0301 ingestion pipeline with an unusually long header"
        diagram["nodes"][0]["brand"] = "postgresql"
        diagram["meta"]["legend"] = {
            "mode": "auto",
            "entries": {"default": {"label": "flow lane"}, "database": {"visible": False}},
        }
        svg = self.assert_svg_valid(diagram)
        self.assertIn("postgresql", svg)
        self.assertIn("flow lane", svg)

    def test_label_offsets_and_segment_selection_match(self):
        diagram = self.read_example()
        flow = diagram["flows"][0]
        flow.pop("labelAt", None)
        flow["labelSegment"] = 1
        flow["labelDx"] = 10
        flow["labelDy"] = 4
        svg = self.assert_svg_valid(diagram)
        self.assertIn('data-detail="context"', svg)

    def test_custom_locale_translations_and_quality_sources_match(self):
        diagram = self.read_example()
        diagram["meta"]["locale"] = "xx"
        diagram["meta"]["translations"] = {
            "node.focus.detail": "Select {label}: {detail}",
            "diagram.description.dataflow": "Localized data-flow description.",
            "legend.title": "Kind key",
            "legend.dataflow.default": "Payload",
        }
        diagram["meta"]["quality_profile"] = "standard"
        svg = self.assert_svg_valid(diagram, quality="showcase")
        self.assertIn("Kind key", svg)
        self.assertIn("Payload", svg)
        self.assertIn("Localized data-flow description.", svg)

        metadata_profile = copy.deepcopy(diagram)
        metadata_profile["meta"]["quality_profile"] = "showcase"
        previous_quality = os.environ.pop("ARCHIFY_QUALITY_PROFILE", None)
        try:
            svg2 = self.assert_svg_valid(metadata_profile)
            self.assertIn("Kind key", svg2)
        finally:
            if previous_quality is not None:
                os.environ["ARCHIFY_QUALITY_PROFILE"] = previous_quality

    def test_unknown_endpoint_reports_the_canonical_layout_diagnostic(self):
        diagram = self.read_example()
        diagram["flows"][0]["to"] = "missing-node"
        self.assert_failure(diagram, "layout/constraint")

    def test_authored_side_and_label_collisions_keep_actionable_diagnostics(self):
        wrong_side = self.read_example()
        wrong_side["flows"][0]["fromSide"] = "left"
        self.assert_failure(wrong_side, "clean-flow/endpoint-side-direction")

        label_collision = self.read_example()
        label_collision["flows"][0]["labelAt"] = [156, 157]
        self.assert_failure(label_collision, "layout/constraint")

    def test_quality_profile_applies_showcase_route_rhythm_gate(self):
        diagram = {
            "schema_version": 1,
            "diagram_type": "dataflow",
            "meta": {"title": "Route rhythm", "output": "route-rhythm.html", "viewBox": [600, 600]},
            "stages": [{"label": "Input"}, {"label": "Store"}],
            "nodes": [
                {"id": "source", "label": "Source", "stage": 0, "row": 0, "type": "external"},
                {"id": "sink", "label": "Sink", "stage": 1, "row": 2, "type": "database"},
            ],
            "flows": [{
                "from": "source", "to": "sink", "label": "payload", "variant": "default",
                "fromSide": "right", "toSide": "left",
                "via": [[180, 157], [180, 164], [200, 164], [200, 385]],
                "labelAt": [220, 300],
            }],
            "cards": [],
        }
        self.assert_svg_valid(diagram, quality="standard")
        self.assert_failure(diagram, "composition/micro-segment", quality="showcase")


class AutomaticPortSpreadTests(unittest.TestCase):
    def test_spacing_callback_reserves_group_width_and_empty_via_is_authored(self):
        boxes = {
            "hub": {"id": "hub", "x": 0, "y": 0, "width": 80, "height": 120,
                    "cx": 40, "cy": 60},
            "a": {"id": "a", "x": 200, "y": 0, "width": 40, "height": 40,
                  "cx": 220, "cy": 20},
            "b": {"id": "b", "x": 200, "y": 180, "width": 40, "height": 40,
                  "cx": 220, "cy": 200},
        }
        flows = [{"id": "one", "from": "hub", "to": "a"},
                 {"id": "two", "from": "hub", "to": "b"}]
        side_for = lambda relation, endpoint: "right" if endpoint == "source" else "left"
        spread = automatic_port_spread(flows, boxes, side_for=side_for, gutter=0,
                                       spacing_for=lambda left, right: 40)
        self.assertEqual(spread[id(flows[0])]["from"], [80, 40])
        self.assertEqual(spread[id(flows[1])]["from"], [80, 80])

        authored_empty_via = [{**flows[0], "via": []}, flows[1]]
        self.assertEqual(automatic_port_spread(authored_empty_via, boxes, side_for=side_for), {})


if __name__ == "__main__":
    unittest.main()
