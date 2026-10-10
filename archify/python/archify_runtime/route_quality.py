"""Bounded orthogonal grid search used by native diagram routing.

The graph construction, direction state, budgets and heap tie behaviour follow
renderers/shared/route-quality.mjs. No external layout process is required.
"""

import math

from .common import diagnostic, js_round
from .geometry import is_finite_point, segment_intersects_rect

OUTWARD = {"left": [-1, 0], "right": [1, 0], "top": [0, -1], "bottom": [0, 1]}
DEFAULTS = {"clearance": 2, "minimumDetourRatio": 2.5,
            "minimumExcessLengthPx": 200, "minimumEmptyExcursionPx": 96,
            "maximumObstacleCount": 80, "sharedCorridorMinimumPx": 32}


class MinHeap:
    """Match the canonical heap's stable choices when distances are equal."""

    def __init__(self):
        self.entries = []

    def push(self, key, distance):
        entry = (key, distance)
        self.entries.append(entry)
        index = len(self.entries) - 1
        while index > 0:
            parent = (index - 1) // 2
            if self.entries[parent][1] <= distance:
                break
            self.entries[index] = self.entries[parent]
            index = parent
        self.entries[index] = entry

    def pop(self):
        first = self.entries[0]
        last = self.entries.pop()
        if not self.entries:
            return first
        index = 0
        while True:
            left, right = index * 2 + 1, index * 2 + 2
            if left >= len(self.entries):
                break
            child = right if right < len(self.entries) and self.entries[right][1] < self.entries[left][1] else left
            if self.entries[child][1] >= last[1]:
                break
            self.entries[index] = self.entries[child]
            index = child
        self.entries[index] = last
        return first


def orthogonal_length(points):
    total = 0
    for a, b in zip(points, points[1:]):
        if a[0] != b[0] and a[1] != b[1]:
            return None
        total += abs(b[0] - a[0]) + abs(b[1] - a[1])
    return total


def collinear_overlap(a, b, c, d):
    if a[0] == b[0] == c[0] == d[0]:
        axis = 1
    elif a[1] == b[1] == c[1] == d[1]:
        axis = 0
    else:
        return 0
    return max(0, min(max(a[axis], b[axis]), max(c[axis], d[axis]))
               - max(min(a[axis], b[axis]), min(c[axis], d[axis])))


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def proper_intersection(a, b, c, d):
    def opposite(left, right):
        return (left > 0.0001 and right < -0.0001) or (left < -0.0001 and right > 0.0001)
    denominator = (a[0] - b[0]) * (c[1] - d[1]) - (a[1] - b[1]) * (c[0] - d[0])
    return abs(denominator) >= 0.0001 and opposite(cross(a, b, c), cross(a, b, d)) and opposite(cross(c, d, a), cross(c, d, b))


def orthogonal_touch(a, b, c, d):
    eps = 0.0001
    if abs(a[1] - b[1]) <= eps and abs(c[0] - d[0]) <= eps:
        return min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps and min(c[1], d[1]) + eps < a[1] < max(c[1], d[1]) - eps
    if abs(a[0] - b[0]) <= eps and abs(c[1] - d[1]) <= eps:
        return min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps and min(c[0], d[0]) + eps < a[0] < max(c[0], d[0]) - eps
    return False


def point_on_interior(p, a, b):
    return abs(cross(a, b, p)) <= 0.0001 and (p[0] - a[0]) * (p[0] - b[0]) + (p[1] - a[1]) * (p[1] - b[1]) < -0.0001


