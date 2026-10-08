// settings.js — centered settings overlay shell (Linear-style lightweight groups).
// Plain classic script: no imports, no exports, no frameworks (like theme.js).
// Load with <script src="/settings.js"></script> BEFORE app.js; app.js
// defines the settings, registers the groups, and calls
// WebMuseSettings.init() at startup.
//
// The shell owns the open target, open/close, group navigation, the
// persisted settings store, and modal behavior (focus trap, Esc,
// background inert). Each setting row is one quiet label-plus-control
// line. Values live in one registry, not in the DOM:
//
//   WebMuseSettings.defineSetting("effort", {
//     get: function () { return currentEffort(); },
//     set: function (v) { applyEffort(v); } });
//   WebMuseSettings.registerGroup({ id: "defaults", title: "Defaults",
//     render: function (body, h) { ... h.makeSelect(...) ... } });
//
// render(body, h) appends rows built with the h.row/makeSelect/makeToggle
// helpers and reads live values through getSetting, so rows and outside
// controls (topbar pickers, starfield switch) share the same canonical
// setters: no DOM scraping, no fake events. The shell re-renders the open
// group on WebMuseSettings.sync(), preserving focus on the edited control.
//
// Programmer errors throw loudly (TypeError/RangeError/Error): invalid
// groups, duplicate ids, unknown groups or settings. Render failures
// log the real error and show which group failed instead of wiping the
// panel silently.
(function (root) {
  'use strict';

  var STORE_KEY = 'webmuse.settings';
  var STORE_VERSION = 1;
  var LEGACY_GROUP_KEY = 'webmuse.settingsGroup';
  var PANEL_ID = 'settings';
  var BODY_ID = 'settings-body';
  var NAV_ID = 'settings-nav';

  var groups = [];
  var groupSeq = 0;
  var activeId = null;
  var inited = false;
  var opener = null;

  var settingDefs = {};

  var listeners = { open: [], close: [], group: [] };

  function el(id) { return document.getElementById(id); }

  function warn(msg) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[WebMuseSettings] ' + msg);
    }
  }

  function findGroup(id) {
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].id === id) return groups[i];
    }
    return null;
  }

  function sortGroups() {
    groups.sort(function (a, b) {
      var ao = (typeof a.order === 'number') ? a.order : Infinity;
      var bo = (typeof b.order === 'number') ? b.order : Infinity;
      if (ao !== bo) return ao - bo;
      return a.seq - b.seq;
    });
  }

  function registerGroup(g) {
    if (!g || typeof g !== 'object') {
      throw new TypeError('registerGroup needs {id, title, render}');
    }
    if (typeof g.id !== 'string' || !g.id) {
      throw new TypeError('registerGroup needs a non-empty string id');
    }
    if (typeof g.title !== 'string' || !g.title) {
      throw new TypeError('registerGroup("' + g.id + '") needs a non-empty string title');
    }
    if (typeof g.render !== 'function') {
      throw new TypeError('registerGroup("' + g.id + '") needs a render(body, helpers) function');
    }
    if (g.order !== undefined && typeof g.order !== 'number') {
      throw new TypeError('registerGroup("' + g.id + '") order must be a number');
    }
    if (findGroup(g.id)) {
      throw new Error('duplicate settings group id "' + g.id + '" (unregister first)');
    }
    groups.push({ id: g.id, title: g.title, render: g.render,
      order: g.order, seq: groupSeq++ });
    sortGroups();
    if (!activeId) { activeId = groups[0].id; }
    if (inited) {
      loadActive();
      renderNav();
      if (isOpen()) renderBody();
    }
  }

  function unregisterGroup(id) {
    if (typeof id !== 'string' || !id) {
      throw new TypeError('unregisterGroup needs a non-empty string id');
    }
    var at = -1;
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].id === id) { at = i; break; }
    }
    if (at === -1) throw new RangeError('unknown settings group "' + id + '"');
    groups.splice(at, 1);
    if (activeId === id) {
      activeId = groups.length ? groups[0].id : null;
      saveActive();
      if (inited) {
        renderNav();
        renderBody();
        emit('group', activeId);
      }
    } else if (inited) {
      renderNav();
    }
    return true;
  }

  function getGroups() {
    return groups.map(function (g) { return { id: g.id, title: g.title }; });
  }

  function getActiveGroup() { return activeId; }

  function on(evt, fn) {
    if (!listeners[evt]) throw new RangeError('unknown settings event "' + evt + '"');
    if (typeof fn !== 'function') throw new TypeError('on("' + evt + '") needs a function');
    listeners[evt].push(fn);
    return function () { off(evt, fn); };
  }

  function off(evt, fn) {
    if (!listeners[evt]) throw new RangeError('unknown settings event "' + evt + '"');
    listeners[evt] = listeners[evt].filter(function (f) { return f !== fn; });
  }

  function emit(evt, data) {
    var fns = listeners[evt] || [];
    for (var i = 0; i < fns.length; i++) {
      try { fns[i](data); }
      catch (e) {
        if (typeof console !== 'undefined' && console.error) {
          console.error('[WebMuseSettings] "' + evt + '" listener failed:', e);
        }
      }
    }
  }

  // Canonical value registry: one get/set pair per setting key, shared
  // by in-panel rows and outside controls. Unknown keys throw — a
  // misspelled key is a bug, not a no-op.
  function defineSetting(key, def) {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('defineSetting needs a non-empty string key');
    }
    if (!def || typeof def.get !== 'function' || typeof def.set !== 'function') {
      throw new TypeError('defineSetting("' + key + '") needs {get, set} functions');
    }
    if (settingDefs[key]) {
      throw new Error('duplicate settings key "' + key + '"');
    }
    settingDefs[key] = { get: def.get, set: def.set };
  }

  function hasSetting(key) { return !!settingDefs[key]; }

  function getSetting(key) {
    if (!settingDefs[key]) throw new RangeError('unknown setting "' + key + '"');
    return settingDefs[key].get();
  }

  function setSetting(key, value) {
    if (!settingDefs[key]) throw new RangeError('unknown setting "' + key + '"');
    var r = settingDefs[key].set(value);
    sync();
    return r;
  }

  // Versioned store: {version, activeGroup} under STORE_KEY. The pre-v1
  // plain-string LEGACY_GROUP_KEY migrates forward on first read, then
  // is deleted. Corrupt or future-version stores warn and fall back to
  // defaults instead of throwing or sticking on a stale group.
  function readStore() {
    var raw = null;
    try {
      raw = root.localStorage ? root.localStorage.getItem(STORE_KEY) : null;
    } catch (_) { return null; }
    if (raw) {
      try {
        var j = JSON.parse(raw);
        if (j && j.version === STORE_VERSION && typeof j.activeGroup === 'string') {
          return j;
        }
        warn('ignoring settings store with unknown version/shape');
      } catch (e) {
        warn('corrupt settings store, using defaults');
      }
      return null;
    }
    try {
      var saved = root.localStorage ? root.localStorage.getItem(LEGACY_GROUP_KEY) : null;
      if (saved) {
        try { root.localStorage.removeItem(LEGACY_GROUP_KEY); } catch (_) {}
        return { version: STORE_VERSION, activeGroup: saved, migrated: true };
      }
    } catch (_) { /* storage unavailable: defaults apply */ }
    return null;
  }

  function loadActive() {
    var s = readStore();
    if (s && s.activeGroup && findGroup(s.activeGroup)) {
      activeId = s.activeGroup;
    } else if (s && s.activeGroup) {
      warn('unknown saved group "' + s.activeGroup + '", using default');
    }
    if (s && s.migrated) saveActive();
    if (!activeId && groups.length) activeId = groups[0].id;
  }

  function saveActive() {
    try {
      if (root.localStorage && activeId) {
        root.localStorage.setItem(STORE_KEY, JSON.stringify(
          { version: STORE_VERSION, activeGroup: activeId }));
      }
    } catch (_) { /* storage unavailable: lasts the session */ }
  }

  function panel() { return el(PANEL_ID); }

  function isOpen() {
    var p = panel();
    return !!(p && p.classList.contains('open'));
  }

  // Background goes inert while the modal is up, so Tab and screen
  // readers stay inside the panel. The panel lives in #layout next to
  // the sidebars, so its siblings (plus the topbar) inert individually.
  var INERT_IDS = ['topbar', 'sessions', 'center', 'inspector'];

  function setBackgroundInert(on) {
    for (var i = 0; i < INERT_IDS.length; i++) {
      var n = el(INERT_IDS[i]);
      if (!n) continue;
      try {
        if ('inert' in n) n.inert = on;
        else if (on) n.setAttribute('aria-hidden', 'true');
        else n.removeAttribute('aria-hidden');
      } catch (_) { /* old engine: trap still holds */ }
    }
  }

  function focusables() {
    var p = panel();
    if (!p) return [];
    var all = p.querySelectorAll('button, select, input, textarea, a[href], [tabindex]');
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var m = all[i];
      if (m.disabled) continue;
      if (m.tabIndex < 0) continue;
      if (m.getAttribute('aria-hidden') === 'true') continue;
      out.push(m);
    }
    return out;
  }

  function trapTab(ev) {
    var f = focusables();
    if (!f.length) { ev.preventDefault(); return; }
    var first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function onDocumentKeydown(ev) {
    if (!isOpen()) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
      return;
    }
    if (ev.key === 'Tab') trapTab(ev);
  }

  function open() {
    var p = panel();
    if (!p || p.classList.contains('open')) return;
    opener = (document.activeElement instanceof HTMLElement) ? document.activeElement : null;
    if (opener && p.contains(opener)) opener = null;
    p.classList.add('open');
    setBackgroundInert(true);
    renderNav();
    renderBody();
    // Move focus into the panel so keyboard users land inside it.
    var x = el('btn-close-settings');
    if (x) x.focus();
    emit('open');
  }

  function close() {
    var p = panel();
    if (!p || !p.classList.contains('open')) return;
    p.classList.remove('open');
    setBackgroundInert(false);
    emit('close');
    // Return focus where it came from (the gear in the normal flow).
    var back = (opener && opener.isConnected) ? opener : el('btn-settings');
    opener = null;
    if (back && back.focus) {
      try { back.focus(); } catch (_) {}
    }
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  function selectGroup(id) {
    if (typeof id !== 'string' || !id) {
      throw new TypeError('selectGroup needs a non-empty string id');
    }
    if (!findGroup(id)) throw new RangeError('unknown settings group "' + id + '"');
    if (id === activeId) return;
    activeId = id;
    saveActive();
    renderNav();
    renderBody();
    emit('group', id);
  }

  function tabId(id) { return 'settings-tab-' + id; }

  function renderNav() {
    var nav = el(NAV_ID);
    if (!nav) return;
    // Remember whether focus sat on a group tab so a re-render (e.g.
    // selectGroup) can put it back instead of dropping it on <body>.
    var refocus = nav.contains(document.activeElement)
      ? (document.activeElement.dataset.group || true) : null;
    nav.innerHTML = '';
    nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', 'Settings groups');
    groups.forEach(function (g) {
      var selected = g.id === activeId;
      var b = document.createElement('button');
      b.type = 'button';
      b.id = tabId(g.id);
      b.textContent = g.title;
      b.dataset.group = g.id;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', selected ? 'true' : 'false');
      b.setAttribute('aria-controls', BODY_ID);
      b.tabIndex = selected ? 0 : -1;
      if (selected) b.classList.add('active');
      b.onclick = function () { selectGroup(g.id); };
      nav.appendChild(b);
      if (refocus === g.id || (refocus === true && selected)) b.focus();
    });
    if (!nav.onkeydown) {
      // Roving tabindex: arrows move between group tabs (Linear-style).
      nav.onkeydown = function (ev) {
        var k = ev.key;
        if (k !== 'ArrowRight' && k !== 'ArrowLeft' &&
            k !== 'ArrowUp' && k !== 'ArrowDown' &&
            k !== 'Home' && k !== 'End') return;
        ev.preventDefault();
        var i = 0;
        for (; i < groups.length; i++) {
          if (groups[i].id === activeId) break;
        }
        if (k === 'ArrowRight' || k === 'ArrowDown') i = (i + 1) % groups.length;
        else if (k === 'ArrowLeft' || k === 'ArrowUp') i = (i - 1 + groups.length) % groups.length;
        else if (k === 'Home') i = 0;
        else i = groups.length - 1;
        selectGroup(groups[i].id);
        var btn = nav.querySelector('[data-group="' + groups[i].id + '"]');
        if (btn) btn.focus();
      };
    }
  }

  function renderBody() {
    var body = el(BODY_ID);
    if (!body) return;
    body.innerHTML = '';
    body.setAttribute('role', 'tabpanel');
    if (activeId) body.setAttribute('aria-labelledby', tabId(activeId));
    var g = findGroup(activeId);
    if (!g) {
      body.textContent = groups.length
        ? 'No settings group selected.'
        : 'No settings groups registered.';
      return;
    }
    try {
      g.render(body, helpers());
    } catch (e) {
      if (typeof console !== 'undefined' && console.error) {
        console.error('[WebMuseSettings] group "' + g.id + '" render failed:', e);
      }
      body.innerHTML = '';
      var err = document.createElement('div');
      err.className = 'setting-error';
      err.textContent = 'The "' + g.title + '" settings failed to load.' +
        (e && e.message ? ' ' + e.message : '');
      body.appendChild(err);
    }
  }

  // Refresh the open group after outside state changed (a topbar picker,
  // the starfield switch, an async model/theme list). Focus on the edited
  // control survives: the focused row is matched by setting key after the
  // rebuild and gets focus back, so async updates land even while the
  // user sits inside the panel.
  function sync() {
    var p = panel();
    if (!p || !p.classList.contains('open')) return;
    var body = el(BODY_ID);
    var active = document.activeElement;
    var key = null;
    if (body && active && body.contains(active) && active.getAttribute) {
      key = active.getAttribute('data-setting-key') ||
        active.getAttribute('aria-label');
    }
    renderBody();
    if (key && body) {
      var cands = body.querySelectorAll('select, button, input, textarea');
      for (var i = 0; i < cands.length; i++) {
        var c = cands[i];
        if (c.getAttribute('data-setting-key') === key ||
            c.getAttribute('aria-label') === key) {
          try { c.focus({ preventScroll: true }); }
          catch (_) { try { c.focus(); } catch (_) {} }
          break;
        }
      }
    }
  }

  // One quiet row: title + optional description on the left, the control
  // passed by the group on the right.
  function row(title, desc) {
    var wrap = document.createElement('div');
    wrap.className = 'setting-row';
    var lab = document.createElement('div');
    lab.className = 'setting-label';
    var t = document.createElement('div');
    t.className = 'setting-title';
    t.textContent = title;
    lab.appendChild(t);
    if (desc) {
      var d = document.createElement('div');
      d.className = 'setting-desc';
      d.textContent = desc;
      lab.appendChild(d);
    }
    var ctl = document.createElement('div');
    ctl.className = 'setting-control';
    wrap.appendChild(lab);
    wrap.appendChild(ctl);
    return { wrap: wrap, control: ctl };
  }

  function tagControl(node, ariaLabel) {
    if (ariaLabel) {
      node.setAttribute('aria-label', ariaLabel);
      node.setAttribute('data-setting-key', ariaLabel);
    }
    return node;
  }

  // options: [{value, label}]. Calls onPick(value) on change.
  function makeSelect(options, current, onPick, ariaLabel) {
    var sel = document.createElement('select');
    sel.className = 'setting-select';
    tagControl(sel, ariaLabel);
    (options || []).forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      if (o.value === current) opt.selected = true;
      if (o.disabled) opt.disabled = true;
      sel.appendChild(opt);
    });
    sel.onchange = function (ev) { onPick(ev.target.value); };
    return sel;
  }

  // Switch button in the stars-toggle idiom. onFlip receives the button
  // so the group can flip the real state, then update the pressed visual
  // directly (no rebuild, so focus stays on the switch).
  function makeToggle(checked, onFlip, ariaLabel) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'setting-toggle';
    btn.setAttribute('role', 'switch');
    btn.setAttribute('aria-checked', checked ? 'true' : 'false');
    tagControl(btn, ariaLabel);
    var thumb = document.createElement('span');
    thumb.className = 'setting-thumb';
    thumb.setAttribute('aria-hidden', 'true');
    btn.appendChild(thumb);
    btn.onclick = function () {
      onFlip(btn);
    };
    return btn;
  }

  function helpers() {
    return { row: row, makeSelect: makeSelect, makeToggle: makeToggle };
  }

  function init() {
    // Cheap on purpose: nav only. The body (including the async theme
    // list fetch) builds on open(), so page load stays untouched.
    if (inited) return;
    inited = true;
    loadActive();
    renderNav();
    var body = el(BODY_ID);
    if (body) {
      body.setAttribute('role', 'tabpanel');
      if (activeId) body.setAttribute('aria-labelledby', tabId(activeId));
    }
    var x = el('btn-close-settings');
    if (x) x.onclick = function () {
      close();
      // Return focus to the gear that opened the panel.
      var g = el('btn-settings');
      if (g) g.focus();
    };
    var p = panel();
    if (p && !p.dataset.shellBackdrop) {
      p.dataset.shellBackdrop = '1';
      p.addEventListener('click', function (ev) {
        if (ev.target === p) close();
      });
    }
    document.addEventListener('keydown', onDocumentKeydown);
  }

  root.WebMuseSettings = {
    registerGroup: registerGroup,
    unregisterGroup: unregisterGroup,
    getGroups: getGroups,
    getActiveGroup: getActiveGroup,
    on: on,
    off: off,
    defineSetting: defineSetting,
    hasSetting: hasSetting,
    getSetting: getSetting,
    setSetting: setSetting,
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    isOpen: isOpen,
    selectGroup: selectGroup,
    sync: sync,
    groupKey: LEGACY_GROUP_KEY,
    storeKey: STORE_KEY,
    storeVersion: STORE_VERSION,
  };
})(typeof window !== 'undefined' ? window : this);
