"""Tests for temporal queries — activity_since, nodes_changed_since, activity_by_actor, changelog CLI, meta table."""

import datetime
import json
import subprocess
import sys
import time

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


class TestActivitySince:
    def test_activity_since(self, store):
        """Add nodes, query since timestamp."""
        # Use a timestamp far enough in the past to account for any timezone issues
        # (SQLite datetime('now') returns UTC)
        before = "2020-01-01T00:00:00"

        store.add_node("Concept One", content="About one", node_id="c1")
        store.add_node("Concept Two", content="About two", node_id="c2")

        activity = store.activity_since(before)

        # Should have at least 2 add_node entries
        add_actions = [a for a in activity if a["action"] == "add_node"]
        assert len(add_actions) >= 2

    def test_activity_since_filtered_by_action(self, store):
        """Filter activity by action type."""
        before = "2020-01-01T00:00:00"

        store.add_node("Node A", node_id="a")
        store.add_node("Node B", node_id="b")
        store.add_edge("a", "b")

        # Filter to only add_node
        add_activity = store.activity_since(before, action="add_node")
        edge_activity = store.activity_since(before, action="add_edge")

        assert all(a["action"] == "add_node" for a in add_activity)
        assert all(a["action"] == "add_edge" for a in edge_activity)

    def test_activity_since_future(self, store):
        """Query with future timestamp returns empty."""
        store.add_node("Node X", node_id="x")

        future = "2099-01-01T00:00:00"
        activity = store.activity_since(future)
        assert activity == []

    def test_activity_since_captures_titles(self, store):
        """Activity log should capture target titles."""
        before = "2020-01-01T00:00:00"

        store.add_node("Memorable Title", node_id="mt")

        activity = store.activity_since(before)
        titles = [a.get("target_title", "") for a in activity]
        assert "Memorable Title" in titles


class TestNodesChangedSince:
    def test_nodes_changed_since(self, store):
        """Update nodes, query changes."""
        # Add a node
        nid = store.add_node("Original Node", content="Original content", node_id="orig")

        # Record timestamp
        before = (datetime.datetime.now() - datetime.timedelta(seconds=1)).isoformat(timespec="seconds")

        # Update the node
        time.sleep(0.05)
        store.update_node(nid, content="Updated content")

        changed = store.nodes_changed_since(before)

        # Should find the updated node
        assert len(changed) >= 1
        changed_ids = [n["id"] for n in changed]
        assert nid in changed_ids

    def test_nodes_changed_since_no_changes(self, store):
        """No changes after timestamp should return empty."""
        store.add_node("Old Node", node_id="old")

        future = "2099-01-01T00:00:00"
        changed = store.nodes_changed_since(future)
        assert changed == []


class TestActivityByActor:
    def test_activity_by_actor(self, store):
        """Query by actor."""
        # Add nodes with specific actors
        store.add_node("Alice's Work", node_id="alice-work",
                        prov_who=["alice"], prov_activity="manual-add")
        store.add_node("Bob's Work", node_id="bob-work",
                        prov_who=["bob"], prov_activity="manual-add")

        alice_activity = store.activity_by_actor("alice")
        bob_activity = store.activity_by_actor("bob")

        # Alice should have activity for her node
        alice_targets = [a.get("target_id", "") for a in alice_activity]
        assert "alice-work" in alice_targets

        # Bob should have activity for his node
        bob_targets = [a.get("target_id", "") for a in bob_activity]
        assert "bob-work" in bob_targets

    def test_activity_by_actor_empty(self, store):
        """Unknown actor returns empty."""
        store.add_node("Something", node_id="something")

        activity = store.activity_by_actor("nonexistent-user")
        assert activity == []

    def test_activity_by_actor_limit(self, store):
        """Limit parameter should cap results."""
        for i in range(10):
            store.add_node(f"Node {i}", node_id=f"n{i}",
                            prov_who=["testuser"], prov_activity="manual-add")

        activity = store.activity_by_actor("testuser", limit=3)
        assert len(activity) <= 3


class TestActivityBound:
    def test_a_local_bound_keeps_the_boundary_day_and_filters_by_actor(self, store):
        """A local, T-separated bound is compared in the log's UTC form."""
        store.conn  # create the schema
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        stamp = (utc_now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        store.conn.executemany(
            "INSERT INTO activity_log (timestamp, action, target_id, actor) VALUES (?, ?, ?, ?)",
            [(stamp, "add_node", "n1", "alice"), (stamp, "add_node", "n2", "bob")],
        )
        store.conn.commit()
        bound = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")

        ids = {row["target_id"] for row in store.activity_since(bound)}
        assert {"n1", "n2"} <= ids
        assert [row["target_id"] for row in store.activity_since(bound, actor="alice")] == ["n1"]
        later = (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
        assert store.activity_since(later, actor="alice") == []


class TestChangelogCLI:
    def test_changelog_cli(self, tmp_path):
        """Test changelog command via subprocess."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Add some nodes
        run("add", "Changelog Test Concept One", data_dir=d)
        run("add", "Changelog Test Concept Two", data_dir=d)

        r = run("changelog", "--days", "1", data_dir=d)
        assert r.returncode == 0
        # Should show some activity
        assert "Changelog" in r.stdout

    def test_changelog_cli_empty(self, tmp_path):
        """Changelog on empty store shows no activity."""
        d = str(tmp_path)
        run("init", data_dir=d)

        r = run("changelog", "--days", "1", data_dir=d)
        assert r.returncode == 0
        assert "No activity" in r.stdout or "Changelog" in r.stdout

    def test_changelog_cli_json(self, tmp_path):
        """Test changelog --json output."""
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "JSON Test Concept", data_dir=d)

        r = run("changelog", "--days", "1", "--json", data_dir=d)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "since" in data
        assert "total" in data

    def test_changelog_cli_since(self, tmp_path):
        """Test changelog --since option."""
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Since Test Concept", data_dir=d)

        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        r = run("changelog", "--since", yesterday, data_dir=d)
        assert r.returncode == 0

    def test_changelog_cli_with_actor(self, tmp_path):
        """Test changelog --actor filter."""
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Actor Filtered Concept", data_dir=d)

        r = run("changelog", "--days", "1", "--actor", "nonexistent", "--json", data_dir=d)
        assert r.returncode == 0
        assert json.loads(r.stdout)["total"] == 0


class TestMetaTable:
    def test_get_meta_nonexistent(self, store):
        """get_meta returns None for nonexistent keys."""
        result = store.get_meta("nonexistent_key")
        assert result is None

    def test_set_and_get_meta(self, store):
        """Test get_meta and set_meta."""
        store.set_meta("test_key", "test_value")
        result = store.get_meta("test_key")
        assert result == "test_value"

    def test_set_meta_overwrite(self, store):
        """set_meta should overwrite existing values."""
        store.set_meta("key", "value1")
        assert store.get_meta("key") == "value1"

        store.set_meta("key", "value2")
        assert store.get_meta("key") == "value2"

    def test_meta_schema_version(self, store):
        """Schema version should be set in meta table."""
        version = store.get_meta("schema_version")
        assert version is not None
        assert int(version) >= 1

    def test_meta_multiple_keys(self, store):
        """Multiple meta keys should coexist."""
        store.set_meta("alpha", "1")
        store.set_meta("beta", "2")
        store.set_meta("gamma", "3")

        assert store.get_meta("alpha") == "1"
        assert store.get_meta("beta") == "2"
        assert store.get_meta("gamma") == "3"
