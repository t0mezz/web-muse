"""Auto-approve allowlist for GitHub-opened sessions.

The bridge answers `approval/request` for `gh pr create` (plus read-only
`gh pr` queries) bridge-side via `approval/decide`, so agents in sessions
opened with /github open never park on an unanswered prompt for the PR
flow. Everything else still raises the normal UI approval card.

Run: python3 -m unittest tests.test_github_approvals -v   (from repo root)
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sessions import (  # noqa: E402
    SessionRouter,
    shell_auto_allowed,
    pick_approve_once_choice,
)

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()


def _shell(command):
    return {"kind": "shell", "command": command}


def _approval(session_id="sid-gh", command="gh pr create --title T",
              choices=None):
    if choices is None:
        choices = [
            {"choiceId": "allow-once", "decision": "approved",
             "label": "Allow", "scope": "once"},
            {"choiceId": "deny", "decision": "denied",
             "label": "Deny", "scope": "once"},
        ]
    return {
        "sessionId": session_id,
        "approvalId": "ap-1",
        "toolName": "shell",
        "subject": _shell(command),
        "availableChoices": choices,
        "currentRequirementId": {"approvalId": "ap-1", "sourceIndex": 0},
    }


class FakeMsp:
    """Records MSP commands (stands in for the serve host)."""

    def __init__(self):
        self.commands = []

    async def command(self, method, params):
        self.commands.append((method, dict(params)))
        return {}


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class TestAllowlistMatcher(unittest.TestCase):
    def test_all_gh_forms_allowed(self):
        for cmd in ("gh",
                    "gh pr create",
                    "gh pr create --title T --body B",
                    "gh pr create --fill",
                    "  gh pr create --title T  ",
                    "gh pr view 12", "gh pr status", "gh pr diff",
                    "gh pr checks 12", "gh pr list --limit 5",
                    "gh pr merge 12", "gh pr close 12", "gh pr edit 12",
                    "gh release create v1", "gh api repos/octo/hello",
                    "gh repo view octo/hello", "gh auth status",
                    "gh issue create --title T", "gh repo delete octo/x",
                    "gh pr"):
            self.assertTrue(shell_auto_allowed(_shell(cmd)), cmd)

    def test_non_allowlisted_commands_rejected(self):
        for cmd in ("xgh pr create",
                    "sudo gh pr create", "GH PR CREATE", "",
                    "ghi pr list"):
            self.assertFalse(shell_auto_allowed(_shell(cmd)), cmd)

    def test_basic_git_allowed(self):
        for cmd in ("git",
                    "git status", "git diff", "git diff --cached",
                    "git log --oneline -5", "git show HEAD",
                    "git branch -a", "git rev-parse --abbrev-ref HEAD",
                    "git add .", "git commit -m \"feat: x\"",
                    "git push", "git push -u origin feat/x",
                    "git pull", "git fetch origin",
                    "git switch -c feat/x", "git switch main",
                    "git checkout main", "git stash push -m wip",
                    "git remote -v", "git tag v1"):
            self.assertTrue(shell_auto_allowed(_shell(cmd)), cmd)

    def test_destructive_git_rejected(self):
        for cmd in ("git reset --hard HEAD~1",
                    "git clean -fd", "git clean -fx",
                    "git clean --force -d",
                    "git push --force origin feat/x",
                    "git push -f origin feat/x",
                    "git push origin --delete feat/x",
                    "git branch -D feat/x",
                    "git stash drop", "git stash clear"):
            self.assertFalse(shell_auto_allowed(_shell(cmd)), cmd)

    def test_inspection_commands_allowed(self):
        for cmd in ("ls", "ls -la", "cat package.json",
                    "head -n 20 README.md", "tail -f app.log",
                    "find . -name '*.py'", "grep -rn foo src",
                    "rg --files", "tree -L 2", "wc -l a b",
                    "file bin/x", "stat main.py", "diff a b",
                    "jq . package.json", "pwd", "echo done",
                    "printf '%s' x"):
            self.assertTrue(shell_auto_allowed(_shell(cmd)), cmd)

    def test_toolchains_allowed(self):
        for cmd in ("node script.js", "npm test", "npm run build",
                    "npx tsc --noEmit", "python3 -m pytest -q",
                    "pytest tests/", "pip list", "pip3 install -r r.txt",
                    "uv run pytest", "uvx ruff check",
                    "cargo test", "rustc --version", "go test ./...",
                    "make check", "tsc --noEmit"):
            self.assertTrue(shell_auto_allowed(_shell(cmd)), cmd)

    def test_mutation_and_network_still_prompt(self):
        for cmd in ("rm -rf dist", "sudo ls", "mv a b", "cp a b",
                    "find . -delete", "find . -exec rm {} ;",
                    "curl https://example.com/x", "wget x",
                    "ssh host", "vim main.py", "docker run x"):
            self.assertFalse(shell_auto_allowed(_shell(cmd)), cmd)

    def test_non_shell_subjects_rejected(self):
        for subject in (None, {}, {"kind": "shell"},
                        {"kind": "shell", "command": None},
                        {"kind": "shell", "command": 123},
                        {"kind": "tool", "command": "gh pr create"},
                        {"kind": "fileAccess", "command": "gh pr create",
                         "path": "/x"}):
            self.assertFalse(shell_auto_allowed(subject), subject)

class TestPickApproveChoice(unittest.TestCase):
    def test_prefers_approved_once(self):
        choices = [
            {"choiceId": "allow-session", "decision": "approved",
             "label": "Allow session", "scope": "session"},
            {"choiceId": "allow-once", "decision": "approved",
             "label": "Allow once", "scope": "once"},
            {"choiceId": "deny", "decision": "denied", "label": "Deny",
             "scope": "once"},
        ]
        self.assertEqual(pick_approve_once_choice(choices), "allow-once")

    def test_falls_back_to_session_approve(self):
        choices = [{"choiceId": "allow-session",
                    "decision": "approvedForSession",
                    "label": "Allow session", "scope": "session"}]
        self.assertEqual(pick_approve_once_choice(choices), "allow-session")

    def test_never_picks_denials_or_amendments(self):
        choices = [
            {"choiceId": "deny", "decision": "denied", "label": "Deny",
             "scope": "once"},
            {"choiceId": "amend", "decision": "approvedPolicyAmendment",
             "label": "Allow + save rule", "scope": "localPersistent"},
        ]
        self.assertIsNone(pick_approve_once_choice(choices))

    def test_missing_or_empty_is_none(self):
        self.assertIsNone(pick_approve_once_choice(None))
        self.assertIsNone(pick_approve_once_choice([]))
        self.assertIsNone(pick_approve_once_choice("allow"))


class TestRouterAutoApprove(unittest.TestCase):
    def _router(self):
        return SessionRouter(msp=FakeMsp(), workspace_base="/tmp")

    def test_gh_session_auto_decides_without_card(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            params = _approval()
            router.on_server_request("approval/request", params)
            await asyncio.sleep(0.1)  # let the queued decide run
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(cards, [])
            notices = [f for f in conn.frames
                       if f.get("method") == "githubAutoApproved"]
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["params"]["command"],
                             "gh pr create --title T")
            self.assertEqual(len(router._msp.commands), 1)
            method, decide = router._msp.commands[0]
            self.assertEqual(method, "approval/decide")
            self.assertEqual(decide["sessionId"], "sid-gh")
            self.assertEqual(decide["approvalId"], "ap-1")
            self.assertEqual(decide["choiceId"], "allow-once")
            # Race guard travels under the decide key, copied by value.
            self.assertEqual(decide["requirementId"],
                             {"approvalId": "ap-1", "sourceIndex": 0})
        asyncio.run(body())

    def test_other_sessions_still_prompt(self):
        async def body():
            router = self._router()
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request("approval/request", _approval())
            await asyncio.sleep(0.05)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(router._msp.commands, [])
        asyncio.run(body())

    def test_non_allowlisted_command_still_prompts(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request(
                "approval/request", _approval(command="docker run x"))
            await asyncio.sleep(0.05)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(router._msp.commands, [])
        asyncio.run(body())

    def test_basic_git_auto_decides(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request(
                "approval/request",
                _approval(command="git push -u origin feat/x"))
            await asyncio.sleep(0.1)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(cards, [])
            self.assertEqual(len(router._msp.commands), 1)
            method, decide = router._msp.commands[0]
            self.assertEqual(method, "approval/decide")
            self.assertEqual(decide["choiceId"], "allow-once")
        asyncio.run(body())

    def test_toolchain_auto_decides(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request(
                "approval/request", _approval(command="npm test"))
            await asyncio.sleep(0.1)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(cards, [])
            self.assertEqual(len(router._msp.commands), 1)
            self.assertEqual(router._msp.commands[0][0], "approval/decide")
        asyncio.run(body())

    def test_find_delete_still_prompts(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request(
                "approval/request",
                _approval(command="find . -delete"))
            await asyncio.sleep(0.05)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(router._msp.commands, [])
        asyncio.run(body())

    def test_destructive_git_still_prompts(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request(
                "approval/request",
                _approval(command="git reset --hard HEAD~1"))
            await asyncio.sleep(0.05)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(router._msp.commands, [])
        asyncio.run(body())

    def test_no_approve_choice_still_prompts(self):
        async def body():
            router = self._router()
            router._gh_auto_sids.add("sid-gh")
            conn = FakeConn()
            router.add_conn(conn)
            router.on_server_request("approval/request", _approval(choices=[
                {"choiceId": "deny", "decision": "denied",
                 "label": "Deny", "scope": "once"}]))
            await asyncio.sleep(0.05)
            cards = [f for f in conn.frames if f.get("type") == "approval"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(router._msp.commands, [])
        asyncio.run(body())


class TestWiring(unittest.TestCase):
    def test_bridge_and_ui_agree_on_notice(self):
        for marker in ("GH_AUTO_ALLOW_RE", "GH_GIT_DENY_RE",
                       "GH_AUTO_ALLOW_TOOLS_RE", "GH_TOOL_DENY_RE",
                       "shell_auto_allowed",
                       "pick_approve_once_choice", "_auto_approve_gh_pr",
                       "_gh_auto_decide", "_gh_auto_sids",
                       "githubAutoApproved"):
            self.assertIn(marker, SESSIONS_PY)
        self.assertIn("githubAutoApproved", APP_JS)


if __name__ == "__main__":
    unittest.main()
