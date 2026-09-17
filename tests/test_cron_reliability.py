"""Cron reminder-delivery reliability — ordering, recurring re-execution,
stale-run recovery, embedding drain time budget, and scheduler command shape.

Regression suite for the MEA incident where `kin cron` starved reminder
delivery behind a Voyage embedding backlog and recurring wake reminders
stopped re-executing after their first occurrence.
"""

from __future__ import annotations

import datetime

import pytest

from kindex.config import Config, ProfileEntry
from kindex.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=str(tmp_path),
        claude_dir=str(tmp_path / "claude"),
        project_dirs=[str(tmp_path / "projects")],
    )


@pytest.fixture
def store(config):
    s = Store(config)
    yield s
    s.close()


def _quiet_notify(monkeypatch):
    monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)


def _past() -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )


# ── Cron ordering: reminders fire before slow maintenance ───────────


class TestCronReminderOrdering:
    def test_reminders_fire_before_embedding_drain(self, config, store, monkeypatch):
        """By the time the embedding drain runs, due reminders are already fired."""
        from kindex import vectors
        from kindex.daemon import cron_run

        _quiet_notify(monkeypatch)
        rid = store.add_reminder("due now", _past())

        status_at_drain = {}

        def fake_drain(s, cfg, **kw):
            status_at_drain["value"] = store.get_reminder(rid)["status"]
            return {"status": "ok", "embedded": 0, "pending": 0}

        monkeypatch.setattr(vectors, "drain_embedding_queue", fake_drain)

        results = cron_run(config, store)

        assert status_at_drain["value"] == "fired"
        assert results["reminders_fired"] >= 1

    def test_reminders_fire_even_when_ingest_raises(self, config, store, monkeypatch):
        """A failing maintenance step cannot retroactively starve reminders."""
        from kindex import ingest
        from kindex.daemon import cron_run

        _quiet_notify(monkeypatch)
        rid = store.add_reminder("due now", _past())

        def boom(*a, **kw):
            raise RuntimeError("provider hung")

        monkeypatch.setattr(ingest, "scan_projects", boom)

        with pytest.raises(RuntimeError):
            cron_run(config, store)

        assert store.get_reminder(rid)["status"] == "fired"


# ── Recurring actions must re-execute every occurrence ──────────────


