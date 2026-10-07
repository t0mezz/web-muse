"""Seeded instruction set: session AGENTS.md + clone-leaf AGENTS.md.

Every bridge-owned session workspace gets an AGENTS.md at its root
(plain orientation, or naming `./<repo>/` for repo sessions); every
fresh clone leaf gets the bridge-owned GitHub instructions
(`server/github_instructions.md` rendered with the repo name) as its
own AGENTS.md — but only when the repo ships none, so repo-authored
rules always win. The host loads them via `--trust-workspace` (now
default-on). Sessions rooted at a seeded workspace join the gh/git
auto-approve set; pre-rename `WEB-MUSE.md` leaves are still recognized.

Run: python3 -m unittest tests.test_github_instructions -v (from repo root)
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.main import build_serve_argv, parse_args  # noqa: E402
from server.sessions import (  # noqa: E402
    AUTO_ALLOW_INSPECT,
    AUTO_ALLOW_TOOLCHAINS,
    GITHUB_INSTRUCTIONS_FILENAME,
    GITHUB_INSTRUCTIONS_MARKER,
    GITHUB_INSTRUCTIONS_TEMPLATE,
    GIT_DENY_SUMMARY,
    LEGACY_GITHUB_INSTRUCTIONS_FILENAME,
    SESSION_AGENTS_MARKER,
    SessionRouter,
    has_seeded_instructions,
    render_github_instructions,
    render_session_agents,
    seed_github_instructions,
    seed_session_agents,
)

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_PY = (ROOT / "server" / "sessions.py").read_text()


class FakeMsp:
    def __init__(self, session_id="sid-seed"):
        self.session_id = session_id
        self.commands = []

    async def command(self, method, params):
        self.commands.append((method, dict(params)))
        if method == "session/start":
            return {"session": {"sessionId": params.get("sessionId")
                                or self.session_id,
                                "name": ""}}
        return {}


class FakeConn:
    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class TestTemplate(unittest.TestCase):
    def test_template_exists_and_carries_marker(self):
        self.assertTrue(GITHUB_INSTRUCTIONS_TEMPLATE.is_file())
        text = GITHUB_INSTRUCTIONS_TEMPLATE.read_text()
        self.assertTrue(text.startswith(GITHUB_INSTRUCTIONS_MARKER),
                        "first line must be the seed marker")
        self.assertIn("__FULL_NAME__", text)

    def test_render_substitutes_repo(self):
        out = render_github_instructions("octo/hello")
        self.assertIn("octo/hello", out)
        self.assertNotIn("__FULL_NAME__", out)

    def test_render_explicit_template(self):
        out = render_github_instructions("o/r", template="x __FULL_NAME__ y")
        self.assertTrue(out.startswith("x o/r y\n\n"))
        self.assertIn("## Pre-approved commands", out)

    def test_rendered_content_covers_branches_prs_approvals(self):
        out = render_github_instructions("octo/hello")
        for marker in ("git switch -c", "gh pr create", "gh pr view",
                       "git push -u origin", "do not commit",
                       "NEVER merge", "BLOCKED", "IS the repo root",
                       "users.noreply.github.com", "gh auth setup-git"):
            self.assertIn(marker, out)


class TestSeed(unittest.TestCase):
    def test_seed_writes_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeded, path = seed_github_instructions(tmp, "octo/hello")
            self.assertTrue(seeded)
            self.assertEqual(
                path, str(Path(tmp) / GITHUB_INSTRUCTIONS_FILENAME))
            self.assertEqual(GITHUB_INSTRUCTIONS_FILENAME, "AGENTS.md")
            body = Path(path).read_text()
            self.assertIn("octo/hello", body)
            self.assertTrue(body.startswith(GITHUB_INSTRUCTIONS_MARKER))

    def test_seed_yields_to_repo_rules(self):
        # The seed IS AGENTS.md now, so a repo's own AGENTS.md blocks
        # it: repo-authored rules always win, left byte-identical.
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "AGENTS.md"
            own.write_text("# repo's own rules\n")
            seeded, path = seed_github_instructions(tmp, "octo/hello")
            self.assertFalse(seeded)
            self.assertEqual(
                path, str(Path(tmp) / GITHUB_INSTRUCTIONS_FILENAME))
            self.assertEqual(own.read_text(), "# repo's own rules\n")

    def test_seed_never_overwrites_bridge_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / GITHUB_INSTRUCTIONS_FILENAME
            target.write_text("# earlier seed\n")
            seeded, path = seed_github_instructions(tmp, "octo/hello")
            self.assertFalse(seeded)
            self.assertEqual(Path(path).read_text(), "# earlier seed\n")

    def test_has_seeded_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(has_seeded_instructions(tmp))
            seed_github_instructions(tmp, "octo/hello")
            self.assertTrue(has_seeded_instructions(tmp))
            self.assertFalse(has_seeded_instructions(str(Path(tmp) / "nope")))
            self.assertFalse(has_seeded_instructions(None))
            self.assertFalse(has_seeded_instructions(""))

    def test_repo_owned_file_is_not_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENTS.md").write_text("# repo's own rules\n")
            self.assertFalse(has_seeded_instructions(tmp))

    def test_legacy_web_muse_still_recognized(self):
        # Sessions cloned before the rename keep gh auto-approve: the
        # old leaf filename with the seed marker still counts.
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / LEGACY_GITHUB_INSTRUCTIONS_FILENAME
            self.assertEqual(LEGACY_GITHUB_INSTRUCTIONS_FILENAME,
                             "WEB-MUSE.md")
            legacy.write_text(GITHUB_INSTRUCTIONS_MARKER + "\nbody\n")
            self.assertTrue(has_seeded_instructions(tmp))

    def test_legacy_filename_without_marker_is_not_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / LEGACY_GITHUB_INSTRUCTIONS_FILENAME).write_text(
                "# something else\n")
            self.assertFalse(has_seeded_instructions(tmp))


class TestSessionAgents(unittest.TestCase):
    def test_render_plain_session(self):
        out = render_session_agents()
        self.assertTrue(out.startswith(SESSION_AGENTS_MARKER + "\n"))
        self.assertIn("session workspace", out)
        self.assertIn(".web-muse/ORDERS.md", out)
        self.assertNotIn("./", out.split("session workspace", 1)[1].split(
            ".web-muse")[0])

    def test_render_repo_session_names_leaf(self):
        out = render_session_agents("hello", "octo/hello")
        self.assertTrue(out.startswith(SESSION_AGENTS_MARKER + "\n"))
        self.assertIn("./hello/", out)
        self.assertIn("octo/hello", out)
        self.assertIn(".web-muse/ORDERS.md", out)

    def test_seed_writes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeded, path = seed_session_agents(tmp)
            self.assertTrue(seeded)
            self.assertEqual(path, str(Path(tmp) / "AGENTS.md"))
            body = Path(path).read_text()
            self.assertTrue(body.startswith(SESSION_AGENTS_MARKER))
            self.assertTrue(has_seeded_instructions(tmp))
            # Never overwrites: repo checkouts and older seeds win.
            Path(path).write_text("# mine\n")
            seeded, _ = seed_session_agents(tmp, "hello", "octo/hello")
            self.assertFalse(seeded)
            self.assertEqual(Path(path).read_text(), "# mine\n")

    def test_seed_repo_variant_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_session_agents(tmp, "hello", "octo/hello")
            self.assertTrue(has_seeded_instructions(tmp))
            self.assertIn("./hello/",
                          (Path(tmp) / "AGENTS.md").read_text())


class TestSessionRegistration(unittest.TestCase):
    def test_do_new_registers_seeded_leaf(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                seed_github_instructions(tmp, "octo/hello")
                router = SessionRouter(msp=FakeMsp(), workspace_base="/tmp")
                conn = FakeConn()
                result = await router._do_new(
                    conn, {"workspaceRoot": tmp, "sessionId": "sid-seed"})
                self.assertEqual(result["session"]["sessionId"], "sid-seed")
                self.assertIn("sid-seed", router._gh_auto_sids)
        asyncio.run(body())

    def test_do_new_registers_session_agents_root(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                seed_session_agents(tmp, "hello", "octo/hello")
                router = SessionRouter(msp=FakeMsp(), workspace_base="/tmp")
                conn = FakeConn()
                result = await router._do_new(
                    conn, {"workspaceRoot": tmp, "sessionId": "sid-sess"})
                self.assertEqual(result["session"]["sessionId"], "sid-sess")
                self.assertIn("sid-sess", router._gh_auto_sids)
        asyncio.run(body())

    def test_do_new_registers_legacy_leaf(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / LEGACY_GITHUB_INSTRUCTIONS_FILENAME
                 ).write_text(GITHUB_INSTRUCTIONS_MARKER + "\nbody\n")
                router = SessionRouter(msp=FakeMsp(), workspace_base="/tmp")
                conn = FakeConn()
                await router._do_new(
                    conn, {"workspaceRoot": tmp, "sessionId": "sid-leg"})
                self.assertIn("sid-leg", router._gh_auto_sids)
        asyncio.run(body())

    def test_do_new_ignores_unseeded_root(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = SessionRouter(msp=FakeMsp(), workspace_base="/tmp")
                conn = FakeConn()
                await router._do_new(
                    conn, {"workspaceRoot": tmp, "sessionId": "sid-plain"})
                self.assertNotIn("sid-plain", router._gh_auto_sids)
        asyncio.run(body())

    def test_do_new_auto_workspace_seeds_plain_agents(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = SessionRouter(msp=FakeMsp(),
                                       workspace_base=tmp)
                conn = FakeConn()
                result = await router._do_new(
                    conn, {"sessionId": "sid-auto"})
                wsdir = Path(result["workspaceRoot"])
                self.assertEqual(wsdir.parent, Path(tmp))
                body = (wsdir / "AGENTS.md").read_text()
                self.assertTrue(body.startswith(SESSION_AGENTS_MARKER))
                self.assertNotIn("./hello/", body)
        asyncio.run(body())

    def test_do_new_manual_root_gets_no_agents_file(self):
        # A manual root is the user's own dir: orders skill yes (hidden),
        # root-level AGENTS.md no.
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                mine = Path(tmp) / "mine"
                mine.mkdir()
                router = SessionRouter(msp=FakeMsp(),
                                       workspace_base=tmp)
                conn = FakeConn()
                await router._do_new(
                    conn, {"workspaceRoot": str(mine),
                           "sessionId": "sid-man"})
                self.assertFalse((mine / "AGENTS.md").exists())
                self.assertTrue(
                    (mine / ".web-muse" / "ORDERS.md").is_file())
        asyncio.run(body())


class TestExplicitPreapprovedList(unittest.TestCase):
    def test_every_allowlisted_binary_rendered(self):
        out = render_github_instructions("octo/hello")
        for cmd in list(AUTO_ALLOW_INSPECT) + list(AUTO_ALLOW_TOOLCHAINS):
            self.assertIn(f"`{cmd}`", out, cmd)

    def test_git_carveouts_rendered(self):
        out = render_github_instructions("octo/hello")
        self.assertIn(GIT_DENY_SUMMARY, out)
        self.assertIn("## Pre-approved commands", out)


class TestPromptDispositionPassthrough(unittest.TestCase):
    def test_queued_disposition_reaches_client(self):
        async def body():
            msp = FakeMsp()
            seen = {}

            async def command(method, params):
                seen.update(params)
                if method == "turn/start":
                    return {"disposition": "queued", "startedNewTurn": False,
                            "status": {"state": "admitted"},
                            "turnId": "t-9", "commandId": "c-1"}
                return {}
            msp.command = command
            router = SessionRouter(msp=msp, workspace_base="/tmp")
            result = await router._do_prompt(
                FakeConn(), {"sessionId": "sid-q", "text": "follow-up"})
            self.assertEqual(result["disposition"], "queued")
            self.assertEqual(result["turnId"], "t-9")
            self.assertEqual(result["sessionId"], "sid-q")
        asyncio.run(body())


class TestOpenClonedModel(unittest.TestCase):
    def _open(self, open_opts):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "sid-m" / "hello"
                dest.mkdir(parents=True)
                msp = FakeMsp()
                router = SessionRouter(msp=msp, workspace_base="/tmp")
                await router._open_cloned(
                    FakeConn(), "octo/hello", str(dest), "sid-m",
                    "op-m", open_opts)
                starts = [p for m, p in msp.commands
                          if m == "session/start"]
                self.assertEqual(len(starts), 1)
                return starts[0]
        return asyncio.run(body())

    def test_valid_model_forwarded(self):
        p = self._open({"model": {"modelId": "m1", "providerId": "p1"}})
        self.assertEqual(p["modelId"], "m1")
        self.assertEqual(p["providerId"], "p1")

    def test_model_without_provider_ok(self):
        p = self._open({"model": {"modelId": "m1"}})
        self.assertEqual(p["modelId"], "m1")
        self.assertNotIn("providerId", p)

    def test_invalid_model_dropped(self):
        for bad in (None, "m1", {}, {"modelId": "  "},
                    {"providerId": "p1"}):
            p = self._open({"model": bad})
            self.assertNotIn("modelId", p, repr(bad))


class TestOpenClonedRoot(unittest.TestCase):
    def test_roots_at_session_dir_and_seeds_agents(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "sid-r" / "hello"
                dest.mkdir(parents=True)
                msp = FakeMsp()
                router = SessionRouter(msp=msp, workspace_base="/tmp")
                result = await router._open_cloned(
                    FakeConn(), "octo/hello", str(dest), "sid-r",
                    "op-r", {})
                starts = [p for m, p in msp.commands
                          if m == "session/start"]
                self.assertEqual(len(starts), 1)
                # Host root is the session dir, not the clone leaf.
                self.assertEqual(starts[0]["workspaceRoot"],
                                 str(Path(tmp) / "sid-r"))
                self.assertEqual(result["workspaceRoot"],
                                 str(Path(tmp) / "sid-r"))
                # Session AGENTS.md names the leaf; the leaf itself is
                # untouched here (its seed happens in _clone_task).
                agents = (Path(tmp) / "sid-r" / "AGENTS.md").read_text()
                self.assertTrue(agents.startswith(SESSION_AGENTS_MARKER))
                self.assertIn("./hello/", agents)
                self.assertFalse((dest / "AGENTS.md").exists())
                self.assertIn("sid-r", router._gh_auto_sids)
        asyncio.run(body())


class TestTrustDefault(unittest.TestCase):
    def test_trust_workspace_default_on(self):
        self.assertTrue(parse_args([]).trust_workspace)

    def test_trust_workspace_opt_out(self):
        self.assertFalse(parse_args(["--no-trust-workspace"]).trust_workspace)

    def test_serve_argv_follows_flag(self):
        argv = build_serve_argv(parse_args([]))
        self.assertIn("--trust-workspace", argv)
        argv = build_serve_argv(parse_args(["--no-trust-workspace"]))
        self.assertNotIn("--trust-workspace", argv)


class TestSandboxNetwork(unittest.TestCase):
    def test_default_unset_leaves_host_default(self):
        self.assertIsNone(parse_args([]).sandbox_network)
        argv = build_serve_argv(parse_args([]))
        self.assertNotIn("--sandbox-network", argv)

    def test_enabled_passes_through(self):
        argv = build_serve_argv(parse_args(["--sandbox-network", "enabled"]))
        self.assertIn("--sandbox-network", argv)
        self.assertIn("enabled", argv)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--sandbox-network", "wide-open"])

    def test_deployed_unit_enables_network(self):
        unit = (ROOT / "deploy" / "web-muse.service").read_text()
        self.assertIn("--sandbox-network enabled", unit)


class TestWiring(unittest.TestCase):
    def test_seed_plumbing_present(self):
        for marker in ("seed_github_instructions",
                       "render_github_instructions",
                       "has_seeded_instructions",
                       "seed_session_agents",
                       "render_session_agents",
                       "GITHUB_INSTRUCTIONS_FILENAME",
                       "LEGACY_GITHUB_INSTRUCTIONS_FILENAME",
                       "GITHUB_INSTRUCTIONS_MARKER",
                       "SESSION_AGENTS_MARKER",
                       "seededInstructions"):
            self.assertIn(marker, SESSIONS_PY)


if __name__ == "__main__":
    unittest.main()
