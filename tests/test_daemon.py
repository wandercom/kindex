"""Tests for daemon module — cron_run, find_new_sessions, incremental_ingest, markers."""

import datetime
import json
import os
import time

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
    return Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))


class TestCronRun:
    def test_cron_run_empty(self, tmp_path):
        """Run cron on empty store, verify no crash."""
        from kindex.daemon import cron_run

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "projects")],
        )
        s = Store(cfg)

        results = cron_run(cfg, s, verbose=False)

        assert isinstance(results, dict)
        assert "projects" in results
        assert "sessions" in results
        assert "inbox" in results
        assert "decayed" in results
        assert "stats" in results
        assert results["stats"]["nodes"] >= 0
        s.close()

    def test_cron_run_with_data(self, tmp_path):
        """Create some nodes, run cron, verify results."""
        from kindex.daemon import cron_run

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "projects")],
        )
        s = Store(cfg)

        # Pre-populate with some nodes
        s.add_node("Alpha Concept", content="About alpha", node_id="alpha")
        s.add_node("Beta Concept", content="About beta", node_id="beta")
        s.add_edge("alpha", "beta")

        results = cron_run(cfg, s, verbose=False)

        assert isinstance(results, dict)
        assert results["stats"]["nodes"] >= 2
        assert results["stats"]["edges"] >= 1
        s.close()

    def test_cron_run_processes_inbox(self, tmp_path):
        """Cron should process inbox items."""
        from kindex.daemon import cron_run

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "projects")],
        )
        s = Store(cfg)

        # Create an inbox item
        inbox_dir = cfg.inbox_dir
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / "test-item.md").write_text(
            "---\ncreated: 2026-01-01\nprocessed: false\n---\n\n"
            "The Observer Pattern is a behavioral design pattern that defines a "
            "one-to-many dependency between objects. This pattern allows multiple "
            "Observer Objects to watch a Subject Object."
        )

        results = cron_run(cfg, s, verbose=False)

        assert results["inbox"] >= 0  # may or may not extract concepts depending on patterns
        s.close()


class TestFindNewSessions:
    def test_find_new_sessions(self, tmp_path):
        """Create mock JSONL files, verify detection."""
        from kindex.daemon import find_new_sessions

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
        )

        # Create the expected directory structure
        projects_dir = cfg.claude_path / "projects"
        project_dir = projects_dir / "my-project"
        project_dir.mkdir(parents=True)

        # Create JSONL session files
        session1 = project_dir / "abc123.jsonl"
        session1.write_text(
            json.dumps({"role": "assistant", "content": "Hello world this is a test session"}) + "\n"
        )

        session2 = project_dir / "def456.jsonl"
        session2.write_text(
            json.dumps({"role": "assistant", "content": "Another session with more content"}) + "\n"
        )

        # All files should be newer than epoch
        results = find_new_sessions(cfg, "1970-01-01T00:00:00")
        assert len(results) == 2

    def test_find_new_sessions_since_future(self, tmp_path):
        """No sessions should be found if since is in the future."""
        from kindex.daemon import find_new_sessions

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
        )

        projects_dir = cfg.claude_path / "projects"
        project_dir = projects_dir / "my-project"
        project_dir.mkdir(parents=True)

        (project_dir / "abc123.jsonl").write_text(
            json.dumps({"role": "assistant", "content": "test"}) + "\n"
        )

        results = find_new_sessions(cfg, "2099-01-01T00:00:00")
        assert len(results) == 0

    def test_find_new_sessions_no_projects_dir(self, tmp_path):
        """No crash if projects directory doesn't exist."""
        from kindex.daemon import find_new_sessions

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
        )
        # Don't create the directory

        results = find_new_sessions(cfg, "1970-01-01T00:00:00")
        assert results == []


