"""Obstacle-aware orthogonal routing for native Architecture and Lifecycle SVGs.

The planner follows the renderer's fixed side order, endpoint spreading,
explicit-route precedence, bounded grid-search budgets, and stable tie breaks.
"""

import math

from .common import number
from .flow_geometry import (anchor, automatic_port_rhythm_bridge,
                            automatic_port_spread, chosen_side, collect_border_runs,
                            default_from_side, default_to_side, normalize_route_points,
                            route_honors_endpoint_sides, route_rhythm_issues)
from .geometry import rects_overlap, segment_intersects_rect
from .route_quality import proper_intersection, shortest_orthogonal_grid_route

SIDE_ORDER = ("right", "bottom", "left", "top")
OUTWARD = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def rounded_path(points, radius):
    """Canonical rounded orthogonal path with JavaScript number spelling."""
    if len(points) < 3 or radius <= 0:
        return " ".join(f'{"M" if index == 0 else "L"} {number(point[0])} {number(point[1])}'
                        for index, point in enumerate(points))
    commands = [f'M {number(points[0][0])} {number(points[0][1])}']
    for index in range(1, len(points) - 1):
        px, py = points[index - 1]
        cx, cy = points[index]
        nx, ny = points[index + 1]
        previous_length = math.hypot(cx - px, cy - py)
        next_length = math.hypot(nx - cx, ny - cy)
        corner = min(radius, previous_length / 2, next_length / 2)
        if corner < 1:
            commands.append(f'L {number(cx)} {number(cy)}')
            continue
        before = [cx - (cx - px) / previous_length * corner,
                  cy - (cy - py) / previous_length * corner]
        after = [cx + (nx - cx) / next_length * corner,
                 cy + (ny - cy) / next_length * corner]
        commands.extend((f'L {number(before[0])} {number(before[1])}',
                         f'Q {number(cx)} {number(cy)} {number(after[0])} {number(after[1])}'))
    commands.append(f'L {number(points[-1][0])} {number(points[-1][1])}')
    return " ".join(commands)


