"""Final native SVG checks, separate from authoring/layout validation."""

import math
import xml.etree.ElementTree as ET

from .common import DiagnosticError, diagnostic
from .desktop_readability import declared_wide_readability_budget
from .artifact_geometry import final_crossing_diagnostics


def check_svg(svg, quality=None, diagram_type="sequence"):
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as error:
        raise DiagnosticError("Rendered SVG is invalid XML", [diagnostic("artifact/single-svg", str(error))]) from error
    diagnostics = []
    if root.tag != "svg" or any(e.tag == "svg" for e in list(root.iter())[1:]):
        diagnostics.append(diagnostic("artifact/single-svg", "Expected one diagram SVG root."))
    if root.attrib.get("data-quality-profile") == "showcase":
        diagnostics.extend(final_crossing_diagnostics(svg, root.attrib.get("data-layout-contract") == "readable-v2"))
        _, _, width, height = map(float, root.attrib["viewBox"].split())
        entries = []
        def visit(element, groups):
            parents = groups + [element.attrib] if element.tag == "g" else groups
            if element.tag == "text":
                primary = "data-node-label" in element.attrib
                boundary = "data-boundary-label" in element.attrib
                context = element.attrib.get("data-detail") == "context" or any(p.get("data-detail") == "context" for p in parents)
                fine = element.attrib.get("data-detail") == "fine" or any(p.get("data-detail") == "fine" for p in parents)
                owned = any("data-node-id" in p or "data-edge-from" in p for p in parents)
                if (primary or boundary or context and owned) and not fine:
                    source = float(element.attrib.get("font-size", "nan"))
                    entries.append(source)
            for child in element:
                visit(child, parents)
        visit(root, [])
        available = 930
        requested = float(root.attrib.get("data-reader-min-text", "nan"))
        # Native output is always inserted into the canonical declared-wide Viewer.
        if root.attrib.get("data-reader-fit") == "intrinsic-height" and entries and all(math.isfinite(n) and n > 0 for n in entries):
            budget = declared_wide_readability_budget(width, height, min(entries), requested)
            if budget:
                available = budget["guaranteedSvgWidth"]
        scale = min(1, available / width)
        if entries:
            source = min(entries)
            if not math.isfinite(source) or source * scale < 6:
                diagnostics.append(diagnostic("composition/desktop-readability", "Final artifact failed composition/desktop-readability.",
                    {"check": "composition"}, {"sourceFontPx": source, "projectedFontPx": source * scale,
                    "minimumProjectedFontPx": 6}, ["use a narrower viewBox or larger semantic text"]))
    if diagnostics:
        # Node's final checker reports only the worst readability issue.
        raise DiagnosticError("Final artifact check failed.", diagnostics)
    return [{"name": "single_svg", "ok": True}, {"name": "svg_xml", "ok": True},
            {"name": diagram_type + "_layout", "ok": True}, {"name": "desktop_readability", "ok": True}]
