"""Legend resolution and SVG rendering shared by native diagram renderers."""

import math

from .common import DiagnosticError, diagnostic, esc, translate
from .drawing import text_units

DEFAULT_FONT_SIZE = 8
DEFAULT_ITEM_GAP = 22
DEFAULT_LINE_GAP = 22
DEFAULT_SWATCH_GAP = 8
TEXT_ADVANCE_EM = 0.62
INTERACTIVE_BADGE_ALLOWANCE = 21


def resolve_legend(config, catalog, present_kinds):
    mode = (config or {}).get("mode") or "auto"
    if mode == "hidden":
        return []
    present = set(present_kinds or ())
    overrides = (config or {}).get("entries") or {}
    entries = []
    for catalog_entry in catalog:
        item = dict(catalog_entry)
        kind = item["kind"]
        override = overrides.get(kind) or {}
        selected = mode == "all" or kind in present
        visible = override.get("visible") is True or (selected and override.get("visible") is not False)
        if not visible:
            continue
        item["label"] = override.get("label") or item.get("label", "")
        item["present"] = kind in present
        item["interactive"] = item.get("interactive") is not False and kind in present
        entries.append(item)
    return entries


def _entry_width(entry, font_size, swatch_gap):
    return math.ceil(entry.get("swatchWidth", 14) + entry.get("swatchGap", swatch_gap)
                     + text_units(entry.get("label", "")) * font_size * TEXT_ADVANCE_EM
                     + (INTERACTIVE_BADGE_ALLOWANCE if entry.get("interactive") else 0))


def legend_footprint(entries, width, font_size=DEFAULT_FONT_SIZE, item_gap=DEFAULT_ITEM_GAP,
                     line_gap=DEFAULT_LINE_GAP, swatch_gap=DEFAULT_SWATCH_GAP):
    if not entries:
        return {"measured": [], "rows": [], "rowCount": 0, "minWidth": 0, "extraHeight": 0}
    measured = [{**entry, "width": _entry_width(entry, font_size, swatch_gap)} for entry in entries]
    rows = [[]]
    cursor = 0
    for entry in measured:
        row = rows[-1]
        required = (item_gap if row else 0) + entry["width"]
        if row and cursor + required > width:
            rows.append([entry])
            cursor = entry["width"]
        else:
            row.append(entry)
            cursor += required
    return {"measured": measured, "rows": rows, "rowCount": len(rows),
            "minWidth": max(entry["width"] for entry in measured),
            "extraHeight": (len(rows) - 1) * line_gap}


