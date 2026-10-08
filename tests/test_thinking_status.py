"""Thinking status: TUI-style turn footer in the transcript.

A "Thinking… (Ns)" row stays pinned below each running turn's lines
(tool logs stack above it) and is removed once the turn settles —
turn/completed, retraction, and an idle status flip clear it.
Intermediate agent messages land mid-turn and must NOT clear it; new
lines re-pin (and restore) the row without resetting the turn clock.
Sending a new task restarts the clock optimistically; a queued
follow-up keeps the current turn's row. Settled turns clear the active
turn id so the restart cannot derive off the previous turn. The timer interval is always
cleared, including on transcript reset. A turn already active when the
session opens (reload, late join) or started elsewhere (TUI, other
client) never fires turn/started here, so opening a session and
session/statusChanged reconcile the thinking status and running-state
CSS from the session status instead, preserving the clock.

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
        self.assertIn("function showThinking(", APP_JS)
        self.assertIn("function ensureThinking()", APP_JS)
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
        self.assertIn("handleSubscribeResult(sub, sessionId)", body)
        self.assertIn("reconcileRunningState();", body)
        self.assertLess(body.index("handleSubscribeResult(sub, sessionId)"),
                        body.index("reconcileRunningState();"))

    def test_reconcile_helper_starts_verbs_when_running(self):
        self.assertIn("function reconcileRunningState()", APP_JS)
        body = fn_body("reconcileRunningState")
        self.assertIn("state.session", body)
        self.assertIn("status", body)
        self.assertIn("state.running", body)
        self.assertIn("updateRunChip()", body)
        # Restores the row without restarting the turn clock.
        self.assertIn("ensureThinking();", body)
        self.assertNotIn('getElementById("thinking-row")', body)

    def test_status_changed_starts_verbs(self):
        body = case_body("session/statusChanged")
        self.assertIn("ensureThinking();", body)
        # A flip back to idle drops a stale row (lost turn/completed).
        self.assertIn("hideThinking();", body)

    def test_midturn_answer_keeps_status(self):
        body = case_body("item/completed")
        self.assertIn('=== "agent"', body)
        # Intermediate agent notes land mid-turn: the row survives them.
        self.assertIn("!state.running", body)
        self.assertIn("hideThinking();", body)

    def test_send_restarts_clock(self):
        body = fn_body("sendPromptText")
        self.assertIn("showThinking();", body)
        # A queued follow-up keeps the current turn's row and clock.
        self.assertIn("queued", body)

    def test_settled_turn_clears_active_turn(self):
        # Otherwise the next optimistic send derives the clock off the
        # previous turn's first item and restarts at its elapsed time.
        for marker in ("turn/completed", "turn/retracted"):
            body = case_body(marker)
            self.assertIn("activeTurnId", body)
            self.assertIn("= null", body)

    def test_turn_started_adopts_active_turn(self):
        self.assertIn("activeTurnId", case_body("turn/started"))

    def test_send_adopts_new_turn_before_show(self):
        body = fn_body("sendPromptText")
        self.assertIn("activeTurnId", body)
        self.assertLess(body.index("activeTurnId"),
                        body.index("showThinking();"))

    def test_send_records_request_time(self):
        body = fn_body("sendPromptText")
        self.assertIn("thinkingRequestAt = Date.now()", body)
        self.assertLess(body.index("thinkingRequestAt = Date.now()"),
                        body.index("showThinking();"))

    def test_reset_clamps_to_request_time(self):
        self.assertIn("thinkingRequestAt", fn_body("showThinking"))

    def test_request_clock_cleared_on_settle(self):
        for marker in ("turn/completed", "turn/retracted"):
            body = case_body(marker)
            self.assertIn("thinkingRequestAt", body)
            self.assertIn("= 0", body)
        self.assertIn("thinkingRequestAt = 0", fn_body("clearTranscript"))

    def test_ensure_preserves_clock(self):
        body = fn_body("ensureThinking")
        self.assertIn("state.running", body)
        self.assertIn("showThinking(false)", body)

    def test_show_distinguishes_reset_from_restore(self):
        body = fn_body("showThinking")
        self.assertIn("reset", body)
        self.assertIn("if (reset || !thinkingStartedAt)", body)

    def test_pin_restores_missing_row(self):
        self.assertIn("ensureThinking();", fn_body("pinThinking"))

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
