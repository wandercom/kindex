"""Tests for recently implemented features that lacked test coverage.

Covers: PII stripping, directive state, synonym rings, parent .kin walk,
        write_kin_index, analytics module, Linear adapter, CLI analytics.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml


# ── Helpers ────────────────────────────────────────────────────────────


def run(*args):
    """Run the kin CLI as a subprocess (same pattern as test_cli.py)."""
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args],
        capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def data_dir(tmp_path):
    """Initialise a temp data directory with a few seed nodes."""
    d = str(tmp_path)
    r = run("init", "--data-dir", d)
    assert r.returncode == 0
    # Seed a concept node
    run("add", "Graph theory is fundamental to kindex",
        "--data-dir", d)
    return d


@pytest.fixture
def store_and_config(tmp_path):
    """Return a (Store, Config) pair rooted in tmp_path."""
    from kindex.config import Config
    from kindex.store import Store

    cfg = Config(data_dir=str(tmp_path))
    st = Store(cfg)
    # Touch the DB so schema is created
    _ = st.conn
    yield st, cfg
    st.close()


# ── 1. PII stripping ──────────────────────────────────────────────────


class TestPIIStripping:
    """Test the _strip_pii helper used by `kin export`."""

    @staticmethod
    def _strip_pii(node):
        # Import the private helper
        from kindex.cli import _strip_pii
        return _strip_pii(node)

    def test_email_stripped(self):
        node = {
            "content": "Contact alice@example.com for details.",
            "prov_who": ["alice"],
            "prov_source": "/home/alice/project/notes.md",
        }
        cleaned = self._strip_pii(node)
        assert "alice@example.com" not in cleaned["content"]
        assert "[email]" in cleaned["content"]

    def test_long_token_redacted(self):
        token = "A" * 45  # 45-char token should be redacted
        node = {
            "content": f"API key is {token} keep it safe.",
            "prov_who": ["bob"],
            "prov_source": "",
        }
        cleaned = self._strip_pii(node)
        assert token not in cleaned["content"]
        assert "[redacted]" in cleaned["content"]

    def test_prov_who_anonymized(self):
        node = {
            "content": "some content",
            "prov_who": ["jeremy", "erik"],
            "prov_source": "",
        }
        cleaned = self._strip_pii(node)
        assert cleaned["prov_who"] == ["anonymous"]

    def test_prov_source_filename_only(self):
        node = {
            "content": "text",
            "prov_who": [],
            "prov_source": "/home/user/secret/project/notes.md",
        }
        cleaned = self._strip_pii(node)
        assert cleaned["prov_source"] == "notes.md"

    def test_extra_actor_removed(self):
        node = {
            "content": "",
            "prov_who": [],
            "prov_source": "",
            "extra": {"actor": "jeremy", "key": "value"},
        }
        cleaned = self._strip_pii(node)
        assert "actor" not in cleaned["extra"]
        assert cleaned["extra"]["key"] == "value"

    def test_does_not_mutate_original(self):
        original = {
            "content": "alice@example.com",
            "prov_who": ["alice"],
            "prov_source": "/a/b/c.txt",
            "extra": {"actor": "x"},
        }
        self._strip_pii(original)
        # Original should be unmodified
        assert original["prov_who"] == ["alice"]
        assert original["content"] == "alice@example.com"


# ── 2. Directive state ────────────────────────────────────────────────


class TestDirectiveState:
    """Test set-state via CLI (creates a directive, sets state, verifies)."""

    def test_set_state_roundtrip(self, data_dir):
        # Create a directive node
        r = run("add", "Always run linter before commit",
                "--type", "directive", "--data-dir", data_dir)
        assert r.returncode == 0

        # Set a state key
        r = run("set-state", "Always run linter before commit",
                "enabled", "true", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "enabled" in r.stdout
        assert "True" in r.stdout

    def test_set_state_numeric(self, data_dir):
        run("add", "Max retries directive",
            "--type", "directive", "--data-dir", data_dir)
        r = run("set-state", "Max retries directive",
                "count", "5", "--data-dir", data_dir)
        assert r.returncode == 0
        assert "count" in r.stdout

    def test_set_state_nonexistent_node(self, data_dir):
        r = run("set-state", "this-does-not-exist",
                "key", "val", "--data-dir", data_dir)
        assert r.returncode != 0


# ── 3. Synonym rings ─────────────────────────────────────────────────


class TestSynonymRings:
    """Test load_synonym_rings from ingest module."""

    def test_load_synonym_rings_applies_aka(self, store_and_config):
        store, cfg = store_and_config

        # Create a node whose title matches a synonym
        store.add_node("database", node_type="concept")

        # Create the synonyms directory and a .syn file
        syn_dir = cfg.data_path / "synonyms"
        syn_dir.mkdir(parents=True, exist_ok=True)
        syn_file = syn_dir / "db-terms.syn"
        syn_file.write_text(yaml.dump({
            "ring": "database-terms",
            "synonyms": ["database", "db", "datastore"],
        }))

        from kindex.ingest import load_synonym_rings

        count = load_synonym_rings(cfg, store, verbose=False)
        assert count >= 1

        # Verify AKA entries were added
        node = store.get_node_by_title("database")
        assert node is not None
        aka = node.get("aka", [])
        assert "db" in aka
        assert "datastore" in aka

    def test_load_synonym_rings_no_dir(self, store_and_config):
        """Returns 0 when synonyms directory doesn't exist."""
        store, cfg = store_and_config

        from kindex.ingest import load_synonym_rings

        count = load_synonym_rings(cfg, store)
        assert count == 0

    def test_load_synonym_rings_skips_unmatched(self, store_and_config):
        """Synonyms without matching nodes are silently skipped."""
        store, cfg = store_and_config

        syn_dir = cfg.data_path / "synonyms"
        syn_dir.mkdir(parents=True, exist_ok=True)
        (syn_dir / "nope.syn").write_text(yaml.dump({
            "ring": "nope",
            "synonyms": ["xyzzy", "plugh"],
        }))

        from kindex.ingest import load_synonym_rings

        count = load_synonym_rings(cfg, store)
        assert count == 0