class TestRecurringActionReset:
    def _recurring_with_action(self, store, action_status=None):
        extra = {"action_command": "echo hi", "action_mode": "shell"}
        if action_status:
            extra["action_status"] = action_status
            extra["action_executed_at"] = datetime.datetime.now().isoformat(
                timespec="seconds"
            )
        return store.add_reminder(
            "recurring action", _past(),
            reminder_type="recurring", schedule="FREQ=HOURLY", extra=extra,
        )

    def test_advance_recurring_resets_action_status(self, store):
        from kindex.reminders import advance_recurring

        rid = self._recurring_with_action(store, action_status="completed")
        next_due = advance_recurring(store, rid)

        assert next_due is not None
        r = store.get_reminder(rid)
        assert r["extra"]["action_status"] == "pending"
        assert r["status"] == "active"

    def test_action_reexecutes_each_occurrence(self, config, store, monkeypatch):
        """The action runs again on the next occurrence, not exactly once ever."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        rid = self._recurring_with_action(store)

        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": "done"},
        )

        assert len(check_and_fire(store, config)) == 1
        assert len(runs) == 1

        # Simulate the next occurrence coming due
        store.update_reminder(rid, next_due=_past())
        assert len(check_and_fire(store, config)) == 1
        assert len(runs) == 2

    def test_manual_exec_then_advance_is_reexecutable(self, config, store, monkeypatch):
        """kin remind exec, then the schedule advances — the action is re-armed."""
        from kindex import actions
        from kindex.actions import execute_action
        from kindex.reminders import advance_recurring

        rid = self._recurring_with_action(store)
        monkeypatch.setattr(
            actions, "_run_shell", lambda cmd, **kw: {"ok": True, "output": ""}
        )

        assert execute_action(store, store.get_reminder(rid), config)["status"] == "completed"
        # Manual exec marks completed; without advance it stays skipped
        assert execute_action(store, store.get_reminder(rid), config)["status"] == "skipped"

        advance_recurring(store, rid)
        assert execute_action(store, store.get_reminder(rid), config)["status"] == "completed"


# ── Stale "running" recovery ─────────────────────────────────────────


class TestStaleRunningRecovery:
    def _reminder(self, store, *, executed_at) -> str:
        extra = {"action_command": "echo hi", "action_status": "running"}
        if executed_at is not None:
            extra["action_executed_at"] = executed_at
        return store.add_reminder("stuck", _past(), extra=extra)

    def test_fresh_running_is_skipped(self, config, store):
        from kindex.actions import execute_action

        rid = self._reminder(
            store,
            executed_at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        result = execute_action(store, store.get_reminder(rid), config)
        assert result["status"] == "skipped"

    def test_stale_running_is_reclaimed(self, config, store, monkeypatch):
        """A running marker from a killed cron must not brick the reminder."""
        from kindex import actions
        from kindex.actions import execute_action

        stale = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        rid = self._reminder(store, executed_at=stale)
        monkeypatch.setattr(
            actions, "_run_shell", lambda cmd, **kw: {"ok": True, "output": ""}
        )

        result = execute_action(store, store.get_reminder(rid), config)
        assert result["status"] == "completed"

    def test_running_without_timestamp_is_reclaimed(self, config, store, monkeypatch):
        from kindex import actions
        from kindex.actions import execute_action

        rid = self._reminder(store, executed_at=None)
        monkeypatch.setattr(
            actions, "_run_shell", lambda cmd, **kw: {"ok": True, "output": ""}
        )

        result = execute_action(store, store.get_reminder(rid), config)
        assert result["status"] == "completed"


# ── Embedding drain wall-clock budget ────────────────────────────────


class TestEmbeddingDrainTimeBudget:
    def test_drain_stops_at_time_budget(self, config, store, monkeypatch):
        """A slow provider cannot hold the drain past its wall-clock budget."""
        from kindex import vectors

        for i in range(5):
            store.add_node(f"Node {i}", content=f"text {i}", node_id=f"n{i}")
            vectors.enqueue_embedding(store, f"n{i}")

        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(vectors, "_upsert_embedding_outcome", lambda s, nid, text: vectors._EmbeddingOutcome(True))

        class FakeClock:
            t = 0.0

            def monotonic(self):
                # Each embed attempt costs 50 "seconds" of provider latency
                self.t += 50.0
                return self.t

        monkeypatch.setattr(vectors, "time", FakeClock())

        result = vectors.drain_embedding_queue(store, config, time_budget=120)

        # deadline = 50 + 120 = 170; attempts at t=100 and t=150 pass,
        # t=200 exceeds the deadline and carries the rest to the next cron
        assert result["embedded"] == 2
        assert result["pending"] == 3
        # The carried-over queue must be durably persisted for the next cron
        import json

        persisted = json.loads(store.get_meta(vectors.EMBED_QUEUE_META) or "[]")
        assert persisted == ["n2", "n3", "n4"]

    def test_drain_completes_within_budget(self, config, store, monkeypatch):
        from kindex import vectors

        store.add_node("Node", content="text", node_id="n0")
        vectors.enqueue_embedding(store, "n0")
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(vectors, "_upsert_embedding_outcome", lambda s, nid, text: vectors._EmbeddingOutcome(True))

        result = vectors.drain_embedding_queue(store, config)
        assert result["embedded"] == 1
        assert result["pending"] == 0

    def test_drain_unlimited_budget_drains_backlog(self, config, store, monkeypatch):
        """`kin embed drain --time-budget 0` maps to an unlimited budget so a
        deliberate interactive drain is not silently capped."""
        from kindex import vectors

        for i in range(5):
            store.add_node(f"Node {i}", content=f"text {i}", node_id=f"n{i}")
            vectors.enqueue_embedding(store, f"n{i}")

        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(vectors, "_upsert_embedding_outcome", lambda s, nid, text: vectors._EmbeddingOutcome(True))

        class SlowClock:
            t = 0.0

            def monotonic(self):
                self.t += 500.0  # every check far past any finite budget
                return self.t

        monkeypatch.setattr(vectors, "time", SlowClock())

        result = vectors.drain_embedding_queue(
            store, config, time_budget=float("inf")
        )
        assert result["embedded"] == 5
        assert result["pending"] == 0


# ── remind check --all-profiles / cron sweep ─────────────────────────


class TestRemindCheckAll:
    def test_legacy_single_graph(self, config, store, monkeypatch):
        from kindex.daemon import remind_check_all

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past())

        sweeps = remind_check_all(config)
        assert len(sweeps) == 1
        assert sweeps[0]["fired"] == 1

    def _profiles_config(self, tmp_path):
        return Config(
            data_dir=str(tmp_path / "legacy"),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "projects")],
            profiles={
                "a": ProfileEntry(data_dir=str(tmp_path / "a")),
                "b": ProfileEntry(data_dir=str(tmp_path / "b")),
            },
            default_profile="a",
        )

    def test_sweeps_every_profile(self, tmp_path, monkeypatch):
        from kindex.daemon import remind_check_all

        _quiet_notify(monkeypatch)
        cfg = self._profiles_config(tmp_path)
        for name in ("a", "b"):
            sub = cfg.model_copy(deep=True)
            sub.data_dir = str(tmp_path / name)
            s = Store(sub)
            s.add_reminder(f"due in {name}", _past())
            s.close()

        sweeps = {s["profile"]: s for s in remind_check_all(cfg)}
        assert sweeps["a"]["fired"] == 1
        assert sweeps["b"]["fired"] == 1

    def test_sweeps_registered_project_graphs(self, tmp_path, config, monkeypatch):
        """Project-local .kin graphs (no profile) get their reminders fired too
        — the MEA case: data_dir: .kindex-data inside a repo."""
        import json

        from kindex.daemon import remind_check_all

        _quiet_notify(monkeypatch)
        project_root = tmp_path / "proj"
        graph_dir = project_root / ".kindex-data"
        graph_dir.mkdir(parents=True)

        sub = config.model_copy(deep=True)
        sub.data_dir = str(graph_dir)
        s = Store(sub)
        rid = s.add_reminder("due in project graph", _past())
        s.close()

        base = Store(config)
        base.set_meta(
            "project_graph_dirs",
            json.dumps({str(project_root): str(graph_dir.resolve())}),
        )
        base.close()

        sweeps = {s["profile"]: s for s in remind_check_all(config)}
        assert sweeps[str(project_root)]["fired"] == 1

        s = Store(sub)
        assert s.get_reminder(rid)["status"] == "fired"
        s.close()

    def test_scan_kin_files_registers_project_graph(self, tmp_path):
        import json

        from kindex.ingest import scan_kin_files

        projects = tmp_path / "projects"
        repo = projects / "mea"
        kin_dir = repo / ".kin"
        kin_dir.mkdir(parents=True)
        (kin_dir / "config").write_text(
            "name: MEA\ndata_dir: .kindex-data\n"
        )
        (repo / ".kindex-data").mkdir()

        cfg = Config(
            data_dir=str(tmp_path / "data"),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(projects)],
        )
        s = Store(cfg)
        scan_kin_files(cfg, s)

        registry = json.loads(s.get_meta("project_graph_dirs") or "{}")
        assert registry == {str(repo): str((repo / ".kindex-data").resolve())}
        s.close()

    def test_scan_kin_files_registers_inherited_data_dir(self, tmp_path):
        """A repo that only `inherits:` a template's data_dir still gets a live
        graph via load_config — the registry must see it too."""
        import json

        from kindex.ingest import scan_kin_files

        projects = tmp_path / "projects"
        template = projects / "template"
        (template / ".kin").mkdir(parents=True)
        (template / ".kin" / "config").write_text("data_dir: .kindex-data\n")
        (template / ".kindex-data").mkdir()

        child = projects / "app"
        (child / ".kin").mkdir(parents=True)
        (child / ".kin" / "config").write_text(
            "name: app\ninherits:\n  - ../../template/.kin/config\n"
        )
        (child / ".kindex-data").mkdir()

        cfg = Config(
            data_dir=str(tmp_path / "data"),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(projects)],
        )
        s = Store(cfg)
        scan_kin_files(cfg, s)

        registry = json.loads(s.get_meta("project_graph_dirs") or "{}")
        assert registry[str(child)] == str((child / ".kindex-data").resolve())
        s.close()

    def test_pinned_profile_run_does_not_double_count(self, tmp_path, config, monkeypatch):
        """`kin cron --profile X` (profiles cleared, active_profile kept) must
        not report the base graph's firings again as project-graph firings."""
        from kindex import daemon

        _quiet_notify(monkeypatch)
        config.active_profile = "work"
        s = Store(config)
        s.add_reminder("due", _past())
        s.close()

        monkeypatch.setattr(
            daemon, "cron_run",
            lambda cfg, st, verbose=False: {"reminders_fired": 0,
                                            "reminders_auto_snoozed": 0},
        )

        passes = daemon.cron_run_all(config)

        assert passes[0]["results"]["reminders_fired"] == 1
        assert "project_graph_reminders_fired" not in passes[0]["results"]

    def test_cron_run_all_fires_reminders_before_passes(self, tmp_path, monkeypatch):
        """Profile B's reminders can't wait behind profile A's maintenance."""
        from kindex import daemon

        _quiet_notify(monkeypatch)
        cfg = self._profiles_config(tmp_path)
        rids = {}
        for name in ("a", "b"):
            sub = cfg.model_copy(deep=True)
            sub.data_dir = str(tmp_path / name)
            s = Store(sub)
            rids[name] = s.add_reminder(f"due in {name}", _past())
            s.close()

        fired_at_pass = {}

        def fake_cron_run(config, store, verbose=False):
            # Snapshot each graph's reminder status when its maintenance
            # pass starts — the sweep must have fired them all already.
            for name in ("a", "b"):
                sub = cfg.model_copy(deep=True)
                sub.data_dir = str(tmp_path / name)
                s = Store(sub)
                fired_at_pass.setdefault(
                    config.active_profile, {}
                )[name] = s.get_reminder(rids[name])["status"]
                s.close()
            return {"reminders_fired": 0, "reminders_auto_snoozed": 0}

        monkeypatch.setattr(daemon, "cron_run", fake_cron_run)

        passes = daemon.cron_run_all(cfg)

        first_pass = passes[0]["profile"]
        assert fired_at_pass[first_pass] == {"a": "fired", "b": "fired"}
        # Sweep counts are merged into the per-profile pass results
        by_profile = {p["profile"]: p["results"] for p in passes}
        assert by_profile["a"]["reminders_fired"] == 1
        assert by_profile["b"]["reminders_fired"] == 1


