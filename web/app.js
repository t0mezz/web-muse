/* web-muse TUI-parity UI: terminal transcript, composer, slash commands.
 * No build step: plain fetch + WebSocket. Mobile-first, desktop 3-column. */
"use strict";

const el = (id) => document.getElementById(id);
const state = {
  ws: null, nextId: 1, pending: new Map(),
  sessionId: null, session: null, sessionsCache: [],
  cursor: "", pageCursor: null, hasOlder: false,
  items: new Map(), // itemId -> {line, body, item}
  tools: new Map(), // itemId -> tool summary row
  running: false, turnId: null, queuedTurnId: null,
  stick: true, history: [], hidx: -1,
  models: [], modelsMeta: null, slashSel: 0, slashList: [],
  // Last explicitly chosen model (picker or /model): applied to the
  // current session AND remembered as the default for created sessions.
  pickedModel: null,
  // Last explicitly chosen reasoning effort (picker, /effort or
  // /default-effort): applied to the current session AND remembered as
  // the default for created sessions (parity with pickedModel).
  pickedEffort: null,
  // Last explicitly chosen approval mode (Session panel): applied to the
  // current session AND remembered as the default for created sessions.
  pickedApprovalMode: null,
  reconnectDelay: 1000, everConnected: false,
  lastCumulative: null, lastContext: null, ctxLine: "", sessionMcp: [],
  githubCache: [], githubOp: null, githubPending: new Map(),
  // Composer-attached repo for the next session: {fullName, branch|null,
  // defaultBranch|null}. Shown only while no session is open; the first
  // sent message clones it (branch) and roots the new session there.
  pendingRepo: null, stagedPrompt: null, stagedRepo: null, repoBusy: null,
  sessionsHidden: 0,
  // "Other sessions" group collapsed (persisted across reloads).
  otherCollapsed: false,
  // Transcript density: active filter chip (all|edits|commands|errors)
  // and the open turn's rollup block (null between turns).
  txFilter: "all",
  turnBlock: null,
  // Renames the host has admitted but not yet applied (list still shows
  // the old name): sessionId -> {name, at}. Re-applied over every
  // refresh until the host catches up or 30s pass.
  pendingNames: new Map(),
};

/* ---------- tiny helpers ---------- */
function toast(msg, isErr) {
  const t = el("toast");
  t.textContent = "";
  void t.offsetWidth;
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.hidden = true; }, 3500);
}
function notice(msg) {
  const n = el("notice");
  if (!msg) { n.hidden = true; n.textContent = ""; return; }
  n.hidden = false; n.textContent = msg;
}
function setStatus(t) { el("status").textContent = t; }
function shortId(id) { return (id || "").slice(0, 8); }
// Default fallback first: an explicit host name wins; otherwise show the
// initial-prompt prefix the bridge auto-names (short id until it lands).
// A later user rename just sets s.name and takes precedence.
function sessionDisplayName(s) {
  const n = (s && s.name || "").trim();
  return n || `Untitled ${shortId(s && s.sessionId)}`;
}
function titleForSession(sessionId) {
  const hit = state.sessionsCache.find((s) => s.sessionId === sessionId);
  return sessionDisplayName(hit || { sessionId });
}

/* ---------- websocket ---------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  setConn("off");
  ws.onopen = async () => {
    setConn("on"); setStatus("connected");
    state.reconnectDelay = 1000;
    try {
      await refreshSessions();
      await refreshModels();
      await refreshUsage();
      if (state.sessionId) {
        // Cursor-tracked replay: pick up where we left off.
        await send({ type: "subscribe", sessionId: state.sessionId,
          after: state.cursor || undefined }).then(handleSubscribeResult).catch(() => {});
        fetchPending();
      } else if (!state.everConnected) {
        sysLine("Connected to web-muse bridge.");
      }
      state.everConnected = true;
    } catch (e) { setStatus("init failed: " + e.message); }
  };
  ws.onclose = () => {
    setConn("off"); setStatus("disconnected — retrying…");
    for (const [, p] of state.pending) p.reject(new Error("ws closed"));
    state.pending.clear();
    const d = Math.min(state.reconnectDelay, 15000);
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 15000);
    setTimeout(connect, d);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) { /* onclose reconnects */ } };
  ws.onmessage = (ev) => {
    let f;
    try { f = JSON.parse(ev.data); } catch { return; }
    if (f.type === "result" && f.id != null) {
      const p = state.pending.get(f.id);
      if (p) {
        state.pending.delete(f.id);
        if (f.ok) p.resolve(f.result || {});
        else p.reject(new Error((f.error || {}).message || "error"));
      }
      return;
    }
    if (f.type === "hello") return onHello(f);
    if (f.type === "approval") return onApproval(f.approval);
    if (f.type === "userInput") return onUserInput(f.prompt);
    if (f.type === "event") return onEvent(f.method, f.params);
  };
}

function send(msg, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (!state.ws || state.ws.readyState !== 1) { reject(new Error("not connected")); return; }
    const id = state.nextId++;
    msg.id = id;
    state.pending.set(id, { resolve, reject });
    state.ws.send(JSON.stringify(msg));
    setTimeout(() => {
      if (state.pending.has(id)) { state.pending.delete(id); reject(new Error("timeout")); }
    }, timeoutMs || 60000);
  });
}

function setConn(s) {
  const d = el("conn-dot");
  d.className = "dot " + (s === "busy" ? "busy" : s === "on" ? "on" : "off");
}
function onHello(f) {
  const fp = f.schema && f.schema.fingerprint ? String(f.schema.fingerprint).slice(7, 15) : "";
  setStatus(`connected${fp ? " · " + fp : ""}${f.mspAlive === false ? " · msp DOWN" : ""}`);
  if (f.mspAlive === false) notice("MSP host is down — prompts will fail until `muse serve` recovers.");
  else notice("");
}

/* ---------- sessions ---------- */
async function refreshSessions() {
  if (state.listPromise) return state.listPromise;
  state.listPromise = (async () => {
    const r = await send({ type: "list", limit: 100 });
  // Rows whose workspace directory was removed from disk stay out of the
  // bar (the bridge flags them); the count keeps the hiding visible.
  const rows = r.sessions || [];
  state.sessionsHidden = rows.filter((s) => s.workspaceMissing).length;
  state.sessionsCache = rows.filter((s) => !s.workspaceMissing);
  // Bridge-created sessions first; host order kept within each group
  // (stable sort), so TUI sessions stay exactly as the host listed them.
  state.sessionsCache.sort((a, b) => ((b.bridgeCreated ? 1 : 0) - (a.bridgeCreated ? 1 : 0)));
    applyPendingNames();
    renderSessionList(state.sessionsCache);
    return r;
  })().finally(() => { state.listPromise = null; });
  return state.listPromise;
}

// Re-apply admitted-but-unapplied renames over fresh list rows. Drops
// an entry once the host row carries the new name (caught up) or after
// 30s (host never applied it — fall back to host truth).
function applyPendingNames() {
  if (!state.pendingNames.size) return;
  const now = Date.now();
  for (const [sid, p] of state.pendingNames) {
    if (now - p.at > 30000) state.pendingNames.delete(sid);
  }
  for (const s of state.sessionsCache) {
    const p = state.pendingNames.get(s.sessionId);
    if (!p) continue;
    if ((s.name || "") === p.name) state.pendingNames.delete(s.sessionId);
    else s.name = p.name;
  }
}

