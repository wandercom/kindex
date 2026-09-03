"""Tests for the slow graph archive system."""

from __future__ import annotations

import datetime

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def setup(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    yield cfg, store
    store.close()


class TestFindArchivable:
    def test_finds_archived_low_weight_old_nodes(self, setup):
        from kindex.archive import find_archivable_nodes

        cfg, store = setup
        store.add_node("Old concept", content="stale", node_id="old1", node_type="concept")
        store.update_node("old1", weight=0.02, status="archived")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'old1'"
        )
        store.conn.commit()

        ids = find_archivable_nodes(store)
        assert "old1" in ids

    def test_skips_active_nodes(self, setup):
        from kindex.archive import find_archivable_nodes

        cfg, store = setup
        store.add_node("Active concept", content="fresh", node_id="active1")
        store.update_node("active1", weight=0.02)
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'active1'"
        )
        store.conn.commit()

        ids = find_archivable_nodes(store)
        assert "active1" not in ids  # status is still 'active'

    def test_skips_active_session_types(self, setup):
        from kindex.archive import find_archivable_nodes

        cfg, store = setup
        store.add_node("Session node", node_id="sess1", node_type="session")
        store.update_node("sess1", weight=0.02, status="archived")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'sess1'"
        )
        store.conn.commit()

        ids = find_archivable_nodes(store)
        assert "sess1" not in ids

    def test_finds_completed_unlinked_session(self, setup):
        from kindex.archive import find_archivable_nodes
        from kindex.sessions import complete_tag, start_tag

        cfg, store = setup
        session_id = start_tag(store, "finished-session")
        complete_tag(store, "finished-session")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = ?",
            (session_id,),
        )
        store.conn.commit()

        assert session_id in find_archivable_nodes(store)

    def test_skips_paused_session(self, setup):
        from kindex.archive import find_archivable_nodes
        from kindex.sessions import pause_tag, start_tag

        cfg, store = setup
        session_id = start_tag(store, "paused-session")
        pause_tag(store, "paused-session")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = ?",
            (session_id,),
        )
        store.conn.commit()

        assert session_id not in find_archivable_nodes(store)

    def test_skips_completed_session_with_linked_knowledge(self, setup):
        from kindex.archive import find_archivable_nodes
        from kindex.sessions import complete_tag, link_node_to_tag, start_tag

        cfg, store = setup
        session_id = start_tag(store, "linked-session")
        node_id = store.add_node("Session knowledge")
        link_node_to_tag(store, "linked-session", node_id)
        complete_tag(store, "linked-session")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = ?",
            (session_id,),
        )
        store.conn.commit()

        assert session_id not in find_archivable_nodes(store)

    def test_empty_store(self, setup):
        from kindex.archive import find_archivable_nodes

        cfg, store = setup
        ids = find_archivable_nodes(store)
        assert ids == []


