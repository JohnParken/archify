"""Black-box tests for the Sequence CLI."""

import json
import os
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "archify" / "bin" / "archify.py"
PYTHON = Path(os.environ.get("ARCHIFY_PYTHON", sys.executable))
EXAMPLES = ROOT / "archify" / "examples"


def sequence(**overrides):
    doc = {
        "schema_version": 1,
        "diagram_type": "sequence",
        "meta": {"title": "Parity fixture", "output": "diagram.html", "viewBox": [920, 760]},
        "participants": [
            {"id": "caller", "type": "frontend", "label": "Caller"},
            {"id": "service", "type": "backend", "label": "Service"},
            {"id": "store", "type": "database", "label": "Store"},
        ],
        "messages": [
            {"id": "request", "from": "caller", "to": "service", "y": 200, "label": "Request", "variant": "emphasis"},
            {"id": "read", "from": "service", "to": "store", "y": 270, "label": "Read", "variant": "security"},
            {"id": "reply", "from": "store", "to": "caller", "y": 340, "label": "结果", "variant": "return", "note": "响应完成"},
        ],
        "segments": [{"from": 175, "to": 370, "label": "处理"}],
        "activations": [{"participant": "service", "from": 200, "to": 320}],
    }
    doc.update(overrides)
    return doc