# ── 4. Parent .kin walk ──────────────────────────────────────────────


def _write_kin_config(directory, content):
    """Helper: create .kin/config inside a directory."""
    kin_dir = directory / ".kin"
    kin_dir.mkdir(exist_ok=True)
    (kin_dir / "config").write_text(content)


class TestParentKinWalk:
    """Test find_parent_kin discovers .kin/config files up the directory tree."""

    def test_finds_nested_kin_configs(self, tmp_path):
        # Create nested structure with .kin/config at different levels
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)

        _write_kin_config(tmp_path, "name: root\n")
        _write_kin_config(tmp_path / "a", "name: mid\n")
        # No .kin in b
        _write_kin_config(deep, "name: leaf\n")

        from kindex.ingest import find_parent_kin

        found = find_parent_kin(deep)
        paths = [p.resolve() for p in found]

        # Should find leaf, mid, root .kin/config files
        assert (deep / ".kin" / "config").resolve() in paths
        assert (tmp_path / "a" / ".kin" / "config").resolve() in paths
        assert (tmp_path / ".kin" / "config").resolve() in paths

        # Leaf should come first (most specific)
        assert paths[0] == (deep / ".kin" / "config").resolve()

    def test_auto_upgrades_old_kin_file(self, tmp_path):
        """Old-style .kin file is auto-upgraded during walk."""
        deep = tmp_path / "child"
        deep.mkdir()

        # Old-style .kin file at root
        (tmp_path / ".kin").write_text("name: old-root\n")
        _write_kin_config(deep, "name: leaf\n")

        from kindex.ingest import find_parent_kin

        found = find_parent_kin(deep)
        paths = [p.resolve() for p in found]

        # Both should be found
        assert (deep / ".kin" / "config").resolve() in paths
        assert (tmp_path / ".kin" / "config").resolve() in paths
        # Old file should have been upgraded
        assert (tmp_path / ".kin").is_dir()
        assert (tmp_path / ".kin" / "config").is_file()

    def test_no_kin_files(self, tmp_path):
        subdir = tmp_path / "empty"
        subdir.mkdir()

        from kindex.ingest import find_parent_kin

        found = find_parent_kin(subdir, max_depth=3)
        # May find some .kin above tmp_path on the real FS, but at minimum
        # none should be inside tmp_path (since we didn't create any)
        for p in found:
            assert not str(p.resolve()).startswith(str(subdir.resolve()))

    def test_defaults_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_kin_config(tmp_path, "name: here\n")

        from kindex.ingest import find_parent_kin

        found = find_parent_kin()  # no argument — should use cwd
        assert any(
            p.resolve() == (tmp_path / ".kin" / "config").resolve() for p in found
        )


# ── 5. write_kin_index ───────────────────────────────────────────────


