"""Session routing: WS connections <-> MSP sessionIds, cursor store, frame mapping.

Pure mapping helpers (build_* / map_*) are unit-tested in
tests/test_msp_mapping.py against recorded fixtures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from server.github import GithubError, run_gh_clone, run_gh_list, validate_fullname
from server.msp import MspError, uuid7

LOG = logging.getLogger("web_muse.sessions")

# JSON-RPC "method not found": a --no-session-log (memory-only) host serves
# no view-store methods — session/resume, session/read, view/subscribe and
# view/page all answer -32601 there (verified live). Durable hosts serve
# them. The bridge falls back below instead of failing the UI.
METHOD_NOT_FOUND = -32601

# MSP methods that are commands: the bridge must NOT accept a client commandId,
# it always mints a fresh UUIDv7 (see MspClient.command).
COMMAND_METHODS = {
    "session/start": "session/start",
    "session/resume": "session/resume",
    "session/fork": "session/fork",
    "session/rename": "session/rename",
    "session/delete": "session/delete",
    "session/setModel": "session/setModel",
    "session/setApprovalMode": "session/setApprovalMode",
    "session/setReasoningEffort": "session/setReasoningEffort",
    "session/compact": "session/compact",
    "turn/start": "turn/start",
    "turn/interrupt": "turn/interrupt",
    "turn/cancel": "turn/cancel",
    "turn/steer": "turn/steer",
    "turn/unqueue": "turn/unqueue",
    "approval/decide": "approval/decide",
    "userInput/answer": "userInput/answer",
}

# Hardening: the bridge never selects the no-approval mode over the wire.
# (The host default onRequest applies unless the user changes it in the TUI.)
FORBIDDEN_APPROVAL_MODES = {"allowAll"}

# MSP ApprovalMode closed enum (schema $defs/ApprovalMode); allowAll is
# rejected above, the other three are selectable.
VALID_APPROVAL_MODES = {"allowAll", "promptUnmatched", "onRequest",
                        "denyUnmatched"}

# MSP ReasoningEffort closed tier vocabulary (schema $defs/ReasoningEffort).
VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high",
                           "xhigh", "max", "ultra"}

# MSP view/page directions (schema $defs/ViewPageDirection).
VALID_PAGE_DIRECTIONS = {"forward", "backward"}

# Default session-name length: the web panel names a session from the
# first few characters of its initial prompt (same convention as the
# muse TUI), as a fallback until an explicit rename takes precedence.
DEFAULT_SESSION_NAME_LEN = 40


def default_session_name(text, limit=DEFAULT_SESSION_NAME_LEN):
    """Initial-prompt prefix used as a session's default (fallback) name.

    Whitespace (including newlines) collapses to single spaces and the
    result hard-slices to `limit` characters. Returns "" when there is
    no usable text (blank prompt, images-only turn), so callers skip
    the rename instead of setting an empty name.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def build_turn_input(text, images=None):
    """WS prompt payload -> MSP TurnInputPart list."""
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images or []:
        parts.append({
            "type": "image",
            "mediaType": img.get("mediaType", "image/png"),
            "base64Data": img.get("base64Data", ""),
        })
    if not parts:
        raise ValueError("prompt needs text or images")
    return parts


def notification_session_id(method, params):
    """Route key for an MSP notification/request; None => broadcast."""
    if not isinstance(params, dict):
        return None
    sid = params.get("sessionId")
    if sid:
        return sid
    if method in ("session/started", "session/listChanged", "session/closed"):
        sess = params.get("session")
        if isinstance(sess, dict) and sess.get("sessionId"):
            return sess["sessionId"]
        # session/closed carries sessionId at top level; listChanged is global.
        return params.get("sessionId")
    return None


def map_notification_to_ws(method, params):
    """MSP notification -> WS server->client frame (thin envelope)."""
    return {"type": "event", "method": method, "params": params}


def map_server_request_to_ws(method, params):
    """MSP server-initiated request -> WS frame (receipt already sent)."""
    if method == "approval/request":
        return {"type": "approval", "approval": params}
    if method == "userInput/request":
        return {"type": "userInput", "prompt": params}
    return {"type": "event", "method": method, "params": params}


def settings_path():
    """User settings file where `mcpServers` entries live (per muse mcp help)."""
    return Path(os.path.expanduser("~")) / ".config" / "muse" / "settings.json"


def read_mcp_servers(path=None):
    """Local MCP server inventory.

    MSP v1 (stable AND experimental schema exports, verified against the
    `muse schema` bundle) exposes no mcp/* methods, so /mcp is served from
    the same settings.json the `muse mcp login` command uses. Returns a
    JSON-able dict; never raises on missing/unparseable files.
    """
    p = Path(path) if path else settings_path()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return {"servers": [], "source": str(p), "configured": False,
                "hint": "no settings.json yet; run `muse login` or add an "
                        "mcpServers entry, then `muse mcp login <server>`"}
    except (OSError, ValueError) as e:
        return {"servers": [], "source": str(p), "configured": False,
                "hint": f"settings unreadable: {e}"}
    entries = raw.get("mcpServers") or {}
    servers = []
    for name, cfg in entries.items() if isinstance(entries, dict) else []:
        if not isinstance(cfg, dict):
            continue
        # Never echo secrets: report shape only (transport/url/command names
        # are config diagnostics-safe; headers/env values are not).
        servers.append({
            "name": name,
            "transport": cfg.get("transport", "streamableHttp"),
            "url": cfg.get("url"),
            "command": cfg.get("command"),
            "hasHeaders": bool(cfg.get("headers")),
            "hasEnv": bool(cfg.get("env")),
        })
    return {"servers": servers, "source": str(p),
            "configured": bool(servers),
            "hint": "" if servers else
                    "no mcpServers entries; add one under mcpServers in "
                    "settings.json, or attach one to a new session with "
                    "`/new --mcp <name>`"}


