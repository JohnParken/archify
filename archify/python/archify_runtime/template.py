"""Fill the canonical offline Viewer without changing its scripts or CSS."""

import json
import re

from .common import catalog, esc, resolved_locale, translate


def script_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_cards(cards):
    items = []
    for card in cards or []:
        lines = "\n".join(f"          <li>{esc(item)}</li>" for item in card["items"])
        items.append(f'''      <div class="card">
        <div class="card-header">
          <div class="card-dot {esc(card["dot"])}"></div>
          <h3>{esc(card["title"])}</h3>
        </div>
        <ul>
{lines}
        </ul>
      </div>''')
    return '    <!-- Info Cards -->\n    <div class="cards">\n' + "\n\n".join(items) + '\n    </div>'


def apply_template(template, diagram, svg, evidence=None):
    meta = diagram["meta"]
    messages = catalog(meta)
    template = re.sub(r"\{\{i18n:([a-zA-Z0-9_.-]+)\}\}", lambda m: esc(translate(meta, m[1])), template)
    locale = resolved_locale(meta)
    replacements = {
        '<html lang="en" data-theme="dark" data-preset="[VISUAL PRESET]">': f'<html lang="{esc(locale)}" data-theme="dark" data-preset="{esc(meta.get("visual_preset", "classic"))}">',
        '<title>[PROJECT NAME] Architecture Diagram</title>': f'<title>{esc(translate(meta, "page.title", {"title": meta["title"]}))}</title>',
        '<h1>[PROJECT NAME] Architecture</h1>': f'<h1>{esc(meta["title"])}</h1>',
        '    <!-- ARCHIFY:I18N_DATA -->': '    <script id="archify-i18n-data" type="application/json">' + script_json({"locale": locale, "messages": {k: v for k, v in messages.items() if k.startswith("viewer.")}}) + '</script>',
        '    <!-- ARCHIFY:SOURCE_EVIDENCE_DATA -->': ('    <script id="archify-source-evidence-data" type="application/json">' + script_json(evidence) + '</script>') if evidence else '',
    }
    for source, replacement in replacements.items():
        if source not in template:
            raise ValueError("canonical template missing " + source)
        template = template.replace(source, replacement, 1)
    subtitle = meta.get("subtitle", "")
    template, count = re.subn(r'^([ \t]*)<p class="subtitle">\[Subtitle description\]</p>[ \t]*(\r?\n)?',
        lambda m: f'{m[1]}<p class="subtitle">{esc(subtitle)}</p>{m[2] or ""}' if subtitle.strip() else '', template, count=1, flags=re.M)
    if count != 1:
        raise ValueError("canonical template missing subtitle slot")
    for name, replacement, indent in [("SVG", svg, "      "), ("CARDS", render_cards(diagram.get("cards")), "    ")]:
        template, count = re.subn(indent + '<!-- ARCHIFY:' + name + '_SLOT_START -->[\\s\\S]*?' + indent + '<!-- ARCHIFY:' + name + '_SLOT_END -->', lambda m: replacement, template, count=1)
        if count != 1:
            raise ValueError("canonical template missing " + name + " slot")
    return template
