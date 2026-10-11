# Native Python Diagram Engine for Archify

The Python runtime provides complete, self-contained SVG generation for Sequence,
Dataflow, Lifecycle, Architecture, and Workflow diagrams. Layout, routing, text
fitting, brands, legends, and layout diagnostics execute entirely in Python.
The SVG is embedded in the standalone HTML Viewer; the user's browser displays it
and provides the interactive theme, focus, navigation, and export interactions.

## Requirements

- Python 3.9 or later
- Direct dependencies: `jsonschema>=4.20.0,<5.0.0`
- No Node.js, JavaScript engine, mini-racer, browser binary, Graphviz, or OS display servers required.

## Quickstart

From the repository root:

```sh
# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r archify/python/requirements.txt

# Render examples
python3 archify/bin/archify.py render dataflow archify/examples/product-analytics.dataflow.json reports/dataflow.html --json
python3 archify/bin/archify.py render lifecycle archify/examples/agent-run.lifecycle.json reports/lifecycle.html --json
python3 archify/bin/archify.py render architecture archify/examples/web-app.architecture.json reports/architecture.html --json
python3 archify/bin/archify.py render workflow archify/examples/agent-tool-call.workflow.json reports/workflow.html --json
python3 archify/bin/archify.py render sequence archify/examples/cache-miss-request.sequence.json reports/sequence.html --json
```

All modes accept `validate`, `render`, `deliver`, `finalize`, and receipt-bound
`check`, plus optional `--quality standard|showcase` and
`--browser off|auto|required` (default `off`). `validate` performs Schema, layout, and final SVG
checks without writing an artifact. HTML generation strictly inspects the final HTML/SVG, persists a
`.python-delivery.json` receipt, and rereads the candidate, artifact and receipt
to verify byte integrity. `check` verifies an existing delivery without rendering.
Exit codes are 0 for policy success, 1 for operation or browser-gate failure,
and 2 for invalid arguments. Browser status remains separate from static
generation; `required` fails when browser inspection is skipped. `validate`
cannot request a browser inspection. See [delivery contract](../references/delivery-contract.md).

## Architecture & Directory Layout

```text
archify/
  bin/
    archify.py              # Main CLI entry point
    archify                 # Executable symlink to archify.py
  python/
    archify_runtime/        # Native Python layout & rendering modules
      data/                 # Built-in brand marks, sigils, and i18n catalogs
    requirements.txt        # Python dependency specification
    tests/                  # Pure Python test suite
      fixtures/             # Self-contained diagram test fixtures
  schemas/                  # JSON Schemas for all diagram types
  assets/
    template.html           # Standalone Viewer HTML template
```

## Running Tests

Run the complete test suite with Python's standard `unittest`:

```sh
python3 scripts/run_tests.py
# Or directly:
PYTHONPATH=archify/python python3 -m unittest discover -s archify/python/tests -v
```

Static tests run with zero Node.js or browser dependencies, including
isolated runtime packaging tests with an empty `PATH`.

## Optional browser acceptance

Ordinary DeepAgent images need only `requirements.txt`; no browser is discovered
or launched by default. `--browser auto` reports missing Playwright/Chromium as
`skipped`, while attempted browser failures exit 1. `--browser required` requires
a passing inspection. Prepare a dedicated browser environment with the optional
Python `playwright` package and its matching Chromium/OS dependencies. The helper
checks desktop containment, projected semantic text, theme switching and page
errors; it does not verify exports or every Viewer interaction.

```sh
python3 archify/bin/archify.py finalize workflow archify/examples/agent-tool-call.workflow.json reports/workflow.html --quality showcase --browser off --json
python3 archify/bin/archify.py check workflow archify/examples/agent-tool-call.workflow.json reports/workflow.html --quality showcase --browser off --json
```

HTML and receipts use a flushed recovery journal and owned directory locks.
`archify recover output.html --json` rolls back an interrupted uncommitted
publication or finishes a recorded commit. Unknown files and live owners are
preserved. This is recoverable publication, not crash-atomic or signed provenance.

## Installed package and migrated commands

Build a wheel/sdist with `python -m build`; install the wheel to obtain `archify`.
Schemas, the offline Viewer and attribution files are included in the wheel.
The source launchers `python bin/archify.py` and `node bin/archify.mjs` retain
existing calling forms. The latter delegates to Python (`ARCHIFY_PYTHON`).

Repository-backed candidates use `--repo-root`, a full commit SHA and node
`sources`. Git verifies origin identity, regular committed blobs and line ranges;
no remote requests or worktree file reads are used as proof.

```sh
archify compare architecture base.json head.json delta.html --receipt delta.receipt.json --json
archify check delta.html --require-provenance --json
```

Comparison distinguishes semantic, evidence, topology and geometry changes,
provides three snapshots and guided review, and exports SVG/PNG. It does not
infer runtime impact. `--candidate-sha256` binds the expected SHA256 of the raw
head input bytes; `--receipt` is part of the publication transaction. A required
browser gate failure retains statically checked HTML and a failed delivery
receipt, then exits 1. `--open` explicitly opens a preview.

CI runs static/distribution tests on Python 3.9–3.13 and Linux/macOS/Windows.
Opt in to the real Viewer tests with `ARCHIFY_BROWSER_TESTS=1`; they cover all
five modes, offline loading, focus/reach, themes, errors and SVG/PNG downloads.
