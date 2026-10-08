"""Session workspace directories, manual roots, explorer, file deletes."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


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


def muse_sessions_base():
    """On-disk MSP session store root (~/.local/share/muse/sessions)."""
    return Path(os.path.expanduser("~")) / ".local" / "share" / "muse" \
        / "sessions"


def delete_session_files(session_id, base=None):
    """Remove one session's on-disk store dirs (rm -rf semantics).

    Deletes the dated main dir (sessions/YYYY/MM/DD/<sessionId>/) and the
    view-store dir (sessions/.msp-view-v1/<sessionId>/), and nothing else.
    The host is never called: its deletion registry admits one deletion
    at a time and rejects back-to-back deletes with Store(Busy), so file
    removal is the reliable path (verified live: with both dirs gone the
    host answers resume with -32020 "was not found", and a restarted host
    no longer lists the session).

    Raises ValueError on unsafe ids or containment failure. Missing dirs
    are success (already gone — rm -f semantics). Returns the removed
    path strings.
    """
    sid = session_id or ""
    if not SAFE_SESSION_ID.fullmatch(sid) or sid.startswith(".") \
            or sid in (".", ".."):
        raise ValueError(f"unsafe sessionId for session delete: {sid!r}")
    root = Path(base).resolve() if base else muse_sessions_base().resolve()
    targets = [root / ".msp-view-v1" / sid]
    # Dated layout is YYYY/MM/DD/<sid>; glob only (the safe charset holds
    # no glob metacharacters) instead of a full-tree walk.
    targets += [p for p in root.glob(f"????/??/??/{sid}")]
    removed = []
    for target in targets:
        if target.is_symlink():
            # Lexical location is inside the store by construction; drop
            # the link itself, never its target.
            target.unlink()
            removed.append(str(target))
            continue
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"refusing to delete outside session store: {sid!r}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
            removed.append(str(resolved))
        elif resolved.exists():
            raise ValueError(
                f"refusing to delete non-directory session path: {resolved}")
        # else: already gone — still success.
    return sorted(removed)


def _workspace_under_base(workspace_root, base):
    """True when workspace_root is contained under base (both resolved)."""
    if not base or not isinstance(workspace_root, str) or not workspace_root:
        return False
    try:
        resolved_base = Path(base).resolve()
        resolved_root = Path(workspace_root).resolve()
        return resolved_base in resolved_root.parents or resolved_root == resolved_base
    except (OSError, ValueError):
        return False
