"""Silent-by-default session transcript with verbose opt-in.

Silent mode keeps only decision-relevant entries (per agreed plan):
user messages, final agent answers, decisions/approvals, file changes
and commands (one line each), errors and resolutions, and workflows.
Verbose mode keeps everything.
"""

from __future__ import annotations

import json
from pathlib import Path

SILENT = "silent"
VERBOSE = "verbose"
TRANSCRIPT_KEY = "transcript"

# Event types kept in silent mode. Everything else is verbose-only.
# "workflow" is always kept: workflow runs must stay visible.
SILENT_KEEP = frozenset({
    "user",
    "agent_final",
    "decision",
    "approval",
    "file_change",
    "command",
    "error",
    "resolution",
    "workflow",
})

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")


def load_mode(config_path=DEFAULT_CONFIG_PATH) -> str:
    """Return transcript mode from config; default is silent."""
    try:
        raw = json.loads(Path(config_path).read_text())
    except (OSError, ValueError, AttributeError):
        return SILENT
    if isinstance(raw, dict) and raw.get(TRANSCRIPT_KEY) == VERBOSE:
        return VERBOSE
    return SILENT


def format_event(event: dict) -> str:
    kind = event.get("type", "?")
    summary = event.get("summary", "")
    return f"[{kind}] {summary}".rstrip()


def render(events: list, mode: str = SILENT) -> list:
    """Render events to lines. Silent drops verbose-only types."""
    if mode == VERBOSE:
        return [format_event(e) for e in events]
    return [format_event(e) for e in events if e.get("type") in SILENT_KEEP]