def shortest_orthogonal_grid_route(**options):
    o = options
    start, end = o["start"], o["end"]
    from_side, to_side = o["fromSide"], o["toSide"]
    clearance = o["clearance"]
    maximum_nodes = o.get("maximumGridNodes", math.inf)
    stub = o.get("endpointStubPx", clearance + 2)
    metrics = o.get("metrics")

    def write(**patch):
        if metrics is not None:
            metrics.update(patch)

    write(status="initializing", maximumGridNodes=maximum_nodes, obstacleCount=0,
          avoidedSegmentCount=0, coordinateCount=0, candidateNodeCount=0,
          usableNodeCount=0, graphEdgeCount=0, visitedNodeCount=0)
    if from_side not in OUTWARD or to_side not in OUTWARD:
        write(status="unsupported-endpoint-side")
        return None

    def outward(p, side):
        v = OUTWARD[side]
        return [p[0] + v[0] * stub, p[1] + v[1] * stub]

    source_point, target_point = outward(start, from_side), outward(end, to_side)
    expanded = [{**r, "x": r["x"] - clearance, "y": r["y"] - clearance,
                 "width": r["width"] + clearance * 2, "height": r["height"] + clearance * 2}
                for r in o["obstacles"] if r and is_finite_point(r.get("x"), r.get("y"), r.get("width"), r.get("height"))]
    avoided = [s for s in o.get("avoidedSegments", []) if s and s.get("start") and s.get("end")]
    borders = [s for s in o.get("borderSegments", []) if s and s.get("start") and s.get("end")]
    allow_crossings = o.get("allowAvoidedCrossings", False)
    separation = o.get("routeSeparationPx", 8)
    minimum_segment = o.get("minimumSegmentPx", 8)
    minimum_overlap = o.get("minimumAvoidedOverlapPx", 8)
    penalty = o.get("bendPenaltyPx", 0)
    write(obstacleCount=len(expanded), avoidedSegmentCount=len(avoided))
    if len(expanded) > o["maximumObstacleCount"]:
        write(status="obstacle-budget-exceeded")
        return None
    xs = {source_point[0], target_point[0], *(p[0] for p in o["points"])}
    ys = {source_point[1], target_point[1], *(p[1] for p in o["points"])}
    for r in expanded:
        xs.update([r["x"] - 1, r["x"] + r["width"] + 1])
        ys.update([r["y"] - 1, r["y"] + r["height"] + 1])
    for s in avoided:
        a, b = s["start"], s["end"]
        xs.update([a[0], b[0]])
        ys.update([a[1], b[1]])
    for s in avoided + borders:
        a, b = s["start"], s["end"]
        if abs(a[0] - b[0]) <= 0.0001:
            xs.update([a[0] - separation, a[0] + separation])
        if abs(a[1] - b[1]) <= 0.0001:
            ys.update([a[1] - separation, a[1] + separation])

    def coalesce(values, keep):
        values = sorted(values)
        return [v for i, v in enumerate(values) if i == 0 or v in keep or v - values[i - 1] >= minimum_segment]

    ordered_x = coalesce(xs, {source_point[0], target_point[0]})
    ordered_y = coalesce(ys, {source_point[1], target_point[1]})
    count = len(ordered_x) * len(ordered_y)
    write(coordinateCount=len(ordered_x) + len(ordered_y), candidateNodeCount=count)
    if count > maximum_nodes:
        write(status="node-budget-exceeded")
        return None

    def blocked(p):
        return any(r["x"] <= p[0] <= r["x"] + r["width"] and r["y"] <= p[1] <= r["y"] + r["height"] for r in expanded)

    nodes = {(x, y): [x, y] for x in ordered_x for y in ordered_y
             if not blocked([x, y]) and (allow_crossings or not any(point_on_interior([x, y], s["start"], s["end"]) for s in avoided))}
    source, target = tuple(source_point), tuple(target_point)
    write(usableNodeCount=len(nodes))
    if source not in nodes or target not in nodes:
        write(status="endpoint-blocked")
        return None
    adjacency = {key: [] for key in nodes}
    edge_count = 0

    def connect(line, axis):
        nonlocal edge_count
        for a, b in zip(line, line[1:]):
            if any(segment_intersects_rect(a, b, r) for r in expanded):
                continue
            if any((not allow_crossings and (proper_intersection(a, b, s["start"], s["end"]) or orthogonal_touch(a, b, s["start"], s["end"])))
                   or collinear_overlap(a, b, s["start"], s["end"]) >= minimum_overlap for s in avoided):
                continue
            if any(collinear_overlap(a, b, s["start"], s["end"]) > 0.0001 for s in borders):
                continue
            distance = abs(b[0] - a[0]) + abs(b[1] - a[1])
            adjacency[tuple(a)].append((tuple(b), distance, "R" if axis == "h" else "D"))
            adjacency[tuple(b)].append((tuple(a), distance, "L" if axis == "h" else "U"))
            edge_count += 1

    for y in ordered_y:
        connect([nodes[(x, y)] for x in ordered_x if (x, y) in nodes], "h")
    for x in ordered_x:
        connect([nodes[(x, y)] for y in ordered_y if (x, y) in nodes], "v")
    write(graphEdgeCount=edge_count)

    def direction(vector):
        dx, dy = vector
        return "R" if dx > 0 else "L" if dx < 0 else "D" if dy > 0 else "U"

    opposite = {"R": "L", "L": "R", "D": "U", "U": "D"}
    source_state = (source, direction(OUTWARD[from_side]))
    target_axis = opposite[direction(OUTWARD[to_side])]
    distances, previous = {source_state: 0}, {}
    queue = MinHeap()
    queue.push(source_state, 0)
    visits, target_state = 0, None
    while queue.entries:
        current, distance = queue.pop()
        if distance != distances.get(current):
            continue
        visits += 1
        node, axis = current
        if node == target:
            arrival = distance + (0 if axis == target_axis else penalty)
            if target_state is None or arrival < target_state[1]:
                target_state = (current, arrival)
            if axis == target_axis or penalty == 0:
                break
            continue
        if target_state and distance >= target_state[1]:
            break
        for neighbor, weight, neighbor_axis in adjacency.get(node, []):
            if neighbor_axis == opposite[axis]:
                continue
            candidate = distance + weight + (0 if neighbor_axis == axis else penalty)
            state = (neighbor, neighbor_axis)
            if candidate >= distances.get(state, math.inf):
                continue
            distances[state] = candidate
            previous[state] = current
            queue.push(state, candidate)
    write(visitedNodeCount=visits)
    if target_state is None:
        write(status="no-route")
        return None
    reversed_points, key = [], target_state[0]
    while key:
        reversed_points.append(nodes[key[0]])
        if key == source_state:
            break
        key = previous.get(key)
    if tuple(reversed_points[-1]) != source:
        write(status="broken-predecessor-chain")
        return None
    # Import only at the final reduction; the shared native geometry module is
    # also used by every renderer, so point semantics have one implementation.
    from .flow_geometry import normalize_route_points
    result = normalize_route_points([start, *reversed(reversed_points), end])
    write(status="routed")
    return {"points": result, "length": orthogonal_length(result), "obstacleCount": len(expanded)}


