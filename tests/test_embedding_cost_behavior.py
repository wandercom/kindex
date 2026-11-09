"""Independent cost/reliability acceptance tests.

Derived from the requested behavior without reading vector implementation.
Only HTTP responses are faked; Store, SQLite/vec, queueing and persistence run
for real. Every database lives beneath pytest's temporary directory.
"""

import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from kindex import vectors
from kindex.config import Config, EmbeddingConfig
from kindex.store import Store


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class Provider:
    """Vectors intentionally depend on the entire ordered contextual request."""

    def __init__(self):
        self.calls = []
        self.fail_at = {}
        self.invalid_at = {}
        self.before_response = None

    @staticmethod
    def embeddings(body):
        group = body["inputs"][0] if "inputs" in body else body["input"]
        dimension = body.get("output_dimension", body.get("dimensions", 8))
        identity = json.dumps(body, sort_keys=True)
        return [
            [int.from_bytes(hashlib.sha256(f"{identity}:{i}:{d}".encode()).digest()[:2], "big") / 65536
             for d in range(dimension)]
            for i in range(len(group))
        ]

    def __call__(self, request, **_kwargs):
        body = json.loads(request.data)
        self.calls.append(body)
        call = len(self.calls)
        if self.before_response:
            self.before_response(call, body)
        error = self.fail_at.get(call)
        if error:
            raise error
        values = self.invalid_at.get(call, self.embeddings(body))
        rows = [{"index": i, "embedding": value}
                for i, value in enumerate(values)]
        return Response({"data": [{"data": rows}] if "inputs" in body else rows})


