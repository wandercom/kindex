"""The extraction boundary and the eval gate (W2).

Placement is the whole design here. An extractor is an INPUT, never an
authority: its output lands in `capture_candidates`, the quarantine table no
query surfaces, and never in `nodes` or `edges`. That is what makes a
low-precision engine survivable — a wrong candidate costs a rejection click, a
wrong node costs the graph the only thing it has.

The gate tests defend a subtler point. Either eval metric alone is gameable:
grounding precision rewards an engine that only copies verbatim (100% by
construction), title recall rewards one that invents freely. So the gate is
two-part, and these tests hold that shape.
"""

from __future__ import annotations

import logging

import pytest

from kindex.config import Config
from kindex.extract_eval import (
    GROUNDING_TOLERANCE,
    EngineScore,
    _is_grounded,
    build_sample,
    run_eval,
    score_engine,
)
from kindex.extractors import (
    DETERMINISTIC,
    KEYWORD,
    LLM,
    DeterministicExtractor,
    ExtractionResult,
    KeywordExtractor,
    get_extractor,
    stage_candidates,
)
from kindex.store import Store

SAMPLE = (
    "The dream cycle merges near-duplicate nodes. Merging appends source "
    "content into the target, so an unguarded target grows without bound. "
    "We decided to cap merge growth because minified symbols are mutually "
    "similar by construction. An open question is whether the beam ordering "
    "should be stable across runs."
)


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


# ── Engine resolution ─────────────────────────────────────────────────

def test_keyword_engine_always_resolves():
    assert get_extractor(KEYWORD).name == KEYWORD


def test_unknown_engine_falls_back_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger="kindex.extractors"):
        extractor = get_extractor("nonsense")
    assert extractor.name == KEYWORD
    assert "unknown extraction engine" in caplog.text


def test_llm_engine_requires_config_and_ledger():
    with pytest.raises(ValueError):
        get_extractor(LLM)


def test_deterministic_degrades_when_extra_is_absent(caplog):
    """Selecting the 2.5 GB engine must never crash a hook or an ingest run."""
    if DeterministicExtractor.available():
        pytest.skip("kindex[talon] is installed; the fallback path cannot run")
    with caplog.at_level(logging.WARNING, logger="kindex.extractors"):
        result = get_extractor(DETERMINISTIC).extract(SAMPLE, [])
    assert "talon extra is not installed" in caplog.text
    assert result.engine == KEYWORD
    assert result.engine_version == "talon-unavailable"


# ── The shared shape ──────────────────────────────────────────────────

def test_every_engine_returns_the_same_shape():
    result = KeywordExtractor().extract(SAMPLE, [])
    assert isinstance(result, ExtractionResult)
    assert isinstance(result.concepts, list)
    assert result.engine == KEYWORD


def test_legacy_roundtrip_preserves_content():
    legacy = {"concepts": [{"title": "a"}], "decisions": [], "questions": [],
              "connections": [], "bridge_opportunities": []}
    result = ExtractionResult.from_legacy(legacy, engine=KEYWORD)
    assert result.to_legacy() == legacy


# ── The quarantine boundary ───────────────────────────────────────────

def test_staging_writes_candidates_not_nodes(store):
    """The load-bearing invariant of W2."""
    nodes_before = store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges_before = store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    result = ExtractionResult(
        concepts=[{"title": "Merge growth is unbounded", "content": "x"}],
        decisions=[{"title": "Cap merge growth", "rationale": "y"}],
        engine=KEYWORD)
    staged = stage_candidates(store, result, source="test")

    assert len(staged) == 2
    assert store.conn.execute(
        "SELECT COUNT(*) FROM capture_candidates").fetchone()[0] == 2
    assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == nodes_before
    assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == edges_before


def test_staged_candidates_carry_their_node_type(store):
    result = ExtractionResult(
        concepts=[{"title": "A concept", "content": "c"}],
        questions=[{"question": "An open question", "context": "q"}],
        engine=KEYWORD)
    stage_candidates(store, result)
    types = {r["node_type"] for r in store.conn.execute(
        "SELECT node_type FROM capture_candidates")}
    assert types == {"concept", "question"}


def test_untitled_items_are_skipped(store):
    result = ExtractionResult(concepts=[{"content": "no title"}], engine=KEYWORD)
    assert stage_candidates(store, result) == []


