"""Tests for Store-based graph algorithms."""

import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.graph import (
    build_nx_from_store,
    store_bridges,
    store_centrality,
    store_communities,
    store_stats,
    store_trailheads,
)
from kindex.store import Store


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    # Build a small graph
    s.add_node("Hub concept", node_id="hub", weight=1.0)
    s.add_node("Spoke A", node_id="a", weight=0.8)
    s.add_node("Spoke B", node_id="b", weight=0.7)
    s.add_node("Spoke C", node_id="c", weight=0.6)
    s.add_node("Far node", node_id="far", weight=0.5)
    s.add_edge("hub", "a", weight=0.9)
    s.add_edge("hub", "b", weight=0.8)
    s.add_edge("hub", "c", weight=0.7)
    s.add_edge("a", "far", weight=0.5)
    yield s
    s.close()


class TestBuildNX:
    def test_builds_graph(self, store):
        G = build_nx_from_store(store)
        assert G.number_of_nodes() == 5
        assert G.number_of_edges() >= 4  # bidirectional adds reverse edges

    def test_empty_store(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)
        G = build_nx_from_store(s)
        assert G.number_of_nodes() == 0
        s.close()

    def test_excludes_sessions_and_derived_domain_edges(self, store):
        from kindex.schema import LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE

        store.add_node("Session", node_id="session", node_type="session")
        store.add_edge("hub", "session", provenance="session-tag")
        store.add_edge(
            "a", "b", provenance=LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE
        )

        graph = build_nx_from_store(store)

        assert "session" not in graph
        assert not graph.has_edge("a", "b")
        assert graph.has_edge("hub", "a")

    def test_semantic_edge_reads_exclude_session_at_either_endpoint(self, store):
        store.add_node("Session", node_id="session", node_type="session")
        store.add_edge("hub", "session", provenance="session-tag")

        assert all(
            edge["to_id"] != "session"
            for edge in store.edges_from("hub", semantic_only=True)
        )
        assert store.edges_from("session", semantic_only=True) == []
        assert store.edges_to("session", semantic_only=True) == []


class TestStoreStats:
    def test_stats(self, store):
        stats = store_stats(store)
        assert stats["nodes"] == 5
        assert stats["edges"] >= 4
        assert stats["density"] > 0
        assert stats["components"] >= 1
        assert stats["avg_degree"] > 0
        assert stats["max_degree_node"] != ""

    def test_empty_stats(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)
        stats = store_stats(s)
        assert stats["nodes"] == 0
        s.close()

    def test_reports_stored_topology_separately(self, store):
        from kindex.schema import LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE

        store.add_node("Session", node_id="session", node_type="session")
        store.add_edge("hub", "session", provenance="session-tag")
        store.add_edge(
            "a", "b", provenance=LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE
        )

        stats = store_stats(store)

        assert stats["nodes"] == 5
        assert stats["semantic_nodes"] == 5
        assert stats["stored_nodes"] == 6
        assert stats["stored_edges"] > stats["edges"]
        assert stats["ignored_domain_edges"] == 2
        assert stats["ignored_session_edges"] == 2

    def test_orphans_ignore_session_and_domain_only_links(self, store):
        from kindex.schema import LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE

        store.add_node("Session", node_id="session", node_type="session")
        store.add_node("Domain only", node_id="domain-only")
        store.add_node("Peer", node_id="peer")
        store.add_edge(
            "domain-only",
            "peer",
            provenance=LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,
        )

        orphan_ids = {node["id"] for node in store.orphans()}

        assert "session" not in orphan_ids
        assert {"domain-only", "peer"} <= orphan_ids


class TestCentrality:
    def test_betweenness(self, store):
        results = store_centrality(store, method="betweenness", top_k=5)
        assert len(results) > 0
        # Hub should be most central
        ids = [r[0] for r in results]
        assert "hub" in ids[:3]

    def test_degree(self, store):
        results = store_centrality(store, method="degree", top_k=5)
        assert len(results) > 0

    def test_closeness(self, store):
        results = store_centrality(store, method="closeness", top_k=5)
        assert len(results) > 0


class TestCommunities:
    def test_detects_communities(self, store):
        comms = store_communities(store)
        assert isinstance(comms, list)
        # With 5 nodes, should have at least 1 community
        if comms:
            assert isinstance(comms[0], list)
            assert "id" in comms[0][0]
            assert "title" in comms[0][0]


class TestBridges:
    def test_finds_bridges(self, store):
        bridges = store_bridges(store, top_k=5)
        assert isinstance(bridges, list)
        if bridges:
            assert "from_title" in bridges[0]
            assert "to_title" in bridges[0]
            assert "betweenness" in bridges[0]


class TestTrailheads:
    def test_finds_trailheads(self, store):
        trails = store_trailheads(store, top_k=5)
        assert isinstance(trails, list)
        if trails:
            assert "id" in trails[0]
            assert "score" in trails[0]
            assert "out_degree" in trails[0]
            # Hub should be a trailhead
            ids = [t["id"] for t in trails]
            assert "hub" in ids


class TestGraphCLI:
    def test_graph_stats(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Concept A is about testing", data_dir=d)
        run("add", "Concept B is about graphs", data_dir=d)
        r = run("graph", "stats", data_dir=d)
        assert r.returncode == 0
        assert "Nodes" in r.stdout

    def test_graph_centrality(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Alpha concept", data_dir=d)
        run("add", "Beta concept", data_dir=d)
        r = run("graph", "centrality", data_dir=d)
        assert r.returncode == 0

    def test_graph_json(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Test node", data_dir=d)
        r = run("graph", "stats", "--json", data_dir=d)
        assert r.returncode == 0
        import json
        data = json.loads(r.stdout)
        assert "nodes" in data
