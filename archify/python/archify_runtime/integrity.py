"""Static final-HTML checks and byte-bound Python delivery receipts.

Receipts prove local byte consistency, not authenticity or browser acceptance.
"""

import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import stat

from .checks import check_svg
from .common import DiagnosticError, diagnostic

CONTRACT = "python-static-v1"


def identity(content):
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def fail(code, message, path=None):
    raise DiagnosticError(message, [diagnostic(code, message,
        {"path": str(path)} if path else {}, supported_fixes=[
            "finalize the unchanged candidate to a separate output, then check its receipt"] )])


def read_regular(path):
    path = Path(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail("integrity/unsafe-file", "Expected a regular file with one hard link.", path)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_nlink)
        if not fields(before) == fields(opened) == fields(after) == fields(current):
            fail("integrity/file-changed", "File changed while being checked.", path)
        return content
    except OSError as error:
        fail("integrity/read", str(error), path)


def receipt_path(output):
    output = Path(output)
    stem = output.stem
    if len(stem.encode("utf-8")) > 160:
        stem = stem[:30] + "-" + hashlib.sha256(stem.encode()).hexdigest()[:16]
    return output.with_name(stem + ".python-delivery.json")


class ArtifactParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.lines = [0]
        for match in re.finditer("\n", html):
            self.lines.append(match.end())
        self.stack = []
        self.svgs = []
        self.start = None
        self.depth = 0
        self.diagram_count = 0
        self.title_count = 0
        self.heading_count = 0
        self.placeholders = False
        self.doctype = False

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_decl(self, decl):
        self.doctype = self.doctype or decl.lower() == "doctype html"

    def handle_comment(self, data):
        if re.search(r"ARCHIFY:(?:SVG_SLOT|CARDS_SLOT|I18N_DATA|SOURCE_EVIDENCE_DATA)", data):
            self.placeholders = True

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "diagram-container" in attrs.get("class", "").split():
            self.diagram_count += 1
        if tag == "title" and not self.depth:
            self.title_count += 1
        if tag == "h1":
            self.heading_count += 1
        if tag == "svg":
            if not self.depth and self.stack and self.stack[-1] == "diagram":
                self.start = self.source_offset()
            self.depth += 1
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append("diagram" if tag == "div" and "diagram-container" in attrs.get("class", "").split() else tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "svg":
            self.depth -= 1
            if not self.depth and self.start is not None:
                self.svgs.append(self.html[self.start:self.source_offset() + len("</svg>")])
                self.start = None
        if self.stack:
            self.stack.pop()


def check_html(content, quality=None, diagram_type="sequence", expected_svg=None):
    try:
        html = content.decode("utf-8")
    except UnicodeError:
        fail("artifact/html-encoding", "Final HTML must be UTF-8.")
    parser = ArtifactParser(html)
    parser.feed(html)
    parser.close()
    if (not parser.doctype or parser.diagram_count != 1 or len(parser.svgs) != 1
            or parser.title_count != 1 or parser.heading_count != 1 or parser.placeholders
            or parser.depth != 0 or parser.stack):
        fail("artifact/html-structure", "Final HTML needs one complete inline diagram, title and heading, with no template slots.")
    svg = parser.svgs[0]
    if expected_svg is not None and svg != expected_svg.strip():
        fail("artifact/svg-mismatch", "Embedded SVG differs from the validated rendering.")
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        fail("artifact/svg-xml", "Final embedded SVG is not valid XML.")
    try:
        viewbox = [float(value) for value in root.attrib["viewBox"].split()]
        valid = len(viewbox) == 4 and all(math.isfinite(value) for value in viewbox) and all(value > 0 for value in viewbox[2:])
    except (KeyError, ValueError):
        valid = False
    if not valid:
        fail("artifact/viewbox", "Final SVG needs a finite viewBox with positive width and height.")
    checks = check_svg(svg, quality, diagram_type)
    if quality and root.attrib.get("data-quality-profile") != quality:
        fail("artifact/quality-profile", "Final SVG quality profile differs from the requested profile.")
    ids = [element.attrib["id"] for element in root.iter() if "id" in element.attrib]
    if len(ids) != len(set(ids)):
        fail("artifact/duplicate-id", "Final SVG contains duplicate IDs.")
    known = set(ids)
    for element in root.iter():
        for key, value in element.attrib.items():
            references = re.findall(r"url\(#([^\)]+)\)", value)
            if key in ("href", "{http://www.w3.org/1999/xlink}href") and value.startswith("#"):
                references.append(value[1:])
            if any(reference not in known for reference in references):
                fail("artifact/missing-reference", "Final SVG references a missing ID.")
    return {"status": "passed", "checks": checks + [
        {"name": "final_html", "ok": True}, {"name": "svg_references", "ok": True}],
        "svg": identity(svg.encode("utf-8"))}


def verify_bytes(input_path, output, specification, artifact):
    if read_regular(input_path) != specification:
        fail("integrity/specification-mismatch", "Candidate changed after validation.", input_path)
    if read_regular(output) != artifact:
        fail("integrity/artifact-mismatch", "Published HTML differs from the validated bytes.", output)
    return {"status": "passed", "specification": identity(specification), "artifact": identity(artifact)}


def check_delivery(input_path, output, diagram_type, quality=None):
    from .output import canonical_path
    input_path = canonical_path(input_path)
    output = canonical_path(output)
    from .delivery import sidecar
    for marker in (output.parent / ".archify-delivery-lock.json", sidecar(output, ".delivery-pending.json")):
        if marker.exists() or marker.is_symlink():
            fail("integrity/delivery-in-progress", "Delivery or recovery must finish before checking.", marker)
    path = receipt_path(output)
    try:
        receipt = json.loads(read_regular(path))
    except (ValueError, UnicodeError):
        fail("integrity/receipt-invalid", "Invalid Python delivery receipt.", path)
    if (not isinstance(receipt, dict) or receipt.get("deliveryContract") != CONTRACT
            or receipt.get("schemaVersion") != 1 or receipt.get("type") != diagram_type
            or not isinstance(receipt.get("input"), str)
            or canonical_path(receipt["input"]) != input_path
            or receipt.get("output") != str(output.absolute())
            or not isinstance(receipt.get("artifactIntegrity"), dict)
            or receipt["artifactIntegrity"].get("status") != "passed"
            or not isinstance(receipt.get("staticChecks"), dict)):
        fail("integrity/receipt-invalid", "Receipt does not bind this candidate and output.", path)
    spec, html = read_regular(input_path), read_regular(output)
    if identity(spec) != receipt.get("specification"):
        fail("integrity/specification-mismatch", "Candidate no longer matches its delivery receipt.", input_path)
    if identity(html) != receipt.get("artifact"):
        fail("integrity/artifact-mismatch", "HTML no longer matches its delivery receipt.", output)
    static = check_html(html, quality or receipt.get("quality"), diagram_type)
    if static["svg"] != receipt.get("staticChecks", {}).get("svg"):
        fail("integrity/svg-mismatch", "Embedded SVG no longer matches its delivery receipt.", output)
    # Verify the same inputs again after static inspection.
    verify_bytes(input_path, output, spec, html)
    if json.loads(read_regular(path)) != receipt:
        fail("integrity/receipt-changed", "Receipt changed while being checked.", path)
    return receipt, static, html
