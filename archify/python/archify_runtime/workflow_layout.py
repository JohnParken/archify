"""Workflow v1 fixed geometry and readable-v2 rank/lane planning."""

import json
import math

from .drawing import text_units
from .geometry import as_array
from .workflow_migration import LEGACY_COLUMN_CENTERS

GROUP_FRAME_BOTTOM_INSET = 4
GROUP_NODE_INSET = 4


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_integer(value):
    return _is_number(value) and value == int(value)


def _js_number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _or_zero(value):
    number = _js_number(value)
    return number if number and math.isfinite(number) else 0


def stable_compare(left, right):
    a, b = "" if left is None else str(left), "" if right is None else str(right)
    return (a > b) - (a < b)


def stable_value_key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def workflow_edge_name(edge):
    return edge.get("id") or f'{edge.get("from")}->{edge.get("to")}'


def workflow_label_width(label):
    return max(30, text_units(label) * 4.8 + 10)


def authored_node_width(node):
    width = node.get("width")
    return width if _is_number(width) else 92


def authored_node_height(node):
    height = node.get("height")
    if _is_number(height):
        return height
    return 68 if node.get("tag") else 52


def node_width_contributor(node):
    return f'node {node.get("id")} width {authored_node_width(node)}px'


def has_absolute_workflow_pins(workflow):
    return any(
        isinstance(edge.get("via"), list) or isinstance(edge.get("labelAt"), list)
        or "channelX" in edge or "channelY" in edge
        for edge in as_array(workflow.get("edges")))


def has_vertical_stack(workflow):
    offsets = {}
    for node in as_array(workflow.get("nodes")):
        col = node.get("col")
        if not _is_integer(col):
            continue
        key = (node.get("lane"), col)
        current = offsets.setdefault(key, set())
        current.add(_or_zero(node.get("yOffset")))
        if len(current) > 1:
            return True
    return False


def uses_independent_lane_measurement(workflow):
    return (has_vertical_stack(workflow) and not (workflow.get("meta") or {}).get("viewBox")
            and not has_absolute_workflow_pins(workflow))


def readable_group_bounds(workflow, group, col_xs):
    start_col, end_col = group.get("fromCol"), group.get("toCol")
    if (not _is_integer(start_col) or not _is_integer(end_col) or start_col < 0
            or start_col > end_col or end_col >= len(col_xs)):
        return {"x": 0, "width": 0, "cx": 0}
    start, end = col_xs[start_col] - 50, col_xs[end_col] + 50
    natural_width = end - start
    minimum_width = text_units(group.get("label")) * 5.6 + 20
    width = max(natural_width, minimum_width)
    left = start if start_col == end_col and width > natural_width else (start + end - width) / 2
    right = left + width
    for node in as_array(workflow.get("nodes")):
        col = node.get("col")
        if (node.get("lane") != group.get("lane") or not _is_integer(col)
                or col < start_col or col > end_col or col < 0 or col >= len(col_xs)):
            continue
        half_width = authored_node_width(node) / 2
        left = min(left, col_xs[col] - half_width - GROUP_NODE_INSET)
        right = max(right, col_xs[col] + half_width + GROUP_NODE_INSET)
    width = right - left
    return {"x": left, "width": width, "cx": left + width / 2}


def vertical_intervals_overlap(left, right, clearance=0):
    left_center, right_center = _or_zero(left.get("yOffset")), _or_zero(right.get("yOffset"))
    return abs(left_center - right_center) < authored_node_height(left) / 2 + authored_node_height(right) / 2 + clearance


