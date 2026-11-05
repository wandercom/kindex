"""Tests for cache-optimized LLM retrieval — codebook, tiers, budget."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from kindex.budget import BudgetLedger
from kindex.config import BudgetConfig, Config, LLMConfig
from kindex.retrieve import (
    build_codebook_index,
    format_tier2,
    generate_codebook,
    predict_tier2,
)
from kindex.store import Store


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=str(tmp_path))


@pytest.fixture
def store(cfg):
    s = Store(cfg)
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Store with a mix of node types and weights."""
    store.add_node(
        title="Alpha Concept", content="Alpha content about systems",
        node_type="concept", node_id="aaaa1111", weight=0.8,
        domains=["systems"],
    )
    store.add_node(
        title="Beta Pattern", content="Beta content about design",
        node_type="concept", node_id="bbbb2222", weight=0.9,
        domains=["design"],
    )
    store.add_node(
        title="Gamma Session", content="Session transcript",
        node_type="session", node_id="cccc3333", weight=0.5,
    )
    store.add_node(
        title="Delta Low Weight", content="Low weight node",
        node_type="concept", node_id="dddd4444", weight=0.2,
    )
    store.add_node(
        title="Epsilon Document", content="Epsilon doc content",
        node_type="document", node_id="eeee5555", weight=0.7,
        domains=["research"],
    )
    # Add edges for tier 2 prediction
    store.add_edge("aaaa1111", "bbbb2222", edge_type="relates_to", weight=0.9)
    store.add_edge("bbbb2222", "eeee5555", edge_type="informs", weight=0.7)
    return store


# ── Codebook generation ───────────────────────────────────────────────


class TestCodebookGeneration:
    def test_deterministic(self, populated_store):
        """Same store produces identical codebook text and hash."""
        text1, hash1 = generate_codebook(populated_store)
        text2, hash2 = generate_codebook(populated_store)
        assert text1 == text2
        assert hash1 == hash2

    def test_sorted_by_node_id(self, populated_store):
        """Entries are sorted by node ID, not weight or title."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        import re
        ids = re.findall(r"id:(\w+)", text)
        assert ids == sorted(ids)

    def test_filters_sessions(self, populated_store):
        """Session nodes are excluded from codebook."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        assert "Gamma Session" not in text
        assert "cccc3333" not in text[:8]  # truncated ID

    def test_min_weight_filter(self, populated_store):
        """Nodes below min_weight are excluded."""
        text, _ = generate_codebook(populated_store, min_weight=0.5)
        assert "Delta Low Weight" not in text
        assert "Alpha Concept" in text
        assert "Beta Pattern" in text

    def test_default_min_weight(self, populated_store):
        """Default min_weight=0.5 filters appropriately."""
        text, _ = generate_codebook(populated_store)
        assert "Delta Low Weight" not in text  # weight 0.2
        assert "Epsilon Document" in text  # weight 0.7

    def test_hash_changes_on_new_node(self, populated_store):
        """Adding a node changes the codebook hash."""
        _, hash1 = generate_codebook(populated_store)
        populated_store.add_node(
            title="Zeta New", node_type="concept",
            node_id="ffff6666", weight=0.8,
        )
        _, hash2 = generate_codebook(populated_store)
        assert hash1 != hash2

    def test_header_contains_count(self, populated_store):
        """Codebook header shows entry count."""
        text, _ = generate_codebook(populated_store, min_weight=0.5)
        assert "[CODEBOOK v1 |" in text
        assert "entries]" in text

    def test_meta_round_trip(self, populated_store):
        """Codebook survives storage in meta table."""
        text, h = generate_codebook(populated_store)
        populated_store.set_meta("codebook_text", text)
        populated_store.set_meta("codebook_hash", h)
        assert populated_store.get_meta("codebook_text") == text
        assert populated_store.get_meta("codebook_hash") == h

    def test_empty_store(self, store):
        """Empty store produces empty codebook."""
        text, _ = generate_codebook(store)
        assert "0 entries" in text


# ── Codebook index ────────────────────────────────────────────────────