class ArchitectureRouter:
    def __init__(self, components, connections=(), *, frames=(), label_rect_for=None,
                 distinct_automatic_ports=False, prefer_readable_routes=False,
                 interior_segment_px=16, micro_segment_px=8):
        self.components = components
        self.connections = list(connections)
        self.frames = list(frames)
        self.label_rect_for = label_rect_for
        self.distinct_automatic_ports = distinct_automatic_ports
        self.prefer_readable_routes = prefer_readable_routes
        self.interior_segment_px = interior_segment_px
        self.micro_segment_px = micro_segment_px
        self._neighbor_rects = {}
        for connection in self.connections:
            source = self.components.get(connection.get("from"))
            target = self.components.get(connection.get("to"))
            if not source or not target or source is target:
                continue
            self._neighbor_rects.setdefault(source["id"], []).append(target)
            self._neighbor_rects.setdefault(target["id"], []).append(source)
        self.path_cache = {}
        self.selected_sides = {}
        self.reserved_labels = []
        self._honour_reserved_labels = True
        self.metrics = {"routeCount": 0, "explicitRouteCount": 0,
                        "automaticRouteCount": 0, "gridSearchCount": 0,
                        "gridRoutedCount": 0, "gridCandidateNodeCount": 0,
                        "gridUsableNodeCount": 0, "gridEdgeCount": 0,
                        "gridVisitedNodeCount": 0, "avoidedSegmentCount": 0,
                        "conflictFallbackCount": 0, "maximumGridSearchCount": 64,
                        "gridBudgetExhaustedCount": 0, "crossoverRoutedCount": 0,
                        "readabilityCandidateCount": 0, "readabilityImprovedCount": 0,
                        "reciprocalCandidateCount": 0, "reciprocalImprovedCount": 0,
                        "gridAttempts": []}
        self._automatic_ports = automatic_port_spread(
            self.connections, components,
            side_for=lambda relation, endpoint: self._inferred_sides(relation)[
                "fromSide" if endpoint == "source" else "toSide"],
            spacing_for=(lambda left, right: (
                max(14, 3.5 * (self._stroke(left) + self._stroke(right)) + 3.5)
                if 3.5 * (self._stroke(left) + self._stroke(right)) > 14 else 14))
            if distinct_automatic_ports else None)
        self._plan_routes()

    @staticmethod
    def _stroke(relation):
        return relation.get("width") or (1.8 if relation.get("variant") == "emphasis" else 1.5)

    @staticmethod
    def has_authored_route_geometry(connection):
        return (connection.get("via") is not None
                or (connection.get("route") and connection.get("route") != "auto")
                or "channelX" in connection or "channelY" in connection)

    @staticmethod
    def has_authored_label_placement(connection):
        return any(key in connection for key in ("labelAt", "labelDx", "labelDy", "labelSegment"))

    def _inferred_sides(self, connection):
        source = self.components.get(connection.get("from"))
        target = self.components.get(connection.get("to"))
        if not source or not target:
            return {"fromSide": "right", "toSide": "left"}
        fan_out = self._row_fan_out_sides(source, target)
        return {
            "fromSide": chosen_side(connection.get("fromSide"),
                                     (fan_out or {}).get("fromSide") or default_from_side(source, target)),
            "toSide": chosen_side(connection.get("toSide"),
                                  (fan_out or {}).get("toSide") or default_to_side(source, target)),
        }

    def _row_fan_out_sides(self, source, target):
        if not self.prefer_readable_routes:
            return None

        def vertical_side(node, other):
            below = other["y"] >= node["y"] + node["height"]
            if not below and other["y"] + other["height"] > node["y"]:
                return None
            if default_from_side(node, other) in ("top", "bottom"):
                return None
            siblings = self._neighbor_rects.get(node["id"], [])
            row_sibling = any(sibling is not other and abs(sibling["cy"] - other["cy"]) < 1
                              and default_from_side(node, sibling) == ("bottom" if below else "top")
                              for sibling in siblings)

            def blocks(candidate):
                if candidate["cx"] < node["cx"]:
                    gap_start, gap_end = candidate["x"] + candidate["width"], node["x"]
                else:
                    gap_start, gap_end = node["x"] + node["width"], candidate["x"]
                return any(rect is not node and rect is not candidate
                           and rect["y"] < candidate["y"] + candidate["height"]
                           and rect["y"] + rect["height"] > candidate["y"]
                           and rect["x"] < gap_end and rect["x"] + rect["width"] > gap_start
                           for rect in self.components.values())

            same_side = [sibling for sibling in siblings
                         if abs(sibling["cy"] - other["cy"]) < 1
                         and default_from_side(node, sibling) == default_from_side(node, other)]
            return ("bottom" if below else "top") if row_sibling and any(blocks(sibling) for sibling in same_side) else None

        from_side = vertical_side(source, target)
        if from_side:
            return {"fromSide": from_side, "toSide": {"top": "bottom", "bottom": "top"}[from_side]}
        to_side = vertical_side(target, source)
        if to_side:
            return {"fromSide": {"top": "bottom", "bottom": "top"}[to_side], "toSide": to_side}
        return None

    def connection_sides(self, connection):
        self._plan_routes()
        return self.selected_sides.get(id(connection), self._inferred_sides(connection))

    def connection_endpoint_side(self, connection, endpoint):
        field = "fromSide" if endpoint == "source" else "toSide"
        return connection.get(field) if connection.get(field) and connection.get(field) != "auto" else self.connection_sides(connection)[field]

    def _endpoint(self, connection, endpoint, rect, side, inferred_side):
        initial = self._automatic_ports.get(id(connection), {}).get("from" if endpoint == "source" else "to") if side == inferred_side else None
        point = initial or anchor(rect, side)
        if self.has_authored_route_geometry(connection) or connection.get("labelAt") is not None:
            return point, bool(initial), False
        axis = 1 if side in ("left", "right") else 0
        occupied = []
        for other in self.connections:
            if other is connection or self.has_authored_route_geometry(other) or other.get("labelAt") is not None:
                continue
            if connection.get("from") not in (other.get("from"), other.get("to")) and connection.get("to") not in (other.get("from"), other.get("to")):
                continue
            for field, side_field in (("from", "fromSide"), ("to", "toSide")):
                if other.get(field) != rect.get("id"):
                    continue
                other_sides = self.selected_sides.get(id(other), self._inferred_sides(other))
                if other_sides.get(side_field) != side:
                    continue
                routed = self.path_cache.get(id(other))
                other_point = (routed["points"][0 if field == "from" else -1] if routed else
                               self._automatic_ports.get(id(other), {}).get(field) or anchor(rect, side))
                spacing = max(14, 3.5 * (self._stroke(connection) + self._stroke(other)) + 3.5)
                occupied.append((other_point[axis], spacing))
        if not occupied:
            return point, bool(initial), False
        candidates = [point[axis]] + [value + delta * spacing for value, spacing in occupied for delta in (-1, 1)]
        candidates.sort(key=lambda value: (abs(value - point[axis]), value))
        low, high = self._side_range(rect, side)
        for value in candidates:
            candidate = list(point)
            candidate[axis] = value
            if (low <= value <= high and self._port_corner_clearance(rect, side, candidate)
                    and all(abs(value - occupied_value) >= spacing - 0.0001 for occupied_value, spacing in occupied)):
                return candidate, True, False
        return point, True, True

    @staticmethod
    def _side_range(rect, side):
        return ((rect["y"] + 8, rect["y"] + rect["height"] - 8) if side in ("left", "right")
                else (rect["x"] + 8, rect["x"] + rect["width"] - 8))

    @staticmethod
    def _port_corner_clearance(rect, side, point):
        gutter = min(16, (rect["height"] if side in ("left", "right") else rect["width"]) / 2)
        if side in ("left", "right"):
            return rect["y"] + gutter <= point[1] <= rect["y"] + rect["height"] - gutter
        return rect["x"] + gutter <= point[0] <= rect["x"] + rect["width"] - gutter

    def _align_facing_ports(self, connection, source, target, start, end,
                            from_side, to_side, start_spread, target_spread):
        explicit = (self.has_authored_route_geometry(connection)
                    or connection.get("labelAt") is not None)
        horizontal = (from_side, to_side) in (("right", "left"), ("left", "right"))
        vertical = (from_side, to_side) in (("bottom", "top"), ("top", "bottom"))
        if explicit or not (horizontal or vertical) or (start_spread and target_spread):
            return start, end
        has_authored_sides = any(connection.get(key) not in (None, "", "auto")
                                 for key in ("fromSide", "toSide"))
        if not start_spread and not target_spread and has_authored_sides:
            return start, end
        delta = abs(start[1] - end[1]) if horizontal else abs(start[0] - end[0])
        if delta >= 16:
            return start, end

        align_end = (start, [end[0], start[1]]) if horizontal else (start, [start[0], end[1]])
        align_start = ([start[0], end[1]], end) if horizontal else ([end[0], start[1]], end)
        candidates = ([align_end] if start_spread else [align_start] if target_spread
                      else [align_end, align_start])
        for candidate_start, candidate_end in candidates:
            points = [candidate_start, candidate_end]
            if (self._port_corner_clearance(source, from_side, candidate_start)
                    and self._port_corner_clearance(target, to_side, candidate_end)
                    and route_honors_endpoint_sides(points, from_side, to_side)
                    and self._route_clears_endpoint_components(points, source, target)
                    and self._route_clears_components(connection, points)):
                return candidate_start, candidate_end
        return start, end

    @staticmethod
    def _route_clears_endpoint_components(points, source, target):
        last = len(points) - 2
        for index, (start, end) in enumerate(zip(points, points[1:])):
            if index > 0 and segment_intersects_rect(start, end, source):
                return False
            if index < last and segment_intersects_rect(start, end, target):
                return False
        return True

    def _route_clears_components(self, connection, points):
        endpoint_ids = (connection.get("from"), connection.get("to"))
        if any(component["id"] not in endpoint_ids
               and segment_intersects_rect(start, end, component, 2)
               for component in self.components.values()
               for start, end in zip(points, points[1:])):
            return False
        return not (self._honour_reserved_labels and any(
            segment_intersects_rect(start, end, label["rect"], 4)
            for label in self.reserved_labels if label.get("conn") is not connection
            for start, end in zip(points, points[1:])))

    def _geometry(self, connection, sides):
        source = self.components[connection["from"]]
        target = self.components[connection["to"]]
        inferred = self._inferred_sides(connection)
        from_side, to_side = sides["fromSide"], sides["toSide"]
        legacy = self._automatic_ports.get(id(connection), {}) if from_side == inferred["fromSide"] and to_side == inferred["toSide"] else {}
        start, start_spread, start_crowded = (
            self._endpoint(connection, "source", source, from_side, inferred["fromSide"])
            if self.distinct_automatic_ports else
            (legacy.get("from") or anchor(source, from_side), bool(legacy.get("from")), False))
        end, target_spread, target_crowded = (
            self._endpoint(connection, "target", target, to_side, inferred["toSide"])
            if self.distinct_automatic_ports else
            (legacy.get("to") or anchor(target, to_side), bool(legacy.get("to")), False))
        start, end = self._align_facing_ports(
            connection, source, target, start, end, from_side, to_side,
            start_spread, target_spread)
        return {"from": source, "to": target, "start": start, "end": end,
                "fromSide": from_side, "toSide": to_side,
                "crowded": start_crowded or target_crowded}

    @staticmethod
    def _collinear_overlap_length(a, b, c, d):
        epsilon = 0.0001
        if abs(a[0] - b[0]) <= epsilon and abs(c[0] - d[0]) <= epsilon and abs(a[0] - c[0]) <= epsilon:
            return max(0, min(max(a[1], b[1]), max(c[1], d[1])) - max(min(a[1], b[1]), min(c[1], d[1])))
        if abs(a[1] - b[1]) <= epsilon and abs(c[1] - d[1]) <= epsilon and abs(a[1] - c[1]) <= epsilon:
            return max(0, min(max(a[0], b[0]), max(c[0], d[0])) - max(min(a[0], b[0]), min(c[0], d[0])))
        return 0

    @staticmethod
    def _orthogonal_touch_on_resolved_interior(a, b, c, d):
        epsilon = 0.0001
        horizontal, vertical = abs(a[1] - b[1]) <= epsilon, abs(c[0] - d[0]) <= epsilon
        if horizontal and vertical:
            return (min(a[0], b[0]) - epsilon <= c[0] <= max(a[0], b[0]) + epsilon
                    and min(c[1], d[1]) + epsilon < a[1] < max(c[1], d[1]) - epsilon)
        vertical, horizontal = abs(a[0] - b[0]) <= epsilon, abs(c[1] - d[1]) <= epsilon
        if vertical and horizontal:
            return (min(a[1], b[1]) - epsilon <= c[1] <= max(a[1], b[1]) + epsilon
                    and min(c[0], d[0]) + epsilon < a[0] < max(c[0], d[0]) - epsilon)
        return False

    @staticmethod
    def _collinear_backtrack(a, b, c):
        first, second = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        return abs(first[0] * second[1] - first[1] * second[0]) <= 0.0001 and first[0] * second[0] + first[1] * second[1] < -0.0001

    @classmethod
    def _side_aware_bridge_candidates(cls, start, end, from_side, to_side):
        start_vector, end_vector = OUTWARD.get(from_side), OUTWARD.get(to_side)
        if not start_vector or not end_vector:
            return []
        start_stub = [start[0] + start_vector[0] * 24, start[1] + start_vector[1] * 24]
        end_stub = [end[0] + end_vector[0] * 24, end[1] + end_vector[1] * 24]
        raw = []
        vertical = from_side in ("top", "bottom") and to_side in ("top", "bottom")
        horizontal = from_side in ("left", "right") and to_side in ("left", "right")
        if vertical and abs(start[0] - end[0]) < 16:
            for channel_x in (max(start[0], end[0]) + 16, min(start[0], end[0]) - 16):
                raw.append([start_stub, [channel_x, start_stub[1]], [channel_x, end_stub[1]], end_stub])
        if horizontal and abs(start[1] - end[1]) < 16:
            for channel_y in (max(start[1], end[1]) + 16, min(start[1], end[1]) - 16):
                raw.append([start_stub, [start_stub[0], channel_y], [end_stub[0], channel_y], end_stub])
        raw.extend(([start_stub, [end_stub[0], start_stub[1]], end_stub],
                    [start_stub, [start_stub[0], end_stub[1]], end_stub]))
        candidates = []
        for candidate in raw:
            points = normalize_route_points([start, *candidate, end])
            if len(points) < 2:
                continue
            if len(points) >= 3 and cls._collinear_backtrack(points[0], points[1], points[2]):
                continue
            if len(points) >= 3 and cls._collinear_backtrack(points[-3], points[-2], points[-1]):
                continue
            if route_honors_endpoint_sides(points, from_side, to_side):
                candidates.append(points[1:-1])
        return candidates

    def _route_overlaps_resolved(self, connection, points, resolved_routes):
        for entry in resolved_routes:
            other = entry["conn"]
            shares = bool({connection.get("from"), connection.get("to")}
                          & {other.get("from"), other.get("to")})
            if shares and (not self.distinct_automatic_ports or self.has_authored_route_geometry(other)
                           or other.get("labelAt") is not None or connection.get("labelAt") is not None):
                continue
            if any(self._collinear_overlap_length(a, b, c, d) >= 8
                   for a, b in zip(points, points[1:])
                   for c, d in zip(entry["points"], entry["points"][1:])):
                return True
        return False

    def _route_is_clear(self, connection, routed, geometry, resolved_routes):
        points = routed["points"]
        if geometry["crowded"] or len(points) < 2:
            return False
        if not route_honors_endpoint_sides(points, geometry["fromSide"], geometry["toSide"]):
            return False
        last = len(points) - 2
        for index, (start, end) in enumerate(zip(points, points[1:])):
            if index > 0 and segment_intersects_rect(start, end, geometry["from"]):
                return False
            if index < last and segment_intersects_rect(start, end, geometry["to"]):
                return False
            if any(component["id"] not in (connection["from"], connection["to"])
                   and segment_intersects_rect(start, end, component, 2)
                   for component in self.components.values()):
                return False
        if route_rhythm_issues([{"points": points}], self.interior_segment_px, self.micro_segment_px):
            return False
        if self.frames and collect_border_runs([{"points": points}], self.frames):
            return False
        if self._honour_reserved_labels and any(
                segment_intersects_rect(start, end, label["rect"], 4)
                for label in self.reserved_labels if label.get("conn") is not connection
                for start, end in zip(points, points[1:])):
            return False
        return not self._route_overlaps_resolved(connection, points, resolved_routes)

    def _reserved_label_obstacles(self, grid_clearance):
        if not self._honour_reserved_labels:
            return []
        growth = 4 - grid_clearance
        return [{"x": entry["rect"]["x"] - growth,
                 "y": entry["rect"]["y"] - growth,
                 "width": entry["rect"]["width"] + growth * 2,
                 "height": entry["rect"]["height"] + growth * 2}
                for entry in self.reserved_labels]

    @staticmethod
    def _route_length(points):
        return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:]))

    def _crossing_count(self, connection, points, resolved_routes):
        count = 0
        for entry in resolved_routes:
            other = entry["conn"]
            shares = bool({connection.get("from"), connection.get("to")}
                          & {other.get("from"), other.get("to")})
            if shares and (not self.distinct_automatic_ports
                           or self.has_authored_route_geometry(other)
                           or other.get("labelAt") is not None
                           or connection.get("labelAt") is not None):
                continue
            if any(proper_intersection(a, b, c, d)
                   for a, b in zip(points, points[1:])
                   for c, d in zip(entry["points"], entry["points"][1:])):
                count += 1
        return count

    def _is_reciprocal(self, connection):
        return any(other is not connection
                   and other.get("from") == connection.get("to")
                   and other.get("to") == connection.get("from")
                   for other in self.connections)

    def _sibling_crossing_count(self, connection, points, resolved_routes):
        if self._is_reciprocal(connection):
            return 0
        unrelated_reciprocals = [entry for entry in resolved_routes
                                 if not self._is_reciprocal(entry["conn"])]
        return self._crossing_count(connection, points, unrelated_reciprocals)

    def _readability_cost(self, connection, points, resolved_routes):
        return (self._route_length(points) + max(0, len(points) - 2) * 48
                + self._crossing_count(connection, points, resolved_routes) * 160)

    def _improve_reciprocal_pairs(self, resolved, within_scene):
        paired, improved_connections = set(), set()
        for first in resolved:
            connection = first["conn"]
            if (id(connection) in paired or self.has_authored_route_geometry(connection)
                    or self.has_authored_label_placement(connection)
                    or connection.get("fromSide") not in (None, "", "auto")
                    or connection.get("toSide") not in (None, "", "auto")):
                continue
            second = next((entry for entry in resolved if entry is not first
                and entry["conn"].get("from") == connection.get("to")
                and entry["conn"].get("to") == connection.get("from")
                and not self.has_authored_route_geometry(entry["conn"])
                and not self.has_authored_label_placement(entry["conn"])
                and entry["conn"].get("fromSide") in (None, "", "auto")
                and entry["conn"].get("toSide") in (None, "", "auto")), None)
            if second is None:
                continue
            reciprocal = second["conn"]
            paired.update((id(connection), id(reciprocal)))
            first_sides, second_sides = self._inferred_sides(connection), self._inferred_sides(reciprocal)
            facing = (first_sides["fromSide"], first_sides["toSide"],
                      second_sides["fromSide"], second_sides["toSide"])
            horizontal = facing in (("right", "left", "left", "right"),
                                    ("left", "right", "right", "left"))
            vertical = facing in (("bottom", "top", "top", "bottom"),
                                  ("top", "bottom", "bottom", "top"))
            if (not horizontal and not vertical) or (len(first["points"]) <= 2 and len(second["points"]) <= 2):
                continue
            axis = 1 if horizontal else 0
            others = [entry for entry in resolved if entry is not first and entry is not second]
            first_geometry = self._geometry(connection, first_sides)
            second_geometry = self._geometry(reciprocal, second_sides)
            if first_geometry["crowded"] or second_geometry["crowded"]:
                continue

            prior_labels = self.reserved_labels
            improved = False
            self.reserved_labels = [entry for entry in self.reserved_labels
                                    if entry.get("conn") is not connection
                                    and entry.get("conn") is not reciprocal]
            try:
                def lanes(geometry):
                    return list(dict.fromkeys((geometry["start"][axis], geometry["end"][axis])))

                def direct(geometry, lane):
                    start, end = list(geometry["start"]), list(geometry["end"])
                    start[axis] = end[axis] = lane
                    points = [start, end]
                    return {"points": points, "d": rounded_path(points, 8)}

                def endpoint_slots_clear(candidates):
                    entries = [*others, *candidates]
                    sides_by_connection = {id(connection): first_sides, id(reciprocal): second_sides}
                    for candidate in candidates:
                        conn = candidate["conn"]
                        sides = sides_by_connection[id(conn)]
                        endpoints = ((conn.get("from"), sides["fromSide"], candidate["points"][0]),
                                     (conn.get("to"), sides["toSide"], candidate["points"][-1]))
                        for component_id, side, point in endpoints:
                            coordinate = 1 if side in ("left", "right") else 0
                            for other in entries:
                                if other["conn"] is conn:
                                    continue
                                other_conn = other["conn"]
                                other_sides = (sides_by_connection.get(id(other_conn))
                                    or self.selected_sides.get(id(other_conn))
                                    or self._inferred_sides(other_conn))
                                other_endpoints = (
                                    (other_conn.get("from"), other_sides["fromSide"], other["points"][0]),
                                    (other_conn.get("to"), other_sides["toSide"], other["points"][-1]))
                                for other_id, other_side, other_point in other_endpoints:
                                    spacing = max(14, 3.5 * (self._stroke(conn) + self._stroke(other_conn)) + 3.5)
                                    if (component_id == other_id and side == other_side
                                            and abs(point[coordinate] - other_point[coordinate]) < spacing - 0.0001):
                                        return False
                    return True

                def label_clears(rect, owner, entries, labels):
                    if not rect:
                        return not owner.get("label")
                    if (not within_scene([rect["x"], rect["y"]])
                            or not within_scene([rect["x"] + rect["width"], rect["y"] + rect["height"]])
                            or any(rects_overlap(rect, label["rect"], 2) for label in labels)):
                        return False
                    return all(entry["conn"] is owner or all(
                        not segment_intersects_rect(start, end, rect, 4)
                        for start, end in zip(entry["points"], entry["points"][1:]))
                        for entry in entries)

                best, best_cost = None, (
                    self._readability_cost(connection, first["points"], [*others, second])
                    + self._readability_cost(reciprocal, second["points"], [*others, first]))
                for first_lane in lanes(first_geometry):
                    for second_lane in lanes(second_geometry):
                        self.metrics["reciprocalCandidateCount"] += 1
                        candidates = [
                            {"conn": connection, **direct(first_geometry, first_lane)},
                            {"conn": reciprocal, **direct(second_geometry, second_lane)},
                        ]
                        if not all(all(within_scene(point) for point in entry["points"]) for entry in candidates):
                            continue
                        if not endpoint_slots_clear(candidates):
                            continue
                        candidate_routes_clear = True
                        for index, (candidate, geometry) in enumerate(zip(candidates,
                                (first_geometry, second_geometry))):
                            if (not self._port_corner_clearance(geometry["from"], geometry["fromSide"], candidate["points"][0])
                                    or not self._port_corner_clearance(geometry["to"], geometry["toSide"], candidate["points"][-1])
                                    or not self._route_is_clear(candidate["conn"], candidate, geometry,
                                        [*others, candidates[1 - index]])):
                                candidate_routes_clear = False
                                break
                        if not candidate_routes_clear:
                            continue
                        first_rect = self.label_rect_for(connection, candidates[0]["points"], {
                            "routes": [entry["points"] for entry in [*others, *candidates]],
                            "labels": [entry["rect"] for entry in self.reserved_labels]}) if self.label_rect_for else None
                        if not label_clears(first_rect, connection, [*others, candidates[1]], self.reserved_labels):
                            continue
                        second_rect = self.label_rect_for(reciprocal, candidates[1]["points"], {
                            "routes": [entry["points"] for entry in [*others, *candidates]],
                            "labels": [entry["rect"] for entry in self.reserved_labels]
                                     + ([first_rect] if first_rect else [])}) if self.label_rect_for else None
                        labels = [*self.reserved_labels,
                                  *([{"conn": connection, "rect": first_rect}] if first_rect else [])]
                        if not label_clears(second_rect, reciprocal, [*others, candidates[0]], labels):
                            continue
                        cost = (self._readability_cost(connection, candidates[0]["points"], [*others, candidates[1]])
                                + self._readability_cost(reciprocal, candidates[1]["points"], [*others, candidates[0]]))
                        if cost < best_cost:
                            best, best_cost = (candidates, first_rect, second_rect), cost
                if best:
                    candidates, first_rect, second_rect = best
                    for candidate, sides in zip(candidates, (first_sides, second_sides)):
                        routed = {"points": candidate["points"], "d": rounded_path(candidate["points"], 8)}
                        self.path_cache[id(candidate["conn"])] = routed
                        self.selected_sides[id(candidate["conn"])] = sides
                    first["points"], second["points"] = candidates[0]["points"], candidates[1]["points"]
                    self.metrics["reciprocalImprovedCount"] += 1
                    improved_connections.update((id(connection), id(reciprocal)))
                    improved = True
                    if first_rect:
                        self.reserved_labels.append({"conn": connection, "rect": first_rect})
                    if second_rect:
                        self.reserved_labels.append({"conn": reciprocal, "rect": second_rect})
            finally:
                if not improved:
                    self.reserved_labels = prior_labels
        return improved_connections

    def _improve_readability(self, resolved):
        scene_points = [point for entry in resolved for point in entry["points"]]
        scene_rects = [*self.components.values(), *self.frames,
                       *(entry["rect"] for entry in self.reserved_labels)]
        for rect in scene_rects:
            scene_points.extend(([rect["x"], rect["y"]],
                                 [rect["x"] + rect["width"], rect["y"] + rect["height"]]))
        if not scene_points:
            return
        bounds = (min(point[0] for point in scene_points), max(point[0] for point in scene_points),
                  min(point[1] for point in scene_points), max(point[1] for point in scene_points))

        def within_scene(point):
            return bounds[0] <= point[0] <= bounds[1] and bounds[2] <= point[1] <= bounds[3]

        jointly_improved = self._improve_reciprocal_pairs(resolved, within_scene)

        for entry in resolved:
            connection, current = entry["conn"], entry["points"]
            if (id(connection) in jointly_improved
                    or self.has_authored_route_geometry(connection) or connection.get("labelAt") is not None
                    or (len(current) <= 4 and not self._crossing_count(
                        connection, current, [other for other in resolved if other is not entry]))):
                continue
            others = [other for other in resolved if other is not entry]
            best = None
            best_cost = self._readability_cost(connection, current, others)
            for sides in self._candidate_side_pairs(connection):
                self.metrics["readabilityCandidateCount"] += 1
                geometry = self._geometry(connection, sides)
                via = self._route_via(connection, geometry, others, allow_grid=False)
                points = normalize_route_points([geometry["start"], *via, geometry["end"]])
                routed = {"points": points, "d": rounded_path(points, 8)}
                if (not all(within_scene(point) for point in points)
                        or not self._route_is_clear(connection, routed, geometry, others)):
                    continue
                cost = self._readability_cost(connection, points, others)
                if cost < best_cost:
                    best = (routed, sides)
                    best_cost = cost
            if not best:
                continue
            routed, sides = best
            self.metrics["readabilityImprovedCount"] += 1
            entry["points"] = routed["points"]
            self.path_cache[id(connection)] = routed
            self.selected_sides[id(connection)] = sides
            if self.label_rect_for:
                labels = [item["rect"] for item in self.reserved_labels if item["conn"] is not connection]
                rect = self.label_rect_for(connection, routed["points"], {
                    "routes": [other["points"] for other in others] + [routed["points"]],
                    "labels": labels})
                self.reserved_labels = [item for item in self.reserved_labels if item["conn"] is not connection]
                if rect:
                    self.reserved_labels.append({"conn": connection, "rect": rect})

    def _route_via(self, connection, geometry, resolved_routes, allow_grid=True):
        source, target = geometry["from"], geometry["to"]
        start, end = geometry["start"], geometry["end"]
        from_side, to_side = geometry["fromSide"], geometry["toSide"]
        if connection.get("via") is not None:
            return connection["via"]
        route = connection.get("route") or "auto"
        if route == "straight":
            return []
        if route == "orthogonal-h":
            middle = (start[0] + end[0]) / 2
            return [[middle, start[1]], [middle, end[1]]]
        if route == "orthogonal-v":
            middle = (start[1] + end[1]) / 2
            return [[start[0], middle], [end[0], middle]]

        def clear(points):
            if not route_honors_endpoint_sides(points, from_side, to_side):
                return False
            for index, (a, b) in enumerate(zip(points, points[1:])):
                if index > 0 and segment_intersects_rect(a, b, source):
                    return False
                if index < len(points) - 2 and segment_intersects_rect(a, b, target):
                    return False
                if any(component["id"] not in (connection["from"], connection["to"])
                       and segment_intersects_rect(a, b, component, 2)
                       for component in self.components.values()):
                    return False
                if (self._honour_reserved_labels
                        and any(segment_intersects_rect(a, b, label["rect"], 4)
                                for label in self.reserved_labels if label.get("conn") is not connection)):
                    return False
            return True

        def meets_floors(points):
            for a, b in zip(points, points[1:]):
                length = abs(a[0] - b[0]) + abs(a[1] - b[1])
                if length < self.micro_segment_px - 0.0001:
                    return False
            return True

        def conflict(points):
            for entry in resolved_routes:
                other = entry["conn"]
                shares = bool({connection.get("from"), connection.get("to")}
                              & {other.get("from"), other.get("to")})
                if shares:
                    continue
                for a, b in zip(points, points[1:]):
                    for c, d in zip(entry["points"], entry["points"][1:]):
                        if (proper_intersection(a, b, c, d)
                                or self._orthogonal_touch_on_resolved_interior(a, b, c, d)
                                or self._collinear_overlap_length(a, b, c, d) >= 8):
                            return True
            return False

        if route == "auto":
            dx, dy = abs(start[0] - end[0]), abs(start[1] - end[1])
            if dx < 4 or dy < 4:
                direct = [start, end]
                if clear(direct) and meets_floors(direct) and not conflict(direct):
                    return []
            bridge = automatic_port_rhythm_bridge(start, end, from_side, to_side,
                accept=lambda points: clear(points) and meets_floors(points) and not conflict(points))
            if bridge:
                return bridge[1:-1]
            mid_x, mid_y = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
            candidates = [[[mid_x, start[1]], [mid_x, end[1]]],
                          [[start[0], mid_y], [end[0], mid_y]]]
            side_safe = [candidate for candidate in candidates
                         if route_honors_endpoint_sides([start, *candidate, end], from_side, to_side)]
            side_aware = self._side_aware_bridge_candidates(start, end, from_side, to_side)
            near_parallel = ((from_side in ("top", "bottom") and to_side in ("top", "bottom") and dx < 16)
                             or (from_side in ("left", "right") and to_side in ("left", "right") and dy < 16))
            ordered = [*(side_aware if near_parallel else side_safe),
                       *(side_safe if near_parallel else side_aware)]
            for candidate in ordered:
                points = [start, *candidate, end]
                if clear(points) and meets_floors(points) and not conflict(points):
                    if (not self.distinct_automatic_ports
                            or (not self._route_overlaps_resolved(connection, points, resolved_routes)
                                and not self._sibling_crossing_count(connection, points, resolved_routes))):
                        return candidate

            if self.distinct_automatic_ports:
                def channels(first, last, middle):
                    low, high = min(first, last) + 24, max(first, last) - 24
                    values, offset = [], 16
                    while middle - offset >= low or middle + offset <= high:
                        values.extend(value for value in (middle + offset, middle - offset)
                                      if low <= value <= high)
                        offset += 16
                    return values

                channel_candidates = [
                    *([ [channel_x, start[1]], [channel_x, end[1]] ]
                      for channel_x in channels(start[0], end[0], mid_x)),
                    *([ [start[0], channel_y], [end[0], channel_y] ]
                      for channel_y in channels(start[1], end[1], mid_y)),
                ]
                for candidate in channel_candidates:
                    points = [start, *candidate, end]
                    if (route_honors_endpoint_sides(points, from_side, to_side)
                            and clear(points) and meets_floors(points) and not conflict(points)
                            and not self._route_overlaps_resolved(connection, points, resolved_routes)
                            and not self._sibling_crossing_count(connection, points, resolved_routes)):
                        return candidate
            if allow_grid and self.metrics["gridSearchCount"] < self.metrics["maximumGridSearchCount"]:
                self.metrics["gridSearchCount"] += 1
                route_metrics = {}
                avoided_segments = [
                    {"start": a, "end": b}
                    for entry in resolved_routes
                    if self.distinct_automatic_ports
                    and ({connection.get("from"), connection.get("to")}
                         & {entry["conn"].get("from"), entry["conn"].get("to")})
                    and not self.has_authored_route_geometry(entry["conn"])
                    and not entry["conn"].get("labelAt") and not connection.get("labelAt")
                    for a, b in zip(entry["points"], entry["points"][1:])]
                self.metrics["avoidedSegmentCount"] += len(avoided_segments)
                searched = shortest_orthogonal_grid_route(
                    start=start, end=end, points=[start, end],
                    obstacles=[*self.components.values(), *self._reserved_label_obstacles(2)], fromSide=from_side,
                    toSide=to_side, clearance=2, maximumObstacleCount=80,
                    endpointStubPx=24, maximumGridNodes=4096,
                    minimumSegmentPx=self.interior_segment_px,
                    borderSegments=[], bendPenaltyPx=48, avoidedSegments=avoided_segments,
                    allowAvoidedCrossings=True, minimumAvoidedOverlapPx=8,
                    routeSeparationPx=8, metrics=route_metrics)
                for key, metric in (("gridCandidateNodeCount", "candidateNodeCount"),
                                     ("gridUsableNodeCount", "usableNodeCount"),
                                     ("gridEdgeCount", "graphEdgeCount"),
                                     ("gridVisitedNodeCount", "visitedNodeCount")):
                    self.metrics[key] += route_metrics.get(metric, 0)
                if searched:
                    points = searched["points"]
                    clears = clear(points)
                    meets = meets_floors(points)
                    conflicts = conflict(points)
                    overlaps = self._route_overlaps_resolved(connection, points, resolved_routes)
                    # Crossings remain renderable with the canonical bridge
                    # halo; the grid uses them as a route preference/metric,
                    # not a hard acceptance gate.
                    accepted = clears and meets and not overlaps
                    self.metrics["gridAttempts"].append({
                        "relationship": connection.get("id") or f'{connection.get("from")}->{connection.get("to")}',
                        "fromSide": from_side, "toSide": to_side,
                        "inputAvoidedSegmentCount": len(avoided_segments), **route_metrics,
                        "accepted": accepted,
                        **({"rejectedBy": (["components"] if not clears else [])
                           + (["composition-floors"] if not meets else [])
                           + (["shared-corridor"] if overlaps else [])}
                           if not accepted else {}),
                        **({"candidatePoints": points} if not accepted else {}),
                    })
                    if accepted:
                        self.metrics["gridRoutedCount"] += 1
                        return points[1:-1]
                    self.metrics["conflictFallbackCount"] += 1
                else:
                    self.metrics["conflictFallbackCount"] += 1
            elif allow_grid:
                self.metrics["gridBudgetExhaustedCount"] += 1
                self.metrics["conflictFallbackCount"] += 1
            return (side_safe[0] if side_safe else side_aware[0] if side_aware else candidates[0])
        return []

    def _candidate_side_pairs(self, connection):
        inferred = self._inferred_sides(connection)
        authored_from = connection.get("fromSide") if connection.get("fromSide") not in (None, "auto") else None
        authored_to = connection.get("toSide") if connection.get("toSide") not in (None, "auto") else None
        froms = [authored_from] if authored_from else [inferred["fromSide"], *[x for x in SIDE_ORDER if x != inferred["fromSide"]]]
        tos = [authored_to] if authored_to else [inferred["toSide"], *[x for x in SIDE_ORDER if x != inferred["toSide"]]]
        pairs = [{"fromSide": a, "toSide": b,
                  "deviations": int(a != inferred["fromSide"]) + int(b != inferred["toSide"])}
                 for a in froms for b in tos]
        return sorted(pairs, key=lambda pair: (
            pair["deviations"],
            abs((g := self._geometry(connection, pair))["end"][0] - g["start"][0])
            + abs(g["end"][1] - g["start"][1])))

    def _route(self, connection, resolved_routes):
        if connection.get("from") not in self.components or connection.get("to") not in self.components:
            return {"points": [[0, 0], [0, 0]], "d": "M 0 0 L 0 0"}
        authored = self.has_authored_route_geometry(connection)
        if authored:
            sides = self._inferred_sides(connection)
            geometry = self._geometry(connection, sides)
            via = self._route_via(connection, geometry, resolved_routes)
            points = [geometry["start"], *via, geometry["end"]]
            routed = {"points": points, "d": rounded_path(points, 8)}
            self.selected_sides[id(connection)] = sides
            return routed

        pairs = self._candidate_side_pairs(connection)
        first = None

        def clear_route():
            chosen = None
            for sides in pairs:
                geometry = self._geometry(connection, sides)
                via = self._route_via(connection, geometry, resolved_routes)
                raw = [geometry["start"], *via, geometry["end"]]
                points = normalize_route_points(raw)
                routed = {"points": points, "d": rounded_path(points, 8)}
                if not first:
                    # Keep the deterministic historical fallback if no side
                    # pair clears even after the label-preference retry.
                    first.append((routed, sides))
                if self._route_is_clear(connection, routed, geometry, resolved_routes):
                    chosen = (routed, sides)
                    break
            return chosen

        # A label is a preference, not an absolute routing obstacle. First
        # seek a clear route that keeps prior labels clear. If that costs much
        # more than a direct route, retry without label reservations so label
        # placement can move the affected mask instead of forcing a perimeter
        # detour.
        first = []
        self._honour_reserved_labels = True
        chosen = clear_route()
        if self.reserved_labels:
            self._honour_reserved_labels = False
            if chosen is None:
                chosen = clear_route()
            else:
                direct = self._route_length([chosen[0]["points"][0], chosen[0]["points"][-1]])
                route_length = self._route_length(chosen[0]["points"])
                if route_length > direct * 1.25 + 64:
                    plain = clear_route()
                    if plain and self._route_length(plain[0]["points"]) * 1.25 + 64 < route_length:
                        chosen = plain
            self._honour_reserved_labels = True
        if chosen:
            routed, sides = chosen
            self.selected_sides[id(connection)] = sides
            return routed
        routed, sides = first[0]
        self.selected_sides[id(connection)] = sides
        return routed

    def _plan_routes(self):
        if getattr(self, "_planned", False):
            return
        self._planned = True
        indexed = [(i, conn) for i, conn in enumerate(self.connections)
                   if conn.get("from") in self.components and conn.get("to") in self.components]
        explicit = [(i, conn) for i, conn in indexed if self.has_authored_route_geometry(conn)]
        automatic = [(i, conn) for i, conn in indexed if not self.has_authored_route_geometry(conn)]
        automatic.sort(key=lambda item: (
            abs(self.components[item[1]["to"]]["cx"] - self.components[item[1]["from"]]["cx"])
            + abs(self.components[item[1]["to"]]["cy"] - self.components[item[1]["from"]]["cy"]), item[0]))
        resolved = []
        for _, connection in [*explicit, *automatic]:
            routed = self._route(connection, resolved)
            self.path_cache[id(connection)] = routed
            resolved.append({"conn": connection, "points": routed["points"]})
            rect = self.label_rect_for(connection, routed["points"], {
                "routes": [entry["points"] for entry in resolved],
                "labels": [entry["rect"] for entry in self.reserved_labels]}) if self.label_rect_for else None
            if rect:
                self.reserved_labels.append({"conn": connection, "rect": rect})
        if self.prefer_readable_routes:
            self._improve_readability(resolved)
        self.metrics["routeCount"] = len(resolved)
        self.metrics["explicitRouteCount"] = len(explicit)
        self.metrics["automaticRouteCount"] = len(automatic)

    def path_for(self, connection):
        self._plan_routes()
        routed = self.path_cache.get(id(connection))
        return routed if routed is not None else self._route(connection, [])

    def routing_metrics(self):
        self._plan_routes()
        return dict(self.metrics)


def create_router(components, connections=(), **options):
    return ArchitectureRouter(components, connections, **options)