def _phase_or_group_constraints(items, kind, column_count, text_factor, pad, single_rank_base, contributors):
    result = []
    for item in items:
        start, end = item.get("fromCol"), item.get("toCol")
        if (not _is_integer(start) or not _is_integer(end) or start < 0 or start > end or end >= column_count):
            continue
        minimum_width = text_units(item.get("label")) * text_factor + pad
        name = item.get("id") or item.get("label")
        if start == end:
            if end < column_count - 1:
                result.append({"from": end, "to": end + 1,
                               "minimum": single_rank_base + max(0, minimum_width - (92 if kind == "phase" else 100)),
                               "contributors": [f"{kind} {name} label span"]})
            continue
        result.append({"from": start, "to": end,
                       "minimum": max(0, minimum_width - (92 if kind == "phase" else 100)),
                       "contributors": [f"{kind} {name} label span"]})
    return result


def create_legacy_layout():
    return {
        "contract": "fixed-v1", "laneX": 40, "laneY": 52, "laneW": 640,
        "laneH": 104, "laneGap": 20, "laneTitleH": 30,
        "colXs": list(LEGACY_COLUMN_CENTERS), "nodeW": 92, "nodeH": 52,
        "defaultViewBoxWidth": 720,
    }


def create_readable_layout(workflow, layout_feedback=None):
    feedback = layout_feedback or {}
    column_count, baseline_pitch, column_start = 6, 120, 94
    nodes = as_array(workflow.get("nodes"))
    edges = as_array(workflow.get("edges"))
    phases = as_array(workflow.get("phases"))
    groups = as_array(workflow.get("groups"))
    nodes_by_id = {node.get("id"): node for node in nodes}
    constraints, feedback_constraints = [], []
    channel_label_edge_keys = set()
    width_contributors, height_contributors = set(), set()

    for col in range(column_count - 1):
        constraints.append({"from": col, "to": col + 1, "minimum": baseline_pitch})
    for key, minimum in sorted((feedback.get("rankGapMinimums") or {}).items()):
        from_col, to_col = (int(part) for part in key.split(":"))
        constraints.append({"from": from_col, "to": to_col, "minimum": minimum,
                            "contributors": (feedback.get("rankGapContributors") or {}).get(
                                key, [f"rank {from_col}→{to_col} route clearance"])})

    for left_index, left in enumerate(nodes):
        for right in nodes[left_index + 1:]:
            if left.get("lane") != right.get("lane") or left.get("col") == right.get("col"):
                continue
            if not vertical_intervals_overlap(left, right, 8):
                continue
            earlier, later = (left, right) if left["col"] < right["col"] else (right, left)
            constraints.append({
                "from": earlier["col"], "to": later["col"],
                "minimum": authored_node_width(earlier) / 2 + 8 + authored_node_width(later) / 2,
                "contributors": [f'rank {earlier["col"]}→{later["col"]} node width clearance',
                                 node_width_contributor(earlier), node_width_contributor(later)],
            })

    for edge in edges:
        source, target = nodes_by_id.get(edge.get("from")), nodes_by_id.get(edge.get("to"))
        if not source or not target or source.get("lane") != target.get("lane") or source.get("col") == target.get("col"):
            continue
        if edge.get("via") or "channelX" in edge or "channelY" in edge or edge.get("route", "auto") not in ("auto", "straight"):
            continue
        if _or_zero(source.get("yOffset")) != _or_zero(target.get("yOffset")):
            continue
        earlier, later = (source, target) if source["col"] < target["col"] else (target, source)
        labeled_clearance = max(28, workflow_label_width(edge.get("label")) + 8) if edge.get("label") and "labelAt" not in edge else 28
        expansion_cost = max(0, labeled_clearance - 28)
        can_channel = bool(edge.get("label")) and "labelAt" not in edge and edge.get("route", "auto") == "auto" \
            and not edge.get("fromSide") and not edge.get("toSide") and "channelX" not in edge and "channelY" not in edge
        prefer_channel = can_channel and expansion_cost > 4 * 28
        if prefer_channel:
            channel_label_edge_keys.add(stable_value_key(edge))
        constraints.append({
            "from": earlier["col"], "to": later["col"],
            "minimum": authored_node_width(earlier) / 2 + 28 + authored_node_width(later) / 2,
            "contributors": [f'rank {earlier["col"]}→{later["col"]} direct clearance',
                             f'rank {earlier["col"]}→{later["col"]} node width clearance',
                             node_width_contributor(earlier), node_width_contributor(later)],
        })
        if not prefer_channel and labeled_clearance > 28:
            feedback_constraints.append({
                "from": earlier["col"], "to": later["col"],
                "minimum": authored_node_width(earlier) / 2 + labeled_clearance + authored_node_width(later) / 2,
                "contributors": [f'rank {earlier["col"]}→{later["col"]} direct clearance',
                                 f'edge {workflow_edge_name(edge)} label mask',
                                 node_width_contributor(earlier), node_width_contributor(later)],
            })

    constraints.extend(_phase_or_group_constraints(phases, "phase", column_count, 5.6, 8, baseline_pitch, None))
    constraints.extend(_phase_or_group_constraints(groups, "group", column_count, 5.6, 20, baseline_pitch, None))

    active = list(constraints)
    col_xs, provenance = None, None
    for iteration in range(3):
        col_xs = [column_start + col * baseline_pitch for col in range(column_count)]
        provenance = [set() for _ in range(column_count)]
        ordered = sorted((c for c in active if _is_integer(c.get("from")) and _is_integer(c.get("to"))
                          and 0 <= c["from"] < c["to"] < column_count and _is_number(c.get("minimum"))),
                         key=lambda c: (c["to"], c["from"], c["minimum"]))
        for to_col in range(1, column_count):
            for constraint in ordered:
                if constraint["to"] != to_col:
                    continue
                from_col = constraint["from"]
                candidate = col_xs[from_col] + constraint["minimum"]
                candidate_provenance = provenance[from_col] | set(as_array(constraint.get("contributors")))
                if candidate > col_xs[to_col] + 0.0001:
                    col_xs[to_col], provenance[to_col] = candidate, candidate_provenance
                elif (abs(candidate - col_xs[to_col]) <= 0.0001
                      and candidate > column_start + to_col * baseline_pitch + 0.0001):
                    provenance[to_col].update(candidate_provenance)
        if iteration > 0 or not feedback_constraints:
            break
        active.extend(feedback_constraints)

    first_rank_nodes = [node for node in nodes if node.get("col") == 0]
    first_extent = max([46, *(authored_node_width(node) / 2 for node in first_rank_nodes)])
    left_shift = max(0, 40 + 8 + first_extent - col_xs[0])
    if left_shift:
        col_xs = [value + left_shift for value in col_xs]
        for node in first_rank_nodes:
            if abs(authored_node_width(node) / 2 - first_extent) <= 0.0001:
                for contributors in provenance:
                    contributors.add(node_width_contributor(node))

    unpinned_top = set()
    for edge in edges:
        if isinstance(edge.get("via"), list) or "channelX" in edge:
            continue
        if edge.get("fromSide") == "top":
            unpinned_top.add(edge.get("from"))
        if edge.get("toSide") == "top":
            unpinned_top.add(edge.get("to"))
    lane_order = {lane.get("id"): index for index, lane in enumerate(as_array(workflow.get("lanes")))}
    lanes = as_array(workflow.get("lanes"))
    header_shift, header_contributors = 0, set()
    for node_id in unpinned_top:
        node = nodes_by_id.get(node_id)
        if not node or not _is_integer(node.get("col")) or node["col"] < 0 or node["col"] >= column_count:
            continue
        lane_position = lane_order.get(node.get("lane"))
        lane = lanes[lane_position] if lane_position is not None else None
        if not lane:
            continue
        prefix = "EX" if lane.get("variant") == "exception" else str(lane_position + 1).zfill(2)
        header_right = 40 + 14 + text_units(f'{prefix} / {lane.get("label", "")}') * 6.2
        required_shift = header_right + 2 - col_xs[node["col"]]
        if required_shift > header_shift + 0.0001:
            header_shift, header_contributors = required_shift, {f'lane {lane.get("id")} label width'}
        elif required_shift > 0 and abs(required_shift - header_shift) <= 0.0001:
            header_contributors.add(f'lane {lane.get("id")} label width')
    if header_shift > 0:
        col_xs = [value + header_shift for value in col_xs]
        for items in provenance:
            items.update(header_contributors)

    measured_left_shift = 0
    for edge in edges:
        if stable_value_key(edge) not in channel_label_edge_keys:
            continue
        source, target = nodes_by_id.get(edge.get("from")), nodes_by_id.get(edge.get("to"))
        if not source or not target:
            continue
        center = (col_xs[source["col"]] + col_xs[target["col"]]) / 2
        measured_left_shift = max(measured_left_shift, 16 - (center - workflow_label_width(edge.get("label")) / 2))
    for phase in phases:
        start, end = phase.get("fromCol"), phase.get("toCol")
        if not _is_integer(start) or not _is_integer(end) or start < 0 or end >= len(col_xs):
            continue
        width = max(col_xs[end] - col_xs[start] + 92, text_units(phase.get("label")) * 5.6 + 8)
        left = col_xs[start] - 46 if start == end else (col_xs[start] + col_xs[end] - width) / 2
        measured_left_shift = max(measured_left_shift, 16 - left)
    for group in groups:
        start, end = group.get("fromCol"), group.get("toCol")
        if _is_integer(start) and _is_integer(end):
            measured_left_shift = max(measured_left_shift, 44 - readable_group_bounds(workflow, group, col_xs)["x"])
    if measured_left_shift > 0:
        col_xs = [value + measured_left_shift for value in col_xs]

    rightmost, rightmost_contributors = col_xs[-1] + 50, set(provenance[-1])
    for node in nodes:
        col = node.get("col")
        if not _is_integer(col) or col < 0 or col >= column_count:
            continue
        node_right = col_xs[col] + authored_node_width(node) / 2
        contributors = provenance[col] | {node_width_contributor(node)}
        if node_right > rightmost + 0.0001:
            rightmost, rightmost_contributors = node_right, set(contributors)
        elif abs(node_right - rightmost) <= 0.0001:
            rightmost_contributors.update(contributors)
    for group in groups:
        start, end = group.get("fromCol"), group.get("toCol")
        if not _is_integer(start) or not _is_integer(end) or start < 0 or end >= len(col_xs):
            continue
        bounds = readable_group_bounds(workflow, group, col_xs)
        group_right = bounds["x"] + bounds["width"]
        contributors = provenance[start] | provenance[end] | {f'group {group.get("id") or group.get("label")} label span'}
        contributors.update(node_width_contributor(node) for node in nodes
                            if node.get("lane") == group.get("lane") and start <= node.get("col", -1) <= end)
        if group_right > rightmost + 0.0001:
            rightmost, rightmost_contributors = group_right, set(contributors)
        elif abs(group_right - rightmost) <= 0.0001:
            rightmost_contributors.update(contributors)

    widest_lane = {"width": 0, "lane": None}
    for index, lane in enumerate(lanes):
        width = text_units(f'{str(index + 1).zfill(2)} / {lane.get("label", "")}') * 6.2 + 30
        if width > widest_lane["width"]:
            widest_lane = {"width": width, "lane": lane}
    rightmost_lane_width = math.ceil(rightmost - 40 + 8)
    lane_width = max(640, rightmost_lane_width, math.ceil(widest_lane["width"]))
    if lane_width > 640:
        if rightmost_lane_width == lane_width:
            width_contributors.update(rightmost_contributors)
        if math.ceil(widest_lane["width"]) == lane_width and widest_lane["lane"]:
            lane = widest_lane["lane"]
            width_contributors.add(f'lane {lane.get("id") or lane.get("label")} label width')

    def vertical_extent(lane_id=None):
        maximum, contributors = 0, set()
        for node in nodes:
            if lane_id is not None and node.get("lane") != lane_id:
                continue
            y_offset = _or_zero(node.get("yOffset"))
            height = authored_node_height(node)
            extent = height / 2 + abs(y_offset)
            contributor = f'node {node.get("id")} height {height}px' + (f' with yOffset {y_offset}px' if y_offset else "")
            if extent > maximum + 0.0001:
                maximum, contributors = extent, {contributor}
            elif abs(extent - maximum) <= 0.0001:
                contributors.add(contributor)
        return maximum, contributors

    shared_extent, shared_contributors = vertical_extent()
    lane_height = 30 + max(74, math.ceil(shared_extent * 2 + 8))
    independent = uses_independent_lane_measurement(workflow)
    base_heights = []
    for lane in lanes:
        if not independent:
            base_heights.append(lane_height)
            continue
        extent, contributors = vertical_extent(lane.get("id"))
        height = 30 + max(74, math.ceil(extent * 2 + 8))
        if height > 104:
            height_contributors.update(contributors)
        base_heights.append(height)
    if not independent and lane_height > 104:
        height_contributors.update(shared_contributors)

    groups_by_lane = {}
    for group in groups:
        groups_by_lane.setdefault(group.get("lane"), []).append(group)
    reserves = []
    for lane_index, lane in enumerate(lanes):
        base_content_height = base_heights[lane_index] - 30
        header = footer = 0
        for group in groups_by_lane.get(lane.get("id"), []):
            bounds = readable_group_bounds(workflow, group, col_xs)
            label_left = bounds["x"] + 10
            label_right = label_left + text_units(group.get("label")) * 5.6
            for node in nodes:
                col = node.get("col")
                if (node.get("lane") != group.get("lane") or not _is_integer(col)
                        or col < group.get("fromCol", 0) or col > group.get("toCol", -1)
                        or col < 0 or col >= len(col_xs)):
                    continue
                half_width = authored_node_width(node) / 2
                node_left, node_right = col_xs[col] - half_width, col_xs[col] + half_width
                overlaps_label = node_right > label_left and node_left < label_right
                top_offset = (base_content_height - authored_node_height(node)) / 2 + _or_zero(node.get("yOffset"))
                header = max(header, math.ceil((11 if overlaps_label else 9) - top_offset))
                bottom_margin = base_content_height - GROUP_FRAME_BOTTOM_INSET - top_offset - authored_node_height(node)
                footer = max(footer, math.ceil(1 - bottom_margin))
        reserves.append({"header": max(0, header), "footer": max(0, footer)})

    group_header_heights = [item["header"] for item in reserves]
    group_footer_heights = [item["footer"] for item in reserves]
    lane_heights = [base_heights[index] + item["header"] + item["footer"] for index, item in enumerate(reserves)]
    lane_gap = max(20, math.ceil(_or_zero(feedback.get("laneGapMin"))))
    for index, reserve in enumerate(group_header_heights):
        if reserve:
            lane = lanes[index]
            height_contributors.add(f'lane {lane.get("id") or lane.get("label")} group label clearance {reserve}px')
    for index, reserve in enumerate(group_footer_heights):
        if reserve:
            lane = lanes[index]
            height_contributors.add(f'lane {lane.get("id") or lane.get("label")} group frame containment {reserve}px')
    if lane_gap > 20:
        height_contributors.update(as_array(feedback.get("laneGapContributors")))

    return {
        "contract": "readable-v2", "laneX": 40, "laneY": 52, "laneW": lane_width,
        "laneH": lane_height, "laneHeights": lane_heights, "laneGap": lane_gap,
        "laneTitleH": 30, "groupHeaderHeights": group_header_heights,
        "groupFooterHeights": group_footer_heights, "colXs": col_xs,
        "nodeW": 92, "nodeH": 52, "defaultViewBoxWidth": 40 + lane_width + 16,
        "channelLabelEdgeKeys": channel_label_edge_keys,
        "widthContributors": sorted(width_contributors, key=lambda value: str(value)),
        "heightContributors": sorted(height_contributors, key=lambda value: str(value)),
    }
