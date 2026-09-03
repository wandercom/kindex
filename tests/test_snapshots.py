"""Pre-merge DB snapshot stopgap (PRD lineage-grounding item 2).

Authority: docs/prd-lineage-grounding-2026-08.md, Review outcome point 4 —
an immediate pre-merge snapshot on automated destructive merges, because a
false merge in the (not-in-git) SQLite store is otherwise unrecoverable.
Each test names the mutation that would redden it (falsifiability ledger in
the test docstrings).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.snapshots import DEFAULT_KEEP, snapshot_db, snapshot_dir_for
from kindex.store import (
    SCHEMA_RECOVERY_PATH_META,
    SCHEMA_RECOVERY_REASON_META,
    SchemaMigrationError,
    Store,
    UnsupportedSchemaVersionError,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
    assert ("alpha",) in rows
    assert integrity == ("ok",)
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


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


def test_schema_migration_takes_and_records_pre_state_snapshot(
    store, tmp_path
):
    """Opening v11 preserves its schema/rows and repairs each project group."""
    store.add_node("Before migration", node_id="before-migration")
    store.add_node("Suggestion A", node_id="suggestion-a")
    store.add_node("Suggestion B", node_id="suggestion-b")
    store.add_suggestion(
        "suggestion-a",
        "suggestion-b",
        source="dream-cycle",
        identity_kind="node_id",
    )
    store.conn.execute("ALTER TABLE suggestions DROP COLUMN identity_kind")
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("DROP INDEX idx_suggestions_pair")
    for node_id, project in (
        ("a-old", "/project/a"),
        ("a-middle", "/project/a/../a"),
        ("a-new", "/project/a"),
        ("b-old", "/project/b"),
        ("b-new", "/project/b"),
    ):
        store.add_node(
            "shared-name",
            node_id=node_id,
            node_type="session",
            extra={
                "tag": "shared-name",
                "project_path": project,
                "session_status": "active",
            },
        )
    store.conn.execute(
        "UPDATE meta SET value = '11' WHERE key = 'schema_version'"
    )
    store.conn.commit()
    db_path = store.db_path
    store.close()

    migrated = Store(Config(data_dir=str(db_path.parent)))
    assert migrated.get_meta("schema_version") == "12"

    rows = migrated.conn.execute(
        "SELECT id, extra FROM nodes WHERE type = 'session' ORDER BY id"
    ).fetchall()
    session_state = {
        row["id"]: json.loads(row["extra"])
        for row in rows
    }
    assert sum(
        value["session_status"] == "active"
        for value in session_state.values()
    ) == 2
    assert {
        value["project_path"]
        for value in session_state.values()
        if value["session_status"] == "active"
    } == {"/project/a", "/project/b"}
    paused = [
        value for value in session_state.values()
        if value["session_status"] == "paused"
    ]
    assert len(paused) == 3
    assert all(
        value["paused_reason"] == "duplicate-active-session-migration-v12"
        for value in paused
    )
    assert {
        value["project_path"] for value in session_state.values()
    } == {"/project/a", "/project/b"}

    snapshots = list(
        (snapshot_dir_for(db_path) / "migrations").glob(
            "*schema-v11-to-v12.sqlite3"
        )
    )
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("11",)
        assert conn.execute(
            "SELECT title FROM nodes WHERE id = 'before-migration'"
        ).fetchone() == ("Before migration",)
        assert conn.execute(
            """SELECT count(*) FROM nodes
                 WHERE type = 'session'
                   AND json_extract(extra, '$.session_status') = 'active'"""
        ).fetchone() == (5,)

    entries = [
        entry for entry in migrated.recent_activity(20)
        if entry["action"] == "db_snapshot"
    ]
    assert str(snapshots[0]) in str(entries[0]["details"])
    assert migrated.get_meta(SCHEMA_RECOVERY_PATH_META) == str(snapshots[0])
    assert migrated.get_meta(SCHEMA_RECOVERY_REASON_META) == "schema-v11-to-v12"
    assert migrated.pending_suggestions()[0]["identity_kind"] == "node_id"
    lock_path = db_path.with_name(
        f".{db_path.name}.schema-migration-lock.sqlite3"
    )
    assert lock_path.is_file()
    with sqlite3.connect(lock_path) as lock_conn:
        assert lock_conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    if os.name == "posix":
        assert lock_path.stat().st_mode & 0o077 == 0
    migrated.close()


def test_schema_migration_refuses_when_snapshot_fails(store, tmp_path, monkeypatch):
    """No migration runs when its recovery snapshot cannot be created."""
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    store.conn.commit()
    db_path = store.db_path
    store.close()

    blocker = tmp_path / "not-a-state-directory"
    blocker.write_text("file")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    reopening = Store(Config(data_dir=str(db_path.parent)))
    with pytest.raises(SchemaMigrationError, match="snapshot failed"):
        _ = reopening.conn
    assert reopening._conn is None

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("11",)


def test_preversion_migration_refuses_before_creating_meta_when_snapshot_fails(
    tmp_path, monkeypatch
):
    """A v1-style store remains byte-semantically pre-versioned on refusal."""
    data_dir = tmp_path / "preversion"
    data_dir.mkdir()
    db_path = data_dir / "kindex.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE nodes "
            "(id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('legacy', 'Legacy node', 'concept')"
        )

    blocker = tmp_path / "not-a-state-root"
    blocker.write_text("file")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))

    reopening = Store(Config(data_dir=str(data_dir)))
    with pytest.raises(SchemaMigrationError, match="snapshot failed"):
        _ = reopening.conn
    assert reopening._conn is None
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'meta'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT title FROM nodes WHERE id = 'legacy'"
        ).fetchone() == ("Legacy node",)


def test_failed_v12_migration_rolls_back_and_reports_snapshot(store):
    """A failure at the version stamp rolls back row and index mutations."""
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("DROP INDEX idx_suggestions_pair")
    extra = json.dumps({
        "tag": "duplicate",
        "project_path": "/project",
        "session_status": "active",
    })
    store.conn.execute(
        "INSERT INTO nodes (id, type, title, extra) VALUES ('one', 'session', 'duplicate', ?)",
        (extra,),
    )
    store.conn.execute(
        "INSERT INTO nodes (id, type, title, extra) VALUES ('two', 'session', 'duplicate', ?)",
        (extra,),
    )
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    store.conn.execute(
        """CREATE TRIGGER fail_v12_stamp BEFORE UPDATE OF value ON meta
             WHEN OLD.key = 'schema_version' AND NEW.value = '12'
             BEGIN SELECT RAISE(ABORT, 'injected v12 failure'); END"""
    )
    store.conn.commit()
    db_path = store.db_path
    store.close()

    reopening = Store(Config(data_dir=str(db_path.parent)))
    with pytest.raises(
        SchemaMigrationError,
        match="recovery snapshot:",
    ) as error:
        _ = reopening.conn
    assert reopening._conn is None

    recovery_path = Path(str(error.value).split("recovery snapshot: ", 1)[1])
    assert recovery_path.is_file()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("11",)
        assert conn.execute(
            """SELECT count(*) FROM nodes
                 WHERE type = 'session'
                   AND json_extract(extra, '$.session_status') = 'active'"""
        ).fetchone() == (2,)
        assert conn.execute(
            """SELECT count(*) FROM sqlite_master
                 WHERE type = 'index'
                   AND name = 'idx_session_active_tag_project'"""
        ).fetchone() == (0,)
        recovery_path = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (SCHEMA_RECOVERY_PATH_META,),
        ).fetchone()[0]
        assert Path(recovery_path).is_file()


def test_newer_schema_is_refused_and_connection_is_closed(store):
    """An older Kindex build must not silently operate on a future schema."""
    store.conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
    store.conn.commit()
    db_path = store.db_path
    store.close()

    reopening = Store(Config(data_dir=str(db_path.parent)))
    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        _ = reopening.conn
    assert reopening._conn is None


def test_invalid_schema_stamp_has_recovery_guidance(store):
    """Corrupt metadata fails as a storage error, not a raw ValueError."""
    store.conn.execute(
        "UPDATE meta SET value = 'not-a-version' WHERE key = 'schema_version'"
    )
    store.conn.commit()
    db_path = store.db_path
    store.close()

    reopening = Store(Config(data_dir=str(db_path.parent)))
    with pytest.raises(
        SchemaMigrationError,
        match="invalid schema_version.*restore a validated",
    ):
        _ = reopening.conn
    assert reopening._conn is None


def test_invalid_snapshot_is_removed(store, tmp_path, monkeypatch):
    """A validation failure leaves no plausible-looking partial backup."""
    import kindex.snapshots as snapshots

    def _reject(*_args, **_kwargs):
        raise sqlite3.DatabaseError("injected invalid snapshot")

    monkeypatch.setattr(snapshots, "_validate_snapshot", _reject)
    target_root = tmp_path / "invalid-snapshots"

    with pytest.raises(sqlite3.DatabaseError, match="injected invalid"):
        snapshot_db(store, "invalid", state_dir=target_root)

    assert not list(target_root.rglob("*.sqlite3"))


def test_concurrent_openers_create_one_migration_snapshot(store, tmp_path):
    """A cross-platform SQLite waiter rechecks instead of migrating twice."""
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("DROP INDEX idx_suggestions_pair")
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    store.conn.commit()
    db_path = store.db_path
    store.close()

    script = """
