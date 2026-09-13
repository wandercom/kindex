"""Conversation override contract; independent Tester, Validator owns execution.

Persist settings through the user CLI and invoke the real hook_request boundary.
Only expensive review execution is replaced: the spy observes the Config actually
handed to supervisor_tick. No production source was read to author these tests.
"""
import json
import subprocess
import sys

import yaml

from test_supervisor_acceptance import WORK, sandbox  # noqa: F401


HOOK_PROBE = r'''
import json
import sys
from unittest.mock import patch

from kindex.config import load_config
from kindex.supervisor import hook_request

request = json.load(sys.stdin)
observed = []

def review_spy(store, config, scope, **kwargs):
    observed.append({
        'tick_interval': config.sim.tick_interval,
        'max_conversation_cost': config.sim.max_conversation_cost,
        'session_id': scope['session_id'],
        'agent': scope['agent'],
    })
    return {'ok': True, 'context': '', 'supervisor': {'state': 'skipped'}}

cfg = load_config(config_path=request['config'], project_path=request['project'])
with patch('kindex.supervisor.supervisor_tick', side_effect=review_spy):
    result = hook_request(
        {'session_id': request['session'], 'cwd': request['project'],
         'prompt': request['prompt']},
        request['adapter'], config=cfg, project_path=request['project'],
    )
assert result['ok'] is True, result
assert len(observed) == 1, (result, observed)
print('CONVERSATION_OVERRIDE_RESULT=' + json.dumps(observed[0]))
'''


def set_setting(box, key, value, *, client="codex", session=None):
    args = ["agent-config", "set", key, str(value), "--client", client]
    if session is not None:
        args += ["--scope", "instance", "--instance", session]
    args += ["--config", str(box.config)]
    result = box.cli(*args)
    assert result.returncode == 0, result.stdout + result.stderr


def configure_clients(box):
    box.configure(command=False)
    for client, cadence, budget in (("codex", 11, 0.75), ("claude", 7, 0.35)):
        set_setting(box, "sim.tick_interval", cadence, client=client)
        set_setting(box, "sim.max_conversation_cost", budget, client=client)


def observe_hook(box, session, *, adapter="codex"):
    result = subprocess.run(
        [sys.executable, "-c", HOOK_PROBE],
        input=json.dumps({"config": str(box.config), "project": str(box.project),
                          "session": session, "adapter": adapter, "prompt": WORK}),
        text=True, capture_output=True, cwd=box.project, env=box.env, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "CONVERSATION_OVERRIDE_RESULT="
    reports = [json.loads(line[len(prefix):]) for line in result.stdout.splitlines()
               if line.startswith(prefix)]
    assert len(reports) == 1, result.stdout
    assert box.requests() == [], "Settings probes must never invoke a provider"
    return reports[0]


def assert_settings(observed, *, session, cadence, budget, adapter="codex"):
    assert observed == {"tick_interval": cadence, "max_conversation_cost": budget,
                        "session_id": session, "agent": adapter}


def test_cli_instance_cadence_and_budget_reach_raw_session_hook(sandbox):
    """A raw payload session must resolve its CLI-canonicalized instance key."""
    configure_clients(sandbox)
    session = "raw-codex-conversation-a"
    set_setting(sandbox, "sim.tick_interval", 2, session=session)
    set_setting(sandbox, "sim.max_conversation_cost", 0.12, session=session)

    saved = yaml.safe_load(sandbox.config.read_text())
    instance = saved["agents"]["instances"][f"codex:{session}"]
    assert instance["client"] == "codex"
    assert instance["sim"]["tick_interval"] == 2
    assert instance["sim"]["max_conversation_cost"] == 0.12

    assert_settings(observe_hook(sandbox, session), session=session, cadence=2, budget=0.12)
    assert_settings(observe_hook(sandbox, "other-session"),
                    session="other-session", cadence=11, budget=0.75)
    assert_settings(observe_hook(sandbox, session, adapter="claude"),
                    session=session, cadence=7, budget=0.35, adapter="claude")


def test_live_instance_changes_affect_next_request_and_preserve_other_sessions(sandbox):
    """A running conversation can adjust cadence and budget independently."""
    configure_clients(sandbox)
    session = "continuing-codex-conversation"
    other = "concurrent-codex-conversation"
    assert_settings(observe_hook(sandbox, session), session=session, cadence=11, budget=0.75)

    set_setting(sandbox, "sim.tick_interval", 3, session=session)
    assert_settings(observe_hook(sandbox, session), session=session, cadence=3, budget=0.75)

    set_setting(sandbox, "sim.max_conversation_cost", 0.0, session=session)
    assert_settings(observe_hook(sandbox, session), session=session, cadence=3, budget=0.0)
    assert_settings(observe_hook(sandbox, other), session=other, cadence=11, budget=0.75)

    set_setting(sandbox, "sim.tick_interval", 1, session=session)
    set_setting(sandbox, "sim.max_conversation_cost", 0.25, session=session)
    assert_settings(observe_hook(sandbox, session), session=session, cadence=1, budget=0.25)
    assert_settings(observe_hook(sandbox, other), session=other, cadence=11, budget=0.75)
    assert_settings(observe_hook(sandbox, session, adapter="claude"),
                    session=session, cadence=7, budget=0.35, adapter="claude")