@pytest.fixture
def world(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    config = Config(data_dir=str(tmp_path), embedding=EmbeddingConfig(
        provider="voyage", model="voyage-context-4", dimensions=8,
        chunk_chars=4, chunk_overlap_chars=0, max_group_chunks=2,
    ))
    provider = Provider()
    monkeypatch.setenv("VOYAGE_API_KEY", "synthetic-provider-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-secret")
    monkeypatch.setattr(vectors.urllib.request, "urlopen", provider)
    instances = []

    def open_store():
        instance = Store(config)
        instances.append(instance)
        return instance

    result = SimpleNamespace(config=config, provider=provider, open=open_store)
    result.store = open_store()
    yield result
    for instance in instances:
        instance.close()


def queue(store):
    return json.loads(store.get_meta(vectors.EMBED_QUEUE_META) or "[]")


def snapshot(store):
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='node_vectors'").fetchone():
        return []
    # Extension loading is per connection; opening a Store need not load it.
    import sqlite_vec
    store.conn.enable_load_extension(True)
    sqlite_vec.load(store.conn)
    store.conn.enable_load_extension(False)
    return [(row[0], bytes(row[1])) for row in store.conn.execute(
        "SELECT node_id, embedding FROM node_vectors ORDER BY node_id")]


def assert_complete(store, node_id):
    assert node_id not in queue(store)
    status = vectors.embedding_status(store)
    assert status["coverage_complete"] is True
    rows = store.conn.execute(
        "SELECT chunk_index, chunk_count FROM node_vector_meta WHERE node_id=? ORDER BY chunk_index",
        (node_id,),
    ).fetchall()
    assert rows
    assert [r[0] for r in rows] == list(range(len(rows)))
    assert all(r[1] == len(rows) for r in rows)
    assert len(snapshot(store)) == len(rows)


def provider_error(code):
    return HTTPError("https://api.voyageai.com/v1/contextualizedembeddings", code,
                     "provider rejected request", None,
                     io.BytesIO(b'{"error":"synthetic-provider-secret PRIVATE-SOURCE-CONTENT"}'))


def test_fresh_duplicate_queue_item_never_repeats_paid_request_after_reopen(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    first = vectors.drain_embedding_queue(world.store)
    assert first["embedded"] == 1
    original = snapshot(world.store)
    paid = len(world.provider.calls)
    world.store.close()
    reopened = world.open()
    vectors.enqueue_embedding(reopened, node)
    vectors.drain_embedding_queue(reopened)
    assert len(world.provider.calls) == paid, "fresh queue duplicate charged again"
    assert snapshot(reopened) == original
    assert_complete(reopened, node)


def test_legacy_vector_without_provenance_is_not_treated_as_fresh(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    assert vectors.ensure_vec_table(world.store)
    # A historical vector can exist without content/config provenance.
    # Its mere presence cannot establish freshness for the current text.
    world.store.conn.execute(
        "INSERT INTO node_vectors(node_id,embedding) VALUES (?,?)",
        (node, struct.pack("8f", *([0.1] * 8))),
    )
    world.store.conn.commit()
    world.store.close()
    reopened = world.open()
    vectors.drain_embedding_queue(reopened)
    assert world.provider.calls, "unknown legacy vector was wrongly considered fresh"
    assert_complete(reopened, node)


def node_vector_values(store, node):
    values = dict(snapshot(store))
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='node_vector_meta'").fetchone():
        return []
    return [values[row[0]] for row in store.conn.execute(
        "SELECT vector_id FROM node_vector_meta WHERE node_id=? ORDER BY chunk_index", (node,))]


def test_identical_inputs_reuse_paid_vectors_across_different_node_ids(world):
    first = world.store.add_node("Same title", content="abcdefghijklmnop", node_id="first-unknown-source")
    vectors.drain_embedding_queue(world.store)
    paid = len(world.provider.calls)
    second = world.store.add_node("Same title", content="abcdefghijklmnop", node_id="second-unknown-source")
    vectors.drain_embedding_queue(world.store)
    assert len(world.provider.calls) == paid, "identical inputs on another node were charged again"
    assert node_vector_values(world.store, first) == node_vector_values(world.store, second)
    assert node_vector_values(world.store, second)
    assert queue(world.store) == []
    assert vectors.embedding_status(world.store)["coverage_complete"] is True


def test_partial_full_group_checkpoint_is_reusable_across_node_ids(world):
    first = world.store.add_node("T", content="abcdefghijklmnop", node_id="partial-source")
    world.provider.fail_at[2] = provider_error(503)
    vectors.drain_embedding_queue(world.store)
    successful_group = world.provider.calls[0]
    second = world.store.add_node("T", content="abcdefghijklmnop", node_id="independent-source")
    vectors.enqueue_embedding(world.store, first)  # Other node is processed first.
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store, max_jobs=1)
    assert successful_group not in world.provider.calls[paid:]
    assert second not in queue(world.store)
    assert first in queue(world.store)
    assert node_vector_values(world.store, second)
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store)
    assert len(world.provider.calls) == paid
    assert node_vector_values(world.store, first) == node_vector_values(world.store, second)


@pytest.mark.parametrize("mutation", ["delete", "archive"])
def test_node_retired_during_provider_request_cannot_publish_generation(world, mutation):
    node = world.store.add_node("T", content="abcdefghijklmnop")

    def retire(call, _body):
        if call == 1:
            other = Store(world.config)
            try:
                if mutation == "delete":
                    other.delete_node(node)
                else:
                    other.update_node(node, status="archived")
            finally:
                other.close()

    world.provider.before_response = retire
    result = vectors.drain_embedding_queue(world.store)
    assert result["embedded"] == 0
    assert node_vector_values(world.store, node) == []
    assert snapshot(world.store) == []
    world.provider.before_response = None
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store)
    assert len(world.provider.calls) == paid
    assert node not in queue(world.store)


def test_config_changed_during_request_does_not_publish_under_new_identity(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")

    def change_config(call, _body):
        if call == 1:
            world.config.embedding.model = "voyage-3.5"

    world.provider.before_response = change_config
    result = vectors.drain_embedding_queue(world.store)
    assert result["embedded"] == 0
    assert node in queue(world.store)
    assert snapshot(world.store) == []
    assert vectors.embedding_status(world.store)["coverage_complete"] is False
    world.provider.before_response = None
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store)
    assert world.provider.calls[paid:]
    assert all(body["model"] == "voyage-3.5" for body in world.provider.calls[paid:])
    assert_complete(world.store, node)


