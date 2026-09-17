"""Every process that spends is counted, and a ledger nobody can read stops
spending rather than forgetting what was spent."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.budget import BudgetLedger
from kindex.config import BudgetConfig

SRC = str(Path(__file__).resolve().parents[1] / "src")
LIMITS = BudgetConfig(daily=0.5, weekly=2.0, monthly=5.0)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    import kindex.config as config
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "_GLOBAL_PATHS", [tmp_path / "home" / "kin.yaml"])


def test_a_second_writer_does_not_erase_the_first(tmp_path):
    path = tmp_path / "budget.yaml"
    first, second = BudgetLedger(path, LIMITS), BudgetLedger(path, LIMITS)
    first.record(0.4, purpose="extract")
    second.record(0.4, purpose="attention")
    assert len(BudgetLedger(path, LIMITS).entries) == 2
    # A ledger built before the other write still sees it.
    assert first.today_spend == pytest.approx(0.8)
    assert not first.can_spend()


WRITER = """
import sys
from pathlib import Path
from kindex.budget import BudgetLedger
from kindex.config import BudgetConfig
ledger = BudgetLedger(Path(sys.argv[1]), BudgetConfig(daily=100, weekly=100, monthly=100))
for _ in range(25):
    ledger.record(0.001, purpose="worker")
"""


def test_concurrent_processes_are_all_counted(tmp_path):
    path = tmp_path / "budget.yaml"
    env = {"PYTHONPATH": SRC, "HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    workers = [subprocess.Popen([sys.executable, "-c", WRITER, str(path)], env=env)
               for _ in range(6)]
    assert all(worker.wait(timeout=60) == 0 for worker in workers)
    assert len(BudgetLedger(path, LIMITS).entries) == 150


def test_an_unreadable_ledger_is_kept_and_spending_stops_for_the_day(tmp_path):
    path = tmp_path / "budget.yaml"
    path.write_text("entries:\n  - date: [unterminated\n")
    ledger = BudgetLedger(path, LIMITS)
    assert not ledger.can_spend()
    kept = list(tmp_path.glob("budget.yaml.unreadable-*"))
    assert len(kept) == 1 and "unterminated" in kept[0].read_text()
    ledger.record(0.01, purpose="extract")
    assert [e["purpose"] for e in BudgetLedger(path, LIMITS).entries] == [
        "ledger-recovered", "extract"]
    events = [json.loads(line) for line in
              (tmp_path / "home" / ".kindex" / "degraded.jsonl").read_text().splitlines()]
    assert events[-1]["cmd"] == "budget"


def test_a_ledger_that_turns_unreadable_mid_run_recovers_without_waiting(tmp_path):
    path = tmp_path / "budget.yaml"
    ledger = BudgetLedger(path, LIMITS)
    ledger.record(0.01, purpose="extract")
    path.write_text(": not yaml : [")
    ledger.record(0.01, purpose="extract")
    assert [e["purpose"] for e in BudgetLedger(path, LIMITS).entries] == [
        "ledger-recovered", "extract"]
