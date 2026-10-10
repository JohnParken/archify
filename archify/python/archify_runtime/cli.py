"""Command line interface for Archify Python runtime."""

import sys
from pathlib import Path

# Add bin directory to import archify script
SKILL_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = SKILL_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import archify as archify_bin


def main(argv=None):
    return archify_bin.main(argv)


if __name__ == "__main__":
    sys.exit(main())
