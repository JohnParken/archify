"""Unit coverage for the optional browser gate without requiring Chromium."""
import builtins
import sys
import types
import unittest
from unittest.mock import patch

from archify_runtime.browser import VIEWPORTS, check_browser


class FakePage:
    def __init__(self, overflow=False, page_error=False):
        self.handlers = {}
        self.theme = "dark"
        self.width = 1440
        self.overflow = overflow
        self.page_error = page_error
    def set_default_timeout(self, value): pass
    def set_default_navigation_timeout(self, value): pass
    def on(self, event, callback): self.handlers[event] = callback
    def goto(self, *args, **kwargs):
        if self.page_error: self.handlers["pageerror"]("fixture exception")
    def evaluate(self, script):
        if "documentWidth" in script:
            width = self.width
            return {"documentWidth": width + (1 if self.overflow else 0), "viewportWidth": width,
                    "svgFound": True, "svgLeft": 0, "svgRight": width,
                    "minTextPx": 6.5, "theme": self.theme}
        return True
    def set_viewport_size(self, size): self.width = size["width"]
    def locator(self, selector): return FakeLocator(self, selector)
    def wait_for_function(self, script, arg=None, timeout=None):
        if self.theme != arg: raise AssertionError("theme did not reach expected state")


class FakeLocator:
    def __init__(self, page, selector): self.page, self.selector = page, selector
    def count(self): return 1
    def click(self): self.page.theme = "light" if self.page.theme == "dark" else "dark"
    def get_attribute(self, key): return self.page.theme


def fake_playwright(page, executable_path=__file__):
    class Browser:
        def new_page(self): return page
        def close(self): pass
    class Chromium:
        def __init__(self): self.executable_path = executable_path
        def launch(self, **kwargs): return Browser()
    class Manager:
        chromium = Chromium()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: Manager()
    return {"playwright": types.ModuleType("playwright"), "playwright.sync_api": module}


class BrowserGateTests(unittest.TestCase):
    def test_off_skips_without_importing_playwright(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith("playwright"): raise AssertionError("playwright imported for off policy")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded):
            result = check_browser(b"<html>", "off")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "disabled-by-policy")

    def test_missing_optional_module_is_skipped(self):
        with patch("builtins.__import__", side_effect=ModuleNotFoundError("No module named playwright")):
            result = check_browser(b"<html>", "auto")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("playwright-unavailable", result["reason"])

    def test_missing_binary_is_skipped_by_preflight(self):
        with patch.dict(sys.modules, fake_playwright(FakePage(), "/definitely/missing/chromium")):
            result = check_browser(b"<html>", "required")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("chromium-unavailable", result["reason"])

    def test_success_checks_all_light_viewports_and_dark_endpoints(self):
        page = FakePage()
        with patch.dict(sys.modules, fake_playwright(page)):
            result = check_browser(b"<html>", "required")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual([m["viewport"] for m in result["measurements"][:4]], [list(v) for v in VIEWPORTS])
        self.assertEqual([m["theme"] for m in result["measurements"][-2:]], ["dark", "dark"])

    def test_overflow_and_pageerror_are_structured_diagnostics(self):
        page = FakePage(overflow=True, page_error=True)
        with patch.dict(sys.modules, fake_playwright(page)):
            result = check_browser(b"<html>", "required")
        self.assertEqual(result["status"], "failed")
        codes = {entry["code"] for entry in result["diagnostics"]}
        self.assertIn("browser/horizontal-overflow", codes)
        self.assertIn("browser/page-error", codes)
