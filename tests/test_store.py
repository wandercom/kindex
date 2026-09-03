"""Tests for SQLite store."""

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


class TestNodeOperations:
    def test_add_and_get(self, store):
        nid = store.add_node("Test Topic", content="Some content", node_type="concept")
        node = store.get_node(nid)
        assert node["title"] == "Test Topic"
        assert node["content"] == "Some content"
        assert node["type"] == "concept"

    def test_add_with_domains(self, store):
        nid = store.add_node("D", domains=["eng", "research"])
        node = store.get_node(nid)
        assert node["domains"] == ["eng", "research"]

    def test_get_by_title(self, store):
        store.add_node("Unique Title", node_id="ut1")
        node = store.get_node_by_title("Unique Title")
        assert node["id"] == "ut1"
        assert store.get_node_by_title("unique title") is not None  # case insensitive

    def test_get_node_domains_is_non_mutating(self, store):
        nid = store.add_node("Tagged", domains=["antigravity", "hooks"])
        # Returns the node's domains without touching last_accessed (non-mutating).
        before = store.conn.execute(
            "SELECT last_accessed FROM nodes WHERE id = ?", (nid,)).fetchone()[0]
        assert store.get_node_domains(nid) == ["antigravity", "hooks"]
        after = store.conn.execute(
            "SELECT last_accessed FROM nodes WHERE id = ?", (nid,)).fetchone()[0]
        assert before == after
        # Missing node / empty id -> empty list, never raises.
        assert store.get_node_domains("does-not-exist") == []
        assert store.get_node_domains("") == []

    def test_update_node(self, store):
        nid = store.add_node("Original")
        store.update_node(nid, title="Updated", weight=0.9)
        node = store.get_node(nid)
        assert node["title"] == "Updated"
        assert node["weight"] == 0.9

    def test_same_id_upsert_preserves_relationships(self, store):
        store.add_node("Original", node_id="same")
        store.add_node("Peer", node_id="peer")
        store.add_edge("same", "peer", provenance="test")
        before_rowid = store.conn.execute(
            "SELECT rowid FROM nodes WHERE id = 'same'"
        ).fetchone()[0]

        store.add_node("Updated", node_id="same")

        assert store.get_node("same")["title"] == "Updated"
        after_rowid = store.conn.execute(
            "SELECT rowid FROM nodes WHERE id = 'same'"
        ).fetchone()[0]
        assert after_rowid == before_rowid
        assert {edge["to_id"] for edge in store.edges_from("same")} == {"peer"}

    def test_delete_node(self, store):
        nid = store.add_node("Doomed")
        store.delete_node(nid)
        assert store.get_node(nid) is None

    def test_all_nodes(self, store):
        store.add_node("A", node_type="concept")
        store.add_node("B", node_type="skill")
        store.add_node("C", node_type="concept")
        assert len(store.all_nodes()) == 3
        assert len(store.all_nodes(node_type="concept")) == 2

    def test_recent_nodes(self, store):
        store.add_node("Old")
        store.add_node("New")
        recent = store.recent_nodes(n=1)
        assert len(recent) == 1
        assert recent[0]["title"] == "New"

    def test_node_ids(self, store):
        store.add_node("A", node_id="a1")
        store.add_node("B", node_id="b2")
        ids = store.node_ids()
        assert "a1" in ids
        assert "b2" in ids


class TestEdgeOperations:
    def test_add_edge_bidirectional(self, store):
        store.add_node("A", node_id="a")
        store.add_node("B", node_id="b")
        store.add_edge("a", "b", provenance="test")
        assert len(store.edges_from("a")) == 1
        assert len(store.edges_to("a")) == 1  # bidirectional creates reverse

    def test_edges_from(self, store):
        store.add_node("X", node_id="x")
        store.add_node("Y", node_id="y")
        store.add_edge("x", "y", edge_type="implements", weight=0.9)
        edges = store.edges_from("x")
        assert edges[0]["to_id"] == "y"
        assert edges[0]["type"] == "implements"
        assert edges[0]["weight"] == 0.9

    def test_orphans(self, store):
        store.add_node("Lonely", node_id="lonely")
        store.add_node("Connected", node_id="conn")
        store.add_node("Also Connected", node_id="also")
        store.add_edge("conn", "also")
        orphans = store.orphans()
        assert len(orphans) == 1
        assert orphans[0]["id"] == "lonely"


class TestFTS:
    def test_fts_search(self, store):
        store.add_node("Stigmergy Coordination", content="Agents communicate indirectly",
                        node_id="stig")
        store.add_node("Database Design", content="Schema normalization", node_id="db")
        results = store.fts_search("stigmergy")
        assert len(results) >= 1
        assert results[0]["id"] == "stig"

    def test_fts_no_results(self, store):
        store.add_node("Something", content="content")
        results = store.fts_search("zzzznonexistent")
        assert results == []


class TestTagFiltering:
    def test_all_nodes_filter_single_tag(self, store):
        store.add_node("A", domains=["python", "web"])
        store.add_node("B", domains=["python", "ml"])
        store.add_node("C", domains=["rust"])
        results = store.all_nodes(tags=["python"])
        assert len(results) == 2

    def test_all_nodes_filter_multiple_tags_and_logic(self, store):
        store.add_node("A", domains=["python", "web"])
        store.add_node("B", domains=["python", "ml"])
        results = store.all_nodes(tags=["python", "web"])
        assert len(results) == 1
        assert results[0]["title"] == "A"

    def test_all_nodes_filter_no_match(self, store):
        store.add_node("A", domains=["python"])
        results = store.all_nodes(tags=["java"])
        assert len(results) == 0

    def test_add_node_with_tags_alias(self, store):
        nid = store.add_node("X", tags=["alpha", "beta"])
        node = store.get_node(nid)
        assert "alpha" in node["domains"]
        assert "beta" in node["domains"]
        assert node["tags"] == node["domains"]

    def test_add_node_tags_supplement_domains(self, store):
        nid = store.add_node("Y", domains=["auto"], tags=["user"])
        node = store.get_node(nid)
        assert "auto" in node["domains"]
        assert "user" in node["domains"]

    def test_update_node_with_tags(self, store):
        nid = store.add_node("Z", domains=["old"])
        store.update_node(nid, tags=["new1", "new2"])
        node = store.get_node(nid)
        assert "new1" in node["domains"]
        assert "new2" in node["domains"]

    def test_row_to_dict_includes_tags(self, store):
        nid = store.add_node("T", domains=["x", "y"])
        node = store.get_node(nid)
        assert "tags" in node
        assert node["tags"] == ["x", "y"]

    def test_tag_filter_no_partial_match(self, store):
        """Tag 'ml' should not match 'html'."""
        store.add_node("A", domains=["html"])
        store.add_node("B", domains=["ml"])
        results = store.all_nodes(tags=["ml"])
        assert len(results) == 1
        assert results[0]["title"] == "B"


class TestStats:
    def test_stats(self, store):
        store.add_node("A", node_id="a")
        store.add_node("B", node_id="b")
        store.add_edge("a", "b")
        s = store.stats()
        assert s["nodes"] == 2
        assert s["edges"] >= 1

    def test_stats_use_same_explicit_node_vocabulary(self, store):
        store.add_node("Knowledge", node_id="knowledge")
        store.add_node("Session", node_id="session", node_type="session")

        stats = store.stats()

        assert stats["nodes"] == stats["semantic_nodes"] == 1
        assert stats["metrics_schema"] == 2
        assert stats["stored_nodes"] == 2
