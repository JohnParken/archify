"""Opt-in Chromium-to-ffmpeg regression for WebM motion exports."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
OPTED_IN = os.environ.get("ARCHIFY_WEBM_TESTS") == "1"


@unittest.skipUnless(
    OPTED_IN,
    "real WebM decode test skipped; set ARCHIFY_WEBM_TESTS=1 to run the WebM gate",
)
class NativeWebmBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured_ffmpeg = os.environ.get("ARCHIFY_FFMPEG")
        cls.ffmpeg = shutil.which(configured_ffmpeg) if configured_ffmpeg else shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise RuntimeError(
                "ARCHIFY_WEBM_TESTS=1 requires ffmpeg; install ffmpeg or set ARCHIFY_FFMPEG"
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "ARCHIFY_WEBM_TESTS=1 requires Playwright; install archify/python/requirements-browser.txt"
            ) from error

        output_root = Path(
            os.environ.get("ARCHIFY_WEBM_TEST_OUT", tempfile.gettempdir())
        ).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="archify-webm-browser-", dir=str(output_root)
        )
        cls.output = Path(cls.temporary.name)
        try:
            cls.artifact = cls._render_animated_fixture()
            cls.playwright = sync_playwright().start()
            executable = Path(cls.playwright.chromium.executable_path)
            if not executable.is_file():
                raise RuntimeError(
                    f"Chromium is unavailable at {executable}; install it with `python -m playwright install chromium`"
                )
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls._close_resources()
            raise

    @classmethod
    def _render_animated_fixture(cls):
        source = ROOT / "archify/examples/web-app.architecture.json"
        document = json.loads(source.read_text(encoding="utf-8"))
        document.setdefault("meta", {})["animation"] = "trace"
        input_path = cls.output / "animated.architecture.json"
        artifact = cls.output / "animated.html"
        input_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(ROOT / "archify/python")
        environment.pop("ARCHIFY_QUALITY_PROFILE", None)
        command = [
            sys.executable,
            str(ROOT / "archify/bin/archify.py"),
            "finalize",
            "architecture",
            str(input_path),
            str(artifact),
            "--quality",
            "showcase",
            "--browser",
            "off",
            "--json",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "could not prepare animated architecture fixture: "
                f"exit={result.returncode}, stderr={result.stderr.strip()}"
            )
        return artifact

    @classmethod
    def _close_resources(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
            del cls.browser
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
            del cls.playwright
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()
            del cls.temporary

    @classmethod
    def tearDownClass(cls):
        cls._close_resources()

    def test_motion_webm_decodes_to_multiple_distinct_frames(self):
        page = self.browser.new_page()
        try:
            page.set_default_timeout(30000)
            page.goto(self.artifact.resolve().as_uri(), wait_until="load")
            page.locator(".diagram-container > svg").wait_for(state="visible")
            self.assertTrue(
                page.evaluate(
                    "!!(window.Archify && Archify.motion && Archify.motion.canRecord())"
                ),
                "animated Viewer must support WebM recording in Chromium",
            )
            payload = page.evaluate(
                """async () => {
                  try {
                    const blob = await Archify.motion.recordWebm({ duration: 1400, fps: 12 });
                    const bytes = new Uint8Array(await blob.arrayBuffer());
                    let binary = '';
                    for (let offset = 0; offset < bytes.length; offset += 32768) {
                      binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
                    }
                    return { ok: true, type: blob.type, size: blob.size, base64: btoa(binary) };
                  } catch (error) {
                    return { ok: false, error: String(error && error.message || error) };
                  }
                }"""
            )
            self.assertTrue(payload.get("ok"), payload.get("error", "WebM recording failed"))
            self.assertRegex(payload.get("type", ""), r"^video/webm(?:;|$)")
            encoded = payload.get("base64", "")
            self.assertTrue(encoded, "WebM recording returned no bytes")
            webm = base64.b64decode(encoded, validate=True)
            self.assertEqual(webm[:4], b"\x1aE\xdf\xa3", "WebM EBML header is missing")
            self.assertGreater(len(webm), 10_000, f"WebM is unexpectedly small ({len(webm)} bytes)")

            webm_path = self.output / "motion.webm"
            webm_path.write_bytes(webm)
            decoded = subprocess.run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(webm_path),
                    "-vf",
                    "fps=6,scale=320:-2",
                    "-f",
                    "framemd5",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                decoded.returncode,
                0,
                f"ffmpeg could not decode the WebM artifact: {decoded.stderr.strip()}",
            )
            hashes = [
                line.split(",")[-1].strip()
                for line in decoded.stdout.splitlines()
                if line and not line.startswith("#")
            ]
            self.assertGreaterEqual(len(hashes), 4, f"decoded only {len(hashes)} WebM frames")
            self.assertGreaterEqual(len(set(hashes)), 2, "decoded WebM frames are static")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
