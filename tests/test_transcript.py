from transcript import SILENT, VERBOSE, load_mode, render


def _events():
    kept = [
        {"type": "user", "summary": "add silent transcript"},
        {"type": "agent_final", "summary": "done"},
        {"type": "decision", "summary": "silent by default"},
        {"type": "approval", "summary": "approved"},
        {"type": "file_change", "summary": "transcript.py"},
        {"type": "command", "summary": "pytest"},
        {"type": "error", "summary": "boom"},
        {"type": "resolution", "summary": "fixed"},
        {"type": "workflow", "summary": "review workflow ran"},
    ]
    dropped = [
        {"type": "tool_call", "summary": "read file args"},
        {"type": "tool_result", "summary": "long output"},
        {"type": "progress", "summary": "tick"},
        {"type": "tokens", "summary": "1234 tokens"},
        {"type": "debug", "summary": "timestamp detail"},
        {"type": "reasoning", "summary": "intermediate thought"},
    ]
    return kept, dropped


def test_silent_keeps_workflows_and_drops_noise():
    kept, dropped = _events()
    lines = render(kept + dropped, SILENT)
    assert len(lines) == len(kept)
    assert any(l.startswith("[workflow]") for l in lines)
    assert not any(l.startswith("[tool_call]") for l in lines)
    assert not any(l.startswith("[reasoning]") for l in lines)


def test_verbose_keeps_everything():
    kept, dropped = _events()
    lines = render(kept + dropped, VERBOSE)
    assert len(lines) == len(kept) + len(dropped)


def test_default_mode_is_silent(tmp_path):
    assert load_mode(tmp_path / "missing.json") == SILENT


def test_verbose_enabled_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"transcript": "verbose"}')
    assert load_mode(cfg) == VERBOSE
