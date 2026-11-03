"""Antigravity sessions get Kindex, and health can tell when they use it.

The shapes here are what agy 1.4.12 was observed to send and write: hooks run in
the directory holding hooks.json with workspacePaths listing the launch directory
first, and MCP calls appear in the transcript as one generic call_mcp_tool.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from kindex.agent_adapters import hook_project_path
from kindex.config import Config
from kindex.store import Store
from kindex.supervisor_health_activity import _call_uses, observe_activity

SRC = str(Path(__file__).resolve().parents[1] / "src")
SESSION = "11111111-2222-4333-8444-555555555555"


def git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    return str(path)


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "kindex").mkdir(parents=True)
    hook_dir = home / ".gemini" / "config"
    hook_dir.mkdir(parents=True)
    health = tmp_path / "health"
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_STATE_HOME": str(home / ".local" / "state"), "KIN_HEALTH_DIR": str(health),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""), SRC) if p),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return {"tmp": tmp_path, "home": home, "hook_dir": hook_dir, "health": health, "env": env}


def hook(world, *args, payload):
    return subprocess.run([sys.executable, "-m", "kindex.cli", *args], cwd=world["hook_dir"],
                          env=world["env"], input=json.dumps(payload), text=True,
                          capture_output=True, timeout=60)


def messages(result):
    if not result.stdout.strip():
        return ""
    return " ".join(step["ephemeralMessage"] for step in json.loads(result.stdout).get("injectSteps", []))


def receipts(world):
    import sqlite3
    conn = sqlite3.connect(world["health"] / "health.sqlite3")
    try:
        return {row[0] for row in conn.execute(
            "SELECT s.project_path FROM scopes s JOIN events e ON e.scope_id=s.id WHERE e.kind='hook'")}
    finally:
        conn.close()


# ── which workspace a session belongs to ──────────────────────────────


def test_first_workspace_is_the_session_project(tmp_path):
    launch, added = git_repo(tmp_path / "launch"), git_repo(tmp_path / "added")
    assert hook_project_path({"workspacePaths": [launch, added]}) == str(Path(launch).resolve())


def test_a_run_directory_outside_git_yields_to_its_added_repository(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    repo = git_repo(tmp_path / "repo")
    assert hook_project_path({"workspacePaths": [str(run), repo]}) == str(Path(repo).resolve())


def test_no_workspace_in_git_stays_with_the_launch_directory(tmp_path):
    run, added = tmp_path / "run", tmp_path / "added"
    run.mkdir()
    added.mkdir()
    assert hook_project_path({"workspacePaths": [str(run), str(added)]}) == str(run.resolve())


def test_other_hosts_multiple_roots_stay_a_refusal(tmp_path):
    roots = [git_repo(tmp_path / "a"), git_repo(tmp_path / "b")]
    with pytest.raises(ValueError, match="multiple workspaces"):
        hook_project_path({"workspace_roots": roots})


def test_every_workspace_entry_is_still_validated(tmp_path):
    with pytest.raises(ValueError, match="explicit workspace"):
        hook_project_path({"workspacePaths": [git_repo(tmp_path / "a"), "relative/path"]})


# ── hooks with an --add-dir launch ────────────────────────────────────


def test_prime_uses_the_workspace_and_not_the_hook_directory(world):
    project = git_repo(world["tmp"] / "lighthouse")
    added = git_repo(world["tmp"] / "unrelated")
    store = Store(Config(data_dir=str(world["home"] / ".kindex")))
    store.add_node("Lighthouse beacon protocol", content="lighthouse uses a rotating beacon",
                   node_type="concept")
    store.add_node("Config file layout", content="config files live under config",
                   node_type="concept")
    store.close()

    result = hook(world, "agent-prime-hook", "--adapter", "antigravity", "--client", "antigravity",
                  "--data-dir", str(world["home"] / ".kindex"),
                  payload={"conversationId": SESSION, "invocationNum": 0, "workspacePaths": [project, added]})

    assert result.returncode == 0, result.stderr
    text = messages(result)
    assert "degraded" not in text
    # Recent activity lists every new node, so only the topic's section is evidence.
    concepts = text.split("### Key concepts")[1].split("###")[0]
    assert "Lighthouse beacon protocol" in concepts
    assert "Config file layout" not in concepts


def test_supervisor_receipt_lands_where_native_activity_is_observed(world):
    repo = git_repo(world["tmp"] / "repo")
    launch = Path(repo) / ".factory" / "runs" / "r1"
    launch.mkdir(parents=True)
    added = git_repo(world["tmp"] / "source")

    result = hook(world, "supervisor-hook", "--adapter", "antigravity",
                  payload={"conversationId": SESSION, "invocationNum": 0, "workspacePaths": [str(launch), added]})

    assert result.returncode == 0, result.stderr
    assert "unavailable" not in result.stdout
    assert {str(Path(repo).resolve()), str(launch.resolve())} <= receipts(world)


def test_workspace_outside_git_is_quiet_and_still_leaves_a_receipt(world):
    (world["home"] / ".config" / "kindex" / "kin.yaml").write_text("sim:\n  enabled: true\n")
    plain = world["tmp"] / "plain"
    plain.mkdir()

    result = hook(world, "supervisor-hook", "--adapter", "antigravity",
                  payload={"conversationId": SESSION, "invocationNum": 0, "workspacePaths": [str(plain)]})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    import sqlite3
    conn = sqlite3.connect(world["health"] / "health.sqlite3")
    details = [json.loads(row[0]) for row in conn.execute("SELECT details FROM events WHERE kind='hook'")]
    conn.close()
    assert details and all(d["reason"] == "no_worktree" for d in details)
    assert str(plain.resolve()) in receipts(world)


@pytest.mark.parametrize("command", [
    ("agent-prime-hook", "--adapter", "antigravity", "--client", "antigravity"),
    ("supervisor-hook", "--adapter", "antigravity"),
])
def test_a_diagnostic_is_said_once_per_turn(world, command):
    broken = {"conversationId": SESSION, "workspacePaths": ["relative/path"]}

    first = hook(world, *command, payload={**broken, "invocationNum": 0})
    repeat = hook(world, *command, payload={**broken, "invocationNum": 4})

    assert first.returncode == repeat.returncode == 0
    assert messages(first)
    assert repeat.stdout.strip() == ""


# ── native use evidence ───────────────────────────────────────────────


@pytest.mark.parametrize("call,expected", [
    ({"name": "call_mcp_tool", "args": {"ServerName": "kindex", "ToolName": "search"}}, ["search"]),
    ({"name": "call_mcp_tool", "args": {"ServerName": "kindex", "ToolName": "query_context"}}, ["context"]),
    ({"name": "call_mcp_tool", "args": {"ServerName": "herald", "ToolName": "search"}}, []),
    ({"name": "call_mcp_tool", "args": "not a mapping"}, []),
    ({"name": "call_mcp_tool"}, []),
    ({"name": "run_command", "args": {"CommandLine": "source ~/.profile; kin add 'a note'"}}, ["add"]),
    ({"name": "run_command", "args": {"CommandLine": "ls -la"}}, []),
    ({"name": "read_file", "args": {"Path": "/kindex/search"}}, []),
])
def test_antigravity_calls_count_as_kindex_use_only_when_they_are(call, expected):
    assert _call_uses(call) == expected


# ── the native observer ───────────────────────────────────────────────


def transcript(home, sid, *, kindex=True):
    logs = home / ".gemini" / "antigravity-cli" / "brain" / sid / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    calls = [{"name": "call_mcp_tool", "args": {"ServerName": "kindex", "ToolName": "search"}}] if kindex else []
    row = {"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": at, "tool_calls": calls}
    (logs / "transcript_full.jsonl").write_text(json.dumps(row) + "\n")


def history(home, sid, workspace):
    path = home / ".gemini" / "antigravity-cli" / "history.jsonl"
    with path.open("a") as out:
        out.write(json.dumps({"conversationId": sid, "workspace": workspace}) + "\n")


@pytest.fixture
def observer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini" / "antigravity-cli" / "brain").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(tmp_path / "health"))
    return home


def recorded(kind):
    from kindex.supervisor_health import _db
    with _db() as conn:
        return {(row[0], row[1]) for row in conn.execute(
            "SELECT s.session_id,s.project_path FROM scopes s JOIN events e ON e.scope_id=s.id WHERE e.kind=?", (kind,))}


def test_registered_automation_cannot_blind_the_observer(observer, tmp_path):
    from kindex.supervisor_health import register_reviewer_session
    project = tmp_path / "project"
    project.mkdir()
    for index in range(70):
        sid = f"automation-{index}"
        transcript(observer, sid, kindex=False)
        register_reviewer_session("antigravity", sid, str(project))
    transcript(observer, SESSION)
    history(observer, SESSION, str(project))

    coverage = observe_activity(time.time())["antigravity"]

    assert coverage["state"] == "observed" and coverage["sessions"] == 1
    assert recorded("use") == {(SESSION, str(project.resolve()))}


def test_many_old_conversations_do_not_blind_the_observer(observer, tmp_path):
    brain = observer / ".gemini" / "antigravity-cli" / "brain"
    for index in range(600):
        (brain / f"old-{index}").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    transcript(observer, SESSION)
    history(observer, SESSION, str(project))

    assert observe_activity(time.time())["antigravity"]["sessions"] == 1


def test_host_record_outranks_a_hook_receipt_at_the_repository_root(observer, tmp_path):
    from kindex.supervisor_health import record
    repo = tmp_path / "repo"
    launch = repo / "runs" / "r1"
    launch.mkdir(parents=True)
    record({"project_path": str(repo), "agent": "antigravity", "session_id": SESSION}, "hook", {"source": "hook"})
    transcript(observer, SESSION)
    history(observer, SESSION, str(launch))

    coverage = observe_activity(time.time())["antigravity"]

    assert coverage["unidentified"] == 0
    assert (SESSION, str(launch.resolve())) in recorded("activity")


def test_receipts_alone_identify_the_launch_directory(observer, tmp_path):
    from kindex.supervisor_health import record
    repo = tmp_path / "repo"
    launch = repo / "runs" / "r1"
    launch.mkdir(parents=True)
    for path in (repo, launch):
        record({"project_path": str(path), "agent": "antigravity", "session_id": SESSION}, "hook", {"source": "hook"})
    transcript(observer, SESSION)

    assert observe_activity(time.time())["antigravity"]["unidentified"] == 0
    assert (SESSION, str(launch.resolve())) in recorded("activity")


def test_unrelated_receipts_stay_unverified(observer, tmp_path):
    from kindex.supervisor_health import record
    for name in ("one", "two"):
        path = tmp_path / name
        path.mkdir()
        record({"project_path": str(path), "agent": "antigravity", "session_id": SESSION}, "hook", {"source": "hook"})
    transcript(observer, SESSION)

    assert observe_activity(time.time())["antigravity"]["unidentified"] == 1


def test_only_the_launch_workspace_identifies_a_cached_conversation(observer, tmp_path):
    launch, added = tmp_path / "launch", tmp_path / "added"
    launch.mkdir()
    added.mkdir()
    cache = observer / ".gemini" / "antigravity-cli" / "cache"
    cache.mkdir()
    (cache / "conversation_metadata.json").write_text(json.dumps({"conversations": {SESSION: {"summary": {
        "ID": SESSION, "WorkspaceURIs": [launch.as_uri(), added.as_uri()]}}}}))
    transcript(observer, SESSION)

    assert observe_activity(time.time())["antigravity"]["unidentified"] == 0
    assert (SESSION, str(launch.resolve())) in recorded("activity")