/* Cursor-style terse relative time: "now", "5m", "3h", "2d", "Oct 5". */
function sessRelTime(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d`;
  const d = new Date(t);
  return d.getFullYear() === new Date().getFullYear()
    ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
    : d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

/* Human status label: allowlisted tokens only, never raw host text. */
function sessStatusLabel(s) {
  const st = String((s && s.status) || "").toLowerCase().replace(/[_-]+/g, " ").trim();
  if (!st || st === "idle") return "Idle";
  if (st === "running") return "Working";
  if (st === "notloaded" || st === "not loaded" || st === "saved") return "Saved";
  if (st.includes("fail") || st.includes("error")) return "Failed";
  if (st === "starting") return "Starting";
  if (st === "paused") return "Paused";
  return "Unknown";
}

/* Second-line preview: turn count (correct singular) + status. */
function sessPreview(s) {
  const n = Number((s && s.turnCount) || 0);
  return (n > 0 ? `${n} turn${n === 1 ? "" : "s"}` : "Empty") + ` · ${sessStatusLabel(s)}`;
}

/* Unnamed sessions show a raw id prefix: render it mono + dimmed. */
function sessIsUnnamed(s) { return !(s && (s.name || "").trim()); }

/* Correct singular/plural without "(s)" litter. */
function sessN(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }



function renderSessionList(sessions) {
  const rawFilter = el("session-filter").value || "";
  const q = rawFilter.trim().toLowerCase();
  const box = el("session-list");
  const ae = document.activeElement;
  const aeRow = ae && ae.closest ? ae.closest(".session-row") : null;
  const focusKey = aeRow && box.contains(aeRow) && aeRow.dataset.sid
    ? { sid: aeRow.dataset.sid, sel: ae.classList.contains("config-btn") ? ".config-btn" : ".sess-open" }
    : null;
  if (document.querySelector(".row-menu:not([hidden])")) {
    state.listDirty = true;
    return;
  }
  const qChanged = q !== renderSessionList._q;
  renderSessionList._q = q;
  const prevScroll = box.scrollTop;
  box.innerHTML = "";
  const rows = sessions.filter((s) =>
    !q || sessionDisplayName(s).toLowerCase().includes(q)
    || (s.sessionId || "").toLowerCase().includes(q)
    || sessPreview(s).toLowerCase().includes(q));
  // Recency within each bridge group (bridge-first order applied above).
  // Missing/unparseable dates sink (NaN would scatter under the comparator).
  const byTime = (a, b) => (Date.parse(b.updatedAt || "") || 0) - (Date.parse(a.updatedAt || "") || 0);
  const web = rows.filter((s) => s.bridgeCreated).sort(byTime);
  const other = rows.filter((s) => !s.bridgeCreated).sort(byTime);
  const groups = [];
  if (web.length) groups.push(["web", sessN(web.length, "Web session"), web]);
  if (other.length) groups.push(["other", web.length
    ? sessN(other.length, "Other session")
    : sessN(other.length, "Session"), other]);
  if (!rows.length) {
    const d = document.createElement("div");
    d.className = "session-empty";
    d.textContent = sessions.length
      ? "No sessions match — clear the filter or start a new session."
      : "No sessions yet — press ＋ or /new.";
    box.append(d);
  } else {
    for (const [kind, label, list] of groups) {
      const h = document.createElement("h2");
      h.className = "sess-group";
      h.id = `sess-group-${kind}`;
      box.append(h);
      const sec = document.createElement("div");
      sec.className = "sess-sec";
      sec.setAttribute("role", "list");
      sec.setAttribute("aria-labelledby", h.id);
      if (kind === "other") {
        const t = document.createElement("button");
        t.type = "button";
        t.className = "sess-toggle";
        t.id = `${h.id}-btn`;
        t.textContent = label;
        sec.id = `sess-sec-${kind}`;
        t.setAttribute("aria-controls", sec.id);
        const collapsed = !!state.otherCollapsed;
        sec.hidden = collapsed;
        t.setAttribute("aria-expanded", String(!collapsed));
        t.onclick = () => {
          const hide = !sec.hidden;
          sec.hidden = hide;
          state.otherCollapsed = hide;
          saveOtherCollapsed();
          t.setAttribute("aria-expanded", String(!hide));
        };
        h.append(t);
        sec.setAttribute("aria-labelledby", t.id);
      } else {
        h.textContent = label;
      }
      for (const s of list) appendSessionRow(sec, s);
      box.append(sec);
    }
  }
  let focusScrolled = false;
  if (focusKey) {
    const ctl = box.querySelector(`.session-row[data-sid="${CSS.escape(focusKey.sid)}"] ${focusKey.sel}`);
    if (ctl && ctl.offsetParent !== null) {
      const row = ctl.closest(".session-row");
      const lr = box.getBoundingClientRect(), rr = row.getBoundingClientRect();
      if (rr.top < lr.top || rr.bottom > lr.bottom) {
        row.scrollIntoView({ block: "nearest" });
        focusScrolled = true;
      }
      ctl.focus({ preventScroll: true });
    } else {
      const fallback = box.querySelector(".sess-open") || el("session-filter");
      if (fallback) fallback.focus({ preventScroll: true });
    }
  }
  const counter = el("sess-count");
  if (counter) {
    clearTimeout(counter._t);
    if (q) {
      const text = !rows.length
        ? `No sessions match "${rawFilter.trim()}"`
        : rows.length < sessions.length
          ? `Showing ${sessN(rows.length, "session")} of ${sessions.length}`
          : `${sessN(sessions.length, "session")}`;
      counter._t = setTimeout(() => { counter.textContent = text; counter.hidden = false; }, 400);
    } else {
      counter.hidden = true;
      counter.textContent = "";
    }
  }
  if (state.sessionsHidden > 0) {
    const n = document.createElement("div");
    n.className = "meta";
    n.textContent = `${sessN(state.sessionsHidden, "session")} hidden — workspace directory removed.`;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sync-btn"; b.textContent = "Sync now";
    b.title = "Preview and delete session files whose workspace directory is gone (/sync)";
    b.onclick = (e) => { e.stopPropagation(); cmdSync(); };
    n.append(b);
    box.append(n);
  }
  if (!focusScrolled) box.scrollTop = qChanged ? 0 : Math.min(prevScroll, box.scrollHeight);
}

function appendSessionRow(box, s) {
  const row = document.createElement("div");
  row.className = "session-row" + (s.sessionId === state.sessionId ? " active" : "");
  row.setAttribute("role", "listitem");
  row.dataset.sid = s.sessionId;
  const icon = document.createElement("span");
  const running = sessStatusLabel(s) === "Working";
  icon.className = "sess-icon" + (running ? " running" : "");
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M5.4 8.2l1.8 1.8 3.4-3.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const open = document.createElement("button");
  open.type = "button";
  open.className = "sess-open";
  const openSpoken = sessRelTime(s.updatedAt) && !Number.isNaN(Date.parse(s.updatedAt || ""))
    ? `${sessionDisplayName(s)} — ${sessPreview(s)} — ${new Date(s.updatedAt).toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}`
    : `${sessionDisplayName(s)} — ${sessPreview(s)}`;
  open.setAttribute("aria-label", openSpoken);
  if (s.sessionId === state.sessionId) open.setAttribute("aria-current", "true");
  open.onclick = (e) => { e.stopPropagation(); openSession(s.sessionId); };
  const top = document.createElement("span");
  top.className = "top sess-line";
  const nm = document.createElement("span");
  nm.className = "name" + (sessIsUnnamed(s) ? " unnamed" : "");
  nm.textContent = sessionDisplayName(s);
  const nmId = (s.name || "").trim() ? `${s.name.trim()} · ${s.sessionId}` : s.sessionId;
  nm.title = nmId;
  const tm = document.createElement("time");
  tm.className = "sess-time";
  tm.textContent = sessRelTime(s.updatedAt);
  const tt = Date.parse(s.updatedAt || "");
  if (Number.isNaN(tt)) {
    tm.title = s.updatedAt || "";
  } else {
    tm.dateTime = new Date(tt).toISOString();
    tm.title = new Date(tt).toLocaleString();
  }
  if (!tm.textContent) tm.hidden = true;
  const cfg = document.createElement("button");
  cfg.type = "button";
  cfg.className = "config-btn";
  cfg.title = "Session actions";
  cfg.setAttribute("aria-label", `Actions for ${sessionDisplayName(s)}`);
  cfg.setAttribute("aria-haspopup", "menu");
  cfg.setAttribute("aria-expanded", "false");
  cfg.innerHTML = '<svg viewBox="0 0 32 32" width="16" height="16" aria-hidden="true"><path class="line line-top-bottom" d="M27 10 13 10C10.8 10 9 8.2 9 6 9 3.5 10.8 2 13 2 15.2 2 17 3.8 17 6L17 26C17 28.2 18.8 30 21 30 23.2 30 25 28.2 25 26 25 23.8 23.2 22 21 22L7 22"/><path class="line" d="M7 16 27 16"/></svg>';
  top.append(nm, tm);
  const meta = document.createElement("span");
  meta.className = "meta sess-preview";
  meta.textContent = sessPreview(s);
  const body = document.createElement("span");
  body.className = "sess-body";
  body.append(top, meta);
  open.append(icon, body);
  // Path hidden by design; identity stays on the name hover. Row actions
  // live behind the config button.
  const menu = document.createElement("div");
  menu.className = "row-menu"; menu.hidden = true;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", `Actions for ${sessionDisplayName(s)}`);
  menu.id = `rowmenu-${s.sessionId}`;
  cfg.setAttribute("aria-controls", menu.id);
  const mkItem = (label, danger, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "row-opt" + (danger ? " danger" : "");
    b.setAttribute("role", "menuitem");
    b.setAttribute("aria-label", `${label} ${sessionDisplayName(s)}`);
    b.textContent = label;
    b.onclick = (e) => {
      e.stopPropagation();
      const op = menu._opener;
      closeRowMenus();
      if (op && op.isConnected) op.focus({ preventScroll: true });
      fn();
    };
    return b;
  };
  menu.append(
    mkItem("rename", false, () => renameSession(s.sessionId)),
    mkItem("fork", false, () => forkSession(s.sessionId)),
    mkItem("delete", true, () => deleteSession(s.sessionId)),
  );
  menu.addEventListener("click", (e) => e.stopPropagation());
  menu.addEventListener("focusout", (e) => {
    if (!menu.contains(e.relatedTarget)) closeRowMenus();
  });
  menu.addEventListener("keydown", (e) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const items = [...menu.querySelectorAll(".row-opt")];
    if (!items.length) return;
    let i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") i = (i + 1) % items.length;
    else if (e.key === "ArrowUp") i = (i - 1 + items.length) % items.length;
    else if (e.key === "Home") i = 0;
    else i = items.length - 1;
    items[i].focus();
  });
  // Clicking the opener while its menu is open must close, not reopen:
  // without this, mousedown steals focus first, the menu focusout closes
  // it, and the click then sees a hidden menu and reopens it.
  cfg.addEventListener("mousedown", (e) => { if (!menu.hidden) e.preventDefault(); });
  cfg.onclick = (e) => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    closeRowMenus();
    if (!cfg.isConnected) return;
    menu.hidden = !willOpen;
    cfg.setAttribute("aria-expanded", String(willOpen));
    if (!willOpen) cfg.focus({ preventScroll: true });
    if (willOpen) {
      menu._opener = cfg;
      menu.classList.remove("flip");
      const first = menu.querySelector(".row-opt");
      if (first) first.focus({ preventScroll: true });
      const lb = el("session-list").getBoundingClientRect();
      const mb = menu.getBoundingClientRect();
      if (mb.bottom > lb.bottom) {
        menu.classList.add("flip");
        if (menu.getBoundingClientRect().top < lb.top) menu.classList.remove("flip");
      }
    }
  };
  row.append(open, cfg, menu);
  box.append(row);
}

function closeRowMenus() {
  document.querySelectorAll(".row-menu").forEach((m) => { m.hidden = true; });
  document.querySelectorAll(".config-btn[aria-expanded]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  if (state.listDirty) {
    state.listDirty = false;
    renderSessionList(state.sessionsCache);
    return true;
  }
  return false;
}

function syncClearBtn() { el("btn-clear-filter").hidden = !el("session-filter").value.trim(); }

// Relative times go stale: refresh the visible list once a minute while
// the drawer is open (skipped with a menu open so popups aren't yanked).
setInterval(() => {
  if (document.hidden || !navigator.onLine) return;
  if (!el("sessions").classList.contains("open")) return;
  if (document.querySelector(".row-menu:not([hidden])")) { state.listDirty = true; return; }
  el("session-list").querySelectorAll("time.sess-time").forEach((t) => {
    if (t.dateTime) t.textContent = sessRelTime(t.dateTime);
  });
}, 60000);

async function openSession(sessionId) {
  // The starfield stays up: the bottom-right switch owns it now.
  closeRowMenus();
  // Assign before clearTranscript: its updateWelcome() would otherwise see
  // a stale null sessionId and restart the starfield mid-open.
  state.sessionId = sessionId;
  clearTranscript();
  updateRepoBar();
  closeDrawer();
  el("session-title").textContent = titleForSession(sessionId);
  try {
    const r = await send({ type: "resume", sessionId });
    state.session = r.session || null;
    el("session-title").textContent = sessionDisplayName(state.session || { sessionId });
    state.cursor = r.viewCursor || "";
    updateCursorChip();
    // History envelope: {mode, items|null, snapshot|null, noneReason?}.
    // inline -> items; snapshot/anchoredSnapshot -> snapshot.state.items
    // (genesis rung serves state:{items} alone); none -> explain + /older.
    const histEnv = r.history || {};
    const mode = histEnv.mode || (histEnv.items ? "inline" : "none");
    const snapItems = histEnv.snapshot && histEnv.snapshot.state && histEnv.snapshot.state.items;
    const hist = mode === "inline" ? (histEnv.items || r.items || [])
      : (snapItems || []);
    for (const it of hist) renderItem(it, false);
    if (mode === "snapshot" || mode === "anchoredSnapshot") {
      if (!hist.length) sysLine("Compacted session: snapshot state has no inline items — /older pages the full view.");
      else sysLine(`Compacted session (${mode}): showing ${hist.length} snapshot items — /older pages more.`);
    } else if (mode === "none") {
      if (histEnv.noneReason === "resume_refused_by_host" || r.fallback === "resume_refused_by_host") {
        sysLine("Session opened (metadata only): the bridge host cannot load this session's " +
          "permission profile (e.g. a TUI session needing the automated reviewer) — " +
          "past transcript and new turns are unavailable here. Continue it in the terminal with /resume, " +
          "or start a new web session.");
      } else if (histEnv.noneReason === "resume_unserved_by_host" || r.fallback === "resume_unserved_by_host") {
        sysLine("Session opened (metadata only): this host serves no session/resume, " +
          "so past transcript is unavailable — new turns stream live below.");
      } else {
        sysLine("No inline history (" + (histEnv.noneReason || "none") + ") — /older pages the view.");
      }
    }
    // view/page has no resume-provided cursor: start from the head and let
    // nextCursor walk backward; itemId dedup drops the overlap.
    state.pageCursor = null;
    state.hasOlder = true;
    updateOlderBtn();
    updateSessionDetail();
    const sub = await send({ type: "subscribe", sessionId, after: state.cursor || undefined }).catch(() => null);
    if (sub) handleSubscribeResult(sub);
    // A turn already running when the session opens (reload, late join)
    // never re-fires turn/started here: reconcile after replay settles.
    reconcileRunningState();
    fetchPending();
    renderSessionList(state.sessionsCache);
    el("input").focus();
    scrollDown(true);
  } catch (e) {
    // A session deleted outside the app (host TUI, another client, host
    // restart) is treated like an in-app delete instead of erroring on
    // every open. Refresh first — a failed refresh means offline, not
    // deleted, so fall through to the plain error then.
    let fresh = false;
    await refreshSessions().then(() => { fresh = true; }).catch(() => {});
    const still = state.sessionsCache.some((s) => s.sessionId === sessionId);
    if (fresh && !still) {
      await dropExternallyDeleted(sessionId, e.message);
    } else {
      sysLine("resume failed: " + e.message, true); toast("resume failed: " + e.message, true);
    }
  }
}

/* Late-join/reconnect reconcile: the thinking status and the running-state
 * CSS (run chip, idle glow, stop button) must reflect an already-active
 * turn even though its turn/started fired before this client subscribed.
 * Strictly additive: never hides the row (lifecycle events own that) and
 * never restarts a status row already showing. */
function reconcileRunningState() {
  const st = String((state.session && state.session.status) || "").toLowerCase().trim();
  state.running = st === "running" || state.running;
  updateRunChip();
  if (state.running && !document.getElementById("thinking-row")) showThinking();
}

function handleSubscribeResult(r) {
  if (!r) return;
  // Some hosts replay missed events inline in the subscribe result.
  const evs = r.events || r.missed || [];
  for (const e of evs) {
    if (e.method) onEvent(e.method, e.params || {});
    else if (e.item) renderItem(e.item, false);
  }
  if (r.viewCursor) { state.cursor = r.viewCursor; updateCursorChip(); }
}

function parseNewArgs(args) {
  // /new [name] [--mcp a,b] [--path <dir>] — --mcp may repeat; names are
  // comma/space split. First --path wins (whitespace-split args, so no
  // spaces in paths here — the + button has no such limit).
  const names = [];
  const rest = [];
  let workspaceRoot;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--mcp" && i + 1 < args.length) {
      names.push(...args[++i].split(",").map((s) => s.trim()).filter(Boolean));
    } else if (args[i].startsWith("--mcp=")) {
      names.push(...args[i].slice(6).split(",").map((s) => s.trim()).filter(Boolean));
    } else if (args[i] === "--path" && i + 1 < args.length) {
      if (workspaceRoot === undefined) workspaceRoot = args[++i];
      else i++;
    } else if (args[i].startsWith("--path=")) {
      if (workspaceRoot === undefined) workspaceRoot = args[i].slice(7);
    } else {
      rest.push(args[i]);
    }
  }
  const out = { name: rest.join(" ") || undefined,
    mcpAttach: [...new Set(names)] };
  if (workspaceRoot !== undefined) out.workspaceRoot = workspaceRoot;
  return out;
}

// Manual session directories: any on-device path is allowed, but the
// first session touching one needs an explicit allow. Allowed roots
// persist per browser.
const ALLOWED_ROOTS_KEY = "webmuse.allowedRoots";
function confirmedRoots() {
  try {
    const v = JSON.parse(localStorage.getItem(ALLOWED_ROOTS_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch (_) { return []; }
}
function normalizeRoot(p) {
  p = (p || "").trim();
  if (p.length > 1) p = p.replace(/\/+$/, "");
  return p;
}
function ensureRootConfirmed(path) {
  const norm = normalizeRoot(path);
  if (!norm) return false;
  const known = confirmedRoots();
  if (known.includes(norm)) return true;
  if (!window.confirm(`Allow this session to access ${norm}?\n\nThe agent will read and write files there.`)) return false;
  known.push(norm);
  try { localStorage.setItem(ALLOWED_ROOTS_KEY, JSON.stringify(known)); } catch (_) {}
  return true;
}

async function newSession(name, opts) {
  clearTranscript();
  state.sessionId = null; state.session = null;
  updateRepoBar();
  el("session-title").textContent = "new session";
  updateSessionDetail();
  const mcpAttach = (opts && opts.mcpAttach) || [];
  const root = normalizeRoot(opts && opts.workspaceRoot);
  if (root && !ensureRootConfirmed(root)) {
    toast("session not created: directory not confirmed", true);
    return;
  }
  if (name || mcpAttach.length || root) {
    // Create eagerly so the name sticks and MCP attaches at construction
    // (session/start is the only wire touchpoint for per-session MCP);
    // otherwise creation is lazy on first prompt.
    try {
      const req = { type: "new" };
      if (mcpAttach.length) req.mcpAttach = mcpAttach;
      if (root) req.workspaceRoot = root;
      if (state.pickedModel) {
        req.modelId = state.pickedModel.modelId;
        if (state.pickedModel.providerId) req.providerId = state.pickedModel.providerId;
      }
      if (state.pickedEffort) req.reasoningEffort = state.pickedEffort;
      if (state.pickedApprovalMode) req.approvalMode = state.pickedApprovalMode;
      const r = await send(req);
      const sid = r.session && r.session.sessionId;
      if (sid) {
        await openSession(sid);
        if (Array.isArray(r.mcpAttached)) state.sessionMcp = r.mcpAttached;
        if (name) await renameSession(sid, name, true);
        if (state.sessionMcp.length) sysLine("MCP attached: " + state.sessionMcp.join(", "));
      }
    } catch (e) { toast("new failed: " + e.message, true); }
  } else {
    sysLine("New session — type your first prompt. (Created lazily on send.)");
  }
}

async function renameSession(sessionId, name, quiet) {
  const sid = sessionId || state.sessionId;
  if (!sid) return toast("no session", true);
  const hit = state.sessionsCache.find((s) => s.sessionId === sid);
  // Prefill the real host name only: prefilling the synthetic
  // "Untitled <id>" placeholder would save it as a permanent name.
  const cur = hit ? (hit.name || "").trim() : "";
  const nm = (name != null ? name : prompt("Session name:", cur) || "");
  if (!nm.trim()) return;
  try {
    await send({ type: "rename", sessionId: sid, name: nm.trim() });
    toast("renamed to " + nm.trim());
    // Optimistic update on both surfaces: the host applies the rename
    // asynchronously, so the refresh below still lists the old name and
    // the bar lagged one rename behind. The pending entry survives stale
    // refreshes until the host settles (session/nameChanged + fresh list).
    state.pendingNames.set(sid, { name: nm.trim(), at: Date.now() });
    applyPendingNames();
    renderSessionList(state.sessionsCache);
    if (state.session && state.session.sessionId === sid) state.session.name = nm.trim();
    if (sid === state.sessionId) el("session-title").textContent = nm.trim();
    refreshSessions().catch(() => {});
  } catch (e) { if (!quiet) toast("rename failed: " + e.message, true); }
}

async function forkSession(sessionId) {
  const sid = sessionId || state.sessionId;
  if (!sid) return toast("no session", true);
  try {
    const r = await send({ type: "fork", sessionId: sid });
    const nid = (r.session && r.session.sessionId) || r.sessionId;
    toast("forked → " + shortId(nid || "?"));
    refreshSessions().catch(() => {});
    if (nid) openSession(nid);
  } catch (e) { toast("fork failed: " + e.message, true); }
}

// A session already deleted outside the app goes through the same
// aftermath as the in-app delete below: best-effort host delete (ignored
// when the host no longer knows it), drop from the bar, fresh session.
// No confirm — there is nothing left to protect.
async function dropExternallyDeleted(sessionId, reason) {
  try { await send({ type: "delete", sessionId }); } catch (_) {}
  state.sessionsCache = state.sessionsCache.filter((s) => s.sessionId !== sessionId);
  renderSessionList(state.sessionsCache);
  toast("deleted " + shortId(sessionId));
  if (sessionId === state.sessionId) newSession();
  sysLine(`Session ${shortId(sessionId)} was already deleted outside the app — cleaned up (${reason}).`);
  refreshSessions().catch(() => {});
}

// Sync the session store with the workspace dir: remove session files
// whose workspace directory is gone. Always previews first (dry run),
// then confirms — and only sessions rooted under the bridge's workspace
// base are candidates; external manual roots are left alone (a missing
// mount is not a deletion).
async function cmdSync() {
  let prev;
  try {
    prev = await send({ type: "pruneMissing", dryRun: true });
  } catch (e) { sysLine("sync failed: " + e.message, true); return; }
  const cands = prev.candidates || [];
  const outside = prev.outsideBase || 0;
  if (!cands.length) {
    sysLine("Nothing to sync." + (outside
      ? ` (${outside} missing-dir session(s) outside the workspace base — left alone)` : ""));
    return;
  }
  sysLine("Sessions with removed workspace directories (workspace base only):\n" +
    cands.map((c) => `  ${shortId(c.sessionId)}  ${c.name || "(unnamed)"}\n    ${c.workspaceRoot}`).join("\n") +
    (outside ? `\n(${outside} more outside the workspace base — left alone)` : ""));
  if (!confirm(`Delete these ${cands.length} session(s) from disk? Their transcripts will be lost.`)) return;
  try {
    const r = await send({ type: "pruneMissing", dryRun: false });
    const done = (r.deleted || []).length, bad = (r.failed || []).length;
    const pend = (r.pending || []).length;
    sysLine(`Sync done: ${done} deleted` +
      (pend ? `, ${pend} admitted but still listed — re-run /sync to confirm` : "") +
      (bad ? `, ${bad} failed` : "") + "." +
      (r.confirmed === false ? " (confirmation listing failed)" : "") +
      (r.failed || []).map((f) => `\n  ${shortId(f.sessionId)}: ${f.message}`).join("") +
      (r.outsideBase ? `\n(${r.outsideBase} outside the workspace base — left alone)` : ""));
    toast(`sync: ${done} deleted`);
    refreshSessions().catch(() => {});
  } catch (e) { sysLine("sync failed: " + e.message, true); }
}

async function deleteSession(sessionId) {
  const sid = sessionId || state.sessionId;
  if (!sid) return toast("no session", true);
  if (!confirm("Delete session " + shortId(sid) + "?")) return;
  try {
    await send({ type: "delete", sessionId: sid });
    toast("deleted " + shortId(sid));
    if (sid === state.sessionId) newSession();
    refreshSessions().catch(() => {});
  } catch (e) { toast("delete failed: " + e.message, true); }
}

const isNarrow = () => matchMedia("(max-width: 899px)").matches;
function syncScrim() {
  // Scrim shows for the sessions drawer, and on mobile also for the
  // inspector overlay (which otherwise covers its own toggle: a trap).
  el("scrim").hidden = !(el("sessions").classList.contains("open") ||
    (isNarrow() && el("inspector").classList.contains("open")));
}
function closeDrawer() {
  // Auto-shut is a mobile-drawer behavior; the desktop panel is
  // user-toggled and must survive session open/new.
  if (!isNarrow()) return;
  if (el("sessions").contains(document.activeElement)) el("btn-sessions").focus();
  closeRowMenus();
  el("sessions").classList.remove("open");
  syncScrim();
  savePanelState();
}
function toggleSessions() {
  const p = el("sessions");
  if (p.classList.contains("open")) {
    if (p.contains(document.activeElement)) el("btn-sessions").focus();
    closeRowMenus();
    p.classList.remove("open");
  } else {
    if (isNarrow()) el("inspector").classList.remove("open");
    p.classList.add("open");
    refreshSessions().catch(() => {});
    el("session-filter").focus();
  }
  syncScrim();
  savePanelState();
}
function toggleInspector() {
  el("inspector").classList.toggle("open");
  if (isNarrow() && el("inspector").classList.contains("open")) {
    closeRowMenus();
    el("sessions").classList.remove("open");
  }
  syncScrim();
  savePanelState();
}
function closeInspector() {
  el("inspector").classList.remove("open");
  syncScrim();
  savePanelState();
}

// Panel visibility persists across reloads (localStorage). First run
// defaults to both bars hidden; opening them is explicit and persisted.
const PANEL_KEYS = { sessions: "webmuse.sessionsOpen",
  inspector: "webmuse.inspectorOpen" };
// "Other sessions" group visibility persists across reloads; default expanded.
const OTHER_COLLAPSED_KEY = "webmuse.otherCollapsed";
function saveOtherCollapsed() {
  try {
    localStorage.setItem(OTHER_COLLAPSED_KEY, state.otherCollapsed ? "1" : "0");
  } catch (_) { /* storage unavailable: lasts the session */ }
}
function savePanelState() {
  try {
    localStorage.setItem(PANEL_KEYS.sessions,
      el("sessions").classList.contains("open") ? "1" : "0");
    localStorage.setItem(PANEL_KEYS.inspector,
      el("inspector").classList.contains("open") ? "1" : "0");
  } catch (_) { /* storage unavailable: defaults apply each load */ }
}
function restorePanelState() {
  let s = null, insp = null;
  try {
    s = localStorage.getItem(PANEL_KEYS.sessions);
    insp = localStorage.getItem(PANEL_KEYS.inspector);
  } catch (_) {}
  if (s === null && insp === null) {
    // Hidden by default: no width-based auto-open.
    el("sessions").classList.remove("open");
    el("inspector").classList.remove("open");
  } else {
    el("sessions").classList.toggle("open", s === "1");
    el("inspector").classList.toggle("open", insp === "1");
  }
  try {
    state.otherCollapsed = localStorage.getItem(OTHER_COLLAPSED_KEY) === "1";
  } catch (_) {}
  syncScrim();
}

/* ---------- terminal transcript ---------- */
function clearTranscript() {
  el("terminal").innerHTML = "";
  el("cards").innerHTML = "";
  el("tab-approvals").innerHTML = "";
  el("tab-tools").innerHTML = "";
  state.items.clear(); state.tools.clear();
  state.running = false; state.turnId = null; state.turnBlock = null;
  // A fresh transcript reads unfiltered; the chip row reflects it.
  state.txFilter = "all";
  document.querySelectorAll("#tx-filters button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.txf === "all"));
  });
  state.cursor = ""; state.pageCursor = null; state.hasOlder = false;
  state.ctxLine = "";
  state.lastCumulative = null; state.lastContext = null; state.sessionMcp = [];
  el("sess-usage").textContent = ""; el("sess-usage").title = "";
  updateRunChip(); updateOlderBtn(); updateCursorChip(); updateWelcome();
  hideThinking();
}

/* Welcome state: centered composer until the first message opens the transcript. */
function updateWelcome() {
  const welcome = state.items.size === 0;
  el("center").classList.toggle("is-welcome", welcome);
  if (welcome && !state.sessionId) startStarsFx();
}

/* Welcome starfield (web/stars.js): fullscreen backdrop behind the app,
 * shown on the empty welcome view until the first message is sent.
 * Opening a session leaves it running: the bottom-right switch owns it,
 * and the choice persists per browser. */
const STARS_KEY = "web-muse:stars";
// Locked-in look: dimmed, thinned-out, gentle parallax. The star hue is
// the theme fg; the literal below is the same color as fallback.
const STARS_OPTIONS = {
  starColor: "#e6e9ef",
  counts: [650, 260, 130],
  opacity: 0.7,
  factor: 0.0125,
};
// theme.js (loaded first) re-sources the hue from the color config;
// without it the literal above stands as the fallback.
if (window.WebMuseTheme) {
  STARS_OPTIONS.starColor = window.WebMuseTheme.get("fg", "#e6e9ef");
}
let stopStarsFx = null;
// Explicit toggle choice wins; otherwise follow prefers-reduced-motion.
function starsWanted() {
  try {
    const saved = localStorage.getItem(STARS_KEY);
    if (saved !== null) return saved === "1";
  } catch (_) {}
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
function startStarsFx() {
  if (stopStarsFx || typeof window.Stars === "undefined") return;
  if (!starsWanted()) return;
  const old = el("stars-bg");
  if (old) old.remove();
  const bg = document.createElement("div");
  bg.id = "stars-bg";
  document.body.prepend(bg);
  try {
    stopStarsFx = window.Stars.createStarsBackground(bg, STARS_OPTIONS);
  } catch (_) {
    bg.remove();
    stopStarsFx = null;
  }
  syncStarsToggle();
}
function stopStarsFxNow() {
  if (stopStarsFx) {
    try { stopStarsFx(); } catch (_) {}
    stopStarsFx = null;
  }
  const bg = el("stars-bg");
  if (bg) bg.remove();
  syncStarsToggle();
}
// Bottom-right switch: reflects whether the field is up; flipping it
// shows/hides the field at once and remembers the choice.
function syncStarsToggle() {
  const t = el("stars-toggle");
  if (!t) return;
  t.setAttribute("aria-checked", stopStarsFx ? "true" : "false");
}
function toggleStarsFx() {
  if (stopStarsFx) {
    try { localStorage.setItem(STARS_KEY, "0"); } catch (_) {}
    stopStarsFxNow();
  } else {
    try { localStorage.setItem(STARS_KEY, "1"); } catch (_) {}
    startStarsFx();
    syncStarsToggle();
  }
}

/* TUI-style thinking status: heads the turn's block while it runs (tool
   logs and the answer print below it), replaced by the final answer —
   i.e. removed — as soon as generation finishes. */
let thinkingTimer = null;
let thinkingStartedAt = 0;
function showThinking() {
  hideThinking();
  const line = document.createElement("div");
  line.className = "tline thinking";
  line.id = "thinking-row";
  // Three-body spinner left of the thinking text (Uiverse.io, dovatgabriel).
  const spinBox = document.createElement("span");
  spinBox.className = "three-body";
  spinBox.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i++) {
    const d = document.createElement("span");
    d.className = "three-body__dot";
    spinBox.append(d);
  }
  line.append(spinBox);
  const status = document.createElement("span");
  status.className = "body";
  // Static prefix as per-character wave spans (built once so the loop
  // never restarts); only the clock suffix updates on tick.
  [..."✻ Thinking… "].forEach((ch, i) => {
    const s = document.createElement("span");
    s.className = "wv";
    s.style.setProperty("--i", i);
    s.textContent = ch;
    status.append(s);
  });
  const clock = document.createElement("span");
  status.append(clock);
  line.append(status);
  turnContainer().append(line);
  thinkingStartedAt = Date.now();
  const tick = () => {
    const s = Math.max(0, Math.round((Date.now() - thinkingStartedAt) / 1000));
    clock.textContent = `(${s}s)`;
  };
  tick();
  thinkingTimer = setInterval(tick, 1000);
  scrollDown();
}
function hideThinking() {
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
  const line = document.getElementById("thinking-row");
  if (line) line.remove();
}
/* Keep the thinking row pinned below new lines: tool logs stack above
   it while it stays fixed at the end of the turn's block. */
function pinThinking() {
  const think = document.getElementById("thinking-row");
  if (think) turnContainer().append(think);
}

function scrollDown(force) {
  const t = el("terminal");
  if (state.stick || force) t.scrollTop = t.scrollHeight;
  updateJumpBtn();
}
function updateJumpBtn() { el("jump-latest").hidden = state.stick; }

function sysLine(text, isErr) {
  const line = document.createElement("div");
  line.className = "tline " + (isErr ? "error" : "system");
  const gut = document.createElement("span");
  gut.className = "gut"; gut.textContent = isErr ? "⚠" : "◦";
  const body = document.createElement("span");
  body.className = "body"; body.textContent = text;
  line.append(gut, body);
  turnContainer().append(line);
  applyTranscriptFilter();
  pinThinking();
  scrollDown();
  return line;
}

function itemText(it) {
  if (!it) return "";
  if (typeof it.text === "string") return it.text;
  if (typeof it.fallbackText === "string") return it.fallbackText;
  if (typeof it.displayText === "string") return it.displayText;
  if (it.content) {
    if (typeof it.content === "string") return it.content;
    if (Array.isArray(it.content)) {
      return it.content.map((c) => (typeof c === "string" ? c : (c.text || ""))).join("");
    }
  }
  return "";
}

function itemKind(it) {
  const k = String(it.kind || it.type || "");
  if (k === "userMessage" || k.includes("user")) return "user";
  // Reminder child sessions are host-side reminder activity, not agent
  // output: never rendered (renderItem drops them; the mapping stays
  // "system" so nothing else treats them as agent rows).
  if (k.toLowerCase().includes("reminder")) return "system";
  if (k.includes("tool") || k.includes("Tool") || k.includes("command") || k.includes("Command")) return "tool";
  if (k.includes("system") || k.includes("System")) return "system";
  if (k.includes("error") || k.includes("Error") || it.isError) return "error";
  return "agent";
}
const GUTTER = { user: "❯", agent: "●", tool: "⚙", system: "◦", error: "⚠" };

// Reminder items (reminderChild and kin) are host-internal activity:
// hidden from the transcript entirely.
function isReminder(it) {
  return String((it && (it.kind || it.type)) || "").toLowerCase().includes("reminder");
}

function toolType(it) {
  // The tool call type as the wire sends it (tool:"bash"), with fallbacks.
  return it.tool || it.toolName || it.name || "";
}
function toolSummary(it) {
  // One human line for a toolCall: description/command from args, else output.
  const fromArgs = (a) => (a && (a.description || a.command))
    ? [a.description, a.command ? "`" + a.command + "`" : ""].filter(Boolean).join(" · ")
    : "";
  if (typeof it.args === "string" && it.args) {
    try {
      const s = fromArgs(JSON.parse(it.args));
      if (s) return s;
    } catch (_) { /* fall through to output */ }
  } else if (it.args && typeof it.args === "object") {
    const s = fromArgs(it.args);
    if (s) return s;
  }
  return (it.visibleOutput || itemText(it) || "").split("\n")[0].slice(0, 200);
}
function itemHeadLabel(it, kind) {
  // Transcript head line: kind plus tool call type and status when set.
  const stamp = it.timestamp || it.recordedAt;
  const when = stamp ? " · " + String(stamp).slice(11, 19) : "";
  if (kind !== "tool") return String(it.kind || kind) + when;
  return [it.kind || kind, toolType(it), it.status].filter(Boolean).join(" · ") + when;
}

/* Consecutive tool-call grouping: N same-tool rows in a row collapse
 * into one expandable group ("edit × 3 — app.js"); the member rows move
 * inside untouched, so expanding shows the actual calls. Only completed
 * rows group (streaming rows would churn). Failed rows never collapse:
 * they stay loud and force their group open. History prepends group
 * symmetrically. Groups live under any parent (terminal or turn block),
 * so all moves go through the line's own parent node. */
function toolGroupKey(it) {
  return String(toolType(it) || (it && it.kind) || "tool").toLowerCase();
}
function toolArgsObj(it) {
  if (it && it.args && typeof it.args === "object") return it.args;
  if (it && typeof it.args === "string" && it.args) {
    try {
      const o = JSON.parse(it.args);
      if (o && typeof o === "object") return o;
    } catch (_) { /* plain-text args: no file fields */ }
  }
  return null;
}
function toolFiles(it) {
  // Basename list of path-like args (path, file, filePath, ...), deduped.
  const a = toolArgsObj(it);
  if (!a) return [];
  const out = [];
  const grab = (v) => {
    if (typeof v === "string" && v) {
      const b = v.split("/").pop().split("\\").pop().trim();
      if (b) out.push(b);
    } else if (Array.isArray(v)) v.forEach(grab);
  };
  for (const k of Object.keys(a)) if (/path|file/i.test(k)) grab(a[k]);
  return [...new Set(out)].filter(Boolean).slice(0, 6);
}
function toolFailed(it) {
  const s = String((it && it.status) || "").toLowerCase();
  return s === "failed" || s === "error" || !!(it && it.isError);
}
function toolGroupLabel(tool, n, files) {
  const f = (files || []).filter(Boolean);
  const tail = f.length
    ? " — " + f.slice(0, 3).join(", ") + (f.length > 3 ? `, +${f.length - 3}` : "")
    : "";
  return `${tool} × ${n}${tail}`;
}
// Stamp a tool row for grouping, filtering, and header file lists.
function tagToolLine(rec) {
  rec.line.dataset.toolKey = toolGroupKey(rec.item);
  const files = toolFiles(rec.item);
  if (files.length) rec.line.dataset.files = files.join("|");
  else delete rec.line.dataset.files;
  if (toolFailed(rec.item)) rec.line.dataset.failed = "1";
  else delete rec.line.dataset.failed;
}
function makeToolGroup(tool) {
  const group = document.createElement("div");
  group.className = "tool-group";
  group.dataset.tool = tool;
  const det = document.createElement("details");
  const sum = document.createElement("summary");
  sum.className = "tool-group-head";
  const items = document.createElement("div");
  items.className = "tool-group-items";
  det.append(sum, items);
  group.append(det);
  return group;
}
function refreshToolGroup(group) {
  const det = group.querySelector(":scope > details");
  const items = group.querySelector(":scope > details > .tool-group-items");
  const sum = group.querySelector(":scope > details > summary");
  const n = items ? items.children.length : 0;
  if (n < 2) {
    // Dissolve: a lone row reads better ungrouped.
    const parent = group.parentNode || el("terminal");
    while (items && items.firstChild) parent.insertBefore(items.firstChild, group);
    group.remove();
    return;
  }
  const tool = group.dataset.tool || "tool";
  const files = [];
  items.querySelectorAll(":scope > .tline").forEach((l) => {
    String(l.dataset.files || "").split("|").forEach((f) => {
      if (f && !files.includes(f)) files.push(f);
    });
  });
  sum.textContent = toolGroupLabel(tool, n, files);
  sum.title = `${n} consecutive ${tool} calls` +
    (files.length ? ` — ${files.join(", ")}` : "") + " — expand for detail";
  // A failed member forces the group open and tinted; a clean group
  // keeps whatever toggle state the user left it in.
  if (items.querySelector(":scope > .tline[data-failed]")) {
    group.dataset.failed = "1";
    if (det) det.open = true;
  } else delete group.dataset.failed;
}
function groupItemsBox(group) {
  return group.querySelector(":scope > details > .tool-group-items");
}
// Live + history-replay direction: the new row is the last child.
function groupToolLine(rec) {
  if (!rec || !rec.line || rec.line.classList.contains("streaming")) return;
  if (!rec.line.classList.contains("tool")) return;
  tagToolLine(rec);
  const key = toolGroupKey(rec.item);
  if (toolFailed(rec.item)) { ungroupToolLine(rec.line); return; }
  if (rec.line.closest(".tool-group")) {
    refreshToolGroup(rec.line.closest(".tool-group"));
    return;
  }
  const parent = rec.line.parentNode || el("terminal");
  let prev = rec.line.previousSibling;
  while (prev && prev.id === "thinking-row") prev = prev.previousSibling;
  if (prev && prev.classList && prev.classList.contains("tool-group") && prev.dataset.tool === key) {
    const box = groupItemsBox(prev);
    if (box.querySelector(".streaming")) return;
    box.append(rec.line);
    refreshToolGroup(prev);
    return;
  }
  if (prev && prev.classList && prev.classList.contains("tline") && prev.classList.contains("tool")
    && !prev.classList.contains("streaming") && prev.dataset.toolKey === key
    && !prev.hasAttribute("data-failed")) {
    const group = makeToolGroup(key);
    parent.insertBefore(group, prev);
    groupItemsBox(group).append(prev, rec.line);
    refreshToolGroup(group);
  }
}
// History-paging direction: the new row is prepended first.
function groupToolLinePrepend(rec) {
  if (!rec || !rec.line || !rec.line.classList.contains("tool")) return;
  if (rec.line.classList.contains("streaming")) return;
  tagToolLine(rec);
  const key = toolGroupKey(rec.item);
  if (toolFailed(rec.item)) return;
  let next = rec.line.nextSibling;
  while (next && next.id === "thinking-row") next = next.nextSibling;
  if (next && next.classList && next.classList.contains("tool-group") && next.dataset.tool === key) {
    groupItemsBox(next).prepend(rec.line);
    refreshToolGroup(next);
    return;
  }
  if (next && next.classList && next.classList.contains("tline") && next.classList.contains("tool")
    && !next.classList.contains("streaming") && next.dataset.toolKey === key
    && !next.hasAttribute("data-failed")) {
    const group = makeToolGroup(key);
    (rec.line.parentNode || el("terminal")).insertBefore(group, rec.line);
    groupItemsBox(group).append(rec.line, next);
    refreshToolGroup(group);
  }
}
// A row leaving the transcript (or changing kind) restores its group.
function ungroupToolLine(line) {
  const group = line.closest ? line.closest(".tool-group") : null;
  if (!group) return;
  (group.parentNode || el("terminal")).insertBefore(line, group.nextSibling);
  refreshToolGroup(group);
}
function removeGroupedLine(line) {
  const group = line.closest ? line.closest(".tool-group") : null;
  line.remove();
  if (group) refreshToolGroup(group);
}

/* Turn-level rollup: each live turn gets a borderless section with a
 * one-line head ("turn done · …") that collapses the whole turn. The
 * head doubles as the turn's completion record, so turn/completed
 * closes the block instead of printing a separate sysLine. History
 * rows predate any open block and keep landing in the terminal. */
function turnContainer() {
  const t = state.turnBlock;
  return (t && t.box.isConnected) ? t.box : el("terminal");
}
function openTurnBlock() {
  const block = document.createElement("div");
  block.className = "turn-block";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "turn-head";
  head.setAttribute("aria-expanded", "true");
  head.textContent = "turn · running…";
  head.title = "Collapse turn";
  head.onclick = () => {
    const shut = block.classList.toggle("collapsed");
    head.setAttribute("aria-expanded", String(!shut));
  };
  const box = document.createElement("div");
  box.className = "turn-items";
  block.append(head, box);
  el("terminal").append(block);
  state.turnBlock = {
    block, head, box, tools: new Map(), files: [], failed: false,
  };
}
function recordTurnItem(kind, item) {
  const t = state.turnBlock;
  if (!t) return;
  if (kind === "tool") {
    const k = toolGroupKey(item);
    t.tools.set(k, (t.tools.get(k) || 0) + 1);
    for (const f of toolFiles(item)) {
      if (f && !t.files.includes(f)) t.files.push(f);
    }
    if (toolFailed(item)) t.failed = true;
  } else if (kind === "error") t.failed = true;
}
function closeTurnBlock(text, isErr) {
  const t = state.turnBlock;
  state.turnBlock = null;
  if (!t || !t.block.isConnected) return;
  const bits = [];
  if (t.tools.size) {
    bits.push([...t.tools].map(([k, c]) => (c > 1 ? `${k} ×${c}` : k)).join(", "));
  }
  if (t.files.length) {
    bits.push(t.files.slice(0, 4).join(", ") +
      (t.files.length > 4 ? `, +${t.files.length - 4}` : ""));
  }
  t.head.textContent = text + (bits.length ? " · " + bits.join(" · ") : "");
  if (isErr || t.failed) t.head.classList.add("failed");
  t.block.dataset.closed = "1";
}
function dissolveTurnBlock() {
  const t = state.turnBlock;
  state.turnBlock = null;
  if (!t || !t.block.isConnected) return;
  while (t.box.firstChild) t.block.parentNode.insertBefore(t.box.firstChild, t.block);
  t.block.remove();
}

/* Long-output collapse: tool bodies past 12 lines (or 2000 chars)
 * show a head excerpt plus a "show all N lines" toggle. Agent and
 * user markdown is untouched. */
const OUT_MAX_LINES = 12;
const OUT_MAX_CHARS = 2000;
function maybeCollapseOutput(rec, txt) {
  if (!rec || !rec.body) return;
  const s = String(txt == null ? "" : txt);
  const lines = s.split("\n");
  if (lines.length <= OUT_MAX_LINES && s.length <= OUT_MAX_CHARS) return;
  const head = lines.slice(0, OUT_MAX_LINES).join("\n");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "out-toggle";
  btn.textContent = `show all ${lines.length} lines`;
  btn.title = `${s.length} characters — expand full output`;
  let open = false;
  btn.onclick = () => {
    open = !open;
    rec.body.textContent = open ? s : head;
    rec.body.append(btn);
    btn.textContent = open ? "show less" : `show all ${lines.length} lines`;
  };
  rec.body.textContent = head;
  rec.body.append(btn);
}

/* Transcript filter chips: all | edits | commands | errors. Specific
 * filters hide agent/user chatter and non-matching tool rows; groups
 * and turn blocks hide only when none of their rows match. */
function txVisible(line) {
  const f = state.txFilter || "all";
  if (f === "all") return true;
  if (line.id === "thinking-row") return true;
  if (line.classList.contains("error") || line.hasAttribute("data-failed")) {
    return f === "errors";
  }
  if (!line.classList.contains("tool")) return false;
  const k = line.dataset.toolKey || "";
  if (f === "edits") return k.includes("edit") || k.includes("write") || k.includes("apply");
  if (f === "commands") {
    return k.includes("bash") || k.includes("shell") ||
      k.includes("command") || k.includes("exec");
  }
  return false;
}
function applyTranscriptFilter() {
  const term = el("terminal");
  term.querySelectorAll(".tline").forEach((l) => { l.hidden = !txVisible(l); });
  term.querySelectorAll(".tool-group").forEach((g) => {
    let any = false;
    g.querySelectorAll(":scope > details > .tool-group-items > .tline").forEach((k) => {
      if (!k.hidden) any = true;
    });
    g.hidden = !any;
  });
  term.querySelectorAll(".turn-block").forEach((b) => {
    b.hidden = !b.querySelector(".tline:not([hidden])");
  });
}
function setTranscriptFilter(f) {
  state.txFilter = f;
  document.querySelectorAll("#tx-filters button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.txf === f));
  });
  applyTranscriptFilter();
}

/* ---------- markdown (transcript bodies) ---------- */
// Dependency-free, XSS-safe markdown for agent/user transcript bodies.
// Raw text is HTML-escaped first, then a small block/inline subset is
// shaped; URLs are scheme-checked so `javascript:` links stay inert.
// Streaming frames stay plain text (deltas append cheaply without
// re-parsing); the completed item re-renders as markdown.
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function sanitizeUrl(u) {
  const t = String(u || "").trim();
  if (!t) return null;
  const m = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.exec(t);
  if (!m) return t; // relative URL, anchor, or bare path: no scheme to abuse.
  const scheme = m[0].toLowerCase();
  if (scheme === "http:" || scheme === "https:" || scheme === "mailto:") return t;
  return null;
}
function renderInline(src) {
  const codeBits = [], linkBits = [];
  const inlineWith = (s, allowCode, allowLinks) => {
    if (allowCode) {
      s = s.replace(/`([^`\n]+?)`/g, (m, t) => {
        codeBits.push(`<code>${t}</code>`);
        return `\x00IC${codeBits.length - 1}\x00`;
      });
    }
    if (allowLinks) {
      s = s.replace(/\[([^\]]+?)\]\(((?:[^()\s]|\([^()]*\))+)(?:\s+"[^"]*")?\)/g, (m, text, url) => {
        const safe = sanitizeUrl(url);
        if (!safe) return text;
        linkBits.push(`<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${inlineWith(text, false, false)}</a>`);
        return `\x00LK${linkBits.length - 1}\x00`;
      });
      s = s.replace(/(https?:\/\/[^\s<]+)/g, (m, url) => {
        const trail = /[.,;:!?)\]]+$/.exec(url);
        let clean = url, suffix = "";
        if (trail) { clean = url.slice(0, -trail[0].length); suffix = trail[0]; }
        if (!sanitizeUrl(clean)) return m;
        linkBits.push(`<a href="${escapeHtml(clean)}" target="_blank" rel="noopener noreferrer">${clean}</a>`);
        return `\x00LK${linkBits.length - 1}\x00${suffix}`;
      });
    }
    return s
      .replace(/~~([^~]+?)~~/g, "<del>$1</del>")
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+?)__/g, "<strong>$1</strong>")
      .replace(/\*([^*]+?)\*/g, "<em>$1</em>")
      .replace(/(^|\W)_([^_]+?)_(\W|$)/g, "$1<em>$2</em>$3");
  };
  const out = inlineWith(String(src == null ? "" : src), true, true);
  return out
    .replace(/\x00IC(\d+)\x00/g, (m, i) => codeBits[+i] || "")
    .replace(/\x00LK(\d+)\x00/g, (m, i) => linkBits[+i] || "");
}
function renderMarkdown(src) {
  const raw = String(src == null ? "" : src).replace(/\r\n?/g, "\n");
  if (!raw.trim()) return "";
  // Fenced code blocks come out first (on the raw text) so no inline or
  // block rule can rewrite their contents.
  const fences = [];
  const deFenced = raw.replace(/```(\w*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    fences.push({ lang: (lang || "").slice(0, 20), code: code.replace(/\n$/, "") });
    return `\n\x00FENCE${fences.length - 1}\x00\n`;
  });
  const lines = escapeHtml(deFenced).split("\n");
  const out = [];
  let para = [], list = null, quote = [];
  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p>${para.map((l) => renderInline(l)).join("<br>")}</p>`);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const tag = list.ordered ? "ol" : "ul";
    out.push(`<${tag}>${list.items.map((l) => `<li>${renderInline(l)}</li>`).join("")}</${tag}>`);
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    out.push(`<blockquote>${quote.map((l) => renderInline(l)).join("<br>")}</blockquote>`);
    quote = [];
  };
  const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], t = line.trim();
    let m;
    const fm = /^\x00FENCE(\d+)\x00$/.exec(t);
    if (fm) {
      flushPara(); flushList(); flushQuote();
      const f = fences[+fm[1]] || { lang: "", code: "" };
      const cls = f.lang ? ` class="lang-${escapeHtml(f.lang)}"` : "";
      out.push(`<pre><code${cls}>${escapeHtml(f.code)}</code></pre>`);
      continue;
    }
    if (!t) { flushPara(); flushList(); flushQuote(); continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) {
      flushPara(); flushList(); flushQuote();
      out.push("<hr>");
      continue;
    }
    if ((m = /^(#{1,6})\s+(.*)$/.exec(t))) {
      flushPara(); flushList(); flushQuote();
      out.push(`<h${m[1].length}>${renderInline(m[2])}</h${m[1].length}>`);
      continue;
    }
    // GFM table: a pipe row followed by a delimiter row.
    if (t.includes("|") && i + 1 < lines.length &&
        /^\s*\|?[\s:|\-]+\|?[\s:|\-]*$/.test(lines[i + 1]) && /-/.test(lines[i + 1])) {
      flushPara(); flushList(); flushQuote();
      const head = cells(line), delim = cells(lines[i + 1]);
      const aligns = head.map((_, k) => {
        const d = (delim[k] || "").trim();
        if (/^:-+:$/.test(d)) return "center";
        if (/^-+:$/.test(d)) return "right";
        return "left";
      });
      i += 1;
      const rows = [];
      while (i + 1 < lines.length && lines[i + 1].includes("|") && lines[i + 1].trim()) {
        rows.push(cells(lines[++i]));
      }
      const th = head.map((c, k) =>
        `<th style="text-align:${aligns[k]}">${renderInline(c)}</th>`).join("");
      const tb = rows.map((r) =>
        `<tr>${head.map((_, k) =>
          `<td style="text-align:${aligns[k]}">${renderInline(r[k] || "")}</td>`).join("")}</tr>`).join("");
      out.push(`<table><thead><tr>${th}</tr></thead>${tb ? `<tbody>${tb}</tbody>` : ""}</table>`);
      continue;
    }
    if (/^&gt;/.test(t)) { flushPara(); flushList(); quote.push(t.replace(/^&gt;\s?/, "")); continue; }
    if ((m = /^(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(t))) {
      flushPara(); flushQuote();
      const ordered = !!m[2];
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] }; }
      list.items.push(m[3]);
      continue;
    }
    flushQuote();
    if (list && /^\s/.test(line)) { list.items.push(t); continue; }
    flushList();
    para.push(t);
  }
  flushPara(); flushList(); flushQuote();
  return out.join("");
}
// Chat-authored bodies (agent + user) render as markdown once complete.
// Tool, system, and error rows stay plain text, as do streaming frames.
function setBodyContent(rec, txt, kind, streaming) {
  const s = String(txt == null ? "" : txt);
  const useMd = !streaming && (kind === "agent" || kind === "user") && !!s.trim();
  rec.body.classList.toggle("md", useMd);
  if (useMd) {
    rec.body.innerHTML = renderMarkdown(s);
    addCodeCopyButtons(rec.body);
  } else rec.body.textContent = s;
}

