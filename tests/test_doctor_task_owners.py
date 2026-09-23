"""Regression coverage for the opt-in legacy task-owner doctor repair."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from kindex import config as kindex_config
from kindex import tasks
from kindex.config import Config, load_config, resolve_agent_id
from kindex.store import Store
from kindex.task_service import execute, repair_task_owners, task_owner_findings


def _scope(tmp_path, agent="codex"):
    return {"project_path": str(tmp_path / "repo"), "session_id": "session-a", "agent": agent}


def _create(store, scope, operation_id, title, *, source_tool, owner="claude"):
    result = execute(
        store, "create", {"operation_id": operation_id, "title": title, "owner": owner}, scope,
        source_tool=source_tool,
    )
    assert result["ok"], result
    return result["task"]["id"]


def test_task_owner_repair_preserves_hook_claims_and_releases_expired_claims(tmp_path):
    store = Store(Config(data_dir=str(tmp_path / "data")))
    scope = _scope(tmp_path)
    try:
        legacy = _create(store, scope, "legacy", "legacy mcp", source_tool="kindex.task_execute")
        tasks.claim_task(store, legacy, "claude")
        hook = _create(store, scope, "hook", "live hook", source_tool="TaskCreate")
        tasks.claim_task(store, hook, "claude")
        unmarked = tasks.create_task(store, "unmarked legacy", owner="claude")
        tasks.claim_task(store, unmarked, "claude")
        with tasks.transaction(store):
            node = tasks.get_task(store, unmarked)
            node["extra"]["claim"]["expires_at"] = "2000-01-01T00:00:00"
            tasks._write_task(store, node)

        findings = {item["title"]: item for item in task_owner_findings(store, "codex")}
        assert findings["legacy mcp"]["classification"] == "legacy_task_execute"
        assert findings["unmarked legacy"]["classification"] == "possibly_stale"
        assert findings["live hook"]["classification"] == "hook_owned"
        assert not findings["live hook"]["repairable"]

        repaired = repair_task_owners(store, "codex")
        assert {item["title"] for item in repaired} == {"legacy mcp", "unmarked legacy"}
        legacy_extra = tasks.get_task(store, legacy)["extra"]
        assert legacy_extra["owner"] == "codex"
        assert legacy_extra["claim"]["agent"] == "codex"
        expired_extra = tasks.get_task(store, unmarked)["extra"]
        assert expired_extra["owner"] == "codex"
        assert "claim" not in expired_extra and expired_extra["task_status"] == "open"
        hook_extra = tasks.get_task(store, hook)["extra"]
        assert hook_extra["owner"] == "claude"
        assert hook_extra["claim"]["agent"] == "claude"
        assert store.conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='repair_task_owner'"
        ).fetchone()[0] == 2

        # The repaired stable identity can complete through task_execute's
        # session-qualified MCP path; a bare claude claim remains protected.
        completed = execute(store, "complete", {"operation_id": "finish", "id": legacy}, scope,
                            source_tool="kindex.task_execute")
        assert completed["ok"], completed
        blocked = execute(store, "complete", {"operation_id": "hook-finish", "id": hook},
                          _scope(tmp_path, "claude"), source_tool="TaskCreate")
        assert not blocked["ok"] and blocked["error"]["code"] == "task_claimed"
    finally:
        store.close()


def _doctor(repo, home, *args):
    env = os.environ.copy()
    for name in ("KIN_PROJECT", "KIN_PROJECT_PATH", "KIN_PROFILE", "KIN_AGENT_ID"):
        env.pop(name, None)
    env.update(
        HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        KIN_NO_SCHEDULER_WRITES="1",
    )
    return subprocess.run([sys.executable, "-m", "kindex.cli", "doctor", "--json", *args],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=30)


def test_doctor_skips_task_owner_scan_outside_a_git_worktree(tmp_path):
    plain, home = tmp_path / "plain", tmp_path / "home"
    plain.mkdir()
    home.mkdir()
    result = _doctor(plain, home)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_owner_scan"] == "skipped: no project store"


def test_doctor_does_not_create_or_register_a_fresh_project_store(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = _doctor(repo, home)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_owner_scan"] == "skipped: no project store"
    assert not (repo / ".kin").exists()
    assert not (home / ".local" / "state" / "kindex" / "project-graphs.json").exists()


def test_doctor_repairs_with_the_mcp_configured_agent_identity(tmp_path, monkeypatch):
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    project = Store(Config(data_dir=str(repo / ".kin" / "local" / "kindex")))
    try:
        task_id = _create(project, _scope(tmp_path), "legacy", "doctor target",
                          source_tool="kindex.task_execute")
        tasks.claim_task(project, task_id, "claude")
    finally:
        project.close()
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text("agent_id: configured-bot\n")

    report = _doctor(repo, home, "--fix-task-owners")
    assert report.returncode == 0, report.stderr
    repaired = json.loads(report.stdout)["task_owner_repairs"]
    assert repaired[0]["changes"] == [
        "owner: claude -> configured-bot", "claim.agent: claude -> configured-bot"]
    monkeypatch.setattr(kindex_config, "_GLOBAL_PATHS", [config_dir / "kin.yaml"])
    assert resolve_agent_id(load_config(project_path=repo)) == "configured-bot"
    checked = Store(Config(data_dir=str(repo / ".kin" / "local" / "kindex")))
    try:
        extra = tasks.get_task(checked, task_id)["extra"]
        assert extra["owner"] == extra["claim"]["agent"] == "configured-bot"
    finally:
        checked.close()
