"""Idle composer glow: orb-style aura on the chat input while no turn runs.

- `updateRunChip` toggles `idle-glow` on `#input`: on when idle, off while
  a turn runs; startup applies it via the init call.
- The glow reuses the theme accent as `--glow-color` (no new palette),
  intensifies on focus, breathes only under `prefers-reduced-motion:
  no-preference`, and stays static and tighter on narrow phones.

No JS harness in this repo: guarded at the source level, following
tests/test_thinking_status.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


def fn_body(name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestIdleGlowToggle(unittest.TestCase):
    def test_run_chip_drives_glow(self):
        body = fn_body("updateRunChip")
        self.assertIn('el("input").classList.toggle("idle-glow", '
                      '!state.running)', body)

    def test_glow_applies_on_startup(self):
        self.assertIn("updateRunChip();", APP_JS)


class TestSlashPopupLayout(unittest.TestCase):
    def test_popup_floats_above_composer(self):
        # Overlay (not in-flow) anchored to the positioned #composer-wrap:
        # opening the preview must never move the input box. The old
        # in-flow contract pushed the composer and is retired.
        m = re.search(r"^#slash-popup \{(.*?)\n\}$", STYLE_CSS,
                      re.S | re.M)
        self.assertIsNotNone(m, "#slash-popup missing")
        self.assertIn("position: absolute", m.group(1))
        self.assertIn("bottom: calc(100%", m.group(1))
        anchor = re.search(r"^#composer-wrap \{([^}]*)\}", STYLE_CSS,
                           re.M)
        self.assertIsNotNone(anchor, "#composer-wrap missing")
        self.assertIn("position: relative", anchor.group(1))


class TestSlashPopupBackdrop(unittest.TestCase):
    def test_backdrop_always_on(self):
        # The overlay floats over the transcript, so the opaque panel
        # backdrop is permanent — the running-only toggle is retired.
        body = fn_body("updateRunChip")
        self.assertNotIn('slash-popup").classList.toggle("running"',
                         body)
        m = re.search(r"^#slash-popup \{(.*?)\n\}$", STYLE_CSS,
                      re.S | re.M)
        self.assertIsNotNone(m, "#slash-popup missing")
        self.assertIn("background: var(--panel)", m.group(1))
        self.assertNotIn("#slash-popup.running", STYLE_CSS)


class TestIdleGlowStyling(unittest.TestCase):
    def test_glow_uses_theme_accent(self):
        m = re.search(r"#input\.idle-glow \{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, "#input.idle-glow missing")
        self.assertIn("--glow-color: 110, 168, 254", m.group(1))

    def test_focus_intensifies(self):
        self.assertIn("#input.idle-glow:focus", STYLE_CSS)

    def test_breathe_freezes_under_reduced_motion(self):
        # style.css legitimately holds several no-preference blocks
        # (copy-pop, idle-glow breathe, ...): scan all of them, not just
        # the first match.
        blocks = re.findall(r"@media \(prefers-reduced-motion: no-preference\) "
                            r"\{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertTrue(blocks, "reduced-motion guard missing")
        self.assertTrue(
            any("#input.idle-glow" in b and "input-breathe" in b
                for b in blocks),
            "idle-glow breathe not gated under reduced-motion guard")

    def test_narrow_phones_stay_static(self):
        m = re.search(r"@media \(max-width: 560px\) \{(.*?)\n\}\n",
                      STYLE_CSS, re.S)
        self.assertIsNotNone(m, "560px block missing")
        glow = re.search(r"#input\.idle-glow \{(.*?)\n  \}", m.group(1),
                         re.S)
        self.assertIsNotNone(glow, "no small-screen glow rule")
        self.assertIn("animation: none", glow.group(1))


if __name__ == "__main__":
    unittest.main()
