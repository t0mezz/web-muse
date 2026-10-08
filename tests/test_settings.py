"""Expandable settings system: gear icon opens a centered grouped overlay.

Contract (source level, following tests/test_inspector_toggle.py —
no JS harness in this repo):

- Topbar: a small `#btn-settings` gear sits next to the inspector icon
  and advertises the Ctrl+, shortcut.
- Shell (web/settings.js, classic script like theme.js): WebMuseSettings
  owns open/close/toggle, group navigation, a versioned settings store,
  a value registry, and modal behavior (focus trap, Esc, background
  inert). Groups register via registerGroup, so future groups need no
  markup/CSS/wiring changes.
- Groups (app.js): Appearance (theme, starfield) and Defaults (effort,
  model, approval mode). Rows and outside controls share canonical
  setters (applyEffortPick/applyModelPick/applyApprovalPick,
  toggleStarsFx, applySettingsTheme) through the registry, so every pick
  persists through the existing keys and bridge sends with no DOM
  scraping between surfaces.
- Behavior: Ctrl+, toggles, Esc closes with focus back on the gear,
  the panel's own backdrop click closes (the scrim owns the drawers
  only), and open groups re-render with focus preserved when outside
  state changes (Settings.sync).

Covers the six pieces: icon placement, open-close, group navigation,
individual row layout, persistence, mobile.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text()
INDEX_HTML = (ROOT / "web" / "index.html").read_text()
STYLE_CSS = (ROOT / "web" / "style.css").read_text()
SETTINGS_JS = (ROOT / "web" / "settings.js").read_text()


def body_of(name, src=APP_JS):
    m = re.search(r"function " + name + r"\([^)]*\) \{(.*?)\n\}", src, re.S)
    assert m, f"{name} missing"
    return m.group(1)


class TestSettingsIcon(unittest.TestCase):
    def test_gear_button_in_topbar(self):
        self.assertIn('id="btn-settings"', INDEX_HTML)
        # Next to the inspector icon, before the panel closes over it.
        self.assertLess(INDEX_HTML.index('id="btn-inspector"'),
                        INDEX_HTML.index('id="btn-settings"'))
        self.assertIn('aria-label="Settings"', INDEX_HTML)

    def test_button_advertises_shortcut(self):
        self.assertIn("Ctrl+,", INDEX_HTML)

    def test_button_wired_to_toggle(self):
        self.assertIn('el("btn-settings").onclick = toggleSettings;', APP_JS)

    def test_ctrl_comma_shortcut(self):
        self.assertIn('} else if (k === ",") {', APP_JS)
        self.assertIn("toggleSettings();", APP_JS)

    def test_script_loaded_before_app(self):
        self.assertIn('<script src="/settings.js"></script>', INDEX_HTML)
        self.assertLess(INDEX_HTML.index("/settings.js"),
                        INDEX_HTML.index("/app.js"))

    def test_gear_size_beats_topbar_base(self):
        # The gear bump must outrank `#topbar button`: ID+element beats
        # a lone ID, so plain `#btn-settings` silently loses (no visual
        # change at any size).
        m = re.search(r"#topbar\s+#btn-settings\s*\{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m, "gear size rule missing")
        self.assertIn("font-size:", m.group(1))

    def test_gear_spins_like_refresh(self):
        # Same one-shot 360° as the session refresh: shared keyframes,
        # reflow-restarted on every toggle press, class cleared after the
        # 650ms one-shot, and disabled under reduced motion.
        self.assertIn("#btn-settings.spin svg", STYLE_CSS)
        self.assertIn("sess-spin-once 0.65s", STYLE_CSS)
        body = body_of("toggleSettings")
        self.assertIn('el("btn-settings")', body)
        self.assertIn("void g.offsetWidth", body)
        self.assertIn('setTimeout(() => g.classList.remove("spin"), 650)', body)


class TestSettingsOpenClose(unittest.TestCase):
    def test_panel_markup(self):
        for token in ('id="settings"', 'id="settings-nav"',
                      'id="settings-body"', 'id="btn-close-settings"',
                      'settings-box', 'role="dialog"'):
            self.assertIn(token, INDEX_HTML)

    def test_toggle_flips_open_state(self):
        body = body_of("toggleSettings")
        # Delegates to the shell (open/close own the .open class there).
        self.assertIn("S.open()", body)
        self.assertIn("S.close()", body)
        self.assertIn("WebMuseSettings", body)

    def test_shell_toggle_contract(self):
        for fn in ("open", "close", "toggle", "isOpen"):
            self.assertIn(fn, SETTINGS_JS)
        self.assertIn('classList.toggle("open")',
                      SETTINGS_JS + APP_JS)

    def test_esc_closes_and_refocuses(self):
        self.assertIn("WebMuseSettings.isOpen()", APP_JS)
        self.assertIn("closeSettings();", APP_JS)
        self.assertIn('el("btn-settings").focus();', APP_JS)

    def test_scrim_ignores_settings(self):
        # Single backdrop: the scrim owns the drawers only; settings has
        # its own backdrop (the #settings click handler), so one surface
        # owns each close.
        m = re.search(r'el\("scrim"\)\.onclick = \(\) => \{(.*?)\n\};',
                      APP_JS, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("closeSettings()", m.group(1))
        self.assertNotIn("isOpen()", m.group(1))


class TestSettingsGroups(unittest.TestCase):
    def test_shell_registers_groups(self):
        self.assertIn("registerGroup", SETTINGS_JS)
        self.assertIn("WebMuseSettings.registerGroup", APP_JS)

    def test_initial_groups(self):
        self.assertIn('id: "appearance"', APP_JS)
        self.assertIn('id: "defaults"', APP_JS)

    def test_group_nav_renders_and_selects(self):
        self.assertIn("settings-nav", SETTINGS_JS)
        self.assertIn("selectGroup", SETTINGS_JS)
        # Tabs use tab semantics only: aria-selected, never the button
        # idiom aria-pressed.
        self.assertIn("aria-selected", SETTINGS_JS)
        self.assertNotIn("aria-pressed", SETTINGS_JS)

    def test_row_layout_helpers(self):
        for helper in ("setting-row", "setting-label", "setting-title",
                       "makeSelect", "makeToggle"):
            self.assertIn(helper, SETTINGS_JS)
        self.assertIn(".setting-row", STYLE_CSS)
        self.assertIn(".setting-select", STYLE_CSS)
        self.assertIn(".setting-toggle", STYLE_CSS)


class TestSettingsPersistence(unittest.TestCase):
    def test_active_group_persists(self):
        self.assertIn("webmuse.settingsGroup", SETTINGS_JS)
        self.assertIn("localStorage.setItem", SETTINGS_JS)
        self.assertIn("localStorage.getItem", SETTINGS_JS)

    def test_defaults_share_canonical_setters(self):
        # One write path per setting: rows and topbar controls call the
        # same apply* functions through the value registry — no DOM
        # scraping, no fake-event dispatch between surfaces.
        self.assertNotIn("mirrorTopbarPick", APP_JS)
        self.assertNotIn("sel.onchange({", APP_JS)
        for fn in ("applyEffortPick", "applyModelPick", "applyApprovalPick"):
            self.assertIn(f"function {fn}(", APP_JS)
        for key in ("effort", "model", "approvalMode", "theme", "starfield"):
            self.assertIn(f'defineSetting("{key}"', APP_JS)
            self.assertIn(f'setSetting("{key}"', APP_JS)
        for key in ("effort", "model", "approvalMode"):
            self.assertIn(f'getSetting("{key}")', APP_JS)

    def test_every_setting_uses_existing_storage(self):
        # Theme (theme.js override key), starfield, effort, model,
        # approval mode — no parallel settings store to drift.
        self.assertIn("activeThemeName()", APP_JS)
        self.assertIn("toggleStarsFx()", APP_JS)
        self.assertIn("EFFORT_TIERS", APP_JS)
        self.assertIn("APPROVAL_MODES", APP_JS)
        self.assertIn("state.models", APP_JS)

    def test_mirrors_refresh_when_outside_changes(self):
        for fn in ("syncStarsToggle", "syncEffortPicker",
                   "syncApprovalSelect", "syncModelPicker"):
            self.assertIn("WebMuseSettings.sync()", body_of(fn),
                          f"{fn} does not refresh settings")
        m = re.search(r"async function refreshModels\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "refreshModels missing")
        self.assertIn("WebMuseSettings.sync()", m.group(1))

    def test_panel_starts_closed(self):
        # Closed by default: init never adds .open (explicit open only).
        m = re.search(r"function init\(\) \{(.*?)\n  \}", SETTINGS_JS, re.S)
        self.assertIsNotNone(m, "settings init missing")
        self.assertNotIn('add("open")', m.group(1))


class TestSettingsMobile(unittest.TestCase):
    def test_overlay_styles(self):
        self.assertIn("#settings.open", STYLE_CSS)
        self.assertIn("inset: 0", STYLE_CSS)
        self.assertIn("justify-content: center", STYLE_CSS)
        self.assertIn(".settings-box", STYLE_CSS)
        self.assertIn("min(480px", STYLE_CSS)
        self.assertNotIn("min(88vw, 340px)", STYLE_CSS)

    def test_modal_independent_of_drawers(self):
        # Centered overlay: the side drawers never shut settings, and
        # the scrim belongs to the drawers only.
        self.assertNotIn('el("settings").classList.remove("open")',
                         body_of("toggleSessions"))
        self.assertNotIn('el("settings").classList.remove("open")',
                         body_of("toggleInspector"))
        self.assertNotIn('el("settings")', body_of("syncScrim"))

    def test_backdrop_click_closes_and_refocuses(self):
        self.assertIn('el("settings").addEventListener("click"', APP_JS)
        self.assertIn('ev.target === el("settings")', APP_JS)

    def test_close_button_and_no_static_drawer(self):
        self.assertIn("#btn-close-settings { display: block; }", STYLE_CSS)
        self.assertNotIn("#settings { position: static;", STYLE_CSS)

    def test_focus_and_touch(self):
        self.assertIn("#btn-settings:focus-visible", STYLE_CSS)
        self.assertIn(".setting-select", STYLE_CSS)
        self.assertIn("16px", STYLE_CSS)




class TestSettingsCriticFixes(unittest.TestCase):
    """Regression tests for the harsh-critic round (blind vs Linear bar)."""

    def test_gear_has_pressed_state(self):
        self.assertIn('aria-pressed="false"', INDEX_HTML)
        body = body_of("syncSettingsGear")
        self.assertIn('aria-pressed', body)
        for fn in ("toggleSettings", "closeSettings"):
            self.assertIn("syncSettingsGear();", body_of(fn))

    def test_gear_mirrors_shell_open_close_events(self):
        # The X button closes inside the shell (settings.js init),
        # bypassing app.js closeSettings(): without an event-driven
        # sync the gear stays pressed after an X close while every
        # other close path (Esc, backdrop, gear toggle) clears it.
        self.assertIn('WebMuseSettings.on("open", syncSettingsGear)',
                      APP_JS)
        self.assertIn('WebMuseSettings.on("close", syncSettingsGear)',
                      APP_JS)

    def test_open_moves_focus_into_panel(self):
        m = re.search(r"function open\(\) \{(.*?)\n  \}", SETTINGS_JS, re.S)
        self.assertIsNotNone(m, "settings open missing")
        self.assertIn('btn-close-settings', m.group(1))
        self.assertIn('.focus()', m.group(1))

    def test_all_close_paths_return_focus(self):
        # Esc path (app.js) refocuses the gear.
        self.assertIn('el("btn-settings").focus();', APP_JS)
        # X button path (settings.js init) refocuses the gear.
        m = re.search(r"function init\(\) \{(.*?)\n  \}", SETTINGS_JS, re.S)
        self.assertIsNotNone(m, "settings init missing")
        self.assertIn('btn-settings', m.group(1))
        self.assertIn('.focus()', m.group(1))
        # Backdrop path (app.js #settings click) refocuses the gear.
        m = re.search(r'el\("settings"\)\.addEventListener\("click", '
                      r'\(ev\) => \{(.*?)\n\}\);', APP_JS, re.S)
        self.assertIsNotNone(m, "settings backdrop handler missing")
        self.assertIn('el("btn-settings").focus();', m.group(1))

    def test_close_button_visible_on_desktop(self):
        m = re.search(r"#btn-close-settings \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m)
        self.assertNotIn("display: none", m.group(1))
        self.assertIn("display: block", m.group(1))

    def test_group_nav_uses_tab_semantics(self):
        self.assertIn('tablist', SETTINGS_JS)
        self.assertIn("'tab'", SETTINGS_JS)
        self.assertIn('aria-selected', SETTINGS_JS)
        self.assertIn('ArrowRight', SETTINGS_JS)
        self.assertIn('ArrowLeft', SETTINGS_JS)

    def test_starfield_switch_matches_topbar_idiom(self):
        # Topbar: checked == field up (stop handle present). The settings
        # registry must expose the same polarity, not the inverse.
        m = re.search(r"function syncStarsToggle\(\) \{(.*?)\n\}", APP_JS, re.S)
        self.assertIsNotNone(m)
        self.assertIn('stopStarsFx ? "true" : "false"', m.group(1))
        m = re.search(r"function defineAppSettings\(\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "defineAppSettings missing")
        self.assertIn("get: () => !!stopStarsFx", m.group(1))
        self.assertIn('getSetting("starfield")', APP_JS)

class TestSettingsHarshCriticFixes(unittest.TestCase):
    """Second harsh-critic round: model, sync, loud API, modal, store."""

    def test_setting_rows_read_state_not_dom(self):
        # No settings render reaches into another surface's live DOM
        # value (the old model row read the topbar picker's .value, which
        # throws when the picker is absent).
        self.assertNotIn('el("model-picker").value', APP_JS)
        self.assertNotIn('el("effort-picker").value', APP_JS)
        self.assertNotIn('el("approval-mode").value ||', APP_JS)

    def test_value_registry_rejects_unknown_keys(self):
        for tok in ("defineSetting", "hasSetting",
                    "getSetting", "setSetting"):
            self.assertIn(tok, SETTINGS_JS)
        self.assertIn("unknown setting", SETTINGS_JS)
        self.assertIn("duplicate settings key", SETTINGS_JS)

    def test_group_api_validates_loudly(self):
        self.assertIn("throw new TypeError", SETTINGS_JS)
        self.assertIn("duplicate settings group id", SETTINGS_JS)
        self.assertIn("unknown settings group", SETTINGS_JS)
        self.assertIn("unregisterGroup", SETTINGS_JS)
        self.assertIn("getGroups", SETTINGS_JS)
        self.assertIn("getActiveGroup", SETTINGS_JS)

    def test_group_events_and_ordering(self):
        for tok in ("emit('open')", "emit('close')", "emit('group'"):
            self.assertIn(tok, SETTINGS_JS)
        self.assertIn("sortGroups", SETTINGS_JS)

    def test_render_failure_names_group(self):
        m = re.search(r"function renderBody\(\) \{(.*?)\n  \}",
                      SETTINGS_JS, re.S)
        self.assertIsNotNone(m, "settings renderBody missing")
        self.assertIn("console.error", m.group(1))
        self.assertIn("setting-error", m.group(1))
        self.assertIn(".setting-error", STYLE_CSS)

    def test_sync_preserves_focus(self):
        # The old sync bailed whenever focus sat inside the panel (which
        # open() guarantees), so async updates never landed while open.
        m = re.search(r"function sync\(\) \{(.*?)\n  \}", SETTINGS_JS, re.S)
        self.assertIsNotNone(m, "settings sync missing")
        self.assertNotIn("if (p.contains(document.activeElement)) return",
                         m.group(1))
        self.assertIn("data-setting-key", m.group(1))
        self.assertIn("renderBody()", m.group(1))
        self.assertIn(".focus()", m.group(1))

    def test_theme_list_cached_and_deduped(self):
        # Every Appearance render used to fetch("themes") with no cache,
        # no dedup, and no stale-render guard.
        self.assertIn("themeNamesCache", APP_JS)
        self.assertIn("THEME_LIST_TTL_MS", APP_JS)
        self.assertIn("function getThemeNames()", APP_JS)
        m = re.search(r"function fillSettingsThemeOptions\(sel\) \{(.*?)\n\}",
                      APP_JS, re.S)
        self.assertIsNotNone(m, "fillSettingsThemeOptions missing")
        self.assertIn("getThemeNames()", m.group(1))
        self.assertIn("isConnected", m.group(1))

    def test_modal_traps_focus_and_inerts_background(self):
        self.assertIn("trapTab", SETTINGS_JS)
        self.assertIn("'Tab'", SETTINGS_JS)
        self.assertIn("n.inert", SETTINGS_JS)
        self.assertIn("setBackgroundInert(true)", SETTINGS_JS)
        self.assertIn("setBackgroundInert(false)", SETTINGS_JS)

    def test_shell_owns_escape(self):
        self.assertIn("onDocumentKeydown", SETTINGS_JS)
        self.assertIn("'Escape'", SETTINGS_JS)
        self.assertIn("addEventListener('keydown', onDocumentKeydown)",
                      SETTINGS_JS)

    def test_tabs_wire_tabpanel(self):
        self.assertIn("tabpanel", SETTINGS_JS)
        self.assertIn("aria-controls", SETTINGS_JS)
        self.assertIn("aria-labelledby", SETTINGS_JS)
        self.assertIn("tabpanel", INDEX_HTML)
        self.assertIn("ArrowUp", SETTINGS_JS)
        self.assertIn("ArrowDown", SETTINGS_JS)

    def test_store_versioned_with_legacy_migration(self):
        self.assertIn("STORE_KEY = 'webmuse.settings'", SETTINGS_JS)
        self.assertIn("STORE_VERSION", SETTINGS_JS)
        self.assertIn("migrat", SETTINGS_JS)
        # The pre-v1 plain-string key still migrates forward on first read.
        self.assertIn("webmuse.settingsGroup", SETTINGS_JS)

    def test_nav_scales_and_rows_wrap(self):
        m = re.search(r"#settings-nav \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m)
        self.assertIn("overflow-x: auto", m.group(1))
        m = re.search(r"#settings-nav button \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m)
        self.assertIn("min-width: max-content", m.group(1))
        m = re.search(r"\.setting-row \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m)
        self.assertIn("flex-wrap: wrap", m.group(1))
        m = re.search(r"\.setting-select \{([^}]*)\}", STYLE_CSS)
        self.assertIsNotNone(m)
        self.assertIn("min(260px", m.group(1))


if __name__ == "__main__":
    unittest.main()
