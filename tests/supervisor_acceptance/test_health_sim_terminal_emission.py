"""Independent integration appendix for public Sim terminal health receipts.

Oracle: parent-ratified stale and superseded settlement behavior. Public API and
stub response/config shapes came from the specifically permitted test_sim fixtures.
No implementation source inspected and no tests executed by the Tester.
"""
import json
import subprocess
import sys

import pytest

from test_health import b, issues, session


SCENARIO = r'''
import json, sys
from types import SimpleNamespace
from kindex.config import AdvocateConfig, BudgetConfig, Config, LLMConfig, SimConfig
from kindex.sim import enqueue_sim_review, drain_sim_queue, pop_pending_sim_injection
from kindex.store import Store

p = json.load(sys.stdin)
window = (
    'User: Repair the invoice parser while preserving reconciliation. '
    'Assistant: I plan to delete reconciliation validation during the migration. '
    'Validation: parser_negative_case failed and settlement_roundtrip has not run.'
)
second_window = window + ' User: Preserve the same requirements and validate the second parser revision.'
cfg = Config(
    data_dir=p['data_dir'], llm=LLMConfig(enabled=True),
    budget=BudgetConfig(daily=1.0, weekly=5.0, monthly=10.0),
    sim=SimConfig(enabled=True, tick_interval=6, threshold=0.7,
                  max_stale_ticks=4, min_overlap=0.18, triage_banter=False,
                  advocate=AdvocateConfig(enabled=False),
                  command=""),
)

class Messages:
    def __init__(self, note):
        self.note, self.calls = note, 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps({
                'rating': 0.95, 'note': self.note,
                'basis': 'Reconciliation validation is required before completion',
                'stakes': 'low', 'dimension': 'alignment', 'escalate': False,
            }))],
            usage=SimpleNamespace(input_tokens=300, output_tokens=80,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

class Client:
    def __init__(self, note):
        self.messages = Messages(note)

store = Store(cfg)
try:
    if p['operation'] in ('initial_reviewed', 'initial_queued'):
        admitted = enqueue_sim_review(store, cfg, 'c1', window, tick=6, scope=p['scope'])
        if not admitted:
            raise AssertionError('Initial scoped public review was not admitted')
        if p['operation'] == 'initial_reviewed':
            client = Client('FIRST_ADVICE_CANARY')
            drain_sim_queue(store, cfg, client=client)
            if client.messages.calls != 1:
                raise AssertionError('Expected one actual synthetic provider request')
        print(json.dumps({'admitted': True}))
    elif p['operation'] == 'stale_pop':
        injection = pop_pending_sim_injection(store, cfg, 'c1', window, tick=11)
        print(json.dumps({'message': injection.message if injection is not None else None}))
    elif p['operation'] == 'replace_and_deliver':
        admitted = enqueue_sim_review(store, cfg, 'c1', second_window, tick=12, scope=p['scope'])
        if not admitted:
            raise AssertionError('Replacement scoped public review was not admitted')
        client = Client('SECOND_ADVICE_CANARY')
        drain_sim_queue(store, cfg, client=client)
        if client.messages.calls != 1:
            raise AssertionError('Only the replacement review should reach the synthetic provider')
        injection = pop_pending_sim_injection(store, cfg, 'c1', second_window, tick=13)
        print(json.dumps({'message': injection.message if injection is not None else None}))
finally:
    store.close()
'''


def scenario(b, operation):
    # Public stub-client fixture requires admission config; never inherit a live key.
    env = dict(b.env)
    env["ANTHROPIC_API_KEY"] = "synthetic-test-only"
    result = subprocess.run([sys.executable, "-c", SCENARIO],
                            input=json.dumps({"operation": operation,
                                              "data_dir": str(b.root / "sim-store"),
                                              "scope": b.scope}),
                            text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr  # Public fixture API must be reached.
    return json.loads(result.stdout)


def overdue_check(b):
    # Explicit recent activity keeps the synthetic future check inside active grace.
    b.record("activity", 1790, active=True, source="explicit")
    return b.check(offset=1801)


def test_real_stale_sim_removal_settles_health_without_delivery_or_value(b):
    """Ratified emission: stale public pop emits matching terminal settlement.

    Mutation: remove stale advice without a matching discarded receipt, or count
    discarded advice as delivered/useful. The initial overdue issue proves reachability.
    """
    b.seed_active()
    scenario(b, "initial_reviewed")
    assert issues(overdue_check(b), "undelivered_review")
    assert scenario(b, "stale_pop")["message"] is None
    settled = overdue_check(b)
    assert not issues(settled, "undelivered_review")
    summary = session(settled)
    assert summary["counts"].get("delivery", 0) == 0
    assert summary["last"].get("delivery") is None
    assert summary["value"] == "unverified"


@pytest.mark.parametrize("initial_operation", ["initial_reviewed", "initial_queued"])
def test_real_sim_replacement_settles_older_work_before_delivering_newest(b, initial_operation):
    """Ratified emission: replacing reviewed or still-queued work settles the old ID.

    Mutation: overwrite older pending/queued work without its health receipt,
    review both obsolete/new work, or falsely label delivered advice useful.
    """
    b.seed_active()
    scenario(b, initial_operation)
    assert issues(overdue_check(b), "undelivered_review")
    delivered = scenario(b, "replace_and_deliver")
    assert delivered["message"] == "SECOND_ADVICE_CANARY"
    settled = overdue_check(b)
    assert not issues(settled, "undelivered_review")
    summary = session(settled)
    assert summary["counts"].get("delivery", 0) == 1
    assert summary["last"].get("delivery") is not None
    assert summary["value"] == "unverified"
