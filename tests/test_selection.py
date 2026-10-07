"""Selection system for session list.

- Ctrl/Cmd+click toggles selection (desktop), 500ms long-press on mobile
- Click toggles when selection active
- Shift+click selects range between anchor and target
- Ticker pill in top side-head row with search/refresh, fully hidden when 0; trash to its right
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()


class TestSelectionState(unittest.TestCase):
    def test_state_fields(self):
        self.assertIn("selectedIds: new Set()", APP_JS)
        self.assertIn("selectionAnchor", APP_JS)

    def test_helpers_exist(self):
        for name in ["getVisibleSessionIds", "syncSelectionUI", "toggleSelection", "selectRange", "clearSelection", "deleteSelected"]:
            self.assertIn(f"function {name}", APP_JS)

    def test_visible_ids_respects_filter(self):
        # getVisibleSessionIds must mirror renderSessionList filter + sort
        self.assertIn('el("session-filter").value', APP_JS)
        self.assertIn("bridgeCreated", APP_JS)


class TestSelectionInteractions(unittest.TestCase):
    def test_ctrl_cmd_toggle(self):
        self.assertIn("e.ctrlKey || e.metaKey", APP_JS)
        self.assertIn("toggleSelection", APP_JS)

    def test_shift_range(self):
        self.assertIn("e.shiftKey", APP_JS)
        self.assertIn("selectRange", APP_JS)
        self.assertIn("selectionAnchor", APP_JS)

    def test_long_press_mobile(self):
        self.assertIn("touchstart", APP_JS)
        self.assertIn("500", APP_JS)
        self.assertIn("navigator.vibrate", APP_JS)
        self.assertIn("touchend", APP_JS)

    def test_click_again_deselects(self):
        # toggle logic deletes if already present
        self.assertIn("state.selectedIds.has(sid)", APP_JS)
        self.assertIn("state.selectedIds.delete", APP_JS)


class TestSelectionUI(unittest.TestCase):
    def test_ticker_pinned_left_below_status(self):
        self.assertIn('id="selection-ticker"', INDEX_HTML)
        # Pill in top side-head row, fully hidden when 0
        self.assertIn(".ticker", STYLE_CSS)
        self.assertIn("#selection-ticker", STYLE_CSS)
        idx_ticker = INDEX_HTML.index('id="selection-ticker"')
        idx_trash = INDEX_HTML.index('id="btn-delete-selected"')
        idx_search = INDEX_HTML.index('id="btn-search-sessions"')
        # ticker in side-head, trash to its right, both before search/refresh
        self.assertLess(idx_ticker, idx_trash)
        self.assertLess(idx_trash, idx_search)
        # ticker is inside side-head (not in selection-bar)
        side_head = INDEX_HTML.split('id="sess-count"')[0]
        self.assertIn('id="selection-ticker"', side_head)

    def test_trash_on_search_refresh_height(self):
        self.assertIn('id="btn-delete-selected"', INDEX_HTML)
        # trash in side-head, hidden until selection active
        self.assertIn("#btn-delete-selected", STYLE_CSS)
        self.assertIn("trash.hidden", APP_JS)
        self.assertIn(".side-head", STYLE_CSS)
        # trash is in side-head before search/refresh
        idx_trash = INDEX_HTML.index('id="btn-delete-selected"')
        idx_search = INDEX_HTML.index('id="btn-search-sessions"')
        self.assertLess(idx_trash, idx_search)

    def test_sync_visibility(self):
        self.assertIn('ticker.hidden = n === 0', APP_JS)
        self.assertIn('trash.hidden = n === 0', APP_JS)

    def test_selected_row_styled(self):
        self.assertIn(".session-row.selected", STYLE_CSS)
        self.assertIn('row.classList.toggle("selected"', APP_JS)

    def test_ticker_clears_and_escape(self):
        self.assertIn('el("selection-ticker").onclick = clearSelection', APP_JS)
        self.assertIn('el("btn-delete-selected").onclick = deleteSelected', APP_JS)
        self.assertIn('key === "Escape"', APP_JS)

    def test_delete_no_confirmation(self):
        # verification removed: delete is immediate, no confirm()
        self.assertIn('type: "delete"', APP_JS)
        # ensure deleteSelected body does not gate on confirm
        body = APP_JS.split("async function deleteSelected")[1].split("async function")[0]
        self.assertNotIn("confirm(", body)
        body2 = APP_JS.split("async function deleteSession")[1].split("\n}")[0]
        self.assertNotIn("confirm(", body2)


if __name__ == "__main__":
    unittest.main()
