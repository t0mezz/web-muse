"""Collapsible workflow panel below the composer.

The panel renders overall progress in its header (counts + bar) plus a
step list, driven strictly by published events: kind:"workflow" items
via item/started|delta|updated|completed and the todo list via
session/todoListChanged. It renders synchronously in the event path
(first progress with the start event itself), never shows a started
state without progress, and its open state persists across reloads.

Field names are pinned to the `muse schema` MSP v1 bundle (stable):
Item{kind,itemId,children,status,label,objective,workflowRunId,
entryId,scriptId,triggerSource,recordedAt,durationMs},
WorkflowChild{childId,attempt,status,label,phase,terminal,durationMs,
failureReason,failureKind,resultRef,usage},
SessionTodoListChangedParams{items,revision,
sourceTool}, TodoItem{status,text,activeForm}.

No JS harness in this repo, so the contract is guarded at the source
level, following tests/test_panel_persist.py.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX = (ROOT / "web" / "index.html").read_text()
CSS = (ROOT / "web" / "style.css").read_text()


def body_of(name):
    m = re.search(r"function " + name + r"\((.*?)\) \{(.*?)\n\}", APP_JS, re.S)
    assert m, f"{name} missing"
    return m.group(2)


def case_body(label):
    m = re.search(r'case "' + label + r'":(.*?)break;', APP_JS, re.S)
    assert m, f"case {label} missing"
    return m.group(1)


class TestWorkflowPanelMarkup(unittest.TestCase):
    def test_panel_sits_between_composer_and_hintbar(self):
        form_at = INDEX.index('</form>')
        panel_at = INDEX.index('id="workflow-panel"')
        hint_at = INDEX.index('id="hintbar"')
        self.assertLess(form_at, panel_at)
        self.assertLess(panel_at, hint_at)

    def test_header_carries_progress_and_body_is_list(self):
        for wid in ("workflow-head", "workflow-title", "workflow-count",
                    "workflow-body"):
            self.assertIn(f'id="{wid}"', INDEX)
        # No bar: overall progress is the counts text only.
        self.assertNotIn("workflow-track", INDEX)
        self.assertNotIn("workflow-fill", INDEX)
        self.assertIn('aria-controls="workflow-body"', INDEX)
        self.assertIn('aria-expanded=', INDEX)
        self.assertIn('role="status"', INDEX)
        # Hidden until the first published event lands.
        m = re.search(r'<section id="workflow-panel"[^>]*>', INDEX)
        self.assertIn("hidden", m.group(0))


class TestWorkflowPanelLive(unittest.TestCase):
    def test_every_item_event_tracks_workflow(self):
        for label in ("item/started", "item/delta", "item/completed"):
            with self.subTest(label=label):
                self.assertIn("trackWorkflowItem(p.item)",
                              case_body(label))
        # Deltas carry no item member per schema (delta/field/itemId):
        # tracking must stay inside the defensive `if (p.item)` branch.
        self.assertIn("if (p.item)", case_body("item/delta"))

    def test_todo_event_renders_instead_of_break(self):
        body = case_body("session/todoListChanged")
        self.assertIn("onTodoListChanged(p)", body)

    def test_history_and_resubscribe_hydrate(self):
        self.assertIn("trackWorkflowItem(it)",
                      body_of("openSession"))
        self.assertIn("trackWorkflowItem(e.item)",
                      body_of("handleSubscribeResult"))

    def test_session_switch_resets(self):
        body = body_of("clearTranscript")
        self.assertIn("state.workflow = null", body)
        self.assertIn("state.todos = null", body)
        self.assertIn("renderWorkflow()", body)

    def test_state_fields(self):
        self.assertIn("workflow: null, todos: null", APP_JS)
        self.assertIn("workflowOpen: true", APP_JS)


class TestWorkflowPanelFields(unittest.TestCase):
    def test_item_fields_match_schema(self):
        body = body_of("trackWorkflowItem")
        for field in ("it.kind", "it.itemId", "it.children", "it.status",
                      "it.label", "it.objective", "it.workflowRunId",
                      "it.entryId", "it.scriptId",
                      "it.triggerSource", "it.recordedAt", "it.durationMs"):
            self.assertIn(field, body)

    def test_child_and_todo_fields_match_schema(self):
        body = body_of("renderWorkflow")
        for field in ("c.label", "c.childId", "c.phase", "c.attempt",
                      "c.durationMs", "c.status", "c.terminal",
                      "c.failureReason", "t.text", "t.activeForm",
                      "t.status"):
            self.assertIn(field, body)
        todo = body_of("onTodoListChanged")
        for field in ("p.items", "p.revision", "p.sourceTool"):
            self.assertIn(field, todo)

    def test_header_progress_and_no_bare_started_state(self):
        body = body_of("renderWorkflow")
        self.assertIn('el("workflow-count")', body)
        self.assertNotIn("workflow-fill", body)
        self.assertIn("done}/${total}", body)
        # Zero steps still render a status line, never an empty start.
        self.assertIn("starting…", body)
        self.assertIn("status: ", body)


class TestWorkflowPanelRunSetup(unittest.TestCase):
    def test_meta_row_markup(self):
        for wid in ("workflow-meta", "workflow-status", "workflow-def",
                    "workflow-runid", "workflow-trigger", "workflow-started",
                    "workflow-elapsed"):
            self.assertIn(f'id="{wid}"', INDEX)
        # Run-only: hidden until a run renders.
        m = re.search(r'<div id="workflow-meta"[^>]*>', INDEX)
        self.assertIn("hidden", m.group(0))
        head_at = INDEX.index('id="workflow-head"')
        meta_at = INDEX.index('id="workflow-meta"')
        body_at = INDEX.index('id="workflow-body"')
        self.assertLess(head_at, meta_at)
        self.assertLess(meta_at, body_at)
        # Definition first: "file · #run · Triggered via source" order.
        status_at = INDEX.index('id="workflow-status"')
        def_at = INDEX.index('id="workflow-def"')
        runid_at = INDEX.index('id="workflow-runid"')
        self.assertLess(status_at, def_at)
        self.assertLess(def_at, runid_at)

    def test_render_populates_meta(self):
        body = body_of("renderWorkflow")
        for wid in ("workflow-meta", "workflow-status", "workflow-def",
                    "workflow-runid", "workflow-trigger", "workflow-started",
                    "workflow-elapsed"):
            self.assertIn(f'el("{wid}")', body)
        self.assertIn("wf-pill", body)
        self.assertIn("wfElapsedText(run)", body)
        self.assertIn("durationMs", body_of("wfElapsedText"))
        self.assertIn("startedAt", body_of("wfElapsedText"))

    def test_meta_shows_definition_and_trigger_provenance(self):
        body = body_of("renderWorkflow")
        # Definition identity prefers the file-like scriptId, falls back
        # to the entry name, and never repeats the title.
        self.assertIn("run.scriptId", body)
        self.assertIn("run.entryId", body)
        self.assertIn("v !== titleText", body)
        # Trigger provenance answers why, not just "via".
        self.assertIn("Triggered via ", body)
        # The title itself names the workflow when the wire gave one.
        self.assertIn("run.label || run.entryId || run.scriptId", body)

    def test_meta_selectors_present(self):
        for sel in ("#workflow-meta", ".wf-pill", ".wf-meta",
                    "#workflow-def", "#workflow-runid", "#workflow-elapsed"):
            self.assertIn(sel, CSS)


class TestWorkflowStepDetail(unittest.TestCase):
    def test_render_builds_expandable_step_rows(self):
        body = body_of("renderWorkflow")
        self.assertIn('document.createElement("details")', body)
        self.assertIn("wf-detail", body)
        self.assertIn('document.createElement("summary")', body)
        self.assertIn("wf-sum", body)
        self.assertIn("wfStepDetail(s)", body)

    def test_detail_carries_status_timestamps_and_output(self):
        body = body_of("wfStepDetail")
        self.assertIn("status: ", body)
        self.assertIn("first seen", body)
        self.assertIn("updated", body)
        self.assertIn("wf-log", body)
        render = body_of("renderWorkflow")
        for field in ("c.failureKind", "c.resultRef", "c.usage"):
            self.assertIn(field, render)

    def test_failed_forces_open_and_toggle_survives_rerender(self):
        body = body_of("renderWorkflow")
        self.assertIn("openKeys", body)
        self.assertIn("details[data-key][open]", body)
        self.assertIn("det.open = true", body)

    def test_step_clocks_tracked_per_child_attempt(self):
        body = body_of("trackWorkflowItem")
        self.assertIn("stepSeen", body)
        self.assertIn("wfStepKey", body)
        self.assertIn("Date.now()", body)

    def test_detail_selectors_present(self):
        for sel in (".wf-detail", ".wf-sum", ".wf-info", ".wf-log"):
            self.assertIn(sel, CSS)

    def test_summary_has_focus_ring(self):
        rules = re.findall(
            r"([^{}]*:focus-visible[^{}]*)\{([^}]*)\}", CSS)
        bodies = [b for sels, b in rules
                  if ".wf-sum:focus-visible" in sels]
        self.assertTrue(bodies, ".wf-sum has no :focus-visible rule")
        for body in bodies:
            self.assertIn("outline: 2px solid var(--focus)", body)


class TestWorkflowAnnotations(unittest.TestCase):
    def test_strip_markup_above_step_list(self):
        self.assertIn('id="workflow-annotations"', INDEX)
        m = re.search(r'<div id="workflow-annotations"[^>]*>', INDEX)
        self.assertIn("hidden", m.group(0))
        meta_at = INDEX.index('id="workflow-meta"')
        ann_at = INDEX.index('id="workflow-annotations"')
        body_at = INDEX.index('id="workflow-body"')
        self.assertLess(meta_at, ann_at)
        self.assertLess(ann_at, body_at)

    def test_render_rolls_up_step_failures(self):
        body = body_of("renderWorkflow")
        self.assertIn('el("workflow-annotations")', body)
        self.assertIn("s.fail || s.failKind", body)
        self.assertIn("Annotations: ", body)
        self.assertIn("ann.hidden", body)
        self.assertIn("wf-ann", body)

    def test_collapse_hides_body_only(self):
        # The strip must survive the panel fold with the meta row.
        self.assertIn("#workflow-panel:not(.open) #workflow-body", CSS)
        self.assertNotIn(":not(.open) #workflow-annotations", CSS)
        for sel in ("#workflow-annotations", ".wf-ann-head", ".wf-ann"):
            self.assertIn(sel, CSS)


class TestWorkflowPanelToggle(unittest.TestCase):
    def test_toggle_persists(self):
        self.assertIn('"webmuse.workflowOpen"', APP_JS)
        body = body_of("toggleWorkflow")
        self.assertIn("localStorage.setItem", body)
        self.assertIn("WORKFLOW_OPEN_KEY", body)
        self.assertIn("syncWorkflowOpen()", body)

    def test_restore_defaults_open(self):
        body = body_of("restoreWorkflowOpen")
        self.assertIn("localStorage.getItem", body)
        self.assertIn("WORKFLOW_OPEN_KEY", body)
        self.assertIn("v === null ? true", body)

    def test_head_bound_and_restored_at_init(self):
        self.assertIn('el("workflow-head").addEventListener("click"',
                      APP_JS)
        self.assertIn("restoreWorkflowOpen();", APP_JS)


class TestWorkflowPanelStyle(unittest.TestCase):
    def test_selectors_present(self):
        for sel in ("#workflow-panel", "#workflow-head", "#workflow-count",
                    "#workflow-body", ".wf-step"):
            self.assertIn(sel, CSS)
        self.assertNotIn("#workflow-track", CSS)
        self.assertNotIn("#workflow-fill", CSS)

    def test_theme_tokens_only_in_panel_block(self):
        block = CSS.split("/* workflow panel:")[1].split("/* slash popup:")[0]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("gradient", block)
        self.assertIn("var(--", block)

    def test_head_has_focus_ring(self):
        rules = re.findall(
            r"([^{}]*:focus-visible[^{}]*)\{([^}]*)\}", CSS)
        bodies = [b for sels, b in rules
                  if "#workflow-head:focus-visible" in sels]
        self.assertTrue(bodies, "#workflow-head has no :focus-visible rule")
        for body in bodies:
            self.assertIn("outline: 2px solid var(--focus)", body)


if __name__ == "__main__":
    unittest.main()
