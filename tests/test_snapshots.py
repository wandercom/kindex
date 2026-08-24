"""Pre-merge DB snapshot stopgap (PRD lineage-grounding item 2).

Authority: docs/prd-lineage-grounding-2026-08.md, Review outcome point 4 —
an immediate pre-merge snapshot on automated destructive merges, because a
false merge in the (not-in-git) SQLite store is otherwise unrecoverable.
Each test names the mutation that would redden it (falsifiability ledger in
the test docstrings).
"""

from __future__ import annotations

import sqlite3

import pytest

from kindex.config import Config
from kindex.snapshots import DEFAULT_KEEP, snapshot_db, snapshot_dir_for
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    s = Store(cfg)
    yield s
    s.close()


def _snap_dir(store, tmp_path):
    return snapshot_dir_for(store.db_path, tmp_path / "snaps")


def test_snapshot_is_a_valid_pre_state_copy(store, tmp_path):
    """Snapshot is a readable SQLite DB holding the current node set.

    Mutation that reddens this: snapshotting the wrong file / skipping the
    backup call leaves an unreadable or empty DB.
    """
    store.add_node("Alpha", node_id="alpha")
    path = snapshot_db(store, "unit-test", state_dir=tmp_path / "snaps")
    assert path.exists()
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT id FROM nodes").fetchall()
    assert ("alpha",) in rows


def test_snapshot_rotation_keeps_newest(store, tmp_path):
    """Twelve snapshots leave exactly DEFAULT_KEEP, the newest ones.

    Mutation that reddens this: dropping the _rotate call leaves 12 files.
    """
    for i in range(12):
        snapshot_db(store, f"r{i:02d}", state_dir=tmp_path / "snaps")
    remaining = sorted(p.name for p in _snap_dir(store, tmp_path).glob("*.sqlite3"))
    assert len(remaining) == DEFAULT_KEEP
    # Newest survive: the last two reasons written are still present.
    assert any("r11" in n for n in remaining)
    assert any("r10" in n for n in remaining)
    assert not any("r00" in n for n in remaining)


