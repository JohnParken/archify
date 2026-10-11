"""Node-free native renderer for Archify Lifecycle diagrams (schema v1/v2)."""

import math
import os

from .architecture_router import create_router
from .architecture_router import rounded_path
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
                            legacy_default_to_side, route_points_value,
                            suggest_label_obstacle_fix, suggest_label_pair_fix)
from .geometry import as_array, is_finite_point, rects_overlap
from .legend import legend_footprint, render_legend, resolve_legend
from .lifecycle_grid import LifecycleGridRouter
from .text_fit import node_label_layout
from .architecture_labels import place_automatic_labels
from .architecture_labels import reserved_label_rect

LAYOUT = {
    "phaseY": 126, "eventY": 278, "outcomeY": 450,
    "phaseW": 118, "phaseH": 62, "eventW": 126, "eventH": 58,
    "outcomeW": 118, "outcomeH": 58,
    "phaseXs": [94, 248, 402, 556, 710],
    "eventXs": [402, 556, 710], "outcomeXs": [402, 556, 710],
}
LAYOUT_V2 = {"stateW": 140, "stateH": 64, "marginX": 60, "minGap": 64,
             "floorGap": 44, "firstRowTop": 56, "rowPitch": 184}
TYPE_CLASS = {"start": "c-frontend", "active": "c-frontend", "waiting": "c-cloud",
              "decision": "c-database", "success": "c-backend", "failure": "c-security",
              "neutral": "c-external", "external": "c-external"}
TEXT_CLASS = {"start": "t-frontend", "active": "t-frontend", "waiting": "t-cloud",
              "decision": "t-database", "success": "t-backend", "failure": "t-security",
              "neutral": "t-muted", "external": "t-muted"}
ARROW_CLASS = {"default": ("a-default", "arrowhead"), "emphasis": ("a-emphasis", "arrowhead-emphasis"),
               "security": ("a-security", "arrowhead-security"), "dashed": ("a-dashed", "arrowhead-dashed")}
EDGE_ACCENT = {"security": "t-edge-security", "emphasis": "t-edge-emphasis",
               "dashed": "t-edge-dashed", "default": "t-edge-default"}


