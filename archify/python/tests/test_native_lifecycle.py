"""Tests for the native Lifecycle SVG renderer."""

import json
from pathlib import Path
import tempfile
import unittest

from archify_runtime.common import DiagnosticError
from archify_runtime.lifecycle import render_lifecycle


ARCHIFY = Path(__file__).resolve().parents[2]
EXAMPLES = ARCHIFY / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="archify-native-lc-arch-lifecycle-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.tmp = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def read_example(self, name):
        return json.loads((EXAMPLES / f"{name}.lifecycle.json").read_text(encoding="utf-8"))

    def assert_svg_valid(self, diagram, quality=None):
        actual = render_lifecycle(diagram, quality).strip()
        self.assertTrue(actual.startswith("<svg"), "Output does not start with <svg")
        self.assertTrue(actual.endswith("</svg>"), "Output does not end with </svg>")
        self.assertIn("viewBox=", actual)
        for state in diagram.get("states", []):
            self.assertIn(f'data-node-id="{state["id"]}"', actual)
        for transition in diagram.get("transitions", []):
            self.assertIn(f'data-edge-from="{transition["from"]}"', actual)
            self.assertIn(f'data-edge-to="{transition["to"]}"', actual)
        return actual

    def assert_failure(self, diagram, code):
        with self.assertRaises(DiagnosticError) as raised:
            render_lifecycle(diagram)
        matching = [entry for entry in raised.exception.diagnostics if entry.get("code") == code]
        self.assertTrue(matching, f"Expected diagnostic code {code} in {raised.exception.diagnostics}")
        return raised.exception.diagnostics

    def test_checked_in_v1_and_v2_examples_render_valid_svg(self):
        examples = sorted(EXAMPLES.glob("*.lifecycle.json"))
        examples.extend(sorted((FIXTURES / "lifecycle-planner").glob("*.lifecycle.json")))
        examples = [path for path in examples if path.name not in ("band-title-overlap.lifecycle.json",)]
        self.assertTrue(examples)
        for path in examples:
            with self.subTest(example=path.name):
                diagram = json.loads(path.read_text(encoding="utf-8"))
                self.assert_svg_valid(diagram)

    def test_v2_incompatible_pin_uses_label_reserved_fallback_routes(self):
        diagram = self.read_example("deployment-release")
        diagram["transitions"][0]["fromSide"] = "left"
        svg = self.assert_svg_valid(diagram)
        self.assertIn('data-composition-points=', svg)

    def test_custom_locale_translation_and_explicit_label_geometry_match(self):
        diagram = self.read_example("deployment-release")
        diagram["meta"]["locale"] = "xx"
        diagram["meta"]["translations"] = {
            "diagram.description.lifecycle": "Localized release state diagram.",
            "legend.lifecycle.active": "Working",
        }
        diagram["transitions"][0]["label"] = "image sealed"
        diagram["transitions"][0]["labelSegment"] = 2
        diagram["transitions"][0]["labelDx"] = 7
        svg = self.assert_svg_valid(diagram, quality="showcase")
        self.assertIn("Localized release state diagram.", svg)
        self.assertIn("Working", svg)
        self.assertIn("image sealed", svg)

    def test_unknown_transition_endpoint_raises_native_diagnostic(self):
        diagram = self.read_example("agent-run")
        diagram["transitions"][0]["to"] = "missing-state"
        self.assert_failure(diagram, "layout/constraint")


if __name__ == "__main__":
    unittest.main()
