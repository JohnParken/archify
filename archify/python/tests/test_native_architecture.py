"""Direct tests for the native Architecture SVG renderer."""

import json
from pathlib import Path
import re
import tempfile
import unittest

from archify_runtime.architecture import render_architecture
from archify_runtime.common import DiagnosticError


ARCHIFY = Path(__file__).resolve().parents[2]
EXAMPLES = ARCHIFY / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NativeArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="archify-native-lc-arch-architecture-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.tmp = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def read_example(self, name="web-app"):
        return json.loads((EXAMPLES / f"{name}.architecture.json").read_text(encoding="utf-8"))

    def assert_svg_valid(self, diagram, quality=None):
        actual = render_architecture(diagram, quality).strip()
        self.assertTrue(actual.startswith("<svg"), "Output does not start with <svg")
        self.assertTrue(actual.endswith("</svg>"), "Output does not end with </svg>")
        self.assertIn("viewBox=", actual)
        for comp in diagram.get("components", []):
            self.assertIn(f'data-node-id="{comp["id"]}"', actual)
        for conn in diagram.get("connections", []):
            if "id" in conn:
                self.assertIn(f'data-edge-id="{conn["id"]}"', actual)
        return actual

    def assert_failure(self, diagram, code):
        with self.assertRaises(DiagnosticError) as raised:
            render_architecture(diagram)
        matching = [entry for entry in raised.exception.diagnostics if entry.get("code") == code]
        self.assertTrue(matching, f"Expected diagnostic code {code} in {raised.exception.diagnostics}")
        return raised.exception.diagnostics

    def test_checked_in_examples_render_valid_svg(self):
        examples = sorted(EXAMPLES.glob("*.architecture.json"))
        examples.append(FIXTURES / "architecture-label-repair/worldscope.json")
        self.assertTrue(examples)
        for path in examples:
            with self.subTest(example=path.name):
                diagram = json.loads(path.read_text(encoding="utf-8"))
                self.assert_svg_valid(diagram)

    def test_synthetic_wide_reciprocal_ports_keep_fourteen_pixel_marker_gap(self):
        path = FIXTURES / "reader-readability/synthetic-wide.architecture.json"
        diagram = json.loads(path.read_text(encoding="utf-8"))
        svg = self.assert_svg_valid(diagram)
        routes = {}
        for edge in ("request-envelope", "json-response"):
            match = re.search(rf'data-edge-id="{edge}"[^>]*data-composition-points="([^"]+)"', svg)
            self.assertIsNotNone(match, edge)
            routes[edge] = [[float(value) for value in point.split(",")]
                            for point in match.group(1).split(";")]
        request, response = routes["request-envelope"], routes["json-response"]
        self.assertEqual(len(request), 2)
        self.assertEqual(len(response), 2)
        self.assertEqual(abs(request[0][1] - response[-1][1]), 14)
        self.assertEqual(abs(request[-1][1] - response[0][1]), 14)

    def test_first_draft_fixtures_preserve_canonical_failure_diagnostics(self):
        fixtures = FIXTURES / "architecture-first-draft"
        for name, code in (("audit-luna", "composition/label-gap"),
                           ("audit-terra", "composition/label-gap"),
                           ("queue-sol", "composition/label-gap"),
                           ("queue-luna", "layout/self-loop-ports")):
            with self.subTest(first_draft=name):
                diagram = json.loads((fixtures / f"{name}.architecture.json").read_text(encoding="utf-8"))
                diagnostics = self.assert_failure(diagram, code)
                if name == "queue-luna":
                    codes = {entry.get("code") for entry in diagnostics}
                    self.assertIn("layout/self-loop-ports", codes)
                    self.assertNotIn("layout/constraint", codes)

    def test_automatic_pinned_side_and_label_routing_match(self):
        diagram = self.read_example()
        diagram["connections"][0]["fromSide"] = "top"
        svg = self.assert_svg_valid(diagram)
        self.assertIn('data-composition-points=', svg)

    def test_custom_locale_brand_label_pins_and_quality_match(self):
        diagram = self.read_example()
        diagram["meta"]["locale"] = "xx"
        diagram["meta"]["translations"] = {
            "diagram.description.architecture": "Localized system architecture.",
            "legend.architecture.external": "Client",
        }
        diagram["components"][0]["brand"] = "cloudflare"
        diagram["connections"][0]["labelSegment"] = 1
        diagram["connections"][0]["labelDx"] = 8
        svg = self.assert_svg_valid(diagram, quality="showcase")
        self.assertIn("Localized system architecture.", svg)
        self.assertIn("Client", svg)
        self.assertIn('data-brand-mark="cloudflare"', svg)

    def test_unknown_connection_endpoint_raises_native_diagnostic(self):
        diagram = self.read_example()
        diagram["connections"][0]["to"] = "missing-component"
        self.assert_failure(diagram, "layout/constraint")


if __name__ == "__main__":
    unittest.main()