class TestCodebookIndex:
    def test_parses_entries(self, populated_store):
        """Index maps truncated IDs to entry numbers."""
        text, _ = generate_codebook(populated_store, min_weight=0.5)
        index = build_codebook_index(text)
        assert len(index) > 0
        # All values should be entry numbers like "#001"
        for entry_num in index.values():
            assert entry_num.startswith("#")

    def test_lookup_by_truncated_id(self, populated_store):
        """Can look up entry number by truncated node ID."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        index = build_codebook_index(text)
        assert "aaaa1111" in index
        assert "bbbb2222" in index

    def test_empty_codebook(self):
        """Empty codebook produces empty index."""
        index = build_codebook_index("[CODEBOOK v1 | 0 entries]")
        assert index == {}


# ── Tier 2 prediction ─────────────────────────────────────────────────


class TestTier2Prediction:
    def test_includes_search_results(self, populated_store):
        """Tier 2 includes direct search results."""
        search = [populated_store.get_node("aaaa1111")]
        search[0]["edges_out"] = populated_store.edges_from("aaaa1111")
        result = predict_tier2(populated_store, "systems", search, top_k=5)
        assert any(r["id"] == "aaaa1111" for r in result)

    def test_includes_graph_neighbors(self, populated_store):
        """Tier 2 includes 1-hop graph neighbors."""
        hit = populated_store.get_node("aaaa1111")
        hit["edges_out"] = populated_store.edges_from("aaaa1111")
        result = predict_tier2(populated_store, "systems", [hit], top_k=5)
        ids = {r["id"] for r in result}
        assert "bbbb2222" in ids  # neighbor via edge

    def test_excludes_sessions(self, populated_store):
        """Predicted nodes exclude session types."""
        # Add edge from a concept to the session
        populated_store.add_edge("aaaa1111", "cccc3333", edge_type="discussed_in", weight=0.8)
        hit = populated_store.get_node("aaaa1111")
        hit["edges_out"] = populated_store.edges_from("aaaa1111")
        result = predict_tier2(populated_store, "systems", [hit], top_k=5)
        assert not any(r["id"] == "cccc3333" for r in result)

    def test_respects_top_k(self, populated_store):
        """Result count respects top_k limit."""
        hit = populated_store.get_node("aaaa1111")
        hit["edges_out"] = populated_store.edges_from("aaaa1111")
        result = predict_tier2(populated_store, "systems", [hit], top_k=2)
        assert len(result) <= 2


# ── Tier 2 formatting ─────────────────────────────────────────────────


class TestTier2Formatting:
    def test_includes_titles(self, populated_store):
        """Format includes node titles."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        index = build_codebook_index(text)
        nodes = [populated_store.get_node("aaaa1111")]
        nodes[0]["edges_out"] = []
        result = format_tier2(nodes, index)
        assert "Alpha Concept" in result

    def test_sorted_by_id(self, populated_store):
        """Entries are sorted by node ID."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        index = build_codebook_index(text)
        n1 = populated_store.get_node("aaaa1111")
        n2 = populated_store.get_node("bbbb2222")
        n1["edges_out"] = []
        n2["edges_out"] = []
        result = format_tier2([n2, n1], index)  # out of order
        pos_a = result.find("Alpha")
        pos_b = result.find("Beta")
        assert pos_a < pos_b  # sorted by ID, a before b

    def test_respects_token_budget(self, populated_store):
        """Stops adding entries when token budget exceeded."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        index = build_codebook_index(text)
        nodes = []
        for nid in ["aaaa1111", "bbbb2222", "eeee5555"]:
            n = populated_store.get_node(nid)
            n["edges_out"] = []
            n["content"] = "x" * 2000  # large content
            nodes.append(n)
        result = format_tier2(nodes, index, max_tokens=100)
        # Should be truncated — not all nodes included
        assert len(result) < 2000

    def test_includes_codebook_refs(self, populated_store):
        """Format includes codebook entry references."""
        text, _ = generate_codebook(populated_store, min_weight=0.0)
        index = build_codebook_index(text)
        n = populated_store.get_node("aaaa1111")
        n["edges_out"] = populated_store.edges_from("aaaa1111")
        result = format_tier2([n], index)
        # Should have entry numbers from codebook
        assert "#" in result


# ── Cache-aware budget ────────────────────────────────────────────────


