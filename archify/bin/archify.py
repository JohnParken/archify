#!/usr/bin/env python3
"""Source-tree launcher for the Archify Python runtime."""

from pathlib import Path
import sys

RUNTIME_PACKAGE = Path(__file__).resolve().parents[1] / "python"
if str(RUNTIME_PACKAGE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PACKAGE))

from archify_runtime.cli import main


if __name__ == "__main__":
    sys.exit(main())
