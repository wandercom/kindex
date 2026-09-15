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

    @pytest.mark.parametrize("raw", ["", "not-json", "[]", '{"version": 1, "items": null}'])
    def test_empty_or_malformed_legacy_quarantine_meta_is_tolerated(self, store, raw, monkeypatch):
        """Contract 5: upgrading from absent/corrupt meta does not break draining."""
        store.set_meta(QUARANTINE_META, raw)
        monkeypatch.setattr(vectors, "is_available", lambda: True)

        result = drain_embedding_queue(store)

        assert result["status"] == "empty"
