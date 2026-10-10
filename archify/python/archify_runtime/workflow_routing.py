"""Native Workflow route planning and authored-geometry preservation."""

import math
from functools import cmp_to_key

from .composition import (
    collect_ambiguous_corridors,
    collect_label_route_clearance,
    forward_collinear_analysis_segments,
    source_segment_index_at_point,
)
from .flow_geometry import (
    anchor,
    automatic_port_spread,
    collect_border_runs,
    chosen_side,
    default_from_side,
    default_to_side,
    join_route_points,
    label_point,
    legacy_default_from_side,
    legacy_default_to_side,
    normalize_route_points,
    polyline_path,
    route_honors_endpoint_sides,
    route_points_value,
)
from .geometry import is_finite_point, rects_overlap, segment_intersects_rect, segment_rect_clearance
from .workflow_layout import (
    LEGACY_COLUMN_CENTERS,
    _is_integer,
    stable_value_key,
    workflow_edge_name,
    workflow_label_width,
)

EPSILON = 0.0001
READABLE_CANDIDATE_COST_PRIORITY = (
    "properCrossingCount", "sharedCorridorPx", "automaticForwardReversePx",
    "labelRouteClearanceDeficit", "interiorPreferred28Deficit", "bendCount",
    "stretchMilli", "canvasGrowthPx", "portDisplacementMilli",
    "legacyCoordinateDisplacement", "stableCandidateOrdinal",
)
OUTWARD_SIDE_VECTOR = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
READABLE_PRESET_PIN_FIELDS = {
    "straight": (), "drop": ("channelY",), "outside-right": ("channelX",),
    "return-left": ("channelX",), "bottom-channel": ("channelY",), "up-channel": ("channelY",),
}


class WorkflowLayoutFeedback(Exception):
    def __init__(self, request):
        super().__init__(f'Workflow layout requires {request["kind"]} feedback.')
        self.request = request


class SpatialGrid:
    """Same bounded uniform-grid query behavior as renderers/shared/spatial-grid.mjs."""

    def __init__(self, cell_size, max_cells=4096):
        self.cell_size, self.max_cells = cell_size, max_cells
        self.buckets, self.items, self.overflow = {}, [], []

    def _range(self, box):
        return (math.floor(box["minX"] / self.cell_size), math.floor(box["maxX"] / self.cell_size),
                math.floor(box["minY"] / self.cell_size), math.floor(box["maxY"] / self.cell_size))

    def _walkable(self, bounds):
        x0, x1, y0, y1 = bounds
        count = (x1 - x0 + 1) * (y1 - y0 + 1)
        return count <= self.max_cells and all(abs(value) <= 2 ** 53 - 1 for value in bounds)

    def insert(self, box, item):
        self.items.append(item)
        x0, x1, y0, y1 = bounds = self._range(box)
        if not self._walkable(bounds):
            self.overflow.append(item)
            return
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                self.buckets.setdefault((x, y), []).append(item)

    def query(self, box):
        x0, x1, y0, y1 = bounds = self._range(box)
        if not self._walkable(bounds):
            return list(self.items)
        found, seen = [], set()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for item in self.buckets.get((x, y), ()):
                    key = id(item)
                    if key not in seen:
                        found.append(item)
                        seen.add(key)
        for item in self.overflow:
            key = id(item)
            if key not in seen:
                found.append(item)
                seen.add(key)
        return found


def _route_bounds(points):
    return {"minX": min(point[0] for point in points), "minY": min(point[1] for point in points),
            "maxX": max(point[0] for point in points), "maxY": max(point[1] for point in points)}


def _rect_bounds(rect):
    return {"minX": rect["x"], "minY": rect["y"],
            "maxX": rect["x"] + rect["width"], "maxY": rect["y"] + rect["height"]}


def _bounds_overlap(left, right, margin=0):
    return (left["minX"] <= right["maxX"] + margin and right["minX"] <= left["maxX"] + margin
            and left["minY"] <= right["maxY"] + margin and right["minY"] <= left["maxY"] + margin)


def _point_equal(left, right):
    return abs(left[0] - right[0]) <= EPSILON and abs(left[1] - right[1]) <= EPSILON


