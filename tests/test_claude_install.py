import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kindex.claude_install import install, legacy_manifest, unresolved_handlers
from kindex.config import Config
from kindex.agent_adapters import render_hook_context


def test_advice_never_grants_permission():
    result = json.loads(render_hook_context("Remember the project constraint", adapter="claude", event="PreToolUse"))
    assert "permissionDecision" not in result["hookSpecificOutput"]
    assert result["hookSpecificOutput"]["additionalContext"]


def test_packaged_legacy_manifest_is_generated_from_installer():
    path = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
    assert json.loads(path.read_text()) == {"hooks": legacy_manifest(Config(), "kin")}
    assert all(1 <= h["timeout"] <= 10 for entries in legacy_manifest(Config(), "kin").values()
               for entry in entries for h in entry["hooks"])


def test_modern_switch_removes_only_owned_handlers_and_legacy_rolls_back(tmp_path, monkeypatch):
    import kindex.claude_install as installer
    import kindex.setup as setup
    monkeypatch.setattr(setup, "_find_kin_path", lambda: "/usr/bin/kin")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="2.1.263 (Claude Code)\n"))
    cfg = Config(claude_dir=str(tmp_path / "claude"))
    cfg.claude_path.mkdir()
    foreign = {"type": "command", "command": "foreign-policy-check"}
    settings = cfg.claude_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "", "hooks": [
        {"type": "command", "command": "kin prime --for hook"}, foreign]}]}}))
    install(cfg, mode="modern")
    state = json.loads(settings.read_text())
    assert state["hooks"]["SessionStart"][0]["hooks"] == [foreign]
    assert state["enabledPlugins"]["kindex-modern@skills-dir"]
    assert unresolved_handlers(state) == []
    assert (cfg.claude_path / "skills/kindex-modern/hooks/kindex.ts").exists()
    install(cfg, mode="legacy")
    state = json.loads(settings.read_text())
    assert state["enabledPlugins"]["kindex-modern@skills-dir"] is False
    assert state["hooks"]["SessionStart"][0]["hooks"] == [foreign]
    assert len(state["hooks"]["SessionStart"]) == 2


def test_unknown_wrapper_blocks_modern_switch_without_changing_settings(tmp_path):
    cfg = Config(claude_dir=str(tmp_path))
    settings = tmp_path / "settings.json"
    original = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "/bin/bash /custom/kin-prompt-recall.sh"}]}]}})
    settings.write_text(original)
    with pytest.raises(ValueError, match="retire-command"):
        install(cfg, mode="modern")
    assert settings.read_text() == original


def test_idempotent_legacy_install_does_not_rewrite_settings(tmp_path):
    cfg = Config(claude_dir=str(tmp_path))
    install(cfg)
    settings = tmp_path / "settings.json"
    before = settings.stat().st_mtime_ns
    install(cfg)
    assert settings.stat().st_mtime_ns == before


@pytest.mark.parametrize("old_path", ["/opt/homebrew/bin/kin", "/usr/local/bin/kin"])
def test_explicit_historical_location_requires_exact_command(old_path):
    from kindex.claude_install import _known_commands
    from kindex.setup import _kin_stop_hook_command

    known = _known_commands(Config(), "/current/bin/kin")
    command = _kin_stop_hook_command(old_path, ["compact-hook", "--text", "Session ended"])
    assert command in known
    assert command.replace("\n", "\\n") in known
    assert command + "; custom-audit" not in known
    assert command.replace("then exit 0", "then exit 7") not in known


@pytest.mark.parametrize("unknown_path", ["/custom/bin/kin", "/custom tools/bin/kin"])
def test_custom_kin_location_is_not_implicitly_owned(unknown_path):
    from kindex.claude_install import _known_commands
    from kindex.setup import _kin_stop_hook_command

    command = _kin_stop_hook_command(unknown_path, ["compact-hook", "--text", "Session ended"])
    assert command not in _known_commands(Config(), "/current/bin/kin")
    assert command in _known_commands(Config(), unknown_path)
