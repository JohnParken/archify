"""Node-free Python port of the Archify Sequence SVG renderer."""

import math
from .common import DiagnosticError, diagnostic, esc, js_round, number, problems, translate
from .drawing import (
    animate_attr, available_node_text_width, brand_label_fit_width,
    brand_metadata, brand_top_rail_problem, focus_edge_attrs, focus_node_attrs,
    focus_node_title, fitted_node_font_size, minimum_node_text_width,
    render_brand_mark, render_definitions, render_semantic_sigil,
    resolve_brand, svg_accessible_text, svg_root_attrs, text_units,
)
from .geometry import (
    as_array, is_finite_point, rects_overlap, route_points_value, segment_intersects_rect,
    segment_rect_clearance,
)


LEGEND_CATALOG = (
    {"kind": "emphasis", "className": "a-emphasis", "marker": "arrowhead-emphasis", "strokeWidth": 1.8},
    {"kind": "return", "className": "a-default", "marker": "arrowhead", "dash": "3,5"},
    {"kind": "security", "className": "a-security", "marker": "arrowhead-security"},
    {"kind": "dashed", "className": "a-dashed", "marker": "arrowhead-dashed"},
    {"kind": "default", "className": "a-default", "marker": "arrowhead"},
)
COMPONENT_FILL = {
    "frontend": "c-frontend", "backend": "c-backend", "database": "c-database",
    "cloud": "c-cloud", "security": "c-security", "messagebus": "c-messagebus",
    "external": "c-external",
}
ARROW_CLASS = {
    "default": ("a-default", "arrowhead"), "emphasis": ("a-emphasis", "arrowhead-emphasis"),
    "security": ("a-security", "arrowhead-security"), "dashed": ("a-dashed", "arrowhead-dashed"),
    "return": ("a-default", "arrowhead"),
}
EDGE_ACCENT = {
    "security": "t-edge-security", "emphasis": "t-edge-emphasis",
    "dashed": "t-edge-dashed", "default": "t-edge-default", "return": "t-edge-default",
}

SIDE_MARGIN = 62
LEGEND_CONTENT_GAP = 12
LEGEND_BLOCK_HEIGHT = 86


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _problem(message, code="layout/constraint", subject=None, evidence=None, fixes=None, severity="error"):
    return diagnostic(code, message, subject or {"diagramType": "sequence"}, evidence,
                      fixes, severity)


def _round_px(value):
    return js_round(value)


def _round_tenth(value):
    return math.floor(value * 10 + 0.5) / 10


def _fmt_point(point, precision=0):
    if precision == 1:
        values = [_round_tenth(value) for value in point]
    else:
        values = [_round_px(value) for value in point]
    return ", ".join(number(value) for value in values)


def _relationship_subject(index, message):
    subject = {"diagramType": "sequence", "collection": "messages", "index": index}
    for key in ("id", "from", "to"):
        if message.get(key):
            subject[key] = message[key]
    return subject


def _legend_entries(meta, messages):
    config = meta.get("legend") or {}
    mode = config.get("mode") or "auto"
    if mode == "hidden":
        return []
    present = {message.get("variant") or "default" for message in messages}
    overrides = config.get("entries") or {}
    entries = []
    for item in LEGEND_CATALOG:
        kind = item["kind"]
        override = overrides.get(kind) or {}
        selected = mode == "all" or kind in present
        visible = override.get("visible") is True or (selected and override.get("visible") is not False)
        if not visible:
            continue
        entry = {
            **item,
            "interactive": False,
            "swatchWidth": 34,
            "swatchGap": 9,
            "label": override.get("label") or translate(meta, f"legend.sequence.{kind}"),
            "present": kind in present,
        }
        entries.append(entry)
    return entries


def _legend_footprint(entries, width):
    if not entries:
        return {"measured": [], "rows": [], "rowCount": 0, "minWidth": 0, "extraHeight": 0}
    measured = []
    for entry in entries:
        measured.append({**entry, "width": math.ceil(
            entry.get("swatchWidth", 14) + entry.get("swatchGap", 8)
            + text_units(entry["label"]) * 8 * 0.62
            + (21 if entry.get("interactive") else 0)
        )})
    rows = [[]]
    cursor = 0
    for entry in measured:
        row = rows[-1]
        required = (22 if row else 0) + entry["width"]
        if row and cursor + required > width:
            rows.append([entry])
            cursor = entry["width"]
        else:
            row.append(entry)
            cursor += required
    return {"measured": measured, "rows": rows, "rowCount": len(rows),
            "minWidth": max(entry["width"] for entry in measured),
            "extraHeight": (len(rows) - 1) * 22}


