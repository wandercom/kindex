"""Tests for optional vector search module."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from kindex import vectors
from kindex.vectors import (
    _check_vec, _chunk_text, _embed_voyage, _resolve_embedding_config,
    contextual_embeddings_supported, embed_document_chunks, embed_text,
    embedding_strategy, enqueue_reindex, estimate_reindex_cost, is_available,
    PROVIDER_DEFAULTS, select_reindex_nodes, upsert_embedding, vector_search,
)
from kindex.config import Config, EmbeddingConfig
from kindex.store import Store


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestVectorAvailability:
    def test_is_available_returns_bool(self):
        result = is_available()
        assert isinstance(result, bool)

    def test_check_vec_consistent(self):
        """_check_vec should return same result on repeated calls."""
        a = _check_vec()
        b = _check_vec()
        assert a == b

    def test_embed_text_without_model(self):
        """embed_text should return None if sentence-transformers not installed."""
        result = embed_text("test text")
        assert result is None or isinstance(result, list)


class TestProviderConfigResolution:
    def test_none_config_defaults_to_voyage(self):
        provider, model, dims, key_env = _resolve_embedding_config(None)
        assert provider == "voyage"
        assert model == "voyage-context-4"
        assert dims == 1024
        assert key_env == "VOYAGE_API_KEY"

    def test_default_config_is_voyage(self):
        config = Config()
        provider, model, dims, key_env = _resolve_embedding_config(config)
        assert provider == "voyage"
        assert model == "voyage-context-4"
        assert dims == 1024
        assert key_env == "VOYAGE_API_KEY"

    def test_openai_provider_defaults(self):
        config = Config(embedding=EmbeddingConfig(provider="openai"))
        provider, model, dims, key_env = _resolve_embedding_config(config)
        assert provider == "openai"
        assert model == "text-embedding-3-small"
        assert dims == 1536
        assert key_env == "OPENAI_API_KEY"

    def test_gemini_provider_defaults(self):
        config = Config(embedding=EmbeddingConfig(provider="gemini"))
        provider, model, dims, key_env = _resolve_embedding_config(config)
        assert provider == "gemini"
        assert model == "gemini-embedding-001"
        assert dims == 3072
        assert key_env == "GEMINI_API_KEY"

    def test_voyage_provider_defaults(self):
        config = Config(embedding=EmbeddingConfig(provider="voyage"))
        provider, model, dims, key_env = _resolve_embedding_config(config)
        assert provider == "voyage"
        assert model == "voyage-context-4"
        assert dims == 1024
        assert key_env == "VOYAGE_API_KEY"

    def test_custom_model_override(self):
        config = Config(embedding=EmbeddingConfig(
            provider="openai", model="text-embedding-3-large"
        ))
        _, model, _, _ = _resolve_embedding_config(config)
        assert model == "text-embedding-3-large"

    def test_custom_dimensions_override(self):
        config = Config(embedding=EmbeddingConfig(
            provider="openai", dimensions=512
        ))
        _, _, dims, _ = _resolve_embedding_config(config)
        assert dims == 512

    def test_custom_api_key_env_override(self):
        config = Config(embedding=EmbeddingConfig(
            provider="openai", api_key_env="MY_CUSTOM_KEY"
        ))
        _, _, _, key_env = _resolve_embedding_config(config)
        assert key_env == "MY_CUSTOM_KEY"

    def test_unknown_provider_falls_back_to_local_defaults(self):
        config = Config(embedding=EmbeddingConfig(provider="unknown"))
        provider, model, dims, _ = _resolve_embedding_config(config)
        assert provider == "unknown"
        assert model == "all-MiniLM-L6-v2"
        assert dims == 384

    def test_contextual_strategy_is_provider_gated(self):
        voyage = Config(embedding=EmbeddingConfig(provider="voyage"))
        openai = Config(embedding=EmbeddingConfig(
            provider="openai",
            strategy="contextual",
        ))

        assert contextual_embeddings_supported(voyage)
        assert embedding_strategy(voyage) == "contextual"
        assert not contextual_embeddings_supported(openai)
        assert embedding_strategy(openai) == "single"


class TestEmbedTextDispatch:
    def test_unknown_provider_returns_none(self):
        config = Config(embedding=EmbeddingConfig(provider="nonexistent"))
        result = embed_text("hello", config=config)
        assert result is None

    def test_openai_without_key_returns_none(self):
        config = Config(embedding=EmbeddingConfig(provider="openai"))
        with patch.dict("os.environ", {}, clear=True):
            result = embed_text("hello", config=config)
        assert result is None

    def test_gemini_without_key_returns_none(self):
        config = Config(embedding=EmbeddingConfig(provider="gemini"))
        with patch.dict("os.environ", {}, clear=True):
            result = embed_text("hello", config=config)
        assert result is None

    def test_local_dispatch(self):
        """embed_text with local provider calls _embed_local path."""
        config = Config(embedding=EmbeddingConfig(provider="local"))
        with patch("kindex.vectors._embed_local", return_value=[0.1, 0.2]) as mock_embed:
            result = embed_text("test", config=config)
        mock_embed.assert_called_once_with("test", "all-MiniLM-L6-v2")
        assert result == [0.1, 0.2]

    def test_voyage_context_model_uses_contextualized_endpoint(self):
        requests = []

        def fake_urlopen(req, **_kwargs):
            requests.append(req)
            return FakeHTTPResponse({
                "data": [{"data": [{"embedding": [0.1, 0.2], "index": 0}]}],
                "model": "voyage-context-4",
            })

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"}, clear=True):
            with patch("kindex.vectors.urllib.request.urlopen", fake_urlopen):
                result = _embed_voyage(
                    "hello",
                    "voyage-context-4",
                    1024,
                    "VOYAGE_API_KEY",
                    input_type="document",
                )

        assert result == [0.1, 0.2]
        assert requests[0].get_full_url() == "https://api.voyageai.com/v1/contextualizedembeddings"
        body = json.loads(requests[0].data.decode("utf-8"))
        assert body == {
            "inputs": [["hello"]],
            "model": "voyage-context-4",
            "input_type": "document",
            "output_dimension": 1024,
        }

    def test_voyage_context_query_uses_query_input_shape(self):
        requests = []

        def fake_urlopen(req, **_kwargs):
            requests.append(req)
            return FakeHTTPResponse({
                "data": [{"data": [{"embedding": [0.3, 0.4], "index": 0}]}],
                "model": "voyage-context-4",
            })

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"}, clear=True):
            with patch("kindex.vectors.urllib.request.urlopen", fake_urlopen):
                result = _embed_voyage(
                    "find this",
                    "voyage-context-4",
                    1024,
                    "VOYAGE_API_KEY",
                    input_type="query",
                )

        assert result == [0.3, 0.4]
        body = json.loads(requests[0].data.decode("utf-8"))
        assert body["inputs"] == ["find this"]
        assert body["input_type"] == "query"

    def test_voyage_standard_model_keeps_embeddings_endpoint(self):
        requests = []

        def fake_urlopen(req, **_kwargs):
            requests.append(req)
            return FakeHTTPResponse({"data": [{"embedding": [0.5, 0.6]}]})

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"}, clear=True):
            with patch("kindex.vectors.urllib.request.urlopen", fake_urlopen):
                result = _embed_voyage(
                    "legacy",
                    "voyage-3.5",
                    1024,
                    "VOYAGE_API_KEY",
                    input_type="document",
                )

        assert result == [0.5, 0.6]
        assert requests[0].get_full_url() == "https://api.voyageai.com/v1/embeddings"
        body = json.loads(requests[0].data.decode("utf-8"))
        assert body == {
            "input": ["legacy"],
            "model": "voyage-3.5",
            "input_type": "document",
        }

    def test_vector_search_embeds_query_as_query(self, monkeypatch):
        seen = {}

        def fake_ensure_vec_table(store):
            seen["ensured_store"] = store
            return True

        def fake_embed_text(text, config=None, *, input_type="document"):
            seen["text"] = text
            seen["config"] = config
            seen["input_type"] = input_type
            return None

        config = Config()
        store = SimpleNamespace(config=config)
        monkeypatch.setattr(vectors, "ensure_vec_table", fake_ensure_vec_table)
        monkeypatch.setattr(vectors, "embed_text", fake_embed_text)

        assert vector_search(store, "find me") == []
        assert seen["ensured_store"] is store
        assert seen == {
            "ensured_store": store,
            "text": "find me",
            "config": config,
            "input_type": "query",
        }

    def test_chunk_text_uses_overlap(self):
        assert _chunk_text("abcdefghij", chunk_chars=4, overlap_chars=1) == [
            "abcd", "defg", "ghij",
        ]

    def test_contextual_document_embedding_batches_chunks(self, monkeypatch):
        calls = []
        config = Config(embedding=EmbeddingConfig(
            provider="voyage",
            chunk_chars=4,
            chunk_overlap_chars=0,
            max_group_chunks=2,
        ))

        def fake_embed(group, *_args, **_kwargs):
            calls.append(list(group))
            return [[float(len(c))] for c in group]

        monkeypatch.setattr(vectors, "_embed_voyage_context_chunks", fake_embed)
        records = embed_document_chunks("abcdefghijkl", config)

        assert calls == [["abcd", "efgh"], ["ijkl"]]
        assert [r["embedding"] for r in records] == [[4.0], [4.0], [4.0]]
        assert [r["index"] for r in records] == [0, 1, 2]

    def test_upsert_embedding_stores_chunk_metadata(self, tmp_path, monkeypatch):
        store = Store(Config(data_dir=str(tmp_path)))
        try:
            store.conn.execute(
                "CREATE TABLE node_vectors (node_id TEXT PRIMARY KEY, embedding BLOB)"
            )
            vectors._ensure_vector_meta_table(store)
            monkeypatch.setattr(vectors, "ensure_vec_table", lambda store: True)
            monkeypatch.setattr(vectors, "_embed_document_chunks", lambda text, config, *, embed_one: [
                {
                    "index": 0,
                    "text": "first",
                    "embedding": [0.1, 0.2],
                    "text_hash": "hash",
                    "token_estimate": 2,
                },
                {
                    "index": 1,
                    "text": "second",
                    "embedding": [0.3, 0.4],
                    "text_hash": "hash",
                    "token_estimate": 2,
                },
            ])

            assert upsert_embedding(store, "node-1", "ignored")
            rows = store.conn.execute(
                "SELECT node_id FROM node_vectors ORDER BY node_id"
            ).fetchall()
            meta = store.conn.execute(
                "SELECT vector_id, node_id, chunk_index, chunk_count "
                "FROM node_vector_meta ORDER BY chunk_index"
            ).fetchall()

            assert [r["node_id"] for r in rows] == ["node-1#0000", "node-1#0001"]
            assert [(r["vector_id"], r["node_id"], r["chunk_index"], r["chunk_count"])
                    for r in meta] == [
                        ("node-1#0000", "node-1", 0, 2),
                        ("node-1#0001", "node-1", 1, 2),
                    ]
        finally:
            store.close()

    def test_reindex_selection_estimate_and_enqueue(self, tmp_path):
        store = Store(Config(data_dir=str(tmp_path)))
        try:
            a = store.add_node("Alpha", content="body", domains=["kindex"])
            store.add_node("Beta", content="body", domains=["other"])

            selected = select_reindex_nodes(store, tags=["kindex"])
            estimate = estimate_reindex_cost(selected, store.config)
            result = enqueue_reindex(store, tags=["kindex"])

            assert [n["id"] for n in selected] == [a]
            assert estimate["nodes"] == 1
            assert estimate["chunks"] == 1
            assert result["enqueued"] == 1
        finally:
            store.close()


class TestProviderDefaults:
    def test_all_providers_have_required_keys(self):
        for name, defaults in PROVIDER_DEFAULTS.items():
            assert "model" in defaults, f"{name} missing model"
            assert "dimensions" in defaults, f"{name} missing dimensions"
            assert "api_key_env" in defaults, f"{name} missing api_key_env"

    def test_local_needs_no_api_key(self):
        assert PROVIDER_DEFAULTS["local"]["api_key_env"] == ""

    def test_remote_providers_have_api_key_env(self):
        for name in ("openai", "gemini"):
            assert PROVIDER_DEFAULTS[name]["api_key_env"] != "", f"{name} should require an API key"