def read_settings_raw(path=None):
    """Parsed settings.json; {} when missing/unparseable (never raises)."""
    p = Path(path) if path else settings_path()
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# SessionMcpServerMode + SessionMcpStdioFraming closed enums: invalid optional
# values are dropped with a warning (the host would reject the whole start).
VALID_MCP_MODES = {"required", "optional"}
VALID_MCP_FRAMINGS = {"auto", "contentLength", "lineDelimitedJson"}


def build_session_mcp_config(names, path=None):
    """settings.json entries -> wire SessionConfig {"mcpServers": {...}}.

    Secrets (headers/env values) are resolved here, bridge-side, so they
    never cross the browser boundary. Returns (config, warnings).
    Raises ValueError on unknown names (with the known-name list).
    """
    entries = read_settings_raw(path).get("mcpServers") or {}
    if not isinstance(entries, dict):
        entries = {}
    servers = {}
    warnings = []
    for name in names or []:
        cfg = entries.get(name)
        if not isinstance(cfg, dict):
            known = sorted(entries) or ["(none configured)"]
            raise ValueError(f"unknown MCP server {name!r}; known: "
                             + ", ".join(known))
        if cfg.get("command"):
            arm = {"transport": "stdio", "command": cfg["command"]}
            if isinstance(cfg.get("args"), list):
                arm["args"] = [str(a) for a in cfg["args"]]
            if isinstance(cfg.get("env"), dict):
                arm["env"] = {str(k): str(v)
                              for k, v in cfg["env"].items()}
            if cfg.get("framing") in VALID_MCP_FRAMINGS:
                arm["framing"] = cfg["framing"]
            elif cfg.get("framing") is not None:
                warnings.append(f"{name}: dropping invalid framing "
                                f"{cfg['framing']!r}")
            if cfg.get("mode") in VALID_MCP_MODES:
                arm["mode"] = cfg["mode"]
            elif cfg.get("mode") is not None:
                warnings.append(f"{name}: dropping invalid mode "
                                f"{cfg['mode']!r}")
        elif cfg.get("url"):
            arm = {"transport": "streamableHttp", "url": cfg["url"]}
            if isinstance(cfg.get("headers"), dict):
                arm["headers"] = {str(k): str(v)
                                  for k, v in cfg["headers"].items()}
            if cfg.get("mode") in VALID_MCP_MODES:
                arm["mode"] = cfg["mode"]
            elif cfg.get("mode") is not None:
                warnings.append(f"{name}: dropping invalid mode "
                                f"{cfg['mode']!r}")
        else:
            warnings.append(f"{name}: skipped (entry has neither "
                            "command nor url)")
            continue
        servers[name] = arm
    return ({"mcpServers": servers} if servers else {}), warnings


# Client-supplied sessionIds become directory names: strict charset, no
# leading dot, bounded length. Anything else is rejected, never remapped
# (silent remapping could park two sessions in one workspace).
SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def session_workspace_dir(base, session_id):
    """Directory for one session under `base`; created, absolute, contained.

    Raises ValueError on unsafe ids or containment failure. Never raises
    on pre-existing directories.
    """
    sid = session_id or ""
    if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
            or sid in (".", ".."):
        raise ValueError(f"unsafe sessionId for workspace dir: {sid!r}")
    root = Path(base).resolve()
    target = (root / sid).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"workspace dir escapes base: {sid!r}")
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def validate_manual_root(path):
    """Check a client-supplied session directory: absolute, existing dir.

    Manual roots may point anywhere on the device (first-use confirm
    happens in the UI), but the bridge creates nothing outside its own
    workspace base — so the directory must already exist. Raises
    ValueError, which the caller turns into an error reply.
    """
    p = path if isinstance(path, str) else ""
    if not p.strip():
        raise ValueError("workspaceRoot must be a non-empty path")
    if not os.path.isdir(p):
        raise ValueError(f"workspaceRoot is not an existing directory: {p!r}")
    return str(Path(p).resolve())


def browse_home():
    """Default explorer root: the server's $HOME, falling back to /."""
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return str(Path(home).resolve())
    return "/"


def browse_dir(path=None):
    """List one directory for the new-session explorer.

    Any on-device path may be listed (manual roots may point anywhere);
    nothing is created. Returns a JSON-able dict:
    {path, parent, home, entries:[{name, path, isDir, isHidden}]} sorted
    dirs-first, alphabetical (case-insensitive). Raises ValueError on a
    missing/non-directory path or an unreadable directory.
    """
    raw = path if isinstance(path, str) else ""
    raw = raw.strip() or browse_home()
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    target = Path(raw)
    if not target.is_absolute():
        raise ValueError(f"browse path must be absolute: {raw!r}")
    if not os.path.isdir(target):
        raise ValueError(f"not an existing directory: {raw!r}")
    resolved = str(target.resolve())
    try:
        with os.scandir(resolved) as it:
            rows = [(e.name, e.path, e.is_dir(follow_symlinks=True))
                    for e in it]
    except OSError as e:
        raise ValueError(f"directory unreadable: {resolved!r} ({e})")
    entries = [{
        "name": name,
        "path": str(Path(resolved) / name),
        "isDir": bool(is_dir),
        "isHidden": name.startswith("."),
    } for name, _, is_dir in rows]
    entries.sort(key=lambda e: (not e["isDir"], e["name"].lower(), e["name"]))
    parent = str(Path(resolved).parent)
    return {"path": resolved, "parent": parent, "home": browse_home(),
            "entries": entries}


