"""Acceptance oracles for state-resilience schema version 8.

These tests are authored from product P6, architecture A2, and strategy T3.
The version-7 fixture is test-owned SQL: it deliberately contains representative
legacy rows and only uses the migration hook declared by A2.
"""
from __future__ import annotations

import gc
import hashlib
import sqlite3
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store


_NODE_V8_FIELDS = {
    "verified_at", "verified_by", "prov_method", "valid_at", "invalid_at",
}
_RAW_CANDIDATE_FIELDS = {
    "title", "content", "node_type", "domains", "connections",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _assert_declared_v8_inventory(conn: sqlite3.Connection) -> None:
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    # >= 8: the declared v8 inventory must exist; later user-authorized
    # migrations (v9 referent binding, PRD lineage-grounding R0) advance the
    # stamp past it. Reverting to v7 still turns this red.
    assert version is not None and int(version[0]) >= 8
    assert _NODE_V8_FIELDS <= _columns(conn, "nodes")
    assert "updated_at" in _columns(conn, "edges")
    assert "kind" in _columns(conn, "suggestions")
    assert "capture_candidates" in _tables(conn)

    candidate_columns = _columns(conn, "capture_candidates")
    assert candidate_columns == {
        "id", "title", "content", "node_type", "domains", "connections",
        "source_digest", "payload_digest", "status", "created_at",
        "updated_at", "expires_at", "reviewed_at", "reviewed_by",
        "review_method", "disposition_code", "conflict_ids",
        "conflict_codes", "created_node_id",
    }

    index_rows = list(conn.execute("PRAGMA index_list(capture_candidates)"))
    indexed = []
    for row in index_rows:
        name = row[1]
        cols = tuple(
            info[2] for info in conn.execute(f"PRAGMA index_info({name})")
        )
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        indexed.append((cols, bool(row[2]), (sql_row[0] or "") if sql_row else ""))
    assert any(cols == ("status", "created_at") for cols, _, _ in indexed)
    assert any(cols == ("status", "expires_at") for cols, _, _ in indexed)
    assert any(
        cols == ("payload_digest",)
        and unique
        and "where" in sql.lower()
        and "pending" in sql.lower()
        and "conflicted" in sql.lower()
        for cols, unique, sql in indexed
    )

    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='capture_candidates'"
    ).fetchone()[0].lower()
    for status in ("pending", "conflicted", "accepted", "rejected", "expired"):
        assert status in table_sql
    assert "check" in table_sql


def _declared_v8_fingerprint(conn: sqlite3.Connection) -> tuple:
    """Fingerprint only schema elements frozen by A2, not legacy internals."""
    def field_rows(table: str, names: set[str]) -> tuple:
        return tuple(sorted(
            tuple(row[1:6])
            for row in conn.execute(f"PRAGMA table_info({table})")
            if row[1] in names
        ))

    candidate_columns = field_rows(
        "capture_candidates", _columns(conn, "capture_candidates")
    )
    candidate_indexes = []
    for row in conn.execute("PRAGMA index_list(capture_candidates)"):
        name = row[1]
        cols = tuple(
            info[2] for info in conn.execute(f"PRAGMA index_info({name})")
        )
        candidate_indexes.append((bool(row[2]), bool(row[4]), cols))
    return (
        field_rows("nodes", _NODE_V8_FIELDS),
        field_rows("edges", {"updated_at"}),
        field_rows("suggestions", {"kind"}),
        candidate_columns,
        tuple(sorted(candidate_indexes)),
    )


