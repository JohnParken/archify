"""Measure native SVG paths as the canonical final crossing check does."""

import math
import re
import xml.etree.ElementTree as ET

from .common import diagnostic, js_round
from .composition import forward_collinear_analysis_segments
from .flow_geometry import proper_segment_intersection

TOKENS = re.compile(r"[MLHVQZmlhvqz]|[-+]?(?:\d*\.)?\d+(?:e[-+]?\d+)?")
COMMANDS = set("MLHVQZmlhvqz")
FIX = ("if authored via/route/channelX/channelY controls exist and are not required by the user, "
       "remove them to let the renderer re-plan; otherwise preserve that intent and adjust route/via "
       "or channel coordinates so unrelated relationships use separate corridors")


def path_segments(path):
    """Return sampled visible segments and exact straight primitives (Q: eight samples)."""
    tokens, index, command = TOKENS.findall(path), 0, ""
    current, origin, sampled, straight = [0, 0], None, [], []
    while index < len(tokens):
        if tokens[index] in COMMANDS:
            command, index = tokens[index], index + 1
        if not command:
            break
        upper, absolute = command.upper(), command.isupper()
        first = True
        count = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4}.get(upper)
        if upper == "Z":
            if origin is not None:
                sampled.append(origin)
                straight.append({"start": current, "end": origin})
                current = origin
            command = ""
            continue
        if count is None:
            return [], []
        progressed = False
        while index + count <= len(tokens) and tokens[index] not in COMMANDS:
            if any(token in COMMANDS for token in tokens[index:index + count]):
                return [], []
            values = list(map(float, tokens[index:index + count]))
            index += count
            progressed = True
            if not all(math.isfinite(value) for value in values):
                return [], []
            if upper in ("M", "L"):
                end = values if absolute else [current[0] + values[0], current[1] + values[1]]
                if upper == "L" or not first:
                    straight.append({"start": current, "end": end})
                current = end
                sampled.append(end)
                if origin is None:
                    origin = end
            elif upper in ("H", "V"):
                axis = 0 if upper == "H" else 1
                end = list(current)
                end[axis] = values[0] if absolute else current[axis] + values[0]
                straight.append({"start": current, "end": end})
                current = end
                sampled.append(end)
            else:
                control = values[:2] if absolute else [current[0] + values[0], current[1] + values[1]]
                end = values[2:] if absolute else [current[0] + values[2], current[1] + values[3]]
                cross = ((control[0] - current[0]) * (end[1] - current[1])
                         - (control[1] - current[1]) * (end[0] - current[0]))
                if abs(cross) <= 1e-9:
                    straight.append({"start": current, "end": end})
                for step in range(1, 9):
                    amount, remaining = step / 8, 1 - step / 8
                    sampled.append([remaining * remaining * current[axis] + 2 * remaining * amount * control[axis]
                                    + amount * amount * end[axis] for axis in (0, 1)])
                current = end
            first = False
        if not progressed:
            break
    return [{"start": a, "end": b} for a, b in zip(sampled, sampled[1:])], straight


def collect_svg_arrows(svg, workflow_v2=False):
    fragment = svg.split("<!-- Legend -->", 1)[0]
    arrows, previous, previous_end = [], None, 0
    for match in re.finditer(r"<(path|line)\b[^>]*>", fragment, re.I):
        raw = match.group()
        attrs = ET.fromstring(raw if raw.endswith("/>") else raw[:-1] + "/>").attrib
        preceding = previous if previous and not fragment[previous_end:match.start()].strip() else None
        previous = attrs if match[1].lower() == "path" and attrs.get("data-graph-role") == "automatic-crossover-underlay" else None
        previous_end = match.end()
        if not re.search(r"\ba-(?:default|emphasis|security|dashed)\b", attrs.get("class", "")) or "marker-end" not in attrs:
            continue
        if match[1].lower() == "line":
            segments = [{"start": [float(attrs.get("x1", "nan")), float(attrs.get("y1", "nan"))],
                         "end": [float(attrs.get("x2", "nan")), float(attrs.get("y2", "nan"))]}]
            straight = segments
        else:
            segments, straight = path_segments(attrs.get("d", ""))
        width, halo_width = float(attrs.get("stroke-width", "nan")), float((preceding or {}).get("stroke-width", "nan"))
        halo = bool(attrs.get("data-composition-crossover") == "halo" and preceding
                    and preceding.get("d") == attrs.get("d") and preceding.get("fill") == "none"
                    and preceding.get("stroke") == "var(--mask)" and preceding.get("pointer-events") == "none"
                    and math.isfinite(width) and math.isfinite(halo_width) and halo_width >= width + 3)
        if workflow_v2:
            points = [straight[0]["start"], *[segment["end"] for segment in straight]] if straight else []
            segments = forward_collinear_analysis_segments(points)
        arrows.append({"attrs": attrs, "index": len(arrows) + 1, "segments": segments, "halo": halo,
                       "independent": halo and attrs.get("data-composition-independent") == "true",
                       "automaticWorkflow": attrs.get("data-composition-routing") == "workflow-v2-auto",
                       "from": attrs.get("data-edge-from") or attrs.get("data-composition-edge-from"),
                       "to": attrs.get("data-edge-to") or attrs.get("data-composition-edge-to")})
    return arrows


def relationship_record(arrow):
    attrs = arrow["attrs"]
    key = attrs.get("data-edge-key")
    try:
        stable = float(key)
    except (ValueError, TypeError):
        stable = math.nan
    result = {"from": arrow["from"], "to": arrow["to"], "label": attrs.get("data-edge-label") or "",
              "collectionIndex": int(stable) if math.isfinite(stable) and stable.is_integer() and stable >= 0 else arrow["index"] - 1,
              "artifactIndex": arrow["index"]}
    identity = attrs.get("data-edge-id") or attrs.get("data-composition-edge-id")
    if identity:
        result["id"] = identity
    return result


def final_crossing_diagnostics(svg, workflow_v2=False):
    relationships = [a for a in collect_svg_arrows(svg, workflow_v2) if a["from"] and a["to"] and a["segments"]]
    result = []
    for index, left in enumerate(relationships):
        for right in relationships[index + 1:]:
            shared = bool({left["from"], left["to"]} & {right["from"], right["to"]})
            if shared and not (workflow_v2 or left["independent"] and right["independent"]
                               or left["automaticWorkflow"] and right["automaticWorkflow"]):
                continue
            if left["halo"] and right["halo"]:
                continue
            point = None
            for a in left["segments"]:
                for b in right["segments"]:
                    point = proper_segment_intersection(a["start"], a["end"], b["start"], b["end"], epsilon=1e-9)
                    if point is not None:
                        break
                if point is not None:
                    break
            if point is not None:
                result.append(diagnostic("composition/proper-crossing", "Final artifact failed composition/proper-crossing.",
                    {"relationship": relationship_record(left)},
                    {"otherRelationship": relationship_record(right), "point": [js_round(n * 10) / 10 for n in point]}, [FIX]))
    return result
