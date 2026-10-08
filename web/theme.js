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

  // Ashen-rose default: near-black surfaces (#0C0C0C base), pale
  // #CCD3DB body text with muted rose secondary roles, rose accent
  // #D25380. Status hues stay warm (ember warn, red err) against the
  // dark base; the starfield sky runs near-black with warm stars.
  var COLORS = {
    bg: '#0C0C0C',
    panel: '#15171A',
    panel2: '#1E2126',
    line: '#33363A',
    fg: '#CCD3DB',
    dim: '#935B6C',
    faint: '#A2717F',
    accent: '#D25380',
    // Keyboard focus ring: the accent itself, which already holds
    // >= 3:1 on every dark surface. Light themes (e.g. parchment)
    // darken it instead; tests/test_focus_rings.py guards the ratio
    // per theme.
    focus: '#D25380',
    ok: '#98614A',
    warn: '#FB3D18',
    err: '#ED1C24',
    user: '#1E2126',
    agent: '#15171A',
    select: '#3A2029',
    warnBg: '#2A1C12',
    warnFg: '#8A9098',
    errFg: '#ED1C24',
    codeBg: '#15171A',
    cardBg: '#1E2126',
    pickedBg: '#2A2E34',
    onOk: '#FFFAF4',
    onAccent: '#0C0C0C',
    chipInk: '#742E46',
    light: '#fff',
    // Non-hex roles (kept as strings; applied verbatim).
    glow: '210, 83, 128',
    scrim: 'rgba(12, 12, 12, 0.55)',
    star: '#F6E3C2',
    starBg0: '#0C0C0C',
    starBg1: '#050607',
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
