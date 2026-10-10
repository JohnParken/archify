"""Final SVG checks must measure rounded paths, including verified crossover halos."""

import json
from pathlib import Path
import tempfile
import unittest

from archify_runtime.artifact_geometry import final_crossing_diagnostics
from archify_runtime.common import DiagnosticError
from archify_runtime.checks import check_svg
from archify_runtime.lifecycle import render_lifecycle

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NativeArtifactParityTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="archify-native-artifact-", dir=str(Path("/tmp").resolve()))
        self.tmp = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def test_rounded_curve_crossing_matches_public_final_diagnostic(self):
        source = FIXTURES / "v1-baseline/agent-run.lifecycle.json"
        doc = json.loads(source.read_text(encoding="utf-8"))
        svg = render_lifecycle(doc, quality="showcase")
        diagnostics = final_crossing_diagnostics(svg)
        self.assertTrue(diagnostics, "Expected crossover diagnostics on unmitigated showcase curves")
        self.assertTrue(any(d["code"] == "composition/proper-crossing" for d in diagnostics))
        with self.assertRaises(DiagnosticError) as error:
            check_svg(svg, quality="showcase", diagram_type="lifecycle")
        self.assertEqual(error.exception.diagnostics, diagnostics)

    def test_verified_halos_and_shared_endpoint_policy_match_artifact_checker(self):
        for halos in (0, 1, 2):
            for shared in (False, True):
                for workflow in (False, True):
                    with self.subTest(halos=halos, shared=shared, workflow=workflow):
                        parts = []
                        for index, path in enumerate(("M 10 50 L 90 50", "M 50 10 L 50 90")):
                            if index < halos:
                                parts.append(f'<path data-graph-role="automatic-crossover-underlay" d="{path}" fill="none" stroke="var(--mask)" stroke-width="5.1" pointer-events="none"/>')
                            start = "a" if index == 0 or shared else "c"
                            end = "b" if index == 0 else "d"
                            parts.append(f'<path data-edge-from="{start}" data-edge-to="{end}" data-edge-key="{index}" data-composition-crossover="halo" d="{path}" class="a-default" stroke-width="1.1" marker-end="url(#arrowhead)"/>')
                        layout = ' data-layout-contract="readable-v2"' if workflow else ""
                        svg = '<svg viewBox="0 0 100 100" data-quality-profile="showcase"' + layout + '>' + '\n'.join(parts) + '</svg>'
                        actual = final_crossing_diagnostics(svg, workflow)
                        # Two halos always eliminate crossing issues
                        if halos == 2:
                            self.assertEqual(len(actual), 0)
                        elif shared and not workflow:
                            self.assertEqual(len(actual), 0)
                        else:
                            self.assertEqual(len(actual), 1)
                            self.assertEqual(actual[0]["code"], "composition/proper-crossing")
                            self.assertEqual(actual[0]["evidence"]["point"], [50.0, 50.0])


if __name__ == "__main__":
    unittest.main()