# ── Cross-process double-fire protection ────────────────────────────


class TestSweepLock:
    def test_concurrent_sweep_is_excluded(self, config, store, monkeypatch):
        """While one process holds the sweep lock, a second check fires nothing."""
        from kindex.reminders import _acquire_check_lock, check_and_fire

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past())

        token = _acquire_check_lock(store)
        assert token is not None
        assert check_and_fire(store, config) == []  # lost the lock -> no-op

    def test_release_allows_next_sweep(self, config, store, monkeypatch):
        from kindex.reminders import (
            _acquire_check_lock,
            _release_check_lock,
            check_and_fire,
        )

        _quiet_notify(monkeypatch)
        rid = store.add_reminder("due", _past())

        token = _acquire_check_lock(store)
        _release_check_lock(store, token)
        fired = check_and_fire(store, config)
        assert [r["id"] for r in fired] == [rid]

    def test_expired_lock_is_reclaimed(self, config, store, monkeypatch):
        """A crashed holder's lock expires and the next sweep proceeds."""
        from kindex.reminders import _acquire_check_lock, check_and_fire

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past())

        token = _acquire_check_lock(store, ttl=-5)  # already expired
        assert token is not None
        assert len(check_and_fire(store, config)) == 1

    def test_second_contender_loses_atomically(self, store):
        from kindex.reminders import _acquire_check_lock

        assert _acquire_check_lock(store) is not None
        assert _acquire_check_lock(store) is None

    def test_renew_extends_held_lock(self, store):
        """The heartbeat keeps a long sweep's lock alive past the base TTL."""
        from kindex.reminders import _acquire_check_lock, _renew_check_lock

        token = _acquire_check_lock(store, ttl=-5)  # expired the moment it's taken
        assert token is not None
        assert _renew_check_lock(store, token, ttl=120) is True
        # Renewed: a contender can no longer claim it as expired
        assert _acquire_check_lock(store) is None

    def test_renew_fails_after_takeover(self, store):
        from kindex.reminders import _acquire_check_lock, _renew_check_lock

        stale = _acquire_check_lock(store, ttl=-5)
        thief = _acquire_check_lock(store)
        assert thief is not None
        assert _renew_check_lock(store, stale) is False
        assert _renew_check_lock(store, thief) is True

    def test_sweep_stops_when_lock_stolen_mid_run(self, config, store, monkeypatch):
        """If a contender reclaims the lock mid-sweep, the old holder must not
        keep firing — that would be the double-fire the lock exists to stop."""
        from kindex import reminders

        monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)
        first = store.add_reminder("first", _past())
        second = store.add_reminder(
            "second",
            (datetime.datetime.now() - datetime.timedelta(minutes=4)).isoformat(
                timespec="seconds"
            ),
        )

        def stealing_dispatch(r, cfg, channel_names=None):
            # Simulate a contender taking over while this reminder is processed
            store.set_meta(
                reminders._CHECK_LOCK_KEY,
                "someone-else|" + (
                    datetime.datetime.now() + datetime.timedelta(seconds=120)
                ).isoformat(timespec="seconds"),
            )
            return []

        monkeypatch.setattr("kindex.notify.dispatch", stealing_dispatch)

        fired = reminders.check_and_fire(store, config)

        fired_ids = {r["id"] for r in fired}
        statuses = {rid: store.get_reminder(rid)["status"] for rid in (first, second)}
        # Exactly one processed; the other untouched for the new lock holder
        assert len(fired_ids) == 1
        assert sorted(statuses.values()) == ["active", "fired"]


    def test_the_lock_outlives_a_full_action(self, config, store, monkeypatch):
        """While a reminder's action runs, the lock stays held past the action's
        whole budget, so no contender can reclaim it mid-action."""
        from kindex import reminders

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past(), extra={"action_command": "true"})
        seen = {}

        def slow_action(st, reminder, cfg, *, timeout=300, manual=False):
            value = st.get_meta(reminders._CHECK_LOCK_KEY)
            seen["expires"] = datetime.datetime.fromisoformat(value.split("|", 1)[1])
            seen["timeout"] = timeout
            # A contender arriving now finds the lock held.
            seen["contender"] = reminders._acquire_check_lock(st)
            return {"status": "completed", "output": ""}

        monkeypatch.setattr("kindex.actions.execute_action", slow_action)
        config.reminders.action_enabled = True
        assert len(reminders.check_and_fire(store, config)) == 1
        remaining = (seen["expires"] - datetime.datetime.now()).total_seconds()
        assert remaining > seen["timeout"] + 60
        assert seen["contender"] is None


