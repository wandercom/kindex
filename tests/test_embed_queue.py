"""Tests for the deferred embedding queue (enqueue on the hot path, drain in
the daemon) — see vectors.enqueue_embedding / drain_embedding_queue.

Deferral keeps a slow embedding provider off the add/edit/supersede hot path;
these tests pin the queue semantics (dedup, ordering, bounding, transient
retry) and that the hot path no longer embeds synchronously.
"""

import json

import pytest

import kindex.vectors as vectors
from kindex.config import Config
from kindex.store import Store
from kindex.vectors import (
    EMBED_QUEUE_META,
    drain_embedding_queue,
    enqueue_embedding,
)


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


@pytest.fixture
def embed_calls(monkeypatch):
    """Capture upsert_embedding calls and pretend the backend is available."""
    calls = []
    monkeypatch.setattr(vectors, "is_available", lambda: True)
    monkeypatch.setattr(
        vectors, "upsert_embedding",
        lambda store, node_id, text: calls.append((node_id, text)) or True,
    )
    return calls


def _queue(store):
    raw = store.get_meta(EMBED_QUEUE_META)
    return json.loads(raw) if raw else []


class TestEnqueue:
    def test_enqueue_appends_node_id(self, store):
        enqueue_embedding(store, "n1")
        assert _queue(store) == ["n1"]

    def test_enqueue_dedups_moving_to_tail(self, store):
        enqueue_embedding(store, "a")
        enqueue_embedding(store, "b")
        enqueue_embedding(store, "a")  # re-enqueue moves to tail, no duplicate
        assert _queue(store) == ["b", "a"]

    def test_enqueue_empty_id_is_noop(self, store):
        assert enqueue_embedding(store, "") is False
        assert _queue(store) == []

    def test_enqueue_bounds_queue(self, store):
        for i in range(10):
            enqueue_embedding(store, f"n{i}", max_queue=3)
        assert _queue(store) == ["n7", "n8", "n9"]


class TestDrain:
    def test_drain_empty(self, store, embed_calls):
        assert drain_embedding_queue(store)["status"] == "empty"
        assert embed_calls == []

    def test_drain_embeds_current_text_and_clears(self, store, embed_calls):
        nid = store.add_node("Title", content="body", node_type="concept")
        # add_node enqueued nid but did NOT embed synchronously.
        assert embed_calls == []
        assert _queue(store) == [nid]

        res = drain_embedding_queue(store)
        assert res["status"] == "ok"
        assert res["embedded"] == 1
        assert res["pending"] == 0
        assert res["quarantined"] == 0
        assert res["drain_complete"] is True
        assert res["coverage_complete"] is True
        assert embed_calls == [(nid, "Title body")]
        assert _queue(store) == []

    def test_drain_skips_missing_and_superseded(self, store, embed_calls):
        nid = store.add_node("Live", content="x", node_type="concept")
        enqueue_embedding(store, "ghost-id")           # node never existed
        store.supersede_node(nid, "Live v2")           # nid -> superseded
        drain_embedding_queue(store)
        embedded_ids = {c[0] for c in embed_calls}
        assert nid not in embedded_ids                 # superseded -> dropped
        assert "ghost-id" not in embedded_ids          # missing -> dropped

    def test_drain_unavailable_keeps_queue(self, store, monkeypatch):
        monkeypatch.setattr(vectors, "is_available", lambda: False)
        enqueue_embedding(store, "n1")
        res = drain_embedding_queue(store)
        assert res["status"] == "unavailable"
        assert _queue(store) == ["n1"]                 # left intact for later

    def test_drain_requeues_transient_failure(self, store, monkeypatch):
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(
            vectors, "upsert_embedding",
            lambda store, node_id, text: False,        # provider down
        )
        store.add_node("Flaky", content="x", node_type="concept")
        res = drain_embedding_queue(store)
        assert res["embedded"] == 0
        assert len(_queue(store)) == 1                 # retried next cron

    def test_drain_swallows_raising_provider(self, store, monkeypatch):
        monkeypatch.setattr(vectors, "is_available", lambda: True)

        def boom(*a, **kw):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(vectors, "upsert_embedding", boom)
        store.add_node("Boom", content="x", node_type="concept")
        # Must not propagate; node is re-queued for a later attempt.
        res = drain_embedding_queue(store)
        assert res["embedded"] == 0
        assert len(_queue(store)) == 1

    def test_drain_bounded_by_max_jobs(self, store, embed_calls):
        ids = [store.add_node(f"x{i}", content="c", node_type="concept")
               for i in range(5)]
        # add_node already enqueued these five ids; pin the queue to exactly them.
        store.set_meta(EMBED_QUEUE_META, json.dumps(ids))
        res = drain_embedding_queue(store, max_jobs=2)
        assert res["embedded"] == 2
        assert res["pending"] == 3
        assert _queue(store) == ids[2:]                # the carried remainder
