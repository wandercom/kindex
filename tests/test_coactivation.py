"""Learned pair co-activation — a third channel, kept separate on purpose.

The design constraint these tests defend is not the math, it is the boundary.
`edges.weight` is topology ASSERTED by a human or an agent. Folding a learned
correction into it destroys the told/inferred distinction, after which the
graph can no longer tell you what it was told from what it inferred. So the
raw signal lives in its own table, the applied correction is its own ensemble
weight, and the two never merge.

The deposit gate matters just as much: co-retrieval is not usefulness.
Strengthening on mere co-occurrence would teach the graph the retriever's own
biases and then present the result as evidence.
"""

from __future__ import annotations

import pytest

from kindex.config import Config
from kindex.reinforce import (
    ReinforceOutcome,
    _deposit_coactivation,
    auto_ramp_coactivation_weight,
    learned_coactivation_weight,
)
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


def _backdate(store, days):
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE node_coactivation SET last_decay = ?", (past,))
    store.conn.commit()


# ── The bounded update ────────────────────────────────────────────────

def test_bounded_update_saturates_instead_of_running_away(store):
    """w <- w + eta*(1-w): the one piece of Hillock's math worth taking.

    A pair confirmed a hundred times should saturate near 1.0, not dominate
    the channel the way an unbounded additive deposit would.
    """
    a, b = store.add_node("a"), store.add_node("b")
    for _ in range(100):
        strength = store.deposit_coactivation(a, b, eta=0.15)
    assert strength < 1.0
    assert strength > 0.99


def test_first_deposit_equals_eta(store):
    a, b = store.add_node("a"), store.add_node("b")
    assert store.deposit_coactivation(a, b, eta=0.15) == pytest.approx(0.15)


def test_second_deposit_follows_the_bounded_form(store):
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b, eta=0.15)
    # 0.15 + 0.15*(1-0.15) = 0.2775
    assert store.deposit_coactivation(a, b, eta=0.15) == pytest.approx(0.2775)


def test_pair_order_is_canonical(store):
    """One row per pair, whichever way round the caller asks."""
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b)
    store.deposit_coactivation(b, a)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM node_coactivation").fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT events FROM node_coactivation").fetchone()[0] == 2


def test_self_pair_is_rejected(store):
    a = store.add_node("a")
    assert store.deposit_coactivation(a, a) == 0.0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM node_coactivation").fetchone()[0] == 0


# ── Decay ─────────────────────────────────────────────────────────────

def test_strength_decays(store):
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b, eta=0.8, half_life_days=14.0)
    _backdate(store, 14)
    scores = dict(store.coactivation_scores({a, b}, min_events=1))
    assert scores[a] == pytest.approx(0.4, abs=0.01)


def test_decay_prunes_dead_pairs(store):
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b, eta=0.15)
    _backdate(store, 365)
    assert store.decay_coactivation(half_life_days=14.0, floor=0.02) == 1
    assert store.conn.execute(
        "SELECT COUNT(*) FROM node_coactivation").fetchone()[0] == 0


# ── Reads ─────────────────────────────────────────────────────────────

def test_min_events_suppresses_anecdotes(store):
    """A single co-occurrence is an anecdote, not signal."""
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b)
    assert store.coactivation_scores({a, b}, min_events=3) == []
    store.deposit_coactivation(a, b)
    store.deposit_coactivation(a, b)
    assert store.coactivation_scores({a, b}, min_events=3) != []


def test_score_credits_only_requested_endpoints(store):
    a, b = store.add_node("a"), store.add_node("b")
    store.deposit_coactivation(a, b, eta=0.5)
    scores = dict(store.coactivation_scores({a}, min_events=1))
    assert set(scores) == {a}


def test_empty_query_returns_nothing(store):
    assert store.coactivation_scores(set()) == []


# ── The deposit gate ──────────────────────────────────────────────────

def _outcome(nid, category="used", injected=True):
    return ReinforceOutcome(nid, "t", category, 1.0, 1.0, "evidence",
                            injected=injected)


def test_confirmed_use_deposits_pairs(store):
    cfg = store.config
    ids = [store.add_node(f"n{i}") for i in range(3)]
    n = _deposit_coactivation(store, cfg, [_outcome(i) for i in ids], "")
    assert n == 3  # 3 choose 2


