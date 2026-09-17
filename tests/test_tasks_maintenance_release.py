"""Task claims, repo-local maintenance, the reinforce queue and weight decay.

- task_execute answers every refusal in its {ok, error} shape.
- A claim belongs to one host session; another session (or agent) cannot
  complete, cancel or reopen it away, and the holder can refresh it.
- A repo-local graph the modern lane opened is found by the maintenance
  sweep, which fires its reminders and does its local upkeep.
- A Stop hook's enqueue during a drain is kept, and one conversation is
  graded by one drain at a time.
- A decay run whose writes are all suppressed rewrites no accounting.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from kindex import tasks
from kindex.config import AttentionConfig, BudgetConfig, Config, LLMConfig
from kindex.store import Store
from kindex.task_service import execute


@pytest.fixture
def store(tmp_path):
    graph = Store(Config(data_dir=str(tmp_path / "data")))
    yield graph
    graph.close()


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_task_execute_answers_refusals_in_its_own_shape(tmp_path):
    from kindex import mcp_server

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    for project_path in ("relative/path", str(not_a_repo), str(tmp_path / "missing")):
        result = mcp_server.task_execute("list", {}, project_path, "session-1", agent="claude")
        assert result["ok"] is False, result
        assert result["error"]["code"] == "invalid_scope", result
    bad_session = mcp_server.task_execute("list", {}, str(_git_repo(tmp_path / "repo")),
                                          "bad session id", agent="claude")
    assert bad_session["ok"] is False and bad_session["error"]["code"] == "invalid_scope"


def _scope(tmp_path, session):
    return {"project_path": str(tmp_path / "repo"), "session_id": session, "agent": "claude"}


def test_a_claim_belongs_to_one_session(store, tmp_path):
    first, second = _scope(tmp_path, "session-a"), _scope(tmp_path, "session-b")
    created = execute(store, "create", {"operation_id": "c1", "title": "Ship parser"}, first)
    task_id = created["task"]["id"]
    claimed = execute(store, "claim", {"operation_id": "k1", "id": task_id}, first)
    assert claimed["ok"], claimed
    assert tasks.get_task(store, task_id)["extra"]["claim"]["agent"] == "claude:session-a"

    for operation in ("complete", "cancel"):
        refused = execute(store, operation, {"operation_id": f"x-{operation}", "id": task_id}, second)
        assert refused["ok"] is False and refused["error"]["code"] == "task_claimed", refused
    reopened = execute(store, "update", {"operation_id": "u1", "id": task_id, "status": "open"}, second)
    assert reopened["error"]["code"] == "task_claimed"
    released = execute(store, "release", {"operation_id": "r1", "id": task_id}, second)
    assert released["ok"] is False
    assert tasks.get_task(store, task_id)["extra"]["claim"]["agent"] == "claude:session-a"

    # The holder refreshes its own claim, and completes the task.
    refreshed = execute(store, "claim", {"operation_id": "k2", "id": task_id}, first)
    assert refreshed["ok"], refreshed
    done = execute(store, "complete", {"operation_id": "d1", "id": task_id}, first)
    assert done["ok"], done
    assert "claim" not in tasks.get_task(store, task_id)["extra"]


def test_a_legacy_status_change_needs_the_holder_or_force(store):
    task_id = tasks.create_task(store, "Rotate keys")
    tasks.claim_task(store, task_id, "worker-a")
    with pytest.raises(tasks.TaskClaimedError, match="claimed by worker-a"):
        tasks.complete_task(store, task_id, actor="worker-b")
    with pytest.raises(tasks.TaskClaimedError):
        tasks.update_task(store, task_id, task_status="open")
    # A field edit that ends no claim is anyone's.
    tasks.update_task(store, task_id, actor="worker-b", priority=1)
    assert tasks.get_task(store, task_id)["extra"]["claim"]["agent"] == "worker-a"
    cancelled = tasks.cancel_task(store, task_id, actor="worker-b", force=True)
    assert "claim" not in cancelled["extra"]


def test_the_sweep_serves_a_repo_local_graph_the_modern_lane_opened(tmp_path, monkeypatch):
    from kindex import daemon
    from kindex.integrations import open_project_store
    from kindex.project_store import registered_project_graphs
    from kindex.reminders import create_reminder

    monkeypatch.delenv("KIN_NO_SCHEDULER_WRITES", raising=False)
    repo = _git_repo(tmp_path / "service")
    graph = open_project_store({"project_path": str(repo), "session_id": "s1", "agent": "claude"})
    try:
        reminder = create_reminder(graph, "Rotate the deploy key", "in 1 minute")
        graph.conn.execute("UPDATE reminders SET next_due = '2000-01-01T00:00:00' WHERE id = ?",
                           (reminder,))
        graph.conn.commit()
        data_dir = str(graph.config.data_path)
    finally:
        graph.close()
    assert registered_project_graphs() == {str(repo.resolve()): data_dir}

    monkeypatch.setattr(daemon, "_check_reminders",
                        lambda cfg, store, verbose=False: {"fired": 1 if str(cfg.data_path) == data_dir else 0})
    upkeep = []
    monkeypatch.setattr(daemon, "_project_housekeeping", lambda store: upkeep.append(str(store.config.data_path)))
    base = Config(data_dir=str(tmp_path / "base"))
    results = daemon.remind_check_all(base)
    assert any(entry["profile"] == str(repo.resolve()) and entry["fired"] == 1 for entry in results), results
    assert upkeep == [data_dir]


def test_a_scan_registers_a_repo_local_graph_without_a_data_dir(tmp_path, store):
    from kindex.ingest import scan_kin_files

    root = tmp_path / "projects"
    repo = root / "service"
    local = repo / ".kin" / "local" / "kindex"
    local.mkdir(parents=True)
    Store(Config(data_dir=str(local))).close()
    (local / "kindex.db").touch()
    config = Config(data_dir=store.config.data_dir, project_dirs=[str(root)])
    scan_kin_files(config, store)
    registry = json.loads(store.get_meta("project_graph_dirs"))
    assert registry == {str(repo): str(local.resolve())}


class _Messages:
    def __init__(self, on_call):
        self.on_call = on_call
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.on_call()
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps({"observed": [], "missed": []}))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _reinforce_config(tmp_path):
    return Config(data_dir=str(tmp_path / "graph"), llm=LLMConfig(enabled=True),
                  budget=BudgetConfig(daily=1.0, weekly=5.0, monthly=10.0),
                  attention=AttentionConfig(enabled=True))


def test_an_enqueue_during_a_drain_is_kept(tmp_path, monkeypatch):
    from kindex.reinforce import REINFORCE_QUEUE_META, drain_reinforce_queue, enqueue_reinforce

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _reinforce_config(tmp_path)
    graph = Store(cfg)
    try:
        enqueue_reinforce(graph, "conv-1", trace="the agent used the rule")
        client = SimpleNamespace(messages=_Messages(
            lambda: enqueue_reinforce(graph, "conv-2", trace="a later session")))
        result = drain_reinforce_queue(graph, cfg, client=client)
        assert result["pending"] == 1
        queued = [job["conversation_id"] for job in json.loads(graph.get_meta(REINFORCE_QUEUE_META))]
        assert queued == ["conv-2"]
    finally:
        graph.close()


def test_one_conversation_is_graded_by_one_drain(tmp_path, monkeypatch):
    from kindex import reinforce

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _reinforce_config(tmp_path)
    graph = Store(cfg)
    try:
        assert reinforce._claim_inflight(graph, "conv-1")
        client = SimpleNamespace(messages=_Messages(lambda: None))
        held = reinforce.reinforce_session(graph, cfg, "conv-1", "a trace", client=client)
        assert held["status"] == "in_flight"
        assert client.messages.calls == 0
        reinforce._release_inflight(graph, "conv-1")
        graded = reinforce.reinforce_session(graph, cfg, "conv-1", "a trace", client=client)
        assert graded["status"] == "ok"
        assert client.messages.calls == 1
        assert graph.get_meta(reinforce.REINFORCE_INFLIGHT_PREFIX + "conv-1") is None
    finally:
        graph.close()


def test_a_suppressed_decay_run_rewrites_no_accounting(store):
    for n in range(20):
        store.add_node(f"note {n}", "body", node_id=f"n{n}", weight=0.5)
    store.conn.execute("UPDATE nodes SET last_accessed = '2000-01-01T00:00:00'")
    store.conn.commit()
    store.apply_weight_decay()  # cold start stamps the checkpoint

    def keys():
        return {row[0]: row[1] for row in store.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE '\\_wtr.node.%' ESCAPE '\\'")}

    store.apply_weight_decay()  # seconds later: every write is suppressed
    first = keys()
    assert set(first) == {f"_wtr.node.n{n}" for n in range(20)}
    before = store.conn.total_changes
    store.apply_weight_decay()
    assert keys() == first, "a still-valid snapshot is not rewritten"
    assert store.conn.total_changes - before <= 2, "only the run stamp is written"

    store.delete_node("n0")
    store.apply_weight_decay()
    assert "_wtr.node.n0" not in keys()
