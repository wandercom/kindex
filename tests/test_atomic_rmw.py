"""Regression tests for read-modify-write races on nodes.extra.

Each test encodes the adversarial repro from the fix-wave findings: a
concurrent agent's committed write lands between a caller's snapshot and
its write-back. The pre-fix code wrote the whole extra column from the
stale snapshot (silently erasing the concurrent commit); the fixed code
routes the mutation through Store.atomic_extra_update (or an equivalent
BEGIN IMMEDIATE re-read), so the fresh state is honored.

The stale snapshot is injected deterministically by monkeypatching the
seam read (store.get_node / sessions.get_tag / tasks.list_tasks /
store.nodes_with_expiry) after the rival commit, which models exactly
the original interleaving without thread-timing flakiness.
"""

import copy
import json
import threading

import pytest

from kindex import sessions, tasks
from kindex.config import Config
from kindex.locks import lock_node
from kindex.store import LockHeldError, Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


def _fresh_extra(store, node_id):
    """Read extra straight from the DB, bypassing any get_node monkeypatch."""
    row = store.conn.execute(
        "SELECT extra FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return json.loads(row["extra"] or "{}")


def _fresh_row(store, node_id):
    row = store.conn.execute(
        "SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def _stale(node):
    return copy.deepcopy(node)


# ── idx 7: task claims are check-then-set ──────────────────────────────


class TestClaimAtomicity:
    def test_claim_with_stale_snapshot_raises_not_overwrites(
            self, store, monkeypatch):
        """A claim committed after this caller's snapshot must block it."""
        tid = tasks.create_task(store, "Shared work")
        stale = _stale(store.get_node(tid))          # no claim yet
        tasks.claim_task(store, tid, "agent-b")      # rival commit

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda nid: _stale(stale))
            with pytest.raises(ValueError, match="agent-b"):
                tasks.claim_task(store, tid, "agent-a")

        extra = _fresh_extra(store, tid)
        assert extra["claim"]["agent"] == "agent-b", "rival claim was erased"
        assert extra["task_status"] == "in_progress"

    def test_concurrent_claims_exactly_one_winner(self, tmp_path):
        """Two Store handles racing on one claim: one wins, one raises."""
        setup = Store(Config(data_dir=str(tmp_path)))
        tid = tasks.create_task(setup, "Contended work")
        setup.close()

        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def worker(name: str):
            s = Store(Config(data_dir=str(tmp_path)))
            try:
                barrier.wait(timeout=10)
                try:
                    tasks.claim_task(s, tid, name)
                    results[name] = "won"
                except ValueError:
                    results[name] = "lost"
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(f"agent-{i}",),
                                    daemon=True) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        assert sorted(results.values()) == ["lost", "won"], results
        winner = next(k for k, v in results.items() if v == "won")
        check = Store(Config(data_dir=str(tmp_path)))
        assert check.get_node(tid)["extra"]["claim"]["agent"] == winner
        check.close()

    def test_release_with_stale_snapshot_refuses_foreign_claim(
            self, store, monkeypatch):
        """A claim re-acquired by another agent must not be dropped by a
        release decided against the stale snapshot."""
        tid = tasks.create_task(store, "Handover")
        tasks.claim_task(store, tid, "agent-a")
        stale = _stale(store.get_node(tid))          # claim: agent-a
        tasks.claim_task(store, tid, "agent-b", force=True)  # rival takeover

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda nid: _stale(stale))
            with pytest.raises(ValueError, match="agent-b"):
                tasks.release_task_claim(store, tid, agent="agent-a")

        assert _fresh_extra(store, tid)["claim"]["agent"] == "agent-b"


# ── idx 10: update_task / cleanup_expired_claims vs claim lifecycle ────


