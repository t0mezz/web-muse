"""Transcript echo + reminder rendering (source-level, no JS harness).

Two defects reported against the live transcript:

1. Every sent message rendered twice: `submitComposer` (and `/steer`)
   paints an optimistic echo with a `local-<Date.now()>` itemId, and the
   server's real `userMessage` arrives later with its own itemId, so
   dedupe-by-itemId can never collapse them. The fix is
   `reconcileLocalEcho`: when a server userMessage arrives, drop the
   oldest text-matching `local-` line (DOM node + registry entry).

2. `reminderChild` (an official MSP v1 ItemKind: reminder-agent child
   session activity) fell through `itemKind()` into full agent styling,
   so host-internal "Reminder child session" firings rendered as loud
   agent rows. The fix demotes any `*reminder*` kind to system lines.

Run: python3 -m unittest tests.test_transcript_echo -v   (from repo root)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_JS = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text()


def fn_body(src, name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestEchoReconciliation(unittest.TestCase):
    def test_echoes_still_optimistic(self):
        # All send paths paint an instant local echo (no behavior change):
        # plain submit, /steer, and the staged repo-first message (which
        # echoes at staging; the post-clone flush must not echo twice).
        self.assertEqual(APP_JS.count('"local-" + Date.now()'), 3)
        self.assertIn("sendPromptText(t, true)", APP_JS)

    def test_reconcile_helper_exists_and_matches_by_text(self):
        body = fn_body(APP_JS, "reconcileLocalEcho")
        # Only server userMessages with real text can collapse an echo.
        self.assertIn('itemKind(serverItem) !== "user"', body)
        self.assertIn("itemText(serverItem)", body)
        self.assertIn("if (!text) return;", body)
        # Oldest text-matching echo only: remove its line, drop the
        # registry entry, stop after one.
        self.assertIn('id.startsWith("local-")', body)
        self.assertIn("itemText(rec.item) === text", body)
        self.assertIn("rec.line.remove();", body)
        self.assertIn("state.items.delete(id);", body)
        self.assertIn("return;", body)

    def test_render_paths_reconcile_server_items(self):
        for name in ("renderItem", "renderItemPrepend"):
            self.assertIn("reconcileLocalEcho(it)", fn_body(APP_JS, name),
                          f"{name} never reconciles")
        # Already-rendered items update in place; local echoes themselves
        # must never trigger a reconcile pass.
        self.assertIn('!it.itemId.startsWith("local-")',
                      fn_body(APP_JS, "renderItem"))


class TestReminderHidden(unittest.TestCase):
    def test_helper_detects_reminder_kinds(self):
        body = fn_body(APP_JS, "isReminder")
        self.assertIn("toLowerCase()", body)
        self.assertIn('"reminder"', body)

    def test_render_paths_drop_reminders(self):
        for name in ("renderItem", "renderItemPrepend"):
            body = fn_body(APP_JS, name)
            self.assertIn("isReminder(it)", body,
                          f"{name} never drops reminders")
            self.assertIn("return;", body)


class TestReminderDemotion(unittest.TestCase):
    def test_reminder_kinds_map_to_system(self):
        body = fn_body(APP_JS, "itemKind")
        m = re.search(r"reminder.*?return \"system\"", body, re.S | re.I)
        self.assertIsNotNone(m, "no reminder -> system mapping")
        # The demotion sits before the agent fallback, not after it.
        self.assertLess(body.index("reminder"), body.index('return "agent"'))

    def test_sibling_kinds_unchanged(self):
        body = fn_body(APP_JS, "itemKind")
        self.assertIn('return "user"', body)
        self.assertIn('return "tool"', body)
        self.assertIn('return "agent"', body)


if __name__ == "__main__":
    unittest.main()
