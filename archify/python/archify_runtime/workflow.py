"""Node-free Workflow renderer, preserving the canonical v1/v2 contracts."""

import copy
import math

from .common import DiagnosticError, diagnostic, diagnostic_scope, esc, number, record_diagnostic, translate
from .composition import (
    collect_ambiguous_corridors, collect_arrowhead_collisions,
    collect_label_route_clearance, forward_collinear_analysis_segments,
    relationship_legend_obstacles, source_segment_index_at_point,
)
from .drawing import (
    animate_attr, available_node_text_width, brand_label_fit_width, brand_metadata,
    brand_top_rail_problem, focus_edge_attrs, focus_node_attrs, focus_node_title,
    fitted_node_font_size, render_brand_mark, render_definitions, render_semantic_sigil,
    resolve_brand, svg_accessible_text, svg_root_attrs, text_units,
)
from .flow_geometry import (
    clean_ambiguous_corridor_problems, clean_border_run_problems,
    clean_crossing_problems, clean_endpoint_side_problems, clean_flow_problems,
    clean_label_canvas_problems, clean_label_route_clearance_problems,
    clean_route_rhythm_problems, label_point, route_points_value,
    anchor, proper_segment_intersection,
)
from .geometry import as_array, is_finite_point, rects_overlap, segment_intersects_rect
from .legend import legend_footprint, measure_legend, resolve_legend, render_legend
from .text_fit import node_label_layout
from .validation import validate_document
from .workflow_layout import (
    _is_integer, _is_number, authored_node_height, authored_node_width,
    create_legacy_layout, create_readable_layout, readable_group_bounds,
    stable_compare, stable_value_key, uses_independent_lane_measurement,
    workflow_edge_name, workflow_label_width,
)
from .workflow_migration import (
    LEGACY_COLUMN_CENTERS, create_mapped_workflow_candidate, intrinsic_workflow,
    planning_workflow,
)
from .workflow_routing import WorkflowLayoutFeedback, WorkflowRouter
from .workflow_semantics import semantic_contract_diagnostics

GROUP_FRAME_TOP_INSET = 8
GROUP_FRAME_BOTTOM_INSET = 4
GROUP_LABEL_BASELINE_OFFSET = -2
GROUP_LABEL_MASK_ASCENT = 10
GROUP_LABEL_MASK_H = 14
NODE_TEXT_FIT = {"labelPreferred": 11, "labelMinimum": 9, "sublabelPreferred": 8,
                 "sublabelMinimum": 6, "tagPreferred": 7, "tagMinimum": 6}
COMPONENT_FILL = {"frontend": "c-frontend", "backend": "c-backend", "database": "c-database",
                  "cloud": "c-cloud", "security": "c-security", "messagebus": "c-messagebus",
                  "external": "c-external"}
COMPONENT_TEXT = {"frontend": "t-frontend", "backend": "t-backend", "database": "t-database",
                  "cloud": "t-cloud", "security": "t-security", "messagebus": "t-messagebus",
                  "external": "t-external"}
ARROW_CLASS_MAP = {"default": ("a-default", "arrowhead"), "emphasis": ("a-emphasis", "arrowhead-emphasis"),
                   "security": ("a-security", "arrowhead-security"), "dashed": ("a-dashed", "arrowhead-dashed")}


def _variant_accent(variant):
    return "t-security" if variant == "security" else "t-backend" if variant == "emphasis" else "t-messagebus" if variant == "dashed" else "t-muted"


def _edge_label_accent(variant):
    return {"security": "t-edge-security", "emphasis": "t-edge-emphasis", "dashed": "t-edge-dashed"}.get(variant, "t-edge-default")


def _stable_sorted(values, fields):
    def key(item):
        return tuple("" if item.get(field) is None else str(item.get(field)) for field in fields)
    return sorted(values, key=key)


def canonical_readable_workflow(workflow):
    if workflow.get("schema_version") != 2:
        return workflow
    lanes = as_array(workflow.get("lanes"))
    lane_order = {lane.get("id"): index for index, lane in enumerate(lanes)}
    nodes = sorted(as_array(workflow.get("nodes")), key=lambda node: (
        lane_order.get(node.get("lane"), 2 ** 53), node.get("col", math.inf), str(node.get("id") or "")))
    edges = sorted(as_array(workflow.get("edges")), key=lambda edge: (
        str(edge.get("id") or ""), str(edge.get("from") or ""), str(edge.get("to") or ""),
        str(edge.get("label") or ""), str(edge.get("route") or ""), stable_value_key(edge)))
    result = {**workflow, "nodes": nodes, "edges": edges}
    if "phases" in workflow:
        result["phases"] = sorted(as_array(workflow.get("phases")), key=lambda phase: (
            phase.get("fromCol", math.inf), phase.get("toCol", math.inf), str(phase.get("id") or "")))
    if "groups" in workflow:
        result["groups"] = sorted(as_array(workflow.get("groups")), key=lambda group: (
            lane_order.get(group.get("lane"), 2 ** 53), group.get("fromCol", math.inf),
            group.get("toCol", math.inf), str(group.get("id") or "")))
    return result


