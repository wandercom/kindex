import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kindex import platform_io
from kindex.claude_install import install, legacy_manifest, unresolved_handlers
from kindex.config import Config
from kindex.agent_adapters import render_hook_context


def test_advice_never_grants_permission():
    result = json.loads(render_hook_context("Remember the project constraint", adapter="claude", event="PreToolUse"))
    assert "permissionDecision" not in result["hookSpecificOutput"]
    assert result["hookSpecificOutput"]["additionalContext"]


def test_packaged_legacy_manifest_is_generated_from_installer():
    from kindex.claude_install import PACKAGED_PLUGIN_KIN
    path = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
    assert json.loads(path.read_text()) == {"hooks": legacy_manifest(Config(), PACKAGED_PLUGIN_KIN)}
    assert all(1 <= h["timeout"] <= 10 for entries in legacy_manifest(Config(), "kin").values()
               for entry in entries for h in entry["hooks"])


def test_packaged_manifest_expands_plugin_root_in_hook_shell(tmp_path):
    import subprocess
    from kindex.claude_install import PACKAGED_PLUGIN_KIN
    root = tmp_path / "plugin root"
    shim = root / "scripts" / "claude-plugin" / "kin"
    shim.parent.mkdir(parents=True)
    log = tmp_path / "argv"
    shim.write_text(f'#!/bin/sh\ncat >/dev/null\necho "$@" >> "{log}"\n')
    shim.chmod(0o755)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manifest = legacy_manifest(Config(), PACKAGED_PLUGIN_KIN)
    handlers = [h for entries in manifest.values() for entry in entries for h in entry["hooks"]]
    for handler in handlers:
        done = subprocess.run([platform_io.posix_shell(), "-c", handler["command"]], cwd=cwd, input="{}",
                              capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                                   "CLAUDE_PLUGIN_ROOT": str(root)})
        assert done.returncode == 0, done.stderr
    assert len(log.read_text().splitlines()) == len(handlers)


def test_modern_switch_removes_only_owned_handlers_and_legacy_rolls_back(tmp_path, monkeypatch):
    import kindex.claude_install as installer
    import kindex.setup as setup
    monkeypatch.setattr(setup, "_find_kin_path", lambda: "/usr/bin/kin")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="2.1.274 (Claude Code)\n"))
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


def test_a_legacy_install_writes_no_plugin_key(tmp_path, monkeypatch):
    import kindex.claude_install as installer
    import kindex.setup as setup
    monkeypatch.setattr(setup, "_find_kin_path", lambda: "/usr/bin/kin")
    cfg = Config(claude_dir=str(tmp_path / "claude"))
    cfg.claude_path.mkdir()
    (cfg.claude_path / "settings.json").write_text(json.dumps({"model": "opus"}))
    installer.install(cfg, mode="legacy")
    written = json.loads((cfg.claude_path / "settings.json").read_text())
    assert "enabledPlugins" not in written
    assert written["model"] == "opus"


def _modern_install_with_host(tmp_path, monkeypatch, version, validate_rc=0):
    import kindex.claude_install as installer
    import kindex.setup as setup
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return SimpleNamespace(stdout=f"{version} (Claude Code)\n", returncode=0)
        return SimpleNamespace(stdout="", stderr="hooks: refused" if validate_rc else "",
                               returncode=validate_rc)

    monkeypatch.setattr(setup, "_find_kin_path", lambda: "/usr/bin/kin")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(installer.subprocess, "run", run)
    cfg = Config(claude_dir=str(tmp_path / "claude"))
    cfg.claude_path.mkdir()
    return installer, cfg, calls


def test_mods_release_installs_when_the_host_validates_the_plugin(tmp_path, monkeypatch):
    installer, cfg, calls = _modern_install_with_host(tmp_path, monkeypatch, "2.1.290")
    install(cfg, mode="modern")
    state = json.loads((cfg.claude_path / "settings.json").read_text())
    assert state["enabledPlugins"]["kindex-modern@skills-dir"]
    # Mods are on by default from 2.1.287; the early-access switch is not written.
    assert "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS" not in state.get("env", {})
    assert any(argv[1:3] == ["plugin", "validate"] for argv in calls)


def test_a_host_that_refuses_the_plugin_blocks_the_modern_switch(tmp_path, monkeypatch):
    installer, cfg, _ = _modern_install_with_host(tmp_path, monkeypatch, "2.1.290", validate_rc=1)
    with pytest.raises(ValueError, match="refuses the Kindex plugin"):
        install(cfg, mode="modern")
    assert not (cfg.claude_path / "skills/kindex-modern").exists()


def test_modern_hook_modules_declare_themselves_es_modules():
    """Node takes a .ts file's module type from the nearest package.json; one
    above the plugin declaring commonjs made kindex.ts fail to load."""
    import kindex

    hooks = Path(kindex.__file__).parent / "claude_modern" / "hooks"
    assert json.loads((hooks / "package.json").read_text(encoding="utf-8"))["type"] == "module"


def test_a_host_between_early_access_and_mods_is_refused(tmp_path, monkeypatch):
    installer, cfg, calls = _modern_install_with_host(tmp_path, monkeypatch, "2.1.280")
    with pytest.raises(ValueError, match="predates mods"):
        install(cfg, mode="modern")
    assert not any(argv[1:3] == ["plugin", "validate"] for argv in calls)
