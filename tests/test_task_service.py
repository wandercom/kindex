"""Durable task acceptance tests: replay, scope, transitions and atomicity."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from kindex.config import Config
from kindex.store import Store
from kindex import tasks
from kindex.task_service import execute, pending_outcomes, acknowledge_outcome, lookup_completed, TaskServiceError


@pytest.fixture
def store(tmp_path):
    instance = Store(Config(data_dir=str(tmp_path / "data")))
    yield instance
    instance.close()


@pytest.fixture
def scope(tmp_path):
    return {"project_path": str(tmp_path / "repo"), "session_id": "session-a", "agent": "agent-a"}


def create(store, scope, operation_id="create-a", **fields):
    result = execute(store, "create", {"operation_id": operation_id, "title": "Ship parser", **fields}, scope)
    assert result["ok"], result
    return result["task"]


def test_reopen_and_terminal_claim_lifecycle(store):
    task_id = tasks.create_task(store, "work")
    tasks.claim_task(store, task_id, "worker")
    done = tasks.update_task(store, task_id, task_status="done")
    assert "claim" not in done["extra"]
    assert done["status"] == "archived"
    # Repeated completion preserves completion instant and version.
    assert tasks.complete_task(store, task_id)["extra"] == done["extra"]
    with pytest.raises(ValueError, match="reopen"):
        tasks.claim_task(store, task_id, "worker")
    reopened = tasks.update_task(store, task_id, task_status="open")
    assert reopened["status"] == "active"
    assert "completed_at" not in reopened["extra"]
    assert tasks.list_tasks(store)[0]["id"] == task_id
    tasks.update_task(store, task_id, task_status="cancelled")
    assert tasks.get_task(store, task_id)["status"] == "archived"


def test_empty_release_does_not_bypass_claim_owner(store):
    task_id = tasks.create_task(store, "owned")
    tasks.claim_task(store, task_id, "worker")
    with pytest.raises(ValueError, match="claimed by worker"):
        tasks.release_task_claim(store, task_id)
    assert tasks.get_task(store, task_id)["extra"]["claim"]["agent"] == "worker"


def test_due_parsing_and_invalid_links_are_atomic(store):
    task_id = tasks.create_task(store, "due", due="tomorrow")
    due = tasks.get_task(store, task_id)["extra"]["due"]
    assert "T" in due and due != "tomorrow"
    assert tasks.compute_task_weight(1, "2026-01-01T00:00:00+00:00") >= .9
    with pytest.raises(ValueError, match="link target"):
        tasks.create_task(store, "orphan", link_to=["missing-target"])
    assert not store.get_node_by_title("orphan")
    with pytest.raises(ValueError):
        tasks.create_task(store, "invalid date", due="not a date")
    assert not store.get_node_by_title("invalid date")


def test_filters_apply_before_window_and_path_is_component_based(store, scope):
    # A backlog beyond the old 500-node window must not hide the matching task.
    with tasks.transaction(store):
        for i in range(505):
            tasks.create_task(store, f"global-{i}", priority=1, scope="global")
        selected = tasks.create_task(store, "matching", priority=5, project_path=scope["project_path"])
    assert [node["id"] for node in tasks.list_tasks(store, scope="contextual")] == [selected]
    sibling = scope["project_path"] + "2"
    assert selected not in {node["id"] for node in tasks.list_tasks(store, project_path=sibling, limit=None)}


def test_retry_returns_original_committed_result_and_conflict_rejects(store, scope):
    original = create(store, scope)
    updated = execute(store, "update", {"operation_id": "update-a", "id": original["id"], "title": "Updated"}, scope)
    assert updated["task"]["version"] == 2
    store.close()
    replay = execute(store, "create", {"operation_id": "create-a", "title": "Ship parser"}, scope)
    assert replay["replayed"]
    assert replay["task"] == original
    conflict = execute(store, "create", {"operation_id": "create-a", "title": "Another"}, scope)
    assert conflict["error"]["code"] == "operation_conflict"
    assert len(tasks.list_tasks(store, status="all", limit=None)) == 1


@pytest.mark.parametrize("linked", [False, True])
def test_concurrent_retries_commit_one_task_and_receipt(store, scope, linked):
    store.conn  # initialize schema before the competing operation
    target = store.add_node("context") if linked else None
    barrier = Barrier(2)

    def run():
        other = Store(store.config)
        try:
            other.conn
            barrier.wait()
            return execute(other, "create", {"operation_id": "same-op", "title": "Concurrent",
                           "link_to": [target] if linked else []}, scope)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert all(result["ok"] for result in results), results
    assert {result["replayed"] for result in results} == {False, True}
    assert len({result["task"]["id"] for result in results}) == 1
    assert len(tasks.list_tasks(store, status="all", limit=None)) == 1


def test_link_validation_does_not_commit_an_ambient_transaction(store):
    target = store.add_node("linked context")
    with pytest.raises(ValueError, match="link target"):
        with tasks.transaction(store):
            tasks.create_task(store, "must roll back")
            tasks.create_task(store, "invalid linked", link_to=[target, "missing"])
    assert tasks.list_tasks(store, status="all", limit=None) == []


def test_linked_service_create_has_one_final_commit(store, scope):
    target = store.add_node("linked context")
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        created = create(store, scope, link_to=[target])
    finally:
        store.conn.set_trace_callback(None)
    assert sum(sql.strip().upper() == "COMMIT" for sql in statements) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM edges WHERE from_id=? OR to_id=?",
                              (created["id"], created["id"])).fetchone()[0] == 2


def test_concurrent_cas_has_exactly_one_winner(store, scope):
    original = create(store, scope)
    barrier = Barrier(2)

    def update(index):
        other = Store(store.config)
        try:
            other.conn
            barrier.wait()
            return execute(other, "update", {"operation_id": f"update-{index}", "id": original["id"],
                           "expected_version": original["version"], "title": f"Winner {index}"}, scope)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, range(2)))
    assert sum(result["ok"] for result in results) == 1
    assert [result["error"]["code"] for result in results if not result["ok"]] == ["version_conflict"]
    assert tasks.get_task(store, original["id"])["extra"]["task_version"] == 2


@pytest.mark.parametrize("failed_prefix", ["task.operation.", "task.outcome."])
def test_receipt_or_outbox_failure_rolls_back_all_effects(store, scope, failed_prefix):
    store.conn.execute(f"""CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON meta
        WHEN NEW.key LIKE '{failed_prefix}%' BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END""")
    auth = {"operation_id": "create-a", "input_digest": "input", "scope_digest": "scope", "policy_revision": "rev1"}
    result = execute(store, "create", {"operation_id": "create-a", "title": "must roll back"},
                     {**scope, "authorization_receipt": auth})
    assert result["error"]["code"] == "unavailable"
    assert tasks.list_tasks(store, status="all", limit=None) == []
    assert store.conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'task.%'").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 0


def test_outcome_delivery_retry_preserves_original_authorization(store, scope):
    auth = {"operation_id": "create-a", "input_digest": "input", "scope_digest": "scope",
            "policy_revision": "rev1", "authorized_at": "first"}
    args = {"operation_id": "create-a", "title": "Ship parser"}
    first = execute(store, "create", args, {**scope, "authorization_receipt": auth})
    assert first["ok"]
    reauthorized = {**auth, "authorized_at": "second"}
    second = execute(store, "create", args, {**scope, "authorization_receipt": reauthorized})
    assert second["replayed"]
    outcomes = pending_outcomes(store, scope)
    assert len(outcomes) == 1
    assert outcomes[0]["authorization_receipt"] == auth
    assert outcomes[0]["result"] == first
    assert pending_outcomes(store, {**scope, "session_id": "other"}) == []
    assert acknowledge_outcome(store, scope, "create-a")
    assert acknowledge_outcome(store, scope, "create-a")
    assert pending_outcomes(store, scope) == []
    bad = execute(store, "create", args, {**scope, "authorization_receipt": {**auth, "operation_id": "different"}})
    assert bad["error"]["code"] == "authorization_mismatch"


def test_scope_rejects_implicit_project_and_cross_repo_dependencies(store, scope):
    item = create(store, scope)
    other = {**scope, "project_path": scope["project_path"] + "2"}
    assert execute(store, "get", {"id": item["id"]}, other)["error"]["code"] == "not_found"
    assert execute(store, "get", {"id": item["id"]}, {})["error"]["code"] == "invalid_scope"
    blocked = execute(store, "create", {"operation_id": "dep", "title": "other", "dependencies": [item["id"]]}, other)
    assert blocked["error"]["code"] == "not_found"
    assert execute(store, "get", {"id": item["id"]}, {**scope, "profile": "wrong"})["error"]["code"] == "scope_mismatch"


def test_dependencies_and_unknown_fields_cannot_be_silently_lost(store, scope):
    first = create(store, scope)
    second = create(store, scope, "create-b", dependencies=[first["id"]])
    cycle = execute(store, "update", {"operation_id": "cycle", "id": first["id"], "dependencies": [second["id"]]}, scope)
    assert not cycle["ok"]
    assert tasks.get_task(store, first["id"])["extra"]["dependencies"] == []
    unsupported = execute(store, "update", {"operation_id": "meta", "id": first["id"], "metadata": {"x": 1}}, scope)
    assert unsupported["error"]["code"] == "unsupported_argument"


def test_paginated_list_is_complete_in_its_scope(store, scope):
    created = {create(store, scope, f"create-{i}")["id"] for i in range(5)}
    create(store, {**scope, "project_path": scope["project_path"] + "2"}, "outside")
    cursor, seen = "", []
    while True:
        page = execute(store, "list", {"limit": 2, "cursor": cursor}, scope)
        seen.extend(task["id"] for task in page["tasks"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == created


def test_todowrite_reconciliation_is_scoped_and_atomic(store, scope):
    unrelated = create(store, scope)
    args = {"operation_id": "batch-1", "namespace": "claude-todo", "items": [
        {"external_id": "a", "title": "A", "status": "open"},
        {"external_id": "b", "title": "B", "status": "in_progress"}]}
    first = execute(store, "reconcile", args, scope)
    assert first["ok"], first
    assert execute(store, "reconcile", args, scope)["replayed"]
    next_args = {"operation_id": "batch-2", "namespace": "claude-todo", "cancel_missing": True,
                 "items": [{"external_id": "a", "title": "Renamed", "status": "done"}]}
    changed = execute(store, "reconcile", next_args, scope)
    assert changed["ok"], changed
    assert changed["tasks"][0]["id"] == first["tasks"][0]["id"]
    assert changed["cancelled"][0]["id"] == first["tasks"][1]["id"]
    assert tasks.get_task(store, unrelated["id"])["extra"]["task_status"] == "open"
    before = tasks.get_task(store, first["tasks"][0]["id"])
    failure = execute(store, "reconcile", {"operation_id": "batch-3", "namespace": "claude-todo", "items": [
        {"external_id": "a", "title": "Rollback me"}, {"external_id": "c", "title": ""}]}, scope)
    assert not failure["ok"]
    assert tasks.get_task(store, first["tasks"][0]["id"]) == before
    assert len(tasks.list_tasks(store, status="all", limit=None)) == 3


def test_task_and_receipt_never_persist_credential_text(store, scope):
    secret = "sk-ant-api03-" + "a" * 48
    result = create(store, scope, content="Authorization: Bearer " + secret)
    assert secret not in json.dumps(result)
    for table, column in (("nodes", "content"), ("meta", "value"), ("activity_log", "details")):
        assert all(secret not in str(row[0]) for row in store.conn.execute(f"SELECT {column} FROM {table}"))


def test_cli_update_preserves_unspecified_priority_and_scope(store, monkeypatch):
    from kindex import cli
    task_id = tasks.create_task(store, "Urgent global task", priority=1, scope="global")
    monkeypatch.setattr(cli, "_store", lambda args: store)
    args = cli.build_parser().parse_args(["task", "update", "--task-id", task_id, "--content", "More detail"])
    cli.cmd_task(args)
    node = tasks.get_task(store, task_id)
    assert node["content"] == "More detail"
    assert node["extra"]["priority"] == 1
    assert node["extra"]["scope"] == "global"
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_task(cli.build_parser().parse_args(["task", "done", "--task-id", "missing"]))
    assert stopped.value.code == 2


def test_mcp_task_api_preserves_legacy_text_and_adds_structured_updates(store, monkeypatch):
    from kindex import mcp_server as mcp
    monkeypatch.setattr(mcp, "_get_store", lambda: (store, store.config))
    monkeypatch.setattr(mcp, "_default_agent", lambda agent="": agent or "worker")
    assert mcp.task_add("MCP task").startswith("Created task:")
    task_id = tasks.list_tasks(store)[0]["id"]
    initial = mcp.task_get(task_id)
    updated = mcp.task_update(task_id, title="Renamed", content="Details", expected_version=initial["task"]["version"])
    assert updated["ok"] and updated["task"]["title"] == "Renamed"
    assert mcp.task_claim(task_id).startswith("Claimed task:")
    assert mcp.task_release(task_id).startswith("Released task claim:")
    assert mcp.task_cancel(task_id)["task"]["status"] == "cancelled"


def test_service_rejects_ambient_transaction_without_claiming_commit(store, scope):
    with tasks.transaction(store):
        result = execute(store, "create", {"operation_id": "ambient", "title": "uncommitted"}, scope)
        assert result["error"]["code"] == "transaction_conflict"
    assert not tasks.list_tasks(store, status="all", limit=None)


def test_receipt_lookup_is_authorization_independent_but_intent_bound(store, scope):
    args = {"operation_id": "committed", "title": "Committed effect"}
    authorization = {"operation_id": "committed", "input_digest": "input", "scope_digest": "scope", "policy_revision": "rev1", "expires_at": "past"}
    first = execute(store, "create", args, {**scope, "authorization_receipt": authorization})
    assert first["ok"]
    # Receipt lookup never consumes or trusts a new authorization envelope.
    again = lookup_completed(store, "create", args, {**scope, "authorization_receipt": {"invalid": True}})
    assert again == {**first, "replayed": True}
    assert lookup_completed(store, "create", args, {**scope, "session_id": "elsewhere"}) is None
    with pytest.raises(TaskServiceError, match="different arguments"):
        lookup_completed(store, "create", {**args, "title": "Changed intent"}, scope)
    with pytest.raises(TaskServiceError, match="different arguments"):
        lookup_completed(store, "update", args, scope)
    assert len(tasks.list_tasks(store, status="all", limit=None)) == 1
