"""What retrieval shows is what the reader may see, ranked by relevance first.

- Neighbour titles skipped the archived, expired and trusted-only fences.
- Standing sorted every candidate, so a graph neighbour or a distant vector
  hit with any standing pushed out the best text match.
- trusted_only filled its window with unverified rows and missed verified
  matches ranked just below it.
- The prime topic came from every word of the absolute path.
"""

from __future__ import annotations

import subprocess

import pytest

from kindex.config import Config
from kindex.retrieve import detect_domain_from_path, hybrid_search
from kindex.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    graph = Store(Config(data_dir=str(tmp_path / "data")))
    yield graph
    graph.close()


def verify(store, node_id):
    store.verify_node(node_id, verified_by="reviewer", prov_method="manual-review",
                      verified_at="2020-01-01T00:00:00+00:00")


def titles(results):
    return [r["title"] for r in results]


def neighbour_titles(result):
    return [edge.get("to_title") for edge in result.get("edges_out", [])]


def test_neighbours_pass_the_same_fences_as_results(store):
    store.add_node("deploy pipeline", "how we deploy things", node_id="a")
    store.add_node("old credentials location", "retired", node_id="b")
    store.add_node("live runbook", "current", node_id="live")
    store.add_edge("a", "b", edge_type="relates_to")
    store.add_edge("a", "live", edge_type="relates_to")
    store.update_node("b", status="archived")
    store.add_node("expired claim", "no longer true", node_id="c",
                   extra={"expires": "2020-01-01"})
    store.add_edge("a", "c", edge_type="relates_to")
    results = hybrid_search(store, "deploy pipeline", top_k=3, use_vectors=False)
    names = neighbour_titles(next(r for r in results if r["id"] == "a"))
    assert names == ["live runbook"], names


def test_a_trusted_block_names_only_trusted_neighbours(store):
    store.add_node("deploy fact", "deploy fact verified", node_id="v")
    store.add_node("unreviewed neighbour", "not verified", node_id="u")
    store.add_node("reviewed neighbour", "verified", node_id="r")
    for target in ("u", "r"):
        store.add_edge("v", target, edge_type="relates_to")
    verify(store, "v")
    verify(store, "r")
    results = hybrid_search(store, "deploy fact", top_k=3, use_vectors=False,
                            trusted_only=True)
    assert "unreviewed neighbour" not in titles(results)
    fact = next(r for r in results if r["id"] == "v")
    assert neighbour_titles(fact) == ["reviewed neighbour"]


def test_prime_does_not_name_a_neighbour_scoped_to_another_client(store):
    from kindex.hooks import prime_context
    store.add_node("deploy pipeline", "how we deploy things", node_id="a", domains=["deploy"])
    store.add_node("antigravity hook protocol", "nested toolCall", node_id="g",
                   domains=["antigravity"])
    store.add_node("shared runbook", "for everyone", node_id="s")
    store.add_edge("a", "g", edge_type="relates_to", weight=0.9)
    store.add_edge("a", "s", edge_type="relates_to", weight=0.5)
    block = prime_context(store, topic="deploy pipeline", adapter="claude")
    assert "shared runbook" in block
    assert "antigravity hook protocol" not in block


def test_a_neighbour_with_standing_does_not_outrank_the_match(store):
    store.add_node("payment reconciliation runbook", "payment reconciliation steps",
                   node_id="exact", weight=0.9)
    for n in range(8):
        store.add_node(f"lunch menu ideas {n}", "sandwiches", node_id=f"lunch{n}",
                       standing="present")
        store.add_edge("exact", f"lunch{n}", edge_type="relates_to", weight=0.1)
    results = hybrid_search(store, "payment reconciliation", top_k=8, use_vectors=False)
    assert titles(results)[0] == "payment reconciliation runbook", titles(results)


def test_standing_still_orders_the_matches_themselves(store):
    store.add_node("retry policy note", "retry policy three attempts", node_id="present",
                   standing="present", weight=0.9)
    store.add_node("retry policy ruling", "retry policy three attempts", node_id="ratified",
                   standing="ratified", weight=0.1)
    results = hybrid_search(store, "retry policy", top_k=2, use_vectors=False,
                            expand_graph=False)
    assert titles(results) == ["retry policy ruling", "retry policy note"]


def test_trusted_recall_finds_a_verified_match_below_the_unverified_window(store):
    for n in range(20):
        store.add_node(f"deploy note {n}", "deploy deploy deploy pipeline", node_id=f"f{n}")
    store.add_node("verified deploy fact", "mentions deploy once", node_id="verified")
    verify(store, "verified")
    stats: dict = {}
    results = hybrid_search(store, "deploy", top_k=5, use_vectors=False,
                            expand_graph=False, trusted_only=True, fence_stats=stats)
    assert titles(results) == ["verified deploy fact"]


def test_the_prime_topic_is_the_project_not_the_path(store, tmp_path):
    project = tmp_path / "users" / "someone" / "code" / "myproj"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    store.add_node("myproj architecture", "layers", node_id="p", domains=["myproj", "rust"])
    for n in range(5):
        store.add_node(f"preference {n}", "users like dark mode code", node_id=f"pref{n}",
                       domains=[f"standing{n}"], standing="present")
    assert detect_domain_from_path(store, str(project)) == ["myproj", "rust"]


def test_a_project_nothing_names_has_no_detected_domains(store, tmp_path):
    project = tmp_path / "users" / "code" / "unnamed"
    project.mkdir(parents=True)
    store.add_node("preference", "users like code", node_id="pref", domains=["ui"])
    assert detect_domain_from_path(store, str(project)) == []
