"""Acceptance tests for terminal embedding-failure quarantine.

Oracle: ratified Kindex issue #32 acceptance contract.
"""

import io
import json
from urllib.error import HTTPError

import pytest

import kindex.vectors as vectors
from kindex.config import Config, EmbeddingConfig
from kindex.store import Store
from kindex.vectors import EMBED_QUEUE_META, drain_embedding_queue, enqueue_embedding


# Ratified durable, versioned meta payload from issue #32's approved design.
QUARANTINE_META = "embed.quarantine.v1"


@pytest.fixture
def store(tmp_path):
    instance = Store(
        Config(
            data_dir=str(tmp_path),
            embedding=EmbeddingConfig(provider="openai"),
        )
    )
    yield instance
    instance.close()


@pytest.fixture
def openai_config():
    return Config(embedding=EmbeddingConfig(provider="openai"))


def _queue(store):
    return json.loads(store.get_meta(EMBED_QUEUE_META) or "[]")


def _quarantine(store):
    raw = store.get_meta(QUARANTINE_META)
    return json.loads(raw) if raw else {"version": 1, "items": {}}


def _context_limit_error():
    return HTTPError(
        "https://api.openai.com/v1/embeddings",
        400,
        "Bad Request",
        None,
        io.BytesIO(b'{"error":{"message":"maximum context length is 8192 tokens"}}'),
    )


