"""Public rebuild contract: real CLI and SQLite, HTTP replaced by a fake provider.

Written against the pre-feature base; no rebuild implementation helpers are used.
"""
import io
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from kindex import cli, vectors
from kindex.config import Config
from kindex.store import Store


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.value = 0.1
        self.fail_text = None
        self.deny = False

    def __call__(self, request, **kwargs):
        body = json.loads(request.data)
        self.calls.append(body)
        if self.deny or (self.fail_text and self.fail_text in str(body["input"])):
            raise HTTPError(request.full_url, 401 if self.deny else 503,
                            "fake provider failure", {}, io.BytesIO(b'{}'))
        vector = [self.value] * body.get("dimensions", 2)
        return io.BytesIO(json.dumps({"data": [{"embedding": vector}]}).encode())


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-offline-test")
    provider = FakeProvider()
    monkeypatch.setattr("urllib.request.urlopen", provider)
    cfg = Config(data_dir=str(tmp_path / "graph"), embedding={
        "provider": "openai", "model": "text-embedding-3-small",
        "dimensions": 2, "strategy": "single"})
    store = Store(cfg, manage_project_storage=False)
    alpha = store.add_node("alpha", content="alpha durable graph evidence", queue_embedding=False)
    beta = store.add_node("beta", content="beta durable graph evidence", queue_embedding=False)
    store.add_edge(alpha, beta, "relates_to")
    store.set_meta("operator-sentinel", "retain this operational metadata")
    for node_id in (alpha, beta):
        node = store.get_node(node_id)
        assert vectors.upsert_embedding(store, node_id, node["title"] + "\n" + node["content"])
    durable = {table: [tuple(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
               for table in ("nodes", "edges", "activity_log")}
    store.close()
    provider.calls.clear()
    config_path = tmp_path / "target.yaml"

    def configure(model="text-embedding-3-large", dimensions=3, **extra):
        target = cfg.model_copy(deep=True)
        target.embedding.model = model
        target.embedding.dimensions = dimensions
        for key, value in extra.items():
            setattr(target.embedding, key, value)
        config_path.write_text(json.dumps(target.model_dump(mode="json")))
        return target

    target = configure()
    return {"config": config_path, "configure": configure, "target": target,
            "durable": durable, "original": cfg, "provider": provider, "ids": {alpha, beta}, "root": tmp_path}


def invoke(graph, monkeypatch, capsys, *args, answer="", interactive=False, before_answer=None):
    class ConfirmationInput(io.StringIO):
        def readline(self, *args, **kwargs):
            if before_answer is not None:
                before_answer()
            return super().readline(*args, **kwargs)

    stdin = ConfirmationInput(answer)
    monkeypatch.setattr(stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["kin", "embed", *args, "--config", str(graph["config"])])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def state(config):
    with closing(Store(config, manage_project_storage=False)) as store:
        assert vectors.ensure_vec_table(store)
        return {name: [tuple(row) for row in store.conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
                for name in ("node_vectors", "node_vector_meta", "vec_meta")}


def assert_graph_preserved(graph):
    with closing(Store(graph["target"], manage_project_storage=False)) as store:
        assert [tuple(row) for row in store.conn.execute("SELECT * FROM nodes ORDER BY 1")] == graph["durable"]["nodes"]
        assert [tuple(row) for row in store.conn.execute("SELECT * FROM edges ORDER BY 1")] == graph["durable"]["edges"]
        activity = {tuple(row) for row in store.conn.execute("SELECT * FROM activity_log")}
        assert set(graph["durable"]["activity_log"]).issubset(activity)
        assert store.get_meta("operator-sentinel") == "retain this operational metadata"
        assert store.conn.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'durable'").fetchone()[0] == 2


def backups(graph):
    # Find SQLite artifacts by file header, independent of backup naming/layout.
    found = []
    live = Path(graph["original"].data_dir)
    for path in graph["root"].rglob("*"):
        if path.is_file() and not path.is_relative_to(live):
            with path.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    found.append(path)
    return found


def assert_backup_preserved(graph, output, original_state):
    # XDG state is temporary (conftest); selected graph and state share tmp_path.
    import os
    roots = [graph["root"], Path(os.environ["XDG_STATE_HOME"])]
    paths = set(backups(graph))
    for root in roots:
        paths.update(root.rglob("*.sqlite3"))
    paths = [p for p in paths if str(p) in output]
    assert paths, "CLI must report the recoverable SQLite backup path"
    import sqlite_vec
    conn = sqlite3.connect(paths[0])
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, rows in original_state.items():
            assert conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert conn.execute("SELECT * FROM nodes ORDER BY 1").fetchall() == graph["durable"]["nodes"]
        assert conn.execute("SELECT * FROM edges ORDER BY 1").fetchall() == graph["durable"]["edges"]
    finally:
        conn.close()


@pytest.mark.parametrize("answer,interactive", [("n\n", True), ("\n", True), ("", True), ("", False)])
def test_decline_or_missing_confirmation_preserves_index(graph, monkeypatch, capsys, answer, interactive):
    before = state(graph["original"])
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", answer=answer, interactive=interactive)
    assert "invalid choice" not in output, "rebuild must be a registered CLI command"
    assert code != 0
    assert state(graph["original"]) == before
    assert graph["provider"].calls == []
    assert_graph_preserved(graph)
    assert "text-embedding-3-small" in output and "text-embedding-3-large" in output
    assert str(Path(graph["original"].data_dir)) in output
    assert "token" in output.lower() and "cost" in output.lower()
    assert "2" in output and "semantic" in output.lower()


@pytest.mark.parametrize("same_identity", [False, True])
def test_approved_rebuild_recomputes_and_keeps_recovery_backup(graph, monkeypatch, capsys, same_identity):
    before = state(graph["original"])
    if same_identity:
        graph["target"] = graph["configure"](model="text-embedding-3-small", dimensions=2)
    graph["provider"].value = 0.7
    config_bytes = graph["config"].read_bytes()
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert code == 0, output
    after = state(graph["target"])
    assert after["node_vectors"] != before["node_vectors"]
    assert len(after["node_vectors"]) == 2
    assert dict(after["vec_meta"])["embedding_fingerprint"] == vectors.embedding_fingerprint(graph["target"])
    assert dict(after["vec_meta"])["embedding_dim"] == str(graph["target"].embedding.dimensions)
    assert any("alpha" in str(call["input"]) for call in graph["provider"].calls)
    assert any("beta" in str(call["input"]) for call in graph["provider"].calls)
    assert graph["config"].read_bytes() == config_bytes
    assert_graph_preserved(graph)
    assert_backup_preserved(graph, output, before)


@pytest.mark.parametrize("failure", ["missing_key", "authentication", "backup"])
def test_readiness_and_backup_fail_closed(graph, monkeypatch, capsys, failure):
    before = state(graph["original"])
    if failure == "missing_key":
        monkeypatch.delenv("OPENAI_API_KEY")
    elif failure == "authentication":
        graph["provider"].deny = True
    else:
        # Existing supported backup storage is physically unwritable: fail even as root.
        blocker = graph["root"] / "not-a-directory"
        blocker.write_text("block backup directory creation")
        monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert "invalid choice" not in output
    assert code != 0
    assert state(graph["original"]) == before
    assert_graph_preserved(graph)
    assert "fake-key-for-offline-test" not in output


def test_partial_failure_keeps_successful_work_and_stale_resume(graph, monkeypatch, capsys):
    before = state(graph["original"])
    graph["provider"].value = 0.7
    graph["provider"].fail_text = "beta"
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert "invalid choice" not in output
    assert code != 0
    assert_backup_preserved(graph, output, before)
    partial = state(graph["target"])
    assert len(partial["node_vectors"]) == 1
    assert_graph_preserved(graph)
    assert "reindex" in output and "stale" in output
    graph["provider"].fail_text = None
    graph["provider"].calls.clear()
    code, output = invoke(graph, monkeypatch, capsys, "reindex", "--stale")
    assert code == 0, output
    complete = state(graph["target"])
    assert len(complete["node_vectors"]) == 2
    assert set(partial["node_vectors"]).issubset(set(complete["node_vectors"]))
    assert not any("alpha" in str(call["input"]) for call in graph["provider"].calls)
    assert any("beta" in str(call["input"]) for call in graph["provider"].calls)


def test_ordinary_cli_reindex_never_rebuilds_incompatible_index(graph, monkeypatch, capsys):
    before = state(graph["original"])
    code, output = invoke(graph, monkeypatch, capsys, "reindex")
    assert code != 0
    assert state(graph["original"]) == before
    assert graph["provider"].calls == []
    assert_graph_preserved(graph)
    assert "fingerprint" in output.lower()


def test_explicit_interactive_approval_can_rebuild(graph, monkeypatch, capsys):
    before = state(graph["original"])
    graph["provider"].value = 0.7
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", answer="y\n", interactive=True)
    assert code == 0, output
    assert len(state(graph["target"])["node_vectors"]) == 2
    assert_graph_preserved(graph)
    assert_backup_preserved(graph, output, before)



def test_concurrent_embedding_writer_refuses_rebuild_without_provider_calls(graph, monkeypatch, capsys):
    import fcntl

    before = state(graph["original"])
    with closing(Store(graph["original"], manage_project_storage=False)) as store:
        lock_path = store.db_path.resolve().with_suffix(".embedding.lock")
    # Real OS lock, using the established embedding-writer lock protocol.
    with lock_path.open("a+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert "invalid choice" not in output
    assert code != 0
    assert graph["provider"].calls == []
    assert state(graph["original"]) == before
    assert_graph_preserved(graph)


def test_index_identity_changed_during_confirmation_cannot_reuse_approval(graph, monkeypatch, capsys):
    before = state(graph["original"])
    concurrent = graph["original"].model_copy(deep=True)
    concurrent.embedding.model = "concurrent-operator-index"
    concurrent_fingerprint = vectors.embedding_fingerprint(concurrent)
    changed = []

    def another_operator_changes_index():
        with closing(Store(graph["original"], manage_project_storage=False)) as store:
            store.conn.execute("UPDATE vec_meta SET value=? WHERE key='embedding_fingerprint'",
                               (concurrent_fingerprint,))
            store.conn.commit()
        changed.append(True)

    code, output = invoke(graph, monkeypatch, capsys, "rebuild", answer="y\n", interactive=True,
                          before_answer=another_operator_changes_index)
    assert "invalid choice" not in output
    assert changed, "fixture must change the stored identity after preview and before approval"
    assert code != 0
    assert graph["provider"].calls == []
    after = state(concurrent)
    assert after["node_vectors"] == before["node_vectors"]
    assert after["node_vector_meta"] == before["node_vector_meta"]
    assert dict(after["vec_meta"])["embedding_fingerprint"] == concurrent_fingerprint
    assert_graph_preserved(graph)


def test_transient_workspace_policy_refuses_even_explicit_yes(graph, monkeypatch, capsys):
    before = state(graph["original"])
    # Public project-store policy marker: descendants cannot authorize paid indexing.
    (graph["root"] / ".kindex-no-index").touch()
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert "invalid choice" not in output
    assert code != 0
    assert "transient" in output.lower()
    assert graph["provider"].calls == []
    assert state(graph["original"]) == before
    assert_graph_preserved(graph)


def test_backup_path_is_reported_before_provider_interrupts_rebuild(graph, monkeypatch, capsys):
    before = state(graph["original"])
    provider = graph["provider"]
    notices = []

    def interrupt_document_request(request, **kwargs):
        body = json.loads(request.data)
        if any(word in str(body["input"]) for word in ("alpha", "beta")):
            # This happens during real document embedding, after readiness probing.
            captured = capsys.readouterr()
            notices.append(captured.out + captured.err)
            raise KeyboardInterrupt("offline interruption during document embedding")
        return provider(request, **kwargs)

    monkeypatch.setattr("urllib.request.urlopen", interrupt_document_request)
    try:
        code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
        assert code != 0, output
    except KeyboardInterrupt:
        pass
    assert notices, "the test must reach post-approval document embedding"
    assert_backup_preserved(graph, notices[0], before)
    assert_graph_preserved(graph)


def test_target_index_creation_failure_rolls_back_original_index(graph, monkeypatch, capsys):
    before = state(graph["original"])
    open_selected_store = cli._store
    storage_events = []

    def open_store_with_creation_fault(args):
        store = open_selected_store(args)

        def authorize(action, table, detail, database, source):
            if table == "node_vectors":
                if action == sqlite3.SQLITE_DROP_VTABLE:
                    storage_events.append("drop original index")
                elif action == sqlite3.SQLITE_CREATE_VTABLE:
                    storage_events.append("reject target index creation")
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.conn.set_authorizer(authorize)
        return store

    # Keep CLI config/scope resolution and the real SQLite connection. Only the
    # database's permission to create the replacement virtual table is denied.
    monkeypatch.setattr(cli, "_store", open_store_with_creation_fault)
    code, output = invoke(graph, monkeypatch, capsys, "rebuild", "--yes")
    assert "invalid choice" not in output
    assert storage_events == ["drop original index", "reject target index creation"], storage_events
    assert code != 0
    assert state(graph["original"]) == before
    assert_graph_preserved(graph)
    assert_backup_preserved(graph, output, before)