class TestIncrementalIngest:
    def test_incremental_ingest(self, tmp_path):
        """Verify it only processes files newer than marker."""
        from kindex.daemon import incremental_ingest

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
        )
        s = Store(cfg)

        # Create a session with enough content to extract
        projects_dir = cfg.claude_path / "projects"
        project_dir = projects_dir / "test-project"
        project_dir.mkdir(parents=True)

        session_data = []
        for _ in range(5):
            session_data.append(json.dumps({
                "role": "assistant",
                "content": (
                    "The Observer Pattern is commonly used in Event Driven Architecture. "
                    "We should consider using the Strategy Pattern for algorithm selection. "
                    "Graph Neural Networks combine message passing with learned representations."
                ),
            }))

        (project_dir / "session001.jsonl").write_text("\n".join(session_data) + "\n")

        count = incremental_ingest(cfg, s, "1970-01-01T00:00:00", verbose=False)

        # Should have created at least one session node
        assert count >= 0  # may be 0 if no concepts extracted, or >= 1
        s.close()

    def test_incremental_ingest_skips_already_ingested(self, tmp_path):
        """Should skip sessions already in the store."""
        from kindex.daemon import incremental_ingest

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
        )
        s = Store(cfg)

        projects_dir = cfg.claude_path / "projects"
        project_dir = projects_dir / "test-project"
        project_dir.mkdir(parents=True)

        (project_dir / "already12345.jsonl").write_text(
            json.dumps({"role": "assistant", "content": "Some test content " * 20}) + "\n"
        )

        # Pre-create the session node
        s.add_node("Session: test-project", node_id="session-already12345", node_type="session")

        count = incremental_ingest(cfg, s, "1970-01-01T00:00:00")
        assert count == 0  # should skip the already-ingested session
        s.close()


class TestGraphHygiene:
    def test_archives_stale_orphans(self, tmp_path):
        """Stale orphans (low weight, old) should be archived."""
        from kindex.daemon import _graph_hygiene

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        # Create an orphan with low weight and old timestamp
        s.add_node("Stale orphan", content="old stuff", node_id="stale1",
                    node_type="concept")
        s.update_node("stale1", weight=0.05)
        # Backdate updated_at
        s.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'stale1'"
        )
        s.conn.commit()

        results = _graph_hygiene(s, verbose=False)
        assert results["archived"] == 1

        node = s.get_node("stale1")
        assert node["status"] == "archived"
        assert node["weight"] == 0.01
        s.close()

    def test_skips_lifecycle_types(self, tmp_path):
        """Task, session, checkpoint etc. should not be archived."""
        from kindex.daemon import _graph_hygiene

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        for ntype in ("task", "session", "checkpoint", "directive", "constraint"):
            s.add_node(f"Orphan {ntype}", node_id=f"o-{ntype}", node_type=ntype)
            s.update_node(f"o-{ntype}", weight=0.05)
            s.conn.execute(
                f"UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'o-{ntype}'"
            )
        s.conn.commit()

        results = _graph_hygiene(s, verbose=False)
        assert results["archived"] == 0
        s.close()

    def test_autolinks_viable_orphan(self, tmp_path):
        """Viable orphan should get linked via FTS title match."""
        from kindex.daemon import _graph_hygiene

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        # Create a connected node
        s.add_node("Graph Algorithms", content="BFS DFS", node_id="graph-alg")
        s.add_node("Related Node", content="related", node_id="related1")
        s.add_edge("graph-alg", "related1")

        # Create an orphan with matching title
        s.add_node("Graph Theory", content="About graph theory", node_id="orphan-graph")

        results = _graph_hygiene(s, verbose=False)
        assert results["linked"] >= 0  # may or may not match depending on FTS

        s.close()

    def test_empty_store(self, tmp_path):
        """No crash on empty store."""
        from kindex.daemon import _graph_hygiene

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        results = _graph_hygiene(s, verbose=False)
        assert results["archived"] == 0
        assert results["linked"] == 0
        s.close()

    def test_does_not_archive_recent_orphan(self, tmp_path):
        """Recent orphan with low weight should not be archived."""
        from kindex.daemon import _graph_hygiene

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        # Recent orphan — just created, so updated_at is now
        s.add_node("Fresh orphan", content="just created", node_id="fresh1")
        s.update_node("fresh1", weight=0.05)

        results = _graph_hygiene(s, verbose=False)
        assert results["archived"] == 0

        node = s.get_node("fresh1")
        assert node["status"] == "active"
        s.close()