/* Code-block copy buttons: a small transparent copy/check button pinned
 * top-right of each fenced block, after the animate-ui CopyButton
 * (clipboard write with execCommand fallback, check feedback for 3s). */
const COPY_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
const CHECK_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
function addCodeCopyButtons(root) {
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.parentElement && pre.parentElement.classList.contains("code-wrap")) return;
    const wrap = document.createElement("div");
    wrap.className = "code-wrap";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", "Copy code to clipboard");
    btn.title = "Copy code to clipboard";
    btn.innerHTML = COPY_SVG;
    btn.onclick = () => copyCodeBlock(btn, pre);
    pre.replaceWith(wrap);
    wrap.append(pre, btn);
  });
}
function copyCodeBlock(btn, pre) {
  const code = pre.querySelector("code");
  const text = code ? code.textContent : pre.textContent;
  if (!text) return;
  const done = () => flashCopied(btn);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done)
      .catch((e) => console.error("Error copying code block", e));
  } else {
    // Fallback for non-secure contexts without the async clipboard API.
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (e) { console.error("Error copying code block", e); }
    ta.remove();
  }
}
function flashCopied(btn) {
  if (btn.dataset.copied) return;
  btn.dataset.copied = "1";
  btn.innerHTML = CHECK_SVG;
  btn.setAttribute("aria-label", "Copied");
  btn.classList.remove("pop");
  void btn.offsetWidth;
  btn.classList.add("pop");
  setTimeout(() => {
    delete btn.dataset.copied;
    btn.innerHTML = COPY_SVG;
    btn.setAttribute("aria-label", "Copy code to clipboard");
  }, 3000);
}

