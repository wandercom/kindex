"""Review fixes: one input cap for every entry point, verification and inventory
checks against what was shown, optional rereads, and chat transcript roles."""

import json
from types import SimpleNamespace

import pytest

from kindex import answer as answer_mod
from kindex.config import AskConfig, Config, LLMConfig
from kindex.store import Store


def _usage():
    return SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _reply(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=_usage())


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    s.add_node("user: My first kayak trip", content="user: My first kayak trip was on the river.",
               node_id="k1", node_type="document", prov_when="2024/01/05 (Fri) 18:30")
    yield s
    s.close()


def _config(tmp_path, **ask):
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"))
    cfg.ask = cfg.ask.model_copy(update=ask)
    return cfg


def test_input_cap_is_the_lower_nonzero_limit():
    assert answer_mod.input_cap(AskConfig()) == 20000  # the default (F25) cap
    assert answer_mod.input_cap(AskConfig(max_input_tokens=0)) is None
    assert answer_mod.input_cap(AskConfig(verify=True, verify_input_tokens=20000)) == 20000
    assert answer_mod.input_cap(AskConfig(verify=True, max_input_tokens=4000, verify_input_tokens=20000)) == 4000
    assert answer_mod.input_cap(AskConfig(verify=False, max_input_tokens=0, verify_input_tokens=9000)) is None