def measure_legend(entries, layout, obstacles=()):
    if not entries:
        return {"entries": [], "rowCount": 0, "titleY": None}
    font_size = layout.get("fontSize", DEFAULT_FONT_SIZE)
    item_gap = layout.get("itemGap", DEFAULT_ITEM_GAP)
    line_gap = layout.get("lineGap", DEFAULT_LINE_GAP)
    swatch_gap = layout.get("swatchGap", DEFAULT_SWATCH_GAP)
    footprint = legend_footprint(entries, layout["width"], font_size, item_gap, line_gap, swatch_gap)
    too_wide = next((entry for entry in footprint["measured"] if entry["width"] > layout["width"]), None)
    if too_wide:
        if layout.get("unfit") == "hide":
            return None
        kind = too_wide["kind"]
        message = (f'[legend/label-too-wide] {layout.get("diagramType", "diagram")} legend label for "{kind}" '
                   f'needs {too_wide["width"]}px but only {layout["width"]}px is available.')
        raise DiagnosticError(message, [diagnostic(
            "legend/label-too-wide", message,
            {"diagramType": layout.get("diagramType", "diagram"), "path": f"/meta/legend/entries/{kind}/label"},
            {"kind": kind, "measuredWidthPx": too_wide["width"], "availableWidthPx": layout["width"]},
            ["shorten the legend label or use a wider viewBox"],
        )])

    title_y = layout["baselineY"] - footprint["extraHeight"] - 20
    legend_top_y = title_y - 10
    if legend_top_y < layout.get("minTitleY", 0):
        if layout.get("unfit") == "hide":
            return None
        message = (f'[legend/vertical-overflow] {layout.get("diagramType", "diagram")} legend needs '
                   f'{footprint["rowCount"]} rows, which would start at y={_number(legend_top_y)} above the '
                   f'available legend band at y={_number(layout.get("minTitleY", 0))}.')
        raise DiagnosticError(message, [diagnostic(
            "legend/vertical-overflow", message,
            {"diagramType": layout.get("diagramType", "diagram"), "path": "/meta/legend"},
            {"rowCount": footprint["rowCount"], "requiredTopY": legend_top_y,
             "availableTopY": layout.get("minTitleY", 0)},
            ["shorten legend labels, hide nonessential entries, or use a wider viewBox"],
        )])

    positioned = []
    for row_index, row in enumerate(footprint["rows"]):
        entry_x = layout["x"]
        baseline = layout["baselineY"] - (footprint["rowCount"] - row_index - 1) * line_gap
        for entry in row:
            positioned.append({**entry, "x": entry_x, "baseline": baseline, "row": row_index})
            entry_x += entry["width"] + item_gap

    # Dataflow passes no obstacles to the shared legend layout today, but keep
    # the reusable helper's collision contract available for later modes.
    if obstacles:
        from .geometry import rects_overlap, segment_intersects_rect
        legend_rects = [{"kind": "title", "x": layout["x"], "y": legend_top_y, "width": 48, "height": 14}]
        legend_rects.extend({"kind": entry["kind"], "x": entry["x"], "y": entry["baseline"] - 10,
                             "width": entry["width"], "height": 14} for entry in positioned)
        collision = next((box for box in legend_rects if any(
            segment_intersects_rect(obstacle["start"], obstacle["end"], box)
            if "start" in obstacle and "end" in obstacle else rects_overlap(obstacle, box)
            for obstacle in obstacles)), None)
        if collision:
            if layout.get("unfit") == "hide":
                return None
            kind = collision["kind"]
            message = (f'[legend/content-overlap] {layout.get("diagramType", "diagram")} legend entry '
                       f'"{kind}" overlaps authored relationship geometry.')
            raise DiagnosticError(message, [diagnostic(
                "legend/content-overlap", message,
                {"diagramType": layout.get("diagramType", "diagram"), "path": "/meta/legend"},
                {"legendKind": kind, "legendRect": collision},
                ["shorten or hide legend entries, use a wider viewBox, or move the authored relationship route/label out of the legend band"],
            )])
    return {"entries": positioned, "rowCount": footprint["rowCount"], "titleY": title_y,
            "fontSize": font_size}


def render_legend(entries, layout, render_swatch, locale, obstacles=()):
    if not entries:
        return ""
    measured = measure_legend(entries, layout, obstacles)
    if not measured:
        return ""
    has_interactive = any(entry.get("interactive") for entry in measured["entries"])
    rendered_font_size = measured["fontSize"] + (0.5 if measured["fontSize"] < 8 else 2)
    root_attrs = ' data-legend="" data-legend-bridge=""' if has_interactive else ' data-legend=""'
    translation_meta = locale if isinstance(locale, dict) else {"locale": locale}
    parts = [f'        <g{root_attrs}>',
             f'          <text x="{layout["x"]}" y="{_number(measured["titleY"])}" class="t-primary" '
             f'font-size="12" font-weight="650">{esc(translate(translation_meta, "legend.title"))}</text>']
    for entry in measured["entries"]:
        interactive = (f' data-legend-kind="{esc(entry["kind"])}" data-legend-label="{esc(entry["label"])}"'
                       if entry.get("interactive") else "")
        parts.append(f'          <g data-legend-semantic-kind="{esc(entry["kind"])}"{interactive} '
                     f'data-legend-x="{entry["x"]}" data-legend-baseline="{_number(entry["baseline"])}" '
                     f'data-legend-width="{entry["width"]}">')
        parts.append(f'            {render_swatch(entry)}')
        parts.append(f'            <text x="{_number(entry["x"] + entry.get("swatchWidth", 14) + entry.get("swatchGap", 8))}" '
                     f'y="{_number(entry["baseline"])}" class="t-muted" font-size="{_number(rendered_font_size)}" '
                     f'font-weight="500">{esc(entry["label"])}</text>')
        parts.append('          </g>')
    parts.append('        </g>')
    return "\n".join(parts)


def _number(value):
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return repr(float(value))