// Optimistic echoes (itemId "local-*") are placeholders until the server's
// real userMessage arrives with its own itemId. Drop the oldest echo with
// matching text so one sent message renders exactly once.
function reconcileLocalEcho(serverItem) {
  if (!serverItem || itemKind(serverItem) !== "user") return;
  const text = itemText(serverItem);
  if (!text) return;
  for (const [id, rec] of state.items) {
    if (id.startsWith("local-") && itemText(rec.item) === text) {
      rec.line.remove();
      state.items.delete(id);
      return;
    }
  }
}

function renderItem(it, streaming) {
  if (!it || !it.itemId) return;
  if (!it.itemId.startsWith("local-") && !state.items.has(it.itemId)) {
    reconcileLocalEcho(it);
  }
  let rec = state.items.get(it.itemId);
  const kind = itemKind(it);
  if (isReminder(it)) {
    if (rec) { removeGroupedLine(rec.line); state.items.delete(it.itemId); }
    return;
  }
  if (!rec) {
    const line = document.createElement("div");
    line.className = `tline ${kind}`;
    const gut = document.createElement("span");
    gut.className = "gut";
    gut.textContent = GUTTER[kind] || "●";
    const wrap = document.createElement("span");
    wrap.className = "body";
    const head = document.createElement("div");
    head.className = "head";
    head.textContent = itemHeadLabel(it, kind);
    const body = document.createElement("span");
    body.className = "txt";
    wrap.append(head, body);
    line.append(gut, wrap);
    turnContainer().append(line);
    rec = { line, body, head, item: it };
    state.items.set(it.itemId, rec);
  } else {
    rec.item = Object.assign({}, rec.item, it);
    rec.line.className = `tline ${kind}`;
    rec.line.querySelector(".gut").textContent = GUTTER[kind] || "●";
    rec.head.textContent = itemHeadLabel(rec.item, kind);
    if (kind === "tool") tagToolLine(rec);
    else {
      delete rec.line.dataset.toolKey;
      delete rec.line.dataset.files;
      delete rec.line.dataset.failed;
    }
  }
  let txt = itemText(rec.item);
  if (!txt && kind === "tool" && !streaming) txt = toolSummary(rec.item);
  setBodyContent(rec, txt, kind, streaming);
  // Tool detail disclosure (raw JSON) — rebuilt only on completion to avoid churn.
  rec.line.querySelectorAll("details").forEach((d) => d.remove());
  if (kind === "tool" && !streaming) {
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "detail";
    const pre = document.createElement("pre");
    pre.className = "raw";
    pre.textContent = JSON.stringify(rec.item, null, 1).slice(0, 4000);
    det.append(sum, pre);
    rec.line.querySelector(".body").append(det);
    syncToolRow(it.itemId, rec.item);
  } else if (kind === "tool") {
    syncToolRow(it.itemId, rec.item);
  }
  if (kind === "tool" && !streaming) maybeCollapseOutput(rec, txt);
  // Streaming caret
  rec.body.querySelectorAll(".caret").forEach((c) => c.remove());
  if (streaming) {
    const c = document.createElement("span");
    c.className = "caret";
    rec.body.append(c);
  }
  rec.line.classList.toggle("streaming", !!streaming);
  if (kind === "tool" && !streaming) groupToolLine(rec);
  else ungroupToolLine(rec.line);
  if (!streaming && state.turnBlock) recordTurnItem(kind, rec.item);
  applyTranscriptFilter();
  updateWelcome();
  pinThinking();
  scrollDown();
}

function appendDelta(itemId, delta) {
  const rec = state.items.get(itemId);
  if (!rec) return;
  rec.body.querySelectorAll(".caret").forEach((c) => c.remove());
  // Streaming frames stay plain text even when the completed item will
  // render as markdown; item/completed re-renders via setBodyContent.
  const cur = itemText(rec.item) + delta;
  rec.item = Object.assign({}, rec.item, { text: cur });
  rec.body.classList.remove("md");
  rec.body.textContent = cur;
  const c = document.createElement("span");
  c.className = "caret";
  rec.body.append(c);
  rec.line.classList.add("streaming");
  scrollDown();
}

function syncToolRow(itemId, it) {
  let row = state.tools.get(itemId);
  if (!row) {
    row = document.createElement("div");
    row.className = "tool-row";
    row.innerHTML = "";
    const t = document.createElement("div");
    t.className = "t";
    const s = document.createElement("div");
    s.className = "s";
    row.append(t, s);
    el("tab-tools").prepend(row);
    state.tools.set(itemId, row);
  }
  row.querySelector(".t").textContent = String([toolType(it), it.status].filter(Boolean).join(" · ") || it.kind || "tool");
  row.querySelector(".s").textContent = toolSummary(it) || JSON.stringify(it).slice(0, 500);
}

function updateRunChip() {
  const chip = el("run-chip");
  chip.textContent = state.running ? "● running" : "idle";
  chip.className = "chip " + (state.running ? "running" : "idle");
  el("btn-stop").disabled = !state.running;
  // Idle composer glows; a running turn drops the aura.
  el("input").classList.toggle("idle-glow", !state.running);
  // A running turn keeps streaming transcript behind the popup: give it
  // a backdrop so the rows stay readable.
  el("slash-popup").classList.toggle("running", state.running);
  setConn(state.ws && state.ws.readyState === 1 ? (state.running ? "busy" : "on") : "off");
}
function updateCursorChip() {
  el("cursor-chip").textContent =
    (state.cursor ? "⌖ " + state.cursor.slice(-12) : "") +
    (state.ctxLine ? (state.cursor ? " · " : "") + state.ctxLine : "");
}
function updateOlderBtn() { el("btn-older").disabled = !state.hasOlder; }
function updateSessionDetail() {
  el("session-detail").textContent = state.session ? JSON.stringify(state.session, null, 1).slice(0, 2000)
    : "(no session — send a prompt or /new)";
  el("sess-meta").textContent = state.session
    ? `${state.session.turnCount || 0} turns · ${state.session.status || ""}` : "";
}

async function loadOlder() {
  if (!state.sessionId || !state.hasOlder) return;
  el("btn-older").disabled = true;
  try {
    // view/page: direction is forward|backward; result is {events, nextCursor}
    // where each event is an unframed {method, params} view notification,
    // always ascending by viewCursor in both directions.
    const p = { type: "page", sessionId: state.sessionId, limit: 50, direction: "backward" };
    if (state.pageCursor) p.cursor = state.pageCursor;
    const r = await send(p);
    const events = r.events || [];
    let added = 0;
    // Collect item snapshots oldest-first, then prepend in order.
    const fresh = [];
    for (const e of events) {
      const it = e && e.params && e.params.item;
      if (it && it.itemId && !state.items.has(it.itemId)
        && !fresh.some((x) => x.itemId === it.itemId)) fresh.push(it);
    }
    const term = el("terminal");
    const first = term.firstChild;
    // fresh is oldest-first; prepend newest-first so the oldest lands on top.
    for (let i = fresh.length - 1; i >= 0; i--) renderItemPrepend(fresh[i]);
    added = fresh.length;
    if (first && added) first.scrollIntoView();
    state.pageCursor = r.nextCursor != null ? r.nextCursor : null;
    state.hasOlder = state.pageCursor != null;
    if (!events.length || (state.pageCursor == null && !added)) {
      state.hasOlder = false;
      toast(added ? "reached oldest history" : "no older history");
    } else if (added) {
      toast(`loaded ${added} older item${added === 1 ? "" : "s"}`);
    }
  } catch (e) {
    // Hosts without view/page fail every attempt: stop offering /older.
    if (/not served/i.test(e.message)) {
      state.hasOlder = false;
      sysLine("History paging unavailable: this host serves no view/page.");
    }
    toast("page failed: " + e.message, true);
  }
  updateOlderBtn();
}

function renderItemPrepend(it) {
  if (!it || !it.itemId || state.items.has(it.itemId)) return;
  if (!it.itemId.startsWith("local-")) reconcileLocalEcho(it);
  if (isReminder(it)) return;
  const kind = itemKind(it);
  const line = document.createElement("div");
  line.className = `tline ${kind}`;
  const gut = document.createElement("span");
  gut.className = "gut"; gut.textContent = GUTTER[kind] || "●";
  const wrap = document.createElement("span");
  wrap.className = "body";
  const head = document.createElement("div");
  head.className = "head"; head.textContent = itemHeadLabel(it, kind);
  const body = document.createElement("span");
  body.className = "txt";
  const txt = itemText(it) || (kind === "tool" ? toolSummary(it) : "");
  wrap.append(head, body);
  line.append(gut, wrap);
  el("terminal").prepend(line);
  const rec = { line, body, head, item: it };
  state.items.set(it.itemId, rec);
  setBodyContent(rec, txt, kind, false);
  if (kind === "tool") { tagToolLine(rec); groupToolLinePrepend(rec); }
  applyTranscriptFilter();
  updateWelcome();
}

/* ---------- MSP event fan-in ---------- */
function onEvent(method, p) {
  p = p || {};
  if (p.sessionId && state.sessionId && p.sessionId !== state.sessionId) {
    if (method === "session/listChanged" || method === "session/started") refreshSessions().catch(() => {});
    return; // another session's traffic
  }
  if (p.viewCursor && (!p.sessionId || p.sessionId === state.sessionId)) {
    state.cursor = p.viewCursor; updateCursorChip();
  }
  switch (method) {
    case "item/started":
      renderItem(p.item, true);
      break;
    case "item/delta": {
      if (p.item) renderItem(p.item, true);
      else if (p.itemId && !state.items.has(p.itemId)) {
        renderItem({ itemId: p.itemId, kind: p.kind || "agentMessage", text: "" }, true);
      }
      if (p.field === "text" || p.field == null) appendDelta(p.itemId, p.delta || "");
      break;
    }
    case "item/completed":
    case "item/updated":
      // The model's answer fired: drop the thinking status now instead
      // of waiting for turn/completed (which can lag behind).
      if (p.item && itemKind(p.item) === "agent") hideThinking();
      renderItem(p.item || { itemId: p.itemId, kind: p.kind, text: p.text }, false);
      break;
    case "turn/started":
      state.running = true; state.turnId = p.turnId || null; updateRunChip();
      openTurnBlock();
      showThinking();
      if (state.queuedTurnId && p.turnId === state.queuedTurnId) {
        state.queuedTurnId = null;
        sysLine("queued turn started.");
      }
      break;
    case "turn/completed": {
      hideThinking();
      // Single terminal event: p.terminal is completed|failed|cancelled.
      // (There are no turn/cancelled or turn/failed notifications on MSP v1.)
      state.running = false; state.turnId = null; updateRunChip();
      document.querySelectorAll(".tline.streaming").forEach((d) => {
        d.classList.remove("streaming");
        d.querySelectorAll(".caret").forEach((c) => c.remove());
      });
      const term = p.terminal || "completed";
      const tok = (p.usage && p.usage.totalTokens != null) ? ` · ${p.usage.totalTokens} tok` : "";
      const dur = (p.durationMs != null) ? ` · ${(p.durationMs / 1000).toFixed(1)}s` : "";
      // The open turn block's head doubles as the completion record;
      // without one (missed turn/started) fall back to a plain sysLine.
      if (state.turnBlock) {
        if (term === "failed") {
          closeTurnBlock("turn failed: " +
            String((p.error && (p.error.message || p.error.code)) || p.reason || "unknown") + dur + tok, true);
        } else if (term === "cancelled") {
          closeTurnBlock("turn cancelled" +
            (p.reason ? ": " + String(p.reason) : "") + dur + tok, false);
        } else {
          closeTurnBlock("turn done" + dur + tok, false);
        }
        applyTranscriptFilter();
      } else if (term === "failed") {
        sysLine("turn failed: " + String((p.error && (p.error.message || p.error.code)) || p.reason || "unknown"), true);
      } else if (term === "cancelled") {
        sysLine("turn cancelled." + (p.reason ? " " + String(p.reason) : ""));
      } else if (p.usage && p.usage.totalTokens != null) {
        sysLine(`turn done · ${p.usage.totalTokens} tok` +
          (p.durationMs != null ? ` · ${(p.durationMs / 1000).toFixed(1)}s` : ""));
      }
      refreshUsage().catch(() => {});
      refreshSessions().catch(() => {});
      break;
    }
    case "turn/retracted":
      hideThinking();
      dissolveTurnBlock();
      sysLine("turn retracted — prompt restored to the composer.");
      if (p.promptText) { el("input").value = p.promptText; autosize(); }
      break;
    case "turn/unqueued":
      state.queuedTurnId = null;
      sysLine("queued turn reclaimed.");
      break;
    case "turn/retryScheduled":
      sysLine("turn retry scheduled" + (p.reason ? ": " + String(p.reason) : "") + ".");
      break;
    case "session/statusChanged":
      state.running = String(p.status || "").toLowerCase().trim() === "running";
      updateRunChip();
      // A turn started elsewhere (TUI, other client, reconnect) never fires
      // turn/started here: start the verbs off the status flip instead.
      if (state.running && !document.getElementById("thinking-row")) showThinking();
      if (state.session) { state.session.status = p.status; updateSessionDetail(); }
      break;
    case "session/tokenUsage":
      if (p.cumulative) {
        // CumulativeTokenUsage: {promptTokens, outputTokens, totalTokens,
        // cacheReadTokens?, cacheWriteTokens?, cost?: {usd, partial}}.
        // Session line lives in #sess-usage; #usage is the subscription
        // line — the two writers never share an element.
        const c = p.cumulative;
        state.lastCumulative = c;
        el("sess-usage").textContent = `${c.totalTokens || 0} tok` +
          (c.cost && c.cost.usd != null
            ? ` · $${Number(c.cost.usd).toFixed(3)}${c.cost.partial ? " (partial)" : ""}` : "");
        el("sess-usage").title = `session cumulative: ${c.promptTokens || 0}↑ ${c.outputTokens || 0}↓` +
          (c.cacheReadTokens ? ` · cache ${c.cacheReadTokens}r/${c.cacheWriteTokens || 0}w` : "");
      }
      break;
    case "session/contextUsage":
      // Context-window pressure triple (usedTokens/windowTokens/pressure).
      if (p.usedTokens != null && p.windowTokens) {
        state.lastContext = { usedTokens: p.usedTokens, windowTokens: p.windowTokens, pressure: p.pressure };
        const pct = Math.round((p.usedTokens / p.windowTokens) * 100);
        state.ctxLine = `ctx ${pct}%${p.pressure ? " " + p.pressure : ""}`;
        updateCursorChip();
        el("cursor-chip").title = `Context: ${p.usedTokens}/${p.windowTokens} tokens · pressure ${p.pressure || "?"}`;
      }
      break;
    case "usage/changed":
      // Same payload shape as usage/read's usage member; refresh the footer.
      refreshUsage().catch(() => {});
      break;
    case "githubCloneProgress":
      onGithubProgress(p);
      break;
    case "githubCloneResult":
      onGithubResult(p);
      break;
    case "githubAutoApproved":
      sysLine(`auto-approved (github policy): ${String(p.command || "gh command")}`);
      break;
    case "themeApply":
      applyThemeOrder(p);
      break;
    case "ordersPending":
      renderOrderCard(p);
      break;
    case "session/listChanged":
    case "session/started":
    case "session/closed":
      refreshSessions().catch(() => {});
      break;
    case "approval/updated":
      if (p.sessionId === state.sessionId || !state.sessionId) fetchPending();
      break;
    case "approval/resolved":
      removeCard(p.approvalId);
      break;
    case "userInput/settled":
      removeCard(p.userInputId);
      break;
    case "session/modelChanged":
      // (The old "model/changed" name never existed on MSP v1.)
      sysLine(`model → ${p.modelId || "?"}${p.providerId ? " (" + p.providerId + ")" : ""}.`);
      refreshModels().catch(() => {});
      break;
    case "session/nameChanged":
      if (state.session) {
        state.session.name = p.name || state.session.name;
        el("session-title").textContent = sessionDisplayName(state.session);
      }
      refreshSessions().catch(() => {});
      break;
    case "session/reasoningEffortChanged":
      sysLine(`reasoning effort → ${p.reasoningEffort || "?"} (session default).`);
      break;
    case "session/approvalModeChanged":
      sysLine(`approval mode → ${p.mode || p.approvalMode || "?"} (host).`);
      break;
    case "session/todoListChanged":
      break;
    default:
      break;
  }
}