class TestTaskUpdateVsClaim:
    def test_update_task_preserves_claim_landed_after_snapshot(
            self, store, monkeypatch):
        tid = tasks.create_task(store, "Tuned task", priority=3)
        stale = _stale(store.get_node(tid))          # unclaimed snapshot
        tasks.claim_task(store, tid, "agent-b")      # rival commit

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda nid: _stale(stale))
            tasks.update_task(store, tid, priority=1)

        extra = _fresh_extra(store, tid)
        assert extra["claim"]["agent"] == "agent-b", "claim erased by update"
        assert extra["task_status"] == "in_progress"
        assert extra["priority"] == 1
        # weight follow-up applied without clobbering extra
        assert _fresh_row(store, tid)["weight"] == pytest.approx(0.9)

    def test_cleanup_sweep_keeps_claim_refreshed_mid_sweep(
            self, store, monkeypatch):
        tid = tasks.create_task(store, "Long job")
        expired_claim = {"agent": "agent-a",
                         "claimed_at": "2020-01-01T00:00:00",
                         "expires_at": "2020-01-01T02:00:00", "note": ""}
        store.atomic_extra_update(
            tid, lambda e: e.update(
                {"claim": dict(expired_claim),
                 "task_status": "in_progress"}) or None)

        stale_tasks = copy.deepcopy(
            tasks.list_tasks(store, status="in_progress", limit=500))
        assert stale_tasks and stale_tasks[0]["id"] == tid

        # Refresh lands between the sweep's snapshot and its write
        tasks.claim_task(store, tid, "agent-a")

        with monkeypatch.context() as m:
            m.setattr(tasks, "list_tasks",
                      lambda s, **kw: copy.deepcopy(stale_tasks))
            assert tasks.cleanup_expired_claims(store) == 0

        extra = _fresh_extra(store, tid)
        assert extra.get("claim", {}).get("agent") == "agent-a", \
            "refreshed claim dropped by sweep"
        assert extra["task_status"] == "in_progress"

    def test_cleanup_sweep_still_releases_genuinely_expired(self, store):
        tid = tasks.create_task(store, "Stale job")
        store.atomic_extra_update(
            tid, lambda e: e.update(
                {"claim": {"agent": "ghost",
                           "expires_at": "2020-01-01T00:00:00"},
                 "task_status": "in_progress"}) or None)
        assert tasks.cleanup_expired_claims(store) == 1
        extra = _fresh_extra(store, tid)
        assert "claim" not in extra
        assert extra["task_status"] == "open"


# ── idx 11: session-tag extra RMW ──────────────────────────────────────


class TestSessionTagAtomicity:
    TAG = "fix-wave"

    def _tag(self, store):
        sessions.start_tag(store, self.TAG, focus="initial work")
        return sessions.get_tag(store, self.TAG)

    def test_segment_preserves_concurrently_linked_node(
            self, store, monkeypatch):
        tag = self._tag(store)
        stale = _stale(tag)
        nid = store.add_node("Captured by B")
        sessions.link_node_to_tag(store, self.TAG, nid)  # rival commit

        with monkeypatch.context() as m:
            m.setattr(sessions, "get_tag", lambda s, n, **_: _stale(stale))
            sessions.add_segment(store, self.TAG, new_focus="next topic",
                                 summary="first part done")

        extra = _fresh_extra(store, tag["id"])
        assert nid in extra["linked_nodes"], "B's link erased by segment"
        segs = extra["segments"]
        assert len(segs) == 2
        assert segs[0]["ended_at"] and segs[0]["summary"] == "first part done"
        assert nid in segs[0]["artifacts"], "B's artifact erased by segment"
        assert segs[1]["focus"] == "next topic" and not segs[1]["ended_at"]

    def test_link_preserves_concurrently_added_segment(
            self, store, monkeypatch):
        tag = self._tag(store)
        stale = _stale(tag)
        sessions.add_segment(store, self.TAG, new_focus="topic-2")  # rival
        nid = store.add_node("Captured by A")

        with monkeypatch.context() as m:
            m.setattr(sessions, "get_tag", lambda s, n, **_: _stale(stale))
            sessions.link_node_to_tag(store, self.TAG, nid)

        extra = _fresh_extra(store, tag["id"])
        assert len(extra["segments"]) == 2, "rival segment erased by link"
        assert nid in extra["linked_nodes"]
        # The node lands in the (fresh) open segment, not the stale one
        assert nid in extra["segments"][1]["artifacts"]

    def test_update_tag_preserves_concurrent_link(self, store, monkeypatch):
        tag = self._tag(store)
        stale = _stale(tag)
        nid = store.add_node("Captured by B")
        sessions.link_node_to_tag(store, self.TAG, nid)

        with monkeypatch.context() as m:
            m.setattr(sessions, "get_tag", lambda s, n, **_: _stale(stale))
            sessions.update_tag(store, self.TAG, focus="refocus",
                                append_remaining=["todo-x"])

        extra = _fresh_extra(store, tag["id"])
        assert nid in extra["linked_nodes"]
        assert extra["current_focus"] == "refocus"
        assert extra["remaining"] == ["todo-x"]

    def test_pause_preserves_concurrent_link(self, store, monkeypatch):
        tag = self._tag(store)
        stale = _stale(tag)
        nid = store.add_node("Captured by B")
        sessions.link_node_to_tag(store, self.TAG, nid)

        with monkeypatch.context() as m:
            m.setattr(sessions, "get_tag", lambda s, n, **_: _stale(stale))
            sessions.pause_tag(store, self.TAG, summary="pausing")

        extra = _fresh_extra(store, tag["id"])
        assert nid in extra["linked_nodes"]
        assert extra["session_status"] == "paused"

    def test_complete_preserves_concurrent_link(self, store, monkeypatch):
        tag = self._tag(store)
        stale = _stale(tag)
        nid = store.add_node("Captured by B")
        sessions.link_node_to_tag(store, self.TAG, nid)

        with monkeypatch.context() as m:
            m.setattr(sessions, "get_tag", lambda s, n, **_: _stale(stale))
            sessions.complete_tag(store, self.TAG, summary="all done")

        extra = _fresh_extra(store, tag["id"])
        assert nid in extra["linked_nodes"]
        assert extra["session_status"] == "completed"
        assert all(s["ended_at"] for s in extra["segments"])


