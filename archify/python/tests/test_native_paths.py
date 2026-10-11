"""Native host paths, portable authored outputs and receipt ownership."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from archify_runtime.cli import main
from archify_runtime.common import DiagnosticError
from archify_runtime.delivery import publish_transaction, sidecar
from archify_runtime.integrity import CONTRACT, identity, receipt_path
from archify_runtime.output import canonical_path, resolve_output, snapshot

ROOT = Path(__file__).resolve().parents[2]


class NativePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archify-paths-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.directory = Path(self.temp.name)
        self.source = self.directory / "candidate.json"
        self.source.write_bytes((ROOT / "examples/web-app.architecture.json").read_bytes())
        self.output = self.directory / "diagram.html"

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        text = StringIO()
        with redirect_stdout(text):
            status = main([*map(str, args), "--json"])
        return status, json.loads(text.getvalue())

    def test_existing_short_name_ancestor_accepts_relative_and_absolute_output(self):
        parent = self.directory / "RUNNER~1"
        parent.mkdir()
        source = parent / "candidate.json"
        source.write_bytes(self.source.read_bytes())
        previous = Path.cwd()
        try:
            os.chdir(parent)
            for name in ("relative.html", str(parent / "absolute.html")):
                status, result = self.invoke("finalize", "architecture", source, name)
                self.assertEqual(status, 0, result)
                self.assertTrue(Path(name).is_file())
                self.assertEqual(self.invoke("finalize", "architecture", source, name)[0], 0)
        finally:
            os.chdir(previous)

    def test_cli_syntax_diagnostics_do_not_blame_meta_output(self):
        for name in ("CON.html", "bad?.html", "bad:stream.html", "trailing./diagram.html", "NEW~1/diagram.html"):
            with self.subTest(name=name):
                status, result = self.invoke("finalize", "architecture", self.source, self.directory / name)
                self.assertEqual(status, 1, result)
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "output/cli-path-syntax")
                self.assertNotEqual(diagnostic["subject"].get("path"), "/meta/output")
                self.assertNotIn("meta.output", diagnostic["message"])
                self.assertFalse((self.directory / name).exists())

    def test_authored_output_keeps_portable_path_diagnostics(self):
        diagram = json.loads(self.source.read_bytes())
        diagram["meta"]["output"] = "RUNNER~1/diagram.html"
        self.source.write_text(json.dumps(diagram))
        status, result = self.invoke("finalize", "architecture", self.source)
        self.assertEqual(status, 1, result)
        diagnostic = next(d for d in result["diagnostics"] if d["code"] == "output/meta-path-syntax")
        self.assertEqual(diagnostic["subject"]["path"], "/meta/output")

    def test_explicit_non_html_output_uses_cli_extension_diagnostic(self):
        for requested in ("diagram.svg", "diagram.txt"):
            with self.subTest(requested=requested):
                with self.assertRaises(DiagnosticError) as raised:
                    resolve_output(self.directory / requested, None, self.source)
                self.assertEqual(raised.exception.diagnostics[0]["code"], "output/cli-extension")

    def directory_alias(self):
        alias = self.directory / "alias"
        real = self.directory / "real"
        real.mkdir()
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Directory symlinks unavailable: {error}")
        return alias, real

    def test_alias_inputs_support_check_and_repeated_publication_from_physical_cwd(self):
        alias, real = self.directory_alias()
        (real / "candidate.json").write_bytes(self.source.read_bytes())
        status, result = self.invoke("finalize", "architecture", alias / "candidate.json", alias / "diagram.html")
        self.assertEqual(status, 0, result)
        self.assertEqual(result["input"], str(real / "candidate.json"))
        previous = Path.cwd()
        try:
            os.chdir(real)
            for command in ("check", "finalize"):
                status, result = self.invoke(command, "architecture", "candidate.json", "diagram.html")
                self.assertEqual(status, 0, result)
        finally:
            os.chdir(previous)

    def test_canonicalization_preserves_final_symlink_rejection(self):
        alias = self.directory / "linked.json"
        try:
            alias.symlink_to(self.source)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"File symlinks unavailable: {error}")
        self.assertEqual(canonical_path(alias), alias)
        status, result = self.invoke("finalize", "architecture", alias, self.output)
        self.assertEqual(status, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "input/read")
        self.assertFalse(self.output.exists())
        with self.assertRaises(DiagnosticError):
            resolve_output(alias, None, self.source)

    def test_receipts_with_existing_directory_alias_spelling_remain_usable(self):
        alias, real = self.directory_alias()
        source, output = real / "candidate.json", real / "diagram.html"
        source.write_bytes(self.source.read_bytes())
        self.assertEqual(self.invoke("finalize", "architecture", source, output)[0], 0)
        for path in (receipt_path(output), sidecar(output, ".delivery.json")):
            saved = json.loads(path.read_bytes())
            saved["input"] = str(alias / "candidate.json")
            path.write_text(json.dumps(saved))
        status, result = self.invoke("check", "architecture", source, output)
        self.assertEqual(status, 0, result)
        status, result = self.invoke("finalize", "architecture", source, output)
        self.assertEqual(status, 0, result)

    def test_compare_receipt_cannot_replace_either_input_through_directory_alias(self):
        alias, real = self.directory_alias()
        base, head = real / "base.json", real / "head.json"
        base.write_bytes(self.source.read_bytes())
        head.write_bytes(self.source.read_bytes())
        for name in ("base.json", "head.json"):
            before = (base.read_bytes(), head.read_bytes())
            status, result = self.invoke("compare", "architecture", alias / base.name, alias / head.name,
                self.output, "--receipt", alias / name)
            self.assertNotEqual(status, 0, result)
            self.assertEqual(before, (base.read_bytes(), head.read_bytes()))
            self.assertFalse(self.output.exists())

    def test_compare_republication_rejects_different_base_owner(self):
        base = self.directory / "base.json"
        other = self.directory / "other-base.json"
        base.write_bytes(self.source.read_bytes())
        other.write_bytes(self.source.read_bytes())
        self.assertEqual(self.invoke("compare", "architecture", base, self.source, self.output)[0], 0)
        prior = [p.read_bytes() for p in (self.output, receipt_path(self.output), sidecar(self.output, ".receipt.json"))]
        status, result = self.invoke("compare", "architecture", other, self.source, self.output)
        self.assertEqual(status, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "integrity/receipt-conflict")
        self.assertEqual(prior, [p.read_bytes() for p in (self.output, receipt_path(self.output), sidecar(self.output, ".receipt.json"))])
        self.assertEqual(self.invoke("compare", "architecture", base, self.source, self.output)[0], 0)

    def test_transaction_rejects_base_input_alias_before_writes(self):
        alias, real = self.directory_alias()
        base = real / "base.json"
        base.write_bytes(b"base")
        receipt = {"schemaVersion": 1, "deliveryContract": CONTRACT, "type": "architecture",
            "input": str(self.source), "baseInput": str(alias / "base.json"),
            "output": str(self.output), "specification": identity(self.source.read_bytes()),
            "artifact": identity(b"html"), "requestedReceipt": str(base)}
        with self.assertRaises(DiagnosticError) as raised:
            publish_transaction(self.output, b"html", snapshot(self.output), "01234567-abcd", receipt, None)
        self.assertEqual(raised.exception.diagnostics[0]["code"], "output/input-alias")
        self.assertEqual(base.read_bytes(), b"base")
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows native drive-relative syntax")
    def test_drive_relative_output_is_rejected(self):
        with self.assertRaises(DiagnosticError) as raised:
            resolve_output("C:diagram.html", None, self.source)
        self.assertEqual(raised.exception.diagnostics[0]["code"], "output/cli-path-syntax")