class TestConcurrencyGuards:
    def test_advance_recurring_preserves_running(self, store):
        """A live running marker survives advance — no overlapping executions;
        a dead one is recovered by the stale-reclaim in execute_action."""
        from kindex.reminders import advance_recurring

        rid = store.add_reminder(
            "recurring", _past(), reminder_type="recurring",
            schedule="FREQ=HOURLY",
            extra={
                "action_command": "echo hi",
                "action_status": "running",
                "action_executed_at": datetime.datetime.now().isoformat(
                    timespec="seconds"
                ),
            },
        )
        advance_recurring(store, rid)
        assert store.get_reminder(rid)["extra"]["action_status"] == "running"

    def test_auto_snooze_does_not_resurrect_completed(self, config, store):
        from kindex.reminders import auto_snooze_stale

        stale = (datetime.datetime.now() - datetime.timedelta(hours=3)).isoformat(
            timespec="seconds"
        )
        rid = store.add_reminder("was fired", _past())
        store.update_reminder(rid, status="fired", last_fired=stale)

        real_get = store.get_reminder

        def complete_then_get(reminder_id):
            # Simulate the user completing it between SELECT and snooze
            store.conn.execute(
                "UPDATE reminders SET status='done' WHERE id = ?", (reminder_id,)
            )
            store.conn.commit()
            return real_get(reminder_id)

        store.get_reminder = complete_then_get
        try:
            assert auto_snooze_stale(store, config) == 0
        finally:
            store.get_reminder = real_get
        assert real_get(rid)["status"] == "done"


# ── Staleness guard: stale backlogs must not auto-execute ────────────


class TestActionStalenessGuard:
    def _actionable(self, store, *, overdue_seconds, recurring=False):
        due = (
            datetime.datetime.now() - datetime.timedelta(seconds=overdue_seconds)
        ).isoformat(timespec="seconds")
        kwargs = {"extra": {"action_command": "echo hi", "action_mode": "shell"}}
        if recurring:
            kwargs.update(reminder_type="recurring", schedule="FREQ=HOURLY")
        return store.add_reminder("actionable", due, **kwargs)

    def test_fresh_overdue_action_executes(self, config, store, monkeypatch):
        """Minutes overdue — a live reentry wake — executes immediately."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        self._actionable(store, overdue_seconds=300)

        check_and_fire(store, config)
        assert len(runs) == 1

    def test_stale_action_notifies_but_does_not_execute(self, config, store, monkeypatch):
        """Overdue past max_action_overdue: notification fires, action does not
        — a first-install backlog cannot detonate a swarm of headless agents."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        rid = self._actionable(store, overdue_seconds=3 * 86400)

        fired = check_and_fire(store, config)

        assert runs == []
        assert [r["id"] for r in fired] == [rid]
        # Fired-pending: the action stays available for a deliberate
        # `kin remind exec`
        r = store.get_reminder(rid)
        assert r["status"] == "fired"
        assert (r["extra"] or {}).get("action_status", "pending") == "pending"

    def test_stale_recurring_is_parked_not_just_delayed(self, config, store, monkeypatch):
        """A stale recurring poller must not resume one period later — the
        advance re-arms it, so without parking the swarm returns synchronized.
        Sweeps after the advance must still not execute."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        rid = self._actionable(store, overdue_seconds=3 * 86400, recurring=True)

        check_and_fire(store, config)

        assert runs == []
        r = store.get_reminder(rid)
        assert r["status"] == "active"  # advanced to the next occurrence
        assert datetime.datetime.fromisoformat(r["next_due"]) > datetime.datetime.now()
        assert r["extra"]["action_status"] == "paused"

        # One period later: the occurrence is fresh-overdue, but the parked
        # action must still not auto-execute.
        store.update_reminder(rid, next_due=_past())
        check_and_fire(store, config)
        assert runs == []
        assert store.get_reminder(rid)["extra"]["action_status"] == "paused"

    def test_manual_exec_resumes_parked_recurring(self, config, store, monkeypatch):
        """kin remind exec resumes a parked poller; it auto-executes again on
        the following occurrences."""
        from kindex import actions
        from kindex.actions import execute_action
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        rid = self._actionable(store, overdue_seconds=3 * 86400, recurring=True)
        check_and_fire(store, config)  # parks it
        assert store.get_reminder(rid)["extra"]["action_status"] == "paused"

        # Sweep-context exec still refuses; deliberate manual exec resumes
        assert execute_action(store, store.get_reminder(rid), config)["status"] == "skipped"
        assert execute_action(
            store, store.get_reminder(rid), config, manual=True
        )["status"] == "completed"
        assert len(runs) == 1

        # Next occurrence auto-executes again (completed -> pending on advance)
        from kindex.reminders import advance_recurring
        advance_recurring(store, rid)
        store.update_reminder(rid, next_due=_past())
        check_and_fire(store, config)
        assert len(runs) == 2

    def test_snoozed_action_executes_on_snooze_expiry(self, config, store, monkeypatch):
        """An explicit long snooze is a deferral, not staleness: the action
        must execute when the snooze expires even if next_due is days old."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        rid = self._actionable(store, overdue_seconds=2 * 86400)
        just_expired = (
            datetime.datetime.now() - datetime.timedelta(seconds=10)
        ).isoformat(timespec="seconds")
        store.snooze_reminder(rid, just_expired)

        check_and_fire(store, config)
        assert len(runs) == 1

    def test_unparseable_due_time_does_not_abort_sweep(self, config, store, monkeypatch):
        """A reminder with a malformed/aware next_due is treated as stale —
        it must not crash the sweep for everyone else."""
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        bad = self._actionable(store, overdue_seconds=300)
        store.update_reminder(bad, next_due="2026-07-07T00:00:00+00:00")
        good = self._actionable(store, overdue_seconds=300)

        fired = check_and_fire(store, config)

        assert len(runs) == 1  # only the well-formed one executed
        assert {r["id"] for r in fired} >= {good}

    def test_guard_disabled_with_zero(self, config, store, monkeypatch):
        from kindex import actions
        from kindex.reminders import check_and_fire

        _quiet_notify(monkeypatch)
        config.reminders.action_enabled = True
        config.reminders.max_action_overdue = 0
        runs = []
        monkeypatch.setattr(
            actions, "_run_shell",
            lambda cmd, **kw: runs.append(cmd) or {"ok": True, "output": ""},
        )
        self._actionable(store, overdue_seconds=30 * 86400)

        check_and_fire(store, config)
        assert len(runs) == 1