class TestKinIndex:
    """Test write_kin_index produces correct JSON."""

    def test_write_kin_index_output(self, store_and_config, tmp_path):
        store, cfg = store_and_config

        # Add a couple of nodes (team audience: the non-repo fallback only
        # includes public/team nodes since the index is meant to be committed)
        store.add_node("Alpha concept", node_type="concept",
                       domains=["eng"], weight=0.8, audience="team")
        store.add_node("Beta concept", node_type="decision",
                       domains=["design"], weight=0.6, audience="team")

        from kindex.ingest import write_kin_index

        output_dir = tmp_path / "project"
        output_dir.mkdir()
        result_path = write_kin_index(store, output_dir)

        assert result_path.exists()
        assert result_path.name == "index.json"
        assert result_path.parent.name == ".kin"

        data = json.loads(result_path.read_text())
        # v2 = unknown-field passthrough convention (PRD lineage-grounding
        # item 1); writer and merge driver are single-sourced on this value.
        from kindex.kin_merge import KIN_INDEX_SCHEMA_VERSION
        assert data["version"] == 2
        assert data["version"] == KIN_INDEX_SCHEMA_VERSION
        assert data["node_count"] >= 2
        assert isinstance(data["nodes"], list)
        assert any(n["title"] == "Alpha concept" for n in data["nodes"])
        assert "eng" in data["domains"] or "design" in data["domains"]

    def test_write_kin_index_creates_dir(self, store_and_config, tmp_path):
        store, cfg = store_and_config
        store.add_node("Single node")

        from kindex.ingest import write_kin_index

        output_dir = tmp_path / "brand_new"
        output_dir.mkdir()
        path = write_kin_index(store, output_dir)
        assert (output_dir / ".kin").is_dir()
        assert path.exists()

    def test_write_kin_index_uses_canonical_ordering(self, store_and_config, tmp_path):
        store, cfg = store_and_config
        store.add_node(
            "Zeta concept",
            node_id="z-node",
            domains=["zeta", "alpha"],
            weight=1.0,
            audience="team",
        )
        store.add_node(
            "Alpha concept",
            node_id="a-node",
            domains=["beta"],
            weight=0.1,
            audience="public",
        )

        from kindex.ingest import write_kin_index

        output_dir = tmp_path / "canonical"
        output_dir.mkdir()
        path = write_kin_index(store, output_dir)

        data = json.loads(path.read_text())
        node_ids = [node["id"] for node in data["nodes"]]
        assert node_ids == sorted(node_ids)
        assert data["domains"] == sorted(data["domains"])
        assert "generated_at" not in data
        # No volatile timestamp in the committed snapshot (would churn git history
        # and conflict on every concurrent merge); per-node updated_at is enough.
        assert "source_updated_at" not in data
        assert all("updated_at" in node for node in data["nodes"])


# ── 6. Analytics module ──────────────────────────────────────────────


class TestAnalyticsModule:
    """Test analytics functions with no real archive database."""

    def test_find_archive_db_returns_none(self, tmp_path, monkeypatch):
        from kindex.analytics import find_archive_db
        from kindex.config import Config

        # Redirect Path.home() so the hardcoded fallback doesn't find the
        # real ~/.claude/archive/index.db on this machine.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
        result = find_archive_db(cfg)
        assert result is None

    def test_session_stats_returns_error_dict(self, tmp_path, monkeypatch):
        from kindex.analytics import session_stats
        from kindex.config import Config

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
        result = session_stats(cfg)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_activity_heatmap_returns_error_dict(self, tmp_path, monkeypatch):
        from kindex.analytics import activity_heatmap
        from kindex.config import Config

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
        result = activity_heatmap(cfg)
        assert "error" in result

    def test_find_archive_db_finds_db(self, tmp_path):
        """When the archive DB exists, find_archive_db returns its path."""
        from kindex.analytics import find_archive_db
        from kindex.config import Config

        archive_dir = tmp_path / "claude" / "archive"
        archive_dir.mkdir(parents=True)
        db_path = archive_dir / "index.db"
        db_path.write_text("")  # empty file -- just needs to exist

        cfg = Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
        result = find_archive_db(cfg)
        assert result is not None
        assert result.name == "index.db"


# ── 7. Linear adapter ───────────────────────────────────────────────


class TestLinearAdapter:
    """Test linear adapter helpers without calling the real API."""

    def test_is_linear_available_false_without_env(self):
        from kindex.adapters.linear import is_linear_available

        with mock.patch.dict(os.environ, {}, clear=True):
            # Ensure LINEAR_API_KEY is not set
            os.environ.pop("LINEAR_API_KEY", None)
            assert is_linear_available() is False

    def test_is_linear_available_true_with_env(self):
        from kindex.adapters.linear import is_linear_available

        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "lin_test_key"}):
            assert is_linear_available() is True

    def test_ingest_issues_returns_zero_without_api(self, store_and_config):
        from kindex.adapters.linear import ingest_issues

        store, cfg = store_and_config
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("LINEAR_API_KEY", None)
            count = ingest_issues(store)
            assert count == 0


# ── 8. CLI analytics command ─────────────────────────────────────────


class TestCLIAnalytics:
    """Test the `kin analytics` CLI command end-to-end."""

    def test_analytics_runs_without_crash(self, data_dir):
        """analytics completes without crashing.

        When a real ~/.claude/archive/index.db exists on the machine, the
        command succeeds and prints stats.  When no archive exists, it
        exits with an error message.  Either outcome is acceptable.
        """
        r = run("analytics", "--data-dir", data_dir)
        if r.returncode == 0:
            # Found an archive -- output should contain session info
            assert "session" in r.stdout.lower() or "total" in r.stdout.lower()
        else:
            # No archive -- should report a helpful error
            assert "error" in r.stderr.lower() or "not found" in r.stderr.lower()

    def test_analytics_json_flag(self, data_dir):
        """analytics --json produces JSON output or a graceful error."""
        r = run("analytics", "--json", "--data-dir", data_dir)
        # Should not crash regardless of archive presence
        combined = r.stdout + r.stderr
        assert len(combined) > 0
        if r.returncode == 0 and r.stdout.strip():
            # If it succeeded, stdout should be valid JSON
            data = json.loads(r.stdout)
            assert isinstance(data, dict)
