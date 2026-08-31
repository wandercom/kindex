"""Retrieval grounding: the calibrated similarity floor and its verdict.

The defect: `vector_search` returned the top-k nearest neighbours for any query
however nonsensical, and `ask()` could only reach its "no relevant knowledge"
branch when the result list was empty — which vector search made unreachable.
The graph could never say it knows nothing.

These tests hold the line on the three design decisions that make the fix safe:
the floor is a versioned record and never a config number; the verdict is
computed but NOT enforced by default (silent false negatives are worse than
loud false positives); and `uncalibrated` stays distinct from `ungrounded`.
"""

from __future__ import annotations

import pytest

from kindex.config import Config
from kindex.grounding import (
    GROUNDED,
    UNCALIBRATED,
    UNGROUNDED,
    WEAK,
    CalibrationRecord,
    calibration_is_stale,
    evaluate,
    load_calibration,
    save_calibration,
    similarity_from_distance,
)
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


def _record(store, floor=0.5, embedding_count=100, **kw):
    rec = CalibrationRecord(
        provider=kw.get("provider", "voyage"),
        model=kw.get("model", "voyage-context-4"),
        floor=floor,
        percentile=95.0,
        sample_size=200,
        corpus_node_count=1000,
        embedding_count=embedding_count,
        calibrated_at="2026-08-31T12:00:00",
    )
    save_calibration(store, rec)
    return rec


def _fake_vector_meta(store, count):
    """Populate node_vector_meta without needing the sqlite-vec extension.

    The real table is created by `ensure_vec_table` only when sqlite-vec is
    installed; staleness only reads a COUNT, so a plain table is enough.
    """
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS node_vector_meta ("
        "node_id TEXT, vector_id TEXT, chunk_index INTEGER)")
    store.conn.executemany(
        "INSERT INTO node_vector_meta (node_id, vector_id) VALUES (?, ?)",
        [(f"n{i}", f"n{i}") for i in range(count)])
    store.conn.commit()


# ── Distance/similarity conversion ────────────────────────────────────

def test_similarity_conversion_uses_the_l2_identity():
    """sqlite-vec vec0 returns L2 distance, NOT cosine distance.

    For unit-normalised embeddings d^2 = 2(1 - cos), so cos = 1 - d^2/2. The
    naive `1 - d` collapses the entire useful range to zero — real distances
    on the live index run 0.94 (strong match) to 1.32 (null query) — which
    calibrates a floor of exactly 0.0 and a gate that never fires.
    """
    assert similarity_from_distance(0.0) == 1.0          # identical
    assert similarity_from_distance(2.0 ** 0.5) == pytest.approx(0.0)  # orthogonal
    assert similarity_from_distance(2.0) == 0.0          # opposed, clamped
    assert similarity_from_distance(None) == 0.0
    # The band that actually matters, and that `1 - d` destroyed:
    assert similarity_from_distance(0.94) == pytest.approx(0.5582, abs=1e-4)
    assert similarity_from_distance(1.29) == pytest.approx(0.1680, abs=1e-4)


def test_real_matches_outrank_null_queries():
    """The property the floor depends on: the conversion must discriminate."""
    real = similarity_from_distance(0.94)
    null = similarity_from_distance(1.29)
    assert real > null
    assert real - null > 0.3


# ── Calibration records ───────────────────────────────────────────────

def test_calibration_roundtrips(store):
    rec = _record(store, floor=0.42)
    loaded = load_calibration(store, "voyage", "voyage-context-4")
    assert loaded == rec


def test_missing_calibration_returns_none(store):
    assert load_calibration(store, "voyage", "nope") is None


def test_unreadable_calibration_is_not_a_guessed_floor(store):
    """Corrupt record must degrade to uncalibrated, never to a default number."""
    store.set_meta("grounding.calibration.voyage:voyage-context-4", "{not json")
    assert load_calibration(store, "voyage", "voyage-context-4") is None


