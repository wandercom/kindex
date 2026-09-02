"""Tests for the Sim supervisory check-in (enqueue -> drain -> pickup, with
threshold gating, staleness drops, and the runtime kill switch)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kindex.config import (
    AdvocateConfig,
    BudgetConfig,
    Config,
    LLMConfig,
    SimConfig,
)
from kindex.sim import (
    SIM_PENDING_META,
    SIM_QUEUE_META,
    clear_sim_override,
    drain_sim_queue,
    enqueue_sim_review,
    format_sim_injection,
    pop_pending_sim_injection,
    set_sim_enabled,
    sim_effective_enabled,
)
from kindex.store import Store


class _Messages:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=300, output_tokens=80,
                                  cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
        )


class _Client:
    def __init__(self, payload: dict):
        self.messages = _Messages(payload)


def _config(tmp_path, **sim_kw):
    sim = SimConfig(enabled=True, tick_interval=6, threshold=0.7,
                    max_stale_ticks=4, min_overlap=0.18, **sim_kw)
    return Config(
        data_dir=str(tmp_path),
        llm=LLMConfig(enabled=True),
        budget=BudgetConfig(daily=1.0, weekly=5.0, monthly=10.0),
        sim=sim,
    )


_WINDOW = (
    "User: I think every microservice should own its own database, no exceptions. "
    "Agent: agreed, let's split the shared schema into seven per-service stores. "
    "User: yes and we drop foreign keys entirely since services can't share them."
)


def _meta_list(store, key):
    raw = store.get_meta(key)
    return json.loads(raw) if raw else []


# ── grounding: Sim reviews WITH the graph's context, not blind ──────────────

def test_build_sim_grounding_surfaces_constraints_and_concepts(tmp_path):
    from kindex.sim import build_sim_grounding
    cfg = _config(tmp_path)  # grounding_chars defaults to 1500
    store = Store(cfg)
    store.add_node("Microservice database ownership", content="each service owns its own store",
                   node_type="concept", node_id="msdb")
    store.add_node("No foreign keys across service boundaries", node_type="constraint",
                   node_id="fk", extra={"trigger": "schema-change", "action": "warn"})
    g = build_sim_grounding(store, _WINDOW, cfg)
    assert g  # non-empty
    assert "[constraint:warn]" in g  # active constraint surfaced via operational_summary
    store.close()


def test_build_sim_grounding_disabled_returns_empty(tmp_path):
    from kindex.sim import build_sim_grounding
    cfg = _config(tmp_path, grounding_chars=0)
    store = Store(cfg)
    store.add_node("Some concept", content="x", node_type="concept",
                   node_id="c", extra={"action": "warn"})
    assert build_sim_grounding(store, _WINDOW, cfg) == ""
    store.close()


def test_build_sim_grounding_respects_char_budget(tmp_path):
    from kindex.sim import build_sim_grounding
    cfg = _config(tmp_path, grounding_chars=120)
    store = Store(cfg)
    for i in range(12):
        store.add_node(f"Microservice database concept {i}",
                       content="microservice database foreign keys " * 10,
                       node_type="concept", node_id=f"c{i}")
    g = build_sim_grounding(store, _WINDOW, cfg)
    assert len(g) <= 120 + 200  # capped (allow a single trailing line's overshoot)
    store.close()


def test_supervisor_prompt_includes_grounding_only_when_present():
    from kindex.sim import build_supervisor_prompt
    with_g = build_supervisor_prompt("window text", 1000,
                                     grounding="- [constraint:warn] No FKs across services")
    assert "WHAT KINDEX ALREADY KNOWS" in with_g and "No FKs across services" in with_g
    without_g = build_supervisor_prompt("window text", 1000)
    assert "WHAT KINDEX ALREADY KNOWS" not in without_g


def test_drain_feeds_grounded_prompt_to_sim(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    store.add_node("No foreign keys across service boundaries", node_type="constraint",
                   node_id="fk", extra={"trigger": "schema-change", "action": "warn"})
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)

    captured = {}

    class _CapMessages:
        def create(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps({"rating": 0.1, "note": "", "basis": ""}))],
                usage=SimpleNamespace(input_tokens=300, output_tokens=10,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0),
            )

    class _CapClient:
        messages = _CapMessages()

    drain_sim_queue(store, cfg, client=_CapClient())
    assert "WHAT KINDEX ALREADY KNOWS" in captured.get("prompt", "")
    assert "[constraint:warn]" in captured["prompt"]
    store.close()


def test_enqueue_gated_by_tick_interval(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    # tick 5 is not a multiple of interval 6 -> no enqueue
    assert enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=5) is False
    assert _meta_list(store, SIM_QUEUE_META) == []
    # tick 6 enqueues
    assert enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6) is True
    queue = _meta_list(store, SIM_QUEUE_META)
    assert len(queue) == 1 and queue[0]["conversation_id"] == "c1"
    assert queue[0]["fingerprint"]  # tail fingerprint captured
    store.close()


def test_enqueue_dedups_by_conversation(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    enqueue_sim_review(store, cfg, "c1", _WINDOW + " more", tick=12)
    queue = _meta_list(store, SIM_QUEUE_META)
    assert len(queue) == 1 and queue[0]["tick"] == 12  # fresher replaces staler
    store.close()


def test_drain_above_threshold_creates_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "Dropping all FKs is a direction worth re-examining.",
                      "basis": "user is committing to no-FK across services"})
    res = drain_sim_queue(store, cfg, client=client)
    assert res["status"] == "ok" and res["reviewed"] == 1 and res["flagged"] == 1
    assert _meta_list(store, SIM_QUEUE_META) == []  # queue drained
    pending = _meta_list(store, SIM_PENDING_META)
    assert len(pending) == 1 and pending[0]["rating"] == 0.85
    store.close()


def test_drain_below_threshold_no_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.3, "note": "minor nit", "basis": "x"})
    res = drain_sim_queue(store, cfg, client=client)
    assert res["reviewed"] == 1 and res["flagged"] == 0
    assert _meta_list(store, SIM_PENDING_META) == []
    store.close()


def test_drain_empty_note_no_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    # high rating but no note -> nothing to say, no inject
    client = _Client({"rating": 0.9, "note": "", "basis": ""})
    drain_sim_queue(store, cfg, client=client)
    assert _meta_list(store, SIM_PENDING_META) == []
    store.close()


def test_pickup_fresh_window_injects(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "Re-examine the no-FK call.", "basis": "b"})
    drain_sim_queue(store, cfg, client=client)
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=7)
    assert inj is not None and inj.message == "Re-examine the no-FK call."
    assert abs(inj.confidence - 0.85) < 1e-6
    # consumed — second pickup returns nothing
    assert pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=8) is None
    store.close()


def test_pickup_dropped_when_stale_by_ticks(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "n", "basis": "b"})
    drain_sim_queue(store, cfg, client=client)
    # tick 6 + max_stale_ticks 4 = 10; tick 11 is stale -> dropped, not shown
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=11)
    assert inj is None
    assert _meta_list(store, SIM_PENDING_META) == []  # consumed, never re-queued
    store.close()


def test_pickup_dropped_when_window_moved_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "n", "basis": "b"})
    drain_sim_queue(store, cfg, client=client)
    moved = "Completely different topic about frontend button colors and CSS spacing tokens."
    inj = pop_pending_sim_injection(store, cfg, "c1", moved, tick=7)
    assert inj is None  # tail overlap below floor
    store.close()


def test_runtime_kill_switch_overrides_config(tmp_path):
    cfg = _config(tmp_path)  # config enabled
    store = Store(cfg)
    assert sim_effective_enabled(store, cfg) is True
    set_sim_enabled(store, False)
    assert sim_effective_enabled(store, cfg) is False
    # disabled at runtime -> enqueue is a no-op even though config is on
    assert enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6) is False
    clear_sim_override(store)
    assert sim_effective_enabled(store, cfg) is True
    store.close()


def test_command_path_invokes_subprocess(tmp_path):
    payload = '{"rating": 0.9, "note": "from command", "basis": "b"}'
    cfg = _config(tmp_path, command=f"printf '{payload}'")
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    res = drain_sim_queue(store, cfg)  # no client; uses the shell command
    assert res["flagged"] == 1
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=7)
    assert inj is not None and inj.message == "from command"
    store.close()


def test_format_sim_injection_modes():
    from kindex.attention import AttentionInjection
    assert format_sim_injection(None) == []
    inj = AttentionInjection(id="sim:c1", title="Sim (supervisory)",
                             message="reconsider X", reason="because Y", confidence=0.8)
    # minimal — a single bare user-facing line, no chrome
    assert format_sim_injection(inj, display="minimal") == ["Sim: reconsider X"]
    # quiet (default) — agent-facing act-or-escalate directive, invisible to user
    quiet = format_sim_injection(inj, display="quiet")
    assert len(quiet) == 1
    assert "reconsider X" in quiet[0]
    assert "act on it yourself" in quiet[0].lower()
    assert "the user does NOT see this" in quiet[0]
    # full — labelled block with basis + advisory footer
    full = format_sim_injection(inj, display="full")
    assert full[0] == "KINDEX · SIM"
    assert any("because Y" in ln for ln in full)
    assert any("kin sim disable" in ln for ln in full)


def test_guidance_set_get_clear(tmp_path):
    from kindex.sim import clear_sim_guidance, get_sim_guidance, set_sim_guidance
    cfg = _config(tmp_path)
    store = Store(cfg)
    assert get_sim_guidance(store) == ""
    assert clear_sim_guidance(store) is False  # nothing to clear
    set_sim_guidance(store, "  take this to production-grade compliance  ")
    assert get_sim_guidance(store) == "take this to production-grade compliance"
    assert clear_sim_guidance(store) is True
    assert get_sim_guidance(store) == ""
    store.close()


def test_guidance_steers_prompt_without_lowering_bar(tmp_path):
    from kindex.sim import build_supervisor_prompt
    base = build_supervisor_prompt(_WINDOW, 12000)
    guided = build_supervisor_prompt(_WINDOW, 12000, guidance="demand telemetry and runbooks")
    assert "demand telemetry and runbooks" in guided
    assert "demand telemetry and runbooks" not in base
    # the bar-preservation instruction rides along with the guidance
    assert "do NOT lower the bar" in guided
    # default-silent discipline is present in both
    assert "say NOTHING" in base and "say NOTHING" in guided


def test_drain_applies_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    from kindex.sim import set_sim_guidance
    set_sim_guidance(store, "watch for missing alerting")

    captured = {}

    class _CapMessages:
        def create(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps({"rating": 0.2, "note": "", "basis": ""}))],
                usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0))

    class _CapClient:
        messages = _CapMessages()

    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    drain_sim_queue(store, cfg, client=_CapClient())
    assert "watch for missing alerting" in captured["prompt"]
    store.close()


# ── expanded lens: alignment + trajectory + architecture (Helland) ──────────

def test_supervisor_prompt_lenses_and_reversibility_tripwire():
    from kindex.sim import build_supervisor_prompt
    p = build_supervisor_prompt(_WINDOW, 12000)
    # graduated framing + the three note-lenses
    assert "CALIBRATE YOUR SCRUTINY" in p
    assert "DIRECTION" in p and "ALIGNMENT" in p and "TRAJECTORY" in p
    # the purpose is centered: catch competent execution of the wrong thing
    assert "COMPETENTLY DOING THE WRONG THING" in p
    assert "PAUSE AND CONSIDER" in p
    # architecture is a REVERSIBILITY trip-wire that ROUTES, not a verdict rendered inline
    assert "TRIP-WIRE" in p and "EXPENSIVE TO REVERSE" in p
    assert "Do NOT render the architectural verdict yourself" in p
    assert "Helland" in p  # named as the seat the escalation routes to
    # the per-tick guardrail is GONE (it moves upstream into the Advocate persona)
    assert "forcing function, not a generative principle" not in p
    # aggregation rule is specified, not left to vibe
    assert "SINGLE MOST CONSEQUENTIAL lens" in p and "do NOT average" in p
    # notes are considerations, not facts
    assert "CONSIDERATION" in p and "it may be wrong" in p
    # extended output schema
    assert '"stakes"' in p and '"escalate"' in p and '"dimension"' in p


def test_supervisor_prompt_intent_block_only_when_present():
    from kindex.sim import build_supervisor_prompt
    with_intent = build_supervisor_prompt(_WINDOW, 12000, intent="ship the release checklist")
    assert "WHAT THE USER SET OUT TO DO" in with_intent
    assert "ship the release checklist" in with_intent
    without = build_supervisor_prompt(_WINDOW, 12000)
    assert "WHAT THE USER SET OUT TO DO" not in without


def test_result_parsing_defaults_and_escalate_requires_high_stakes():
    from kindex.sim import _result_from_parsed
    # missing fields default safely (old clients that only return rating/note/basis)
    r = _result_from_parsed({"rating": 0.8, "note": "n", "basis": "b"})
    assert r.dimension == "" and r.stakes == "low" and r.escalate is False
    # escalate is gated on high stakes even if the model says escalate=true
    r2 = _result_from_parsed({"rating": 0.9, "note": "n", "basis": "b",
                              "stakes": "medium", "escalate": True})
    assert r2.escalate is False
    r3 = _result_from_parsed({"rating": 0.9, "note": "n", "basis": "b",
                              "stakes": "high", "escalate": True,
                              "escalate_reason": "two-owner ledger", "dimension": "architecture"})
    assert r3.escalate is True and r3.stakes == "high" and r3.dimension == "architecture"
    # a garbage stakes value falls back to low
    assert _result_from_parsed({"rating": 0.5, "note": "n", "stakes": "catastrophic"}).stakes == "low"


# ── Tier 0 triage: banter is skipped before it costs anything ───────────────

_BANTER = "User: hey! Agent: hi, how's it going? User: good thanks, nice weather today. Agent: haha yeah."


def test_looks_trivial_skips_banter_keeps_work():
    from kindex.sim import _looks_trivial
    assert _looks_trivial(_BANTER) is True
    assert _looks_trivial(_WINDOW) is False           # has work signals
    assert _looks_trivial("") is True                  # nothing to review
    # a long chatty tail with no work signal is still skipped
    assert _looks_trivial("thanks so much, that was great, really appreciate it. " * 20) is True


def test_enqueue_triages_banter_but_reviews_work(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    assert enqueue_sim_review(store, cfg, "c1", _BANTER, tick=6) is False  # skipped
    assert _meta_list(store, SIM_QUEUE_META) == []
    assert enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6) is True   # work reviewed
    store.close()


def test_triage_can_be_disabled(tmp_path):
    cfg = _config(tmp_path, triage_banter=False)
    store = Store(cfg)
    assert enqueue_sim_review(store, cfg, "c1", _BANTER, tick=6) is True   # no skip
    store.close()


def test_enqueue_captures_session_focus_as_intent(tmp_path):
    from kindex.sessions import start_tag
    cfg = _config(tmp_path)
    store = Store(cfg)
    start_tag(store, "release-work", focus="ship v0.35.0 and verify on PyPI")
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    job = _meta_list(store, SIM_QUEUE_META)[0]
    assert "ship v0.35.0" in job.get("intent", "")
    store.close()


def test_drain_persists_stakes_and_escalate(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "You're committing to no-FK across services.",
                      "basis": "b", "dimension": "reversibility", "stakes": "high",
                      "escalate": True, "escalate_reason": "cross-service integrity ownership"})
    drain_sim_queue(store, cfg, client=client)
    p = _meta_list(store, SIM_PENDING_META)[0]
    assert p["stakes"] == "high" and p["escalate"] is True
    assert p["dimension"] == "reversibility"
    assert "advocate" not in p  # advocate disabled by default -> recommend-only
    store.close()


def test_triage_never_skips_a_decisive_move(tmp_path):
    from kindex.sim import _looks_trivial
    # Terse approvals of expensive-to-reverse moves read like banter but must NOT skip.
    assert _looks_trivial("sure, ship it") is False
    assert _looks_trivial("yeah go ahead and drop the table") is False
    assert _looks_trivial("ok pay the invoice") is False
    cfg = _config(tmp_path)
    store = Store(cfg)
    assert enqueue_sim_review(store, cfg, "c1", "sure, ship it.", tick=6) is True
    store.close()


def test_focus_change_drops_stale_alignment_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    from kindex.sim import sim_counters
    # Intent at enqueue differs from intent at pickup -> the goal moved on -> drop,
    # rather than surface a note that fights the user's redirected work.
    calls = {"n": 0}

    def fake_intent(store):
        calls["n"] += 1
        return "ship v0.35.0 and verify on pypi" if calls["n"] == 1 else "fix the auth regression"

    monkeypatch.setattr("kindex.sim._capture_intent", fake_intent)
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "reconsider the no-FK call", "basis": "b",
                      "dimension": "alignment", "stakes": "medium"})
    drain_sim_queue(store, cfg, client=client)
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=7)
    assert inj is None  # dropped: enqueue focus != current focus
    assert sim_counters(store)["focus_stale_drops"] == 1
    store.close()


def test_status_surfaces_suppression_counters(tmp_path):
    from kindex.sim import sim_status
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", "hey, thanks, nice one!", tick=6)  # banter -> skip
    st = sim_status(store, cfg)
    assert "suppressed" in st
    assert st["suppressed"]["triaged_skips"] >= 1
    assert st["advocate"] == "recommend-only"
    store.close()


def test_high_stakes_recommendation_is_folded_into_message(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = _config(tmp_path)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    client = _Client({"rating": 0.85, "note": "Reconsider dropping foreign keys.",
                      "basis": "b", "stakes": "high", "escalate": True,
                      "escalate_reason": "referential integrity"})
    drain_sim_queue(store, cfg, client=client)
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=7)
    assert inj is not None
    assert "Reconsider dropping foreign keys." in inj.message
    assert "Advocate/Helland review" in inj.message
    assert "referential integrity" in inj.message
    store.close()


# ── Tier 2: gated Advocate escalation with an adversarial verify pass ────────

def _advocate_cfg(tmp_path, findings_json, **adv_kw):
    # max_cost defaults to 0.1 (below the test daily budget of 1.0) so the admission
    # gate passes; a test that wants to exercise the gate overrides it explicitly.
    adv_kw.setdefault("max_cost", 0.1)
    adv = AdvocateConfig(enabled=True, command=f"printf '%s' '{findings_json}'",
                         cooldown_ticks=30, **adv_kw)
    return _config(tmp_path, advocate=adv)


class _RoutingClient:
    """Routes create() by prompt: the verify pass ('keep') vs the Sim review.
    The verify pass now returns grounded {"i","quote"} entries."""
    def __init__(self, review_payload, keep_entries):
        self.review_payload = review_payload
        self.keep_entries = keep_entries
        outer = self

        class _M:
            def create(self, **kwargs):
                prompt = kwargs["messages"][0]["content"]
                payload = ({"keep": outer.keep_entries} if '"keep"' in prompt
                           else outer.review_payload)
                return SimpleNamespace(
                    content=[SimpleNamespace(text=json.dumps(payload))],
                    usage=SimpleNamespace(input_tokens=200, output_tokens=40,
                                          cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0))
        self.messages = _M()


def test_advocate_gate_closed_by_default(tmp_path):
    from kindex.sim import _advocate_gate_open
    cfg = _config(tmp_path)  # advocate disabled
    store = Store(cfg)
    assert _advocate_gate_open(store, cfg, "c1", 6) is False
    store.close()


def test_advocate_escalation_runs_verifies_and_folds_survivors(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    findings = ('[{"severity":"high","persona":"helland",'
                '"title":"Dropping all FKs abandons referential integrity"},'
                '{"severity":"high","persona":"sage","title":"generic hand-wave with no support"}]')
    cfg = _advocate_cfg(tmp_path, findings)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    review = {"rating": 0.9, "note": "You're locking in a no-foreign-key design.",
              "basis": "b", "dimension": "reversibility", "stakes": "high",
              "escalate": True, "escalate_reason": "cross-service integrity ownership"}
    # verify keeps finding 0 with a quote that actually appears in _WINDOW; finding 1
    # is not in keep, so it's dropped.
    client = _RoutingClient(review, keep_entries=[{"i": 0, "quote": "drop foreign keys entirely"}])
    drain_sim_queue(store, cfg, client=client)
    p = _meta_list(store, SIM_PENDING_META)[0]
    assert p.get("advocate") == ["[high · helland] Dropping all FKs abandons referential integrity"]
    inj = pop_pending_sim_injection(store, cfg, "c1", _WINDOW, tick=7)
    assert "Advocate (incl. Helland seat)" in inj.message
    assert "claims, not confirmed facts" in inj.message  # provenance/consideration framing
    assert "referential integrity" in inj.message
    assert "generic hand-wave" not in inj.message  # dropped by verify
    store.close()


def test_verify_drops_finding_whose_quote_is_not_in_window(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    from kindex.budget import BudgetLedger
    from kindex.sim import _verify_findings
    cfg = _config(tmp_path)
    store = Store(cfg)
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)

    class _KeepClient:
        def __init__(self, entries):
            outer_entries = entries

            class _M:
                def create(self, **kwargs):
                    return SimpleNamespace(
                        content=[SimpleNamespace(text=json.dumps({"keep": outer_entries}))],
                        usage=SimpleNamespace(input_tokens=50, output_tokens=20,
                                              cache_creation_input_tokens=0,
                                              cache_read_input_tokens=0))
            self.messages = _M()

    findings = ["real one", "fabricated one"]
    # First quote is in _WINDOW; second is a hallucinated span not present.
    client = _KeepClient([{"i": 0, "quote": "drop foreign keys entirely"},
                          {"i": 1, "quote": "this text is nowhere in the window at all"}])
    survivors = _verify_findings(cfg, ledger, _WINDOW, "", findings, "c1",
                                 client=client, store=store)
    assert survivors == ["real one"]  # unquotable finding dropped
    from kindex.sim import sim_counters
    assert sim_counters(store)["verify_drops"] == 1
    store.close()


def test_advocate_run_marks_cooldown_even_with_no_survivors(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    from kindex.sim import _advocate_gate_open, sim_counters
    # Advocate emits a finding, but verify keeps nothing (empty keep) -> no survivors.
    findings = '[{"severity":"high","persona":"helland","title":"some claim"}]'
    cfg = _advocate_cfg(tmp_path, findings)
    store = Store(cfg)
    enqueue_sim_review(store, cfg, "c1", _WINDOW, tick=6)
    review = {"rating": 0.9, "note": "n", "basis": "b", "dimension": "reversibility",
              "stakes": "high", "escalate": True, "escalate_reason": "r"}
    client = _RoutingClient(review, keep_entries=[])  # verify keeps nothing
    drain_sim_queue(store, cfg, client=client)
    p = _meta_list(store, SIM_PENDING_META)[0]
    assert "advocate" not in p  # nothing survived
    # ...but the cooldown advanced because Advocate actually RAN — no re-pay next tick.
    assert _advocate_gate_open(store, cfg, "c1", 7) is False
    assert sim_counters(store)["escalation_failures"] == 1
    store.close()


def test_advocate_admission_gate_blocks_when_budget_below_max_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    from kindex.budget import BudgetLedger
    from kindex.sim import maybe_escalate_to_advocate, _SimResult
    # max_cost above the entire daily budget -> admission gate refuses to start.
    cfg = _advocate_cfg(tmp_path, '[{"title":"x"}]', max_cost=100.0)
    store = Store(cfg)
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)  # daily=1.0 from _config
    res = _SimResult(rating=0.9, note="n", basis="b", stakes="high", escalate=True)
    ran, survivors = maybe_escalate_to_advocate(
        store, cfg, ledger, _WINDOW, "", "", res, "c1", client=None)
    assert ran is False and survivors == []
    store.close()


def test_advocate_cooldown_blocks_second_run(tmp_path, monkeypatch):
    from kindex.sim import _advocate_gate_open, _mark_advocate_run
    cfg = _advocate_cfg(tmp_path, "[]")
    store = Store(cfg)
    assert _advocate_gate_open(store, cfg, "c1", 6) is True
    _mark_advocate_run(store, "c1", 6)
    assert _advocate_gate_open(store, cfg, "c1", 12) is False  # within 30-tick cooldown
    assert _advocate_gate_open(store, cfg, "c1", 40) is True   # cooldown elapsed
    store.close()


def test_parse_advocate_real_review_schema_and_severity_filter():
    from kindex.sim import _parse_advocate_findings
    # The real `advocate review -o <file>` dump: findings NESTED under persona_reports.
    review = json.dumps({
        "target": "<stdin>",
        "persona_reports": [
            {"persona": "helland", "ok": True, "findings": [
                {"persona": "helland", "severity": "high", "dimension": "data_ownership",
                 "title": "events.jsonl has two writers, no named owner",
                 "detail": "d", "recommendation": "r"},
                {"persona": "helland", "severity": "info", "dimension": "concept",
                 "title": "stylistic aside — should be dropped"},
            ]},
            {"persona": "sage", "ok": True, "findings": [
                {"persona": "sage", "severity": "critical", "dimension": "blast_radius",
                 "title": "verification recursion has no stopping rule"},
            ]},
        ],
    })
    out = _parse_advocate_findings(review)
    assert any("events.jsonl has two writers" in s for s in out)
    assert any("verification recursion" in s for s in out)
    assert all("stylistic aside" not in s for s in out)  # info dropped
    assert any(s.startswith("[high · helland]") for s in out)  # severity+persona label


def test_parse_advocate_tolerates_leading_banner():
    from kindex.sim import _parse_advocate_findings
    # If the human report leaks onto stdout ahead of the JSON, still recover it.
    banner = "=" * 40 + "\nADVOCATE REVIEW: <stdin>\n" + "=" * 40 + "\n"
    payload = banner + json.dumps({"persona_reports": [
        {"persona": "adversarial", "findings": [
            {"persona": "adversarial", "severity": "high", "dimension": "wrong_assumptions",
             "title": "assumes the tick is monotonic"}]}]})
    out = _parse_advocate_findings(payload)
    assert out and "assumes the tick is monotonic" in out[0]


def test_parse_advocate_empty_and_garbage():
    from kindex.sim import _parse_advocate_findings
    assert _parse_advocate_findings("") == []
    assert _parse_advocate_findings("not json at all") == []
    assert _parse_advocate_findings('{"persona_reports": []}') == []


def test_verify_fails_closed_without_client(tmp_path):
    from kindex.budget import BudgetLedger
    from kindex.sim import _verify_findings
    cfg = _config(tmp_path)
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    # no client and no real LLM -> return nothing rather than surface unverified claims
    assert _verify_findings(cfg, ledger, _WINDOW, "", ["some finding"], "c1", client=None) == []
