"""Node text sizing and placement helpers shared by native renderers."""

import math
import re
import unicodedata

from .drawing import text_units

SEMANTIC_SIGIL_INSET = 6
SEMANTIC_SIGIL_SIZE = 11
SEMANTIC_SIGIL_FOOTPRINT = SEMANTIC_SIGIL_INSET + SEMANTIC_SIGIL_SIZE
SOURCE_BADGE_FOOTPRINT = 38


def fitted_node_font_size(text, width, preferred, minimum):
    units = max(1, text_units(text))
    available = max(1, width - 8)
    fitted = min(preferred, available / (units * 0.6))
    return max(minimum, math.floor(fitted * 10) / 10)


def minimum_node_text_width(text, minimum):
    return text_units(text) * minimum * 0.6


def available_node_text_width(width):
    return width - 8


def node_label_layout(width, height, rows, side="left", brand=False, source=False, step=""):
    """Place a node's label rows clear of the semantic icon and brand rail."""
    result = {"x": width / 2, "ys": [row["y"] for row in rows],
              "sigilY": SEMANTIC_SIGIL_INSET, "sigilSize": SEMANTIC_SIGIL_SIZE}
    label_width = minimum_node_text_width(rows[0]["text"], rows[0]["font"])
    step_end = ((23 if side == "left" else 10) + minimum_node_text_width(step, 8) + 3) if step else 0
    left = max(SEMANTIC_SIGIL_FOOTPRINT + 2 if side == "left" else 4, step_end)
    right = width - (26 if brand else SEMANTIC_SIGIL_FOOTPRINT + 2 if side == "right" else 4)
    if source:
        right -= SOURCE_BADGE_FOOTPRINT
    if result["x"] - label_width / 2 >= left and result["x"] + label_width / 2 <= right:
        return result
    if label_width <= right - left:
        result["x"] = min(math.floor((right - label_width / 2) * 10) / 10,
                           max(result["x"], math.ceil((left + label_width / 2) * 10) / 10))
        return result

    bottom = max(22 if brand else SEMANTIC_SIGIL_FOOTPRINT, 19 if source else 0)
    ys = []
    for row in rows:
        y = max(row["y"], math.ceil((bottom + 2 + row["font"] * 1.2) * 10) / 10)
        ys.append(y)
        bottom = y + row["font"] * 0.3
    if bottom <= height - 2:
        result["ys"] = ys
        return result

    if source:
        compact_bottom = max(22 if brand else SEMANTIC_SIGIL_FOOTPRINT, 19) + 1
        compact_ys = []
        for row in rows:
            y = math.ceil((compact_bottom + 1 + row["font"]) * 10) / 10
            compact_ys.append(y)
            compact_bottom = y + row["font"] * 0.3
        if compact_bottom <= height - 0.5:
            result["ys"] = compact_ys
            return result

    result["sigilY"] = 1
    result["sigilSize"] = max(1, min(SEMANTIC_SIGIL_SIZE,
                                    math.floor(rows[0]["y"] - rows[0]["font"] * 1.2 - 3)))
    return result


def grapheme_clusters(value):
    """Split Unicode text into the extended clusters used by stage wrapping.

    Python's standard library does not expose UAX #29 segmentation. These
    cases cover combining marks, selectors, emoji modifiers/ZWJ sequences,
    tag sequences, and paired regional indicators, preserving authored text.
    """
    chars = list(value)
    if not chars:
        return []
    result = []
    current = chars[0]
    regional_count = 1 if _is_regional(chars[0]) else 0
    join_next = False
    for char in chars[1:]:
        code = ord(char)
        category = unicodedata.category(char)
        extender = (category.startswith("M") or 0xFE00 <= code <= 0xFE0F
                    or 0xE0100 <= code <= 0xE01EF or 0x1F3FB <= code <= 0x1F3FF
                    or 0xE0020 <= code <= 0xE007F or code == 0x20E3)
        if extender or join_next or code == 0x200D:
            current += char
            join_next = code == 0x200D
            if not join_next and code != 0x200D:
                join_next = False
            continue
        if _is_regional(char) and regional_count == 1:
            current += char
            regional_count = 0
            continue
        result.append(current)
        current = char
        regional_count = 1 if _is_regional(char) else 0
        join_next = False
    result.append(current)
    return result


def _is_regional(char):
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def wrap_header(text, available, font):
    """Wrap a stage header at words, then graphemes, without dropping spaces."""
    if minimum_node_text_width(text, font) <= available:
        return [text]
    lines = []
    line = ""
    for word in re.findall(r"\s+|\S+", text, flags=re.UNICODE):
        if line and minimum_node_text_width(line + word, font) > available:
            lines.append(line)
            line = ""
        for cluster in grapheme_clusters(word):
            if line and minimum_node_text_width(line + cluster, font) > available:
                lines.append(line)
                line = ""
            line += cluster
    if line:
        lines.append(line)
    return lines
