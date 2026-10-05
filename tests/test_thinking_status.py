"""Thinking status: TUI-style turn footer in the transcript.

A "Thinking… (Ns)" row stays pinned below each running turn's lines
(tool logs stack above it) and is removed as soon as the model's answer
fires — turn/completed and retraction clear it as a backstop. The timer
interval is always cleared, including on transcript reset.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_session_toggle.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


def case_body(marker):
    m = re.search(r'case "' + marker + r'":(.*?)break;', APP_JS, re.S)
    assert m, f'case "{marker}" missing'
    return m.group(1)


def fn_body(name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestThinkingStatus(unittest.TestCase):
    def test_helpers_exist(self):
        self.assertIn("function showThinking()", APP_JS)
        self.assertIn("function hideThinking()", APP_JS)
        self.assertIn('id = "thinking-row"', APP_JS)

    def test_status_row_heads_the_turn(self):
        body = fn_body("showThinking")
        self.assertIn('"tline thinking"', body)
        self.assertIn("setInterval", body)

    def test_timer_cleared_on_hide(self):
        self.assertIn("clearInterval", fn_body("hideThinking"))

    def test_wave_built_per_character(self):
        body = fn_body("showThinking")
        self.assertIn('"wv"', body)
        self.assertIn('"--i"', body)

    def test_wave_animation_present(self):
        self.assertIn("@keyframes think-wave", STYLE_CSS)
        self.assertIn(".tline.thinking .wv", STYLE_CSS)

    def test_wave_freezes_under_reduced_motion(self):
        m = re.search(
            r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}",
            STYLE_CSS, re.S)
        self.assertIsNotNone(m, "reduced-motion block missing")
        self.assertIn(".wv", m.group(1))

    def test_turn_started_shows_status(self):
        self.assertIn("showThinking();", case_body("turn/started"))

    def test_answer_firing_clears_status(self):
        body = case_body("item/completed")
        self.assertIn("hideThinking();", body)
        self.assertIn('=== "agent"', body)

    def test_new_lines_pin_status_below(self):
        self.assertIn("function pinThinking()", APP_JS)
        for name in ("renderItem", "sysLine"):
            self.assertIn("pinThinking();", fn_body(name),
                          f"{name} lets lines pile below the status")

    def test_settled_turns_clear_status(self):
        for marker in ("turn/completed", "turn/retracted"):
            self.assertIn("hideThinking();", case_body(marker),
                          f"{marker} leaves the status behind")

    def test_reset_clears_status(self):
        self.assertIn("hideThinking();", fn_body("clearTranscript"))

    def test_status_styling_present(self):
        self.assertIn(".tline.thinking", STYLE_CSS)

    def test_pyramid_built_left_of_text(self):
        body = fn_body("showThinking")
        for cls in ('"pyr-box"', '"pyramid-loader"', '"shadow"'):
            self.assertIn(cls, body)
        self.assertLess(body.index("line.append(pyrBox)"),
                        body.index("line.append(status)"))

    def test_pyramid_assets_present(self):
        for marker in (".pyramid-loader .wrapper {", "@keyframes pyr-spin",
                       ".pyramid-loader .wrapper .side",
                       ".pyramid-loader .wrapper .shadow"):
            self.assertIn(marker, STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
