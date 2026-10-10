"""Optional, bounded Chromium checks for rendered Archify HTML."""

import tempfile
from pathlib import Path

from .common import diagnostic

VIEWPORTS = ((1440, 900), (1600, 1000), (1920, 1080), (2048, 1320))

MEASURE_SCRIPT = """() => {
                          const svg = document.querySelector('.diagram-container > svg');
                          const r=svg?.getBoundingClientRect();
                          const labels = svg ? [...svg.querySelectorAll('text')].filter(e => {
                            const semantic=e.hasAttribute('data-node-label') || e.hasAttribute('data-boundary-label') ||
                              (e.closest('[data-detail="context"]') && e.closest('[data-node-id],[data-edge-from]'));
                            return semantic && !e.closest('[data-detail="fine"]') && e.getClientRects().length &&
                              getComputedStyle(e).visibility !== 'hidden';
                          }) : [];
                          const px = labels.map(e => {
                            const c=e.getScreenCTM(); if (!c) return 0;
                            const scale=Math.min(Math.hypot(c.a,c.b),Math.hypot(c.c,c.d));
                            return parseFloat(getComputedStyle(e).fontSize)*scale;
                          });
                          return {documentWidth: document.documentElement.scrollWidth,
                            viewportWidth: innerWidth, svgFound: !!svg && r.width > 0 && r.height > 0,
                            svgLeft: r?.left ?? null, svgRight: r?.right ?? null,
                            minTextPx: px.length && px.every(Number.isFinite) ? Math.min(...px) : null,
                            theme: document.documentElement.getAttribute('data-theme')};
                        }"""


def _result(policy, status, reason=None, diagnostics=None, measurements=None):
    return {"policy": policy, "status": status, "reason": reason,
            "diagnostics": diagnostics or [], "measurements": measurements or []}


def check_browser(html: bytes, policy: str) -> dict:
    """Check a stable HTML snapshot using optional headless Playwright Chromium."""
    if policy == "off":
        return _result(policy, "skipped", "disabled-by-policy")
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError) as exc:
        return _result(policy, "skipped", f"playwright-unavailable: {exc}")

    diagnostics, measurements = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="archify-browser-") as directory:
            snapshot = Path(directory) / "snapshot.html"
            snapshot.write_bytes(bytes(html))
            with sync_playwright() as playwright:
                executable = Path(playwright.chromium.executable_path)
                if not executable.is_file():
                    return _result(policy, "skipped", f"chromium-unavailable: executable missing at {executable}")
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_default_timeout(10000)
                    page.set_default_navigation_timeout(10000)
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(snapshot.as_uri() + "?theme=dark", wait_until="load", timeout=10000)
                    page.evaluate("() => Promise.race([document.fonts.ready, new Promise((_, reject) => setTimeout(() => reject(new Error('font timeout')), 8000))])")
                    page.evaluate("() => Promise.race([new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))), new Promise((_, reject) => setTimeout(() => reject(new Error('frame timeout')), 8000))])")
                    button = page.locator("#btn-theme")
                    if button.count() != 1:
                        diagnostics.append(diagnostic("browser/theme-toggle-missing", "Theme toggle #btn-theme is missing or ambiguous."))
                    else:
                        button.click()
                        page.wait_for_function("expected => document.documentElement.getAttribute('data-theme') === expected", arg="light", timeout=10000)
                    for width, height in VIEWPORTS:
                        page.set_viewport_size({"width": width, "height": height})
                        page.evaluate("() => Promise.race([new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))), new Promise((_, reject) => setTimeout(() => reject(new Error('frame timeout')), 8000))])")
                        m = page.evaluate(MEASURE_SCRIPT)
                        m.update({"viewport": [width, height]})
                        measurements.append(m)
                        subject = {"viewport": [width, height]}
                        if m["documentWidth"] > width:
                            diagnostics.append(diagnostic("browser/horizontal-overflow", f"Document width {m['documentWidth']} exceeds viewport {width}.", subject, {"documentWidth": m["documentWidth"]}))
                        if not m["svgFound"]:
                            diagnostics.append(diagnostic("browser/diagram-missing", "Visible diagram SVG is missing.", subject))
                        elif m["svgLeft"] < 0 or m["svgRight"] > width:
                            diagnostics.append(diagnostic("browser/diagram-outside-viewport", "Diagram SVG extends outside the viewport.", subject, {"left": m["svgLeft"], "right": m["svgRight"]}))
                        if m["minTextPx"] is None or m["minTextPx"] < 6:
                            diagnostics.append(diagnostic("browser/text-too-small", "Semantic SVG text is missing or projects below 6px.", subject, {"minimumProjectedTextPx": m["minTextPx"]}))
                        if m["theme"] != "light":
                            diagnostics.append(diagnostic("browser/theme-state", "Expected light theme at viewport check.", subject, {"theme": m["theme"]}))
                    if button.count() == 1:
                        button.click()
                        page.wait_for_function("expected => document.documentElement.getAttribute('data-theme') === expected", arg="dark", timeout=10000)
                        for width, height in (VIEWPORTS[0], VIEWPORTS[-1]):
                            page.set_viewport_size({"width": width, "height": height})
                            page.evaluate("() => Promise.race([new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))), new Promise((_, reject) => setTimeout(() => reject(new Error('frame timeout')), 8000))])")
                            m = page.evaluate(MEASURE_SCRIPT)
                            m.update({"viewport": [width, height]})
                            measurements.append(m)
                            subject = {"viewport": [width, height]}
                            if m["theme"] != "dark":
                                diagnostics.append(diagnostic("browser/theme-state", "Expected dark theme at endpoint viewport.", subject, {"theme": m["theme"]}))
                            if m["documentWidth"] > width:
                                diagnostics.append(diagnostic("browser/horizontal-overflow", f"Document width {m['documentWidth']} exceeds viewport {width}.", subject, {"documentWidth": m["documentWidth"]}))
                            if not m["svgFound"] or m["svgLeft"] < 0 or m["svgRight"] > width:
                                diagnostics.append(diagnostic("browser/diagram-outside-viewport", "Diagram SVG is missing or extends outside the viewport.", subject))
                            if m["minTextPx"] is None or m["minTextPx"] < 6:
                                diagnostics.append(diagnostic("browser/text-too-small", "Semantic SVG text is missing or projects below 6px.", subject, {"minimumProjectedTextPx": m["minTextPx"]}))
                        button.click()
                        page.wait_for_function("expected => document.documentElement.getAttribute('data-theme') === expected", arg="light", timeout=10000)
                    diagnostics.extend(diagnostic("browser/page-error", "Uncaught browser page error.", evidence={"error": error}) for error in page_errors)
                finally:
                    browser.close()
    except Exception as exc:
        return _result(policy, "failed", "browser-runtime-error", [diagnostic("browser/runtime-error", str(exc))], measurements)
    return _result(policy, "failed" if diagnostics else "passed", None, diagnostics, measurements)
