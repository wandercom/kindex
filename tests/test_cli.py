"""Tests for Kindex (kin) CLI commands."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args],
        capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def data_dir(tmp_path):
    """Set up a temp data dir with some data for CLI tests."""
    d = str(tmp_path)
    # Init
    r = run("init", "--data-dir", d)
    assert r.returncode == 0

    # Add some nodes
    run("add", "Stigmergy is coordination through environmental traces", "--data-dir", d)
    run("add", "Python is an expert-level skill", "--type", "skill", "--data-dir", d)
    return d


class TestVersion:
    def test_version(self):
        from kindex import __version__
        r = run("--version")
        assert __version__ in r.stdout


class TestInit:
    def test_init_creates_db(self, tmp_path):
        d = str(tmp_path / "new")
        r = run("init", "--data-dir", d)
        assert r.returncode == 0
        assert (tmp_path / "new" / "kindex.db").exists()

    def test_newer_schema_refusal_has_no_traceback(self, tmp_path):
        data_dir = tmp_path / "future"
        assert run("init", "--data-dir", str(data_dir)).returncode == 0
        with sqlite3.connect(data_dir / "kindex.db") as conn:
            conn.execute(
                "UPDATE meta SET value = '999' WHERE key = 'schema_version'"
            )

        result = run("status", "--data-dir", str(data_dir))

        assert result.returncode == 2
        assert "newer than this Kindex build supports" in result.stderr
        assert "Traceback" not in result.stderr

    def test_status_exposes_durable_schema_recovery_path(self, tmp_path):
        data_dir = tmp_path / "recovery-status"
        assert run("init", "--data-dir", str(data_dir)).returncode == 0
        recovery = tmp_path / "before-v12.sqlite3"
        with sqlite3.connect(data_dir / "kindex.db") as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_recovery_snapshot_path", str(recovery)),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_recovery_snapshot_reason", "schema-v11-to-v12"),
            )

        result = run("status", "--data-dir", str(data_dir))

        assert result.returncode == 0
        assert f"Recovery:  {recovery} (schema-v11-to-v12)" in result.stdout


class TestAdd:
    def test_add_creates_node(self, data_dir):
        r = run("add", "Test concept about graph theory", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Created" in r.stdout or "node(s) added" in r.stdout

    def test_add_with_type(self, data_dir):
        r = run("add", "Should we use Redis?", "--type", "question", "--data-dir", data_dir)
        assert r.returncode == 0


class TestSearch:
    def test_search_finds(self, data_dir):
        r = run("search", "stigmergy", "--data-dir", data_dir)
        assert r.returncode == 0
        # Should find the node we added
        assert "stigmergy" in r.stdout.lower() or "coordination" in r.stdout.lower()

    def test_search_json(self, data_dir):
        r = run("search", "stigmergy", "--json", "--data-dir", data_dir)
        assert r.returncode == 0
        import json
        data = json.loads(r.stdout)
        assert isinstance(data, list)

    def test_search_no_results(self, data_dir):
        r = run("search", "zzzznonexistent", "--data-dir", data_dir)
        assert r.returncode == 0


class TestShow:
    def test_show_by_title(self, data_dir):
        # First find a node ID
        r = run("list", "--data-dir", data_dir)
        # Extract first ID from output
        lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
        if lines:
            nid = lines[0].split()[-1]  # last token is the ID
            r2 = run("show", nid, "--data-dir", data_dir)
            assert r2.returncode == 0


class TestList:
    def test_list_all(self, data_dir):
        r = run("list", "--data-dir", data_dir)
        assert r.returncode == 0
        assert len(r.stdout.strip().split("\n")) >= 2  # at least 2 nodes

    def test_list_json(self, data_dir):
        r = run("list", "--json", "--data-dir", data_dir)
        assert r.returncode == 0
        import json
        data = json.loads(r.stdout)
        assert isinstance(data, list)


class TestStatus:
    def test_status(self, data_dir):
        r = run("status", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Nodes" in r.stdout

    def test_status_json(self, data_dir):
        r = run("status", "--json", "--data-dir", data_dir)
        import json
        data = json.loads(r.stdout)
        assert "nodes" in data


class TestBudget:
    def test_budget(self, data_dir):
        r = run("budget", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Budget" in r.stdout


class TestRecent:
    def test_recent(self, data_dir):
        r = run("recent", "--data-dir", data_dir)
        assert r.returncode == 0


class TestOrphans:
    def test_orphans(self, data_dir):
        r = run("orphans", "--data-dir", data_dir)
        assert r.returncode == 0


class TestDoctor:
    def test_doctor(self, data_dir):
        r = run("doctor", "--data-dir", data_dir)
        assert r.returncode == 0


class TestContext:
    def test_context(self, data_dir):
        r = run("context", "--topic", "stigmergy", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Kindex" in r.stdout


class TestAsk:
    def test_ask_fallback(self, data_dir):
        """Ask falls back to search results when no LLM."""
        r = run("ask", "stigmergy", "--data-dir", data_dir)
        assert r.returncode == 0
        # Should show search results even without LLM
        assert "stigmergy" in r.stdout.lower() or "search results" in r.stdout.lower()


class TestRegister:
    def test_register_file(self, data_dir, tmp_path):
        # Create a temp file to register
        f = tmp_path / "example.py"
        f.write_text("# example")
        # Get a node ID first
        r = run("list", "--data-dir", data_dir)
        lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
        if lines:
            nid = lines[0].split()[-1]
            r2 = run("register", nid, str(f), "--data-dir", data_dir)
            assert r2.returncode == 0
            assert "Registered" in r2.stdout

    def test_register_nonexistent_node(self, data_dir):
        r = run("register", "nonexistent-id", "/tmp/foo.py", "--data-dir", data_dir)
        assert r.returncode != 0


class TestPersonNode:
    def test_add_person(self, data_dir):
        r = run("add", "Erik handles auth refactors", "--type", "person", "--data-dir", data_dir)
        assert r.returncode == 0

    def test_list_person_type(self, data_dir):
        run("add", "Jeremy is the project lead", "--type", "person", "--data-dir", data_dir)
        r = run("list", "--type", "person", "--data-dir", data_dir)
        assert r.returncode == 0


class TestConfig:
    def test_config_show(self, data_dir):
        r = run("config", "show", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "data_dir" in r.stdout

    def test_config_get(self, data_dir):
        r = run("config", "get", "defaults.hops", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "2" in r.stdout

    def test_config_set(self, tmp_path):
        cfg_path = str(tmp_path / "test-kin.yaml")
        # Write initial config
        (tmp_path / "test-kin.yaml").write_text("data_dir: /tmp/test\n")
        r = run("config", "set", "llm.enabled", "true", "--config", cfg_path)
        assert r.returncode == 0
        assert "Set" in r.stdout
        # Verify it was written
        import yaml
        data = yaml.safe_load((tmp_path / "test-kin.yaml").read_text())
        assert data["llm"]["enabled"] is True


class TestMigrate:
    def test_migrate_from_markdown(self, tmp_path):
        """Test migrating existing markdown topics into SQLite."""
        d = str(tmp_path)
        # Create markdown topics
        topics_dir = tmp_path / "topics"
        topics_dir.mkdir()
        (topics_dir / "test-topic.md").write_text(
            "---\ntopic: test-topic\ntitle: Test Topic\nweight: 0.8\n"
            "domains: [eng]\nconnects_to: []\n---\n# Test\n\nContent.\n"
        )
        (tmp_path / ".tmp").mkdir()

        # Init db first
        run("init", "--data-dir", d)
        # Migrate
        r = run("migrate", "--data-dir", d)
        assert r.returncode == 0
        assert "Migrated" in r.stdout

        # Verify searchable
        r2 = run("search", "test topic", "--data-dir", d)
        assert "Test Topic" in r2.stdout or "test" in r2.stdout.lower()


# ── collab: lock/unlock + coord join/attach/inject ────────────────────


def run_as(agent, *args):
    """Run kin as a specific agent identity (KIN_AGENT_ID)."""
    import os
    env = {**os.environ, "KIN_AGENT_ID": agent}
    env.pop("KIN_PROFILE", None)
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args],
        capture_output=True, text=True, timeout=30, env=env,
    )


class TestLockCLI:
    def test_lock_unlock_cycle(self, data_dir):
        r = run_as("alpha", "lock", "Stigmergy is coordination through environmental traces",
                   "--ttl", "30", "--note", "editing", "--data-dir", data_dir)
        assert r.returncode == 0, r.stderr
        assert "Locked" in r.stdout
        assert "alpha" in r.stdout

        # Foreign lock refused without force
        r = run_as("beta", "lock", "Stigmergy is coordination through environmental traces",
                   "--data-dir", data_dir)
        assert r.returncode == 1
        assert "locked by 'alpha'" in r.stderr

        # Foreign unlock refused without force
        r = run_as("beta", "unlock", "Stigmergy is coordination through environmental traces",
                   "--data-dir", data_dir)
        assert r.returncode == 1
        assert "alpha" in r.stderr

        # Force takeover, then release
        r = run_as("beta", "lock", "Stigmergy is coordination through environmental traces",
                   "--force", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "beta" in r.stdout

        r = run_as("beta", "unlock", "Stigmergy is coordination through environmental traces",
                   "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Unlocked" in r.stdout

        # Unlock with nothing held reports cleanly
        r = run_as("beta", "unlock", "Stigmergy is coordination through environmental traces",
                   "--data-dir", data_dir)
        assert r.returncode == 0
        assert "No lock" in r.stdout

    def test_lock_missing_node(self, data_dir):
        r = run_as("alpha", "lock", "definitely-not-a-node", "--data-dir", data_dir)
        assert r.returncode == 1
        assert "not found" in r.stderr

    def test_lock_explicit_agent_flag(self, data_dir):
        r = run_as("alpha", "lock", "Stigmergy is coordination through environmental traces",
                   "--agent", "gamma", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "gamma" in r.stdout


class TestCoordCLI:
    def test_join_attach_inject_flow(self, data_dir):
        r = run_as("alpha", "coord", "start", "crew", "--data-dir", data_dir)
        assert r.returncode == 0, r.stderr
        assert "Started coordination conversation" in r.stdout

        r = run_as("beta", "coord", "join", "crew", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Joined crew as beta" in r.stdout

        # Attach by title
        r = run_as("alpha", "coord", "attach", "crew",
                   "Stigmergy is coordination through environmental traces",
                   "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Attached" in r.stdout

        # Attach a missing node fails cleanly
        r = run_as("alpha", "coord", "attach", "crew", "no-such-node",
                   "--data-dir", data_dir)
        assert "Error" in r.stderr

        # Inject set (targeted), list, clear
        r = run_as("alpha", "coord", "inject", "crew", "set", "branch", "frozen",
                   "--to", "beta", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "Set inject message #1" in r.stdout

        r = run_as("alpha", "coord", "inject", "crew", "list", "--data-dir", data_dir)
        assert "branch frozen" in r.stdout
        assert "-> beta" in r.stdout

        r = run_as("alpha", "coord", "inject", "crew", "clear", "--id", "1",
                   "--data-dir", data_dir)
        assert "Cleared 1" in r.stdout

        r = run_as("alpha", "coord", "inject", "crew", "list", "--data-dir", data_dir)
        assert "No inject messages" in r.stdout

    def test_post_defaults_agent_and_targets(self, data_dir):
        run_as("alpha", "coord", "start", "room", "--data-dir", data_dir)
        r = run_as("alpha", "coord", "post", "room", "hello", "team",
                   "--to", "beta", "--data-dir", data_dir)
        assert r.returncode == 0, r.stderr
        assert "Posted message #1" in r.stdout

        r = run_as("beta", "coord", "read", "room", "--data-dir", data_dir)
        assert "alpha -> beta: hello team" in r.stdout
