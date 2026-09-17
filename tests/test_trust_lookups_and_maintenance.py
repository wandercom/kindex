"""Writes land on the node meant, and maintenance converges.

- verify/invalidate and other mutators resolved a title to whichever
  duplicate SQLite returned, possibly an archived twin.
- cron re-suggested the same pairs every pass once 100 newer suggestions
  existed, and re-suggested pairs the user had rejected.
- The prime showed imported Kinbase evidence with no governance label and
  no open Unknowns.
- A session launched from a repository subdirectory raised missing_hooks.
- The stale reindex took its limit before checking staleness and stalled.
"""

from __future__ import annotations

import datetime
import subprocess
import time

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    graph = Store(Config(data_dir=str(tmp_path / "kindex")))
    yield graph
    graph.close()


TITLE = "Deploy rule: never push to main without tests"


def twins(store):
    first = store.add_node(TITLE, "first", node_id="twin-a")
    second = store.add_node(TITLE, "second", node_id="twin-b")
    return first, second


def test_a_title_naming_two_live_nodes_is_refused_for_writes(store):
    from kindex import mcp_server
    from kindex.store import AmbiguousTitleError

    twins(store)
    with pytest.raises(AmbiguousTitleError, match="twin-a, twin-b|twin-b, twin-a"):
        store.resolve_node_for_write(TITLE)
    mcp_server._store = store
    mcp_server._config = store.config
    try:
        result = mcp_server.verify(TITLE, "me", "manual-review")
    finally:
        mcp_server._store = None
    assert str(result).startswith("Error: title_collision:")
    assert store.get_node("twin-a").get("verified_by") in (None, "")


def test_a_title_prefers_the_live_node_over_an_archived_twin(store):
    twins(store)
    store.update_node("twin-a", status="archived")
    assert store.resolve_node_for_write(TITLE)["id"] == "twin-b"
    assert store.get_node_by_title(TITLE)["id"] == "twin-b"
    # An id still names its node, archived or not.
    assert store.resolve_node_for_write("twin-a")["id"] == "twin-a"


def test_the_cli_refuses_an_ambiguous_title(store, tmp_path):
    import os
    import sys
    twins(store)
    store.close()
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "edit", TITLE, "--content", "changed",
         "--data-dir", str(tmp_path / "kindex")],
        capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 1
    assert "use a node id" in result.stderr


def test_cron_does_not_re_suggest_a_pair_already_raised(store, monkeypatch):
    from kindex import daemon, graph

    pairs = [{"concept_a": f"A{n}", "concept_b": f"B{n}", "reason": "gap"} for n in range(5)]
    monkeypatch.setattr(graph, "suggest_cross_component_links",
                        lambda store, max_suggestions=5: pairs)
    assert daemon._suggest_links(store) == 5
    for n in range(101):
        store.add_suggestion(f"X{n}", f"Y{n}", source="dream")
    store.conn.execute("UPDATE suggestions SET created_at = '2999-01-01' WHERE source = 'dream'")
    store.conn.execute("UPDATE suggestions SET status = 'rejected' WHERE concept_a = 'A0'")
    store.conn.commit()
    assert daemon._suggest_links(store) == 0
    assert daemon._suggest_links(store) == 0
    count = store.conn.execute(
        "SELECT count(*) FROM suggestions WHERE source = 'cron-auto-suggest'").fetchone()[0]
    assert count == 5


