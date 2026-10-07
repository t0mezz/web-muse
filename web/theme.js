// theme.js — frontend color config (single source of truth).
// Plain classic script: no imports, no exports, no frameworks.
// Load with <script src="/theme.js"></script> BEFORE stars.js/app.js,
// then every major color lives in WebMuseTheme.colors and reaches CSS
// as :root custom properties.
//
// The values below ARE the current colors, so the file doubles as the
// fallback: when no override is present the UI looks exactly as before.
// Optional overrides: a JSON object in localStorage["web-muse:theme"]
// (e.g. {"accent": "#ff0000"}) is merged over the defaults and applied
// to :root on load. Corrupt or missing overrides are ignored silently.
//
// Deliberately NOT centralized (generic depth effects, not palette):
// rgba(0,0,0,.*) shadows/scrims and rgba(255,255,255,.*) hairline
// highlights stay inline in style.css.

(function (root) {
  'use strict';

  var STORAGE_KEY = 'web-muse:theme';

  // Warm-cream palette (Color Hunt fcf0daaeac78f2c46a4c4541):
  // cream #FCF0DA, olive #AEAC78, gold #F2C46A, espresso #4C4541.
  // Four hues cannot fill every role, so supporting shades are linear
  // mixes of the palette extremes (e.g. panel = cream + 6% espresso);
  // text roles are mixed to hold >= ~4:1 contrast on their surfaces.
  // The palette has no red: err/errFg are gold deepened toward espresso
  // (bronze), kept distinct from fg by hue and lightness.
  var COLORS = {
    bg: '#FCF0DA',
    panel: '#F1E6D1',
    panel2: '#E9DDC9',
    line: '#D5CAB8',
    fg: '#4C4541',
    dim: '#736B63',
    faint: '#81786F',
    accent: '#AEAC78',
    // Keyboard focus ring: accent mixed 2/3 toward espresso so the
    // 2px :focus-visible outline holds >= 3:1 on every light surface
    // (accent alone only reaches ~1.5-2.3:1 there). Dark themes reuse
    // their accent, which already passes; tests/test_focus_rings.py
    // guards the ratio per theme.
    focus: '#6D6753',
    ok: '#78735A',
    warn: '#F2C46A',
    err: '#8B7551',
    user: '#F6D89C',
    agent: '#E9DDC9',
    select: '#F6D89C',
    warnBg: '#F4CC7E',
    warnFg: '#71614A',
    errFg: '#77664C',
    codeBg: '#EEE2CE',
    cardBg: '#F8DEAD',
    pickedBg: '#F6D697',
    onOk: '#FCF0DA',
    onAccent: '#4C4541',
    chipInk: '#4C4541',
    light: '#fff',
    // Non-hex roles (kept as strings; applied verbatim).
    glow: '174, 172, 120',
    scrim: 'rgba(241, 230, 209, 0.4)',
    star: '#4C4541',
    starBg0: '#F7DAA2',
    starBg1: '#FCF0DA',
  };

  // Theme key -> CSS custom property. Most are --<key>; mapped here so
  // legacy CSS names (--glow-color) survive without a stylesheet churn.
  var VAR_NAMES = {
    glow: '--glow-color',
    scrim: '--scrim',
  };

  function varName(key) {
    return VAR_NAMES[key] || ('--' + key);
  }

  // Last palette produced by apply(): the live theme. get() reads from
  // here so canvas/inline-style consumers (stars.js) follow /theme
  // switches and stored overrides; COLORS stays the pristine default.
  var applied = null;

  function get(key, fallback) {
    if (applied && Object.prototype.hasOwnProperty.call(COLORS, key) &&
        typeof applied[key] === 'string' && applied[key]) {
      return applied[key];
    }
    var v = COLORS[key];
    if (typeof v === 'string' && v) return v;
    return fallback !== undefined ? fallback : v;
  }

  function apply(overrides) {
    var doc = root.document;
    if (!doc || !doc.documentElement) return {};
    var merged = {};
    var k;
    for (k in COLORS) {
      if (Object.prototype.hasOwnProperty.call(COLORS, k)) {
        merged[k] = COLORS[k];
      }
    }
    if (overrides) {
      for (k in overrides) {
        if (Object.prototype.hasOwnProperty.call(overrides, k) &&
            typeof overrides[k] === 'string' && overrides[k]) {
          merged[k] = overrides[k];
        }
      }
    }
    var style = doc.documentElement.style;
    for (k in merged) {
      if (Object.prototype.hasOwnProperty.call(merged, k)) {
        try {
          style.setProperty(varName(k), merged[k]);
        } catch (_) {}
      }
    }
    applied = merged;
    return merged;
  }

  function loadOverrides() {
    try {
      var store = root.localStorage;
      if (!store) return null;
      var raw = store.getItem(STORAGE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  var Theme = {
    colors: COLORS,
    varName: varName,
    get: get,
    apply: apply,
    storageKey: STORAGE_KEY,
  };

  root.WebMuseTheme = Theme;
  apply(loadOverrides());
})(typeof window !== 'undefined' ? window : this);