class TestEmbeddingQuarantine:
    def test_public_upsert_keeps_legacy_boolean_contract_for_terminal_failure(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(
            vectors,
            "_upsert_embedding_outcome",
            lambda *_args: vectors._EmbeddingOutcome(
                False, terminal=True, kind="input_too_large"
            ),
        )

        assert vectors.upsert_embedding(store, "node-id", "too large") is False

    def test_terminal_context_limit_failure_is_quarantined_and_dropped_from_queue(
        self, store, openai_config, monkeypatch
    ):
        """Contract 1: deterministic failures do not permanently block draining."""
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        provider_error = vectors._provider_http_error(_context_limit_error())
        assert provider_error.kind == "input_too_large"
        monkeypatch.setattr(
            vectors,
            "_upsert_embedding_outcome",
            lambda *_args: vectors._EmbeddingOutcome(
                False,
                terminal=True,
                kind=provider_error.kind,
                http_status=provider_error.http_status,
                message=str(provider_error),
            ),
        )
        node_id = store.add_node("Archived checkpoint", content="x" * 202_000)

        result = drain_embedding_queue(store, openai_config)

        assert result["embedded"] == 0
        assert result["pending"] == 0
        assert result["quarantined"] == 1
        assert result["drain_complete"] is True
        assert _queue(store) == []
        quarantine = _quarantine(store)
        assert quarantine["version"] == 1
        record = quarantine["items"][node_id]
        assert record["kind"] == "input_too_large"
        assert record["http_status"] == 400
        assert "8192" in record["message"]

    def test_openai_http_error_reaches_drain_classifier(self, store, monkeypatch):
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(vectors, "ensure_vec_table", lambda _store: True)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        def reject_oversize(*_args, **_kwargs):
            raise _context_limit_error()
        monkeypatch.setattr(
            vectors.urllib.request, "urlopen", reject_oversize
        )
        node_id = store.add_node("HTTP path", content="x" * 202_000)

        result = drain_embedding_queue(store)

        assert result["pending"] == 0
        assert _quarantine(store)["items"][node_id]["kind"] == "input_too_large"

    def test_transient_provider_failure_remains_actionable_and_is_not_quarantined(
        self, store, openai_config, monkeypatch
    ):
        """Contract 2: a 429 may recover, so it remains queued."""
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        def rate_limited(*_args, **_kwargs):
            raise HTTPError("https://api.openai.com/v1/embeddings", 429, "Too Many Requests", None, None)

        monkeypatch.setattr(vectors.urllib.request, "urlopen", rate_limited)
        node_id = store.add_node("Retry later", content="body")

        result = drain_embedding_queue(store, openai_config)

        assert result["embedded"] == 0
        assert result["pending"] == 1
        assert _queue(store) == [node_id]
        assert node_id not in _quarantine(store)["items"]

    def test_reenqueue_clears_prior_quarantine_before_queuing(self, store):
        """Contract 3: a changed node gets another embedding attempt."""
        node_id = store.add_node("Edited checkpoint", content="replacement")
        store.set_meta(EMBED_QUEUE_META, "[]")
        store.set_meta(
            QUARANTINE_META,
            json.dumps(
                {
                    "version": 1,
                    "items": {node_id: {"kind": "input_too_large", "attempts": 1}},
                }
            ),
        )

        assert enqueue_embedding(store, node_id) is True

        assert _queue(store) == [node_id]
        assert node_id not in _quarantine(store)["items"]

    def test_unchanged_quarantine_is_not_sent_to_provider_again(self, store, monkeypatch):
        node_id = store.add_node("Rejected", content="body")
        text = vectors._embedding_text_for_node(store.get_node(node_id))
        store.set_meta(
            QUARANTINE_META,
            json.dumps({"version": 1, "items": {node_id: {
                "kind": "input_too_large", "text_hash": vectors._hash_text(text),
                "fingerprint": vectors.embedding_fingerprint(store.config), "attempts": 1,
            }}}),
        )
        calls = []
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(
            vectors, "_upsert_embedding_outcome",
            lambda *_args: calls.append(True) or vectors._EmbeddingOutcome(True),
        )

        result = drain_embedding_queue(store)

        assert calls == []
        assert result["pending"] == 0
        assert node_id in _quarantine(store)["items"]

    def test_delete_retires_quarantine_even_without_vector_table(self, store):
        node_id = store.add_node("Deleted", content="body")
        store.set_meta(QUARANTINE_META, json.dumps({"version": 1, "items": {
            node_id: {"kind": "input_too_large"},
        }}))

        vectors.delete_embedding(store, node_id)

        assert node_id not in _quarantine(store)["items"]

    def test_successful_direct_reindex_retires_quarantine(self, store, monkeypatch):
        node_id = store.add_node("Recovered", content="body")
        text = vectors._embedding_text_for_node(store.get_node(node_id))
        store.set_meta(QUARANTINE_META, json.dumps({"version": 1, "items": {
            node_id: {
                "kind": "input_too_large", "text_hash": vectors._hash_text(text),
                "fingerprint": vectors.embedding_fingerprint(store.config),
            },
        }}))
        store.conn.execute("CREATE TABLE node_vectors (node_id TEXT PRIMARY KEY, embedding BLOB)")
        monkeypatch.setattr(vectors, "ensure_vec_table", lambda _store: True)
        monkeypatch.setattr(vectors, "_embed_document_chunks", lambda *_args, **_kwargs: [{
            "index": 0, "text": text, "embedding": [0.1],
            "text_hash": vectors._hash_text(text), "token_estimate": 1,
        }])

        assert vectors.upsert_embedding(store, node_id, text) is True
        assert node_id not in _quarantine(store)["items"]

    def test_malformed_attempts_cannot_abort_drain(self, store, monkeypatch):
        node_id = store.add_node("Malformed", content="body")
        store.set_meta(QUARANTINE_META, json.dumps({"version": 1, "items": {
            node_id: {"kind": "input_too_large", "attempts": "not-an-int"},
        }}))
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(
            vectors, "_upsert_embedding_outcome",
            lambda *_args: vectors._EmbeddingOutcome(False, terminal=True, kind="input_too_large"),
        )

        assert drain_embedding_queue(store)["status"] == "ok"
        assert _quarantine(store)["items"][node_id]["attempts"] == 1

    def test_enqueue_persists_when_caller_has_pending_dml(self, store):
        node_id = store.add_node("Pending transaction", content="body")
        store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("test.pending", "1"))

        assert enqueue_embedding(store, node_id) is True
        assert node_id in _queue(store)

    def test_drain_result_reports_actionable_pending_quarantine_and_drain_completion(self, store, monkeypatch):
        """Contract 4: drain completion and complete vector coverage are distinct."""
        node_id = store.add_node("Quarantined", content="body")
        store.set_meta(EMBED_QUEUE_META, "[]")
        store.set_meta(
            QUARANTINE_META,
            json.dumps(
                {"version": 1, "items": {node_id: {"kind": "input_too_large"}}},
            ),
        )

        monkeypatch.setattr(vectors, "is_available", lambda: True)
        result = drain_embedding_queue(store)

        assert result["pending"] == 0
        assert result["quarantined"] == 1
        assert result["drain_complete"] is True
        assert result["coverage_complete"] is False

        status = vectors.embedding_status(store)
        assert status["queue_pending"] == 0
        assert status["quarantined"] == 1
        assert status["drain_complete"] is True
        assert status["coverage_complete"] is False

    def test_status_groups_quarantine_by_justification(self, store):
        first = store.add_node("First", content="body")
        second = store.add_node("Second", content="body")
        store.set_meta(QUARANTINE_META, json.dumps({"version": 1, "items": {
            first: {"kind": "input_too_large", "http_status": 400, "message": "context limit"},
            second: {"kind": "input_too_large", "http_status": 400, "message": "context limit"},
        }}))

        summary = vectors.embedding_status(store)["quarantine_summary"]

        assert summary == [{
            "kind": "input_too_large", "http_status": 400,
            "message": "context limit", "count": 2,
        }]

    @pytest.mark.parametrize("raw", ["", "not-json", "[]", '{"version": 1, "items": null}'])
    def test_empty_or_malformed_legacy_quarantine_meta_is_tolerated(self, store, raw, monkeypatch):
        """Contract 5: upgrading from absent/corrupt meta does not break draining."""
        store.set_meta(QUARANTINE_META, raw)
        monkeypatch.setattr(vectors, "is_available", lambda: True)

        result = drain_embedding_queue(store)

        assert result["status"] == "empty"
