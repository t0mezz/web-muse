"""GitHub sessions via `gh` (stage 2, gh-only v1).

Bridge: `owner/repo` validation, branch validation, gh argv shapes, row
parsing/filtering, token redaction, error codes, hosts.yml perms,
clone-leaf containment — plus live runner tests against a stub `gh` on
PATH (no network, no auth).
UI (source-level, no JS harness): /github slash family, composer
repo/branch pills with staged first-message clone, and clone-progress
wiring.

Run: python3 -m unittest tests.test_github -v   (from repo root)
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.github as G  # noqa: E402
from server.github import (  # noqa: E402
    GithubError,
    build_branches_argv,
    build_clone_argv,
    build_list_argv,
    filter_repos,
    hosts_file_ok,
    map_gh_error,
    merge_branch_names,
    merge_repo_rows,
    normalize_limit,
    parse_branch_names,
    parse_repo_rows,
    redact,
    run_gh_branches,
    run_gh_clone,
    run_gh_list,
    validate_branch,
    validate_fullname,
)
from server.sessions import SessionRouter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
SESSIONS_PY = "".join(p.read_text() for p in sorted((ROOT / "server" / "sessions").glob("*.py")))
GITHUB_PY = (ROOT / "server" / "github.py").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()

LIST_FIXTURE = [
    {"nameWithOwner": "octo/hello", "isPrivate": False,
     "defaultBranchRef": {"name": "main"},
     "updatedAt": "2026-01-01T00:00:00Z"},
    {"nameWithOwner": "octo/secret", "isPrivate": True,
     "defaultBranchRef": None, "updatedAt": None},
    {"junk": "no full name here"},
    "not-a-dict",
]


class TestValidateFullname(unittest.TestCase):
    def test_valid_passes_through(self):
        self.assertEqual(validate_fullname("octo/hello"), "octo/hello")
        self.assertEqual(validate_fullname("a.b-c_d/e.f-g_h"), "a.b-c_d/e.f-g_h")

    def test_traversal_and_paths_rejected(self):
        for bad in ("../escape", "a/../../b", "/etc/passwd", "a/b/c",
                     "owner/", "/repo", "a b/c", "--help", "", "   ",
                     ".hidden/repo", "octo/.git", "-flags/repo", "a/..",
                     None, 123, ["a/b"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_fullname(bad)


class TestListShape(unittest.TestCase):
    def test_argv_shape(self):
        argv = build_list_argv()
        self.assertEqual(argv[:3], ["gh", "repo", "list"])
        self.assertIn("--json", argv)
        self.assertNotIn("octo", " ".join(argv))  # no repo data in argv

    def test_custom_bin_and_limit(self):
        argv = build_list_argv(5, gh_bin="/tmp/bin/gh")
        self.assertEqual(argv[0], "/tmp/bin/gh")
        self.assertEqual(argv[-1], "5")

    def test_limit_clamped(self):
        self.assertEqual(normalize_limit(500), 100)
        self.assertEqual(normalize_limit(0), 1)
        self.assertEqual(normalize_limit("junk"), 50)
        self.assertEqual(normalize_limit(None), 50)

    def test_parse_rows(self):
        rows = parse_repo_rows(LIST_FIXTURE)
        self.assertEqual(len(rows), 2)  # junk entries skipped
        pub, priv = rows
        self.assertEqual(pub, {"name": "hello", "fullName": "octo/hello",
                               "private": False, "defaultBranch": "main",
                               "updatedAt": "2026-01-01T00:00:00Z"})
        self.assertTrue(priv["private"])
        self.assertIsNone(priv["defaultBranch"])

    def test_parse_non_list_safe(self):
        self.assertEqual(parse_repo_rows({"oops": 1}), [])
        self.assertEqual(parse_repo_rows(None), [])

    def test_prefix_filter(self):
        rows = parse_repo_rows(LIST_FIXTURE)
        self.assertEqual(len(filter_repos(rows, "")), 2)
        self.assertEqual([r["fullName"] for r in filter_repos(rows, "octo/h")],
                         ["octo/hello"])
        self.assertEqual(len(filter_repos(rows, "OCTO/")), 2)  # insensitive
        self.assertEqual(filter_repos(rows, "other/"), [])


class TestCloneArgv(unittest.TestCase):
    def test_argv_shape_shallow(self):
        argv = build_clone_argv("octo/hello", "/tmp/ws/sid/repo")
        self.assertEqual(argv, ["gh", "repo", "clone", "octo/hello",
                                "/tmp/ws/sid/repo", "--", "--depth", "1"])

    def test_argv_is_list_not_shell(self):
        # Must stay an argv list: joining into a shell string would reopen
        # injection through the validated name.
        self.assertIsInstance(build_clone_argv("a/b", "d"), list)

    def test_branch_pins_clone(self):
        argv = build_clone_argv("octo/hello", "/tmp/ws/sid/repo",
                                branch="feature/x")
        self.assertEqual(argv, ["gh", "repo", "clone", "octo/hello",
                                "/tmp/ws/sid/repo", "--", "--depth", "1",
                                "--branch", "feature/x"])

    def test_bad_branch_rejected(self):
        for bad in ("../escape", "-flags", ".hidden", "a//b", "a/",
                    "feat.lock", ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_clone_argv("octo/hello", "d", branch=bad)

    def test_none_branch_clones_default(self):
        argv = build_clone_argv("octo/hello", "d", branch=None)
        self.assertNotIn("--branch", argv)


class TestValidateBranch(unittest.TestCase):
    def test_valid_passes_through(self):
        self.assertEqual(validate_branch("main"), "main")
        self.assertEqual(validate_branch("feature/x-1.2_y"), "feature/x-1.2_y")

    def test_traversal_and_flags_rejected(self):
        for bad in ("../escape", "..", "a/../b", "-x", ".hidden",
                    "a//b", "/lead", "trail/", "a/.git", "a/-x",
                    "x.lock/y", "", "   ", None, 123):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_branch(bad)


class TestBranchesShape(unittest.TestCase):
    def test_argv_shape(self):
        argv = build_branches_argv("octo/hello")
        self.assertEqual(argv[:2], ["gh", "api"])
        self.assertIn("repos/octo/hello/branches", argv[2])
        self.assertNotIn("octo/secret", " ".join(argv))

    def test_parse_names(self):
        payload = [{"name": "main"}, {"name": "dev"},
                   {"nope": 1}, "junk", {"name": ""}, {"name": None}]
        self.assertEqual(parse_branch_names(payload), ["main", "dev"])

    def test_parse_non_list_safe(self):
        self.assertEqual(parse_branch_names({"oops": 1}), [])
        self.assertEqual(parse_branch_names(None), [])


class TestRedact(unittest.TestCase):
    def test_token_shapes_scrubbed(self):
        dirty = ("token github_pat_abc123XYZ and ghp_deadbeef "
                 "at https://secrethere@github.com/octo/hello")
        clean = redact(dirty)
        self.assertNotIn("github_pat_abc123XYZ", clean)
        self.assertNotIn("ghp_deadbeef", clean)
        self.assertNotIn("secrethere@", clean)

    def test_clean_text_untouched(self):
        text = "Cloning into 'repo'... done."
        self.assertEqual(redact(text), text)


class TestMapError(unittest.TestCase):
    def test_unauth(self):
        e = map_gh_error("error: not logged in, run `gh auth login`", 4, "list")
        self.assertEqual(e.code, "gh_unauth")
        self.assertIn("gh auth login", str(e))

    def test_invalid_repo(self):
        for err in ("could not resolve to a repository 'x/y'",
                    "404 Not Found", "repo not found"):
            e = map_gh_error(err, 1, "clone")
            self.assertEqual(e.code, "invalid_repo", err)

    def test_dest_exists(self):
        e = map_gh_error("destination 'repo' already exists", 1, "clone")
        self.assertEqual(e.code, "dest_exists")

    def test_generic_carries_exit_and_op(self):
        e = map_gh_error("boom", 3, "clone")
        self.assertEqual(e.code, "clone_failed")
        self.assertIn("exit 3", str(e))
        self.assertEqual(map_gh_error("boom", 1, "list").code, "list_failed")

    def test_multiline_tail_has_no_brackets(self):
        e = map_gh_error("line1\nline2\nline3", 1, "clone")
        self.assertNotIn("[", str(e).split(": ", 1)[-1][:1])
        self.assertIn("line1\nline2\nline3", str(e))

    def test_tokens_never_in_message(self):
        e = map_gh_error("failing with github_pat_abc123 at "
                         "https://tok@github.com/x", 1, "clone")
        self.assertNotIn("github_pat_abc123", str(e))
        self.assertNotIn("tok@", str(e))


class TestHostsFile(unittest.TestCase):
    def _write(self, d, mode):
        p = Path(d) / "hosts.yml"
        p.write_text("github.com:\n  user: octo\n")
        os.chmod(p, mode)
        return str(p)

    def test_strict_ok(self):
        with tempfile.TemporaryDirectory() as d:
            ok, hint = hosts_file_ok(self._write(d, 0o600))
            self.assertTrue(ok)
            self.assertEqual(hint, "")

    def test_group_readable_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ok, hint = hosts_file_ok(self._write(d, 0o640))
            self.assertFalse(ok)
            self.assertIn("0600", hint)

    def test_missing_means_login_hint(self):
        ok, hint = hosts_file_ok("/no/such/hosts-xyz-123.yml")
        self.assertFalse(ok)
        self.assertIn("gh auth login", hint)


def _stub_gh(bin_dir, mode):
    """Executable fake `gh`: records argv, serves canned list/clone."""
    gh = Path(bin_dir) / "gh"
    if mode == "list":
        gh.write_text("#!/bin/sh\n"
                      'echo "$@" > "$GH_ARGV_LOG"\n'
                      'cat "$GH_JSON_FIXTURE"\n')
    else:
        gh.write_text("#!/bin/sh\n"
                      'echo "$@" > "$GH_ARGV_LOG"\n'
                      'if [ "$GH_CLONE_FAIL" = "1" ]; then\n'
                      "  echo \"could not resolve to a repository\" >&2\n"
                      "  exit 1\n"
                      "fi\n"
                      'echo "Cloning into \'$4\'..." >&2\n'
                      'mkdir -p "$4"\n')
    gh.chmod(0o755)
    return str(gh)


class GhStubCase(unittest.TestCase):
    """run_gh_* against a stub `gh` on PATH: no network, no real auth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = str(Path(self.tmp.name) / "bin")
        os.makedirs(self.bin)
        self.argv_log = str(Path(self.tmp.name) / "argv")
        fixture = Path(self.tmp.name) / "repos.json"
        fixture.write_text(json.dumps(LIST_FIXTURE))
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin + os.pathsep + self.old_path
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        for k, v in (("GH_ARGV_LOG", self.argv_log),
                     ("GH_JSON_FIXTURE", str(fixture))):
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(os.environ.__setitem__ if old is not None else
                            os.environ.pop, k, *([old] if old is not None else []))
        self.old_hosts = G.hosts_file_ok
        G.hosts_file_ok = lambda path=None: (True, "")  # runner, not perms
        self.addCleanup(setattr, G, "hosts_file_ok", self.old_hosts)

    def _argv(self):
        return Path(self.argv_log).read_text().split()