# Sync pacing: the host's deletion registry admits one deletion at a time
# and stays busy while one settles, so back-to-back deletes are rejected
# with Store(Busy). Retry those with backoff and pause between deletes.
_PRUNE_RETRY_DELAYS_S = (2, 4, 6)
_PRUNE_PAUSE_S = 0.5


def _transient_delete_error(e):
    return "busy" in str(e).lower()


def flag_missing_workspaces(result):
    """Flag `session/list` rows whose workspaceRoot is gone from disk.

    Sessions are host-owned: a removed directory only hides the row (the
    UI filters on `workspaceMissing`) — the session itself is never deleted
    here, so a transiently missing mount reappears with history intact
    instead of losing everything. Rows without a root are left alone.
    """
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if not isinstance(sessions, list):
        return result
    for s in sessions:
        if not isinstance(s, dict):
            continue
        root = s.get("workspaceRoot")
        if isinstance(root, str) and root and not os.path.isdir(root):
            s["workspaceMissing"] = True
    return result


class SessionRouter:
    """Tracks WS<->sessionId subscriptions and last-seen view cursors."""

    def __init__(self, msp, workspace_base=None, gh_bin="gh"):
        self._msp = msp
        # Base dir for per-session workspaces (None = send no workspaceRoot).
        self._workspace_base = str(workspace_base) if workspace_base else None
        self._gh_bin = gh_bin or "gh"
        # Clone opId -> [task, conn, fullName]. Terminal-event ownership
        # goes to whoever pops the record first (finishing task or
        # canceller), so exactly one githubCloneResult fires even when a
        # cancel lands before the task's first step (a coroutine cancelled
        # that early never runs its body, not even `finally`).
        self._github_ops = {}
        self._subs = {}   # sessionId -> set of ClientConnection
        # Sessions this bridge created while still unnamed: the first
        # prompt names them from its initial text (default fallback).
        # An explicit rename drops the id, so user/host names always win.
        self._auto_name_pending = set()
        self._cursors = {}  # sessionId -> last viewCursor seen
        self._turns = {}  # sessionId -> running turnId (from turn/started)
        self._conns = set()
        self._lock = asyncio.Lock()

    # -- connection registry ------------------------------------------------
    def add_conn(self, conn):
        self._conns.add(conn)

    def drop_conn(self, conn):
        self._conns.discard(conn)
        for members in self._subs.values():
            members.discard(conn)

    # -- subscriptions ------------------------------------------------------
    async def subscribe(self, conn, session_id, after=None):
        params = {"sessionId": session_id}
        if after:
            params["after"] = after
        result = await self._msp.call("view/subscribe", params)
        async with self._lock:
            self._subs.setdefault(session_id, set()).add(conn)
            conn.sessions.add(session_id)
        return result

    async def unsubscribe(self, conn, session_id):
        try:
            result = await self._msp.call("view/unsubscribe",
                                          {"sessionId": session_id})
        finally:
            async with self._lock:
                self._subs.get(session_id, set()).discard(conn)
                conn.sessions.discard(session_id)
        return result

    def cursor(self, session_id):
        return self._cursors.get(session_id, "")

    # -- fan-out ------------------------------------------------------------
    def on_notification(self, method, params):
        if isinstance(params, dict) and params.get("viewCursor") \
                and params.get("sessionId"):
            self._cursors[params["sessionId"]] = params["viewCursor"]
        # Track the running turn per session so steer/interrupt/cancel can
        # default their exact-turn target (turn/steer REQUIRES expectedTurnId).
        if isinstance(params, dict) and params.get("sessionId"):
            if method == "turn/started" and params.get("turnId"):
                self._turns[params["sessionId"]] = params["turnId"]
            elif method == "turn/completed":
                if self._turns.get(params["sessionId"]) == params.get("turnId"):
                    self._turns.pop(params["sessionId"], None)
        frame = map_notification_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        for conn in targets:
            conn.queue_frame(frame)

    def on_server_request(self, method, params):
        frame = map_server_request_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        # Approval/userInput must reach a surface: fall back to broadcast
        # when nobody subscribed to the session yet.
        if not targets and method in ("approval/request", "userInput/request"):
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(frame)

    def _targets_for(self, session_id):
        if session_id:
            subs = self._subs.get(session_id, set())
            if subs:
                return list(subs)
            # Session-scoped events also go to conns with no subscription yet
            # (fresh clients waiting for their first subscribe).
            unattached = [c for c in self._conns if not c.sessions]
            return unattached
        return list(self._conns)

    # -- WS request dispatch ------------------------------------------------
    async def handle_client_message(self, conn, msg):
        """Route one decoded WS frame; returns the reply frame (or None)."""
        if not isinstance(msg, dict):
            return {"type": "result", "ok": False,
                    "error": {"message": "frame must be a JSON object"}}
        rid = msg.get("id")
        mtype = msg.get("type", "")

        def reply(ok, **kw):
            frame = {"type": "result", "ok": ok}
            if rid is not None:
                frame["id"] = rid
            frame.update(kw)
            return frame

        try:
            if mtype == "prompt":
                return reply(True, result=await self._do_prompt(conn, msg))
            if mtype == "new":
                return reply(True, result=await self._do_new(conn, msg))
            if mtype == "list":
                p = {}
                for k in ("cursor", "limit", "updatedAfter", "workspaceRoot"):
                    if msg.get(k) is not None:
                        p[k] = msg[k]
                if msg.get("filter") is not None:
                    p["filter"] = msg["filter"]
                result = await self._msp.call("session/list", p)
                return reply(True, result=flag_missing_workspaces(result))
            if mtype == "resume":
                try:
                    result = await self._msp.command(
                        "session/resume",
                        _pick(msg, ("sessionId", "cursor", "excludeItems",
                                    "history")))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    result = await self._resume_fallback(msg.get("sessionId"))
                self._attach(conn, msg.get("sessionId"))
                return reply(True, result=result)
            if mtype == "read":
                try:
                    return reply(True, result=await self._msp.call(
                        "session/read",
                        _pick(msg, ("sessionId", "excludeItems"))))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    # Read-only fallback: same metadata, no attach.
                    return reply(True, result=await self._resume_fallback(
                        msg.get("sessionId")))
            if mtype == "fork":
                result = await self._msp.command(
                    "session/fork",
                    _pick(msg, ("sessionId", "cutPoint", "excludeItems")))
                return reply(True, result=result)
            if mtype == "rename":
                result = await self._msp.command(
                    "session/rename", _pick(msg, ("sessionId", "name")))
                # Explicit renames take precedence over the default: the
                # session leaves the auto-name set, present or future.
                self._auto_name_pending.discard(msg.get("sessionId"))
                return reply(True, result=result)
            if mtype == "delete":
                return reply(True, result=await self._msp.command(
                    "session/delete", _pick(msg, ("sessionId",))))
            if mtype == "interrupt":
                return reply(True, result=await self._msp.command(
                    "turn/interrupt",
                    _pick(msg, ("sessionId", "turnId", "retract"))))
            if mtype == "cancel":
                return reply(True, result=await self._msp.command(
                    "turn/cancel", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "steer":
                p = _pick(msg, ("sessionId", "expectedTurnId", "reasoningEffort"))
                p["input"] = build_turn_input(msg.get("text", ""),
                                              msg.get("images"))
                # turn/steer requires the exact running turn; default it from
                # the last turn/started we fanned out when the client omits it.
                if not p.get("expectedTurnId"):
                    tracked = self._turns.get(p.get("sessionId", ""))
                    if tracked:
                        p["expectedTurnId"] = tracked
                    else:
                        return reply(False, error={
                            "message": "steer needs the running turn: no turn "
                                       "is tracked for this session (send "
                                       "expectedTurnId or wait for "
                                       "turn/started)"})
                return reply(True, result=await self._msp.command("turn/steer", p))
            if mtype == "unqueue":
                return reply(True, result=await self._msp.command(
                    "turn/unqueue", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "approve":
                return reply(True, result=await self._msp.command(
                    "approval/decide",
                    _pick(msg, ("sessionId", "approvalId", "choiceId",
                                "requirementId", "feedback"))))
            if mtype == "answer":
                return reply(True, result=await self._msp.command(
                    "userInput/answer",
                    _pick(msg, ("sessionId", "userInputId", "answers"))))
            if mtype == "subscribe":
                try:
                    result = await self.subscribe(conn, msg["sessionId"],
                                                  msg.get("after"))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    # Memory-only hosts stream nothing anyway; attach
                    # locally so live routing still works if events flow.
                    self._attach(conn, msg["sessionId"])
                    result = {"viewCursor": self.cursor(msg["sessionId"]),
                              "replay": "unserved_by_host"}
                return reply(True, result=result)
            if mtype == "unsubscribe":
                result = await self.unsubscribe(conn, msg["sessionId"])
                return reply(True, result=result)
            if mtype == "page":
                p = _pick(msg, ("sessionId", "cursor", "anchor", "direction"))
                p["limit"] = msg.get("limit", 100)
                if p.get("direction") is not None \
                        and p["direction"] not in VALID_PAGE_DIRECTIONS:
                    return reply(False, error={
                        "message": "direction must be one of "
                                   f"{sorted(VALID_PAGE_DIRECTIONS)}"})
                try:
                    return reply(True, result=await self._msp.call("view/page", p))
                except MspError as e:
                    if e.code != METHOD_NOT_FOUND:
                        raise
                    return reply(False, error={
                        "message": "view/page is not served by this host; "
                                   "history paging unavailable",
                        "code": METHOD_NOT_FOUND})
            if mtype == "models":
                p = {}
                if msg.get("sessionId"):
                    p["sessionId"] = msg["sessionId"]
                return reply(True, result=await self._msp.call("model/list", p))
            if mtype == "setModel":
                p = {"sessionId": msg["sessionId"], "model": msg["model"]}
                return reply(True, result=await self._msp.command(
                    "session/setModel", p))
            if mtype == "setApprovalMode":
                mode = msg.get("mode", "")
                if mode in FORBIDDEN_APPROVAL_MODES:
                    return reply(False, error={
                        "message": f"approval mode {mode!r} is disabled "
                                   "on this bridge"})
                if mode not in VALID_APPROVAL_MODES:
                    return reply(False, error={
                        "message": f"unknown approval mode {mode!r}; "
                                   f"want one of {sorted(VALID_APPROVAL_MODES)} "
                                   "(allowAll is disabled on this bridge)"})
                return reply(True, result=await self._msp.command(
                    "session/setApprovalMode",
                    {"sessionId": msg["sessionId"], "mode": mode}))
            if mtype == "setEffort":
                effort = msg.get("reasoningEffort", "")
                if effort not in VALID_REASONING_EFFORTS:
                    return reply(False, error={
                        "message": f"unknown reasoning effort {effort!r}; "
                                   f"want one of {sorted(VALID_REASONING_EFFORTS)}"})
                return reply(True, result=await self._msp.command(
                    "session/setReasoningEffort",
                    {"sessionId": msg["sessionId"],
                     "reasoningEffort": effort}))
            if mtype == "skills":
                return reply(True, result=await self._msp.call(
                    "skill/list", {"sessionId": msg["sessionId"]}))
            if mtype == "readOutput":
                return reply(True, result=await self._msp.call(
                    "item/readOutput",
                    _pick(msg, ("sessionId", "itemId", "outputRef",
                                "offsetBytes", "lengthBytes"))))
            if mtype == "mcp":
                # No mcp/* methods exist on MSP v1 (stable+experimental):
                # serve the local settings.json inventory instead.
                return reply(True, result=read_mcp_servers())
            if mtype == "browse":
                # Filesystem explorer for the + dialog: list one
                # directory; empty/missing path opens the server's $HOME.
                return reply(True, result=browse_dir(msg.get("path")))
            if mtype == "compact":
                return reply(True, result=await self._msp.command(
                    "session/compact", _pick(msg, ("sessionId", "turnId"))))
            if mtype == "usage":
                return reply(True, result=await self._msp.call("usage/read", {}))
            if mtype == "pending":
                return reply(True, result=await self._msp.call(
                    "approval/listPending", {"sessionId": msg["sessionId"]}))
            if mtype == "githubRepos":
                # Repo picker rows, served live from `gh` (never cached).
                return reply(True, result={
                    "repos": await run_gh_list(
                        msg.get("search"), msg.get("limit"),
                        gh_bin=self._gh_bin),
                })
            if mtype == "githubClone":
                # Shallow-clone into workspaces/<sessionId>/repo/, admitted
                # async: the reply is instant ({accepted, opId, ...}) and
                # the outcome streams back as a githubCloneResult event, so
                # the connection stays responsive and githubCancel can
                # preempt a hanging clone. Progress streams as
                # githubCloneProgress events (no sessionId: global).
                return reply(True, result=self._do_github_clone(
                    conn, msg))
            if mtype == "githubCancel":
                return reply(True, result=self._cancel_github_op(
                    conn, msg.get("opId")))
            if mtype == "githubClean":
                # Delete one session's clone leaf after confirm (the UI
                # confirms; the bridge only enforces the leaf shape).
                return reply(True, result=self._clean_github_clone(
                    msg.get("sessionId")))
            if mtype == "pruneMissing":
                # Delete host sessions whose workspace dir is gone (the UI
                # previews with dryRun, then confirms before executing).
                return reply(True, result=await self._prune_missing(msg))
            if mtype == "githubOpen":
                # Clone + session/start rooted at the clone, admitted async
                # like githubClone (same events; the result carries the
                # started session). The browser records the dest in its
                # first-use allow list; picking the repo in the UI is the
                # consent, equivalent to the manual-root confirm.
                return reply(True, result=self._do_github_open(
                    conn, msg))
            if mtype == "ping":
                return reply(True, result={"pong": True})
            return reply(False, error={"message": f"unknown type {mtype!r}"})
        except KeyError as e:
            return reply(False, error={"message": f"missing field: {e}"})
        except ValueError as e:
            return reply(False, error={"message": str(e)})
        except Exception as e:  # MspError and friends -> error reply
            code = getattr(e, "code", None)
            err = {"message": str(e)}
            if code is not None:
                err["code"] = code
            data = getattr(e, "data", None)
            if data is not None:
                err["data"] = data
            return reply(False, error=err)

    async def _do_prompt(self, conn, msg):
        session_id = msg.get("sessionId")
        if not session_id:
            started = await self._do_new(conn, msg)
            session_id = started["session"]["sessionId"]
        p = {"sessionId": session_id,
             "input": build_turn_input(msg.get("text", ""), msg.get("images"))}
        if msg.get("ifBusy"):
            p["ifBusy"] = msg["ifBusy"]
        if msg.get("displayText"):
            p["displayText"] = msg["displayText"]
        if msg.get("reasoningEffort"):
            p["reasoningEffort"] = msg["reasoningEffort"]
        result = await self._msp.command("turn/start", p)
        result["sessionId"] = session_id
        # First prompt in a bridge-created (still unnamed) session names
        # it from the initial text; best effort, never fails the turn.
        await self._auto_name_from_prompt(session_id, msg.get("text"))
        return result

    async def _auto_name_from_prompt(self, session_id, text):
        """Rename one pending session to its initial-prompt prefix.

        No-op unless the session is still in the auto-name set (created
        unnamed via this bridge, never explicitly renamed). The rename
        itself is best effort: failures log and leave the turn result
        untouched.
        """
        if session_id not in self._auto_name_pending:
            return None
        self._auto_name_pending.discard(session_id)
        name = default_session_name(text)
        if not name:
            return None
        try:
            await self._msp.command(
                "session/rename",
                {"sessionId": session_id, "name": name})
        except Exception as e:
            LOG.warning("auto-name failed for %s: %s", session_id, e)
            return None
        return name

    async def _do_new(self, conn, msg):
        p = {}
        for k in ("approvalMode", "modelId", "providerId", "sessionId",
                  "workspaceRoot", "workspaceRoots", "config"):
            if msg.get(k) is not None:
                p[k] = msg[k]
        if p.get("approvalMode") in FORBIDDEN_APPROVAL_MODES:
            raise ValueError("that approvalMode is disabled on this bridge")
        # Client-supplied roots are validated (must exist; nothing is
        # created outside the base). Auto-created workspaces below skip
        # this — they are made by session_workspace_dir.
        if msg.get("workspaceRoot") is not None:
            p["workspaceRoot"] = validate_manual_root(msg.get("workspaceRoot"))
        if isinstance(msg.get("workspaceRoots"), list):
            p["workspaceRoots"] = [validate_manual_root(r)
                                   for r in msg.get("workspaceRoots")]
        # Default workspace: every new session gets its own directory under
        # the base, unless the client named its own root(s). A client-made
        # sessionId is minted here when absent so the dir exists pre-start.
        if self._workspace_base and p.get("workspaceRoot") is None \
                and p.get("workspaceRoots") is None:
            sid = p.get("sessionId") or uuid7()
            wsdir = session_workspace_dir(self._workspace_base, sid)
            p["sessionId"] = sid
            p["workspaceRoot"] = wsdir
        # mcpAttach: resolve settings.json server names bridge-side (secrets
        # never cross the browser boundary); explicit config.mcpServers wins.
        attach = msg.get("mcpAttach")
        attached = []
        if attach:
            if not isinstance(attach, list) or not all(
                    isinstance(n, str) for n in attach):
                raise ValueError("mcpAttach must be a list of server names")
            built, warnings = build_session_mcp_config(attach)
            if warnings:
                LOG.warning("mcpAttach warnings: %s", "; ".join(warnings))
            if built.get("mcpServers"):
                merged = dict(built["mcpServers"])
                explicit = (p.get("config") or {}).get("mcpServers") or {}
                if isinstance(explicit, dict):
                    merged.update(explicit)
                p["config"] = dict(p.get("config") or {})
                p["config"]["mcpServers"] = merged
                attached = sorted(merged)
        result = await self._msp.command("session/start", p)
        self._attach(conn, result["session"]["sessionId"])
        # Hosts that leave the name empty mark the session for default
        # naming on its first prompt; already-named sessions never enter.
        sid = result["session"]["sessionId"]
        if result["session"].get("name"):
            self._auto_name_pending.discard(sid)
        else:
            self._auto_name_pending.add(sid)
        if attached:
            result["mcpAttached"] = attached
        if p.get("workspaceRoot"):
            result["workspaceRoot"] = p["workspaceRoot"]
        return result

    # -- GitHub clones (gh-only v1) -----------------------------------------
    def _clone_leaf(self, session_id):
        """`workspaces/<sid>/repo` leaf for a clone: contained and reusable.

        The leaf is created only by cloning, so a non-git residue is always
        a previous attempt's leftover: remove it so retries start clean.
        Raises ValueError (bad id / no workspace base) or GithubError
        (dest_exists when this session already holds a clone).
        """
        if not self._workspace_base:
            raise ValueError("github clone needs a workspace base "
                             "(bridge started with --workspace-base \"\")")
        wsdir = session_workspace_dir(self._workspace_base, session_id or "")
        leaf = Path(wsdir) / "repo"
        if leaf.is_dir() and (leaf / ".git").is_dir():
            raise GithubError(
                "dest_exists",
                f"session {session_id} already holds a clone at {leaf}")
        if leaf.exists() or leaf.is_symlink():
            shutil.rmtree(leaf, ignore_errors=True)
        return str(leaf)

    def _emit_clone_progress(self, conn, op_id, full_name, phase, line=None):
        """Push one githubCloneProgress event (global: no sessionId)."""
        try:
            params = {"opId": op_id, "fullName": full_name, "phase": phase}
            if line is not None:
                params["line"] = line
            conn.queue_frame({"type": "event",
                              "method": "githubCloneProgress",
                              "params": params})
        except Exception:
            LOG.warning("clone progress frame dropped for %s", full_name,
                        exc_info=True)

    def _check_op_id(self, op_id):
        if not isinstance(op_id, str) or not op_id.strip() \
                or len(op_id) > 128:
            raise ValueError("opId must be a 1..128 char string")
        return op_id.strip()

    def _launch_clone(self, conn, full_name, dest, op_id, session_id,
                      open_opts=None):
        """Start the clone as a background task; reply accepted at once.

        The WS read loop awaits each handler serially, so awaiting a
        minutes-long clone inline would wedge the connection and make
        githubCancel unprocessable. Instead the outcome streams back as a
        global githubCloneResult event (ok + result, or ok False + error).
        Returns the accepted receipt; raises ValueError/GithubError on bad
        input before anything is launched.
        """
        self._emit_clone_progress(conn, op_id, full_name, "started")
        task = asyncio.create_task(
            self._clone_task(conn, full_name, dest, op_id, session_id,
                             open_opts),
            name=f"github-clone-{op_id}")
        self._github_ops[op_id] = [task, conn, full_name]
        return {"accepted": True, "opId": op_id, "fullName": full_name,
                "dest": dest, "sessionId": session_id}

    def _finish_op(self, op_id, task):
        """Take terminal-event ownership iff this task still owns the op."""
        rec = self._github_ops.get(op_id)
        if rec is not None and rec[0] is task:
            self._github_ops.pop(op_id, None)
            return rec
        return None

    async def _clone_task(self, conn, full_name, dest, op_id, session_id,
                          open_opts):
        """Background clone body: progress, result event, cleanup."""
        me = asyncio.current_task()
        try:
            await run_gh_clone(
                full_name, dest, gh_bin=self._gh_bin,
                on_line=lambda line: self._emit_clone_progress(
                    conn, op_id, full_name, "progress", line))
            self._emit_clone_progress(conn, op_id, full_name, "completed")
            if open_opts is None:
                result = {"fullName": full_name, "dest": dest,
                          "sessionId": session_id, "opId": op_id}
            else:
                result = await self._open_cloned(
                    conn, full_name, dest, session_id, op_id, open_opts)
        except asyncio.CancelledError:
            # Whoever cancelled already owns the terminal event (see
            # _cancel_github_op); only emit when this task still owns it.
            if self._finish_op(op_id, me) is None:
                raise
            self._emit_clone_progress(conn, op_id, full_name, "cancelled")
            self._emit_clone_result(
                conn, op_id, False,
                error={"code": "clone_cancelled",
                       "message": f"clone of {full_name} cancelled"})
            return
        except GithubError as e:
            if self._finish_op(op_id, me) is None:
                return
            self._emit_clone_result(
                conn, op_id, False,
                error={"code": e.code, "message": str(e)})
            return
        except Exception as e:  # MspError from session/start and friends
            if self._finish_op(op_id, me) is None:
                return
            err = {"message": str(e)}
            if getattr(e, "code", None) is not None:
                err["code"] = e.code
            self._emit_clone_result(conn, op_id, False, error=err)
            return
        if self._finish_op(op_id, me) is None:
            return  # a canceller already emitted the terminal event
        self._emit_clone_result(conn, op_id, True, result=result)

    def _emit_clone_result(self, conn, op_id, ok, result=None, error=None):
        """Push the terminal githubCloneResult event (global)."""
        try:
            params = {"opId": op_id, "ok": ok}
            if result is not None:
                params["result"] = result
            if error is not None:
                params["error"] = error
            conn.queue_frame({"type": "event",
                              "method": "githubCloneResult",
                              "params": params})
        except Exception:
            LOG.warning("clone result frame dropped for op %s", op_id,
                        exc_info=True)

    async def _open_cloned(self, conn, full_name, dest, session_id, op_id,
                           open_opts):
        """session/start rooted at a fresh clone (open half of githubOpen)."""
        new_msg = {"workspaceRoot": dest, "sessionId": session_id}
        if open_opts.get("mcpAttach") is not None:
            new_msg["mcpAttach"] = open_opts.get("mcpAttach")
        result = await self._do_new(conn, new_msg)
        name = open_opts.get("name")
        if isinstance(name, str) and name.strip():
            await self._msp.command(
                "session/rename", {"sessionId": session_id,
                                   "name": name.strip()})
            if isinstance(result.get("session"), dict):
                result["session"]["name"] = name.strip()
            # Explicit name: the default fallback no longer applies.
            self._auto_name_pending.discard(session_id)
        result["fullName"] = full_name
        result["opId"] = op_id
        return result

    def _cancel_github_op(self, conn, op_id):
        """Cancel a running clone by opId (raises on unknown/finished op).

        The canceller takes terminal-event ownership and emits
        cancelled + clone_cancelled itself: a task cancelled before its
        first step never runs its body, so waiting on the task to report
        would leak the op with no terminal event.
        """
        op_id = self._check_op_id(op_id or "")
        rec = self._github_ops.get(op_id)
        task = rec[0] if rec else None
        if rec is None or task.done():
            if rec is not None and rec[0] is task:
                self._github_ops.pop(op_id, None)
            raise GithubError(
                "unknown_op",
                f"clone op {op_id!r} already finished"
                if task is not None and task.done() else
                f"no running clone op {op_id!r}")
        self._github_ops.pop(op_id, None)
        task.cancel()
        # Terminal events go to the originating conn (it holds the pending
        # UI state); the canceller gets the {cancelled} reply. Usually both
        # are the same client.
        origin = rec[1]
        self._emit_clone_progress(origin, op_id, rec[2], "cancelled")
        self._emit_clone_result(
            origin, op_id, False,
            error={"code": "clone_cancelled",
                   "message": f"clone of {rec[2]} cancelled"})
        return {"cancelled": True, "opId": op_id}

    def _do_github_clone(self, conn, msg):
        """Admit a clone into `workspaces/<sid>/repo/` (no MSP session).

        The caller then roots a session at `dest` via `new` (which runs the
        usual first-use confirm) or uses `githubOpen` for the combined step.
        """
        full_name = validate_fullname(msg.get("fullName"))
        sid = msg.get("sessionId") or uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid)
        return self._launch_clone(conn, full_name, dest, op_id, sid)

    def _do_github_open(self, conn, msg):
        """Admit a clone + `session/start` rooted at the clone."""
        full_name = validate_fullname(msg.get("fullName"))
        sid = uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid)
        return self._launch_clone(conn, full_name, dest, op_id, sid,
                                  {"mcpAttach": msg.get("mcpAttach"),
                                   "name": msg.get("name")})

    async def _prune_missing(self, msg):
        """Delete host sessions whose workspace dir is gone (scoped, safe).

        Only sessions rooted under this bridge's workspace base are
        candidates: external manual roots may be transiently missing (an
        unmounted drive is not a deletion), and removing those sessions
        would destroy their transcripts. dryRun previews without deleting.
        """
        dry = bool(msg.get("dryRun"))
        try:
            limit = max(1, min(1000, int(msg.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        listed = await self._msp.call("session/list", {"limit": limit})
        sessions = listed.get("sessions") if isinstance(listed, dict) else None
        if not isinstance(sessions, list):
            sessions = []
        flag_missing_workspaces({"sessions": sessions})
        base = Path(self._workspace_base).resolve() \
            if self._workspace_base else None
        candidates, outside = [], 0
        for s in sessions:
            if not isinstance(s, dict) or not s.get("workspaceMissing"):
                continue
            try:
                contained = base is not None and \
                    base in Path(s.get("workspaceRoot") or "").resolve().parents
            except (OSError, ValueError):
                contained = False
            if contained:
                candidates.append(s)
            else:
                outside += 1
        brief = lambda s: {"sessionId": s.get("sessionId"),
                           "name": s.get("name"),
                           "workspaceRoot": s.get("workspaceRoot")}
        if dry:
            return {"dryRun": True,
                    "candidates": [brief(s) for s in candidates],
                    "outsideBase": outside}
        deleted, failed = [], []
        for s in candidates:
            sid = s.get("sessionId")
            error = None
            for attempt in range(1 + len(_PRUNE_RETRY_DELAYS_S)):
                try:
                    await self._msp.command("session/delete",
                                            {"sessionId": sid})
                    error = None
                    break
                except Exception as e:
                    error = e
                    if _transient_delete_error(e) \
                            and attempt < len(_PRUNE_RETRY_DELAYS_S):
                        LOG.info("prune %s busy, retrying in %ss "
                                 "(attempt %d)", sid,
                                 _PRUNE_RETRY_DELAYS_S[attempt], attempt + 1)
                        await asyncio.sleep(_PRUNE_RETRY_DELAYS_S[attempt])
                    else:
                        break
            if error is None:
                deleted.append(brief(s))
            else:
                err = {"sessionId": sid, "message": str(error)}
                if getattr(error, "code", None) is not None:
                    err["code"] = error.code
                failed.append(err)
            await asyncio.sleep(_PRUNE_PAUSE_S)
        return {"dryRun": False, "deleted": deleted, "failed": failed,
                "outsideBase": outside}

    def _clean_github_clone(self, session_id):
        """Delete one session's `repo/` leaf (raises outside the leaf)."""
        sid = session_id or ""
        if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
                or sid in (".", ".."):
            raise ValueError(f"unsafe sessionId for github clean: {sid!r}")
        if not self._workspace_base:
            raise ValueError("github clean needs a workspace base")
        root = Path(self._workspace_base).resolve()
        leaf = (root / sid / "repo").resolve()
        if leaf.name != "repo" or leaf.parent.name != sid \
                or root not in leaf.parents:
            raise ValueError(f"refusing to clean outside clone leaf: {sid!r}")
        if not leaf.is_dir():
            return {"removed": False, "dest": str(leaf)}
        shutil.rmtree(leaf)
        try:  # drop the session dir too when the clone was all it held
            leaf.parent.rmdir()
        except OSError:
            pass
        return {"removed": True, "dest": str(leaf)}

    def _attach(self, conn, session_id):
        if session_id:
            self._subs.setdefault(session_id, set()).add(conn)
            conn.sessions.add(session_id)

    async def _resume_fallback(self, session_id):
        """Open a session when the host serves no session/resume
        (--no-session-log hosts; durable hosts serve it).

        Metadata comes from session/list (whose sessionId filter the host
        honors); history is honestly `none` — the host offers no transcript
        read-back, so the UI shows metadata and streams new turns live.
        Shape matches SessionResumeResult so clients don't branch.
        """
        if not session_id:
            raise ValueError("resume needs sessionId")
        listed = await self._msp.call(
            "session/list",
            {"filter": {"sessionId": {"anyOf": [session_id]}}, "limit": 5})
        sessions = listed.get("sessions", [])
        sess = next((s for s in sessions if s.get("sessionId") == session_id),
                    None)
        if sess is None:
            raise MspError(-32000, f"unknown session {session_id}")
        pending = {"approvals": [], "userInputs": []}
        try:
            pending = await self._msp.call(
                "approval/listPending", {"sessionId": session_id})
        except MspError:
            pass  # best effort; the UI re-fetches via /pending anyway
        return {
            "session": sess,
            "history": {"items": None, "mode": "none",
                        "noneReason": "resume_unserved_by_host",
                        "snapshot": None},
            "pendingRequests": [
                {"kind": "approval", "approvalId": a.get("approvalId")}
                for a in pending.get("approvals", [])
                if a.get("approvalId")
            ] + [
                {"kind": "userInput", "userInputId": u.get("userInputId")}
                for u in pending.get("userInputs", [])
                if u.get("userInputId")
            ],
            "viewCursor": self.cursor(session_id),
            "fallback": "resume_unserved_by_host",
        }


def _pick(msg, keys):
    out = {}
    for k in keys:
        if k in msg and msg[k] is not None:
            out[k] = msg[k]
    return out