# ── Late-due re-check and disabled/fault paths ───────────────────────


class TestSweepRobustness:
    def test_reminder_due_mid_maintenance_fires_same_run(self, config, store, monkeypatch):
        """Step 10 re-checks: something that comes due during maintenance
        fires before cron_run returns, and counts accumulate."""
        from kindex import daemon

        _quiet_notify(monkeypatch)
        early = store.add_reminder("due at start", _past())
        late = store.add_reminder(
            "due mid-run",
            (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat(
                timespec="seconds"
            ),
        )

        def hygiene_making_late_due(s, verbose=False):
            store.update_reminder(late, next_due=_past())
            return {"archived": 0, "linked": 0}

        monkeypatch.setattr(daemon, "_graph_hygiene", hygiene_making_late_due)

        results = daemon.cron_run(config, store)

        assert store.get_reminder(early)["status"] == "fired"
        assert store.get_reminder(late)["status"] == "fired"
        assert results["reminders_fired"] == 2

    def test_sweep_zeros_when_reminders_disabled(self, config, store, monkeypatch):
        from kindex.daemon import remind_check_all

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past())
        config.reminders.enabled = False

        sweeps = remind_check_all(config)
        assert sweeps[0]["fired"] == 0
        assert store.get_reminder(
            store.list_reminders()[0]["id"]
        )["status"] == "active"

    def test_broken_project_graph_does_not_abort_sweep(self, tmp_path, config, store, monkeypatch):
        """A corrupt registered graph is reported, not fatal."""
        import json

        from kindex.daemon import remind_check_all

        _quiet_notify(monkeypatch)
        rid = store.add_reminder("due in base", _past())

        broken = tmp_path / "brokenproj" / ".kindex-data"
        broken.parent.mkdir(parents=True)
        broken.mkdir()
        (broken / "kindex.db").write_text("this is not a sqlite database" * 100)
        store.set_meta(
            "project_graph_dirs",
            json.dumps({str(tmp_path / "brokenproj"): str(broken)}),
        )

        sweeps = {s["profile"]: s for s in remind_check_all(config)}
        assert sweeps[None]["fired"] == 1
        assert store.get_reminder(rid)["status"] == "fired"
        broken_entry = sweeps[str(tmp_path / "brokenproj")]
        assert broken_entry["fired"] == 0
        assert "error" in broken_entry

    def test_legacy_cron_run_all_services_project_graphs(self, tmp_path, config, monkeypatch):
        """No profiles configured (the common install): kin cron alone must
        still fire project-graph reminders via the sweep."""
        import json

        from kindex import daemon

        _quiet_notify(monkeypatch)
        project_root = tmp_path / "proj"
        graph_dir = project_root / ".kindex-data"
        graph_dir.mkdir(parents=True)

        sub = config.model_copy(deep=True)
        sub.data_dir = str(graph_dir)
        s = Store(sub)
        rid = s.add_reminder("due in project graph", _past())
        s.close()

        base = Store(config)
        base.set_meta(
            "project_graph_dirs",
            json.dumps({str(project_root): str(graph_dir.resolve())}),
        )
        base.close()

        monkeypatch.setattr(
            daemon, "cron_run",
            lambda cfg, st, verbose=False: {"reminders_fired": 0,
                                            "reminders_auto_snoozed": 0},
        )

        passes = daemon.cron_run_all(config)

        s = Store(sub)
        assert s.get_reminder(rid)["status"] == "fired"
        s.close()
        assert passes[0]["results"]["project_graph_reminders_fired"] == 1


# ── Relative .kin data_dir anchoring ─────────────────────────────────


class TestProjectDataDirAnchoring:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".kin").mkdir(parents=True)
        (repo / ".kin" / "config").write_text("name: proj\ndata_dir: .kindex-data\n")
        (repo / ".kindex-data").mkdir()
        return repo

    def test_relative_data_dir_anchors_to_project_root(self, tmp_path, monkeypatch):
        """--project-path from any cwd must open the project's graph, not
        <cwd>/.kindex-data — the scheduler/hook invocation shape."""
        from kindex.config import load_config

        repo = self._repo(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        cfg = load_config(project_path=repo)
        assert cfg.data_path == (repo / ".kindex-data").resolve()

    def test_subdirectory_does_not_split_the_graph(self, tmp_path, monkeypatch):
        from kindex.config import load_config

        repo = self._repo(tmp_path)
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)

        cfg = load_config(project_path=sub)
        assert cfg.data_path == (repo / ".kindex-data").resolve()

    def test_absolute_data_dir_untouched(self, tmp_path, monkeypatch):
        from kindex.config import load_config

        repo = tmp_path / "repo"
        target = tmp_path / "graphs" / "proj"
        (repo / ".kin").mkdir(parents=True)
        (repo / ".kin" / "config").write_text(f"name: proj\ndata_dir: {target}\n")
        monkeypatch.chdir(tmp_path)

        cfg = load_config(project_path=repo)
        assert cfg.data_path == target.resolve()


# ── Scheduler command shape ──────────────────────────────────────────


