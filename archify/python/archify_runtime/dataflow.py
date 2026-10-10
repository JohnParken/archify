"""Node-free native Dataflow renderer matching the canonical SVG renderer."""

import math
import os

from .common import DiagnosticError, diagnostic, esc, number, record_diagnostic, translate
from .drawing import (animate_attr, available_node_text_width, brand_label_fit_width,
                      brand_metadata, brand_top_rail_problem, focus_edge_attrs,
                      focus_node_attrs, focus_node_title, fitted_node_font_size,
                      minimum_node_text_width, render_brand_mark,
                      render_definitions, render_semantic_sigil, resolve_brand,
                      svg_accessible_text, svg_root_attrs, text_units)
from .flow_geometry import (anchor, authored_straight_route_attrs, automatic_port_spread,
                            chosen_side, clean_ambiguous_corridor_problems,
                            clean_border_run_problems, clean_crossing_problems,
                            clean_endpoint_side_problems, clean_flow_problems,
                            clean_label_canvas_problems, clean_label_route_clearance_problems,
                            clean_route_rhythm_problems, label_point, legacy_default_from_side,
                            legacy_default_to_side, polyline_path, route_points_value)
from .geometry import as_array, is_finite_point, rects_overlap
from .legend import render_legend, resolve_legend
from .text_fit import node_label_layout, wrap_header

NODE_TEXT_FIT = {"sublabelPreferred": 7, "sublabelMinimum": 6,
                 "tagPreferred": 7, "tagMinimum": 6}

LAYOUT = {
    "stageY": 46, "stageH": 36, "stageBottomPad": 74,
    "leftX": 100, "colGap": 215, "stageW": 168,
    "nodeW": 112, "nodeH": 58,
    "rowYs": [128, 242, 356, 470, 584], "labelH": 16,
}

COMPONENT_FILL = {
    "frontend": "c-frontend", "backend": "c-backend", "database": "c-database",
    "cloud": "c-cloud", "security": "c-security", "messagebus": "c-messagebus",
    "external": "c-external",
}
COMPONENT_TEXT = {
    "frontend": "t-frontend", "backend": "t-backend", "database": "t-database",
    "cloud": "t-cloud", "security": "t-security", "messagebus": "t-messagebus",
    "external": "t-external",
}
ARROW_CLASS = {
    "default": ("a-default", "arrowhead"), "emphasis": ("a-emphasis", "arrowhead-emphasis"),
    "security": ("a-security", "arrowhead-security"), "dashed": ("a-dashed", "arrowhead-dashed"),
}
EDGE_ACCENT = {
    "security": "t-edge-security", "emphasis": "t-edge-emphasis",
    "dashed": "t-edge-dashed", "default": "t-edge-default",
}


def _num(value):
    return number(value)


def _round(value):
    return math.floor(value + 0.5)


def _round_tenth(value):
    return math.floor(value * 10 + 0.5) / 10


def _layout_problem(message):
    return diagnostic("layout/constraint", message, {"diagramType": "dataflow"})


def _flow_label_size(flow):
    longest = max(text_units(flow.get("label")), text_units(flow.get("classification") or ""))
    width = _round(max(34, longest * 4.9 + 12) * 10) / 10
    return {"width": width, "height": 27 if flow.get("classification") else LAYOUT["labelH"]}


