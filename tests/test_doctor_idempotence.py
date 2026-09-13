"""Doctor repairs integrity; graph enrichment remains an advisory workflow."""

from contextlib import closing
import itertools
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
def low_bridge_graph(tmp_path, monkeypatch):
    home = tmp_path / "home"
    isolated = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "XDG_STATE_HOME": home / "state",
        "XDG_CACHE_HOME": home / "cache",
    }
    for name, path in isolated.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    data_dir = tmp_path / "graph"
    config_path = home / "config" / "doctor.json"
    config_path.write_text(json.dumps({
        "data_dir": str(data_dir),
        "profiles": {},
        "reminders": {"action_enabled": False},
    }))
    with closing(Store(Config(data_dir=str(data_dir)))) as store:
        # Two complete four-node domains joined by one bidirectional edge:
        # connected, no orphans, and only 2/26 directed edges cross domains.
        for domain in ("alpha", "beta"):
            ids = [f"{domain}-{i}" for i in range(4)]
            for node_id in ids:
                store.add_node(
                    node_id, f"canonicalneedle knowledge about {node_id}",
                    node_id=node_id, domains=[domain],
                )
            for left, right in itertools.combinations(ids, 2):
                store.add_edge(left, right, bidirectional=True)
        store.add_edge("alpha-0", "beta-0", bidirectional=True)
        db_path = store.db_path

    def doctor(*args):
        env = {name: str(path) for name, path in isolated.items()}
        for name in ("PATH", "SYSTEMROOT"):
            if name in os.environ:
                env[name] = os.environ[name]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-m", "kindex.cli", "doctor", "--json",
             "--config", str(config_path), "--data-dir", str(data_dir), *args],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return db_path, doctor


def persisted_state(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("nodes", "edges", "suggestions", "activity_log")
        }


def assert_bridge_advisory(report):
    warnings = [text for text in report["warnings"]
                if "cross-domain" in text.lower()]
    assert warnings, report
    assert any("dream" in text.lower() for text in warnings), warnings
    assert not any("cross-domain" in text.lower() for text in report["issues"])


def test_repeated_fix_leaves_low_bridge_graph_unchanged(low_bridge_graph):
    db_path, doctor = low_bridge_graph
    before = persisted_state(db_path)
    initial = doctor()
    assert initial["healthy"], initial
    assert persisted_state(db_path) == before

    for _ in range(2):
        report = doctor("--fix")
        # Assert actual durable effects before checking reported fix counts:
        # the old random bridge suggestion behavior violates this invariant.
        assert persisted_state(db_path) == before
        assert report["healthy"], report
        assert report["fixes_applied"] == 0, report
        assert_bridge_advisory(report)
    assert_bridge_advisory(initial)


def test_fts_repair_runs_once_without_enrichment_side_effects(low_bridge_graph):
    db_path, doctor = low_bridge_graph
    before = persisted_state(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
        assert not conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'canonicalneedle'"
        ).fetchall()

    damaged = doctor()
    assert not damaged["healthy"], damaged
    assert any("FTS5" in text for text in damaged["issues"]), damaged
    assert persisted_state(db_path) == before

    repaired = doctor("--fix")
    assert persisted_state(db_path) == before
    assert repaired["fixes_applied"] == 1, repaired
    assert any("FTS5" in text and "FIXED" in text
               for text in repaired["issues"]), repaired
    assert_bridge_advisory(repaired)
    with sqlite3.connect(db_path) as conn:
        assert len(conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'canonicalneedle'"
        ).fetchall()) == 8

    second = doctor("--fix")
    assert second["healthy"], second
    assert second["fixes_applied"] == 0, second
    assert persisted_state(db_path) == before
    assert_bridge_advisory(second)
