"""Tests for the suggestion system — add, list, accept, reject, CLI."""

import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestAddAndListSuggestions:
    def test_add_suggestion(self, store):
        """Add a suggestion and verify it exists."""
        sid = store.add_suggestion(
            concept_a="Machine Learning",
            concept_b="Graph Theory",
            reason="Both use optimization",
            source="test",
        )

        assert sid > 0

    def test_add_multiple_suggestions(self, store):
        """Add multiple suggestions and verify count."""
        store.add_suggestion("A", "B", reason="link 1")
        store.add_suggestion("C", "D", reason="link 2")
        store.add_suggestion("E", "F", reason="link 3")

        pending = store.pending_suggestions()
        assert len(pending) == 3

    def test_pending_suggestions_list(self, store):
        """Verify pending list returns correct data."""
        store.add_suggestion(
            concept_a="Neural Networks",
            concept_b="Stigmergy",
            reason="Both are distributed",
            source="session-end",
        )

        pending = store.pending_suggestions()
        assert len(pending) == 1
        s = pending[0]
        assert s["concept_a"] == "Neural Networks"
        assert s["concept_b"] == "Stigmergy"
        assert s["reason"] == "Both are distributed"
        assert s["source"] == "session-end"
        assert s["status"] == "pending"

    def test_pending_suggestions_limit(self, store):
        """Limit parameter should cap results."""
        for i in range(10):
            store.add_suggestion(f"A{i}", f"B{i}")

        limited = store.pending_suggestions(limit=3)
        assert len(limited) == 3

    def test_suggestion_exists_matches_either_order(self, store):
        """Pair lookup deduplicates direct and reversed suggestions."""
        store.add_suggestion("Alpha", "Beta", reason="test")

        assert store.suggestion_exists("Alpha", "Beta") is True
        assert store.suggestion_exists("Beta", "Alpha") is True
        assert store.suggestion_exists("Alpha", "Gamma") is False

    def test_suggestion_exists_honors_status(self, store):
        """Rejected/accepted suggestions do not count as pending."""
        sid = store.add_suggestion("Alpha", "Beta", reason="test")
        store.update_suggestion(sid, "accepted")

        assert store.suggestion_exists("Alpha", "Beta") is False
        assert store.suggestion_exists("Alpha", "Beta", status="accepted") is True
        assert store.suggestion_exists("Alpha", "Beta", status=None) is True

    def test_suggestions_have_dream_indexes(self, store):
        """Schema includes indexes needed by scheduled dream runs."""
        rows = store.conn.execute("PRAGMA index_list('suggestions')").fetchall()
        names = {row["name"] for row in rows}

        assert "idx_suggestions_status_created" in names
        assert "idx_suggestions_status_pair" in names
        assert "idx_suggestions_pair" in names


class TestAcceptSuggestion:
    def test_accept_suggestion(self, store):
        """Accept a suggestion, verify edge created."""
        # Create the concept nodes first
        store.add_node("Machine Learning", node_id="ml", node_type="concept")
        store.add_node("Graph Theory", node_id="gt", node_type="concept")
        store.add_edge("ml", "gt", provenance="initial")  # prevent orphan issues

        # Add suggestion
        sid = store.add_suggestion(
            concept_a="Machine Learning",
            concept_b="Graph Theory",
            reason="optimization link",
        )

        # Accept it
        store.update_suggestion(sid, "accepted")

        # Verify status changed
        pending = store.pending_suggestions()
        assert len(pending) == 0

    def test_accept_creates_edge_via_cli(self, tmp_path):
        """Accept through suggest --accept creates an edge."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Create nodes with known titles directly via store
        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Alpha Concept", node_id="alpha", node_type="concept")
        s.add_node("Beta Concept", node_id="beta", node_type="concept")
        s.add_edge("alpha", "beta")  # prevent orphan

        sid = s.add_suggestion(
            concept_a="Alpha Concept",
            concept_b="Beta Concept",
            reason="both use optimization",
        )
        s.close()

        # Accept via CLI
        r = run("suggest", "--accept", str(sid), data_dir=d)
        assert r.returncode == 0
        assert "Accepted" in r.stdout or "created" in r.stdout.lower()

    def test_accept_resolves_stable_node_ids_via_cli(self, tmp_path):
        """Dream proposals use IDs so duplicate titles stay unambiguous."""
        d = str(tmp_path)
        run("init", data_dir=d)

        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Alpha Concept", node_id="alpha", node_type="concept")
        s.add_node("Beta Concept", node_id="beta", node_type="concept")
        sid = s.add_suggestion(
            concept_a="alpha",
            concept_b="beta",
            reason="shared domain",
            source="dream-cycle-domain",
        )
        s.close()

        result = run("suggest", "--accept", str(sid), data_dir=d)

        assert result.returncode == 0
        assert "Accepted" in result.stdout
        reopened = Store(cfg)
        assert reopened.edges_from("alpha", semantic_only=True)
        reopened.close()


class TestRejectSuggestion:
    def test_reject_suggestion(self, store):
        """Reject and verify it's no longer pending."""
        sid = store.add_suggestion("A", "B", reason="test")

        # Should be pending
        assert len(store.pending_suggestions()) == 1

        # Reject
        store.update_suggestion(sid, "rejected")

        # Should no longer be pending
        assert len(store.pending_suggestions()) == 0

    def test_reject_via_cli(self, tmp_path):
        """Reject through suggest --reject removes from pending."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Add a suggestion directly
        cfg = Config(data_dir=d)
        s = Store(cfg)
        sid = s.add_suggestion("A", "B", reason="test")
        s.close()

        r = run("suggest", "--reject", str(sid), data_dir=d)
        assert r.returncode == 0
        assert "Rejected" in r.stdout


class TestSuggestCLI:
    def test_suggest_cli_list_empty(self, tmp_path):
        """Test suggest command with no suggestions."""
        d = str(tmp_path)
        run("init", data_dir=d)

        r = run("suggest", data_dir=d)
        assert r.returncode == 0
        assert "No pending" in r.stdout

    def test_suggest_cli_list_with_suggestions(self, tmp_path):
        """Test suggest command shows pending suggestions."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Add suggestions
        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_suggestion("Concept Alpha", "Concept Beta", reason="analogy detected")
        s.add_suggestion("Concept Gamma", "Concept Delta", reason="co-occurrence")
        s.close()

        r = run("suggest", data_dir=d)
        assert r.returncode == 0
        assert "Concept Alpha" in r.stdout
        assert "Concept Beta" in r.stdout
        assert "Bridge Opportunities" in r.stdout

    def test_suggest_cli_json(self, tmp_path):
        """Test suggest --json output."""
        d = str(tmp_path)
        run("init", data_dir=d)

        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_suggestion("X", "Y", reason="test")
        s.close()

        r = run("suggest", "--json", data_dir=d)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["concept_a"] == "X"
