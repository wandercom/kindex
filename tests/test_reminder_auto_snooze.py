"""Automatic notification retries must not renew action freshness."""

import datetime
from unittest.mock import Mock

import pytest

from kindex import actions, reminders
from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    now = [datetime.datetime(2026, 9, 10, 12)]
    monkeypatch.setattr(reminders, "_now_dt", lambda: now[0])
    monkeypatch.setattr(reminders, "_now", lambda: now[0].isoformat())
    monkeypatch.setattr("kindex.store._now", lambda: now[0].isoformat())
    monkeypatch.setattr(reminders, "_try_repack", lambda store: None)
    notify = Mock(return_value=[])
    monkeypatch.setattr("kindex.notify.dispatch", notify)
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda config: False)
    run = Mock(return_value={"ok": True, "output": "mocked"})
    for runner in ("_run_shell", "_run_claude", "_run_codex", "_run_opencode"):
        monkeypatch.setattr(actions, runner, run)
    config = Config(data_dir=str(tmp_path))
    config.reminders.action_enabled = True
    store = Store(config)
    yield store, config, now, notify, run
    store.close()


def add_action(store, mode="shell", due="2026-06-01T12:00:00"):
    return store.add_reminder("isolated replay", due, extra={
        "action_command": "mocked-command",
        "action_instructions": "mocked instructions",
        "action_mode": mode,
    })


def auto_snooze_and_expire(store, config, now, rid):
    now[0] += datetime.timedelta(seconds=config.reminders.auto_snooze_timeout)
    assert reminders.auto_snooze_stale(store, config) == 1
    assert store.get_reminder(rid)["status"] == "snoozed"
    assert reminders.check_and_fire(store, config) == []
    now[0] += datetime.timedelta(seconds=config.reminders.snooze_duration)


@pytest.mark.parametrize("mode", ["shell", "claude", "codex", "opencode"])
def test_stale_one_shot_stays_notification_only_through_auto_snooze(cycle, mode):
    store, config, now, notify, run = cycle
    rid = add_action(store, mode)
    assert [r["id"] for r in reminders.check_and_fire(store, config)] == [rid]
    assert store.get_reminder(rid)["status"] == "fired"
    run.assert_not_called()
    for _ in range(2):
        auto_snooze_and_expire(store, config, now, rid)
        assert [r["id"] for r in reminders.check_and_fire(store, config)] == [rid]
        run.assert_not_called()
        assert store.get_reminder(rid)["status"] == "fired"
    assert notify.call_count == 3
    # Explicit execution remains available even after repeated auto-snoozes.
    assert actions.execute_action(store, store.get_reminder(rid), config,
                                  manual=True)["status"] == "completed"
    run.assert_called_once()


def test_manual_snooze_after_auto_snooze_renews_deferral(cycle):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    reminders.check_and_fire(store, config)
    auto_snooze_and_expire(store, config, now, rid)
    reminders.snooze_reminder(store, rid, duration_seconds=2 * 86400)
    now[0] += datetime.timedelta(days=2)
    reminders.check_and_fire(store, config)
    run.assert_called_once()
    assert store.get_reminder(rid)["status"] == "completed"


def test_auto_snooze_preserves_manual_deadline_without_extending_it(cycle):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    reminders.snooze_reminder(store, rid, duration_seconds=2 * 86400)
    now[0] += datetime.timedelta(days=2)
    run.return_value = {"ok": False, "output": "retryable failure"}
    reminders.check_and_fire(store, config)
    assert run.call_count == 1
    auto_snooze_and_expire(store, config, now, rid)
    reminders.check_and_fire(store, config)
    assert run.call_count == 2  # still fresh relative to the manual deferral
    now[0] += datetime.timedelta(seconds=config.reminders.max_action_overdue)
    auto_snooze_and_expire(store, config, now, rid)
    reminders.check_and_fire(store, config)
    assert run.call_count == 2  # automatic deferral cannot renew it forever


def test_legacy_unmarked_snooze_keeps_deliberate_deferral_semantics(cycle):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    # Old storage has no provenance: do not guess from last_fired/count.
    store.update_reminder(rid, status="snoozed", snooze_until=now[0].isoformat(),
                          last_fired="2026-06-01T12:00:00", snooze_count=12)
    reminders.check_and_fire(store, config)
    run.assert_called_once()


