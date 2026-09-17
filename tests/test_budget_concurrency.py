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


def test_recovery_keeps_a_ledger_another_process_already_repaired(tmp_path, monkeypatch):
    path = tmp_path / "budget.yaml"
    BudgetLedger(path, LIMITS).record(0.3, purpose="repaired-elsewhere")
    real_read = BudgetLedger._read
    calls = []

    def first_read_fails(self):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("torn while this process read it")
        return real_read(self)

    monkeypatch.setattr(BudgetLedger, "_read", first_read_fails)
    ledger = BudgetLedger(path, LIMITS)
    assert [e["purpose"] for e in ledger.entries] == ["repaired-elsewhere"]
    assert not list(tmp_path.glob("budget.yaml.unreadable-*"))
    assert ledger.today_spend == pytest.approx(0.3)


def test_the_ledger_never_goes_missing_during_recovery(tmp_path, monkeypatch):
    path = tmp_path / "budget.yaml"
    path.write_text("entries: [unterminated\n")
    seen_during_write = []

    def failing_write(self, entries):
        seen_during_write.append(path.read_text())
        raise OSError("disk full")

    monkeypatch.setattr(BudgetLedger, "_write", failing_write)
    ledger = BudgetLedger(path, LIMITS)
    assert seen_during_write == ["entries: [unterminated\n"]
    assert not ledger.can_spend()
    ledger.record(0.01, purpose="extract")
    assert path.read_text() == "entries: [unterminated\n"
    assert len(seen_during_write) == 1


def test_a_ledger_that_cannot_be_read_or_kept_stops_spending_and_is_left_alone(tmp_path):
    path = tmp_path / "budget.yaml"
    path.mkdir()  # exists, but no read (or copy) of it succeeds
    (path / "marker").write_text("kept")
    ledger = BudgetLedger(path, LIMITS)
    assert not ledger.can_spend()
    ledger.record(0.01, purpose="extract")
    assert path.is_dir() and (path / "marker").read_text() == "kept"
    assert not ledger.can_spend()


def test_a_ledger_behind_a_closed_directory_is_not_read_as_empty(tmp_path):
    closed = tmp_path / "closed"
    closed.mkdir()
    path = closed / "budget.yaml"
    BudgetLedger(path, LIMITS).record(0.49, purpose="extract")
    closed.chmod(0)
    try:
        ledger = BudgetLedger(path, LIMITS)
        assert not ledger.can_spend()
    finally:
        closed.chmod(0o700)
    assert [e["purpose"] for e in BudgetLedger(path, LIMITS).entries] == ["extract"]


WITHOUT_FCNTL = """
import sys, types
calls = []
sys.modules["fcntl"] = None
sys.modules["msvcrt"] = types.SimpleNamespace(
    LK_LOCK=1, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append(mode))
from pathlib import Path
from kindex.budget import BudgetLedger
from kindex.config import BudgetConfig
ledger = BudgetLedger(Path(sys.argv[1]), BudgetConfig(daily=1, weekly=1, monthly=1))
ledger.record(0.01, purpose="extract")
assert calls == [1, 0], calls
print(len(BudgetLedger(Path(sys.argv[1]), BudgetConfig()).entries))
"""


def test_the_ledger_locks_where_fcntl_does_not_exist(tmp_path):
    env = {"PYTHONPATH": SRC, "HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", WITHOUT_FCNTL, str(tmp_path / "budget.yaml")],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_an_unkept_ledger_stops_spending_on_later_days_too(tmp_path, monkeypatch):
    import kindex.budget as budget
    path = tmp_path / "budget.yaml"
    path.mkdir()
    ledger = BudgetLedger(path, LIMITS)
    monkeypatch.setattr(budget, "_today", lambda: "2999-01-01")
    assert not ledger.can_spend()
