#!/usr/bin/env python3
"""Check that a release tag, Python distribution, and Skill identity agree."""

import argparse
import json
import os
from pathlib import Path
import re
import sys


def fail(message):
    raise ValueError(message)


def read_pyproject_version(path):
    text = path.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if not section:
        fail("pyproject.toml is missing its [project] table")
    match = re.search(r"(?m)^version\s*=\s*['\"]([^'\"]+)['\"]\s*$", section.group(1))
    if not match:
        fail("pyproject.toml [project] is missing a literal version")
    return match.group(1)


def read_skill_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        fail("SKILL.md is missing a YAML frontmatter block")
    frontmatter = parts[1]
    name = re.search(r"(?m)^name:\s*['\"]?([^\s'\"]+)['\"]?\s*$", frontmatter)
    metadata = re.search(r"(?ms)^metadata:\s*\n((?:^[ \t]+.*\n?)*)", frontmatter)
    version = re.search(r"(?m)^[ \t]+version:\s*['\"]?([^\s'\"]+)['\"]?\s*$", metadata.group(1) if metadata else "")
    if not name or not version:
        fail("SKILL.md frontmatter must declare name and metadata.version")
    return name.group(1), version.group(1)


def parse_version(version):
    match = re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$", version)
    if not match:
        fail("release version must use three-part semantic versioning")
    prerelease = match.group(4)
    if prerelease and any(identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
                          for identifier in prerelease.split(".")):
        fail("release version has a numeric prerelease identifier with a leading zero")
    return {
        "family": match.group(1) + "." + match.group(2),
        "prerelease": prerelease,
    }


def validate(root, tag):
    if not tag.startswith("v"):
        fail("release tag must start with v")
    tag_version = tag[1:]
    parsed_version = parse_version(tag_version)

    pyproject_version = read_pyproject_version(root / "pyproject.toml")
    if pyproject_version != tag_version:
        fail(f"tag {tag} does not match pyproject.toml version {pyproject_version}")

    release = json.loads((root / "archify/skill-release.json").read_text(encoding="utf-8"))
    if release.get("schemaVersion") != 1 or release.get("skillId") != "archify":
        fail("archify/skill-release.json must identify schema-v1 Skill archify")
    if release.get("source", {}).get("repository") != "https://github.com/tt-a1i/archify":
        fail("archify/skill-release.json source repository does not match Archify")
    if release.get("updateManifestUrl") != "https://tt-a1i.github.io/archify/skill-updates/archify/stable.json":
        fail("archify/skill-release.json update manifest URL does not match Archify")
    if release.get("version") != tag_version:
        fail(f"tag {tag} does not match archify/skill-release.json version {release.get('version')!r}")
    channel = release.get("channel")
    if channel not in ("stable", "development"):
        fail("archify/skill-release.json channel must be stable or development")
    expected_channel = "development" if parsed_version["prerelease"] else "stable"
    if channel != expected_channel:
        fail(f"release channel {channel!r} does not match version {tag_version!r}")

    skill_name, skill_version = read_skill_frontmatter(root / "archify/SKILL.md")
    if skill_name != release["skillId"]:
        fail(f"SKILL.md name {skill_name!r} does not match skillId {release['skillId']!r}")
    expected_family = parsed_version["family"]
    if skill_version != expected_family:
        fail(f"SKILL.md metadata.version {skill_version!r} must match release family {expected_family!r}")

    return {
        "version": tag_version,
        "channel": channel,
        "prerelease": "true" if channel == "development" else "false",
        "make_latest": "false" if channel == "development" else "true",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if not args.tag:
        parser.error("--tag or GITHUB_REF_NAME is required")
    try:
        result = validate(args.root.resolve(), args.tag)
        lines = "".join(f"{key}={value}\n" for key, value in result.items())
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(lines)
        else:
            sys.stdout.write(lines)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"release identity check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
