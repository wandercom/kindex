"""Bounded multi-hop graph expansion.

Kindex expanded exactly one hop from the top five FTS hits. Depth was the real
limit and it cost nothing to lift: measured on the live 113,355-edge graph, a
3-hop walk runs in the same wall-clock as 1 hop, both inside sqlite3
process-startup noise. Retrieval latency is the embedding round-trip, not BFS.

Two properties are load-bearing and are what these tests defend: the beam is
mandatory (max observed out-fanout is 849), and the beam's ordering is total
and stable, because a nondeterministic walk in a provenance-first graph is a
worse defect than a slow one.
"""

from __future__ import annotations

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


def _chain(store, n, weight=1.0):
    """a0 -> a1 -> ... -> a{n-1}, returning the ids in order."""
    ids = [store.add_node(f"node {i}") for i in range(n)]
    for a, b in zip(ids, ids[1:]):
        store.add_edge(a, b, edge_type="relates_to", weight=weight)
    return ids


def test_single_hop_matches_the_old_behaviour(store):
    ids = _chain(store, 3)
    out = store.expand_multihop({ids[0]: 1.0}, max_hops=1)
    assert [nid for nid, _ in out] == [ids[1]]


def test_two_hops_reach_further(store):
    """The whole point: depth now actually extends reach."""
    ids = _chain(store, 4)
    out = store.expand_multihop({ids[0]: 1.0}, max_hops=2)
    assert [nid for nid, _ in out] == [ids[1], ids[2]]


def test_three_hops_reach_further_still(store):
    ids = _chain(store, 5)
    out = store.expand_multihop({ids[0]: 1.0}, max_hops=3)
    assert {nid for nid, _ in out} == {ids[1], ids[2], ids[3]}


def test_score_attenuates_with_distance(store):
    """A 2-hop neighbour must not outrank a 1-hop one on edge weight alone."""
    ids = _chain(store, 3, weight=1.0)
    out = dict(store.expand_multihop({ids[0]: 1.0}, max_hops=2, hop_decay=0.5))
    assert out[ids[1]] > out[ids[2]]
    assert out[ids[1]] == pytest.approx(0.5)
    assert out[ids[2]] == pytest.approx(0.25)


def test_seeds_are_excluded(store):
    """The caller already has the seeds; returning them double-counts."""
    ids = _chain(store, 3)
    out = store.expand_multihop({ids[0]: 1.0}, max_hops=2)
    assert ids[0] not in {nid for nid, _ in out}


def test_cycles_terminate(store):
    a, b, c = (store.add_node(t) for t in ("a", "b", "c"))
    store.add_edge(a, b, weight=1.0)
    store.add_edge(b, c, weight=1.0)
    store.add_edge(c, a, weight=1.0)
    out = store.expand_multihop({a: 1.0}, max_hops=10)
    assert {nid for nid, _ in out} == {b, c}


def test_beam_caps_the_frontier(store):
    """Mandatory, not tuning: max observed out-fanout on the live graph is 849."""
    hub = store.add_node("hub")
    for i in range(300):
        leaf = store.add_node(f"leaf {i}")
        store.add_edge(hub, leaf, weight=(i + 1) / 300.0)

    out = store.expand_multihop({hub: 1.0}, max_hops=1, beam=50)
    assert len(out) == 50


def test_beam_keeps_the_highest_scoring_neighbours(store):
    hub = store.add_node("hub")
    leaves = {}
    for i in range(100):
        leaf = store.add_node(f"leaf {i}")
        store.add_edge(hub, leaf, weight=(i + 1) / 100.0)
        leaves[leaf] = (i + 1) / 100.0

    kept = {nid for nid, _ in store.expand_multihop({hub: 1.0}, max_hops=1, beam=10)}
    expected = {nid for nid, _ in sorted(leaves.items(), key=lambda kv: -kv[1])[:10]}
    assert kept == expected


def test_beam_ordering_is_deterministic_under_ties(store):
    """The catch that matters: an unspecified beam makes traversal unexplainable.

    With every edge at the same weight the beam is decided purely by the
    tiebreak. If that were row order, adding an edge elsewhere in the hub's
    neighbourhood would silently change what a query returns.
    """
    hub = store.add_node("hub")
    for i in range(50):
        leaf = store.add_node(f"leaf {i}")
        store.add_edge(hub, leaf, weight=0.5)

    first = store.expand_multihop({hub: 1.0}, max_hops=1, beam=10)
    second = store.expand_multihop({hub: 1.0}, max_hops=1, beam=10)
    assert first == second
    # Ties break on node id ascending, so the selection is predictable.
    ids = [nid for nid, _ in first]
    assert ids == sorted(ids)


def test_best_score_wins_across_multiple_paths(store):
    """A node reachable two ways keeps its best score, not its last."""
    seed = store.add_node("seed")
    mid = store.add_node("mid")
    target = store.add_node("target")
    store.add_edge(seed, target, weight=0.9)   # 1 hop, strong
    store.add_edge(seed, mid, weight=0.9)
    store.add_edge(mid, target, weight=0.9)    # 2 hops, weaker after decay

    out = dict(store.expand_multihop({seed: 1.0}, max_hops=2, hop_decay=0.5))
    assert out[target] == pytest.approx(0.45)


def test_empty_seeds_return_nothing(store):
    assert store.expand_multihop({}, max_hops=3) == []


def test_zero_hops_return_nothing(store):
    ids = _chain(store, 3)
    assert store.expand_multihop({ids[0]: 1.0}, max_hops=0) == []


def test_isolated_seed_returns_nothing(store):
    """6,563 of the live graph's components are islands — this is the common case."""
    lonely = store.add_node("lonely")
    assert store.expand_multihop({lonely: 1.0}, max_hops=3) == []