class DataflowRenderer:
    def __init__(self, diagram, quality=None):
        self.dataflow = diagram
        self.meta = diagram.get("meta") or {}
        self.quality = quality or os.environ.get("ARCHIFY_QUALITY_PROFILE") or None
        self.gate_profile = self.quality or self.meta.get("quality_profile")
        self.view_box = self.meta["viewBox"] if self.meta.get("viewBox") is not None else [940, 720]
        self.stages = as_array(diagram.get("stages"))
        self.flow_items = as_array(diagram.get("flows"))
        self.node_items = as_array(diagram.get("nodes"))
        self.composition_frames = [self._stage_frame(stage, index) for index, stage in enumerate(self.stages)]
        self.nodes = {}
        for node in self.node_items:
            self.nodes[node.get("id")] = self._measure_node(node)
        self.node_steps = {}
        for index, flow in enumerate(self.flow_items):
            self.node_steps.setdefault(flow.get("from"), index)
            self.node_steps.setdefault(flow.get("to"), index + 1)
        for index, node in enumerate(self.node_items):
            self.node_steps.setdefault(node.get("id"), index)
        self.automatic_ports = automatic_port_spread(self.flow_items, self.nodes, self._side_for)
        self.path_cache = {}
        self.diagnostics = []

    def stage_x(self, index):
        return LAYOUT["leftX"] + index * LAYOUT["colGap"]

    def _stage_frame(self, stage, index):
        return {"id": index, "label": stage.get("label", ""), "kind": "stage",
                "x": self.stage_x(index) - LAYOUT["stageW"] / 2,
                "y": LAYOUT["stageY"], "width": LAYOUT["stageW"],
                "height": self.view_box[1] - LAYOUT["stageY"] - LAYOUT["stageBottomPad"],
                "radius": 10}

    def _measure_node(self, node):
        width = node.get("width") or LAYOUT["nodeW"]
        height = node.get("height") or LAYOUT["nodeH"]
        cx = self.stage_x(node.get("stage", 0))
        row = node.get("row", 0)
        row_index = int(row) if _is_number(row) and row == int(row) else row
        y = LAYOUT["rowYs"][row_index] + (node.get("yOffset") or 0)
        return {**node, "width": width, "height": height, "cx": cx, "cy": y + height / 2,
                "x": cx - width / 2, "y": y}

    def _flow_sides(self, flow):
        source, target = self.nodes.get(flow.get("from")), self.nodes.get(flow.get("to"))
        return {
            "fromSide": chosen_side(flow.get("fromSide"), legacy_default_from_side(source, target)),
            "toSide": chosen_side(flow.get("toSide"), legacy_default_to_side(source, target)),
        }

    def _side_for(self, flow, endpoint):
        sides = self._flow_sides(flow)
        return sides["fromSide" if endpoint == "source" else "toSide"]

    def _route_via(self, flow, source, target, start, end):
        if flow.get("via") is not None:
            return flow["via"]
        route = flow.get("route") or "auto"
        if route == "straight":
            return []
        if route == "vertical-channel":
            x = flow.get("channelX")
            if x is None:
                x = start[0] + (44 if end[0] > start[0] else -44)
            return [[x, start[1]], [x, end[1]]]
        if route == "bottom-channel":
            y = flow.get("channelY")
            if y is None:
                y = max(source["y"] + source["height"], target["y"] + target["height"]) + 26
            return [[start[0], y], [end[0], y]]
        if route == "top-channel":
            y = flow.get("channelY")
            if y is None:
                y = min(source["y"], target["y"]) - 24
            return [[start[0], y], [end[0], y]]
        if abs(start[1] - end[1]) < 4:
            return []
        mid_x = start[0] + (end[0] - start[0]) / 2
        return [[mid_x, start[1]], [mid_x, end[1]]]

    def path_for(self, flow):
        cache_key = id(flow)
        if cache_key in self.path_cache:
            return self.path_cache[cache_key]
        source, target = self.nodes.get(flow.get("from")), self.nodes.get(flow.get("to"))
        ports = self.automatic_ports.get(cache_key, {})
        sides = self._flow_sides(flow)
        start = ports.get("from") or anchor(source, sides["fromSide"])
        end = ports.get("to") or anchor(target, sides["toSide"])
        raw = [start, *self._route_via(flow, source, target, start, end), end]
        points = []
        for point in raw:
            if not points or abs(point[0] - points[-1][0]) > 0.0001 or abs(point[1] - points[-1][1]) > 0.0001:
                points.append(point)
        if len(points) < 2:
            points.append(end)
        routed = {"d": polyline_path(points), "points": points}
        self.path_cache[cache_key] = routed
        return routed

    def _label_rects(self):
        labels = []
        for flow_index, flow in enumerate(self.flow_items):
            if not flow.get("label") or flow.get("from") not in self.nodes or flow.get("to") not in self.nodes:
                continue
            lx, ly = label_point(flow, self.path_for(flow)["points"])
            size = _flow_label_size(flow)
            labels.append({"relation": flow, "relationIndex": flow_index, "label": flow["label"],
                           "x": lx - size["width"] / 2, "y": ly - 11,
                           "width": size["width"], "height": size["height"], "lx": lx, "ly": ly})
        return labels

    def validate(self):
        problems = []
        details = []
        if len(self.nodes) != len(self.node_items):
            problems.append("Node ids must be unique.")

        stage_count = len(self.stages)
        for node in self.nodes.values():
            stage, row = node.get("stage"), node.get("row")
            if not _is_number(stage) or stage < 0 or stage >= stage_count:
                problems.append(f'Node "{node.get("id")}" uses invalid stage {stage} — valid stages are 0..{stage_count - 1}.')
            if not _is_number(row) or row < 0 or row >= len(LAYOUT["rowYs"]):
                problems.append(f'Node "{node.get("id")}" uses invalid row {row} — valid rows are 0..{len(LAYOUT["rowYs"]) - 1}.')
            if not is_finite_point(node.get("x"), node.get("y"), node.get("cx"), node.get("cy")):
                problems.append(f'Node "{node.get("id")}" produced non-finite coordinates — check stage, row, width, height, and yOffset are numbers.')
                continue
            if node["x"] < 24 or node["x"] + node["width"] > self.view_box[0] - 24:
                problems.append(f'Node "{node.get("id")}" exceeds the horizontal bounds of the viewBox — reduce node.width or increase meta.viewBox[0].')
            if node["y"] < LAYOUT["stageY"] + LAYOUT["stageH"] + 22 or node["y"] + node["height"] > self.view_box[1] - LAYOUT["stageBottomPad"]:
                problems.append(f'Node "{node.get("id")}" exceeds the readable diagram area — keep y between {LAYOUT["stageY"] + LAYOUT["stageH"] + 22} and {self.view_box[1] - LAYOUT["stageBottomPad"]} (adjust row/yOffset or increase meta.viewBox[1]).')
            estimate = text_units(node.get("label")) * 6.2
            if estimate > node["width"] + 6:
                problems.append(f'Label "{node.get("label")}" (~{_round(estimate)}px) is wider than node "{node.get("id")}" ({number(node["width"])}px) — shorten the label or increase node.width.')
            mark = resolve_brand(node.get("brand"))
            brand_problem = brand_top_rail_problem(node, node["width"], 8, mark, "Node")
            if brand_problem:
                problems.append(brand_problem)
            available = available_node_text_width(node["width"])
            for field, value, minimum in (("Sublabel", node.get("sublabel"), NODE_TEXT_FIT["sublabelMinimum"]),
                                           ("Tag", node.get("tag"), NODE_TEXT_FIT["tagMinimum"])):
                if not value:
                    continue
                minimum_width = minimum_node_text_width(value, minimum)
                if minimum_width > available:
                    problems.append(f'{field} "{value}" needs ~{math.ceil(minimum_width)}px at the {minimum}px legible minimum, but node "{node.get("id")}" provides {number(available)}px — shorten the {field.lower()} or increase node.width.')

        for left_index, left_item in enumerate(self.node_items):
            left = self.nodes.get(left_item.get("id"))
            for right_item in self.node_items[left_index + 1:]:
                right = self.nodes.get(right_item.get("id"))
                if left and right and rects_overlap(left, right, 10):
                    problems.append(f'Nodes "{left.get("id")}" and "{right.get("id")}" are less than 10px apart — move one to another stage/row or adjust yOffset.')

        for flow in self.flow_items:
            if flow.get("from") not in self.nodes:
                problems.append(f'Flow "{flow.get("label") or flow.get("from")}" references unknown source "{flow.get("from")}".')
            if flow.get("to") not in self.nodes:
                problems.append(f'Flow "{flow.get("label") or flow.get("to")}" references unknown target "{flow.get("to")}".')
            if not flow.get("label"):
                problems.append(f'Flow "{flow.get("from")}" -> "{flow.get("to")}" must include a short data label.')
            if flow.get("from") in self.nodes and flow.get("to") in self.nodes:
                routed = self.path_for(flow)
                start, end = routed["points"][0], routed["points"][-1]
                distance = math.hypot(end[0] - start[0], end[1] - start[1])
                if distance < 34:
                    problems.append(f'Flow "{flow.get("label")}" is too short ({_round(distance)}px; minimum 34px) — route it through a channel or spread its nodes.')
                if isinstance(flow.get("via"), list):
                    points = routed["points"]
                    for segment_index, (start_point, end_point) in enumerate(zip(points, points[1:])):
                        diagonal = abs(start_point[0] - end_point[0]) > 0.01 and abs(start_point[1] - end_point[1]) > 0.01
                        if diagonal:
                            via_index = min(segment_index, len(flow["via"]) - 1)
                            problems.append(f'Flow "{flow.get("label")}" has a diagonal segment from ({start_point[0]}, {start_point[1]}) to ({end_point[0]}, {end_point[1]}) — align via[{via_index}] with its adjacent point by sharing the same x or y coordinate.')

        known_ids = set(self.nodes)
        side_hint = 'keep automatic routing, or choose fromSide/toSide and via points whose first and final segments cross node borders perpendicularly'
        details.extend(clean_endpoint_side_problems(self.flow_items, known_ids, self.path_for, self._side_for, side_hint))
        details.extend(clean_flow_problems(self.flow_items, list(self.nodes.values()), self.path_for,
            'adjust fromSide/toSide, set route/via or channelX/channelY, or move the node to another stage/row'))
        details.extend(clean_crossing_problems(self.flow_items, known_ids, self.path_for, self.gate_profile,
            'adjust route/via or channelX/channelY so the flows use separate stage corridors'))
        details.extend(clean_ambiguous_corridor_problems(self.flow_items, known_ids, self.path_for,
            self.gate_profile, 'adjust route/via or channelX/channelY so unrelated flows do not visually merge'))
        details.extend(clean_border_run_problems(self.flow_items, known_ids, self.composition_frames, self.path_for,
            self.gate_profile, 'adjust route/via or channelX/channelY so the flow crosses the stage perpendicularly instead of following its border'))
        details.extend(clean_route_rhythm_problems(self.flow_items, known_ids, self.path_for, self.gate_profile,
            'adjust route/via or channelX/channelY so each turn uses a clear inter-stage corridor'))

        label_rects = self._label_rects()
        for rect in label_rects:
            for node in self.nodes.values():
                if rects_overlap(rect, node, -2):
                    suggestion = self._label_obstacle_fix(rect, node, label_rects)
                    problems.append(f'Label "{rect["label"]}" overlaps node "{node["id"]}" — adjust labelDx/labelDy/labelSegment or set labelAt.\n{suggestion}')
        for index, left in enumerate(label_rects):
            for right in label_rects[index + 1:]:
                if rects_overlap(left, right, -2):
                    problems.append(f'Labels "{left["label"]}" and "{right["label"]}" overlap — adjust labelDx/labelDy.\n{self._label_pair_fix(left, right)}')
        details.extend(clean_label_route_clearance_problems(self.flow_items, label_rects, known_ids, self.path_for,
            self.gate_profile, 'adjust labelAt, labelDx, labelDy, or labelSegment; otherwise adjust the other flow route/via/channelX/channelY'))
        details.extend(clean_label_canvas_problems(label_rects, self.view_box, self.gate_profile,
            'adjust labelAt, labelDx, labelDy, or labelSegment; otherwise enlarge meta.viewBox'))

        last_stage_x = self.stage_x(len(self.stages) - 1)
        if last_stage_x + LAYOUT["stageW"] / 2 > self.view_box[0] - 24:
            problems.append(f'Stages exceed viewBox width — set meta.viewBox[0] to at least {math.ceil(last_stage_x + LAYOUT["stageW"] / 2 + 24)}.')

        fatal_details = [entry for entry in details if entry["severity"] == "error"]
        self.diagnostics = details
        for entry in details:
            if entry["severity"] == "warning":
                record_diagnostic(entry)
        if problems or fatal_details:
            diagnostics = [_layout_problem(message) for message in problems] + fatal_details
            messages = problems + [entry["message"] for entry in fatal_details]
            raise DiagnosticError("Data-flow layout validation failed:\n- " + "\n- ".join(messages), diagnostics)

    def _label_obstacle_fix(self, rect, obstacle, labels):
        from .flow_geometry import suggest_label_obstacle_fix
        return suggest_label_obstacle_fix(rect, rect["lx"], rect["ly"], obstacle, self.view_box, self.nodes.values())

    @staticmethod
    def _label_pair_fix(a, b):
        from .flow_geometry import suggest_label_pair_fix
        return "\n".join(suggest_label_pair_fix(a, b))

    def _stage_header_text(self, stage, index):
        return f'{index + 1:02d} / {stage["label"]}'

    def _render_stage_header(self, stage, index, cx):
        text = self._stage_header_text(stage, index)
        font = fitted_node_font_size(text, LAYOUT["stageW"], 9, 7)
        available = available_node_text_width(LAYOUT["stageW"])
        open_tag = (f'<text x="{cx}" y="{LAYOUT["stageY"] + 22}" class="t-dim" font-size="{_num(font)}" '
                    'font-weight="600" text-anchor="middle">')
        if minimum_node_text_width(text, font) <= available:
            return f'{open_tag}{esc(text)}</text>'
        lines = wrap_header(text, available, font)
        first_node_y = min([node["y"] for node in self.nodes.values() if node.get("stage") == index]
                           + [self.view_box[1] - LAYOUT["stageBottomPad"]])
        last_line_bottom = LAYOUT["stageY"] + 22 + (len(lines) - 1) * (font + 4) + font * 0.3
        if last_line_bottom > first_node_y - 4:
            return f'{open_tag}{esc(text)}</text>'
        if len(lines) == 1:
            return f'{open_tag}{esc(text)}</text>'
        tspans = "".join(f'<tspan x="{cx}" dy="{_num(font + 4) if line_index else 0}">{esc(line)}</tspan>'
                         for line_index, line in enumerate(lines))
        return f'{open_tag}{tspans}</text>'

    def render_stage(self, stage, index):
        frame = self.composition_frames[index]
        cx = self.stage_x(index)
        return (f'        <rect data-graph-role="structural-frame" data-composition-frame-kind="stage" '
                f'data-composition-frame-id="{index}" x="{_num(frame["x"])}" y="{_num(frame["y"])}" '
                f'width="{_num(frame["width"])}" height="{_num(frame["height"])}" rx="{_num(frame["radius"])}" '
                'class="c-lane" stroke-width="1"/>\n'
                f'        {self._render_stage_header(stage, index, cx)}')

    def render_node(self, node):
        fill = COMPONENT_FILL.get(node.get("type"), "c-external")
        accent = COMPONENT_TEXT.get(node.get("type"), "t-muted")
        has_sublabel = node.get("sublabel") is not None and node.get("sublabel") != ""
        mark = resolve_brand(node.get("brand"))
        label_font = fitted_node_font_size(node.get("label"), brand_label_fit_width(node, node["width"], mark), 10, 8)
        sub_font = fitted_node_font_size(node.get("sublabel"), node["width"], NODE_TEXT_FIT["sublabelPreferred"], NODE_TEXT_FIT["sublabelMinimum"])
        tag_font = fitted_node_font_size(node.get("tag"), node["width"], NODE_TEXT_FIT["tagPreferred"], NODE_TEXT_FIT["tagMinimum"])
        text_rows = [{"text": node.get("label", ""), "font": label_font, "y": 21}]
        if has_sublabel:
            text_rows.append({"text": node["sublabel"], "font": sub_font, "y": 37})
        if node.get("tag"):
            text_rows.append({"text": node["tag"], "font": tag_font, "y": node["height"] - 11})
        label_layout = node_label_layout(node["width"], node["height"], text_rows, brand=bool(mark), source=False)
        sub = (f'\n          <text data-detail="context" x="{_num(node["cx"])}" y="{_num(node["y"] + label_layout["ys"][1])}" '
               f'class="t-muted" font-size="{_num(sub_font)}" text-anchor="middle">{esc(node["sublabel"])}</text>') if has_sublabel else ""
        tag_index = 2 if has_sublabel else 1
        tag = (f'\n        <text data-detail="fine" x="{_num(node["cx"])}" y="{_num(node["y"] + label_layout["ys"][tag_index])}" '
               f'class="{accent}" font-size="{_num(tag_font)}" text-anchor="middle">{esc(node["tag"])}</text>') if node.get("tag") else ""
        stage_value = node.get("stage")
        stage_index = int(stage_value) if _is_number(stage_value) and stage_value == int(stage_value) else None
        stage = self.stages[stage_index] if stage_index is not None and 0 <= stage_index < len(self.stages) else None
        context = f'{node["stage"] + 1:02d} / {stage["label"]}' if stage else translate(
            self.meta, "node.context.dataflow")
        brand = render_brand_mark(mark, node["x"] + node["width"] - 22, node["y"] + 6)
        passport = {"kind": node.get("type"), "sublabel": node.get("sublabel"), "tag": node.get("tag"),
                    "context": context, **brand_metadata(mark)}
        node_attrs = focus_node_attrs(node, passport, self.meta)
        title = focus_node_title(node.get("label", ""), passport)
        brand_markup = f'\n          {brand}' if brand else ""
        detail_anchor = ' data-detail-anchor=""' if has_sublabel else ""
        return (f'        <g {node_attrs}>\n'
                f'          {title}\n'
                f'          <rect x="{_num(node["x"])}" y="{_num(node["y"])}" width="{_num(node["width"])}" height="{_num(node["height"])}" rx="6" class="c-mask"/>\n'
                f'          <rect x="{_num(node["x"])}" y="{_num(node["y"])}" width="{_num(node["width"])}" height="{_num(node["height"])}" rx="6" class="{fill}"{animate_attr(self.meta, "node", self.node_steps.get(node.get("id")))} stroke-width="1.5"/>\n'
                f'          {render_semantic_sigil(node.get("type"), node["x"] + 6, node["y"] + label_layout["sigilY"], node.get("icon"), label_layout["sigilSize"])}{brand_markup}\n'
                f'          <text data-node-label=""{detail_anchor} x="{_num(node["x"] + label_layout["x"])}" y="{_num(node["y"] + label_layout["ys"][0])}" '
                f'class="t-primary" font-size="{_num(label_font)}" font-weight="600" text-anchor="middle">{esc(node.get("label", ""))}</text>{sub}{tag}\n'
                '        </g>')

    def render_flow_path(self, flow, index):
        class_name, marker = ARROW_CLASS.get(flow.get("variant") or "default", ARROW_CLASS["default"])
        routed = self.path_for(flow)
        stroke = flow.get("width") or (1.8 if flow.get("variant") == "emphasis" else 1.4)
        attrs = authored_straight_route_attrs(flow, routed["points"])
        return (f'        <path {focus_edge_attrs(flow, index)} data-composition-points="{route_points_value(routed["points"])}"'
                f'{attrs} d="{routed["d"]}" class="{class_name}"{animate_attr(self.meta, "edge", index)} '
                f'stroke-width="{_num(stroke)}" marker-end="url(#{marker})"/>')

    def render_flow_label(self, flow, index):
        routed = self.path_for(flow)
        lx, ly = label_point(flow, routed["points"])
        label_size = _flow_label_size(flow)
        classification = (f'\n        <text data-detail="fine" x="{_num(lx)}" y="{_num(ly + 11)}" class="t-dim" '
                          f'font-size="7" text-anchor="middle">{esc(flow["classification"])}</text>') if flow.get("classification") else ""
        accent = EDGE_ACCENT.get(flow.get("variant"), "t-edge-default")
        return (f'        <g data-detail="context" {focus_edge_attrs(flow, index)}>\n'
                f'          <rect x="{_num(lx - label_size["width"] / 2)}" y="{_num(ly - 11)}" width="{_num(label_size["width"])}" '
                f'height="{_num(label_size["height"])}" rx="4" class="c-mask"/>\n'
                f'          <text x="{_num(lx)}" y="{_num(ly)}" class="{accent}" font-size="8" text-anchor="middle">{esc(flow.get("label", ""))}</text>{classification}\n'
                '        </g>')

    def _legend_catalog(self):
        items = [
            {"kind": "emphasis", "className": "a-emphasis", "marker": "arrowhead-emphasis",
             "strokeWidth": 1.8, "swatchWidth": 34, "swatchGap": 9, "interactive": False},
            {"kind": "security", "className": "a-security", "marker": "arrowhead-security",
             "swatchWidth": 34, "swatchGap": 9, "interactive": False},
            {"kind": "dashed", "className": "a-dashed", "marker": "arrowhead-dashed",
             "swatchWidth": 34, "swatchGap": 9, "interactive": False},
            {"kind": "database"},
            {"kind": "default", "className": "a-default", "marker": "arrowhead",
             "swatchWidth": 34, "swatchGap": 9, "interactive": False},
        ]
        return [{**item, "label": translate(self.meta, f'legend.dataflow.{item["kind"]}')}
                for item in items]

    def render_legend(self):
        present = {flow.get("variant") or "default" for flow in self.flow_items}
        if any(node.get("type") == "database" for node in self.nodes.values()):
            present.add("database")
        entries = resolve_legend(self.meta.get("legend"), self._legend_catalog(), present)
        layout = {"x": 40, "baselineY": self.view_box[1] - 36, "width": self.view_box[0] - 80,
                  "minTitleY": self.view_box[1] - 66,
                  "unfit": "hide" if "legend" not in self.meta else "error", "diagramType": "dataflow"}

        def swatch(entry):
            if entry["kind"] == "database":
                return (f'<rect x="{entry["x"]}" y="{_num(entry["baseline"] - 8)}" width="14" height="9" '
                        'rx="2" class="c-database" stroke-width="1"/>')
            return (f'<path d="M {entry["x"]} {_num(entry["baseline"] - 3)} L {entry["x"] + 34} {_num(entry["baseline"] - 3)}" '
                    f'class="{entry["className"]}" stroke-width="{_num(entry.get("strokeWidth") or 1.4)}" '
                    f'marker-end="url(#{entry["marker"]})"/>')

        return render_legend(entries, layout, swatch, self.meta)

    def render_svg(self):
        reader_fit = "" if self.meta.get("viewBox") is not None else ' data-reader-fit="intrinsic-height"'
        parts = [f'      <svg viewBox="0 0 {_num(self.view_box[0])} {_num(self.view_box[1])}"{reader_fit} {svg_root_attrs(self.meta, self.quality)}>',
                 svg_accessible_text(self.meta, "dataflow"), render_definitions(), "",
                 '        <!-- Background Grid -->', '        <rect width="100%" height="100%" fill="url(#grid)" />', "",
                 '        <!-- Data Stages -->', "\n\n".join(self.render_stage(stage, index) for index, stage in enumerate(self.stages)), "",
                 '        <!-- Flow paths -->', "\n".join(self.render_flow_path(flow, index) for index, flow in enumerate(self.flow_items)), "",
                 '        <!-- Nodes -->', "\n\n".join(self.render_node(node) for node in self.nodes.values()), "",
                 '        <!-- Flow labels -->', "\n".join(self.render_flow_label(flow, index) for index, flow in enumerate(self.flow_items)), "",
                 '        <!-- Legend -->', self.render_legend(), '      </svg>']
        return "\n".join(parts)

    def render(self):
        self.validate()
        return self.render_svg()


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render_dataflow(diagram, quality=None):
    """Render a schema-validated Dataflow diagram to its SVG string."""
    return DataflowRenderer(diagram, quality=quality).render()
