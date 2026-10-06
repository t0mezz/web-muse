"""Thinking status: TUI-style turn footer in the transcript.

A "Thinking… (Ns)" row stays pinned below each running turn's lines
(tool logs stack above it) and is removed as soon as the model's answer
fires — turn/completed and retraction clear it as a backstop. The timer
interval is always cleared, including on transcript reset. A turn already
active when the session opens (reload, late join) or started elsewhere
(TUI, other client) never fires turn/started here, so opening a session
and session/statusChanged reconcile the thinking status and running-state
CSS from the session status instead.

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
        blocks = re.findall(
            r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}",
            STYLE_CSS, re.S)
        self.assertTrue(blocks, "reduced-motion block missing")
        self.assertIn(".wv", "\n".join(blocks))

    def test_turn_started_shows_status(self):
        self.assertIn("showThinking();", case_body("turn/started"))

    def test_open_session_reconciles_running_turn(self):
        # Late join: resume + replay settle first, then reconcile.
        body = fn_body("openSession")
        self.assertIn("handleSubscribeResult(sub)", body)
        self.assertIn("reconcileRunningState();", body)
        self.assertLess(body.index("handleSubscribeResult(sub)"),
                        body.index("reconcileRunningState();"))

    def test_reconcile_helper_starts_verbs_when_running(self):
        self.assertIn("function reconcileRunningState()", APP_JS)
        body = fn_body("reconcileRunningState")
        self.assertIn("state.session", body)
        self.assertIn("status", body)
        self.assertIn("state.running", body)
        self.assertIn("updateRunChip()", body)
        # Starts the morph only when no row is playing already.
        self.assertIn("thinking-row", body)
        self.assertIn("showThinking();", body)

    def test_status_changed_starts_verbs(self):
        body = case_body("session/statusChanged")
        self.assertIn("showThinking();", body)
        self.assertIn("thinking-row", body)

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

    def test_threebody_built_left_of_text(self):
        body = fn_body("showThinking")
        for cls in ('"three-body"', '"three-body__dot"'):
            self.assertIn(cls, body)
        self.assertNotIn("pyramid-loader", body)
        self.assertLess(body.index("line.append(spinBox)"),
                        body.index("line.append(status)"))

    def test_threebody_assets_present(self):
        for marker in (".three-body {", ".three-body__dot",
                       "@keyframes spin78236", "@keyframes wobble1",
                       "@keyframes wobble2"):
            self.assertIn(marker, STYLE_CSS)
        self.assertNotIn("pyr-spin", STYLE_CSS)

    def test_threebody_themed_not_purple(self):
        m = re.search(r"\.three-body \{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertIsNotNone(m, ".three-body missing")
        self.assertIn("--uib-color: #6ea8fe", m.group(1))
        self.assertNotIn("#5D3FD3", STYLE_CSS)

    def test_threebody_freezes_under_reduced_motion(self):
        blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\) "
                            r"\{(.*?)\n\}", STYLE_CSS, re.S)
        self.assertTrue(blocks, "reduced-motion guard missing")
        self.assertIn(".three-body", "\n".join(blocks))


if __name__ == "__main__":
    unittest.main()