import sys
import time
from kindex import snapshots
from kindex.config import Config
from kindex.store import Store

original = snapshots.snapshot_schema_migration

def slow_snapshot(*args, **kwargs):
    result = original(*args, **kwargs)
    time.sleep(0.6)
    return result

snapshots.snapshot_schema_migration = slow_snapshot
opened = Store(Config(data_dir=sys.argv[1]), sqlite_timeout=5.0)
assert opened.get_meta('schema_version') == '12'
opened.close()
"""
    first = subprocess.Popen([sys.executable, "-c", script, str(db_path.parent)])
    migration_dir = snapshot_dir_for(db_path) / "migrations"
    deadline = time.monotonic() + 5
    while not list(migration_dir.glob("*.sqlite3")):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    second = subprocess.Popen([sys.executable, "-c", script, str(db_path.parent)])

    assert first.wait(timeout=10) == 0
    assert second.wait(timeout=10) == 0
    assert len(list(migration_dir.glob("*.sqlite3"))) == 1


def test_concurrent_preversion_openers_lock_before_creating_meta(
    tmp_path, monkeypatch
):
    """Ancient no-meta stores take the same lock-and-recheck path."""
    state_root = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    data_dir = tmp_path / "preversion-race"
    data_dir.mkdir()
    db_path = data_dir / "kindex.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE nodes "
            "(id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL)"
        )

    script = """