class LifecycleRenderer:
    def __init__(self, diagram, quality=None):
        self.lifecycle = diagram
        self.meta = diagram.get("meta") or {}
        self.quality = quality or os.environ.get("ARCHIFY_QUALITY_PROFILE") or None
        self.gate_profile = self.quality or self.meta.get("quality_profile")
        self.is_v2 = diagram.get("schema_version") == 2
        self.states_items = as_array(diagram.get("states"))
        self.transitions = as_array(diagram.get("transitions"))
        self.lanes = as_array(diagram.get("lanes"))
        self.lane_labels = {lane.get("id"): lane.get("label") for lane in self.lanes}
        self.state_text_fit = ({"labelPreferred": 11, "labelMinimum": 9,
                                "sublabelPreferred": 8, "sublabelMinimum": 7,
                                "tagPreferred": 8, "tagMinimum": 7, "step": 8}
                               if self.is_v2 else
                               {"labelPreferred": 10, "labelMinimum": 8,
                                "sublabelPreferred": 7, "sublabelMinimum": 6,
                                "tagPreferred": 7, "tagMinimum": 6, "step": 7})
        self.v2_column_centers = self._v2_column_centers()
        self.v2_rows = self.lane_row_order()
        self.v2_row_top = ({lane: LAYOUT_V2["firstRowTop"] + index * LAYOUT_V2["rowPitch"]
                            for index, lane in enumerate(self.v2_rows)} if self.is_v2 else {})
        self.states = {state.get("id"): self._measure_state(state) for state in self.states_items}
        self.planned_transitions = [transition for transition in self.transitions if self.planner_routed(transition)]
        self.use_grid_router = self.is_v2
        if self.is_v2:
            probe = LifecycleGridRouter(self.states, self.planned_transitions,
                row_of=self.row_of, column_xs=self.v2_column_centers)
            self.use_grid_router = all(
                not transition.get(key) or transition.get(key) == "auto"
                or transition.get(key) == probe.connection_sides(transition)[key]
                for transition in self.planned_transitions for key in ("fromSide", "toSide"))
            top = LAYOUT_V2["firstRowTop"]
            for index, lane_id in enumerate(self.v2_rows):
                row_states = [state for state in self.states.values() if state.get("lane") == lane_id]
                row_height = max([LAYOUT_V2["stateH"]]
                                 + [state["y"] + state["height"] - self.v2_row_top[lane_id]
                                    for state in row_states])
                self.v2_row_top[lane_id] = top
                gap = max(LAYOUT_V2["rowPitch"] - LAYOUT_V2["stateH"],
                          probe.gap_height(index) if self.use_grid_router else 0)
                top += row_height + gap
            self.states = {state.get("id"): self._measure_state(state) for state in self.states_items}
        # The fallback router reserves labels while planning later routes. Its
        # viewport and legend footprint depend only on the measured states, so
        # establish them before constructing that router.
        self.authored_outgoing = {transition.get("from") for transition in self.transitions}
        self.entries = self._legend_entries()
        self.authored_view_box = self.meta.get("viewBox")
        if self.is_v2 and self.authored_view_box is None:
            finite = list(self.states.values())
            max_right = max([0] + [s["cx"] + s["width"] / 2 for s in finite if math.isfinite(s["cx"] + s["width"] / 2)])
            width = max(640, math.ceil(max_right + LAYOUT_V2["marginX"]))
            footprint = legend_footprint(self.entries, width - 80)
            states_bottom = max([0] + [s["y"] + s["height"] for s in finite if math.isfinite(s["y"] + s["height"])])
            self.view_box = [width, math.ceil(states_bottom + footprint["extraHeight"] + 96)]
        else:
            self.view_box = self.authored_view_box or [980, 660]
        self.legend_extra_height = legend_footprint(self.entries, self.view_box[0] - 80)["extraHeight"]
        if self.use_grid_router:
            self.planner = LifecycleGridRouter(self.states, self.planned_transitions,
                row_of=self.row_of, column_xs=self.v2_column_centers)
        else:
            self.planner = create_router(self.states, self.planned_transitions,
                label_rect_for=self._reserved_transition_label)
        self.automatic_ports = automatic_port_spread(
            [transition for transition in self.transitions if not self.planner_routed(transition)],
            self.states, side_for=lambda transition, endpoint: self.transition_sides(transition)[
                "fromSide" if endpoint == "source" else "toSide"])
        self.authored_outgoing = {transition.get("from") for transition in self.transitions}
        self.state_steps = self._state_steps()
        self.resolved_label_points = {}
        self.path_cache = {}
        self.validation_diagnostics = []

    def _v2_column_centers(self):
        if not self.is_v2:
            return []
        authored = self.states_items
        widths = [max([LAYOUT_V2["stateW"]] + [state.get("width") or 0 for state in authored if state.get("col") == col])
                  for col in range(5)]
        cols = [state.get("col") for state in authored if _integer(state.get("col")) and 0 <= state["col"] <= 4]
        last_col = max(cols) if cols else 0
        gaps = [LAYOUT_V2["minGap"]] * 4
        by_id = {state.get("id"): state for state in authored}
        for transition in self.transitions:
            source, target = by_id.get(transition.get("from")), by_id.get(transition.get("to"))
            if (not (transition.get("label") or transition.get("note")) or not source or not target
                    or source.get("lane") != target.get("lane") or not _integer(source.get("col"))
                    or not _integer(target.get("col")) or abs(source["col"] - target["col"]) != 1):
                continue
            gap = min(source["col"], target["col"])
            if 0 <= gap < 4:
                gaps[gap] = max(gaps[gap], math.ceil(self.transition_label_width(transition) + 24))
        smallest = min([self.state_text_fit["step"]] + [
            size for state in authored for size in self.state_font_sizes(state, state.get("width") or LAYOUT_V2["stateW"]).values()])
        budget = math.floor(930 * smallest / 6)
        fixed = LAYOUT_V2["marginX"] * 2 + sum(widths[:last_col + 1])
        used = gaps[:last_col]
        excess = fixed + sum(used) - budget
        slack = sum(gap - LAYOUT_V2["floorGap"] for gap in used)
        if excess > 0 and slack > 0:
            ratio = min(1, excess / slack)
            for index, gap in enumerate(used):
                gaps[index] = math.floor(gap - (gap - LAYOUT_V2["floorGap"]) * ratio)
        centers, x = [], LAYOUT_V2["marginX"]
        for col in range(5):
            centers.append(x + widths[col] / 2)
            x += widths[col] + (gaps[col] if col < len(gaps) else 0)
        return centers

    def transition_label_width(self, transition):
        longest = max(text_units(transition.get("label")), text_units(transition.get("note") or ""))
        return max(32, longest * 4.9 + 12)

    def _reserved_transition_label(self, transition, points, context):
        label_text = transition.get("label") or transition.get("note")
        if not label_text:
            return None
        label = {"relation": transition, "label": label_text,
                 **self.transition_label_box_at(transition, label_point(transition, points))}
        return reserved_label_rect(
            label=label,
            points=points,
            routes=[{"relationIndex": index, "points": route}
                    for index, route in enumerate(context["routes"])],
            labels=[{"relation": transition, "relationIndex": -2, **rect}
                    for rect in context["labels"]],
            components=list(self.states.values()),
        )

    def state_font_sizes(self, state, width):
        mark = resolve_brand(state.get("brand"))
        return {
            "label": fitted_node_font_size(state.get("label"), brand_label_fit_width(state, width, mark),
                self.state_text_fit["labelPreferred"], self.state_text_fit["labelMinimum"]),
            "sublabel": fitted_node_font_size(state.get("sublabel"), width,
                self.state_text_fit["sublabelPreferred"], self.state_text_fit["sublabelMinimum"]),
            "tag": fitted_node_font_size(state.get("tag"), width,
                self.state_text_fit["tagPreferred"], self.state_text_fit["tagMinimum"]),
        }

    def lane_row_order(self):
        populated = {state.get("lane") for state in self.states_items}
        ordered = ["main"] if "main" in populated else []
        ordered.extend(lane.get("id") for lane in self.lanes
                       if lane.get("id") not in ("main", "terminal") and lane.get("id") in populated)
        if "terminal" in populated:
            ordered.append("terminal")
        return ordered

    def row_of(self, state):
        try:
            return self.v2_rows.index(state.get("lane"))
        except ValueError:
            return None

    def band_for(self, lane):
        return "phase" if lane == "main" else "outcome" if lane == "terminal" else "event"

    def _measure_state(self, state):
        if self.is_v2:
            width, height = state.get("width") or LAYOUT_V2["stateW"], state.get("height") or LAYOUT_V2["stateH"]
            col = state.get("col")
            cx = self.v2_column_centers[col] if _integer(col) and 0 <= col < len(self.v2_column_centers) else math.nan
            y = self.v2_row_top.get(state.get("lane"), math.nan) + (state.get("yOffset") or 0)
        else:
            band = self.band_for(state.get("lane"))
            width = state.get("width") or LAYOUT[f"{band}W"]
            height = state.get("height") or LAYOUT[f"{band}H"]
            xs = LAYOUT[f"{band}Xs"]
            col = state.get("col")
            cx = xs[col] if _integer(col) and 0 <= col < len(xs) else xs[-1]
            y = LAYOUT[f"{band}Y"] + (state.get("yOffset") or 0)
        return {**state, "width": width, "height": height, "x": cx - width / 2,
                "y": y, "cx": cx, "cy": y + height / 2}

    def lifecycle_area_bottom(self):
        return self.view_box[1] - self.legend_extra_height - 96 if self.is_v2 else self.view_box[1] - 122

    def legend_y(self):
        return self.view_box[1] - 36

    def planner_routed(self, transition):
        return (transition.get("via") is None
                and transition.get("route") in (None, "auto")
                and "channelX" not in transition and "channelY" not in transition)

    def transition_sides(self, transition):
        if self.planner_routed(transition):
            return self.planner.connection_sides(transition)
        source, target = self.states.get(transition.get("from")), self.states.get(transition.get("to"))
        if not source or not target:
            return {"fromSide": "right", "toSide": "left"}
        return {"fromSide": chosen_side(transition.get("fromSide"), legacy_default_from_side(source, target)),
                "toSide": chosen_side(transition.get("toSide"), legacy_default_to_side(source, target))}

    def _route_via(self, transition, source, target, start, end, from_side, to_side):
        if transition.get("via") is not None:
            return transition["via"]
        route = transition.get("route") or "auto"
        if route == "straight":
            return []
        if route == "drop":
            y = transition.get("channelY", (start[1] + end[1]) / 2)
            return [[start[0], y], [end[0], y]]
        if route == "bottom-channel":
            y = transition.get("channelY", max(source["y"] + source["height"], target["y"] + target["height"]) + 34)
            return [[start[0], y], [end[0], y]]
        if route == "top-channel":
            y = transition.get("channelY", min(source["y"], target["y"]) - 28)
            return [[start[0], y], [end[0], y]]
        if route == "right-channel":
            x = transition.get("channelX", max(source["x"] + source["width"], target["x"] + target["width"]) + 36)
            return [[x, start[1]], [x, end[1]]]
        if route == "left-channel":
            x = transition.get("channelX", min(source["x"], target["x"]) - 36)
            return [[x, start[1]], [x, end[1]]]
        if start[0] == end[0] or start[1] == end[1]:
            return []
        from_vertical, to_vertical = from_side in ("top", "bottom"), to_side in ("top", "bottom")
        if from_vertical != to_vertical:
            return [[start[0], end[1]]] if from_vertical else [[end[0], start[1]]]
        if from_vertical:
            y = (start[1] + end[1]) / 2
            return [[start[0], y], [end[0], y]]
        x = (start[0] + end[0]) / 2
        return [[x, start[1]], [x, end[1]]]

    def path_for(self, transition):
        key = id(transition)
        if key in self.path_cache:
            return self.path_cache[key]
        if self.planner_routed(transition):
            planned = self.planner.path_for(transition)
            if isinstance(planned, dict):
                routed = planned
            else:
                routed = {"points": planned,
                          "d": rounded_path(planned, transition.get("cornerRadius", 10))}
        else:
            source, target = self.states.get(transition.get("from")), self.states.get(transition.get("to"))
            if not source or not target:
                return {"points": [[0, 0], [0, 0]], "d": "M 0 0 L 0 0"}
            ports = self.automatic_ports.get(key, {})
            sides = self.transition_sides(transition)
            start = ports.get("from") or anchor(source, sides["fromSide"])
            end = ports.get("to") or anchor(target, sides["toSide"])
            via = self._route_via(transition, source, target, start, end, sides["fromSide"], sides["toSide"])
            if ports and not via and abs(start[0] - end[0]) >= 4 and abs(start[1] - end[1]) >= 4:
                middle = (start[0] + end[0]) / 2
                via = [[middle, start[1]], [middle, end[1]]]
            points = [start, *via, end]
            routed = {"points": points, "d": rounded_path(points, transition.get("cornerRadius", 10))}
        self.path_cache[key] = routed
        return routed

    def transition_label_box_at(self, transition, point):
        width = self.transition_label_width(transition)
        height = 27 if transition.get("label") and transition.get("note") else 16
        lx, ly = point
        return {"x": lx - width / 2, "y": ly - 11, "width": width,
                "height": height, "lx": lx, "ly": ly}

    def transition_label_box(self, transition):
        points = self.resolved_label_points.get(id(transition)) or label_point(transition, self.path_for(transition)["points"])
        return self.transition_label_box_at(transition, points)

    def transition_label_rects(self):
        result = []
        for index, transition in enumerate(self.transitions):
            if not (transition.get("label") or transition.get("note")):
                continue
            if transition.get("from") not in self.states or transition.get("to") not in self.states:
                continue
            result.append({"relation": transition, "relationIndex": index,
                           "label": transition.get("label") or transition.get("note"),
                           **self.transition_label_box(transition)})
        return result

    def band_geometry(self):
        if self.is_v2:
            result = []
            for index, lane_id in enumerate(self.v2_rows):
                cy = self.v2_row_top[lane_id] + LAYOUT_V2["stateH"] / 2
                label = self.lane_labels.get(lane_id) or lane_id
                length = text_units(label) * 6.2
                result.append({"index": index, "label": label, "vertical": True,
                    "cx": 18, "cy": cy, "x": 11, "y": cy - length / 2,
                    "width": 14, "height": length})
            return result
        main = next((lane for lane in self.lanes if lane.get("id") == "main"), None)
        terminal = next((lane for lane in self.lanes if lane.get("id") == "terminal"), None)
        events = [lane for lane in self.lanes if lane.get("id") not in ("main", "terminal")]
        populated = {self.band_for(state.get("lane")) for state in self.states.values()}
        titles = [main.get("label") if main else "Lifecycle phases",
                  " + ".join(lane.get("label", "") for lane in events) if events else "Interruptions + recovery",
                  terminal.get("label") if terminal else "Outcomes"]
        result = []
        for index, title in enumerate(titles):
            band = ("phase", "event", "outcome")[index]
            if band not in populated:
                continue
            label = f"{index + 1:02d} / {title}"
            baseline = (100, 252, 424)[index]
            result.append({"index": index, "band": band, "label": label, "x": 72,
                "y": baseline - 11, "width": text_units(label) * 6.2, "height": 14,
                "baseline": baseline})
        return result

    def _state_steps(self):
        steps = {}
        for index, transition in enumerate(self.transitions):
            steps.setdefault(transition.get("from"), index)
            steps.setdefault(transition.get("to"), index + 1)
        for index, state in enumerate(self.states_items):
            steps.setdefault(state.get("id"), index)
        return steps

    def is_final(self, state):
        if state.get("id") in self.authored_outgoing:
            return False
        if self.is_v2:
            return True
        if self.band_for(state.get("lane")) != "phase":
            return True
        cols = [item["col"] for item in self.states.values() if self.band_for(item.get("lane")) == "phase"]
        return state.get("col") == max(cols)

    def _legend_entries(self):
        catalog = [{"kind": kind,
                    "label": translate(self.meta, f"legend.lifecycle.{kind}"),
                    **({"swatchWidth": 26} if kind == "start" else {})}
                   for kind in ("start", "active", "waiting", "decision", "success", "failure", "neutral", "external")]
        present = {state.get("type") for state in self.states.values()}
        entries = resolve_legend(self.meta.get("legend"), catalog, present)
        if entries and any(self.is_final(state) for state in self.states.values()):
            entries.append({"kind": "final", "label": translate(self.meta, "legend.lifecycle.final"),
                            "interactive": False, "present": True, "swatchWidth": 16})
        return entries

    def _validate(self):
        problems, details = [], []
        if len(self.states) != len(self.states_items):
            problems.append("State ids must be unique.")
        if not self.is_v2 and self.lifecycle_area_bottom() + 4 < 448:
            problems.append(f"viewBox height {self.view_box[1]} is too short for the fixed band layout — set meta.viewBox[1] to at least 566.")
        lane_ids = {lane.get("id") for lane in self.lanes}
        if len(lane_ids) != len(self.lanes):
            problems.append("Lane ids must be unique.")
        if "main" not in lane_ids:
            problems.append('Lifecycle diagrams need a lane with id "main" (the phase rail). Lane ids "main" and "terminal" are reserved: "main" maps to the top phase band, "terminal" to the bottom outcome band, and all other lanes share the middle event band.')
        for state in self.states.values():
            if state.get("lane") not in lane_ids:
                problems.append(f'State "{state.get("id")}" uses unknown lane "{state.get("lane")}".')
                continue
            col = state.get("col")
            if self.is_v2:
                valid = _integer(col) and 0 <= col <= 4
                max_col = 4
            else:
                band = self.band_for(state.get("lane"))
                max_col = len(LAYOUT[f"{band}Xs"])
                valid = _integer(col) and 0 <= col < max_col
            if not valid:
                if self.is_v2:
                    problems.append(f'State "{state.get("id")}" uses invalid column {col} — every lifecycle row has integer columns 0..4.')
                else:
                    problems.append(f'State "{state.get("id")}" uses invalid column {col} — the {band} band has integer columns 0..{max_col - 1}.')
                continue
            if not is_finite_point(state.get("x"), state.get("y"), state.get("cx"), state.get("cy")):
                problems.append(f'State "{state.get("id")}" produced non-finite coordinates — check col, width, height, and yOffset are numbers.')
                continue
            x_min = 28 if self.is_v2 else 32
            if state["x"] < x_min or state["x"] + state["width"] > self.view_box[0] - x_min:
                problems.append(f'State "{state.get("id")}" exceeds the horizontal bounds of the diagram — reduce state.width{", lower its col," if self.is_v2 else ""} or increase meta.viewBox[0].')
            low_y = 44 if self.is_v2 else 64
            if state["y"] < low_y or state["y"] + state["height"] > self.lifecycle_area_bottom():
                problems.append(f'State "{state.get("id")}" exceeds the vertical lifecycle area — keep y between {low_y} and {self.lifecycle_area_bottom()} (adjust yOffset or increase meta.viewBox[1]).')
            estimated = text_units(state.get("label")) * 6.2
            if estimated > state["width"] + 6:
                problems.append(f'Label "{state.get("label")}" (~{math.floor(estimated + 0.5)}px) is wider than state "{state.get("id")}" ({number(state["width"])}px) — shorten the label or increase state.width.')
            mark = resolve_brand(state.get("brand"))
            brand_problem = brand_top_rail_problem(state, state["width"], 8, mark, "State")
            if brand_problem:
                problems.append(brand_problem)
            available = available_node_text_width(state["width"])
            for field, value, minimum in (("Sublabel", state.get("sublabel"), self.state_text_fit["sublabelMinimum"]),
                                          ("Tag", state.get("tag"), self.state_text_fit["tagMinimum"])):
                if value and minimum_node_text_width(value, minimum) > available:
                    needed = math.ceil(minimum_node_text_width(value, minimum))
                    problems.append(f'{field} "{value}" needs ~{needed}px at the {minimum}px legible minimum, but state "{state.get("id")}" provides {available}px — shorten the {field.lower()} or increase state.width.')
        all_states = list(self.states.values())
        for i, left in enumerate(all_states):
            for right in all_states[i + 1:]:
                if rects_overlap(left, right, 10):
                    suffix = "." if self.is_v2 else ' (lanes other than "main"/"terminal" share one band).'
                    problems.append(f'States "{left.get("id")}" and "{right.get("id")}" are less than 10px apart — move one to another col or separate them with yOffset{suffix}')
        for transition in self.transitions:
            source_id, target_id = transition.get("from"), transition.get("to")
            if source_id not in self.states:
                problems.append(f'Transition "{transition.get("label") or source_id}" references unknown source "{source_id}".')
            if target_id not in self.states:
                problems.append(f'Transition "{transition.get("label") or target_id}" references unknown target "{target_id}".')
            if source_id in self.states and target_id in self.states:
                points = self.path_for(transition)["points"]
                distance = math.hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])
                if distance < 32:
                    title = transition.get("label") or f"{source_id}->{target_id}"
                    problems.append(f'Transition "{title}" is too short ({math.floor(distance + 0.5)}px; minimum 32px) — route it through a channel or drop its label.')
        known = set(self.states)
        side_for = lambda transition, endpoint: self.transition_sides(transition)["fromSide" if endpoint == "source" else "toSide"]
        details.extend(clean_endpoint_side_problems(self.transitions, known, self.path_for, side_for,
            "keep automatic routing, or choose fromSide/toSide and via points whose first and final segments cross state borders perpendicularly",
            "lifecycle", "transitions", should_check_relation=lambda transition, _index: transition.get("via") is None))
        details.extend(clean_flow_problems(self.transitions, self.states.values(), self.path_for,
            "adjust fromSide/toSide, set route/via or channelX/channelY, or move the state with col/yOffset",
            diagram_type="lifecycle", relation_collection="transitions", obstacle_kind="state"))
        details.extend(clean_crossing_problems(self.transitions, known, self.path_for, self.gate_profile,
            "adjust route/via or channelX/channelY so the transitions use separate lifecycle corridors",
            diagram_type="lifecycle", relation_collection="transitions",
            crossing_resolved=lambda left, right, _hit: self.planner_routed(left) and self.planner_routed(right)))
        details.extend(clean_ambiguous_corridor_problems(self.transitions, known, self.path_for, self.gate_profile,
            "adjust route/via or channelX/channelY so unrelated transitions do not visually merge",
            diagram_type="lifecycle", relation_collection="transitions"))
        details.extend(clean_border_run_problems(self.transitions, known, [], self.path_for, self.gate_profile,
            "adjust route/via or channelX/channelY so the transition crosses the band perpendicularly instead of following its border",
            diagram_type="lifecycle", relation_collection="transitions"))
        details.extend(clean_route_rhythm_problems(self.transitions, known, self.path_for, self.gate_profile,
            "move route/via or channel coordinates so each lifecycle turn has a readable run-up",
            diagram_type="lifecycle", relation_collection="transitions"))

        label_rects = self.transition_label_rects()
        if self.meta.get("quality_profile") == "showcase":
            for rect in label_rects:
                for title in self.band_geometry():
                    if rects_overlap(rect, title):
                        message = f'Transition {rect["relationIndex"]} label "{rect["label"]}" overlaps lifecycle band title "{title["label"]}" — move the label with labelAt/labelDx/labelDy/labelSegment or provide more space.'
                        details.append(diagnostic("composition/label-band-title-overlap", message,
                            {"diagramType": "lifecycle", "collection": "transitions", "index": rect["relationIndex"],
                             "from": rect["relation"].get("from"), "to": rect["relation"].get("to")},
                            {"labelRect": {k: rect[k] for k in ("x", "y", "width", "height")}, "bandTitle": title},
                            ["move the transition label with labelAt/labelDx/labelDy/labelSegment while preserving its text"]))
        for rect in label_rects:
            for state in self.states.values():
                if rects_overlap(rect, state, -2):
                    suggestion = suggest_label_obstacle_fix(rect, rect["lx"], rect["ly"], state,
                        self.view_box, self.states.values())
                    problems.append(f'Label "{rect["label"]}" overlaps state "{state["id"]}" — adjust labelDx/labelDy/labelSegment or set labelAt.\n{suggestion}')
        for index, left in enumerate(label_rects):
            for right in label_rects[index + 1:]:
                if rects_overlap(left, right, -2):
                    problems.append(f'Labels "{left["label"]}" and "{right["label"]}" overlap — adjust labelDx/labelDy.\n' + "\n".join(suggest_label_pair_fix(left, right)))
        details.extend(clean_label_route_clearance_problems(self.transitions, label_rects, known, self.path_for,
            self.gate_profile, "adjust labelAt, labelDx, labelDy, or labelSegment; otherwise adjust the other transition route/via/channelX/channelY",
            diagram_type="lifecycle", relation_collection="transitions"))
        details.extend(clean_label_canvas_problems(label_rects, self.view_box, self.gate_profile,
            "adjust labelAt, labelDx, labelDy, or labelSegment; otherwise enlarge meta.viewBox",
            diagram_type="lifecycle", relation_collection="transitions"))
        errors = [entry for entry in details if entry.get("severity") == "error"]
        for entry in details:
            if entry.get("severity") == "warning":
                record_diagnostic(entry)
        self.validation_diagnostics = details
        if problems or errors:
            layout_diagnostics = [diagnostic("layout/constraint", message, {"diagramType": "lifecycle"}) for message in problems]
            messages = problems + [entry["message"] for entry in errors]
            raise DiagnosticError("Lifecycle layout validation failed:\n- " + "\n- ".join(messages), layout_diagnostics + errors)

    def _place_labels(self):
        if self.meta.get("quality_profile") != "showcase":
            return
        labels = self.transition_label_rects()
        placed = place_automatic_labels(
            labels=labels,
            routes=[{"relationIndex": index, "points": self.path_for(transition)["points"]}
                    for index, transition in enumerate(self.transitions)
                    if transition.get("from") in self.states and transition.get("to") in self.states],
            components=list(self.states.values()), titles=self.band_geometry(),
            view_box=self.view_box, placement_bottom=self.lifecycle_area_bottom(),
            keep_fallback_near_route=self.is_v2, grid_sweep=self.is_v2)
        self.resolved_label_points = {id(rect["relation"]): [rect["lx"], rect["ly"]] for rect in placed}

    def _render_bands(self):
        output = []
        for band in self.band_geometry():
            if band.get("vertical"):
                output.append(f'        <text x="{number(band["cx"])}" y="{number(band["cy"])}" class="t-dim" font-size="10" font-weight="600" writing-mode="vertical-rl" text-anchor="middle">{esc(band["label"])}</text>')
            else:
                output.append(f'        <path d="M {number(band["x"])} {number(band["baseline"] + 12)} L {number(self.view_box[0] - band["x"])} {number(band["baseline"] + 12)}" class="a-default" stroke-width="0.8" stroke-dasharray="3,8"/>\n        <text x="{number(band["x"])}" y="{number(band["baseline"])}" class="t-dim" font-size="10" font-weight="600">{esc(band["label"])}</text>')
        return "\n".join(output)

    def _render_state(self, state):
        fill, accent = TYPE_CLASS.get(state.get("type"), "c-external"), TEXT_CLASS.get(state.get("type"), "t-muted")
        has_sub = state.get("sublabel") not in (None, "")
        sizes = self.state_font_sizes(state, state["width"])
        text_rows = [{"text": state.get("label", ""), "font": sizes["label"], "y": 23 if self.is_v2 else 21}]
        if has_sub:
            text_rows.append({"text": state["sublabel"], "font": sizes["sublabel"], "y": 40 if self.is_v2 else 37})
        if state.get("tag"):
            text_rows.append({"text": state["tag"], "font": sizes["tag"], "y": state["height"] - (12 if self.is_v2 else 11)})
        mark = resolve_brand(state.get("brand"))
        label_layout = node_label_layout(state["width"], state["height"], text_rows,
            side="left", brand=bool(mark), source=bool(state.get("sources")),
            step=state.get("step") or "")
        sub = (f'\n          <text data-detail="context" x="{number(state["cx"])}" y="{number(state["y"] + label_layout["ys"][1])}" class="t-muted" font-size="{number(sizes["sublabel"])}" text-anchor="middle">{esc(state["sublabel"])}</text>' if has_sub else "")
        tag_index = 2 if has_sub else 1
        tag = (f'\n        <text data-detail="fine" x="{number(state["cx"])}" y="{number(state["y"] + label_layout["ys"][tag_index])}" class="{accent}" font-size="{number(sizes["tag"])}" text-anchor="middle">{esc(state["tag"])}</text>' if state.get("tag") else "")
        step = (f'\n        <text data-detail="fine" x="{number(state["x"] + 23)}" y="{number(state["y"] + 14)}" class="{accent}" font-size="{self.state_text_fit["step"]}" font-weight="700">{esc(state["step"])}</text>' if state.get("step") else "")
        brand = render_brand_mark(mark, state["x"] + state["width"] - 22, state["y"] + 6)
        initial = (f'\n          <g aria-hidden="true" data-lifecycle-initial-marker="">\n            {self._initial_marker_shape(state["x"] - 22, state["x"] - 1, state["cy"])}\n          </g>' if state.get("type") == "start" else "")
        final_border = (f'\n          <rect x="{number(state["x"] + 3)}" y="{number(state["y"] + 3)}" width="{number(state["width"] - 6)}" height="{number(state["height"] - 6)}" rx="4" class="{fill}" style="fill: none" stroke-width="1"/>' if self.is_final(state) else "")
        passport = {"kind": state.get("type"), "sublabel": state.get("sublabel"), "tag": state.get("tag"),
                    "context": self.lane_labels.get(state.get("lane")) or translate(self.meta, "node.context.lifecycle"),
                    **brand_metadata(mark)}
        brand_markup = f"\n          {brand}" if brand else ""
        detail_anchor = ' data-detail-anchor=""' if has_sub else ""
        return (f'        <g {focus_node_attrs(state, passport, self.meta)}>\n'
                f'          {focus_node_title(state.get("label", ""), passport)}\n'
                f'          <rect x="{number(state["x"])}" y="{number(state["y"])}" width="{number(state["width"])}" height="{number(state["height"])}" rx="7" class="c-mask"/>\n'
                f'          <rect x="{number(state["x"])}" y="{number(state["y"])}" width="{number(state["width"])}" height="{number(state["height"])}" rx="7" class="{fill}"{animate_attr(self.meta, "node", self.state_steps.get(state.get("id")))} stroke-width="1.5"/>{final_border}{initial}\n'
                f'          {render_semantic_sigil(state.get("type"), state["x"] + 6, state["y"] + label_layout["sigilY"], state.get("icon"), label_layout["sigilSize"])}{brand_markup}{step}\n'
                f'          <text data-node-label=""{detail_anchor} x="{number(state["x"] + label_layout["x"])}" y="{number(state["y"] + label_layout["ys"][0])}" class="t-primary" font-size="{number(sizes["label"])}" font-weight="600" text-anchor="middle">{esc(state.get("label", ""))}</text>{sub}{tag}\n'
                '        </g>')

    @staticmethod
    def _initial_marker_shape(dot_x, tip_x, y):
        return (f'<circle cx="{number(dot_x)}" cy="{number(y)}" r="4.5" style="fill: var(--arrow-emphasis)"/>'
                f'<path d="M {number(dot_x + 4.5)} {number(y)} L {number(tip_x - 6)} {number(y)}" class="a-emphasis" stroke-width="1.4"/>'
                f'<path d="M {number(tip_x - 7)} {number(y - 3.5)} L {number(tip_x)} {number(y)} L {number(tip_x - 7)} {number(y + 3.5)} Z" style="fill: var(--arrow-emphasis)"/>')

    def _effective_variant(self, transition):
        if transition.get("variant"):
            return transition["variant"]
        if self.is_v2:
            source, target = self.states.get(transition.get("from")), self.states.get(transition.get("to"))
            if source and target and source.get("lane") == target.get("lane") == "main" and target.get("col", 0) > source.get("col", 0):
                return "emphasis"
        return "default"

    def _render_transition_path(self, transition, index):
        variant = self._effective_variant(transition)
        cls, marker = ARROW_CLASS.get(variant, ARROW_CLASS["default"])
        routed = self.path_for(transition)
        width = transition.get("width") or (1.6 if self.is_v2 else 2) if variant == "emphasis" else transition.get("width") or 1.1
        auto = self.planner_routed(transition)
        crossover = ' data-composition-crossover="halo"' if auto else ""
        edge = (f'        <path {focus_edge_attrs(transition, index)} data-composition-points="{route_points_value(routed["points"])}"{crossover}'
                f'{authored_straight_route_attrs(transition, routed["points"])} d="{routed["d"]}" class="{cls}"{animate_attr(self.meta, "edge", index)} stroke-width="{number(width)}" marker-end="url(#{marker})"/>')
        if not auto:
            return edge
        underlay = (f'          <path data-graph-role="automatic-crossover-underlay" d="{routed["d"]}" fill="none" stroke="var(--mask)" stroke-width="{number(width + 4)}" stroke-linecap="round" stroke-linejoin="round" pointer-events="none"/>\n')
        return f'        <g data-graph-role="automatic-crossover" style="--step:{index}">\n{underlay}{edge.replace("        ", "          ", 1)}\n        </g>'

    def _render_transition_label(self, transition, index):
        if not (transition.get("label") or transition.get("note")):
            return ""
        box = self.transition_label_box(transition)
        lx, ly, label_w = box["lx"], box["ly"], box["width"]
        label = (f'\n          <text x="{number(lx)}" y="{number(ly)}" class="{EDGE_ACCENT.get(self._effective_variant(transition), "t-edge-default")}" font-size="8" text-anchor="middle">{esc(transition["label"])}</text>' if transition.get("label") else "")
        note = (f'\n        <text data-detail="fine" x="{number(lx)}" y="{number(ly + (11 if transition.get("label") else 0))}" class="t-dim" font-size="7" text-anchor="middle">{esc(transition["note"])}</text>' if transition.get("note") else "")
        detail = "context" if transition.get("label") else "fine"
        return (f'        <g data-detail="{detail}" {focus_edge_attrs(transition, index)}>\n'
                f'          <rect x="{number(lx - label_w / 2)}" y="{number(ly - 11)}" width="{number(label_w)}" height="{number(box["height"])}" rx="4" class="c-mask"/>{label}{note}\n'
                '        </g>')

    def _render_swatch(self, entry):
        if entry["kind"] == "start":
            return self._initial_marker_shape(entry["x"] + 4.5, entry["x"] + 24, entry["baseline"] - 3.5)
        if entry["kind"] == "final":
            return (f'<rect x="{number(entry["x"])}" y="{number(entry["baseline"] - 8)}" width="14" height="9" rx="2" class="c-external" stroke-width="1"/>'
                    f'<rect x="{number(entry["x"] + 2.5)}" y="{number(entry["baseline"] - 5.5)}" width="9" height="4" rx="1" class="c-external" style="fill: none" stroke-width="0.8"/>')
        return f'<rect x="{number(entry["x"])}" y="{number(entry["baseline"] - 8)}" width="14" height="9" rx="2" class="{TYPE_CLASS.get(entry["kind"], "c-external")}" stroke-width="1"/>'

    def _render_legend(self):
        return render_legend(self.entries,
            {"x": 40, "baselineY": self.legend_y(), "width": self.view_box[0] - 80,
             "minTitleY": self.lifecycle_area_bottom() + 8,
             "unfit": "hide" if "legend" not in self.meta else "error", "diagramType": "lifecycle"},
            self._render_swatch, self.meta)

    def _render_rail(self):
        if self.is_v2:
            return ""
        main_cols = [state.get("col") for state in self.states.values() if self.band_for(state.get("lane")) == "phase"]
        if not main_cols:
            return ""
        end = LAYOUT["phaseXs"][max(main_cols)] + 38
        return f'        <path data-lifecycle-rail="" d="M 154 {number(LAYOUT["phaseY"] + 31)} L {number(end)} {number(LAYOUT["phaseY"] + 31)}" class="a-emphasis" stroke-width="2.2" marker-end="url(#arrowhead-emphasis)"/>'

    def render(self):
        self._place_labels()
        self._validate()
        reader_fit = "" if self.authored_view_box else ' data-reader-fit="intrinsic-height"'
        states_svg = "\n\n".join(self._render_state(state) for state in self.states.values())
        return (f'      <svg viewBox="0 0 {number(self.view_box[0])} {number(self.view_box[1])}"{reader_fit} {svg_root_attrs(self.meta, self.quality)}>\n'
                f'{svg_accessible_text(self.meta, "lifecycle")}\n{render_definitions()}\n\n'
                '        <!-- Background Grid -->\n'
                '        <rect width="100%" height="100%" fill="url(#grid)" />\n\n'
                '        <!-- Lifecycle bands -->\n'
                f'{self._render_bands()}\n\n'
                '        <!-- Primary lifecycle rail -->\n'
                f'{self._render_rail()}\n\n'
                '        <!-- Transition paths -->\n'
                + "\n".join(self._render_transition_path(transition, index) for index, transition in enumerate(self.transitions))
                + '\n\n        <!-- States -->\n' + states_svg
                + '\n\n        <!-- Transition labels -->\n'
                + "\n".join(self._render_transition_label(transition, index) for index, transition in enumerate(self.transitions))
                + '\n\n        <!-- Legend -->\n' + self._render_legend() + '\n      </svg>')


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def render_lifecycle(diagram, quality=None):
    """Render a validated Lifecycle diagram as an SVG string."""
    return LifecycleRenderer(diagram, quality).render()
