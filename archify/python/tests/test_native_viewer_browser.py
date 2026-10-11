"""Opt-in real Chromium regressions for the standalone Viewer."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = {
    "architecture": "web-app.architecture.json",
    "workflow": "agent-tool-call.workflow.json",
    "sequence": "cache-miss-request.sequence.json",
    "dataflow": "product-analytics.dataflow.json",
    "lifecycle": "deployment-release.lifecycle.json",
}
OPTED_IN = os.environ.get("ARCHIFY_BROWSER_TESTS") == "1"


@unittest.skipUnless(
    OPTED_IN,
    "real Chromium Viewer tests skipped; set ARCHIFY_BROWSER_TESTS=1 to run the browser gate",
)
class NativeViewerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "ARCHIFY_BROWSER_TESTS=1 requires Playwright; install archify/python/requirements-browser.txt"
            ) from error

        output_root = Path(os.environ.get("ARCHIFY_BROWSER_TEST_OUT", tempfile.gettempdir())).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="archify-viewer-browser-", dir=str(output_root))
        cls.output = Path(cls.temporary.name)
        cls.artifacts = {}
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(ROOT / "archify/python")
        environment.pop("ARCHIFY_QUALITY_PROFILE", None)
        for mode, filename in EXAMPLES.items():
            source = ROOT / "archify/examples" / filename
            artifact = cls.output / (mode + ".html")
            command = [
                sys.executable,
                str(ROOT / "archify/bin/archify.py"),
                "finalize",
                mode,
                str(source),
                str(artifact),
                "--quality",
                "showcase",
                "--browser",
                "off",
                "--json",
            ]
            result = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(
                    f"could not prepare {mode} Viewer fixture: exit={result.returncode}, stderr={result.stderr.strip()}"
                )
            cls.artifacts[mode] = artifact

        cls.playwright = sync_playwright().start()
        executable = Path(cls.playwright.chromium.executable_path)
        if not executable.is_file():
            cls.playwright.stop()
            raise RuntimeError(
                f"Chromium is unavailable at {executable}; install it with `python -m playwright install chromium`"
            )
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def open_page(self, artifact):
        context = self.browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(artifact.resolve().as_uri(), wait_until="load")
        page.locator(".diagram-container > svg").wait_for(state="visible")
        return context, page

    def test_focus_and_reach_trace(self):
        context, page = self.open_page(self.artifacts["architecture"])
        try:
            origin = page.evaluate(
                """() => {
                  const svg = document.querySelector('.diagram-container > svg');
                  const nodes = Array.from(svg.querySelectorAll('[data-node-id]'));
                  const edges = Array.from(svg.querySelectorAll('[data-edge-from][data-edge-to]'));
                  const node = nodes.find(candidate => edges.some(edge => edge.getAttribute('data-edge-from') === candidate.getAttribute('data-node-id')));
                  return node ? { id: node.getAttribute('data-node-id'), label: node.getAttribute('data-node-label') } : null;
                }"""
            )
            self.assertIsNotNone(origin, "architecture fixture must contain an outgoing relationship")
            page.locator("#btn-node-finder").click()
            page.locator("#node-finder-input").fill(origin["label"])
            results = page.locator("#node-finder-results button[data-node-id]")
            results.first.wait_for(state="visible")
            selected = results.first
            self.assertEqual(selected.get_attribute("data-node-id"), origin["id"])
            selected.click()
            self.assertEqual(page.locator("#focus-id").inner_text(), origin["id"])
            page.locator("#focus-chip").wait_for(state="visible")
            reach = page.locator("#btn-reach-downstream")
            self.assertFalse(reach.is_disabled(), "selected node must have downstream relationships")
            reach.click()
            self.assertEqual(reach.get_attribute("aria-pressed"), "true")
            page.wait_for_function("document.querySelector('.diagram-container > svg').hasAttribute('data-reach-active')")
            self.assertTrue(page.locator("#focus-reach-status").is_visible())
        finally:
            context.close()

    def test_theme_toggle_updates_the_document_theme(self):
        context, page = self.open_page(self.artifacts["sequence"])
        try:
            root = page.locator("html")
            initial = root.get_attribute("data-theme")
            self.assertIn(initial, ("dark", "light"))
            page.locator("#btn-theme").click()
            page.wait_for_function("initial => document.documentElement.getAttribute('data-theme') !== initial", arg=initial)
            self.assertNotEqual(root.get_attribute("data-theme"), initial)
        finally:
            context.close()

    def test_svg_and_png_exports_download_valid_files(self):
        context, page = self.open_page(self.artifacts["architecture"])
        try:
            for format_name, header in (("svg", b"<svg"), ("png", b"\x89PNG\r\n\x1a\n")):
                with self.subTest(format=format_name):
                    page.locator("#btn-export").click()
                    target = self.output / f"export.{format_name}"
                    with page.expect_download(timeout=30000) as download_info:
                        page.locator(f'#export-menu button[data-format="{format_name}"]').click()
                    download_info.value.save_as(str(target))
                    content = target.read_bytes()
                    self.assertGreater(len(content), len(header))
                    if format_name == "svg":
                        self.assertIn(header, content[:512])
                    else:
                        self.assertTrue(content.startswith(header), format_name)
                    target.unlink()
        finally:
            context.close()

    def test_all_diagram_modes_load_without_external_requests(self):
        requests = []
        for mode, artifact in self.artifacts.items():
            with self.subTest(mode=mode):
                context = self.browser.new_context()
                def deny_external(route):
                    requests.append(route.request.url)
                    route.abort()

                context.route("http://**/*", deny_external)
                context.route("https://**/*", deny_external)
                page = context.new_page()
                page.goto(artifact.resolve().as_uri(), wait_until="load")
                page.locator(".diagram-container > svg").wait_for(state="visible")
                self.assertEqual(page.evaluate("document.querySelectorAll('script[src],link[rel=stylesheet][href]').length"), 0)
                context.close()
        self.assertEqual(requests, [], "standalone Viewer must not request network assets")

    def test_all_diagram_modes_start_without_javascript_errors(self):
        errors = []
        for mode, artifact in self.artifacts.items():
            with self.subTest(mode=mode):
                context = self.browser.new_context()
                page = context.new_page()
                page.on("pageerror", lambda error, current=mode: errors.append((current, str(error))))
                page.on(
                    "console",
                    lambda message, current=mode: errors.append((current, message.text)) if message.type == "error" else None,
                )
                page.goto(artifact.resolve().as_uri(), wait_until="load")
                page.locator(".diagram-container > svg").wait_for(state="visible")
                context.close()
        self.assertEqual(errors, [], f"unexpected Viewer JavaScript errors: {errors}")


if __name__ == "__main__":
    unittest.main()