/* ---------- approvals & user input ---------- */
function cardShell(aid) {
  removeCard(aid);
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.aid = aid;
  return div;
}
function removeCard(aid) {
  if (!aid) return;
  document.querySelectorAll(`[data-aid="${CSS.escape(aid)}"]`).forEach((d) => d.remove());
}
function applyThemeOrder(p) {
  // Agent order (validated bridge-side): re-validate, apply, persist.
  const colors = p && p.colors;
  const T = window.WebMuseTheme;
  if (!T || !colors || typeof colors !== "object") return;
  const clean = {};
  for (const k of Object.keys(colors)) {
    const v = colors[k];
    if (typeof T.colors[k] === "string" && typeof v === "string"
        && v && v.length <= 500) clean[k] = v;
  }
  const n = Object.keys(clean).length;
  if (!n) return;
  let merged = {};
  try {
    merged = JSON.parse(localStorage.getItem(T.storageKey) || "{}") || {};
  } catch (_) { merged = {}; }
  Object.assign(merged, clean);
  try { localStorage.setItem(T.storageKey, JSON.stringify(merged)); } catch (_) {}
  T.apply(merged);
  sysLine(`theme updated by session ${shortId(p.sessionId)} (${n} colors, order ${p.orderId}).`);
  toast(`theme updated (${n} colors)`);
}
function renderOrderCard(p) {
  // Staged policy order: the human approves or denies it here.
  const aid = "order-" + (p.sessionId || "") + "-" + (p.orderId || "");
  removeCard(aid);
  const div = cardShell(aid);
  const h = document.createElement("h4");
  h.textContent = `Order: ${p.action || "policy update"} (${shortId(p.sessionId)})`;
  div.append(h);
  const pre = document.createElement("pre");
  try {
    pre.textContent = JSON.stringify(p.params || {}, null, 2).slice(0, 2000);
  } catch (_) { pre.textContent = "(unrenderable params)"; }
  div.append(pre);
  const row = document.createElement("div");
  row.className = "choices";
  const yes = document.createElement("button");
  yes.className = "allow"; yes.textContent = "Approve";
  yes.onclick = () => decideOrder(p, true);
  const no = document.createElement("button");
  no.className = "deny"; no.textContent = "Deny";
  no.onclick = () => decideOrder(p, false);
  row.append(yes, no);
  div.append(row);
  el("cards").append(div);
  sysLine(`order staged: ${p.action || "?"} — approve or deny in the card above.`);
}
function decideOrder(p, approved) {
  send({ type: "ordersDecide", sessionId: p.sessionId,
         orderId: p.orderId, approved })
    .then((r) => {
      removeCard("order-" + (p.sessionId || "") + "-" + (p.orderId || ""));
      sysLine(`order ${p.orderId} ` +
        (r && r.approved ? "approved and applied." : "denied."));
    })
    .catch((e) => toast("ordersDecide failed: " + e.message, true));
}

function onApproval(a) {
  if (!a || !a.approvalId) return;
  const req = a.currentRequirementId || a.requirementId;
  // ApprovalChoice: {choiceId, decision, label, scope, acceptsFeedback?}.
  const choices = a.availableChoices || [
    { choiceId: "allow", label: "Allow" }, { choiceId: "deny", label: "Deny" }];
  const div = cardShell(a.approvalId);
  const h = document.createElement("h4");
  h.textContent = `Approval: ${a.toolName || (a.subject && a.subject.kind) || "tool"}`;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(a.subject || a, null, 1).slice(0, 1000);
  let escNote = null;
  if (a.judgeEscalated) {
    escNote = document.createElement("div");
    escNote.className = "q";
    escNote.textContent = "escalated by approval judge";
  }
  const fb = document.createElement("input");
  fb.type = "text";
  fb.placeholder = "Feedback for the model (optional, denial only)";
  fb.style.display = choices.some((c) => c.acceptsFeedback) ? "" : "none";
  const row = document.createElement("div");
  row.className = "choices";
  for (const c of choices) {
    const cid = c.choiceId || c.id;
    const b = document.createElement("button");
    b.textContent = c.label || cid;
    b.title = c.decision ? `${c.decision} · ${c.scope || ""}` : "";
    if (/allow|approv/i.test(c.decision || c.label || cid)) b.classList.add("allow");
    if (/den/i.test(c.decision || "") || /deny|reject/i.test(c.label || cid)) b.classList.add("deny");
    b.onclick = () => decideApproval(a, req, cid, c, fb.value);
    row.append(b);
  }
  div.append(h);
  if (escNote) div.append(escNote);
  div.append(pre, fb, row);
  // Approvals live ONLY in the right-hand inspector (Approvals tab) —
  // never in the transcript. The sysLine below is the transcript's only
  // trace, so a parked agent is still noticeable there.
  el("tab-approvals").append(div);
  sysLine(`approval requested: ${a.toolName || "tool"} — decide in the inspector (Approvals tab).`);
  // A parked agent is worse than a moved panel: on desktop make sure the
  // card is actually seen (mobile keeps its flash-open behavior below).
  if (window.innerWidth >= 900 && !el("inspector").classList.contains("open")) {
    el("inspector").classList.add("open");
    syncScrim();
  }
  openInspectorOnMobile("approvals");
}

function decideApproval(a, req, choiceId, choice, feedback) {
  const msg = { type: "approve", sessionId: a.sessionId || state.sessionId,
    approvalId: a.approvalId, choiceId, requirementId: req };
  // feedback is only valid on choices with acceptsFeedback — never persist.
  if (choice && choice.acceptsFeedback && feedback && feedback.trim()) {
    msg.feedback = feedback.trim().slice(0, 2000);
  }
  send(msg)
    .then(() => { removeCard(a.approvalId); toast("decision sent: " + choiceId); })
    .catch((e) => toast("decide failed: " + e.message, true));
}

function onUserInput(p) {
  if (!p || !p.userInputId) return;
  const div = cardShell(p.userInputId);
  const h = document.createElement("h4");
  h.textContent = `Input needed${p.toolName ? ": " + p.toolName : ""}`;
  div.append(h);
  // UserInputQuestion: {id, header, question, options[{label,description}],
  // selection:{mode: single|multiple, minSelections?, maxSelections?}}.
  // Answer carries questionId + exactly one of selectedLabel (single),
  // selectedLabels (multiple) or freeText (<=500 chars).
  const questions = p.questions || [];
  // Immediate mode: one single-select question -> tap sends, custom row
  // has its own Send. Everything else is one form with one submit.
  const immediate = questions.length === 1 &&
    (((questions[0].selection && questions[0].selection.mode) || "single") === "single");
  const qidOf = (q, i) => questionId(q, i);

  const sendAnswers = (payload) => send({
    type: "answer", sessionId: p.sessionId || state.sessionId,
    userInputId: p.userInputId, answers: payload })
    .then(() => { removeCard(p.userInputId); toast("answer sent"); })
    .catch((e) => toast("answer failed: " + e.message, true));

  const mountQuestion = (q, form, qi) => {
    const qid = qidOf(q, qi);
    if (q.header) {
      const hd = document.createElement("div");
      hd.className = "qhead";
      hd.textContent = q.header;
      div.append(hd);
    }
    const ql = document.createElement("div");
    ql.className = "q";
    ql.textContent = questionText(q) || q.header || qid;
    div.append(ql);
    const mode = (q.selection && q.selection.mode) || "single";
    // Options may arrive as strings, numbers, or objects whose label lives
    // under a synonym key — drop anything with no usable label so a chip
    // never renders (or sends) "[object Object]".
    const opts = (q.options || []).filter((o) => optionLabel(o));
    const min = (q.selection && q.selection.minSelections) || 0;
    const max = (q.selection && q.selection.maxSelections) || 0;
    const group = document.createElement("div");
    group.className = "opts";
    group.setAttribute("role", "group");
    if (immediate) {
      // Tap an option -> send at once. The first option is the default
      // selection (marked picked) so the card can never submit empty.
      opts.forEach((o, i) => {
        const b = chipButton(o);
        b.setAttribute("aria-pressed", i === 0 ? "true" : "false");
        if (i === 0) b.classList.add("picked");
        b.onclick = () => sendAnswers([{ questionId: qid,
          selectedLabel: optionLabel(o) }]);
        group.append(b);
      });
    } else if (opts.length) {
      // Arm picks; the form submit sends them.
      const picked = new Set();
      const chips = [];
      for (const o of opts) {
        const b = chipButton(o);
        b.setAttribute("aria-pressed", "false");
        b.onclick = () => {
          const label = optionLabel(o);
          if (mode === "multiple") {
            if (picked.has(label)) {
              picked.delete(label);
            } else {
              if (max && picked.size >= max) {
                toast(`at most ${max} selection${max === 1 ? "" : "s"}`, true);
                return;
              }
              picked.add(label);
            }
            b.classList.toggle("picked", picked.has(label));
            b.setAttribute("aria-pressed", picked.has(label) ? "true" : "false");
          } else {
            picked.clear();
            picked.add(label);
            for (const c of chips) {
              const on = c === b;
              c.classList.toggle("picked", on);
              c.setAttribute("aria-pressed", on ? "true" : "false");
            }
          }
          form.sync();
        };
        chips.push(b);
        group.append(b);
      }
      if (mode !== "multiple" && chips.length) {
        // Default selection: the first option starts picked, so a
        // single-select slot is submittable without any tap.
        picked.add(optionLabel(opts[0]));
        chips[0].classList.add("picked");
        chips[0].setAttribute("aria-pressed", "true");
      }
      form.slots.push({ qid, mode, min, max, picked, q });
    }
    div.append(group);
    // Custom row: own Send in immediate mode, form slot otherwise.
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = opts.length ? "…or type a custom answer" : "answer";
    inp.maxLength = 500;
    if (immediate) {
      const row = document.createElement("div");
      row.className = "custom-row";
      const sendBtn = document.createElement("button");
      sendBtn.className = "allow";
      sendBtn.textContent = "Send";
      const go = () => {
        const t = inp.value.trim();
        if (t) {
          sendAnswers([{ questionId: qid, freeText: t.slice(0, 500) }]);
          return;
        }
        // Empty custom input submits the default selection (option 1);
        // with no options at all there is nothing to send.
        if (opts.length) {
          sendAnswers([{ questionId: qid,
            selectedLabel: optionLabel(opts[0]) }]);
        }
      };
      sendBtn.onclick = go;
      inp.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); go(); }
      });
      row.append(inp, sendBtn);
      div.append(row);
    } else {
      div.append(inp);
      const slot = form.slots[form.slots.length - 1];
      if (slot && slot.qid === qid) slot.inp = inp;
      else form.slots.push({ qid, mode, min, max, picked: new Set(), q, inp });
      inp.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); form.submit(); }
      });
    }
  };

  if (immediate) {
    mountQuestion(questions[0], null, 0);
  } else {
    const form = {
      slots: [],
      submitBtn: null,
      sync() {
        // Submit arms only when every slot is answerable; multi slots
        // need min..max counts unless custom text fills the slot.
        let ok = true;
        for (const s of this.slots) {
          const custom = s.inp && s.inp.value.trim();
          if (custom) continue;
          if (s.mode === "multiple") {
            if (s.picked.size < s.min || (s.max && s.picked.size > s.max)) {
              ok = false;
              break;
            }
          } else if (!s.picked.size) {
            ok = false;
            break;
          }
        }
        this.submitBtn.classList.toggle("armed", ok);
        this.submitBtn.setAttribute("aria-disabled", ok ? "false" : "true");
      },
      submit() {
        const payload = [];
        for (const s of this.slots) {
          const custom = (s.inp && s.inp.value.trim()) || "";
          if (custom) {
            payload.push({ questionId: s.qid,
              freeText: custom.slice(0, 500) });
            continue;
          }
          if (s.mode === "multiple") {
            if (s.picked.size < s.min) {
              toast(`"${qTitle(s.q)}" needs at least ${s.min} ` +
                `selection${s.min === 1 ? "" : "s"}`, true);
              return;
            }
            payload.push({ questionId: s.qid,
              selectedLabels: [...s.picked] });
          } else {
            if (!s.picked.size) {
              toast(`pick an option or type an answer for "${qTitle(s.q)}"`,
                true);
              return;
            }
            payload.push({ questionId: s.qid,
              selectedLabel: [...s.picked][0] });
          }
        }
        sendAnswers(payload);
      },
    };
    questions.forEach((q, i) => mountQuestion(q, form, i));
    // Typing custom text can satisfy a slot: re-sync validity on input.
    for (const s of form.slots) {
      if (s.inp) s.inp.addEventListener("input", () => form.sync());
    }
    const btn = document.createElement("button");
    btn.className = "allow submit";
    btn.textContent = "Submit";
    btn.style.marginTop = "8px";
    btn.onclick = () => form.submit();
    form.submitBtn = btn;
    div.append(btn);
    form.sync();
  }
  el("cards").append(div);
  sysLine("input requested — answer in the card above the composer.");
}

function asText(v) {
  // Primitive display text; objects/arrays/null never pass through, so a
  // chip can never render (or send) "[object Object]".
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean" ||
      typeof v === "bigint") return String(v);
  return "";
}
function optionLabel(o) {
  if (o == null) return "";
  if (typeof o === "object") {
    for (const k of ["label", "name", "text", "value", "title"]) {
      const t = asText(o[k]);
      if (t) return t;
    }
    return "";
  }
  return asText(o);
}
function optionDesc(o) {
  if (o && typeof o === "object") {
    for (const k of ["description", "detail", "hint", "subtitle"]) {
      const t = asText(o[k]);
      if (t) return t;
    }
  }
  return "";
}
function questionText(q) {
  if (!q || typeof q !== "object") return asText(q);
  for (const k of ["question", "prompt", "title", "text"]) {
    const t = asText(q[k]);
    if (t) return t;
  }
  return "";
}
function questionId(q, i) {
  if (q && typeof q === "object") {
    for (const k of ["id", "questionId", "key"]) {
      const t = asText(q[k]);
      if (t) return t;
    }
  }
  return `q${(i == null ? 0 : i) + 1}`;
}
function chipButton(o) {
  const label = optionLabel(o);
  const desc = optionDesc(o);
  const b = document.createElement("button");
  b.type = "button";
  b.className = "opt";
  const t = document.createElement("span");
  t.className = "o-label";
  t.textContent = label;
  b.append(t);
  if (desc) {
    const d = document.createElement("span");
    d.className = "o-desc";
    d.textContent = desc;
    b.append(d);
  }
  b.title = desc || label;
  return b;
}
function qTitle(q) {
  return questionText(q) ||
    ((q && (q.header || questionId(q))) || "question");
}

async function fetchPending() {
  if (!state.sessionId) return;
  try {
    // approval/listPending -> {approvals, userInputs} (both exact request params).
    const r = await send({ type: "pending", sessionId: state.sessionId });
    for (const a of r.approvals || r.pending || []) onApproval(a);
    for (const u of r.userInputs || []) onUserInput(u);
  } catch (_) { /* best effort */ }
}