def test_verification_answers_within_the_lower_cap(store, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(answer_mod, "get_client", lambda *a, **kw: SimpleNamespace(messages=None))
    monkeypatch.setattr(answer_mod, "_answer", lambda *a, **kw: seen.update(cap=answer_mod._INPUT_LIMIT.get()))
    cfg = _config(tmp_path, verify=True, max_input_tokens=4000, verify_input_tokens=20000)
    assert answer_mod.answer_question(store, "When was my first kayak trip?", cfg) is None
    assert seen["cap"] == 4000


def test_a_failed_reread_keeps_the_first_draft(store, tmp_path, monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return _reply("The records don't say where the first kayak trip was.")
        raise RuntimeError("OpenAI API deadline exceeded")

    monkeypatch.setattr(answer_mod, "get_client", lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(answer_mod, "gather", lambda *a, **kw: [store.get_node("k1")])
    cfg = _config(tmp_path, reread=True, max_input_tokens=10000)
    result = answer_mod.answer_question(store, "Where was my first kayak trip?", cfg, as_of="2024-03-15")
    assert len(calls) >= 2  # the second reading was attempted
    assert result is not None and result.answer.startswith("The records don't say")


def test_verification_quotes_are_checked_against_the_rendered_lines():
    shown = "[2024/03/10] Source r1: user: I went kayaking yesterday [= 2024-03-09] with Sam."
    sources = {"r1": {"content": "user: I went kayaking yesterday with Sam. And a long unshown tail.",
                      "_rendered": shown}}

    def check(quote):
        return {"evidence": [{"ref": "r1", "include": True, "quote": quote, "item": "kayaking", "value": "1"}]}

    assert answer_mod._checked_rows(check("yesterday [= 2024-03-09] with Sam"), sources) is not None
    assert answer_mod._checked_rows(check("And a long unshown tail"), sources) is None


def test_named_roles_keep_the_user_turn_a_reply_answers():
    reply = " ".join(f"Point {i} is about pacing your work." for i in range(40))
    text = ("user (Ann): I just signed a contract with my first client today.\n"
            f"assistant (Bot): Congratulations! {reply} Send the invoice within a week. {reply}\n"
            "user (Ann): Thanks, that helps.")
    out = answer_mod.excerpt(text, {answer_mod._stem("invoice")}, exchange_context=True)
    assert "[...]" in out and "Send the invoice" in out  # excerpted, not returned whole
    assert "signed a contract with my first client" in out


def test_a_leading_system_turn_does_not_hide_a_chat():
    text = "system: You are a helpful assistant.\nuser: I bought a red kayak.\nassistant: Nice choice."
    assert answer_mod._is_chat(text)
    assert not answer_mod._is_chat("system: notes only\nno roles here")
    assert answer_mod._is_chat("user: hi\nassistant: hello") and not answer_mod._is_chat("Ann: hi\nBen: hello")


def test_an_inventory_keeps_a_lower_bound_answer():
    sources = {"r1": "user: I adopted Rex and Bella, and a few others I won't name."}
    rows = [{"item": name, "ref": "r1", "quote": "I adopted Rex and Bella", "reason": "adopted", "status": "included"}
            for name in ("Rex", "Bella")]
    for answer, complete in (("At least two dogs, with others unnamed.", True),
                             ("A minimum of two dogs, plus others.", True),
                             ("Two dogs named, and others I didn't list.", False)):
        raw = json.dumps({"answer": answer, "mode": "instances", "unit": "dogs", "complete": complete, "rows": rows})
        assert answer_mod.render_inventory(raw, sources) == answer
    exact = json.dumps({"answer": "Two dogs.", "mode": "instances", "unit": "dogs", "complete": True, "rows": rows})
    assert answer_mod.render_inventory(exact, sources).startswith("2 dogs")


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    pytest.importorskip("mcp", reason="mcp not installed")
    import kindex.mcp_server as server

    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    for i in range(40):
        s.add_node(f"user: kayak trip {i}", content=f"user: kayak trip {i} on the lake. " + "Paddle. " * 120,
                   node_id=f"k{i}", node_type="document", prov_when=f"2024/03/{i % 28 + 1:02d}")
    monkeypatch.setattr(server, "_store", s)
    monkeypatch.setattr(server, "_config", cfg)
    monkeypatch.setattr(server, "operation_now", lambda: "2024-03-30T12:00:00+00:00")
    cfg.llm.enabled, cfg.llm.provider, cfg.llm.model = True, "openai", "m"
    cfg.budget.daily = cfg.budget.weekly = cfg.budget.monthly = 100.0
    yield server, s, cfg
    s.close()


def test_mcp_answers_share_the_input_cap(mcp, monkeypatch):
    server, _, cfg = mcp
    calls = []

    def create(**kw):
        calls.append(kw)
        if (kw.get("json_schema") or {}).get("name") == "query_plan":
            return _reply(json.dumps({"intent": "aggregation", "queries": ["kayak"], "needs_all_instances": True}))
        return _reply("Forty trips.")

    monkeypatch.setattr(answer_mod, "get_client", lambda config, **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    cfg.ask = cfg.ask.model_copy(update={"max_input_tokens": 4000})
    out = server.ask("How many kayak trips did I take?", answer=True)
    assert out.startswith("Forty trips.")
    total = sum(answer_mod.estimate_tokens(c.get("system") or "") + answer_mod.estimate_tokens(c["messages"][0]["content"])
                + 64 + (answer_mod.estimate_tokens(json.dumps(c["json_schema"])) if c.get("json_schema") else 0)
                for c in calls)
    assert total <= 4000


def test_mcp_verification_uses_the_kin_ask_pipeline(mcp, monkeypatch):
    server, _, cfg = mcp
    monkeypatch.setattr(answer_mod, "get_client", lambda config, **kw: SimpleNamespace(messages=None))
    monkeypatch.setattr(answer_mod, "answer_question",
                        lambda store, question, config, ledger=None, **kw: SimpleNamespace(
                            answer="Checked: forty trips.", context="## Verification reading\nsources"))
    cfg.ask = cfg.ask.model_copy(update={"verify": True})
    out = server.ask("How many kayak trips did I take?", answer=True, graph="project")
    assert out.startswith("Checked: forty trips.") and "## Verification reading" in out


@pytest.mark.parametrize("answer", [
    "3 days: from January 1, 2024 till January 3, 2024, counting both days.",
    "3 days: from January 1, 2024 through January 3, 2024.",
    "3 days: from January 1, 2024 till January 3, 2024 (inclusively).",
])
def test_day_arithmetic_keeps_an_inclusive_count(answer):
    assert answer_mod.large_day_arithmetic("How many days did the festival last?", answer) == answer


def test_past_week_is_the_seven_days_ending_today_and_last_week_the_calendar_week():
    from datetime import datetime

    past = answer_mod.relative_recall_window("What did I cook in the past week?", "2024-03-15")
    last = answer_mod.relative_recall_window("What did I cook last week?", "2024-03-15")  # a Friday
    assert past == (datetime(2024, 3, 8), datetime(2024, 3, 15))
    assert last == (datetime(2024, 3, 4), datetime(2024, 3, 10))


def test_the_calendar_month_reading_ends_with_the_previous_month():
    from kindex.config import AskConfig

    rules = answer_mod.question_guidance("How many trips did I take last month?", "aggregation",
                                         as_of="2024-03-15", options=AskConfig(window_readings=True))
    assert "previous calendar month (2024-02-01 to 2024-02-29)" in rules and "this month so far" not in rules


def test_window_ranking_keeps_a_fact_that_happened_in_the_window():
    from datetime import datetime

    window = (datetime(2024, 3, 1), datetime(2024, 3, 10))
    late_report = {"id": "f", "prov_when": "2024-04-02", "extra": {"kind": "conversation-fact", "fact_date": "2024-03-05"}}
    outside = {"id": "o", "prov_when": "2024-01-02", "extra": {"kind": "conversation-fact", "fact_date": "2024-01-01"}}
    ranked = answer_mod.favour([outside, late_report], window, facts_first=False)
    assert [n["id"] for n in ranked] == ["f", "o"]


def test_mcp_verification_keeps_the_client_scope(mcp, monkeypatch):
    server, store, cfg = mcp
    seen = {}

    def fake_answer(store, question, config, ledger=None, *, node_filter=None, **kw):
        seen["filter"] = node_filter
        return SimpleNamespace(answer="Checked.", context="ctx")

    monkeypatch.setenv("KIN_CLIENT", "codex")
    monkeypatch.setattr(answer_mod, "get_client", lambda config, **kw: SimpleNamespace(messages=None))
    monkeypatch.setattr(answer_mod, "answer_question", fake_answer)
    cfg.ask = cfg.ask.model_copy(update={"verify": True})
    assert server.ask("How many kayak trips did I take?", answer=True, graph="project").startswith("Checked.")
    keep = seen["filter"]
    assert keep is not None and keep({"tags": []})
    assert not keep({"tags": ["client:claude-code"]})


def test_the_node_scope_reaches_directives_and_searches(store, tmp_path, monkeypatch):
    store.add_node("Always answer in metric units", node_id="d-mine", node_type="directive")
    store.add_node("Reply only in French", node_id="d-other", node_type="directive", tags=["client:other"])
    token = answer_mod._SCOPE.set(lambda n: "client:other" not in (n.get("tags") or []))
    try:
        ids = [d["id"] for d in answer_mod.standing_directives(store)]
    finally:
        answer_mod._SCOPE.reset(token)
    assert "d-mine" in ids and "d-other" not in ids
    assert {"d-mine", "d-other"} <= {d["id"] for d in answer_mod.standing_directives(store)}


def test_a_small_verification_cap_still_answers_with_a_plain_draft(store, tmp_path, monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw)
        return _reply("Your first kayak trip was on the river.")

    monkeypatch.setattr(answer_mod, "get_client", lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    # The benchmarked reading rules fill most of a 4,000-token verification cap.
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"),
                 ask={"preset": "benchmark"})
    cfg.ask = cfg.ask.model_copy(update={"verify": True, "verify_input_tokens": 4000})
    result = answer_mod.answer_question(store, "Where was my first kayak trip?", cfg, as_of="2024-03-15")
    assert result is not None and result.answer.startswith("Your first kayak trip")
    assert len(calls) == 1 and result.input_tokens <= 4000
    assert "first kayak trip was on the river" in calls[0]["messages"][0]["content"]


def test_a_failed_planner_keeps_the_count_classification(tmp_path):
    def create(**kw):
        raise RuntimeError("planner unavailable")

    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"))
    cfg.ask = cfg.ask.model_copy(update={"plan": True})
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    question = "How many kayak trips did I take?"
    intent, queries, needs_all = answer_mod.plan_question(question, cfg, client, None, "2024-03-15")
    assert (intent, needs_all) == answer_mod.classify_question(question) and queries == [question]
    assert needs_all or intent in answer_mod.COMPLETENESS_INTENTS


def test_the_node_scope_filters_originals_before_sessions_are_joined(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    try:
        for i, (who, text, tags) in enumerate([("Ann", "Ann: I painted a barn.", []),
                                               ("Ben", "Ben: SECRET from the other client.", ["client:other"]),
                                               ("Ann", "Ann: Then a lighthouse.", [])]):
            s.add_node("", text, node_id=f"c{i}", node_type="document", prov_activity="conversation-ingest",
                       prov_when="2024-01-10", tags=tags, extra={"conversation_id": "s1", "position": i})
        s.add_node("", "Ben: Hello Ann.", node_id="c9", node_type="document", prov_activity="conversation-ingest",
                   prov_when="2024-01-11", extra={"conversation_id": "s2", "position": 0})
        token = answer_mod._SCOPE.set(lambda n: "client:other" not in (n.get("tags") or []))
        try:
            sessions = answer_mod.dialogue_source_sessions(s)
            facts = answer_mod.every_fact(s, 10000)
            clock = answer_mod.answer_clock(s)
        finally:
            answer_mod._SCOPE.reset(token)
        joined = "\n".join(n["content"] for n in sessions)
        assert "barn" in joined and "lighthouse" in joined and "SECRET" not in joined
        assert facts == [] and clock is not None
    finally:
        s.close()
