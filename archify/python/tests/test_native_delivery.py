"""Native finalize/check delivery contract and receipt integrity."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = ROOT / "archify/bin/archify.py"
PYTHON = os.environ.get("ARCHIFY_PYTHON", sys.executable)
SHOWCASE_EXAMPLES = {
    "sequence": "async-job-roundtrip.sequence.json",
    "dataflow": "event-stream.dataflow.json",
    "lifecycle": "agent-run.lifecycle.json",
    "architecture": "brand-aware-delivery.architecture.json",
    "workflow": "agent-tool-call.workflow.json",
}


def load_cli():
    spec = importlib.util.spec_from_file_location("archify_native_delivery_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archify-native-delivery-", dir=str(Path("/tmp").resolve()))
        self.dir = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def example(self, diagram_type):
        return ROOT / "archify/examples" / SHOWCASE_EXAMPLES[diagram_type]

    def run_cli(self, command, diagram_type, source, output, *extra):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "archify/python")
        env.pop("ARCHIFY_QUALITY_PROFILE", None)
        env["PATH"] = ""
        result = subprocess.run(
            [PYTHON, str(CLI_PATH), command, diagram_type, str(source), str(output),
             "--quality", "showcase", "--json", *extra],
            cwd=ROOT, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.stderr, "", result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if receipt["ok"] else 1, receipt)
        return result, receipt

    def receipt_path(self, output):
        return output.with_name(output.stem + ".python-delivery.json")

    def test_showcase_finalize_defaults_to_static_delivery_and_check_accepts_receipt(self):
        for diagram_type, filename in SHOWCASE_EXAMPLES.items():
            with self.subTest(diagram_type=diagram_type):
                source = self.example(diagram_type)
                output = self.dir / (diagram_type + ".html")
                result, receipt = self.run_cli("finalize", diagram_type, source, output)
                self.assertEqual(result.returncode, 0)
                self.assertTrue(receipt["ok"], receipt)
                self.assertEqual(receipt["browserEvidence"]["status"], "skipped")
                self.assertEqual(receipt["browserEvidence"]["reason"], "disabled-by-policy")
                self.assertEqual(receipt["staticChecks"]["status"], "passed")
                self.assertEqual(receipt["artifactIntegrity"]["status"], "passed")
                artifact = output.read_bytes()
                sidecar = json.loads(self.receipt_path(output).read_text(encoding="utf-8"))
                self.assertEqual(sidecar["artifact"], {
                    "sha256": hashlib.sha256(artifact).hexdigest(), "bytes": len(artifact),
                })
                checked_result, checked = self.run_cli("check", diagram_type, source, output)
                self.assertEqual(checked_result.returncode, 0)
                self.assertTrue(checked["ok"], checked)
                self.assertEqual(checked["artifactIntegrity"]["status"], "passed")

    def test_check_rejects_html_and_candidate_tampering(self):
        source = self.dir / "candidate.sequence.json"
        source.write_bytes(self.example("sequence").read_bytes())
        output = self.dir / "tamper.html"
        self.run_cli("finalize", "sequence", source, output)
        original_html = output.read_bytes()
        output.write_bytes(original_html + b"\n<!-- changed -->\n")
        result, receipt = self.run_cli("check", "sequence", source, output)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(receipt["ok"])
        self.assertIn("integrity/artifact-mismatch", {item["code"] for item in receipt["diagnostics"]})

        output.write_bytes(original_html)
        original_source = source.read_bytes()
        try:
            source.write_bytes(original_source + b"\n")
            result, receipt = self.run_cli("check", "sequence", source, output)
        finally:
            source.write_bytes(original_source)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(receipt["ok"])
        self.assertIn("integrity/specification-mismatch", {item["code"] for item in receipt["diagnostics"]})

    def test_malformed_or_foreign_sidecar_conflict_preserves_existing_files(self):
        for content in (b"{malformed", json.dumps({"deliveryContract": "foreign"}).encode()):
            with self.subTest(sidecar=content):
                source = self.example("sequence")
                output = self.dir / "conflict.html"
                sidecar = self.receipt_path(output)
                prior_html = b"existing html owned by another delivery\n"
                output.write_bytes(prior_html)
                sidecar.write_bytes(content)
                result, receipt = self.run_cli("finalize", "sequence", source, output)
                self.assertEqual(result.returncode, 1)
                self.assertIn("integrity/receipt-conflict", {item["code"] for item in receipt["diagnostics"]})
                self.assertEqual(output.read_bytes(), prior_html)
                self.assertEqual(sidecar.read_bytes(), content)

    def test_unchanged_owned_delivery_can_be_finalized_again(self):
        source = self.example("sequence")
        output = self.dir / "redeliver.html"
        _, first = self.run_cli("finalize", "sequence", source, output)
        first_receipt = json.loads(self.receipt_path(output).read_text(encoding="utf-8"))
        _, second = self.run_cli("finalize", "sequence", source, output)
        second_receipt = json.loads(self.receipt_path(output).read_text(encoding="utf-8"))
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first_receipt["artifact"], second_receipt["artifact"])
        self.assertNotEqual(first_receipt["receiptId"], second_receipt["receiptId"])

    def test_check_is_blocked_by_delivery_marker(self):
        source = self.example("sequence")
        output = self.dir / "locked.html"
        self.run_cli("finalize", "sequence", source, output)
        marker = self.dir / ".archify-delivery-lock.json"
        marker.write_text('{"schemaVersion":1,"receiptId":"other"}', encoding="utf-8")
        result, receipt = self.run_cli("check", "sequence", source, output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("integrity/delivery-in-progress", {item["code"] for item in receipt["diagnostics"]})
        self.assertTrue(marker.exists())

    def test_check_rejects_missing_and_symlink_receipts(self):
        source = self.example("sequence")
        output = self.dir / "unsafe-receipt.html"
        self.run_cli("finalize", "sequence", source, output)
        sidecar = self.receipt_path(output)
        sidecar.unlink()
        result, receipt = self.run_cli("check", "sequence", source, output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("integrity/read", {item["code"] for item in receipt["diagnostics"]})

        self.run_cli("finalize", "sequence", source, output)
        sidecar.unlink()
        sidecar.symlink_to(output)
        result, receipt = self.run_cli("check", "sequence", source, output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("integrity/unsafe-file", {item["code"] for item in receipt["diagnostics"]})

    def test_check_rejects_receipt_output_specification_and_type_mismatches(self):
        source = self.example("sequence")
        output = self.dir / "binding.html"
        self.run_cli("finalize", "sequence", source, output)
        sidecar = self.receipt_path(output)
        original = json.loads(sidecar.read_text(encoding="utf-8"))
        for key, value, expected_code, requested_type in (
            ("output", str(self.dir / "other.html"), "integrity/receipt-invalid", "sequence"),
            ("input", str(self.dir / "other.sequence.json"), "integrity/receipt-invalid", "sequence"),
            ("type", "dataflow", "integrity/receipt-invalid", "sequence"),
        ):
            with self.subTest(field=key):
                altered = dict(original)
                altered[key] = value
                sidecar.write_text(json.dumps(altered), encoding="utf-8")
                result, receipt = self.run_cli("check", requested_type, source, output)
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_code, {item["code"] for item in receipt["diagnostics"]})
        sidecar.write_text(json.dumps(original), encoding="utf-8")

    def run_mocked_browser(self, policy, browser_result, output_name):
        source = self.example("sequence")
        output = self.dir / output_name
        cli = load_cli()
        stdout = io.StringIO()
        with patch("archify_runtime.browser.check_browser", return_value=browser_result):
            with contextlib.redirect_stdout(stdout):
                status = cli.main(["finalize", "sequence", str(source), str(output), "--json", "--browser", policy])
        return status, json.loads(stdout.getvalue()), output

    def assert_static_receipt_exists_after_browser_result(self, output, browser_status):
        sidecar = json.loads(self.receipt_path(output).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["browserEvidence"]["status"], browser_status)
        self.assertEqual(sidecar["staticChecks"]["status"], "passed")
        self.assertEqual(sidecar["artifactIntegrity"]["status"], "passed")
        return sidecar

    def test_publication_verifier_rejects_corrupted_target(self):
        cli = load_cli()
        from archify_runtime import output as output_module

        real_publish = output_module.publish

        def corrupt_before_verification(target, html, expected, command, receipt_id,
                                        receipt=None, verifier=None):
            def corrupt_then_verify():
                target.write_bytes(target.read_bytes() + b"\n<!-- injected corruption -->\n")
                return verifier()

            return real_publish(target, html, expected, command, receipt_id,
                                receipt=receipt, verifier=corrupt_then_verify)

        source = self.example("sequence")
        output = self.dir / "publication-corruption.html"
        stdout = io.StringIO()
        with patch("archify_runtime.output.publish", side_effect=corrupt_before_verification):
            with contextlib.redirect_stdout(stdout):
                status = cli.main(["finalize", "sequence", str(source), str(output), "--json"])
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(receipt["ok"])
        self.assertIn("integrity/artifact-mismatch", {item["code"] for item in receipt["diagnostics"]})
        self.assertTrue(output.is_file())
        self.assertFalse(self.receipt_path(output).exists())

    def test_browser_policies_keep_static_artifact_and_apply_required_gate(self):
        skipped = {"status": "skipped", "reason": "playwright-unavailable"}
        status, receipt, output = self.run_mocked_browser("off", {"status": "skipped", "reason": "disabled-by-policy"}, "browser-off.html")
        self.assertEqual(status, 0)
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(output.is_file())

        status, receipt, output = self.run_mocked_browser("auto", skipped, "browser-auto.html")
        self.assertEqual(status, 0)
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(output.is_file())

        status, receipt, output = self.run_mocked_browser("required", skipped, "browser-required.html")
        self.assertEqual(status, 1)
        self.assertFalse(receipt["ok"], receipt)
        self.assertTrue(output.is_file())
        self.assertEqual(receipt["generation"], "passed")
        required_sidecar = self.assert_static_receipt_exists_after_browser_result(output, "skipped")
        self.assertFalse(required_sidecar["ok"])
        self.assertNotEqual(required_sidecar["browserEvidence"]["status"], "passed")

        status, receipt, output = self.run_mocked_browser("required", {"status": "passed"}, "browser-passed.html")
        self.assertEqual(status, 0)
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(output.is_file())
        passed_sidecar = self.assert_static_receipt_exists_after_browser_result(output, "passed")
        self.assertTrue(passed_sidecar["ok"])

        failed = {"status": "failed", "diagnostics": [{"code": "browser/runtime-failed", "severity": "error"}]}
        status, receipt, output = self.run_mocked_browser("auto", failed, "browser-failed.html")
        self.assertEqual(status, 1)
        self.assertFalse(receipt["ok"], receipt)
        self.assertTrue(output.is_file())
        self.assertEqual(receipt["generation"], "passed")
        failed_sidecar = self.assert_static_receipt_exists_after_browser_result(output, "failed")
        self.assertFalse(failed_sidecar["ok"])
        self.assertNotEqual(failed_sidecar["browserEvidence"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