def test_calibration_history_is_kept(store):
    """A floor that moves silently is the same defect as a config number."""
    _record(store, floor=0.4)
    keys = [r["key"] for r in store.conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'grounding.calibration.history.%'")]
    assert len(keys) == 1


# ── Staleness ─────────────────────────────────────────────────────────

def test_calibration_goes_stale_when_coverage_moves(store):
    """A floor calibrated at 1.6% coverage does not survive a backfill."""
    cfg = store.config
    rec = _record(store, embedding_count=100)
    _fake_vector_meta(store, 200)
    assert "coverage moved" in calibration_is_stale(store, rec, cfg)


def test_calibration_is_fresh_within_tolerance(store):
    cfg = store.config
    rec = _record(store, embedding_count=100)
    _fake_vector_meta(store, 100)
    assert calibration_is_stale(store, rec, cfg) == ""


# ── Verdicts ──────────────────────────────────────────────────────────

def test_no_calibration_yields_uncalibrated_not_ungrounded(store):
    """"No yardstick" and "measured nothing" are different facts.

    Conflating them lets a missing calibration masquerade as a confident
    negative — the graph would claim it knows nothing when it simply has not
    been measured.
    """
    v = evaluate(store, store.config, [("a", 0.9)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == UNCALIBRATED
    assert v.verdict != UNGROUNDED


def test_stale_calibration_is_uncalibrated(store):
    _record(store, embedding_count=1)
    _fake_vector_meta(store, 500)
    v = evaluate(store, store.config, [("a", 0.9)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == UNCALIBRATED
    assert "stale" in v.reason


def test_best_hit_above_floor_is_grounded(store):
    _record(store, floor=0.5)
    v = evaluate(store, store.config, [("a", 0.92), ("b", 0.60)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == GROUNDED
    assert v.best_similarity == 0.92


def test_best_hit_just_above_floor_is_weak(store):
    """Clearing the floor by a hair is not the same as being grounded."""
    _record(store, floor=0.5)  # weak_margin 1.15 -> grounded needs >= 0.575
    v = evaluate(store, store.config, [("a", 0.52)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == WEAK


def test_nothing_clears_the_floor_is_ungrounded(store):
    _record(store, floor=0.5)
    v = evaluate(store, store.config, [("a", 0.31), ("b", 0.22)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == UNGROUNDED
    assert v.best_similarity == 0.31
    assert v.is_grounded is False


def test_empty_result_set_is_ungrounded(store):
    _record(store, floor=0.5)
    v = evaluate(store, store.config, [],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == UNGROUNDED


# ── Shadow mode ───────────────────────────────────────────────────────

def test_shadow_mode_is_the_default(store):
    """Enforcement must be opt-in.

    A miscalibrated floor turns "irrelevant results contaminate context" —
    annoying, visible, self-correcting — into "empty context, agent proceeds
    without knowledge that was there." Silent false negatives are strictly
    worse, so the gate reports before it withholds.
    """
    assert store.config.grounding.enforce is False
    _record(store, floor=0.5)
    v = evaluate(store, store.config, [("a", 0.1)],
                 provider="voyage", model="voyage-context-4")
    assert v.verdict == UNGROUNDED
    assert v.enforced is False
    assert v.dropped == 0
    assert "shadow mode" in v.note()


def test_enforcing_mode_reports_drops(store):
    cfg = store.config
    cfg.grounding.enforce = True
    _record(store, floor=0.5)
    v = evaluate(store, cfg, [("a", 0.1), ("b", 0.05)],
                 provider="voyage", model="voyage-context-4")
    assert v.enforced is True
    assert v.dropped == 2
    assert "shadow mode" not in v.note()


def test_near_misses_are_recorded_for_the_audit_trail(store):
    """"Kindex says it doesn't know X but I know X is in there" needs evidence."""
    _record(store, floor=0.5)
    v = evaluate(store, store.config, [("a", 0.49), ("b", 0.31), ("c", 0.12)],
                 provider="voyage", model="voyage-context-4")
    assert v.near_misses == [0.49, 0.31, 0.12]


def test_grounding_can_be_disabled_entirely(store):
    cfg = store.config
    cfg.grounding.enabled = False
    v = evaluate(store, cfg, [], provider="voyage", model="voyage-context-4")
    assert v.verdict == GROUNDED
    assert v.is_grounded


# ── The floor never lives in config ───────────────────────────────────

def test_config_holds_policy_not_the_floor():
    """Regression guard on the design itself.

    If a `floor` field ever appears on GroundingConfig, the number has escaped
    its versioned record and can be falsified by a stale edit.
    """
    fields = set(Config().grounding.model_dump().keys())
    assert "floor" not in fields
    assert "floor_percentile" in fields


def test_missing_vector_table_does_not_fake_staleness(store):
    """No index table means "cannot tell", which must not read as stale.

    Reporting stale here would flip every store without sqlite-vec into a
    permanent `uncalibrated` verdict for a reason unrelated to the corpus.
    """
    rec = _record(store, embedding_count=100)
    assert calibration_is_stale(store, rec, store.config) == ""