class TestCacheAwareBudget:
    def test_record_cache_fields(self, tmp_path):
        """Ledger records cache_creation and cache_read tokens."""
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        ledger.record(0.001, model="test", purpose="ask",
                      tokens_in=100, tokens_out=50,
                      cache_creation_tokens=2000, cache_read_tokens=0)
        assert ledger.entries[-1]["cache_creation_tokens"] == 2000

    def test_cache_read_recorded(self, tmp_path):
        """Cache read tokens are recorded."""
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        ledger.record(0.0005, model="test", purpose="ask",
                      tokens_in=50, tokens_out=30,
                      cache_creation_tokens=0, cache_read_tokens=3000)
        assert ledger.entries[-1]["cache_read_tokens"] == 3000

    def test_no_cache_fields_when_zero(self, tmp_path):
        """Cache fields omitted when zero (backward compat)."""
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        ledger.record(0.001, model="test", purpose="ask",
                      tokens_in=100, tokens_out=50)
        assert "cache_creation_tokens" not in ledger.entries[-1]
        assert "cache_read_tokens" not in ledger.entries[-1]

    def test_backward_compat_load(self, tmp_path):
        """Old ledger entries without cache fields load fine."""
        import yaml
        old_data = {"entries": [
            {"date": "2026-02-25", "amount": 0.001, "model": "test",
             "purpose": "ask", "tokens_in": 100, "tokens_out": 50}
        ]}
        (tmp_path / "b.yaml").write_text(yaml.dump(old_data))
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        assert len(ledger.entries) == 1
        # cache_efficiency should handle missing fields
        eff = ledger.cache_efficiency()
        assert eff["cache_hit_rate"] == 0.0

    def test_cache_efficiency(self, tmp_path):
        """Cache efficiency calculates hit rate."""
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        ledger.record(0.001, cache_creation_tokens=1000, cache_read_tokens=0)
        ledger.record(0.0001, cache_creation_tokens=0, cache_read_tokens=1000)
        eff = ledger.cache_efficiency()
        assert eff["cache_hit_rate"] == 0.5  # 1000 read / 2000 total
        assert eff["cache_read_tokens"] == 1000
        assert eff["cache_write_tokens"] == 1000

    def test_summary_includes_cache(self, tmp_path):
        """Summary includes cache info when cache tokens exist."""
        ledger = BudgetLedger(tmp_path / "b.yaml", BudgetConfig())
        ledger.record(0.001, cache_creation_tokens=500, cache_read_tokens=500)
        s = ledger.summary()
        assert "cache" in s
        assert s["cache"]["cache_hit_rate"] == 0.5


# ── LLM pricing ──────────────────────────────────────────────────────


class TestCalculateCost:
    def test_basic_cost(self):
        from kindex.llm import calculate_cost
        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 100
        usage.cache_creation_input_tokens = 0
        usage.cache_read_input_tokens = 0
        result = calculate_cost("claude-haiku-4-5-20251001", usage)
        assert result["amount"] > 0
        assert result["tokens_in"] == 1000
        assert result["tokens_out"] == 100

    def test_cache_write_cost(self):
        from kindex.llm import calculate_cost
        usage = MagicMock()
        usage.input_tokens = 0
        usage.output_tokens = 100
        usage.cache_creation_input_tokens = 2000
        usage.cache_read_input_tokens = 0
        result = calculate_cost("claude-haiku-4-5-20251001", usage)
        assert result["cache_creation_tokens"] == 2000
        assert result["amount"] > 0

    def test_cache_read_cheaper(self):
        from kindex.llm import calculate_cost
        # Same token count, cache read vs regular input
        usage_regular = MagicMock()
        usage_regular.input_tokens = 2000
        usage_regular.output_tokens = 0
        usage_regular.cache_creation_input_tokens = 0
        usage_regular.cache_read_input_tokens = 0

        usage_cached = MagicMock()
        usage_cached.input_tokens = 0
        usage_cached.output_tokens = 0
        usage_cached.cache_creation_input_tokens = 0
        usage_cached.cache_read_input_tokens = 2000

        cost_regular = calculate_cost("claude-haiku-4-5-20251001", usage_regular)
        cost_cached = calculate_cost("claude-haiku-4-5-20251001", usage_cached)
        assert cost_cached["amount"] < cost_regular["amount"]

    def test_unknown_model_uses_defaults(self):
        from kindex.llm import calculate_cost
        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 100
        usage.cache_creation_input_tokens = 0
        usage.cache_read_input_tokens = 0
        result = calculate_cost("unknown-model-v99", usage)
        assert result["amount"] > 0

    def test_unknown_model_is_costed_at_the_highest_known_rate(self):
        from kindex.llm import PRICING, _estimate_cost
        highest = max(_estimate_cost(m, 1000, 100) for m in PRICING)
        assert _estimate_cost("unknown-model-v99", 1000, 100) == highest

    def test_platform_prefixed_and_dated_ids_use_the_model_price(self):
        from kindex.llm import price_for, PRICING
        assert price_for("us.anthropic.claude-opus-5-5") is PRICING["claude-opus-5-5"]
        assert price_for("anthropic.claude-sonnet-5-5") is PRICING["claude-sonnet-5-5"]
        assert price_for("claude-haiku-4-5-20251001") is PRICING["claude-haiku-4-5-20251001"]

    def test_openai_cached_usage_from_dict(self):
        from kindex.llm import calculate_cost
        usage = {
            "input_tokens": 1000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 400},
        }

        result = calculate_cost("gpt-5.4-nano", usage)

        assert result["tokens_in"] == 1000
        assert result["tokens_out"] == 100
        assert result["cache_read_tokens"] == 400


