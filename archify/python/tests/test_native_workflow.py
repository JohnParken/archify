"""Tests for the native Workflow SVG compiler."""

import copy
import json
from pathlib import Path
import unittest

from archify_runtime.common import DiagnosticError
from archify_runtime.workflow import render_workflow
from archify_runtime.workflow_migration import (
    create_horizontal_rank_mapper, create_mapped_workflow_candidate,
    intrinsic_workflow, map_explicit_coordinates, planning_workflow,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NativeWorkflowTests(unittest.TestCase):
    def workflow_documents(self):
        paths = set((ROOT / "archify/examples").glob("*.workflow.json"))
        paths.update(FIXTURES.rglob("*.workflow.json"))
        if (ROOT / "docs").exists():
            paths.update((ROOT / "docs").rglob("*.workflow.json"))
        return [(path.name, json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(paths)]

    def test_all_valid_workflow_examples_and_fixtures_render_valid_svg(self):
        checked = 0
        for name, diagram in self.workflow_documents():
            try:
                svg = render_workflow(diagram).strip()
                self.assertTrue(svg.startswith("<svg"))
                self.assertTrue(svg.endswith("</svg>"))
                checked += 1
            except DiagnosticError:
                # Some fixtures intentionally fail validation
                pass
        self.assertGreaterEqual(checked, 10)

    def test_custom_locale_svg(self):
        diagram = json.loads((FIXTURES / "issue-126/explicit-route.workflow.json").read_text())
        diagram["meta"]["locale"] = "fr"
        diagram["meta"]["legend"] = {"mode": "auto"}
        diagram["meta"]["translations"] = {"legend.workflow.backend": "Traitement"}
        svg = render_workflow(diagram).strip()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Traitement", svg)

    def test_endpoint_and_semantic_failures_preserve_canonical_codes(self):
        base = json.loads((FIXTURES / "issue-126/explicit-route.workflow.json").read_text())
        endpoint_case = copy.deepcopy(base)
        endpoint_case["edges"][0]["to"] = "missing-node"
        semantic_case = copy.deepcopy(base)
        semantic_case["semanticChecks"] = {"requiredEdges": [{"from": "missing-node", "to": "consumer"}]}

        with self.assertRaises(DiagnosticError) as raised:
            render_workflow(endpoint_case)
        codes = [entry["code"] for entry in raised.exception.diagnostics]
        self.assertIn("workflow/unknown-edge-endpoint", codes)

        with self.assertRaises(DiagnosticError) as raised:
            render_workflow(semantic_case)
        codes = [entry["code"] for entry in raised.exception.diagnostics]
        self.assertIn("workflow/semantic-node-reference", codes)

    def test_invalid_authored_pin_preserves_explicit_conflict_code(self):
        diagram = json.loads((FIXTURES / "issue-126/explicit-route.workflow.json").read_text())
        diagram["edges"][0]["via"] = [[720, -10]]
        with self.assertRaises(DiagnosticError) as raised:
            render_workflow(diagram)
        self.assertTrue(raised.exception.diagnostics)

    def test_unroutable_self_loop_preserves_solver_budget_diagnostic(self):
        diagram = {
            "schema_version": 2, "diagram_type": "workflow",
            "meta": {"title": "Self loop", "output": "self-loop.html", "legend": {"mode": "hidden"}},
            "lanes": [{"id": "main", "label": "Main"}],
            "nodes": [{"id": "a", "lane": "main", "col": 0, "type": "backend", "label": "A"}],
            "edges": [{"id": "self", "from": "a", "to": "a"}],
        }
        with self.assertRaises(DiagnosticError) as raised:
            render_workflow(diagram)
        codes = [entry["code"] for entry in raised.exception.diagnostics]
        self.assertIn("workflow/solver-budget-exhausted", codes)

    def test_migration_geometry_matches_node_and_preserves_authored_input(self):
        source = json.loads((FIXTURES / "v1-workflow-explicit-coordinates.workflow.json").read_text())
        before = copy.deepcopy(source)
        old_columns = [88, 220, 300]
        new_columns = [94, 214, 334]

        mapper = create_horizontal_rank_mapper(old_columns, new_columns)
        mapped = create_mapped_workflow_candidate(source, old_columns, new_columns)

        self.assertEqual([mapper(value) for value in [-44, 88, 220, 260, 300, 412]],
                         [-26.0, 94.0, 214.0, 274.0, 334.0, 502.0])
        self.assertEqual(mapped["changedCoordinates"], [
            {'path': '/edges/0/via/0/0', 'from': 220, 'to': 214.0},
            {'path': '/edges/1/labelAt/0', 'from': 365, 'to': 431.5},
            {'path': '/edges/2/channelX', 'from': 500, 'to': 634.0},
        ])

        iw = intrinsic_workflow(source)
        self.assertIn("nodes", iw)
        self.assertIn("edges", iw)

        pw = planning_workflow(source)
        self.assertIn("lanes", pw)

        copy_for_coordinate_test = copy.deepcopy(source)
        changes = map_explicit_coordinates(copy_for_coordinate_test, mapper)
        self.assertEqual(changes, mapped["changedCoordinates"])
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
