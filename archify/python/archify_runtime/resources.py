"""Locate canonical Skill resources in a checkout or an installed package."""

from pathlib import Path


def get_resource_root():
    """Return the Skill root in source, or bundled resources after installation."""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "schemas").is_dir() and (source_root / "assets" / "template.html").is_file():
        return source_root
    return Path(__file__).resolve().parent / "resources"