/* ---------- models & usage ---------- */
// The model pick survives reloads (localStorage): changing it anywhere
// saves it, startup restores it, and the picker shows it whenever the
// catalog still lists it.
const PICKED_MODEL_KEY = "webmuse.pickedModel";
function savePickedModel() {
  try {
    if (state.pickedModel && state.pickedModel.modelId) {
      const p = { modelId: state.pickedModel.modelId };
      if (state.pickedModel.providerId) p.providerId = state.pickedModel.providerId;
      localStorage.setItem(PICKED_MODEL_KEY, JSON.stringify(p));
    } else {
      localStorage.removeItem(PICKED_MODEL_KEY);
    }
  } catch (_) { /* storage unavailable: memory-only */ }
}
function loadPickedModel() {
  try {
    const raw = localStorage.getItem(PICKED_MODEL_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if (p && typeof p.modelId === "string" && p.modelId) {
      state.pickedModel = { modelId: p.modelId,
        providerId: typeof p.providerId === "string" && p.providerId
          ? p.providerId : undefined };
    }
  } catch (_) { /* corrupt or unavailable: no default */ }
}
// The effort pick survives reloads (localStorage) like the model pick:
// changing it anywhere saves it, startup restores it, and the picker
// shows it.
const EFFORT_TIERS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
const PICKED_EFFORT_KEY = "webmuse.pickedEffort";
function savePickedEffort() {
  try {
    if (state.pickedEffort) {
      localStorage.setItem(PICKED_EFFORT_KEY, state.pickedEffort);
    } else {
      localStorage.removeItem(PICKED_EFFORT_KEY);
    }
  } catch (_) { /* storage unavailable: memory-only */ }
}
function loadPickedEffort() {
  try {
    const raw = localStorage.getItem(PICKED_EFFORT_KEY);
    if (EFFORT_TIERS.includes(raw)) {
      state.pickedEffort = raw;
    }
  } catch (_) { /* corrupt or unavailable: no default */ }
}
function syncEffortPicker() {
  const sel = el("effort-picker");
  if (!sel) return;
  sel.value = state.pickedEffort || "";
}
// The approval-mode pick survives reloads (localStorage) like the model
// and effort picks: changing it anywhere saves it, startup restores it
// into the Session-panel select, and created sessions inherit it.
const APPROVAL_MODES = ["onRequest", "promptUnmatched", "denyUnmatched", "allowAll"];
const PICKED_APPROVAL_KEY = "webmuse.pickedApprovalMode";
function savePickedApprovalMode() {
  try {
    if (state.pickedApprovalMode) {
      localStorage.setItem(PICKED_APPROVAL_KEY, state.pickedApprovalMode);
    } else {
      localStorage.removeItem(PICKED_APPROVAL_KEY);
    }
  } catch (_) { /* storage unavailable: memory-only */ }
}
function loadPickedApprovalMode() {
  try {
    const raw = localStorage.getItem(PICKED_APPROVAL_KEY);
    if (APPROVAL_MODES.includes(raw)) {
      state.pickedApprovalMode = raw;
    }
  } catch (_) { /* corrupt or unavailable: no default */ }
}
function syncApprovalSelect() {
  const sel = el("approval-mode");
  if (!sel) return;
  sel.value = state.pickedApprovalMode || "onRequest";
  sel.dataset.prev = sel.value;
  syncApprovalWarn();
}
async function refreshModels() {
  const r = await send({ type: "models", sessionId: state.sessionId || undefined });
  state.models = r.models || [];
  state.modelsMeta = { source: r.source, providerId: r.providerId, profileId: r.profileId };
  const sel = el("model-picker");
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = ""; ph.textContent = "model…";
  sel.append(ph);
  const want = state.pickedModel && state.pickedModel.modelId;
  for (const m of state.models) {
    const o = document.createElement("option");
    o.value = m.modelId;
    o.textContent = (m.isActive ? "● " : "") + (m.displayLabel || m.modelId);
    o.dataset.provider = m.providerId || "";
    // A saved pick wins over the host's active mark; without one the
    // active model shows as before.
    if (want ? m.modelId === want : m.isActive) o.selected = true;
    sel.append(o);
  }
}

function fmtUsage(u) {
  // SubscriptionUsage: {observedAtMs, tier, weekly, window}. Point-in-time:
  // render "as of", never imply live data.
  if (!u || !u.window) return null;
  const w = u.window, wk = u.weekly || {};
  const asof = u.observedAtMs ? new Date(u.observedAtMs) : null;
  const hhmm = asof
    ? String(asof.getHours()).padStart(2, "0") + ":" + String(asof.getMinutes()).padStart(2, "0")
    : "?";
  return `${w.usedPercent}%/${w.windowDurationMins || "?"}m · wk ${wk.usedPercent != null ? wk.usedPercent + "%" : "?"} · ${u.tier || "?"} · as of ${hhmm}`;
}

function fmtUsageLong(u) {
  if (!u || !u.window) return "no subscription usage observed yet (usage/read omits usage until the first observation).";
  const w = u.window, wk = u.weekly || {};
  const dt = (ms) => (ms ? new Date(ms).toLocaleString() : "?");
  return `Subscription usage (as of ${dt(u.observedAtMs)}, tier ${u.tier || "?"}):\n` +
    `  window: ${w.usedPercent}% of ${w.windowDurationMins || "?"}m block · resets ${dt(w.resetsAtMs)}\n` +
    `  weekly: ${wk.usedPercent != null ? wk.usedPercent + "%" : "?"} · resets ${dt(wk.resetsAtMs)}`;
}

async function refreshUsage() {
  try {
    // usage/read -> {usage?}; omitted, never null, when nothing observed.
    const r = await send({ type: "usage" });
    const line = fmtUsage(r.usage);
    if (line) {
      el("usage").textContent = line;
      el("usage").title = fmtUsageLong(r.usage);
    } else if (r.usage && Object.keys(r.usage).length) {
      el("usage").textContent = JSON.stringify(r.usage).slice(0, 100);
    }
    // else: keep the session-token footer from session/tokenUsage events.
  } catch (_) { /* optional */ }
}

/* ---------- slash commands (TUI parity) ---------- */
const SLASH = [
  { name: "help", usage: "/help", desc: "List slash commands", run: () => cmdHelp() },
  { name: "new", usage: "/new [name] [--mcp a,b] [--path <dir>]", desc: "Start a new session", run: (a) => { const p = parseNewArgs(a); return newSession(p.name, p); } },
  { name: "list", usage: "/list", desc: "Refresh session list", run: () => refreshSessions().then(() => toast("sessions refreshed")) },
  { name: "sessions", usage: "/sessions", desc: "Alias for /list", run: () => refreshSessions().then(() => toast("sessions refreshed")) },
  { name: "resume", usage: "/resume <id-prefix>", desc: "Open a session by id prefix", run: (a) => cmdResume(a) },
  { name: "open", usage: "/open <id-prefix>", desc: "Alias for /resume", run: (a) => cmdResume(a) },
  { name: "rename", usage: "/rename <name>", desc: "Rename current session", run: (a) => renameSession(null, a.join(" ")) },
  { name: "fork", usage: "/fork", desc: "Fork current session", run: () => forkSession() },
  { name: "delete", usage: "/delete", desc: "Delete current session (confirm)", run: () => deleteSession() },
  { name: "sync", usage: "/sync", desc: "Delete session files whose workspace dir is gone (preview + confirm)", run: () => cmdSync() },
  { name: "clear", usage: "/clear", desc: "Clear local transcript view", run: () => { clearTranscriptKeepSession(); } },
  { name: "models", usage: "/models", desc: "List models in transcript", run: () => cmdModels() },
  { name: "model", usage: "/model <id>", desc: "Set model for current session", run: (a) => cmdSetModel(a) },
  { name: "effort", usage: "/effort <tier>", desc: "Set reasoning effort for current session (remembers default)", run: (a) => cmdSetEffort(a) },
  { name: "default-effort", usage: "/default-effort <tier|clear|show>", desc: "Set default reasoning effort for new chats", run: (a) => cmdDefaultEffort(a) },
  { name: "skills", usage: "/skills", desc: "List session skills", run: () => cmdSkills() },
  { name: "mcp", usage: "/mcp", desc: "Show configured MCP servers", run: () => cmdMcp() },
  { name: "github", usage: "/github list|clone|open|clean|cancel …", desc: "GitHub repos via gh", run: (a) => cmdGithub(a) },
  { name: "output", usage: "/output <itemId>", desc: "Fetch full truncated output", run: (a) => cmdOutput(a) },
  { name: "compact", usage: "/compact", desc: "Compact current session", run: () => cmdCompact() },
  { name: "usage", usage: "/usage", desc: "Show subscription usage", run: () => cmdUsage() },
  { name: "pending", usage: "/pending", desc: "Show pending approvals", run: () => fetchPending().then(() => toast("pending refreshed")) },
  { name: "interrupt", usage: "/interrupt", desc: "Interrupt running turn", run: () => cmdInterrupt() },
  { name: "stop", usage: "/stop", desc: "Alias for /interrupt", run: () => cmdInterrupt() },
  { name: "cancel", usage: "/cancel", desc: "Cancel running turn", run: () => cmdCancel() },
  { name: "steer", usage: "/steer <text>", desc: "Steer running turn", run: (a, raw) => cmdSteer(raw) },
  { name: "unqueue", usage: "/unqueue", desc: "Reclaim queued follow-up turn", run: () => cmdUnqueue() },
  { name: "older", usage: "/older", desc: "Load older history", run: () => loadOlder() },
];

function cmdHelp() {
  const lines = SLASH.map((c) => `${c.usage.padEnd(22)} ${c.desc}`).join("\n");
  sysLine("Slash commands:\n" + lines + "\n\nKeys: Enter send · Shift+Enter newline · ↑/↓ history · Tab complete / · Esc dismiss");
}

function clearTranscriptKeepSession() {
  const sid = state.sessionId, sess = state.session, cur = state.cursor;
  const cum = state.lastCumulative, ctx = state.lastContext, mcp = state.sessionMcp;
  clearTranscript();
  state.sessionId = sid; state.session = sess; state.cursor = cur;
  // Same session: usage and MCP attachment are still valid.
  state.lastCumulative = cum; state.lastContext = ctx; state.sessionMcp = mcp;
  updateCursorChip(); updateSessionDetail();
  sysLine("View cleared (session kept). /resume or reload to refetch history.");
}

async function cmdResume(args) {
  const prefix = (args[0] || "").toLowerCase();
  if (!prefix) {
    sysLine("Sessions:\n" + state.sessionsCache.map((s) =>
      `  ${s.sessionId.slice(0, 8)}  ${s.name || "(unnamed)"}  [${s.status || "?"}]`).join("\n"));
    return;
  }
  const hits = state.sessionsCache.filter((s) => s.sessionId.toLowerCase().startsWith(prefix));
  if (!hits.length) { sysLine("No session matches prefix " + prefix, true); return; }
  if (hits.length > 1) {
    sysLine("Ambiguous prefix " + prefix + " — matches:\n" + hits.map((s) =>
      `  ${s.sessionId.slice(0, 8)}  ${s.name || "(unnamed)"}`).join("\n"), true);
    return;
  }
  await openSession(hits[0].sessionId);
}

function fmtModelRow(m) {
  const head = `  ${m.isActive ? "●" : "○"} ${m.modelId}` +
    (m.isDefault ? " [default]" : "") +
    (m.displayLabel ? " — " + m.displayLabel : "");
  const bits = [];
  if (m.providerId) bits.push("provider " + m.providerId);
  if (m.profileId) bits.push("profile " + m.profileId);
  if (m.contextLimit != null) bits.push("ctx " + m.contextLimit);
  if (m.outputLimit != null) bits.push("out " + m.outputLimit);
  if (m.releaseDate) bits.push("rel " + m.releaseDate);
  if (m.cost && (m.cost.input != null || m.cost.output != null))
    bits.push(`cost ${m.cost.input ?? "?"}/${m.cost.output ?? "?"}${m.cost.cached != null ? "/" + m.cost.cached : ""} per MTok${m.cost.currency ? " " + m.cost.currency : ""}`);
  else if (m.cost) bits.push("cost " + JSON.stringify(m.cost).slice(0, 80));
  if (Array.isArray(m.variants) && m.variants.length) bits.push("effort: " + m.variants.join(","));
  let row = head;
  if (bits.length) row += "\n    " + bits.join(" · ");
  if (m.description) row += "\n    " + String(m.description).slice(0, 300);
  return row;
}

async function cmdModels() {
  await refreshModels();
  const meta = state.modelsMeta || {};
  const src = meta.source != null ? ` (source: ${typeof meta.source === "string" ? meta.source : JSON.stringify(meta.source)})` : "";
  sysLine(state.models.length
    ? `Models${src}:\n` + state.models.map(fmtModelRow).join("\n")
    : "(no models returned)");
}

function matchModel(want) {
  const w = want.toLowerCase();
  const rows = state.models;
  const byId = (m) => (m.modelId || "").toLowerCase();
  const byLabel = (m) => (m.displayLabel || "").toLowerCase();
  let hit = rows.find((m) => byId(m) === w || byLabel(m) === w);
  if (hit) return { hit };
  const pre = rows.filter((m) => byId(m).startsWith(w) || (byLabel(m) && byLabel(m).startsWith(w)));
  if (pre.length === 1) return { hit: pre[0] };
  if (pre.length > 1) return { ambiguous: pre };
  const sub = rows.filter((m) => byId(m).includes(w) || byLabel(m).includes(w));
  if (sub.length === 1) return { hit: sub[0] };
  if (sub.length > 1) return { ambiguous: sub };
  return {};
}

async function cmdSetModel(args) {
  if (!state.sessionId) return sysLine("No session — open one first.", true);
  const want = (args[0] || "").toLowerCase();
  if (!want) return sysLine("Usage: /model <id>  (see /models)", true);
  await refreshModels().catch(() => {});  // match against a fresh catalog
  const { hit, ambiguous } = matchModel(want);
  if (ambiguous) {
    return sysLine("Ambiguous — matches:\n" +
      ambiguous.map((m) => `  ${m.modelId}${m.displayLabel ? " (" + m.displayLabel + ")" : ""}`).join("\n") +
      "\nBe more specific.", true);
  }
  if (!hit) return sysLine("No model matches " + want + " (see /models)", true);
  try {
    const r = await send({ type: "setModel", sessionId: state.sessionId,
      model: { modelId: hit.modelId, providerId: hit.providerId || undefined } });
    state.pickedModel = { modelId: hit.modelId,
      providerId: hit.providerId || undefined };
    savePickedModel();
    sysLine("Model → " + hit.modelId + ` (${r.status || "accepted"})` +
      (state.running ? " — applies at the next model-call boundary." : "") +
      " — remembered for new chats.");
    refreshModels().catch(() => {});
  } catch (e) { sysLine("setModel failed: " + e.message, true); }
}

async function cmdCompact() {
  if (!state.sessionId) return sysLine("No session.", true);
  try {
    // session/compact -> {commandId, status: accepted|noop, reason?};
    // admission only — the compaction itself runs async.
    const r = await send({ type: "compact", sessionId: state.sessionId });
    sysLine(r.status === "noop"
      ? `Compact noop (${r.reason || "no_compactable_history"}).`
      : "Compaction admitted — running async.");
  } catch (e) { sysLine("compact failed: " + e.message, true); }
}

function fmtSessionUsage() {
  const lines = [];
  const c = state.lastCumulative;
  if (c) {
    lines.push(`Session cumulative: ${c.totalTokens || 0} tok ` +
      `(${c.promptTokens || 0} in / ${c.outputTokens || 0} out` +
      (c.cacheReadTokens ? `, cache ${c.cacheReadTokens}r/${c.cacheWriteTokens || 0}w` : "") +
      (c.cost && c.cost.usd != null
        ? `, $${Number(c.cost.usd).toFixed(4)}${c.cost.partial ? " (partial)" : ""}` : "") +
      ")");
  } else {
    lines.push("Session cumulative: (no tokenUsage observed yet for this session)");
  }
  const x = state.lastContext;
  if (x) {
    const pct = Math.round((x.usedTokens / x.windowTokens) * 100);
    lines.push(`Context window: ${x.usedTokens}/${x.windowTokens} (${pct}%, pressure ${x.pressure || "?"})`);
  } else {
    lines.push("Context window: (no contextUsage observed yet)");
  }
  return lines.join("\n");
}

async function cmdUsage() {
  try {
    const r = await send({ type: "usage" });
    sysLine(fmtUsageLong(r.usage) + "\n" + fmtSessionUsage());
    refreshUsage().catch(() => {});
  } catch (e) { sysLine("usage failed: " + e.message, true); }
}

async function cmdMcp() {
  try {
    // MSP v1 has no mcp/* methods: the bridge serves settings.json instead.
    const r = await send({ type: "mcp" });
    const attached = state.sessionMcp && state.sessionMcp.length
      ? "\nAttached to this session: " + state.sessionMcp.join(", ") +
        " (no wire read-back exists — tracked from creation)"
      : "";
    if (r.servers && r.servers.length) {
      sysLine("MCP servers (" + r.source + "):\n" + r.servers.map((s) =>
        `  ${s.name}  [${s.transport || "?"}]` +
        (s.url ? " " + s.url : "") + (s.command ? " cmd:" + s.command : "") +
        (s.hasHeaders ? " +headers" : "") + (s.hasEnv ? " +env" : "")).join("\n") +
        attached +
        "\nAttach at creation: /new [name] --mcp a,b" +
        "\nOAuth: run `muse mcp login <server>` in a terminal (no OAuth on the wire).");
    } else {
      sysLine("No MCP servers configured (" + (r.source || "?") + ").\n" + (r.hint || "") + attached);
    }
  } catch (e) { sysLine("mcp failed: " + e.message, true); }
}

async function cmdSkills() {
  if (!state.sessionId) return sysLine("No session — open one first.", true);
  try {
    const r = await send({ type: "skills", sessionId: state.sessionId });
    const rows = r.skills || [];
    sysLine(rows.length
      ? "Skills (invoke with /<selector> as a prompt skill part):\n" + rows.map((s) =>
        `  /${s.selector}${s.argumentHint ? " " + s.argumentHint : ""} — ${s.displayName || s.selector}` +
        (s.description ? `\n    ${s.description}` : "") +
        (s.source ? ` [${s.source}${s.pluginId ? ":" + s.pluginId : ""}]` : "")).join("\n")
      : "(no skills for this session)");
  } catch (e) { sysLine("skills failed: " + e.message, true); }
}

/* ---------- github repos (gh-only v1) ---------- */
// Clone-then-open records the dest in the first-use allow list without a
// prompt: picking the repo (button or explicit /github open) is the
// consent, equivalent to the manual-root confirm.
function markRootAllowed(path) {
  const norm = normalizeRoot(path);
  if (!norm) return;
  const known = confirmedRoots();
  if (!known.includes(norm)) {
    known.push(norm);
    try { localStorage.setItem(ALLOWED_ROOTS_KEY, JSON.stringify(known)); } catch (_) {}
  }
}

function ghStatus(text, isErr) {
  // The sidebar status line is gone (composer pills now): surface clone
  // progress on the repo pill while a composer clone runs, and stay quiet
  // otherwise — terminal results already go through sysLine.
  if (!state.repoBusy) return;
  const lbl = el("repo-pill-label");
  if (lbl) lbl.textContent = text || "cloning…";
  if (isErr) toast(text || "clone failed", true);
}

function fmtRepoRow(r) {
  const bits = [(r.defaultBranch || ""), ((r.updatedAt || "").slice(0, 10))].filter(Boolean).join(" · ");
  return `  ${r.fullName}${r.private ? " [private]" : ""}${bits ? "  (" + bits + ")" : ""}`;
}

function newCloneOpId() {
  return "gh-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e6).toString(36);
}

async function cmdGithub(args) {
  const sub = (args[0] || "list").toLowerCase();
  if (sub === "list") return cmdGithubList(args.slice(1).join(" "));
  if (sub === "clone") return githubCloneRepo(args[1]);
  if (sub === "open") return githubOpenRepo(args[1], args.slice(2).join(" ") || undefined);
  if (sub === "clean") return cmdGithubClean(args.slice(1));
  if (sub === "cancel") return cmdGithubCancel();
  return sysLine("Usage: /github list [search] | clone <owner/repo> | open <owner/repo> [name] | clean [id-prefix] | cancel", true);
}

async function cmdGithubList(search) {
  try {
    const r = await send({ type: "githubRepos", search: search || undefined });
    state.githubCache = r.repos || [];
    sysLine(state.githubCache.length
      ? `GitHub repos:\n${state.githubCache.map(fmtRepoRow).join("\n")}\nClone: /github clone <owner/repo> · Open: /github open <owner/repo>`
      : "(no repos returned)");
  } catch (e) { sysLine("github list failed: " + e.message, true); }
}

async function githubCloneRepo(fullName) {
  if (!fullName) return sysLine("Usage: /github clone <owner/repo>", true);
  const opId = newCloneOpId();
  state.githubOp = opId;
  state.githubPending.set(opId, { kind: "clone", fullName });
  ghStatus(`cloning ${fullName}…`);
  try {
    // Admitted instantly; the outcome arrives as a githubCloneResult
    // event, so the connection stays responsive (and cancellable).
    await send({ type: "githubClone", fullName, opId });
  } catch (e) {
    state.githubPending.delete(opId);
    state.githubOp = null;
    ghStatus("clone failed: " + e.message, true);
    sysLine("github clone failed: " + e.message, true);
  }
}

async function githubOpenRepo(fullName, opts) {
  // opts: legacy name string, or {name, branch} from the composer pills.
  const o = (opts && typeof opts === "object") ? opts : { name: opts };
  if (!fullName) return sysLine("Usage: /github open <owner/repo> [name]", true);
  const opId = newCloneOpId();
  state.githubOp = opId;
  state.githubPending.set(opId, { kind: "open", fullName, name: o.name });
  ghStatus(`cloning ${fullName}…`);
  try {
    // Admitted instantly; the outcome arrives as a githubCloneResult event.
    const req = { type: "githubOpen", fullName, name: o.name, opId };
    if (o.branch) req.branch = o.branch;
    if (state.pickedModel) req.model = state.pickedModel;
    if (state.pickedEffort) req.reasoningEffort = state.pickedEffort;
    if (state.pickedApprovalMode) req.approvalMode = state.pickedApprovalMode;
    await send(req);
    return opId;
  } catch (e) {
    state.githubPending.delete(opId);
    state.githubOp = null;
    ghStatus("open failed: " + e.message, true);
    sysLine("github open failed: " + e.message, true);
    return null;
  }
}

async function onGithubResult(p) {
  // Terminal event for an admitted clone/open (global, matched by opId).
  const pend = state.githubPending.get(p.opId);
  state.githubPending.delete(p.opId);
  if (state.githubOp === p.opId) state.githubOp = null;
  const staged = state.stagedPrompt && pend && pend.kind === "open" &&
    (pend.fullName || (p.result || {}).fullName) === state.stagedRepo;
  if (!p.ok) {
    const err = p.error || {};
    ghStatus("failed: " + (err.message || err.code || "unknown"), true);
    sysLine(`github ${pend ? pend.kind : "clone"} failed` +
      (err.code ? ` [${err.code}]` : "") + ": " + (err.message || "unknown"), true);
    if (staged) {
      // The first message was never sent: hand it back so it isn't lost.
      el("input").value = state.stagedPrompt; autosize();
      state.stagedPrompt = null; state.stagedRepo = null;
    }
    if (state.repoBusy === p.opId) state.repoBusy = null;
    updateRepoBar();
    return;
  }
  const r = p.result || {};
  if (state.repoBusy === p.opId) state.repoBusy = null;
  ghStatus("");
  if (pend && pend.kind === "open") {
    const sid = r.session && r.session.sessionId;
    markRootAllowed(r.workspaceRoot || "");
    sysLine(`Opened ${r.fullName} → session ${shortId(sid)} (${r.workspaceRoot || "?"})`);
    if (sid) await openSession(sid);
    refreshSessions().catch(() => {});
    if (staged) {
      // Composer repo flow: the session now exists rooted at the clone —
      // send the staged first message into it.
      const t = state.stagedPrompt;
      state.stagedPrompt = null; state.stagedRepo = null;
      state.pendingRepo = null;
      updateRepoBar();
      await sendPromptText(t, true);
      return;
    }
  } else {
    sysLine(`Cloned ${r.fullName} → ${r.dest}\nRoot a session there: /new --path ${r.dest}`);
  }
  updateRepoBar();
}

async function cmdGithubClean(args) {
  const prefix = (args[0] || "").toLowerCase();
  let sid = state.sessionId;
  if (prefix) {
    const hit = state.sessionsCache.find((s) => s.sessionId.toLowerCase().startsWith(prefix));
    // A clone-only sid has no MSP session yet: accept a full id verbatim.
    sid = hit ? hit.sessionId : (prefix.length >= 8 ? args[0] : null);
  }
  if (!sid) return sysLine("Usage: /github clean <id-prefix> (or open a session)", true);
  if (!confirm(`Delete the GitHub clone for session ${shortId(sid)}? (session kept, files removed)`)) return;
  try {
    const r = await send({ type: "githubClean", sessionId: sid });
    sysLine(r.removed ? `Clone removed (${r.dest}).` : `Nothing to remove (${r.dest}).`);
  } catch (e) { sysLine("github clean failed: " + e.message, true); }
}

async function cmdGithubCancel() {
  if (!state.githubOp) return sysLine("No clone running.", true);
  try {
    await send({ type: "githubCancel", opId: state.githubOp });
    sysLine("Clone cancel requested.");
  } catch (e) { sysLine("github cancel failed: " + e.message, true); }
}

function onGithubProgress(p) {
  // Progress events carry no sessionId (global) so they always render.
  const line = (p.line || "").slice(0, 160);
  if (p.phase === "progress") { ghStatus(`${p.fullName || ""}: ${line}`); return; }
  const done = p.phase === "completed";
  ghStatus(done ? `${p.fullName || ""}: cloned` : `${p.fullName || ""}: ${p.phase}`,
    !(done || p.phase === "started"));
}

/* ---------- composer repo/branch pills (pre-session attach) ---------- */
// The pills show only while no session is open. Picking a repo reveals the
// branch pill (default = repo default branch); the first sent message then
// clones that branch and roots the new session at the clone.
function updateRepoBar() {
  const bar = el("repo-bar");
  if (!bar) return;
  bar.hidden = !!state.sessionId;
  if (bar.hidden) { closeRepoMenus(); return; }
  const sel = state.pendingRepo;
  const busy = !!state.repoBusy;
  const pill = el("repo-pill");
  pill.disabled = busy;
  pill.classList.toggle("picked", !!sel);
  const lbl = el("repo-pill-label");
  if (!busy) lbl.textContent = sel ? sel.fullName : "select a repo";
  const bw = el("branch-wrap");
  bw.hidden = !sel;
  if (sel) {
    const bp = el("branch-pill");
    bp.disabled = busy;
    bp.classList.toggle("picked", !!sel.branch);
    if (!busy) el("branch-pill-label").textContent = sel.branch || "default branch";
    bp.title = sel.branch ? `Branch ${sel.branch} (click to change)`
      : "Default branch (click to pick one)";
  }
}

function closeRepoMenus() {
  const rm = el("repo-menu"), bm = el("branch-menu");
  if (rm) rm.hidden = true;
  if (bm) bm.hidden = true;
}

function repoOptRow(label, sub) {
  const b = document.createElement("button");
  b.className = "repo-opt";
  b.setAttribute("role", "option");
  b.textContent = label;
  if (sub) {
    const s = document.createElement("span");
    s.className = "sub"; s.textContent = sub;
    b.append(s);
  }
  return b;
}

function repoDimRow(label) {
  const d = document.createElement("div");
  d.className = "repo-opt dim"; d.textContent = label;
  return d;
}

async function toggleRepoMenu() {
  const m = el("repo-menu");
  if (!m.hidden) { m.hidden = true; return; }
  closeRepoMenus();
  m.innerHTML = "";
  m.append(repoDimRow("loading…"));
  m.hidden = false;
  try {
    const r = await send({ type: "githubRepos" });
    state.githubCache = r.repos || [];
  } catch (e) {
    m.innerHTML = "";
    m.append(repoDimRow("list failed: " + e.message));
    return;
  }
  m.innerHTML = "";
  if (!state.githubCache.length) {
    m.append(repoDimRow("No repos — is `gh auth login` done?"));
    return;
  }
  for (const r of state.githubCache) {
    const b = repoOptRow(r.fullName,
      r.private ? "private" : (r.defaultBranch || ""));
    b.title = "Attach to the next session";
    b.onclick = () => selectRepo(r.fullName, r.defaultBranch || null);
    m.append(b);
  }
}

function selectRepo(fullName, defaultBranch) {
  state.pendingRepo = {
    fullName, branch: defaultBranch || null,
    defaultBranch: defaultBranch || null,
  };
  closeRepoMenus();
  updateRepoBar();
  toggleBranchMenu(true);
  el("input").focus();
}

async function toggleBranchMenu(onlyOpen) {
  const sel = state.pendingRepo;
  if (!sel) return;
  const m = el("branch-menu");
  if (!m.hidden) { if (!onlyOpen) m.hidden = true; return; }
  closeRepoMenus();
  m.innerHTML = "";
  m.append(repoDimRow("loading…"));
  m.hidden = false;
  let branches = [];
  try {
    const r = await send({ type: "githubBranches", fullName: sel.fullName });
    branches = r.branches || [];
  } catch (e) {
    m.innerHTML = "";
    m.append(repoDimRow("branches failed: " + e.message));
    return;
  }
  m.innerHTML = "";
  const current = sel.branch || sel.defaultBranch;
  const names = branches.slice();
  if (current && !names.includes(current)) names.unshift(current);
  if (!names.length) {
    m.append(repoDimRow("(no branches — clones the default)"));
    return;
  }
  for (const name of names) {
    const b = repoOptRow(name, name === current ? "selected" : "");
    b.onclick = () => selectBranch(name);
    m.append(b);
  }
}

function selectBranch(name) {
  if (state.pendingRepo) state.pendingRepo.branch = name;
  closeRepoMenus();
  updateRepoBar();
  el("input").focus();
}

async function submitWithRepo(text) {
  // First message with a repo attached: clone (branch) + root the new
  // session at the clone, then send the staged message into it. The
  // result event (onGithubResult) finishes the job.
  const sel = state.pendingRepo;
  const box = el("input");
  box.value = ""; autosize();
  state.history.unshift(text); state.hidx = -1;
  // Echo now (as usual); the turn itself starts once the session exists.
  renderItem({ itemId: "local-" + Date.now(), kind: "userMessage", text }, false);
  state.stagedPrompt = text;
  state.stagedRepo = sel.fullName;
  sysLine(`Cloning ${sel.fullName}${sel.branch ? " (" + sel.branch + ")" : ""} — the session starts rooted there.`);
  const opId = await githubOpenRepo(sel.fullName, { branch: sel.branch });
  if (!opId) {
    // Admit failed synchronously: nothing is coming, hand the text back.
    box.value = text; autosize();
    state.stagedPrompt = null; state.stagedRepo = null;
    return;
  }
  state.repoBusy = opId;
  el("repo-pill-label").textContent = `cloning ${sel.fullName}…`;
  updateRepoBar();
}

const EFFORTS = EFFORT_TIERS;

async function cmdSetEffort(args) {
  if (!state.sessionId) return sysLine("No session — open one first.", true);
  const want = (args[0] || "").toLowerCase();
  if (!EFFORTS.includes(want)) {
    return sysLine("Usage: /effort <tier>  tiers: " + EFFORTS.join(" | "), true);
  }
  try {
    await send({ type: "setEffort", sessionId: state.sessionId, reasoningEffort: want });
    // Remembered as the default for created chats, not just this one
    // (parity with /model).
    state.pickedEffort = want;
    savePickedEffort();
    syncEffortPicker();
    sysLine("Reasoning effort (session default) → " + want + " — remembered for new chats.");
  } catch (e) { sysLine("setEffort failed: " + e.message, true); }
}

async function cmdDefaultEffort(args) {
  const want = (args[0] || "").toLowerCase();
  if (!want || want === "show") {
    sysLine(state.pickedEffort
      ? `Default reasoning effort: ${state.pickedEffort} (applies to new chats).`
      : "No default reasoning effort — new chats use the host default. " +
        "Set one: /default-effort <tier>  tiers: " + EFFORTS.join(" | "));
    return;
  }
  if (want === "clear") {
    state.pickedEffort = null;
    savePickedEffort();
    syncEffortPicker();
    sysLine("Default reasoning effort cleared — new chats use the host default.");
    return;
  }
  if (!EFFORTS.includes(want)) {
    return sysLine("Usage: /default-effort <tier|clear|show>  tiers: " + EFFORTS.join(" | "), true);
  }
  state.pickedEffort = want;
  savePickedEffort();
  syncEffortPicker();
  if (!state.sessionId) {
    sysLine(`Default reasoning effort → ${want} (applies to new chats).`);
    return;
  }
  try {
    await send({ type: "setEffort", sessionId: state.sessionId, reasoningEffort: want });
    sysLine(`Reasoning effort (session default) → ${want} — remembered for new chats.`);
  } catch (e) { sysLine("setEffort failed: " + e.message, true); }
}

async function cmdOutput(args) {
  if (!state.sessionId) return sysLine("No session.", true);
  const prefix = (args[0] || "").toLowerCase();
  if (!prefix) return sysLine("Usage: /output <itemId-prefix>  (item must carry an outputRef)", true);
  const hit = [...state.items.values()].find((rec) =>
    rec.item && rec.item.itemId && rec.item.itemId.toLowerCase().startsWith(prefix));
  if (!hit) return sysLine("No loaded item matches " + prefix, true);
  const ref = hit.item.outputRef && hit.item.outputRef.id;
  if (!ref) return sysLine("Item " + shortId(hit.item.itemId) + " has no stored outputRef.", true);
  try {
    const r = await send({ type: "readOutput", sessionId: state.sessionId,
      itemId: hit.item.itemId, outputRef: ref, offsetBytes: 0 });
    sysLine(`Full output [${r.mediaType || "?"} · bytes ${r.offsetBytes}-${r.offsetBytes + r.byteLen}${r.eof ? " EOF" : ""}]:\n` +
      String(r.content || "").slice(0, 4000));
  } catch (e) { sysLine("output failed: " + e.message, true); }
}

async function cmdInterrupt() {
  if (!state.sessionId) return toast("no session", true);
  try {
    await send({ type: "interrupt", sessionId: state.sessionId,
      turnId: state.turnId || undefined, retract: false });
  } catch (e) { toast("interrupt failed: " + e.message, true); }
}

async function cmdCancel() {
  if (!state.sessionId) return toast("no session", true);
  try {
    await send({ type: "cancel", sessionId: state.sessionId,
      turnId: state.turnId || undefined });
  } catch (e) { toast("cancel failed: " + e.message, true); }
}

async function cmdUnqueue() {
  if (!state.sessionId) return sysLine("No session.", true);
  try {
    await send({ type: "unqueue", sessionId: state.sessionId,
      turnId: state.queuedTurnId || undefined });
    state.queuedTurnId = null;
    sysLine("Unqueued.");
  } catch (e) { sysLine("unqueue failed: " + e.message, true); }
}

async function cmdSteer(raw) {
  const text = raw.replace(/^\/steer\s*/, "").trim();
  if (!state.sessionId) return sysLine("No session.", true);
  if (!text) return sysLine("Usage: /steer <text>", true);
  // turn/steer requires expectedTurnId; the bridge also tracks it server-side.
  try {
    await send({ type: "steer", sessionId: state.sessionId, text,
      expectedTurnId: state.turnId || undefined });
    sysLine("Steered turn.");
  } catch (e) { sysLine("steer failed: " + e.message, true); }
}

async function dispatchSlash(text) {
  const raw = text.trim();
  const parts = raw.slice(1).split(/\s+/);
  const name = (parts[0] || "").toLowerCase();
  const cmd = SLASH.find((c) => c.name === name);
  if (!cmd) {
    sysLine(`Unknown command /${name} — /help lists commands.`, true);
    return;
  }
  // Echo the command TUI-style, then run.
  renderItem({ itemId: "local-" + Date.now(), kind: "userMessage", text: raw }, false);
  try { await cmd.run(parts.slice(1), raw); }
  catch (e) { sysLine("/" + name + " failed: " + e.message, true); }
}

/* slash autocomplete popup */
function updateSlashPopup() {
  const box = el("input");
  const pop = el("slash-popup");
  const v = box.value;
  if (!v.startsWith("/") || v.includes("\n")) { pop.hidden = true; state.slashList = []; return; }
  const q = v.slice(1).split(/\s+/)[0].toLowerCase();
  const list = SLASH.filter((c) => c.name.startsWith(q));
  state.slashList = list;
  if (!list.length) { pop.hidden = true; return; }
  state.slashSel = Math.min(state.slashSel, list.length - 1);
  pop.innerHTML = "";
  // Pad every usage to the longest one shown so all descriptions start
  // in the same column (.cmd keeps the spaces with white-space: pre).
  const width = Math.max(...list.map((c) => c.usage.length));
  list.forEach((c, i) => {
    const d = document.createElement("div");
    d.className = "slash-item" + (i === state.slashSel ? " sel" : "");
    const cmd = document.createElement("span");
    cmd.className = "cmd"; cmd.textContent = c.usage.padEnd(width);
    const desc = document.createElement("span");
    desc.className = "desc"; desc.textContent = c.desc;
    d.append(cmd, desc);
    d.onclick = () => { applySlash(i); };
    pop.append(d);
  });
  pop.hidden = false;
  const sel = pop.children[state.slashSel];
  if (sel && typeof sel.scrollIntoView === "function") sel.scrollIntoView({ block: "nearest" });
}
function applySlash(i) {
  const c = state.slashList[i != null ? i : state.slashSel];
  if (!c) return;
  const box = el("input");
  const rest = box.value.slice(1).split(/\s+/).slice(1).join(" ");
  box.value = "/" + c.name + (rest ? " " + rest : c.usage.includes(" ") ? " " : "");
  box.focus();
  el("slash-popup").hidden = true;
  autosize();
}

/* ---------- composer ---------- */
function autosize() {
  const box = el("input");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, window.innerHeight * 0.3) + "px";
}

