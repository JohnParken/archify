"""Shared orthogonal-flow geometry, quality gates, and actionable diagnostics.

This is a Node-free port of the renderer's pure geometry helpers. Layout
callers keep ownership of authored bounds and pass those rectangles and routes
into these functions.
"""

import math
import re

from .common import diagnostic, number
from .geometry import (as_array, is_finite_point, rects_overlap,
                       segment_intersects_rect, segment_rect_clearance)

EPSILON = 0.0001

ENDPOINT_SIDE_RULES = {
    "left": ("horizontal", -1, 1, "leftward", "rightward from the left"),
    "right": ("horizontal", 1, -1, "rightward", "leftward from the right"),
    "top": ("vertical", -1, 1, "upward", "downward from above"),
    "bottom": ("vertical", 1, -1, "downward", "upward from below"),
}


def _round(value):
    return math.floor(value + 0.5)


def _round_tenth(value):
    return math.floor(value * 10 + 0.5) / 10


def _fmt_point(point, precision=0):
    vals = [_round_tenth(value) if precision == 1 else _round(value) for value in point]
    return ", ".join(number(value) for value in vals)


def relationship_subject(diagram_type, relation_collection, relation_index, relation):
    subject = {"diagramType": diagram_type, "collection": relation_collection, "index": relation_index}
    for key in ("id", "from", "to"):
        if relation.get(key):
            subject[key] = relation[key]
    return subject


def replan_hint(relations, fallback):
    controls = []
    for relation in as_array(relations):
        for key in ("route", "via", "channelX", "channelY", "fromSide", "toSide",
                    "bias", "labelAt", "labelDx", "labelDy", "labelSegment"):
            if key in relation and key not in controls:
                controls.append(key)
    if not controls:
        return fallback
    return (f'if the authored {"/".join(controls)} are not required by the user, remove them so the renderer can re-plan; '
            f'otherwise preserve that intent and {fallback}')


def chosen_side(side, fallback):
    return side if side and side != "auto" else fallback


def default_from_side(source, target):
    dx, dy = target["cx"] - source["cx"], target["cy"] - source["cy"]
    if dx != 0 and abs(dx) >= abs(dy):
        return "left" if dx < 0 else "right"
    return "bottom" if dy > 0 else "top"


def default_to_side(source, target):
    dx, dy = target["cx"] - source["cx"], target["cy"] - source["cy"]
    if dx != 0 and abs(dx) >= abs(dy):
        return "right" if dx < 0 else "left"
    return "top" if dy > 0 else "bottom"


def legacy_default_from_side(source, target):
    if target["cx"] < source["cx"]:
        return "left"
    if target["cx"] > source["cx"]:
        return "right"
    return "bottom" if target["cy"] > source["cy"] else "top"


def legacy_default_to_side(source, target):
    if target["cx"] < source["cx"]:
        return "right"
    if target["cx"] > source["cx"]:
        return "left"
    return "top" if target["cy"] > source["cy"] else "bottom"


def anchor(rect, side):
    if side == "left":
        return [rect["x"], rect["cy"]]
    if side == "right":
        return [rect["x"] + rect["width"], rect["cy"]]
    if side == "top":
        return [rect["cx"], rect["y"]]
    if side == "bottom":
        return [rect["cx"], rect["y"] + rect["height"]]
    return [rect["x"] + rect["width"], rect["cy"]]


def automatic_port_spread(relations, boxes, side_for=None, gutter=16, max_spacing=14,
                          spacing_for=None):
    """Spread automatic endpoints sharing a node side using canonical ordering."""
    groups = {}
    spread = {}
    for relation in as_array(relations):
        if not relation or (relation.get("route") and relation.get("route") != "auto"):
            continue
        if (relation.get("via") is not None or "channelX" in relation or "channelY" in relation
                or relation.get("labelAt") is not None):
            continue
        source, target = boxes.get(relation.get("from")), boxes.get(relation.get("to"))
        if not source or not target:
            continue
        from_side = chosen_side(relation.get("fromSide"),
                                side_for(relation, "source") if side_for else default_from_side(source, target))
        to_side = chosen_side(relation.get("toSide"),
                              side_for(relation, "target") if side_for else default_to_side(source, target))
        for endpoint, rect, side, counterpart in (("from", source, from_side, target),
                                                   ("to", target, to_side, source)):
            groups.setdefault((rect.get("id"), side), []).append(
                {"relation": relation, "endpoint": endpoint, "rect": rect, "side": side,
                 "counterpart": counterpart})

    for items in groups.values():
        if len(items) < 2:
            continue
        vertical_side = items[0]["side"] in ("left", "right")

        def order(item):
            counterpart = item["counterpart"]
            relation = item["relation"]
            coordinate = counterpart["cy"] if vertical_side else counterpart["cx"]
            key = "\0".join(str(relation.get(field) or "") for field in ("id", "from", "to", "label"))
            return coordinate, key.encode("utf-16-be", "surrogatepass")

        items.sort(key=order)
        extent = items[0]["rect"]["height" if vertical_side else "width"]
        usable = max(0, extent - gutter * 2)
        spacing = min(max_spacing, usable / (len(items) - 1))
        if not spacing > 0:
            continue
        offsets = None
        if spacing_for:
            gaps = [max(max_spacing, spacing_for(items[index]["relation"], items[index + 1]["relation"]))
                     for index in range(len(items) - 1)]
            span = sum(gaps)
            if span <= usable and any(gap > max_spacing for gap in gaps):
                offset = -span / 2
                offsets = [offset]
                for gap in gaps:
                    offset += gap
                    offsets.append(offset)
        for index, item in enumerate(items):
            offset = (offsets[index] if offsets is not None
                      else (index - (len(items) - 1) / 2) * spacing)
            point = anchor(item["rect"], item["side"])
            point[1 if vertical_side else 0] += offset
            spread.setdefault(id(item["relation"]), {})[item["endpoint"]] = point
    return spread


