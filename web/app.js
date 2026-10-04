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
  running: false, turnId: null,
  stick: true, history: [], hidx: -1,
  models: [], modelsMeta: null, slashSel: 0, slashList: [],
  reconnectDelay: 1000, everConnected: false,
  lastCumulative: null, lastContext: null, ctxLine: "", sessionMcp: [],
};

/* ---------- tiny helpers ---------- */
function toast(msg, isErr) {
  const t = el("toast");
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
function esc(s) { return String(s == null ? "" : s); }

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
        sysLine("Connected to web-muse bridge. Type /help for slash commands, or just ask.");
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
  const r = await send({ type: "list", limit: 100 });
  state.sessionsCache = r.sessions || [];
  renderSessionList(state.sessionsCache);
  return r;
}

function renderSessionList(sessions) {
  const q = (el("session-filter").value || "").toLowerCase();
  const box = el("session-list");
  box.innerHTML = "";
  const rows = sessions.filter((s) =>
    !q || (s.name || "").toLowerCase().includes(q) || (s.sessionId || "").includes(q));
  if (!rows.length) {
    const d = document.createElement("div");
    d.className = "session-row"; d.textContent = "No sessions yet — press ＋ or /new.";
    box.append(d);
    return;
  }
  for (const s of rows) {
    const row = document.createElement("div");
    row.className = "session-row" + (s.sessionId === state.sessionId ? " active" : "");
    row.setAttribute("role", "option");
    const top = document.createElement("div");
    top.className = "top";
    const dot = document.createElement("span");
    dot.className = "status-dot" + (s.status === "running" ? " running" : "");
    const nm = document.createElement("span");
    nm.className = "name";
    nm.textContent = s.name || shortId(s.sessionId);
    top.append(dot, nm);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${s.status || "—"} · ${s.turnCount || 0} turns · ${esc((s.updatedAt || "").slice(0, 16).replace("T", " "))}`;
    const acts = document.createElement("div");
    acts.className = "acts";
    const mk = (label, title, fn) => {
      const b = document.createElement("button");
      b.textContent = label; b.title = title;
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      return b;
    };
    acts.append(
      mk("open", "Open session", () => openSession(s.sessionId)),
      mk("rename", "Rename session", () => renameSession(s.sessionId)),
      mk("fork", "Fork session", () => forkSession(s.sessionId)),
      mk("delete", "Delete session", () => deleteSession(s.sessionId)),
    );
    row.append(top, meta, acts);
    row.onclick = () => openSession(s.sessionId);
    box.append(row);
  }
}

async function openSession(sessionId) {
  clearTranscript();
  state.sessionId = sessionId;
  closeDrawer();
  el("session-title").textContent = shortId(sessionId);
  try {
    const r = await send({ type: "resume", sessionId });
    state.session = r.session || null;
    el("session-title").textContent = (state.session && (state.session.name || shortId(sessionId))) || shortId(sessionId);
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
      if (histEnv.noneReason === "resume_unserved_by_host" || r.fallback === "resume_unserved_by_host") {
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
    fetchPending();
    renderSessionList(state.sessionsCache);
    scrollDown(true);
  } catch (e) { sysLine("resume failed: " + e.message, true); toast("resume failed: " + e.message, true); }
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
  const nm = (name != null ? name : prompt("Session name:") || "");
  if (!nm.trim()) return;
  try {
    await send({ type: "rename", sessionId: sid, name: nm.trim() });
    toast("renamed to " + nm.trim());
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
  el("sessions").classList.remove("open");
  syncScrim();
  savePanelState();
}
function toggleSessions() {
  const p = el("sessions");
  if (p.classList.contains("open")) {
    p.classList.remove("open");
  } else {
    if (isNarrow()) el("inspector").classList.remove("open");
    p.classList.add("open");
    refreshSessions().catch(() => {});
  }
  syncScrim();
  savePanelState();
}
function toggleInspector() {
  el("inspector").classList.toggle("open");
  if (isNarrow() && el("inspector").classList.contains("open")) {
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

// Panel visibility persists across reloads (localStorage). First run falls
// back to the width defaults: sessions drawer shut on mobile, open on
// desktop; inspector open only on very wide screens.
const PANEL_KEYS = { sessions: "webmuse.sessionsOpen",
  inspector: "webmuse.inspectorOpen" };
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
    if (!isNarrow()) el("sessions").classList.add("open");
    if (window.innerWidth >= 1300) el("inspector").classList.add("open");
  } else {
    el("sessions").classList.toggle("open", s === "1");
    el("inspector").classList.toggle("open", insp === "1");
  }
  syncScrim();
}

/* ---------- terminal transcript ---------- */
function clearTranscript() {
  el("terminal").innerHTML = "";
  el("cards").innerHTML = "";
  el("tab-approvals").innerHTML = "";
  el("tab-tools").innerHTML = "";
  state.items.clear(); state.tools.clear();
  state.running = false; state.turnId = null;
  state.cursor = ""; state.pageCursor = null; state.hasOlder = false;
  state.ctxLine = "";
  state.lastCumulative = null; state.lastContext = null; state.sessionMcp = [];
  el("sess-usage").textContent = ""; el("sess-usage").title = "";
  updateRunChip(); updateOlderBtn(); updateCursorChip();
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
  el("terminal").append(line);
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
  if (k.includes("tool") || k.includes("Tool") || k.includes("command") || k.includes("Command")) return "tool";
  if (k.includes("system") || k.includes("System")) return "system";
  if (k.includes("error") || k.includes("Error") || it.isError) return "error";
  return "agent";
}
const GUTTER = { user: "❯", agent: "●", tool: "⚙", system: "◦", error: "⚠" };

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
  if (kind !== "tool") return esc(it.kind || kind) + when;
  return [it.kind || kind, toolType(it), it.status].filter(Boolean).join(" · ") + when;
}

function renderItem(it, streaming) {
  if (!it || !it.itemId) return;
  let rec = state.items.get(it.itemId);
  const kind = itemKind(it);
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
    el("terminal").append(line);
    rec = { line, body, head, item: it };
    state.items.set(it.itemId, rec);
  } else {
    rec.item = Object.assign({}, rec.item, it);
    rec.line.className = `tline ${kind}`;
    rec.line.querySelector(".gut").textContent = GUTTER[kind] || "●";
    rec.head.textContent = itemHeadLabel(rec.item, kind);
  }
  let txt = itemText(rec.item);
  if (!txt && kind === "tool" && !streaming) txt = toolSummary(rec.item);
  rec.body.textContent = txt;
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
  // Streaming caret
  rec.body.querySelectorAll(".caret").forEach((c) => c.remove());
  if (streaming) {
    const c = document.createElement("span");
    c.className = "caret";
    rec.body.append(c);
  }
  rec.line.classList.toggle("streaming", !!streaming);
  scrollDown();
}

function appendDelta(itemId, delta) {
  const rec = state.items.get(itemId);
  if (!rec) return;
  rec.body.querySelectorAll(".caret").forEach((c) => c.remove());
  rec.body.textContent += delta;
  const c = document.createElement("span");
  c.className = "caret";
  rec.body.append(c);
  rec.line.classList.add("streaming");
  const cur = itemText(rec.item) + delta;
  rec.item = Object.assign({}, rec.item, { text: cur });
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
  row.querySelector(".t").textContent = esc([toolType(it), it.status].filter(Boolean).join(" · ") || it.kind || "tool");
  row.querySelector(".s").textContent = toolSummary(it) || JSON.stringify(it).slice(0, 500);
}

function updateRunChip() {
  const chip = el("run-chip");
  chip.textContent = state.running ? "● running" : "idle";
  chip.className = "chip " + (state.running ? "running" : "idle");
  el("btn-stop").disabled = !state.running;
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
  body.textContent = itemText(it) || (kind === "tool" ? toolSummary(it) : "");
  wrap.append(head, body);
  line.append(gut, wrap);
  el("terminal").prepend(line);
  state.items.set(it.itemId, { line, body, head, item: it });
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
      renderItem(p.item || { itemId: p.itemId, kind: p.kind, text: p.text }, false);
      break;
    case "turn/started":
      state.running = true; state.turnId = p.turnId || null; updateRunChip();
      break;
    case "turn/completed": {
      // Single terminal event: p.terminal is completed|failed|cancelled.
      // (There are no turn/cancelled or turn/failed notifications on MSP v1.)
      state.running = false; state.turnId = null; updateRunChip();
      document.querySelectorAll(".tline.streaming").forEach((d) => {
        d.classList.remove("streaming");
        d.querySelectorAll(".caret").forEach((c) => c.remove());
      });
      const term = p.terminal || "completed";
      if (term === "failed") {
        sysLine("turn failed: " + esc((p.error && (p.error.message || p.error.code)) || p.reason || "unknown"), true);
      } else if (term === "cancelled") {
        sysLine("turn cancelled." + (p.reason ? " " + esc(p.reason) : ""));
      } else if (p.usage && p.usage.totalTokens != null) {
        sysLine(`turn done · ${p.usage.totalTokens} tok` +
          (p.durationMs != null ? ` · ${(p.durationMs / 1000).toFixed(1)}s` : ""));
      }
      refreshUsage().catch(() => {});
      refreshSessions().catch(() => {});
      break;
    }
    case "turn/retracted":
      sysLine("turn retracted — prompt restored to the composer.");
      if (p.promptText) { el("input").value = p.promptText; autosize(); }
      break;
    case "turn/unqueued":
      sysLine("queued turn reclaimed.");
      break;
    case "turn/retryScheduled":
      sysLine("turn retry scheduled" + (p.reason ? ": " + esc(p.reason) : "") + ".");
      break;
    case "session/statusChanged":
      state.running = p.status === "running"; updateRunChip();
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
        el("session-title").textContent = state.session.name || shortId(state.sessionId);
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
  el("cards").append(div);
  el("tab-approvals").append(div.cloneNode(true));
  // Rebind cloned buttons (cloneNode drops listeners).
  bindClonedApproval(div.dataset.aid, a, req, choices);
  sysLine(`approval requested: ${a.toolName || "tool"} — decide in the card above the composer.`);
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

function bindClonedApproval(aid, a, req, choices) {
  const clone = el("tab-approvals").querySelector(`[data-aid="${CSS.escape(aid)}"]`);
  if (!clone) return;
  const btns = clone.querySelectorAll(".choices button");
  const fb = clone.querySelector("input");
  btns.forEach((b, i) => {
    const c = (choices || [])[i] || {};
    b.onclick = () => decideApproval(a, req, c.choiceId || c.id, c,
      fb ? fb.value : "");
  });
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
async function refreshModels() {
  const r = await send({ type: "models", sessionId: state.sessionId || undefined });
  state.models = r.models || [];
  state.modelsMeta = { source: r.source, providerId: r.providerId, profileId: r.profileId };
  const sel = el("model-picker");
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = ""; ph.textContent = "model…";
  sel.append(ph);
  for (const m of state.models) {
    const o = document.createElement("option");
    o.value = m.modelId;
    o.textContent = (m.isActive ? "● " : "") + (m.displayLabel || m.modelId);
    o.dataset.provider = m.providerId || "";
    if (m.isActive) o.selected = true;
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
  { name: "clear", usage: "/clear", desc: "Clear local transcript view", run: () => { clearTranscriptKeepSession(); } },
  { name: "models", usage: "/models", desc: "List models in transcript", run: () => cmdModels() },
  { name: "model", usage: "/model <id>", desc: "Set model for current session", run: (a) => cmdSetModel(a) },
  { name: "effort", usage: "/effort <tier>", desc: "Set reasoning effort (none…ultra)", run: (a) => cmdSetEffort(a) },
  { name: "skills", usage: "/skills", desc: "List session skills", run: () => cmdSkills() },
  { name: "mcp", usage: "/mcp", desc: "Show configured MCP servers", run: () => cmdMcp() },
  { name: "output", usage: "/output <itemId>", desc: "Fetch full truncated output", run: (a) => cmdOutput(a) },
  { name: "compact", usage: "/compact", desc: "Compact current session", run: () => cmdCompact() },
  { name: "usage", usage: "/usage", desc: "Show subscription usage", run: () => cmdUsage() },
  { name: "pending", usage: "/pending", desc: "Show pending approvals", run: () => fetchPending().then(() => toast("pending refreshed")) },
  { name: "interrupt", usage: "/interrupt", desc: "Interrupt running turn", run: () => cmdInterrupt() },
  { name: "stop", usage: "/stop", desc: "Alias for /interrupt", run: () => cmdInterrupt() },
  { name: "cancel", usage: "/cancel", desc: "Cancel running turn", run: () => cmdCancel() },
  { name: "steer", usage: "/steer <text>", desc: "Steer running turn", run: (a, raw) => cmdSteer(raw) },
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
  const hit = state.sessionsCache.find((s) => s.sessionId.toLowerCase().startsWith(prefix));
  if (!hit) { sysLine("No session matches prefix " + prefix, true); return; }
  await openSession(hit.sessionId);
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
    sysLine("Model → " + hit.modelId + ` (${r.status || "accepted"})` +
      (state.running ? " — applies at the next model-call boundary." : ""));
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

const EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

async function cmdSetEffort(args) {
  if (!state.sessionId) return sysLine("No session — open one first.", true);
  const want = (args[0] || "").toLowerCase();
  if (!EFFORTS.includes(want)) {
    return sysLine("Usage: /effort <tier>  tiers: " + EFFORTS.join(" | "), true);
  }
  try {
    await send({ type: "setEffort", sessionId: state.sessionId, reasoningEffort: want });
    sysLine("Reasoning effort (session default) → " + want);
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
  list.forEach((c, i) => {
    const d = document.createElement("div");
    d.className = "slash-item" + (i === state.slashSel ? " sel" : "");
    const cmd = document.createElement("span");
    cmd.className = "cmd"; cmd.textContent = c.usage;
    const desc = document.createElement("span");
    desc.className = "desc"; desc.textContent = c.desc;
    d.append(cmd, desc);
    d.onclick = () => { applySlash(i); };
    pop.append(d);
  });
  pop.hidden = false;
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
  if (!state.ws || state.ws.readyState !== 1) { toast("not connected", true); return; }
  el("slash-popup").hidden = true;
  if (text.startsWith("/")) {
    box.value = ""; autosize();
    state.history.unshift(text); state.hidx = -1;
    await dispatchSlash(text);
    return;
  }
  box.value = ""; autosize();
  state.history.unshift(text); state.hidx = -1;
  // Optimistic user echo (reconciled by server item/completed).
  renderItem({ itemId: "local-" + Date.now(), kind: "userMessage", text }, false);
  try {
    const r = await send({ type: "prompt", sessionId: state.sessionId || undefined, text });
    if (r.sessionId && !state.sessionId) {
      state.sessionId = r.sessionId;
      el("session-title").textContent = shortId(r.sessionId);
      await send({ type: "subscribe", sessionId: r.sessionId }).catch(() => {});
      refreshSessions().catch(() => {});
      refreshModels().catch(() => {});
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
  if (ev.key === "Escape") { el("slash-popup").hidden = true; return; }
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
document.addEventListener("keydown", (ev) => {
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
el("btn-new").onclick = () => {
  const p = prompt("Session directory (blank for the default workspace):");
  if (p == null) return; // cancelled
  if (p.trim()) newSession(undefined, { workspaceRoot: p });
  else newSession();
  closeDrawer();
};
el("btn-refresh-sessions").onclick = () => refreshSessions().catch((e) => toast(e.message, true));
el("session-filter").oninput = () => renderSessionList(state.sessionsCache);
el("btn-older").onclick = loadOlder;
el("jump-latest").onclick = () => { state.stick = true; scrollDown(true); };
el("terminal").addEventListener("scroll", () => {
  const t = el("terminal");
  state.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  updateJumpBtn();
});
el("model-picker").onchange = (ev) => {
  const o = ev.target.selectedOptions[0];
  if (!o || !o.value || !state.sessionId) return;
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
el("approval-mode").onchange = (ev) => {
  if (!state.sessionId) { toast("no session", true); return; }
  send({ type: "setApprovalMode", sessionId: state.sessionId, mode: ev.target.value })
    .then(() => toast("approval mode → " + ev.target.value))
    .catch((e) => toast("setApprovalMode failed: " + e.message, true));
};
document.addEventListener("click", (ev) => {
  if (!el("slash-popup").hidden && !el("slash-popup").contains(ev.target) && ev.target !== el("input")) {
    el("slash-popup").hidden = true;
  }
});

// Panel visibility: stored toggles win, else width-based first-run defaults.
restorePanelState();

connect();
autosize();
