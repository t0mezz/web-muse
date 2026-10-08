// settings.js — expandable settings panel shell (Linear-style lightweight groups).
// Plain classic script: no imports, no exports, no frameworks (like theme.js).
// Load with <script src="/settings.js"></script> BEFORE app.js; app.js
// registers the groups and calls WebMuseSettings.init() at startup.
//
// The shell owns the icon's open target, open/close, group navigation,
// and the persisted active group. Each setting row is one quiet
// label-plus-control line. New groups stay a single call:
//
//   WebMuseSettings.registerGroup({ id: "audio", title: "Audio",
//     render: function (body, h) { ... } });
//
// render(body, h) appends rows built with the h.row/makeSelect/makeToggle
// helpers and reads live state at render time, so mirrors of outside
// controls (topbar pickers, starfield switch) are always fresh: the shell
// re-renders the open group on WebMuseSettings.sync(), which app.js calls
// from its existing sync functions. Re-render skips while the user is
// editing inside the panel, so focus is never stolen.
(function (root) {
  'use strict';

  var GROUP_KEY = 'webmuse.settingsGroup';
  var PANEL_ID = 'settings';

  var groups = [];
  var activeId = null;

  function el(id) { return document.getElementById(id); }

  function findGroup(id) {
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].id === id) return groups[i];
    }
    return null;
  }

  function registerGroup(g) {
    if (!g || typeof g.id !== 'string' || !g.id ||
        typeof g.title !== 'string' || !g.title ||
        typeof g.render !== 'function') return;
    if (findGroup(g.id)) return; // first registration wins
    groups.push(g);
    if (!activeId) { activeId = g.id; }
  }

  function loadActive() {
    try {
      var saved = root.localStorage ? root.localStorage.getItem(GROUP_KEY) : null;
      if (saved && findGroup(saved)) activeId = saved;
    } catch (_) { /* storage unavailable: first group wins */ }
    if (!activeId && groups.length) activeId = groups[0].id;
  }

  function saveActive() {
    try {
      if (root.localStorage && activeId) root.localStorage.setItem(GROUP_KEY, activeId);
    } catch (_) { /* storage unavailable: lasts the session */ }
  }

  function panel() { return el(PANEL_ID); }

  function isOpen() {
    var p = panel();
    return !!(p && p.classList.contains('open'));
  }

  function open() {
    var p = panel();
    if (!p) return;
    p.classList.add('open');
    renderNav();
    renderBody();
    // Move focus into the panel so keyboard users land inside it.
    var x = el('btn-close-settings');
    if (x) x.focus();
  }

  function close() {
    var p = panel();
    if (!p) return;
    p.classList.remove('open');
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  function selectGroup(id) {
    if (!findGroup(id) || id === activeId) return;
    activeId = id;
    saveActive();
    renderNav();
    renderBody();
  }

  function renderNav() {
    var nav = el('settings-nav');
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
      b.textContent = g.title;
      b.dataset.group = g.id;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', selected ? 'true' : 'false');
      b.setAttribute('aria-pressed', selected ? 'true' : 'false');
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
            k !== 'Home' && k !== 'End') return;
        ev.preventDefault();
        var i = 0;
        for (; i < groups.length; i++) {
          if (groups[i].id === activeId) break;
        }
        if (k === 'ArrowRight') i = (i + 1) % groups.length;
        else if (k === 'ArrowLeft') i = (i - 1 + groups.length) % groups.length;
        else if (k === 'Home') i = 0;
        else i = groups.length - 1;
        selectGroup(groups[i].id);
        var btn = nav.querySelector('[data-group="' + groups[i].id + '"]');
        if (btn) btn.focus();
      };
    }
  }

  function renderBody() {
    var body = el('settings-body');
    if (!body) return;
    body.innerHTML = '';
    var g = findGroup(activeId);
    if (!g) return;
    try {
      g.render(body, helpers());
    } catch (_) {
      body.textContent = 'Settings failed to load.';
    }
  }

  // Refresh the open group after outside state changed (a topbar picker,
  // the starfield switch). Skipped while closed, and while the user is
  // editing inside the panel (rebuilding then would steal focus); the
  // in-panel control already shows the just-picked value.
  function sync() {
    var p = panel();
    if (!p || !p.classList.contains('open')) return;
    if (p.contains(document.activeElement)) return;
    renderBody();
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

  // options: [{value, label}]. Calls onPick(value) on change.
  function makeSelect(options, current, onPick, ariaLabel) {
    var sel = document.createElement('select');
    sel.className = 'setting-select';
    if (ariaLabel) sel.setAttribute('aria-label', ariaLabel);
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
    if (ariaLabel) btn.setAttribute('aria-label', ariaLabel);
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
    loadActive();
    renderNav();
    var x = el('btn-close-settings');
    if (x) x.onclick = function () {
      close();
      // Return focus to the gear that opened the panel.
      var g = el('btn-settings');
      if (g) g.focus();
    };
  }

  root.WebMuseSettings = {
    registerGroup: registerGroup,
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    isOpen: isOpen,
    selectGroup: selectGroup,
    sync: sync,
    groupKey: GROUP_KEY,
  };
})(typeof window !== 'undefined' ? window : this);
