"""A reminder action runs when, and as often as, it should.

- A sweep acted on a snapshot minutes old, so an action finished by a
  manual exec ran again, and a reminder cancelled mid-run was reopened.
- A failing action retried all day; a claude run was capped at five turns
  (every longer task failed) instead of by its budget.
- A runner waited for every background descendant, and one undecodable
  byte turned a finished run into a failure.
- One reminder the store refused to rewrite ended the sweep for every
  reminder after it.
- Scheduled jobs ran with a bare PATH that finds no agent CLI.
"""

from __future__ import annotations

import datetime
import json
import os
import time

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def config(tmp_path):
    cfg = Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"),
                 project_dirs=[str(tmp_path / "projects")])
    cfg.reminders.action_enabled = True
    return cfg


@pytest.fixture
def store(config):
    s = Store(config)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    import kindex.config as kconfig
    monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(kconfig, "_GLOBAL_PATHS", [tmp_path / "home" / "kin.yaml"])


def past(minutes: int = 5) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(
        timespec="seconds")


def test_a_reminder_cancelled_while_its_action_runs_stays_cancelled(config, store, monkeypatch):
    from kindex import actions
    from kindex.reminders import cancel_reminder, check_and_fire

    one_shot = store.add_reminder("one shot", past(), extra={"action_command": "false"})
    recurring = store.add_reminder("recurring", past(), reminder_type="recurring",
                                   schedule="FREQ=HOURLY", extra={"action_command": "true"})
    other = Store(config)

    def run(command, **kwargs):
        target = one_shot if command == "false" else recurring
        cancel_reminder(other, target)
        return {"ok": command == "true", "output": ""}

    monkeypatch.setattr(actions, "_run_shell", run)
    check_and_fire(store, config)
    other.close()
    assert store.get_reminder(one_shot)["status"] == "cancelled"
    assert store.get_reminder(recurring)["status"] == "cancelled"


def test_an_action_finished_by_hand_mid_sweep_does_not_run_again(config, store, monkeypatch):
    from kindex import actions
    from kindex.actions import execute_action
    from kindex.reminders import check_and_fire

    first = store.add_reminder("first", past(), priority="urgent",
                               extra={"action_command": "first"})
    second = store.add_reminder("second", past(), extra={"action_command": "second"})
    other = Store(config)
    runs: list[str] = []

    def run(command, **kwargs):
        runs.append(command)
        if command == "first":
            execute_action(other, other.get_reminder(second), config, manual=True)
        return {"ok": True, "output": ""}

    monkeypatch.setattr(actions, "_run_shell", run)
    check_and_fire(store, config)
    other.close()
    assert runs == ["first", "second"], runs
    assert first


def test_a_failing_action_stops_after_its_attempts(config, store, monkeypatch):
    from kindex import actions
    from kindex.actions import MAX_ACTION_ATTEMPTS, execute_action

    rid = store.add_reminder("flaky", past(), extra={"action_command": "boom"})
    runs: list[str] = []
    monkeypatch.setattr(actions, "_run_shell",
                        lambda command, **kw: runs.append(command) or {"ok": False, "output": "no"})
    statuses = [execute_action(store, store.get_reminder(rid), config)["status"]
                for _ in range(MAX_ACTION_ATTEMPTS + 2)]
    assert len(runs) == MAX_ACTION_ATTEMPTS
    assert statuses[MAX_ACTION_ATTEMPTS - 1] == "exhausted"
    assert statuses[-1] == "skipped"
    # A deliberate exec still runs it.
    assert execute_action(store, store.get_reminder(rid), config, manual=True)["status"] == "failed"


def test_claude_is_capped_by_budget_and_a_spent_budget_is_final(config, store, monkeypatch):
    from kindex import actions
    from kindex.actions import execute_action

    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        return 1, json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                              "is_error": True, "result": ""}), ""

    monkeypatch.setattr(actions, "_run_process", run)
    monkeypatch.setattr(actions, "_resolve_cli", lambda name: f"/opt/bin/{name}")
    rid = store.add_reminder("agent", past(),
                             extra={"action_instructions": "Triage the build", "action_mode": "claude"})
    result = execute_action(store, store.get_reminder(rid), config)
    assert result["status"] == "exhausted"
    assert "--max-turns" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--max-budget-usd") + 1] == "0.5"
    assert seen["cmd"][0] == "/opt/bin/claude"


def test_a_missing_agent_cli_is_final_and_says_where_it_looked(config, store, monkeypatch):
    from kindex import actions
    from kindex.actions import execute_action

    monkeypatch.setattr(actions, "_resolve_cli", lambda name: None)
    rid = store.add_reminder("agent", past(),
                             extra={"action_instructions": "x", "action_mode": "codex"})
    result = execute_action(store, store.get_reminder(rid), config)
    assert result["status"] == "exhausted"
    assert "searched PATH=" in result["output"]


