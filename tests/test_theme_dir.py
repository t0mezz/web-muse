"""Saved themes dir + /theme command: web/themes/<name>.json palettes.

Dropping a JSON palette into web/themes/ makes it appear under bare
`/theme` (bridge lists the dir at GET /themes); `/theme <name>`
fetches and applies it. These tests pin that contract at the source
level, following tests/test_theme.py, plus a live /themes probe.

Run: python3 -m unittest tests.test_theme_dir -v   (from repo root)
"""

import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.ws import HttpWsServer, list_themes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
THEMES_DIR = ROOT / "web" / "themes"
THEME_JS = (ROOT / "web" / "theme.js").read_text()
APP_JS = (ROOT / "web" / "app.js").read_text()
WS_PY = (ROOT / "server" / "ws.py").read_text()


def theme_colors():
    """Parse the COLORS map out of web/theme.js."""
    m = re.search(r"var COLORS = \{(.*?)\};", THEME_JS, re.S)
    assert m, "COLORS map missing from theme.js"
    return dict(re.findall(r"^\s*([A-Za-z0-9]+):\s*'([^']+)'",
                           m.group(1), re.M))


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class TestThemesDir(unittest.TestCase):
    def test_dir_holds_saved_themes(self):
        self.assertTrue(THEMES_DIR.is_dir(), "web/themes/ missing")
        files = sorted(p.name for p in THEMES_DIR.iterdir()
                       if p.is_file() and p.suffix == ".json")
        self.assertIn("default.json", files)
        self.assertGreaterEqual(len(files), 2,  # dir must show choice
                                f"only {files} saved")

    def test_every_theme_uses_known_keys(self):
        known = set(theme_colors())
        for path in sorted(THEMES_DIR.glob("*.json")):
            with self.subTest(theme=path.name):
                payload = json.loads(path.read_text())
                self.assertIsInstance(payload, dict)
                self.assertTrue(payload, "empty theme file")
                for key, value in payload.items():
                    self.assertIn(key, known, f"unknown color {key!r}")
                    self.assertIsInstance(value, str)
                    self.assertTrue(value, f"empty value for {key!r}")
                    self.assertLessEqual(len(value), 500)

    def test_default_matches_theme_js_palette(self):
        """default.json is the current palette, not a drifted copy."""
        payload = json.loads((THEMES_DIR / "default.json").read_text())
        self.assertEqual(payload, theme_colors())


class TestThemeCommand(unittest.TestCase):
    def test_slash_entry(self):
        self.assertIn('{ name: "theme", usage: "/theme [name]"', APP_JS)
        self.assertIn("cmdTheme", APP_JS)

    def test_bare_lists_and_named_applies(self):
        # Empty argument lists what the bridge reports (uncached, so an
        # edited themes dir lists fresh) ...
        self.assertIn('fetch("themes", { cache: "no-store" })', APP_JS)
        self.assertIn("Apply: /theme <name>", APP_JS)
        # ... a name is fetched, validated against theme.js keys,
        # applied, and persisted like agent theme orders.
        self.assertIn('fetch("themes/" + encodeURIComponent(hit)', APP_JS)
        self.assertIn("T.apply(clean)", APP_JS)
        self.assertIn("localStorage.setItem(T.storageKey", APP_JS)
        self.assertIn("Unknown theme", APP_JS)


class TestThemeReload(unittest.TestCase):
    """One-step theme iteration: editing web/themes/<name>.json takes
    effect via the composer alone — no bridge restart, no switching
    away and back."""

    def test_fetches_bypass_http_cache(self):
        # An edited theme file must come back fresh on re-run, even in
        # browsers that otherwise serve a cached copy of the same URL.
        self.assertIn('fetch("themes", { cache: "no-store" })', APP_JS)
        self.assertIn(
            'fetch("themes/" + encodeURIComponent(hit) + ".json", '
            '{ cache: "no-store" })', APP_JS)

    def test_reload_reapplies_active_theme(self):
        # /theme reload re-fetches the active saved theme and applies
        # it; the active name is recorded on every /theme <name>.
        self.assertIn("cmdThemeReload", APP_JS)
        self.assertIn("web-muse:theme-name", APP_JS)
        self.assertIn("localStorage.setItem(THEME_NAME_KEY", APP_JS)
        self.assertIn("Reload active: /theme reload", APP_JS)


class TestThemesEndpoint(unittest.TestCase):
    def test_list_themes_reads_web_dir(self):
        names = list_themes(ROOT / "web")
        self.assertIn("default", names)
        self.assertEqual(names, sorted(names))

    def test_list_themes_missing_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_themes(Path(tmp) / "nobody-here"), [])

    def test_http_route_serves_json(self):
        self.assertIn('"/themes"', WS_PY)

    def test_serve_themes_probe(self):
        async def body():
            srv = HttpWsServer(router=None, web_dir=str(ROOT / "web"))
            writer = FakeWriter()
            await srv._serve_themes(writer)
            head, payload = bytes(writer.data).split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", head)
            self.assertIn(b"application/json", head)
            names = json.loads(payload)["themes"]
            self.assertIn("default", names)
            self.assertEqual(names, sorted(names))
        asyncio.run(body())

    def test_edited_theme_served_without_restart(self):
        """Rewriting web/themes/<name>.json changes the next GET.

        The bridge reads theme files per request and marks them
        no-store, so iterating on a theme never needs a restart —
        the composer just re-fetches."""
        async def get(srv, path):
            writer = FakeWriter()
            await srv._serve_http(writer, path)
            head, payload = bytes(writer.data).split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", head)
            self.assertIn(b"no-store", head)
            return payload

        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                web = Path(tmp)
                (web / "themes").mkdir()
                (web / "index.html").write_text("x")
                (web / "themes" / "wip.json").write_text(
                    '{"accent": "#111111"}')
                srv = HttpWsServer(router=None, web_dir=str(web))
                first = await get(srv, "/themes/wip.json")
                self.assertIn(b"#111111", first)
                (web / "themes" / "wip.json").write_text(
                    '{"accent": "#222222"}')
                second = await get(srv, "/themes/wip.json")
                self.assertIn(b"#222222", second)
                (web / "themes" / "fresh.json").write_text("{}")
                names = json.loads(
                    await get(srv, "/themes"))["themes"]
                self.assertEqual(names, ["fresh", "wip"])
        asyncio.run(body())


if __name__ == "__main__":
    unittest.main()
