"""Mobile rendering guards: the 13-item audit, locked at source level.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "style.css").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()
PROG = (ROOT / "web" / "progress.html").read_text()
HTML = (ROOT / "web" / "index.html").read_text()


def block(selector, source=None):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}",
                  CSS if source is None else source, re.S)
    assert m, f"{selector} missing"
    return m.group(1)


class TestMobileRendering(unittest.TestCase):
    def test_toolbar_wraps(self):
        self.assertIn("flex-wrap", block("#toolbar"))
        self.assertIn("flex-wrap", block("#tx-filters"))

    def test_user_bubble_widens_on_phones(self):
        m560 = CSS.split("@media (max-width: 560px)", 1)[1]
        self.assertIn(".tline.user .body", m560)
        self.assertIn("max-width: 85%", m560)

    def test_panels_opaque(self):
        for sel in ("#sessions", "#inspector"):
            self.assertIn("var(--panel)", block(sel))

    def test_panels_and_topbar_safe_area(self):
        self.assertIn("env(safe-area-inset-top)", block("#topbar"))
        self.assertIn("env(safe-area-inset-top)", block("#sessions"))
        self.assertIn("env(safe-area-inset-top)", block("#inspector"))

    def test_no_translucent_panel_backgrounds(self):
        for sel in ("#sessions", "#inspector"):
            self.assertNotIn("var(--scrim)", block(sel))

    def test_approval_inspector_stays_open(self):
        m = re.search(r"function openInspectorOnMobile\(tab\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "openInspectorOnMobile missing")
        body = m.group(1)
        self.assertNotIn("setTimeout", body)
        self.assertNotIn('classList.remove("open")', body)
        self.assertIn("syncScrim();", body)
        self.assertIn("savePanelState();", body)

    def test_restore_single_panel_on_narrow(self):
        m = re.search(r"function restorePanelState\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "restorePanelState missing")
        self.assertIn("isNarrow()", m.group(1))

    def test_resize_enforces_single_panel(self):
        self.assertIn('window.addEventListener("resize"', APP_JS)

    def test_keyboard_avoidance(self):
        self.assertIn("visualViewport", APP_JS)
        self.assertIn("syncAppHeight", APP_JS)
        self.assertIn("--app-height", CSS)
        self.assertIn("scrollIntoView", APP_JS)

    def test_dvh_fallbacks(self):
        self.assertRegex(block("#app"),
                         r"height:\s*100vh;.*height:\s*100dvh")
        self.assertIn("min-height: 100vh;", PROG)

    def test_no_autofocus_zoom(self):
        m899 = CSS.split("@media (max-width: 899px)", 1)[1]
        self.assertIn("#dir-path", m899)
        self.assertIn("#approval-mode", m899)

    def test_topbar_touch_targets(self):
        self.assertIn("#topbar button::before", CSS)
        self.assertIn("inset: -6px", CSS)

    def test_dialog_and_pills_wrap(self):
        self.assertIn("flex-wrap", block(".dir-foot"))
        self.assertIn("flex-wrap", block(".dir-navrow"))
        self.assertIn("flex-wrap", block("#repo-bar"))
        self.assertIn("min-width: 0", block(".slash-item .desc"))

    def test_star_panels_desktop_only(self):
        self.assertEqual(CSS.count("stars-on"), 2)
        desk = CSS.split("@media (min-width: 900px)", 1)[1]
        m = re.search(
            r"body\.stars-on #sessions,\s*body\.stars-on #inspector"
            r"\s*\{([^}]*)\}", desk)
        self.assertIsNotNone(m, "desktop stars-on panel rule missing")
        self.assertIn("var(--scrim)", m.group(1))

    def test_stars_toggle_syncs_body_class(self):
        m = re.search(r"function syncStarsToggle\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "syncStarsToggle missing")
        self.assertIn('"stars-on"', m.group(1))

    def test_input_multiline_radius(self):
        # Single line keeps the original pill; grown boxes freeze at its
        # curvature instead of scaling the arcs into the text.
        self.assertIn("border-radius: 999px", block("#input"))
        self.assertIn("border-radius: 25px", block("#input.multiline"))
        m = re.search(r"function autosize\(\) \{(.*?)\n\}", APP_JS,
                      re.S)
        self.assertIsNotNone(m, "autosize missing")
        self.assertIn('"multiline"', m.group(1))

    def test_progress_safe_area(self):
        self.assertIn("viewport-fit=cover", PROG)
        self.assertIn("env(safe-area-inset-top)", PROG)

    def test_pickers_form_mobile_second_row(self):
        # Effort + model selects live in a picker-row group inside the
        # topbar: transparent on desktop (order preserved), a full-width
        # second row below the topbar on mobile.
        m = re.search(r'<header id="topbar">(.*?)</header>', HTML, re.S)
        self.assertIsNotNone(m, "topbar missing")
        topbar = m.group(1)
        for needle in ('id="picker-row"', 'id="effort-picker"',
                       'id="model-picker"'):
            self.assertIn(needle, topbar)
        self.assertIn("display: contents", block("#picker-row"))
        mobile = "".join(CSS.split("@media (max-width: 899px)")[1:])
        self.assertIn("#picker-row", mobile)
        self.assertIn("flex: 1 1 100%", mobile)
        tops = re.findall(r"#topbar\s*\{([^}]*)\}", mobile)
        self.assertTrue(any("flex-wrap" in b for b in tops),
                        "no wrapping #topbar rule on mobile")


if __name__ == "__main__":
    unittest.main()
