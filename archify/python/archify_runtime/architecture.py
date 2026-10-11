"""Node-free native renderer for Archify Architecture diagrams."""

import math
import os

from .architecture_grid import grid_layout, resolve_component_pos, validate_grid_placement
from .architecture_labels import place_automatic_labels, reserved_label_rect
from .architecture_router import create_router
from .common import DiagnosticError, diagnostic, esc, number, record_diagnostic, translate
from .composition import collect_arrowhead_collisions, relationship_legend_obstacles
from .drawing import (animate_attr, available_node_text_width, brand_label_fit_width,
                      brand_metadata, brand_top_rail_problem, focus_edge_attrs,
                      focus_node_attrs, focus_node_title, fitted_node_font_size,
                      minimum_node_text_width, render_brand_mark,
                      render_definitions, render_semantic_sigil, resolve_brand,
                      svg_accessible_text, svg_root_attrs, text_units)
from .flow_geometry import (authored_straight_route_attrs, clean_ambiguous_corridor_problems,
                            clean_border_run_problems, clean_crossing_problems,
                            clean_endpoint_side_problems, clean_flow_problems,
                            clean_label_canvas_problems, clean_label_route_clearance_problems,
                            clean_route_rhythm_problems, label_point, route_points_value)
from .geometry import as_array, is_finite_point, rects_overlap, segment_intersects_rect
from .legend import legend_footprint, render_legend, resolve_legend
from .route_quality import clean_route_detour_problems

COMPONENT_TEXT_FIT = {"sublabelPreferred": 9, "sublabelMinimum": 6,
                      "tagPreferred": 7, "tagMinimum": 6}
COMPONENT_FILL = {"frontend": "c-frontend", "backend": "c-backend", "database": "c-database",
                  "cloud": "c-cloud", "security": "c-security", "messagebus": "c-messagebus",
                  "external": "c-external"}
COMPONENT_TEXT = {"frontend": "t-frontend", "backend": "t-backend", "database": "t-database",
                  "cloud": "t-cloud", "security": "t-security", "messagebus": "t-messagebus",
                  "external": "t-external"}
ARROW_CLASS = {"default": ("a-default", "arrowhead"), "emphasis": ("a-emphasis", "arrowhead-emphasis"),
               "security": ("a-security", "arrowhead-security"), "dashed": ("a-dashed", "arrowhead-dashed")}
EDGE_ACCENT = {"security": "t-edge-security", "emphasis": "t-edge-emphasis",
               "dashed": "t-edge-dashed", "default": "t-edge-default"}
LAYOUT = {"defaultW": 120, "defaultH": 60, "margin": 40, "boundaryPad": 30,
          "boundaryExtraBottom": 20, "boundaryLabelBaseline": 18,
          "boundaryLabelClearance": 4, "boundaryLabelFontPreferred": 9,
          "boundaryLabelFontMinimum": 6, "boundaryLabelMaskHeight": 16,
          "boundaryLabelRailGap": 2, "boundaryLabelFrameInset": 4, "legendH": 28}
LEGEND_CATALOG_KINDS = ("frontend", "backend", "database", "cloud", "security", "messagebus", "external")


def _minimum_readable_source_text_px(view_box_width):
    diagram_width = 930
    return 6 / min(1, diagram_width / view_box_width) if view_box_width > 0 else math.nan


def _boundary_label_width(label, font_size):
    return max(30, text_units(label) * font_size * 0.6 + 10)