shortestOrthogonalGridRoute = shortest_orthogonal_grid_route


def bounds_for_rects(rects):
    usable = [r for r in rects if r and is_finite_point(r.get("x"), r.get("y"), r.get("width"), r.get("height"))
              and r["width"] >= 0 and r["height"] >= 0]
    if not usable:
        return None
    left, top = min(r["x"] for r in usable), min(r["y"] for r in usable)
    right = max(r["x"] + r["width"] for r in usable)
    bottom = max(r["y"] + r["height"] for r in usable)
    return {"left": left, "top": top, "right": right, "bottom": bottom,
            "width": right - left, "height": bottom - top}


def bounds_for_points(points):
    if not points:
        return None
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    return {"left": left, "top": top, "right": right, "bottom": bottom,
            "width": right - left, "height": bottom - top}


def inferred_side(points, endpoint):
    if len(points) < 2:
        return None
    a, b = (points[0], points[1]) if endpoint == "source" else (points[-2], points[-1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    direction = "right" if dx > 0 and dy == 0 else "left" if dx < 0 and dy == 0 else "bottom" if dy > 0 and dx == 0 else "top" if dy < 0 and dx == 0 else None
    return direction if endpoint == "source" else {"right": "left", "left": "right", "bottom": "top", "top": "bottom"}.get(direction)


def clean_route_detour_problems(**o):
    if o.get("profile") != "showcase":
        return []
    from .flow_geometry import normalize_route_points
    policy = {**DEFAULTS, **o.get("thresholds", {})}
    obstacles = list(o["obstacles"])
    content_bounds = bounds_for_rects(o.get("contentRects", obstacles))
    endpoint_ids = o["endpointIds"]
    result = []

    def rounded(value):
        return js_round(value * 100) / 100

    def point_distance(p, r):
        return max(r["x"] - p[0], 0, p[0] - r["x"] - r["width"]) + max(r["y"] - p[1], 0, p[1] - r["y"] - r["height"])

    def outside(a, b):
        if not content_bounds:
            return False
        x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return x < content_bounds["left"] or x > content_bounds["right"] or y < content_bounds["top"] or y > content_bounds["bottom"]

    for index, relation in enumerate(o["relations"]):
        if not relation or relation.get("from") not in endpoint_ids or relation.get("to") not in endpoint_ids:
            continue
        if not isinstance(relation.get("via"), list) or not relation["via"]:
            continue
        points = normalize_route_points(o["pathFor"](relation).get("points", []))
        if len(points) < 3 or not all(is_finite_point(*p) for p in points):
            continue
        actual = orthogonal_length(points)
        if actual is None or not math.isfinite(actual):
            continue
        start, end = points[0], points[-1]
        manhattan = abs(end[0] - start[0]) + abs(end[1] - start[1])
        if actual < manhattan * policy["minimumDetourRatio"] or actual - manhattan < policy["minimumExcessLengthPx"]:
            continue
        route_bounds = bounds_for_points(points)
        excursion = None
        if content_bounds:
            excursion = {"left": max(0, content_bounds["left"] - route_bounds["left"]),
                         "top": max(0, content_bounds["top"] - route_bounds["top"]),
                         "right": max(0, route_bounds["right"] - content_bounds["right"]),
                         "bottom": max(0, route_bounds["bottom"] - content_bounds["bottom"])}
            excursion["maximum"] = max(excursion.values())
        usable = [r for r in obstacles if r and is_finite_point(r.get("x"), r.get("y"), r.get("width"), r.get("height")) and r["width"] >= 0 and r["height"] >= 0]
        empty_clearance = None
        if points[1:-1] and usable:
            distances = [min(point_distance(p, r) for r in usable) for p in points[1:-1]]
            maximum = max(distances)
            empty_clearance = {"maximum": maximum, "point": points[1:-1][distances.index(maximum)]}
        if max((excursion or {}).get("maximum", 0), (empty_clearance or {}).get("maximum", 0)) < policy["minimumEmptyExcursionPx"]:
            continue
        shares_outer = False
        for other in o["relations"]:
            if not other or other is relation or not ({relation.get("from"), relation.get("to")} & {other.get("from"), other.get("to")}):
                continue
            other_points = normalize_route_points(o["pathFor"](other).get("points", []))
            if any(outside(a, b) and collinear_overlap(a, b, c, d) >= policy["sharedCorridorMinimumPx"]
                   for a, b in zip(points, points[1:]) for c, d in zip(other_points, other_points[1:])):
                shares_outer = True
                break
        if shares_outer:
            continue
        from_side = o["fromSideFor"](relation) if o.get("fromSideFor") else None
        to_side = o["toSideFor"](relation) if o.get("toSideFor") else None
        shortest = shortest_orthogonal_grid_route(start=start, end=end, points=points, obstacles=obstacles,
            fromSide=from_side or inferred_side(points, "source"), toSide=to_side or inferred_side(points, "target"),
            clearance=policy["clearance"], maximumObstacleCount=policy["maximumObstacleCount"])
        if not shortest or shortest["length"] is None or shortest["length"] <= 0:
            continue
        ratio, excess = actual / shortest["length"], actual - shortest["length"]
        if ratio < policy["minimumDetourRatio"] or excess < policy["minimumExcessLengthPx"]:
            continue
        from .common import number
        relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
        message = (f'[composition/excessive-route-detour] {o["diagramType"]} {o["relationCollection"]}[{index}]{relation_id} '
                   f'"{relation["from"]}" -> "{relation["to"]}" travels {js_round(actual)}px, {number(rounded(ratio))}x the '
                   f'{js_round(shortest["length"])}px shortest obstacle-clearing orthogonal route, and reaches '
                   f'{js_round((excursion or {}).get("maximum", 0))}px beyond the content bounds — remove the distant via corridor or move it close to the connected content.')
        subject = {"diagramType": o["diagramType"], "collection": o["relationCollection"], "index": index,
                   "from": relation["from"], "to": relation["to"]}
        if relation.get("id"):
            subject["id"] = relation["id"]
        result.append(diagnostic("composition/excessive-route-detour", message, subject,
            {"points": points, "actualLengthPx": rounded(actual), "shortestLegalPoints": shortest["points"],
             "shortestLegalLengthPx": rounded(shortest["length"]), "detourRatio": rounded(ratio), "excessLengthPx": rounded(excess),
             "routeBounds": route_bounds, "contentBounds": content_bounds, "emptyExcursionPx": excursion,
             "emptyControlPointClearancePx": empty_clearance, "obstacleCount": shortest["obstacleCount"],
             "thresholds": {key: policy[key] for key in ("minimumDetourRatio", "minimumExcessLengthPx", "minimumEmptyExcursionPx")}},
            ["remove the distant via points and retry automatic routing, or keep the endpoint sides and move the via corridor near the connected nodes while preserving labels and direction"]))
    return result


cleanRouteDetourProblems = clean_route_detour_problems