def test_snapshot_failure_raises_not_swallows(store, tmp_path):
    """A failed snapshot raises so callers can fail closed.

    Mutation that reddens this: wrapping snapshot_db's body in a
    swallow-and-return-None turns this into no exception.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    with pytest.raises(Exception):
        snapshot_db(store, "x", state_dir=blocker)


def test_snapshot_records_changelog_entry(store, tmp_path):
    """A db_snapshot activity entry (kin changelog surface) carries the path.

    Mutation that reddens this: removing the _log call.
    """
    path = snapshot_db(store, "audit-test", state_dir=tmp_path / "snaps")
    entries = [e for e in store.recent_activity(20) if e["action"] == "db_snapshot"]
    assert entries, "db_snapshot entry missing from activity log"
    assert str(path) in str(entries[0].get("details"))


# ── dream auto-merge wiring ─────────────────────────────────────────────


def _add_duplicate_pair(store):
    a = store.add_node("Duplicate concept pattern", content="same text body",
                       node_id="dup-a")
    b = store.add_node("Duplicate concept pattern", content="same text body",
                       node_id="dup-b")
    return a, b


def test_dream_merge_snapshots_pre_merge_state(store, tmp_path, monkeypatch):
    """dream_lightweight snapshots BEFORE merging: the snapshot still holds
    the absorbed node as active while the live DB has archived it.

    Mutation that reddens this: moving the snapshot call after the merge
    loop makes the snapshot contain the post-merge (archived) state.
    """
    from kindex.dream import dream_lightweight

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    _add_duplicate_pair(store)
    results = dream_lightweight(store.config, store, dry_run=False)
    assert results["merged"] == 1
    assert results["merges_skipped_unprotected"] == 0

    snap_root = tmp_path / "xdg" / "kindex" / "snapshots"
    snaps = list(snap_root.glob("*/*.sqlite3"))
    assert len(snaps) == 1, "exactly one pre-merge snapshot expected"

    # Live DB: one of the pair is archived now.
    live_status = {
        n["id"]: n["status"]
        for n in (store.get_node("dup-a"), store.get_node("dup-b"))
    }
    assert "archived" in live_status.values()
    # Snapshot: both still active (pre-merge state — the recoverability proof).
    with sqlite3.connect(snaps[0]) as conn:
        rows = dict(conn.execute(
            "SELECT id, status FROM nodes WHERE id IN ('dup-a', 'dup-b')"
        ).fetchall())
    assert rows == {"dup-a": "active", "dup-b": "active"}


def test_dream_dry_run_and_no_pairs_take_no_snapshot(store, tmp_path, monkeypatch):
    """No merge work -> no snapshot; dry_run -> no snapshot.

    Mutation that reddens this: snapshotting unconditionally.
    """
    from kindex.dream import dream_lightweight

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    snap_root = tmp_path / "xdg" / "kindex" / "snapshots"

    store.add_node("Solo unique concept", node_id="solo")
    dream_lightweight(store.config, store, dry_run=False)
    assert not list(snap_root.glob("*/*.sqlite3"))

    _add_duplicate_pair(store)
    results = dream_lightweight(store.config, store, dry_run=True)
    assert results["merged"] == 1  # would-merge counted
    assert not list(snap_root.glob("*/*.sqlite3"))


def test_dream_merge_fails_closed_when_snapshot_impossible(store, tmp_path, monkeypatch):
    """Snapshot failure skips the auto-merges instead of merging unprotected.

    Mutation that reddens this: ignoring the snapshot error (fail-open)
    archives one of the pair anyway.
    """
    from kindex.dream import dream_lightweight

    blocker = tmp_path / "xdg-blocked"
    blocker.write_text("file, not dir")  # mkdir(parents=True) will fail
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    _add_duplicate_pair(store)
    results = dream_lightweight(store.config, store, dry_run=False)
    assert results["merged"] == 0
    assert results["merges_skipped_unprotected"] == 1
    assert store.get_node("dup-a")["status"] == "active"
    assert store.get_node("dup-b")["status"] == "active"


# ── graph_merge MCP wiring ──────────────────────────────────────────────


def _patch_mcp(store, monkeypatch):
    pytest.importorskip("mcp", reason="mcp not installed")
    import kindex.mcp_server as mcp_mod
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)


def test_graph_merge_snapshots_before_merging(store, tmp_path, monkeypatch):
    """graph_merge takes a pre-merge snapshot and names it in its output.

    Mutation that reddens this: removing the snapshot call from graph_merge.
    """
    from kindex.mcp_server import graph_merge

    _patch_mcp(store, monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store.add_node("Merge source", node_id="src")
    store.add_node("Merge target", node_id="tgt")
    result = graph_merge("src", "tgt")
    assert "Merged" in result
    assert "snapshot" in result.lower()
    snaps = list((tmp_path / "xdg" / "kindex" / "snapshots").glob("*/*.sqlite3"))
    assert len(snaps) == 1
    with sqlite3.connect(snaps[0]) as conn:
        row = conn.execute(
            "SELECT status FROM nodes WHERE id='src'").fetchone()
    assert row == ("active",)  # pre-merge state preserved


def test_graph_merge_refuses_without_snapshot(store, tmp_path, monkeypatch):
    """Fail-closed: snapshot failure refuses the merge, nodes untouched.

    Mutation that reddens this: proceeding on snapshot failure archives src.
    """
    from kindex.mcp_server import graph_merge

    _patch_mcp(store, monkeypatch)
    blocker = tmp_path / "xdg-blocked"
    blocker.write_text("file, not dir")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    store.add_node("Merge source", node_id="src")
    store.add_node("Merge target", node_id="tgt")
    result = graph_merge("src", "tgt")
    assert result.startswith("Error: pre-merge DB snapshot failed")
    assert store.get_node("src")["status"] == "active"
    assert store.get_node("tgt")["status"] == "active"