import sys
import time
from kindex.config import Config
from kindex.store import Store

def slow_migration(self, _current):
    self._conn.execute('BEGIN IMMEDIATE')
    time.sleep(0.6)
    self._conn.execute(
        "UPDATE meta SET value = '12' WHERE key = 'schema_version'"
    )
    self._conn.commit()

Store._migrate_schema = slow_migration
opened = Store(Config(data_dir=sys.argv[1]), sqlite_timeout=5.0)
assert opened.get_meta('schema_version') == '12'
opened.close()
"""
    first = subprocess.Popen([sys.executable, "-c", script, str(data_dir)])
    migration_dir = (
        snapshot_dir_for(db_path, state_root / "kindex" / "snapshots")
        / "migrations"
    )
    deadline = time.monotonic() + 5
    while not list(migration_dir.glob("*.sqlite3")):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    second = subprocess.Popen([sys.executable, "-c", script, str(data_dir)])

    assert first.wait(timeout=10) == 0
    assert second.wait(timeout=10) == 0
    assert len(list(migration_dir.glob("*.sqlite3"))) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("12",)


def test_changelog_failure_does_not_misreport_completed_migration(store):
    """Durable meta remains authoritative if convenience logging breaks."""
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("DROP INDEX idx_suggestions_pair")
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    store.conn.execute(
        """CREATE TRIGGER reject_snapshot_log
             BEFORE INSERT ON activity_log
             WHEN NEW.action = 'db_snapshot'
             BEGIN SELECT RAISE(ABORT, 'injected log failure'); END"""
    )
    store.conn.commit()
    db_path = store.db_path
    store.close()

    migrated = Store(Config(data_dir=str(db_path.parent)))
    assert migrated.get_meta("schema_version") == "12"
    assert Path(migrated.get_meta(SCHEMA_RECOVERY_PATH_META)).is_file()
    migrated.close()


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
