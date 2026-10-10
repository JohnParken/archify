"""Shared data and diagnostic primitives for the Python runtime prototype."""

import json
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
_DIAGNOSTICS = ContextVar("archify_diagnostics", default=None)


@contextmanager
def diagnostic_scope(suppress=False):
    """Keep diagnostics local to a render invocation, including nested probes."""
    entries = []
    token = _DIAGNOSTICS.set(None if suppress else entries)
    try:
        yield entries
    finally:
        _DIAGNOSTICS.reset(token)


def record_diagnostic(entry):
    entries = _DIAGNOSTICS.get()
    if entries is not None and entry not in entries:
        entries.append(entry)
    return entry


@lru_cache(maxsize=3)
def get_data(name):
    return json.loads((DATA / (name + ".json")).read_text(encoding="utf-8"))


class DiagnosticError(Exception):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def diagnostic(code, message, subject=None, evidence=None, supported_fixes=None, severity="error"):
    return {"code": code, "severity": severity, "message": message,
            "subject": subject or {}, "evidence": evidence or {},
            "supportedFixes": supported_fixes or []}


def problems(prefix, entries, code="layout/constraint", subject=None):
    if not entries:
        return
    diagnostics = [entry if isinstance(entry, dict) else diagnostic(
        code, entry, subject or {"diagramType": "sequence"}) for entry in entries]
    raise DiagnosticError(prefix + ":\n- " + "\n- ".join(d["message"] for d in diagnostics), diagnostics)


def esc(value):
    text = "" if value is None else str(value)
    for a, b in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")]:
        text = text.replace(a, b)
    return text


def number(value):
    """SVG number spelling; preserve values rather than quantizing geometry."""
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return repr(float(value))


def js_round(value):
    return math.floor(value + 0.5)


def catalog(meta):
    catalogs = get_data("catalogs")
    locale = meta.get("locale", "en")
    result = dict(catalogs[locale if locale in catalogs else "en"])
    translations = meta.get("translations", {})
    if translations:
        result = dict(catalogs["en"])
        for key, value in translations.items():
            if key in result and isinstance(value, str) and value and set(re.findall(r"\{([a-zA-Z0-9_]+)\}", value)) == set(re.findall(r"\{([a-zA-Z0-9_]+)\}", result[key])):
                result[key] = value
    return result


def resolved_locale(meta):
    locale = meta.get("locale", "en")
    return locale if locale in ("en", "zh-CN") or meta.get("translations") else "en"


def translate(meta, key, values=None):
    text = catalog(meta).get(key, key)
    return re.sub(r"\{([^}]+)\}", lambda m: str((values or {}).get(m[1], m[0])), text)
