#!/usr/bin/env python3
"""Extract a ZIP and exercise its public Python CLI without Node or a browser.

Use an interpreter with archify/python/requirements.txt installed. No live Skill
installation is changed. A supplied --out-dir must not already exist.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile

EXAMPLES = {
    "architecture": "web-app.architecture.json",
    "workflow": "agent-tool-call.workflow.json",
    "sequence": "cache-miss-request.sequence.json",
    "dataflow": "product-analytics.dataflow.json",
    "lifecycle": "deployment-release.lifecycle.json",
}


def extract(archive_bytes, destination):
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError("ZIP CRC failure: " + bad)
        seen = set()
        for entry in archive.infolist():
            parts = entry.filename.split("/")
            mode = entry.external_attr >> 16
            if (parts[0] != "archify" or len(parts) < 2 or any(part in ("", ".", "..") for part in parts)
                    or "\\" in entry.filename or entry.filename in seen
                    or stat.S_IFMT(mode) != stat.S_IFREG
                    or any(part in ("tests", "test", "__pycache__", "node_modules", ".archify") for part in parts)):
                raise ValueError("Unsafe or non-runtime ZIP entry: " + entry.filename)
            seen.add(entry.filename)
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(archive.read(entry))
            target.chmod(stat.S_IMODE(mode))


def run(archive, directory):
    archive_bytes = archive.read_bytes()
    extract(archive_bytes, directory)
    skill = directory / "archify"
    work = directory / "work"
    work.mkdir()
    # -I ignores user-site/PYTHONPATH; the packaged CLI adds only its own runtime.
    environment = os.environ.copy()
    environment["PATH"] = ""
    environment.pop("PYTHONPATH", None)
    environment.pop("ARCHIFY_QUALITY_PROFILE", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    results = []

    def invoke(command, mode, source, output, policy="off", entry="archify.py"):
        args = [sys.executable, "-I", str(skill / "bin" / entry), command, mode, str(source), str(output),
                "--quality", "showcase", "--browser", policy, "--json"]
        process = subprocess.run(args, cwd=work, env=environment, text=True, capture_output=True, timeout=30)
        receipt = json.loads(process.stdout)
        expected_exit = 1 if policy == "required" and receipt.get("browserEvidence", {}).get("status") == "skipped" else 0
        if process.returncode != expected_exit or process.stderr:
            raise AssertionError({"command": args, "exit": process.returncode, "stderr": process.stderr, "receipt": receipt})
        if receipt.get("staticChecks", {}).get("status") != "passed" or receipt.get("artifactIntegrity", {}).get("status") != "passed":
            raise AssertionError(receipt)
        if policy == "off" and receipt["browserEvidence"].get("reason") != "disabled-by-policy":
            raise AssertionError(receipt)
        if policy == "required" and receipt["browserEvidence"]["status"] == "skipped" and receipt["ok"]:
            raise AssertionError("required must not accept skipped browser evidence")
        evidence = output.with_name(output.stem + "." + command + "-" + policy + ".smoke.json")
        evidence.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        results.append({"type": mode, "command": command, "browserPolicy": policy,
                        "exit": process.returncode, "staticChecks": "passed", "artifactIntegrity": "passed",
                        "browserEvidence": receipt["browserEvidence"], "output": str(output), "commandLine": args})

    for mode, example in EXAMPLES.items():
        folder = work / mode
        folder.mkdir()
        source = folder / "candidate.json"
        source.write_bytes((skill / "examples" / example).read_bytes())
        output = folder / (mode + ".html")
        invoke("finalize", mode, source, output)
        invoke("check", mode, source, output)
    source = work / "sequence/candidate.json"
    for policy in ("auto", "required"):
        invoke("finalize", "sequence", source, work / ("browser-" + policy + ".html"), policy)
    invoke("finalize", "sequence", source, work / "alias.html", entry="archify")
    summary = {"status": "passed", "archive": str(archive), "archiveSha256": hashlib.sha256(archive_bytes).hexdigest(),
               "isolatedRoot": str(directory), "python": sys.executable, "executablePath": "empty",
               "pythonIsolatedMode": True, "results": results,
               "scope": "packaged CLI simulation; not an actual LangChain DeepAgent container or real browser measurement"}
    (directory / "package-smoke.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve()
    if args.out_dir:
        directory = args.out_dir.absolute()
        directory.mkdir(parents=True, exist_ok=False)
        summary = run(archive, directory.resolve())
        print(json.dumps({"status": summary["status"], "cases": len(summary["results"]),
                          "archiveSha256": summary["archiveSha256"], "receipt": str(directory / "package-smoke.json")}, indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="archify-package-smoke-") as temporary:
            summary = run(archive, Path(temporary).resolve())
            print(json.dumps({"status": summary["status"], "cases": len(summary["results"]),
                              "archiveSha256": summary["archiveSha256"]}, indent=2))


if __name__ == "__main__":
    main()