class SequenceCliTests(unittest.TestCase):
    def setUp(self):
        if not PYTHON.is_file():
            self.skipTest(f"prototype interpreter is not installed: {PYTHON}")
        self.temp = tempfile.TemporaryDirectory(prefix="archify-parity-", dir=str(Path("/tmp").resolve()))
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_doc(self, doc, name="input.sequence.json"):
        path = self.dir / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, command, source, output=None, *options, empty_path=False):
        args = [str(PYTHON), str(CLI), command, "sequence", str(source)]
        if output is not None:
            args.append(str(output))
        args.extend(options)
        env = os.environ.copy()
        env.pop("ARCHIFY_QUALITY_PROFILE", None)
        if empty_path:
            env["PATH"] = ""
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def receipt(self, result, command, success):
        self.assertEqual(result.stderr, "", result.stderr)
        self.assertIn(result.returncode, (0, 1, 2), result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ok"], success, payload)
        self.assertEqual(payload["command"], command)
        self.assertEqual(payload["type"], "sequence")
        self.assertIsInstance(payload["diagnostics"], list)
        return payload

    def test_checked_in_sequence_examples_validate_and_render(self):
        for source in sorted(EXAMPLES.glob("*.sequence.json")):
            with self.subTest(example=source.name):
                valid = self.run_cli("validate", source, None, "--json")
                self.receipt(valid, "validate", True)
                output = self.dir / (source.stem + ".html")
                rendered = self.run_cli("render", source, output, "--quality", "standard", "--json")
                self.receipt(rendered, "render", True)
                self.assertTrue(output.is_file())
                html = output.read_text(encoding="utf-8")
                self.assertIn("<svg", html)
                self.assertIn("</svg>", html)

    def test_synthetic_sequence_features_and_quality_profiles(self):
        doc = sequence()
        doc["meta"].update({"column_fit": "spread", "legend": {"entries": {"emphasis": {"label": "Main path"}}}, "animation": "trace"})
        doc["participants"][1]["brand"] = "github"
        source = self.write_doc(doc)
        for quality in ("standard", "showcase"):
            with self.subTest(quality=quality):
                output = self.dir / f"{quality}.html"
                result = self.run_cli("render", source, output, "--quality", quality, "--json")
                self.receipt(result, "render", True)
                html = output.read_text(encoding="utf-8")
                for expected in ("Caller", "Service", "Store", "Request", "响应完成", "data-brand-mark=\"github\""):
                    self.assertIn(expected, html)

    def test_meta_and_cli_quality_precedence_matches_node_with_custom_locale(self):
        base = sequence()
        base["participants"][0]["sublabel"] = "Caller context"
        base["meta"]["legend"] = {"entries": {"emphasis": {"label": "Main path"}}}
        base["meta"].update({
            "locale": "xx",
            "translations": {
                "node.focus.detail": "Focus {label}: {detail}",
                "diagram.description.sequence": "Custom sequence description.",
                "legend.title": "Custom legend",
            },
        })
        profiles = (None, "standard", "showcase")
        for meta_profile in profiles:
            for cli_profile in profiles:
                with self.subTest(meta=meta_profile, cli=cli_profile):
                    doc = copy.deepcopy(base)
                    doc["meta"].pop("quality_profile", None)
                    if meta_profile is not None:
                        doc["meta"]["quality_profile"] = meta_profile
                    source = self.write_doc(doc, f"matrix-{meta_profile}-{cli_profile}.json")
                    opts = () if cli_profile is None else ("--quality", cli_profile)
                    python_output = self.dir / f"python-{meta_profile}-{cli_profile}.html"
                    py_result = self.run_cli("render", source, python_output, *opts, "--json")
                    self.receipt(py_result, "render", True)
                    html = python_output.read_text(encoding="utf-8")
                    for expected in ("Focus Caller", "Custom sequence description.", "Custom legend"):
                        self.assertIn(expected, html)

    def test_unprofiled_and_explicit_standard_border_run_diagnostics_match_node(self):
        doc = sequence()
        doc["segments"] = [{"from": 200, "to": 370, "label": "Boundary"}]
        doc["meta"].pop("quality_profile", None)
        source = self.write_doc(doc, "border-run.json")
        code_sets = {}
        for profile in (None, "standard"):
            with self.subTest(profile=profile):
                opts = () if profile is None else ("--quality", profile)
                python_result = self.run_cli("validate", source, None, *opts, "--json")
                python_receipt = self.receipt(python_result, "validate", python_result.returncode == 0)
                python_codes = {(entry["code"], entry["severity"]) for entry in python_receipt["diagnostics"]}
                code_sets[profile] = python_codes
        self.assertNotIn(("composition/container-border-run", "error"), code_sets[None])
        self.assertIn(("composition/container-border-run", "error"), code_sets["standard"])

    def test_schema_errors_have_codes_and_keep_validate_read_only(self):
        invalid = sequence()
        invalid["participants"][0]["unexpected"] = True
        invalid["messages"][0]["variant"] = "unknown"
        source = self.write_doc(invalid)
        before = set(self.dir.iterdir())
        result = self.run_cli("validate", source, None, "--json")
        receipt = self.receipt(result, "validate", False)
        codes = [item.get("code", "") for item in receipt["diagnostics"]]
        self.assertTrue(codes, receipt)
        self.assertTrue(all(code.startswith("schema/") for code in codes), codes)
        self.assertIn("schema/additionalProperties", codes)
        self.assertIn("schema/enum", codes)
        self.assertEqual(set(self.dir.iterdir()), before, "validate must not create artifacts")

    def test_layout_failures_report_diagnostics_and_preserve_existing_output(self):
        cases = []
        unknown_endpoint = sequence()
        unknown_endpoint["messages"][0]["to"] = "missing"
        cases.append(("unknown-endpoint", unknown_endpoint, "layout/constraint"))
        bad_range = sequence()
        bad_range["segments"] = [{"from": 300, "to": 200, "label": "bad"}]
        cases.append(("bad-range", bad_range, "layout/constraint"))
        bad_activation = sequence()
        bad_activation["activations"] = [{"participant": "service", "from": 310, "to": 260}]
        cases.append(("bad-activation-range", bad_activation, "layout/constraint"))
        unknown_activation = sequence()
        unknown_activation["activations"][0]["participant"] = "missing"
        cases.append(("unknown-activation-reference", unknown_activation, "layout/constraint"))
        crowded = sequence()
        crowded["messages"] = [
            {"from": "caller", "to": "service", "y": 200, "label": "first"},
            {"from": "caller", "to": "store", "y": 210, "label": "second"},
        ]
        cases.append(("crowded", crowded, "layout/constraint"))
        overflow = sequence()
        overflow["participants"][0]["label"] = "A participant label too long for this header"
        cases.append(("label-overflow", overflow, "layout/constraint"))
        duplicate = sequence()
        duplicate["participants"][1]["id"] = "caller"
        cases.append(("duplicate-id", duplicate, "layout/constraint"))

        for name, doc, expected_code in cases:
            with self.subTest(case=name):
                source = self.write_doc(doc, name + ".json")
                output = self.dir / (name + ".html")
                output.write_text("prior artifact", encoding="utf-8")
                receipt = self.receipt(self.run_cli("render", source, output, "--json"), "render", False)
                self.assertTrue(receipt["diagnostics"], receipt)
                codes = {d["code"] for d in receipt["diagnostics"]}
                self.assertIn(expected_code, codes, f"{name}: {codes}")
                self.assertEqual(output.read_text(encoding="utf-8"), "prior artifact")

    def test_delivery_failure_preserves_prior_output_and_success_emits_receipt(self):
        source = self.write_doc(sequence())
        output = self.dir / "delivered.html"
        output.write_text("prior artifact", encoding="utf-8")
        invalid = sequence()
        invalid["messages"][0]["from"] = "absent"
        bad_source = self.write_doc(invalid, "invalid.json")
        failed = self.receipt(self.run_cli("deliver", bad_source, output, "--quality", "standard", "--json"), "deliver", False)
        self.assertTrue(failed["diagnostics"])
        self.assertEqual(output.read_text(encoding="utf-8"), "prior artifact")
        passed = self.receipt(self.run_cli("deliver", source, output, "--quality", "standard", "--json"), "deliver", True)
        self.assertTrue(output.is_file())
        self.assertIn("<svg", output.read_text(encoding="utf-8"))
        self.assertEqual(passed["diagnostics"], [])

    def test_runtime_works_with_empty_path_and_rejects_input_output_alias(self):
        source = self.write_doc(sequence())
        result = self.run_cli("validate", source, None, "--json", empty_path=True)
        self.receipt(result, "validate", True)
        rendered_output = self.dir / "without-node.html"
        rendered = self.run_cli("render", source, rendered_output, "--json", empty_path=True)
        self.receipt(rendered, "render", True)
        delivered_output = self.dir / "without-node-delivered.html"
        delivered = self.run_cli("deliver", source, delivered_output, "--json", empty_path=True)
        self.receipt(delivered, "deliver", True)
        aliased = self.run_cli("render", source, source, "--json")
        receipt = self.receipt(aliased, "render", False)
        self.assertTrue(receipt["diagnostics"], receipt)
        self.assertIn('"schema_version": 1', source.read_text(encoding="utf-8"))

    def test_malformed_unreadable_and_non_object_inputs_have_receipts(self):
        malformed = self.dir / "malformed.json"
        malformed.write_text('{"schema_version":', encoding="utf-8")
        receipt = self.receipt(self.run_cli("validate", malformed, None, "--json"), "validate", False)
        self.assertEqual({item["code"] for item in receipt["diagnostics"]}, {"input/json-parse"})
        directory = self.dir / "directory-input"
        directory.mkdir()
        receipt = self.receipt(self.run_cli("validate", directory, None, "--json"), "validate", False)
        self.assertEqual({item["code"] for item in receipt["diagnostics"]}, {"input/read"})
        for name, value, expected in (("null", None, "schema/type"), ("array", [], "schema/type")):
            source = self.write_doc(value, name + ".json")
            receipt = self.receipt(self.run_cli("validate", source, None, "--json"), "validate", False)
            self.assertIn(expected, {item["code"] for item in receipt["diagnostics"]}, receipt)

    def test_unknown_cli_flags_return_usage_exit_code_two(self):
        source = self.write_doc(sequence())
        result = self.run_cli("validate", source, None, "--json", "--not-supported")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["diagnostics"][0]["code"], "cli/invalid-arguments")

    def test_null_authored_output_matches_portable_path_preflight(self):
        doc = sequence()
        doc["meta"]["output"] = None
        source = self.write_doc(doc)
        python = self.receipt(self.run_cli("validate", source, None, "--json"), "validate", False)
        self.assertEqual(python["diagnostics"][0]["code"], "output/meta-path-syntax")

    def test_repository_evidence_and_remote_brands_fail_as_explicitly_unsupported(self):
        repository = sequence()
        repository["meta"]["repository"] = {"url": "https://example.test/repo", "revision": "a" * 40}
        remote_brand = sequence()
        remote_brand["participants"][0]["brand"] = {"url": "https://example.test/brand.svg", "sha256": "a" * 64}
        for name, doc in (("repository", repository), ("remote-brand", remote_brand)):
            source = self.write_doc(doc, name + ".json")
            receipt = self.receipt(self.run_cli("validate", source, None, "--json"), "validate", False)
            self.assertEqual({entry["code"] for entry in receipt["diagnostics"]}, {"python/unsupported-feature"})

    def test_unknown_and_unpinned_brands_match_node_diagnostic_codes(self):
        cases = (
            ("unknown-brand", "brand-that-does-not-exist", "brand/unknown"),
            ("unpinned-brand", "https://example.test/logo.svg", "brand/unpinned-url"),
        )
        for name, brand, expected_code in cases:
            with self.subTest(brand=name):
                doc = sequence()
                doc["participants"][0]["brand"] = brand
                source = self.write_doc(doc, name + ".json")
                python_result = self.run_cli("validate", source, None, "--json")
                python_receipt = self.receipt(python_result, "validate", False)
                python_codes = {entry["code"] for entry in python_receipt["diagnostics"]}
                self.assertIn(expected_code, python_codes)

    def test_symlink_and_hardlink_aliases_never_replace_input_or_target(self):
        source = self.write_doc(sequence())
        source_bytes = source.read_bytes()
        hardlink = self.dir / "hardlink.html"
        os.link(source, hardlink)
        result = self.run_cli("render", source, hardlink, "--json")
        receipt = self.receipt(result, "render", False)
        self.assertIn("output/input-alias", {item["code"] for item in receipt["diagnostics"]})
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(hardlink.read_bytes(), source_bytes)

        target = self.dir / "protected.html"
        target.write_text("preserve target", encoding="utf-8")
        link = self.dir / "linked.html"
        link.symlink_to(target)
        result = self.run_cli("render", source, link, "--json")
        receipt = self.receipt(result, "render", False)
        self.assertTrue(receipt["diagnostics"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve target")

    def test_foreign_delivery_lock_and_production_sidecars_are_preserved(self):
        source = self.write_doc(sequence())
        output = self.dir / "locked.html"
        lock = self.dir / ".archify-delivery-lock.json"
        lock.write_text('{"owner":"another attempt"}', encoding="utf-8")
        result = self.run_cli("deliver", source, output, "--json")
        receipt = self.receipt(result, "deliver", False)
        self.assertIn("delivery/concurrent-attempt", {item["code"] for item in receipt["diagnostics"]})
        self.assertEqual(lock.read_text(encoding="utf-8"), '{"owner":"another attempt"}')
        self.assertFalse(output.exists())

        lock.unlink()
        output.write_text("prior artifact", encoding="utf-8")
        sidecar = output.with_suffix(".delivery.json")
        sidecar.write_text('{"schemaVersion":1,"status":"current"}', encoding="utf-8")
        result = self.run_cli("deliver", source, output, "--json")
        receipt = self.receipt(result, "deliver", False)
        self.assertIn("python/unsupported-feature", {item["code"] for item in receipt["diagnostics"]})
        self.assertEqual(output.read_text(encoding="utf-8"), "prior artifact")
        self.assertEqual(sidecar.read_text(encoding="utf-8"), '{"schemaVersion":1,"status":"current"}')


if __name__ == "__main__":
    unittest.main()