class TestSchedulerCommandShape:
    def _install_home(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        calls = []
        monkeypatch.setattr(
            "kindex.setup.subprocess.run",
            lambda *a, **kw: calls.append(a) or type(
                "P", (), {"returncode": 0, "stdout": ""}
            )(),
        )
        return calls

    def test_launchd_cron_plist_shape(self, tmp_path, config, monkeypatch):
        from kindex import setup as ksetup

        self._install_home(tmp_path, monkeypatch)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_launchd(config)

        plist = (tmp_path / "Library" / "LaunchAgents" / "com.kindex.cron.plist").read_text()
        assert "<string>/usr/local/bin/kin</string>" in plist
        assert "<string>cron</string>" in plist
        assert "com.kindex.cron" in plist

    def test_launchd_reminder_plist_shape(self, tmp_path, config, monkeypatch):
        from kindex import setup as ksetup

        self._install_home(tmp_path, monkeypatch)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_reminder_daemon(config)

        plist = (
            tmp_path / "Library" / "LaunchAgents" / "com.kindex.reminders.plist"
        ).read_text()
        assert "com.kindex.reminders" in plist
        assert "<string>remind</string>" in plist
        assert "<string>check</string>" in plist
        assert "<string>--all-profiles</string>" in plist

    def test_launchd_python_module_fallback_splits_argv(self, tmp_path, config, monkeypatch):
        """The `python -m kindex.cli` fallback must be one argv element each."""
        from kindex import setup as ksetup

        self._install_home(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ksetup, "_find_kin_path", lambda: "/usr/bin/python3 -m kindex.cli"
        )

        ksetup.install_launchd(config)

        plist = (tmp_path / "Library" / "LaunchAgents" / "com.kindex.cron.plist").read_text()
        assert "<string>/usr/bin/python3</string>" in plist
        assert "<string>-m</string>" in plist
        assert "<string>kindex.cli</string>" in plist
        assert "<string>/usr/bin/python3 -m kindex.cli</string>" not in plist

    def test_crontab_installs_maintenance_and_reminder_lines(self, config, monkeypatch):
        from kindex import setup as ksetup

        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_crontab(config)

        assert "kin cron >>" in written["crontab"]
        assert "remind check --all-profiles" in written["crontab"]
        # Maintenance is phase-offset from the :00-aligned reminder checker
        assert "2-59/30 * * * *" in written["crontab"]

    def test_crontab_rerun_migrates_stale_log_path(self, config, monkeypatch):
        """Re-running setup-cron replaces lines whose log path was frozen
        from a previous data_dir/profile — without appending duplicates and
        without touching unrelated user lines. Covers the `python -m
        kindex.cli` fallback shape (no 'kin cron' substring)."""
        from kindex import setup as ksetup

        old_logs = "/somewhere/else/logs"  # a previous data_dir's log path
        user_line = "0 4 * * * /usr/local/bin/backup.sh"
        existing = (
            f"{user_line}\n"
            f"2-59/30 * * * * /usr/bin/python3 -m kindex.cli cron >> {old_logs}/cron.log 2>&1\n"
            f"*/5 * * * * /usr/bin/python3 -m kindex.cli remind check --all-profiles "
            f">> {old_logs}/reminders.log 2>&1\n"
        )
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(
            ksetup, "_find_kin_path", lambda: "/usr/bin/python3 -m kindex.cli"
        )

        actions = ksetup.install_crontab(config)

        new_logs = str(config.scheduler_log_path)
        assert any(a.startswith("Replaced crontab") for a in actions)
        assert written["crontab"].count("kindex.cli cron >>") == 1
        assert written["crontab"].count("remind check --all-profiles") == 1
        assert old_logs not in written["crontab"]
        assert f"{new_logs}/cron.log" in written["crontab"]
        assert f"{new_logs}/reminders.log" in written["crontab"]
        assert user_line in written["crontab"]

    def test_crontab_rerun_identical_is_noop(self, config, monkeypatch):
        """When the installed lines already match, nothing is rewritten."""
        from kindex import setup as ksetup

        logs = str(config.scheduler_log_path)
        existing = (
            f"2-59/30 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin cron >> {logs}/cron.log 2>&1\n"
            f"*/5 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin remind check --all-profiles "
            f">> {logs}/reminders.log 2>&1\n"
        )
        calls = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            calls.append(kw.get("input", ""))
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        actions = ksetup.install_crontab(config)

        assert calls == []  # nothing rewritten
        assert actions == ["Crontab entries already exist"]

    def test_is_kindex_cron_line_matches_jobs_not_mentions(self):
        from kindex.setup import is_kindex_cron_line

        # Our installed job shapes, all historical variants
        assert is_kindex_cron_line(
            "2-59/30 * * * * /usr/local/bin/kin cron >> /x/cron.log 2>&1")
        assert is_kindex_cron_line(
            "*/30 * * * * /usr/bin/python3 -m kindex.cli cron >> /x/cron.log 2>&1")
        assert is_kindex_cron_line(
            "*/5 * * * * /usr/local/bin/kin remind check --all-profiles >> /x/r.log 2>&1")
        assert is_kindex_cron_line(
            "*/7 * * * * /home/u/.local/pipx/venvs/kindex/bin/kin cron >> /x/c.log 2>&1")
        # User lines that merely mention kindex paths must never be ours
        assert not is_kindex_cron_line("0 3 * * * cp ~/.kindex/kindex.db /backups/")
        assert not is_kindex_cron_line("0 4 * * * rsync -a ~/Code/kindex /backup/")
        assert not is_kindex_cron_line("# kindex jobs below")

    def test_crontab_user_lines_mentioning_kindex_survive_noop(self, config, monkeypatch):
        """A user backup job touching ~/.kindex must not trigger a rewrite
        (regression: the bare 'kindex' substring matcher deleted it)."""
        from kindex import setup as ksetup

        logs = str(config.scheduler_log_path)
        existing = (
            "0 3 * * * cp ~/.kindex/kindex.db /backups/\n"
            "# kindex jobs below\n"
            f"2-59/30 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin cron >> {logs}/cron.log 2>&1\n"
            f"*/5 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin remind check --all-profiles "
            f">> {logs}/reminders.log 2>&1\n"
        )
        calls = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            calls.append(kw.get("input", ""))
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        actions = ksetup.install_crontab(config)

        assert calls == []
        assert actions == ["Crontab entries already exist"]

    def test_crontab_migration_keeps_user_kindex_lines(self, config, monkeypatch):
        """Migration rewrites stale kindex jobs but keeps user lines that
        merely mention kindex paths."""
        from kindex import setup as ksetup

        user_backup = "0 3 * * * cp ~/.kindex/kindex.db /backups/"
        user_comment = "# kindex jobs below"
        existing = (
            f"{user_backup}\n{user_comment}\n"
            "2-59/30 * * * * /usr/local/bin/kin cron >> /old/logs/cron.log 2>&1\n"
            "*/5 * * * * /usr/local/bin/kin remind check --all-profiles "
            ">> /old/logs/reminders.log 2>&1\n"
        )
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_crontab(config)

        assert user_backup in written["crontab"]
        assert user_comment in written["crontab"]
        assert "/old/logs" not in written["crontab"]
        assert written["crontab"].count("kin cron >>") == 1

    def test_crontab_rerun_preserves_repacked_interval(self, config, monkeypatch):
        """An adaptively repacked maintenance schedule (same command + log
        target, different cron fields) must survive a setup-cron re-run."""
        from kindex import setup as ksetup

        logs = str(config.scheduler_log_path)
        existing = (
            f"*/7 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin cron >> {logs}/cron.log 2>&1\n"
            f"*/5 * * * * PATH=/usr/bin:/bin /usr/local/bin/kin remind check --all-profiles "
            f">> {logs}/reminders.log 2>&1\n"
        )
        calls = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            calls.append(kw.get("input", ""))
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        actions = ksetup.install_crontab(config)

        assert calls == []
        assert actions == ["Crontab entries already exist"]

    def test_crontab_adds_missing_reminder_line_to_old_install(self, config, monkeypatch):
        """Upgrades from the single-line install get the reminder line added."""
        from kindex import setup as ksetup

        old_line = "*/30 * * * * /usr/local/bin/kin cron >> /x/cron.log 2>&1"
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type(
                    "P", (), {"returncode": 0, "stdout": old_line + "\n", "stderr": ""}
                )()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_crontab(config)

        assert written["crontab"].count("kin cron >>") == 1
        assert "remind check --all-profiles" in written["crontab"]

    def test_reminder_plist_interval_clamped_and_valid(self, tmp_path, monkeypatch):
        """check_interval=3600 -> reminder job clamps to 300s, cron job keeps
        3600s; both rendered plists parse as valid XML plists."""
        import plistlib

        from kindex import setup as ksetup

        self._install_home(tmp_path, monkeypatch)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")
        cfg = Config(data_dir=str(tmp_path / "data"))
        cfg.reminders.check_interval = 3600

        ksetup.install_launchd(cfg)
        ksetup.install_reminder_daemon(cfg)

        agents = tmp_path / "Library" / "LaunchAgents"
        cron = plistlib.loads((agents / "com.kindex.cron.plist").read_bytes())
        rem = plistlib.loads((agents / "com.kindex.reminders.plist").read_bytes())
        assert cron["StartInterval"] == 3600
        assert rem["StartInterval"] == 300
        assert rem["ProgramArguments"] == [
            "/usr/local/bin/kin", "remind", "check", "--all-profiles",
        ]

    def test_setup_cron_cmd_installs_and_uninstalls_both_jobs(self, tmp_path, config, monkeypatch):
        from types import SimpleNamespace

        from kindex import cli as kcli
        from kindex import setup as ksetup

        self._install_home(tmp_path, monkeypatch)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")
        monkeypatch.setattr(kcli, "_config", lambda args: config)

        agents = tmp_path / "Library" / "LaunchAgents"
        args = SimpleNamespace(method="launchd", dry_run=False, uninstall=False)
        kcli.cmd_setup_cron(args)
        assert (agents / "com.kindex.cron.plist").exists()
        assert (agents / "com.kindex.reminders.plist").exists()

        args = SimpleNamespace(method="launchd", dry_run=False, uninstall=True)
        kcli.cmd_setup_cron(args)
        assert not (agents / "com.kindex.cron.plist").exists()
        assert not (agents / "com.kindex.reminders.plist").exists()

    def test_cli_dispatches_remind_check_all_profiles(self, config, store, monkeypatch, capsys):
        """The exact argv the schedulers run parses and sweeps."""
        from kindex import cli as kcli

        _quiet_notify(monkeypatch)
        store.add_reminder("due", _past())
        monkeypatch.setattr(kcli, "_config", lambda args: config)
        monkeypatch.setattr(kcli, "_store", lambda args: Store(config))

        parser = kcli.build_parser()
        args = parser.parse_args(["remind", "check", "--all-profiles"])
        args.func(args)

        out = capsys.readouterr().out
        assert "1 fired" in out

    def test_repack_preserves_reminder_crontab_line(self, config, monkeypatch):
        """Adaptive repack rewrites the maintenance line but keeps remind check,
        even when the kin path contains 'kindex' (pipx/venv installs)."""
        from kindex import scheduling

        existing = (
            "*/30 * * * * /home/u/.local/pipx/venvs/kindex/bin/kin cron >> /x/cron.log 2>&1\n"
            "*/5 * * * * /home/u/.local/pipx/venvs/kindex/bin/kin remind check "
            "--all-profiles >> /x/reminders.log 2>&1\n"
        )
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.scheduling.subprocess.run", fake_run)
        monkeypatch.setattr("kindex.setup._find_kin_path", lambda: "/usr/local/bin/kin")

        result = scheduling._apply_crontab(600, config)

        assert result["action"] == "updated"
        assert "remind check --all-profiles" in written["crontab"]
        assert "*/10 * * * *" in written["crontab"]  # 600s -> every 10 min
        assert written["crontab"].count("cron >>") == 1


# ── Scheduler log location (issue #15) ───────────────────────────────


def _profile_config(tmp_path):
    """A config whose cwd-resolved profile overwrote data_dir, as when
    setup-cron runs from inside a profile root."""
    from kindex.config import _activate_profile

    base = tmp_path / "base"
    prof = tmp_path / "prof"
    cfg = Config(
        data_dir=str(base),
        profiles={"personal": ProfileEntry(data_dir=str(prof), roots=[])},
    )
    return _activate_profile(cfg, "personal", "roots"), base, prof


class TestSchedulerLogLocation:
    def test_scheduler_log_path_without_profile_is_data_dir(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path / "base"))
        assert cfg.scheduler_log_path == (tmp_path / "base" / "logs").resolve()

    def test_scheduler_log_path_with_profile_is_base_dir(self, tmp_path):
        cfg, base, prof = _profile_config(tmp_path)
        assert cfg.data_path == prof.resolve()
        assert cfg.scheduler_log_path == (base / "logs").resolve()

    def test_scheduler_log_path_survives_model_copy(self, tmp_path):
        cfg, base, _prof = _profile_config(tmp_path)
        copied = cfg.model_copy(deep=True)
        assert copied.scheduler_log_path == (base / "logs").resolve()

    def test_crontab_logs_to_base_dir_not_active_profile(self, tmp_path, monkeypatch):
        from kindex import setup as ksetup

        cfg, base, prof = _profile_config(tmp_path)
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_crontab(cfg)

        base_logs = str((base / "logs").resolve())
        assert f">> {base_logs}/cron.log" in written["crontab"]
        assert f">> {base_logs}/reminders.log" in written["crontab"]
        assert str(prof) not in written["crontab"]

    def test_crontab_creates_base_log_dir(self, tmp_path, monkeypatch):
        from kindex import setup as ksetup

        cfg, base, _prof = _profile_config(tmp_path)

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.setup.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_crontab(cfg)

        assert (base / "logs").is_dir()

    def test_launchd_plists_log_to_base_dir(self, tmp_path, monkeypatch):
        import plistlib
        from pathlib import Path

        from kindex import setup as ksetup

        cfg, base, prof = _profile_config(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            "kindex.setup.subprocess.run",
            lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": ""})(),
        )
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        ksetup.install_launchd(cfg)
        ksetup.install_reminder_daemon(cfg)

        base_logs = str((base / "logs").resolve())
        agents = tmp_path / "Library" / "LaunchAgents"
        cron = plistlib.loads((agents / "com.kindex.cron.plist").read_bytes())
        rem = plistlib.loads((agents / "com.kindex.reminders.plist").read_bytes())
        assert cron["StandardOutPath"] == f"{base_logs}/cron.log"
        assert cron["StandardErrorPath"] == f"{base_logs}/cron-error.log"
        assert rem["StandardOutPath"] == f"{base_logs}/reminders.log"
        assert str(prof) not in cron["StandardOutPath"]

    def test_apply_crontab_uses_base_log_dir(self, tmp_path, monkeypatch):
        from kindex import scheduling
        from kindex import setup as ksetup

        cfg, base, prof = _profile_config(tmp_path)
        existing = "0 4 * * * /usr/local/bin/backup.sh\n"
        written = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return type("P", (), {"returncode": 0, "stdout": existing, "stderr": ""})()
            written["crontab"] = kw.get("input", "")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("kindex.scheduling.subprocess.run", fake_run)
        monkeypatch.setattr(ksetup, "_find_kin_path", lambda: "/usr/local/bin/kin")
        monkeypatch.setattr(ksetup, "scheduler_path", lambda: "/usr/bin:/bin")

        result = scheduling._apply_crontab(420, cfg)

        base_logs = str((base / "logs").resolve())
        assert result["action"] == "updated"
        assert f">> {base_logs}/cron.log" in written["crontab"]
        assert str(prof) not in written["crontab"]
        # The redirect target must exist or cron dies silently
        assert (base / "logs").is_dir()

    def test_cron_run_all_profile_pass_anchors_scheduler_logs(self, tmp_path, monkeypatch):
        """Per-profile daemon passes must keep scheduler logs at the base
        dir even in legacy passthrough (profiles, no default_profile, no
        activation) — otherwise the adaptive repack re-poisons the crontab
        log path that issue #15 fixed."""
        from kindex import daemon

        base = tmp_path / "base"
        prof = tmp_path / "prof"
        cfg = Config(
            data_dir=str(base),
            profiles={"work": ProfileEntry(data_dir=str(prof), roots=[])},
        )
        assert getattr(cfg, "_legacy_data_dir", None) is None  # passthrough

        seen = []
        monkeypatch.setattr(daemon, "remind_check_all", lambda c, verbose=False: [])
        monkeypatch.setattr(
            daemon, "cron_run",
            lambda c, s, verbose=False: seen.append(c) or {},
        )

        class _FakeStore:
            def __init__(self, _cfg):
                pass

            def close(self):
                pass

        monkeypatch.setattr("kindex.store.Store", _FakeStore)

        daemon.cron_run_all(cfg)

        work_pass = next(c for c in seen if c.active_profile == "work")
        assert work_pass.data_path == prof.resolve()
        assert work_pass.scheduler_log_path == (base / "logs").resolve()


