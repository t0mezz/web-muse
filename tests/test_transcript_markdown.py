"""Session transcript markdown rendering (source-level, no JS harness).

The transcript bodies in web/app.js render agent/user items as markdown
once complete: `setBodyContent` picks markdown for completed agent/user
rows and plain text otherwise. `renderMarkdown` is dependency-free and
XSS-safe (HTML-escaped first, scheme-checked links), and streaming
deltas stay plain text until item/completed re-renders.

Run: python3 -m unittest tests.test_transcript_markdown -v   (from repo root)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_JS = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text()
STYLE = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text()


def fn_body(src, name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestRendererExists(unittest.TestCase):
    def test_helpers_present(self):
        for name in ("escapeHtml", "sanitizeUrl", "renderInline",
                     "renderMarkdown", "setBodyContent"):
            self.assertIn(f"function {name}(", APP_JS, f"{name} missing")

    def test_no_external_markdown_dependency(self):
        self.assertNotRegex(APP_JS, r"require\(['\"]marked|from ['\"]marked")
        self.assertNotIn("cdn.jsdelivr.net/npm/marked", APP_JS)
        self.assertNotIn("unpkg.com/marked", APP_JS)


class TestSafety(unittest.TestCase):
    def test_raw_html_is_escaped_before_shaping(self):
        body = fn_body(APP_JS, "renderMarkdown")
        self.assertIn("escapeHtml(deFenced)", body)
        # Fence contents are escaped at emit time, not passed through raw.
        self.assertIn("escapeHtml(f.code)", body)

    def test_dangerous_schemes_rejected(self):
        body = fn_body(APP_JS, "sanitizeUrl")
        # Unknown schemes (javascript:, data:, ...) fall through to null;
        # only the allowlist passes.
        self.assertRegex(body, r"return null")
        for scheme in ("http:", "https:", "mailto:"):
            self.assertIn(f'"{scheme}"', body)

    def test_links_open_safely(self):
        body = fn_body(APP_JS, "renderInline")
        self.assertIn('target="_blank"', body)
        self.assertIn("noopener", body)


class TestCoverage(unittest.TestCase):
    def test_block_subset(self):
        body = fn_body(APP_JS, "renderMarkdown")
        for token in ("<pre><code", "<h", "<blockquote>",
                      "<table>", "<hr>", "<p>", "<li>"):
            self.assertIn(token, body, f"{token} missing")
        # List tags are chosen dynamically (ordered vs unordered).
        self.assertIn('"ol"', body)
        self.assertIn('"ul"', body)

    def test_inline_subset(self):
        body = fn_body(APP_JS, "renderInline")
        for token in ("<code>", "<strong>", "<em>", "<del>", "<a href="):
            self.assertIn(token, body, f"{token} missing")

    def test_code_spans_sheltered_from_inline_rules(self):
        body = fn_body(APP_JS, "renderInline")
        # Inline code is stashed to placeholders before links/emphasis run.
        self.assertLess(body.index("IC"), body.index("<strong>"))


class TestWiring(unittest.TestCase):
    def test_completed_agent_and_user_use_markdown(self):
        body = fn_body(APP_JS, "setBodyContent")
        self.assertIn("renderMarkdown", body)
        self.assertIn('"agent"', body)
        self.assertIn('"user"', body)
        self.assertIn("streaming", body)

    def test_tool_system_error_stay_plain(self):
        body = fn_body(APP_JS, "setBodyContent")
        # Only agent/user take the markdown branch; everything else keeps
        # the textContent path.
        self.assertNotIn('"tool"', body)
        self.assertIn("textContent", body)

    def test_render_paths_use_set_body_content(self):
        for name in ("renderItem", "renderItemPrepend"):
            self.assertIn("setBodyContent(",
                          fn_body(APP_JS, name), f"{name} bypasses markdown")
        self.assertNotIn("rec.body.textContent = txt",
                         fn_body(APP_JS, "renderItem"))

    def test_streaming_stays_plain(self):
        delta = fn_body(APP_JS, "appendDelta")
        self.assertIn('classList.remove("md")', delta)
        self.assertIn("textContent", delta)
        self.assertNotIn("renderMarkdown", delta)


class TestStyles(unittest.TestCase):
    def test_markdown_styles_scoped_to_transcript(self):
        for sel in (".tline .txt.md", ".tline .txt.md pre",
                    ".tline .txt.md code", ".tline .txt.md a",
                    ".tline .txt.md table", ".tline .txt.md blockquote"):
            self.assertIn(sel, STYLE, f"{sel} missing")


if __name__ == "__main__":
    unittest.main()
