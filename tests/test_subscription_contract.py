"""Independent subscription reviewer acceptance tests, contract A1--A6.

Validator executes this suite; transport below performs no inference. The sole
new integration seam was agreed before implementation: _run_provider(config,
session_id, prompt, workspace). Assertions target admission and observable
results, rather than transport command spelling.
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from kindex.agent_settings import apply_agent_overrides, agent_settings_summary
from kindex.budget import BudgetLedger
from kindex.config import Config, SimConfig
from kindex import sim, supervisor, subscription_review as native
from kindex.store import Store


WORK = "Implement session isolation and durable review accounting with regression checks."
NOTE = "Check the session boundary before changing the persistent accounting schema."
GOOD = json.dumps({"rating": 0.91, "note": NOTE})


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    for key in list(os.environ):
        if any(word in key for word in ("API_KEY", "TOKEN", "SECRET")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("KINDEX_REVIEW_WORKER", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.setattr("kindex.llm.get_client", Mock(return_value=None))
    monkeypatch.setattr(supervisor, "record_health", lambda *a, **kw: None)
    monkeypatch.setattr(sim, "spawn_background_drain", lambda *a, **kw: False)


def config(root, **changes):
    return Config(data_dir=str(root), sim={
        "enabled": True, "backend": "codex", "tick_interval": 1,
        "triage_banter": False, "drain_on_tick": False, "grounding_chars": 0,
        "max_conversation_reviews": 5, "max_daily_reviews": 10,
        "agent_timeout": 2, **changes,
    })


@pytest.fixture
def transport(monkeypatch):
    calls = []
    real_which = shutil.which

    def fake_which(command, *args, **kwargs):
        if command in {"tmux", "codex", "claude", "agy"}:
            return "/test-native-bin/" + command
        return real_which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)

    def fake(cfg, session_id, prompt, workspace):
        calls.append({"config": cfg.model_dump(), "session_id": session_id,
                      "prompt": prompt, "workspace": str(workspace),
                      "allowance": native.allowance_status(cfg, fake.conversation)})
        return {"status": "ok", "response": GOOD, "session_id": session_id or str(uuid4()),
                "usage": {"input_tokens": 17, "output_tokens": 9}}

    fake.conversation = "alpha"
    monkeypatch.setattr(native, "_run_provider", fake)
    return SimpleNamespace(calls=calls, fake=fake)


@contextmanager
def opened_store(cfg):
    store = Store(cfg)
    try:
        yield store
    finally:
        store.close()


def totals(cfg, conversation):
    value = native.allowance_status(cfg, conversation)
    assert isinstance(value, dict), "A3: allowance status must be structured"
    for scope in ("conversation", "day"):
        assert scope in value, "A3: conversation and project/day counters are both observable"
        assert {"used", "limit", "remaining"} <= value[scope].keys()
    return value


def low_notice(text):
    value = text.lower()
    return "low" in value and any(word in value for word in ("budget", "allowance", "review"))


def scope(root, session="alpha"):
    return {"session_id": session, "agent": "codex", "project_path": str(root.resolve())}


def test_a1_package_defaults_and_subscription_controls():
    value = SimConfig().model_dump()
    expected = {"backend": "api", "max_conversation_reviews": 100,
                "max_daily_reviews": 500, "agent_timeout": 90,
                "budget_warning_fraction": 0.8}
    assert {key: value.get(key) for key in expected} == expected
    assert {"agent_model", "agent_effort"} <= value.keys()


@pytest.mark.parametrize("field,value", [
    ("backend", "automatic"), ("agent_effort", "unbounded"),
    ("max_conversation_reviews", -1), ("max_daily_reviews", -1),
    ("agent_timeout", 0), ("budget_warning_fraction", -0.01),
    ("budget_warning_fraction", 1.01), ("budget_warning_fraction", float("nan")),
])
def test_a1_invalid_controls_rejected_in_config_and_overrides(field, value):
    for payload in ({"sim": {field: value}},
                    {"agents": {"instances": {"codex:alpha": {
                        "client": "codex", "sim": {field: value}}}}}):
        rejected = False
        try:
            cfg = Config(**payload)
            apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
        except (TypeError, ValueError):
            rejected = True
        assert rejected, f"A1: invalid {field} must not enter an effective configuration"


def test_a1_live_overrides_are_scoped_and_prior_effective_configuration_is_pinned(tmp_path):
    controls = {"backend": "claude", "agent_model": "review-model",
                "agent_effort": "high", "max_conversation_reviews": 13,
                "max_daily_reviews": 37, "agent_timeout": 45,
                "budget_warning_fraction": 0.65}
    cfg = config(tmp_path)
    raw = cfg.model_dump()
    raw["agents"] = {"clients": {"codex": {"sim": {"backend": "antigravity"}}},
                     "instances": {"codex:alpha": {"client": "codex", "sim": controls}}}
    cfg = Config(**raw)
    admitted = apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
    assert {key: admitted.sim.model_dump().get(key) for key in controls} == controls
    report = agent_settings_summary(cfg, client="codex", instance_key="codex:alpha")
    assert {"sim." + key for key in controls} <= set(report["allowed_keys"])
    assert {key: report["effective"]["sim"].get(key) for key in controls} == controls
    assert apply_agent_overrides(cfg, client="codex", instance_key="codex:beta").sim.backend == "antigravity"
    cfg.agents.instances["codex:alpha"].sim["backend"] = "codex"
    next_work = apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
    assert next_work.sim.backend == "codex"
    assert admitted.sim.backend == "claude", "A6: admitted configuration must remain pinned"


@pytest.mark.parametrize("backend", ["antigravity", "codex", "claude"])
def test_a2_subscription_routes_with_exhausted_api_allowance_without_touching_dollars(tmp_path, monkeypatch, backend):
    cfg = config(tmp_path, backend=backend, max_review_cost=0, max_conversation_cost=0)
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    ledger.record(1000, purpose=sim.SIM_PURPOSE, conversation_id="alpha")
    before = cfg.ledger_path.read_bytes()
    provider = Mock(return_value={"status": "ok", "response": GOOD,
                                 "session_id": "native-alpha", "usage": {"output_tokens": 9}})
    monkeypatch.setattr(native, "run_review", provider)
    client = SimpleNamespace(messages=SimpleNamespace(create=Mock()))
    result, accounting = sim.call_sim(cfg, ledger, WORK, "alpha", client=client)
    assert result is not None and result.note == NOTE
    assert accounting["status"] == "ok"
    assert provider.call_count == 1 and provider.call_args.args[1] == "alpha"
    assert WORK in provider.call_args.args[2]
    assert client.messages.create.call_count == 0
    assert cfg.ledger_path.read_bytes() == before, "A2: native usage never changes API spend"


@pytest.mark.parametrize("payload,status", [
    ({"rating": float("nan"), "note": "bad"}, "ok"),
    ({"rating": 1.01, "note": "bad"}, "ok"),
    ({"rating": 0.8, "note": ["bad"]}, "ok"),
    ({"rating": 0.8, "note": "looks valid"}, "failed"),
])
def test_a5_malformed_or_unsuccessful_receipts_are_not_advisories(tmp_path, monkeypatch, payload, status):
    cfg = config(tmp_path)
    monkeypatch.setattr(native, "run_review", lambda *a: {
        "status": status, "response": json.dumps(payload), "session_id": "native-alpha", "usage": {}})
    result, accounting = sim.call_sim(cfg, BudgetLedger(cfg.ledger_path, cfg.budget), WORK, "alpha")
    assert result is None
    assert accounting["status"] != "ok"


def test_a3_reservation_precedes_transport_and_failure_remains_counted(tmp_path, monkeypatch, transport):
    cfg = config(tmp_path)
    assert native.run_review(cfg, "alpha", WORK)["status"] == "ok"
    assert transport.calls[0]["allowance"]["conversation"]["used"] == 1
    assert transport.calls[0]["allowance"]["day"]["used"] == 1

    def fail(*args):
        raise OSError("synthetic interrupted native process")

    monkeypatch.setattr(native, "_run_provider", fail)
    result = native.run_review(cfg, "alpha", WORK)
    assert result["status"] != "ok"
    reloaded = Config(**cfg.model_dump())
    observed = totals(reloaded, "alpha")
    assert observed["conversation"]["used"] == observed["day"]["used"] == 2


def test_a3_conversation_and_project_daily_limits_span_providers(tmp_path, transport):
    cfg = config(tmp_path, max_conversation_reviews=2, max_daily_reviews=3)
    assert native.run_review(cfg, "alpha", WORK)["status"] == "ok"
    cfg.sim.backend = "claude"
    assert native.run_review(cfg, "alpha", WORK)["status"] == "ok"
    cfg.sim.backend = "antigravity"
    assert native.preflight(cfg, "alpha") is not None
    assert native.run_review(cfg, "alpha", WORK)["status"] != "ok"
    assert len(transport.calls) == 2
    transport.fake.conversation = "beta"
    assert native.run_review(cfg, "beta", WORK)["status"] == "ok"
    assert native.preflight(cfg, "gamma") is not None
    assert native.run_review(cfg, "gamma", WORK)["status"] != "ok"
    assert len(transport.calls) == 3
    observed = totals(cfg, "beta")
    assert observed["conversation"] == {"used": 1, "limit": 2, "remaining": 1}
    assert observed["day"] == {"used": 3, "limit": 3, "remaining": 0}
    other = config(tmp_path / "other-project", max_conversation_reviews=2, max_daily_reviews=3)
    assert native.preflight(other, "alpha") is None
    assert totals(other, "alpha")["day"]["used"] == 0


def test_a3_live_quota_changes_do_not_reset_consumption(tmp_path, transport):
    cfg = config(tmp_path, max_conversation_reviews=2)
    native.run_review(cfg, "alpha", WORK)
    cfg.sim.max_conversation_reviews = 1
    assert native.preflight(cfg, "alpha") is not None
    cfg.sim.max_conversation_reviews = 4
    assert native.preflight(cfg, "alpha") is None
    observed = totals(cfg, "alpha")
    assert observed["conversation"] == {"used": 1, "limit": 4, "remaining": 3}
    assert native.run_review(cfg, "alpha", WORK)["status"] == "ok"
    assert totals(cfg, "alpha")["conversation"]["used"] == 2


def test_a4_exact_native_session_survives_module_reload_and_is_scope_isolated(tmp_path, monkeypatch, transport):
    cfg = config(tmp_path)
    first = native.run_review(cfg, "alpha", WORK)
    assert first["status"] == "ok" and isinstance(first.get("session_id"), str) and first["session_id"]
    importlib.reload(native)
    monkeypatch.setattr(native, "_run_provider", transport.fake)
    again = native.run_review(Config(**cfg.model_dump()), "alpha", WORK)
    assert again["session_id"] == first["session_id"] == transport.calls[-1]["session_id"]
    beta = native.run_review(cfg, "beta", WORK)
    cfg.sim.backend = "claude"
    claude = native.run_review(cfg, "alpha", WORK)
    other = native.run_review(config(tmp_path / "other"), "alpha", WORK)
    assert len({first["session_id"], beta["session_id"], claude["session_id"], other["session_id"]}) == 4
    cfg.sim.backend = "codex"
    assert native.run_review(cfg, "alpha", WORK)["session_id"] == first["session_id"]


def test_a5_mismatched_resume_identity_is_rejected_and_not_adopted(tmp_path, monkeypatch, transport):
    cfg = config(tmp_path)
    first = native.run_review(cfg, "alpha", WORK)
    assert first["status"] == "ok"
    monkeypatch.setattr(native, "_run_provider", lambda *a: {
        "status": "ok", "response": GOOD, "session_id": "unrelated-native-session", "usage": {}})
    assert native.run_review(cfg, "alpha", WORK)["status"] != "ok"
    monkeypatch.setattr(native, "_run_provider", transport.fake)
    assert native.run_review(cfg, "alpha", WORK)["session_id"] == first["session_id"]
    assert totals(cfg, "alpha")["conversation"]["used"] == 3


@pytest.mark.parametrize("adapter", ["codex", "claude", "antigravity"])
def test_a6_recursive_worker_is_rejected_before_storage_or_scheduling(tmp_path, monkeypatch, adapter):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    cfg = config(tmp_path / "unopened-store")
    monkeypatch.setenv("KINDEX_REVIEW_WORKER", "1")
    store_spy = Mock(side_effect=AssertionError("worker opened storage"))
    tick_spy = Mock(side_effect=AssertionError("worker scheduled review"))
    monkeypatch.setattr("kindex.store.Store", store_spy)
    monkeypatch.setattr(supervisor, "supervisor_tick", tick_spy)
    payload = {"session_id": "alpha", "cwd": str(tmp_path), "prompt": WORK}
    if adapter == "antigravity":
        payload["workspacePaths"] = [str(tmp_path)]
    result = supervisor.hook_request(payload, adapter, config=cfg, project_path=str(tmp_path))
    assert result["context"] == ""
    assert store_spy.call_count == tick_spy.call_count == 0
    assert not cfg.data_path.exists()


def test_a6_subscription_warning_coexists_with_advisory_deduplicates_and_rearms(tmp_path, transport):
    cfg = config(tmp_path / "data", max_conversation_reviews=5, max_daily_reviews=100)
    sc = scope(tmp_path)
    conv = supervisor.session_key(sc)
    transport.fake.conversation = conv
    for _ in range(3):
        assert native.run_review(cfg, conv, WORK)["status"] == "ok"
    with opened_store(cfg) as store:
        assert sim.enqueue_sim_review(store, cfg, conv, WORK, tick=1, intent=WORK, scope=sc)
        assert sim.drain_sim_queue(store, cfg)["reviewed"] == 1
        result = supervisor.supervisor_tick(store, cfg, sc, text=WORK, event_id="first", goal=WORK)
        assert NOTE in result["context"]
        assert low_notice(result["context"]), "A6: advisory must not suppress allowance warning"
        assert "allowance" in result["supervisor"], "A6: hook diagnostics expose allowance"
        second = supervisor.supervisor_tick(store, cfg, sc, text=WORK, event_id="second", goal=WORK)
        assert not low_notice(second["context"])
        cfg.sim.max_conversation_reviews = 10
        raised = supervisor.supervisor_tick(store, cfg, sc, text=WORK, event_id="raised", goal=WORK)
        assert not low_notice(raised["context"])
        for _ in range(4):
            assert native.run_review(cfg, conv, WORK)["status"] == "ok"
        spent = supervisor.supervisor_tick(store, cfg, sc, text=WORK, event_id="spent", goal=WORK)
        assert low_notice(spent["context"]), "A6: a new crossing after raising budget warns again"


def test_a6_api_warning_coexists_with_advisory(tmp_path, monkeypatch):
    cfg = config(tmp_path / "data", backend="api", max_conversation_cost=1)
    cfg.budget.daily = 10
    sc = scope(tmp_path)
    conv = supervisor.session_key(sc)
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    ledger.record(0.8, purpose=sim.SIM_PURPOSE, conversation_id=conv)
    response = SimpleNamespace(content=[SimpleNamespace(text=GOOD)], usage=SimpleNamespace(
        input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
    with opened_store(cfg) as store:
        assert sim.enqueue_sim_review(store, cfg, conv, WORK, tick=1, intent=WORK, scope=sc)
        assert sim.drain_sim_queue(store, cfg, client=client, ledger=ledger)["reviewed"] == 1
        result = supervisor.supervisor_tick(store, cfg, sc, text=WORK, event_id="api", goal=WORK)
        assert NOTE in result["context"]
        assert low_notice(result["context"])


def test_a2_subscription_high_stakes_does_not_launch_paid_advocate(tmp_path, monkeypatch):
    cfg = config(tmp_path, advocate={"enabled": True, "command": "/bin/cat"})
    monkeypatch.setattr(native, "run_review", lambda *a: {
        "status": "ok", "response": json.dumps({"rating": 0.95, "note": NOTE,
            "stakes": "high", "escalate": True}), "session_id": "native-alpha", "usage": {}})
    escalation = Mock(return_value=(False, []))
    monkeypatch.setattr(sim, "maybe_escalate_to_advocate", escalation)
    with opened_store(cfg) as store:
        assert sim.enqueue_sim_review(store, cfg, "alpha", WORK, tick=1, intent=WORK)
        result = sim.drain_sim_queue(store, cfg)
        assert result["reviewed"] == 1
        assert escalation.call_count == 0, "A2: subscription review must not open paid escalation"
