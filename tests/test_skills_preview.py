"""Skill preview wiring: live skill/list selectors are merged into the
slash autocomplete popup and dispatched as turn skill parts.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_slash_popup.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()


class TestSkillPreviewMerge(unittest.TestCase):
    def test_popup_merges_skill_cache(self):
        self.assertIn("state.skillsCache", APP_JS)
        self.assertIn('kind: "skill"', APP_JS)

    def test_static_commands_win_collisions(self):
        self.assertIn("staticNames.has(sel.toLowerCase())", APP_JS)

    def test_cache_refreshes_on_open_and_change(self):
        self.assertIn("fetchSkills(sessionId)", APP_JS)
        self.assertIn('case "skill/changed":', APP_JS)


class TestSkillDispatch(unittest.TestCase):
    def test_unknown_slash_falls_through_to_skills(self):
        self.assertIn("findSkill(name)", APP_JS)
        self.assertIn("cmdSkillInvoke(hit.selector, args)", APP_JS)

    def test_skill_sends_skill_part_with_display_text(self):
        self.assertIn("skills: [{ selector, arguments:", APP_JS)
        self.assertIn("req.skills = extra.skills", APP_JS)
        self.assertIn("req.displayText = extra.displayText", APP_JS)


if __name__ == "__main__":
    unittest.main()