class WorkflowRouter:
    """Stateful route planner owned by one WorkflowRenderer invocation."""

    def __init__(self, renderer):
        self.r = renderer
        self.path_cache = {}
        self._label_rect_cache = {}
        self.readable_side_cache = {}
        self.node_routes = {}
        self.obstacle_sequence = 0
        self.rightmost_routed_edge = -math.inf
        self.rightmost_node_edge = 0
        self.obstacle_grid = SpatialGrid(160)
        for node in self.r.nodes.values():
            self.obstacle_grid.insert(_rect_bounds(node), {"kind": "node", "node": node, "bounds": _rect_bounds(node)})
            self.rightmost_node_edge = max(self.rightmost_node_edge, node["x"] + node["width"])
        self._scene_obstacles = None
        self._frames = None
        self.automatic_ports = automatic_port_spread(
            self.r.workflow.get("edges", []), self.r.nodes,
            side_for=lambda edge, endpoint: self.edge_sides(edge)["fromSide" if endpoint == "source" else "toSide"],
        )

    def gap_y_between(self, from_lane, to_lane, bias=0.5):
        return self.r.lane_top(from_lane) + self.r.lane_height(from_lane) + (
            self.r.lane_top(to_lane) - self.r.lane_top(from_lane) - self.r.lane_height(from_lane)) * bias

    def same_lane_auto_via(self, start, end):
        if start[0] == end[0] or start[1] == end[1]:
            return []
        mid_x = (start[0] + end[0]) / 2
        return [[mid_x, start[1]], [mid_x, end[1]]]

    def route_bounds(self, points):
        return _route_bounds(points)

    def route_clears_unrelated_nodes(self, edge, points, clearance=2):
        endpoint_ids = {edge.get("from"), edge.get("to")}
        extent = _route_bounds(points)
        query_box = {key: value + (-clearance if key.startswith("min") else clearance) for key, value in extent.items()}
        for item in self.obstacle_grid.query(query_box):
            if item["kind"] != "node":
                continue
            node = item["node"]
            if node.get("id") in endpoint_ids or not _bounds_overlap(extent, _rect_bounds(node), clearance):
                continue
            if any(segment_intersects_rect(points[index], points[index + 1], node, clearance)
                   for index in range(len(points) - 1)):
                return False
        return True

    def first_route_node_collision(self, edge, points):
        last = len(points) - 2
        for node in self.r.nodes.values():
            role = "source-endpoint" if node.get("id") == edge.get("from") else (
                "target-endpoint" if node.get("id") == edge.get("to") else "unrelated")
            for index in range(last + 1):
                if role == "source-endpoint" and index == 0 or role == "target-endpoint" and index == last:
                    continue
                clearance = 2 if role == "unrelated" else 0
                start, end = points[index], points[index + 1]
                if segment_intersects_rect(start, end, node, clearance):
                    return {"obstacleNode": node["id"], "obstacleRole": role, "segmentIndex": index,
                            "from": list(start), "to": list(end), "clearancePx": clearance}
        return None

    def one_bend_cross_lane_via(self, edge, start, end, from_side, to_side):
        from_vertical = from_side in ("top", "bottom")
        to_vertical = to_side in ("top", "bottom")
        if from_vertical == to_vertical:
            return None
        corner = [start[0], end[1]] if from_vertical else [end[0], start[1]]
        points = normalize_route_points([start, corner, end])
        if len(points) != 3 or not route_honors_endpoint_sides(points, from_side, to_side):
            return None
        if any(math.hypot(points[i + 1][0] - point[0], points[i + 1][1] - point[1]) < 8
               for i, point in enumerate(points[:-1])) or not self.route_clears_unrelated_nodes(edge, points):
            return None
        return points[1:-1]

    def outward_stub(self, point, side, distance=16):
        dx, dy = OUTWARD_SIDE_VECTOR.get(side, (0, 0))
        return [point[0] + dx * distance, point[1] + dy * distance]

    def orthogonal_route(self, points):
        for index, point in enumerate(points):
            if not isinstance(point, (list, tuple)) or len(point) != 2 or not is_finite_point(*point):
                return False
            if index == 0:
                continue
            previous = points[index - 1]
            dx, dy = abs(point[0] - previous[0]), abs(point[1] - previous[1])
            if (dx <= EPSILON) == (dy <= EPSILON):
                return False
        return True

    def route_clears_endpoint_nodes(self, points, source, target):
        last = len(points) - 2
        for index in range(last + 1):
            if index > 0 and segment_intersects_rect(points[index], points[index + 1], source):
                return False
            if index < last and segment_intersects_rect(points[index], points[index + 1], target):
                return False
        return True

    def route_meets_hard_rhythm(self, points):
        if len(points) == 2:
            return math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]) + EPSILON >= 28
        for index, point in enumerate(points[:-1]):
            end = points[index + 1]
            length = abs(end[0] - point[0]) + abs(end[1] - point[1])
            minimum = 8 if index in (0, len(points) - 2) else 16
            if length + EPSILON < minimum:
                return False
        return True

    def candidate_label_rect(self, edge, points):
        if not edge.get("label"):
            return None
        lx, ly = self.edge_label_point(edge, points)
        width = workflow_label_width(edge["label"])
        return {"x": lx - width / 2, "y": ly - 10, "width": width, "height": 14}

    def edge_label_point(self, edge, points):
        version = self.r.workflow.get("schema_version")
        if version == 1:
            if edge.get("labelAt") or _is_integer(edge.get("labelSegment")) or len(points) != 3:
                return label_point(edge, points)
            lengths = [math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]) for i in range(2)]
            segment = 0 if lengths[0] >= lengths[1] else 1
            result = label_point({**edge, "labelSegment": segment}, points)
            if points[segment][0] == points[segment + 1][0]:
                result[1] += 10
            return result
        if (not edge.get("labelAt") and not _is_integer(edge.get("labelSegment"))
                and "labelDx" not in edge and "labelDy" not in edge and len(points) == 2
                and points[0][0] == points[1][0]):
            return [points[0][0], (points[0][1] + points[1][1]) / 2]
        if edge.get("labelAt") or _is_integer(edge.get("labelSegment")) or len(points) <= 2:
            return label_point(edge, points)
        segments = []
        for index, point in enumerate(points[:-1]):
            end = points[index + 1]
            segments.append({"index": index, "horizontal": abs(end[1] - point[1]) <= EPSILON,
                             "length": math.hypot(end[0] - point[0], end[1] - point[1])})
        segments.sort(key=lambda item: (-int(item["horizontal"]), -item["length"], item["index"]))
        segment = segments[0]["index"] if segments else 0
        result = label_point({**edge, "labelSegment": segment}, points)
        if points[segment][0] == points[segment + 1][0]:
            result[1] += 10
        return result

    def route_label_clears_nodes(self, edge, points):
        if not edge.get("label") or edge.get("labelAt"):
            return True
        rect = self.candidate_label_rect(edge, points)
        query = {"minX": rect["x"] + 2, "minY": rect["y"] + 2,
                 "maxX": rect["x"] + rect["width"] - 2, "maxY": rect["y"] + rect["height"] - 2}
        return not any(item["kind"] == "node" and rects_overlap(rect, item["node"], -2)
                       for item in self.obstacle_grid.query(query))

    def label_route_clearance_deficit(self, edge, points, threshold=8):
        candidate_label = self.candidate_label_rect(edge, points)
        extent = _route_bounds(points)
        if candidate_label:
            label_bounds = _rect_bounds(candidate_label)
            extent = {"minX": min(extent["minX"], label_bounds["minX"]),
                      "minY": min(extent["minY"], label_bounds["minY"]),
                      "maxX": max(extent["maxX"], label_bounds["maxX"]),
                      "maxY": max(extent["maxY"], label_bounds["maxY"])}
        query = {"minX": extent["minX"] - threshold, "minY": extent["minY"] - threshold,
                 "maxX": extent["maxX"] + threshold, "maxY": extent["maxY"] + threshold}
        items = [item for item in self.obstacle_grid.query(query) if item["kind"] in ("route", "label")]
        items.sort(key=lambda item: (item["sequence"], 0 if item["kind"] == "route" else 1))
        deficit = 0
        for item in items:
            if item["kind"] == "route":
                if not candidate_label:
                    continue
                for start, end in zip(item["routed"]["points"], item["routed"]["points"][1:]):
                    clearance = segment_rect_clearance(start, end, candidate_label)
                    if clearance is not None:
                        deficit += max(0, threshold - clearance)
            elif candidate_label or not edge.get("label"):
                for start, end in zip(points, points[1:]):
                    clearance = segment_rect_clearance(start, end, item["rect"])
                    if clearance is not None:
                        deficit += max(0, threshold - clearance)
        return deficit

    def route_clears_placed_labels(self, edge, points):
        label = self.candidate_label_rect(edge, points)
        extent = _route_bounds(points)
        query = {"minX": extent["minX"] - 4, "minY": extent["minY"] - 4,
                 "maxX": extent["maxX"] + 4, "maxY": extent["maxY"] + 4}
        if label:
            query = {"minX": min(query["minX"], label["x"] - 4), "minY": min(query["minY"], label["y"] - 4),
                     "maxX": max(query["maxX"], label["x"] + label["width"] + 4),
                     "maxY": max(query["maxY"], label["y"] + label["height"] + 4)}
        for item in self.obstacle_grid.query(query):
            if item["kind"] == "label":
                rect = item["rect"]
                if label and rects_overlap(label, rect, -2):
                    return False
                if (max(rect["x"] - extent["maxX"], extent["minX"] - (rect["x"] + rect["width"])) >= 4
                        or max(rect["y"] - extent["maxY"], extent["minY"] - (rect["y"] + rect["height"])) >= 4):
                    continue
                if any((clearance := segment_rect_clearance(start, end, rect)) is not None and clearance + EPSILON < 4
                       for start, end in zip(points, points[1:])):
                    return False
            elif item["kind"] == "route" and label:
                other = item["bounds"]
                if (max(label["x"] - other["maxX"], other["minX"] - (label["x"] + label["width"])) >= 4
                        or max(label["y"] - other["maxY"], other["minY"] - (label["y"] + label["height"])) >= 4):
                    continue
                if any((clearance := segment_rect_clearance(start, end, label)) is not None and clearance + EPSILON < 4
                       for start, end in zip(item["routed"]["points"], item["routed"]["points"][1:])):
                    return False
        return True

    def route_clears_legend(self, edge, points):
        if not self.r.legend_entries:
            return True
        label = self.candidate_label_rect(edge, points)
        for rect in self.r.workflow_legend_rects():
            if any(segment_intersects_rect(start, end, rect) for start, end in zip(points, points[1:])):
                return False
            if label and rects_overlap(label, rect):
                return False
        return True

    def scene_label_obstacles(self):
        if self._scene_obstacles is None:
            self._scene_obstacles = self.r.workflow_scene_label_obstacles()
            for obstacle in self._scene_obstacles:
                self.obstacle_grid.insert(_rect_bounds(obstacle), {"kind": "scene", "obstacle": obstacle})
        return self._scene_obstacles

    def route_clears_scene_labels(self, edge, points):
        self.scene_label_obstacles()
        label = self.candidate_label_rect(edge, points)
        extent = _route_bounds(points)
        query = dict(extent)
        if label:
            query = {"minX": min(query["minX"], label["x"]), "minY": min(query["minY"], label["y"]),
                     "maxX": max(query["maxX"], label["x"] + label["width"]),
                     "maxY": max(query["maxY"], label["y"] + label["height"])}
        for item in self.obstacle_grid.query(query):
            if item["kind"] != "scene":
                continue
            obstacle = item["obstacle"]
            if any(segment_intersects_rect(start, end, obstacle) for start, end in zip(points, points[1:])):
                return False
            if label and rects_overlap(label, obstacle):
                return False
        return True

    def composition_frames(self):
        if self._frames is None:
            self._frames = self.r.workflow_composition_frames()
        return self._frames

    def route_clears_frame_borders(self, points):
        extent = _route_bounds(points)
        frames = [frame for frame in self.composition_frames() if _bounds_overlap(_rect_bounds(frame), extent)]
        return not frames or not collect_border_runs([{"points": points}], frames)

    def route_fits_canvas_origin(self, edge, points):
        coordinates = list(points)
        if not edge.get("labelAt"):
            label = self.candidate_label_rect(edge, points)
            if label:
                coordinates.extend(([label["x"], label["y"]], [label["x"] + label["width"], label["y"] + label["height"]]))
        return all(x >= 0 and y >= 0 for x, y in coordinates)

    def readable_candidate_is_feasible(self, edge, points, source, target, from_side, to_side):
        return (len(points) >= 2 and self.orthogonal_route(points) and self.route_meets_hard_rhythm(points)
                and route_honors_endpoint_sides(points, from_side, to_side)
                and self.route_clears_endpoint_nodes(points, source, target)
                and self.route_label_clears_nodes(edge, points)
                and self.route_clears_scene_labels(edge, points)
                and self.route_clears_unrelated_nodes(edge, points)
                and self.route_clears_placed_labels(edge, points)
                and self.route_fits_canvas_origin(edge, points)
                and self.route_clears_frame_borders(points)
                and self.route_clears_legend(edge, points))

    def corridor_via_y(self, start, end, from_side, to_side, y):
        source_stub, target_stub = self.outward_stub(start, from_side), self.outward_stub(end, to_side)
        return [source_stub, [source_stub[0], y], [target_stub[0], y], target_stub]

    def corridor_via_x(self, start, end, from_side, to_side, x):
        source_stub, target_stub = self.outward_stub(start, from_side), self.outward_stub(end, to_side)
        return [source_stub, [x, source_stub[1]], [x, target_stub[1]], target_stub]

    @staticmethod
    def proper_axis_crossing(a, b, c, d):
        first_horizontal = abs(a[1] - b[1]) <= EPSILON
        second_horizontal = abs(c[1] - d[1]) <= EPSILON
        if first_horizontal == second_horizontal:
            return False
        horizontal, vertical = ((a, b), (c, d)) if first_horizontal else ((c, d), (a, b))
        x, y = vertical[0][0], horizontal[0][1]
        return (x > min(horizontal[0][0], horizontal[1][0]) + EPSILON
                and x < max(horizontal[0][0], horizontal[1][0]) - EPSILON
                and y > min(vertical[0][1], vertical[1][1]) + EPSILON
                and y < max(vertical[0][1], vertical[1][1]) - EPSILON)

    def independent_automatic_route(self, edge):
        return (self.r.workflow.get("schema_version") == 2 and not edge.get("via")
                and "channelX" not in edge and "channelY" not in edge and not edge.get("labelAt")
                and edge.get("route") in (None, "auto") and edge.get("fromSide") in (None, "auto")
                and edge.get("toSide") in (None, "auto"))

    def route_interaction_metrics(self, edge, points):
        crossings, shared_corridor = 0, 0
        extent = _route_bounds(points)
        query = {"minX": extent["minX"] - 8, "minY": extent["minY"] - 8,
                 "maxX": extent["maxX"] + 8, "maxY": extent["maxY"] + 8}
        nearby = [item for item in self.obstacle_grid.query(query) if item["kind"] == "route"]
        nearby.sort(key=lambda item: item["sequence"])
        version = self.r.workflow.get("schema_version")
        for item in nearby:
            other, other_points = item["edge"], item["routed"]["points"]
            shared_endpoint = bool({edge.get("from"), edge.get("to")} & {other.get("from"), other.get("to")})
            if shared_endpoint and version != 2:
                continue
            hits = collect_ambiguous_corridors(
                [{"relation": edge, "points": points}, {"relation": other, "points": other_points}],
                include_shared_endpoints=(lambda _a, _b: version == 2),
                allow_short_workflow_trunks=(version == 2),
            )
            shared_corridor += sum(hit["overlapLength"] for hit in hits)
            for left_index, (left_start, left_end) in enumerate(zip(points, points[1:])):
                for right_start, right_end in zip(other_points, other_points[1:]):
                    if self.proper_axis_crossing(left_start, left_end, right_start, right_end):
                        crossings += 1
        return {"properCrossingCount": crossings, "sharedCorridorPx": shared_corridor}

    def automatic_forward_reverse_px(self, edge, points):
        source, target = self.r.nodes.get(edge.get("from")), self.r.nodes.get(edge.get("to"))
        if not source or not target or edge.get("role") in ("return", "error") or target.get("col", 0) <= source.get("col", 0):
            return 0
        return sum(max(0, start[0] - end[0]) for start, end in zip(points, points[1:]))

    def readable_candidate_cost(self, edge, points, ordinal, natural_from_side, natural_to_side):
        interaction = self.route_interaction_metrics(edge, points)
        lengths = [abs(end[0] - start[0]) + abs(end[1] - start[1]) for start, end in zip(points, points[1:])]
        route_length = sum(lengths)
        direct = abs(points[-1][0] - points[0][0]) + abs(points[-1][1] - points[0][1])
        interior_deficit = sum(max(0, 28 - length) for length in lengths[1:-1])
        min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
        min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
        growth = (max(0, -min_x) + max(0, max_x - self.r.minimum_canvas_width)
                  + max(0, -min_y) + max(0, max_y - self.r.auto_height))
        source, target = self.r.nodes[edge["from"]], self.r.nodes[edge["to"]]
        natural_start, natural_end = anchor(source, natural_from_side), anchor(target, natural_to_side)
        port_displacement = sum((abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in
                                 ((points[0], natural_start), (points[-1], natural_end))))
        legacy_displacement = abs(source["cx"] - LEGACY_COLUMN_CENTERS[source["col"]]) \
            + abs(target["cx"] - LEGACY_COLUMN_CENTERS[target["col"]])
        return {
            "automaticForwardReversePx": self.automatic_forward_reverse_px(edge, points),
            "properCrossingCount": interaction["properCrossingCount"],
            "sharedCorridorPx": interaction["sharedCorridorPx"],
            "labelRouteClearanceDeficit": self.label_route_clearance_deficit(edge, points),
            "interiorPreferred28Deficit": interior_deficit,
            "bendCount": max(0, len(points) - 2),
            "stretchMilli": math.floor((route_length / direct if direct > 0 else 1) * 1000 + 0.5),
            "canvasGrowthPx": growth,
            "portDisplacementMilli": math.floor(port_displacement * 1000 + 0.5),
            "legacyCoordinateDisplacement": legacy_displacement,
            "stableCandidateOrdinal": ordinal,
        }

    @staticmethod
    def compare_cost(left, right):
        for dimension in READABLE_CANDIDATE_COST_PRIORITY:
            a, b = left.get(dimension) or 0, right.get(dimension) or 0
            if a != b:
                return -1 if a < b else 1
        return 0

    def readable_automatic_candidate_set(self, edge, source, target, start, end, from_side, to_side,
                                         ordinal_offset=0, natural_from_side=None, natural_to_side=None):
        natural_from_side = natural_from_side or from_side
        natural_to_side = natural_to_side or to_side
        mid_x = (start[0] + end[0]) / 2
        lane_gap_y = self.r.lane_top(source["lane"]) - 16 if source["lane"] == target["lane"] \
            else self.gap_y_between(source["lane"], target["lane"], edge.get("bias", 0.5))
        top_y = max(8, min(self.r.lane_top(source["lane"]), self.r.lane_top(target["lane"])) - 16)
        bottom_y = max(self.r.lane_top(source["lane"]) + self.r.lane_height(source["lane"]),
                       self.r.lane_top(target["lane"]) + self.r.lane_height(target["lane"])) + 16
        layout = self.r.layout
        outside_left, outside_right = layout["laneX"] - 20, layout["laneX"] + layout["laneW"] + 12
        raw = [
            {"family": "facing-straight", "via": []},
            {"family": "horizontal-then-vertical", "via": [[end[0], start[1]]]},
            {"family": "vertical-then-horizontal", "via": [[start[0], end[1]]]},
            {"family": "lane-gap-corridor", "via": self.corridor_via_y(start, end, from_side, to_side, lane_gap_y)},
            {"family": "column-gap-corridor", "via": self.corridor_via_x(start, end, from_side, to_side, mid_x)},
            {"family": "outside-left", "via": self.corridor_via_x(start, end, from_side, to_side, outside_left)},
            {"family": "outside-right", "via": self.corridor_via_x(start, end, from_side, to_side, outside_right)},
            {"family": "top-corridor", "via": self.corridor_via_y(start, end, from_side, to_side, top_y)},
            {"family": "bottom-corridor", "via": self.corridor_via_y(start, end, from_side, to_side, bottom_y)},
        ]
        candidates = []
        for ordinal, candidate in enumerate(raw):
            points = join_route_points(start, candidate["via"], end)
            if self.readable_candidate_is_feasible(edge, points, source, target, from_side, to_side):
                index = ordinal_offset + ordinal
                candidates.append({**candidate, "ordinal": index, "points": points,
                                   "cost": self.readable_candidate_cost(edge, points, index,
                                                                        natural_from_side, natural_to_side)})
        candidates.sort(key=cmp_to_key(lambda left, right: self.compare_cost(left["cost"], right["cost"])))
        return {"rawCandidates": raw, "candidates": candidates, "outsideRight": outside_right}

    def readable_automatic_via(self, edge, source, target, start, end, from_side, to_side):
        planned = self.readable_automatic_candidate_set(edge, source, target, start, end, from_side, to_side)
        raw, candidates, outside_right = planned["rawCandidates"], planned["candidates"], planned["outsideRight"]
        if candidates:
            return candidates[0]["points"][1:-1]
        current_points = join_route_points(start, next(c["via"] for c in raw if c["family"] == "outside-right"), end)
        label_rect = self.candidate_label_rect(edge, current_points)
        outside_min_x = outside_right
        if label_rect:
            label_query = {"minX": label_rect["x"] - 4, "minY": label_rect["y"] - 4,
                           "maxX": label_rect["x"] + label_rect["width"] + 4,
                           "maxY": label_rect["y"] + label_rect["height"] + 4}
            for item in self.obstacle_grid.query(label_query):
                if item["kind"] == "node":
                    rect = item["node"]
                    if not rects_overlap(label_rect, rect, -2):
                        continue
                    deficit = rect["x"] + rect["width"] - 2 - label_rect["x"]
                    if deficit > 0:
                        outside_min_x = max(outside_min_x, outside_right + deficit * 2)
                elif item["kind"] == "label":
                    rect = item["rect"]
                    if not rects_overlap(label_rect, rect, -2):
                        continue
                    deficit = rect["x"] + rect["width"] - 2 - label_rect["x"]
                    if deficit > 0:
                        outside_min_x = max(outside_min_x, outside_right + deficit * 2)
                elif item["kind"] == "route":
                    other_points = item["routed"]["points"]
                    for a, b in zip(other_points, other_points[1:]):
                        clearance = segment_rect_clearance(a, b, label_rect)
                        if clearance is None or clearance + EPSILON >= 4:
                            continue
                        deficit = max(a[0], b[0]) + 4 - label_rect["x"]
                        if deficit > 0:
                            outside_min_x = max(outside_min_x, outside_right + deficit * 2)
        outside_min_x = math.ceil(outside_min_x * 1000) / 1000
        rightmost = max(outside_right, self.rightmost_node_edge, self.rightmost_routed_edge)
        growth = max(32, label_rect["width"] if label_rect else 0, rightmost + 16 - outside_min_x)
        last_infeasible = outside_right
        for _ in range(7):
            if outside_min_x > outside_right + EPSILON:
                expanded = join_route_points(start, self.corridor_via_x(start, end, from_side, to_side, outside_min_x), end)
                if self.readable_candidate_is_feasible(edge, expanded, source, target, from_side, to_side):
                    feasible_x, feasible_points, infeasible_x = outside_min_x, expanded, last_infeasible
                    for _refinement in range(53):
                        if feasible_x - infeasible_x <= 0.001:
                            break
                        midpoint = math.ceil(((infeasible_x + feasible_x) / 2) * 1000) / 1000
                        if midpoint >= feasible_x - EPSILON:
                            break
                        points = join_route_points(start, self.corridor_via_x(start, end, from_side, to_side, midpoint), end)
                        if self.readable_candidate_is_feasible(edge, points, source, target, from_side, to_side):
                            feasible_x, feasible_points = midpoint, points
                        else:
                            infeasible_x = midpoint
                    return feasible_points[1:-1]
                last_infeasible = outside_min_x
            outside_min_x = math.ceil((outside_min_x + growth) * 1000) / 1000
            growth *= 2
        self._automatic_route_failure(edge, source, target, start, end, from_side, to_side, raw)

    def _automatic_route_failure(self, edge, source, target, start, end, from_side, to_side, raw):
        has_pin_context = bool(edge.get("labelAt")) or any(
            other.get("labelAt") or other.get("via") is not None or "channelX" in other or "channelY" in other
            for other in self.r.edges if id(other) in self.path_cache)
        if has_pin_context:
            self.classify_failed_automatic_candidate_pins(edge, raw)
        facing = ((from_side == "right" and to_side == "left" and end[0] > start[0])
                  or (from_side == "left" and to_side == "right" and start[0] > end[0]))
        if facing and source.get("col") != target.get("col"):
            from_col, to_col = sorted((source["col"], target["col"]))
            required = source["width"] / 2 + 32 + target["width"] / 2
            actual = self.r.layout["colXs"][to_col] - self.r.layout["colXs"][from_col]
            if actual + EPSILON < required:
                raise WorkflowLayoutFeedback({"kind": "rank-gap-minimum", "fromCol": from_col, "toCol": to_col,
                                             "minimum": math.ceil(required * 1000) / 1000,
                                             "edge": edge.get("id"), "from": edge.get("from"), "to": edge.get("to"),
                                             "attemptedCandidateFamilies": [item["family"] for item in raw],
                                             "candidateCount": len(raw)})
        if source.get("lane") != target.get("lane") and self.r.layout["laneGap"] < 32:
            raise WorkflowLayoutFeedback({"kind": "lane-gap-minimum", "minimum": 32, "edge": edge.get("id"),
                                         "from": edge.get("from"), "to": edge.get("to"),
                                         "attemptedCandidateFamilies": [item["family"] for item in raw],
                                         "candidateCount": len(raw)})
        from .common import DiagnosticError, diagnostic
        message = f'Workflow edge "{workflow_edge_name(edge)}" has no feasible readable-v2 automatic route.'
        raise DiagnosticError(message, [diagnostic(
            "workflow/solver-budget-exhausted", message,
            {"diagramType": "workflow", "edge": edge.get("id"), "from": edge.get("from"), "to": edge.get("to")},
            {"attemptedCandidateFamilies": [item["family"] for item in raw], "candidateCount": len(raw)}, [])])

    def readable_preset_via(self, edge, source, target, start, end, from_side, to_side):
        preset = edge.get("route")
        if preset == "straight":
            via = []
        elif preset == "drop":
            y = self.gap_y_between(source["lane"], target["lane"], edge.get("bias", 0.5))
            via = [[start[0], y], [end[0], y]]
        elif preset == "outside-right":
            x = self.r.layout["laneX"] + self.r.layout["laneW"] + 12
            via = [[x, start[1]], [x, end[1]]]
        elif preset == "return-left":
            x = min(source["x"], target["x"]) - 28
            via = [[x, start[1]], [x, end[1]]]
        elif preset == "bottom-channel":
            y = max(source["y"] + source["height"], target["y"] + target["height"]) + 32
            via = [[start[0], y], [end[0], y]]
        elif preset == "up-channel":
            y = min(source["y"], target["y"]) - 28
            via = [[start[0], y], [end[0], y]]
        else:
            return self.readable_automatic_via(edge, source, target, start, end, from_side, to_side)
        points = join_route_points(start, via, end)
        if (self.readable_candidate_is_feasible(edge, points, source, target, from_side, to_side)
                and self.route_matches_preset_family(preset, points, source, target)):
            return points[1:-1]
        self.r.raise_preset_conflict(edge, preset, points, from_side, to_side)

    def route_via(self, edge, source, target, start, end, from_side, to_side, validate_preset=True):
        if edge.get("via") is not None:
            return edge["via"]
        has_pins = "channelX" in edge or "channelY" in edge
        route = edge.get("route") or "auto"
        if (self.r.workflow.get("schema_version") == 2 and route != "auto" and not has_pins and validate_preset):
            return self.readable_preset_via(edge, source, target, start, end, from_side, to_side)
        if route == "straight":
            return []
        if route == "drop":
            y = edge.get("channelY") if edge.get("channelY") is not None else self.gap_y_between(source["lane"], target["lane"], self._bias(edge))
            return [[start[0], y], [end[0], y]]
        if route == "outside-right":
            x = edge.get("channelX", self.r.layout["laneX"] + self.r.layout["laneW"] + 12)
            return [[x, start[1]], [x, end[1]]]
        if route == "return-left":
            x = edge.get("channelX", min(source["x"], target["x"]) - 28)
            return [[x, start[1]], [x, end[1]]]
        if route == "bottom-channel":
            y = edge.get("channelY", max(source["y"] + source["height"], target["y"] + target["height"]) + 32)
            return [[start[0], y], [end[0], y]]
        if route == "up-channel":
            y = edge.get("channelY", min(source["y"], target["y"]) - 28)
            return [[start[0], y], [end[0], y]]
        if self.r.workflow.get("schema_version") == 2:
            if "channelX" in edge and "channelY" in edge:
                return [[edge["channelX"], start[1]], [edge["channelX"], edge["channelY"]], [end[0], edge["channelY"]]]
            if "channelX" in edge:
                return [[edge["channelX"], start[1]], [edge["channelX"], end[1]]]
            if "channelY" in edge:
                return [[start[0], edge["channelY"]], [end[0], edge["channelY"]]]
            return self.readable_automatic_via(edge, source, target, start, end, from_side, to_side)
        if source["lane"] == target["lane"]:
            return self.same_lane_auto_via(start, end)
        one_bend = self.one_bend_cross_lane_via(edge, start, end, from_side, to_side)
        if one_bend is not None:
            return one_bend
        y = self.gap_y_between(source["lane"], target["lane"], edge.get("bias", 0.5))
        return [[start[0], y], [end[0], y]]

    @staticmethod
    def _bias(edge):
        return edge.get("bias") if edge.get("bias") is not None else 0.5

    @staticmethod
    def _segment_orientation(start, end):
        if abs(start[0] - end[0]) <= EPSILON:
            return "vertical"
        if abs(start[1] - end[1]) <= EPSILON:
            return "horizontal"
        return "diagonal"

    def _route_segments(self, points):
        return [{"start": start, "end": end, "orientation": self._segment_orientation(start, end)}
                for start, end in zip(points, points[1:])]

    def endpoint_side_is_honored(self, points, side, endpoint):
        if not side or side == "auto" or len(points) < 2:
            return True
        source = endpoint == "source"
        a, b = (points[0], points[1]) if source else (points[-2], points[-1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        if source:
            return {"right": dx > 0 and abs(dy) <= EPSILON, "left": dx < 0 and abs(dy) <= EPSILON,
                    "bottom": dy > 0 and abs(dx) <= EPSILON, "top": dy < 0 and abs(dx) <= EPSILON}.get(side, False)
        return {"right": dx < 0 and abs(dy) <= EPSILON, "left": dx > 0 and abs(dy) <= EPSILON,
                "bottom": dy < 0 and abs(dx) <= EPSILON, "top": dy > 0 and abs(dx) <= EPSILON}.get(side, False)

    def _corridor_topology_matches(self, points, axis, coordinate):
        collapsed = normalize_route_points([list(point) for point in points])
        start, end = collapsed[0], collapsed[-1]
        via = [[coordinate, start[1]], [coordinate, end[1]]] if axis == "x" else [[start[0], coordinate], [end[0], coordinate]]
        expected = join_route_points(start, via, end)
        actual_pattern = [segment["orientation"] for segment in self._route_segments(collapsed)]
        expected_pattern = [segment["orientation"] for segment in self._route_segments(expected)]
        return actual_pattern == expected_pattern and self.route_contains_channel_pin(
            collapsed, "channelX" if axis == "x" else "channelY", coordinate)

    def route_matches_preset_family(self, preset, points, source, target):
        collapsed = normalize_route_points([list(point) for point in points])
        segments = self._route_segments(collapsed)
        if preset == "straight":
            return len(collapsed) == 2
        if preset == "drop":
            if source["lane"] == target["lane"]:
                return False
            if len(collapsed) == 2 and segments[0]["orientation"] == "vertical":
                return True
            upper, lower = (source, target) if source["cy"] <= target["cy"] else (target, source)
            return any(segment["orientation"] == "horizontal"
                       and segment["start"][1] >= upper["y"] + upper["height"] - EPSILON
                       and segment["start"][1] <= lower["y"] + EPSILON
                       and self._corridor_topology_matches(points, "y", segment["start"][1])
                       for segment in segments)
        if preset in ("outside-right", "return-left"):
            boundary = max(source["x"] + source["width"], target["x"] + target["width"]) if preset == "outside-right" else min(source["x"], target["x"])
            return any(segment["orientation"] == "vertical"
                       and (segment["start"][0] > boundary + EPSILON if preset == "outside-right" else segment["start"][0] < boundary - EPSILON)
                       and self._corridor_topology_matches(points, "x", segment["start"][0]) for segment in segments)
        if preset in ("bottom-channel", "up-channel"):
            boundary = max(source["y"] + source["height"], target["y"] + target["height"]) if preset == "bottom-channel" else min(source["y"], target["y"])
            return any(segment["orientation"] == "horizontal"
                       and (segment["start"][1] > boundary + EPSILON if preset == "bottom-channel" else segment["start"][1] < boundary - EPSILON)
                       and self._corridor_topology_matches(points, "y", segment["start"][1]) for segment in segments)
        return False

    @staticmethod
    def route_contains_channel_pin(points, field, value):
        for start, end in zip(points, points[1:]):
            if field == "channelX" and start[0] == value and end[0] == value and abs(end[1] - start[1]) > EPSILON:
                return True
            if field == "channelY" and start[1] == value and end[1] == value and abs(end[0] - start[0]) > EPSILON:
                return True
        return False

    @staticmethod
    def present_channel_pins(edge):
        return [field for field in ("channelX", "channelY") if field in edge]

    def _legacy_automatic_one_bend_sides(self, edge, source, target):
        automatic_route = edge.get("via") is None and edge.get("route") in (None, "auto")
        automatic_from = edge.get("fromSide") in (None, "auto")
        automatic_to = edge.get("toSide") in (None, "auto")
        if not automatic_route or not automatic_from or not automatic_to or source["lane"] == target["lane"]:
            return None
        if source["cx"] == target["cx"] or source["cy"] == target["cy"]:
            return None
        vertical_from = "top" if target["cy"] < source["cy"] else "bottom"
        horizontal_to = "right" if target["cx"] < source["cx"] else "left"
        horizontal_from = "left" if target["cx"] < source["cx"] else "right"
        vertical_to = "bottom" if target["cy"] < source["cy"] else "top"
        candidates = ({"fromSide": vertical_from, "toSide": horizontal_to},
                      {"fromSide": horizontal_from, "toSide": vertical_to})
        for pair in candidates:
            start, end = anchor(source, pair["fromSide"]), anchor(target, pair["toSide"])
            if self.one_bend_cross_lane_via(edge, start, end, pair["fromSide"], pair["toSide"]) is not None:
                return pair
        return None

    def _readable_automatic_sides(self, edge, source, target):
        automatic_route = (edge.get("via") is None and "channelX" not in edge and "channelY" not in edge
                           and edge.get("route") in (None, "auto"))
        authored_from = edge.get("fromSide") if edge.get("fromSide") not in (None, "auto") else None
        authored_to = edge.get("toSide") if edge.get("toSide") not in (None, "auto") else None
        if not automatic_route or authored_from and authored_to:
            return None
        if id(edge) in self.readable_side_cache:
            return self.readable_side_cache[id(edge)]
        preferred = []
        legacy = self._legacy_automatic_one_bend_sides(edge, source, target)
        if legacy:
            preferred.append(legacy)
        preferred.append({"fromSide": authored_from or default_from_side(source, target),
                          "toSide": authored_to or default_to_side(source, target)})
        side_order = ("right", "bottom", "left", "top")
        preferred.extend({"fromSide": fs, "toSide": ts}
                         for fs in ((authored_from,) if authored_from else side_order)
                         for ts in ((authored_to,) if authored_to else side_order))
        seen, side_pairs = set(), []
        for candidate in preferred:
            if authored_from and candidate["fromSide"] != authored_from or authored_to and candidate["toSide"] != authored_to:
                continue
            key = (candidate["fromSide"], candidate["toSide"])
            if key not in seen:
                seen.add(key)
                side_pairs.append(candidate)
        natural_from, natural_to = authored_from or default_from_side(source, target), authored_to or default_to_side(source, target)
        primary = side_pairs[0] if side_pairs else None
        if primary:
            start, end = anchor(source, primary["fromSide"]), anchor(target, primary["toSide"])
            planned = self.readable_automatic_candidate_set(edge, source, target, start, end,
                                                            primary["fromSide"], primary["toSide"],
                                                            natural_from_side=natural_from, natural_to_side=natural_to)
            if planned["candidates"]:
                self.readable_side_cache[id(edge)] = primary
                return primary
        candidates = []
        for ordinal, candidate in enumerate(side_pairs):
            start, end = anchor(source, candidate["fromSide"]), anchor(target, candidate["toSide"])
            planned = self.readable_automatic_candidate_set(edge, source, target, start, end,
                                                            candidate["fromSide"], candidate["toSide"],
                                                            ordinal_offset=ordinal * 9,
                                                            natural_from_side=natural_from, natural_to_side=natural_to)
            candidates.extend({**item, **candidate} for item in planned["candidates"])
        candidates.sort(key=cmp_to_key(lambda a, b: self.compare_cost(a["cost"], b["cost"])))
        selected = {key: candidates[0][key] for key in ("fromSide", "toSide")} if candidates else None
        self.readable_side_cache[id(edge)] = selected
        return selected

    def automatic_one_bend_sides(self, edge, source, target):
        if self.r.workflow.get("schema_version") == 2:
            return self._readable_automatic_sides(edge, source, target)
        return self._legacy_automatic_one_bend_sides(edge, source, target)

    def edge_sides(self, edge):
        source, target = self.r.nodes.get(edge.get("from")), self.r.nodes.get(edge.get("to"))
        if not source or not target:
            return {"fromSide": chosen_side(edge.get("fromSide"), "right"),
                    "toSide": chosen_side(edge.get("toSide"), "left")}
        resolved = self.readable_side_cache.get(id(edge)) if self.r.workflow.get("schema_version") == 2 else None
        if resolved:
            return resolved
        one_bend = self.automatic_one_bend_sides(edge, source, target)
        if one_bend:
            return one_bend
        if (self.r.workflow.get("schema_version") == 2
                and stable_value_key(edge) in self.r.layout.get("channelLabelEdgeKeys", set())
                and not edge.get("fromSide") and not edge.get("toSide")):
            return {"fromSide": "top", "toSide": "top"}
        return {"fromSide": chosen_side(edge.get("fromSide"), default_from_side(source, target)),
                "toSide": chosen_side(edge.get("toSide"), default_to_side(source, target))}

    def automatic_port_candidates(self, edge, node, side, preferred, counterpart):
        if not self.independent_automatic_route(edge):
            return [preferred]
        vertical = side in ("left", "right")
        axis = 1 if vertical else 0
        center = anchor(node, side)
        occupied = []
        for other, routed in self.node_routes.get(node["id"], []):
            for endpoint in ("from", "to"):
                if other.get(endpoint) != node["id"]:
                    continue
                point = routed["points"][0] if endpoint == "from" else routed["points"][-1]
                own_width = edge.get("width") or (1.8 if edge.get("variant") == "emphasis" else 1.4)
                other_width = other.get("width") or (1.8 if other.get("variant") == "emphasis" else 1.4)
                if abs(point[1 - axis] - center[1 - axis]) < EPSILON:
                    occupied.append((point[axis], max(12, 3.5 * (own_width + other_width))))
        clear = lambda point: all(abs(coordinate - point[axis]) >= clearance - EPSILON
                                  for coordinate, clearance in occupied)
        if clear(preferred):
            return [preferred]
        candidates = []
        extent = node["height"] if vertical else node["width"]
        counterpart_position = counterpart["cy"] if vertical else counterpart["cx"]
        direction = 1 if counterpart_position >= center[axis] else -1
        offset = 0
        while offset <= extent / 2 - 16:
            for sign in (direction, -direction):
                point = list(center)
                point[axis] += sign * offset
                if clear(point) and not any(candidate[axis] == point[axis] for candidate in candidates):
                    candidates.append(point)
            if len(candidates) >= 2:
                return candidates[:2]
            offset += 12
        return candidates or [preferred]

    def readable_automatic_route(self, edge, source, target, primary_sides, primary_ports):
        authored_from = edge.get("fromSide") if edge.get("fromSide") not in (None, "auto") else None
        authored_to = edge.get("toSide") if edge.get("toSide") not in (None, "auto") else None
        side_order = ("right", "bottom", "left", "top")
        side_pairs = [primary_sides]
        side_pairs.extend({"fromSide": fs, "toSide": ts}
                          for fs in ((authored_from,) if authored_from else side_order)
                          for ts in ((authored_to,) if authored_to else side_order))
        natural_from = authored_from or default_from_side(source, target)
        natural_to = authored_to or default_to_side(source, target)
        seen, plans, feedback, first_failure = set(), [], [], None

        aligned_start = self.automatic_port_candidates(edge, source, natural_from, anchor(source, natural_from), target)[0]
        aligned_end = self.automatic_port_candidates(edge, target, natural_to, anchor(target, natural_to), source)[0]
        aligned_points = [aligned_start, aligned_end]
        if (target.get("col", 0) > source.get("col", 0) and edge.get("role") not in ("return", "error")
                and not edge.get("fromSide") and not edge.get("toSide") and not edge.get("labelAt")
                and self.readable_candidate_is_feasible(edge, aligned_points, source, target, natural_from, natural_to)):
            plans.append({"family": "aligned-facing", "points": aligned_points, "fromSide": natural_from,
                          "toSide": natural_to,
                          "cost": self.readable_candidate_cost(edge, aligned_points, -1, natural_from, natural_to)})

        for candidate_sides in side_pairs:
            from_side, to_side = candidate_sides["fromSide"], candidate_sides["toSide"]
            if authored_from and from_side != authored_from or authored_to and to_side != authored_to:
                continue
            key = (from_side, to_side)
            if key in seen:
                continue
            pair_ordinal = len(seen)
            seen.add(key)
            primary = pair_ordinal == 0
            preferred_start = primary_ports.get("from") if primary_ports and primary_ports.get("from") is not None and primary else anchor(source, from_side)
            preferred_end = primary_ports.get("to") if primary_ports and primary_ports.get("to") is not None and primary else anchor(target, to_side)
            starts = self.automatic_port_candidates(edge, source, from_side, preferred_start, target)
            ends = self.automatic_port_candidates(edge, target, to_side, preferred_end, source)
            start, end = starts[0], ends[0]
            planned = self.readable_automatic_candidate_set(edge, source, target, start, end, from_side, to_side,
                                                            ordinal_offset=pair_ordinal * 9,
                                                            natural_from_side=natural_from, natural_to_side=natural_to)
            plans.extend({**candidate, **candidate_sides} for candidate in planned["candidates"])
            port_ordinal = 0
            for alternative_start in starts:
                for alternative_end in ends:
                    if alternative_start is start and alternative_end is end:
                        continue
                    port_ordinal += 1
                    alternative = self.readable_automatic_candidate_set(
                        edge, source, target, alternative_start, alternative_end, from_side, to_side,
                        ordinal_offset=pair_ordinal * 9 + port_ordinal * 144,
                        natural_from_side=natural_from, natural_to_side=natural_to)
                    plans.extend({**candidate, **candidate_sides} for candidate in alternative["candidates"])
                    if not alternative["candidates"]:
                        try:
                            via = self.readable_automatic_via(edge, source, target, alternative_start, alternative_end,
                                                              from_side, to_side)
                            points = join_route_points(alternative_start, via, alternative_end)
                            plans.append({"points": points, **candidate_sides,
                                          "cost": self.readable_candidate_cost(edge, points,
                                                                              pair_ordinal * 9 + port_ordinal * 144 + 6,
                                                                              natural_from, natural_to)})
                        except WorkflowLayoutFeedback as error:
                            feedback.append((error, pair_ordinal))
                        except Exception as error:
                            if first_failure is None:
                                first_failure = error
            if planned["candidates"]:
                continue
            try:
                via = self.readable_automatic_via(edge, source, target, start, end, from_side, to_side)
                points = join_route_points(start, via, end)
                outside_ordinal = next((i for i, candidate in enumerate(planned["rawCandidates"])
                                        if candidate["family"] == "outside-right"), 0)
                ordinal = pair_ordinal * 9 + outside_ordinal
                plans.append({"family": "outside-right", "ordinal": ordinal, "points": points,
                              "cost": self.readable_candidate_cost(edge, points, ordinal, natural_from, natural_to),
                              **candidate_sides})
            except WorkflowLayoutFeedback as error:
                feedback.append((error, pair_ordinal))
            except Exception as error:
                if first_failure is None:
                    first_failure = error

        plans.sort(key=cmp_to_key(lambda a, b: self.compare_cost(a["cost"], b["cost"])))
        if plans:
            selected = plans[0]
            return {"points": selected["points"], "fromSide": selected["fromSide"], "toSide": selected["toSide"]}
        if feedback:
            feedback_priority = {"rank-gap-minimum": 0, "lane-gap-minimum": 1}
            feedback.sort(key=lambda item: (feedback_priority.get(item[0].request.get("kind"), 99), item[1]))
            raise feedback[0][0]
        if authored_from or authored_to:
            self.r.raise_endpoint_side_conflict(edge, primary_sides, primary_ports, first_failure)
        if first_failure:
            raise first_failure
        raise RuntimeError("readable-v2 automatic route enumeration produced no result")

    def readable_controlled_route(self, edge, source, target):
        authored_from = edge.get("fromSide") if edge.get("fromSide") not in (None, "auto") else None
        authored_to = edge.get("toSide") if edge.get("toSide") not in (None, "auto") else None
        natural_from, natural_to = authored_from or default_from_side(source, target), authored_to or default_to_side(source, target)
        side_order = ("right", "bottom", "left", "top")
        pairs = [{"fromSide": natural_from, "toSide": natural_to}]
        pairs.extend({"fromSide": fs, "toSide": ts}
                     for fs in ((authored_from,) if authored_from else side_order)
                     for ts in ((authored_to,) if authored_to else side_order))
        seen, side_pairs = set(), []
        for pair in pairs:
            fs, ts = pair["fromSide"], pair["toSide"]
            if authored_from and fs != authored_from or authored_to and ts != authored_to:
                continue
            key = (fs, ts)
            if key not in seen:
                seen.add(key)
                side_pairs.append(pair)
        absolute = isinstance(edge.get("via"), list) or "channelX" in edge or "channelY" in edge
        candidates, diagnostic_candidates, materialized = [], [], []
        for ordinal, pair in enumerate(side_pairs):
            start, end = anchor(source, pair["fromSide"]), anchor(target, pair["toSide"])
            via = self.route_via(edge, source, target, start, end, pair["fromSide"], pair["toSide"], validate_preset=False)
            authored_points = [start, *via, end]
            points = authored_points if absolute else normalize_route_points(authored_points)
            entry = {"points": points, **pair, "ordinal": ordinal}
            materialized.append(entry)
            if (len(points) >= 2 and all(isinstance(point, (list, tuple)) and len(point) == 2 and is_finite_point(*point) for point in points)
                    and route_honors_endpoint_sides(points, pair["fromSide"], pair["toSide"])):
                diagnostic_candidates.append(entry)
            if not self.readable_candidate_is_feasible(edge, points, source, target, pair["fromSide"], pair["toSide"]):
                continue
            if edge.get("route") not in (None, "auto") and not self.route_matches_preset_family(edge["route"], points, source, target):
                continue
            if any(not self.route_contains_channel_pin(points, field, edge[field]) for field in self.present_channel_pins(edge)):
                continue
            candidates.append({**entry, "cost": self.readable_candidate_cost(edge, points, ordinal, natural_from, natural_to)})
        candidates.sort(key=cmp_to_key(lambda a, b: self.compare_cost(a["cost"], b["cost"])))
        if candidates:
            return candidates[0]
        if absolute:
            return (diagnostic_candidates or materialized or [None])[0]
        if not materialized:
            return None
        fallback = materialized[0]
        via = self.readable_preset_via(edge, source, target, fallback["points"][0], fallback["points"][-1],
                                       fallback["fromSide"], fallback["toSide"])
        return {**fallback, "points": join_route_points(fallback["points"][0], via, fallback["points"][-1])}

    def is_readable_controlled_route(self, edge):
        return self.r.workflow.get("schema_version") == 2 and (
            isinstance(edge.get("via"), list) or "channelX" in edge or "channelY" in edge
            or edge.get("route") not in (None, "auto"))

    def register_routed(self, edge, routed):
        self.path_cache[id(edge)] = routed
        sequence = self.obstacle_sequence
        self.obstacle_sequence += 1
        extent = _route_bounds(routed["points"])
        self.obstacle_grid.insert(extent, {"kind": "route", "edge": edge, "routed": routed,
                                           "sequence": sequence, "bounds": extent})
        for point in routed["points"]:
            self.rightmost_routed_edge = max(self.rightmost_routed_edge, point[0])
        for endpoint in ("from", "to"):
            node_id = edge.get(endpoint)
            if node_id is not None:
                by_node = self.node_routes.setdefault(node_id, [])
                if not any(existing is edge for existing, _ in by_node):
                    by_node.append((edge, routed))
                else:
                    by_node[:] = [(existing, routed if existing is edge else value) for existing, value in by_node]
        relation_index = self.r.edge_index_by_identity.get(id(edge))
        label = self.label_rect_for(edge, relation_index) if relation_index is not None else None
        if label:
            self.obstacle_grid.insert(_rect_bounds(label), {"kind": "label", "edge": edge, "rect": label, "sequence": sequence})
            self.rightmost_routed_edge = max(self.rightmost_routed_edge, label["x"] + label["width"])
        return routed

    def label_rect_for(self, edge, relation_index=None):
        if not edge.get("label") or edge.get("from") not in self.r.nodes or edge.get("to") not in self.r.nodes:
            return None
        routed = self.path_for(edge)
        cached = self._label_rect_cache.get(id(routed))
        if cached is not None:
            return cached
        lx, ly = self.edge_label_point(edge, routed["points"])
        width = workflow_label_width(edge["label"])
        rect = {"relation": edge, "relationIndex": relation_index, "label": edge["label"],
                "x": lx - width / 2, "y": ly - 10, "width": width, "height": 14, "lx": lx, "ly": ly}
        self._label_rect_cache[id(routed)] = rect
        return rect

    def path_for(self, edge):
        key = id(edge)
        if key in self.path_cache:
            return self.path_cache[key]
        source, target = self.r.nodes.get(edge.get("from")), self.r.nodes.get(edge.get("to"))
        if not source or not target:
            return {"d": "", "points": []}
        if self.is_readable_controlled_route(edge):
            planned = self.readable_controlled_route(edge, source, target)
            if planned:
                self.readable_side_cache[key] = {"fromSide": planned["fromSide"], "toSide": planned["toSide"]}
                return self.register_routed(edge, {"d": polyline_path(planned["points"]), "points": planned["points"]})
        sides = self.edge_sides(edge)
        primary_ports = self.automatic_ports.get(key, {})
        readable_automatic = (self.r.workflow.get("schema_version") == 2 and edge.get("via") is None
                              and "channelX" not in edge and "channelY" not in edge
                              and edge.get("route") in (None, "auto"))
        if readable_automatic:
            planned = self.readable_automatic_route(edge, source, target, sides, primary_ports)
            self.readable_side_cache[key] = {"fromSide": planned["fromSide"], "toSide": planned["toSide"]}
            return self.register_routed(edge, {"d": polyline_path(planned["points"]), "points": planned["points"]})
        start = primary_ports.get("from") or anchor(source, sides["fromSide"])
        end = primary_ports.get("to") or anchor(target, sides["toSide"])
        authored = [start, *self.route_via(edge, source, target, start, end, sides["fromSide"], sides["toSide"]), end]
        has_absolute = isinstance(edge.get("via"), list) or "channelX" in edge or "channelY" in edge
        points = normalize_route_points(authored) if self.r.workflow.get("schema_version") == 2 and not has_absolute else authored
        return self.register_routed(edge, {"d": polyline_path(points), "points": points})

