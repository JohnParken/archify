#!/usr/bin/env python3
"""Experimental Node-free diagram validate/render/deliver entry point."""

import argparse
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import uuid

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "python"))

from archify_runtime.common import DiagnosticError, diagnostic, diagnostic_scope


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


def main(argv=None):
    with diagnostic_scope() as recorded:
        return _main(argv, recorded)


def _main(argv=None, recorded=None):
    recorded = [] if recorded is None else recorded
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = Parser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render", "deliver", "finalize"))
    parser.add_argument("type", choices=("sequence", "dataflow", "lifecycle", "architecture", "workflow"))
    parser.add_argument("input")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--quality", choices=("standard", "showcase"))
    parser.add_argument("--json", action="store_true")
    receipt = {"schemaVersion": 1, "runtime": "python", "prototype": True, "ok": False,
               "command": argv[0] if argv else None, "type": "sequence", "diagnostics": []}
    stage = "arguments"
    json_mode = "--json" in argv
    try:
        args = parser.parse_args(argv)
        if args.command == "validate" and args.output is not None:
            raise ArgumentError("validate takes an input only")
        from archify_runtime.validation import validate_document
        from archify_runtime.template import apply_template
        from archify_runtime.checks import check_svg
        from archify_runtime.output import resolve_output, snapshot, publish
        receipt.update(command=args.command, type=args.type)
        input_path = Path(os.path.abspath(args.input))
        receipt["input"] = str(input_path)
        stage = "input"
        try:
            specification = input_path.read_bytes()
        except OSError as error:
            raise DiagnosticError(str(error), [diagnostic("input/read", str(error), {"input": str(input_path)},
                {"reason": str(error), "errno": error.errno}, ["provide one readable JSON input file"])]) from error
        try:
            diagram = json.loads(specification.decode("utf-8"), parse_int=js_integer,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as error:
            raise DiagnosticError(str(error), [diagnostic("input/json-parse", str(error), {"input": str(input_path)},
                {"reason": str(error)}, ["repair the JSON syntax and run validation again"])]) from error
        stage = "render"
        validate_document(diagram, args.type)
        quality = args.quality or os.environ.get("ARCHIFY_QUALITY_PROFILE") or None
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
            html = apply_template(template, diagram, svg).encode("utf-8")
            stage = "commit"
            receipt_id = str(uuid.uuid4())
            publish_command = "deliver" if args.command == "finalize" else args.command
            publish(output, html, expected, publish_command, receipt_id)
            receipt.update(artifact=identity(html), receiptId=receipt_id)
        receipt.update(ok=True, diagnostics=recorded)
        if args.command in ("deliver", "finalize"):
            receipt["deliveryContract"] = "prototype-html-only"
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
    receipt["diagnostics"] = recorded + [entry for entry in receipt["diagnostics"] if entry not in recorded]
    if json_mode:
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
    else:
        print(receipt["error"], file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