def test_restaging_the_same_extraction_is_idempotent(store):
    """Re-running an extractor over the same text must not pile up duplicates.

    `add_capture_candidate` returns the EXISTING id for a live duplicate
    payload rather than raising, so a second pass yields the same id and no
    second row — idempotent, and the caller still learns which candidate its
    extraction corresponds to.
    """
    result = ExtractionResult(
        concepts=[{"title": "Same thing", "content": "x"}], engine=KEYWORD)
    first = stage_candidates(store, result)
    second = stage_candidates(store, result)
    assert len(first) == 1
    assert first == second
    assert store.conn.execute(
        "SELECT COUNT(*) FROM capture_candidates").fetchone()[0] == 1


def test_one_bad_item_does_not_abort_the_batch(store):
    result = ExtractionResult(
        concepts=[{"title": "Good one", "content": "x"},
                  {"title": "", "content": "skipped"},
                  {"title": "Another good one", "content": "y"}],
        engine=KEYWORD)
    assert len(stage_candidates(store, result)) == 2


# ── Grounding detection ───────────────────────────────────────────────

def test_verbatim_span_is_grounded():
    assert _is_grounded("dream cycle", SAMPLE.lower())


def test_reworded_phrase_is_still_grounded():
    """"beam ordering stable" for "beam ordering should be stable"."""
    assert _is_grounded("beam ordering stable", SAMPLE.lower())


def test_invented_phrase_is_not_grounded():
    assert not _is_grounded("quarterly revenue forecast", SAMPLE.lower())


def test_empty_item_is_not_grounded():
    assert not _is_grounded("", SAMPLE.lower())


# ── Scoring ───────────────────────────────────────────────────────────

def test_verbatim_engine_scores_full_grounding():
    """This is exactly why grounding alone cannot be the gate."""
    sample = [{"id": "n1", "title": "Merge growth", "content": SAMPLE}]
    score = score_engine(KEYWORD, sample)
    assert score.documents == 1
    assert score.grounding_precision == 1.0


def test_empty_proposals_score_zero_not_divide_by_zero():
    score = EngineScore(engine="x", documents=3)
    assert score.grounding_precision == 0.0
    assert score.recall == 0.0
    assert score.noise == 0.0


# ── The two-part gate ─────────────────────────────────────────────────

def _seed_corpus(store, n=6):
    for i in range(n):
        store.add_node(f"Merge growth note {i}", content=SAMPLE + f" Item {i}.")


def test_sample_excludes_machine_generated_families(store):
    _seed_corpus(store)
    store.conn.execute(
        "INSERT INTO nodes (id, type, title, content, status) "
        "VALUES ('code-sym-x', 'concept', 'class Ha', ?, 'active')",
        (SAMPLE,))
    store.conn.commit()
    ids = {row["id"] for row in build_sample(store, limit=50)}
    assert "code-sym-x" not in ids


def test_sample_is_stable_across_runs(store):
    """An eval whose sample moves cannot detect a regression."""
    _seed_corpus(store)
    assert build_sample(store, limit=5) == build_sample(store, limit=5)


def test_verbatim_copier_fails_the_discriminator(store):
    """The degenerate strategy must not pass.

    Scoring keyword against itself: it clears the grounding floor trivially
    (identical scores) but gains nothing on title recall, so the gate refuses.
    """
    _seed_corpus(store)
    verdict = run_eval(store, store.config, engines=("keyword",), limit=10)
    assert verdict["status"] == "ok"
    # With only the baseline present there is no candidate to pass.
    assert verdict.get("passes_gate") is False


def test_gate_requires_both_parts(store):
    _seed_corpus(store)
    verdict = run_eval(store, store.config,
                       engines=("keyword", "deterministic"), limit=10)
    gate = verdict["gate"]["deterministic"]
    # Deterministic degrades to keyword here, so it ties on both axes:
    # floor cleared, but no recall gain -> refused.
    assert gate["clears_grounding_floor"] is True
    assert gate["beats_title_recall"] is False
    assert gate["beats_baseline"] is False


def test_grounding_tolerance_admits_paraphrase():
    """A paraphrasing engine is not hallucinating just because it reworded.

    The containment test cannot tell those apart, so the floor has slack —
    otherwise every non-extractive engine fails for the wrong reason.
    """
    assert GROUNDING_TOLERANCE > 0


def test_no_sample_is_reported_not_crashed(store):
    verdict = run_eval(store, store.config, engines=("keyword",), limit=10)
    assert verdict["status"] == "no_sample"