class ArchitectureRenderer:
    def __init__(self, diagram, quality=None):
        self.architecture = diagram
        self.meta = diagram.get("meta") or {}
        self.quality = quality or os.environ.get("ARCHIFY_QUALITY_PROFILE") or None
        self.gate_profile = self.quality or self.meta.get("quality_profile")
        self.grid = grid_layout(diagram)
        self.component_items = as_array(diagram.get("components"))
        self.connections = as_array(diagram.get("connections"))
        self.boundary_items = as_array(diagram.get("boundaries"))
        self.components = {component.get("id"): self._measure_component(component)
                           for component in self.component_items}
        self.enforces_boundary_title_composition = bool(self.meta.get("quality_profile"))
        self.component_steps = self._component_steps()
        self.legend_entries = resolve_legend(self.meta.get("legend"),
            [{"kind": kind, "label": translate(self.meta, f"legend.architecture.{kind}")}
             for kind in LEGEND_CATALOG_KINDS], {component.get("type") for component in self.components.values()})
        self.raw_boundaries = [boundary for boundary in
            (self._boundary_rect(item) for item in self.boundary_items) if boundary]
        self.resolved_label_points = {}
        self.router = create_router(self.components, self.connections,
            distinct_automatic_ports=True, prefer_readable_routes=True,
            frames=[{**boundary, "radius": 8 if boundary.get("kind") == "security-group" else 12}
                    for boundary in self.raw_boundaries],
            label_rect_for=self._reserved_connection_label)
        self.connection_labels = self._connection_label_rects()
        self.connection_geometry = [*self.connection_labels]
        self.connection_geometry.extend(
            {"x": x, "y": y, "width": 0, "height": 0}
            for connection in self.connections
            if connection.get("from") in self.components and connection.get("to") in self.components
            for x, y in self.path_for(connection)["points"])
        self.boundaries, self.boundary_readability_problem = self._resolve_boundary_titles()
        self.composition_frames = [{**boundary, "id": boundary.get("id", index),
                                    "kind": boundary.get("kind") or "boundary",
                                    "radius": 8 if boundary.get("kind") == "security-group" else 12}
                                   for index, boundary in enumerate(self.boundaries)]
        self.view_box = self.meta.get("viewBox") or self._auto_view_box(self.boundaries, self.connection_geometry)
        if self.meta.get("quality_profile") == "showcase":
            self.connection_labels = place_automatic_labels(
                keep_fallback_near_route=True, labels=self.connection_labels,
                routes=[{"relationIndex": index, "points": self.path_for(connection)["points"]}
                        for index, connection in enumerate(self.connections)
                        if connection.get("from") in self.components and connection.get("to") in self.components],
                components=list(self.components.values()),
                titles=[boundary["title"] for boundary in self.boundaries],
                view_box=self.view_box,
                placement_bottom=(self.view_box[1] - 16 - 32 - legend_footprint(
                    self.legend_entries, self.view_box[0] - LAYOUT["margin"] * 2)["extraHeight"]
                    if self.legend_entries else self.view_box[1]))
            self.resolved_label_points.update({id(rect["relation"]): [rect["lx"], rect["ly"]]
                                               for rect in self.connection_labels})
        self.validation_diagnostics = []

    def _measure_component(self, component):
        x, y = resolve_component_pos(component, self.grid)
        width, height = component.get("size") if isinstance(component.get("size"), list) else (LAYOUT["defaultW"], LAYOUT["defaultH"])
        return {**component, "x": x, "y": y, "width": width, "height": height,
                "cx": x + width / 2, "cy": y + height / 2}

    def _component_steps(self):
        result = {}
        for index, connection in enumerate(self.connections):
            result.setdefault(connection.get("from"), index)
            result.setdefault(connection.get("to"), index + 1)
        for index, component in enumerate(self.component_items):
            result.setdefault(component.get("id"), index)
        return result

    def _boundary_rect(self, boundary):
        members = [self.components[component_id] for component_id in as_array(boundary.get("wraps"))
                   if component_id in self.components]
        if not members:
            return None
        min_x = min(member["x"] for member in members)
        min_y = min(member["y"] for member in members)
        max_x = max(member["x"] + member["width"] for member in members)
        max_y = max(member["y"] + member["height"] for member in members)
        pad = boundary.get("pad", LAYOUT["boundaryPad"])
        top_pad = max(pad, LAYOUT["boundaryLabelBaseline"] + LAYOUT["boundaryLabelClearance"])
        return {**boundary, "x": min_x - pad, "y": min_y - top_pad,
                "width": max_x - min_x + pad * 2,
                "height": max_y - min_y + top_pad + LAYOUT["boundaryExtraBottom"],
                "memberTop": min_y}

    @staticmethod
    def _rect_contains(outer, inner):
        e = 1e-9
        return (outer["x"] <= inner["x"] + e and outer["y"] <= inner["y"] + e
                and outer["x"] + outer["width"] + e >= inner["x"] + inner["width"]
                and outer["y"] + outer["height"] + e >= inner["y"] + inner["height"])

    def _measure_boundary_title(self, boundary, minimum_font_size):
        available = max(0, boundary["width"] - LAYOUT["boundaryLabelFrameInset"] * 2)
        units = text_units(boundary.get("label"))
        fitted = (available - 10) / (units * 0.6) if units > 0 else LAYOUT["boundaryLabelFontPreferred"]
        preferred = max(LAYOUT["boundaryLabelFontPreferred"], minimum_font_size)
        font = max(minimum_font_size, min(preferred, fitted))
        desired = _boundary_label_width(boundary.get("label", ""), font)
        height = max(LAYOUT["boundaryLabelMaskHeight"], math.ceil(font + 7))
        return {"x": boundary["x"] + LAYOUT["boundaryLabelFrameInset"],
                "y": boundary["memberTop"] - LAYOUT["boundaryLabelClearance"] - height,
                "width": min(available, desired), "height": height,
                "fontSize": font, "minimumFontSize": minimum_font_size,
                "baselineOffset": font + 4, "availableWidth": available,
                "minimumWidth": _boundary_label_width(boundary.get("label", ""), minimum_font_size)}

    def _layout_boundary_titles(self, raw, minimum_font_size):
        placed_titles, measured = [], {}
        ordered = sorted(enumerate(raw), key=lambda entry: (entry[1]["width"] * entry[1]["height"], entry[0]))
        for index, boundary in ordered:
            boundary = dict(boundary)
            if self.enforces_boundary_title_composition:
                required = _boundary_label_width(boundary.get("label", ""), minimum_font_size) + LAYOUT["boundaryLabelFrameInset"] * 2
                extra = max(0, required - boundary["width"])
                if extra:
                    boundary["x"] -= extra / 2
                    boundary["width"] += extra
            title = self._measure_boundary_title(boundary, minimum_font_size)
            guard = 0
            while guard < len(raw) + len(self.components) + 1:
                guard += 1
                blockers = [candidate for candidate in [*placed_titles, *self.components.values()]
                            if title["x"] < candidate["x"] + candidate["width"]
                            and title["x"] + title["width"] > candidate["x"]
                            and rects_overlap(title, candidate)]
                if not blockers:
                    break
                title["y"] = min(blocker["y"] - LAYOUT["boundaryLabelRailGap"] - title["height"]
                                 for blocker in blockers)
            placed_titles.append(title)
            measured[index] = (boundary, title)
        resolved = []
        for index, boundary in enumerate(raw):
            adapted, title = measured[index]
            bottom = adapted["y"] + adapted["height"]
            y = min(adapted["y"], title["y"] - LAYOUT["boundaryLabelFrameInset"]) if self.enforces_boundary_title_composition else adapted["y"]
            resolved.append({**adapted, "y": y, "height": bottom - y, "title": title})
        return resolved

    def _auto_view_box(self, candidate_boundaries, extra_rects=()):
        max_x = max_y = 0
        for rects in (self.components.values(), candidate_boundaries, extra_rects):
            for rect in rects:
                max_x = max(max_x, rect["x"] + rect["width"])
                max_y = max(max_y, rect["y"] + rect["height"])
        width = math.ceil(max_x + LAYOUT["margin"])
        footprint = legend_footprint(self.legend_entries, max(1, width - LAYOUT["margin"] * 2))
        if footprint["minWidth"] > width - LAYOUT["margin"] * 2:
            width = math.ceil(footprint["minWidth"] + LAYOUT["margin"] * 2)
            footprint = legend_footprint(self.legend_entries, width - LAYOUT["margin"] * 2)
        return [width, math.ceil(max_y + LAYOUT["margin"] + LAYOUT["legendH"] + footprint["extraHeight"])]

    def _resolved_view_box_width(self, candidate_boundaries):
        authored = self.meta.get("viewBox")
        if isinstance(authored, list) and authored and isinstance(authored[0], (int, float)):
            return authored[0]
        return self._auto_view_box(candidate_boundaries, self.connection_geometry)[0]

    def _resolve_boundary_titles(self):
        if not self.enforces_boundary_title_composition or not self.raw_boundaries:
            return self._layout_boundary_titles(self.raw_boundaries, LAYOUT["boundaryLabelFontMinimum"]), None
        maximum_iterations, candidates = 32, self.raw_boundaries
        for _ in range(maximum_iterations):
            width = self._resolved_view_box_width(candidates)
            minimum = max(LAYOUT["boundaryLabelFontMinimum"], _minimum_readable_source_text_px(width) + 1e-6)
            next_boundaries = self._layout_boundary_titles(self.raw_boundaries, minimum)
            final_width = self._resolved_view_box_width(next_boundaries)
            final_minimum = max(LAYOUT["boundaryLabelFontMinimum"], _minimum_readable_source_text_px(final_width))
            if minimum >= final_minimum:
                return next_boundaries, None
            candidates = next_boundaries
        final_width = self._resolved_view_box_width(candidates)
        message = (f'[composition/desktop-readability] Boundary title layout did not converge after {maximum_iterations} iterations for the final {final_width}px viewBox — shorten boundary labels, provide a wider authored viewBox, or move wrapped components closer to the left edge.')
        return candidates, message

    def _connection_label_box_at(self, connection, point):
        lx, ly = point
        width = max(30, text_units(connection.get("label")) * 4.8 + 10)
        return {"x": lx - width / 2, "y": ly - 10, "width": width, "height": 14, "lx": lx, "ly": ly}

    def _connection_label_box(self, connection):
        if not connection.get("label"):
            return None
        point = self.resolved_label_points.get(id(connection)) or label_point(connection, self.path_for(connection)["points"])
        return self._connection_label_box_at(connection, point)

    def _connection_label_rects(self):
        result = []
        for index, connection in enumerate(self.connections):
            if connection.get("from") not in self.components or connection.get("to") not in self.components:
                continue
            box = self._connection_label_box(connection)
            if box:
                result.append({"relation": connection, "relationIndex": index,
                               "label": connection["label"], **box})
        return result

    def _reserved_connection_label(self, connection, points, context):
        if not connection.get("label"):
            return None
        label = {"relation": connection, "label": connection["label"],
                 **self._connection_label_box_at(connection, label_point(connection, points))}
        return reserved_label_rect(label=label, points=points,
            routes=[{"relationIndex": i, "points": route} for i, route in enumerate(context["routes"])],
            labels=[{"relation": connection, "relationIndex": -2, **rect} for rect in context["labels"]],
            components=list(self.components.values()), view_box=(math.inf, math.inf))

    def path_for(self, connection):
        return self.router.path_for(connection)

    def connection_context(self, component):
        scopes = sorted((boundary for boundary in self.boundaries if component.get("id") in as_array(boundary.get("wraps"))),
                        key=lambda boundary: -(boundary["width"] * boundary["height"]))
        labels = [boundary.get("label", "") for boundary in scopes]
        return " › ".join(labels) if labels else translate(self.meta, "node.context.architecture")

    def _validate(self):
        problems, diagnostics = [], []
        if self.boundary_readability_problem:
            problems.append(self.boundary_readability_problem)
        nested = self.meta.get("engineering_profile") == "deployment-ownership"
        if len(self.components) != len(self.component_items):
            problems.append("Component ids must be unique.")
        if self.grid:
            validate_grid_placement(self.architecture, self.grid, problems)
        else:
            for component in self.component_items:
                if not (isinstance(component.get("pos"), list) and len(component["pos"]) == 2):
                    problems.append(f'Component "{component.get("id")}" must include pos [x, y] when layout.mode is omitted (free placement).')
        for component in self.components.values():
            if not is_finite_point(component.get("x"), component.get("y"), component.get("width"), component.get("height")):
                problems.append(f'Component "{component.get("id")}" has non-finite pos/size — pos and size must be [number, number].')
                continue
            if component["width"] <= 0 or component["height"] <= 0:
                problems.append(f'Component "{component.get("id")}" has invalid size {component["width"]}x{component["height"]} — width and height must be greater than 0.')
                continue
            if component["x"] < 0 or component["y"] < 0 or component["x"] + component["width"] > self.view_box[0] or component["y"] + component["height"] > self.view_box[1]:
                problems.append(f'Component "{component.get("id")}" falls outside the viewBox {self.view_box[0]}x{self.view_box[1]} — adjust pos/size or set a larger meta.viewBox.')
            estimate = text_units(component.get("label")) * 6.6
            if estimate > component["width"] + 8:
                problems.append(f'Label "{component.get("label")}" (~{math.floor(estimate + 0.5)}px) is wider than component "{component.get("id")}" ({number(component["width"])}px) — shorten the label or widen size.')
            mark = resolve_brand(component.get("brand"))
            issue = brand_top_rail_problem(component, component["width"], 8, mark, "Component")
            if issue:
                problems.append(issue)
            available = available_node_text_width(component["width"])
            for field, value, minimum in (("Sublabel", component.get("sublabel"), COMPONENT_TEXT_FIT["sublabelMinimum"]),
                                          ("Tag", component.get("tag"), COMPONENT_TEXT_FIT["tagMinimum"])):
                if value and minimum_node_text_width(value, minimum) > available:
                    needed = math.ceil(minimum_node_text_width(value, minimum))
                    problems.append(f'{field} "{value}" needs ~{needed}px at the {minimum}px legible minimum, but component "{component.get("id")}" provides {available}px — shorten the {field.lower()} or widen size.')
        component_list = list(self.components.values())
        for i, left in enumerate(component_list):
            for right in component_list[i + 1:]:
                if rects_overlap(left, right, 8):
                    problems.append(f'Components "{left.get("id")}" and "{right.get("id")}" are less than 8px apart — move one or shrink its size.')
        for boundary in self.boundary_items:
            for component_id in as_array(boundary.get("wraps")):
                if component_id not in self.components:
                    problems.append(f'Boundary "{boundary.get("label")}" wraps unknown component "{component_id}".')
        view_rect = {"x": 0, "y": 0, "width": self.view_box[0], "height": self.view_box[1]}
        for boundary in self.boundaries:
            if not self.enforces_boundary_title_composition:
                continue
            title = boundary["title"]
            if title["minimumWidth"] > title["availableWidth"]:
                problems.append(f'Boundary label "{boundary["label"]}" needs ~{math.ceil(title["minimumWidth"])}px to fit at the {title["minimumFontSize"]:.2f}px desktop-readable source minimum, but its frame provides {math.floor(title["availableWidth"])}px — shorten the boundary label, increase pad, or widen the wrapped component layout.')
            if not self._rect_contains(boundary, title):
                problems.append(f'Boundary label "{boundary["label"]}" extends outside its final frame — shorten the label or increase boundary pad.')
            if not self._rect_contains(view_rect, title):
                problems.append(f'Boundary label "{boundary["label"]}" extends outside the viewBox — move wrapped components away from the canvas edge, shorten the label, or increase the viewBox.')
            for component in self.components.values():
                if rects_overlap(title, component):
                    problems.append(f'Boundary label "{boundary["label"]}" overlaps component "{component["id"]}" — move the component, increase boundary title space, or shorten the label.')
        for i, left in enumerate(self.boundaries):
            left_members = set(as_array(left.get("wraps")))
            for right in self.boundaries[i + 1:]:
                right_members = set(as_array(right.get("wraps")))
                shared = left_members & right_members
                if self.enforces_boundary_title_composition and rects_overlap(left["title"], right["title"]):
                    problems.append(f'Boundary labels "{left["label"]}" and "{right["label"]}" overlap — shorten a label or increase boundary title space.')
                if not nested:
                    continue
                left_nested, right_nested = left_members <= right_members, right_members <= left_members
                if shared and not left_nested and not right_nested:
                    problems.append(f'Boundary "{left["label"]}" crosses boundary "{right["label"]}" because their memberships partially overlap — keep one boundary fully nested by removing outside members, or split the boundary.')
                    continue
                if not rects_overlap(left, right):
                    continue
                left_contains, right_contains = self._rect_contains(left, right), self._rect_contains(right, left)
                if not left_contains and not right_contains:
                    problems.append(f'Boundary "{left["label"]}" and boundary "{right["label"]}" final frames partially overlap — adjust wraps, pad, or component positions so the frames are disjoint or one fully contains the other.')
                elif not shared:
                    problems.append(f'Boundary "{left["label"]}" and boundary "{right["label"]}" final frames overlap even though their memberships are disjoint — adjust pad or component positions so the frames are disjoint, or make wraps express the intended nesting.')
                elif not ((left_nested and right_contains) or (right_nested and left_contains)):
                    problems.append(f'Boundary "{left["label"]}" and boundary "{right["label"]}" final frame containment contradicts their wraps membership — reduce the inner boundary pad, move its components, or correct wraps so geometry and nesting agree.')
        for boundary in self.boundaries:
            if boundary["x"] < 0 or boundary["y"] < 0 or boundary["x"] + boundary["width"] > self.view_box[0] or boundary["y"] + boundary["height"] > self.view_box[1]:
                overflow = {"left": max(0, -boundary["x"]), "top": max(0, -boundary["y"]),
                            "right": max(0, boundary["x"] + boundary["width"] - self.view_box[0]),
                            "bottom": max(0, boundary["y"] + boundary["height"] - self.view_box[1])}
                sides = ", ".join(f"{side} by {math.ceil(pixels)}px" for side, pixels in overflow.items() if pixels > 0)
                fixes = []
                if overflow["left"] or overflow["top"]:
                    fixes.append(f'move the wrapped components right by at least {math.ceil(overflow["left"])}px and down by at least {math.ceil(overflow["top"])}px, then revalidate connected routes and the opposite canvas sides; enlarging meta.viewBox cannot fix left/top overflow')
                if overflow["right"] or overflow["bottom"]:
                    fixes.append(f'increase meta.viewBox to at least [{math.ceil(max(self.view_box[0], boundary["x"] + boundary["width"]))}, {math.ceil(max(self.view_box[1], boundary["y"] + boundary["height"]))}] for right/bottom overflow, or move the wrapped components inward; revalidate desktop readability')
                message = f'Boundary "{boundary["label"]}" extends outside the viewBox ({sides}) — preserve wraps membership and repair the measured canvas side.'
                diagnostics.append(diagnostic("layout/boundary-out-of-bounds", message,
                    {"diagramType": "architecture", "boundary": {key: boundary.get(key) for key in ("kind", "label", "wraps")}},
                    {"bounds": {key: boundary[key] for key in ("x", "y", "width", "height")},
                     "viewBox": list(self.view_box), "overflowPx": overflow}, fixes))
                problems.append(message)
        for connection in self.connections:
            source_id, target_id = connection.get("from"), connection.get("to")
            if source_id not in self.components:
                problems.append(f'Connection "{connection.get("label") or source_id}" references unknown source "{source_id}".')
            if target_id not in self.components:
                problems.append(f'Connection "{connection.get("label") or f"{source_id}->{target_id}"}" references unknown target "{target_id}".')
            if source_id not in self.components or target_id not in self.components:
                continue
            points = self.path_for(connection)["points"]
            outside = [[x, y] for x, y in points if x < 0 or y < 0 or x > self.view_box[0] or y > self.view_box[1]]
            if self.meta.get("quality_profile") == "showcase" and outside:
                message = f'Connection "{connection.get("id") or f"{source_id}->{target_id}"}" extends outside the viewBox — move the measured outside route points inward or enlarge an authored viewBox for right/bottom overflow.'
                diagnostics.append(diagnostic("layout/route-out-of-bounds", message,
                    {"diagramType": "architecture", "collection": "connections", "index": self.connections.index(connection),
                     **({"id": connection["id"]} if connection.get("id") else {}), "from": source_id, "to": target_id},
                    {"viewBox": list(self.view_box), "outsidePoints": outside, "points": points},
                    ["move negative route coordinates inside the canvas; for right/bottom overflow, enlarge meta.viewBox or reroute inward; preserve endpoints, direction and labels, then revalidate"]))
                problems.append(message)
            distance = math.hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])
            if distance < 24 and source_id == target_id:
                message = f'Self-loop "{connection.get("id") or connection.get("label") or source_id}" on component "{source_id}" has its two ports only {math.floor(distance + 0.5)}px apart (minimum 24px) — remove fromSide/toSide so the renderer can choose the loop\'s sides, or set fromSide and toSide to different sides.'
                diagnostics.append(diagnostic("layout/self-loop-ports", message,
                    {"diagramType": "architecture", "collection": "connections", "index": self.connections.index(connection),
                     **({"id": connection["id"]} if connection.get("id") else {}), "from": source_id, "to": target_id},
                    {"distancePx": math.floor(distance + 0.5), "minimumPx": 24,
                     "fromSide": self.router.connection_endpoint_side(connection, "source"),
                     "toSide": self.router.connection_endpoint_side(connection, "target"), "points": points},
                    ["remove fromSide/toSide from the self-loop", "set fromSide and toSide to different sides of the component"]))
                problems.append(message)
            elif distance < 24:
                problems.append(f'Connection "{connection.get("label") or f"{source_id}->{target_id}"}" is too short ({math.floor(distance + 0.5)}px; minimum 24px) — place its components farther apart.')
        known = set(self.components)
        side_for = lambda connection, endpoint: self.router.connection_endpoint_side(connection, endpoint)
        details = []
        details.extend(clean_endpoint_side_problems(self.connections, known, self.path_for, side_for,
            "keep automatic routing so the renderer can use a side-aware bridge, or set truthful fromSide/toSide with perpendicular via segments",
            "architecture", "connections"))
        details.extend(clean_flow_problems(self.connections, self.components.values(), self.path_for,
            "adjust fromSide/toSide, set route/via, or move the component",
            diagram_type="architecture", relation_collection="connections", obstacle_kind="component"))
        automatic = lambda connection: not self.router.has_authored_route_geometry(connection)
        details.extend(clean_crossing_problems(self.connections, known, self.path_for, self.gate_profile,
            "adjust route/via or fromSide/toSide so the connections use separate corridors",
            diagram_type="architecture", relation_collection="connections",
            crossing_resolved=lambda left, right, _hit: automatic(left) and automatic(right)))
        details.extend(clean_ambiguous_corridor_problems(self.connections, known, self.path_for, self.gate_profile,
            "adjust route/via or fromSide/toSide so distinct connections do not visually merge",
            diagram_type="architecture", relation_collection="connections",
            include_shared_endpoints=lambda left, right: automatic(left) and not left.get("labelAt") and automatic(right) and not right.get("labelAt")))
        if self.gate_profile == "showcase":
            routed = [{"relation": connection, "relationIndex": index, "points": self.path_for(connection)["points"]}
                      for index, connection in enumerate(self.connections)
                      if connection.get("from") in self.components and connection.get("to") in self.components
                      and automatic(connection) and not connection.get("labelAt")]
            for hit in collect_arrowhead_collisions(routed):
                left, right = hit["left"]["relation"], hit["right"]["relation"]
                message = f'[composition/arrowhead-collision] automatic connections "{left.get("id") or left.get("from")}" and "{right.get("id") or right.get("from")}" into "{left.get("to")}" have arrowheads {hit["distance"]}px apart (minimum {hit["minimum"]}px) — enlarge or reposition the destination, or choose separate toSide ports.'
                details.append(diagnostic("composition/arrowhead-collision", message,
                    {"diagramType": "architecture", "collection": "connections", "id": left.get("id"), "from": left.get("from"), "to": left.get("to")},
                    {"otherId": right.get("id"), "distancePx": hit["distance"], "minimumPx": hit["minimum"],
                     "endpoints": [hit["left"]["tip"], hit["right"]["tip"]]},
                    ["enlarge or reposition the destination", "choose separate toSide ports"]))
        details.extend(clean_border_run_problems(self.connections, known, self.composition_frames, self.path_for,
            self.gate_profile,
            "adjust route/via or fromSide/toSide so the connection crosses the boundary perpendicularly instead of following its border",
            diagram_type="architecture", relation_collection="connections"))
        details.extend(clean_route_rhythm_problems(self.connections, known, self.path_for, self.gate_profile,
            "move route/via points into a wider corridor or move the component so every turn has room to read",
            diagram_type="architecture", relation_collection="connections"))
        details.extend(clean_route_detour_problems(
            relations=self.connections, obstacles=self.components.values(),
            contentRects=[*self.components.values(), *self.boundaries], endpointIds=known,
            pathFor=self.path_for, fromSideFor=lambda c: self.router.connection_endpoint_side(c, "source"),
            toSideFor=lambda c: self.router.connection_endpoint_side(c, "target"),
            diagramType="architecture", relationCollection="connections", profile=self.gate_profile))
        for rect in self.connection_labels:
            blocked = [component for component in self.components.values() if rects_overlap(rect, component, -2)]
            pinned = any(key in rect["relation"] for key in ("labelAt", "labelDx", "labelDy", "labelSegment"))
            points = self.path_for(rect["relation"])["points"]
            gap = abs(points[1][0] - points[0][0]) if len(points) == 2 and abs(points[1][1] - points[0][1]) < 0.0001 else None
            required = math.ceil(rect["width"] + 16)
            if self.meta.get("quality_profile") == "showcase" and not pinned and blocked and gap is not None and gap < required:
                message = f'Label "{rect["label"]}" has only {math.floor(gap + 0.5)}px between "{rect["relation"].get("from")}" and "{rect["relation"].get("to")}"; it needs at least {required}px to stay beside its route — increase that clear gap or place the connected nodes on another readable row, preserving the label.'
                details.append(diagnostic("composition/label-gap", message,
                    {"diagramType": "architecture", "collection": "connections", "id": rect["relation"].get("id"),
                     "from": rect["relation"].get("from"), "to": rect["relation"].get("to")},
                    {"clearGapPx": gap, "minimumGapPx": required, "labelWidthPx": rect["width"],
                     "obstacles": [component["id"] for component in blocked]},
                    [f"increase the clear gap between the connected nodes to at least {required}px",
                     "reposition the connected nodes together while preserving the full relationship label"]))
            else:
                for component in blocked:
                    details.append(diagnostic("layout/constraint",
                        f'Label "{rect["label"]}" overlaps component "{component["id"]}" — adjust labelDx/labelDy/labelSegment or set labelAt.',
                        {"diagramType": "architecture", "collection": "connections", "index": rect["relationIndex"]}))
            if self.enforces_boundary_title_composition:
                for boundary in self.boundaries:
                    if rects_overlap(boundary["title"], rect):
                        details.append(diagnostic("layout/constraint",
                            f'Boundary label "{boundary["label"]}" overlaps connection label "{rect["label"]}" — move the boundary title rail by adjusting wrapped component positions, or move the connection label with labelAt/labelDx/labelDy/labelSegment.',
                            {"diagramType": "architecture", "collection": "connections", "index": rect["relationIndex"]}))
        details.extend(clean_label_route_clearance_problems(self.connections, self.connection_labels, known,
            self.path_for, self.gate_profile,
            "adjust labelAt, labelDx, labelDy, or labelSegment; otherwise adjust the other connection route/via/channelX/channelY",
            diagram_type="architecture", relation_collection="connections"))
        details.extend(clean_label_canvas_problems(self.connection_labels, self.view_box, self.gate_profile,
            "adjust labelAt, labelDx, labelDy, or labelSegment; otherwise enlarge meta.viewBox",
            diagram_type="architecture", relation_collection="connections"))
        errors = [entry for entry in details if entry.get("severity") == "error"]
        for entry in details:
            if entry.get("severity") == "warning":
                record_diagnostic(entry)
        self.validation_diagnostics = diagnostics + details
        if problems or errors:
            errors.extend(diagnostics)
            specific = {entry["message"]: entry for entry in [*diagnostics, *details]}
            layout_details = [specific.get(message) or diagnostic(
                "layout/constraint", message, {"diagramType": "architecture"}) for message in problems]
            raise DiagnosticError("Architecture layout validation failed:\n- " + "\n- ".join(
                problems + [entry["message"] for entry in errors if entry not in diagnostics]), layout_details + errors)

    def _render_boundary_frame(self, boundary, index):
        cls = "c-security-group" if boundary.get("kind") == "security-group" else "c-region"
        radius = 8 if boundary.get("kind") == "security-group" else 12
        return (f'        <rect data-graph-role="structural-frame" data-composition-frame-kind="{esc(boundary.get("kind") or "boundary")}" data-composition-frame-id="{index}" data-composition-frame-label="{esc(boundary.get("label", ""))}" x="{number(boundary["x"])}" y="{number(boundary["y"])}" width="{number(boundary["width"])}" height="{number(boundary["height"])}" rx="{radius}" class="{cls}" stroke-width="1"/>')

    def _render_boundary_label(self, boundary, index):
        title = boundary["title"]
        label_cls = "t-security" if boundary.get("kind") == "security-group" else "t-cloud"
        return (f'        <g data-graph-role="structural-frame-label" data-composition-frame-id="{index}" data-composition-frame-kind="{esc(boundary.get("kind") or "boundary")}" data-composition-frame-label="{esc(boundary.get("label", ""))}">\n'
                f'          <rect data-graph-role="structural-frame-label-mask" x="{number(title["x"])}" y="{number(title["y"])}" width="{number(title["width"])}" height="{number(title["height"])}" rx="3" class="c-mask"/>\n'
                f'          <text data-boundary-label="" x="{number(title["x"] + 4)}" y="{number(title["y"] + title["baselineOffset"])}" class="{label_cls}" font-size="{number(title["fontSize"])}" font-weight="600">{esc(boundary.get("label", ""))}</text>\n'
                '        </g>')

    def _render_connection_path(self, connection, index):
        cls, marker = ARROW_CLASS.get(connection.get("variant") or "default", ARROW_CLASS["default"])
        routed = self.path_for(connection)
        width = connection.get("width") or (1.8 if connection.get("variant") == "emphasis" else 1.5)
        automatic = not self.router.has_authored_route_geometry(connection)
        underlay = (f'          <path data-graph-role="automatic-crossover-underlay" d="{routed["d"]}" fill="none" stroke="var(--mask)" stroke-width="{number(width + 4)}" stroke-linecap="round" stroke-linejoin="round" pointer-events="none"/>\n' if automatic else "")
        crossover = ' data-composition-crossover="halo"' + ('' if connection.get("labelAt") else ' data-composition-independent="true"') if automatic else ""
        edge = (f'        <path {focus_edge_attrs(connection, index)} data-composition-points="{route_points_value(routed["points"])}"{crossover}'
                f'{authored_straight_route_attrs(connection, routed["points"])} d="{routed["d"]}" class="{cls}"{animate_attr(self.meta, "edge", index)} stroke-width="{number(width)}" marker-end="url(#{marker})"/>')
        return f'        <g data-graph-role="automatic-crossover" style="--step:{index}">\n{underlay}{edge.replace("        ", "          ", 1)}\n        </g>' if automatic else edge

    def _render_component(self, component):
        fill, accent = COMPONENT_FILL.get(component.get("type"), "c-external"), COMPONENT_TEXT.get(component.get("type"), "t-muted")
        cx = component["cx"]
        has_sub = component.get("sublabel") not in (None, "")
        label_y = component["y"] + component["height"] / 2 - 2 if has_sub else component["y"] + component["height"] / 2 + 4
        sub = (f'\n        <text data-detail="context" x="{number(cx)}" y="{number(component["y"] + component["height"] / 2 + 14)}" class="t-muted" font-size="{number(fitted_node_font_size(component.get("sublabel"), component["width"], 9, 6))}" text-anchor="middle">{esc(component["sublabel"])}</text>' if has_sub else "")
        tag = (f'\n        <text data-detail="fine" x="{number(cx)}" y="{number(component["y"] + component["height"] - 8)}" class="{accent}" font-size="{number(fitted_node_font_size(component.get("tag"), component["width"], 7, 6))}" text-anchor="middle">{esc(component["tag"])}</text>' if component.get("tag") else "")
        brand = resolve_brand(component.get("brand"))
        brand_svg = render_brand_mark(brand, component["x"] + component["width"] - 22, component["y"] + 6)
        label_size = fitted_node_font_size(component.get("label"), brand_label_fit_width(component, component["width"], brand), 11, 8)
        beacon_left = component["x"] + component["width"] - 42 - (22 if brand else 0)
        label_right = cx + text_units(component.get("label", "")) * label_size * 0.3
        if (component.get("sources") and label_y - label_size < component["y"] + 17.5
                and label_y > component["y"] + 4.5 and label_right > beacon_left):
            raise DiagnosticError("Component does not reserve room for its source badge.", [diagnostic(
                "source-evidence/node-footprint", "Component does not reserve room for its source badge.",
                {"nodeId": component.get("id")}, supported_fixes=["increase authored component height or width to reserve a separate source badge rail"] )])
        passport = {"kind": component.get("type"), "sublabel": component.get("sublabel"), "tag": component.get("tag"),
                    "context": self.connection_context(component), **brand_metadata(brand)}
        brand_markup = f"\n          {brand_svg}" if brand_svg else ""
        detail_anchor = ' data-detail-anchor=""' if has_sub else ""
        return (f'        <g {focus_node_attrs(component, passport, self.meta)}>\n'
                f'          {focus_node_title(component.get("label", ""), passport)}\n'
                f'          <rect x="{number(component["x"])}" y="{number(component["y"])}" width="{number(component["width"])}" height="{number(component["height"])}" rx="6" class="c-mask"/>\n'
                f'          <rect x="{number(component["x"])}" y="{number(component["y"])}" width="{number(component["width"])}" height="{number(component["height"])}" rx="6" class="{fill}"{animate_attr(self.meta, "node", self.component_steps.get(component.get("id")))} stroke-width="1.5"/>\n'
                f'          {render_semantic_sigil(component.get("type"), component["x"] + 6, component["y"] + 6, component.get("icon"))}{brand_markup}\n'
                f'          <text data-node-label=""{detail_anchor} x="{number(cx)}" y="{number(label_y)}" class="t-primary" font-size="{number(label_size)}" font-weight="600" text-anchor="middle">{esc(component.get("label", ""))}</text>{sub}{tag}\n'
                '        </g>')

    def _render_connection_label(self, connection, index):
        box = self._connection_label_box(connection)
        if not box:
            return ""
        attrs = focus_edge_attrs(connection, index)
        return (f'        <g data-detail="context" {attrs}>\n'
                f'          <rect x="{number(box["x"])}" y="{number(box["y"])}" width="{number(box["width"])}" height="{number(box["height"])}" rx="3" class="c-mask"/>\n'
                f'          <text x="{number(box["lx"])}" y="{number(box["ly"])}" class="{EDGE_ACCENT.get(connection.get("variant"), "t-edge-default")}" font-size="8" text-anchor="middle">{esc(connection.get("label", ""))}</text>\n'
                '        </g>')

    def _render_legend(self):
        obstacles = relationship_legend_obstacles(
            self.connections,
            points_for=lambda connection, _index: self.path_for(connection)["points"],
            label_rect_for=lambda connection, _index: self._connection_label_box(connection))
        content_bottom = max([0] + [component["y"] + component["height"] for component in self.components.values()]
                             + [boundary["y"] + boundary["height"] for boundary in self.boundaries])
        return render_legend(self.legend_entries,
            {"x": LAYOUT["margin"], "baselineY": self.view_box[1] - 16,
             "width": self.view_box[0] - LAYOUT["margin"] * 2,
             "minTitleY": content_bottom + 8, "obstacles": obstacles,
             "unfit": "hide" if "legend" not in self.meta else "error", "diagramType": "architecture"},
            lambda entry: (f'<rect x="{number(entry["x"])}" y="{number(entry["baseline"] - 9)}" width="16" height="10" rx="2.5" class="{COMPONENT_FILL.get(entry["kind"], "c-external")}" stroke-width="1"/>'),
            self.meta, obstacles)

    def render(self):
        self._validate()
        reader_fit = (' data-diagram-type="architecture" data-reader-fit="authored-height"'
                      if self.meta.get("viewBox") else ' data-reader-fit="intrinsic-height"')
        min_text = '' if self.meta.get("viewBox") else ' data-reader-min-text="7.5"'
        primary_text = '' if self.meta.get("viewBox") else ' data-reader-primary-text="14"'
        boundaries = "\n\n".join(self._render_boundary_frame(item, index) for index, item in enumerate(self.boundaries))
        components = "\n\n".join(self._render_component(item) for item in self.components.values())
        labels = "\n".join(self._render_connection_label(item, index) for index, item in enumerate(self.connections))
        boundary_labels = "\n\n".join(self._render_boundary_label(item, index) for index, item in enumerate(self.boundaries))
        return (f'      <svg viewBox="0 0 {number(self.view_box[0])} {number(self.view_box[1])}" {svg_root_attrs(self.meta, self.quality)}{reader_fit}{min_text}{primary_text}>\n'
                f'{svg_accessible_text(self.meta, "architecture")}\n{render_definitions()}\n\n'
                '        <!-- Background Grid -->\n'
                '        <rect width="100%" height="100%" fill="url(#grid)" />\n\n'
                '        <!-- Boundaries (behind everything) -->\n' + boundaries
                + '\n\n        <!-- Connection paths (before components for correct z-order) -->\n'
                + "\n".join(self._render_connection_path(item, index) for index, item in enumerate(self.connections))
                + '\n\n        <!-- Components -->\n' + components
                + '\n\n        <!-- Connection labels -->\n' + labels
                + '\n\n        <!-- Boundary labels (foreground masks keep routes out of titles) -->\n'
                + boundary_labels + '\n\n        <!-- Legend -->\n' + self._render_legend() + '\n      </svg>')


def render_architecture(diagram, quality=None):
    """Render a validated Architecture diagram as an SVG string."""
    return ArchitectureRenderer(diagram, quality).render()