class TestArchiveNodes:
    def test_moves_node_to_archive(self, setup):
        from kindex.archive import archive_nodes, _current_archive_path, _open_archive

        cfg, store = setup
        store.add_node("To archive", content="old data", node_id="arc1",
                        node_type="concept")
        store.add_node("Connected", content="linked", node_id="conn1")
        store.add_edge("arc1", "conn1", weight=0.5)

        count = archive_nodes(cfg, store, ["arc1"])
        assert count == 1

        # Node should be gone from fast graph
        assert store.get_node("arc1") is None

        # Should be in archive
        import sqlite3
        archive_path = _current_archive_path(cfg)
        assert archive_path.exists()
        conn = sqlite3.connect(str(archive_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM archived_nodes WHERE id='arc1'").fetchone()
        assert row is not None
        assert row["title"] == "To archive"

        # Edge should be archived too
        edges = conn.execute(
            "SELECT * FROM archived_edges WHERE from_id='arc1'"
        ).fetchall()
        assert len(edges) >= 1
        conn.close()

    def test_empty_list(self, setup):
        from kindex.archive import archive_nodes

        cfg, store = setup
        count = archive_nodes(cfg, store, [])
        assert count == 0


class TestArchiveCycle:
    def test_full_cycle(self, setup):
        from kindex.archive import archive_cycle

        cfg, store = setup
        # Create an archivable node
        store.add_node("Stale concept", content="old", node_id="stale1")
        store.update_node("stale1", weight=0.02, status="archived")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2025-01-01T00:00:00' WHERE id = 'stale1'"
        )
        store.conn.commit()

        count = archive_cycle(cfg, store)
        assert count == 1
        assert store.get_node("stale1") is None

    def test_nothing_to_archive(self, setup):
        from kindex.archive import archive_cycle

        cfg, store = setup
        count = archive_cycle(cfg, store)
        assert count == 0


class TestListArchives:
    def test_lists_archive_files(self, setup):
        from kindex.archive import archive_nodes, list_archives

        cfg, store = setup
        store.add_node("Archived", content="data", node_id="a1")
        archive_nodes(cfg, store, ["a1"])

        archives = list_archives(cfg)
        assert len(archives) >= 1
        assert archives[0]["nodes"] >= 1

    def test_empty_archive_dir(self, setup):
        from kindex.archive import list_archives

        cfg, store = setup
        archives = list_archives(cfg)
        assert archives == []


class TestSearchArchives:
    def test_finds_by_title(self, setup):
        from kindex.archive import archive_nodes, search_archives

        cfg, store = setup
        store.add_node("Graph Theory Basics", content="about graphs", node_id="gt1")
        archive_nodes(cfg, store, ["gt1"])

        results = search_archives(cfg, "Graph Theory")
        assert len(results) >= 1
        assert results[0]["title"] == "Graph Theory Basics"

    def test_no_results(self, setup):
        from kindex.archive import search_archives

        cfg, store = setup
        results = search_archives(cfg, "nonexistent")
        assert results == []


class TestRestoreNode:
    def test_restore_to_fast_graph(self, setup):
        from kindex.archive import archive_nodes, restore_node

        cfg, store = setup
        store.add_node("Restorable", content="important data", node_id="r1",
                        node_type="concept", domains=["test"])
        archive_nodes(cfg, store, ["r1"])

        # Verify it's gone
        assert store.get_node("r1") is None

        # Restore
        ok = restore_node(cfg, store, "r1")
        assert ok

        # Verify it's back
        node = store.get_node("r1")
        assert node is not None
        assert node["title"] == "Restorable"
        assert node["status"] == "active"
        assert node["weight"] == 0.3

    def test_restore_nonexistent(self, setup):
        from kindex.archive import restore_node

        cfg, store = setup
        ok = restore_node(cfg, store, "nonexistent")
        assert not ok

    def test_restore_recovers_edges(self, setup):
        from kindex.archive import archive_nodes, restore_node

        cfg, store = setup
        store.add_node("Node A", node_id="a1")
        store.add_node("Node B", node_id="b1")
        store.add_edge("a1", "b1", weight=0.5)

        # Archive a1 (b1 stays in fast graph)
        archive_nodes(cfg, store, ["a1"])

        # Restore a1
        ok = restore_node(cfg, store, "a1")
        assert ok

        # Edge should be restored since b1 is still in fast graph
        edges = store.edges_from("a1")
        assert len(edges) >= 1

    def test_restore_preserves_completed_session_history(self, setup):
        from kindex.archive import archive_nodes, restore_node
        from kindex.sessions import complete_tag, start_tag

        cfg, store = setup
        session_id = start_tag(store, "session-history", focus="Done work")
        complete_tag(store, "session-history", summary="Finished")
        archive_nodes(cfg, store, [session_id])

        assert restore_node(cfg, store, session_id)
        restored = store.get_node(session_id)
        assert restored["type"] == "session"
        assert restored["extra"]["session_status"] == "completed"
        assert restored["extra"]["segments"][0]["summary"] == "Finished"


class TestRotation:
    def test_rotation_by_size(self, setup, tmp_path):
        from kindex.archive import (
            _current_archive_path, _open_archive, _should_rotate, _rotate_archive
        )

        cfg, store = setup
        # Create an archive and check it doesn't need rotation (too small)
        path = _current_archive_path(cfg)
        _open_archive(path).close()
        assert not _should_rotate(path, max_size_mb=50)

        # Force rotation check with tiny threshold
        assert _should_rotate(path, max_size_mb=0.0001)

        # Actually rotate
        rotated = _rotate_archive(cfg)
        assert rotated is not None
        assert "archive_" in rotated.name
        assert not path.exists()  # current.db should be gone
        assert rotated.exists()

    def test_no_rotation_needed_on_empty(self, setup):
        from kindex.archive import _current_archive_path, _should_rotate

        cfg, store = setup
        path = _current_archive_path(cfg)
        # File doesn't exist yet
        assert not _should_rotate(path)