def polyline_path(points):
    return " ".join(f'{"M" if index == 0 else "L"} {number(point[0])} {number(point[1])}'
                     for index, point in enumerate(points))


def route_points_value(points):
    return ";".join(f"{number(point[0])},{number(point[1])}" for point in as_array(points)
                    if isinstance(point, list) and len(point) == 2 and is_finite_point(*point))


def authored_straight_route_attrs(relation, points):
    if relation.get("route") != "straight" or relation.get("via") or len(points) != 2:
        return ""
    start, end = points
    return (' data-composition-route="straight"'
            if abs(start[0] - end[0]) > 0.01 and abs(start[1] - end[1]) > 0.01 else "")


def normalize_route_points(points):
    """Remove duplicate and forward-collinear points for quality analysis."""
    normalized = []
    for point in as_array(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2 or not is_finite_point(*point):
            continue
        point = list(point)
        previous = normalized[-1] if normalized else None
        if previous and abs(point[0] - previous[0]) <= EPSILON and abs(point[1] - previous[1]) <= EPSILON:
            continue
        while len(normalized) >= 2 and _collinear_forward(normalized[-2], normalized[-1], point):
            normalized.pop()
        normalized.append(point)
    return normalized


def _collinear_forward(a, b, c):
    if abs(_cross_product(a, b, c)) > EPSILON:
        return False
    return ((b[0] - a[0]) * (c[0] - b[0])
            + (b[1] - a[1]) * (c[1] - b[1])) >= -EPSILON


def rounded_path(points, radius):
    if len(points) < 3 or radius <= 0:
        return polyline_path(points)
    commands = [f"M {points[0][0]} {points[0][1]}"]
    for index in range(1, len(points) - 1):
        px, py = points[index - 1]
        cx, cy = points[index]
        nx, ny = points[index + 1]
        previous_length = math.hypot(cx - px, cy - py)
        next_length = math.hypot(nx - cx, ny - cy)
        r = min(radius, previous_length / 2, next_length / 2)
        if r < 1:
            commands.append(f"L {cx} {cy}")
            continue
        before = [cx - ((cx - px) / previous_length) * r,
                  cy - ((cy - py) / previous_length) * r]
        after = [cx + ((nx - cx) / next_length) * r,
                 cy + ((ny - cy) / next_length) * r]
        commands.extend((f"L {before[0]} {before[1]}", f"Q {cx} {cy} {after[0]} {after[1]}"))
    commands.append(f"L {points[-1][0]} {points[-1][1]}")
    return " ".join(commands)


def label_point(item, points):
    if item.get("labelAt"):
        return item["labelAt"]
    dx, dy = item.get("labelDx") or 0, item.get("labelDy") or 0
    if len(points) == 2:
        return [(points[0][0] + points[1][0]) / 2 + dx,
                points[0][1] - 10 + dy]
    segment_index = min(len(points) - 2, max(0, item.get("labelSegment", 1)))
    a, b = points[segment_index], points[segment_index + 1]
    return [(a[0] + b[0]) / 2 + dx, (a[1] + b[1]) / 2 - 10 + dy]


def route_honors_endpoint_sides(points, from_side, to_side):
    return not _endpoint_side_issue(points, "source", from_side) and not _endpoint_side_issue(points, "target", to_side)


def _endpoint_side_issue(points, endpoint, side):
    rule = ENDPOINT_SIDE_RULES.get(side)
    normalized = normalize_route_points(points)
    if not rule or len(normalized) < 2:
        return None
    axis, source_sign, target_sign, source_direction, target_direction = rule
    segment_index = 0 if endpoint == "source" else len(normalized) - 2
    start, end = normalized[segment_index:segment_index + 2]
    dx, dy = end[0] - start[0], end[1] - start[1]
    along, across = (dx, dy) if axis == "horizontal" else (dy, dx)
    expected_sign = source_sign if endpoint == "source" else target_sign
    if abs(across) <= EPSILON and along * expected_sign > EPSILON:
        return None
    return {"endpoint": endpoint, "side": side, "segmentIndex": segment_index,
            "start": start, "end": end, "expectedAxis": axis,
            "expectedDirection": source_direction if endpoint == "source" else target_direction}


def clean_endpoint_side_problems(relations, endpoint_ids, path_for, side_for, route_hint,
                                 diagram_type="dataflow", relation_collection="flows", should_check_relation=None):
    result = []
    for index, relation in enumerate(as_array(relations)):
        if not relation or relation.get("from") not in endpoint_ids or relation.get("to") not in endpoint_ids:
            continue
        if should_check_relation and not should_check_relation(relation, index):
            continue
        points = path_for(relation)["points"]
        authored_from = relation.get("fromSide") if relation.get("fromSide") not in (None, "", "auto") else None
        authored_to = relation.get("toSide") if relation.get("toSide") not in (None, "", "auto") else None
        authored_route = bool((relation.get("route") and relation.get("route") != "auto") or isinstance(relation.get("via"), list))
        sides = [authored_from or (None if authored_route else side_for(relation, "source")),
                 authored_to or (None if authored_route else side_for(relation, "target"))]
        for endpoint, side, authored in (("source", sides[0], authored_from), ("target", sides[1], authored_to)):
            issue = _endpoint_side_issue(points, endpoint, side)
            if not issue:
                continue
            side_origin = "authored" if authored else "inferred"
            field = "fromSide" if endpoint == "source" else "toSide"
            side_field = field if side_origin == "authored" else f"inferred {field}"
            role = "first" if endpoint == "source" else "final"
            relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
            message = (f'[clean-flow/endpoint-side-direction] {diagram_type} {relation_collection}[{index}]{relation_id} '
                       f'"{relation.get("from")}" -> "{relation.get("to")}" {role} segment {issue["segmentIndex"]} '
                       f'[{_fmt_point(issue["start"], 1)}] -> [{_fmt_point(issue["end"], 1)}] does not honor '
                       f'{side_field} "{side}" — it must run {issue["expectedAxis"]} {issue["expectedDirection"]}; {route_hint}.')
            result.append(diagnostic("clean-flow/endpoint-side-direction", message,
                relationship_subject(diagram_type, relation_collection, index, relation),
                {"endpoint": endpoint, "authoredField": field, "sideOrigin": side_origin, "side": side,
                 "segmentIndex": issue["segmentIndex"], "from": issue["start"], "to": issue["end"],
                 "expectedAxis": issue["expectedAxis"], "expectedDirection": issue["expectedDirection"]}, [route_hint]))
    return result


def clean_flow_problems(relations, obstacles, path_for, route_hint, clearance=2,
                        diagram_type="dataflow", relation_collection="flows", obstacle_kind="node"):
    result = []
    obstacle_ids = {item.get("id") for item in obstacles}
    for index, relation in enumerate(as_array(relations)):
        if not relation or not isinstance(relation.get("from"), str) or not isinstance(relation.get("to"), str):
            continue
        if relation["from"] not in obstacle_ids or relation["to"] not in obstacle_ids:
            continue
        points = path_for(relation)["points"]
        if len(points) < 2 or not all(is_finite_point(*point) for point in points):
            continue
        for obstacle in obstacles:
            if obstacle.get("id") in (relation["from"], relation["to"]):
                continue
            if not is_finite_point(obstacle.get("x"), obstacle.get("y"), obstacle.get("width"), obstacle.get("height")):
                continue
            hit = next((segment_index for segment_index, (start, end) in enumerate(zip(points, points[1:]))
                        if segment_intersects_rect(start, end, obstacle, clearance)), None)
            if hit is None:
                continue
            hint = replan_hint([relation], route_hint)
            relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
            message = (f'[clean-flow/edge-through-node] {diagram_type} {relation_collection}[{index}]{relation_id} '
                       f'"{relation["from"]}" -> "{relation["to"]}" crosses {obstacle_kind} "{obstacle["id"]}" '
                       f'(unrelated to this relationship) on segment {hit} [{_fmt_point(points[hit])}] -> '
                       f'[{_fmt_point(points[hit + 1])}] ({clearance}px clearance) — {hint}.')
            result.append(diagnostic("clean-flow/edge-through-node", message,
                relationship_subject(diagram_type, relation_collection, index, relation),
                {"obstacleKind": obstacle_kind, "obstacleId": obstacle["id"], "segmentIndex": hit,
                 "from": points[hit], "to": points[hit + 1], "clearancePx": clearance}, [hint]))
    return result


def _cross_product(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def proper_segment_intersection(a, b, c, d, epsilon=EPSILON):
    ab_c, ab_d = _cross_product(a, b, c), _cross_product(a, b, d)
    cd_a, cd_b = _cross_product(c, d, a), _cross_product(c, d, b)
    opposite = lambda left, right: (left > epsilon and right < -epsilon) or (left < -epsilon and right > epsilon)
    if not opposite(ab_c, ab_d) or not opposite(cd_a, cd_b):
        return None
    denominator = (a[0] - b[0]) * (c[1] - d[1]) - (a[1] - b[1]) * (c[0] - d[0])
    if abs(denominator) < epsilon:
        return None
    ab = a[0] * b[1] - a[1] * b[0]
    cd = c[0] * d[1] - c[1] * d[0]
    return [(ab * (c[0] - d[0]) - (a[0] - b[0]) * cd) / denominator,
            (ab * (c[1] - d[1]) - (a[1] - b[1]) * cd) / denominator]


def clean_crossing_problems(relations, endpoint_ids, path_for, quality, route_hint,
                            diagram_type="dataflow", relation_collection="flows",
                            include_shared_endpoints=None, crossing_resolved=None,
                            merge_forward_collinear_waypoints=False, warn_in_standard=False):
    severity = "error" if quality == "showcase" else "warning"
    if severity == "warning" and not warn_in_standard:
        return []
    result = []
    routed = []
    for index, relation in enumerate(as_array(relations)):
        if relation and relation.get("from") in endpoint_ids and relation.get("to") in endpoint_ids:
            points = path_for(relation)["points"]
            if len(points) >= 2 and all(is_finite_point(*point) for point in points):
                routed.append((index, relation, points))
    for left_index, (li, left, lp) in enumerate(routed):
        for ri, right, rp in routed[left_index + 1:]:
            if ({left.get("from"), left.get("to")} & {right.get("from"), right.get("to")}
                    and not (include_shared_endpoints and include_shared_endpoints(left, right))):
                continue
            hit = None
            if merge_forward_collinear_waypoints:
                from .composition import forward_collinear_analysis_segments, source_segment_index_at_point
            else:
                source_segment_index_at_point = lambda segment, point: segment["segmentIndex"]
            left_segments = forward_collinear_analysis_segments(lp) if merge_forward_collinear_waypoints else [
                {"start": a, "end": b, "segmentIndex": index} for index, (a, b) in enumerate(zip(lp, lp[1:]))]
            right_segments = forward_collinear_analysis_segments(rp) if merge_forward_collinear_waypoints else [
                {"start": a, "end": b, "segmentIndex": index} for index, (a, b) in enumerate(zip(rp, rp[1:]))]
            for left_segment in left_segments:
                for right_segment in right_segments:
                    a, b = left_segment["start"], left_segment["end"]
                    c, d = right_segment["start"], right_segment["end"]
                    point = proper_segment_intersection(a, b, c, d)
                    if point:
                        hit = (point, source_segment_index_at_point(left_segment, point), source_segment_index_at_point(right_segment, point))
                        break
                if hit:
                    break
            if not hit:
                continue
            point, ls, rs = hit
            if crossing_resolved and crossing_resolved(left, right, {"point": point, "leftSegment": ls, "rightSegment": rs}):
                continue
            hint = replan_hint([left, right], route_hint)
            def describe(index, relation):
                ident = f' id "{relation["id"]}"' if relation.get("id") else ""
                return f'{relation_collection}[{index}]{ident} "{relation.get("from")}" -> "{relation.get("to")}"'
            profile_name = "showcase" if severity == "error" else "standard"
            message = (f'[composition/proper-crossing] {profile_name} {diagram_type} {describe(li, left)} crosses '
                       f'{describe(ri, right)} at [{_fmt_point(point, 1)}] (segments {ls} and {rs}) — {hint}.')
            result.append(diagnostic("composition/proper-crossing", message,
                relationship_subject(diagram_type, relation_collection, li, left),
                {"otherRelationship": relationship_subject(diagram_type, relation_collection, ri, right),
                 "point": point, "segmentIndex": ls, "otherSegmentIndex": rs}, [hint], severity))
    return result


def _collinear_overlap(a, b, c, d):
    horizontal = abs(a[1] - b[1]) <= EPSILON and abs(c[1] - d[1]) <= EPSILON and abs(a[1] - c[1]) <= EPSILON
    vertical = abs(a[0] - b[0]) <= EPSILON and abs(c[0] - d[0]) <= EPSILON and abs(a[0] - c[0]) <= EPSILON
    if not horizontal and not vertical:
        return None
    axis = 0 if horizontal else 1
    low = max(min(a[axis], b[axis]), min(c[axis], d[axis]))
    high = min(max(a[axis], b[axis]), max(c[axis], d[axis]))
    if high - low <= EPSILON:
        return None
    fixed = a[1] if horizontal else a[0]
    return {"length": high - low,
            "start": [low, fixed] if horizontal else [fixed, low],
            "end": [high, fixed] if horizontal else [fixed, high]}


def clean_ambiguous_corridor_problems(relations, endpoint_ids, path_for, quality, route_hint,
                                     diagram_type="dataflow", relation_collection="flows",
                                     include_shared_endpoints=None, include_shared_endpoint_counterflow=None,
                                     allow_short_workflow_trunks=False, min_overlap_px=8):
    severity = "error" if quality == "showcase" else "warning"
    if severity == "warning" and not allow_short_workflow_trunks:
        return []
    from .composition import collect_ambiguous_corridors
    routed = [{"relation": relation, "relationIndex": index, "points": path_for(relation)["points"]}
              for index, relation in enumerate(as_array(relations))
              if relation and relation.get("from") in endpoint_ids and relation.get("to") in endpoint_ids]
    result = []
    for hit in collect_ambiguous_corridors(routed, min_overlap_px, include_shared_endpoints,
                                          include_shared_endpoint_counterflow, allow_short_workflow_trunks):
        li, ri = hit["left"]["relationIndex"], hit["right"]["relationIndex"]
        left, right = hit["left"]["relation"], hit["right"]["relation"]
        hint = replan_hint([left, right], route_hint)
        def describe(index, relation):
            ident = f' id "{relation["id"]}"' if relation.get("id") else ""
            return f'{relation_collection}[{index}]{ident} "{relation.get("from")}" -> "{relation.get("to")}"'
        length = _round_tenth(hit["overlapLength"])
        profile_name = "showcase" if severity == "error" else "standard"
        message = (f'[composition/ambiguous-corridor] {profile_name} {diagram_type} {describe(li, left)} shares a '
                   f'{number(length)}px corridor with {describe(ri, right)} at [{_fmt_point(hit["overlapStart"], 1)}] -> '
                   f'[{_fmt_point(hit["overlapEnd"], 1)}] (segments {hit["leftSegment"]} and {hit["rightSegment"]}; minimum {min_overlap_px}px) — {hint}.')
        result.append(diagnostic("composition/ambiguous-corridor", message,
            relationship_subject(diagram_type, relation_collection, li, left),
            {"otherRelationship": relationship_subject(diagram_type, relation_collection, ri, right),
             "overlapLengthPx": length, "minimumPx": min_overlap_px, "from": hit["overlapStart"], "to": hit["overlapEnd"],
             "segmentIndex": hit["leftSegment"], "otherSegmentIndex": hit["rightSegment"]}, [hint], severity))
    return result


def _frame_border_segments(frame):
    if not is_finite_point(frame.get("x"), frame.get("y"), frame.get("width"), frame.get("height")):
        return []
    if frame["width"] <= 0 or frame["height"] <= 0:
        return []
    radius = max(0, min(frame.get("radius") or 0, frame["width"] / 2, frame["height"] / 2))
    left, right = frame["x"], frame["x"] + frame["width"]
    top, bottom = frame["y"], frame["y"] + frame["height"]
    borders = [("top", [left + radius, top], [right - radius, top]),
               ("right", [right, top + radius], [right, bottom - radius]),
               ("bottom", [right - radius, bottom], [left + radius, bottom]),
               ("left", [left, bottom - radius], [left, top + radius])]
    return [{"side": side, "start": start, "end": end} for side, start, end in borders
            if math.hypot(end[0] - start[0], end[1] - start[1]) > EPSILON]


def _merge_border_overlaps(overlaps, border):
    horizontal = abs(border["start"][1] - border["end"][1]) <= EPSILON
    axis = 0 if horizontal else 1
    fixed = border["start"][1] if horizontal else border["start"][0]
    intervals = sorted(({"low": min(item["start"][axis], item["end"][axis]),
                        "high": max(item["start"][axis], item["end"][axis])} for item in overlaps),
                       key=lambda item: (item["low"], item["high"]))
    merged = []
    for interval in intervals:
        if merged and interval["low"] <= merged[-1]["high"] + EPSILON:
            merged[-1]["high"] = max(merged[-1]["high"], interval["high"])
        else:
            merged.append(dict(interval))
    return [{**item, "length": item["high"] - item["low"],
             "start": [item["low"], fixed] if horizontal else [fixed, item["low"]],
             "end": [item["high"], fixed] if horizontal else [fixed, item["high"]]} for item in merged]


def collect_border_runs(routed_relations, frames):
    hits = []
    for routed in routed_relations:
        points = routed.get("points") or []
        segments = list(zip(points, points[1:]))
        if not segments or not all(is_finite_point(*start, *end) for start, end in segments):
            continue
        for frame_index, frame in enumerate(frames):
            for border in _frame_border_segments(frame):
                overlaps = []
                for segment_index, (start, end) in enumerate(segments):
                    overlap = _collinear_overlap(start, end, border["start"], border["end"])
                    if overlap and overlap["length"] > EPSILON:
                        overlaps.append({**overlap, "segmentIndex": segment_index})
                if not overlaps:
                    continue
                merged = _merge_border_overlaps(overlaps, border)
                longest = sorted(merged, key=lambda item: (-item["length"], item["low"]))[0]
                hits.append({**routed, "frame": frame, "frameIndex": frame_index,
                             "side": border["side"], "segmentIndex": min(item["segmentIndex"] for item in overlaps),
                             "overlapLength": sum(item["length"] for item in merged),
                             "overlapStart": longest["start"], "overlapEnd": longest["end"]})
    return hits


def clean_border_run_problems(relations, endpoint_ids, frames, path_for, quality, route_hint,
                              diagram_type="dataflow", relation_collection="flows"):
    if quality not in ("standard", "showcase"):
        return []
    routed = [{"relation": relation, "relationIndex": index, "points": path_for(relation)["points"]}
              for index, relation in enumerate(as_array(relations))
              if relation and relation.get("from") in endpoint_ids and relation.get("to") in endpoint_ids]
    result = []
    for hit in collect_border_runs(routed, frames):
        relation = hit["relation"]
        frame = hit["frame"]
        frame_kind = frame.get("kind") or frame.get("shape") or "frame"
        frame_identity = frame.get("label") or frame.get("id") or hit["frameIndex"]
        hint = replan_hint([relation], route_hint)
        length = _round_tenth(hit["overlapLength"])
        relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
        message = (f'[composition/container-border-run] {diagram_type} {relation_collection}[{hit["relationIndex"]}]'
                   f'{relation_id} '
                   f'"{relation.get("from")}" -> "{relation.get("to")}" follows {frame_kind} "{frame_identity}" '
                   f'{hit["side"]} border for {number(length)}px on segment {hit["segmentIndex"]} '
                   f'[{_fmt_point(hit["overlapStart"], 1)}] -> [{_fmt_point(hit["overlapEnd"], 1)}] — {hint}.')
        result.append(diagnostic("composition/container-border-run", message,
            relationship_subject(diagram_type, relation_collection, hit["relationIndex"], relation),
            {"frameKind": frame_kind, "frameId": frame.get("id"), "frameLabel": frame.get("label"),
             "side": hit["side"], "segmentIndex": hit["segmentIndex"], "overlapLengthPx": length,
             "from": hit["overlapStart"], "to": hit["overlapEnd"]}, [hint]))
    return result


def route_rhythm_issues(routed_relations, interior_segment_px=16, micro_segment_px=8):
    issues = []
    for fallback_index, routed in enumerate(routed_relations):
        points = normalize_route_points(routed.get("points"))
        if len(points) < 2:
            continue
        segment_count = len(points) - 1
        for index, (start, end) in enumerate(zip(points, points[1:])):
            length = abs(end[0] - start[0]) + abs(end[1] - start[1])
            if length <= EPSILON:
                continue
            position = "source-stub" if index == 0 else "target-stub" if index == segment_count - 1 else "interior"
            code = ("composition/micro-segment" if length < micro_segment_px - EPSILON else
                    "composition/short-interior-segment" if position == "interior" and length < interior_segment_px - EPSILON else None)
            if code:
                issues.append({"code": code, "relation": routed.get("relation"),
                    "relationIndex": routed.get("relationIndex", fallback_index), "segmentIndex": index,
                    "position": position, "length": length, "start": start, "end": end})
    return issues


def clean_route_rhythm_problems(relations, endpoint_ids, path_for, quality, route_hint,
                                interior_segment_px=16, micro_segment_px=8,
                                diagram_type="dataflow", relation_collection="flows"):
    if quality != "showcase":
        return []
    routed = [{"relation": relation, "relationIndex": index, "points": path_for(relation)["points"]}
              for index, relation in enumerate(as_array(relations))
              if relation and relation.get("from") in endpoint_ids and relation.get("to") in endpoint_ids]
    result = []
    for hit in route_rhythm_issues(routed, interior_segment_px, micro_segment_px):
        relation = hit.get("relation") or {}
        hint = replan_hint([relation], route_hint)
        length = _round_tenth(hit["length"])
        floor = micro_segment_px if hit["code"] == "composition/micro-segment" else interior_segment_px
        rule = f"is below the {floor}px {'micro-segment' if floor == micro_segment_px else 'interior-segment'} floor"
        relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
        message = (f'[{hit["code"]}] showcase {diagram_type} {relation_collection}[{hit["relationIndex"]}]{relation_id} '
                   f'"{relation.get("from")}" -> "{relation.get("to")}" has a {number(length)}px {hit["position"]} segment '
                   f'{hit["segmentIndex"]} [{_fmt_point(hit["start"], 1)}] -> [{_fmt_point(hit["end"], 1)}] that {rule} — {hint}.')
        result.append(diagnostic(hit["code"], message,
            relationship_subject(diagram_type, relation_collection, hit["relationIndex"], relation),
            {"segmentIndex": hit["segmentIndex"], "position": hit["position"], "lengthPx": length,
             "minimumPx": floor, "from": hit["start"], "to": hit["end"]}, [hint]))
    return result


def _format_rect(rect):
    return f'[{_round(rect["x"])}, {_round(rect["y"])}, {_round(rect["width"])}, {_round(rect["height"])}]'


def _canvas_overflow(start, size, extent):
    return max(0, -start), max(0, start + size - extent)


def clean_label_canvas_problems(labels, view_box, quality, route_hint,
                                diagram_type="dataflow", relation_collection="flows", tolerance=0.5):
    if quality != "showcase":
        return []
    from .composition import collect_label_canvas_overflow
    result = []
    for hit in collect_label_canvas_overflow(labels, view_box, tolerance):
        label, rect, overflow = hit["label"], hit["rect"], hit["overflowPx"]
        width, height = hit["viewBox"]
        relation = hit["relation"] or {}
        extent = " and ".join(f'{side} edge by {number(overflow[side])}px' for side in hit["sides"])
        hint = replan_hint([relation], route_hint)
        relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
        label_text = label.get("label") or relation.get("label", "")
        message = (f'[composition/label-canvas-containment] showcase {diagram_type} label "{label_text}" on {relation_collection}[{hit["relationIndex"]}]'
                   f'{relation_id} "{relation.get("from")}" -> "{relation.get("to")}" extends past the {extent} '
                   f'(label rect {_format_rect(rect)}; viewBox {number(width)}x{number(height)}) — {hint}.')
        result.append(diagnostic("composition/label-canvas-containment", message,
            relationship_subject(diagram_type, relation_collection, hit["relationIndex"], relation),
            {"label": label_text, "labelRect": {k: rect[k] for k in ("x", "y", "width", "height")},
             "viewBox": [width, height], "overflowPx": overflow, "tolerancePx": tolerance}, [hint]))
    return result


def clean_label_route_clearance_problems(relations, labels, endpoint_ids, path_for, quality, route_hint, threshold=4,
                                        diagram_type="dataflow", relation_collection="flows"):
    if quality != "showcase":
        return []
    from .composition import collect_label_route_clearance
    routes = [{"relation": relation, "relationIndex": index, "points": path_for(relation)["points"]}
              for index, relation in enumerate(as_array(relations))
              if relation and (endpoint_ids is None or (relation.get("from") in endpoint_ids and relation.get("to") in endpoint_ids))]
    result = []
    for hit in collect_label_route_clearance(labels, routes, threshold):
        label, rect = hit["label"], hit["rect"]
        label_relation, label_index = hit["labelRelation"] or {}, hit["labelRelationIndex"]
        other, other_index = hit["otherRelation"], hit["otherRelationIndex"]
        segment_index = hit["segmentIndex"]
        start, end, clearance = hit["start"], hit["end"], hit["clearance"]
        hint = replan_hint([label_relation, other], route_hint)
        describe = lambda rel, index: (f'{relation_collection}[{index}]' + (f' id "{rel["id"]}"' if rel.get("id") else "")
            + f' "{rel.get("from")}" -> "{rel.get("to")}"'
            + (f' label "{rel["label"]}"' if rel.get("label") else ""))
        clearance = _round_tenth(clearance)
        message = (f'[composition/label-route-clearance] showcase {diagram_type} label "{label.get("label") or label_relation.get("label", "")}" '
                   f'on {describe(label_relation, label_index)} is {number(clearance)}px from {describe(other, other_index)} segment {segment_index} '
                   f'[{_fmt_point(start, 1)}] -> [{_fmt_point(end, 1)}] (label rect {_format_rect(rect)}; minimum {number(threshold)}px) — {hint}.')
        result.append(diagnostic("composition/label-route-clearance", message,
            relationship_subject(diagram_type, relation_collection, label_index, label_relation),
            {"label": label.get("label") or label_relation.get("label", ""),
             "otherRelationship": relationship_subject(diagram_type, relation_collection, other_index, other),
             "segmentIndex": segment_index, "clearancePx": clearance, "minimumPx": threshold,
             "labelRect": {k: rect[k] for k in ("x", "y", "width", "height")},
             "from": start, "to": end}, [hint]))
    return result


def suggest_label_pair_fix(a, b):
    return [f'  "{a["label"]}" {_format_rect(a)}; "{b["label"]}" {_format_rect(b)}',
            "  Suggested fix: adjust labelDx/labelDy/labelSegment, or route one relationship through a separate corridor"]


def suggest_label_obstacle_fix(label_rect, lx, ly, obstacle, view_box, obstacles):
    # Actionable replacements must clear every tested node and stay on canvas.
    x_offset, y_offset = label_rect["x"] - lx, label_rect["y"] - ly
    min_x, max_x = math.ceil(-x_offset), math.floor(view_box[0] - label_rect["width"] - x_offset)
    anchor_x = None if max_x < min_x else min(max(_round(lx), min_x), max_x)
    placements = []
    for name, y in (("below", _round(obstacle["y"] + obstacle["height"] + 4 - y_offset)),
                    ("above", _round(obstacle["y"] - 4 - label_rect["height"] - y_offset))):
        if anchor_x is None or y + y_offset < 0 or y + y_offset + label_rect["height"] > view_box[1]:
            continue
        candidate = {"x": anchor_x + x_offset, "y": y + y_offset,
                     "width": label_rect["width"], "height": label_rect["height"]}
        if all(not rects_overlap(candidate, blocker, -2) for blocker in obstacles):
            placements.append((name, y))
    lines = [f'  label rect: {_format_rect(label_rect)}',
             f'  node "{obstacle["id"]}" rect: {_format_rect(obstacle)}']
    if not placements:
        if anchor_x is None:
            lines.append(f'  Suggested fix: the {_round(label_rect["width"])}px label rect cannot fit the '
                         f'{number(view_box[0])}x{number(view_box[1])} viewBox at any anchor — shorten the label or enlarge meta.viewBox')
        else:
            lines.append(f'  Suggested fix: no placement directly above or below "{obstacle["id"]}" stays clear inside the '
                         f'{number(view_box[0])}x{number(view_box[1])} viewBox — move the label to an open area with labelAt, '
                         'shorten the label, move the node, or enlarge meta.viewBox')
        return "\n".join(lines)

    relation = label_rect.get("relation") or {}
    authored_dx = relation.get("labelDx") if _is_finite_number(relation.get("labelDx")) else 0
    authored_dy = relation.get("labelDy") if _is_finite_number(relation.get("labelDy")) else 0

    def anchor_fits(anchor, offset, size, extent):
        return anchor + offset >= 0 and anchor + offset + size <= extent

    def relative_hint(y):
        if isinstance(relation.get("labelAt"), list):
            return None
        dx = _round(authored_dx + anchor_x - lx)
        dy = _round(authored_dy + y - ly)
        moves_x = abs(dx - authored_dx) >= 0.5
        lands_at_x = lx - authored_dx + (dx if moves_x else authored_dx)
        lands_at_y = ly - authored_dy + dy
        if not anchor_fits(lands_at_x, x_offset, label_rect["width"], view_box[0]):
            return None
        if not anchor_fits(lands_at_y, y_offset, label_rect["height"], view_box[1]):
            return None
        return (f'set labelDx {_round(dx)} with labelDy {_round(dy)}' if moves_x
                else f'set labelDy {_round(dy)}')

    hints = []
    for name, y in placements:
        relative = relative_hint(y)
        hints.append(f'set labelAt [{anchor_x}, {y}]' + (f' or {relative}' if relative else '') + f' ({name})')
    lines.append(f'  Suggested fix: {"; or ".join(hints)}')
    return "\n".join(lines)


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def automatic_port_rhythm_bridge(start, end, from_side, to_side, endpoint_stub_px=24,
                                 interior_segment_px=16, accept=None):
    """Return an outside-channel route when a narrow midpoint dogleg is cramped."""
    if not (isinstance(start, list) and isinstance(end, list) and len(start) == len(end) == 2
            and is_finite_point(*start, *end)):
        return None
    vectors = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
    from_vector, to_vector = vectors.get(from_side), vectors.get(to_side)
    if not from_vector or not to_vector:
        return None
    opposed_gap = None
    if from_side == "right" and to_side == "left" and end[0] > start[0] or from_side == "left" and to_side == "right" and start[0] > end[0]:
        opposed_gap = abs(end[0] - start[0])
    elif from_side == "bottom" and to_side == "top" and end[1] > start[1] or from_side == "top" and to_side == "bottom" and start[1] > end[1]:
        opposed_gap = abs(end[1] - start[1])
    bounded = (max(8, min(endpoint_stub_px, (opposed_gap - interior_segment_px) / 2))
               if opposed_gap is not None and opposed_gap - endpoint_stub_px * 2 < interior_segment_px else None)
    stub_distances = [endpoint_stub_px]
    if bounded is not None and bounded >= 8 and bounded != endpoint_stub_px:
        stub_distances.append(bounded)
    candidates = []
    for stub in stub_distances:
        start_stub = [start[0] + from_vector[0] * stub, start[1] + from_vector[1] * stub]
        end_stub = [end[0] + to_vector[0] * stub, end[1] + to_vector[1] * stub]
        if from_side in ("top", "bottom") and to_side in ("top", "bottom") and abs(start[0] - end[0]) < interior_segment_px:
            for channel_x in (max(start[0], end[0]) + interior_segment_px, min(start[0], end[0]) - interior_segment_px):
                candidates.append([start, start_stub, [channel_x, start_stub[1]], [channel_x, end_stub[1]], end_stub, end])
        if from_side in ("left", "right") and to_side in ("left", "right") and abs(start[1] - end[1]) < interior_segment_px:
            for channel_y in (max(start[1], end[1]) + interior_segment_px, min(start[1], end[1]) - interior_segment_px):
                candidates.append([start, start_stub, [start_stub[0], channel_y], [end_stub[0], channel_y], end_stub, end])
    for points in candidates:
        normalized = normalize_route_points(points)
        if (route_honors_endpoint_sides(normalized, from_side, to_side)
                and not route_rhythm_issues([{"points": normalized}], interior_segment_px)
                and (accept is None or accept(normalized))):
            return normalized
    return None


def route_budget_metrics(routed_relations, bends_per_relationship=2, stretch=1.35,
                         segment_px=16, micro_segment_px=8):
    metrics = {"maxBends": 0, "routesOverSuggestedBends": 0, "maxStretch": None,
               "routesOverSuggestedStretch": 0, "minSegmentPx": None, "minInteriorSegmentPx": None,
               "shortSegmentCount": 0, "shortEndpointSegmentCount": 0,
               "shortInteriorSegmentCount": 0, "microSegmentCount": 0}
    for routed in routed_relations:
        points = normalize_route_points(routed.get("points"))
        if len(points) < 2:
            continue
        bends = max(0, len(points) - 2)
        metrics["maxBends"] = max(metrics["maxBends"], bends)
        metrics["routesOverSuggestedBends"] += bends > bends_per_relationship
        route_length = 0
        segment_count = len(points) - 1
        for index, (start, end) in enumerate(zip(points, points[1:])):
            length = abs(end[0] - start[0]) + abs(end[1] - start[1])
            if length <= EPSILON:
                continue
            route_length += length
            metrics["minSegmentPx"] = length if metrics["minSegmentPx"] is None else min(metrics["minSegmentPx"], length)
            interior = index not in (0, segment_count - 1)
            if interior:
                metrics["minInteriorSegmentPx"] = length if metrics["minInteriorSegmentPx"] is None else min(metrics["minInteriorSegmentPx"], length)
            if length < segment_px:
                metrics["shortSegmentCount"] += 1
                metrics["shortInteriorSegmentCount" if interior else "shortEndpointSegmentCount"] += 1
            if length < micro_segment_px:
                metrics["microSegmentCount"] += 1
        direct = abs(points[-1][0] - points[0][0]) + abs(points[-1][1] - points[0][1])
        if direct > EPSILON:
            ratio = route_length / direct
            metrics["maxStretch"] = ratio if metrics["maxStretch"] is None else max(metrics["maxStretch"], ratio)
            metrics["routesOverSuggestedStretch"] += ratio > stretch + EPSILON
    return metrics


def join_route_points(start, via, end):
    return normalize_route_points([start, *as_array(via), end])


frame_border_segments = _frame_border_segments
collect_route_rhythm_issues = route_rhythm_issues
