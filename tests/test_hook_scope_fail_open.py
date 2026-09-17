"""A configuration refusal must degrade a hook, never block the host.

The strict implicit-scope rule (3efd1b9) raises ValueError when an unscoped
invocation inside a repository finds durable work in both the home store and
the repository store. `_config` turned that into SystemExit(2) before main()'s
fail-open handler could see it, so Claude Code reported "Blocked by hook" on
every tool call. batch0 R2.1 already requires every hook surface to catch all
exceptions and exit 0, and R2.2 requires every degraded event in the ledger;
these probes hold the configuration-refusal path to both. Commands still
refuse with exit 2.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def ambiguous_world(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "acme-client-portal"
    for directory in (home, project / ".kin", home / ".config" / "kindex"):
        directory.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    (project / ".kin" / "config").write_text("name: fail-open-fixture\n")
    for data_dir, node_id in ((home / ".kindex", "home-node"),
                              (project / ".kin" / "local" / "kindex", "project-node")):
        store = Store(Config(data_dir=str(data_dir)))
        store.add_node(title=f"Synthetic {node_id}", node_id=node_id)
        store.close()
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""),
                                                  str(Path(__file__).resolve().parents[1] / "src")) if p),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return {"home": home, "project": project, "env": env}


def run_kin(world, *args, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args],
        cwd=world["project"], env=world["env"], input=stdin,
        text=True, capture_output=True, timeout=15, check=False,
    )


def degraded_ledger(world):
    """The ledger path the product derives when no config could be loaded,
    asked of the product in the environment the hook ran in. In-process it
    would read the developer's real global config: kindex.config computes its
    global config paths from HOME at import time."""
    result = subprocess.run(
        [sys.executable, "-c",
         "from kindex.config import degraded_ledger_path; print(degraded_ledger_path(None))"],
        cwd=world["project"], env=world["env"], text=True, capture_output=True,
        timeout=15, check=True,
    )
    return Path(result.stdout.strip())


def ledger_events(world):
    ledger = degraded_ledger(world)
    assert ledger.is_file(), "the refusal is recorded, not swallowed"
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def isolate_global_config(monkeypatch, home):
    import kindex.config as config
    monkeypatch.setenv("HOME", str(home))
    global_config = home / ".config" / "kindex" / "kin.yaml"
    monkeypatch.setattr(config, "_GLOBAL_PATHS", [global_config])
    return global_config


def test_non_hook_command_still_refuses_the_ambiguous_scope(ambiguous_world):
    result = run_kin(ambiguous_world, "status", "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Ambiguous Kindex scope" in result.stderr


def test_pre_tool_use_hook_degrades_instead_of_blocking(ambiguous_world):
    payload = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": str(ambiguous_world["project"]), "session_id": "s1",
    })
    result = run_kin(ambiguous_world, "attention-hook", "--adapter", "claude",
                     "--event", "PreToolUse", stdin=payload)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "" and "Error:" not in result.stderr
    event = next(e for e in ledger_events(ambiguous_world) if e["cmd"] == "attention-hook")
    assert event["error_class"] == "ConfigResolutionError"
    assert event["msg"].startswith("Ambiguous Kindex scope")


@pytest.mark.parametrize("command, payload", [
    (["prompt-check"], {"hook_event_name": "UserPromptSubmit", "prompt": "hello"}),
    (["stop-guard"], {"hook_event_name": "Stop", "stop_hook_active": False}),
])
def test_guard_type_hooks_fail_open_empty_and_record(ambiguous_world, command, payload):
    """R2.1: guard-type hooks give empty fail-open output and exit 0; a
    stop-guard that blocked here would loop the session on a config error."""
    payload = {**payload, "cwd": str(ambiguous_world["project"]), "session_id": "s1"}
    result = run_kin(ambiguous_world, *command, stdin=json.dumps(payload))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert any(e["cmd"] == command[0] and e["error_class"] == "ConfigResolutionError"
               for e in ledger_events(ambiguous_world))


def test_prime_degrades_to_its_single_line(ambiguous_world):
    result = run_kin(ambiguous_world, "prime", "--for", "hook", stdin="{}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ("# kindex degraded: ConfigResolutionError — "
                                     "session starting without memory context")


def test_cron_degrades_to_exit_zero_and_records(ambiguous_world):
    """R2.1 names the cron family among the surfaces that exit 0."""
    result = run_kin(ambiguous_world, "cron")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(e["cmd"] == "cron" for e in ledger_events(ambiguous_world))


def test_every_refusal_is_recorded(ambiguous_world):
    """R2.2: each degraded event is its own line."""
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "ls"},
                          "cwd": str(ambiguous_world["project"]), "session_id": "s1"})
    for _ in range(3):
        result = run_kin(ambiguous_world, "attention-hook", "--adapter", "claude",
                         "--event", "PreToolUse", stdin=payload)
        assert result.returncode == 0
    assert sum(e["cmd"] == "attention-hook" for e in ledger_events(ambiguous_world)) == 3


def test_status_surfaces_a_refusal_recorded_without_a_config(ambiguous_world, tmp_path):
    """R2.2/R2.3: a refusal recorded while configuration itself failed goes to
    the fixed ~/.kindex ledger; a later `kin status` running on a different,
    configured base still reports it."""
    base = tmp_path / "declared-base"
    (ambiguous_world["home"] / ".config" / "kindex" / "kin.yaml").write_text(f"data_dir: {base}\n")
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello",
                          "cwd": str(ambiguous_world["project"]), "session_id": "s1"})
    refused = run_kin(ambiguous_world, "prompt-check", "--profile", "no-such-profile", stdin=payload)
    assert refused.returncode == 0 and refused.stdout == ""
    fixed_ledger = ambiguous_world["home"] / ".kindex" / "degraded.jsonl"
    assert "no-such-profile" in fixed_ledger.read_text()
    status = run_kin(ambiguous_world, "status", "--json")
    assert status.returncode == 0, status.stdout + status.stderr
    report = json.loads(status.stdout)
    assert report["degraded_7d"] >= 1
    assert report["degraded_last"]["cmd"] == "prompt-check"


def test_status_reads_both_ledgers(tmp_path, monkeypatch):
    """A config-less refusal (fixed ~/.kindex) and a configured one (the
    store's base) are both surfaced."""
    from kindex.config import read_degraded_events, record_degraded
    isolate_global_config(monkeypatch, tmp_path)
    record_degraded("prompt-check", ValueError("recorded without a config"))
    project_cfg = Config(data_dir=str(tmp_path / "repo" / ".kin" / "local" / "kindex"))
    record_degraded("attention-hook", ValueError("recorded with a config"), config=project_cfg)
    # Timestamps are to the second; both must be found, order within a second is not meaningful.
    messages = sorted(event["msg"] for event in read_degraded_events(project_cfg))
    assert messages == ["recorded with a config", "recorded without a config"]
    assert read_degraded_events(project_cfg, override_dir=str(tmp_path / "elsewhere")) == [], \
        "an explicit --data-dir reads only its own ledger"


def test_composed_read_counts_a_shared_ledger_once(tmp_path, monkeypatch):
    """When the configured base is ~/.kindex itself, or reaches it through a
    link, the ledger is read once, so one event is counted once."""
    from kindex.config import read_degraded_events, record_degraded
    isolate_global_config(monkeypatch, tmp_path)
    record_degraded("prompt-check", ValueError("one event"))
    assert [e["msg"] for e in read_degraded_events(Config(data_dir="~/.kindex"))] == ["one event"]
    (tmp_path / "linked").symlink_to(tmp_path / ".kindex")
    assert [e["msg"] for e in read_degraded_events(Config(data_dir=str(tmp_path / "linked")))] == ["one event"]


def test_ledger_trim_keeps_mode_0600(tmp_path, monkeypatch):
    """The cap's rewrite used a plain write, so the replacement took the
    umask's mode and a 0600 ledger came back 0644 after the first trim."""
    import kindex.config as config
    from kindex.config import degraded_ledger_path, record_degraded
    isolate_global_config(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_DEGRADED_MAX_BYTES", 256)
    monkeypatch.setattr(config, "_DEGRADED_KEEP_LINES", 3)
    old_umask = os.umask(0o022)
    try:
        for index in range(40):
            record_degraded("prompt-check", ValueError(f"condition {index}"))
    finally:
        os.umask(old_umask)
    ledger = degraded_ledger_path(None)
    assert len(ledger.read_text().splitlines()) <= 4, "the cap ran"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
