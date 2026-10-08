"""MCP settings inventory and per-session MCP config building."""

from __future__ import annotations

import json
import os
from pathlib import Path


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