def test_counterfactual_outcomes_do_not_deposit(store):
    """A node that was never injected has no co-activation to observe."""
    cfg = store.config
    ids = [store.add_node(f"n{i}") for i in range(3)]
    outcomes = [_outcome(i, category="inferred", injected=False) for i in ids]
    assert _deposit_coactivation(store, cfg, outcomes, "") == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM node_coactivation").fetchone()[0] == 0


def test_ignored_injections_do_not_deposit(store):
    """Co-RETRIEVAL is not usefulness — this is the whole gate.

    Depositing here would learn the retriever's own biases and call the result
    evidence.
    """
    cfg = store.config
    ids = [store.add_node(f"n{i}") for i in range(3)]
    # 'agent_admission' is a counterfactual category, not a confirmed use.
    outcomes = [_outcome(i, category="agent_admission") for i in ids]
    assert _deposit_coactivation(store, cfg, outcomes, "") == 0


def test_single_confirmed_node_makes_no_pair(store):
    cfg = store.config
    assert _deposit_coactivation(store, cfg, [_outcome(store.add_node("a"))], "") == 0


def test_pair_count_is_capped(store):
    """Quadratic growth: 30 nodes would otherwise write 435 rows."""
    cfg = store.config
    cfg.attention.coactivation_max_pairs = 10
    ids = [store.add_node(f"n{i}") for i in range(20)]
    assert _deposit_coactivation(store, cfg, [_outcome(i) for i in ids], "") == 10


def test_disabled_config_deposits_nothing(store):
    cfg = store.config
    cfg.attention.coactivation_enabled = False
    ids = [store.add_node(f"n{i}") for i in range(3)]
    assert _deposit_coactivation(store, cfg, [_outcome(i) for i in ids], "") == 0


# ── The boundary that matters ─────────────────────────────────────────

def test_coactivation_never_touches_edge_weight(store):
    """The load-bearing invariant.

    `edges.weight` is asserted topology. If a learned correction were folded
    into it, the graph could no longer distinguish what it was told from what
    it inferred.
    """
    a, b = store.add_node("a"), store.add_node("b")
    store.add_edge(a, b, edge_type="relates_to", weight=0.30)

    for _ in range(50):
        store.deposit_coactivation(a, b, eta=0.15)

    edge = store.edges_from(a)[0]
    assert edge["weight"] == pytest.approx(0.30)


def test_coactivation_does_not_invent_edges(store):
    """A learned pair is not an asserted relationship."""
    a, b = store.add_node("a"), store.add_node("b")
    for _ in range(10):
        store.deposit_coactivation(a, b)
    assert store.edges_from(a) == []


# ── Auto-ramp: raw signal vs applied correction ───────────────────────

def test_channel_is_inert_until_warm(store):
    """The applied correction stays zero while the raw signal accumulates."""
    cfg = store.config
    a, b = store.add_node("a"), store.add_node("b")
    for _ in range(5):
        store.deposit_coactivation(a, b)
    ramp = auto_ramp_coactivation_weight(store, cfg)
    assert ramp["weight"] == 0.0
    assert learned_coactivation_weight(store) == 0.0
    # ...but the raw signal is recorded regardless.
    assert store.coactivation_stats()["pairs"] == 1


def test_ramp_lifts_weight_once_warm(store):
    cfg = store.config
    cfg.attention.coactivation_min_warm_pairs = 2
    cfg.attention.coactivation_min_signal = 1.0
    cfg.attention.coactivation_full_signal = 3.0
    ids = [store.add_node(f"n{i}") for i in range(4)]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            for _ in range(20):
                store.deposit_coactivation(a, b, eta=0.5)
    ramp = auto_ramp_coactivation_weight(store, cfg)
    assert ramp["weight"] > 0
    assert learned_coactivation_weight(store) == ramp["weight"]


def test_ramp_falls_back_when_signal_cools(store):
    """A learned weight must fall when its evidence decays, not persist."""
    cfg = store.config
    cfg.attention.coactivation_min_warm_pairs = 2
    cfg.attention.coactivation_min_signal = 1.0
    ids = [store.add_node(f"n{i}") for i in range(4)]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            for _ in range(20):
                store.deposit_coactivation(a, b, eta=0.5)
    assert auto_ramp_coactivation_weight(store, cfg)["weight"] > 0

    _backdate(store, 365)
    assert auto_ramp_coactivation_weight(store, cfg)["weight"] == 0.0


def test_ensemble_weight_is_opt_in_by_default():
    assert Config().ranking.coactivation_weight == 0.0
    assert "coactivation" not in Config().ranking.ensemble_weights
