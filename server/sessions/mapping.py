"""MSP wire mapping: methods, enums, notification routing, turn input.

Pure mapping helpers (build_* / map_*) are unit-tested in
tests/test_msp_mapping.py against recorded fixtures.
"""

from __future__ import annotations


# JSON-RPC "method not found": a --no-session-log (memory-only) host serves
# no view-store methods — session/resume, session/read, view/subscribe and
# view/page all answer -32601 there (verified live). Durable hosts serve
# them. The bridge falls back below instead of failing the UI.
METHOD_NOT_FOUND = -32601


# A serve host cannot load sessions whose retained permission profile it
# cannot compose (verified live: TUI-created `:auto-review` sessions are
# refused with -32603 "retained session refused", while serve-created
# sessions resume fine; the TUI resumes in-process where the reviewer
# exists). This is a host capability gap, not a missing method, so the
# bridge degrades honestly (metadata + live attach) instead of failing
# the switch — same philosophy as the --no-session-log fallback below.
RETAINED_REFUSED_CODE = -32603
RETAINED_REFUSED_MARKER = "retained session refused"


def _retained_refused(e):
    """True when the host refuses to load a stored session (-32603).

    Matched on code plus the host's marker text so unrelated internal
    errors still surface instead of degrading silently.
    """
    return (getattr(e, "code", None) == RETAINED_REFUSED_CODE
            and RETAINED_REFUSED_MARKER in str(e).lower())


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

# MSP ApprovalMode closed enum (schema $defs/ApprovalMode). All four are
# selectable, including allowAll ("approve all" in the Session panel):
# with it the host runs every command without prompting, so it carries
# an explicit warning in the UI and should be used only in throwaway
# sessions. (The host default onRequest applies unless changed here or
# in the TUI.)
VALID_APPROVAL_MODES = {"allowAll", "promptUnmatched", "onRequest",
                        "denyUnmatched"}


def pick_approve_once_choice(choices):
    """ChoiceId of the narrowest approve choice, or None.

    Prefers a one-shot `approved` choice; falls back to any non-amendment
    approve decision (`approvedForSession`). Amendment decisions (which
    would persist a policy rule) and denials are never picked, and None
    means "leave it to the UI card".
    """
    if not isinstance(choices, list):
        return None
    cands = [c for c in choices
             if isinstance(c, dict) and c.get("choiceId")
             and c.get("decision") in ("approved", "approvedForSession")]
    if not cands:
        return None
    for c in cands:
        if c.get("decision") == "approved" and c.get("scope") == "once":
            return c["choiceId"]
    return cands[0]["choiceId"]


# Offered reasoning-effort tiers: the middle six of the MSP ReasoningEffort
# vocabulary (schema $defs/ReasoningEffort). The `none` and `ultra`
# extremes are deliberately excluded — `none` silently degrades answer
# quality and `ultra` burns budget with little return.
VALID_REASONING_EFFORTS = {"minimal", "low", "medium", "high",
                           "xhigh", "max"}

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


def build_turn_input(text, images=None, skills=None):
    """WS prompt payload -> MSP TurnInputPart list.

    skills: optional list of {selector, arguments?} dicts, each becoming
    a {type: skill} part (selector required, arguments optional free
    text — the wire twin of what the TUI accepts after the shortcut
    token). Skill parts come first so "/selector args" reads as the
    skill invocation with args, not as plain user text.
    """
    parts = []
    for sk in skills or []:
        if not isinstance(sk, dict):
            raise ValueError("skill entries must be {selector, arguments?}")
        selector = sk.get("selector", "")
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("skill entries need a non-empty selector")
        arguments = sk.get("arguments", "")
        if arguments is None:
            arguments = ""
        if not isinstance(arguments, str):
            raise ValueError("skill arguments must be text")
        part = {"type": "skill", "selector": selector.strip()}
        if arguments.strip():
            part["arguments"] = arguments.strip()
        parts.append(part)
    if text:
        parts.append({"type": "text", "text": text})
    for img in images or []:
        parts.append({
            "type": "image",
            "mediaType": img.get("mediaType", "image/png"),
            "base64Data": img.get("base64Data", ""),
        })
    if not parts:
        raise ValueError("prompt needs text, images, or a skill")
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