def test_old_accepted_suggestions_are_pruned(store):
    kept = store.add_suggestion("A", "B")
    old = store.add_suggestion("C", "D")
    store.conn.execute("UPDATE suggestions SET status = 'accepted' WHERE id IN (?, ?)", (kept, old))
    stamp = (datetime.datetime.now() - datetime.timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute("UPDATE suggestions SET created_at = ? WHERE id = ?", (stamp, old))
    rejected = store.add_suggestion("E", "F")
    store.conn.execute("UPDATE suggestions SET status = 'rejected', created_at = ? WHERE id = ?",
                       (stamp, rejected))
    store.conn.commit()
    assert store.prune_suggestions() == 1
    remaining = {row[0] for row in store.conn.execute("SELECT id FROM suggestions")}
    assert remaining == {kept, rejected}


def test_the_prime_labels_imported_evidence_and_its_unknowns(store):
    from kindex.hooks import prime_context

    store.add_node("deploy skips tests on Fridays", "deploy skips tests on Fridays",
                   node_id="policy", standing="authoritative",
                   extra={"kinbase": {"repo": "/tmp/somerepo", "mode": "raw",
                                      "logical_key": "deploy.policy", "provenance": "human"}})
    store.add_node("Is the Friday deploy policy still in force?", "Is the Friday deploy policy still in force?",
                   node_id="question", node_type="question",
                   extra={"kinbase": {"repo": "/tmp/somerepo", "mode": "raw",
                                      "logical_key": "deploy.policy", "status": "open",
                                      "owner_role": "release-manager"}})
    block = prime_context(store, topic="deploy Friday")
    entry = block[block.index("**deploy skips tests on Fridays**"):]
    entry = entry[:entry.index("\n- ") if "\n- " in entry else len(entry)]
    assert "raw signed evidence; governance not evaluated" in entry
    assert "Unknown [open]: Is the Friday deploy policy still in force?" in entry


def test_a_subdirectory_session_has_its_hook_receipt(tmp_path, monkeypatch):
    from kindex import integrations, supervisor_health, supervisor_health_activity

    monkeypatch.setenv("KIN_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.setattr(supervisor_health_activity, "observe_activity", lambda now: {})
    repo = tmp_path / "repo"
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    native = {"project_path": str(subdir), "session_id": "sub-session", "agent": "claude"}
    now = time.time()
    for at in (now - 400, now - 5):
        supervisor_health.record(native, "activity", {"source": "native", "active": True, "timestamp": at})
    assert any(issue["code"] == "missing_hooks"
               for issue in supervisor_health.check_health(now=now)["issues"])
    integrations.dispatch({"protocol_version": 1, "scope": native, "action": "supervisor",
                           "text": "work"})
    health = supervisor_health.check_health(now=time.time())
    assert not any(issue["code"] == "missing_hooks" for issue in health["issues"]), health["issues"]


def test_the_stale_reindex_reaches_past_its_first_page(store, monkeypatch):
    from kindex import vectors

    for n in range(30):
        store.add_node(f"note {n}", f"body {n}", node_id=f"n{n:02d}", weight=1 - n / 100)
    fresh = {f"n{n:02d}" for n in range(10)}
    monkeypatch.setattr(vectors, "_node_embedding_fresh",
                        lambda store, node, fingerprint: node["id"] in fresh)
    monkeypatch.setattr(vectors, "embedding_fingerprint", lambda config: "F2")
    selected = vectors.select_reindex_nodes(store, stale=True, status="active", limit=10)
    assert [node["id"] for node in selected] == [f"n{n:02d}" for n in range(10, 20)]
    plain = vectors.select_reindex_nodes(store, status="active", limit=5)
    assert [node["id"] for node in plain] == [f"n{n:02d}" for n in range(5)]


@pytest.mark.parametrize("command", [
    ["verify", TITLE, "--by", "me", "--method", "manual-review"],
    ["invalidate", TITLE, "--by", "me", "--code", "superseded"],
    ["coord", "attach", "room", TITLE],
])
def test_cli_writes_refuse_an_ambiguous_title_with_exit_1(store, tmp_path, command):
    import os
    import sys
    from kindex.coordination import create_conversation
    twins(store)
    create_conversation(store, "room")
    store.close()
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", *command, "--data-dir", str(tmp_path / "kindex")],
        capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "title_collision" in result.stderr


def test_a_cursor_subdirectory_session_has_its_hook_receipt(tmp_path, monkeypatch):
    from kindex import supervisor

    recorded = []
    monkeypatch.setattr(supervisor, "record_health",
                        lambda scope, kind, **details: recorded.append((scope["project_path"], kind)))
    repo = tmp_path / "repo"
    subdir = repo / "pkg"
    subdir.mkdir(parents=True)
    scope = {"project_path": str(repo), "agent": "cursor", "session_id": "s"}
    supervisor.record_hook_receipt(scope, [str(subdir), str(subdir), None, str(repo)])
    assert recorded == [(str(repo), "hook"), (str(subdir.resolve()), "hook")]
