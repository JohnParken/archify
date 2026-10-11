"""Native preflight checks which previously ran inside browser JavaScript."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PYTHON = os.environ.get("ARCHIFY_PYTHON", sys.executable)


class NativePreflightTests(unittest.TestCase):
    def run_python(self, mode, doc, command="validate", preserve=False):
        with tempfile.TemporaryDirectory(prefix="archify-native-preflight-", dir=str(Path(tempfile.gettempdir()).resolve())) as directory:
            directory = Path(directory)
            source = directory / "input.json"
            source.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            output = directory / "python.html"
            if preserve:
                output.write_text("previous artifact", encoding="utf-8")
            args = [PYTHON, str(ROOT / "archify/bin/archify.py"), command, mode, str(source)]
            if command != "validate":
                args.append(str(output))
            args.append("--json")
            env = os.environ.copy()
            env.pop("ARCHIFY_QUALITY_PROFILE", None)
            env["PATH"] = ""
            result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(result.stderr, "", result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0 if receipt["ok"] else 1, receipt)
            if preserve:
                self.assertEqual(output.read_text(encoding="utf-8"), "previous artifact")
            return receipt

    def test_deployment_profile_failures_preserve_existing_output(self):
        base = json.loads((ROOT / "archify/examples/production-deployment.architecture.json").read_text())
        self.assertEqual(base["meta"]["engineering_profile"], "deployment-ownership")
        component = next(c for c in base["components"] if c["type"] != "external")
        cases = []

        missing_owner = copy.deepcopy(base)
        next(c for c in missing_owner["components"] if c["id"] == component["id"]).pop("tag", None)
        cases.append(("owner", missing_owner, "engineering/deployment-owner-missing"))

        missing_region = copy.deepcopy(base)
        for boundary in missing_region["boundaries"]:
            if boundary["kind"] == "region":
                boundary["wraps"] = [cid for cid in boundary["wraps"] if cid != component["id"]]
        cases.append(("region", missing_region, "engineering/deployment-region-scope"))

        ambiguous_region = copy.deepcopy(base)
        original = next(b for b in ambiguous_region["boundaries"] if b["kind"] == "region" and component["id"] in b["wraps"])
        duplicate = {**original, "label": "Another region", "wraps": [component["id"]]}
        duplicate.pop("id", None)
        ambiguous_region["boundaries"].append(duplicate)
        cases.append(("ambiguous", ambiguous_region, "engineering/deployment-region-ambiguous"))

        missing_private = copy.deepcopy(base)
        database_ids = {c["id"] for c in base["components"] if c["type"] == "database"}
        self.assertTrue(database_ids)
        for boundary in missing_private["boundaries"]:
            if boundary["kind"] == "security-group":
                boundary["wraps"] = [cid for cid in boundary["wraps"] if cid not in database_ids]
        missing_private["boundaries"] = [b for b in missing_private["boundaries"] if b["wraps"]]
        cases.append(("private", missing_private, "engineering/deployment-private-state"))

        for name, doc, expected in cases:
            with self.subTest(case=name):
                receipt = self.run_python("architecture", doc, "render", preserve=True)
                self.assertFalse(receipt["ok"])
                codes = {d["code"] for d in receipt["diagnostics"]}
                self.assertIn(expected, codes)

    def test_locale_fallback_and_partial_translation_diagnostics(self):
        base = json.loads((ROOT / "archify/examples/cache-miss-request.sequence.json").read_text())
        base["meta"]["locale"] = "xx"
        base["meta"].pop("translations", None)
        for translations, expected in ((None, "i18n/locale-fallback"), ({"node.focus": "Focus {label}"}, "i18n/translation-coverage")):
            doc = copy.deepcopy(base)
            if translations is not None:
                doc["meta"]["translations"] = translations
            with self.subTest(translations=translations):
                receipt = self.run_python("sequence", doc)
                self.assertTrue(receipt["ok"])
                codes = {d["code"] for d in receipt["diagnostics"]}
                self.assertIn(expected, codes)


if __name__ == "__main__":
    unittest.main()