# ── idx 3/8/28: edit_node --expires vs concurrent lock / extra ─────────


class TestEditExpiresAtomicity:
    def test_lock_acquired_after_snapshot_blocks_expires_edit(
            self, store, monkeypatch):
        """The in-mutator lock recheck must abort the edit and keep the lock."""
        nid = store.add_node("Guarded")
        stale = _stale(store.get_node(nid))          # no lock yet
        lock_node(store, nid, "agent-b")             # rival lock commits

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda x: _stale(stale))
            with pytest.raises(LockHeldError, match="agent-b"):
                store.edit_node(nid, actor="agent-a", expires="2031-01-01")

        extra = _fresh_extra(store, nid)
        assert extra.get("lock", {}).get("agent") == "agent-b", \
            "rival lock erased by expires edit"
        assert "expires" not in extra

    def test_expires_edit_preserves_concurrent_extra_key(
            self, store, monkeypatch):
        nid = store.add_node("Annotated")
        stale = _stale(store.get_node(nid))
        store.atomic_extra_update(
            nid, lambda e: e.update({"custom": "kept"}) or None)  # rival

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda x: _stale(stale))
            store.edit_node(nid, actor="agent-a", expires="2031-01-01")

        extra = _fresh_extra(store, nid)
        assert extra["custom"] == "kept", "concurrent extra key erased"
        assert extra["expires"] == "2031-01-01"

    def test_holder_can_set_expires_and_keep_own_lock(self, store):
        nid = store.add_node("Mine")
        lock_node(store, nid, "agent-a")
        store.edit_node(nid, actor="agent-a", expires="2031-01-01")
        extra = _fresh_extra(store, nid)
        assert extra["lock"]["agent"] == "agent-a"
        assert extra["expires"] == "2031-01-01"


# ── idx 9/28: supersede_node lock check inside BEGIN IMMEDIATE ─────────


class TestSupersedeAtomicity:
    def test_lock_acquired_after_snapshot_blocks_supersede(
            self, store, monkeypatch):
        nid = store.add_node("Old rule")
        stale = _stale(store.get_node(nid))          # no lock yet
        lock_node(store, nid, "agent-b")             # rival lock commits

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda x: _stale(stale))
            with pytest.raises(LockHeldError, match="agent-b"):
                store.supersede_node(nid, "New rule", actor="agent-a")

        row = _fresh_row(store, nid)
        assert row["status"] == "active", "locked node was superseded"
        extra = json.loads(row["extra"] or "{}")
        assert extra.get("lock", {}).get("agent") == "agent-b"
        assert "superseded_by" not in extra
        # The rolled-back transaction must not leave a successor node
        count = store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert count == 1


# ── idx 12: update_directive_state / daemon expiry sweep ───────────────


