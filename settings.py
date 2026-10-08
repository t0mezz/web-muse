"""Settings store for the session workspace.

Reads/writes config.json next to this file. Currently manages:
- transcript: "silent" (default) or "verbose".
"""

from __future__ import annotations

import json
from pathlib import Path

from transcript import SILENT, TRANSCRIPT_KEY, VERBOSE

CONFIG_PATH = Path(__file__).with_name("config.json")
TRANSCRIPT_MODES = (SILENT, VERBOSE)


def load_settings(config_path=CONFIG_PATH) -> dict:
    """Return all settings as a dict; missing/invalid file means {}."""
    try:
        raw = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_settings(settings: dict, config_path=CONFIG_PATH) -> None:
    Path(config_path).write_text(json.dumps(settings, indent=2) + "\n")


def get_transcript_mode(config_path=CONFIG_PATH) -> str:
    """Return the configured transcript mode; default is silent."""
    mode = load_settings(config_path).get(TRANSCRIPT_KEY)
    return mode if mode in TRANSCRIPT_MODES else SILENT


def set_transcript_mode(mode: str, config_path=CONFIG_PATH) -> dict:
    """Persist the transcript mode; rejects unknown modes."""
    if mode not in TRANSCRIPT_MODES:
        raise ValueError(f"unknown transcript mode: {mode!r}")
    settings = load_settings(config_path)
    settings[TRANSCRIPT_KEY] = mode
    save_settings(settings, config_path)
    return settings