class TestLLMProviderConfig:
    def test_resolve_api_key_uses_first_available_fallback(self, monkeypatch):
        from kindex.llm import resolve_api_key

        monkeypatch.delenv("ORG_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
        cfg = Config(llm=LLMConfig(
            enabled=True,
            provider="openai",
            api_key_env="ORG_OPENAI_API_KEY,OPENAI_API_KEY",
        ))

        key, env_name = resolve_api_key(cfg)

        assert key == "test-openai"
        assert env_name == "OPENAI_API_KEY"

    def test_openai_configured_with_fallback_key(self, monkeypatch):
        from kindex.llm import is_configured

        monkeypatch.delenv("ORG_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
        cfg = Config(llm=LLMConfig(
            enabled=True,
            provider="openai",
            api_key_env="ORG_OPENAI_API_KEY,OPENAI_API_KEY",
        ))

        assert is_configured(cfg) is True


# ── Config ────────────────────────────────────────────────────────────


class TestLLMConfig:
    def test_cache_control_default_true(self):
        cfg = LLMConfig()
        assert cfg.cache_control is True

    def test_codebook_min_weight_default(self):
        cfg = LLMConfig()
        assert cfg.codebook_min_weight == 0.5

    def test_tier2_max_tokens_default(self):
        cfg = LLMConfig()
        assert cfg.tier2_max_tokens == 4000

    def test_timeout_default(self):
        cfg = LLMConfig()
        assert cfg.timeout == 6.0

    def test_timeout_parses_value(self):
        cfg = LLMConfig(timeout=30)
        assert cfg.timeout == 30.0

    def test_timeout_rejects_non_positive(self):
        with pytest.raises(ValidationError):
            LLMConfig(timeout=0)
        with pytest.raises(ValidationError):
            LLMConfig(timeout=-1)
        with pytest.raises(ValidationError):
            LLMConfig(timeout=4000)
        with pytest.raises(ValidationError):
            LLMConfig(timeout=float("inf"))


# ── Response text (thinking blocks before the reply) ──────────────────


class TestResponseText:
    def test_skips_thinking_blocks(self):
        from types import SimpleNamespace
        from kindex.llm import response_text
        response = SimpleNamespace(content=[
            SimpleNamespace(type="thinking", thinking="", signature="sig"),
            SimpleNamespace(type="text", text='{"ok": true}'),
        ])
        assert response_text(response) == '{"ok": true}'

    def test_untyped_blocks_are_text(self):
        from types import SimpleNamespace
        from kindex.llm import response_text
        response = SimpleNamespace(content=[SimpleNamespace(text="plain")])
        assert response_text(response) == "plain"

    def test_no_text_is_empty(self):
        from types import SimpleNamespace
        from kindex.llm import response_text
        assert response_text(SimpleNamespace(content=[])) == ""


class TestThinkingHeadroom:
    def test_thinking_models_get_headroom(self):
        from kindex.llm import THINKING_HEADROOM_TOKENS, max_tokens_for
        for model in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                      "claude-sonnet-5", "claude-fable-5-1", "anthropic.claude-opus-5-5"):
            assert max_tokens_for(model, 300) == 300 + THINKING_HEADROOM_TOKENS, model

    def test_thinking_off_models_keep_the_reply_cap(self):
        from kindex.llm import max_tokens_for
        for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001",
                      "claude-sonnet-4-6", "claude-opus-4-8", "gpt-5.4-mini"):
            assert max_tokens_for(model, 300) == 300, model


class TestCurrentModelPricing:
    def test_current_models_are_priced(self):
        from kindex.llm import PRICING
        for model in ("claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5",
                      "claude-opus-5", "claude-fable-5-1"):
            assert model in PRICING, model

    def test_opus_5_5_is_not_billed_at_the_default_price(self):
        from kindex.llm import calculate_cost
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        assert calculate_cost("claude-opus-5-5", usage)["amount"] == 4.0


class TestMinCacheableTokens:
    def test_documented_minimums(self):
        from kindex.llm import min_cacheable_tokens
        assert min_cacheable_tokens("claude-haiku-4-5-20251001") == 4096
        assert min_cacheable_tokens("claude-opus-4-6") == 4096
        assert min_cacheable_tokens("claude-opus-4-7") == 2048
        assert min_cacheable_tokens("claude-sonnet-4-6") == 1024
        assert min_cacheable_tokens("claude-sonnet-5") == 1024
        assert min_cacheable_tokens("claude-sonnet-5-5") == 512
        assert min_cacheable_tokens("claude-opus-5") == 512
        assert min_cacheable_tokens("claude-opus-5-5") == 512
