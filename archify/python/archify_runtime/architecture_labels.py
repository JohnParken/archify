"""Automatic relationship-label placement used by native SVG renderers."""

from .flow_geometry import normalize_route_points
from .geometry import rects_overlap, segment_rect_clearance


def place_automatic_labels(*, labels, routes, components, titles, view_box,
                           placement_bottom=None, fallback_ring=True,
                           keep_fallback_near_route=False, grid_sweep=False):
    placed = list(labels)
    placement_bottom = view_box[1] if placement_bottom is None else placement_bottom
    obstacles = [*components, *titles]
    segments = []
    for route in routes:
        points = normalize_route_points(route["points"])
        segments.extend({"relationIndex": route["relationIndex"], "start": points[i], "end": points[i + 1]}
                        for i in range(len(points) - 1))

    def inside(rect):
        return (rect["x"] >= 0 and rect["y"] >= 0
                and rect["x"] + rect["width"] <= view_box[0]
                and rect["y"] + rect["height"] <= view_box[1])

    def masks_route(rect):
        for segment in segments:
            if segment["relationIndex"] == rect.get("relationIndex"):
                continue
            clearance = segment_rect_clearance(segment["start"], segment["end"], rect)
            if clearance is not None and clearance < 4 - 0.0001:
                return True
        return False

    def overlaps_label(rect, index, gap=0):
        return any(other_index != index and rects_overlap(rect, other, gap)
                   for other_index, other in enumerate(placed))

    def clear(rect, index):
        return (inside(rect) and rect["y"] + rect["height"] <= placement_bottom
                and not any(rects_overlap(rect, obstacle, 2) for obstacle in obstacles)
                and not overlaps_label(rect, index, 2) and not masks_route(rect))

    def rect_at(label, lx, ly):
        return {**label, "lx": lx, "ly": ly,
                "x": lx - label["width"] / 2, "y": ly - 10}

    fractions = [0.5, 0.25, 0.75, 0.375, 0.625, 0.125, 0.875]

    def parallel_on_low_side(a, b, axis, relation_index):
        across = 1 - axis
        low, high = min(a[axis], b[axis]), max(a[axis], b[axis])
        nearest = None
        for other in segments:
            if other["relationIndex"] == relation_index:
                continue
            if abs(other["start"][across] - other["end"][across]) > 0.0001:
                continue
            distance = other["start"][across] - a[across]
            if abs(distance) < 0.0001 or abs(distance) > 36:
                continue
            overlap = min(high, max(other["start"][axis], other["end"][axis])) - max(
                low, min(other["start"][axis], other["end"][axis]))
            if overlap <= 0:
                continue
            if nearest is None or abs(distance) < abs(nearest):
                nearest = distance
        return nearest is not None and nearest < 0

    def grid_candidates(label):
        ranked = []
        own = [segment for segment in segments if segment["relationIndex"] == label["relationIndex"]]
        for segment in own:
            a, b = segment["start"], segment["end"]
            if abs(a[1] - b[1]) < 0.0001 and abs(a[0] - b[0]) >= label["width"] + 16:
                above, below = ((1, 0) if parallel_on_low_side(a, b, 0, label["relationIndex"])
                                else (0, 1))
                for fraction in fractions:
                    x = a[0] + (b[0] - a[0]) * fraction
                    if min(abs(x - a[0]), abs(x - b[0])) < label["width"] / 2 + 6:
                        continue
                    ranked.extend(((above, x, a[1] - 10), (below, x, a[1] + 20),
                                   (2, x, a[1] + 3), (3, x, a[1] - 18), (3, x, a[1] + 28)))
            elif abs(a[0] - b[0]) < 0.0001 and abs(a[1] - b[1]) >= label["height"] + 16:
                left_first = not parallel_on_low_side(a, b, 1, label["relationIndex"])
                for fraction in fractions:
                    y = a[1] + (b[1] - a[1]) * fraction
                    if min(abs(y - a[1]), abs(y - b[1])) < label["height"] / 2 + 6:
                        continue
                    ranked.append((2, a[0], y + 3))
                    for step, offset in enumerate((6, 14, 22, 30, 38, 46, 54)):
                        tier = 0 if step == 0 else 1 if step == 1 else 2 + step
                        ranked.append((tier + (0 if left_first else 0.5),
                                       a[0] - label["width"] / 2 - offset, y + 3))
                        ranked.append((tier + (0.5 if left_first else 0),
                                       a[0] + label["width"] / 2 + offset, y + 3))
        ranked = sorted(enumerate(ranked), key=lambda item: (item[1][0], item[0]))
        return [(entry[1], entry[2]) for _, entry in ranked]

    for index, label in enumerate(placed):
        relation = label["relation"]
        if any(key in relation for key in ("labelAt", "labelDx", "labelDy", "labelSegment")):
            continue
        if (not grid_sweep and inside(label)
                and not any(rects_overlap(label, component, -2) for component in components)
                and not any(rects_overlap(label, title) for title in titles)
                and not overlaps_label(label, index) and not masks_route(label)):
            continue
        if grid_sweep:
            replacement = None
            for lx, ly in grid_candidates(label):
                candidate = rect_at(label, lx, ly)
                slack = {**candidate, "x": candidate["x"] - 1, "y": candidate["y"] - 1,
                         "width": candidate["width"] + 2, "height": candidate["height"] + 2}
                if clear(slack, index):
                    replacement = candidate
                    break
            if replacement:
                placed[index] = replacement
                continue
        candidates = []
        for segment in (segment for segment in segments if segment["relationIndex"] == label["relationIndex"]):
            a, b = segment["start"], segment["end"]
            if abs(a[1] - b[1]) < 0.0001 and abs(a[0] - b[0]) >= label["width"] + 16:
                for fraction in (0.5, 0.25, 0.75, 0.125, 0.875):
                    x = a[0] + (b[0] - a[0]) * fraction
                    if min(abs(x - a[0]), abs(x - b[0])) < 8:
                        continue
                    candidates.extend(((x, a[1] - 10), (x, a[1] + 20),
                                       (x, a[1] - 18), (x, a[1] + 28)))
            elif abs(a[0] - b[0]) < 0.0001 and abs(a[1] - b[1]) >= label["height"] + 16:
                for fraction in (0.5, 0.25, 0.75, 0.125, 0.875):
                    y = a[1] + (b[1] - a[1]) * fraction
                    if min(abs(y - a[1]), abs(y - b[1])) < 8:
                        continue
                    candidates.extend(((a[0] - label["width"] / 2 - 6, y + 3),
                                       (a[0] + label["width"] / 2 + 6, y + 3),
                                       (a[0] - label["width"] / 2 - 14, y + 3),
                                       (a[0] + label["width"] / 2 + 14, y + 3)))
        replacement = next((rect_at(label, lx, ly) for lx, ly in candidates
                            if clear(rect_at(label, lx, ly), index)), None)
        if replacement:
            placed[index] = replacement
            continue
        if not fallback_ring:
            continue
        own_segments = [segment for segment in segments if segment["relationIndex"] == label["relationIndex"]]
        base_anchors = [(label["lx"], label["ly"])] + [
            ((segment["start"][0] + segment["end"][0]) / 2,
             (segment["start"][1] + segment["end"][1]) / 2)
            for segment in own_segments]
        horizontal_step = label["width"] / 2 + 12
        ring_offsets = [(0, -28), (0, 38), (-horizontal_step, -28), (horizontal_step, -28),
                        (-horizontal_step, 38), (horizontal_step, 38),
                        (-(label["width"] + 20), -52), (label["width"] + 20, -52),
                        (-(label["width"] + 20), 62), (label["width"] + 20, 62),
                        (-(label["width"] + 20), -76), (label["width"] + 20, -76),
                        (-(label["width"] + 20), 86), (label["width"] + 20, 86)]
        fallback = None
        for base_x, base_y in base_anchors:
            for dx, dy in ring_offsets:
                rect = rect_at(label, base_x + dx, base_y + dy)
                if not clear(rect, index):
                    continue
                if keep_fallback_near_route and not any(
                    (clearance := segment_rect_clearance(segment["start"], segment["end"], rect)) is not None
                    and clearance <= label["height"] * 2 for segment in own_segments):
                    continue
                fallback = rect
                break
            if fallback:
                break
        if fallback:
            placed[index] = fallback
    return placed


def reserved_label_rect(*, label, points, routes=(), labels=(), components,
                        view_box=(float("inf"), float("inf")), placement_bottom=float("inf")):
    rect = place_automatic_labels(
        labels=[{**label, "relationIndex": -1},
                *[{**other, "relationIndex": -2} for other in labels]],
        routes=[{"relationIndex": -1, "points": points}, *routes],
        components=components, titles=[], view_box=view_box,
        placement_bottom=placement_bottom, fallback_ring=False)[0]
    if any(rects_overlap(rect, component, -2) for component in components):
        return None
    return rect
