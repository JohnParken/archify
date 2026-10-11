"""Black-box CLI checks for the native SVG runtime."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "archify" / "bin" / "archify.py"
PYTHON = Path(os.environ.get("ARCHIFY_PYTHON", sys.executable))
EXAMPLES = ROOT / "archify" / "examples"
MODES = ("dataflow", "lifecycle", "architecture", "workflow")


class NativeModeCliTests(unittest.TestCase):
    def setUp(self):
        if not PYTHON.is_file():
            self.skipTest(f"prototype interpreter is not installed: {PYTHON}")
        self.temp = tempfile.TemporaryDirectory(prefix="archify-modes-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, command, mode, source, output=None, *, empty_path=False):
        args = [str(PYTHON), str(CLI), command, mode, str(source)]
        if output is not None:
            args.append(str(output))
        args.append("--json")
        env = os.environ.copy()
        env.pop("ARCHIFY_QUALITY_PROFILE", None)
        if empty_path:
            env["PATH"] = ""
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def read_example(self, mode):
        source = sorted(EXAMPLES.glob(f"*.{mode}.json"))[0]
        return source, json.loads(source.read_text(encoding="utf-8"))

    @staticmethod
    def receipt(result, command, mode, success):
        payload = json.loads(result.stdout)
        if success:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode == 1, result.stdout + result.stderr
        assert result.stderr == "", result.stderr
        assert payload["ok"] is success, payload
        assert payload["command"] == command
        assert payload["type"] == mode
        assert payload["runtime"] == "python"
        assert payload["prototype"] is True
        assert isinstance(payload["diagnostics"], list)
        return payload

    def write_doc(self, doc, filename="input.json"):
        path = self.dir / filename
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def test_all_checked_in_mode_examples_validate(self):
        for mode in MODES:
            examples = sorted(EXAMPLES.glob(f"*.{mode}.json"))
            self.assertTrue(examples, f"No checked-in {mode} examples")
            for source in examples:
                with self.subTest(mode=mode, example=source.name):
                    receipt = self.receipt(self.run_cli("validate", mode, source, empty_path=True), "validate", mode, True)
                    self.assertEqual(receipt["rendering"]["mode"], "python")
                    self.assertEqual(receipt["rendering"]["status"], "complete")
                    self.assertNotIn("output", receipt, "validate must not publish an artifact")

    def test_render_and_deliver_receipts_run_without_node(self):
        for mode in MODES:
            source, _ = self.read_example(mode)
            for command in ("render", "deliver"):
                with self.subTest(mode=mode, command=command):
                    output = self.dir / f"{mode}-{command}.html"
                    receipt = self.receipt(self.run_cli(command, mode, source, output, empty_path=True), command, mode, True)
                    self.assertEqual(receipt["rendering"]["mode"], "python")
                    self.assertEqual(receipt["rendering"]["status"], "complete")
                    self.assertTrue(output.is_file())
                    html = output.read_text(encoding="utf-8")
                    self.assertIn("<svg", html)
                    self.assertNotIn('id="archify-render-input"', html)
                    self.assertNotIn("archifyRender(", html)
                    if command == "deliver":
                        self.assertEqual(receipt["deliveryContract"], "python-static-v1")

    def test_schema_relationship_brand_failures_preserve_existing_output(self):
        source, example = self.read_example("dataflow")
        base = copy.deepcopy(example)
        bad_schema = copy.deepcopy(base)
        bad_schema["unexpected_property"] = True
        bad_type = copy.deepcopy(base)
        bad_type["nodes"] = "not-an-array"
        duplicate_id = copy.deepcopy(base)
        self.assertGreaterEqual(len(duplicate_id["flows"]), 2)
        duplicate_id["flows"][0]["id"] = "reused-flow-id"
        duplicate_id["flows"][1]["id"] = "reused-flow-id"
        unknown_brand = copy.deepcopy(base)
        unknown_brand["nodes"][0]["brand"] = "brand-that-does-not-exist"
        wrong_diagram_type = copy.deepcopy(base)
        wrong_diagram_type["diagram_type"] = "workflow"
        cases = (("schema", bad_schema, None), ("wrong-type", bad_type, None),
                 ("duplicate-id", duplicate_id, "relationship/duplicate-id"),
                 ("unknown-brand", unknown_brand, None),
                 ("wrong-diagram-type", wrong_diagram_type, "schema/const"))
        for name, doc, expected_code in cases:
            with self.subTest(case=name):
                input_path = self.write_doc(doc, f"{name}.json")
                output = self.dir / f"{name}.html"
                output.write_text("prior artifact", encoding="utf-8")
                receipt = self.receipt(self.run_cli("render", "dataflow", input_path, output), "render", "dataflow", False)
                self.assertTrue(receipt["diagnostics"], receipt)
                if expected_code:
                    self.assertIn(expected_code, {item["code"] for item in receipt["diagnostics"]})
                self.assertEqual(output.read_text(encoding="utf-8"), "prior artifact")

    def test_malformed_input_is_rejected_without_replacing_output(self):
        source = self.dir / "malformed.json"
        source.write_text('{"schema_version":', encoding="utf-8")
        output = self.dir / "malformed.html"
        output.write_text("prior artifact", encoding="utf-8")
        receipt = self.receipt(self.run_cli("render", "dataflow", source, output), "render", "dataflow", False)
        self.assertEqual({item["code"] for item in receipt["diagnostics"]}, {"input/json-parse"})
        self.assertEqual(output.read_text(encoding="utf-8"), "prior artifact")

    def test_authored_script_terminator_is_escaped_in_static_svg(self):
        _, doc = self.read_example("workflow")
        doc["meta"]["title"] = "Safe </script><script>injected()"
        source = self.write_doc(doc, "script-terminator.json")
        output = self.dir / "safe.html"
        self.receipt(self.run_cli("render", "workflow", source, output), "render", "workflow", True)
        html = output.read_text(encoding="utf-8")
        match = re.search(r"<svg\b.*?</svg>", html, re.DOTALL)
        self.assertIsNotNone(match, "native SVG is missing")
        import xml.etree.ElementTree as ET
        title = ET.fromstring(match.group()).find("title")
        self.assertEqual(title.text, doc["meta"]["title"])
        self.assertNotIn("</script><script>injected()", html.lower())


if __name__ == "__main__":
    unittest.main()
