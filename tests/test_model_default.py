"""Model default, approval placement, and follow-up queue signals (UI).

- The top-right model picker remembers its pick (`pickedModel`) and every
  session-creation path (`new`, first-prompt, `githubOpen`) carries it, so
  opening a chat applies the selection instead of only switching in-chat.
- Approval cards mount ONLY in the right-hand inspector (Approvals tab):
  nothing approval-shaped is appended to the transcript `#cards`.
- Follow-up queue state is surfaced: the prompt ack's `disposition` drives
  a "queued" notice plus `/unqueue`, instead of looking lost until it
  fires later.

No JS harness in this repo: guarded at the source level, following
tests/test_inspector_toggle.py.

Run: python3 -m unittest tests.test_model_default -v   (from repo root)
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()


def _fn_body(name):
    m = re.search(r"(?:async )?function %s\(.*?\) \{(.*?)\n\}"
                  % re.escape(name), APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestModelDefault(unittest.TestCase):
    def test_pick_remembered(self):
        self.assertIn("pickedModel: null", APP_JS)
        # Canonical setter, shared by the topbar picker and settings rows.
        self.assertIn("state.pickedModel = { modelId: value", APP_JS)
        self.assertIn("applyModelPick(o.value, o.dataset.provider", APP_JS)

    def test_picker_records_without_session(self):
        self.assertIn("default model →", APP_JS)
        self.assertIn('toast("default model → " + value)', APP_JS)
        self.assertNotIn('toast("default model → " + value + " (applies to new chats)")', APP_JS)

    def test_creation_paths_carry_model(self):
        self.assertIn("req.modelId = state.pickedModel.modelId", APP_JS)
        self.assertIn("if (state.pickedModel) req.model = state.pickedModel",
                      APP_JS)

    def test_bridge_forwards_github_open_model(self):
        for marker in ('"model": msg.get("model")',
                       'new_msg["modelId"] = model["modelId"].strip()',
                       '"modelId", "providerId"'):
            self.assertIn(marker, SESSIONS_PY)

    def test_slash_model_remembers(self):
        self.assertIn("state.pickedModel = { modelId: hit.modelId", APP_JS)

    def test_pick_persisted_across_reloads(self):
        # Changing the model anywhere saves it; startup restores it.
        self.assertIn('"webmuse.pickedModel"', APP_JS)
        self.assertIn("function savePickedModel()", APP_JS)
        self.assertIn("function loadPickedModel()", APP_JS)
        self.assertGreaterEqual(APP_JS.count("savePickedModel();"), 2)
        self.assertIn("loadPickedModel();", APP_JS)

    def test_picker_shows_saved_pick(self):
        # A saved pick wins over the host's active mark in the picker;
        # without one the active model shows as before.
        body = _fn_body("refreshModels")
        self.assertIn("state.pickedModel && state.pickedModel.modelId",
                      body)
        self.assertIn("want ? m.modelId === want : m.isActive", body)


class TestApprovalPlacement(unittest.TestCase):
    def test_approval_mounts_only_in_inspector(self):
        body = _fn_body("onApproval")
        self.assertIn('el("tab-approvals").append(div)', body)
        self.assertNotIn('el("cards").append', body)
        self.assertNotIn("cloneNode", body)
        self.assertNotIn("bindClonedApproval", APP_JS)

    def test_transcript_keeps_only_a_pointer_line(self):
        self.assertIn("decide in the inspector (Approvals tab)", APP_JS)

    def test_inspector_opens_for_approval(self):
        body = _fn_body("onApproval")
        self.assertIn('classList.add("open")', body)


class TestQueueVisibility(unittest.TestCase):
    def test_disposition_drives_notice(self):
        self.assertIn('r.disposition === "queued"', APP_JS)
        self.assertIn("queued behind the running turn", APP_JS)
        self.assertIn("queuedTurnId", APP_JS)

    def test_unqueue_wired(self):
        self.assertIn("cmdUnqueue", APP_JS)
        self.assertIn('{ name: "unqueue"', APP_JS)
        self.assertIn('type: "unqueue"', APP_JS)

    def test_bridge_supports_unqueue(self):
        self.assertIn('"turn/unqueue"', SESSIONS_PY)


if __name__ == "__main__":
    unittest.main()
