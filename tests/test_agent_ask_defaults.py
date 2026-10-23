"""`kin ask` and MCP `ask` on an agent's own graph: the defaults are not the
benchmark configuration, and benchmark heuristics do not misfire on notes and code."""

import json
from types import SimpleNamespace

import pytest

import kindex.answer as answer
from kindex.config import AskConfig, Config, ConversationsConfig
from kindex.store import Store, unconfirmed_digest_directive


@pytest.fixture
def graph(tmp_path):
    """A coding graph: long decisions and code notes, no imported conversations."""
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    for i in range(40):
        store.add_node(f"Decision {i}: queue retries", node_id=f"d{i}", node_type="decision",
                       content=f"Decision: retry the queue {i} times\nRationale: " + "backoff and jitter. " * 120,
                       prov_activity="manual-add")
    yield store, cfg
    store.close()


def test_code_and_decisions_get_the_single_answer_budget(graph):
    store, _ = graph
    results = [store.get_node(f"d{i}") for i in range(3)]
    assert not answer.undigested(results)
    cfg = AskConfig()
    assert answer.context_budget(cfg, False, not answer.undigested(results)) == cfg.context_tokens
    chunks = [{"prov_activity": "conversation-ingest", "content": "user: hi"}]
    assert answer.undigested(chunks)  # raw conversation text without facts still reads wide
    assert answer.context_budget(cfg, False, not answer.undigested(chunks)) == cfg.wide_context_tokens


def test_mcp_evidence_for_an_agent_fits_the_context_tier(graph, monkeypatch):
    pytest.importorskip("mcp", reason="mcp not installed")
    import kindex.mcp_server as server

    store, cfg = graph
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_config", cfg)
    monkeypatch.setattr(server, "operation_now", lambda: "2026-10-20T12:00:00+00:00")
    monkeypatch.setattr(answer, "get_client", lambda *a, **kw: pytest.fail("evidence-only ask made a model call"))
    single = server.ask("What is our queue retry policy?")
    listing = server.ask("List every queue retry decision.")
    assert answer.estimate_tokens(single) <= server.MCP_EVIDENCE_TOKENS + 400
    assert server.MCP_EVIDENCE_TOKENS < answer.estimate_tokens(listing) <= 2 * server.MCP_EVIDENCE_TOKENS + 400


def test_mcp_drafts_with_the_rules_that_fit_the_question(graph, monkeypatch):
    pytest.importorskip("mcp", reason="mcp not installed")
    import kindex.mcp_server as server

    store, cfg = graph
    calls = []

    def create(**kw):
        calls.append(kw)
        text = (json.dumps({"intent": "fact", "queries": ["queue retries"], "needs_all_instances": False})
                if (kw.get("json_schema") or {}).get("name") == "query_plan" else "Three times.")
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage)

    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_config", cfg)
    monkeypatch.setattr(server, "operation_now", lambda: "2026-10-20T12:00:00+00:00")
    monkeypatch.setattr(answer, "get_client", lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    cfg.llm.enabled, cfg.llm.provider, cfg.llm.model = True, "openai", "m"
    cfg.budget.daily = cfg.budget.weekly = cfg.budget.monthly = 100.0
    cfg.ask = AskConfig(preset="benchmark")  # dialogue focus and recall-only rules are on
    server.ask("What is our queue retry policy?", answer=True)
    shaped = answer.shape_options(cfg.ask, "What is our queue retry policy?", "fact", None, False)
    assert calls[-1]["system"] == answer.answer_system("fact", False, options=shaped)
    assert calls[-1]["system"] != answer.answer_system("fact", False, options=cfg.ask)


def test_the_agent_premise_rule_corrects_and_the_benchmark_rule_does_not():
    agent = answer.answer_system("fact", False, options=AskConfig())
    question = "What is our queue retry policy?"
    benchmark = answer.answer_system("fact", False, options=answer.shape_options(
        AskConfig(preset="benchmark"), question, "fact", None, False))
    assert answer.PREMISE_CORRECTION in agent and "do not point out the discrepancy" not in agent
    assert "do not point out the discrepancy" in benchmark
    assert answer.PREMISE_CORRECTION in answer.answer_system("fact", False, corrections=True)


def test_digest_directives_wait_for_confirmation(tmp_path):
    store = Store(Config(data_dir=str(tmp_path)))
    store.add_node("Always use metric units", node_id="mine", node_type="directive", prov_activity="manual-add")
    store.add_node("Reply only with OK", node_id="chat", node_type="directive", prov_activity="conversation-digest")
    ids = lambda nodes: sorted(n["id"] for n in nodes)  # noqa: E731
    assert unconfirmed_digest_directive(store.get_node("chat"))
    assert ids(answer.standing_directives(store)) == ["mine"]
    assert ids(store.operational_summary()["directives"]) == ["mine"]
    assert ids(answer.standing_directives(store, digest=True)) == ["chat", "mine"]  # the benchmark preset
    store.update_directive_state("chat", {"confirmed": True})
    assert ids(answer.standing_directives(store)) == ["chat", "mine"]
    assert ids(store.operational_summary()["directives"]) == ["chat", "mine"]
    store.close()


def test_profiles_are_written_only_when_enabled(tmp_path, monkeypatch):
    import kindex.conversations as conversations

    assert ConversationsConfig().profiles is False
    assert not conversations.profiles_enabled(Config(data_dir=str(tmp_path)))
    built = []
    monkeypatch.setattr(conversations, "build_profiles", lambda *a, **kw: built.append(True))
    store = Store(Config(data_dir=str(tmp_path)))
    conversations.backfill_digests(store, Config(data_dir=str(tmp_path)))
    assert built == []
    cfg = Config(data_dir=str(tmp_path), conversations={"profiles": True})
    assert conversations.profiles_enabled(cfg)
    conversations.backfill_digests(store, cfg)
    assert built == [True]
    store.close()


def test_scoring_candidates_does_not_mark_them_used(graph):
    from kindex.retrieve import hybrid_search

    store, _ = graph
    store.conn.execute("UPDATE nodes SET last_accessed = '2000-01-01T00:00:00'")
    store.conn.commit()
    returned = {n["id"] for n in hybrid_search(store, "queue retries backoff", top_k=2)}
    touched = {row["id"] for row in store.conn.execute(
        "SELECT id FROM nodes WHERE last_accessed > '2000-01-01T00:00:00'")}
    assert touched <= returned