def _create_v7_fixture(root: Path) -> Path:
    """Create a complete-enough historical v7 database without app code."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kindex.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES ('schema_version', '7');

        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'concept',
            content TEXT NOT NULL DEFAULT '',
            aka TEXT NOT NULL DEFAULT '[]',
            domains TEXT NOT NULL DEFAULT '[]',
            weight REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'active',
            audience TEXT NOT NULL DEFAULT '[]',
            intent TEXT NOT NULL DEFAULT '',
            prov_who TEXT NOT NULL DEFAULT '[]',
            prov_when TEXT NOT NULL DEFAULT '',
            prov_why TEXT NOT NULL DEFAULT '',
            prov_source TEXT NOT NULL DEFAULT '',
            prov_activity TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            last_accessed TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_nodes_audience ON nodes(audience);
        CREATE INDEX idx_nodes_status ON nodes(status);
        CREATE INDEX idx_nodes_type ON nodes(type);
        CREATE INDEX idx_nodes_updated ON nodes(updated_at);
        CREATE INDEX idx_nodes_weight ON nodes(weight);

        CREATE TABLE edges (
            id TEXT PRIMARY KEY,
            from_id TEXT NOT NULL,
            to_id TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'relates_to',
            weight REAL NOT NULL DEFAULT 0.5,
            provenance TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_edges_from ON edges(from_id);
        CREATE INDEX idx_edges_to ON edges(to_id);

        CREATE TABLE suggestions (
            id TEXT PRIMARY KEY,
            concept_a TEXT NOT NULL,
            concept_b TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_suggestions_status ON suggestions(status);
        CREATE INDEX idx_suggestions_status_created
            ON suggestions(status, created_at);
        CREATE INDEX idx_suggestions_status_pair
            ON suggestions(status, concept_a, concept_b);

        CREATE TABLE reminders (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            reminder_type TEXT NOT NULL DEFAULT 'once',
            schedule TEXT NOT NULL DEFAULT '',
            next_due TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            status TEXT NOT NULL DEFAULT 'active',
            priority TEXT NOT NULL DEFAULT 'normal',
            channels TEXT NOT NULL DEFAULT '[]',
            snooze_until TEXT,
            snooze_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            last_fired TEXT,
            extra TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_reminders_next_due ON reminders(next_due);
        CREATE INDEX idx_reminders_priority ON reminders(priority);
        CREATE INDEX idx_reminders_status ON reminders(status);

        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target_id TEXT NOT NULL DEFAULT '',
            target_title TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '{}',
            actor TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL
        );
        CREATE INDEX idx_activity_action ON activity_log(action);
        CREATE INDEX idx_activity_timestamp ON activity_log(timestamp);

        CREATE TABLE injection_pheromone (
            node_id TEXT PRIMARY KEY,
            strength REAL NOT NULL DEFAULT 0,
            deposits INTEGER NOT NULL DEFAULT 0,
            reinforcements INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_pheromone_node ON injection_pheromone(node_id);
        CREATE INDEX idx_pheromone_strength ON injection_pheromone(strength);

        INSERT INTO nodes(
            id, title, type, content, domains, status, prov_when,
            created_at, updated_at, last_accessed
        ) VALUES (
            'legacy-node', 'Legacy node', 'concept', 'preserve me',
            '["legacy"]', 'active', '2025-01-01T00:00:00Z',
            '2025-01-01T00:00:00Z', '2025-01-02T00:00:00Z',
            '2025-01-03T00:00:00Z'
        );
        INSERT INTO nodes(
            id, title, type, content, created_at, updated_at, last_accessed
        ) VALUES (
            'legacy-peer', 'Legacy peer', 'concept', 'also preserve',
            '2025-01-01T00:00:00Z', '2025-01-02T00:00:00Z',
            '2025-01-03T00:00:00Z'
        );
        INSERT INTO edges(
            id, from_id, to_id, type, weight, provenance, created_at
        ) VALUES (
            'legacy-edge', 'legacy-node', 'legacy-peer', 'relates_to', 0.7,
            'legacy fixture', '2025-02-01T00:00:00Z'
        );
        INSERT INTO suggestions(
            id, concept_a, concept_b, reason, source, status,
            created_at, updated_at
        ) VALUES (
            'legacy-suggestion', 'legacy-node', 'legacy-peer', 'bridge them',
            'legacy fixture', 'pending', '2025-02-02T00:00:00Z',
            '2025-02-02T00:00:00Z'
        );
        INSERT INTO reminders(
            id, title, next_due, status, priority, created_at, updated_at
        ) VALUES (
            'legacy-reminder', 'Remember legacy', '2030-01-01T00:00:00Z',
            'active', 'high', '2025-02-03T00:00:00Z',
            '2025-02-03T00:00:00Z'
        );
        INSERT INTO activity_log(
            action, target_id, target_title, details, actor, timestamp
        ) VALUES (
            'legacy_action', 'legacy-node', 'Legacy node', '{}', 'fixture',
            '2025-02-04T00:00:00Z'
        );
        INSERT INTO injection_pheromone(
            node_id, strength, deposits, reinforcements, created_at, updated_at
        ) VALUES (
            'legacy-node', 1.25, 2, 1,
            '2025-02-05T00:00:00Z', '2025-02-05T00:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _assert_v7_rollback(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == "7"
        assert _NODE_V8_FIELDS.isdisjoint(_columns(conn, "nodes"))
        assert "updated_at" not in _columns(conn, "edges")
        assert "kind" not in _columns(conn, "suggestions")
        assert "capture_candidates" not in _tables(conn)
        assert conn.execute(
            "SELECT content FROM nodes WHERE id='legacy-node'"
        ).fetchone()[0] == "preserve me"
        assert conn.execute(
            "SELECT provenance FROM edges WHERE id='legacy-edge'"
        ).fetchone()[0] == "legacy fixture"
    finally:
        conn.close()


@pytest.mark.red_now
def test_p6_1_fresh_store_exposes_declared_v8_schema(tmp_path):
    """P6.1/A2: a fresh Store reaches SQLite metadata; v7/missing fields are
    forbidden and version 8 plus every declared field/check/index is demanded.
    Reverting SCHEMA_VERSION to 7 is the smallest reversion that turns red.
    """
    store = Store(Config(data_dir=str(tmp_path)))
    try:
        _assert_declared_v8_inventory(store.conn)
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO capture_candidates("
                "id, source_digest, payload_digest, status, created_at, "
                "updated_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                ("bad", "a" * 64, "b" * 64, "unknown",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                 "2026-01-02T00:00:00Z"),
            )
        store.conn.rollback()
    finally:
        store.close()


@pytest.mark.red_now
def test_p6_1_p6_4_real_v7_fixture_upgrades_and_preserves_legacy_rows(tmp_path):
    """P6.1/P6.4: the test-owned v7 file reaches migration with six legacy
    data classes; loss, implicit verification, or non-bridge backfill is
    forbidden, while v8 fields and byte-preserved values are demanded. Removing
    any backfill/preservation clause is the smallest mutation that turns red.
    """
    root = tmp_path / "upgrade"
    _create_v7_fixture(root)
    store = Store(Config(data_dir=str(root)))
    fresh = Store(Config(data_dir=str(tmp_path / "fresh-comparison")))
    try:
        _assert_declared_v8_inventory(store.conn)
        _assert_declared_v8_inventory(fresh.conn)
        assert _declared_v8_fingerprint(store.conn) == (
            _declared_v8_fingerprint(fresh.conn)
        )
        node = store.conn.execute(
            "SELECT content, verified_at, verified_by, prov_method, valid_at, "
            "invalid_at FROM nodes WHERE id='legacy-node'"
        ).fetchone()
        assert node is not None
        assert tuple(node) == ("preserve me", None, None, None, None, None)
        edge = store.conn.execute(
            "SELECT provenance, created_at, updated_at FROM edges "
            "WHERE id='legacy-edge'"
        ).fetchone()
        assert edge is not None
        assert tuple(edge) == (
            "legacy fixture", "2025-02-01T00:00:00Z",
            "2025-02-01T00:00:00Z",
        )
        suggestion = store.conn.execute(
            "SELECT reason, kind FROM suggestions "
            "WHERE id='legacy-suggestion'"
        ).fetchone()
        assert suggestion is not None
        assert tuple(suggestion) == ("bridge them", "bridge")
        assert store.conn.execute(
            "SELECT title FROM reminders WHERE id='legacy-reminder'"
        ).fetchone()[0] == "Remember legacy"
        assert store.conn.execute(
            "SELECT action FROM activity_log WHERE target_id='legacy-node'"
        ).fetchone()[0] == "legacy_action"
        assert store.conn.execute(
            "SELECT strength FROM injection_pheromone "
            "WHERE node_id='legacy-node'"
        ).fetchone()[0] == 1.25
    finally:
        fresh.close()
        store.close()


@pytest.mark.red_now
def test_p6_3_reopen_performs_no_migration_write(tmp_path):
    """P6.3: an upgraded fixture reaches a second Store open through the A2
    hook; any hook call or changed database bytes is forbidden and unchanged
    data is demanded. Removing the version-8 early return is the smallest
    mutation that turns this red.
    """
    root = tmp_path / "reopen"
    db = _create_v7_fixture(root)
    first = Store(Config(data_dir=str(root)))
    _ = first.conn
    first.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    calls: list[tuple[int, str]] = []
    second = Store(
        Config(data_dir=str(root)),
        migration_step_hook=lambda index, label: calls.append((index, label)),
    )
    _ = second.conn
    second.close()
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert calls == []
    assert after == before
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT content FROM nodes WHERE id='legacy-node'"
        ).fetchone()[0] == "preserve me"
    finally:
        conn.close()


@pytest.mark.red_now
def test_p6_2_failure_at_every_reported_migration_statement_is_atomic(tmp_path):
    """P6.2/A2: a successful probe makes every stable hook index reachable;
    partial v8 fields/stamps or lost legacy rows are forbidden after each
    injected BaseException, and the exact v7 state is demanded. Moving the
    version update outside BEGIN is the smallest mutation that turns this red.
    """
    probe_root = tmp_path / "probe"
    _create_v7_fixture(probe_root)
    steps: list[tuple[int, str]] = []
    probe = Store(
        Config(data_dir=str(probe_root)),
        migration_step_hook=lambda index, label: steps.append((index, label)),
    )
    _ = probe.conn
    probe.close()
    assert steps
    assert [index for index, _ in steps] == list(range(len(steps)))
    assert all(label and isinstance(label, str) for _, label in steps)

    class InjectedMigrationFailure(BaseException):
        pass

    for target, expected_label in steps:
        root = tmp_path / f"failure-{target}"
        db = _create_v7_fixture(root)
        reached = []

        def fail_at(index: int, label: str) -> None:
            reached.append((index, label))
            if index == target:
                assert label == expected_label
                raise InjectedMigrationFailure(label)

        failing = Store(
            Config(data_dir=str(root)), migration_step_hook=fail_at
        )
        with pytest.raises(InjectedMigrationFailure):
            _ = failing.conn
        del failing
        gc.collect()
        assert (target, expected_label) in reached
        _assert_v7_rollback(db)


@pytest.mark.red_now
def test_p6_2_migration_lock_error_is_visible_and_cannot_stamp_v8(tmp_path):
    """P6.2/A10: a real exclusive SQLite lock reaches Store migration;
    swallowed contention or a fake v8 stamp is forbidden, while an observable
    OperationalError and retained version 7 are demanded. Catching the lock
    error and continuing is the smallest mutation that turns red.
    """
    root = tmp_path / "locked"
    db = _create_v7_fixture(root)
    lock = sqlite3.connect(db)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        contender = Store(Config(data_dir=str(root)))
        with pytest.raises(sqlite3.OperationalError):
            _ = contender.conn
        del contender
        gc.collect()
    finally:
        lock.rollback()
        lock.close()
    _assert_v7_rollback(db)
