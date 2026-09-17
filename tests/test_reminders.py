"""Tests for the reminders subsystem — time parsing, lifecycle, store CRUD, CLI."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path))


# ── Time parsing ────────────────────────────────────────────────────


class TestParseTimeSpec:
    def test_parse_in_minutes(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("in 30 minutes")
        assert rtype == "once"
        assert schedule == ""
        # Should be roughly 30 min from now
        parsed = datetime.datetime.fromisoformat(next_due)
        diff = (parsed - datetime.datetime.now()).total_seconds()
        assert 25 * 60 <= diff <= 35 * 60

    def test_parse_tomorrow_at(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("tomorrow at 3pm")
        assert rtype == "once"
        parsed = datetime.datetime.fromisoformat(next_due)
        assert parsed.hour == 15
        assert parsed > datetime.datetime.now()

    def test_parse_every_weekday(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("every weekday at 9am")
        assert rtype == "recurring"
        assert "BYDAY=MO,TU,WE,TH,FR" in schedule
        assert "BYHOUR=9" in schedule

    def test_parse_every_n_hours(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("every 2 hours")
        assert rtype == "recurring"
        assert "FREQ=HOURLY" in schedule
        assert "INTERVAL=2" in schedule

    def test_parse_raw_rrule(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0")
        assert rtype == "recurring"
        assert schedule == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0"

    def test_parse_daily_shorthand(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("daily")
        assert rtype == "recurring"
        assert "FREQ=DAILY" in schedule

    def test_parse_weekly_shorthand(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("weekly")
        assert rtype == "recurring"
        assert "FREQ=WEEKLY" in schedule

    def test_parse_every_monday_at(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("every monday at 3pm")
        assert rtype == "recurring"
        assert "BYDAY=MO" in schedule
        assert "BYHOUR=15" in schedule

    def test_parse_every_30_minutes(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("every 30 minutes")
        assert rtype == "recurring"
        assert "FREQ=MINUTELY" in schedule
        assert "INTERVAL=30" in schedule

    def test_parse_daily_at_time(self):
        from kindex.reminders import parse_time_spec

        next_due, schedule, rtype = parse_time_spec("daily at 8:30am")
        assert rtype == "recurring"
        assert "BYHOUR=8" in schedule
        assert "BYMINUTE=30" in schedule

    def test_parse_invalid_raises(self):
        from kindex.reminders import parse_time_spec

        with pytest.raises(ValueError):
            parse_time_spec("xyzzy nonsense gibberish")


class TestParseTimeOfDay:
    def test_am(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("9am") == (9, 0)

    def test_pm(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("3pm") == (15, 0)

    def test_pm_with_minutes(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("3:30pm") == (15, 30)

    def test_24_hour(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("14:00") == (14, 0)

    def test_noon(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("12pm") == (12, 0)

    def test_midnight(self):
        from kindex.reminders import _parse_time_of_day

        assert _parse_time_of_day("12am") == (0, 0)


class TestParseDuration:
    def test_minutes(self):
        from kindex.reminders import parse_duration

        assert parse_duration("15m") == 900

    def test_hours(self):
        from kindex.reminders import parse_duration

        assert parse_duration("1h") == 3600

    def test_combined(self):
        from kindex.reminders import parse_duration

        assert parse_duration("2h30m") == 9000

    def test_bare_number(self):
        from kindex.reminders import parse_duration

        assert parse_duration("60") == 60

    def test_default(self):
        from kindex.reminders import parse_duration

        assert parse_duration("whatever") == 900


# ── Store CRUD ──────────────────────────────────────────────────────


class TestStoreCRUD:
    def test_add_and_get(self, store):
        rid = store.add_reminder("Test reminder", "2026-03-01T10:00:00")
        assert rid
        r = store.get_reminder(rid)
        assert r is not None
        assert r["title"] == "Test reminder"
        assert r["next_due"] == "2026-03-01T10:00:00"
        assert r["status"] == "active"

    def test_update(self, store):
        rid = store.add_reminder("Update me", "2026-03-01T10:00:00")
        store.update_reminder(rid, priority="urgent", body="Important!")
        r = store.get_reminder(rid)
        assert r["priority"] == "urgent"
        assert r["body"] == "Important!"

    def test_delete(self, store):
        rid = store.add_reminder("Delete me", "2026-03-01T10:00:00")
        store.delete_reminder(rid)
        assert store.get_reminder(rid) is None

    def test_list_filters(self, store):
        store.add_reminder("A", "2026-03-01T10:00:00", priority="high")
        store.add_reminder("B", "2026-03-01T11:00:00", priority="low")
        store.add_reminder("C", "2026-03-01T12:00:00", priority="high")

        high = store.list_reminders(priority="high")
        assert len(high) == 2
        assert all(r["priority"] == "high" for r in high)

    def test_due_reminders(self, store):
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        future = (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        store.add_reminder("Past", past)
        store.add_reminder("Future", future)

        due = store.due_reminders()
        assert len(due) == 1
        assert due[0]["title"] == "Past"

    def test_scoped_due_reminders_filters_by_conversation(self, store):
        from kindex.reminders import scoped_due_reminders

        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        chat_a = store.add_reminder(
            "Chat A",
            past,
            extra={"conversation_id": "chat-a", "reminder_scope": "chat"},
        )
        chat_b = store.add_reminder(
            "Chat B",
            past,
            extra={"conversation_id": "chat-b", "reminder_scope": "chat"},
        )
        legacy = store.add_reminder("Legacy", past)
        global_id = store.add_reminder(
            "Global",
            past,
            extra={"reminder_scope": "global"},
        )

        due = scoped_due_reminders(store, "chat-a")
        ids = {r["id"] for r in due}

        assert chat_a in ids
        assert global_id in ids
        assert chat_b not in ids
        assert legacy not in ids

    def test_due_includes_snoozed_past(self, store):
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        rid = store.add_reminder("Snoozed", past)
        snooze_past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.snooze_reminder(rid, snooze_past)

        due = store.due_reminders()
        assert len(due) == 1
        assert due[0]["title"] == "Snoozed"

    def test_snooze_increments_count(self, store):
        rid = store.add_reminder("Snoozy", "2026-03-01T10:00:00")
        future = (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        store.snooze_reminder(rid, future)
        r = store.get_reminder(rid)
        assert r["snooze_count"] == 1
        assert r["status"] == "snoozed"

    def test_complete(self, store):
        rid = store.add_reminder("Done", "2026-03-01T10:00:00")
        store.complete_reminder(rid)
        r = store.get_reminder(rid)
        assert r["status"] == "completed"

    def test_priority_ordering(self, store):
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        store.add_reminder("Low", past, priority="low")
        store.add_reminder("Urgent", past, priority="urgent")
        store.add_reminder("Normal", past, priority="normal")

        due = store.due_reminders()
        assert due[0]["priority"] == "urgent"
        assert due[-1]["priority"] == "low"

    def test_channels_json(self, store):
        rid = store.add_reminder("Notify", "2026-03-01T10:00:00",
                                 channels=["slack", "email"])
        r = store.get_reminder(rid)
        assert r["channels"] == ["slack", "email"]


# ── Lifecycle ───────────────────────────────────────────────────────


class TestCreateReminder:
    def test_create_one_shot(self, store):
        from kindex.reminders import create_reminder

        rid = create_reminder(store, "Test", "in 30 minutes")
        r = store.get_reminder(rid)
        assert r["reminder_type"] == "once"
        assert r["schedule"] == ""

    def test_create_recurring(self, store):
        from kindex.reminders import create_reminder

        rid = create_reminder(store, "Standup", "every weekday at 9am")
        r = store.get_reminder(rid)
        assert r["reminder_type"] == "recurring"
        assert "BYDAY=MO,TU,WE,TH,FR" in r["schedule"]

    def test_create_with_priority(self, store):
        from kindex.reminders import create_reminder

        rid = create_reminder(store, "Urgent", "in 1 hour", priority="urgent")
        r = store.get_reminder(rid)
        assert r["priority"] == "urgent"

    def test_invalid_priority(self, store):
        from kindex.reminders import create_reminder

        with pytest.raises(ValueError, match="Invalid priority"):
            create_reminder(store, "Bad", "in 1 hour", priority="mega")


class TestSnooze:
    def test_snooze_sets_status(self, store):
        from kindex.reminders import create_reminder, snooze_reminder

        rid = create_reminder(store, "Test", "in 30 minutes")
        new_time = snooze_reminder(store, rid)
        r = store.get_reminder(rid)
        assert r["status"] == "snoozed"
        assert r["snooze_until"] is not None

    def test_snooze_custom_duration(self, store, config):
        from kindex.reminders import create_reminder, snooze_reminder

        rid = create_reminder(store, "Test", "in 30 minutes")
        new_time = snooze_reminder(store, rid, duration_seconds=3600)
        parsed = datetime.datetime.fromisoformat(new_time)
        diff = (parsed - datetime.datetime.now()).total_seconds()
        assert 3500 <= diff <= 3700

    def test_snooze_nonexistent(self, store):
        from kindex.reminders import snooze_reminder

        with pytest.raises(ValueError, match="not found"):
            snooze_reminder(store, "nonexistent")


class TestComplete:
    def test_complete_one_shot(self, store):
        from kindex.reminders import complete_reminder, create_reminder

        rid = create_reminder(store, "Done", "in 30 minutes")
        complete_reminder(store, rid)
        r = store.get_reminder(rid)
        assert r["status"] == "completed"

    def test_complete_recurring_advances(self, store):
        from kindex.reminders import complete_reminder

        # Create a recurring reminder with next_due in the past
        past = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
        rid = store.add_reminder(
            "Daily", past,
            reminder_type="recurring",
            schedule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        )
        r_before = store.get_reminder(rid)
        complete_reminder(store, rid)
        r_after = store.get_reminder(rid)
        # Recurring should stay active with new next_due
        assert r_after["status"] == "active"
        assert r_after["next_due"] > r_before["next_due"]


class TestCancel:
    def test_cancel_sets_status(self, store):
        from kindex.reminders import cancel_reminder, create_reminder

        rid = create_reminder(store, "Cancelled", "in 30 minutes")
        cancel_reminder(store, rid)
        r = store.get_reminder(rid)
        assert r["status"] == "cancelled"


class TestCheckAndFire:
    def test_fires_due_reminders(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire, create_reminder

        # Create a reminder in the past
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.add_reminder("Past due", past)

        # Mock notify dispatch to avoid actual notifications
        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

        fired = check_and_fire(store, config)
        assert len(fired) == 1
        assert fired[0]["title"] == "Past due"

    def test_skips_future(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire, create_reminder

        create_reminder(store, "Future", "in 2 hours")
        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

        fired = check_and_fire(store, config)
        assert len(fired) == 0

    def test_skips_when_idle(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire

        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.add_reminder("Past due", past)

        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: True)

        fired = check_and_fire(store, config)
        assert len(fired) == 0

    def test_priority_ordering(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire

        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.add_reminder("Normal", past, priority="normal")
        store.add_reminder("Urgent", past, priority="urgent")

        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

        fired = check_and_fire(store, config)
        assert fired[0]["priority"] == "urgent"


class TestAutoSnooze:
    def test_auto_snoozes_stale_fired(self, store, config):
        from kindex.reminders import auto_snooze_stale

        past = (datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
        rid = store.add_reminder("Stale", past)
        # Simulate a fired state with old last_fired
        fired_at = (datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
        store.update_reminder(rid, status="fired", last_fired=fired_at)

        count = auto_snooze_stale(store, config)
        assert count == 1
        r = store.get_reminder(rid)
        assert r["status"] == "snoozed"


# ── CLI ─────────────────────────────────────────────────────────────


def _run_cli(*args, tmp_path=None, input_text=None, env=None):
    """Run kin CLI as subprocess."""
    import os
    cmd = [sys.executable, "-m", "kindex.cli"] + list(args)
    if tmp_path:
        cmd.extend(["--data-dir", str(tmp_path)])
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=30,
                          env=None if env is None else {**os.environ, **env})


def _codex_home(root, *sessions):
    """A CODEX_HOME whose rollouts hold ``sessions`` (oldest first)."""
    import os
    import time
    day = root / "codex-home" / "sessions" / "2026" / "09" / "17"
    day.mkdir(parents=True)
    for n, session in enumerate(sessions):
        rollout = day / f"rollout-{n}.jsonl"
        rollout.write_text(json.dumps(
            {"type": "session_meta", "payload": {"id": session, "cwd": "/w"}}) + "\n")
        stamp = time.time() - 100 + n
        os.utime(rollout, (stamp, stamp))
    return root / "codex-home"


class TestReminderCLI:
    def test_remind_create(self, tmp_path):
        result = _run_cli("remind", "create", "Test CLI", "--at", "in 30 minutes",
                          tmp_path=tmp_path)
        assert result.returncode == 0
        assert "Created reminder:" in result.stdout

    def test_remind_list(self, tmp_path):
        _run_cli("remind", "create", "Listed", "--at", "in 1 hour", tmp_path=tmp_path)
        result = _run_cli("remind", "list", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "Listed" in result.stdout

    def test_remind_list_empty(self, tmp_path):
        result = _run_cli("remind", "list", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "No reminders" in result.stdout

    def test_remind_check(self, tmp_path):
        result = _run_cli("remind", "check", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "Checked:" in result.stdout

    def test_prompt_check_uses_conversation_scoped_reminder_board(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        store.add_reminder(
            "Chat A reminder",
            past,
            extra={"conversation_id": "chat-a", "reminder_scope": "chat"},
        )
        store.add_reminder(
            "Chat B reminder",
            past,
            extra={"conversation_id": "chat-b", "reminder_scope": "chat"},
        )
        store.add_reminder("Legacy reminder", past)
        store.add_reminder("Global reminder", past, extra={"reminder_scope": "global"})
        store.close()

        result = _run_cli(
            "prompt-check",
            tmp_path=tmp_path,
            input_text=json.dumps({"session_id": "chat-a", "prompt": "status"}),
        )

        assert result.returncode == 0
        assert "Chat A reminder" in result.stdout
        assert "Global reminder" in result.stdout
        assert "Chat B reminder" not in result.stdout
        assert "Legacy reminder" not in result.stdout

    def test_remind_create_with_codex_wake_json(self, tmp_path):
        home = _codex_home(tmp_path, "older-thread", "newest-thread")
        result = _run_cli(
            "remind", "create", "Continue build",
            "--at", "in 30 minutes",
            "--wake", "codex",
            "--session", "last",
            "--cwd", str(tmp_path),
            "--wake-model", "gpt-5",
            "--instructions", "Continue the outstanding build work.",
            "--json",
            tmp_path=tmp_path,
            env={"CODEX_HOME": str(home), "CODEX_THREAD_ID": ""},
        )

        assert result.returncode == 0, result.stderr
        reminder = json.loads(result.stdout)
        extra = reminder["extra"]
        assert extra["wake_client"] == "codex"
        # "last" names the session that was newest when the reminder was made.
        assert extra["wake_session_id"] == "newest-thread"
        assert extra["wake_cwd"] == str(tmp_path.resolve())
        assert extra["wake_model"] == "gpt-5"
        assert extra["action_mode"] == "codex"
        assert extra["action_status"] == "pending"


# ── Actions ────────────────────────────────────────────────────────


class TestActionFields:
    def test_has_action_false_for_plain(self, store):
        from kindex.actions import has_action
        rid = store.add_reminder("Plain", "2099-03-01T10:00:00")
        r = store.get_reminder(rid)
        assert not has_action(r)

    def test_has_action_true_with_command(self, store):
        from kindex.actions import has_action
        rid = store.add_reminder(
            "Actionable", "2099-03-01T10:00:00",
            extra={"action_command": "echo hello", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        assert has_action(r)

    def test_has_action_true_with_instructions(self, store):
        from kindex.actions import has_action
        rid = store.add_reminder(
            "Clever", "2099-03-01T10:00:00",
            extra={"action_instructions": "Check the server status",
                   "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        assert has_action(r)

    def test_has_action_true_with_wake_client(self, store):
        from kindex.actions import has_action
        rid = store.add_reminder(
            "Wake Codex", "2099-03-01T10:00:00",
            extra={"wake_client": "codex", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        assert has_action(r)

    def test_resolve_mode_auto_shell(self):
        from kindex.actions import resolve_mode
        assert resolve_mode({"action_command": "ls",
                             "action_instructions": "",
                             "action_mode": "auto"}) == "shell"

    def test_resolve_mode_auto_claude(self):
        from kindex.actions import resolve_mode
        assert resolve_mode({"action_command": "ls",
                             "action_instructions": "check it",
                             "action_mode": "auto"}) == "claude"

    def test_resolve_mode_auto_wake_client(self):
        from kindex.actions import resolve_mode
        assert resolve_mode({"wake_client": "opencode",
                             "action_mode": "auto"}) == "opencode"

    def test_resolve_mode_explicit(self):
        from kindex.actions import resolve_mode
        assert resolve_mode({"action_mode": "shell"}) == "shell"
        assert resolve_mode({"action_mode": "claude"}) == "claude"


class TestExecuteShellAction:
    def test_shell_success(self, store, config):
        from kindex.actions import execute_action
        rid = store.add_reminder(
            "Echo test", "2099-03-01T10:00:00",
            extra={"action_command": "echo hello-world",
                   "action_mode": "shell", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)
        assert result["status"] == "completed"
        assert "hello-world" in result["output"]
        # Verify stored result
        r2 = store.get_reminder(rid)
        assert r2["extra"]["action_status"] == "completed"
        assert "hello-world" in r2["extra"]["action_result"]

    def test_shell_failure(self, store, config):
        from kindex.actions import execute_action
        rid = store.add_reminder(
            "Fail test", "2099-03-01T10:00:00",
            extra={"action_command": "exit 1",
                   "action_mode": "shell", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)
        assert result["status"] == "failed"

    def test_shell_timeout(self, store, config):
        from kindex.actions import execute_action
        rid = store.add_reminder(
            "Slow", "2099-03-01T10:00:00",
            extra={"action_command": "sleep 60",
                   "action_mode": "shell", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config, timeout=1)
        assert result["status"] == "failed"
        assert "Timed out" in result["output"]

    def test_skip_already_completed(self, store, config):
        from kindex.actions import execute_action
        rid = store.add_reminder(
            "Done", "2099-03-01T10:00:00",
            extra={"action_command": "echo x",
                   "action_mode": "shell", "action_status": "completed"},
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)
        assert result["status"] == "skipped"

    def test_skip_no_action(self, store, config):
        from kindex.actions import execute_action
        rid = store.add_reminder("No action", "2099-03-01T10:00:00")
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)
        assert result["status"] == "skipped"


class TestExecuteWakeActions:
    def test_codex_wake_uses_exec_resume_last(self, store, config, monkeypatch, tmp_path):
        from kindex.actions import execute_action

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return 0, "codex done", ""

        monkeypatch.setattr("kindex.actions._run_process", fake_run)
        monkeypatch.setattr("kindex.actions._resolve_cli", lambda name: name)
        rid = store.add_reminder(
            "Continue Codex", "2099-03-01T10:00:00",
            extra={
                "wake_client": "codex",
                "wake_session_id": "last",
                "wake_cwd": str(tmp_path),
                "wake_model": "gpt-5",
                "action_instructions": "Pick up the interrupted task.",
                "action_mode": "codex",
                "action_status": "pending",
            },
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)

        assert result["status"] == "completed"
        assert seen["cmd"] == [
            "codex", "exec", "--cd", str(tmp_path), "--model", "gpt-5",
            "resume", "--last", "-",
        ]
        assert "Pick up the interrupted task." in seen["kwargs"]["input_text"]

    def test_opencode_wake_uses_run_session(self, store, config, monkeypatch, tmp_path):
        from kindex.actions import execute_action

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return 0, "opencode done", ""

        monkeypatch.setattr("kindex.actions._run_process", fake_run)
        monkeypatch.setattr("kindex.actions._resolve_cli", lambda name: name)
        rid = store.add_reminder(
            "Continue OpenCode", "2099-03-01T10:00:00",
            extra={
                "wake_client": "opencode",
                "wake_session_id": "ses_123",
                "wake_cwd": str(tmp_path),
                "wake_model": "anthropic/claude-sonnet-4-5",
                "wake_agent": "build",
                "action_instructions": "Continue the failing build triage.",
                "action_mode": "opencode",
                "action_status": "pending",
            },
        )
        r = store.get_reminder(rid)
        result = execute_action(store, r, config)

        assert result["status"] == "completed"
        assert seen["cmd"][:-1] == [
            "opencode", "run", "--session", "ses_123", "--dir", str(tmp_path),
            "--model", "anthropic/claude-sonnet-4-5", "--agent", "build",
        ]
        assert "Continue the failing build triage." in seen["cmd"][-1]


class TestCreateReminderWithAction:
    def test_create_with_command(self, store):
        from kindex.reminders import create_reminder
        rid = create_reminder(
            store, "Kill instance", "in 1 hour",
            action_command="vastai destroy instance 12345",
        )
        r = store.get_reminder(rid)
        assert r["extra"]["action_command"] == "vastai destroy instance 12345"
        assert r["extra"]["action_status"] == "pending"
        assert r["extra"]["action_mode"] == "auto"

    def test_create_with_instructions(self, store):
        from kindex.reminders import create_reminder
        rid = create_reminder(
            store, "Review results", "in 2 hours",
            action_instructions="Download data from vast.ai before killing",
            action_command="vastai ssh-url 12345",
        )
        r = store.get_reminder(rid)
        assert r["extra"]["action_instructions"] == "Download data from vast.ai before killing"
        assert r["extra"]["action_command"] == "vastai ssh-url 12345"

    def test_create_without_action_has_no_extra(self, store):
        from kindex.reminders import create_reminder
        rid = create_reminder(store, "Simple", "in 1 hour")
        r = store.get_reminder(rid)
        assert not r["extra"].get("action_command")

    def test_create_with_conversation_scope(self, store):
        from kindex.reminders import create_reminder
        rid = create_reminder(
            store,
            "Review deployment",
            "in 1 hour",
            conversation_id="chat-a",
            attention_triggers=["deploy"],
        )
        r = store.get_reminder(rid)
        assert r["extra"]["conversation_id"] == "chat-a"
        assert r["extra"]["reminder_scope"] == "chat"
        assert r["extra"]["attention_triggers"] == ["deploy"]

    def test_create_with_codex_wake(self, store, tmp_path, monkeypatch):
        from kindex.reminders import create_reminder
        monkeypatch.setenv("CODEX_THREAD_ID", "thread-in-use")
        rid = create_reminder(
            store,
            "Wake Codex",
            "in 1 hour",
            action_instructions="Continue the current review.",
            wake_client="codex",
            wake_session_id="last",
            wake_cwd=str(tmp_path),
            wake_model="gpt-5",
        )
        r = store.get_reminder(rid)
        assert r["extra"]["wake_client"] == "codex"
        # Created from inside a Codex session, "last" is that session.
        assert r["extra"]["wake_session_id"] == "thread-in-use"
        assert r["extra"]["wake_cwd"] == str(tmp_path.resolve())
        assert r["extra"]["wake_model"] == "gpt-5"
        assert r["extra"]["action_mode"] == "codex"

    def test_create_rejects_unknown_wake_client(self, store):
        from kindex.reminders import create_reminder
        with pytest.raises(ValueError, match="Invalid wake client"):
            create_reminder(store, "Bad wake", "in 1 hour", wake_client="cursor")

    def test_wake_options_need_a_wake_client(self, store):
        from kindex.reminders import create_reminder
        with pytest.raises(ValueError, match="need a wake client"):
            create_reminder(store, "Dropped", "in 1 hour", action_command="true",
                            wake_session_id="abc")

    @pytest.mark.parametrize("field,value,message", [
        ("wake_session_id", "--dangerously-bypass-approvals-and-sandbox", "Invalid wake session"),
        ("wake_model", "-c sandbox=none", "Invalid wake model"),
        ("wake_model", "gpt 5", "Invalid wake model"),
        ("wake_agent", "build", "applies only to OpenCode"),
        ("wake_cwd", "/definitely/not/a/dir", "does not exist"),
    ])
    def test_wake_values_that_cannot_reach_argv(self, store, field, value, message):
        from kindex.reminders import create_reminder
        with pytest.raises(ValueError, match=message):
            create_reminder(store, "Bad wake", "in 1 hour", action_instructions="go",
                            wake_client="codex", **{field: value})

    def test_wake_cwd_defaults_to_the_creator(self, store, tmp_path, monkeypatch):
        from kindex.reminders import create_reminder
        monkeypatch.chdir(tmp_path)
        rid = create_reminder(store, "Here", "in 1 hour", action_instructions="go",
                              wake_client="opencode", wake_session_id="ses_1")
        assert store.get_reminder(rid)["extra"]["wake_cwd"] == str(tmp_path.resolve())

    def test_last_without_a_resolvable_session_is_refused(self, store, tmp_path, monkeypatch):
        from kindex.reminders import create_reminder
        monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
        with pytest.raises(ValueError, match="Cannot resolve the latest opencode session"):
            create_reminder(store, "Later", "in 1 hour", action_instructions="go",
                            wake_client="opencode", wake_session_id="last",
                            wake_cwd=str(tmp_path))
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
        with pytest.raises(ValueError, match="Cannot resolve the latest codex session"):
            create_reminder(store, "Later", "in 1 hour", action_instructions="go",
                            wake_client="codex", wake_session_id="last",
                            wake_cwd=str(tmp_path))


class TestCheckAndFireWithActions:
    def test_fires_and_executes_action(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(
            timespec="seconds")
        store.add_reminder(
            "Auto action", past,
            extra={"action_command": "echo done",
                   "action_mode": "shell", "action_status": "pending"},
        )
        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

        fired = check_and_fire(store, config)
        assert len(fired) == 1
        # After successful action, reminder should be completed
        r = store.get_reminder(fired[0]["id"])
        assert r["status"] == "completed"

    def test_action_disabled_skips_execution(self, store, config, monkeypatch):
        from kindex.reminders import check_and_fire
        config.reminders.action_enabled = False
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(
            timespec="seconds")
        store.add_reminder(
            "Disabled", past,
            extra={"action_command": "echo nope",
                   "action_mode": "shell", "action_status": "pending"},
        )
        monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

        fired = check_and_fire(store, config)
        assert len(fired) == 1
        r = store.get_reminder(fired[0]["id"])
        assert r["status"] == "fired"


class TestFormatReminderWithAction:
    def test_format_shows_action_info(self, store):
        from kindex.reminders import format_reminder
        rid = store.add_reminder(
            "With action", "2099-03-01T10:00:00",
            extra={"action_command": "echo hello",
                   "action_mode": "shell", "action_status": "pending"},
        )
        r = store.get_reminder(rid)
        output = format_reminder(r)
        assert "Action [shell]: pending" in output
        assert "Command: echo hello" in output

    def test_format_shows_wake_info(self, store):
        from kindex.reminders import format_reminder
        rid = store.add_reminder(
            "Wake action", "2099-03-01T10:00:00",
            extra={
                "wake_client": "opencode",
                "wake_session_id": "last",
                "wake_cwd": "/tmp/project",
                "wake_agent": "build",
                "action_mode": "opencode",
                "action_status": "pending",
            },
        )
        r = store.get_reminder(rid)
        output = format_reminder(r)
        assert "Action [opencode]: pending" in output
        assert "Wake: opencode session=last" in output
        assert "Cwd: /tmp/project" in output
        assert "Agent: build" in output

    def test_format_shows_result_on_completed(self, store):
        from kindex.reminders import format_reminder
        rid = store.add_reminder(
            "Completed action", "2099-03-01T10:00:00",
            extra={"action_command": "echo hello",
                   "action_mode": "shell", "action_status": "completed",
                   "action_result": "hello"},
        )
        r = store.get_reminder(rid)
        output = format_reminder(r)
        assert "Result: hello" in output


class TestStopGuardCLI:
    def test_stop_guard_blocks_with_pending(self, tmp_path):
        import json as _json
        cfg = tmp_path / "kin.yaml"
        result = _run_cli(
            "config", "set", "reminders.stop_guard_enabled", "true",
            "--config", str(cfg),
            tmp_path=tmp_path,
        )
        assert result.returncode == 0
        result = _run_cli(
            "remind", "create", "Kill instance",
            "--at", "in 30 minutes",
            "--action", "vastai destroy 123",
            "--config", str(cfg),
            tmp_path=tmp_path,
        )
        assert result.returncode == 0

        result = _run_cli("stop-guard", "--config", str(cfg), tmp_path=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip()
        data = _json.loads(result.stdout)
        assert data["decision"] == "block"
        assert "Kill instance" in data["message"]

    def test_stop_guard_default_quiet_with_pending(self, tmp_path):
        result = _run_cli(
            "remind", "create", "Kill instance",
            "--at", "in 30 minutes",
            "--action", "vastai destroy 123",
            tmp_path=tmp_path,
        )
        assert result.returncode == 0

        result = _run_cli("stop-guard", tmp_path=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_stop_guard_allows_without_pending(self, tmp_path):
        result = _run_cli("stop-guard", tmp_path=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_stop_guard_allows_recursive_stop_hook(self, tmp_path):
        result = _run_cli(
            "stop-guard",
            tmp_path=tmp_path,
            input_text='{"stop_hook_active": true}',
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestReminderExecCLI:
    def test_exec_via_cli(self, tmp_path):
        result = _run_cli(
            "remind", "create", "Echo test",
            "--at", "in 1 hour",
            "--action", "echo hello-exec",
            tmp_path=tmp_path,
        )
        assert result.returncode == 0
        # Extract the reminder ID from "Created reminder: <id> (due: ...)"
        rid = result.stdout.split(":")[1].strip().split(" ")[0]

        result = _run_cli(
            "remind", "exec", "--reminder-id", rid,
            tmp_path=tmp_path,
        )
        assert result.returncode == 0
        assert "completed" in result.stdout.lower() or "hello-exec" in result.stdout


class TestPromptCheckCLI:
    def test_prompt_check_no_due(self, tmp_path):
        """No output when no reminders are due."""
        result = _run_cli("prompt-check", tmp_path=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_prompt_check_with_due(self, tmp_path):
        """Outputs reminder context when reminders are due."""
        from kindex.store import Store
        from kindex.config import Config
        import datetime

        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        # Create reminder due in the past via store directly
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.add_reminder("Prompt test reminder", next_due=past)
        store.close()

        result = _run_cli("prompt-check", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "KINDEX REMINDERS DUE" in result.stdout
        assert "Prompt test reminder" in result.stdout
        assert "kin remind done" in result.stdout

    def test_prompt_check_with_action(self, tmp_path):
        """Shows action details for actionable reminders."""
        from kindex.store import Store
        from kindex.config import Config
        import datetime

        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        store.add_reminder(
            "Action reminder", next_due=past,
            extra={
                "action_command": "echo hello",
                "action_instructions": "Run the echo command",
                "action_mode": "auto",
                "action_status": "pending",
            },
        )
        store.close()

        result = _run_cli("prompt-check", tmp_path=tmp_path)
        assert result.returncode == 0
        assert "Action reminder" in result.stdout
        # Summarised as the prime does: mode, sizes, digest and a preview of
        # what an agent would act on (the instructions), never a call to run it.
        assert "Action (claude, instructions" in result.stdout
        assert "Run the echo command" in result.stdout
        assert "kin remind exec" not in result.stdout
