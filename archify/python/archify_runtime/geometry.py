"""Shared geometry with the canonical tolerances and rectangle semantics."""

import math

NUMERIC_CLEARANCE_PX = 0.0001


def as_array(value):
    return value if isinstance(value, list) else []


def is_finite_point(*coords):
    return all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in coords)


def rects_overlap(a, b, gap=0):
    if not is_finite_point(a.get("x"), a.get("y"), a.get("width"), a.get("height"),
                           b.get("x"), b.get("y"), b.get("width"), b.get("height")):
        return False
    return not (
        a["x"] + a["width"] + gap <= b["x"] + NUMERIC_CLEARANCE_PX
        or b["x"] + b["width"] + gap <= a["x"] + NUMERIC_CLEARANCE_PX
        or a["y"] + a["height"] + gap <= b["y"] + NUMERIC_CLEARANCE_PX
        or b["y"] + b["height"] + gap <= a["y"] + NUMERIC_CLEARANCE_PX
    )


def segment_intersects_rect(start, end, rect, gap=0):
    """Return whether a segment intersects a rectangle (with optional halo)."""
    if not is_finite_point(*(start + end), rect.get("x"), rect.get("y"),
                           rect.get("width"), rect.get("height")):
        return False
    left = rect["x"] - gap
    top = rect["y"] - gap
    right = rect["x"] + rect["width"] + gap
    bottom = rect["y"] + rect["height"] + gap

    def inside(point):
        return left <= point[0] <= right and top <= point[1] <= bottom

    if inside(start) or inside(end):
        return True

    # Liang-Barsky clipping is stable for horizontal, vertical, and diagonal
    # paths, including zero-length segments.
    dx, dy = end[0] - start[0], end[1] - start[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, start[0] - left), (dx, right - start[0]),
                 (-dy, start[1] - top), (dy, bottom - start[1])):
        if abs(p) <= 1e-15:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


def segment_rect_clearance(start, end, rect):
    """Euclidean clearance from a straight route segment to an axis-aligned rect."""
    if not is_finite_point(*(start + end), rect.get("x"), rect.get("y"),
                           rect.get("width"), rect.get("height")):
        return None
    if rect["width"] < 0 or rect["height"] < 0:
        return None
    if segment_intersects_rect(start, end, rect):
        return 0.0

    # Sequence paths are horizontal. Keep a correct general segment fallback
    # for diagnostics and future call sites.
    x1, y1 = start
    x2, y2 = end
    if abs(y2 - y1) <= 1e-12:
        route_low, route_high = min(x1, x2), max(x1, x2)
        dx = max(rect["x"] - route_high, route_low - (rect["x"] + rect["width"]), 0)
        dy = max(rect["y"] - y1, y1 - (rect["y"] + rect["height"]), 0)
        return math.hypot(dx, dy)
    if abs(x2 - x1) <= 1e-12:
        dx = max(rect["x"] - x1, x1 - (rect["x"] + rect["width"]), 0)
        route_low, route_high = min(y1, y2), max(y1, y2)
        dy = max(rect["y"] - route_high, route_low - (rect["y"] + rect["height"]), 0)
        return math.hypot(dx, dy)

    def point_rect_distance(point):
        dx = max(rect["x"] - point[0], 0, point[0] - (rect["x"] + rect["width"]))
        dy = max(rect["y"] - point[1], 0, point[1] - (rect["y"] + rect["height"]))
        return math.hypot(dx, dy)

    def point_segment_distance(point):
        vx, vy = x2 - x1, y2 - y1
        denominator = vx * vx + vy * vy
        if not denominator:
            return math.hypot(point[0] - x1, point[1] - y1)
        t = max(0.0, min(1.0, ((point[0] - x1) * vx + (point[1] - y1) * vy) / denominator))
        return math.hypot(point[0] - (x1 + t * vx), point[1] - (y1 + t * vy))

    corners = ((rect["x"], rect["y"]), (rect["x"] + rect["width"], rect["y"]),
               (rect["x"] + rect["width"], rect["y"] + rect["height"]),
               (rect["x"], rect["y"] + rect["height"]))
    return min(point_rect_distance(start), point_rect_distance(end),
               *(point_segment_distance(point) for point in corners))


def route_points_value(points):
    return ";".join(f"{number_string(x)},{number_string(y)}" for x, y in points
                    if isinstance(x, (int, float)) and isinstance(y, (int, float))
                    and math.isfinite(x) and math.isfinite(y))


def number_string(value):
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return repr(float(value))
