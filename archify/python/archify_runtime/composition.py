"""Scene composition measurements shared by the native layout planners."""

import math

from .flow_geometry import normalize_route_points, _collinear_overlap, _cross_product
from .geometry import as_array, is_finite_point, segment_rect_clearance

EPSILON = 0.0001


def point_lies_on_segment(point, start, end):
    if not all(isinstance(p, (list, tuple)) and len(p) == 2 and is_finite_point(*p) for p in (point, start, end)):
        return False
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length <= EPSILON:
        return math.hypot(point[0] - start[0], point[1] - start[1]) <= EPSILON
    return abs(_cross_product(start, end, point)) <= EPSILON * length and all(
        min(start[axis], end[axis]) - EPSILON <= point[axis] <= max(start[axis], end[axis]) + EPSILON for axis in (0, 1))


def forward_collinear_analysis_segments(points):
    segments = []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        start, end = list(a), list(b)
        source = {"start": start, "end": end, "segmentIndex": index}
        previous = segments[-1] if segments else None
        continuing = False
        if previous and all(is_finite_point(*p) for p in (previous["start"], previous["end"], start, end)):
            first = [previous["end"][axis] - previous["start"][axis] for axis in (0, 1)]
            second = [end[axis] - start[axis] for axis in (0, 1)]
            continuing = (all(abs(previous["end"][axis] - start[axis]) <= EPSILON for axis in (0, 1))
                          and math.hypot(*first) > EPSILON and math.hypot(*second) > EPSILON
                          and abs(first[0] * second[1] - first[1] * second[0]) <= EPSILON
                          and first[0] * second[0] + first[1] * second[1] > EPSILON)
        if continuing:
            previous["end"] = end
            previous["sourceSegments"].append(source)
        else:
            segments.append({**source, "sourceSegments": [source]})
    return segments


def source_segment_index_at_point(segment, point):
    source = next((s for s in segment.get("sourceSegments", []) if point_lies_on_segment(point, s["start"], s["end"])), None)
    return source["segmentIndex"] if source else segment.get("segmentIndex", 0)


def short_workflow_trunk(left, right, left_segment, right_segment, length):
    if length > 24 + EPSILON:
        return False
    a, b = left["relation"], right["relation"]
    variant = lambda edge: edge.get("variant") or "default"
    width = lambda edge: edge.get("width") or (1.8 if variant(edge) == "emphasis" else 1.4)
    if variant(a) != variant(b) or width(a) != width(b) or (a.get("role") or "") != (b.get("role") or ""):
        return False
    same = lambda p, q: all(abs(p[axis] - q[axis]) < EPSILON for axis in (0, 1))
    source = a["from"] == b["from"] and left_segment == right_segment == 0 and same(left["points"][0], right["points"][0])
    target = a["to"] == b["to"] and left_segment == len(left["points"]) - 2 and right_segment == len(right["points"]) - 2 and same(left["points"][-1], right["points"][-1])
    if not source and not target:
        return False
    p, q = left["points"][left_segment:left_segment + 2]
    r, s = right["points"][right_segment:right_segment + 2]
    return (q[0] - p[0]) * (s[0] - r[0]) + (q[1] - p[1]) * (s[1] - r[1]) > 0