class SequenceRenderer:
    def __init__(self, diagram, quality=None):
        self.sequence = diagram
        self.meta = diagram.get("meta") or {}
        self.quality = quality
        self.gate_profile = quality or self.meta.get("quality_profile")
        self.messages = as_array(diagram.get("messages"))
        self.participant_items = as_array(diagram.get("participants"))
        self.segments = as_array(diagram.get("segments"))
        self.activations = as_array(diagram.get("activations"))
        self.entries = _legend_entries(self.meta, self.messages)
        self.content_bottom = max(
            [0]
            + [message.get("y", 0) + (22 if message.get("note") else 6) for message in self.messages
               if _is_number(message.get("y"))]
            + [activation.get("to", 0) for activation in self.activations if _is_number(activation.get("to"))]
            + [segment.get("to", 0) for segment in self.segments if _is_number(segment.get("to"))]
        )
        authored_view_box = self.meta.get("viewBox")
        if authored_view_box is None:
            required = self.legend_required_height(920)
            self.view_box = [920, max(760, required)]
        else:
            self.view_box = authored_view_box
        self.column_fit = "spread" if self.meta.get("column_fit") == "spread" else "fixed"
        self.participant_count = max(1, len(self.participant_items))
        self.participant_width = (
            max(86, min(190, _round_px((self.view_box[0] - SIDE_MARGIN * 2) / self.participant_count) - 24))
            if self.column_fit == "spread" else 86
        )
        self.column_gap = (
            max(108, (self.view_box[0] - 40 - SIDE_MARGIN - self.participant_width) / (self.participant_count - 1))
            if self.column_fit == "spread" and self.participant_count > 1 else 108
        )
        self.readable_messages = self.meta.get("quality_profile") == "showcase"
        self.message_font_size = 11 if self.readable_messages else 9
        self.message_unit_width = 6.6 if self.readable_messages else 5.2
        self.label_height = 18 if self.readable_messages else 16
        self.layout = {
            "topY": 72, "participantW": self.participant_width, "participantH": 60,
            "participantLabelY": 36, "participantSublabelY": 50, "lifelineTop": 142,
            "lifelineBottom": self.view_box[1] - 65, "legendY": self.view_box[1] - 54,
            "leftX": SIDE_MARGIN + self.participant_width / 2 if self.column_fit == "spread" else SIDE_MARGIN,
            "colGap": self.column_gap, "labelH": self.label_height,
        }
        self.participants = {}
        for index, participant in enumerate(self.participant_items):
            cx = self.layout["leftX"] + index * self.layout["colGap"]
            self.participants[participant.get("id")] = {
                **participant, "index": index, "cx": cx,
                "x": cx - self.participant_width / 2, "y": 72,
                "width": self.participant_width, "height": 60, "cy": 102,
            }

    def legend_required_height(self, width):
        if not self.entries:
            return 0
        footprint = _legend_footprint(self.entries, width - 80)
        return math.ceil(self.content_bottom + LEGEND_CONTENT_GAP + LEGEND_BLOCK_HEIGHT + footprint["extraHeight"])

    def message_geometry(self, message):
        source = self.participants.get(message.get("from"))
        target = self.participants.get(message.get("to"))
        if not source or not target or not _is_number(message.get("y")):
            return None
        direction = 1 if target["cx"] > source["cx"] else -1
        start = source["cx"] + direction * 7
        end = target["cx"] - direction * 7
        return {"start": start, "end": end, "center": (start + end) / 2}

    def message_label_box(self, message, relation_index=None):
        geometry = self.message_geometry(message)
        if not geometry:
            return None
        width = max(34, text_units(message.get("label")) * self.message_unit_width + 12)
        return {"relation": message, "relationIndex": relation_index, "label": message.get("label", ""),
                "x": geometry["center"] - width / 2, "y": message["y"] - 20,
                "width": width, "height": self.layout["labelH"]}

    def message_route_box(self, message):
        geometry = self.message_geometry(message)
        if not geometry:
            return None
        return {"x": min(geometry["start"], geometry["end"]), "y": message["y"] - 2,
                "width": abs(geometry["end"] - geometry["start"]), "height": 4}

    def segment_label_box(self, segment):
        width = max(42, text_units(segment.get("label")) * 5.2 + 14)
        occupied = []
        for message in self.messages:
            occupied.extend(rect for rect in (self.message_label_box(message), self.message_route_box(message)) if rect)
        label = {"x": 56, "y": segment.get("from", 0) - 22, "width": width, "height": 18}
        for _ in range(4):
            if not any(rects_overlap(label, rect, 2) for rect in occupied):
                break
            label["y"] -= 22
        return label

    def frames(self):
        return [{"id": index, "label": segment.get("label", ""), "kind": "segment",
                 "x": 48, "y": segment.get("from", 0), "width": self.view_box[0] - 96,
                 "height": segment.get("to", 0) - segment.get("from", 0), "radius": 10}
                for index, segment in enumerate(self.segments)]

    def legend_layout(self):
        return {"x": 40, "baselineY": self.layout["legendY"], "width": self.view_box[0] - 80,
                "minTitleY": max(self.layout["lifelineTop"], self.content_bottom + LEGEND_CONTENT_GAP),
                "unfit": "hide" if self.meta.get("legend") is None else "error"}

    def measure_legend(self):
        if not self.entries:
            return {"entries": [], "rowCount": 0, "titleY": None}
        layout = self.legend_layout()
        footprint = _legend_footprint(self.entries, layout["width"])
        too_wide = next((entry for entry in footprint["measured"] if entry["width"] > layout["width"]), None)
        if too_wide:
            if layout["unfit"] == "hide":
                return None
            message = (f'[legend/label-too-wide] sequence legend label for "{too_wide["kind"]}" needs '
                       f'{too_wide["width"]}px but only {layout["width"]}px is available.')
            raise DiagnosticError(message, [diagnostic(
                "legend/label-too-wide", message,
                {"diagramType": "sequence", "path": f'/meta/legend/entries/{too_wide["kind"]}/label'},
                {"kind": too_wide["kind"], "measuredWidthPx": too_wide["width"],
                 "availableWidthPx": layout["width"]},
                ["shorten the legend label or use a wider viewBox"],
            )])
        title_y = layout["baselineY"] - footprint["extraHeight"] - 20
        top_y = title_y - 10
        if top_y < layout["minTitleY"]:
            if layout["unfit"] == "hide":
                return None
            message = (f'[legend/vertical-overflow] sequence legend needs {footprint["rowCount"]} rows, '
                       f'which would start at y={number(top_y)} above the available legend band at y={number(layout["minTitleY"])}.')
            raise DiagnosticError(message, [diagnostic(
                "legend/vertical-overflow", message,
                {"diagramType": "sequence", "path": "/meta/legend"},
                {"rowCount": footprint["rowCount"], "requiredTopY": top_y,
                 "availableTopY": layout["minTitleY"]},
                ["shorten legend labels, hide nonessential entries, or use a wider viewBox"],
            )])
        positioned = []
        for row_index, row in enumerate(footprint["rows"]):
            entry_x = layout["x"]
            baseline = layout["baselineY"] - (footprint["rowCount"] - row_index - 1) * 22
            for entry in row:
                positioned.append({**entry, "x": entry_x, "baseline": baseline, "row": row_index})
                entry_x += entry["width"] + 22
        return {"entries": positioned, "rowCount": footprint["rowCount"], "titleY": title_y, "fontSize": 8}

    def validate(self):
        issues = []
        if len(self.participants) != len(self.participant_items):
            issues.append("Participant ids must be unique.")

        if self.layout["lifelineBottom"] - self.layout["lifelineTop"] < 120:
            issues.append(f'viewBox height {number(self.view_box[1])} leaves under 120px of timeline — '
                          f'set meta.viewBox[1] to at least {self.layout["lifelineTop"] + 120 + 65}.')

        spread_note = (f'participant boxes are {number(self.participant_width)}px for this viewBox width and '
                       f'{self.participant_count} participants' if self.column_fit == "spread" else
                       f'participant boxes are a fixed {number(self.participant_width)}px unless meta.column_fit is "spread"')
        for participant in self.participants.values():
            label_width = text_units(participant.get("label")) * 6.8
            if label_width > self.participant_width + 6:
                issues.append(f'Label "{participant.get("label", "")}" (~{_round_px(label_width)}px) is wider than the '
                              f'{number(self.participant_width)}px participant box — shorten it.')
            mark = resolve_brand(participant.get("brand"))
            brand_problem = brand_top_rail_problem(participant, self.participant_width, 8, mark, "Participant")
            if brand_problem:
                issues.append(brand_problem)
            sublabel = participant.get("sublabel")
            if sublabel:
                available = available_node_text_width(self.participant_width)
                minimum = minimum_node_text_width(sublabel, 6)
                if minimum > available:
                    issues.append(f'Sublabel "{sublabel}" needs ~{math.ceil(minimum)}px at the 6px legible minimum, '
                                  f'but participant "{participant.get("id", "")}" provides {number(available)}px — '
                                  f'shorten the sublabel ({spread_note}).')

        # Opaque participant headers must not obscure an unrelated message.
        for index, message in enumerate(self.messages):
            start_participant = self.participants.get(message.get("from"))
            end_participant = self.participants.get(message.get("to"))
            if not start_participant or not end_participant or not _is_number(message.get("y")):
                continue
            start = [start_participant["cx"], message["y"]]
            end = [end_participant["cx"], message["y"]]
            for obstacle in self.participants.values():
                if obstacle.get("id") in (message.get("from"), message.get("to")):
                    continue
                if segment_intersects_rect(start, end, obstacle):
                    obstacle_index = index
                    relation_id = f' id "{message["id"]}"' if message.get("id") else ""
                    hint = "move the message y below the participant headers or reorder participants"
                    text = (f'[clean-flow/edge-through-node] sequence messages[{obstacle_index}]{relation_id} '
                            f'"{message.get("from")}" -> "{message.get("to")}" crosses participant header '
                            f'"{obstacle.get("id")}" (unrelated to this relationship) on segment 0 '
                            f'[{_fmt_point(start)}] -> [{_fmt_point(end)}] (0px clearance) — {hint}.')
                    issues.append(_problem(text, "clean-flow/edge-through-node", _relationship_subject(index, message),
                                           {"obstacleKind": "participant header", "obstacleId": obstacle.get("id"),
                                            "segmentIndex": 0, "from": start, "to": end, "clearancePx": 0}, [hint]))

        placed = []
        for message in self.messages:
            source, target = self.participants.get(message.get("from")), self.participants.get(message.get("to"))
            if source and target and _is_number(message.get("y")):
                placed.append({"label": message.get("label", ""), "y": message["y"],
                               "x1": min(source["cx"], target["cx"]), "x2": max(source["cx"], target["cx"])})
        placed.sort(key=lambda item: item["y"])
        for index, left in enumerate(placed):
            for right in placed[index + 1:]:
                if right["y"] - left["y"] >= 28:
                    break
                if left["x1"] < right["x2"] and right["x1"] < left["x2"]:
                    issues.append(f'Messages "{left["label"]}" and "{right["label"]}" are less than 28px apart '
                                  'and share horizontal space — spread their y values.')

        label_rects = [self.message_label_box(message, index) for index, message in enumerate(self.messages)]
        label_rects = [rect for rect in label_rects if rect]
        for index, left in enumerate(label_rects):
            for right in label_rects[index + 1:]:
                if rects_overlap(left, right, -2):
                    issues.append(f'Labels "{left["label"]}" and "{right["label"]}" overlap — spread their message y values or shorten the labels.')

        # Showcase profile rejects unrelated messages that borrow the same
        # corridor. Authored shared participant endpoints remain intentional.
        if self.gate_profile == "showcase":
            routed = []
            for index, message in enumerate(self.messages):
                source, target = self.participants.get(message.get("from")), self.participants.get(message.get("to"))
                if source and target and _is_number(message.get("y")):
                    routed.append((index, message, [source["cx"], message["y"]], [target["cx"], message["y"]]))
            for left_index, left in enumerate(routed):
                for right in routed[left_index + 1:]:
                    li, lm, la, lb = left
                    ri, rm, ra, rb = right
                    if {lm.get("from"), lm.get("to")} & {rm.get("from"), rm.get("to")}:
                        continue
                    if abs(la[1] - ra[1]) > 0.0001:
                        continue
                    overlap_start = max(min(la[0], lb[0]), min(ra[0], rb[0]))
                    overlap_end = min(max(la[0], lb[0]), max(ra[0], rb[0]))
                    overlap = overlap_end - overlap_start
                    if overlap + 0.0001 < 8:
                        continue
                    hint = "adjust route/via or channel coordinates so the relationships use separate corridors"
                    left_id = f' id "{lm["id"]}"' if lm.get("id") else ""
                    right_id = f' id "{rm["id"]}"' if rm.get("id") else ""
                    text = (f'[composition/ambiguous-corridor] showcase sequence messages[{li}]{left_id} '
                            f'"{lm.get("from")}" -> "{lm.get("to")}" shares a {_round_tenth(overlap)}px corridor with '
                            f'messages[{ri}]{right_id} "{rm.get("from")}" -> "{rm.get("to")}" at '
                            f'[{_fmt_point([overlap_start, la[1]], 1)}] -> [{_fmt_point([overlap_end, la[1]], 1)}] '
                            f'(segments 0 and 0; minimum 8px) — {hint}.')
                    issues.append(_problem(text, "composition/ambiguous-corridor", _relationship_subject(li, lm),
                                           {"otherRelationship": _relationship_subject(ri, rm), "overlapLengthPx": _round_tenth(overlap),
                                            "minimumPx": 8, "from": [overlap_start, la[1]], "to": [overlap_end, la[1]],
                                            "segmentIndex": 0, "otherSegmentIndex": 0}, [hint]))

        # Labels are distinct rectangles even when their arrows are short.
        if self.gate_profile == "showcase":
            for label in label_rects:
                for other_index, message in enumerate(self.messages):
                    if other_index == label["relationIndex"]:
                        continue
                    source, target = self.participants.get(message.get("from")), self.participants.get(message.get("to"))
                    if not source or not target or not _is_number(message.get("y")):
                        continue
                    clearance = segment_rect_clearance([source["cx"], message["y"]],
                                                       [target["cx"], message["y"]], label)
                    if clearance is None or clearance + 0.0001 >= 4:
                        continue
                    relation = label["relation"]
                    relation_index = label["relationIndex"]
                    hint = "spread the message y values, shorten the label, or reorder participants so the adjacent route stays visible"
                    relation_id = f' id "{relation["id"]}"' if relation.get("id") else ""
                    other_id = f' id "{message["id"]}"' if message.get("id") else ""
                    text = (f'[composition/label-route-clearance] showcase sequence label "{label["label"]}" on '
                            f'messages[{relation_index}]{relation_id} "{relation.get("from")}" -> "{relation.get("to")}" '
                            f'is {_round_tenth(clearance)}px from messages[{other_index}]{other_id} '
                            f'"{message.get("from")}" -> "{message.get("to")}" segment 0 '
                            f'[{_fmt_point([source["cx"], message["y"]], 1)}] -> '
                            f'[{_fmt_point([target["cx"], message["y"]], 1)}] '
                            f'(label rect [{_round_px(label["x"])}, {_round_px(label["y"])}, '
                            f'{_round_px(label["width"])}, {_round_px(label["height"])}]; minimum 4px) — {hint}.')
                    issues.append(_problem(text, "composition/label-route-clearance", _relationship_subject(relation_index, relation),
                                           {"label": label["label"], "otherRelationship": _relationship_subject(other_index, message),
                                            "segmentIndex": 0, "clearancePx": _round_tenth(clearance), "minimumPx": 4,
                                            "labelRect": {key: label[key] for key in ("x", "y", "width", "height")},
                                            "from": [source["cx"], message["y"]], "to": [target["cx"], message["y"]]}, [hint]))

            for label in label_rects:
                overflow = {}
                for side, amount in (
                    ("left", -label["x"]), ("right", label["x"] + label["width"] - self.view_box[0]),
                    ("top", -label["y"]), ("bottom", label["y"] + label["height"] - self.view_box[1]),
                ):
                    if amount > 0.5:
                        overflow[side] = _round_tenth(amount)
                if not overflow:
                    continue
                sides = list(overflow)
                extent = " and ".join(f'{side} edge by {overflow[side]}px' for side in sides)
                relation = label["relation"]
                index = label["relationIndex"]
                label_text = label["label"] or relation.get("label", "")
                hint = "shorten the label, reorder participants, or enlarge meta.viewBox"
                message_id = f' id "{relation["id"]}"' if relation.get("id") else ""
                text = (f'[composition/label-canvas-containment] showcase sequence label "{label_text}" on messages[{index}]'
                        f'{message_id} "{relation.get("from")}" -> "{relation.get("to")}" extends past the {extent} '
                        f'(label rect [{_round_px(label["x"])}, {_round_px(label["y"])}, {_round_px(label["width"])}, '
                        f'{_round_px(label["height"])}]; viewBox {number(self.view_box[0])}x{number(self.view_box[1])}) — {hint}.')
                issues.append(_problem(text, "composition/label-canvas-containment", _relationship_subject(index, relation),
                                       {"label": label_text,
                                        "labelRect": {key: label[key] for key in ("x", "y", "width", "height")},
                                        "viewBox": self.view_box, "overflowPx": overflow, "tolerancePx": 0.5}, [hint]))

        frames = self.frames()
        for relation_index, message in enumerate(self.messages) if self.gate_profile else ():
            source, target = self.participants.get(message.get("from")), self.participants.get(message.get("to"))
            if not source or not target or not _is_number(message.get("y")):
                continue
            start, end = [source["cx"], message["y"]], [target["cx"], message["y"]]
            for frame_index, frame in enumerate(frames):
                if (not is_finite_point(frame["x"], frame["y"], frame["width"], frame["height"])
                        or frame["width"] <= 0 or frame["height"] <= 0):
                    continue
                for side, border_y in (("top", frame["y"]), ("bottom", frame["y"] + frame["height"])):
                    if abs(message["y"] - border_y) > 0.0001:
                        continue
                    left, right = frame["x"] + 10, frame["x"] + frame["width"] - 10
                    overlap_start = max(min(start[0], end[0]), left)
                    overlap_end = min(max(start[0], end[0]), right)
                    overlap = overlap_end - overlap_start
                    if overlap <= 0.0001:
                        continue
                    hint = "move the message y so it crosses a segment boundary perpendicularly or stays clearly inside the segment"
                    message_id = f' id "{message["id"]}"' if message.get("id") else ""
                    text = (f'[composition/container-border-run] sequence messages[{relation_index}]{message_id} '
                            f'"{message.get("from")}" -> "{message.get("to")}" follows segment "{frame["label"]}" '
                            f'{side} border for {_round_tenth(overlap)}px on segment 0 '
                            f'[{_fmt_point([overlap_start, border_y], 1)}] -> '
                            f'[{_fmt_point([overlap_end, border_y], 1)}] — {hint}.')
                    issues.append(_problem(text, "composition/container-border-run", _relationship_subject(relation_index, message),
                                           {"frameKind": "segment", "frameId": frame_index, "frameLabel": frame["label"],
                                            "side": side, "segmentIndex": 0, "overlapLengthPx": _round_tenth(overlap),
                                            "from": [overlap_start, border_y], "to": [overlap_end, border_y]}, [hint]))

        for segment in self.segments:
            if _is_number(segment.get("from")) and _is_number(segment.get("to")):
                if segment["to"] <= segment["from"]:
                    issues.append(f'Segment "{segment.get("label", "")}" has invalid y range (from {number(segment["from"])} '
                                  f'to {number(segment["to"])}) — "to" must be greater than "from".')
                if segment["from"] < self.layout["topY"] or segment["to"] > self.layout["lifelineBottom"] + 20:
                    issues.append(f'Segment "{segment.get("label", "")}" extends outside the canvas — keep its y range between '
                                  f'{self.layout["topY"]} and {number(self.layout["lifelineBottom"] + 20)}.')
            label = self.segment_label_box(segment)
            available = max(0, self.view_box[0] - 48 - label["x"])
            if label["x"] + label["width"] > self.view_box[0] - 48:
                required = math.ceil(label["x"] + label["width"] + 48)
                issues.append(f'Segment "{segment.get("label", "")}" label (~{_round_px(label["width"])}px) exceeds the segment frame\'s '
                              f'available width ({number(available)}px) — shorten the label or increase meta.viewBox[0] to at least {required}.')

        for activation in self.activations:
            if activation.get("participant") not in self.participants:
                issues.append(f'Activation references unknown participant "{activation.get("participant", "")}".')
            if _is_number(activation.get("from")) and _is_number(activation.get("to")) and activation["to"] <= activation["from"]:
                issues.append(f'Activation for "{activation.get("participant", "")}" has invalid time range — "to" must be greater than "from".')

        for message in self.messages:
            if message.get("from") not in self.participants:
                issues.append(f'Message "{message.get("label", "")}" references unknown source "{message.get("from", "")}".')
            if message.get("to") not in self.participants:
                issues.append(f'Message "{message.get("label", "")}" references unknown target "{message.get("to", "")}".')
            if not _is_number(message.get("y")):
                issues.append(f'Message "{message.get("label", "")}" must provide a numeric y.')
                continue
            if message["y"] < self.layout["lifelineTop"] + 18 or message["y"] > self.layout["lifelineBottom"] - 18:
                issues.append(f'Message "{message.get("label", "")}" sits outside the readable timeline — keep y between '
                              f'{self.layout["lifelineTop"] + 18} and {number(self.layout["lifelineBottom"] - 18)}.')
            source, target = self.participants.get(message.get("from")), self.participants.get(message.get("to"))
            if source and target:
                distance = abs(target["cx"] - source["cx"])
                if distance < 60:
                    issues.append(f'Message "{message.get("label", "")}" spans {_round_px(distance)}px (minimum 60px) — give its participants more column distance.')

        last = self.participant_items[-1] if self.participant_items else None
        if last:
            participant = self.participants.get(last.get("id"))
            if participant and participant["cx"] + self.participant_width / 2 > self.view_box[0] - 40:
                required = math.ceil(participant["cx"] + self.participant_width / 2 + 40)
                issues.append(f'Participants exceed viewBox width — set meta.viewBox[0] to at least {required} or remove a participant.')

        legend_height = self.legend_required_height(self.view_box[0])
        if (self.meta.get("quality_profile") == "showcase" and self.meta.get("legend") is None
                and legend_height > self.view_box[1] and self.measure_legend() is None):
            issues.append(f'Sequence content ends at y={number(self.content_bottom)}, leaving no room for the legend below it — '
                          f'set meta.viewBox[1] to at least {legend_height} or omit meta.viewBox so the canvas grows.')

        problems("Sequence layout validation failed", issues)

    def render_participant(self, participant):
        fill = COMPONENT_FILL.get(participant.get("type"), "c-external")
        has_sub = participant.get("sublabel") is not None and participant.get("sublabel") != ""
        sub = (f'\n          <text data-detail="context" x="{number(participant["cx"])}" y="{72 + 50}" class="t-muted" '
               f'font-size="{number(fitted_node_font_size(participant["sublabel"], self.participant_width, 7, 6))}" '
               f'text-anchor="middle">{esc(participant["sublabel"])}</text>') if has_sub else ""
        mark = resolve_brand(participant.get("brand"))
        brand = render_brand_mark(mark, participant["x"] + self.participant_width - 22, 72 + 6)
        label_font = fitted_node_font_size(participant.get("label"), brand_label_fit_width(participant, self.participant_width, mark), 11, 8)
        passport = {"kind": participant.get("type"), "sublabel": participant.get("sublabel"),
                    "context": translate(self.meta, "node.context.sequence"), **brand_metadata(mark)}
        attrs = focus_node_attrs(participant, passport, self.meta)
        node_title = focus_node_title(participant.get("label", ""), passport)
        brand_text = f"\n          {brand}" if brand else ""
        detail_anchor = ' data-detail-anchor=""' if has_sub else ""
        return (f'        <g {attrs}>\n'
                f'          {node_title}\n'
                f'          <rect x="{number(participant["x"])}" y="72" width="{number(self.participant_width)}" height="60" rx="6" class="c-mask"/>\n'
                f'          <rect x="{number(participant["x"])}" y="72" width="{number(self.participant_width)}" height="60" rx="6" class="{fill}"{animate_attr(self.meta, "node", participant["index"])} stroke-width="1.5"/>\n'
                f'          {render_semantic_sigil(participant.get("type"), participant["x"] + 6, 78, participant.get("icon"))}'
                f'{brand_text}\n'
                f'          <text data-node-label=""{detail_anchor} x="{number(participant["cx"])}" y="108" class="t-primary" '
                f'font-size="{number(label_font)}" font-weight="600" text-anchor="middle">{esc(participant.get("label", ""))}</text>{sub}\n'
                f'        </g>')

    def render_lifeline(self, participant, end):
        return (f'        <path d="M {number(participant["cx"])} 142 L {number(participant["cx"])} {number(end)}" '
                'class="a-default" stroke-width="0.8" stroke-dasharray="3,7"/>')

    def render_segment(self, segment, index):
        return (f'        <rect data-graph-role="structural-frame" data-composition-frame-kind="segment" '
                f'data-composition-frame-id="{index}" x="48" y="{number(segment.get("from"))}" '
                f'width="{number(self.view_box[0] - 96)}" height="{number(segment.get("to") - segment.get("from"))}" '
                'rx="10" class="c-lane" stroke-width="1"/>')

    def render_segment_label(self, segment, index):
        label = self.segment_label_box(segment)
        return (f'        <g data-graph-role="segment-label" data-segment-id="{index}">\n'
                f'          <rect x="{number(label["x"])}" y="{number(label["y"])}" width="{number(label["width"])}" '
                f'height="18" rx="3" class="c-mask"/>\n'
                f'          <text x="{number(label["x"] + 6)}" y="{number(label["y"] + 13)}" class="t-dim" '
                f'font-size="9" font-weight="600">{esc(segment.get("label", ""))}</text>\n'
                '        </g>')

    def render_activation(self, activation):
        participant = self.participants[activation.get("participant")]
        fill = COMPONENT_FILL.get(activation.get("type")) or COMPONENT_FILL.get(participant.get("type")) or "c-external"
        x = participant["cx"] - 5
        height = activation["to"] - activation["from"]
        return (f'        <rect x="{number(x)}" y="{number(activation["from"])}" width="10" height="{number(height)}" rx="3" class="c-mask"/>\n'
                f'        <rect x="{number(x)}" y="{number(activation["from"])}" width="10" height="{number(height)}" rx="3" class="{fill}" stroke-width="1"/>')

    def render_message_label(self, message, start, end):
        box = self.message_label_box(message)
        center = box["x"] + box["width"] / 2 if box else (start + end) / 2
        y = message["y"] - 10
        label_width = box["width"] if box else max(34, text_units(message.get("label")) * self.message_unit_width + 12)
        accent = EDGE_ACCENT.get(message.get("variant"), "t-edge-default")
        if message.get("variant") not in ("emphasis", "security", "dashed"):
            accent = "t-muted"
        return (f'        <g data-detail="context">\n'
                f'          <rect x="{number(center - label_width / 2)}" y="{number(y - 10)}" width="{number(label_width)}" '
                f'height="{number(self.layout["labelH"])}" rx="3" class="c-mask"/>\n'
                f'          <text x="{number(center)}" y="{number(y)}" class="{accent}" font-size="{self.message_font_size}" '
                f'text-anchor="middle">{esc(message.get("label", ""))}</text>\n'
                '        </g>')

    def render_message(self, message, index):
        geometry = self.message_geometry(message)
        start, end = geometry["start"], geometry["end"]
        class_name, marker = ARROW_CLASS.get(message.get("variant") or "default", ARROW_CLASS["default"])
        stroke = 1.8 if message.get("variant") == "emphasis" else 1.4
        dash = ' stroke-dasharray="3,5"' if message.get("variant") == "return" else ""
        note = (f'\n        <text data-detail="fine" x="{number(min(start, end) + 12)}" y="{number(message["y"] + 18)}" '
                f'class="t-dim" font-size="7">{esc(message["note"])}</text>') if message.get("note") else ""
        points = route_points_value([[start, message["y"]], [end, message["y"]]])
        message_id_attr = f' data-composition-edge-id="{esc(message["id"])}"' if message.get("id") else ""
        return (f'        <g {focus_edge_attrs(message, index)}>\n'
                f'          <path data-composition-edge-from="{esc(message.get("from"))}" data-composition-edge-to="{esc(message.get("to"))}"'
                f'{message_id_attr} '
                f'data-composition-points="{points}" d="M {number(start)} {number(message["y"])} L {number(end)} {number(message["y"])}" '
                f'class="{class_name}"{animate_attr(self.meta, "edge", index)} stroke-width="{number(stroke)}"{dash} marker-end="url(#{marker})"/>\n'
                f'{self.render_message_label(message, start, end)}{note}\n'
                '        </g>')

    def render_legend(self):
        measured = self.measure_legend()
        if not self.entries or not measured:
            return ""
        parts = ['        <g data-legend="">',
                 f'          <text x="40" y="{number(measured["titleY"])}" class="t-primary" font-size="12" font-weight="650">'
                 f'{esc(translate(self.meta, "legend.title"))}</text>']
        rendered_size = measured["fontSize"] + (0.5 if measured["fontSize"] < 8 else 2)
        for entry in measured["entries"]:
            parts.append(f'          <g data-legend-semantic-kind="{esc(entry["kind"])}" data-legend-x="{number(entry["x"])}" '
                         f'data-legend-baseline="{number(entry["baseline"])}" data-legend-width="{entry["width"]}">')
            dash = f' stroke-dasharray="{entry["dash"]}"' if entry.get("dash") else ""
            stroke = entry.get("strokeWidth") or 1.4
            parts.append(f'            <path d="M {number(entry["x"])} {number(entry["baseline"] - 3)} L '
                         f'{number(entry["x"] + 34)} {number(entry["baseline"] - 3)}" class="{entry["className"]}" '
                         f'stroke-width="{number(stroke)}"{dash} marker-end="url(#{entry["marker"]})"/>')
            parts.append(f'            <text x="{number(entry["x"] + 34 + 9)}" y="{number(entry["baseline"])}" '
                         f'class="t-muted" font-size="{number(rendered_size)}" font-weight="500">{esc(entry["label"])}</text>')
            parts.append('          </g>')
        parts.append('        </g>')
        return "\n".join(parts)

    def lifeline_end(self):
        legend = self.measure_legend()
        return self.layout["lifelineBottom"] if legend is None or legend["titleY"] is None else min(
            self.layout["lifelineBottom"], legend["titleY"] - 22)

    def render_svg(self):
        participant_list = list(self.participants.values())
        reader_fit = "" if self.meta.get("viewBox") is not None else ' data-reader-fit="intrinsic-height"'
        root = (f'      <svg viewBox="0 0 {number(self.view_box[0])} {number(self.view_box[1])}" '
                f'data-sequence-column-fit="{self.column_fit}"{reader_fit} {svg_root_attrs(self.meta, self.quality)}>')
        parts = [root, svg_accessible_text(self.meta), render_definitions(), "",
                 '        <!-- Background Grid -->',
                 '        <rect width="100%" height="100%" fill="url(#grid)" />', "",
                 '        <!-- Time Segments -->',
                 "\n\n".join(self.render_segment(segment, index) for index, segment in enumerate(self.segments)), "",
                 '        <!-- Lifelines -->',
                 "\n".join(self.render_lifeline(participant, self.lifeline_end()) for participant in participant_list), "",
                 '        <!-- Activations -->',
                 "\n".join(self.render_activation(item) for item in self.activations), "",
                 '        <!-- Messages -->',
                 "\n\n".join(self.render_message(message, index) for index, message in enumerate(self.messages)), "",
                 '        <!-- Segment Labels -->',
                 "\n".join(self.render_segment_label(segment, index) for index, segment in enumerate(self.segments)), "",
                 '        <!-- Participants -->',
                 "\n\n".join(self.render_participant(participant) for participant in participant_list), "",
                 '        <!-- Legend -->', self.render_legend(), '      </svg>']
        return "\n".join(parts)

    def render(self):
        self.validate()
        return self.render_svg()

def render_sequence(diagram, quality=None):
    """Render a validated Sequence diagram to its canonical SVG string."""
    return SequenceRenderer(diagram, quality=quality).render()