async function submitComposer() {
  const box = el("input");
  const text = box.value.trim();
  if (!text) return;
  stopStarsFxNow();
  if (!state.ws || state.ws.readyState !== 1) { toast("not connected", true); return; }
  el("slash-popup").hidden = true;
  closeRepoMenus();
  if (text.startsWith("/")) {
    box.value = ""; autosize();
    state.history.unshift(text); state.hidx = -1;
    await dispatchSlash(text);
    return;
  }
  if (!state.sessionId && state.pendingRepo && !state.repoBusy) {
    await submitWithRepo(text);
    return;
  }
  box.value = ""; autosize();
  await sendPromptText(text);
}

async function sendPromptText(text, alreadyEchoed) {
  if (!alreadyEchoed) {
    state.history.unshift(text); state.hidx = -1;
    // Optimistic user echo (reconciled when the server item arrives).
    renderItem({ itemId: "local-" + Date.now(), kind: "userMessage", text }, false);
  }
  try {
    const req = { type: "prompt", sessionId: state.sessionId || undefined, text };
    // Lazily created session: carry the remembered default model.
    if (!state.sessionId && state.pickedModel) {
      req.modelId = state.pickedModel.modelId;
      if (state.pickedModel.providerId) req.providerId = state.pickedModel.providerId;
    }
    if (!state.sessionId && state.pickedEffort) req.reasoningEffort = state.pickedEffort;
    if (!state.sessionId && state.pickedApprovalMode) req.approvalMode = state.pickedApprovalMode;
    const r = await send(req);
    if (r.sessionId && !state.sessionId) {
      state.sessionId = r.sessionId;
      updateRepoBar();
      el("session-title").textContent = titleForSession(r.sessionId);
      await send({ type: "subscribe", sessionId: r.sessionId }).catch(() => {});
      refreshSessions().catch(() => {});
      refreshModels().catch(() => {});
    }
    // The host queues follow-ups behind a running turn by default
    // (ifBusy omitted): say so, or the message looks lost until it
    // fires later — which reads as "sent spontaneously".
    if (r.disposition === "queued") {
      state.queuedTurnId = r.turnId || null;
      sysLine("queued behind the running turn — runs when it finishes (/unqueue reclaims it).");
    } else {
      state.queuedTurnId = null;
    }
    state.running = true; updateRunChip();
  } catch (e) {
    sysLine("send failed: " + e.message, true);
    setStatus("send failed: " + e.message);
  }
}