def test_generic_provider_exception_never_echoes_private_text_or_opaque_key(world, caplog, capsys):
    opaque_key = "y49pP3qW7u1eC9vR6m8bA2dK"
    private_text = "unrelated private diary content must stay private"
    world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[1] = RuntimeError(f"{private_text}; {opaque_key}")
    result = vectors.drain_embedding_queue(world.store)
    captured = capsys.readouterr()
    visible = json.dumps(result) + caplog.text + captured.out + captured.err
    assert result["status"] != "ok"
    assert result["embedded"] == 0
    assert result["pending"] == 1
    assert opaque_key not in visible
    assert private_text not in visible


@pytest.fixture
def isolated_surface(world, tmp_path, monkeypatch):
    import kindex.config as config_module

    home = tmp_path / "synthetic-home"
    home.mkdir()
    config_path = home / "kin.yaml"
    config_path.write_text(
        "embedding:\n  provider: voyage\n  model: voyage-context-4\n"
        "  dimensions: 8\n  chunk_chars: 4\n  chunk_overlap_chars: 0\n  max_group_chunks: 2\n"
        "llm:\n  enabled: false\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(home / "health"))
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    for variable in ("KIN_PROFILE", "KIN_PROJECT", "KIN_PROJECT_PATH", "KINDEX_REVIEW_WORKER"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [config_path])
    monkeypatch.chdir(tmp_path)
    world.config.claude_dir = str(home / "claude")
    world.config.project_dirs = [str(home / "projects")]
    world.config.llm.enabled = False
    return world


def test_cli_auth_failure_exits_unsuccessfully_with_actionable_nonsecret_json(isolated_surface, monkeypatch, capsys):
    from kindex import cli

    world = isolated_surface
    world.store.add_node("T", content="PRIVATE-SOURCE-CONTENT")
    world.provider.fail_at = {n: provider_error(401) for n in range(1, 20)}
    monkeypatch.setattr(sys, "argv", ["kin", "embed", "drain", "--data-dir", world.config.data_dir, "--json"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code != 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] != "ok"
    assert result["pending"] == 1
    assert len(world.provider.calls) == 1
    visible = output.out + output.err
    assert "401" in visible
    assert "synthetic-provider-secret" not in visible
    assert "PRIVATE-SOURCE-CONTENT" not in visible


def test_cron_auth_failure_remains_visible_and_not_successful(isolated_surface, monkeypatch, capsys):
    from kindex.daemon import cron_run

    world = isolated_surface
    world.store.add_node("T", content="PRIVATE-SOURCE-CONTENT")
    world.provider.fail_at = {n: provider_error(403) for n in range(1, 20)}
    monkeypatch.setattr("kindex.notify.dispatch", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda _config: False)
    result = cron_run(world.config, world.store, verbose=False)
    output = capsys.readouterr()
    assert len(world.provider.calls) == 1
    assert len(queue(world.store)) == 1

    def nested_dicts(value):
        if isinstance(value, dict):
            yield value
            for nested in value.values():
                yield from nested_dicts(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from nested_dicts(nested)

    failures = [item for item in nested_dicts(result)
                if item.get("status") not in (None, "ok", "empty", "unavailable")
                and item.get("embedded") == 0 and item.get("pending") == 1]
    assert failures, "cron silently reported no embedding failure"
    visible = json.dumps(result, default=str) + output.out + output.err
    assert "403" in visible
    assert "synthetic-provider-secret" not in visible
    assert "PRIVATE-SOURCE-CONTENT" not in visible


@pytest.mark.parametrize("change", ["content", "model", "dimensions", "chunk_chars", "chunk_overlap_chars", "max_group_chunks"])
def test_semantic_changes_require_a_new_complete_generation(world, change):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    vectors.drain_embedding_queue(world.store)
    before = snapshot(world.store)
    paid = len(world.provider.calls)
    if change == "content":
        world.store.update_node(node, content="qrstuvwxyzABCDEF")
    else:
        setattr(world.config.embedding, change, {
            "model": "voyage-3.5", "dimensions": 16, "chunk_chars": 6,
            "chunk_overlap_chars": 1, "max_group_chunks": 3,
        }[change])
    vectors.enqueue_embedding(world.store, node)
    vectors.drain_embedding_queue(world.store)
    assert len(world.provider.calls) > paid
    assert snapshot(world.store) != before
    assert_complete(world.store, node)


def test_paid_contextual_group_survives_later_failure_and_database_reopen(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[2] = provider_error(503)
    failed = vectors.drain_embedding_queue(world.store)
    assert failed["embedded"] == 0
    assert node in queue(world.store)
    assert not snapshot(world.store), "partial generation became visible"
    assert failed["coverage_complete"] is False
    successful_group = world.provider.calls[0]
    world.store.close()
    reopened = world.open()
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(reopened)
    assert successful_group not in world.provider.calls[paid:], "durable paid group was bought twice"
    assert_complete(reopened, node)
    first_vector = snapshot(reopened)[0][1]
    assert struct.unpack("8f", first_vector) == pytest.approx(Provider.embeddings(successful_group)[0])


def test_interruption_after_paid_checkpoint_is_resumable_without_false_completion(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[2] = KeyboardInterrupt("synthetic process interruption")
    with pytest.raises(KeyboardInterrupt):
        vectors.drain_embedding_queue(world.store)
    assert snapshot(world.store) == []
    paid = len(world.provider.calls)
    successful_group = world.provider.calls[0]
    world.store.close()
    reopened = world.open()
    assert node in queue(reopened)
    vectors.drain_embedding_queue(reopened)
    assert successful_group not in world.provider.calls[paid:]
    assert_complete(reopened, node)


def test_hard_process_exit_retains_completed_paid_group(world, tmp_path):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    world.store.close()
    call_log = tmp_path / "synthetic-provider-calls.jsonl"
    script = '''
import json, os, sys
sys.path.insert(0, sys.argv[3])
from test_embedding_cost_behavior import Provider
from kindex import vectors
from kindex.config import Config, EmbeddingConfig
from kindex.store import Store
provider = Provider()
def call(request, **kwargs):
    with open(sys.argv[2], "a") as log:
        log.write(json.dumps(json.loads(request.data)) + "\\n")
        log.flush()
        os.fsync(log.fileno())
    if len(provider.calls) == 1:
        os._exit(73)
    return provider(request, **kwargs)
vectors.urllib.request.urlopen = call
os.environ["VOYAGE_API_KEY"] = "synthetic-provider-secret"
store = Store(Config(data_dir=sys.argv[1], embedding=EmbeddingConfig(
    provider="voyage", model="voyage-context-4", dimensions=8,
    chunk_chars=4, chunk_overlap_chars=0, max_group_chunks=2)))
vectors.drain_embedding_queue(store)
'''
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(call_log), str(Path(__file__).parent)],
        capture_output=True, text=True, timeout=10,
    )
    assert child.returncode == 73, child.stderr
    paid_calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    assert len(paid_calls) == 2
    reopened = world.open()
    assert node in queue(reopened)
    assert snapshot(reopened) == []
    vectors.drain_embedding_queue(reopened)
    assert paid_calls[0] not in world.provider.calls
    assert_complete(reopened, node)


@pytest.mark.parametrize("change", ["model", "dimensions", "chunk_chars", "chunk_overlap_chars", "max_group_chunks"])
def test_partial_paid_work_cannot_cross_incompatible_configuration(world, change):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[2] = provider_error(503)
    vectors.drain_embedding_queue(world.store)
    setattr(world.config.embedding, change, {
        "model": "voyage-3.5", "dimensions": 16, "chunk_chars": 6,
        "chunk_overlap_chars": 1, "max_group_chunks": 3,
    }[change])
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store)
    calls = world.provider.calls[paid:]
    assert calls
    dimension = world.config.embedding.dimensions
    assert struct.unpack(f"{dimension}f", snapshot(world.store)[0][1]) == pytest.approx(
        Provider.embeddings(calls[0])[0])
    assert_complete(world.store, node)


@pytest.mark.parametrize("invalid", [[], [[0.1] * 7, [0.2] * 7], [[float("nan")] * 8, [0.2] * 8]])
def test_invalid_provider_generation_preserves_old_vectors_and_remains_pending(world, invalid):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    vectors.drain_embedding_queue(world.store)
    old = snapshot(world.store)
    world.store.update_node(node, content="qrstuvwxyzABCDEF")
    vectors.enqueue_embedding(world.store, node)
    world.provider.invalid_at[len(world.provider.calls) + 1] = invalid
    result = vectors.drain_embedding_queue(world.store)
    assert result["embedded"] == 0
    assert result["status"] != "ok"
    assert snapshot(world.store) == old
    assert node in queue(world.store)
    vectors.drain_embedding_queue(world.store)
    assert snapshot(world.store) != old
    assert_complete(world.store, node)


@pytest.mark.parametrize("replacement", ["abZZefghijklmnop", "efabcdghijklmnop"])
def test_changed_neighbor_or_order_cannot_reuse_contextual_chunk_in_isolation(world, replacement):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[2] = provider_error(503)
    vectors.drain_embedding_queue(world.store)
    old_group = world.provider.calls[0]
    world.store.update_node(node, content=replacement)
    vectors.enqueue_embedding(world.store, node)
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(world.store)
    new_calls = world.provider.calls[paid:]
    assert new_calls
    assert new_calls[0] != old_group
    # The first chunk stays "T ab" for the neighbor-change case. Its vector
    # must still change because the rest of its contextual group changed.
    assert struct.unpack("8f", snapshot(world.store)[0][1]) == pytest.approx(
        Provider.embeddings(new_calls[0])[0])
    assert_complete(world.store, node)


def test_failed_replacement_preserves_entire_previous_vector_generation(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    vectors.drain_embedding_queue(world.store)
    old = snapshot(world.store)
    world.store.update_node(node, content="qrstuvwxyzABCDEF")
    vectors.enqueue_embedding(world.store, node)
    world.provider.fail_at[len(world.provider.calls) + 2] = provider_error(503)
    result = vectors.drain_embedding_queue(world.store)
    assert result["embedded"] == 0
    assert snapshot(world.store) == old
    assert node in queue(world.store)
    world.store.close()
    assert snapshot(world.open()) == old


def test_failed_dimension_migration_preserves_previous_vectors_across_reopen(world):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    vectors.drain_embedding_queue(world.store)
    old = snapshot(world.store)
    world.config.embedding.dimensions = 16
    vectors.enqueue_embedding(world.store, node)
    world.provider.fail_at[len(world.provider.calls) + 2] = provider_error(503)
    result = vectors.drain_embedding_queue(world.store)
    assert result["embedded"] == 0
    assert snapshot(world.store) == old, "dimension migration removed live vectors before replacement was complete"
    assert node in queue(world.store)
    world.store.close()
    reopened = world.open()
    assert snapshot(reopened) == old
    vectors.drain_embedding_queue(reopened)
    assert all(len(blob) == 16 * 4 for _identity, blob in snapshot(reopened))
    assert_complete(reopened, node)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_stops_drain_and_reports_nonsecret_actionable_failure(world, status, caplog):
    ids = [world.store.add_node(f"T{i}", content="PRIVATE-SOURCE-CONTENT") for i in range(4)]
    world.provider.fail_at = {n: provider_error(status) for n in range(1, 30)}
    result = vectors.drain_embedding_queue(world.store)
    assert len(world.provider.calls) == 1, "auth failure repeated across backlog"
    assert result["status"] != "ok"
    assert result["embedded"] == 0
    assert result["pending"] == len(ids)
    assert set(queue(world.store)) == set(ids)
    diagnostic = json.dumps(result) + caplog.text
    assert str(status) in diagnostic
    assert "synthetic-provider-secret" not in diagnostic
    assert "PRIVATE-SOURCE-CONTENT" not in diagnostic
    assert result["drain_complete"] is False


def test_transient_failure_is_not_reported_as_ok(world):
    world.store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at[1] = provider_error(503)
    result = vectors.drain_embedding_queue(world.store)
    assert result["status"] != "ok"
    assert result["embedded"] == 0
    assert result["pending"] == 1
    assert result["drain_complete"] is False


def test_archived_node_does_not_trigger_paid_requests(world):
    node = world.store.add_node("Archived", content="abcdefghijklmnop" * 20, status="archived")
    vectors.enqueue_embedding(world.store, node)
    vectors.drain_embedding_queue(world.store)
    assert world.provider.calls == []
    assert node not in queue(world.store)
    assert snapshot(world.store) == []


def test_time_budget_stops_between_groups_and_retains_paid_checkpoint(world, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(vectors.time, "monotonic", lambda: clock[0])
    world.provider.before_response = lambda *_args: clock.__setitem__(0, clock[0] + 11)
    node = world.store.add_node("T", content="abcdefghijklmnop" * 4)
    first = vectors.drain_embedding_queue(world.store, time_budget=10)
    assert len(world.provider.calls) == 1, "budget not checked between paid contextual groups"
    assert first["embedded"] == 0
    assert first["coverage_complete"] is False
    assert node in queue(world.store)
    assert snapshot(world.store) == []
    successful_group = world.provider.calls[0]
    world.store.close()
    reopened = world.open()
    world.provider.before_response = None
    vectors.drain_embedding_queue(reopened, time_budget=float("inf"))
    assert world.provider.calls.count(successful_group) == 1
    assert_complete(reopened, node)


def test_enqueue_with_commit_false_preserves_callers_transaction(world):
    node = world.store.add_node("T", content="body")
    world.store.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('test.uncommitted','private')")
    vectors.enqueue_embedding(world.store, node, commit=False)
    world.store.conn.rollback()
    assert world.open().get_meta("test.uncommitted") is None


def test_drain_does_not_commit_callers_unrelated_transaction(world):
    world.store.add_node("T", content="body")
    world.store.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('test.uncommitted','private')")
    vectors.drain_embedding_queue(world.store)
    world.store.conn.rollback()
    assert world.open().get_meta("test.uncommitted") is None


@pytest.mark.parametrize("mutation", ["enqueue", "edit"])
def test_work_arriving_during_provider_request_is_retained(world, mutation):
    node = world.store.add_node("T", content="abcdefghijklmnop")
    arrived = []

    def concurrent_change(call, _body):
        if call != 1:
            return
        other = Store(world.config)
        try:
            if mutation == "enqueue":
                arrived.append(other.add_node("Later", content="new work"))
            else:
                other.update_node(node, content="changed while provider was busy")
                vectors.enqueue_embedding(other, node)
                arrived.append(node)
        finally:
            other.close()

    world.provider.before_response = concurrent_change
    vectors.drain_embedding_queue(world.store, max_jobs=1)
    assert arrived
    assert arrived[0] in queue(world.store), "concurrent work overwritten by stale drain queue"
    world.provider.before_response = None
    vectors.drain_embedding_queue(world.store)
    assert not queue(world.store)
    assert vectors.embedding_status(world.store)["coverage_complete"] is True


def test_concurrent_drainers_do_not_duplicate_paid_requests(world):
    world.config.embedding.chunk_chars = 100
    node = world.store.add_node("T", content="body")
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors = []

    def block_first(call, _body):
        if call == 1:
            entered.set()
            assert release.wait(10), "test provider wait timed out"

    world.provider.before_response = block_first

    def drain():
        instance = Store(world.config)
        try:
            results.append(vectors.drain_embedding_queue(instance))
        except BaseException as exc:
            errors.append(exc)
        finally:
            instance.close()

    first = threading.Thread(target=drain)
    second = threading.Thread(target=drain)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        second.join(2)
    finally:
        release.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(world.provider.calls) == 1, "overlapping drainers paid for the same generation twice"
    assert_complete(world.store, node)