class WorkflowRenderer:
    def __init__(self, diagram, quality=None, discover_fixes=True, layout_feedback=None):
        if not isinstance(diagram, dict):
            message = "compileWorkflow requires one parsed workflow document object."
            raise DiagnosticError(message, [diagnostic("workflow/input-contract", message,
                {"diagramType": "workflow", "path": "/"}, {}, [])])
        original_meta = diagram.get("meta") if isinstance(diagram.get("meta"), dict) else {}
        self.quality = quality or original_meta.get("quality_profile")
        source_workflow = diagram
        if self.quality and self.quality != original_meta.get("quality_profile"):
            source_workflow = {**diagram, "meta": {**original_meta, "quality_profile": self.quality}}
        # Schema and duplicate relationship IDs remain owned by the canonical
        # Schema/validation layer used by every native renderer.
        validate_document(source_workflow, "workflow")
        self.workflow = canonical_readable_workflow(source_workflow)
        semantic = semantic_contract_diagnostics(self.workflow)
        if semantic:
            raise DiagnosticError("\n".join(entry["message"] for entry in semantic), semantic)
        self.meta = self.workflow.get("meta") or {}
        self.discover_fixes = discover_fixes
        self.edges = as_array(self.workflow.get("edges"))
        self.lanes = as_array(self.workflow.get("lanes"))
        self.authored_lane_indexes = {id(item): index for index, item in enumerate(as_array(source_workflow.get("lanes")))}
        self.authored_node_indexes = {id(item): index for index, item in enumerate(as_array(source_workflow.get("nodes")))}
        self.authored_edge_indexes = {id(item): index for index, item in enumerate(as_array(source_workflow.get("edges")))}
        # V2 sorting retains each item object, so identity indexes remain stable.
        self.edge_index_by_identity = {id(edge): index for index, edge in enumerate(self.edges)}
        self.layout_feedback = layout_feedback or {}
        self.layout = create_readable_layout(self.workflow, self.layout_feedback) if self.workflow.get("schema_version") == 2 else create_legacy_layout()
        self._resolve_legend_footprint()
        self._create_lane_geometry()
        self.view_box = list(self.initial_view_box)
        self.required_view_box = list(self.view_box)
        self.nodes = self._measure_nodes()
        self.edge_steps, self.main_path_steps = self._create_step_indexes()
        self.workflow_diagnostics = []
        self.router = WorkflowRouter(self)
        self._accepted_fixes = discover_fixes

    def _resolve_legend_footprint(self):
        catalog = [{"kind": kind, "label": translate(self.meta, f"legend.workflow.{kind}")}
                   for kind in ("frontend", "backend", "security", "messagebus", "database", "cloud", "external")]
        present = {node.get("type") for node in as_array(self.workflow.get("nodes"))}
        self.legend_entries = resolve_legend(self.meta.get("legend"), catalog, present)
        one_row = legend_footprint(self.legend_entries, width=2 ** 53, font_size=7, item_gap=7)
        self.minimum_canvas_width = (max(self.layout["defaultViewBoxWidth"], one_row["minWidth"] + 40)
                                     if self.workflow.get("schema_version") == 2 else self.layout["defaultViewBoxWidth"])
        authored_viewbox = self.meta.get("viewBox")
        self.legend_packing_width = max(1, (self.minimum_canvas_width if self.workflow.get("schema_version") == 2
                                             else (authored_viewbox[0] if authored_viewbox else self.minimum_canvas_width)) - 40)
        packed = legend_footprint(self.legend_entries, width=self.legend_packing_width, font_size=7, item_gap=7)
        self.legend_extra_height = packed["extraHeight"] if self.workflow.get("schema_version") == 2 else 0

    def _create_lane_geometry(self):
        layout = self.layout
        lane_heights = layout.get("laneHeights")
        count = len(self.lanes) or 1
        self.auto_height = (layout["laneY"] + (sum(lane_heights) if lane_heights is not None else count * layout["laneH"])
                            + (count - 1) * layout["laneGap"] + 124 + self.legend_extra_height)
        authored = self.meta.get("viewBox")
        self.initial_view_box = list(authored) if authored else [self.minimum_canvas_width, self.auto_height]
        self.lane_index = {lane.get("id"): index for index, lane in enumerate(self.lanes)}
        self.lane_labels = {lane.get("id"): lane.get("label", "") for lane in self.lanes}

    def lane_height(self, id_or_index):
        index = id_or_index if isinstance(id_or_index, int) and not isinstance(id_or_index, bool) else self.lane_index.get(id_or_index)
        if index is None:
            return self.layout["laneH"]
        heights = self.layout.get("laneHeights")
        return heights[index] if heights is not None and index < len(heights) else self.layout["laneH"]

    def lane_group_header_height(self, id_or_index):
        index = id_or_index if isinstance(id_or_index, int) and not isinstance(id_or_index, bool) else self.lane_index.get(id_or_index)
        values = self.layout.get("groupHeaderHeights") or []
        return values[index] if index is not None and index < len(values) else 0

    def lane_group_footer_height(self, id_or_index):
        index = id_or_index if isinstance(id_or_index, int) and not isinstance(id_or_index, bool) else self.lane_index.get(id_or_index)
        values = self.layout.get("groupFooterHeights") or []
        return values[index] if index is not None and index < len(values) else 0

    def lane_top(self, lane_id):
        index = self.lane_index.get(lane_id)
        if index is None:
            return self.layout["laneY"]
        return self.layout["laneY"] + sum(self.lane_height(position) for position in range(index)) + index * self.layout["laneGap"]

    def last_lane_bottom(self):
        return self.layout["laneY"] + sum(self.lane_height(index) for index in range(len(self.lanes))) + (len(self.lanes) - 1) * self.layout["laneGap"]

    def legend_y(self):
        return self.last_lane_bottom() + 44 + self.legend_extra_height

    def node_context(self, node):
        group = next((item for item in as_array(self.workflow.get("groups"))
                      if item.get("lane") == node.get("lane") and item.get("fromCol", math.inf) <= node.get("col", -math.inf) <= item.get("toCol", -math.inf)), None)
        phase = next((item for item in as_array(self.workflow.get("phases"))
                      if item.get("fromCol", math.inf) <= node.get("col", -math.inf) <= item.get("toCol", -math.inf)), None)
        parts = [self.lane_labels.get(node.get("lane")), group.get("label") if group else None, phase.get("label") if phase else None]
        return " › ".join(value for value in parts if value) or translate(self.meta, "node.context.workflow")

    def _measure_nodes(self):
        result = {}
        for node in as_array(self.workflow.get("nodes")):
            width = node.get("width") or self.layout["nodeW"]
            height = node.get("height") or (68 if node.get("tag") else self.layout["nodeH"])
            column = node.get("col", 0)
            cx = (self.layout["colXs"][column] if _is_integer(column) and 0 <= column < len(self.layout["colXs"])
                  else math.nan)
            group_header = self.lane_group_header_height(node.get("lane"))
            content_height = (self.lane_height(node.get("lane")) - self.layout["laneTitleH"] - group_header
                              - self.lane_group_footer_height(node.get("lane")))
            y = (self.lane_top(node.get("lane")) + self.layout["laneTitleH"] + group_header
                 + (content_height - height) / 2 + (node.get("yOffset") or 0))
            result[node.get("id")] = {**node, "width": width, "height": height, "x": cx - width / 2,
                                       "y": y, "cx": cx, "cy": y + height / 2}
        return result

    def _create_step_indexes(self):
        main_path = as_array(self.workflow.get("mainPath"))
        self.main_path_steps = {node_id: index for index, node_id in enumerate(main_path)}
        result = {}
        for index, edge in enumerate(self.edges):
            source_step = self.main_path_steps.get(edge.get("from"))
            target_step = self.main_path_steps.get(edge.get("to"))
            step = source_step if source_step is not None and target_step == source_step + 1 else len(main_path) + index
            result[id(edge)] = step
        return result, self.main_path_steps

    def node_step(self, node):
        if node.get("id") in self.main_path_steps:
            return self.main_path_steps[node["id"]]
        index = next((i for i, item in enumerate(as_array(self.workflow.get("nodes"))) if item.get("id") == node.get("id")), -1)
        return len(as_array(self.workflow.get("mainPath"))) + index

    def phase_span(self, phase):
        return self.span_for_cols(phase.get("fromCol"), phase.get("toCol"), 46,
                                  text_units(phase.get("label")) * 5.6 + 8 if self.workflow.get("schema_version") == 2 else 0)

    def span_for_cols(self, from_col, to_col, pad=46, minimum_width=0):
        start, end = self.layout["colXs"][from_col] - pad, self.layout["colXs"][to_col] + pad
        width = max(end - start, minimum_width)
        if from_col == to_col and width > end - start:
            return {"x": start, "width": width, "cx": start + width / 2}
        center = (start + end) / 2
        return {"x": center - width / 2, "width": width, "cx": center}

    def group_span(self, group):
        if self.workflow.get("schema_version") == 2:
            return readable_group_bounds(self.workflow, group, self.layout["colXs"])
        return self.span_for_cols(group.get("fromCol"), group.get("toCol"), 50)

    def workflow_legend_layout(self, obstacles=()):
        return {"x": 20, "baselineY": self.legend_y(), "width": self.legend_packing_width if self.workflow.get("schema_version") == 2 else self.view_box[0] - 40,
                "fontSize": 7, "itemGap": 7, "minTitleY": self.last_lane_bottom() + 8,
                "obstacles": list(obstacles), "unfit": "hide" if "legend" not in self.meta else "error", "diagramType": "workflow"}

    def workflow_legend_rects(self):
        if not self.legend_entries:
            return []
        measured = measure_legend(self.legend_entries, self.workflow_legend_layout())
        if not measured:
            return []
        return ([{"kind": "title", "x": 20, "y": measured["titleY"] - 10, "width": 48, "height": 14}]
                + [{"kind": entry["kind"], "x": entry["x"], "y": entry["baseline"] - 10,
                    "width": entry["width"], "height": 14} for entry in measured["entries"]])

    def workflow_composition_frames(self):
        frames = []
        for index, lane in enumerate(self.lanes):
            y, height = self.lane_top(lane["id"]), self.lane_height(index)
            frames.append({"id": f"lane-{index}", "label": lane.get("label"), "kind": "lane",
                           "x": self.layout["laneX"], "y": y, "width": self.layout["laneW"], "height": height, "radius": 10})
            if lane.get("variant") == "exception":
                frames.append({"id": f"lane-{index}-exception", "label": f'{lane.get("label")} exception', "kind": "exception-lane",
                               "x": self.layout["laneX"] + 6, "y": y + 6, "width": self.layout["laneW"] - 12,
                               "height": height - 12, "radius": 8})
        for index, group in enumerate(as_array(self.workflow.get("groups"))):
            span = self.group_span(group)
            frames.append({"id": f"group-{index}", "label": group.get("label"), "kind": "group", "x": span["x"],
                           "y": self.lane_top(group.get("lane")) + self.layout["laneTitleH"] + GROUP_FRAME_TOP_INSET,
                           "width": span["width"], "height": (self.lane_height(group.get("lane")) - self.layout["laneTitleH"]
                           - GROUP_FRAME_TOP_INSET - GROUP_FRAME_BOTTOM_INSET) if self.workflow.get("schema_version") == 2
                           else self.layout["laneH"] - self.layout["laneTitleH"] - 16, "radius": 9})
        return frames

    def workflow_scene_label_obstacles(self):
        result = []
        for index, lane in enumerate(self.lanes):
            prefix = "EX" if lane.get("variant") == "exception" else str(index + 1).zfill(2)
            label = f'{prefix} / {lane.get("label", "")}'
            result.append({"kind": "lane-header", "id": lane.get("id"), "x": self.layout["laneX"] + 14,
                           "y": self.lane_top(lane["id"]) + 12, "width": text_units(label) * 6.2, "height": 14})
        for phase in as_array(self.workflow.get("phases")):
            start, end = phase.get("fromCol"), phase.get("toCol")
            if not _is_integer(start) or not _is_integer(end) or start < 0 or end >= len(self.layout["colXs"]) or start > end:
                continue
            span = self.phase_span(phase)
            result.append({"kind": "phase-header", "id": phase.get("id"), "x": span["x"], "y": 27,
                           "width": span["width"], "height": 16})
        for group in as_array(self.workflow.get("groups")):
            start, end = group.get("fromCol"), group.get("toCol")
            if (group.get("lane") not in self.lane_index or not _is_integer(start) or not _is_integer(end)
                    or start < 0 or end >= len(self.layout["colXs"]) or start > end):
                continue
            span = self.group_span(group)
            frame_y = self.lane_top(group["lane"]) + self.layout["laneTitleH"] + GROUP_FRAME_TOP_INSET
            result.append({"kind": "group-label", "id": group.get("id"), "x": span["x"] + 10,
                           "y": frame_y + GROUP_LABEL_BASELINE_OFFSET - GROUP_LABEL_MASK_ASCENT,
                           "width": text_units(group.get("label")) * 5.6, "height": GROUP_LABEL_MASK_H})
        return result

    def render_lane(self, lane, index):
        y, height = self.lane_top(lane["id"]), self.lane_height(index)
        exception = ""
        if lane.get("variant") == "exception":
            exception = (f'\n        <rect data-graph-role="structural-frame" data-composition-frame-kind="exception-lane" '
                        f'data-composition-frame-id="lane-{index}-exception" x="{number(self.layout["laneX"] + 6)}" '
                        f'y="{number(y + 6)}" width="{number(self.layout["laneW"] - 12)}" height="{number(height - 12)}" '
                        'rx="8" class="c-security-group" stroke-width="1"/>')
        label_class = "t-security" if lane.get("variant") == "exception" else "t-dim"
        prefix = "EX" if lane.get("variant") == "exception" else str(index + 1).zfill(2)
        return (f'        <rect data-graph-role="structural-frame" data-composition-frame-kind="lane" data-composition-frame-id="lane-{index}" '
                f'x="{number(self.layout["laneX"])}" y="{number(y)}" width="{number(self.layout["laneW"])}" '
                f'height="{number(height)}" rx="10" class="c-lane" stroke-width="1"/>{exception}\n'
                f'        <text x="{number(self.layout["laneX"] + 14)}" y="{number(y + 22)}" class="{label_class}" '
                f'font-size="10" font-weight="600">{prefix} / {esc(lane.get("label", ""))}</text>')

    def render_phase(self, phase):
        span = self.phase_span(phase)
        accent = _variant_accent(phase.get("variant"))
        line_class = ARROW_CLASS_MAP.get(phase.get("variant") or "default", ARROW_CLASS_MAP["default"])[0]
        return (f'        <line x1="{number(span["x"])}" y1="35" x2="{number(span["x"] + span["width"])}" y2="35" '
                f'class="{line_class}" stroke-width="1.1"/>\n'
                f'        <rect x="{number(span["x"])}" y="27" width="{number(span["width"])}" height="16" rx="4" class="c-mask"/>\n'
                f'        <text x="{number(span["cx"])}" y="39" class="{accent}" font-size="8" font-weight="600" '
                f'text-anchor="middle">{esc(phase.get("label", ""))}</text>')

    def render_group(self, group, index):
        span = self.group_span(group)
        y = self.lane_top(group.get("lane")) + self.layout["laneTitleH"] + GROUP_FRAME_TOP_INSET
        height = (self.lane_height(group.get("lane")) - self.layout["laneTitleH"] - GROUP_FRAME_TOP_INSET - GROUP_FRAME_BOTTOM_INSET
                  if self.workflow.get("schema_version") == 2 else self.layout["laneH"] - self.layout["laneTitleH"] - 16)
        cls = "c-security-group" if group.get("variant") == "security" else "c-lane"
        text_class = _variant_accent(group.get("variant"))
        label_y = y + GROUP_LABEL_BASELINE_OFFSET if self.workflow.get("schema_version") == 2 else y + 14
        return (f'        <rect data-graph-role="structural-frame" data-composition-frame-kind="group" data-composition-frame-id="group-{index}" '
                f'x="{number(span["x"])}" y="{number(y)}" width="{number(span["width"])}" height="{number(height)}" '
                f'rx="9" class="{cls}" stroke-width="1"/>\n'
                f'        <text x="{number(span["x"] + 10)}" y="{number(label_y)}" class="{text_class}" font-size="7" '
                f'font-weight="600">{esc(group.get("label", ""))}</text>')

    def render_node(self, node):
        fill = COMPONENT_FILL.get(node.get("type"), "c-external")
        accent = COMPONENT_TEXT.get(node.get("type"), "t-muted")
        has_sub = node.get("sublabel") is not None and node.get("sublabel") != ""
        mark = resolve_brand(node.get("brand"))
        label_font = fitted_node_font_size(node.get("label"), brand_label_fit_width(node, node["width"], mark),
                                           NODE_TEXT_FIT["labelPreferred"], NODE_TEXT_FIT["labelMinimum"])
        sub_font = fitted_node_font_size(node.get("sublabel"), node["width"], NODE_TEXT_FIT["sublabelPreferred"], NODE_TEXT_FIT["sublabelMinimum"])
        tag_font = fitted_node_font_size(node.get("tag"), node["width"], NODE_TEXT_FIT["tagPreferred"], NODE_TEXT_FIT["tagMinimum"])
        rows = [{"text": node.get("label"), "font": label_font, "y": 21}]
        if has_sub:
            rows.append({"text": node.get("sublabel"), "font": sub_font, "y": 38})
        if node.get("tag"):
            rows.append({"text": node.get("tag"), "font": tag_font, "y": node["height"] - 12})
        label_layout = node_label_layout(node["width"], node["height"], rows, brand=bool(mark))
        sub = (f'\n          <text data-detail="context" x="{number(node["cx"])}" y="{number(node["y"] + label_layout["ys"][1])}" '
               f'class="t-muted" font-size="{number(sub_font)}" text-anchor="middle">{esc(node.get("sublabel"))}</text>') if has_sub else ""
        tag_index = 2 if has_sub else 1
        tag = (f'\n        <text data-detail="fine" x="{number(node["cx"])}" y="{number(node["y"] + label_layout["ys"][tag_index])}" '
               f'class="{accent}" font-size="{number(tag_font)}" text-anchor="middle">{esc(node.get("tag"))}</text>') if node.get("tag") else ""
        brand = render_brand_mark(mark, node["x"] + node["width"] - 22, node["y"] + 6)
        passport = {"kind": node.get("type"), "sublabel": node.get("sublabel"), "tag": node.get("tag"),
                    "context": self.node_context(node), **brand_metadata(mark)}
        sigil = render_semantic_sigil(node.get("type"), node["x"] + 6, node["y"] + label_layout["sigilY"],
                                      node.get("icon"), label_layout["sigilSize"])
        brand_markup = f"\n          {brand}" if brand else ""
        detail_anchor = ' data-detail-anchor=""' if has_sub else ""
        return (f'        <g {focus_node_attrs(node, passport, self.meta)}>\n'
                f'          {focus_node_title(node.get("label", ""), passport)}\n'
                f'          <rect x="{number(node["x"])}" y="{number(node["y"])}" width="{number(node["width"])}" '
                f'height="{number(node["height"])}" rx="6" class="c-mask"/>\n'
                f'          <rect x="{number(node["x"])}" y="{number(node["y"])}" width="{number(node["width"])}" '
                f'height="{number(node["height"])}" rx="6" class="{fill}"{animate_attr(self.meta, "node", self.node_step(node))} stroke-width="1.5"/>\n'
                f'          {sigil}{brand_markup}\n'
                f'          <text data-node-label=""{detail_anchor} x="{number(node["x"] + label_layout["x"])}" '
                f'y="{number(node["y"] + label_layout["ys"][0])}" class="t-primary" font-size="{number(label_font)}" '
                f'font-weight="600" text-anchor="middle">{esc(node.get("label", ""))}</text>{sub}{tag}\n        </g>')

    def render_edge_path(self, edge, index):
        cls, marker = ARROW_CLASS_MAP.get(edge.get("variant") or "default", ARROW_CLASS_MAP["default"])
        routed = self.router.path_for(edge)
        stroke_width = edge.get("width") or (1.8 if edge.get("variant") == "emphasis" else 1.4)
        routing = ' data-composition-routing="workflow-v2-auto"' if self.router.independent_automatic_route(edge) else ""
        role = f' data-edge-role="{esc(edge.get("role", ""))}"' if self.workflow.get("schema_version") == 2 else ""
        return (f'        <path {focus_edge_attrs(edge, index)}{routing}{role} data-composition-points="{route_points_value(routed["points"])}" '
                f'd="{routed["d"]}" class="{cls}"{animate_attr(self.meta, "edge", self.edge_steps.get(id(edge), index))} '
                f'stroke-width="{number(stroke_width)}" marker-end="url(#{marker})"/>')

    def render_edge_label(self, edge, index):
        if not edge.get("label"):
            return ""
        routed = self.router.path_for(edge)
        lx, ly = self.router.edge_label_point(edge, routed["points"])
        width = workflow_label_width(edge["label"])
        return (f'        <g data-detail="context" {focus_edge_attrs(edge, index)}>\n'
                f'          <rect x="{number(lx - width / 2)}" y="{number(ly - 10)}" width="{number(width)}" height="14" rx="3" class="c-mask"/>\n'
                f'          <text x="{number(lx)}" y="{number(ly)}" class="{_edge_label_accent(edge.get("variant"))}" '
                f'font-size="8" text-anchor="middle">{esc(edge["label"])}</text>\n        </g>')

    def render_legend(self):
        obstacles = relationship_legend_obstacles(
            self.edges,
            points_for=lambda edge, _index: self.router.path_for(edge)["points"],
            label_rect_for=lambda edge, index: self.router.label_rect_for(edge, index),
        ) if self.workflow.get("schema_version") == 2 else []
        return render_legend(self.legend_entries, self.workflow_legend_layout(obstacles),
                             lambda entry: (f'<rect x="{number(entry["x"])}" y="{number(entry["baseline"] - 8)}" '
                                            f'width="14" height="9" rx="2" class="{COMPONENT_FILL.get(entry["kind"], "c-external")}" stroke-width="1"/>'),
                             self.meta)

    def render_svg(self):
        stacked_intrinsic = (self.workflow.get("schema_version") == 2 and not self.meta.get("viewBox")
                             and any(len({node.get("yOffset", 0) or 0 for node in self.workflow.get("nodes", [])
                                          if node.get("lane") == lane.get("id") and node.get("col") == col}) > 1
                                     for lane in self.lanes for col in range(len(self.layout["colXs"])))
                             and any(height > 104 for height in self.layout.get("laneHeights", [])))
        reader_fit = ' data-reader-fit="intrinsic-height"' if stacked_intrinsic else ""
        contract = ' data-layout-contract="readable-v2"' if self.workflow.get("schema_version") == 2 else ""
        return (f'      <svg viewBox="0 0 {number(self.view_box[0])} {number(self.view_box[1])}"{reader_fit}{contract} '
                f'{svg_root_attrs(self.meta, self.quality)}>\n{svg_accessible_text(self.meta, "workflow")}\n{render_definitions()}\n\n'
                '        <!-- Background Grid -->\n        <rect width="100%" height="100%" fill="url(#grid)" />\n\n'
                '        <!-- Swimlanes -->\n' + "\n\n".join(self.render_lane(lane, index) for index, lane in enumerate(self.lanes))
                + '\n\n        <!-- Phase headers -->\n' + "\n".join(self.render_phase(item) for item in as_array(self.workflow.get("phases")))
                + '\n\n        <!-- Workflow groups -->\n' + "\n".join(self.render_group(item, index) for index, item in enumerate(as_array(self.workflow.get("groups"))))
                + '\n\n        <!-- Edge paths -->\n' + "\n".join(self.render_edge_path(edge, index) for index, edge in enumerate(self.edges))
                + '\n\n        <!-- Nodes -->\n' + "\n\n".join(self.render_node(node) for node in self.nodes.values())
                + '\n\n        <!-- Edge labels -->\n' + "\n".join(self.render_edge_label(edge, index) for index, edge in enumerate(self.edges))
                + '\n\n        <!-- Legend -->\n' + self.render_legend() + '\n      </svg>')

    def measured_content_bounds(self):
        left, top = self.layout["laneX"], 27
        right, bottom = self.layout["laneX"] + self.layout["laneW"], self.legend_y() + 18
        owners = {"left": "workflow lanes", "top": "phase header band" if self.workflow.get("phases") else "workflow top padding",
                  "right": "workflow lanes", "bottom": "legend" if self.legend_entries else "workflow lanes and bottom padding"}

        def include_point(point, contributor):
            nonlocal left, top, right, bottom
            x, y = point
            if x < left: left, owners["left"] = x, contributor
            if y < top: top, owners["top"] = y, contributor
            if x > right: right, owners["right"] = x, contributor
            if y > bottom: bottom, owners["bottom"] = y, contributor

        def include_rect(rect, contributor):
            include_point((rect["x"], rect["y"]), contributor)
            include_point((rect["x"] + rect["width"], rect["y"] + rect["height"]), contributor)

        for node in self.nodes.values(): include_rect(node, f'node {node["id"]}')
        for index, edge in enumerate(self.edges):
            if edge.get("from") not in self.nodes or edge.get("to") not in self.nodes: continue
            for point in self.router.path_for(edge)["points"]: include_point(point, f'edge {edge.get("id") or index}')
            label = self.router.label_rect_for(edge, index)
            if label: include_rect(label, f'edge {edge.get("id") or index} label mask')
        for phase in as_array(self.workflow.get("phases")):
            start, end = phase.get("fromCol"), phase.get("toCol")
            if not _is_integer(start) or not _is_integer(end) or start < 0 or end >= len(self.layout["colXs"]) or start > end: continue
            span = self.phase_span(phase)
            include_rect({"x": span["x"], "y": 27, "width": span["width"], "height": 16}, f'phase {phase.get("id")}')
        for group in as_array(self.workflow.get("groups")):
            start, end = group.get("fromCol"), group.get("toCol")
            if group.get("lane") not in self.lane_index or not _is_integer(start) or not _is_integer(end) or start < 0 or end >= len(self.layout["colXs"]) or start > end: continue
            span = self.group_span(group)
            include_rect({"x": span["x"], "y": self.lane_top(group["lane"]) + self.layout["laneTitleH"] + GROUP_FRAME_TOP_INSET,
                          "width": span["width"], "height": (self.lane_height(group["lane"]) - self.layout["laneTitleH"] - GROUP_FRAME_TOP_INSET - GROUP_FRAME_BOTTOM_INSET
                          if self.workflow.get("schema_version") == 2 else self.layout["laneH"] - self.layout["laneTitleH"] - 16)}, f'group {group.get("id")}')
            if self.workflow.get("schema_version") == 2:
                frame_y = self.lane_top(group["lane"]) + self.layout["laneTitleH"] + GROUP_FRAME_TOP_INSET
                baseline = frame_y + GROUP_LABEL_BASELINE_OFFSET
                include_rect({"x": span["x"] + 10, "y": baseline - GROUP_LABEL_MASK_ASCENT,
                              "width": text_units(group.get("label")) * 5.6, "height": GROUP_LABEL_MASK_H}, f'group {group.get("id")} label')
        if self.legend_entries:
            for rect in self.workflow_legend_rects(): include_rect(rect, f'legend {rect["kind"]}')
        return {"left": left, "top": top, "right": right, "bottom": bottom,
                "contributors": list(dict.fromkeys([*owners.values(), *as_array(self.layout.get("widthContributors")), *as_array(self.layout.get("heightContributors"))]))}

    def finalize_view_box(self):
        if self.workflow.get("schema_version") != 2:
            self.required_view_box = list(self.view_box)
            return
        bounds = self.measured_content_bounds()
        self.required_view_box = [max(self.minimum_canvas_width, math.ceil(bounds["right"] + 16)),
                                  max(self.auto_height, math.ceil(bounds["bottom"] + 18))]
        if bounds["left"] < 0 or bounds["top"] < 0:
            message = f'Workflow geometry extends above or left of the viewBox origin ({math.floor(bounds["left"] + 0.5)}, {math.floor(bounds["top"] + 0.5)}).'
            code = "workflow/explicit-pin-conflict" if self.workflow.get("edges") and any(
                edge.get("via") is not None or "channelX" in edge or "channelY" in edge or edge.get("labelAt") is not None
                for edge in self.edges) else "workflow/solver-budget-exhausted"
            raise DiagnosticError(message, [diagnostic(code, message, {"diagramType": "workflow", "path": "/meta/viewBox"},
                {"actualViewBox": list(self.view_box), "requiredViewBox": list(self.required_view_box),
                 "contentBounds": [bounds["left"], bounds["top"], bounds["right"], bounds["bottom"]],
                 "contributors": bounds["contributors"]}, [])])
        if not self.meta.get("viewBox"):
            self.view_box = list(self.required_view_box)
            return
        if self.view_box[0] >= self.required_view_box[0] and self.view_box[1] >= self.required_view_box[1]:
            return
        message = (f'Workflow viewBox {self.view_box[0]}×{self.view_box[1]} cannot contain the readable-v2 layout; '
                   f'minimum {self.required_view_box[0]}×{self.required_view_box[1]}.')
        fixes = [f'set meta.viewBox to at least [{self.required_view_box[0]}, {self.required_view_box[1]}]',
                 'omit meta.viewBox so the compiler can use its measured intrinsic canvas']
        raise DiagnosticError(message, [diagnostic("workflow/viewbox-capacity", message,
            {"diagramType": "workflow", "path": "/meta/viewBox"},
            {"actualViewBox": list(self.view_box), "requiredViewBox": list(self.required_view_box),
             "contentBounds": [bounds["left"], bounds["top"], bounds["right"], bounds["bottom"]],
             "contributors": bounds["contributors"]}, fixes)])

    def _accepts_fix(self, mutator):
        if not self.discover_fixes:
            return False
        candidate = copy.deepcopy(self.workflow)
        mutator(candidate)
        try:
            with diagnostic_scope(suppress=True):
                _render_workflow_with_feedback(candidate, self.quality, discover_fixes=False)
            return True
        except (DiagnosticError, WorkflowLayoutFeedback, KeyError, IndexError, TypeError, ValueError):
            return False

    def _raise_problem_list(self, prefix, messages, code="layout/constraint", subject=None):
        if not messages:
            return
        entries = [diagnostic(code, message, subject or {"diagramType": "workflow"}, {}, []) for message in messages]
        raise DiagnosticError(prefix + ":\n- " + "\n- ".join(messages), entries)

    def _authored_pin_evidence(self, edge, field):
        edge_index = self.authored_edge_indexes.get(id(edge))
        value = copy.deepcopy(edge.get(field))
        return {"edge": workflow_edge_name(edge), "field": field,
                **({"path": f"/edges/{edge_index}/{field}"} if edge_index is not None else {}), "value": value}

    @staticmethod
    def _authored_route_assertion_fields(edge):
        fields = []
        if isinstance(edge.get("via"), list): fields.append("via")
        if "channelX" in edge: fields.append("channelX")
        if "channelY" in edge: fields.append("channelY")
        if edge.get("route") not in (None, "auto"): fields.append("route")
        if edge.get("fromSide") not in (None, "auto"): fields.append("fromSide")
        if edge.get("toSide") not in (None, "auto"): fields.append("toSide")
        return fields

    def _raise_explicit_pin_conflict(self, edge, invariant, evidence, supported_fixes=()):
        message = f'Workflow edge "{workflow_edge_name(edge)}" has explicit geometry that violates {invariant}.'
        pins = as_array(evidence.get("conflictingPins"))
        edge_index = self.authored_edge_indexes.get(id(edge))
        pin_path = None
        if len(pins) == 1 and edge_index is not None and pins[0].get("field"):
            pin_path = pins[0].get("path") or f'/edges/{edge_index}/{pins[0]["field"]}'
        subject = {"diagramType": "workflow", "edge": edge.get("id"), "from": edge.get("from"), "to": edge.get("to")}
        if pin_path:
            subject["path"] = pin_path
        raise DiagnosticError(message, [diagnostic("workflow/explicit-pin-conflict", message, subject,
                                                    {"invariant": invariant, **evidence}, list(supported_fixes))])

    def _route_fix(self, edge, clear_sides=False):
        edge_index = self.edge_index_by_identity[id(edge)]
        if clear_sides:
            message = f'remove explicit route geometry and endpoint sides from edge "{workflow_edge_name(edge)}" so readable-v2 can use its verified automatic candidate'
        else:
            message = f'remove explicit route geometry from edge "{workflow_edge_name(edge)}" so readable-v2 can use its verified automatic candidate'
        def mutate(document):
            candidate = document["edges"][edge_index]
            for field in ("via", "channelX", "channelY", "route"):
                candidate.pop(field, None)
            if clear_sides:
                candidate.pop("fromSide", None)
                candidate.pop("toSide", None)
        return message if self._accepts_fix(mutate) else None

    def _pin_removal_alternatives(self, edge, fields, reason):
        edge_index = self.edge_index_by_identity[id(edge)]
        unique = list(dict.fromkeys(field for field in fields if field in edge))
        if not self.discover_fixes:
            return {"removalSets": [], "supportedFixes": []}
        # Search in increasing edit count, matching the canonical compiler's
        # verified alternatives and stable field order.
        from itertools import combinations
        for size in range(1, len(unique) + 1):
            removal_sets = []
            for field_set in combinations(unique, size):
                if self._accepts_fix(lambda document, fs=field_set: [document["edges"][edge_index].pop(field, None) for field in fs]):
                    removal_sets.append(list(field_set))
            if removal_sets:
                return {"removalSets": removal_sets, "supportedFixes": [
                    f'remove {" and ".join(field_set)} from edge "{workflow_edge_name(edge)}" {reason}'
                    for field_set in removal_sets]}
        return {"removalSets": [], "supportedFixes": []}

    def _conflict_pin_fields(self, edge, removal_sets, fallback=()):
        fields = list(dict.fromkeys(field for field_set in removal_sets for field in field_set)) if removal_sets else list(dict.fromkeys(fallback))
        return [self._authored_pin_evidence(edge, field) for field in fields]

    def raise_preset_conflict(self, edge, preset, points, from_side, to_side):
        message = (f'Workflow edge "{workflow_edge_name(edge)}" cannot satisfy route preset "{preset}" under readable-v2 constraints '
                   '(minimum 8px endpoint stubs, 16px interior turns, and 28px direct clearance).')
        fixes = []
        edge_index = self.edge_index_by_identity[id(edge)]
        for candidate in ("straight", "drop", "outside-right", "return-left", "bottom-channel", "up-channel"):
            if candidate == preset:
                continue
            if self._accepts_fix(lambda document, candidate=candidate: document["edges"][edge_index].__setitem__("route", candidate)):
                fixes.append(f'set edge "{workflow_edge_name(edge)}" route to verified preset "{candidate}"')
        if self._accepts_fix(lambda document: document["edges"][edge_index].pop("route", None)):
            fixes.append(f'remove route from edge "{workflow_edge_name(edge)}" so readable-v2 can use its verified automatic candidate')
        raise DiagnosticError(message, [diagnostic("workflow/route-preset-conflict", message,
            {"diagramType": "workflow", "edge": edge.get("id"), "from": edge.get("from"), "to": edge.get("to"), "route": preset},
            {"attemptedCandidateFamily": preset, "points": points, "fromSide": from_side, "toSide": to_side,
             "requiredEndpointStubPx": 8, "requiredInteriorSegmentPx": 16, "requiredDirectClearancePx": 28}, fixes)])

    def raise_endpoint_side_conflict(self, edge, primary_sides, primary_ports, failure):
        fields = [field for field, side in (("fromSide", edge.get("fromSide")), ("toSide", edge.get("toSide")))
                  if side and side != "auto"]
        alternatives = self._pin_removal_alternatives(edge, fields, "so readable-v2 can replan the remaining endpoint-side pins")
        source = primary_ports.get("from") if primary_ports else None
        target = primary_ports.get("to") if primary_ports else None
        source = source or anchor(self.nodes[edge["from"]], primary_sides["fromSide"])
        target = target or anchor(self.nodes[edge["to"]], primary_sides["toSide"])
        evidence = {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields),
                    "actualCoordinates": {"sourceAnchor": list(source), "targetAnchor": list(target)},
                    "fromSide": primary_sides["fromSide"], "toSide": primary_sides["toSide"]}
        if failure and getattr(failure, "diagnostics", None):
            attempted = failure.diagnostics[0].get("evidence", {})
            for key in ("attemptedCandidateFamilies", "candidateCount"):
                if key in attempted: evidence[key] = attempted[key]
        self._raise_explicit_pin_conflict(edge, "readable route feasibility with authored endpoint sides", evidence, alternatives["supportedFixes"])

    def validate_readable_inputs_before_routing(self):
        if self.workflow.get("schema_version") != 2:
            return

        def fail(code, message, subject, evidence=None, fixes=()):
            raise DiagnosticError(message, [diagnostic(code, message, subject, evidence or {}, list(fixes))])

        def unused_id(base, used):
            suffix = 2
            while f"{base}-{suffix}" in used:
                suffix += 1
            return f"{base}-{suffix}"

        for kind, collection, source_indexes, id_field in (
            ("lane", self.lanes, self.authored_lane_indexes, "lanes"),
            ("node", as_array(self.workflow.get("nodes")), self.authored_node_indexes, "nodes"),
        ):
            first = {}
            for item in sorted(collection, key=lambda value: source_indexes.get(id(value), 0)):
                ident = item.get("id")
                index = source_indexes.get(id(item), 0)
                if ident in first:
                    code = f"workflow/duplicate-{kind}-id"
                    message = f'Workflow {kind} id "{ident}" is duplicated.'
                    replacement = unused_id(ident, {candidate.get("id") for candidate in collection})
                    canonical_index = next(i for i, candidate in enumerate(collection) if candidate is item)
                    fixes = [f'rename /{id_field}/{index}/id to verified unique id "{replacement}"'] if self._accepts_fix(
                        lambda document, i=canonical_index, replacement=replacement: document[id_field][i].__setitem__("id", replacement)) else []
                    fail(code, message, {"diagramType": "workflow", kind: ident, "path": f"/{id_field}/{index}/id"},
                         {f"duplicate{kind.title()}Id": ident, "firstPath": f'/{id_field}/{first[ident]}/id',
                          "duplicatePath": f"/{id_field}/{index}/id"}, fixes)
                first[ident] = index

        known = set(self.nodes)
        available_nodes = sorted(known, key=str)
        for edge in sorted(self.edges, key=lambda item: self.authored_edge_indexes.get(id(item), 0)):
            for field, endpoint in (("from", "source"), ("to", "target")):
                if edge.get(field) in known:
                    continue
                edge_index = self.edges.index(edge)
                source_index = self.authored_edge_indexes.get(id(edge), edge_index)
                message = f'Workflow edge "{workflow_edge_name(edge)}" references unknown {endpoint} "{edge.get(field)}".'
                fixes = []
                for node_id in available_nodes:
                    if self._accepts_fix(lambda document, i=edge_index, field=field, node_id=node_id:
                                         document["edges"][i].__setitem__(field, node_id)):
                        fixes.append(f'set /edges/{source_index}/{field} to verified node id "{node_id}"')
                fail("workflow/unknown-edge-endpoint", message,
                     {"diagramType": "workflow", "edge": edge.get("id"), "path": f"/edges/{edge_index}/{field}",
                      "from": edge.get("from"), "to": edge.get("to")},
                     {"endpoint": endpoint, "unknownNodeId": edge.get(field), "availableNodeIds": available_nodes}, fixes)

        lane_ids = {lane.get("id") for lane in self.lanes}
        available_lanes = sorted(lane_ids, key=str)
        nodes_by_lane = {}
        original_nodes = as_array(self.workflow.get("nodes"))
        for node in sorted(original_nodes, key=lambda item: self.authored_node_indexes.get(id(item), 0)):
            node_index = self.authored_node_indexes.get(id(node), original_nodes.index(node))
            measured = self.nodes.get(node.get("id"), {})
            if node.get("lane") not in lane_ids:
                message = f'Workflow node "{node.get("id")}" uses unknown lane "{node.get("lane")}".'
                canonical_index = self.workflow["nodes"].index(node)
                fixes = [f'set /nodes/{node_index}/lane to verified lane id "{lane_id}"' for lane_id in available_lanes
                         if self._accepts_fix(lambda document, i=canonical_index, lane_id=lane_id:
                                              document["nodes"][i].__setitem__("lane", lane_id))]
                fail("workflow/unknown-node-lane", message,
                     {"diagramType": "workflow", "node": node.get("id"), "path": f"/nodes/{node_index}/lane"},
                     {"unknownLaneId": node.get("lane"), "availableLaneIds": available_lanes}, fixes)
            col = node.get("col")
            if not _is_integer(col) or col < 0 or col >= len(self.layout["colXs"]):
                message = f'Workflow node "{node.get("id")}" uses column {col}, but valid columns are integers 0..{len(self.layout["colXs"]) - 1}.'
                canonical_index = self.workflow["nodes"].index(node)
                fixes = [f"set /nodes/{node_index}/col to verified column {candidate}"
                         for candidate in range(len(self.layout["colXs"]))
                         if self._accepts_fix(lambda document, i=canonical_index, candidate=candidate:
                                              document["nodes"][i].__setitem__("col", candidate))]
                fail("workflow/invalid-node-column", message,
                     {"diagramType": "workflow", "node": node.get("id"), "path": f"/nodes/{node_index}/col"},
                     {"actualColumn": col, "minimumColumn": 0, "maximumColumn": len(self.layout["colXs"]) - 1}, fixes)
            if not is_finite_point(measured.get("x"), measured.get("y"), measured.get("cx"), measured.get("cy")):
                message = f'Workflow node "{node.get("id")}" produced non-finite coordinates.'
                fail("workflow/non-finite-node-geometry", message,
                     {"diagramType": "workflow", "node": node.get("id"), "path": f"/nodes/{node_index}"},
                     {"measuredRect": {"x": measured.get("x"), "y": measured.get("y"), "width": measured.get("width"), "height": measured.get("height")},
                      "authored": {"col": node.get("col"), "width": node.get("width"), "height": node.get("height"), "yOffset": node.get("yOffset")}}, [])
            nodes_by_lane.setdefault(node["lane"], []).append(measured)

        overlaps = []
        for lane, lane_nodes in nodes_by_lane.items():
            for left_index, left in enumerate(lane_nodes):
                for right in lane_nodes[left_index + 1:]:
                    if not rects_overlap(left, right, 8):
                        continue
                    right_index = self.authored_node_indexes.get(id(next(n for n in original_nodes if n.get("id") == right.get("id"))))
                    canonical_index = self.workflow["nodes"].index(next(n for n in original_nodes if n.get("id") == right.get("id")))
                    fixes = [f"set /nodes/{right_index}/col to verified free column {col}"
                             for col in range(len(self.layout["colXs"])) if col != right.get("col")
                             and self._accepts_fix(lambda document, i=canonical_index, col=col:
                                                  document["nodes"][i].__setitem__("col", col))]
                    message = f'Workflow nodes "{left["id"]}" and "{right["id"]}" are less than 8px apart in lane "{lane}".'
                    overlaps.append(diagnostic("workflow/node-overlap", message,
                        {"diagramType": "workflow", "node": right["id"], "path": f"/nodes/{right_index}"},
                        {"lane": lane, "minimumClearancePx": 8,
                         "nodes": [{"id": left["id"], "rect": {key: left[key] for key in ("x", "y", "width", "height")}},
                                   {"id": right["id"], "rect": {key: right[key] for key in ("x", "y", "width", "height")}}]}, fixes))
        if overlaps:
            message = overlaps[0]["message"] if len(overlaps) == 1 else "Workflow node overlap:\n- " + "\n- ".join(item["message"] for item in overlaps)
            raise DiagnosticError(message, overlaps)

    def validate_readable_pinned_geometry(self):
        if self.workflow.get("schema_version") != 2:
            return
        for edge in self.edges:
            if not isinstance(edge.get("via"), list) and "channelX" not in edge and "channelY" not in edge:
                continue
            if edge.get("from") not in self.nodes or edge.get("to") not in self.nodes:
                continue
            fields = [field for field in ("channelX", "channelY") if field in edge]
            preset = edge.get("route") or "auto"
            allowed = {"straight": set(), "drop": {"channelY"}, "outside-right": {"channelX"},
                       "return-left": {"channelX"}, "bottom-channel": {"channelY"},
                       "up-channel": {"channelY"}}.get(preset, set())
            conflicting = [field for field in fields if field not in allowed]
            if preset != "auto" and conflicting:
                alternatives = self._pin_removal_alternatives(edge, ["route", *conflicting],
                                                               "and keep the remaining verified route assertions")
                self._raise_explicit_pin_conflict(edge, "route preset compatibility",
                    {"route": preset, "allowedPins": sorted(allowed),
                     "conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], ["route", *conflicting])},
                    alternatives["supportedFixes"])

        # Absolute geometry on a contested endpoint takes precedence over any
        # automatic route that might otherwise reserve a different port.
        for edge in self.edges:
            if not (isinstance(edge.get("via"), list) or "channelX" in edge or "channelY" in edge):
                continue
            if edge.get("from") in self.nodes and edge.get("to") in self.nodes and any(
                    other is not edge and not (isinstance(other.get("via"), list) or "channelX" in other or "channelY" in other)
                    and {edge.get("from"), edge.get("to")}.intersection((other.get("from"), other.get("to")))
                    for other in self.edges):
                self.router.path_for(edge)

        for edge in self.edges:
            if edge.get("from") not in self.nodes or edge.get("to") not in self.nodes:
                continue
            edge_index = self.authored_edge_indexes.get(id(edge), self.edge_index_by_identity[id(edge)])
            edge_name = workflow_edge_name(edge)
            via = edge.get("via") if isinstance(edge.get("via"), list) else []
            negative_via = next((index for index, point in enumerate(via)
                                 if point[0] < 0 or point[1] < 0), None)
            negative = ({"field": "via", "path": f"/edges/{edge_index}/via/{negative_via}",
                         "value": copy.deepcopy(via[negative_via])} if negative_via is not None else
                        {"field": "channelX", "path": f"/edges/{edge_index}/channelX", "value": edge["channelX"]}
                        if edge.get("channelX", 0) < 0 else
                        {"field": "channelY", "path": f"/edges/{edge_index}/channelY", "value": edge["channelY"]}
                        if edge.get("channelY", 0) < 0 else None)
            if negative:
                fields = [field for field in ("via", "channelX", "channelY") if field in edge]
                alternatives = self._pin_removal_alternatives(edge, fields, "and replan the remaining explicit route assertions")
                self._raise_explicit_pin_conflict(edge, "viewBox-origin containment",
                    {"conflictingPins": [{"edge": edge_name, **negative}], "minimumCoordinate": 0},
                    alternatives["supportedFixes"])

            if isinstance(edge.get("labelAt"), list):
                rect = self.router.label_rect_for(edge, self.edge_index_by_identity[id(edge)])
                if rect and (rect["x"] < 0 or rect["y"] < 0):
                    pin = self._authored_pin_evidence(edge, "labelAt")
                    self._raise_explicit_pin_conflict(edge, "viewBox-origin containment",
                        {"conflictingPins": [pin], "offendingRect": {key: rect[key] for key in ("x", "y", "width", "height")},
                         "minimumCoordinate": 0}, [])

            pinned_route = isinstance(edge.get("via"), list) or "channelX" in edge or "channelY" in edge
            routed = self.router.path_for(edge)
            points = routed["points"]
            if pinned_route:
                for index, point in enumerate(points):
                    if not isinstance(point, (list, tuple)) or len(point) != 2 or not is_finite_point(*point):
                        alternatives = self._pin_removal_alternatives(edge, [field for field in ("via", "channelX", "channelY") if field in edge],
                            "and replan the remaining explicit route assertions")
                        self._raise_explicit_pin_conflict(edge, "finite route coordinates",
                            {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], ["via"]),
                             "pointIndex": index, "point": point}, alternatives["supportedFixes"])
                for index, (start, end) in enumerate(zip(points, points[1:])):
                    dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
                    fields = [field for field in ("via", "channelX", "channelY") if field in edge]
                    alternatives = self._pin_removal_alternatives(edge, fields, "and replan the remaining explicit route assertions")
                    conflicting = self._conflict_pin_fields(edge, alternatives["removalSets"], fields)
                    if dx <= 0.0001 and dy <= 0.0001:
                        self._raise_explicit_pin_conflict(edge, "non-zero route segments",
                            {"conflictingPins": conflicting, "segmentIndex": index, "from": start, "to": end},
                            alternatives["supportedFixes"])
                    if dx > 0.0001 and dy > 0.0001:
                        self._raise_explicit_pin_conflict(edge, "orthogonal route segments",
                            {"conflictingPins": conflicting, "segmentIndex": index, "from": start, "to": end},
                            alternatives["supportedFixes"])
                    endpoint = index == 0 or index == len(points) - 2
                    minimum = 28 if len(points) == 2 else 8 if endpoint else 16
                    length = dx + dy
                    if length + 0.0001 < minimum:
                        invariant = "8px endpoint stub clearance" if endpoint else "16px interior turn clearance"
                        self._raise_explicit_pin_conflict(edge, invariant,
                            {"conflictingPins": conflicting, "segmentIndex": index,
                             "position": "source-stub" if index == 0 else "target-stub" if endpoint else "interior",
                             "from": start, "to": end, "lengthPx": length, "minimumPx": minimum},
                            alternatives["supportedFixes"])
                from_side, to_side = self.router.edge_sides(edge)["fromSide"], self.router.edge_sides(edge)["toSide"]
                for field in ("channelX", "channelY"):
                    if field in edge and not self.router.route_contains_channel_pin(points, field, edge[field]) and isinstance(edge.get("via"), list):
                        self._raise_explicit_pin_conflict(edge, "channel pin preservation",
                            {"conflictingPins": self._conflict_pin_fields(edge, [["via", field]], ["via", field]),
                             "route": edge.get("route") or "auto", "points": points}, [])
                if edge.get("route") not in (None, "auto") and not self.router.route_matches_preset_family(
                        edge["route"], points, self.nodes[edge["from"]], self.nodes[edge["to"]]):
                    self.raise_preset_conflict(edge, edge["route"], points, from_side, to_side)
                if not self.router.endpoint_side_is_honored(points, from_side, "source") or not self.router.endpoint_side_is_honored(points, to_side, "target"):
                    fields = [field for field, side, endpoint in (("fromSide", from_side, "source"), ("toSide", to_side, "target"))
                              if edge.get(field) not in (None, "auto") and not self.router.endpoint_side_is_honored(points, side, endpoint)]
                    alternatives = self._pin_removal_alternatives(edge, [*fields, *[field for field in ("via", "channelX", "channelY") if field in edge]],
                                                                    "and replan the remaining explicit pins")
                    self._raise_explicit_pin_conflict(edge, "perpendicular endpoint-side direction",
                        {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields),
                         "points": points, "fromSide": from_side, "toSide": to_side}, alternatives["supportedFixes"])
                collision = self.router.first_route_node_collision(edge, points)
                if collision:
                    alternatives = self._pin_removal_alternatives(edge, fields, "and replan the remaining explicit route assertions")
                    self._raise_explicit_pin_conflict(edge, "node clearance",
                        {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields), **collision},
                        alternatives["supportedFixes"])
                for invariant, obstacles in (("legend clearance", self.workflow_legend_rects()),
                                              ("lane/phase/group label clearance", self.workflow_scene_label_obstacles())):
                    obstacle = next((rect for rect in obstacles if any(segment_intersects_rect(start, end, rect)
                                      for start, end in zip(points, points[1:]))) , None)
                    if obstacle:
                        self._raise_explicit_pin_conflict(edge, invariant,
                            {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields),
                             "points": points, "obstacle": obstacle}, alternatives["supportedFixes"])
                runs = self.router.composition_frames()
                frame_hits = self.router.r  # Keep frame collection invocation-local.
                from .flow_geometry import collect_border_runs
                hit = next(iter(collect_border_runs([{"points": points}], runs)), None)
                if hit:
                    self._raise_explicit_pin_conflict(edge, "structural-frame border clearance",
                        {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields),
                         "points": points, "frame": hit["frame"].get("id", hit["frameIndex"]),
                         "side": hit["side"], "overlapLengthPx": hit["overlapLength"]}, alternatives["supportedFixes"])

            if edge.get("labelAt") is not None:
                rect = self.router.label_rect_for(edge, self.edge_index_by_identity[id(edge)])
                obstacle = next((node for node in self.nodes.values() if rect and rects_overlap(rect, node, -2)), None)
                if obstacle:
                    self._raise_explicit_pin_conflict(edge, "edge-label node clearance",
                        {"conflictingPins": [self._authored_pin_evidence(edge, "labelAt")], "labelAt": edge["labelAt"],
                         "labelRect": {key: rect[key] for key in ("x", "y", "width", "height")}, "obstacleNode": obstacle["id"]}, [])
                for invariant, obstacles in (("edge-label legend clearance", self.workflow_legend_rects()),
                                              ("edge-label lane/phase/group clearance", self.workflow_scene_label_obstacles())):
                    obstacle = next((item for item in obstacles if rect and rects_overlap(rect, item)), None)
                    if obstacle:
                        self._raise_explicit_pin_conflict(edge, invariant,
                            {"conflictingPins": [self._authored_pin_evidence(edge, "labelAt")], "labelAt": edge["labelAt"],
                             "labelRect": {key: rect[key] for key in ("x", "y", "width", "height")}, "obstacle": obstacle}, [])

        self.validate_readable_pairwise_pin_conflicts()

    def validate_readable_pairwise_pin_conflicts(self):
        labels = [self.router.label_rect_for(edge, index) for index, edge in enumerate(self.edges)]
        labels = [item for item in labels if item]
        routes = [{"relation": edge, "relationIndex": index, "points": self.router.path_for(edge)["points"]}
                  for index, edge in enumerate(self.edges) if edge.get("from") in self.nodes and edge.get("to") in self.nodes]
        hit = next((item for item in collect_label_route_clearance(labels, routes, 4)
                    if isinstance(item.get("labelRelation", {}).get("labelAt"), list)
                    or self._authored_route_assertion_fields(item.get("otherRelation", {}))), None)
        if hit:
            label_edge, route_edge = hit["labelRelation"], hit["otherRelation"]
            fields = self._authored_route_assertion_fields(route_edge)
            label_pinned = isinstance(label_edge.get("labelAt"), list)
            diagnostic_edge = label_edge if label_pinned else route_edge
            route_alternatives = self._pin_removal_alternatives(route_edge, fields,
                "so readable-v2 can replan the remaining authored label-route pins") if fields else {"removalSets": [], "supportedFixes": []}
            evidence = {"conflictingPins": ([self._authored_pin_evidence(label_edge, "labelAt")] if label_pinned else [])
                        + self._conflict_pin_fields(route_edge, route_alternatives["removalSets"], fields),
                        "label": hit["label"].get("label"), "routeSegmentIndex": hit["segmentIndex"],
                        "routeSegment": {"from": hit["start"], "to": hit["end"]},
                        "clearancePx": hit["clearance"], "minimumClearancePx": 4}
            self._raise_explicit_pin_conflict(diagnostic_edge, "explicit label-route clearance", evidence,
                                               route_alternatives["supportedFixes"])
        for left_index, left in enumerate(labels):
            for right in labels[left_index + 1:]:
                if not rects_overlap(left, right, -2):
                    continue
                pinned = [item["relation"] for item in (left, right) if item["relation"].get("labelAt") is not None]
                if not pinned:
                    continue
                edge = pinned[0]
                self._raise_explicit_pin_conflict(edge, "explicit label-label clearance",
                    {"conflictingPins": [self._authored_pin_evidence(item, "labelAt") for item in pinned],
                     "labels": [{"edge": item.get("id"), "rect": {key: item_rect[key] for key in ("x", "y", "width", "height")}}
                                for item, item_rect in ((left["relation"], left), (right["relation"], right))]}, [])
        if self.meta.get("quality_profile") != "showcase":
            return
        for left_index, left in enumerate(routes):
            for right in routes[left_index + 1:]:
                a, b = left["relation"], right["relation"]
                if not (self._authored_route_assertion_fields(a) or self._authored_route_assertion_fields(b)):
                    continue
                if {a.get("from"), a.get("to")}.intersection((b.get("from"), b.get("to"))):
                    continue
                for ls in forward_collinear_analysis_segments(left["points"]):
                    for rs in forward_collinear_analysis_segments(right["points"]):
                        point = proper_segment_intersection(ls["start"], ls["end"], rs["start"], rs["end"])
                        if not point:
                            continue
                        lidx, ridx = source_segment_index_at_point(ls, point), source_segment_index_at_point(rs, point)
                        edge = a if self._authored_route_assertion_fields(a) else b
                        self._raise_explicit_pin_conflict(edge, "explicit route-route crossing",
                            {"conflictingPins": self._conflict_pin_fields(a, [], self._authored_route_assertion_fields(a))
                                + self._conflict_pin_fields(b, [], self._authored_route_assertion_fields(b)),
                             "point": point, "segmentIndex": lidx, "otherSegmentIndex": ridx,
                             "routeSegments": [{"edge": a.get("id"), "from": ls["start"], "to": ls["end"]},
                                               {"edge": b.get("id"), "from": rs["start"], "to": rs["end"]}]}, [])
        corridor = next((item for item in collect_ambiguous_corridors(routes, min_overlap_px=8)
                         if self._authored_route_assertion_fields(item["left"]["relation"])
                         or self._authored_route_assertion_fields(item["right"]["relation"])), None)
        if corridor:
            left, right = corridor["left"], corridor["right"]
            edge = left["relation"] if self._authored_route_assertion_fields(left["relation"]) else right["relation"]
            fields = self._authored_route_assertion_fields(edge)
            alternatives = self._pin_removal_alternatives(edge, fields,
                "so readable-v2 can replan the remaining authored route assertions")
            self._raise_explicit_pin_conflict(edge, "explicit route-route corridor clearance",
                {"conflictingPins": self._conflict_pin_fields(edge, alternatives["removalSets"], fields),
                 "segmentIndex": corridor["leftSegment"], "otherSegmentIndex": corridor["rightSegment"],
                 "overlapStart": corridor["overlapStart"], "overlapEnd": corridor["overlapEnd"],
                 "overlapLengthPx": corridor["overlapLength"], "minimumClearancePx": 8}, alternatives["supportedFixes"])

    def validate_workflow(self):
        problems_found = []
        diagnostics_found = []
        lane_ids = {lane.get("id") for lane in self.lanes}
        if len(lane_ids) != len(self.lanes):
            problems_found.append("Lane ids must be unique.")
        if len(self.nodes) != len(as_array(self.workflow.get("nodes"))):
            problems_found.append("Node ids must be unique.")
        phases = as_array(self.workflow.get("phases"))
        groups = as_array(self.workflow.get("groups"))
        if len({phase.get("id") for phase in phases}) != len(phases): problems_found.append("Phase ids must be unique.")
        if len({group.get("id") for group in groups}) != len(groups): problems_found.append("Group ids must be unique.")
        for node in self.nodes.values():
            if node.get("lane") not in lane_ids:
                problems_found.append(f'Node "{node.get("id")}" uses unknown lane "{node.get("lane")}".')
                continue
            col = node.get("col")
            if not _is_integer(col) or col < 0 or col >= len(self.layout["colXs"]):
                problems_found.append(f'Node "{node.get("id")}" uses column {col}, but valid columns are integers 0..{len(self.layout["colXs"]) - 1}.')
                continue
            if not is_finite_point(node.get("x"), node.get("y"), node.get("cx"), node.get("cy")):
                problems_found.append(f'Node "{node.get("id")}" produced non-finite coordinates — check col, width, height, and yOffset are numbers.')
                continue
            estimated = text_units(node.get("label")) * 6.8
            if estimated > node["width"] + 6:
                problems_found.append(f'Label "{node.get("label")}" (~{math.floor(estimated + 0.5)}px) is wider than node "{node.get("id")}" ({number(node["width"])}px) — shorten the label or increase node.width.')
            top = self.lane_top(node["lane"])
            content_top = top + self.layout["laneTitleH"] + self.lane_group_header_height(node["lane"])
            if node["x"] < self.layout["laneX"] or node["x"] + node["width"] > self.layout["laneX"] + self.layout["laneW"]:
                problems_found.append(f'Node "{node.get("id")}" exceeds the horizontal bounds of lane "{node.get("lane")}".')
            if node["y"] < content_top or node["y"] + node["height"] > top + self.lane_height(node["lane"]):
                problems_found.append(f'Node "{node.get("id")}" collides with the title or boundary of lane "{node.get("lane")}".')
        phase_ranges = []
        for phase in phases:
            start, end = phase.get("fromCol"), phase.get("toCol")
            if not _is_integer(start) or not _is_integer(end):
                problems_found.append(f'Phase "{phase.get("id")}" must use integer fromCol/toCol values.')
                continue
            if start < 0 or end >= len(self.layout["colXs"]) or start > end:
                problems_found.append(f'Phase "{phase.get("id")}" uses invalid columns {start}..{end}; use an ordered range within 0..{len(self.layout["colXs"]) - 1}.')
            else:
                phase_ranges.append(phase)
        phase_ranges.sort(key=lambda item: (item["fromCol"], item["toCol"]))
        for index, earlier in enumerate(phase_ranges):
            for later in phase_ranges[index + 1:]:
                if later["fromCol"] > earlier["toCol"]: break
                problems_found.append(f'Phase "{later.get("id")}" ({later["fromCol"]}..{later["toCol"]}) overlaps phase "{earlier.get("id")}" ({earlier["fromCol"]}..{earlier["toCol"]}) — start at col {earlier["toCol"] + 1} or later, or end the earlier phase at col {later["fromCol"] - 1}.')
        for group in groups:
            start, end = group.get("fromCol"), group.get("toCol")
            if group.get("lane") not in lane_ids:
                problems_found.append(f'Group "{group.get("id")}" uses unknown lane "{group.get("lane")}".')
                continue
            if not _is_integer(start) or not _is_integer(end):
                problems_found.append(f'Group "{group.get("id")}" must use integer fromCol/toCol values.')
                continue
            if start < 0 or end >= len(self.layout["colXs"]) or start > end:
                problems_found.append(f'Group "{group.get("id")}" uses invalid columns {start}..{end}; use an ordered range within 0..{len(self.layout["colXs"]) - 1}.')
            elif not any(node.get("lane") == group["lane"] and start <= node.get("col", -1) <= end for node in self.nodes.values()):
                problems_found.append(f'Group "{group.get("id")}" does not contain any nodes — align its lane/columns with the parallel or branch work it frames.')
        by_lane = {}
        for node in self.nodes.values(): by_lane.setdefault(node.get("lane"), []).append(node)
        for lane, lane_nodes in by_lane.items():
            for index, left in enumerate(lane_nodes):
                for right in lane_nodes[index + 1:]:
                    if rects_overlap(left, right, 8):
                        problems_found.append(f'Nodes "{left["id"]}" and "{right["id"]}" are less than 8px apart in lane "{lane}" — move one to another col, adjust yOffset, or reduce width/height.')
        for edge in self.edges:
            if edge.get("from") not in self.nodes: problems_found.append(f'Edge "{edge.get("label") or edge.get("from")}" references unknown source "{edge.get("from")}".')
            if edge.get("to") not in self.nodes: problems_found.append(f'Edge "{edge.get("label") or edge.get("to")}" references unknown target "{edge.get("to")}".')
            if edge.get("from") in self.nodes and edge.get("to") in self.nodes:
                points = self.router.path_for(edge)["points"]
                if len(points) == 2:
                    length = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
                    if length < 28:
                        problems_found.append(f'Edge "{edge.get("from")}" -> "{edge.get("to")}" is too short ({math.floor(length + 0.5)}px; minimum 28px) — move the nodes farther apart or use a verified orthogonal route with readable clearance.')
        endpoint_ids = set(self.nodes)
        problems_found.extend(clean_endpoint_side_problems(self.edges, endpoint_ids, self.router.path_for,
            lambda edge, endpoint: self.router.edge_sides(edge)["fromSide" if endpoint == "source" else "toSide"],
            "keep automatic routing, or choose fromSide/toSide and via points whose first and final segments cross node borders perpendicularly",
            "workflow", "edges"))
        problems_found.extend(clean_flow_problems(self.edges, self.nodes.values(), self.router.path_for,
            "adjust fromSide/toSide, set route/via or channel coordinates, or move the node to a clearer lane/column",
            diagram_type="workflow", relation_collection="edges", obstacle_kind="node"))
        crossing = clean_crossing_problems(self.edges, endpoint_ids, self.router.path_for, self.meta.get("quality_profile"),
            "adjust route/via, bias, or channel coordinates so the edges use separate lane corridors",
            diagram_type="workflow", relation_collection="edges", include_shared_endpoints=lambda _a, _b: self.workflow.get("schema_version") == 2,
            merge_forward_collinear_waypoints=self.workflow.get("schema_version") == 2,
            warn_in_standard=self.workflow.get("schema_version") == 2)
        diagnostics_found.extend(crossing)
        problems_found.extend(clean_ambiguous_corridor_problems(self.edges, endpoint_ids, self.router.path_for,
            self.meta.get("quality_profile"), "adjust route/via, bias, or channel coordinates so unrelated edges do not visually merge",
            diagram_type="workflow", relation_collection="edges",
            include_shared_endpoints=lambda _a, _b: self.workflow.get("schema_version") == 2,
            allow_short_workflow_trunks=self.workflow.get("schema_version") == 2))
        if self.workflow.get("schema_version") == 2:
            routed = [{"relation": {**edge, "width": edge.get("width") or (1.8 if edge.get("variant") == "emphasis" else 1.4)},
                       "relationIndex": index, "points": self.router.path_for(edge)["points"]}
                      for index, edge in enumerate(self.edges) if edge.get("from") in self.nodes and edge.get("to") in self.nodes]
            for hit in collect_arrowhead_collisions(routed, allow_short_workflow_trunks=True):
                left, right = hit["left"], hit["right"]
                name = lambda entry: entry["relation"].get("id") or f'{entry["relation"].get("from")}->{entry["relation"].get("to")}'
                severity = "error" if self.meta.get("quality_profile") == "showcase" else "warning"
                message = f'[composition/arrowhead-collision] workflow arrows "{name(left)}" and "{name(right)}" overlap at their destination (clearance {hit["distance"]}px; minimum {hit["minimum"]}px).'
                diagnostics_found.append(diagnostic("composition/arrowhead-collision", message,
                    {"diagramType": "workflow", "edge": name(left), "from": left["relation"].get("from"), "to": left["relation"].get("to")},
                    {"otherEdge": name(right), "distancePx": hit["distance"], "minimumPx": hit["minimum"]},
                    ["use separate destination sides or ports with clearance for both arrow markers"], severity))
                if severity == "error": problems_found.append(message)
        problems_found.extend(clean_border_run_problems(self.edges, endpoint_ids, self.workflow_composition_frames(),
            self.router.path_for, self.meta.get("quality_profile"),
            "adjust route/via, bias, or channel coordinates so the edge crosses the lane or group perpendicularly instead of following its border",
            diagram_type="workflow", relation_collection="edges"))
        problems_found.extend(clean_route_rhythm_problems(self.edges, endpoint_ids, self.router.path_for,
            self.meta.get("quality_profile"), "adjust route/via, bias, or channel coordinates so each turn has a readable run-up",
            diagram_type="workflow", relation_collection="edges"))
        main_path = self.workflow.get("mainPath")
        if isinstance(main_path, list):
            for node_id in main_path:
                if node_id not in self.nodes: problems_found.append(f'mainPath references unknown node "{node_id}".')
            for from_id, to_id in zip(main_path, main_path[1:]):
                if from_id not in self.nodes or to_id not in self.nodes: continue
                if not any(edge.get("from") == from_id and edge.get("to") == to_id for edge in self.edges):
                    problems_found.append(f'mainPath step "{from_id}" -> "{to_id}" has no matching edge — add the edge or remove the pair from mainPath.')
                if self.nodes[to_id].get("col", 0) < self.nodes[from_id].get("col", 0):
                    problems_found.append(f'mainPath step "{from_id}" -> "{to_id}" moves backward from col {self.nodes[from_id].get("col")} to {self.nodes[to_id].get("col")} — use a return edge outside mainPath for loops.')
        labels = [self.router.label_rect_for(edge, index) for index, edge in enumerate(self.edges)]
        labels = [label for label in labels if label]
        for rect in labels:
            for node in self.nodes.values():
                if rects_overlap(rect, node, -2):
                    problems_found.append(f'Label "{rect["label"]}" overlaps node "{node["id"]}" — adjust labelDx/labelDy/labelSegment or set labelAt.')
        for index, left in enumerate(labels):
            for right in labels[index + 1:]:
                if rects_overlap(left, right, -2):
                    problems_found.append(f'Labels "{left["label"]}" and "{right["label"]}" overlap — adjust labelDx/labelDy/labelSegment or route one relationship through a separate corridor.')
        problems_found.extend(clean_label_route_clearance_problems(self.edges, labels, endpoint_ids, self.router.path_for,
            self.meta.get("quality_profile"), "adjust label placement or route one relationship through a separate corridor",
            diagram_type="workflow", relation_collection="edges"))
        if self.workflow.get("schema_version") == 1:
            if self.view_box[0] < self.layout["laneX"] + self.layout["laneW"] + 16:
                problems_found.append(f'viewBox width {self.view_box[0]} clips the {self.layout["laneW"]}px lanes — set meta.viewBox[0] to at least {self.layout["laneX"] + self.layout["laneW"] + 16}.')
            if self.legend_y() + 18 > self.view_box[1]:
                problems_found.append(f'Legend exceeds viewBox height {self.view_box[1]} — set meta.viewBox[1] to at least {self.legend_y() + 18}.')
            problems_found.extend(clean_label_canvas_problems(labels, self.view_box, self.meta.get("quality_profile"),
                "adjust the label placement or enlarge meta.viewBox", "workflow", "edges"))
        if problems_found:
            errors = [entry for entry in problems_found if isinstance(entry, dict) and entry.get("severity") != "warning"]
            diagnostics_found.extend(entry for entry in problems_found if isinstance(entry, dict) and entry.get("severity") == "warning")
            diagnostics_found.extend(diagnostic("layout/constraint", message, {"diagramType": "workflow"}, {}, [])
                                    for message in problems_found if isinstance(message, str))
            self.workflow_diagnostics.extend(diagnostics_found)
            fatal = [*errors, *(entry for entry in diagnostics_found if entry.get("severity") == "error")]
            if fatal:
                first = fatal[0]["message"] if len(fatal) == 1 else "Workflow layout validation failed:\n- " + "\n- ".join(entry["message"] for entry in fatal)
                raise DiagnosticError(first, fatal)
        else:
            self.workflow_diagnostics.extend(diagnostics_found)


