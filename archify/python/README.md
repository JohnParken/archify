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

All modes accept `validate`, `render`, `deliver`, and `finalize`, plus optional
`--quality standard|showcase`. `validate` performs Schema, layout, and final SVG
checks without writing an artifact. Exit codes are 0 for success, 1 for operation
failure, and 2 for invalid arguments.

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
    tests/                  # Pure Python test suite (59 tests)
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

All 59 unit tests run with zero Node.js or external tool dependencies, including
isolated runtime packaging tests with an empty `PATH`.