class TestManualExecSettles:
    def test_a_manual_one_shot_run_completes_the_reminder(self, config, store, monkeypatch, tmp_path):
        import argparse

        from kindex import actions, cli

        monkeypatch.setattr(actions, "_run_shell", lambda cmd, **kw: {"ok": True, "output": "ok"})
        rid = store.add_reminder("one shot", _past(), extra={"action_command": "echo hi"})
        monkeypatch.setattr(cli, "_store", lambda args: store)
        monkeypatch.setattr(cli, "_config", lambda args: config)
        monkeypatch.setattr(store, "close", lambda: None)
        cli.cmd_remind(argparse.Namespace(remind_action="exec", reminder_id=rid, json=True))
        assert store.get_reminder(rid)["status"] == "completed"

    def test_mcp_exec_does_not_resume_a_paused_action(self, config, store, monkeypatch):
        import kindex.mcp_server as mcp_mod
        from kindex import actions

        ran = []
        monkeypatch.setattr(actions, "_run_shell", lambda cmd, **kw: ran.append(cmd) or {"ok": True, "output": ""})
        monkeypatch.setattr(mcp_mod, "_get_store", lambda: (store, config))
        rid = store.add_reminder("parked", _past(),
                                 extra={"action_command": "echo hi", "action_status": "paused"})
        message = mcp_mod.remind_exec(rid)
        assert message.startswith("Action skipped"), message
        assert ran == []
