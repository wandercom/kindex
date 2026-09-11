"""FTS diagnostics must release locks and never report an unverified repair."""

import argparse
import json
import sqlite3

import pytest

from kindex import cli
from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=str(tmp_path)))
    value.add_node("Alpha", "originalneedle", node_id="alpha")
    value.add_node("Beta", "betaneedle", node_id="beta")
    value.add_edge("alpha", "beta", bidirectional=True)
    yield value
    value.close()


def remove_postings(store):
    store.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
    store.conn.commit()


@pytest.mark.parametrize("corrupt", [False, True])
def test_integrity_check_preserves_pending_transaction(store, corrupt):
    if corrupt:
        remove_postings(store)
    store.conn.execute("INSERT INTO meta(key,value) VALUES('pending-test','yes')")
    if corrupt:
        with pytest.raises(sqlite3.DatabaseError):
            store.check_fts_integrity()
    else:
        store.check_fts_integrity()
    assert store.conn.in_transaction
    assert store.get_meta("pending-test") == "yes"
    store.conn.rollback()
    assert store.get_meta("pending-test") is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_integrity_check_releases_writer_lock(store, corrupt):
    if corrupt:
        remove_postings(store)
        with pytest.raises(sqlite3.DatabaseError):
            store.check_fts_integrity()
    else:
        store.check_fts_integrity()
    assert not store.conn.in_transaction
    with sqlite3.connect(store.db_path, timeout=0) as other:
        other.execute("INSERT INTO meta(key,value) VALUES('another-writer','yes')")


def test_failed_post_rebuild_validation_rolls_back_and_reports_failure(store, monkeypatch, capsys):
    remove_postings(store)
    real_check = store.check_fts_integrity
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.DatabaseError("injected post-rebuild integrity failure")
        return real_check()

    monkeypatch.setattr(store, "check_fts_integrity", check)
    monkeypatch.setattr(cli, "_store", lambda args: store)
    monkeypatch.setenv("KIN_PROFILE", "unregistered-doctor-test-profile")
    monkeypatch.setattr(cli, "_config", lambda args: store.config)
    args = argparse.Namespace(json=True, fix=True, data_dir=str(store.db_path.parent))
    cli.cmd_doctor(args)
    report = json.loads(capsys.readouterr().out)
    assert calls == 2
    assert not report["healthy"]
    assert report["fixes_applied"] == 0
    assert any("FIX FAILED" in issue for issue in report["issues"])
    assert not any("FIXED" in issue for issue in report["issues"])
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'originalneedle'"
        ).fetchall() == []


def test_interrupted_check_preserves_original_sqlite_error(store):
    statements = []

    def trace(statement):
        statements.append(statement)
        if "VALUES('integrity-check', 1)" in statement:
            # Older SQLite versions need the handler to remain armed until
            # the interruption propagates out of the FTS virtual table.
            store.conn.set_progress_handler(lambda: 1, 1)

    store.conn.set_trace_callback(trace)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            store.check_fts_integrity()
    finally:
        store.conn.set_trace_callback(None)
        store.conn.set_progress_handler(None, 0)
    assert not store.conn.in_transaction
    # A still-armed handler can also interrupt erroneous cleanup, hiding the
    # missing-savepoint regression behind another "interrupted" exception.
    assert not any(sql.startswith(("ROLLBACK TO", "RELEASE")) for sql in statements)
