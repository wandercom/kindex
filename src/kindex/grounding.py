"""Retrieval grounding — does the graph actually know anything about this query?

Kindex's vector channel had no similarity floor: `vector_search` returned the
top-k nearest neighbours for any query however nonsensical, and `ask()` could
only reach its "no relevant knowledge" branch when the result list was *empty*
— which vector search made unreachable. So the graph could never say "I don't
know", and a near-null query still pulled real nodes into an agent's context.

This module supplies the missing authority. Retrieval is authoritative about
its OWN confidence — not about what the caller should then do — so it emits a
`RetrievalVerdict` and the caller decides. The two facts are kept apart on
purpose:

  * the floor is a derived fact about an external system (the provider's
    similarity distribution over this corpus), stored as an immutable,
    versioned calibration record keyed by `provider:model`;
  * the policy (which percentile, whether to enforce) is configuration.

Config never holds the number. A number in config outlives the corpus it was
computed against and keeps looking exactly as authoritative as the day it was
written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

from .privacy import protect_logger

logger = protect_logger(logging.getLogger(__name__))

# Verdicts, worst to best. `uncalibrated` is deliberately distinct from
# `ungrounded`: "we have no yardstick" is a different fact from "we measured
# and found nothing", and conflating them would let a missing calibration
# masquerade as a confident negative.
UNCALIBRATED = "uncalibrated"
UNGROUNDED = "ungrounded"
WEAK = "weak"
GROUNDED = "grounded"

VERDICT_ORDER = (UNCALIBRATED, UNGROUNDED, WEAK, GROUNDED)

CALIBRATION_META_PREFIX = "grounding.calibration."


@dataclass(frozen=True)
class CalibrationRecord:
    """One immutable calibration of the similarity floor.

    Carries the corpus it was measured against so staleness is detectable
    rather than assumed away. `embedding_count` in particular: a floor
    calibrated when 1.6% of the graph was embedded describes a distribution
    that no longer exists once the backfill lands.
    """

    provider: str
    model: str
    floor: float
    percentile: float
    sample_size: int
    corpus_node_count: int
    embedding_count: int
    calibrated_at: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "floor": self.floor,
            "percentile": self.percentile,
            "sample_size": self.sample_size,
            "corpus_node_count": self.corpus_node_count,
            "embedding_count": self.embedding_count,
            "calibrated_at": self.calibrated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationRecord":
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            floor=float(data.get("floor", 0.0)),
            percentile=float(data.get("percentile", 0.0)),
            sample_size=int(data.get("sample_size", 0)),
            corpus_node_count=int(data.get("corpus_node_count", 0)),
            embedding_count=int(data.get("embedding_count", 0)),
            calibrated_at=str(data.get("calibrated_at", "")),
        )


@dataclass
class RetrievalVerdict:
    """What retrieval believes about its own confidence in this result set.

    Advisory by construction — `enforced` records whether anything was actually
    dropped. In shadow mode the verdict is computed and reported while every
    row still flows, so the gate can be measured against real traffic before it
    is allowed to withhold anything.
    """

    verdict: str = UNCALIBRATED
    floor: float | None = None
    best_similarity: float | None = None
    enforced: bool = False
    dropped: int = 0
    reason: str = ""
    # Scores of rows that fell below the floor, best first. When someone says
    # "Kindex claims it doesn't know X but I know X is in there", this is the
    # evidence that explains the decision instead of leaving them to
    # reverse-engineer it from behaviour.
    near_misses: list[float] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        return self.verdict in (GROUNDED, WEAK)

    @property
    def should_warn(self) -> bool:
        return self.verdict in (UNGROUNDED, UNCALIBRATED)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "floor": self.floor,
            "best_similarity": self.best_similarity,
            "enforced": self.enforced,
            "dropped": self.dropped,
            "reason": self.reason,
            "near_misses": self.near_misses[:5],
        }

    def note(self) -> str:
        """One line for a human or an agent reading the result surface."""
        if self.verdict == UNCALIBRATED:
            return ("[grounding: uncalibrated — no similarity floor for the "
                    "active embedding model; run `kin embed calibrate`]")
        if self.verdict == UNGROUNDED:
            best = f"{self.best_similarity:.3f}" if self.best_similarity is not None else "n/a"
            floor = f"{self.floor:.3f}" if self.floor is not None else "n/a"
            tail = "" if self.enforced else " (shadow mode — results still shown)"
            return (f"[grounding: UNGROUNDED — best similarity {best} is below "
                    f"the floor {floor}; the graph likely knows nothing about "
                    f"this{tail}]")
        if self.verdict == WEAK:
            return "[grounding: weak — results clear the floor but only just]"
        return ""


def similarity_from_distance(distance: float | None) -> float:
    """Convert a stored vector distance to a cosine similarity in [0, 1].

    sqlite-vec's ``vec0`` returns **L2 (Euclidean) distance**, not cosine
    distance. For unit-normalised embeddings — which is what every provider
    Kindex supports returns, verified at norm 1.000000 for voyage-context-4 —
    the two are related by ``d^2 = 2(1 - cos)``, so:

        cos = 1 - d^2 / 2

    Getting this wrong is not a rounding error. The naive ``1 - d`` clamps to
    zero for every distance above 1.0, and real L2 distances here run 0.94
    (a strong match) to 1.32 (a null query) — so the whole range collapses to
    0, calibration produces a floor of exactly 0.0, and the gate silently never
    fires. That is the same class of defect as everything else found in this
    codebase today: a mechanism that exists, looks configured, and does
    nothing.

    Kept in one place so the floor and the ranking channel cannot drift into
    different units. A floor compared against the wrong scale is worse than no
    floor, because it looks like it is working.
    """
    if distance is None:
        return 0.0
    d = float(distance)
    return max(0.0, min(1.0, 1.0 - (d * d) / 2.0))


def load_calibration(store: "Store", provider: str, model: str) -> CalibrationRecord | None:
    """Newest calibration record for this provider/model, or None."""
    import json

    raw = store.get_meta(f"{CALIBRATION_META_PREFIX}{provider}:{model}")
    if not raw:
        return None
    try:
        return CalibrationRecord.from_dict(json.loads(raw))
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "grounding: calibration record for %s:%s is unreadable; treating "
            "as uncalibrated rather than guessing a floor", provider, model)
        return None


def save_calibration(store: "Store", record: CalibrationRecord) -> None:
    """Persist a calibration record, superseding the previous one for its key.

    History is kept under a timestamped key so a floor change is auditable —
    a floor that moves silently is the same defect as a config number.
    """
    import json

    payload = json.dumps(record.to_dict(), sort_keys=True)
    store.set_meta(f"{CALIBRATION_META_PREFIX}{record.key}", payload)
    store.set_meta(
        f"{CALIBRATION_META_PREFIX}history.{record.key}.{record.calibrated_at}",
        payload,
    )


def calibration_is_stale(store: "Store", record: CalibrationRecord,
                         config: "Config") -> str:
    """Return a reason string if the record no longer describes this corpus.

    Empty string means usable. Coverage is the signal that matters: a floor
    calibrated at 1.6% embedding coverage describes a distribution that ceases
    to exist the moment a backfill lands.
    """
    try:
        live = store.conn.execute(
            "SELECT COUNT(*) FROM node_vector_meta").fetchone()[0]
    except Exception:
        return ""
    if record.embedding_count <= 0:
        return "calibrated against an empty index"
    delta = abs(live - record.embedding_count) / max(record.embedding_count, 1)
    tolerance = config.grounding.recalibrate_coverage_delta
    if delta > tolerance:
        return (f"embedding coverage moved {delta:.0%} since calibration "
                f"({record.embedding_count} -> {live}, tolerance {tolerance:.0%})")
    return ""


def evaluate(
    store: "Store",
    config: "Config",
    vector_hits: list[tuple[str, float]],
    *,
    provider: str,
    model: str,
) -> RetrievalVerdict:
    """Judge a vector result set against the calibrated floor.

    `vector_hits` is [(node_id, similarity)], best first. Returns the verdict;
    it never mutates the caller's results — dropping, if any, is the caller's
    act, made against `verdict.floor`.
    """
    if not config.grounding.enabled:
        return RetrievalVerdict(verdict=GROUNDED, reason="grounding disabled")

    record = load_calibration(store, provider, model)
    if record is None:
        return RetrievalVerdict(
            verdict=UNCALIBRATED,
            reason=f"no calibration record for {provider}:{model}",
        )

    stale = calibration_is_stale(store, record, config)
    if stale:
        # Stale is reported as uncalibrated, not silently trusted: an
        # out-of-date yardstick is not a yardstick.
        return RetrievalVerdict(
            verdict=UNCALIBRATED, floor=record.floor, reason=f"stale: {stale}")

    best = max((s for _, s in vector_hits), default=None)
    floor = record.floor
    near = sorted((s for _, s in vector_hits if s < floor), reverse=True)

    if best is None or best < floor:
        return RetrievalVerdict(
            verdict=UNGROUNDED,
            floor=floor,
            best_similarity=best,
            enforced=config.grounding.enforce,
            dropped=len(near) if config.grounding.enforce else 0,
            reason="no vector hit cleared the floor",
            near_misses=near[:5],
        )

    verdict = WEAK if best < floor * config.grounding.weak_margin else GROUNDED
    return RetrievalVerdict(
        verdict=verdict,
        floor=floor,
        best_similarity=best,
        enforced=config.grounding.enforce,
        dropped=len(near) if config.grounding.enforce else 0,
        reason="",
        near_misses=near[:5],
    )


# ── Calibration ───────────────────────────────────────────────────────

# Queries chosen to be semantically empty *for any corpus* — not nonsense
# strings, which can land oddly close to sparse regions of an embedding space,
# but ordinary well-formed sentences about domains the graph is not about. The
# floor is the similarity these earn: anything a real query scores below is
# indistinguishable from asking about nothing.
NULL_QUERIES = (
    "the migratory patterns of arctic terns in late autumn",
    "a recipe for braised short ribs with red wine",
    "seventeenth century Dutch still life painting technique",
    "how to re-grout bathroom tile without cracking it",
    "the rules governing offside in association football",
    "care instructions for a potted fiddle leaf fig",
    "tuning a mandolin to open G",
    "the geology of limestone cave formation",
    "knitting a cable pattern on circular needles",
    "why sourdough starter needs regular feeding",
)


def calibrate(
    store: "Store",
    config: "Config",
    *,
    percentile: float | None = None,
    queries: tuple[str, ...] = NULL_QUERIES,
    top_k: int = 20,
) -> CalibrationRecord:
    """Measure the null-query similarity distribution and record the floor.

    Runs each null query through the live vector index, pools every similarity
    returned, and sets the floor at the configured percentile of that pool. The
    resulting record carries the corpus it was measured against so a later
    reader can tell whether it still applies.
    """
    from .vectors import _resolve_embedding_config, vector_search

    provider, model, _, _ = _resolve_embedding_config(config)
    pct = percentile if percentile is not None else config.grounding.floor_percentile

    scores: list[float] = []
    for query in queries:
        for node in vector_search(store, query, top_k=top_k):
            scores.append(similarity_from_distance(node.get("vec_distance")))

    if not scores:
        raise RuntimeError(
            "calibration found no vector results — is the index populated and "
            "the embedding provider reachable?")

    scores.sort()
    # Nearest-rank percentile: no interpolation, so the floor is always a
    # similarity the index actually produced.
    idx = min(len(scores) - 1, max(0, int(round(pct / 100.0 * len(scores))) - 1))
    floor = scores[idx]

    try:
        corpus = store.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0]
    except Exception:
        corpus = 0
    try:
        embedded = store.conn.execute(
            "SELECT COUNT(*) FROM node_vector_meta").fetchone()[0]
    except Exception:
        embedded = 0

    record = CalibrationRecord(
        provider=provider,
        model=model,
        floor=round(float(floor), 6),
        percentile=pct,
        sample_size=len(scores),
        corpus_node_count=corpus,
        embedding_count=embedded,
        calibrated_at=datetime.now().isoformat(timespec="seconds"),
    )
    save_calibration(store, record)
    return record
