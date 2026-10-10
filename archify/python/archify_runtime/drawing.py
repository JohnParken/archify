"""SVG drawing primitives shared by the Python renderer prototype."""

import math
import re
from urllib.parse import urlparse

from .common import esc, get_data, number, resolved_locale, translate


_FULLWIDTH_RANGES = (
    (0x1100, 0x115F), (0x231A, 0x231B), (0x2329, 0x232A),
    (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2630, 0x2637),
    (0x2648, 0x2653), (0x267F, 0x267F), (0x268A, 0x268F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB),
    (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26CE, 0x26CE),
    (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3),
    (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55), (0x2E80, 0xA4CF), (0xA960, 0xA97C),
    (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F), (0xFF01, 0xFF60), (0xFFE0, 0xFFE6),
    (0x16FE0, 0x18DFF), (0x1AFF0, 0x1AFFF), (0x1B000, 0x1B2FF),
    (0x1F000, 0x1FAFF), (0x20000, 0x3FFFD),
)
_VS_FIRST, _VS_LAST, _VS_EMOJI = 0xFE00, 0xFE0F, 0xFE0F


def text_units(value):
    chars = list("" if value is None else str(value))
    units = 0
    for index, char in enumerate(chars):
        codepoint = ord(char)
        if _VS_FIRST <= codepoint <= _VS_LAST:
            continue
        if index + 1 < len(chars) and ord(chars[index + 1]) == _VS_EMOJI:
            units += 2
        else:
            units += 2 if any(low <= codepoint <= high for low, high in _FULLWIDTH_RANGES) else 1
    return units


def render_definitions():
    return get_data("sigils")["definitions"]


def render_semantic_sigil(kind, x, y, icon=None, size=11):
    if icon == "none":
        return ""
    data = get_data("sigils")
    shapes = data["shapes"]
    selected = icon if icon is not None else kind
    normalized = selected if selected in shapes else "neutral"
    tone = data["tones"].get(kind, "external")
    scale = size / 16
    return (f'<g aria-hidden="true" data-semantic-sigil="{esc(normalized)}" class="semantic-sigil s-{esc(tone)}" '
            f'transform="translate({number(x)} {number(y)}) scale({number(scale)})">\n'
            f'            {shapes[normalized]}\n          </g>')


def fitted_node_font_size(value, width, preferred, minimum):
    units = max(1, text_units(value))
    available = max(1, width - 8)
    fitted = min(preferred, available / (units * 0.6))
    return max(minimum, math.floor(fitted * 10) / 10)


def minimum_node_text_width(value, minimum):
    return text_units(value) * minimum * 0.6


def available_node_text_width(width):
    return width - 8


def svg_root_attrs(meta, explicit_quality=None):
    animation = ' data-animation="trace"' if meta.get("animation") == "trace" else ""
    preset = f' data-preset="{esc(meta.get("visual_preset") or "classic")}"'
    engineering = (f' data-engineering-profile="{esc(meta["engineering_profile"])}"'
                   if meta.get("engineering_profile") else "")
    requested = explicit_quality or meta.get("quality_profile")
    quality = "showcase" if requested == "showcase" else "standard"
    advisory = "" if requested else ' data-quality-gates="advisory"'
    return (f'role="img" lang="{esc(resolved_locale(meta))}" '
            f'aria-labelledby="archify-diagram-title archify-diagram-description"{animation}{preset}{engineering} '
            f'data-quality-profile="{quality}"{advisory}')


def svg_accessible_text(meta, kind="sequence"):
    description = meta.get("subtitle") or translate(meta, "diagram.description." + kind)
    return (f'        <title id="archify-diagram-title">{esc(meta.get("title", ""))}</title>\n'
            f'        <desc id="archify-diagram-description">{esc(description)}</desc>')


def animate_attr(meta, kind, step):
    if meta.get("animation") != "trace":
        return ""
    safe_step = min(12, math.floor(step)) if isinstance(step, (int, float)) and step >= 0 else 0
    return f' data-animate="{kind}" style="--step:{safe_step}"'


def _nonempty(value):
    return value is not None and str(value).strip() != ""


