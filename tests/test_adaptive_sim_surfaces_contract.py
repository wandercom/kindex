"""C4/C5/C8 capability/admission acceptance; see core module clause legend.
Validator executes. Mocks replace transport only; no admission/control mock.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from kindex import adaptive_sim as control, sim, supervisor
from kindex.budget import BudgetLedger
from kindex.sim import call_sim as real_call_sim
from test_adaptive_sim_contract import box, config_for, WORK, GOAL, NOTE, NOW, generate_advice, pickup


def cli(box, *arguments):
    """Actual argparse/CLI process, synthetic HOME/config, no live routing."""
    box.clock = time.time()
    config = box.root / "test-config.json"
    config.write_text(json.dumps(box.cfg.model_dump()))
    home = box.root / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / "state"), "KIN_HEALTH_DIR": str(home / "health"),
        "KIN_NO_SCHEDULER_WRITES": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run([sys.executable, "-m", "kindex.cli", "sim", *arguments,
        "--data-dir", str(box.cfg.data_dir), "--config", str(config), "--json"],
        cwd=home, env=env, capture_output=True, text=True, timeout=15)


def test_cli_backoff_status_clear_share_persisted_scope(box):
    """C8: missing command, independent CLI state, or wrong store routing fails."""
    result = cli(box, "backoff", "--duration-seconds", "600", "--conversation-id", "alpha")
    assert result.returncode == 0, result.stdout + result.stderr
    state = box.status()
    assert state["override"]["mode"] == "backoff"
    queried = cli(box, "control-status", "--conversation-id", "alpha")
    assert queried.returncode == 0, queried.stdout + queried.stderr
    assert json.loads(queried.stdout)["override"]["mode"] == "backoff"
    assert box.status("beta")["override"] is None
    cleared = cli(box, "clear-control", "--conversation-id", "alpha")
    assert cleared.returncode == 0, cleared.stdout + cleared.stderr
    assert box.status()["override"] is None


@pytest.mark.parametrize("arguments", [
    ["attention", "--duration-seconds", "60"],
    ["backoff", "--duration-seconds", "60", "--store", "--conversation-id", "alpha"],
    ["backoff", "--duration-seconds", "1.5", "--store"],
    ["attention", "--duration-seconds", "0", "--store"],
    ["clear-control"],
    ["clear-control", "--store", "--conversation-id", "alpha"],
])
def test_cli_invalid_or_ambiguous_scope_never_writes(box, arguments):
    """C1/C8: default-global mutations or coercion of invalid duration fails."""
    before = box.store.get_meta("sim.adaptive.control")
    result = cli(box, *arguments)
    assert result.returncode != 0
    assert box.store.get_meta("sim.adaptive.control") == before


def test_cli_attention_store_scope_is_supported_and_persistent(box):
    """C8: store attention parsed but not persisted fails."""
    result = cli(box, "attention", "--duration-seconds", "600", "--store")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.status(None)["override"]["mode"] == "attention"


@pytest.fixture
def mcp_module(box, monkeypatch):
    import kindex.mcp_server as module
    monkeypatch.setattr(module, "_store", box.store)
    monkeypatch.setattr(module, "_config", box.cfg)
    return module


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def test_mcp_capabilities_are_actually_registered(mcp_module):
    """C8: undecorated functions masquerading as MCP tools fail discovery."""
    names = {t.name for t in asyncio.run(mcp_module.mcp.list_tools())}
    assert {"sim_control_status", "sim_attention", "sim_backoff", "sim_clear_control", "sim_ack"} <= names


def test_mcp_controls_and_ack_use_real_selected_store(box, mcp_module):
    """C8/C7: thin adapters that report success without persisting fail."""
    mcp_module.sim_backoff(duration_seconds=600, conversation_id="alpha")
    assert box.status()["override"]["mode"] == "backoff"
    assert unpack(mcp_module.sim_control_status(conversation_id="alpha"))["override"]["mode"] == "backoff"
    mcp_module.sim_clear_control(conversation_id="alpha")
    assert box.status()["override"] is None
    mcp_module.sim_attention(duration_seconds=600, store_scope=True)
    assert box.status(None)["override"]["mode"] == "attention"
    receipt = generate_advice(box)
    assert pickup(box) is not None
    acknowledged = unpack(mcp_module.sim_ack(conversation_id="alpha", review_id=receipt["review_id"]))
    assert acknowledged["ok"] is True and acknowledged["state"] == "acknowledged"
    assert box.status()["receipts"][0]["state"] == "acknowledged"


@pytest.mark.parametrize("arguments", [{}, {"conversation_id": "alpha", "store_scope": True}, {"conversation_id": ""}])
def test_mcp_mutation_requires_exact_explicit_scope(box, mcp_module, arguments):
    """C1/C8: a default or ambiguous MCP scope must never mutate state."""
    before = box.store.get_meta("sim.adaptive.control")
    try:
        result = unpack(mcp_module.sim_backoff(duration_seconds=60, **arguments))
    except (ValueError, RuntimeError) as error:
        # Existing MCP _safe_output convention projects ValueError to RuntimeError
        # (tests/test_mcp_privacy.py::test_exception_projection_never_emits_raw_credential).
        diagnostic = str(error)
    else:
        assert isinstance(result, dict) and result.get("ok") is False
        diagnostic = result.get("reason", "")
    assert isinstance(diagnostic, str) and diagnostic.strip(), "C1/C8: rejected scope emits a diagnostic"
    assert box.store.get_meta("sim.adaptive.control") == before


def command_provider(box):
    script = box.root / "fake-provider.py"
    log = box.root / "fake-provider-calls"
    script.write_text("from pathlib import Path\nimport json,sys\n"
        "sys.stdin.read()\n"
        f"with Path({str(log)!r}).open('a') as f: f.write('called\\n')\n"
        "print(json.dumps({'rating':0.1,'note':'','basis':''}))\n")
    box.cfg.sim.command = shlex.join([sys.executable, str(script)])
    return log


def test_attention_cannot_override_real_money_admission_at_dispatch(box, monkeypatch):
    """C4/C5: a valid queued job cannot spend after the real ledger is exhausted."""
    monkeypatch.setattr(sim, "call_sim", real_call_sim)
    log = command_provider(box)
    assert box.enqueue()
    box.drain()
    assert log.read_text().splitlines() == ["called"], "C4 reachability: fake command traversed real admission"
    box.clock += 300
    assert box.enqueue()
    ledger = BudgetLedger(box.cfg.ledger_path, box.cfg.budget)
    ledger.record(100000, purpose="sim", conversation_id="alpha")
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600)
    box.drain()
    assert log.read_text().splitlines() == ["called"]
    assert box.status()["quiet_streak"] == 1
    assert box.status()["failures"] == 0


def test_attention_cannot_override_native_counts(box, monkeypatch, utc_calendar):
    """C4: attention cannot create native provider calls past existing limits."""
    from kindex import subscription_review as native
    monkeypatch.setattr(sim, "call_sim", real_call_sim)
    box.cfg.sim.command = ""
    box.cfg.sim.backend = "codex"
    box.cfg.sim.max_conversation_reviews = 1
    box.cfg.sim.max_daily_reviews = 1
    actual_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda command, *a, **kw:
        "/synthetic/native/" + command if command in {"codex", "tmux"} else actual_which(command, *a, **kw))
    calls = []
    def provider(cfg, session_id, prompt, workspace):
        calls.append(session_id)
        return {"status": "ok", "response": json.dumps({"rating": .1, "note": ""}),
            "session_id": session_id or "synthetic-native-session", "usage": {}}
    monkeypatch.setattr(native, "_run_provider", provider)
    assert box.enqueue()
    box.drain()
    assert len(calls) == 1, "C4 reachability: first review traversed real native admission"
    box.clock += 60
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600)
    box.enqueue("alpha")
    box.enqueue("beta")
    box.drain()
    assert len(calls) == 1
    assert box.status()["failures"] == 0


def test_worker_retains_provider_snapshot_while_live_control_is_rechecked(box):
    """C5: replacing admitted provider snapshot with live provider settings fails."""
    box.cfg.sim.command = "false # admitted-provider"
    assert box.enqueue()
    box.cfg.sim.command = "false # replacement-provider"
    box.drain()
    assert len(box.calls) == 1
    assert box.calls[0][2] == "false # admitted-provider"


def test_manual_cli_check_honors_backoff_without_provider_dispatch(box):
    """C8: the manual synchronous check must not bypass explicit backoff."""
    log = command_provider(box)
    # CLI uses real wall time, so create control through CLI as well.
    set_result = cli(box, "backoff", "--duration-seconds", "600", "--store")
    assert set_result.returncode == 0, set_result.stdout + set_result.stderr
    checked = cli(box, "check", "--text", WORK, "--project-path", str(box.root))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert not log.exists()


def test_supervisor_deferred_queue_does_not_request_respawn(box, monkeypatch):
    """C5: repeatedly requesting workers for a control-deferred queue fails."""
    scope = {"project_path": str(box.root), "session_id": "alpha", "agent": "codex"}
    conv = supervisor.session_key(scope)
    assert box.enqueue(conv)
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=600)
    spawns = []
    monkeypatch.setattr(sim, "spawn_background_drain", lambda *a, **kw: spawns.append(1) or False)
    for i in range(3):
        supervisor.supervisor_tick(box.store, box.cfg, scope, text=WORK + f" activity {i}",
            event_id=f"observed-{i}", goal=GOAL)
    assert box.calls == []
    assert spawns == []


def test_cli_ack_requires_emission_and_persists_exact_review(box):
    """C7/C8: CLI accepting generated or failing to acknowledge emitted advice fails."""
    box.clock = time.time()
    receipt = generate_advice(box)
    before = cli(box, "ack", "--conversation-id", "alpha", "--review-id", receipt["review_id"])
    assert box.status()["receipts"][0]["state"] == "generated"
    assert pickup(box) is not None
    result = cli(box, "ack", "--conversation-id", "alpha", "--review-id", receipt["review_id"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.status()["receipts"][0]["state"] == "acknowledged"


def test_attention_cannot_reenable_store_optout_before_worker_dispatch(box):
    """C4/C5: attention enabling opted-out provider work fails after valid enqueue."""
    assert box.enqueue()
    sim.set_sim_enabled(box.store, False)
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600)
    box.drain()
    assert box.calls == []
    assert sim.sim_effective_enabled(box.store, box.cfg) is False


def test_advice_ack_rejects_id_from_another_store(box):
    """C7: resolving receipts globally instead of exact selected store fails."""
    from kindex.store import Store
    receipt = generate_advice(box)
    assert pickup(box) is not None
    other = Store(config_for(box.root / "neighbor"))
    try:
        result = control.acknowledge_advice(other, conversation_id="alpha", review_id=receipt["review_id"])
        assert result["ok"] is False and result["reason"]
        assert box.status()["receipts"][0]["state"] == "emitted"
    finally:
        other.close()


def test_two_workers_allow_only_one_failed_probe_after_cooldown(box, monkeypatch):
    """C5/C6: ignoring worker ownership/live reopened circuit dispatches two probes."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from kindex.store import Store
    box.cfg.sim.adaptive.failure_threshold = 1
    box.kind = "llm_error"
    assert box.enqueue("initial-failure")
    box.drain()
    box.clock += 300
    assert box.enqueue("probe-alpha")
    box.enqueue("probe-beta")
    entered, release = Event(), Event()
    def blocked_provider(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError("synthetic probe release was not signaled")
        return box.provider(*args, **kwargs)
    monkeypatch.setattr(sim, "call_sim", blocked_provider)
    def worker():
        store = Store(box.cfg)
        try:
            return sim.drain_sim_queue(store, box.cfg, max_jobs=20)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker)
        assert entered.wait(5), "C5 reachability: first cooldown probe entered provider"
        second = executor.submit(worker)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert len(box.calls) == 2
    assert box.status()["circuit_until"] == box.clock + 300


def test_explicit_changed_goal_accelerates_supervisor_but_same_goal_does_not(box, monkeypatch):
    """C3: failing to derive goal_changed, or deriving urgency from ordinary text, fails."""
    scope = {"project_path": str(box.root), "session_id": "goal-session", "agent": "codex"}
    conv = supervisor.session_key(scope)
    monkeypatch.setattr(sim, "spawn_background_drain", lambda *a, **kw: False)
    supervisor.supervisor_tick(box.store, box.cfg, scope, text=WORK, event_id="first", goal=GOAL)
    box.drain()
    assert len(box.calls) == 1
    box.clock += 60
    supervisor.supervisor_tick(box.store, box.cfg, scope, text=WORK + " New observed patch.",
        event_id="same-goal", goal=GOAL)
    box.drain()
    assert len(box.calls) == 1
    supervisor.supervisor_tick(box.store, box.cfg, scope, text=WORK + " Another observed patch.",
        event_id="new-goal", goal="Repair invoice rounding and preserve settlement history")
    box.drain()
    assert len(box.calls) == 2
    assert box.status(conv)["quiet_streak"] == 2