MAX_READABLE_LAYOUT_FEEDBACK_ROUNDS = 3


def _feedback_failure(request):
    edge_name = request.get("edge") or f'{request.get("from")}->{request.get("to")}'
    message = f'Workflow edge "{edge_name}" exhausted bounded readable-v2 layout feedback without a feasible automatic route.'
    return DiagnosticError(message, [diagnostic("workflow/solver-budget-exhausted", message,
        {"diagramType": "workflow", "edge": request.get("edge"), "from": request.get("from"), "to": request.get("to")},
        {"attemptedCandidateFamilies": request.get("attemptedCandidateFamilies"),
         "candidateCount": request.get("candidateCount")}, [])])


def _render_workflow_with_feedback(diagram, quality=None, discover_fixes=True):
    feedback = {}
    for attempt in range(MAX_READABLE_LAYOUT_FEEDBACK_ROUNDS + 1):
        try:
            renderer = WorkflowRenderer(diagram, quality, discover_fixes, feedback)
            renderer.validate_readable_inputs_before_routing()
            renderer.validate_readable_pinned_geometry()
            renderer.validate_workflow()
            renderer.finalize_view_box()
            if discover_fixes:
                for entry in renderer.workflow_diagnostics:
                    record_diagnostic(entry)
            return renderer.render_svg()
        except WorkflowLayoutFeedback as error:
            request = error.request
            next_feedback = None
            if (request.get("kind") == "rank-gap-minimum" and _is_integer(request.get("fromCol"))
                    and _is_integer(request.get("toCol")) and _is_number(request.get("minimum"))):
                key = f'{request["fromCol"]}:{request["toCol"]}'
                current = (feedback.get("rankGapMinimums") or {}).get(key, -math.inf)
                if request["minimum"] > current + 0.0001:
                    next_feedback = {**feedback,
                        "rankGapMinimums": {**(feedback.get("rankGapMinimums") or {}), key: request["minimum"]},
                        "rankGapContributors": {**(feedback.get("rankGapContributors") or {}), key: [
                            f'rank {request["fromCol"]}→{request["toCol"]} route clearance',
                            f'edge {request.get("edge") or request.get("from") + "->" + request.get("to")} route']}}
            elif (request.get("kind") == "lane-gap-minimum" and _is_number(request.get("minimum"))
                  and request["minimum"] > feedback.get("laneGapMin", -math.inf) + 0.0001):
                next_feedback = {**feedback, "laneGapMin": request["minimum"],
                    "laneGapContributors": [f'edge {request.get("edge") or request.get("from") + "->" + request.get("to")} lane-gap route clearance']}
            if next_feedback is None or attempt == MAX_READABLE_LAYOUT_FEEDBACK_ROUNDS:
                raise _feedback_failure(request)
            feedback = next_feedback
    raise RuntimeError("unreachable readable-v2 layout feedback state")


def render_workflow(diagram, quality=None):
    """Render a validated Workflow diagram to SVG without a JavaScript runtime."""
    return _render_workflow_with_feedback(diagram, quality)