def focus_node_attrs(node, metadata, meta):
    fields = (
        ("data-node-kind", metadata.get("kind")),
        ("data-node-sublabel", metadata.get("sublabel")),
        ("data-node-tag", metadata.get("tag")),
        ("data-node-context", metadata.get("context")),
        ("data-node-brand", metadata.get("brand")),
        ("data-node-brand-id", metadata.get("brandId")),
        ("data-node-brand-status", metadata.get("brandStatus")),
        ("data-node-brand-source", metadata.get("brandSource")),
    )
    optional = "".join(f' {name}="{esc(value)}"' for name, value in fields if _nonempty(value))
    detail = ", ".join(str(value) for value in (metadata.get("sublabel"), metadata.get("context"), metadata.get("brand")) if _nonempty(value))
    translation_meta = meta if isinstance(meta, dict) else {"locale": meta}
    aria = translate(translation_meta, "node.focus.detail" if detail else "node.focus",
                     {"label": node.get("label", ""), "detail": detail})
    return (f'id="node-{esc(node.get("id"))}" data-node-id="{esc(node.get("id"))}" '
            f'data-node-label="{esc(node.get("label", ""))}" tabindex="0" role="button" '
            f'aria-label="{esc(aria)}" aria-pressed="false"{optional}')


def focus_node_title(label, metadata):
    parts = [label, metadata.get("sublabel"), metadata.get("context"), metadata.get("tag"), metadata.get("brand")]
    return f'<title>{esc(" · ".join(str(item) for item in parts if _nonempty(item)))}</title>'


def focus_edge_attrs(message, index):
    named = f' data-edge-label="{esc(message.get("label"))}"' if message.get("label") else ""
    identified = (f' data-edge-id="{esc(message["id"])}"'
                  if message.get("id") is not None and str(message.get("id")).strip() else "")
    return (f'data-edge-from="{esc(message.get("from"))}" data-edge-to="{esc(message.get("to"))}"'
            f'{named} data-edge-key="{index}"{identified}')


def _lookup_forms(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return []
    dashed = re.sub(r"[\s_]+", "-", raw)
    compact = re.sub(r"[\s_.-]+", "", raw)
    return list(dict.fromkeys((raw, dashed, compact)))


def resolve_brand(value):
    """Resolve a built-in mark. Digest-pinned remote objects are unsupported."""
    if not value or isinstance(value, dict):
        return None
    raw = str(value).strip()
    parsed = urlparse(raw)
    by_lookup = {}
    marks = get_data("brands")
    for mark in marks:
        for form in (mark["id"], mark.get("title"), *mark.get("aliases", [])):
            for key in _lookup_forms(form):
                by_lookup.setdefault(key, mark)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host = parsed.hostname.lower().rstrip(".")
        candidates = [mark for mark in marks if any(host == domain or host.endswith("." + domain)
                                                   for domain in mark.get("domains", []))]
        if candidates:
            mark = max(candidates, key=lambda item: max((len(domain) for domain in item.get("domains", [])
                                                         if host == domain or host.endswith("." + domain)), default=0))
        else:
            return None
    else:
        mark = next((by_lookup[key] for key in _lookup_forms(raw) if key in by_lookup), None)
    if not mark:
        return None
    return {**mark, "kind": "preset", "status": "preset",
            "sourceUrl": mark.get("provenance", {}).get("source")}


def brand_label_fit_width(node, width, mark=None):
    return max(1, width - 48) if mark else width


def brand_top_rail_problem(node, width, minimum_font_size, mark=None, subject="Participant"):
    if not mark:
        return None
    available = width - 48
    required = text_units(node.get("label")) * minimum_font_size * 0.6
    if available >= required:
        return None
    return (f'{subject} "{node.get("id", "")}" brand top rail leaves {max(0, available)}px for its label, but '
            f'"{node.get("label", "")}" needs ~{math.ceil(required)}px at the {minimum_font_size}px legible minimum '
            '— widen the node or shorten the label.')


def brand_metadata(mark):
    if not mark:
        return {}
    return {"brand": mark["title"], "brandId": mark["id"], "brandStatus": mark["status"],
            "brandSource": mark.get("sourceUrl")}


def render_brand_mark(mark, x, y, size=16):
    if not mark:
        return ""
    inset = 3
    scale = (size - inset * 2) / mark["viewBox"]
    attrs = [f'data-brand-mark="{esc(mark["id"])}"', f'data-brand-title="{esc(mark["title"])}"',
             f'data-brand-status="{esc(mark["status"])}"']
    if mark.get("sourceUrl"):
        attrs.append(f'data-brand-source="{esc(mark["sourceUrl"])}"')
    content = (f'<path d="{esc(mark["path"])}" transform="translate({inset} {inset}) scale({number(scale)})" '
               f'fill="#{esc(mark["hex"])}"/>')
    return (f'<g aria-hidden="true" {" ".join(attrs)} class="brand-mark" transform="translate({number(x)} {number(y)})">\n'
            f'            <rect width="{number(size)}" height="{number(size)}" rx="4" class="brand-mark-badge"/>\n'
            f'            {content}\n'
            f'            <rect width="{number(size)}" height="{number(size)}" rx="4" class="brand-mark-frame"/>\n'
            '          </g>')
