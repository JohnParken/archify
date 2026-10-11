#!/usr/bin/env python3
"""Node-free static diagram generation and optional browser inspection."""

import argparse
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import uuid

from .common import DiagnosticError, diagnostic, diagnostic_scope
from .resources import get_resource_root

SKILL_ROOT = get_resource_root()


class ArgumentError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def identity(content):
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def js_integer(value):
    """JSON numbers use the same IEEE-754 precision as the Node oracle."""
    number = float(value)
    return int(number) if number not in (float("inf"), -float("inf")) else number


def _command(argv):
    """Locate the command while allowing argparse's leading options."""
    value_options = {"--quality", "--browser", "--repo-root", "--receipt", "--candidate-sha256", "--out-dir"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if not token.startswith("-"):
            return token
        index += 2 if token in value_options else 1
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if _command(argv) in ('compare', 'recover', 'browser-check', 'visual-check', 'check'):
        from .commands import advanced_main
        return advanced_main(argv)
    with diagnostic_scope() as recorded:
        return _main(argv, recorded)


def _main(argv=None, recorded=None):
    recorded = [] if recorded is None else recorded
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = Parser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render", "deliver", "finalize", "check"))
    parser.add_argument("type", choices=("sequence", "dataflow", "lifecycle", "architecture", "workflow"))
    parser.add_argument("input")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--quality", choices=("standard", "showcase"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--browser", choices=("off", "auto", "required"), default="off",
                        help="browser inspection policy (default: off for browser-free containers)")
    parser.add_argument("--repo-root")
    parser.add_argument("--receipt")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--out-dir")
    parser.add_argument("--layout-json", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--open", action="store_true")
    receipt = {"schemaVersion": 1, "runtime": "python", "prototype": True, "ok": False,
               "command": argv[0] if argv else None, "type": "sequence", "diagnostics": []}
    stage = "arguments"
    json_mode = "--json" in argv
    try:
        args = parser.parse_intermixed_args(argv)
        if args.command == "validate" and args.output is not None:
            raise ArgumentError("validate takes an input only")
        if args.command == "validate" and args.browser != "off":
            raise ArgumentError("validate does not inspect HTML; use finalize or check for browser inspection")
        from archify_runtime.validation import validate_document
        from archify_runtime.template import apply_template
        from archify_runtime.checks import check_svg
        from archify_runtime.output import canonical_path, resolve_output, snapshot, publish
        from archify_runtime.integrity import CONTRACT, check_html, check_delivery, read_regular, receipt_path, verify_bytes
        from archify_runtime.browser import check_browser
        receipt.update(command=args.command, type=args.type)
        input_path = canonical_path(args.input)
        receipt["input"] = str(input_path)
        stage = "input"
        if args.command != "validate" and args.output is not None:
            # Preserve the input-alias diagnostic before stricter input reads.
            resolve_output(args.output, None, input_path, f"{args.type}.html")
        try:
            specification = read_regular(input_path)
        except DiagnosticError as error:
            raise DiagnosticError(str(error), [diagnostic("input/read", str(error), {"input": str(input_path)},
                {"reason": str(error)}, ["provide one readable regular JSON input file"])]) from error
        try:
            diagram = json.loads(specification.decode("utf-8"), parse_int=js_integer,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as error:
            raise DiagnosticError(str(error), [diagnostic("input/json-parse", str(error), {"input": str(input_path)},
                {"reason": str(error)}, ["repair the JSON syntax and run validation again"])]) from error
        if args.candidate_sha256:
            import re
            if not re.fullmatch(r'[0-9a-fA-F]{64}', args.candidate_sha256):
                raise ArgumentError('--candidate-sha256 requires 64 hex digits')
            if identity(specification)['sha256'] != args.candidate_sha256.lower():
                raise DiagnosticError('Candidate hash differs.', [diagnostic('integrity/specification-mismatch', 'Candidate hash differs.')])
        stage = "render"
        validate_document(diagram, args.type)
        from .repository import verify_repository
        evidence = verify_repository(diagram, args.type, args.repo_root)
        if evidence:receipt['repositoryEvidence'] = evidence
        if args.out_dir and args.output is None:
            args.output = str(Path(args.out_dir) / (args.type + '.html'))
        quality = args.quality or os.environ.get("ARCHIFY_QUALITY_PROFILE") or None
        if args.command == "check":
            stage = "check"
            output = resolve_output(args.output, diagram["meta"].get("output"), input_path, f"{args.type}.html")
            receipt["output"] = str(output)
            saved, static, html = check_delivery(input_path, output, args.type, quality)
            receipt.update(staticChecks=static, checks=static["checks"], specification=identity(specification),
                           artifact=identity(html), artifactIntegrity={"status": "passed"},
                           deliveryContract=CONTRACT, deliveryReceipt=str(receipt_path(output)),
                           visualReview="not-requested")
            stage = "browser"
            browser = check_browser(html, args.browser)
            browser.update(artifact=identity(html), evidenceKind="automated-browser" if browser["status"] != "skipped" else "none")
            receipt["browserEvidence"] = browser
            verify_bytes(input_path, output, specification, html)
            if json.loads(read_regular(receipt_path(output))) != saved:
                raise DiagnosticError("Receipt changed during browser inspection.", [diagnostic("integrity/receipt-changed", "Receipt changed during browser inspection.")])
            if browser["status"] == "failed" or args.browser == "required" and browser["status"] != "passed":
                raise DiagnosticError("Browser inspection did not pass.", browser.get("diagnostics") or [
                    diagnostic("browser/unavailable", browser.get("reason", "Browser inspection unavailable."))])
            receipt.update(ok=True, diagnostics=recorded)
            print(json.dumps(receipt, ensure_ascii=True, indent=2) if json_mode else f"checked {output}")
            return 0
        output = None
        expected = None
        if args.command != "validate":
            output = resolve_output(args.output, diagram["meta"].get("output"), input_path, f"{args.type}.html")
            receipt["output"] = str(output)
            expected = snapshot(output)
        renderer = import_module("archify_runtime." + args.type)
        svg = getattr(renderer, "render_" + args.type)(diagram, quality=quality)
        stage = "check"
        checks = check_svg(svg, quality, args.type)
        receipt["rendering"] = {"mode": "python", "status": "complete"}
        receipt.update(checks=checks, specification=identity(specification))
        if args.command != "validate":
            template = (SKILL_ROOT / "assets/template.html").read_text(encoding="utf-8")
            html = apply_template(template, diagram, svg, evidence).encode("utf-8")
            stage = "strict-check"
            static = check_html(html, quality, args.type, expected_svg=svg)
            receipt.update(staticChecks=static, checks=static["checks"], visualReview="not-requested")
            stage = "browser"
            browser = check_browser(html, args.browser)
            browser.update(artifact=identity(html), evidenceKind="automated-browser" if browser["status"] != "skipped" else "none")
            receipt["browserEvidence"] = browser
            browser_ok = browser["status"] != "failed" and (args.browser != "required" or browser["status"] == "passed")
            if read_regular(input_path) != specification:
                raise DiagnosticError("Candidate changed before publication.", [
                    diagnostic("integrity/specification-mismatch", "Candidate changed before publication.")])
            stage = "commit"
            receipt_id = str(uuid.uuid4())
            receipt.update(artifact=identity(html), receiptId=receipt_id, quality=quality,
                           deliveryContract=CONTRACT, generation="passed", ok=browser_ok, diagnostics=recorded)
            sidecar = receipt_path(output)
            if sidecar.resolve() == input_path.resolve():
                raise ArgumentError("delivery receipt must not replace the input JSON")
            receipt["deliveryReceipt"] = str(sidecar)
            if args.receipt:
                receipt['requestedReceipt'] = str(canonical_path(args.receipt))
            if not browser_ok:
                receipt["stage"] = "browser"
                receipt["diagnostics"] = recorded + (browser.get("diagnostics") or [
                    diagnostic("browser/unavailable", browser.get("reason", "Browser inspection unavailable."))])
            # All HTML publishers use the directory mutex and persist byte-bound receipts.
            publish(output, html, expected, "deliver", receipt_id, receipt=receipt,
                    verifier=lambda: verify_bytes(input_path, output, specification, html))
            if not browser_ok:
                stage = "browser"
                raise DiagnosticError("Browser inspection did not pass; static HTML was generated.", browser.get("diagnostics") or [
                    diagnostic("browser/unavailable", browser.get("reason", "Browser inspection unavailable."))])
        receipt.update(ok=True, diagnostics=recorded)
        if args.layout_json:receipt['layout'] = {'svg': svg}
        if args.open and output:
            import webbrowser
            webbrowser.open(output.as_uri())
        if json_mode:
            print(json.dumps(receipt, ensure_ascii=True, indent=2, allow_nan=False))
        else:
            print(f"valid {args.type}" if args.command == "validate" else str(output))
        return 0
    except ArgumentError as error:
        receipt.update(stage="arguments", error=str(error), diagnostics=[diagnostic("cli/invalid-arguments", str(error))])
        status = 2
    except DiagnosticError as error:
        receipt.update(stage=stage, error=str(error), diagnostics=error.diagnostics)
        status = 1
    except ImportError as error:
        receipt.update(stage="bootstrap", error=str(error), diagnostics=[diagnostic("python/dependency-missing", str(error),
            supported_fixes=["install archify/python/requirements.txt into the selected Python environment"])])
        status = 1
    except OSError as error:
        receipt.update(stage=stage, error=str(error), diagnostics=[diagnostic("output/write", str(error), {"output": receipt.get("output")})])
        status = 1
    except Exception as error:
        receipt.update(stage=stage, error=str(error), diagnostics=[diagnostic("internal/unclassified", str(error))])
        status = 1
    receipt["ok"] = False
    receipt["diagnostics"] = recorded + [entry for entry in receipt["diagnostics"] if entry not in recorded]
    if json_mode:
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
    else:
        print(receipt["error"], file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
