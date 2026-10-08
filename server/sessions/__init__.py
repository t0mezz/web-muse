"""Session routing: WS connections <-> MSP sessionIds, cursor store, frame mapping.

Pure mapping helpers (build_* / map_*) are unit-tested in
tests/test_msp_mapping.py against recorded fixtures.

This package is a split of the former `server/sessions.py`: domain
modules hold the helpers, `router.py` holds `SessionRouter`. Every
name the old module defined is re-exported here, so `from
server.sessions import ...` keeps working unchanged.
"""

from server.sessions.github_parts import GITHUB_INSTRUCTIONS_FILENAME, GITHUB_INSTRUCTIONS_MARKER, GITHUB_INSTRUCTIONS_TEMPLATE, LEGACY_GITHUB_INSTRUCTIONS_FILENAME, SESSION_AGENTS_MARKER, _read_marker_first_line, github_preapproved_section, has_seeded_instructions, render_github_instructions, render_session_agents, seed_github_instructions, seed_session_agents
from server.sessions.mapping import COMMAND_METHODS, DEFAULT_SESSION_NAME_LEN, METHOD_NOT_FOUND, RETAINED_REFUSED_CODE, RETAINED_REFUSED_MARKER, VALID_APPROVAL_MODES, VALID_PAGE_DIRECTIONS, VALID_REASONING_EFFORTS, _retained_refused, build_turn_input, default_session_name, map_notification_to_ws, map_server_request_to_ws, notification_session_id, pick_approve_once_choice
from server.sessions.mcp import VALID_MCP_FRAMINGS, VALID_MCP_MODES, build_session_mcp_config, read_mcp_servers, read_settings_raw, settings_path
from server.sessions.orders import DEFAULT_THEMES_DIR, ORDERS_DIRNAME, ORDERS_FILENAME, ORDERS_MAX_BYTES, ORDERS_MAX_COUNT, ORDERS_RECEIPT, ORDERS_RECEIPT_CAP, ORDERS_SKILL_FILE, ORDERS_TEMPLATE, ORDER_ACTIONS, RESTART_DELAY_DEFAULT, RESTART_DELAY_MAX, RESTART_DELAY_MIN, RESTART_REASON_MAX, _read_receipt, _validate_allowed_update, _validate_restart, _write_receipt, seed_orders_skill, validate_orders
from server.sessions.policy import ALLOWED_COMMANDS_FILE, AUTO_ALLOW_INSPECT, AUTO_ALLOW_TOOLCHAINS, GH_AUTO_ALLOW_RE, GH_AUTO_ALLOW_TOOLS_RE, GH_GIT_DENY_RE, GH_TOOL_DENY_RE, GIT_DENY_SUMMARY, MAX_ALLOWED_PATTERNS, MAX_PATTERN_LEN, SAFE_COMMAND_NAME, _builtin_allowed_config, _compile_patterns, default_allowed_config, load_allowed_commands, shell_allowed, shell_auto_allowed
from server.sessions.router import LOG, SessionRouter, _pick
from server.sessions.themes import GLOW_RE, HEX_COLOR, HEX_COLOR_RE, SCRIM_RE, THEME_KEYS, THEME_NAME_RE, _rgb_triplet, _valid_theme_value, theme_color_error
from server.sessions.workspaces import SAFE_SESSION_ID, _workspace_under_base, browse_dir, browse_home, delete_session_files, muse_sessions_base, session_workspace_dir, validate_manual_root