function composerHistory(dir) {
  // dir: +1 older (Up), -1 newer (Down)
  if (!state.history.length) return;
  const box = el("input");
  const singleLine = !box.value.includes("\n");
  if (!singleLine) return;
  const next = state.hidx + dir;
  if (next < -1 || next >= state.history.length) return;
  state.hidx = next;
  box.value = next === -1 ? "" : state.history[next];
  autosize();
}

/* ---------- inspector ---------- */
function openInspectorOnMobile(tab) {
  if (window.innerWidth >= 900) {
    if (tab) selectTab(tab);
    return;
  }
  // Mobile: flash the inspector open only for approvals so the card is seen.
  if (tab === "approvals" && !el("inspector").classList.contains("open")) {
    el("inspector").classList.add("open");
    selectTab("approvals");
    setTimeout(() => {
      if (window.innerWidth < 900) el("inspector").classList.remove("open");
    }, 100);
  }
}
function selectTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  for (const n of ["approvals", "tools", "session"]) {
    el("tab-" + n).hidden = n !== name;
  }
}

/* ---------- wiring ---------- */
el("stars-toggle").addEventListener("click", () => { toggleStarsFx(); });
el("composer").addEventListener("submit", (ev) => { ev.preventDefault(); submitComposer(); });
el("input").addEventListener("input", () => { autosize(); state.slashSel = 0; updateSlashPopup(); });
el("input").addEventListener("keydown", (ev) => {
  const popOpen = !el("slash-popup").hidden && state.slashList.length;
  if (popOpen && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) {
    ev.preventDefault();
    const d = ev.key === "ArrowDown" ? 1 : -1;
    state.slashSel = (state.slashSel + d + state.slashList.length) % state.slashList.length;
    updateSlashPopup();
    return;
  }
  if (popOpen && (ev.key === "Tab" || ev.key === "Enter")) {
    // Tab always completes; Enter completes only when the text is still a bare prefix.
    if (ev.key === "Tab" || /^\/\S*$/.test(el("input").value)) {
      ev.preventDefault(); applySlash(); return;
    }
  }
  if (ev.key === "Escape") { el("slash-popup").hidden = true; closeRepoMenus(); const om = document.querySelector(".row-menu:not([hidden])"); if (om) { const op = om._opener; closeRowMenus(); if (op) op.focus(); return; } if (state.running) cmdCancel(); return; }
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); el("composer").requestSubmit(); return; }
  if (ev.key === "ArrowUp" && (el("input").selectionStart === 0 || !el("input").value)) {
    // Only hijack Up at top-of-input for history.
    if (!el("input").value.includes("\n") || el("input").selectionStart === 0) {
      ev.preventDefault(); composerHistory(1); return;
    }
  }
  if (ev.key === "ArrowDown" && state.hidx >= 0) {
    ev.preventDefault(); composerHistory(-1); return;
  }
});
el("btn-stop").onclick = () => cmdInterrupt();
el("btn-send").onclick = null; // submit via form
el("btn-sessions").onclick = toggleSessions;
document.querySelectorAll("#tx-filters button").forEach((b) => {
  b.onclick = () => setTranscriptFilter(b.dataset.txf || "all");
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    // Turn canceling on Escape anywhere outside the composer: the
    // directory dialog owns Escape while open, and the composer input
    // already cancelled above (skip its bubbled copy to avoid double).
    if (!el("dir-dialog").hidden) return;
    if (ev.target === el("input")) return;
    const openMenu = document.querySelector(".row-menu:not([hidden])");
    if (openMenu) { const op = openMenu._opener; closeRowMenus(); if (op) op.focus(); return; }
    if (ev.target === el("session-filter")) {
      if (el("session-filter").value.trim() !== "") {
        el("session-filter").value = "";
        syncClearBtn();
        renderSessionList(state.sessionsCache);
      } else {
        document.querySelector(".filter-box").hidden = true;
        el("btn-search-sessions").setAttribute("aria-expanded", "false");
        el("btn-search-sessions").focus();
      }
      return;
    }
    if (state.running) cmdCancel();
    return;
  }
  if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && !ev.shiftKey) {
    const k = (ev.key || "").toLowerCase();
    if (k === "b") {
      ev.preventDefault();
      toggleSessions();
    } else if (k === ".") {
      ev.preventDefault();
      toggleInspector();
    }
  }
});
el("btn-close-sessions").onclick = closeDrawer;
el("scrim").onclick = () => { closeDrawer(); closeInspector(); };
/* ---------- new-session directory explorer ---------- */
// Filesystem explorer for the + button: lists server-side directories via
// the bridge `browse` method. Current directory is the selection; files
// are shown greyed-out and unselectable. Pick-existing-only.
const dirState = { path: "", parent: "/", home: "" };
function dirError(msg) {
  const e = el("dir-error");
  if (!msg) { e.hidden = true; e.textContent = ""; return; }
  e.hidden = false; e.textContent = msg;
}
function openDirDialog() {
  el("dir-dialog").hidden = false;
  dirError("");
  el("dir-list").innerHTML = "";
  el("dir-path").value = "";
  loadDir("");
  setTimeout(() => { try { el("dir-path").focus(); } catch (_) {} }, 0);
}
function closeDirDialog() { el("dir-dialog").hidden = true; }
async function loadDir(path) {
  dirError("");
  try {
    const r = await send(path ? { type: "browse", path } : { type: "browse" });
    renderDir(r);
  } catch (e) { dirError(e.message); }
}
function renderCrumbs(path) {
  const box = el("dir-crumbs");
  box.innerHTML = "";
  const parts = String(path || "/").split("/").filter(Boolean);
  const mk = (label, target, isCur) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (isCur) b.className = "cur";
    else b.onclick = () => loadDir(target);
    return b;
  };
  const sep = () => {
    const s = document.createElement("span");
    s.className = "sep"; s.textContent = "/";
    return s;
  };
  box.append(mk("/", "/", parts.length === 0));
  let acc = "";
  parts.forEach((p, i) => {
    acc += "/" + p;
    box.append(sep());
    box.append(mk(p, acc, i === parts.length - 1));
  });
}
function renderDir(r) {
  dirState.path = r.path; dirState.parent = r.parent; dirState.home = r.home || "";
  el("dir-path").value = r.path;
  renderCrumbs(r.path);
  const box = el("dir-list");
  box.innerHTML = "";
  if (!r.entries.length) {
    const d = document.createElement("div");
    d.className = "dir-empty"; d.textContent = "Empty directory.";
    box.append(d);
    return;
  }
  for (const e of r.entries) {
    const b = document.createElement("button");
    b.className = "dir-row " + (e.isDir ? "isdir" : "isfile");
    b.setAttribute("role", "option");
    b.textContent = e.isDir ? e.name + "/" : e.name;
    b.title = e.path;
    if (e.isDir) b.onclick = () => loadDir(e.path);
    else b.disabled = true;
    box.append(b);
  }
}
el("btn-new").onclick = () => { openDirDialog(); };
el("btn-search-sessions").onclick = () => {
  const box = document.querySelector(".filter-box");
  const show = box.hidden;
  box.hidden = !show;
  el("btn-search-sessions").setAttribute("aria-expanded", String(show));
  if (show) {
    el("session-filter").focus();
  } else {
    el("session-filter").value = "";
    syncClearBtn();
    renderSessionList(state.sessionsCache);
  }
};
el("dir-go").onclick = () => loadDir(el("dir-path").value.trim());
el("dir-path").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); loadDir(el("dir-path").value.trim()); }
  else if (ev.key === "Escape") closeDirDialog();
});
el("dir-up").onclick = () => { if (dirState.path && dirState.path !== dirState.parent) loadDir(dirState.parent); };
el("dir-home").onclick = () => loadDir(dirState.home || "");
el("dir-root").onclick = () => loadDir("/");
el("dir-cancel").onclick = closeDirDialog;
el("dir-cancel-x").onclick = closeDirDialog;
el("dir-dialog").addEventListener("click", (ev) => {
  if (ev.target === el("dir-dialog")) closeDirDialog();
});
el("dir-default").onclick = () => { closeDirDialog(); newSession(); closeDrawer(); };
el("dir-use").onclick = () => {
  const p = (dirState.path || el("dir-path").value || "").trim();
  if (!p) { dirError("no directory selected"); return; }
  closeDirDialog();
  newSession(undefined, { workspaceRoot: p });
  closeDrawer();
};
el("btn-refresh-sessions").onclick = async () => {
  if (state.listBusy) return;
  state.listBusy = true;
  const btn = el("btn-refresh-sessions");
  // Restart the one-shot 360° on every press, even mid-tail of a prior spin.
  btn.classList.remove("spin");
  void btn.offsetWidth;
  btn.classList.add("spin");
  btn.setAttribute("aria-busy", "true");
  btn.setAttribute("aria-disabled", "true");
  try {
    await refreshSessions();
    toast("sessions refreshed");
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.listBusy = false;
    btn.removeAttribute("aria-busy");
    btn.removeAttribute("aria-disabled");
    // The refresh usually beats the 650ms spin: keep the class until the
    // one-shot finishes, otherwise add+remove land before paint and the
    // animation never starts.
    setTimeout(() => btn.classList.remove("spin"), 650);
  }
};
el("session-filter").oninput = () => {
  const rendered = closeRowMenus();
  syncClearBtn();
  if (!rendered) renderSessionList(state.sessionsCache);
};
el("btn-clear-filter").onclick = () => {
  el("session-filter").value = "";
  const rendered = closeRowMenus();
  syncClearBtn();
  if (!rendered) renderSessionList(state.sessionsCache);
  el("session-filter").focus();
};
el("session-filter").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowDown") return;
  const first = el("session-list").querySelector(".sess-open");
  if (first) { e.preventDefault(); first.focus(); }
});
// Arrow-key movement between rows (all row buttons stay tabbable).
el("session-list").addEventListener("keydown", (e) => {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const btns = [...el("session-list").querySelectorAll(".sess-open, .config-btn")];
  const i = btns.indexOf(document.activeElement);
  if (i === -1) return;
  if (e.key === "ArrowUp" && i === 0) {
    e.preventDefault();
    el("session-filter").focus();
    return;
  }
  let n = i;
  if (e.key === "ArrowDown") n = Math.min(i + 1, btns.length - 1);
  else if (e.key === "ArrowUp") n = Math.max(i - 1, 0);
  else if (e.key === "Home") n = 0;
  else n = btns.length - 1;
  e.preventDefault();
  btns[n].focus();
});
el("repo-pill").onclick = (e) => { e.stopPropagation(); if (!state.repoBusy) toggleRepoMenu(); };
el("branch-pill").onclick = (e) => { e.stopPropagation(); if (!state.repoBusy) toggleBranchMenu(); };
el("btn-older").onclick = loadOlder;
el("jump-latest").onclick = () => { state.stick = true; scrollDown(true); };
el("terminal").addEventListener("scroll", () => {
  const t = el("terminal");
  state.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  updateJumpBtn();
});
el("effort-picker").onchange = (ev) => {
  const v = ev.target.value || "";
  if (!v) {
    // Placeholder (effort…): clear the remembered default.
    state.pickedEffort = null;
    savePickedEffort();
    syncEffortPicker();
    toast("default effort cleared (new chats use the host default)");
    return;
  }
  // Remembered as the default for created chats, not just this one.
  state.pickedEffort = v;
  savePickedEffort();
  syncEffortPicker();
  if (!state.sessionId) {
    toast("default effort → " + v + " (applies to new chats)");
    return;
  }
  send({ type: "setEffort", sessionId: state.sessionId, reasoningEffort: v })
    .then(() => toast("effort → " + v))
    .catch((e) => toast("setEffort failed: " + e.message, true));
};
el("model-picker").onchange = (ev) => {
  const o = ev.target.selectedOptions[0];
  if (!o || !o.value) return;
  // Remembered as the default for created chats, not just this one.
  state.pickedModel = { modelId: o.value,
    providerId: o.dataset.provider || undefined };
  savePickedModel();
  if (!state.sessionId) {
    toast("default model → " + o.value + " (applies to new chats)");
    return;
  }
  send({ type: "setModel", sessionId: state.sessionId,
    model: { modelId: o.value, providerId: o.dataset.provider || undefined } })
    .then(() => toast("model → " + o.value))
    .catch((e) => toast("setModel failed: " + e.message, true));
};
el("btn-inspector").onclick = toggleInspector;
el("btn-close-inspector").onclick = closeInspector;
document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => selectTab(t.dataset.tab); });
el("btn-compact").onclick = () => cmdCompact();
el("btn-usage").onclick = () => cmdUsage();
el("btn-pending").onclick = () => fetchPending().then(() => toast("pending refreshed"));
function syncApprovalWarn() {
  // The confirm panel opens only for a not-yet-confirmed allowAll pick:
  // once enabled (prev === allowAll) it stays closed until re-selected.
  const sel = el("approval-mode");
  el("approval-warn").hidden =
    sel.value !== "allowAll" || sel.dataset.prev === "allowAll";
}
function setApprovalDefault(mode) {
  // Remember the pick for created sessions (parity with pickedModel).
  state.pickedApprovalMode = mode;
  savePickedApprovalMode();
  el("approval-mode").dataset.prev = mode;
}
el("approval-mode").onchange = (ev) => {
  const mode = ev.target.value;
  if (!state.sessionId) {
    // No session loaded: the pick becomes the default for new sessions
    // (allowAll still confirms first via the panel below).
    if (mode !== "allowAll") {
      setApprovalDefault(mode);
      toast("default approval mode → " + mode + " (new sessions)");
    }
    syncApprovalWarn();
    return;
  }
  syncApprovalWarn();
  if (mode === "allowAll") return; // confirm panel below decides
  send({ type: "setApprovalMode", sessionId: state.sessionId, mode })
    .then(() => {
      setApprovalDefault(mode);
      syncApprovalWarn();
      toast("approval mode → " + mode);
    })
    .catch((e) => toast("setApprovalMode failed: " + e.message, true));
};
el("approval-confirm-yes").onclick = () => {
  if (!state.sessionId) {
    // No session loaded: confirm the default, send nothing.
    setApprovalDefault("allowAll");
    syncApprovalWarn();
    toast("default approval mode → allowAll (new sessions)");
    return;
  }
  send({ type: "setApprovalMode", sessionId: state.sessionId,
         mode: "allowAll" })
    .then(() => {
      setApprovalDefault("allowAll");
      syncApprovalWarn();
      toast("approval mode → allowAll");
    })
    .catch((e) => toast("setApprovalMode failed: " + e.message, true));
};
el("approval-confirm-no").onclick = () => {
  el("approval-mode").value =
    el("approval-mode").dataset.prev || "onRequest";
  syncApprovalWarn();
};
document.addEventListener("click", (ev) => {
  if (!el("slash-popup").hidden && !el("slash-popup").contains(ev.target) && ev.target !== el("input")) {
    el("slash-popup").hidden = true;
  }
  if ((!el("repo-menu").hidden || !el("branch-menu").hidden) && !ev.target.closest(".pill-wrap")) {
    closeRepoMenus();
  }
  if (!ev.target.closest(".row-menu") && !ev.target.closest(".config-btn")) {
    closeRowMenus();
  }
});

/* Rotating typed placeholder for the composer: type → pause (blink) →
 * delete → next hint. Hints live in composer-hints.json; a one-item
 * fallback covers the fetch. Harmless while the user has typed (the
 * placeholder is hidden then anyway); skipped under prefers-reduced-motion. */
let composerHints = null;
const hintsReady = fetch("composer-hints.json")
  .then((r) => (r.ok ? r.json() : Promise.reject(new Error("hints " + r.status))))
  .then((j) => { if (Array.isArray(j) && j.length) composerHints = j.map(String); })
  .catch(() => {});
function startComposerHints() {
  const input = el("input");
  if (!input || input.dataset.hintsOn) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  input.dataset.hintsOn = "1";
  const TYPE_MS = 65, DELETE_MS = 32, PAUSE_MS = 1500, START_MS = 800, BLINK_MS = 450;
  let ci = 0, phase = "typing", blinkOn = true, blinkT = 0, stopped = false;
  // Random cycle with no repeats: shuffled index order, reshuffled per pass.
  const shuffle = (n) => {
    const a = Array.from({ length: n }, (_, i) => i);
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  };
  const list = composerHints && composerHints.length ? composerHints : ["Ask Muse…"];
  let order = shuffle(list.length), oi = 0;
  const cur = () => list[order[oi]] ?? "";
  const advance = () => {
    oi++;
    if (oi >= order.length) {
      order = shuffle(list.length);
      oi = 0;
    }
  };
  const render = () => {
    const text = Array.from(cur()).slice(0, ci).join("");
    input.placeholder = text + (blinkOn ? "|" : "");
  };
  const step = () => {
    if (stopped || !input.isConnected) return;
    const len = Array.from(cur()).length;
    if (phase === "typing") {
      blinkOn = true;
      if (ci < len) { ci++; render(); setTimeout(step, TYPE_MS); }
      else { phase = "pause"; blinkT = 0; setTimeout(step, BLINK_MS); }
    } else if (phase === "pause") {
      blinkT++;
      blinkOn = !blinkOn; render();
      if (blinkT * BLINK_MS >= PAUSE_MS) { phase = "deleting"; setTimeout(step, DELETE_MS); }
      else setTimeout(step, BLINK_MS);
    } else {
      blinkOn = true;
      if (ci > 0) { ci--; render(); setTimeout(step, DELETE_MS); }
      else { advance(); phase = "typing"; setTimeout(step, TYPE_MS); }
    }
  };
  // Once the user enters the box, the loop stops for good (until reload);
  // blurring just restores the classic static placeholder.
  input.addEventListener("focus", () => { stopped = true; input.placeholder = ""; });
  input.addEventListener("blur", () => { if (stopped) input.placeholder = "Ask Muse…"; });
  render();
  setTimeout(step, START_MS);
}

// Stored UI prefs win; first run falls back to hidden bars, no model/effort pick.
restorePanelState();
updateWelcome();
syncStarsToggle();
updateRepoBar();
loadPickedModel();
loadPickedEffort();
loadPickedApprovalMode();
syncEffortPicker();
syncApprovalSelect();
updateRunChip();

connect();
autosize();
// Start the hints only once the file has loaded (or 1.5s max), so the
// first hint already comes from the shuffled file, not the fallback.
Promise.race([hintsReady, new Promise((r) => setTimeout(r, 1500))]).then(startComposerHints);