class TestStubList(GhStubCase):
    def test_rows_and_search(self):
        _stub_gh(self.bin, "list")
        rows = asyncio.run(run_gh_list())
        self.assertEqual([r["fullName"] for r in rows],
                         ["octo/hello", "octo/secret"])
        self.assertIn("repo list", " ".join(self._argv()))
        self.assertIn("--json", self._argv())
        rows = asyncio.run(run_gh_list(search="octo/s"))
        self.assertEqual([r["fullName"] for r in rows], ["octo/secret"])

    def test_bad_json_is_list_failed(self):
        _stub_gh(self.bin, "list")
        Path(os.environ["GH_JSON_FIXTURE"]).write_text("{nope")
        with self.assertRaises(GithubError) as cm:
            asyncio.run(run_gh_list())
        self.assertEqual(cm.exception.code, "list_failed")


class TestStubClone(GhStubCase):
    def test_clone_argv_and_dest(self):
        _stub_gh(self.bin, "clone")
        with tempfile.TemporaryDirectory() as ws:
            dest = str(Path(ws) / "sid" / "repo")
            seen = []
            out = asyncio.run(run_gh_clone(
                "octo/hello", dest, on_line=seen.append))
            self.assertEqual(out, dest)
            self.assertTrue(Path(dest).is_dir())
            argv = self._argv()
            self.assertEqual(argv[:4], ["repo", "clone", "octo/hello", dest])
            self.assertEqual(argv[-3:], ["--", "--depth", "1"])
            self.assertTrue(any("Cloning" in line for line in seen))

    def test_clone_failure_maps_code(self):
        _stub_gh(self.bin, "clone")
        os.environ["GH_CLONE_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "GH_CLONE_FAIL")
        with tempfile.TemporaryDirectory() as ws:
            dest = str(Path(ws) / "sid" / "repo")
            with self.assertRaises(GithubError) as cm:
                asyncio.run(run_gh_clone("octo/nope", dest))
            self.assertEqual(cm.exception.code, "invalid_repo")
            self.assertFalse(Path(dest).exists())  # half-clone removed

    def test_missing_binary_is_gh_missing(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(GithubError) as cm:
                asyncio.run(run_gh_clone("octo/hello",
                                         str(Path(ws) / "repo"),
                                         gh_bin="/no/such/gh-xyz-123"))
            self.assertEqual(cm.exception.code, "gh_missing")


class TestStubBranches(GhStubCase):
    BRANCH_FIXTURE = [{"name": "main", "protected": True},
                      {"name": "dev"},
                      {"junk": "no name here"}]

    def test_names_and_argv(self):
        _stub_gh(self.bin, "list")
        Path(os.environ["GH_JSON_FIXTURE"]).write_text(
            json.dumps(self.BRANCH_FIXTURE))
        names = asyncio.run(run_gh_branches("octo/hello"))
        self.assertEqual(names, ["main", "dev"])
        argv = self._argv()
        self.assertEqual(argv[0], "api")
        self.assertIn("repos/octo/hello/branches", argv[1])

    def test_bad_json_is_branches_failed(self):
        _stub_gh(self.bin, "list")
        Path(os.environ["GH_JSON_FIXTURE"]).write_text("{nope")
        with self.assertRaises(GithubError) as cm:
            asyncio.run(run_gh_branches("octo/hello"))
        self.assertEqual(cm.exception.code, "branches_failed")

    def test_invalid_fullname_rejected(self):
        _stub_gh(self.bin, "list")
        with self.assertRaises(ValueError):
            asyncio.run(run_gh_branches("../escape"))


class TestRepoDirName(unittest.TestCase):
    def test_slug_used_as_is(self):
        from server.github import repo_dir_name
        self.assertEqual(repo_dir_name("octo/hello"), "hello")
        self.assertEqual(repo_dir_name("a.b-c_d/e.f-g_h"), "e.f-g_h")

    def test_unsafe_slugs_fall_back(self):
        from server.github import repo_dir_name
        # A bare but dir-safe slug passes through (callers always pass
        # validated owner/repo; this is just the last-segment rule).
        self.assertEqual(repo_dir_name("no-slash"), "no-slash")
        for bad in ("octo/.hidden", "octo/-flags", "octo/..", "octo/",
                    "", "   ", None, 123, ["a/b"]):
            self.assertEqual(repo_dir_name(bad), "repo", repr(bad))

    def test_safe_leaf_names(self):
        from server.github import is_safe_leaf_name
        self.assertTrue(is_safe_leaf_name("hello"))
        self.assertTrue(is_safe_leaf_name("repo"))  # legacy leaf
        for bad in ("", ".hidden", "-flags", ".", "..", "a/b",
                    "has space", None, 123):
            self.assertFalse(is_safe_leaf_name(bad), repr(bad))


class TestCloneLeaf(unittest.TestCase):
    def test_leaf_shape(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-1")
            self.assertEqual(dest, str(Path(base) / "sid-1" / "repo"))

    def test_named_leaf_from_fullname(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-1n", "octo/hello")
            self.assertEqual(dest, str(Path(base) / "sid-1n" / "hello"))

    def test_unsafe_sid_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            for bad in ("../escape", "", ".hidden"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    r._clone_leaf(bad)

    def test_existing_git_is_dest_exists(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-2", "octo/hello")
            (Path(dest) / ".git").mkdir(parents=True)
            with self.assertRaises(GithubError) as cm:
                r._clone_leaf("sid-2", "octo/hello")
            self.assertEqual(cm.exception.code, "dest_exists")

    def test_residue_cleared_for_retry(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-3", "octo/hello")
            Path(dest).mkdir(parents=True)
            (Path(dest) / "partial.out").write_text("half")
            self.assertEqual(r._clone_leaf("sid-3", "octo/hello"), dest)
            self.assertFalse(Path(dest).exists())

    def test_no_base_refuses(self):
        r = SessionRouter(msp=None, workspace_base=None)
        with self.assertRaises(ValueError):
            r._clone_leaf("sid-4")


class TestGithubClean(unittest.TestCase):
    def test_removes_named_leaf_only(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-5", "octo/hello")
            (Path(dest) / ".git").mkdir(parents=True)
            (Path(dest) / "f").write_text("x")
            keep = Path(base) / "sid-5" / "AGENTS.md"
            keep.write_text("# session workspace\n")
            out = r._clean_github_clone("sid-5")
            self.assertEqual(out, {"removed": True, "dest": dest})
            self.assertFalse(Path(dest).exists())
            self.assertTrue(keep.is_file())  # session file untouched

    def test_removes_legacy_repo_leaf(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            dest = r._clone_leaf("sid-5b")
            (Path(dest) / ".git").mkdir(parents=True)
            out = r._clean_github_clone("sid-5b")
            self.assertEqual(out, {"removed": True, "dest": dest})

    def test_bare_dir_without_git_is_not_a_clone(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            wsdir = Path(base) / "sid-5c"
            wsdir.mkdir()
            (wsdir / "scratch.txt").write_text("x")
            out = r._clean_github_clone("sid-5c")
            self.assertFalse(out["removed"])
            self.assertTrue((wsdir / "scratch.txt").is_file())

    def test_missing_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            out = r._clean_github_clone("sid-6")
            self.assertFalse(out["removed"])

    def test_unsafe_sid_refused(self):
        with tempfile.TemporaryDirectory() as base:
            r = SessionRouter(msp=None, workspace_base=base)
            with self.assertRaises(ValueError):
                r._clean_github_clone("../escape")


class FakeConn:
    """Minimal WS conn: collects frames, tracks sessions."""

    def __init__(self):
        self.frames = []
        self.sessions = set()

    def queue_frame(self, frame):
        self.frames.append(frame)


class TestRouterCloneDispatch(unittest.TestCase):
    """Full dispatch chain with stub `gh` binaries (no network).

    Clones are admitted instantly and finish in the background, so the
    dispatch loop stays responsive: cancel is processed mid-clone.
    """

    def _router(self, tmp, gh_body):
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\n" + gh_body)
        gh.chmod(0o755)
        old_hosts = G.hosts_file_ok
        G.hosts_file_ok = lambda path=None: (True, "")
        self.addCleanup(setattr, G, "hosts_file_ok", old_hosts)
        return SessionRouter(msp=None, workspace_base=str(Path(tmp) / "ws"),
                             gh_bin=str(gh))

    async def _wait_result(self, conn, op_id, timeout=15):
        for _ in range(int(timeout * 10)):
            for f in conn.frames:
                if f.get("method") == "githubCloneResult" \
                        and f["params"].get("opId") == op_id:
                    return f["params"]
            await asyncio.sleep(0.1)
        raise AssertionError(f"no githubCloneResult for {op_id}")

    def test_clone_accepted_then_result_event(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(
                    tmp, 'mkdir -p "$4/.git"\n'
                         'echo "Cloning into..." >&2\n'
                         'echo "done." >&2\n')
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubClone",
                           "fullName": "octo/hello", "opId": "op-1",
                           "sessionId": "sid-x"})
                self.assertTrue(reply["ok"], reply)
                accepted = reply["result"]
                self.assertTrue(accepted["accepted"])
                # Background completion lands as a global result event.
                params = await self._wait_result(conn, "op-1")
                self.assertTrue(params["ok"], params)
                res = params["result"]
                self.assertEqual(res["fullName"], "octo/hello")
                self.assertTrue(Path(res["dest"], ".git").is_dir())
                phases = [f["params"]["phase"] for f in conn.frames
                          if f.get("method") == "githubCloneProgress"]
                self.assertEqual(phases[0], "started")
                self.assertEqual(phases[-1], "completed")
                self.assertIn("progress", phases)
                # Global frames carry no sessionId: every client renders them.
                for f in conn.frames:
                    self.assertNotIn("sessionId", f["params"])
        asyncio.run(body())

    def test_cancel_preempts_hanging_clone(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp, "sleep 60\n")
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubClone",
                           "fullName": "octo/hello", "opId": "op-hang",
                           "sessionId": "sid-h"})
                self.assertTrue(reply["ok"], reply)
                # The dispatch loop is free (launch returned at once), so a
                # cancel sent mid-clone is processed, not queued behind it.
                cancel = await router.handle_client_message(
                    conn, {"id": 2, "type": "githubCancel",
                           "opId": "op-hang"})
                self.assertTrue(cancel["ok"], cancel)
                self.assertTrue(cancel["result"]["cancelled"])
                params = await self._wait_result(conn, "op-hang")
                self.assertFalse(params["ok"])
                self.assertEqual(params["error"]["code"], "clone_cancelled")
                phases = [f["params"]["phase"] for f in conn.frames
                          if f.get("method") == "githubCloneProgress"]
                self.assertIn("cancelled", phases)
                # Exactly one terminal event, even though the cancelled task
                # also runs its CancelledError handler afterwards.
                await asyncio.sleep(0.5)
                results = [f for f in conn.frames
                           if f.get("method") == "githubCloneResult"]
                self.assertEqual(len(results), 1)
        asyncio.run(body())

    def test_cancel_unknown_op(self):
        router = SessionRouter(msp=None, workspace_base="/tmp")
        conn = FakeConn()
        reply = asyncio.run(router.handle_client_message(
            conn, {"id": 1, "type": "githubCancel", "opId": "nope"}))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"].get("code"), "unknown_op")

    def test_branches_dispatch(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(
                    tmp, 'echo \'[{"name": "main"}, {"name": "dev"}]\'\n')
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubBranches",
                           "fullName": "octo/hello"})
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["result"]["branches"],
                                 ["main", "dev"])
                bad = await router.handle_client_message(
                    conn, {"id": 2, "type": "githubBranches",
                           "fullName": "../escape"})
                self.assertFalse(bad["ok"])
        asyncio.run(body())

    def test_clone_with_branch_reaches_gh(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(
                    tmp, f'echo "$@" > "{tmp}/argv"\n'
                         'mkdir -p "$4/.git"\n'
                         'echo "done." >&2\n')
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubClone",
                           "fullName": "octo/hello", "opId": "op-b",
                           "sessionId": "sid-b", "branch": "dev"})
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["result"]["branch"], "dev")
                params = await self._wait_result(conn, "op-b")
                self.assertTrue(params["ok"], params)
                argv = Path(tmp, "argv").read_text().split()
                self.assertEqual(argv[-2:], ["--branch", "dev"])
                self.assertIn("--depth", argv)
        asyncio.run(body())

    def test_clone_bad_branch_rejected(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp, 'mkdir -p "$4/.git"\n')
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubClone",
                           "fullName": "octo/hello", "opId": "op-bad",
                           "sessionId": "sid-bad", "branch": "../x"})
                self.assertFalse(reply["ok"])
        asyncio.run(body())


class TestGithubWiring(unittest.TestCase):
    def test_bridge_dispatch(self):
        for marker in ('"githubRepos"', '"githubBranches"', '"githubClone"',
                       '"githubCancel"', '"githubClean"', '"githubOpen"',
                       "_do_github_clone", "_do_github_open",
                       "_clean_github_clone", "_clone_leaf", "_launch_clone",
                       "_clone_task", "_emit_clone_result",
                       "run_gh_branches", "validate_branch",
                       "githubCloneProgress", "githubCloneResult"):
            self.assertIn(marker, SESSIONS_PY)
        for marker in ("build_branches_argv", "parse_branch_names",
                       "run_gh_branches", "validate_branch"):
            self.assertIn(marker, GITHUB_PY)

    def test_no_shell_in_github_path(self):
        self.assertNotIn("shell=True", SESSIONS_PY)
        self.assertNotIn("shell=True", (ROOT / "server" / "github.py").read_text())

    def test_slash_family(self):
        for marker in ('name: "github"', "cmdGithub", "githubCloneRepo",
                       "githubOpenRepo", "cmdGithubClean", "cmdGithubCancel",
                       "cmdGithubList"):
            self.assertIn(marker, APP_JS)

    def test_picker_and_progress(self):
        for marker in ("onGithubProgress", "onGithubResult", "githubPending",
                       "githubCloneProgress", "githubCloneResult",
                       "markRootAllowed", "private"):
            self.assertIn(marker, APP_JS)
        # Composer pills replaced the sidebar drawer: repo/branch
        # selectors, drop-up menus, and the staged first-message flow.
        for marker in ("repo-bar", "repo-pill", "branch-pill", "repo-menu",
                       "branch-menu", "pendingRepo", "githubBranches",
                       "updateRepoBar", "selectRepo", "selectBranch",
                       "submitWithRepo", "sendPromptText", "closeRepoMenus",
                       "stagedPrompt"):
            self.assertIn(marker, APP_JS)
        for gone in ("btn-github", "github-panel", "github-list",
                     "github-status", "renderGithubList",
                     "toggleGithubPanel", "github-search"):
            self.assertNotIn(gone, APP_JS)

    def test_composer_pills_markup(self):
        # Pill buttons + upward menus live just above the composer input;
        # the sidebar picker is gone.
        for marker in ('id="repo-bar"', 'id="repo-pill"',
                       'id="branch-pill"', 'id="repo-menu"',
                       'id="branch-menu"', 'id="branch-wrap"',
                       'id="repo-pill-label"', 'id="branch-pill-label"'):
            self.assertIn(marker, INDEX_HTML)
        for gone in ('id="github-panel"', 'id="github-list"',
                     'id="github-status"', 'id="btn-github"',
                     "github-search"):
            self.assertNotIn(gone, INDEX_HTML)

    def test_first_message_clones_before_prompt(self):
        # Staged text is handed to githubOpen (with branch) and only sent
        # as a prompt once the rooted session exists.
        self.assertIn("submitWithRepo(text)", APP_JS)
        self.assertIn("githubOpenRepo(sel.fullName, { branch: sel.branch })",
                      APP_JS)
        self.assertIn("await sendPromptText(t, true)", APP_JS)


class TestMergeHelpers(unittest.TestCase):
    def _row(self, full, branch="main"):
        return {"name": full.split("/")[-1], "fullName": full,
                "private": False, "defaultBranch": branch,
                "updatedAt": None}

    def test_repo_union_fresh_first_cached_tails(self):
        cached = [self._row("octo/a"), self._row("octo/b")]
        fresh = [self._row("octo/b", "dev"), self._row("octo/c")]
        merged = merge_repo_rows(cached, fresh)
        self.assertEqual([r["fullName"] for r in merged],
                         ["octo/b", "octo/c", "octo/a"])
        # Fresh wins on conflicts.
        self.assertEqual(merged[0]["defaultBranch"], "dev")

    def test_repo_empty_and_junk_safe(self):
        self.assertEqual(merge_repo_rows([], []), [])
        self.assertEqual(merge_repo_rows(None, None), [])
        rows = merge_repo_rows([{"junk": 1}, "nope"],
                               [self._row("octo/a"), {"junk": 2}])
        self.assertEqual([r["fullName"] for r in rows], ["octo/a"])

    def test_branch_union_order_and_dedup(self):
        self.assertEqual(
            merge_branch_names(["main", "old"], ["dev", "main"]),
            ["dev", "main", "old"])
        self.assertEqual(merge_branch_names(None, ["a"]), ["a"])
        self.assertEqual(merge_branch_names(["a"], None), ["a"])


def _repo_payload(names):
    return [{"nameWithOwner": n, "isPrivate": False,
             "defaultBranchRef": {"name": "main"},
             "updatedAt": None} for n in names]


class TestRouterGithubCache(unittest.TestCase):
    """Known repos/branches are cached; `gh` is still checked each call."""

    def _router(self, tmp):
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        # Fixture-driven stub: rewrite fixture.json between calls to
        # prove each listing re-queries `gh` instead of serving cache.
        gh.write_text('#!/bin/sh\ncat "%s"\n' % (Path(tmp) / "fixture.json"))
        gh.chmod(0o755)
        old_hosts = G.hosts_file_ok
        G.hosts_file_ok = lambda path=None: (True, "")
        self.addCleanup(setattr, G, "hosts_file_ok", old_hosts)
        return SessionRouter(msp=None, workspace_base=str(Path(tmp) / "ws"),
                             gh_bin=str(gh))

    def _write(self, tmp, payload):
        Path(tmp, "fixture.json").write_text(json.dumps(payload))

    def test_repos_accumulate_and_requery(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp)
                conn = FakeConn()
                self._write(tmp, _repo_payload(["octo/a"]))
                first = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubRepos"})
                self.assertTrue(first["ok"], first)
                self.assertEqual(
                    [r["fullName"] for r in first["result"]["repos"]],
                    ["octo/a"])
                self.assertNotIn("stale", first["result"])
                # New repo appears: `gh` was checked again, not skipped.
                self._write(tmp, _repo_payload(["octo/b"]))
                second = await router.handle_client_message(
                    conn, {"id": 2, "type": "githubRepos"})
                self.assertTrue(second["ok"], second)
                self.assertEqual(
                    [r["fullName"] for r in second["result"]["repos"]],
                    ["octo/b", "octo/a"])
                # Filtered calls merge without evicting the known set.
                filt = await router.handle_client_message(
                    conn, {"id": 3, "type": "githubRepos",
                           "search": "octo/a"})
                self.assertEqual(
                    [r["fullName"] for r in filt["result"]["repos"]],
                    ["octo/a"])
                third = await router.handle_client_message(
                    conn, {"id": 4, "type": "githubRepos"})
                self.assertEqual(
                    [r["fullName"] for r in third["result"]["repos"]],
                    ["octo/b", "octo/a"])
        asyncio.run(body())

    def test_repos_failure_serves_stale(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp)
                conn = FakeConn()
                self._write(tmp, _repo_payload(["octo/a"]))
                ok = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubRepos"})
                self.assertTrue(ok["ok"], ok)
                gh = Path(router._gh_bin)
                gh.write_text('#!/bin/sh\necho "boom" >&2\nexit 1\n')
                stale = await router.handle_client_message(
                    conn, {"id": 2, "type": "githubRepos"})
                self.assertTrue(stale["ok"], stale)
                self.assertTrue(stale["result"].get("stale"))
                self.assertEqual(
                    [r["fullName"] for r in stale["result"]["repos"]],
                    ["octo/a"])
        asyncio.run(body())

    def test_repos_failure_empty_cache_errors(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp)
                Path(tmp, "fixture.json").write_text("{nope")
                conn = FakeConn()
                reply = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubRepos"})
                self.assertFalse(reply["ok"])
                self.assertNotIn("stale", reply.get("result", {}))
        asyncio.run(body())

    def test_branches_cache_fallback_and_invalid(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                router = self._router(tmp)
                conn = FakeConn()
                self._write(tmp, [{"name": "main"}, {"name": "dev"}])
                first = await router.handle_client_message(
                    conn, {"id": 1, "type": "githubBranches",
                           "fullName": "octo/hello"})
                self.assertTrue(first["ok"], first)
                self.assertEqual(first["result"]["branches"],
                                 ["main", "dev"])
                self._write(tmp, [{"name": "feature"}])
                second = await router.handle_client_message(
                    conn, {"id": 2, "type": "githubBranches",
                           "fullName": "octo/hello"})
                self.assertEqual(second["result"]["branches"],
                                 ["feature", "main", "dev"])
                gh = Path(router._gh_bin)
                gh.write_text('#!/bin/sh\necho "boom" >&2\nexit 1\n')
                stale = await router.handle_client_message(
                    conn, {"id": 3, "type": "githubBranches",
                           "fullName": "octo/hello"})
                self.assertTrue(stale["ok"], stale)
                self.assertTrue(stale["result"].get("stale"))
                self.assertEqual(stale["result"]["branches"],
                                 ["feature", "main", "dev"])
                # Invalid names are still rejected, never served stale.
                bad = await router.handle_client_message(
                    conn, {"id": 4, "type": "githubBranches",
                           "fullName": "../escape"})
                self.assertFalse(bad["ok"])
        asyncio.run(body())

    def test_cache_wiring_markers(self):
        self.assertIn("merge_repo_rows", SESSIONS_PY)
        self.assertIn("merge_branch_names", SESSIONS_PY)
        self.assertIn("_known_repos", SESSIONS_PY)
        self.assertIn("_known_branches", SESSIONS_PY)
        self.assertIn("merge_repo_rows", GITHUB_PY)
        self.assertIn("merge_branch_names", GITHUB_PY)


if __name__ == "__main__":
    unittest.main()
