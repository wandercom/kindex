"""Tests for system setup commands."""
import json
import subprocess
import sys
import pytest
from kindex.config import Config


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestSetupHooks:
    def test_setup_hooks_dry_run(self, tmp_path):
        """Dry run should not modify settings.json."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Create a fake claude dir
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        r = run("setup-hooks", "--dry-run", data_dir=d)
        assert r.returncode == 0

    def test_setup_hooks_installs(self, tmp_path):
        """Should install hooks into settings.json."""
        from kindex.setup import install_claude_hooks

        # Create a tmp claude dir
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text("{}")

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(claude_dir))
        actions = install_claude_hooks(cfg)

        assert any("SessionStart" in a for a in actions)
        assert any("PreCompact" in a for a in actions)
        assert any("UserPromptSubmit" in a for a in actions)

        # Verify settings file was updated
        data = json.loads(settings.read_text())
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]
        assert "UserPromptSubmit" in data["hooks"]
        assert "PreToolUse" in data["hooks"]
        assert "attention-hook" in str(data["hooks"]["PreToolUse"])
        assert "Stop" in data["hooks"]
        assert "stop-guard" not in str(data["hooks"]["Stop"])
        assert "compact-hook" in str(data["hooks"]["Stop"])
        assert "dream" in str(data["hooks"]["Stop"])

    def test_setup_hooks_can_disable_stop_dream(self, tmp_path):
        """Should omit stop-time dream when explicitly disabled."""
        from kindex.setup import install_claude_hooks

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text("{}")

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(claude_dir))
        cfg.reminders.dream_on_stop_enabled = False
        install_claude_hooks(cfg)

        data = json.loads(settings.read_text())
        assert "compact-hook" in str(data["hooks"]["Stop"])
        assert "dream" not in str(data["hooks"]["Stop"])

    def test_setup_hooks_idempotent(self, tmp_path):
        """Installing twice should not duplicate hooks."""
        from kindex.setup import install_claude_hooks

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}")

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(claude_dir))
        install_claude_hooks(cfg)
        actions2 = install_claude_hooks(cfg)

        assert any("already installed" in a for a in actions2)

    def test_setup_hooks_dry_run_does_not_write(self, tmp_path):
        """Dry run should not create or modify settings.json."""
        from kindex.setup import install_claude_hooks

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Don't create settings.json — dry run should not create it either

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(claude_dir))
        actions = install_claude_hooks(cfg, dry_run=True)

        # Should still report the actions it would take
        assert any("SessionStart" in a for a in actions)
        assert any("PreCompact" in a for a in actions)
        # But the "Wrote" action should not appear
        assert not any("Wrote" in a for a in actions)

    def test_setup_hooks_preserves_existing(self, tmp_path):
        """Should preserve existing settings when adding hooks."""
        from kindex.setup import install_claude_hooks

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"customSetting": True}))

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(claude_dir))
        install_claude_hooks(cfg)

        data = json.loads(settings.read_text())
        assert data["customSetting"] is True
        assert "hooks" in data


class TestSetupCodex:
    def test_setup_codex_mcp_installs(self, tmp_path):
        """Should install Kindex MCP server into Codex config.toml."""
        from kindex.setup import install_codex_mcp

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = codex_dir / "config.toml"
        config.write_text('[projects."/tmp/example"]\ntrust_level = "trusted"\n')

        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))
        actions = install_codex_mcp(cfg)

        assert any("Codex MCP" in a for a in actions)
        text = config.read_text()
        assert '[mcp_servers.kindex]' in text
        assert 'command = "kin-mcp"' in text
        assert 'trust_level = "trusted"' in text

    def test_setup_codex_mcp_idempotent(self, tmp_path):
        """Installing twice should not duplicate the Codex MCP block."""
        from kindex.setup import install_codex_mcp

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))

        install_codex_mcp(cfg)
        actions2 = install_codex_mcp(cfg)

        assert any("already installed" in a for a in actions2)
        text = (codex_dir / "config.toml").read_text()
        assert text.count("[mcp_servers.kindex]") == 1

    def test_setup_codex_mcp_dry_run_does_not_write(self, tmp_path):
        """Dry run should not create Codex config.toml."""
        from kindex.setup import install_codex_mcp

        codex_dir = tmp_path / ".codex"
        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))

        actions = install_codex_mcp(cfg, dry_run=True)

        assert any("Would add" in a for a in actions)
        assert not (codex_dir / "config.toml").exists()

    def test_uninstall_codex_mcp_removes_only_kindex_block(self, tmp_path):
        """Uninstall should preserve unrelated Codex config."""
        from kindex.setup import uninstall_codex_mcp

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config = codex_dir / "config.toml"
        config.write_text(
            '[projects."/tmp/example"]\n'
            'trust_level = "trusted"\n\n'
            '[mcp_servers.kindex]\n'
            'command = "kin-mcp"\n\n'
            '[mcp_servers.other]\n'
            'command = "other-mcp"\n'
        )

        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))
        actions = uninstall_codex_mcp(cfg)

        assert any("Removed" in a for a in actions)
        text = config.read_text()
        assert "[mcp_servers.kindex]" not in text
        assert "[mcp_servers.other]" in text
        assert 'trust_level = "trusted"' in text

    def test_setup_codex_hooks_installs(self, tmp_path):
        """Should install Kindex prompt hook into Codex hooks.json."""
        from kindex.setup import install_codex_hooks

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
            },
        }))

        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))
        actions = install_codex_hooks(cfg)

        assert any("Codex UserPromptSubmit" in a for a in actions)
        assert any("Codex PostToolUse" in a for a in actions)
        assert any("Codex SessionStart" in a for a in actions)
        data = json.loads(hooks_path.read_text())
        assert "Stop" in data["hooks"]
        prompt_hooks = data["hooks"]["UserPromptSubmit"]
        assert len(prompt_hooks) == 1
        assert "attention-hook" in prompt_hooks[0]["hooks"][0]["command"]
        assert "--deadline-ms 3500" in prompt_hooks[0]["hooks"][0]["command"]
        assert "--adapter" in prompt_hooks[0]["hooks"][0]["command"]
        assert "source ~/.profile" in prompt_hooks[0]["hooks"][0]["command"]
        post_hooks = data["hooks"]["PostToolUse"]
        assert len(post_hooks) == 1
        assert "attention-hook" in post_hooks[0]["hooks"][0]["command"]
        assert "--deadline-ms 3500" in post_hooks[0]["hooks"][0]["command"]
        # SessionStart hook injects the prime block (parity with Claude) via the
        # codex adapter so Codex gets the directive + .kin guidance at startup.
        session_hooks = data["hooks"]["SessionStart"]
        assert len(session_hooks) == 1
        session_cmd = session_hooks[0]["hooks"][0]["command"]
        assert "prime" in session_cmd and "--for hook" in session_cmd
        assert "--adapter codex" in session_cmd
        assert "source ~/.profile" in session_cmd

    def test_setup_opencode_hooks_installs_plugin(self, tmp_path):
        """Installs an auto-loaded OpenCode plugin that primes from the repo."""
        from kindex.setup import install_opencode_hooks

        oc_dir = tmp_path / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(oc_dir))
        actions = install_opencode_hooks(cfg)

        plugin = oc_dir / "plugin" / "kindex.js"
        assert plugin.exists(), actions
        js = plugin.read_text()
        # runs kin prime in the working directory and injects into the system prompt
        assert "experimental.chat.system.transform" in js
        assert "prime --for hook --adapter opencode" in js
        assert "experimental.session.compacting" in js
        assert ".cwd(directory)" in js          # primes IN the repo, not the server cwd
        assert any("OpenCode plugin" in a for a in actions)

    def test_setup_opencode_hooks_idempotent(self, tmp_path):
        from kindex.setup import install_opencode_hooks

        oc_dir = tmp_path / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(oc_dir))
        install_opencode_hooks(cfg)
        actions = install_opencode_hooks(cfg)
        assert actions == ["OpenCode plugin already installed"]

    def test_setup_opencode_hooks_dry_run_does_not_write(self, tmp_path):
        from kindex.setup import install_opencode_hooks

        oc_dir = tmp_path / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(oc_dir))
        actions = install_opencode_hooks(cfg, dry_run=True)
        assert any("Would write" in a for a in actions)
        assert not (oc_dir / "plugin" / "kindex.js").exists()

    def test_uninstall_opencode_hooks_removes_plugin(self, tmp_path):
        from kindex.setup import install_opencode_hooks, uninstall_opencode_hooks

        oc_dir = tmp_path / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(oc_dir))
        install_opencode_hooks(cfg)
        actions = uninstall_opencode_hooks(cfg)
        assert any("Removed" in a for a in actions)
        assert not (oc_dir / "plugin" / "kindex.js").exists()
        assert uninstall_opencode_hooks(cfg) == ["No Kindex OpenCode plugin found"]

    def test_prime_and_hooks_accept_opencode_adapter(self):
        """Regression: the OpenCode plugin runs `kin prime --adapter opencode`, so
        the argparse choices MUST include it — else the plugin gets an exit-2
        'invalid choice' and injects nothing."""
        from kindex.cli import build_parser

        parser = build_parser()
        for sub in ("prime", "attention-hook", "agent-prime-hook", "agent-stop-hook"):
            args = parser.parse_args([sub, "--adapter", "opencode"])
            assert args.adapter == "opencode", sub

    def test_setup_codex_hooks_idempotent(self, tmp_path):
        """Installing twice should not duplicate Codex prompt hook."""
        from kindex.setup import install_codex_hooks

        codex_dir = tmp_path / ".codex"
        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))

        install_codex_hooks(cfg)
        actions2 = install_codex_hooks(cfg)

        assert any("already installed" in a for a in actions2)
        data = json.loads((codex_dir / "hooks.json").read_text())
        assert len(data["hooks"]["UserPromptSubmit"]) == 1
        assert len(data["hooks"]["PostToolUse"]) == 1
        assert len(data["hooks"]["SessionStart"]) == 1

    def test_uninstall_codex_hooks_preserves_other_hooks(self, tmp_path):
        """Uninstall should remove only Kindex prompt-check hooks."""
        from kindex.setup import install_codex_hooks, uninstall_codex_hooks

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "echo other"}]},
                ],
            },
        }))
        cfg = Config(data_dir=str(tmp_path), codex_dir=str(codex_dir))
        install_codex_hooks(cfg)
        actions = uninstall_codex_hooks(cfg)

        assert any("Removed" in a for a in actions)
        data = json.loads(hooks_path.read_text())
        prompt_hooks = data["hooks"]["UserPromptSubmit"]
        assert len(prompt_hooks) == 1
        assert prompt_hooks[0]["hooks"][0]["command"] == "echo other"
        assert "PostToolUse" not in data["hooks"]
        assert "SessionStart" not in data["hooks"]

    def test_setup_codex_hooks_cli_dry_run(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-codex-hooks", "--dry-run", data_dir=d)
        assert r.returncode == 0


class TestSetupGemini:
    def test_install_gemini_mcp_writes_settings(self, tmp_path):
        from kindex.setup import install_gemini_mcp

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings = gemini_dir / "settings.json"
        settings.write_text(json.dumps({"theme": "dark"}))

        cfg = Config(data_dir=str(tmp_path), gemini_dir=str(gemini_dir))
        actions = install_gemini_mcp(cfg)

        assert any("Gemini MCP" in a for a in actions)
        data = json.loads(settings.read_text())
        assert data["theme"] == "dark"
        assert data["mcpServers"]["kindex"] == {"command": "kin-mcp", "args": []}

    def test_install_gemini_mcp_idempotent(self, tmp_path):
        from kindex.setup import install_gemini_mcp

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        cfg = Config(data_dir=str(tmp_path), gemini_dir=str(gemini_dir))

        install_gemini_mcp(cfg)
        actions2 = install_gemini_mcp(cfg)

        assert any("already installed" in a for a in actions2)

    def test_install_gemini_mcp_dry_run_does_not_write(self, tmp_path):
        from kindex.setup import install_gemini_mcp

        gemini_dir = tmp_path / ".gemini"
        cfg = Config(data_dir=str(tmp_path), gemini_dir=str(gemini_dir))
        actions = install_gemini_mcp(cfg, dry_run=True)
        assert any("Would add" in a for a in actions)
        assert not (gemini_dir / "settings.json").exists()

    def test_uninstall_gemini_mcp_preserves_other_servers(self, tmp_path):
        from kindex.setup import uninstall_gemini_mcp

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings = gemini_dir / "settings.json"
        settings.write_text(json.dumps({
            "theme": "dark",
            "mcpServers": {
                "kindex": {"command": "kin-mcp", "args": []},
                "other": {"command": "other-mcp", "args": ["--x"]},
            },
        }))

        cfg = Config(data_dir=str(tmp_path), gemini_dir=str(gemini_dir))
        actions = uninstall_gemini_mcp(cfg)

        assert any("Removed" in a for a in actions)
        data = json.loads(settings.read_text())
        assert "kindex" not in data["mcpServers"]
        assert "other" in data["mcpServers"]
        assert data["theme"] == "dark"

    def test_setup_gemini_mcp_cli_dry_run(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-gemini-mcp", "--dry-run", data_dir=d)
        assert r.returncode == 0


class TestSetupAntigravity:
    def test_install_antigravity_mcp_writes_both_global_configs(self, tmp_path):
        from kindex.setup import install_antigravity_mcp

        ag_dir = tmp_path / ".gemini" / "config"
        cli_dir = tmp_path / ".gemini" / "antigravity-cli"
        ag_dir.mkdir(parents=True)
        editor_config = ag_dir / "mcp_config.json"
        editor_config.write_text(json.dumps({
            "mcpServers": {
                "other": {"command": "other-mcp"},
            },
        }))

        cfg = Config(
            data_dir=str(tmp_path),
            antigravity_dir=str(ag_dir),
            antigravity_cli_dir=str(cli_dir),
        )
        actions = install_antigravity_mcp(cfg)

        assert any("Antigravity editor/shared" in a for a in actions)
        assert any("Antigravity CLI" in a for a in actions)
        editor = json.loads(editor_config.read_text())
        cli = json.loads((cli_dir / "mcp_config.json").read_text())
        assert editor["mcpServers"]["other"] == {"command": "other-mcp"}
        assert editor["mcpServers"]["kindex"] == {"command": "kin-mcp", "args": []}
        assert cli["mcpServers"]["kindex"] == {"command": "kin-mcp", "args": []}

    def test_uninstall_antigravity_mcp_preserves_other_servers(self, tmp_path):
        from kindex.setup import uninstall_antigravity_mcp

        ag_dir = tmp_path / ".gemini" / "config"
        cli_dir = tmp_path / ".gemini" / "antigravity-cli"
        ag_dir.mkdir(parents=True)
        cli_dir.mkdir(parents=True)
        for path in (ag_dir / "mcp_config.json", cli_dir / "mcp_config.json"):
            path.write_text(json.dumps({
                "mcpServers": {
                    "kindex": {"command": "kin-mcp", "args": []},
                    "other": {"command": "other-mcp"},
                },
            }))

        cfg = Config(
            data_dir=str(tmp_path),
            antigravity_dir=str(ag_dir),
            antigravity_cli_dir=str(cli_dir),
        )
        actions = uninstall_antigravity_mcp(cfg)

        assert any("Removed" in a for a in actions)
        for path in (ag_dir / "mcp_config.json", cli_dir / "mcp_config.json"):
            data = json.loads(path.read_text())
            assert "kindex" not in data["mcpServers"]
            assert "other" in data["mcpServers"]

    def test_install_antigravity_hooks_writes_schema(self, tmp_path):
        from kindex.setup import install_antigravity_hooks

        ag_dir = tmp_path / ".gemini" / "config"
        ag_dir.mkdir(parents=True)
        hooks = ag_dir / "hooks.json"
        hooks.write_text(json.dumps({"other-hook": {"enabled": True}}))
        cfg = Config(data_dir=str(tmp_path), antigravity_dir=str(ag_dir))

        actions = install_antigravity_hooks(cfg)

        assert any("Antigravity Kindex hooks" in a for a in actions)
        data = json.loads(hooks.read_text())
        assert "other-hook" in data
        block = data["kindex"]
        assert block["enabled"] is True
        assert "PreInvocation" in block
        assert "PreToolUse" in block
        assert "Stop" in block
        assert "agent-prime-hook" in str(block["PreInvocation"])
        assert "supervisor-hook" in str(block["PreInvocation"])
        assert "attention-hook" in str(block["PreToolUse"])
        assert "agent-stop-hook" in str(block["Stop"])
        assert "source ~/.profile" in str(block)

    def test_uninstall_antigravity_hooks_preserves_other_hooks(self, tmp_path):
        from kindex.setup import uninstall_antigravity_hooks

        ag_dir = tmp_path / ".gemini" / "config"
        ag_dir.mkdir(parents=True)
        hooks = ag_dir / "hooks.json"
        hooks.write_text(json.dumps({
            "other-hook": {"enabled": True},
            "kindex": {"enabled": True},
        }))
        cfg = Config(data_dir=str(tmp_path), antigravity_dir=str(ag_dir))

        actions = uninstall_antigravity_hooks(cfg)

        assert any("Removed" in a for a in actions)
        data = json.loads(hooks.read_text())
        assert "kindex" not in data
        assert "other-hook" in data

    def test_antigravity_setup_cli_dry_runs(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        mcp = run("setup-antigravity-mcp", "--dry-run", data_dir=d)
        hooks = run("setup-antigravity-hooks", "--dry-run", data_dir=d)
        assert mcp.returncode == 0
        assert hooks.returncode == 0


class TestSetupOpenCode:
    def test_install_opencode_mcp_writes_settings(self, tmp_path):
        from kindex.setup import install_opencode_mcp

        opencode_dir = tmp_path / ".config" / "opencode"
        opencode_dir.mkdir(parents=True)
        settings = opencode_dir / "opencode.json"
        settings.write_text(json.dumps({"theme": "tokyonight"}))

        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(opencode_dir))
        actions = install_opencode_mcp(cfg)

        assert any("OpenCode MCP" in a for a in actions)
        data = json.loads(settings.read_text())
        assert data["theme"] == "tokyonight"
        assert data["mcp"]["kindex"] == {
            "type": "local",
            "command": ["kin-mcp"],
            "enabled": True,
        }

    def test_install_opencode_mcp_idempotent(self, tmp_path):
        from kindex.setup import install_opencode_mcp

        opencode_dir = tmp_path / ".config" / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(opencode_dir))

        install_opencode_mcp(cfg)
        actions2 = install_opencode_mcp(cfg)

        assert any("already installed" in a for a in actions2)

    def test_install_opencode_mcp_dry_run_does_not_write(self, tmp_path):
        from kindex.setup import install_opencode_mcp

        opencode_dir = tmp_path / ".config" / "opencode"
        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(opencode_dir))
        actions = install_opencode_mcp(cfg, dry_run=True)
        assert any("Would add" in a for a in actions)
        assert not (opencode_dir / "opencode.json").exists()

    def test_uninstall_opencode_mcp_preserves_other_servers(self, tmp_path):
        from kindex.setup import uninstall_opencode_mcp

        opencode_dir = tmp_path / ".config" / "opencode"
        opencode_dir.mkdir(parents=True)
        settings = opencode_dir / "opencode.json"
        settings.write_text(json.dumps({
            "theme": "tokyonight",
            "mcp": {
                "kindex": {"type": "local", "command": ["kin-mcp"], "enabled": True},
                "other": {"type": "local", "command": ["other"], "enabled": True},
            },
        }))

        cfg = Config(data_dir=str(tmp_path), opencode_dir=str(opencode_dir))
        actions = uninstall_opencode_mcp(cfg)

        assert any("Removed" in a for a in actions)
        data = json.loads(settings.read_text())
        assert "kindex" not in data["mcp"]
        assert "other" in data["mcp"]

    def test_setup_opencode_mcp_cli_dry_run(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-opencode-mcp", "--dry-run", data_dir=d)
        assert r.returncode == 0


class TestSetupCursor:
    def test_install_cursor_mcp_writes_settings(self, tmp_path):
        from kindex.setup import install_cursor_mcp

        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()

        cfg = Config(data_dir=str(tmp_path), cursor_dir=str(cursor_dir))
        actions = install_cursor_mcp(cfg)

        assert any("Cursor MCP" in a for a in actions)
        data = json.loads((cursor_dir / "mcp.json").read_text())
        assert data["mcpServers"]["kindex"] == {"type": "stdio", "command": "kin-mcp"}

    def test_install_cursor_mcp_idempotent(self, tmp_path):
        from kindex.setup import install_cursor_mcp

        cursor_dir = tmp_path / ".cursor"
        cfg = Config(data_dir=str(tmp_path), cursor_dir=str(cursor_dir))

        install_cursor_mcp(cfg)
        actions2 = install_cursor_mcp(cfg)

        assert any("already installed" in a for a in actions2)

    def test_install_cursor_mcp_dry_run_does_not_write(self, tmp_path):
        from kindex.setup import install_cursor_mcp

        cursor_dir = tmp_path / ".cursor"
        cfg = Config(data_dir=str(tmp_path), cursor_dir=str(cursor_dir))
        actions = install_cursor_mcp(cfg, dry_run=True)
        assert any("Would add" in a for a in actions)
        assert not (cursor_dir / "mcp.json").exists()

    def test_uninstall_cursor_mcp_preserves_other_servers(self, tmp_path):
        from kindex.setup import uninstall_cursor_mcp

        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        settings = cursor_dir / "mcp.json"
        settings.write_text(json.dumps({
            "mcpServers": {
                "kindex": {"type": "stdio", "command": "kin-mcp"},
                "other": {"type": "stdio", "command": "other"},
            },
        }))

        cfg = Config(data_dir=str(tmp_path), cursor_dir=str(cursor_dir))
        actions = uninstall_cursor_mcp(cfg)

        assert any("Removed" in a for a in actions)
        data = json.loads(settings.read_text())
        assert "kindex" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_setup_cursor_mcp_cli_dry_run(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-cursor-mcp", "--dry-run", data_dir=d)
        assert r.returncode == 0

    def test_setup_cursor_rules_prints_block(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-cursor-rules", data_dir=d)
        assert r.returncode == 0
        assert "alwaysApply: true" in r.stdout
        assert "Kindex" in r.stdout


class TestSetupCron:
    def test_setup_cron_dry_run(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("setup-cron", "--dry-run", data_dir=d)
        assert r.returncode == 0
        assert "Would" in r.stdout

    def test_install_launchd_dry_run(self, tmp_path):
        """install_launchd with dry_run should not write plist."""
        from kindex.setup import install_launchd

        cfg = Config(data_dir=str(tmp_path))
        actions = install_launchd(cfg, dry_run=True)
        assert any("Would install" in a for a in actions)

    def test_install_crontab_dry_run(self, tmp_path):
        """install_crontab with dry_run should not modify crontab."""
        from kindex.setup import install_crontab
        from unittest.mock import patch, MagicMock

        cfg = Config(data_dir=str(tmp_path))

        mock_result = MagicMock()
        mock_result.returncode = 1  # no existing crontab
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            actions = install_crontab(cfg, dry_run=True)

        assert any("Would add crontab" in a for a in actions)

    def test_uninstall_launchd_dry_run(self, tmp_path):
        """uninstall_launchd with dry_run should not delete plist."""
        from kindex.setup import uninstall_launchd
        from unittest.mock import patch

        # The function checks Path.home() / "Library/LaunchAgents/com.kindex.cron.plist"
        # In dry run mode with no plist, it should say "No launchd plist found"
        actions = uninstall_launchd(dry_run=True)
        # It either finds the plist and says "Would remove" or doesn't find it
        assert len(actions) > 0
