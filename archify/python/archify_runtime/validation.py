"""Offline Schema validation with the existing Agent-facing diagnostic codes."""

import json
import re
import unicodedata
from pathlib import Path

import math
from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource

from .common import DiagnosticError, diagnostic, problems, get_data, js_round, record_diagnostic

SKILL_ROOT = Path(__file__).resolve().parents[2]
COLLECTIONS = {
    "sequence": ("participants", "messages"),
    "dataflow": ("nodes", "flows"),
    "lifecycle": ("states", "transitions"),
    "architecture": ("components", "connections"),
    "workflow": ("nodes", "edges"),
}


def finite_number(checker, value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine("number", finite_number).redefine(
    "integer", lambda checker, value: finite_number(checker, value) and value == int(value))
SchemaValidator = validators.extend(Draft202012Validator, type_checker=TYPE_CHECKER)


def fail(code, message, subject=None, evidence=None, fixes=None):
    raise DiagnosticError(message, [diagnostic(code, message, subject, evidence, fixes)])


def validate_authored_output(value):
    reason = None
    details = {}
    if not isinstance(value, str):
        reason = "type"
    elif not value:
        reason = "empty"
    elif value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", value):
        reason = "absolute"
    elif re.match(r"^[A-Za-z]:", value):
        reason = "drive-relative"
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        reason = "uri"
    elif "\\" in value:
        reason = "backslash"
    else:
        for index, part in enumerate(value.split("/")):
            details = {"segment": part, "segmentIndex": index}
            if not part:
                reason = "empty-segment"
            elif part in (".", ".."):
                reason = "dot-segment"
            elif any(unicodedata.category(c) == "Cc" for c in part):
                reason = "control"
            elif any(0xD800 <= ord(c) <= 0xDFFF for c in part):
                reason = "unpaired-surrogate"
            elif ":" in part:
                reason = "windows-ads"
            elif re.search(r'[<>"|?*]', part):
                reason = "windows-invalid-character"
            elif part.endswith((".", " ")):
                reason = "windows-trailing-dot-space"
            elif re.match(r"^(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", part, re.I):
                reason = "windows-reserved-name"
            elif re.search(r"~[1-9][0-9]*(?:\.|$)", part):
                reason = "windows-short-name"
            elif len(part.encode("utf-8")) > 255 or len(part.encode("utf-16-le")) // 2 > 255:
                reason = "component-too-long"
                details.update(utf8Bytes=len(part.encode("utf-8")), utf16CodeUnits=len(part.encode("utf-16-le")) // 2, limit=255)
            if reason:
                break
    if reason:
        code = "output/meta-absolute" if reason == "absolute" else "output/meta-path-syntax"
        message = "meta.output must be a portable POSIX-relative path."
        fail(code, message, {"output": value, "path": "/meta/output"}, {"reason": reason, **details},
             ["set meta.output to a portable relative .html path such as reports/diagram.html"])


def pointer(parts):
    return "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in parts) if parts else "/"


def schema_diagnostics(error, diagram, diagram_type="sequence"):
    entries = []
    # AJV allErrors includes branch errors as well as the composite keyword.
    for child in error.context:
        entries.extend(schema_diagnostics(child, diagram, diagram_type))
    subject = {"diagramType": diagram_type, "path": pointer(error.absolute_path)}
    node = diagram
    for segment in error.absolute_path:
        node = node[segment]
        if isinstance(node, dict) and ("id" in node or "label" in node):
            subject["identity"] = str(node.get("id", node.get("label")))
    evidence = {"keyword": error.validator, "expected": error.validator_value}
    fixes = []
    if error.validator == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        for key in missing:
            entries.append(diagnostic("schema/required", f'{subject["path"]} must have required property {json.dumps(key)}', subject,
                                      {**evidence, "missingProperty": key}, [f'add required property {json.dumps(key)}']))
        return entries
    if error.validator == "type":
        evidence["type"] = error.validator_value
        fixes = [f'use {json.dumps(error.validator_value)} at {subject["path"]}']
    elif error.validator == "enum":
        evidence["allowedValues"] = error.validator_value
        fixes = [f'choose one of {json.dumps(error.validator_value, ensure_ascii=False)}']
    elif error.validator == "pattern":
        evidence["pattern"] = error.validator_value
        fixes = ["match the required identifier/path pattern"]
    elif error.validator in ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength"):
        evidence["limit"] = error.validator_value
        fixes = [f"satisfy {error.validator} {error.validator_value}"]
    elif error.validator == "additionalProperties":
        fixes = ["remove unsupported properties"]
    entries.append(diagnostic("schema/" + error.validator, f'{subject["path"]} {error.message}', subject, evidence, fixes))
    return entries


def locale_diagnostics(diagram_type, meta):
    locale = meta.get("locale")
    if not locale:
        return
    translations = meta.get("translations", {})
    sub = {"diagramType": diagram_type, "path": "/meta/translations"}
    if translations:
        english = get_data("catalogs")["en"]
        keys = list(english)
        missing = [key for key in keys if key not in translations]
        unknown = [key for key in translations if key not in english]
        mismatches = []
        covered = 0
        for key, value in translations.items():
            if key not in english:
                continue
            expected = sorted(set(re.findall(r"\{([a-zA-Z0-9_]+)\}", english[key])))
            actual = sorted(set(re.findall(r"\{([a-zA-Z0-9_]+)\}", value))) if isinstance(value, str) and value else None
            if actual == expected:
                covered += 1
            else:
                mismatches.append({"key": key, "expected": expected, "actual": actual})
        if missing or unknown or mismatches:
            record_diagnostic(diagnostic("i18n/translation-coverage",
                f'meta.translations for locale {json.dumps(locale, ensure_ascii=False)} covers {covered}/{len(keys)} renderer-owned messages ({js_round(covered / len(keys) * 100)}%); uncovered keys fall back to English.', sub,
                {"missingKeys": missing[:10], "missingKeysTotal": len(missing), "unknownKeys": unknown[:10],
                 "unknownKeysTotal": len(unknown), "placeholderMismatches": mismatches[:10], "placeholderMismatchesTotal": len(mismatches)},
                ["Add the missing keys to meta.translations.", "Match each translation's {placeholders} to the English source string."], "warning"))
    elif locale not in ("en", "zh-CN"):
        record_diagnostic(diagnostic("i18n/locale-fallback",
            f'meta.locale {json.dumps(locale, ensure_ascii=False)} has no built-in catalog and no meta.translations; the Viewer chrome and <html lang> fall back to English.',
            {"diagramType": diagram_type, "path": "/meta/locale"}, supported_fixes=["Supply meta.translations for this locale.", "Use a built-in locale: en, zh-CN."], severity="warning"))


def validate_document(diagram, diagram_type="sequence"):
    nodes, relationships = COLLECTIONS[diagram_type]
    if isinstance(diagram, dict) and isinstance(diagram.get("meta"), dict) and "output" in diagram["meta"]:
        # Match the public CLI: portable path preflight precedes Schema errors.
        validate_authored_output(diagram["meta"]["output"])
    common = json.loads((SKILL_ROOT / "schemas/common.schema.json").read_text(encoding="utf-8"))
    schema = json.loads((SKILL_ROOT / f"schemas/{diagram_type}.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resources([
        (common["$id"], Resource.from_contents(common)),
        (schema["$id"], Resource.from_contents(schema)),
    ])
    errors = list(SchemaValidator(schema, registry=registry).iter_errors(diagram))
    if errors:
        diagnostics = [entry for error in errors for entry in schema_diagnostics(error, diagram, diagram_type)]
        raise DiagnosticError(f"{diagram_type} schema validation failed", diagnostics)
    locale_diagnostics(diagram_type, diagram["meta"])
    seen = set()
    duplicates = []
    for index, relation in enumerate(diagram.get(relationships, [])):
        if relation.get("id") in seen:
            duplicates.append(f'/{relationships}/{index}/id duplicates relationship id {json.dumps(relation["id"])}')
        if relation.get("id"):
            seen.add(relation["id"])
    problems("Relationship identity validation failed", duplicates, "relationship/duplicate-id",
             {"diagramType": diagram_type, "collection": relationships})
    from .engineering import validate_engineering_profile
    validate_engineering_profile(diagram_type, diagram)
    if diagram["meta"].get("repository") or any(node.get("sources") for node in diagram[nodes]):
        fail("python/unsupported-feature", "Repository evidence is not yet implemented in the Python prototype.",
             {"diagramType": diagram_type, "feature": "repository-evidence"}, fixes=["use the existing Node CLI for repository-backed diagrams"])
    if any(isinstance(node.get("brand"), dict) for node in diagram[nodes]):
        fail("python/unsupported-feature", "Remote digest-pinned brand capture is not yet implemented in the Python prototype.",
             {"diagramType": diagram_type, "feature": "remote-brand"}, fixes=["use a built-in brand ID or the existing Node CLI"])
    from .drawing import resolve_brand
    brand_errors = []
    for index, node in enumerate(diagram[nodes]):
        value = node.get("brand")
        if not value:
            continue
        try:
            mark = resolve_brand(value)
        except ValueError:
            mark = None
        if mark is not None:
            continue
        unpinned = re.match(r"^https?://[^/]+", value, re.I) is not None
        code = "brand/unpinned-url" if unpinned else "brand/unknown"
        message = f'/{nodes}/{index}/brand {json.dumps(value)} ' + ("is an unpinned URL" if unpinned else "is not a built-in brand")
        brand_errors.append(diagnostic(code, message, {"diagramType": diagram_type, "collection": nodes},
            supported_fixes=["choose a built-in brand ID; use the Node CLI for remote capture"]))
    if brand_errors:
        raise DiagnosticError("Brand mark validation failed", brand_errors)
