"""Welcome starfield: dependency-free star backdrop on the empty welcome
view, auto-stopped when the first message is sent but left running when
a real session opens, with a persisted bottom-right toggle switch.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_panel_persist.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
STARS_JS = (ROOT / "web" / "stars.js").read_text()


class TestStarsWelcome(unittest.TestCase):
    def test_stars_script_included_before_app(self):
        self.assertIn("/stars.js", INDEX_HTML)
        self.assertLess(INDEX_HTML.index("/stars.js"), INDEX_HTML.index("/app.js"))

    def test_backdrop_pinned_behind_app(self):
        self.assertIn("#stars-bg", STYLE_CSS)
        self.assertIn("pointer-events: none", STYLE_CSS)
        # Negative z-index: a z-index:0 fixed layer would paint over all
        # non-positioned app content (e.g. the topbar) while stars run.
        self.assertIn("z-index: -1", STYLE_CSS)

    def test_starts_only_on_empty_welcome_without_session(self):
        self.assertIn("startStarsFx()", APP_JS)
        self.assertIn("if (welcome && !state.sessionId) startStarsFx();", APP_JS)

    def test_session_id_set_before_transcript_clear(self):
        # openSession must assign the id before clearTranscript runs, or
        # its updateWelcome() sees a stale null id and restarts the field.
        self.assertIn("state.sessionId = sessionId;\n  clearTranscript();", APP_JS)

    def test_stops_on_first_message(self):
        self.assertIn("stopStarsFxNow()", APP_JS)
        # First submit kills it (placed right after the empty-text guard).
        self.assertIn("if (!text) return;\n  stopStarsFxNow();", APP_JS)

    def test_stays_on_when_session_opened(self):
        # The toggle owns the field now: opening a session leaves it up.
        self.assertNotIn(
            "async function openSession(sessionId) {\n  stopStarsFxNow();", APP_JS)

    def test_reduced_motion_opt_out(self):
        self.assertIn("prefers-reduced-motion", APP_JS)

    def test_theme_matched_backdrop(self):
        self.assertIn(
            "radial-gradient(ellipse at bottom, #1a2334 0%, #0c0e12 100%)",
            STARS_JS)
        self.assertIn('starColor: "#e6e9ef"', APP_JS)


class TestStarsTuning(unittest.TestCase):
    def test_locked_tuning_values(self):
        self.assertIn("counts: [650, 260, 130],", APP_JS)
        self.assertIn("opacity: 0.7,", APP_JS)
        self.assertIn("factor: 0.0125,", APP_JS)

    def test_tuning_passed_directly_as_options(self):
        # No post-hoc regeneration of the star layers after creation.
        self.assertIn("createStarsBackground(bg, STARS_OPTIONS)", APP_JS)
        self.assertIn("counts = options.counts", STARS_JS)
        self.assertIn("opacity = options.opacity", STARS_JS)
        self.assertIn("parallax.style.opacity", STARS_JS)

    def test_no_posthoc_hacks_left(self):
        self.assertNotIn("applyStarTuning", APP_JS)
        self.assertNotIn("buildStarsField", APP_JS)
        self.assertNotIn("STARS_TRANSPARENCY", APP_JS)
        self.assertNotIn("STARS_COUNT_SCALE", APP_JS)
        self.assertNotIn("STARS_STRENGTH_SCALE", APP_JS)
        self.assertNotIn("firstElementChild.style.zIndex", APP_JS)
        self.assertNotIn('bg.style.position = "fixed"', APP_JS)
        # The effect no longer fights the caller's positioning.
        self.assertNotIn("container.style.position", STARS_JS)

    def test_no_temp_panel_left(self):
        self.assertNotIn("stars-tmp-panel", APP_JS)
        self.assertNotIn("stars-intensity", APP_JS)
        self.assertNotIn("buildStarsTmpPanel", APP_JS)


class TestStarsToggle(unittest.TestCase):
    def test_toggle_markup(self):
        self.assertIn('id="stars-toggle"', INDEX_HTML)
        self.assertIn('role="switch"', INDEX_HTML)
        self.assertIn('aria-checked=', INDEX_HTML)
        self.assertIn('aria-label="Toggle starfield"', INDEX_HTML)

    def test_toggle_pinned_bottom_right(self):
        self.assertIn("#stars-toggle", STYLE_CSS)
        self.assertIn("position: fixed; right: 14px;", STYLE_CSS)
        self.assertIn("width: 32px; height: 20px;", STYLE_CSS)

    def test_toggle_thumb_and_pressed_width(self):
        self.assertIn(".stars-thumb", STYLE_CSS)
        self.assertIn("width: 16px; height: 16px;", STYLE_CSS)
        self.assertIn("#stars-toggle:active .stars-thumb { width: 19px; }", STYLE_CSS)
        self.assertIn("#stars-toggle:focus-visible", STYLE_CSS)

    def test_toggle_wired_and_persisted(self):
        self.assertIn('const STARS_KEY = "web-muse:stars";', APP_JS)
        self.assertIn("localStorage.getItem(STARS_KEY)", APP_JS)
        self.assertIn("localStorage.setItem(STARS_KEY,", APP_JS)
        self.assertIn("function toggleStarsFx() {", APP_JS)
        self.assertIn("function syncStarsToggle() {", APP_JS)
        self.assertIn('setAttribute("aria-checked"', APP_JS)
        self.assertIn(
            'el("stars-toggle").addEventListener("click", () => { toggleStarsFx(); });',
            APP_JS,
        )


if __name__ == "__main__":
    unittest.main()