def collect_ambiguous_corridors(routed_relations, min_overlap_px=8, include_shared_endpoints=None,
                                include_shared_endpoint_counterflow=None, allow_short_workflow_trunks=False):
    routed = []
    for index, entry in enumerate(as_array(routed_relations)):
        relation = (entry or {}).get("relation")
        if not relation or not isinstance(relation.get("from"), str) or not isinstance(relation.get("to"), str):
            continue
        points = normalize_route_points(entry.get("points"))
        if len(points) >= 2:
            routed.append({"relation": relation, "relationIndex": entry.get("relationIndex", index), "points": points})
    hits = []
    for index, left in enumerate(routed):
        for right in routed[index + 1:]:
            a, b = left["relation"], right["relation"]
            shared = bool({a["from"], a["to"]} & {b["from"], b["to"]})
            counterflow = shared and not (include_shared_endpoints and include_shared_endpoints(a, b))
            if counterflow and not (include_shared_endpoint_counterflow and include_shared_endpoint_counterflow(a, b)):
                continue
            longest = None
            for li, (p, q) in enumerate(zip(left["points"], left["points"][1:])):
                for ri, (r, s) in enumerate(zip(right["points"], right["points"][1:])):
                    overlap = _collinear_overlap(p, q, r, s)
                    if not overlap or overlap["length"] + EPSILON < min_overlap_px:
                        continue
                    if allow_short_workflow_trunks and short_workflow_trunk(left, right, li, ri, overlap["length"]):
                        continue
                    if counterflow and (q[0] - p[0]) * (s[0] - r[0]) + (q[1] - p[1]) * (s[1] - r[1]) >= 0:
                        continue
                    if longest is None or overlap["length"] > longest["overlapLength"] + EPSILON:
                        longest = {"left": left, "right": right, "leftSegment": li, "rightSegment": ri,
                                   "overlapLength": overlap["length"], "overlapStart": overlap["start"], "overlapEnd": overlap["end"]}
            if longest:
                hits.append(longest)
    return hits


def collect_arrowhead_collisions(routed_relations, allow_short_workflow_trunks=False):
    incoming, hits = {}, []
    for entry in as_array(routed_relations):
        points = normalize_route_points(entry.get("points"))
        relation = entry.get("relation") or {}
        if len(points) < 2 or not relation.get("to"):
            continue
        tip, previous = points[-1], points[-2]
        axis = 0 if abs(tip[0] - previous[0]) < EPSILON else 1
        delta = tip[1 - axis] - previous[1 - axis]
        direction = 1 if delta > 0 else -1 if delta < 0 else 0
        half_width = 3.5 * (relation.get("width") or (1.8 if relation.get("variant") == "emphasis" else 1.5))
        siblings = incoming.setdefault((relation["to"], axis, direction), [])
        current = {**entry, "tip": tip, "halfWidth": half_width}
        for sibling in siblings:
            if abs(tip[1 - axis] - sibling["tip"][1 - axis]) > EPSILON:
                continue
            distance = abs(tip[axis] - sibling["tip"][axis])
            minimum = half_width + sibling["halfWidth"]
            if allow_short_workflow_trunks:
                left, right = {**sibling, "points": normalize_route_points(sibling["points"])}, {**entry, "points": points}
                overlap = _collinear_overlap(left["points"][-2], left["points"][-1], points[-2], tip)
                if overlap and short_workflow_trunk(left, right, len(left["points"]) - 2, len(points) - 2, overlap["length"]):
                    continue
            if distance < minimum - EPSILON:
                hits.append({"left": sibling, "right": current, "distance": distance, "minimum": minimum})
        siblings.append(current)
    return hits


def segment_rect_intersection_length(start, end, rect):
    if not is_finite_point(*start, *end, rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) or rect["width"] < 0 or rect["height"] < 0:
        return None
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0.0000001:
        return 0
    enter, leave = 0, 1
    for direction, distance in ((-dx, start[0] - rect["x"]), (dx, rect["x"] + rect["width"] - start[0]),
                                (-dy, start[1] - rect["y"]), (dy, rect["y"] + rect["height"] - start[1])):
        if abs(direction) <= 0.0000001:
            if distance < -0.0000001:
                return 0
            continue
        ratio = distance / direction
        if direction < 0:
            enter = max(enter, ratio)
        else:
            leave = min(leave, ratio)
        if enter > leave + 0.0000001:
            return 0
    return length * max(0, leave - enter)


def relationship_identity(relation, index):
    relation = relation or {}
    if "key" in relation:
        return "key", relation["key"]
    if relation.get("id"):
        return "id", relation.get("from") or "", relation.get("to") or "", relation["id"]
    return "index", index


def same_relationship(left, right):
    if not left or not right:
        return False
    if left is right:
        return True
    if "key" in left and "key" in right:
        return left["key"] == right["key"]
    return bool(left.get("id") and left.get("id") == right.get("id") and left.get("from") == right.get("from") and left.get("to") == right.get("to"))


