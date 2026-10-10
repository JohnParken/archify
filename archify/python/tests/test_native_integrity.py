"""Strict checks inspect final HTML instead of trusting renderer receipts."""

import unittest

from archify_runtime.common import DiagnosticError
from archify_runtime.integrity import check_html


class StaticIntegrityTests(unittest.TestCase):
    SVG = '<svg viewBox="0 0 100 100" data-quality-profile="standard"><text x="5" y="20">Test</text></svg>'

    def html(self, svg=None):
        return ('<!DOCTYPE html><html><head><title>Test</title></head><body>'
                '<h1>Test</h1><div class="diagram-container">' + (svg or self.SVG) +
                '</div></body></html>').encode()

    def assert_rejected(self, html, code, **kwargs):
        with self.assertRaises(DiagnosticError) as error:
            check_html(html, "standard", **kwargs)
        self.assertIn(code, {entry["code"] for entry in error.exception.diagnostics})

    def test_valid_html_and_validated_svg_binding(self):
        self.assertEqual(check_html(self.html(), "standard", expected_svg=self.SVG)["status"], "passed")
        self.assert_rejected(self.html(), "artifact/svg-mismatch", expected_svg=self.SVG.replace("Test", "Other"))

    def test_duplicate_and_unresolved_svg_ids_are_rejected(self):
        self.assert_rejected(self.html(self.SVG.replace('<text ', '<g id="same"/><g id="same"/><text ')), "artifact/duplicate-id")
        self.assert_rejected(self.html(self.SVG.replace('<text ', '<path stroke="url(#missing)"/><text ')), "artifact/missing-reference")

    def test_invalid_viewbox_and_quality_profile_are_rejected(self):
        for value in ("0 0 nan 100", "0 0 0 100", "0 0 100", "invalid"):
            with self.subTest(viewbox=value):
                self.assert_rejected(self.html(self.SVG.replace("0 0 100 100", value)), "artifact/viewbox")
        self.assert_rejected(self.html(self.SVG.replace('data-quality-profile="standard"', 'data-quality-profile="showcase"')), "artifact/quality-profile")

    def test_unfilled_slots_missing_diagram_and_truncated_html_are_rejected(self):
        html = self.html()
        for invalid in (html.replace(b"</body>", b"<!-- ARCHIFY:CARDS_SLOT_START --></body>"),
                        html.replace(self.SVG.encode(), b""), html.replace(b"</body></html>", b"")):
            self.assert_rejected(invalid, "artifact/html-structure")


if __name__ == "__main__":
    unittest.main()
