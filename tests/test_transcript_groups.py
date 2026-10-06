"""Transcript density in the session transcript (source-level).

- Consecutive same-tool rows collapse into one expandable group
  ("edit x 3 — app.js"); expanding shows the actual calls.
- Each live turn gets a collapsible rollup block whose head doubles
  as the completion record (tools used, files touched, tokens).
- Long tool output collapses past 12 lines behind a "show all" toggle.
- Failed rows never collapse: they force their group open and tinted.
- Toolbar filter chips narrow the transcript to edits, commands, or errors.

Run: python3 -m unittest tests.test_transcript_groups -v   (from repo root)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
STYLE = (ROOT / "web" / "style.css").read_text()
INDEX = (ROOT / "web" / "index.html").read_text()


def fn_body(src, name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n\}",
                  src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestGroupHelpers(unittest.TestCase):
    def test_key_derives_from_tool_type(self):
        body = fn_body(APP_JS, "toolGroupKey")
        self.assertIn("toolType(it)", body)
        self.assertIn("toLowerCase()", body)

    def test_label_counts_members(self):
        body = fn_body(APP_JS, "toolGroupLabel")
        self.assertIn("n", body)
        self.assertIn("\u00d7", body)

    def test_group_uses_native_disclosure(self):
        body = fn_body(APP_JS, "makeToolGroup")
        self.assertIn('"details"', body)
        self.assertIn('"summary"', body)
        self.assertIn("tool-group", body)
        self.assertIn("tool-group-items", body)

    def test_header_refreshes_and_dissolves(self):
        body = fn_body(APP_JS, "refreshToolGroup")
        self.assertIn("toolGroupLabel", body)
        # A lone row dissolves back to a plain line.
        self.assertIn("n < 2", body)
        self.assertIn("group.remove()", body)

    def test_streaming_never_groups(self):
        for name in ("groupToolLine", "groupToolLinePrepend"):
            self.assertIn("streaming", fn_body(APP_JS, name),
                          f"{name} ignores streaming state")


class TestRenderPaths(unittest.TestCase):
    def test_live_path_groups_completed_tools(self):
        body = fn_body(APP_JS, "renderItem")
        self.assertIn("groupToolLine(rec)", body)
        self.assertIn('kind === "tool"', body)
        self.assertIn("!streaming", body)

    def test_kind_changes_leave_the_group(self):
        body = fn_body(APP_JS, "renderItem")
        self.assertIn("ungroupToolLine(rec.line)", body)

    def test_prepend_path_groups_symmetrically(self):
        self.assertIn("groupToolLinePrepend(rec)",
                      fn_body(APP_JS, "renderItemPrepend"))

    def test_removals_restore_the_group(self):
        # Reminder-evicted rows leave via the group-aware remover (they
        # may sit inside a group if their kind changed); local echoes
        # are user rows and never group, so they remove directly.
        self.assertIn("removeGroupedLine(rec.line)",
                      fn_body(APP_JS, "renderItem"))
        self.assertIn("rec.line.remove();",
                      fn_body(APP_JS, "reconcileLocalEcho"))

    def test_existing_contracts_intact(self):
        # Echo reconciliation and reminder filtering still run on both
        # render paths.
        for name in ("renderItem", "renderItemPrepend"):
            body = fn_body(APP_JS, name)
            self.assertIn("reconcileLocalEcho(it)", body)
            self.assertIn("isReminder(it)", body)


class TestGroupStyles(unittest.TestCase):
    def test_group_rules_present(self):
        for token in (".tool-group", ".tool-group-head",
                      ".tool-group-items"):
            self.assertIn(token, STYLE, f"{token} missing")

    def test_head_reads_as_a_row(self):
        m = re.search(r"\.tool-group-head \{(.*?)\}", STYLE, re.S)
        self.assertIsNotNone(m)
        self.assertIn("cursor: pointer", m.group(1))
        self.assertIn("var(--mono)", m.group(1))


def case_body(marker):
    m = re.search(r'case "' + re.escape(marker) + r'":(.*?)break;',
                  APP_JS, re.S)
    assert m, f'case "{marker}" missing'
    return m.group(1)


class TestGroupFileHeaders(unittest.TestCase):
    def test_files_extracted_from_path_like_args(self):
        body = fn_body(APP_JS, "toolFiles")
        self.assertIn("/path|file/i", body)
        # Basenames, not full paths, land in the header.
        self.assertIn('split("/")', body)

    def test_string_args_parsed(self):
        self.assertIn("JSON.parse", fn_body(APP_JS, "toolArgsObj"))

    def test_header_lists_files(self):
        body = fn_body(APP_JS, "toolGroupLabel")
        self.assertIn("files", body)
        self.assertIn('", "', body)

    def test_refresh_aggregates_member_files(self):
        body = fn_body(APP_JS, "refreshToolGroup")
        self.assertIn("dataset.files", body)
        self.assertIn("toolGroupLabel", body)

    def test_rows_tagged_for_headers(self):
        self.assertIn("tagToolLine(rec)", fn_body(APP_JS, "groupToolLine"))
        self.assertIn("tagToolLine(rec)",
                      fn_body(APP_JS, "groupToolLinePrepend"))


class TestFailureRouting(unittest.TestCase):
    def test_failed_status_detected(self):
        body = fn_body(APP_JS, "toolFailed")
        self.assertIn('"failed"', body)
        self.assertIn("isError", body)

    def test_failed_rows_skip_grouping(self):
        body = fn_body(APP_JS, "groupToolLine")
        self.assertIn("toolFailed(rec.item)", body)
        self.assertIn("ungroupToolLine(rec.line)", body)

    def test_failed_member_forces_group_open(self):
        body = fn_body(APP_JS, "refreshToolGroup")
        self.assertIn("data-failed", body)
        self.assertIn(".open = true", body)

    def test_failed_styles_present(self):
        self.assertIn(".tool-group[data-failed", STYLE)
        self.assertIn(".turn-head.failed", STYLE)
        self.assertIn(".tline.tool[data-failed", STYLE)


class TestOutputCollapse(unittest.TestCase):
    def test_limits_defined(self):
        self.assertIn("OUT_MAX_LINES = 12", APP_JS)
        self.assertIn("OUT_MAX_CHARS = 2000", APP_JS)

    def test_completed_tool_rows_collapse(self):
        body = fn_body(APP_JS, "renderItem")
        self.assertIn("maybeCollapseOutput(rec", body)

    def test_toggle_offers_full_output(self):
        body = fn_body(APP_JS, "maybeCollapseOutput")
        self.assertIn("show all", body)
        self.assertIn("show less", body)
        self.assertIn("out-toggle", body)

    def test_toggle_styles_present(self):
        self.assertIn(".out-toggle", STYLE)


class TestTurnRollup(unittest.TestCase):
    def test_block_helpers_exist(self):
        for name in ("turnContainer", "openTurnBlock", "recordTurnItem",
                     "closeTurnBlock", "dissolveTurnBlock"):
            self.assertIn(f"function {name}(", APP_JS, f"{name} missing")

    def test_block_head_collapses_turn(self):
        body = fn_body(APP_JS, "openTurnBlock")
        self.assertIn("turn-head", body)
        self.assertIn('"collapsed"', body)
        self.assertIn("aria-expanded", body)

    def test_head_summarizes_tools_and_files(self):
        body = fn_body(APP_JS, "closeTurnBlock")
        self.assertIn("t.tools", body)
        self.assertIn("t.files", body)

    def test_turn_started_opens_block(self):
        self.assertIn("openTurnBlock();", case_body("turn/started"))

    def test_turn_completed_closes_block(self):
        body = case_body("turn/completed")
        self.assertIn("closeTurnBlock(", body)
        # Missed turn/started still prints the legacy completion line.
        self.assertIn("turn done · ", body)

    def test_retraction_dissolves_block(self):
        self.assertIn("dissolveTurnBlock();", case_body("turn/retracted"))

    def test_live_rows_land_in_open_block(self):
        for name in ("renderItem", "sysLine"):
            self.assertIn("turnContainer()", fn_body(APP_JS, name),
                          f"{name} bypasses the turn block")

    def test_reset_drops_block(self):
        self.assertIn("state.turnBlock = null", fn_body(APP_JS, "clearTranscript"))

    def test_turn_styles_present(self):
        for token in (".turn-block", ".turn-head", ".turn-block.collapsed"):
            self.assertIn(token, STYLE, f"{token} missing")


class TestFilterChips(unittest.TestCase):
    def test_chip_markup_present(self):
        self.assertIn('id="tx-filters"', INDEX)
        for f in ("all", "edits", "commands", "errors"):
            self.assertIn(f'data-txf="{f}"', INDEX)
        self.assertIn('aria-pressed="true"', INDEX)

    def test_categories_defined(self):
        body = fn_body(APP_JS, "txVisible")
        for token in ('"edits"', '"commands"', '"errors"',
                      '"edit"', '"bash"', '"error"'):
            self.assertIn(token, body, f"{token} missing")

    def test_renders_apply_filter(self):
        for name in ("renderItem", "renderItemPrepend", "sysLine"):
            self.assertIn("applyTranscriptFilter();", fn_body(APP_JS, name),
                          f"{name} leaves stale visibility")

    def test_empty_groups_and_blocks_hide(self):
        body = fn_body(APP_JS, "applyTranscriptFilter")
        self.assertIn(".tool-group", body)
        self.assertIn(".turn-block", body)

    def test_reset_restores_all(self):
        body = fn_body(APP_JS, "clearTranscript")
        self.assertIn('state.txFilter = "all"', body)
        self.assertIn("#tx-filters button", body)

    def test_chip_styles_present(self):
        self.assertIn("#tx-filters", STYLE)
        self.assertIn('aria-pressed="true"', STYLE)


if __name__ == "__main__":
    unittest.main()
