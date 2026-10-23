"""Isolation, source admission and input-budget checks for the large-chat reading options."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from kindex import answer as a
from kindex.config import Config, LLMConfig
from kindex.store import Store

from ask_baseline import PRE_F25_ASK


OPTIONS = a.LARGE_READING_OPTIONS
QUESTIONS = (
    "What specific feedback did I receive on the sensor trial?",
    "Have I completed any sensor calibration trials?",
    "Can you summarize the sensor project?",
)


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=str(tmp_path)))
    yield value
    value.close()


def source(store, nid, text, *, conv=None, position=0, **extra):
    store.add_node("", text, node_id=nid, node_type="document",
                   prov_activity="conversation-ingest", prov_when="2024-01-10",
                   extra={"conversation_id": conv or nid, "position": position, **extra})
    return store.get_node(nid)


@pytest.mark.parametrize("option,question", zip(OPTIONS, QUESTIONS))
@pytest.mark.parametrize("scale,named,chat", [
    (1.0, False, True), (2.0, True, True), (2.0, False, False),
])
def test_options_and_dependencies_are_disabled_outside_large_chats(
        store, monkeypatch, option, question, scale, named, chat):
    source(store, "user", "user: I completed a sensor trial.")
    if chat:
        source(store, "assistant", "assistant: Noted.")
    monkeypatch.setattr(a, "memory_scale", lambda s: scale)
    monkeypatch.setattr(a, "named_memory", lambda s: named)
    cfg = Config()
    cfg.ask = cfg.ask.model_copy(update={**PRE_F25_ASK, option: True})
    normalized = a.large_memory_config(store, question, cfg)
    assert not any(getattr(normalized.ask, name) for name in OPTIONS)
    assert not normalized.ask.large_claim_witnesses
    assert not normalized.ask.large_summary_coverage


def test_scan_rejoins_continuations_but_not_gaps_and_ignores_foreign_denials(store):
    first = source(store, "start", "user: I have never calibrated ", conv="split", position=0)
    source(store, "tail", "the lidar sensor.\nassistant: Noted.", conv="split", position=1)
    source(store, "gap-start", "user: I have never calibrated ", conv="gap", position=0)
    source(store, "gap-tail", "the lidar sensor.", conv="gap", position=2)
    source(store, "foreign", "user: I calibrated the lidar sensor. I have never played badminton.")
    source(store, "example", "user: Can we calibrate the lidar sensor?\n"
           "assistant: You have never calibrated the lidar sensor.")
    source(store, "digest", "user: I have never calibrated the lidar sensor.",
           kind="conversation-summary")
    source(store, "expired", "user: I have never calibrated the lidar sensor.",
           expires="1999-01-01")
    retired = source(store, "retired", "user: I have never calibrated the lidar sensor.")
    store.update_node(retired["id"], status="archived")
    hits = a.large_claim_denial_hits(store, "Have I ever calibrated the lidar sensor?")
    assert [n["id"] for n in hits] == ["start"]
    assert hits[0]["content"] == "user: I have never calibrated the lidar sensor."
    assert store.get_node("start")["content"] == first["content"]


def test_scanned_denial_keeps_the_occurrence_from_the_same_chunk(store):
    node = source(store, "both", "user: I calibrated the lidar sensor.\nassistant: Noted.\n"
                  "user: I have never calibrated the lidar sensor.")
    denials = a.large_claim_denial_hits(store, "Have I calibrated the lidar sensor?")
    quoted = a.quoted_history_nodes(store, [
        ("calibrated lidar sensor", False, [node]),
        ("never calibrated lidar sensor", True, denials),
    ], 2000, literal_denials=True)
    text = "\n".join(n["content"] for n in quoted)
    assert "user: I calibrated the lidar sensor." in text
    assert "user: I have never calibrated the lidar sensor." in text


def test_scan_expiry_uses_real_time_not_the_archive_clock(store):
    # Expiry is when a memory stops being true now; an archive clock fixed in
    # the past must not bring an expired conversation back.
    source(store, "expired", "user: I have never calibrated the lidar sensor.", expires="2024-02-01")
    source(store, "current", "user: I have never calibrated the radar sensor.", expires="2999-01-01")
    token = a._CLOCK.set(datetime(2024, 1, 20))
    try:
        hits = [n["id"] for n in a.large_claim_denial_hits(store, "Have I calibrated the lidar sensor?")]
        assert "expired" not in hits and "current" in hits
    finally:
        a._CLOCK.reset(token)


def test_summary_retains_a_complete_method_at_the_existing_cap():
    text = " ".join(
        f"The user reported project sensor calibration progress field{i}a field{i}b field{i}c."
        for i in range(80))
    method = "The assistant explained sensor calibration using a reference standard."
    text += " " + method
    node = {"id": "summary", "content": text, "prov_when": "2024-01-10",
            "extra": {"kind": "conversation-summary", "conversation_id": "trial"}}
    terms = a.query_terms("project sensor calibration progress")
    baseline = a.summary_coverage_nodes([node], terms)
    assert a.summary_coverage_nodes([node], terms, methods_first=False) == baseline
    result = a.summary_coverage_nodes([node], terms, methods_first=True)[0]
    assert method in result["content"]
    assert a.estimate_tokens(a.node_text(result)) <= 320
    assert result["id"] == node["id"] and result["extra"] == node["extra"]
    assert node["content"] == text


def test_scan_adds_a_hidden_denial_in_the_existing_answer_call(store, monkeypatch):
    occurrence = source(store, "occurrence",
                        "user: I completed three sensor calibration trials.\nassistant: Noted.")
    source(store, "denial", "user: I have never completed any ", conv="hidden", position=0)
    source(store, "denial-tail", "sensor calibration trials.\nassistant: Noted.",
           conv="hidden", position=1)
    monkeypatch.setattr(a, "memory_scale", lambda s: 2.0)
    monkeypatch.setattr(a, "named_memory", lambda s: False)
    monkeypatch.setattr(a, "gather", lambda *args, **kwargs: [occurrence])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text="Both claims are recorded.")])

    monkeypatch.setattr(a, "get_client",
                        lambda *args, **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)))
    cfg = Config(llm=LLMConfig(enabled=True, provider="openai", model="gpt-6-luna"))
    cfg.ask = cfg.ask.model_copy(update={
        "large_claim_witnesses": True, "large_claim_scan": True,
        "fixed_clock": True, "max_input_tokens": 7000,
    })
    result = a.answer_question(store, QUESTIONS[1], cfg, as_of="2024-03-01")
    assert result is not None and result.calls == len(calls) == 1
    assert result.input_tokens <= 7000
    assert "completed three sensor calibration trials" in result.context
    assert "never completed any sensor calibration trials" in result.context


def test_small_chat_requests_replay_identically_with_options_enabled(store, monkeypatch):
    occurrence = source(store, "occurrence",
                        "user: I completed a sensor trial.\nassistant: Noted.")
    monkeypatch.setattr(a, "gather", lambda *args, **kwargs: [occurrence])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text="Recorded.")])

    monkeypatch.setattr(a, "get_client",
                        lambda *args, **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create)))
    cfg = Config(llm=LLMConfig(enabled=True, provider="openai", model="gpt-6-luna"))
    cfg.ask = cfg.ask.model_copy(update={"fixed_clock": True, "max_input_tokens": 7000})
    enabled = cfg.model_copy(update={"ask": cfg.ask.model_copy(update=dict.fromkeys(OPTIONS, True))})
    for question in QUESTIONS:
        a.answer_question(store, question, cfg, as_of="2024-03-01")
        baseline = calls[-1]
        a.answer_question(store, question, enabled, as_of="2024-03-01")
        assert calls[-1] == baseline
