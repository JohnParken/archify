"""Cloud generation acceptance: complete SVG before opening the Viewer."""

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
PYTHON = os.environ.get("ARCHIFY_PYTHON", sys.executable)
MODES = ("dataflow", "lifecycle", "architecture", "workflow")
RELATIONS = {"dataflow": "flows", "lifecycle": "transitions", "architecture": "connections", "workflow": "edges"}


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archify-native-cli-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_python(self, command, mode, source, output=None, cli=None):
        args = [PYTHON, str(cli or ROOT / "archify/bin/archify.py"), command, mode, str(source)]
        if output is not None:
            args.append(str(output))
        args.extend(["--quality", "standard", "--json"])
        env = os.environ.copy()
        env["PATH"] = ""
        env.pop("ARCHIFY_QUALITY_PROFILE", None)
        result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertEqual(result.stderr, "", result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if receipt["ok"] else 1, receipt)
        return receipt

    def assert_static_svg(self, output):
        html = output.read_text(encoding="utf-8")
        self.assertNotIn('id="archify-render-input"', html)
        self.assertNotIn("archifyRender(", html)
        self.assertNotIn("archify-render-placeholder", html)
        match = re.search(r"<svg\b.*?</svg>", html, re.DOTALL)
        self.assertIsNotNone(match)
        root = ET.fromstring(match.group())
        self.assertEqual(root.tag, "svg")
        self.assertTrue(any("data-node-id" in e.attrib for e in root.iter()))
        self.assertTrue(any(e.tag == "text" for e in root.iter()))
        self.assertIn("var Archify = {};", html, "the existing Viewer is retained")

    def example(self, mode):
        return sorted((ROOT / "archify/examples").glob(f"*.{mode}.json"))[0]

    def test_every_example_has_inline_svg_and_complete_receipt_with_empty_path(self):
        for mode in MODES:
            for source in sorted((ROOT / "archify/examples").glob(f"*.{mode}.json")):
                with self.subTest(mode=mode, example=source.name):
                    output = self.dir / (source.stem + ".html")
                    receipt = self.run_python("render", mode, source, output)
                    self.assertTrue(receipt["ok"], receipt)
                    self.assertEqual(receipt["rendering"], {"mode": "python", "status": "complete"})
                    self.assert_static_svg(output)

    def test_layout_failures_preserve_previous_html_and_report_diagnostics(self):
        for mode in MODES:
            base = json.loads(self.example(mode).read_text(encoding="utf-8"))
            endpoint = copy.deepcopy(base)
            endpoint[RELATIONS[mode]][0]["to"] = "missing-native-endpoint"
            small_canvas = copy.deepcopy(base)
            small_canvas["meta"]["viewBox"] = [200, 200]
            for name, doc in (("endpoint", endpoint), ("canvas", small_canvas)):
                with self.subTest(mode=mode, case=name):
                    source = self.dir / (mode + "-" + name + ".json")
                    source.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
                    output = self.dir / (mode + "-" + name + ".html")
                    output.write_text("previous artifact", encoding="utf-8")
                    python = self.run_python("deliver", mode, source, output)
                    self.assertFalse(python["ok"], python)
                    error_codes = {d["code"] for d in python["diagnostics"] if d["severity"] == "error"}
                    self.assertTrue(error_codes, python["diagnostics"])
                    if name == "endpoint":
                        expected_code = "workflow/unknown-edge-endpoint" if mode == "workflow" else "layout/constraint"
                        self.assertIn(expected_code, error_codes)
                    elif name == "canvas":
                        self.assertIn("schema/minimum", error_codes)
                    self.assertEqual(output.read_text(encoding="utf-8"), "previous artifact")

    def test_copied_runtime_needs_no_javascript_assets_or_executables(self):
        skill = self.dir / "runtime"
        (skill / "bin").mkdir(parents=True)
        shutil.copyfile(ROOT / "archify/bin/archify.py", skill / "bin/archify.py")
        module = skill / "python/archify_runtime"
        shutil.copytree(ROOT / "archify/python/archify_runtime", module,
                        ignore=shutil.ignore_patterns("data", "__pycache__", "*.pyc"))
        (module / "data").mkdir()
        for name in ("brands", "catalogs", "sigils"):
            shutil.copyfile(ROOT / "archify/python/archify_runtime/data" / (name + ".json"), module / "data" / (name + ".json"))
        shutil.copytree(ROOT / "archify/schemas", skill / "schemas")
        (skill / "assets").mkdir()
        shutil.copyfile(ROOT / "archify/assets/template.html", skill / "assets/template.html")
        self.assertFalse(list(skill.rglob("*.mjs")))
        self.assertFalse(list(skill.rglob("*.js")))
        for mode in (*MODES, "sequence"):
            with self.subTest(mode=mode):
                output = self.dir / ("isolated-" + mode + ".html")
                receipt = self.run_python("deliver", mode, self.example(mode), output, skill / "bin/archify.py")
                self.assertTrue(receipt["ok"], receipt)
                self.assertEqual(receipt["rendering"], {"mode": "python", "status": "complete"})
                self.assert_static_svg(output)


if __name__ == "__main__":
    unittest.main()
