"""The Codex installer changes only the handlers Kindex installed.

It matched an entry by the substring "kin prime" or "kindex" and replaced the
whole entry, dropping any other handler in it, and rewrote hooks.json with no
backup.
"""

from __future__ import annotations

import json

import pytest

from kindex.config import Config


@pytest.fixture
def codex(tmp_path, monkeypatch):
    from kindex import setup
    monkeypatch.setattr(setup, "_find_kin_path", lambda: "/usr/local/bin/kin")
    directory = tmp_path / ".codex"
    directory.mkdir()
    return directory, Config(data_dir=str(tmp_path / "data"), codex_dir=str(directory))


def write(directory, hooks):
    (directory / "hooks.json").write_text(json.dumps({"hooks": hooks}))


def commands(directory, event):
    data = json.loads((directory / "hooks.json").read_text())
    return [handler["command"] for entry in data["hooks"].get(event, [])
            for handler in entry["hooks"]]


def test_a_foreign_handler_sharing_an_entry_survives(codex):
    from kindex.setup import _kin_hook_command, install_codex_hooks
    directory, cfg = codex
    old = "/usr/local/bin/kin prime --for hook"
    write(directory, {"SessionStart": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "my-linter --init"},
        {"type": "command", "command": old},
    ]}]})
    install_codex_hooks(cfg)
    present = commands(directory, "SessionStart")
    assert "my-linter --init" in present
    assert old not in present
    assert _kin_hook_command("/usr/local/bin/kin",
                             ["prime", "--for", "hook", "--adapter", "codex"]) in present
    backups = list(directory.glob("hooks.kindex-backup-*.json"))
    assert len(backups) == 1 and old in backups[0].read_text()


def test_a_handler_that_only_mentions_kindex_is_not_ours(codex):
    from kindex.setup import install_codex_hooks, uninstall_codex_hooks
    directory, cfg = codex
    mention = "backup ~/.kindex nightly"
    write(directory, {"SessionStart": [{"hooks": [{"type": "command", "command": mention}]}],
                      "UserPromptSubmit": [{"hooks": [
                          {"type": "command", "command": "/usr/local/bin/kin attention-hook-wrapper"}]}]})
    install_codex_hooks(cfg)
    assert mention in commands(directory, "SessionStart")
    assert "/usr/local/bin/kin attention-hook-wrapper" in commands(directory, "UserPromptSubmit")
    uninstall_codex_hooks(cfg)
    assert commands(directory, "SessionStart") == [mention]
    assert commands(directory, "UserPromptSubmit") == ["/usr/local/bin/kin attention-hook-wrapper"]


def test_an_unchanged_install_does_not_rewrite(codex):
    from kindex.setup import install_codex_hooks
    directory, cfg = codex
    install_codex_hooks(cfg)
    before = (directory / "hooks.json").stat().st_mtime_ns
    actions = install_codex_hooks(cfg)
    assert all("already installed" in action for action in actions)
    assert (directory / "hooks.json").stat().st_mtime_ns == before
