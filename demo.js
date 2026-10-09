// demo.js — GitHub Pages backend workaround (static hosting has no backend).
// This is the ONLY file in this deployment that is not a verbatim copy of
// web/: it fakes the exact wire touchpoints the real app.js needs, speaking
// the same JSON protocol, backed by in-memory canned data:
//
//   * WebSocket .../ws -> demo bridge (same {id, type} request/result frames
//     plus {type:"hello"/"event"} pushes app.js already handles)
//   * fetch("themes")  -> saved-theme list (the real themes/*.json files load
//     as static files, unchanged)
//   * fetch("/health")  -> canned health for progress.html
//
// Everything on screen is the real UI (same index.html/app.js/style.css).
// Transcripts and replies are canned in the browser; nothing leaves it.
(function () {
  "use strict";

  var t0 = Date.now();
  var THEME_NAMES = ["claudish", "default", "gpt", "midnight", "muse-code", "parchment", "sorbet"];
  var MODELS = [
    { modelId: "opus", displayLabel: "Opus" },
    { modelId: "sonnet", displayLabel: "Sonnet", isActive: true },
    { modelId: "haiku", displayLabel: "Haiku" }
  ];
  var C1 = "Demo mode \u2014 no backend is attached, so this reply is canned in your browser and nothing leaves it.\n\n";
  var C2 = "In the real app this streams from your local `muse serve` host. ";
  var C3 = "Try `/theme` to list saved palettes, `/models` for the model catalog, or open the other demo session.";
  var DEMO_REPLY = C1 + C2 + C3;

  function isoNow() { return new Date().toISOString(); }

  var seq = 0;
  function nid(prefix) { seq += 1; return (prefix || "demo-") + Date.now().toString(36) + "-" + seq; }

  var sessions = [
    { sessionId: "demo-getting-started", name: "Getting started", status: "idle", bridgeCreated: true, turnCount: 1, updatedAt: isoNow() },
    { sessionId: "demo-theme-review", name: "Theme review", status: "idle", bridgeCreated: true, turnCount: 1, updatedAt: isoNow() }
  ];
  var transcripts = {
    "demo-getting-started": [
      { itemId: "demo-g1-u", kind: "userMessage", text: "How do I run web-muse locally?" },
      { itemId: "demo-g1-a", kind: "agentMessage", text: "Run `python3 -m server.main --port 8000` and open http://127.0.0.1:8000/.\n\nYou are looking at the real web-muse UI. This page is a static demo with no backend: the sessions and replies here are canned in your browser and nothing leaves it." }
    ],
    "demo-theme-review": [
      { itemId: "demo-t1-u", kind: "userMessage", text: "Can I try the themes?" },
      { itemId: "demo-t1-a", kind: "agentMessage", text: "Yes \u2014 run `/theme` to list the saved palettes, then `/theme <name>` to apply one. The choice is stored per browser." }
    ]
  };
  var liveTurns = {};

  function cloneSession(s) {
    return { sessionId: s.sessionId, name: s.name, status: s.status,
      bridgeCreated: s.bridgeCreated, turnCount: s.turnCount, updatedAt: s.updatedAt };
  }
  function findSession(sid) {
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].sessionId === sid) return sessions[i];
    }
    return null;
  }
  function touch(s) { s.updatedAt = isoNow(); }

  /* ---------- fetch patch: API routes only, static files pass through --- */
  function pathOf(url) {
    var u = String((url && url.url) || url || "");
    u = u.split("#")[0].split("?")[0];
    var m = u.match(/^[a-z][a-z0-9+.-]*:\/\/[^/]+(\/.*)$/i);
    if (m) u = m[1];
    return u;
  }
  function jsonResponse(obj) {
    return new Response(JSON.stringify(obj),
      { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (window.fetch) {
    var rawFetch = window.fetch.bind(window);
    window.fetch = function (url, opts) {
      var p = pathOf(url);
      if (p === "themes" || p === "/themes" || /\/themes$/.test(p)) {
        return Promise.resolve(jsonResponse({ themes: THEME_NAMES.slice() }));
      }
      if (p === "health" || p === "/health" || /\/health$/.test(p)) {
        return Promise.resolve(jsonResponse({
          ok: true, mspAlive: true,
          server: { version: "demo" },
          schema: { fingerprint: "demo-fp-0123456789", version: 1 },
          uptimeS: Math.floor((Date.now() - t0) / 1000),
          wsConns: 1, sessionsTracked: sessions.length
        }));
      }
      return rawFetch(url, opts);
    };
  }

  /* ---------- WebSocket fake: same frames, canned data ------------------ */
  var RealWebSocket = window.WebSocket;
  function isBridgeUrl(url) { return /\/ws(\?.*)?$/.test(String(url)); }

  function DemoSocket(url) {
    this.url = String(url);
    this.readyState = 0; // CONNECTING
    this._listeners = {};
    this.onopen = null; this.onclose = null; this.onmessage = null; this.onerror = null;
    var self = this;
    setTimeout(function () {
      if (self.readyState !== 0) return;
      self.readyState = 1; // OPEN
      fire(self, "open", {});
      deliver(self, { type: "hello", mspAlive: true,
        server: { version: "demo" },
        schema: { fingerprint: "demo-fp-0123456789", version: 1 } });
    }, 30);
  }
  DemoSocket.prototype.send = function (data) {
    var self = this;
    var msg;
    try { msg = JSON.parse(data); } catch (_) { return; }
    setTimeout(function () { handleMessage(self, msg); }, 10);
  };
  DemoSocket.prototype.close = function () {
    if (this.readyState === 3) return;
    this.readyState = 3; // CLOSED
    fire(this, "close", { code: 1000 });
  };
  DemoSocket.prototype.addEventListener = function (type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  };
  DemoSocket.prototype.removeEventListener = function (type, fn) {
    var a = this._listeners[type];
    if (!a) return;
    var i = a.indexOf(fn);
    if (i >= 0) a.splice(i, 1);
  };
  function fire(sock, type, ev) {
    ev = ev || {};
    try { if (!("target" in ev)) ev.target = sock; } catch (_) { /* frozen */ }
    ev.type = type;
    var a = sock._listeners[type] || [];
    for (var i = 0; i < a.length; i++) {
      try { a[i].call(sock, ev); } catch (_) { /* listener error */ }
    }
    var prop = sock["on" + type];
    if (typeof prop === "function") {
      try { prop.call(sock, ev); } catch (_) { /* handler error */ }
    }
  }
  function deliver(sock, obj) {
    if (sock.readyState !== 1) return;
    fire(sock, "message", { data: JSON.stringify(obj) });
  }
  function reply(sock, id, result) {
    deliver(sock, { id: id, type: "result", ok: true, result: result || {} });
  }
  function fail(sock, id, message) {
    deliver(sock, { id: id, type: "result", ok: false, error: { message: message } });
  }

  function cancelLive(sid) {
    var live = sid && liveTurns[sid];
    if (!live) return false;
    live.cancelled = true;
    for (var i = 0; i < live.timers.length; i++) clearTimeout(live.timers[i]);
    delete liveTurns[sid];
    return true;
  }

  function streamReply(sock, sess, turnId) {
    var sid = sess.sessionId;
    cancelLive(sid);
    var itemId = nid("demo-a-");
    var timers = [];
    var live = { timers: timers, cancelled: false };
    liveTurns[sid] = live;
    function later(ms, fn) {
      var h = setTimeout(function () {
        if (live.cancelled) return;
        fn();
      }, ms);
      timers.push(h);
    }
    later(200, function () {
      deliver(sock, { type: "event", method: "turn/started",
        params: { sessionId: sid, turnId: turnId } });
      deliver(sock, { type: "event", method: "item/started",
        params: { sessionId: sid, item: { itemId: itemId, kind: "agentMessage", text: "" } } });
    });
    var chunks = [C1, C2, C3];
    var at = 450;
    for (var i = 0; i < chunks.length; i++) {
      (function (c, ms) {
        later(ms, function () {
          deliver(sock, { type: "event", method: "item/delta",
            params: { sessionId: sid, itemId: itemId, field: "text", delta: c } });
        });
      })(chunks[i], at);
      at += 250;
    }
    later(at + 200, function () {
      var item = { itemId: itemId, kind: "agentMessage", text: DEMO_REPLY };
      transcripts[sid].push(item);
      deliver(sock, { type: "event", method: "item/completed",
        params: { sessionId: sid, item: item } });
      deliver(sock, { type: "event", method: "turn/completed",
        params: { sessionId: sid, turnId: turnId, terminal: "completed",
          usage: { totalTokens: 96 }, durationMs: at } });
      deliver(sock, { type: "event", method: "session/tokenUsage",
        params: { sessionId: sid,
          cumulative: { promptTokens: 64, outputTokens: 32, totalTokens: 96,
            cost: { usd: 0 } } } });
      sess.status = "idle";
      touch(sess);
      delete liveTurns[sid];
    });
  }

  function handleMessage(sock, msg) {
    if (sock.readyState !== 1) return;
    var id = msg.id;
    var type = msg.type;
    var i, s;
    switch (type) {
      case "list":
        reply(sock, id, { sessions: sessions.map(cloneSession) });
        break;
      case "models":
        reply(sock, id, { models: MODELS, source: "demo",
          providerId: "demo", profileId: "demo" });
        break;
      case "usage": {
        var t = Date.now();
        reply(sock, id, { usage: { observedAtMs: t, tier: "demo",
          window: { usedPercent: 12, windowDurationMins: 300, resetsAtMs: t + 3600e3 },
          weekly: { usedPercent: 34, resetsAtMs: t + 7 * 86400e3 } } });
        break;
      }
      case "new":
        s = { sessionId: nid("demo-"), name: "", status: "idle",
          bridgeCreated: true, turnCount: 0, updatedAt: isoNow() };
        sessions.unshift(s);
        transcripts[s.sessionId] = [];
        reply(sock, id, { session: cloneSession(s), mcpAttached: [] });
        break;
      case "resume":
        s = findSession(msg.sessionId);
        if (!s) { fail(sock, id, "unknown session (demo)"); break; }
        reply(sock, id, { session: cloneSession(s), viewCursor: "",
          history: { mode: "inline", items: (transcripts[s.sessionId] || []).slice() } });
        break;
      case "subscribe":
        reply(sock, id, { viewCursor: "", events: [] });
        break;
      case "prompt": {
        var sid = msg.sessionId;
        s = (sid && findSession(sid)) || null;
        if (!s) {
          s = { sessionId: nid("demo-"), name: "", status: "idle",
            bridgeCreated: true, turnCount: 0, updatedAt: isoNow() };
          sessions.unshift(s);
          transcripts[s.sessionId] = [];
          sid = s.sessionId;
        }
        var turnId = nid("turn-");
        transcripts[sid].push(
          { itemId: nid("demo-u-"), kind: "userMessage", text: String(msg.text || "") });
        s.turnCount = (s.turnCount || 0) + 1;
        s.status = "running";
        touch(s);
        reply(sock, id, { sessionId: sid, turnId: turnId, disposition: "started" });
        streamReply(sock, s, turnId);
        break;
      }
      case "page":
        reply(sock, id, { events: [], nextCursor: null });
        break;
      case "rename":
        s = findSession(msg.sessionId);
        if (s && msg.name) { s.name = String(msg.name).slice(0, 200); touch(s); }
        reply(sock, id, {});
        break;
      case "delete":
        for (i = 0; i < sessions.length; i++) {
          if (sessions[i].sessionId === msg.sessionId) {
            cancelLive(msg.sessionId);
            sessions.splice(i, 1);
            delete transcripts[msg.sessionId];
            break;
          }
        }
        reply(sock, id, {});
        break;
      case "fork": {
        var src = findSession(msg.sessionId);
        var c = { sessionId: nid("demo-"),
          name: src && src.name ? src.name + " (fork)" : "",
          status: "idle", bridgeCreated: true,
          turnCount: (src && src.turnCount) || 0, updatedAt: isoNow() };
        sessions.unshift(c);
        transcripts[c.sessionId] = ((src && transcripts[src.sessionId]) || []).slice();
        reply(sock, id, { session: cloneSession(c), sessionId: c.sessionId });
        break;
      }
      case "pending":
        reply(sock, id, { approvals: [], userInputs: [] });
        break;
      case "skills":
        reply(sock, id, { skills: [] });
        break;
      case "plugins":
        reply(sock, id, { plugins: [] });
        break;
      case "mcp":
        reply(sock, id, { servers: [], source: "demo",
          hint: "Demo mode \u2014 no MCP servers are configured." });
        break;
      case "browse":
        reply(sock, id, { path: msg.path || "~", entries: [], parent: null });
        break;
      case "githubRepos":
        reply(sock, id, { repos: [] });
        break;
      case "githubBranches":
        reply(sock, id, { branches: [], defaultBranch: null });
        break;
      case "ordersList":
        reply(sock, id, { orders: [] });
        break;
      case "compact":
        reply(sock, id, { status: "noop", reason: "demo_no_backend" });
        break;
      case "readOutput":
        reply(sock, id, { content: "", mediaType: "text/plain",
          offsetBytes: 0, byteLen: 0, eof: true });
        break;
      case "interrupt":
      case "cancel": {
        var stopped = cancelLive(msg.sessionId);
        reply(sock, id, stopped ? { cancelled: true } : {});
        if (stopped && msg.sessionId) {
          deliver(sock, { type: "event", method: "turn/completed",
            params: { sessionId: msg.sessionId, terminal: "cancelled",
              reason: "demo cancel" } });
          s = findSession(msg.sessionId);
          if (s) { s.status = "idle"; touch(s); }
        }
        break;
      }
      default:
        // approve, answer, steer, unqueue, setModel, setEffort,
        // setApprovalMode, setSubagentAutoApprove, ordersDecide,
        // githubClone, githubOpen, githubCancel, githubClean, read, ...
        reply(sock, id, {});
        break;
    }
  }

  function FakeWebSocket(url, protocols) {
    if (isBridgeUrl(url)) return new DemoSocket(url);
    return new RealWebSocket(url, protocols);
  }
  FakeWebSocket.prototype = RealWebSocket ? RealWebSocket.prototype : DemoSocket.prototype;
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;
  window.WebSocket = FakeWebSocket;
})();
