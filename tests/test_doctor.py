"""Tests for enhanced doctor command."""

import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store
from kindex.vectors import EMBED_QUEUE_META, EMBED_QUARANTINE_META


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestDoctorBasic:
    def test_healthy_graph(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Alpha concept is about testing", data_dir=d)
        run("add", "Beta concept is about graphs", data_dir=d)
        r = run("doctor", data_dir=d)
        assert r.returncode == 0

    def test_empty_graph(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        r = run("doctor", data_dir=d)
        assert r.returncode == 0
        assert "No nodes" in r.stdout

    def test_doctor_json(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Test node", data_dir=d)
        r = run("doctor", "--json", data_dir=d)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "healthy" in data
        assert "issues" in data
        assert "warnings" in data
        assert "stats" in data

    def test_fix_quarantines_known_oversized_openai_queue_input(self, tmp_path):
        """Doctor clears a known-impossible input without a provider call."""
        data_dir = str(tmp_path / "data")
        store = Store(Config(
            data_dir=data_dir,
            embedding={"provider": "openai", "model": "text-embedding-3-small"},
        ))
        node_id = store.add_node("Archived checkpoint", content="x" * 202_000)
        store.close()
        config = tmp_path / "kin.yaml"
        config.write_text(
            "embedding:\n  provider: openai\n  model: text-embedding-3-small\n"
        )

        before = run("doctor", "--json", "--config", str(config), data_dir=data_dir)
        assert before.returncode == 0
        assert json.loads(before.stdout)["embedding_queue"]["oversized_inputs"] == 1

        repaired = run(
            "doctor", "--fix", "--json", "--config", str(config), data_dir=data_dir
        )
        assert repaired.returncode == 0
        report = json.loads(repaired.stdout)
        assert report["fixes_applied"] == 1

        store = Store(Config(
            data_dir=data_dir,
            embedding={"provider": "openai", "model": "text-embedding-3-small"},
        ))
        assert json.loads(store.get_meta(EMBED_QUEUE_META) or "[]") == []
        quarantine = json.loads(store.get_meta(EMBED_QUARANTINE_META) or "{}")
        record = quarantine["items"][node_id]
        assert record["kind"] == "input_too_large"
        assert record["attempts"] == 0
        assert "8192-token" in record["message"]
        store.close()


class TestDoctorInvariants:
    def test_detects_orphans(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        # Create isolated nodes (no edges)
        for i in range(5):
            store.add_node(f"Orphan {i}", node_id=f"orphan-{i}")
        store.close()

        r = run("doctor", "--json", data_dir=str(tmp_path))
        data = json.loads(r.stdout)
        # Should detect orphans
        assert any("orphan" in w.lower() for w in data.get("warnings", []) + data.get("issues", []))

    def test_detects_dangling_edges(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        store.add_node("Real node", node_id="real")
        store.add_node("To delete", node_id="del")
        store.add_edge("real", "del")
        db_path = str(store.db_path)
        store.close()
        # Separate connection with FK off to create dangling edge
        import sqlite3
        conn2 = sqlite3.connect(db_path)
        conn2.execute("PRAGMA foreign_keys=OFF")
        conn2.execute("DELETE FROM nodes WHERE id = 'del'")
        conn2.commit()
        conn2.close()

        r = run("doctor", "--json", data_dir=str(tmp_path))
        data = json.loads(r.stdout)
        assert any("dangling" in i.lower() for i in data.get("issues", []))

    def test_fix_dangling_edges(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        store.add_node("Real node", node_id="real")
        store.add_node("To delete", node_id="del")
        store.add_edge("real", "del")
        db_path = str(store.db_path)
        store.close()
        import sqlite3
        conn2 = sqlite3.connect(db_path)
        conn2.execute("PRAGMA foreign_keys=OFF")
        conn2.execute("DELETE FROM nodes WHERE id = 'del'")
        conn2.commit()
        conn2.close()

        r = run("doctor", "--fix", "--json", data_dir=str(tmp_path))
        data = json.loads(r.stdout)
        assert data["fixes_applied"] >= 1
