"""Tests for native planning measurements and geometric analysis."""

import unittest

from archify_runtime.composition import (collect_ambiguous_corridors, collect_arrowhead_collisions,
    collect_label_canvas_overflow, collect_label_route_clearance, forward_collinear_analysis_segments)
from archify_runtime.flow_geometry import clean_label_canvas_problems, clean_label_route_clearance_problems
from archify_runtime.route_quality import shortest_orthogonal_grid_route
from archify_runtime.desktop_readability import declared_wide_readability_budget
from archify_runtime.checks import check_svg
from archify_runtime.common import DiagnosticError


class NativeGeometryTests(unittest.TestCase):
    def test_bounded_grid_search_matches_points_and_metrics(self):
        base = {"start": [100, 50], "end": [300, 170], "points": [[100, 50], [300, 170]],
                "obstacles": [{"x": 165, "y": 35, "width": 70, "height": 140}],
                "fromSide": "right", "toSide": "left", "clearance": 2, "maximumObstacleCount": 80,
                "endpointStubPx": 24, "maximumGridNodes": 4096, "bendPenaltyPx": 48}
        cases = [
            (base, True, 336, 1),
            ({**base, "obstacles": []}, True, 320, 0),
            ({**base, "maximumGridNodes": 1}, False, None, None),
            ({**base, "maximumObstacleCount": 0}, False, None, None),
            ({**base, "fromSide": "unknown"}, False, None, None),
            ({**base, "avoidedSegments": [{"start": [240, 0], "end": [240, 200]}]}, True, 380, 1),
            ({**base, "avoidedSegments": [{"start": [240, 0], "end": [240, 200]}], "allowAvoidedCrossings": True}, True, 336, 1),
            ({**base, "borderSegments": [{"start": [120, 210], "end": [310, 210]}]}, True, 336, 1),
        ]
        for index, (options, should_route, expected_len, expected_obs) in enumerate(cases):
            with self.subTest(case=index):
                metrics = {}
                result = shortest_orthogonal_grid_route(**options, metrics=metrics)
                if should_route:
                    self.assertIsNotNone(result)
                    self.assertEqual(result["length"], expected_len)
                    self.assertEqual(result["obstacleCount"], expected_obs)
                    self.assertEqual(metrics["status"], "routed")
                else:
                    self.assertIsNone(result)
                    self.assertNotEqual(metrics["status"], "routed")

    def test_collinear_analysis_preserves_authored_segment_identity(self):
        points = [[0, 0], [10, 0], [20, 0], [20, 0], [10, 0], [10, 20]]
        segments = forward_collinear_analysis_segments(points)
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0]["start"], [0, 0])
        self.assertEqual(segments[0]["end"], [20, 0])
        self.assertEqual(len(segments[0]["sourceSegments"]), 2)

    @staticmethod
    def routes():
        return [
            {"relationIndex": 0, "relation": {"id": "one", "from": "a", "to": "sink"}, "points": [[0, 60], [100, 60]]},
            {"relationIndex": 1, "relation": {"id": "two", "from": "b", "to": "sink"}, "points": [[0, 67], [100, 67]]},
            {"relationIndex": 2, "relation": {"id": "three", "from": "c", "to": "d"}, "points": [[20, 60], [80, 60]]},
            {"relationIndex": 3, "relation": {"id": "four", "from": "a", "to": "e"}, "points": [[0, 60], [20, 60], [20, 100]]},
        ]

    def test_corridors_and_arrowheads_match_shared_endpoint_policy(self):
        routes = self.routes()
        expected_corridor_counts = {
            (False, False): 1,
            (False, True): 1,
            (True, False): 2,
            (True, True): 1,
        }
        for shared in (False, True):
            for trunks in (False, True):
                with self.subTest(shared=shared, trunks=trunks):
                    corridors = collect_ambiguous_corridors(routes, include_shared_endpoints=lambda a, b: shared,
                                                            allow_short_workflow_trunks=trunks)
                    self.assertEqual(len(corridors), expected_corridor_counts[(shared, trunks)])

        collisions = collect_arrowhead_collisions(routes)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["distance"], 7)
        self.assertEqual(collisions[0]["minimum"], 10.5)

    def test_label_route_clearance_matches_nearest_segment_and_deduplication(self):
        routes = self.routes()
        labels = [{"relation": routes[0]["relation"], "relationIndex": 0, "x": 40, "y": 58, "width": 20, "height": 10}]
        clearances = collect_label_route_clearance(labels + labels, routes + [routes[1]], 4)
        self.assertEqual(len(clearances), 2)
        self.assertEqual(clearances[0]["otherRelationIndex"], 1)
        self.assertEqual(clearances[1]["otherRelationIndex"], 2)

    def test_canvas_overflow_handles_nested_rects_and_nonzero_origin(self):
        labels = [{"rect": {"x": -2, "y": 19, "width": 24, "height": 10}, "relationIndex": 3},
                  {"x": 3, "y": 4, "width": -1, "height": 1},
                  {"x": 9.6, "y": 20, "width": 10, "height": 10}]
        expected_counts = {
            (10, 20, 40, 50): 1,
            (40, 50): 1,
            (None, 50): 0,
        }
        for view_box, expected_count in expected_counts.items():
            with self.subTest(view_box=view_box):
                overflows = collect_label_canvas_overflow(labels, list(view_box))
                self.assertEqual(len(overflows), expected_count)

    def test_label_gate_messages_match_every_diagram_mode(self):
        routes = self.routes()
        label = {"relation": routes[0]["relation"], "relationIndex": 0, "label": "payload",
                 "rect": {"x": -2, "y": 58, "width": 20, "height": 10}}
        for mode, collection in (("dataflow", "flows"), ("lifecycle", "transitions"),
                                 ("architecture", "connections"), ("workflow", "edges")):
            with self.subTest(mode=mode):
                path_for = lambda r: next(p for p in routes if p["relation"]["id"] == r["id"])
                canvas = clean_label_canvas_problems([label, label], [100, 60], "showcase", "move the label", mode, collection)
                clearance = clean_label_route_clearance_problems([r["relation"] for r in routes], [label, label], None, path_for, "showcase", "move the label", diagram_type=mode, relation_collection=collection)
                self.assertTrue(len(canvas) >= 1)
                self.assertTrue(len(clearance) >= 1)
                self.assertTrue(any("payload" in d["message"] for d in canvas))
                self.assertTrue(any("payload" in d["message"] for d in clearance))

    def test_declared_reader_budget_matches_canonical_caps(self):
        b1 = declared_wide_readability_budget(1400, 600, 7.5, 7.5)
        self.assertIsNotNone(b1)
        self.assertEqual(b1["guaranteedSvgWidth"], 1346)
        self.assertEqual(b1["actualReaderWidth"], 1376)

        b2 = declared_wide_readability_budget(2000, 600, 10, 7.5)
        self.assertIsNotNone(b2)
        self.assertEqual(b2["guaranteedSvgWidth"], 1346)

        b3 = declared_wide_readability_budget(1000, 900, 8, 7.5)
        self.assertIsNone(b3)

        b4 = declared_wide_readability_budget(1400, 600, 7.5, 0)
        self.assertIsNone(b4)

    def test_final_check_uses_declared_budget_and_includes_boundary_text(self):
        text = '<g data-node-id="node"><text data-node-label="true" font-size="7.5">Node</text></g>'
        root = '<svg viewBox="0 0 1400 600" data-quality-profile="showcase"{}>' + text + '</svg>'
        self.assertTrue(check_svg(root.format(' data-reader-fit="intrinsic-height" data-reader-min-text="7.5"')))
        with self.assertRaises(DiagnosticError):
            check_svg(root.format(''))
        with self.assertRaises(DiagnosticError):
            check_svg('<svg viewBox="0 0 100 100" data-quality-profile="showcase"><text data-boundary-label="true" font-size="5">Region</text></svg>')


if __name__ == "__main__":
    unittest.main()