def test_a_command_that_starts_a_background_service_finishes(tmp_path):
    from kindex.actions import _run_shell
    started = time.monotonic()
    result = _run_shell("sleep 30 & echo started", timeout=20)
    assert result["ok"] and "started" in result["output"]
    assert time.monotonic() - started < 10


def test_undecodable_output_is_not_a_failure(tmp_path):
    from kindex.actions import _run_shell
    marker = tmp_path / "ran"
    result = _run_shell(f"printf 'done\\377\\n'; touch {marker}", timeout=10)
    assert result["ok"] and marker.exists()


def test_a_timed_out_run_takes_its_children_with_it(tmp_path):
    from kindex.actions import _run_shell
    pidfile = tmp_path / "child.pid"
    result = _run_shell(f"sleep 60 & echo $! > {pidfile}; wait", timeout=1)
    assert result == {"ok": False, "output": "Timed out after 1s"}
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the timed-out run's child is still alive")


def test_a_reminder_the_store_refuses_does_not_stop_the_sweep(config, store, monkeypatch):
    from kindex import actions
    from kindex.reminders import check_and_fire

    poisoned = store.add_reminder("legacy", past(), priority="urgent",
                                  extra={"action_command": "echo placeholder"})
    store.conn.execute(
        "UPDATE reminders SET extra = ? WHERE id = ?",
        (json.dumps({"action_command": "curl https://admin:hunter2x@example.com/x"}), poisoned))
    store.conn.commit()
    later = store.add_reminder("later", past(), extra={"action_command": "later"})
    runs: list[str] = []
    monkeypatch.setattr(actions, "_run_shell",
                        lambda command, **kw: runs.append(command) or {"ok": True, "output": ""})
    check_and_fire(store, config)
    assert runs == ["later"]
    assert store.get_reminder(later)["status"] == "completed"
    extra = json.loads(store.conn.execute(
        "SELECT extra FROM reminders WHERE id = ?", (poisoned,)).fetchone()[0])
    assert extra["action_status"] == "paused"
    assert extra["action_command"] == "curl https://admin:hunter2x@example.com/x"


def test_scheduled_jobs_run_with_a_path_that_finds_the_agent_clis(monkeypatch, tmp_path):
    from kindex import setup as ksetup
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "claude").write_text("#!/bin/sh\n")
    (tools / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}relative/bin{os.pathsep}/usr/bin")
    path = ksetup.scheduler_path().split(os.pathsep)
    assert str(tools) in path and "relative/bin" not in path
    plist = ksetup._launchd_plist(label="x", program_args=["kin", "cron"], interval=60,
                                  stdout_path="/o", stderr_path="/e",
                                  environment={"PATH": "/a:/b"}, working_directory="/home/u")
    assert "<key>EnvironmentVariables</key>" in plist and "<string>/a:/b</string>" in plist
    assert "<key>WorkingDirectory</key>" in plist


def test_crontab_lines_carry_the_path(monkeypatch, config):
    from kindex import setup as ksetup
    written = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["crontab", "-l"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        written["crontab"] = kw.get("input", "")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
    monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
    monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/opt/tools:/usr/bin")
    ksetup.install_crontab(config)
    lines = [line for line in written["crontab"].splitlines() if line]
    assert len(lines) == 2
    assert all("PATH=/opt/tools:/usr/bin /usr/local/bin/kin" in line for line in lines)


def test_a_large_prompt_does_not_deadlock_a_chatty_child():
    from kindex.actions import _run_process
    script = ("import sys; sys.stdout.write('x' * 200000); sys.stdout.flush(); "
              "data = sys.stdin.read(); print(len(data))")
    started = time.monotonic()
    returncode, stdout, _ = _run_process(["python3", "-c", script],
                                         input_text="p" * 300000, timeout=20)
    assert returncode == 0
    assert stdout.endswith("300000\n")
    assert time.monotonic() - started < 10


def test_quarantine_keeps_an_unparseable_extra(store):
    rid = store.add_reminder("legacy", past(), extra={"action_command": "true"})
    store.conn.execute("UPDATE reminders SET extra = ? WHERE id = ?", ("{not json", rid))
    store.conn.commit()
    store.quarantine_reminder_action(rid, "set aside")
    extra = json.loads(store.conn.execute(
        "SELECT extra FROM reminders WHERE id = ?", (rid,)).fetchone()[0])
    assert extra == {"action_status": "paused", "action_result": "set aside",
                     "unparsed_extra": "{not json"}
