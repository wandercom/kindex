"""Regression tests for the v10 migration and schema-drift detection.

The defect these cover: `injection_pheromone.missed` was added to the v7
block's `CREATE TABLE IF NOT EXISTS` after v7 had shipped. For a store that
ran v7 before that edit the CREATE is a no-op, the column never arrives, and
`schema_version` still reads current — so every `deposit_pheromone` call
raised `no such column: missed`, the attention hook swallowed it, and the
whole stigmergic channel was silently dead for three months.

The existing pheromone tests all build fresh stores, so none of them could
see it. These start from the *old* table shape on purpose.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from kindex.attention import _deposit_injection_pheromone
from kindex.config import Config
from kindex.schema import SCHEMA_VERSION
from kindex.store import Store

# The v7 table exactly as it shipped before `missed` was added.
V7_PHEROMONE_TABLE = """
CREATE TABLE injection_pheromone (
    node_id TEXT NOT NULL REFERENCES nodes(id),
    context TEXT NOT NULL DEFAULT '',
    strength REAL NOT NULL DEFAULT 0.0,
    deposits INTEGER NOT NULL DEFAULT 0,
    reinforcements INTEGER NOT NULL DEFAULT 0,
    last_deposit TEXT NOT NULL DEFAULT (datetime('now')),
    last_decay TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (node_id, context)
);
"""


def _store_with_pre_v10_pheromone(tmp_path) -> Store:
    """A store whose pheromone table predates the `missed` column.

    Built by creating a normal store, dropping the table, recreating it in the
    old shape, and winding `schema_version` back to 9 — the exact state of
    every install that ran v7 before the column was added.
    """
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.executescript(V7_PHEROMONE_TABLE)
    store.conn.execute(
        "UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    store.conn.commit()
    store.close()
    return Store(Config(data_dir=str(tmp_path)))


def test_pre_v10_store_is_missing_the_column(tmp_path):
    """Guard the fixture itself: without reopening, the column really is gone.

    If this ever stops holding, every other test in this file becomes vacuous.
    """
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.executescript(V7_PHEROMONE_TABLE)
    store.conn.commit()
    cols = {r["name"] for r in
            store.conn.execute("PRAGMA table_info(injection_pheromone)")}
    assert "missed" not in cols
    nid = store.add_node("Node A")
    with pytest.raises(sqlite3.OperationalError, match="missed"):
        store.deposit_pheromone(nid)
    store.close()


def test_v10_migration_adds_missed_column(tmp_path):
    store = _store_with_pre_v10_pheromone(tmp_path)
    cols = {r["name"] for r in
            store.conn.execute("PRAGMA table_info(injection_pheromone)")}
    assert "missed" in cols
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.close()


def test_deposit_works_after_migration(tmp_path):
    """The actual user-visible symptom: deposits land instead of vanishing."""
    store = _store_with_pre_v10_pheromone(tmp_path)
    nid = store.add_node("Node A")
    assert store.deposit_pheromone(nid, amount=1.0) == 1.0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM injection_pheromone").fetchone()[0] == 1
    store.close()


def test_migration_is_idempotent_on_a_fresh_store(tmp_path):
    """A store created after the CREATE was fixed already has the column.

    ALTER TABLE would raise duplicate-column there, so v10 must check first —
    and reopening must stay a no-op.
    """
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    nid = store.add_node("Node A")
    store.deposit_pheromone(nid, amount=1.0)
    store.close()

    reopened = Store(Config(data_dir=str(tmp_path)))
    assert reopened.schema_drift() == {}
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM injection_pheromone").fetchone()[0] == 1
    reopened.close()


def test_schema_drift_reports_missing_columns(tmp_path):
    """Drift detection must see a wrong-shape table, not just a missing one."""
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.executescript(V7_PHEROMONE_TABLE)
    store.conn.commit()

    drift = store.schema_drift()
    assert "injection_pheromone" in drift
    assert drift["injection_pheromone"] == {"missed"}
    store.close()


def test_schema_drift_clean_on_current_store(tmp_path):
    store = Store(Config(data_dir=str(tmp_path)))
    assert store.schema_drift() == {}
    store.close()


def test_absent_table_is_not_drift(tmp_path):
    """A table that does not exist is a migration's job, not drift."""
    store = Store(Config(data_dir=str(tmp_path)))
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.commit()
    assert "injection_pheromone" not in store.schema_drift()
    store.close()


def test_failed_deposit_is_logged_and_counted(tmp_path, caplog):
    """The recovery path must emit a signal, never route to silence."""
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.executescript(V7_PHEROMONE_TABLE)
    store.conn.commit()
    nid = store.add_node("Node A")

    with caplog.at_level(logging.WARNING, logger="kindex.attention"):
        ok = _deposit_injection_pheromone(store, cfg, f"node:{nid}", "proj")

    assert ok is False
    assert "pheromone deposit failed" in caplog.text
    assert store.get_meta("pheromone.deposit_failures") == "1"
    store.close()


def test_successful_deposit_reports_true(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    nid = store.add_node("Node A")

    assert _deposit_injection_pheromone(store, cfg, f"node:{nid}", "proj") is True
    # Global trail plus the conditioned trail.
    assert store.conn.execute(
        "SELECT COUNT(*) FROM injection_pheromone").fetchone()[0] == 2
    assert store.get_meta("pheromone.deposit_failures") is None
    store.close()


def test_reminder_ids_are_not_deposits(tmp_path):
    """Reminders are ephemeral and are not graph nodes — no trail, no failure."""
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    assert _deposit_injection_pheromone(store, cfg, "reminder:abc", "proj") is False
    assert store.get_meta("pheromone.deposit_failures") is None
    store.close()


def test_state_records_only_deposits_the_store_accepted(tmp_path):
    """State that claims a deposit the store refused is how this stayed hidden.

    Phase 2 reinforcement reads `pheromone_deposits` as ground truth, so a
    phantom entry there would grade an injection that laid no trail.
    """
    from kindex.attention import AttentionInjection, _load_state, _record_attention_delivery

    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    store.conn.execute("DROP TABLE IF EXISTS injection_pheromone")
    store.conn.executescript(V7_PHEROMONE_TABLE)
    store.conn.commit()
    nid = store.add_node("Node A")

    injection = AttentionInjection(id=f"node:{nid}", title="Node A", message="x",
                                   reason="test", confidence=1.0)
    _record_attention_delivery(store, cfg, "conv-1", [injection])

    state = _load_state(store, "conv-1")
    assert state.get("pheromone_deposits", {}) == {}
    # The delivery itself still happened — only the trail claim is withheld.
    assert injection.id in state.get("injected", {})
    store.close()
