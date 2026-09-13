"""Doctor must validate actual FTS postings against all canonical v12 rows."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def graph(tmp_path, monkeypatch):
    """An isolated connected graph; doctor runs in a separate CLI process."""
    home = tmp_path / "home"
    home.mkdir()
    for name, path in {
        "HOME": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "XDG_STATE_HOME": home / "state",
        "XDG_CACHE_HOME": home / "cache",
    }.items():
        monkeypatch.setenv(name, str(path))
    data_dir = tmp_path / "graph"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("Alpha", "originalneedle canonical body", node_id="alpha")
    store.add_node("Beta", "betaneedle canonical body", node_id="beta")
    store.add_edge("alpha", "beta", bidirectional=True)
    assert store.get_meta("schema_version") == "13"
    db_path = store.db_path
    store.close()

    def doctor(*args):
        # Whitelist the child environment: no ambient profiles, provider keys,
        # project selection, reminder callbacks, or user config can leak in.
        env = {name: os.environ[name] for name in (
            "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
            "XDG_CACHE_HOME", "PATH", "SYSTEMROOT",
        ) if name in os.environ}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-m", "kindex.cli", "doctor", "--json",
             "--data-dir", str(data_dir), *args],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return data_dir, db_path, doctor


def fts_issues(report):
    return [item for item in report["issues"] if "FTS5" in item]


def matches(db_path, token):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (token,))}


def canonical_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()


def test_v12_mixed_populations_do_not_request_rebuild(graph):
    data_dir, db_path, doctor = graph
    with closing(Store(Config(data_dir=str(data_dir)))) as store:
        for node_id, node_type, status in [
            ("session", "session", "active"),
            ("ignored", "concept", "ignored"),
            ("archived", "concept", "archived"),
            ("superseded", "concept", "superseded"),
            ("deprecated", "concept", "deprecated"),
        ]:
            store.add_node(node_id, f"{node_id}needle body", node_id=node_id,
                           node_type=node_type, status=status)
            store.add_edge("alpha", node_id, bidirectional=True)
    before = canonical_rows(db_path)
    for report in (doctor(), doctor("--fix"), doctor()):
        assert not fts_issues(report), report
        assert report["healthy"], report
        assert report["fixes_applied"] == 0
    assert canonical_rows(db_path) == before
    for token in ("sessionneedle", "ignoredneedle", "archivedneedle",
                  "supersededneedle", "deprecatedneedle"):
        assert len(matches(db_path, token)) == 1


@pytest.mark.parametrize("mixed", [False, True], ids=["semantic", "mixed"])
@pytest.mark.parametrize("damage", ["missing", "extra", "equal_count_swap", "stale_text"])
def test_doctor_detects_and_repairs_postings_corruption(graph, damage, mixed):
    data_dir, db_path, doctor = graph
    if mixed:
        with closing(Store(Config(data_dir=str(data_dir)))) as store:
            store.add_node("Session", "sessionneedle body", node_id="session",
                           node_type="session")
    assert doctor()["healthy"]
    before = canonical_rows(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT rowid, id, title, content, aka, intent, domains "
            "FROM nodes WHERE id='alpha'"
        ).fetchone()
        if damage in ("missing", "equal_count_swap", "stale_text"):
            conn.execute(
                "INSERT INTO nodes_fts(nodes_fts,rowid,id,title,content,aka,intent,domains) "
                "VALUES('delete',?,?,?,?,?,?,?)", row,
            )
        if damage in ("extra", "equal_count_swap"):
            conn.execute(
                "INSERT INTO nodes_fts(rowid,id,title,content,aka,intent,domains) "
                "VALUES(999999,'ghost','Ghost','ghostneedle','','','')"
            )
        if damage == "stale_text":
            stale = list(row)
            stale[3] = "staleneedle obsolete body"
            conn.execute(
                "INSERT INTO nodes_fts(rowid,id,title,content,aka,intent,domains) "
                "VALUES(?,?,?,?,?,?,?)", stale,
            )
        # External-content COUNT reads the canonical table, even when actual
        # posting membership is missing, extra, swapped, or text is stale.
        assert conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == (3 if mixed else 2)
    if damage != "extra":
        assert not matches(db_path, "originalneedle")
    if damage in ("extra", "equal_count_swap"):
        assert matches(db_path, "ghostneedle") == {999999}
    report = doctor()
    assert fts_issues(report), report
    assert not report["healthy"]
    assert canonical_rows(db_path) == before
    repaired = doctor("--fix")
    assert repaired["fixes_applied"] == 1, repaired
    assert any("FIXED" in issue for issue in fts_issues(repaired)), repaired
    after = doctor()
    assert after["healthy"], after
    assert not fts_issues(after)
    assert matches(db_path, "originalneedle") == {row[0]}
    assert not matches(db_path, "ghostneedle")
    assert not matches(db_path, "staleneedle")
    assert canonical_rows(db_path) == before


def test_fts_triggers_keep_updates_and_deletes_healthy(graph):
    _, db_path, doctor = graph
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE nodes SET content='replacementneedle body' WHERE id='alpha'")
    assert not matches(db_path, "originalneedle")
    assert matches(db_path, "replacementneedle")
    assert not fts_issues(doctor())
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM edges WHERE from_id='alpha' OR to_id='alpha'")
        conn.execute("DELETE FROM nodes WHERE id='alpha'")
    assert not matches(db_path, "replacementneedle")
    assert not fts_issues(doctor())


def test_rebuild_restores_mixed_rows_and_full_indexed_content(graph):
    data_dir, db_path, doctor = graph
    long_body = "canonical body " * 800 + "longtailneedle"
    with closing(Store(Config(data_dir=str(data_dir)))) as store:
        store.add_node(
            "titleneedle", long_body, node_id="history", node_type="session",
            status="archived", aka=["aliasneedle"], intent="intentneedle",
            domains=["domainneedle"],
        )
    def search_scope():
        with closing(Store(Config(data_dir=str(data_dir)))) as store:
            return (
                {node["id"] for node in store.fts_search("longtailneedle")},
                {node["id"] for node in store.fts_search(
                    "longtailneedle", include_archived=True)},
            )

    assert search_scope() == (set(), {"history"})
    before = canonical_rows(db_path)
    with sqlite3.connect(db_path) as conn:
        rowid = conn.execute("SELECT rowid FROM nodes WHERE id='history'").fetchone()[0]
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
    assert not matches(db_path, "longtailneedle")
    assert fts_issues(doctor())
    repaired = doctor("--fix")
    assert repaired["fixes_applied"] == 1, repaired
    assert doctor()["healthy"]
    for token in ("titleneedle", "longtailneedle", "aliasneedle",
                  "intentneedle", "domainneedle"):
        assert matches(db_path, token) == {rowid}
    assert matches(db_path, "originalneedle")
    assert matches(db_path, "betaneedle")
    assert canonical_rows(db_path) == before
    assert search_scope() == (set(), {"history"})


def test_unavailable_index_stays_unhealthy_when_repair_fails(graph):
    _, db_path, doctor = graph
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE nodes_fts")
    for args in ((), ("--fix",), ()):
        report = doctor(*args)
        assert not report["healthy"]
        assert fts_issues(report)
        assert report["fixes_applied"] == 0
        assert not any("FIXED" in issue for issue in fts_issues(report))
        if args:
            assert any("FIX FAILED" in issue for issue in fts_issues(report))