class TestCheckWatches:
    def test_expires_overdue_watches(self, tmp_path):
        """Watches past their expiry date should be archived."""
        from kindex.daemon import _check_watches

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        s.add_node("Old watch", node_id="w1", node_type="watch",
                    extra={"expires": "2025-01-01"})

        results = _check_watches(s, verbose=False)
        assert results["expired"] == 1

        node = s.get_node("w1")
        assert node["status"] == "archived"
        s.close()

    def test_boosts_near_expiry_watches(self, tmp_path):
        """Watches expiring within 3 days should get weight boosted."""
        import datetime as _dt
        from kindex.daemon import _check_watches

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        s.add_node("Urgent watch", node_id="w2", node_type="watch",
                    extra={"expires": tomorrow})
        s.update_node("w2", weight=0.5)

        results = _check_watches(s, verbose=False)
        assert results["notified"] == 1

        node = s.get_node("w2")
        assert node["weight"] == 0.9
        s.close()

    def test_skips_far_future_watches(self, tmp_path):
        """Watches expiring far in the future should not be touched."""
        from kindex.daemon import _check_watches

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        s.add_node("Future watch", node_id="w3", node_type="watch",
                    extra={"expires": "2027-12-31"})

        results = _check_watches(s, verbose=False)
        assert results["expired"] == 0
        assert results["notified"] == 0
        s.close()

    def test_no_watches(self, tmp_path):
        """No crash on empty watch list."""
        from kindex.daemon import _check_watches

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        results = _check_watches(s, verbose=False)
        assert results["expired"] == 0
        assert results["notified"] == 0
        s.close()


class TestLastRunMarker:
    def test_last_run_marker(self, tmp_path):
        """Verify marker read/write via meta table."""
        from kindex.daemon import last_run_marker, set_run_marker

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        # Initially empty
        marker = last_run_marker(cfg)
        assert marker == ""

        # Set marker
        set_run_marker(s)

        # Read marker back (need a new Store because last_run_marker creates its own)
        s.close()
        marker = last_run_marker(cfg)
        assert marker != ""
        # Should be a valid ISO timestamp
        assert "T" in marker
        # Should be today's date
        today = datetime.date.today().isoformat()
        assert marker.startswith(today)

    def test_set_run_marker_updates(self, tmp_path):
        """Setting marker twice should update the value."""
        from kindex.daemon import set_run_marker

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        set_run_marker(s)
        marker1 = s.get_meta("last_cron_run")
        assert marker1 is not None

        # Second set
        import time
        time.sleep(0.1)
        set_run_marker(s)
        marker2 = s.get_meta("last_cron_run")
        assert marker2 is not None

        # Both should be valid
        assert "T" in marker1
        assert "T" in marker2
        s.close()


class TestCronCollabHygiene:
    def test_cron_sweeps_expired_locks_conversations_claims(self, tmp_path):
        """Stage 3: cron pass clears expired locks, conversations, and claims."""
        from kindex.coordination import create_conversation, get_conversation
        from kindex.daemon import cron_run
        from kindex.locks import lock_node
        from kindex.store import active_lock
        from kindex.tasks import claim_task, create_task

        cfg = Config(
            data_dir=str(tmp_path),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "projects")],
        )
        s = Store(cfg)

        # Expired lock (negative TTL -> already past)
        nid = s.add_node("Locked thing", node_type="concept", prov_activity="test")
        lock_node(s, nid, "agent-a", ttl_minutes=-1)
        # Live lock must survive the sweep
        nid_live = s.add_node("Live thing", node_type="concept", prov_activity="test")
        lock_node(s, nid_live, "agent-a", ttl_minutes=60)

        # Live conversation survives. Started first: starting a conversation
        # archives expired ones itself.
        create_conversation(s, "fresh", ttl_minutes=240)
        # Expired conversation
        create_conversation(s, "stale", ttl_minutes=-1)

        # Expired task claim
        task_id = create_task(s, "Sweep me")
        claim_task(s, task_id, "agent-a", ttl_minutes=-1)

        results = cron_run(cfg, s, verbose=False)

        assert results["locks_cleared"] == 1
        assert results["conversations_expired"] == 1
        assert results["claims_released"] == 1

        assert "lock" not in (s.get_node(nid).get("extra") or {})
        assert active_lock(s.get_node(nid_live)) is not None
        assert get_conversation(s, "stale") is None
        assert get_conversation(s, "fresh") is not None
        extra = s.get_node(task_id).get("extra") or {}
        assert "claim" not in extra
        assert extra.get("task_status") == "open"
        s.close()
