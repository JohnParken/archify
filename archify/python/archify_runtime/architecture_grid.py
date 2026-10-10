"""Fixed-cell component placement for native Architecture rendering."""

import math

DEFAULT_GRID = {
    "mode": "grid", "origin": [40, 80], "cols": 4,
    "gapX": 30, "gapY": 40, "cellW": 130, "cellH": 64,
}


def grid_layout(architecture):
    raw = architecture.get("layout")
    if not isinstance(raw, dict) or raw.get("mode") != "grid":
        return None
    return {**DEFAULT_GRID, **raw}


def resolve_component_pos(component, grid):
    position = component.get("pos")
    if isinstance(position, list) and len(position) == 2:
        return position
    if not grid:
        return [math.nan, math.nan]
    row, col = component.get("row"), component.get("col")
    if not _integer(row) or not _integer(col):
        return [math.nan, math.nan]
    ox, oy = grid["origin"]
    return [ox + col * (grid["cellW"] + grid["gapX"]),
            oy + row * (grid["cellH"] + grid["gapY"])]


def validate_grid_placement(architecture, grid, problems):
    if not grid:
        return
    if architecture.get("layout", {}).get("mode") != "grid":
        problems.append('layout.mode must be "grid" when layout is set (free placement omits layout entirely).')
        return
    seen = {}
    for component in architecture.get("components", []):
        has_pos = isinstance(component.get("pos"), list) and len(component["pos"]) == 2
        row, col = component.get("row"), component.get("col")
        has_cell = _integer(row) and _integer(col)
        if has_pos:
            continue
        if not has_cell:
            problems.append(f'Component "{component.get("id")}" needs pos [x,y] or grid row/col when layout.mode is "grid".')
            continue
        if row < 0 or col < 0:
            problems.append(f'Component "{component.get("id")}" row/col must be non-negative integers.')
            continue
        if col >= grid["cols"]:
            problems.append(f'Component "{component.get("id")}" col {col} exceeds layout.cols {grid["cols"]} (valid: 0..{grid["cols"] - 1}).')
        key = (row, col)
        if key in seen:
            problems.append(f'Components "{seen[key]}" and "{component.get("id")}" share grid cell row {row} col {col}.')
        else:
            seen[key] = component.get("id")


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)