class BeforeReminderWrite:
    """Commit through a second connection immediately before the snooze write."""

    def __init__(self, conn, callback):
        self.conn = conn
        self.callback = callback

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if self.callback and (
            sql.lstrip().startswith("UPDATE reminders") or sql == "BEGIN IMMEDIATE"
        ):
            callback, self.callback = self.callback, None
            callback()
        return self.conn.execute(sql, *args)


@pytest.mark.parametrize("automatic", [False, True])
def test_snooze_does_not_replay_concurrent_manual_completion(cycle, monkeypatch, automatic):
    store, config, now, notify, run = cycle
    rid = add_action(store, due=now[0].isoformat())
    config.reminders.action_enabled = False
    reminders.check_and_fire(store, config)
    config.reminders.action_enabled = True
    other = Store(config)
    try:
        def finish_action():
            assert actions.execute_action(other, other.get_reminder(rid), config,
                                          manual=True)["status"] == "completed"

        monkeypatch.setattr(store, "_conn", BeforeReminderWrite(store.conn, finish_action))
        now[0] += datetime.timedelta(seconds=config.reminders.auto_snooze_timeout)
        if automatic:
            assert reminders.auto_snooze_stale(store, config) == 1
        else:
            reminders.snooze_reminder(store, rid, config=config)
        now[0] += datetime.timedelta(seconds=config.reminders.snooze_duration)
        reminders.check_and_fire(store, config)
        run.assert_called_once()
        extra = store.get_reminder(rid)["extra"]
        assert extra["action_status"] == "completed"
        assert extra["action_result"] == "mocked"
    finally:
        other.close()


def test_action_finishing_after_auto_snooze_keeps_stale_deadline(cycle, monkeypatch):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    reminders.check_and_fire(store, config)
    other = Store(config)
    try:
        def snooze_during_action(*args, **kwargs):
            now[0] += datetime.timedelta(seconds=config.reminders.auto_snooze_timeout)
            assert reminders.auto_snooze_stale(other, config) == 1
            return {"ok": False, "output": "failed manual attempt"}

        run.side_effect = snooze_during_action
        assert actions.execute_action(store, store.get_reminder(rid), config,
                                      manual=True)["status"] == "failed"
        run.side_effect = None
        now[0] += datetime.timedelta(seconds=config.reminders.snooze_duration)
        reminders.check_and_fire(store, config)
        run.assert_called_once()
    finally:
        other.close()


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_auto_snooze_does_not_reopen_concurrent_terminal_reminder(cycle, monkeypatch, terminal):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    reminders.check_and_fire(store, config)
    other = Store(config)
    try:
        monkeypatch.setattr(store, "_conn", BeforeReminderWrite(
            store.conn, lambda: other.update_reminder(rid, status=terminal),
        ))
        now[0] += datetime.timedelta(seconds=config.reminders.auto_snooze_timeout)
        assert reminders.auto_snooze_stale(store, config) == 0
        assert store.get_reminder(rid)["status"] == terminal
        run.assert_not_called()
    finally:
        other.close()


def test_recurring_cleanup_preserves_concurrent_manual_resume(cycle, monkeypatch):
    store, config, now, notify, run = cycle
    rid = add_action(store)
    store.update_reminder(rid, reminder_type="recurring", schedule="FREQ=HOURLY")
    reminders.check_and_fire(store, config)  # stale recurrence becomes paused
    assert store.get_reminder(rid)["extra"]["action_status"] == "paused"
    now[0] += datetime.timedelta(hours=1)
    reminders.snooze_reminder(store, rid, duration_seconds=1)
    now[0] += datetime.timedelta(seconds=1)
    other = Store(config)
    try:
        def resume_action():
            assert actions.execute_action(other, other.get_reminder(rid), config,
                                          manual=True)["status"] == "completed"

        monkeypatch.setattr(store, "_conn", BeforeReminderWrite(store.conn, resume_action))
        reminders.check_and_fire(store, config)
        run.assert_called_once()  # the deliberate resume during advancement
        now[0] += datetime.timedelta(hours=1)
        reminders.check_and_fire(store, config)
        assert run.call_count == 2  # the next occurrence must remain resumed
        assert store.get_reminder(rid)["extra"]["action_status"] == "pending"
    finally:
        other.close()
