"""Expandable settings system: gear icon opens a lightweight grouped panel.

Contract (source level, following tests/test_inspector_toggle.py —
no JS harness in this repo):

- Topbar: a small `#btn-settings` gear sits next to the inspector icon
  and advertises the Ctrl+, shortcut.
- Shell (web/settings.js, classic script like theme.js): WebMuseSettings
  owns open/close/toggle, group navigation, and the persisted active
  group (`webmuse.settingsGroup`); groups register via registerGroup, so
  future groups need no markup/CSS/wiring changes.
- Groups (app.js): Appearance (theme, starfield) and Defaults (effort,
  model, approval mode). Default rows mirror the existing controls
  write-through (mirrorTopbarPick reuses their onchange logic), so every
  pick persists through the existing keys and bridge sends.
- Behavior: Ctrl+, toggles, Esc closes with focus back on the gear,
  scrim click closes, narrow screens keep one overlay drawer, and open
  groups re-render when outside state changes (Settings.sync).

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


class TestSettingsOpenClose(unittest.TestCase):
    def test_panel_markup(self):
        for token in ('id="settings"', 'id="settings-nav"',
                      'id="settings-body"', 'id="btn-close-settings"'):
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

    def test_scrim_closes_settings(self):
        m = re.search(r'el\("scrim"\)\.onclick = \(\) => \{([^}]*)\}', APP_JS)
        self.assertIsNotNone(m)
        self.assertIn("closeSettings()", m.group(1))


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
        self.assertIn("aria-pressed", SETTINGS_JS)

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

    def test_defaults_mirror_existing_controls(self):
        body = body_of("mirrorTopbarPick")
        self.assertIn("onchange", body)
        for picker in ("effort-picker", "model-picker", "approval-mode"):
            self.assertIn(f'mirrorTopbarPick("{picker}"', APP_JS)

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
                   "syncApprovalSelect"):
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
    def test_drawer_styles(self):
        self.assertIn("#settings.open", STYLE_CSS)
        self.assertIn("min(88vw, 340px)", STYLE_CSS)

    def test_narrow_keeps_one_drawer(self):
        self.assertIn('el("settings").classList.remove("open")',
                      body_of("toggleSessions"))
        self.assertIn('el("settings").classList.remove("open")',
                      body_of("toggleInspector"))
        self.assertIn('el("settings")', body_of("syncScrim"))

    def test_mobile_close_button_and_static_desktop(self):
        self.assertIn("#btn-close-settings { display: block; }", STYLE_CSS)
        self.assertIn("#settings { position: static;", STYLE_CSS)

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
        # Scrim path refocuses the gear when settings was open.
        m = re.search(r'el\("scrim"\)\.onclick = \(\) => \{(.*?)\n\};',
                      APP_JS, re.S)
        self.assertIsNotNone(m, "scrim handler missing")
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
        # row must use the same polarity, not the inverse.
        m = re.search(r"function syncStarsToggle\(\) \{(.*?)\n\}", APP_JS, re.S)
        self.assertIsNotNone(m)
        self.assertIn('stopStarsFx ? "true" : "false"', m.group(1))
        self.assertIn("makeToggle(!!stopStarsFx", APP_JS)

if __name__ == "__main__":
    unittest.main()