def collect_label_route_clearance(labels, routed_relations, threshold):
    if not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold <= 0:
        return []
    seen_routes, routes = set(), []
    for fallback_index, entry in enumerate(as_array(routed_relations)):
        relation = entry.get("relation") or entry
        points = normalize_route_points(entry.get("points") or relation.get("routePoints"))
        index = entry.get("relationIndex", fallback_index)
        identity = relationship_identity(relation, index)
        if len(points) < 2 or identity in seen_routes:
            continue
        seen_routes.add(identity)
        routes.append({"relation": relation, "relationIndex": index, "points": points,
                       "bounds": (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))})
    hits, seen_labels = [], set()
    for fallback_index, label in enumerate(as_array(labels)):
        rect = label.get("rect") or label
        if not is_finite_point(rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) or rect["width"] < 0 or rect["height"] < 0:
            continue
        index = label.get("relationIndex", fallback_index)
        identity = relationship_identity(label.get("relation"), index)
        if identity in seen_labels:
            continue
        seen_labels.add(identity)
        for route in routes:
            if index == route["relationIndex"] or same_relationship(label.get("relation"), route["relation"]):
                continue
            left, top, right, bottom = route["bounds"]
            if right < rect["x"] - threshold or left > rect["x"] + rect["width"] + threshold or bottom < rect["y"] - threshold or top > rect["y"] + rect["height"] + threshold:
                continue
            nearest = None
            for segment_index, (start, end) in enumerate(zip(route["points"], route["points"][1:])):
                clearance = segment_rect_clearance(start, end, rect)
                if clearance is not None and (nearest is None or clearance < nearest["clearance"]):
                    nearest = {"clearance": clearance, "intersectionLength": segment_rect_intersection_length(start, end, rect),
                               "segmentIndex": segment_index, "start": start, "end": end}
            if nearest is None or nearest["clearance"] + EPSILON >= threshold:
                continue
            hits.append({"label": label, "labelRelation": label.get("relation"), "labelRelationIndex": index,
                         "otherRelation": route["relation"], "otherRelationIndex": route["relationIndex"],
                         "rect": rect, **nearest, "threshold": threshold})
    return hits


def relationship_legend_obstacles(relations, points_for=None, label_rect_for=None):
    obstacles = []
    for index, relation in enumerate(as_array(relations)):
        points = [p for p in (points_for(relation, index) if points_for else []) if isinstance(p, (list, tuple)) and len(p) == 2 and is_finite_point(*p)]
        obstacles.extend({"kind": "relationship-segment", "start": a, "end": b} for a, b in zip(points, points[1:]))
        rect = label_rect_for(relation, index) if label_rect_for else None
        if rect and is_finite_point(rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")):
            obstacles.append({"kind": "relationship-label", **rect})
    return obstacles


def collect_label_canvas_overflow(labels, view_box, tolerance=0.5):
    box = as_array(view_box)
    if len(box) == 4:
        origin_x, origin_y, width, height = box
    elif len(box) == 2:
        origin_x, origin_y, width, height = 0, 0, box[0], box[1]
    else:
        return []
    if not is_finite_point(origin_x, origin_y, width, height):
        return []
    hits = []
    for index, label in enumerate(as_array(labels)):
        rect = label.get("rect") or label
        if not is_finite_point(rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")):
            continue
        if rect["width"] < 0 or rect["height"] < 0:
            continue
        overflow = {"left": origin_x - rect["x"], "right": rect["x"] + rect["width"] - (origin_x + width),
                    "top": origin_y - rect["y"], "bottom": rect["y"] + rect["height"] - (origin_y + height)}
        sides = [side for side, value in overflow.items() if value > tolerance]
        if not sides:
            continue
        hits.append({"label": label, **({"relation": label["relation"]} if "relation" in label else {}), "relationIndex": label.get("relationIndex", index),
                     "rect": rect, "viewBox": [width, height], "viewBoxOrigin": [origin_x, origin_y], "sides": sides,
                     "overflowPx": {side: math.floor(overflow[side] * 10 + 0.5) / 10 for side in sides}, "tolerance": tolerance})
    return hits