class TestDirectiveStateAtomicity:
    def test_set_state_preserves_concurrent_lock(self, store, monkeypatch):
        nid = store.add_node("Ops directive", node_type="directive",
                             extra={"current_state": {"v": 1}})
        stale = _stale(store.get_node(nid))
        lock_node(store, nid, "agent-b")             # rival lock commits

        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda x: _stale(stale))
            store.update_directive_state(nid, {"v": 2})

        extra = _fresh_extra(store, nid)
        assert extra.get("lock", {}).get("agent") == "agent-b", \
            "rival lock erased by set-state"
        assert extra["current_state"] == {"v": 2}
        assert "state_updated_at" in extra


class TestExpirySweepAtomicity:
    def test_sweep_skips_node_extended_after_snapshot(
            self, store, monkeypatch):
        from kindex.daemon import _expire_nodes

        nid = store.add_node("Ephemeral", extra={"expires": "2020-01-01"})
        stale_nodes = copy.deepcopy(store.nodes_with_expiry(status="active"))
        assert stale_nodes and stale_nodes[0]["id"] == nid

        # Extension lands between the sweep's snapshot and its write
        store.edit_node(nid, expires="2099-01-01")

        with monkeypatch.context() as m:
            m.setattr(store, "nodes_with_expiry",
                      lambda **kw: copy.deepcopy(stale_nodes))
            results = _expire_nodes(store)

        assert results["archived"] == 0
        row = _fresh_row(store, nid)
        assert row["status"] == "active", "extended node archived anyway"
        extra = json.loads(row["extra"] or "{}")
        assert extra["expires"] == "2099-01-01", "extension reverted by sweep"
        assert "expired_at" not in extra

    def test_sweep_still_archives_genuinely_expired(self, store):
        from kindex.daemon import _expire_nodes

        nid = store.add_node("Lapsed", extra={"expires": "2020-01-01"})
        results = _expire_nodes(store)
        assert results["archived"] == 1
        row = _fresh_row(store, nid)
        assert row["status"] == "archived"
        extra = json.loads(row["extra"] or "{}")
        assert extra["expires"] == "2020-01-01"
        assert extra["expired_at"]

    def test_atomic_archive_expired_unit(self, store):
        # Still expired -> archived, stamped, True
        lapsed = store.add_node("A", extra={"expires": "2020-01-01"})
        assert store.atomic_archive_expired(lapsed, "2026-06-11T00:00:00")
        assert _fresh_row(store, lapsed)["status"] == "archived"
        assert _fresh_extra(store, lapsed)["expired_at"] == "2026-06-11T00:00:00"

        # Future expiry -> untouched, False
        live = store.add_node("B", extra={"expires": "2099-01-01"})
        assert store.atomic_archive_expired(live, "2026-06-11T00:00:00") is False
        assert _fresh_row(store, live)["status"] == "active"
        assert "expired_at" not in _fresh_extra(store, live)

        # Non-active node -> untouched, False (idempotent re-sweep)
        assert store.atomic_archive_expired(lapsed, "2026-06-12T00:00:00") is False
        assert _fresh_extra(store, lapsed)["expired_at"] == "2026-06-11T00:00:00"

        # Missing node -> False
        assert store.atomic_archive_expired("nope", "2026-06-11T00:00:00") is False


class TestWatchResolveAtomicity:
    def test_resolve_preserves_concurrent_extra_key(self, store, monkeypatch):
        import kindex.mcp_server as mcp_mod

        nid = store.add_node("Flaky CI", node_type="watch",
                             extra={"owner": "me"})
        stale = _stale(store.get_node(nid))
        store.atomic_extra_update(
            nid, lambda e: e.update({"custom": "kept"}) or None)  # rival

        monkeypatch.setattr(mcp_mod, "_store", store)
        monkeypatch.setattr(mcp_mod, "_config", store.config)
        with monkeypatch.context() as m:
            m.setattr(store, "get_node", lambda x: _stale(stale))
            result = mcp_mod.watch_resolve(nid, reason="fixed upstream")

        assert "Resolved watch" in result
        row = _fresh_row(store, nid)
        assert row["status"] == "archived"
        extra = json.loads(row["extra"] or "{}")
        assert extra["custom"] == "kept", "concurrent extra key erased"
        assert extra["watch_status"] == "resolved"
        assert extra["resolved_reason"] == "fixed upstream"
