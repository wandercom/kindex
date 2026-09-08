"""Extraction eval harness — the gate a new engine must clear.

Kindex had no way to answer "is this extractor any good?", which is exactly how
you end up adopting a 2.5 GB dependency on the strength of somebody's README.
Hillock reports 13.8% extraction precision on its own 32-query benchmark and
its author calls the numbers "directional, not final". Those figures say
nothing about performance on *this* corpus, so this harness measures engines
against the local graph instead of trusting a published number.

The method: take existing active nodes as ground truth, feed each node's
content back to an engine, and ask whether the engine recovers a title close to
the node's own. That is a proxy — it measures "would this engine have proposed
the knowledge a human already curated", not truth in the abstract — and it is
stated as a proxy rather than dressed up as precision.

The gate is comparative, not absolute. A candidate engine must BEAT
`keyword_extract` on the same sample. An engine that cannot beat regexes has
not earned 2.5 GB of dependencies, whatever its own benchmark says.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

from .privacy import protect_logger

log = protect_logger(logging.getLogger(__name__))

# Two titles count as the same knowledge above this similarity. Deliberately
# loose: an extractor that says "SQLite FTS5 sync trigger" for a node titled
# "FTS5 sync triggers" found the right thing.
MATCH_THRESHOLD = 0.62

# How far below the baseline's grounding precision a candidate may sit and
# still clear the hallucination floor. Non-zero on purpose: a paraphrasing
# engine is not hallucinating just because it did not copy verbatim, and the
# containment test cannot tell those apart. Set from the observed gap between
# a purely-extractive engine (100% by construction) and a paraphrasing one.
GROUNDING_TOLERANCE = 0.30


@dataclass
class EngineScore:
    engine: str
    documents: int = 0
    items_proposed: int = 0
    documents_recalled: int = 0
    items_grounded: int = 0
    per_doc_best: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def recall(self) -> float:
        """Fraction of documents where the engine recovered the known title."""
        if not self.documents:
            return 0.0
        return self.documents_recalled / self.documents

    @property
    def noise(self) -> float:
        """Items proposed per document. High noise means a costly review queue."""
        if not self.documents:
            return 0.0
        return self.items_proposed / self.documents

    @property
    def grounding_precision(self) -> float:
        """Fraction of proposed items whose text actually occurs in the source.

        This is the metric that speaks to the W2 decision, because it is what
        an extraction-precision claim like Hillock's 13.8% is about: did the
        engine report something that is really in the document, or invent it?
        Title recall below is a different and much harsher question — whether
        the engine guessed the curator's own title — and on a corpus whose
        titles carry dates and file paths no engine can win it.
        """
        if not self.items_proposed:
            return 0.0
        return self.items_grounded / self.items_proposed

    @property
    def mean_best_similarity(self) -> float:
        if not self.per_doc_best:
            return 0.0
        return sum(self.per_doc_best) / len(self.per_doc_best)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "documents": self.documents,
            "items_proposed": self.items_proposed,
            "documents_recalled": self.documents_recalled,
            "recall": round(self.recall, 4),
            "items_grounded": self.items_grounded,
            "grounding_precision": round(self.grounding_precision, 4),
            "noise_items_per_doc": round(self.noise, 2),
            "mean_best_similarity": round(self.mean_best_similarity, 4),
            "errors": self.errors,
        }


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _is_grounded(item: str, haystack: str) -> bool:
    """Does this proposed item actually occur in the source text?

    Exact containment first (the common case), then a token-overlap fallback so
    a lightly-reworded phrase still counts — an extractor that says "FTS5 sync
    triggers" for text containing "the FTS5 sync trigger" found a real thing.
    Requiring verbatim-only would understate every engine equally, which is a
    different way of being wrong.
    """
    needle = item.lower().strip()
    if not needle:
        return False
    if needle in haystack:
        return True
    tokens = [t for t in needle.split() if len(t) > 3]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in haystack)
    return hits / len(tokens) >= 0.8


def build_sample(store: "Store", *, limit: int = 200,
                 min_content_chars: int = 200,
                 max_content_chars: int = 8000) -> list[dict]:
    """Pick evaluable nodes: curated, substantive, and not machine-generated.

    Excludes the generated-code node families (`code-sym-*`, `code-mod-*`,
    `file-*`) — an extractor's job here is prose knowledge, and scoring it on
    minified bundles would measure the wrong thing. Ordered by id so the sample
    is stable across runs; an eval whose sample moves cannot detect regressions.
    """
    rows = store.conn.execute(
        """SELECT id, title, content FROM nodes
           WHERE status = 'active'
             AND type IN ('concept', 'decision', 'question', 'document')
             AND LENGTH(content) BETWEEN ? AND ?
             AND id NOT LIKE 'code-sym-%'
             AND id NOT LIKE 'code-mod-%'
             AND id NOT LIKE 'file-%'
           ORDER BY id
           LIMIT ?""",
        (min_content_chars, max_content_chars, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def score_engine(engine_name: str, sample: list[dict], *,
                 config: "Config" | None = None,
                 ledger=None) -> EngineScore:
    """Run one engine over the sample and score it."""
    from .extractors import get_extractor

    extractor = get_extractor(engine_name, config=config, ledger=ledger)
    score = EngineScore(engine=engine_name)

    for row in sample:
        score.documents += 1
        try:
            result = extractor.extract(row["content"], [])
        except Exception as exc:
            score.errors += 1
            log.debug("engine %s failed on %s: %s", engine_name, row["id"], exc)
            score.per_doc_best.append(0.0)
            continue

        proposed = [
            str(item.get("title") or item.get("question") or "")
            for item in (result.concepts + result.decisions + result.questions)
        ]
        proposed = [p for p in proposed if p.strip()]
        score.items_proposed += len(proposed)

        haystack = row["content"].lower()
        for item in proposed:
            if _is_grounded(item, haystack):
                score.items_grounded += 1

        best = max((_similar(row["title"], p) for p in proposed), default=0.0)
        score.per_doc_best.append(best)
        if best >= MATCH_THRESHOLD:
            score.documents_recalled += 1

    return score


def run_eval(store: "Store", config: "Config", *, engines: tuple[str, ...],
             limit: int = 200, ledger=None) -> dict:
    """Score each engine against the local corpus and apply the gate.

    Returns a verdict dict. `passes_gate` is True only when a candidate engine
    beats the keyword baseline on recall — the comparison is the whole point,
    because an absolute number from someone else's corpus predicts nothing
    about this one.
    """
    sample = build_sample(store, limit=limit)
    if not sample:
        return {"status": "no_sample",
                "detail": "no evaluable nodes matched the sample criteria"}

    scores = {name: score_engine(name, sample, config=config, ledger=ledger)
              for name in engines}
    baseline = scores.get("keyword")

    verdict = {
        "status": "ok",
        "sample_size": len(sample),
        "match_threshold": MATCH_THRESHOLD,
        "scores": {name: s.to_dict() for name, s in scores.items()},
    }

    if baseline is not None:
        verdict["baseline_recall"] = round(baseline.recall, 4)
        verdict["baseline_grounding"] = round(baseline.grounding_precision, 4)
        gates = {}
        for name, s in scores.items():
            if name == "keyword":
                continue
            # TWO-PART GATE, because either metric alone is gameable.
            #
            # Grounding precision alone rewards a degenerate strategy: an
            # engine that only ever copies verbatim spans scores 100% by
            # construction. That is exactly what `keyword` does, and it is why
            # it "wins" grounding while finding almost nothing useful.
            #
            # Title recall alone rewards the opposite failure — an engine that
            # invents plausible titles unconstrained by the source.
            #
            # So: grounding is a FLOOR (do not hallucinate materially more than
            # the baseline), and title recall is the DISCRIMINATOR (actually
            # find what a curator would have recorded). A candidate must clear
            # both.
            grounding_delta = s.grounding_precision - baseline.grounding_precision
            recall_delta = s.recall - baseline.recall
            clears_floor = grounding_delta >= -GROUNDING_TOLERANCE
            beats_recall = recall_delta > 0
            gates[name] = {
                "beats_baseline": clears_floor and beats_recall,
                "clears_grounding_floor": clears_floor,
                "beats_title_recall": beats_recall,
                "grounding_delta": round(grounding_delta, 4),
                "recall_delta": round(recall_delta, 4),
                "noise_delta": round(s.noise - baseline.noise, 2),
            }
        verdict["gate"] = gates
        verdict["passes_gate"] = any(g["beats_baseline"] for g in gates.values())
    return verdict
