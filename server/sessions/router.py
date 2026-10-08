"""Session routing: WS connections <-> MSP sessionIds, cursor store, dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from server.github import GithubError, filter_repos, is_safe_leaf_name, merge_branch_names, merge_repo_rows, repo_dir_name, run_gh_branches, run_gh_clone, run_gh_list, validate_branch, validate_fullname
from server.msp import MspError, uuid7
from server.sessions.github_parts import has_seeded_instructions, seed_github_instructions, seed_session_agents
from server.sessions.mapping import METHOD_NOT_FOUND, VALID_APPROVAL_MODES, VALID_PAGE_DIRECTIONS, VALID_REASONING_EFFORTS, _retained_refused, build_turn_input, default_session_name, map_notification_to_ws, map_server_request_to_ws, notification_session_id, pick_approve_once_choice
from server.sessions.mcp import build_session_mcp_config, read_mcp_servers
from server.sessions.orders import DEFAULT_THEMES_DIR, ORDERS_DIRNAME, ORDERS_FILENAME, ORDERS_MAX_BYTES, ORDERS_MAX_COUNT, RESTART_DELAY_DEFAULT, RESTART_DELAY_MAX, RESTART_DELAY_MIN, _read_receipt, _write_receipt, seed_orders_skill, validate_orders
from server.sessions.policy import ALLOWED_COMMANDS_FILE, load_allowed_commands, shell_auto_allowed
from server.sessions.themes import THEME_KEYS, THEME_NAME_RE, theme_color_error
from server.sessions.workspaces import SAFE_SESSION_ID, _workspace_under_base, browse_dir, delete_session_files, session_workspace_dir, validate_manual_root

LOG = logging.getLogger("web_muse.sessions")


class SessionRouter:
    """Tracks WS<->sessionId subscriptions and last-seen view cursors."""

    def __init__(self, msp, workspace_base=None, gh_bin="gh",
                 sessions_base=None, allowed_commands_path=None,
                 themes_dir=None, on_restart=None):
        self._msp = msp
        # Base dir for per-session workspaces (None = send no workspaceRoot).
        self._workspace_base = str(workspace_base) if workspace_base else None
        # On-disk session store root for file-based deletes (None = the
        # default ~/.local/share/muse/sessions).
        self._sessions_base = str(sessions_base) if sessions_base else None
        # Sessions this bridge removed from disk: hidden from list/resume
        # until the serve host reindexes (it keeps listing them from
        # memory while running).
        self._deleted_sids = set()
        # Sessions started through this bridge (any path: new, prompt,
        # githubOpen): the UI lists them before TUI-created ones.
        # Persisted as one root-level file in the session store (beside,
        # never inside, session dirs) so a bridge restart keeps the
        # grouping. sessions_base=None keeps it memory-only — which is
        # also what makes the test suite hermetic (no home-dir writes).
        # Production passes an explicit store dir (see deploy notes).
        self._bridge_sids = set()
        self._bridge_sids_file = (
            str(Path(self._sessions_base) / ".web-muse-bridge-sids.json")
            if self._sessions_base else None)
        self._load_bridge_sids()
        self._gh_bin = gh_bin or "gh"
        # Shell auto-approve policy: validated server-side config
        # (allowed_commands_path, defaulting to the shipped file), never
        # client-supplied. Corrupt files fall back to builtins (logged).
        # The path is kept: approved policy orders rewrite this file and
        # reload it, so no restart is needed for order-driven updates.
        self._allowed_path = (allowed_commands_path
                              or ALLOWED_COMMANDS_FILE)
        self._allowed, _ac_warnings = load_allowed_commands(
            self._allowed_path)
        for _w in _ac_warnings:
            LOG.warning("%s", _w)
        # Agent orders channel: sessionId -> workspace root (for locating
        # `.web-muse/orders.json`) and staged orders awaiting a human's
        # ordersDecide (policy updates, saved themes, backend restarts),
        # keyed (sessionId, orderId).
        self._workspace_roots = {}
        self._pending_orders = {}
        # Mid-turn orders polling: sessionId -> (mtime_ns, size) of
        # orders.json at the last check. Streamed turn events only
        # schedule a check when the stamp changed (see _poll_orders).
        self._orders_seen = {}
        # Approved-restart executor: set by server/main.py to the real
        # graceful-restart coroutine; None in tests unless injected, in
        # which case an approved restart is recorded but not executed.
        self._on_restart = on_restart
        # Destination for approved theme.save orders (None = the shipped
        # web/themes dir). Kept as a path: approved saves write one
        # <name>.json file there, which is what bare `/theme` lists.
        # Tests pass a tmp dir to stay hermetic (same pattern as the
        # allowed-commands path above).
        self._themes_dir = (str(themes_dir) if themes_dir is not None
                            else str(DEFAULT_THEMES_DIR))
        # Known repos/branches: every listing still shells out to `gh`
        # for new entries, then merges them in — so repeat picker opens
        # keep working (stale) when `gh` hiccups and accumulate rows
        # across `--limit` pages. Per-router (not global), which keeps
        # the test suite hermetic.
        self._known_repos = []
        self._known_branches = {}
        # Clone opId -> [task, conn, fullName]. Terminal-event ownership
        # goes to whoever pops the record first (finishing task or
        # canceller), so exactly one githubCloneResult fires even when a
        # cancel lands before the task's first step (a coroutine cancelled
        # that early never runs its body, not even `finally`).
        self._github_ops = {}
        # Sessions opened via githubOpen: shell approvals matching
        # GH_AUTO_ALLOW_RE are decided bridge-side (gh PR flow never
        # parks on an unanswered prompt). Scoped here — not global — so
        # other sessions keep prompting as before.
        self._gh_auto_sids = set()
        # Subagent auto-approve (Settings toggle, default off): when
        # enabled, a child approval projected onto a parent session
        # whose effective mode is allowAll is decided bridge-side with
        # the one-shot approve choice instead of opening a card. The
        # host never propagates the parent's mode to children (each
        # spawn resolves its own profile), so without this an allowAll
        # session still parks on every subagent prompt. Memory-only and
        # global: every client re-syncs its (persisted) pick on
        # (re)connect, so the last writer wins across tabs.
        self._subagent_auto_approve = False
        # Last-known effective approval mode per sessionId (session/start
        # request, setApprovalMode results, resume/read/list rows, and
        # session/approvalModeChanged events). Missing means unknown —
        # never assumed allowAll.
        self._approval_modes = {}
        self._subs = {}   # sessionId -> set of ClientConnection
        # Conns that ever opened a session (any attach path). The fan-out
        # fallback below is for fresh tabs waiting on their first
        # subscribe — a tab that attached and then unsubscribed everything
        # explicitly left, so it must not receive the fallback again.
        self._attached_once = set()
        # Sessions this bridge created while still unnamed: the first
        # prompt names them from its initial text (default fallback).
        # An explicit rename drops the id, so user/host names always win.
        self._auto_name_pending = set()
        self._cursors = {}  # sessionId -> last viewCursor seen
        self._turns = {}  # sessionId -> running turnId (from turn/started)
        self._conns = set()
        self._lock = asyncio.Lock()

    def _load_bridge_sids(self):
        """Restore the bridge-created set (never raises)."""
        if not self._bridge_sids_file:
            return
        try:
            raw = json.loads(Path(self._bridge_sids_file).read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, list):
            self._bridge_sids = {s for s in raw
                                 if isinstance(s, str) and s}

    def _save_bridge_sids(self):
        """Persist the bridge-created set (never raises)."""
        if not self._bridge_sids_file:
            return
        try:
            p = Path(self._bridge_sids_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(sorted(self._bridge_sids)))
        except OSError:
            LOG.warning("bridge sid store unwritable", exc_info=True)

    def _remember_bridge_sid(self, session_id):
        if session_id and session_id not in self._bridge_sids:
            self._bridge_sids.add(session_id)
            self._save_bridge_sids()

    # -- connection registry ------------------------------------------------
    def add_conn(self, conn):
        self._conns.add(conn)

    def drop_conn(self, conn):
        self._conns.discard(conn)
        self._attached_once.discard(conn)
        for members in self._subs.values():
            members.discard(conn)

    # -- subscriptions ------------------------------------------------------
    async def subscribe(self, conn, session_id, after=None):
        params = {"sessionId": session_id}
        if after:
            params["after"] = after
        result = await self._msp.call("view/subscribe", params)
        async with self._lock:
            self._attach(conn, session_id)
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
            sid = params["sessionId"]
            if method == "turn/started" and params.get("turnId"):
                self._turns[sid] = params["turnId"]
            if method == "turn/completed":
                if self._turns.get(sid) == params.get("turnId"):
                    self._turns.pop(sid, None)
                # Agent orders are checked after each turn, in the
                # background: the agent's file lands during the turn, so
                # completion is the final moment it can be complete.
                if sid in self._workspace_roots:
                    try:
                        asyncio.create_task(
                            self._check_orders(sid))
                    except RuntimeError:
                        pass  # no running loop (tests): caller checks directly
            elif sid in self._workspace_roots:
                # Mid-turn events (item/delta, ...): the orders file can
                # land minutes before completion, so poll it here too —
                # gated on change, so idle streams cost one stat each.
                self._poll_orders(sid)
        if method == "session/approvalModeChanged" \
                and isinstance(params, dict):
            self._note_approval_mode(params.get("sessionId"),
                                     params.get("mode"))
        frame = map_notification_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        for conn in targets:
            conn.queue_frame(frame)

    def on_server_request(self, method, params):
        if method == "approval/request" and self._auto_approve_gh_pr(params):
            return
        if method == "approval/request" \
                and self._auto_approve_subagent(params):
            return
        frame = map_server_request_to_ws(method, params)
        sid = notification_session_id(method, params)
        targets = self._targets_for(sid)
        # Approval/userInput must reach a surface: fall back to broadcast
        # when nobody subscribed to the session yet.
        if not targets and method in ("approval/request", "userInput/request"):
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(frame)

    def _auto_approve_gh_pr(self, params):
        """Decide a gh-PR approval for a GitHub-opened session (no UI card).

        Returns True when the approval was consumed: it matched the
        allowlist and an approve choice existed, so the decide was queued
        and a notice event fanned out instead of the approval card.
        Anything else returns False and the caller forwards the card
        unchanged (the agent parks until a human decides, as before).
        """
        if not isinstance(params, dict):
            return False
        sid = params.get("sessionId")
        if not sid or sid not in self._gh_auto_sids:
            return False
        subject = params.get("subject")
        if not shell_auto_allowed(subject, self._allowed):
            return False
        choice_id = pick_approve_once_choice(params.get("availableChoices"))
        if choice_id is None:
            LOG.warning("gh auto-approve: no approve choice for %s",
                        params.get("approvalId"))
            return False
        command = subject.get("command", "").strip()
        decide = {"sessionId": sid,
                  "approvalId": params.get("approvalId"),
                  "choiceId": choice_id,
                  # Race guard: must equal the request's current stage.
                  "requirementId": params.get("currentRequirementId")}
        try:
            asyncio.create_task(self._gh_auto_decide(decide, command))
        except RuntimeError:
            return False  # no running loop: fall through to the UI card
        notice = {"type": "event", "method": "githubAutoApproved",
                  "params": {"sessionId": sid,
                             "approvalId": params.get("approvalId"),
                             "command": command}}
        targets = self._targets_for(sid)
        if not targets:
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(notice)
        return True

    async def _gh_auto_decide(self, decide, command):
        """Send the queued approval/decide (all failures are logged)."""
        try:
            await self._msp.command("approval/decide", decide)
        except Exception:
            LOG.warning("gh auto-approve decide failed for %s (%s)",
                        decide.get("approvalId"), command, exc_info=True)
        else:
            LOG.info("gh auto-approved in session %s: %s",
                     decide.get("sessionId"), command)

    def _note_approval_mode(self, session_id, mode):
        """Record one session's effective approval mode (never raises).

        Unknown modes (and missing ids) are ignored: only a mode the
        host actually folded is ever trusted by _auto_approve_subagent.
        """
        if session_id and mode in VALID_APPROVAL_MODES:
            self._approval_modes[session_id] = mode

    def _note_session_obj(self, sess):
        """Record the mode a Session object carries, when it carries one.

        Index-derived session/list rows may omit approvalMode; loaded
        sessions (resume/read) fold {mode, source, lastCommandId}.
        Anything else is ignored (never raises).
        """
        if not isinstance(sess, dict):
            return
        folded = sess.get("approvalMode")
        if isinstance(folded, dict):
            self._note_approval_mode(sess.get("sessionId"),
                                     folded.get("mode"))

    def _auto_approve_subagent(self, params):
        """Decide a child approval on an allowAll parent (no UI card).

        Returns True when the approval was consumed: the toggle is on,
        the params carry subagentOrigin (parent-own approvals omit it),
        the parent's last-known effective mode is allowAll, and a
        one-shot approve choice exists — so the decide was queued and a
        notice event fanned out instead of the approval card. Anything
        else returns False and the caller forwards the card unchanged.
        """
        if not isinstance(params, dict):
            return False
        if not self._subagent_auto_approve:
            return False
        origin = params.get("subagentOrigin")
        if not isinstance(origin, dict):
            return False
        sid = params.get("sessionId")
        if not sid or self._approval_modes.get(sid) != "allowAll":
            return False
        choice_id = pick_approve_once_choice(params.get("availableChoices"))
        if choice_id is None:
            LOG.warning("subagent auto-approve: no approve choice for %s",
                        params.get("approvalId"))
            return False
        tool = params.get("toolName") or "tool"
        decide = {"sessionId": sid,
                  "approvalId": params.get("approvalId"),
                  "choiceId": choice_id,
                  # Race guard: must equal the request's current stage.
                  "requirementId": params.get("currentRequirementId")}
        try:
            asyncio.create_task(
                self._subagent_auto_decide(decide, tool))
        except RuntimeError:
            return False  # no running loop: fall through to the UI card
        notice = {"type": "event", "method": "subagentAutoApproved",
                  "params": {"sessionId": sid,
                             "approvalId": params.get("approvalId"),
                             "tool": tool,
                             "subagent": origin.get("subagentId")}}
        targets = self._targets_for(sid)
        if not targets:
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(notice)
        return True

    async def _subagent_auto_decide(self, decide, tool):
        """Send the queued approval/decide (all failures are logged)."""
        try:
            await self._msp.command("approval/decide", decide)
        except Exception:
            LOG.warning("subagent auto-approve decide failed for %s (%s)",
                        decide.get("approvalId"), tool, exc_info=True)
        else:
            LOG.info("subagent auto-approved in session %s: %s",
                     decide.get("sessionId"), tool)

    # -- Agent orders ------------------------------------------------------
    def _emit_orders_event(self, sid, method, params):
        """Fan one orders event out (session targets, else broadcast)."""
        frame = {"type": "event", "method": method, "params": params}
        targets = self._targets_for(sid)
        if not targets:
            targets = list(self._conns)
        for conn in targets:
            conn.queue_frame(frame)

    def _broadcast_orders_event(self, method, params):
        """Fan one orders event out to every connection.

        Used for bridge restarts: the restart drops *all* browser tabs,
        not just the ordering session's, so every client must see the
        card and the bridgeRestarting notice.
        """
        frame = {"type": "event", "method": method, "params": params}
        for conn in list(self._conns):
            conn.queue_frame(frame)

    def _poll_orders(self, sid):
        """Schedule a mid-turn orders check when the file looks new.

        Never raises: a missing file means nothing to do, and without
        a running loop (tests) the caller checks directly.
        """
        try:
            root = self._workspace_roots.get(sid)
            if not root:
                return
            st = (Path(root) / ORDERS_DIRNAME / ORDERS_FILENAME).stat()
            if (st.st_mtime_ns, st.st_size) == self._orders_seen.get(sid):
                return
            asyncio.create_task(self._check_orders(sid))
        except (OSError, RuntimeError):
            pass

    async def _check_orders(self, sid):
        """Validate one session's orders file and act (never raises)."""
        root = self._workspace_roots.get(sid)
        if not root:
            return
        odir = Path(root) / ORDERS_DIRNAME
        ofile = odir / ORDERS_FILENAME
        try:
            if not ofile.is_file():
                return
            st = ofile.stat()
            if st.st_size > ORDERS_MAX_BYTES:
                self._orders_seen[sid] = (st.st_mtime_ns, st.st_size)
                return
            payload = json.loads(ofile.read_text())
        except (OSError, ValueError) as e:
            LOG.warning("orders: unreadable file for session %s (%s)",
                        sid, e)
            return
        # Stamp what was actually read: a torn mid-write read leaves
        # the old stamp, so the next event retries instead of going
        # blind until the completion backstop.
        self._orders_seen[sid] = (st.st_mtime_ns, st.st_size)
        if isinstance(payload, dict) \
                and isinstance(payload.get("orders"), list) \
                and len(payload["orders"]) > ORDERS_MAX_COUNT:
            LOG.warning("orders: too many orders for session %s (%d)",
                        sid, len(payload["orders"]))
            return
        receipt = _read_receipt(odir)
        valid, rejected = validate_orders(payload, receipt)
        # Rechecks (mid-turn growth, completion backstop) must not
        # rewrite settled statuses: ids already in the receipt keep
        # whatever they reported; only new ids are recorded.
        rejected = {oid: reason for oid, reason in rejected.items()
                    if oid not in receipt}
        processed = {oid: {"status": "rejected", "reason": reason}
                     for oid, reason in rejected.items()}
        for oid, action, params in valid:
            if action == "theme.apply":
                colors = params["colors"]
                self._emit_orders_event(sid, "themeApply",
                                        {"sessionId": sid, "orderId": oid,
                                         "colors": colors})
                processed[oid] = {"status": "applied"}
                LOG.info("orders: theme.apply from session %s (%d "
                         "colors)", sid, len(colors))
            elif action == "theme.save":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._emit_orders_event(sid, "ordersPending",
                                        {"sessionId": sid, "orderId": oid,
                                         "action": action, "params": params,
                                         "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: theme.save staged for session %s "
                         "(awaiting human)", sid)
            elif action == "allowedCommands.update":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._emit_orders_event(sid, "ordersPending",
                                        {"sessionId": sid, "orderId": oid,
                                         "action": action, "params": params,
                                         "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: policy update staged for session %s "
                         "(awaiting human)", sid)
            elif action == "bridge.restart":
                self._pending_orders[(sid, oid)] = {
                    "action": action, "params": params}
                self._broadcast_orders_event(
                    "ordersPending",
                    {"sessionId": sid, "orderId": oid,
                     "action": action, "params": params,
                     "needsConfirm": True})
                processed[oid] = {"status": "needsConfirm"}
                LOG.info("orders: bridge restart staged for session %s "
                         "(awaiting human)", sid)
        if processed:
            _write_receipt(odir, processed)

    def _mark_order(self, sid, oid, status, reason=""):
        """Record one order's terminal status in its session receipt."""
        root = self._workspace_roots.get(sid)
        if not root:
            return
        entry = {"status": status}
        if reason:
            entry["reason"] = reason
        _write_receipt(Path(root) / ORDERS_DIRNAME, {oid: entry})

    async def _decide_order(self, msg):
        """Run a human's ordersDecide on a staged order."""
        sid = msg.get("sessionId")
        oid = msg.get("orderId")
        approved = msg.get("approved")
        if not isinstance(approved, bool):
            raise ValueError("approved must be true or false")
        rec = self._pending_orders.pop((sid, oid), None)
        if rec is None:
            raise ValueError(f"unknown order {oid!r} for session {sid!r}")
        if not approved:
            self._mark_order(sid, oid, "denied")
            LOG.info("orders: %s denied for session %s",
                     rec.get("action", "order"), sid)
            return {"orderId": oid, "approved": False}
        if rec.get("action") == "theme.save":
            self._apply_theme_save(sid, oid, rec["params"])
        elif rec.get("action") == "bridge.restart":
            # Receipt first: the restart kills this process, so the
            # agent's receipt must already say approved when we go down.
            self._mark_order(sid, oid, "approved")
            self._broadcast_orders_event(
                "bridgeRestarting",
                {"sessionId": sid, "orderId": oid,
                 "params": rec["params"]})
            self._schedule_restart(rec["params"])
            LOG.info("orders: bridge restart approved for session %s "
                     "(restarting)", sid)
            return {"orderId": oid, "approved": True, "restarting": True}
        else:
            self._apply_policy_update(sid, oid, rec["params"])
        return {"orderId": oid, "approved": True}

    def _schedule_restart(self, params):
        """Queue the approved restart after its grace delay (never raises).

        The delay lets this approval's reply frame and the
        bridgeRestarting event flush to browsers before the process
        exits; without it the approver would see only a dropped socket.
        """
        delay = params.get("delaySeconds", RESTART_DELAY_DEFAULT) \
            if isinstance(params, dict) else RESTART_DELAY_DEFAULT
        try:
            delay = int(delay)
        except (TypeError, ValueError):
            delay = RESTART_DELAY_DEFAULT
        delay = min(max(delay, RESTART_DELAY_MIN), RESTART_DELAY_MAX)
        try:
            asyncio.create_task(self._run_restart_later(delay))
        except RuntimeError:
            LOG.warning("orders: no running loop; restart not scheduled")

    async def _run_restart_later(self, delay):
        """Sleep out the grace delay, then run the restart hook."""
        try:
            await asyncio.sleep(delay)
            hook = self._on_restart
            if hook is None:
                LOG.error("orders: bridge restart approved but no "
                          "restart hook is configured; staying up")
                return
            if asyncio.iscoroutinefunction(hook):
                await hook(delay)
            else:
                hook(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("orders: bridge restart failed")

    def _list_orders(self, sid):
        """All staged (needsConfirm) orders, optionally session-scoped.

        Lets fresh or reconnected clients re-render cards that were
        fanned out as ordersPending events before they attached.
        """
        out = []
        for (psid, oid), rec in self._pending_orders.items():
            if sid and psid != sid:
                continue
            out.append({"sessionId": psid, "orderId": oid,
                        "action": rec.get("action"),
                        "params": rec.get("params", {}),
                        "needsConfirm": True})
        return {"orders": out}

    def _apply_theme_save(self, sid, oid, params):
        """Write an approved theme.save order to web/themes/<name>.json."""
        name = params.get("name") if isinstance(params, dict) else None
        colors = params.get("colors") if isinstance(params, dict) else None
        if not isinstance(name, str) or not THEME_NAME_RE.match(name):
            raise ValueError(f"bad theme name {name!r}")
        if not isinstance(colors, dict) or not colors:
            raise ValueError("theme.save needs a non-empty colors object")
        for k, v in colors.items():
            if k not in THEME_KEYS:
                raise ValueError(f"unknown color name {k!r}")
            err = theme_color_error(k, v)
            if err is not None:
                raise ValueError(err)
        target = Path(self._themes_dir) / f"{name}.json"
        if target.resolve().parent != Path(self._themes_dir).resolve():
            raise ValueError(f"refusing to save outside themes dir: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dict(colors), indent=2) + "\n")
        self._mark_order(sid, oid, "approved")
        LOG.info("orders: theme saved as %s for session %s", target, sid)

    def _apply_policy_update(self, sid, oid, update):
        """Rewrite the allowed-commands file from an approved order."""
        try:
            base = json.loads(Path(self._allowed_path).read_text())
            if not isinstance(base, dict):
                base = {}
        except (OSError, ValueError):
            base = {}
        merged = dict(base)
        merged["_comment"] = (
            "Updated by approved agent order "
            f"{oid} (session {sid}); previous version kept in "
            f"{Path(self._allowed_path).name}.bak")
        if "allow" in update:
            merged["allow"] = update["allow"]
        if "deny" in update:
            merged["deny"] = update["deny"]
        if "allow" not in merged or "deny" not in merged:
            raise ValueError("current policy has no allow/deny to merge "
                             "into; refusing partial update")
        tmp = Path(self._allowed_path).with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(merged, indent=2))
            try:
                bak = Path(str(self._allowed_path) + ".bak")
                bak.write_bytes(Path(self._allowed_path).read_bytes())
            except OSError:
                pass  # backup best-effort; the tmp file is authoritative
            os.replace(tmp, self._allowed_path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        self._allowed, warnings = load_allowed_commands(self._allowed_path)
        for w in warnings:
            LOG.warning("%s", w)
        self._mark_order(sid, oid, "approved")
        LOG.info("orders: policy update applied for session %s", sid)

    def _targets_for(self, session_id):
        if session_id:
            subs = self._subs.get(session_id, set())
            if subs:
                return list(subs)
            # Session-scoped events also go to conns with no subscription yet
            # (fresh clients waiting for their first subscribe). Tabs that
            # attached before and unsubscribed everything are excluded:
            # they left on purpose (see _attached_once).
            unattached = [c for c in self._conns
                        if not c.sessions and c not in self._attached_once]
            return unattached
        return list(self._conns)

    # -- GitHub picker caches ------------------------------------------------
    async def _github_repos(self, search=None, limit=None):
        """Cached repo rows; `gh` is still checked for new ones every call.

        Returns (rows, stale): fresh rows are merged into the known set
        and served; when `gh` fails and known rows exist, the known
        (search-filtered) rows are served with stale True instead of
        erroring the picker.
        """
        try:
            fresh = await run_gh_list(search, limit, gh_bin=self._gh_bin)
        except GithubError:
            if self._known_repos:
                LOG.warning("githubRepos: gh failed, serving %d known "
                            "row(s)", len(self._known_repos))
                return filter_repos(self._known_repos, search), True
            raise
        # Fresh rows arrive search-filtered; merging keeps each complete
        # row, so the known set only ever grows toward the full list.
        self._known_repos = merge_repo_rows(self._known_repos, fresh)
        return filter_repos(self._known_repos, search), False

    async def _github_branches(self, full_name):
        """Cached branch names for one repo; `gh` still checked every call.

        Returns (names, stale) with the same fresh-or-known contract as
        _github_repos. Invalid names raise ValueError (never served from
        cache).
        """
        try:
            fresh = await run_gh_branches(full_name, gh_bin=self._gh_bin)
        except GithubError:
            known = self._known_branches.get(full_name)
            if known:
                LOG.warning("githubBranches: gh failed for %s, serving "
                            "%d known branch(es)", full_name, len(known))
                return list(known), True
            raise
        self._known_branches[full_name] = merge_branch_names(
            self._known_branches.get(full_name), fresh)
        return list(self._known_branches[full_name]), False

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
                # Auto-sync: workspace dir gone => delete the session (scoped to base).
                if isinstance(result, dict) and isinstance(result.get("sessions"), list):
                    for s in list(result["sessions"]):
                        if not isinstance(s, dict):
                            continue
                        root = s.get("workspaceRoot")
                        sid = s.get("sessionId")
                        if not sid or not isinstance(root, str) or not root:
                            continue
                        if os.path.isdir(root):
                            continue
                        if not _workspace_under_base(root, self._workspace_base):
                            continue
                        try:
                            delete_session_files(sid, base=self._sessions_base)
                        except Exception:
                            LOG.warning("auto-delete missing workspace failed for %s", sid, exc_info=True)
                            continue
                        try:
                            self._drop_routing(sid)
                        except Exception:
                            pass
                        self._deleted_sids.add(sid)
                # Mark bridge-started rows for first-group sorting in the
                # UI. Presence-only (no False key) so untouched rows stay
                # byte-identical to the host's.
                if isinstance(result, dict):
                    for s in result.get("sessions") or []:
                        if isinstance(s, dict) \
                                and s.get("sessionId") in self._bridge_sids:
                            s["bridgeCreated"] = True
                        self._note_session_obj(s)
                # Sessions removed from disk read as gone even though the
                # running host still lists them from memory.
                rows = result.get("sessions") \
                    if isinstance(result, dict) else None
                if isinstance(rows, list) and self._deleted_sids:
                    result["sessions"] = [
                        s for s in rows
                        if not (isinstance(s, dict) and s.get("sessionId")
                                in self._deleted_sids)]
                return reply(True, result=result)
            if mtype == "resume":
                if msg.get("sessionId") in self._deleted_sids:
                    raise MspError(-32000,
                                   f"unknown session {msg.get('sessionId')}")
                try:
                    result = await self._msp.command(
                        "session/resume",
                        _pick(msg, ("sessionId", "cursor", "excludeItems",
                                    "history")))
                except MspError as e:
                    if e.code == METHOD_NOT_FOUND:
                        result = await self._resume_fallback(
                            msg.get("sessionId"))
                    elif _retained_refused(e):
                        result = await self._resume_fallback(
                            msg.get("sessionId"),
                            none_reason="resume_refused_by_host",
                            fallback="resume_refused_by_host")
                    else:
                        raise
                self._attach(conn, msg.get("sessionId"))
                self._note_session_obj(result.get("session"))
                return reply(True, result=result)
            if mtype == "read":
                if msg.get("sessionId") in self._deleted_sids:
                    raise MspError(-32000,
                                   f"unknown session {msg.get('sessionId')}")
                try:
                    result = await self._msp.call(
                        "session/read",
                        _pick(msg, ("sessionId", "excludeItems")))
                except MspError as e:
                    if e.code == METHOD_NOT_FOUND:
                        # Read-only fallback: same metadata, no attach.
                        result = await self._resume_fallback(
                            msg.get("sessionId"))
                    elif _retained_refused(e):
                        result = await self._resume_fallback(
                            msg.get("sessionId"),
                            none_reason="resume_refused_by_host",
                            fallback="resume_refused_by_host")
                    else:
                        raise
                self._note_session_obj(result.get("session"))
                return reply(True, result=result)
            if mtype == "fork":
                result = await self._msp.command(
                    "session/fork",
                    _pick(msg, ("sessionId", "cutPoint", "excludeItems")))
                if isinstance(result, dict):
                    self._note_session_obj(result.get("session"))
                return reply(True, result=result)
            if mtype == "rename":
                result = await self._msp.command(
                    "session/rename", _pick(msg, ("sessionId", "name")))
                # Explicit renames take precedence over the default: the
                # session leaves the auto-name set, present or future.
                self._auto_name_pending.discard(msg.get("sessionId"))
                return reply(True, result=result)
            if mtype == "delete":
                # File-based delete: rm -rf the session's store dirs, never
                # the host's deletion registry (Store(Busy) on back-to-back
                # deletes). Missing dirs are success (already gone).
                sid = msg.get("sessionId")
                removed = delete_session_files(
                    sid, base=self._sessions_base)
                self._drop_routing(sid)
                self._deleted_sids.add(sid)
                return reply(True, result={"deleted": True,
                                           "sessionId": sid,
                                           "removed": removed})
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
                if mode not in VALID_APPROVAL_MODES:
                    return reply(False, error={
                        "message": f"unknown approval mode {mode!r}; "
                                   f"want one of {sorted(VALID_APPROVAL_MODES)}"})
                result = await self._msp.command(
                    "session/setApprovalMode",
                    {"sessionId": msg["sessionId"], "mode": mode})
                eff = result.get("effectiveMode") \
                    if isinstance(result, dict) else None
                if isinstance(eff, dict):
                    self._note_approval_mode(msg["sessionId"],
                                             eff.get("mode"))
                return reply(True, result=result)
            if mtype == "setSubagentAutoApprove":
                enabled = msg.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false")
                self._subagent_auto_approve = enabled
                LOG.info("subagent auto-approve %s",
                         "enabled" if enabled else "disabled")
                return reply(True, result={"enabled": enabled})
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
            if mtype == "plugins":
                return reply(True, result=await self._msp.call(
                    "plugin/list", {"sessionId": msg["sessionId"]}))
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
                # Repo picker rows: known rows cached, `gh` still
                # checked for new ones every call (stale only when
                # `gh` fails and known rows exist).
                repos, stale = await self._github_repos(
                    msg.get("search"), msg.get("limit"))
                result = {"repos": repos}
                if stale:
                    result["stale"] = True
                return reply(True, result=result)
            if mtype == "githubBranches":
                # Branch picker rows for one repo: same cache contract.
                branches, stale = await self._github_branches(
                    msg.get("fullName"))
                result = {"branches": branches}
                if stale:
                    result["stale"] = True
                return reply(True, result=result)
            if mtype == "githubClone":
                # Shallow-clone into workspaces/<sessionId>/<repo>/, admitted
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
            if mtype == "ordersDecide":
                # Human verdict on a staged order (gated actions —
                # policy updates, saved themes, backend restarts —
                # never run without this).
                return reply(True, result=await self._decide_order(msg))
            if mtype == "ordersList":
                # Staged orders awaiting a human (for clients that
                # attached after the ordersPending fan-out, e.g. on
                # reload or reconnect).
                return reply(True, result=self._list_orders(
                    msg.get("sessionId")))
            if mtype == "githubClean":
                # Delete one session's clone leaf after confirm (the UI
                # confirms; the bridge only enforces the leaf shape).
                return reply(True, result=self._clean_github_clone(
                    msg.get("sessionId")))
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
             "input": build_turn_input(msg.get("text", ""),
                                       msg.get("images"),
                                       msg.get("skills"))}
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
        # Remembered effort default (picker / /effort / /default-effort):
        # validated up front so a bad tier fails before creating the
        # session, applied after start via session/setReasoningEffort
        # (session/start takes no effort field on MSP v1).
        effort = msg.get("reasoningEffort")
        if effort is not None:
            if effort not in VALID_REASONING_EFFORTS:
                raise ValueError(
                    f"unknown reasoning effort {effort!r}; "
                    f"want one of {sorted(VALID_REASONING_EFFORTS)}")
        if (p.get("approvalMode") is not None
                and p["approvalMode"] not in VALID_APPROVAL_MODES):
            raise ValueError(
                "unknown approvalMode "
                f"{p['approvalMode']!r}; want one of "
                f"{sorted(VALID_APPROVAL_MODES)}")
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
        auto_ws = False
        if self._workspace_base and p.get("workspaceRoot") is None \
                and p.get("workspaceRoots") is None:
            sid = p.get("sessionId") or uuid7()
            wsdir = session_workspace_dir(self._workspace_base, sid)
            p["sessionId"] = sid
            p["workspaceRoot"] = wsdir
            auto_ws = True
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
        new_sid = result["session"]["sessionId"]
        if p.get("approvalMode") in VALID_APPROVAL_MODES:
            self._note_approval_mode(new_sid, p["approvalMode"])
        # Folded truth wins when the host echoes it (same value normally).
        self._note_session_obj(result.get("session"))
        if effort is not None:
            try:
                await self._msp.command(
                    "session/setReasoningEffort",
                    {"sessionId": result["session"]["sessionId"],
                     "reasoningEffort": effort})
            except Exception:
                LOG.warning("default effort %r not applied to session %s",
                            effort, result["session"]["sessionId"],
                            exc_info=True)
        self._attach(conn, result["session"]["sessionId"])
        # A fresh session under a previously deleted id exists again.
        self._deleted_sids.discard(result["session"]["sessionId"])
        # Started through this bridge (whatever the path): sort first.
        self._remember_bridge_sid(result["session"]["sessionId"])
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
            # Every workspace-backed session learns the orders protocol
            # (best-effort skill seed) and is checked for orders after
            # each turn. Manual roots included: one hidden doc file.
            self._workspace_roots[sid] = p["workspaceRoot"]
            seed_orders_skill(p["workspaceRoot"])
            if auto_ws:
                # Bridge-owned session dir: plain AGENTS.md at the root.
                # Manual roots are the user's own dirs — no root-level
                # file is seeded there. (Repo sessions seed theirs in
                # _open_cloned, which routes through here too — but with
                # an explicit workspaceRoot, so auto_ws is False there.)
                seed_session_agents(p["workspaceRoot"])
        if p.get("workspaceRoot") and has_seeded_instructions(
                p.get("workspaceRoot")):
            # Rooted at a seeded workspace (githubOpen session dir, a
            # later `new --path` at the session dir or its clone leaf, or
            # a pre-rename WEB-MUSE.md leaf): gh/git approvals
            # auto-decide and the instruction file is already on disk.
            self._gh_auto_sids.add(sid)
        return result

    # -- GitHub clones (gh-only v1) -----------------------------------------
    def _clone_leaf(self, session_id, full_name=None):
        """`workspaces/<sid>/<repo>` leaf for a clone: contained and reusable.

        The leaf name is the repo slug (legacy sessions used "repo", kept
        as the fallback when no fullname is given). The leaf is created
        only by cloning, so a non-git residue is always a previous
        attempt's leftover: remove it so retries start clean.
        Raises ValueError (bad id / no workspace base) or GithubError
        (dest_exists when this session already holds a clone).
        """
        if not self._workspace_base:
            raise ValueError("github clone needs a workspace base "
                             "(bridge started with --workspace-base \"\")")
        wsdir = session_workspace_dir(self._workspace_base, session_id or "")
        leaf = Path(wsdir) / (repo_dir_name(full_name)
                              if full_name is not None else "repo")
        if leaf.is_dir() and (leaf / ".git").is_dir():
            raise GithubError(
                "dest_exists",
                f"session {session_id} already holds a clone at {leaf}")
        if leaf.exists() or leaf.is_symlink():
            shutil.rmtree(leaf, ignore_errors=True)
        return str(leaf)

    def _find_clone_leaf(self, session_id):
        """Locate one session's clone leaf, whatever its name (pure-ish).

        Returns the leaf Path (a direct child of the session dir holding
        `.git`), or None. Legacy "repo" leaves match the same way. Raises
        ValueError on a bad id or missing workspace base. Never raises on
        a missing session dir (None — nothing to clean).
        """
        sid = session_id or ""
        if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
                or sid in (".", ".."):
            raise ValueError(f"unsafe sessionId for github clean: {sid!r}")
        if not self._workspace_base:
            raise ValueError("github clean needs a workspace base")
        root = Path(self._workspace_base).resolve()
        wsdir = (root / sid).resolve()
        if wsdir.parent != root:
            raise ValueError(f"refusing to clean outside workspace: {sid!r}")
        if not wsdir.is_dir():
            return None
        try:
            children = sorted(p for p in wsdir.iterdir()
                              if p.is_dir() and not p.is_symlink()
                              and is_safe_leaf_name(p.name))
        except OSError:
            return None
        for child in children:
            if (child / ".git").is_dir():
                return child
        return None

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
                      open_opts=None, branch=None):
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
                             open_opts, branch),
            name=f"github-clone-{op_id}")
        self._github_ops[op_id] = [task, conn, full_name]
        return {"accepted": True, "opId": op_id, "fullName": full_name,
                "dest": dest, "sessionId": session_id, "branch": branch}

    def _finish_op(self, op_id, task):
        """Take terminal-event ownership iff this task still owns the op."""
        rec = self._github_ops.get(op_id)
        if rec is not None and rec[0] is task:
            self._github_ops.pop(op_id, None)
            return rec
        return None

    async def _clone_task(self, conn, full_name, dest, op_id, session_id,
                          open_opts, branch=None):
        """Background clone body: progress, result event, cleanup."""
        me = asyncio.current_task()
        try:
            await run_gh_clone(
                full_name, dest, gh_bin=self._gh_bin, branch=branch,
                on_line=lambda line: self._emit_clone_progress(
                    conn, op_id, full_name, "progress", line))
            self._emit_clone_progress(conn, op_id, full_name, "completed")
            # Seed the leaf AGENTS.md into the fresh clone (both paths —
            # opened now or rooted later via `new --path`); skipped when
            # the repo ships its own. Seeding never fails the clone: on
            # error we log and report seededInstructions False.
            try:
                seeded, _ = seed_github_instructions(dest, full_name)
            except OSError:
                LOG.warning("instruction seeding failed for %s", dest,
                            exc_info=True)
                seeded = False
            if open_opts is None:
                result = {"fullName": full_name, "dest": dest,
                          "sessionId": session_id, "opId": op_id,
                          "seededInstructions": seeded}
            else:
                result = await self._open_cloned(
                    conn, full_name, dest, session_id, op_id, open_opts)
                result["seededInstructions"] = seeded
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
        """session/start rooted at the session dir (open half of githubOpen).

        The agent works in ./<leaf>/ (named in the session AGENTS.md);
        the session root holds the bridge files (AGENTS.md, .web-muse/).
        The leaf AGENTS.md was seeded by _clone_task before this runs.
        """
        session_root = str(Path(dest).parent)
        leaf = Path(dest).name
        new_msg = {"workspaceRoot": session_root, "sessionId": session_id}
        if open_opts.get("mcpAttach") is not None:
            new_msg["mcpAttach"] = open_opts.get("mcpAttach")
        model = open_opts.get("model")
        if isinstance(model, dict) and isinstance(model.get("modelId"), str) \
                and model["modelId"].strip():
            new_msg["modelId"] = model["modelId"].strip()
            if isinstance(model.get("providerId"), str) \
                    and model["providerId"].strip():
                new_msg["providerId"] = model["providerId"].strip()
        if isinstance(open_opts.get("reasoningEffort"), str) \
                and open_opts["reasoningEffort"].strip():
            new_msg["reasoningEffort"] = \
                open_opts["reasoningEffort"].strip()
        if isinstance(open_opts.get("approvalMode"), str) \
                and open_opts["approvalMode"].strip():
            new_msg["approvalMode"] = open_opts["approvalMode"].strip()
        result = await self._do_new(conn, new_msg)
        # Session-root AGENTS.md naming the clone leaf (best-effort,
        # never overwrites — _do_new seeds only auto-created roots).
        seed_session_agents(session_root, leaf, full_name)
        self._gh_auto_sids.add(session_id)
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
        """Admit a clone into `workspaces/<sid>/<repo>/` (no MSP session).

        The caller then roots a session at the session dir via `new`
        (which runs the usual first-use confirm) or uses `githubOpen`
        for the combined step.
        """
        full_name = validate_fullname(msg.get("fullName"))
        sid = msg.get("sessionId") or uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid, full_name)
        branch = msg.get("branch")
        if branch is not None:
            branch = validate_branch(branch)
        return self._launch_clone(conn, full_name, dest, op_id, sid,
                                  branch=branch)

    def _do_github_open(self, conn, msg):
        """Admit a clone + `session/start` rooted at the session dir."""
        full_name = validate_fullname(msg.get("fullName"))
        sid = uuid7()
        op_id = self._check_op_id(msg.get("opId") or uuid7())
        dest = self._clone_leaf(sid, full_name)
        branch = msg.get("branch")
        if branch is not None:
            branch = validate_branch(branch)
        return self._launch_clone(conn, full_name, dest, op_id, sid,
                                  {"mcpAttach": msg.get("mcpAttach"),
                                   "name": msg.get("name"),
                                   "model": msg.get("model"),
                                   "reasoningEffort": msg.get("reasoningEffort"),
                                   "approvalMode": msg.get("approvalMode")},
                                  branch=branch)

    def _clean_github_clone(self, session_id):
        """Delete one session's clone leaf (raises outside the leaf).

        The leaf is found by `.git` presence, so named leaves and legacy
        "repo" leaves clean the same way. Only the clone leaf goes —
        the session dir (AGENTS.md, .web-muse/) is kept unless it ends
        up empty.
        """
        leaf = self._find_clone_leaf(session_id)
        sid = session_id or ""
        if leaf is None:
            if not self._workspace_base:
                raise ValueError("github clean needs a workspace base")
            return {"removed": False,
                    "dest": str(Path(self._workspace_base).resolve()
                                / sid)}
        if not is_safe_leaf_name(leaf.name):
            raise ValueError(
                f"refusing to clean outside clone leaf: {sid!r}")
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
            self._attached_once.add(conn)

    def _drop_routing(self, session_id):
        """Forget router state for a deleted session (subs, cursors...)."""
        members = self._subs.pop(session_id, set())
        for conn in members:
            conn.sessions.discard(session_id)
        self._cursors.pop(session_id, None)
        self._turns.pop(session_id, None)
        self._auto_name_pending.discard(session_id)
        self._gh_auto_sids.discard(session_id)
        self._approval_modes.pop(session_id, None)
        if session_id in self._bridge_sids:
            self._bridge_sids.discard(session_id)
            self._save_bridge_sids()

    async def _resume_fallback(self, session_id, none_reason=None,
                               fallback=None):
        """Open a session when the host serves no session/resume
        (--no-session-log hosts; durable hosts serve it), or refuses to
        load it (retained permission profile the host cannot compose).

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
                        "noneReason": none_reason or "resume_unserved_by_host",
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
            "fallback": fallback or "resume_unserved_by_host",
        }


def _pick(msg, keys):
    out = {}
    for k in keys:
        if k in msg and msg[k] is not None:
            out[k] = msg[k]
    return out
