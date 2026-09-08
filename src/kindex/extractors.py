"""Extraction engines and the boundary that keeps them out of the graph.

Kindex has one LLM extractor (`extract.llm_extract`) and one keyword fallback
(`extract.keyword_extract`). Both answer the same question — "what knowledge is
in this text?" — and neither is an *authority* on whether the answer is true.
This module makes that explicit and adds the only safe place for a third,
lower-precision engine to plug in.

**Placement is the whole design.** An extractor is an INPUT. It never writes to
`nodes` or `edges`; its output lands in `capture_candidates`, the quarantine
table whose schema comment already states it is "deliberately separate from
nodes/edges/FTS so no query can accidentally" surface it. That is what makes a
13.8%-precision engine survivable: wrong candidates in a review queue cost a
rejection click, wrong nodes cost the graph its trustworthiness — which is the
only thing it has.

The deterministic engine itself (Hillock's TALON shape: coreference resolution,
bi-encoder predicate routing, zero-shot relation extraction) lives behind the
optional `kindex[talon]` extra. It is NEVER a core dependency: the stack is
~2.5 GB against a core that is presently pure Python plus optional sqlite-vec,
and Kindex's whole premise is that it stays nimble. When the extra is absent,
selecting it degrades to the keyword engine with a warning — never a crash.

Nothing here is derived from Hillock's implementation, which is AGPL-3.0 and
incompatible with this project's MIT licence. The ideas are not copyrightable;
the code is, and none of it was copied.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .budget import BudgetLedger
    from .config import Config
    from .store import Store

from .privacy import protect_logger

log = protect_logger(logging.getLogger(__name__))

LLM = "llm"
KEYWORD = "keyword"
DETERMINISTIC = "deterministic"
ENGINES = (LLM, KEYWORD, DETERMINISTIC)


@dataclass
class ExtractionResult:
    """What every engine returns. The anti-corruption layer's shared shape.

    `engine` and `engine_version` are carried so a candidate promoted to a node
    can record WHICH extractor proposed it. Without that, an accepted candidate
    becomes indistinguishable from a human assertion at the promotion boundary,
    and the told/inferred separation defended elsewhere is quietly lost.
    """

    concepts: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    bridge_opportunities: list[dict] = field(default_factory=list)
    engine: str = KEYWORD
    engine_version: str = ""

    @classmethod
    def from_legacy(cls, data: dict, *, engine: str,
                    engine_version: str = "") -> "ExtractionResult":
        """Adapt the dict shape `extract.py` has always returned."""
        data = data or {}
        return cls(
            concepts=list(data.get("concepts") or []),
            decisions=list(data.get("decisions") or []),
            questions=list(data.get("questions") or []),
            connections=list(data.get("connections") or []),
            bridge_opportunities=list(data.get("bridge_opportunities") or []),
            engine=engine,
            engine_version=engine_version,
        )

    def to_legacy(self) -> dict:
        return {
            "concepts": self.concepts,
            "decisions": self.decisions,
            "questions": self.questions,
            "connections": self.connections,
            "bridge_opportunities": self.bridge_opportunities,
        }

    @property
    def item_count(self) -> int:
        return (len(self.concepts) + len(self.decisions)
                + len(self.questions))


class Extractor(Protocol):
    """One extraction engine. Implementations MUST NOT write nodes or edges."""

    name: str

    def extract(self, text: str, existing_titles: list[str]) -> ExtractionResult:
        ...


# ── Built-in engines ──────────────────────────────────────────────────


class KeywordExtractor:
    """Pure-Python regex/heuristic extraction. The floor every engine must beat."""

    name = KEYWORD

    def extract(self, text: str, existing_titles: list[str]) -> ExtractionResult:
        from .extract import keyword_extract
        return ExtractionResult.from_legacy(
            keyword_extract(text, existing_titles=existing_titles),
            engine=KEYWORD)


class LLMExtractor:
    """Anthropic extraction. Returns the keyword result when unavailable."""

    name = LLM

    def __init__(self, config: "Config", ledger: "BudgetLedger") -> None:
        self.config = config
        self.ledger = ledger

    def extract(self, text: str, existing_titles: list[str]) -> ExtractionResult:
        from .extract import llm_extract
        result = llm_extract(text, existing_titles, self.config, self.ledger)
        if result is None:
            return KeywordExtractor().extract(text, existing_titles)
        return ExtractionResult.from_legacy(
            result, engine=LLM, engine_version=self.config.llm.model)


class DeterministicExtractor:
    """LLM-free extraction via the optional `kindex[talon]` stack.

    Degrades to the keyword engine — loudly, never silently — when the extra is
    not installed, so selecting it can never crash a hook or an ingest run.
    """

    name = DETERMINISTIC

    def __init__(self, config: "Config" | None = None) -> None:
        self.config = config

    @staticmethod
    def available() -> bool:
        try:
            import glirel  # noqa: F401
            import spacy  # noqa: F401
            return True
        except Exception:
            return False

    def extract(self, text: str, existing_titles: list[str]) -> ExtractionResult:
        if not self.available():
            log.warning(
                "extract.engine=deterministic selected but the talon extra is "
                "not installed (pip install 'kindex[talon]'); falling back to "
                "keyword extraction. Deterministic extraction is optional by "
                "design — the stack is ~2.5 GB and is never a core dependency.")
            result = KeywordExtractor().extract(text, existing_titles)
            result.engine_version = "talon-unavailable"
            return result
        from .talon import talon_extract
        return talon_extract(text, existing_titles)


def get_extractor(engine: str, config: "Config" | None = None,
                  ledger: "BudgetLedger" | None = None) -> Extractor:
    """Resolve an engine name to an extractor. Unknown names fall back loudly."""
    if engine == DETERMINISTIC:
        return DeterministicExtractor(config)
    if engine == LLM:
        if config is None or ledger is None:
            raise ValueError("the llm engine needs a config and a ledger")
        return LLMExtractor(config, ledger)
    if engine != KEYWORD:
        log.warning("unknown extraction engine %r; using keyword", engine)
    return KeywordExtractor()


# ── The quarantine boundary ───────────────────────────────────────────


def _digest(engine: str, title: str, content: str) -> str:
    return hashlib.sha256(
        json.dumps([engine, title, content], sort_keys=True).encode("utf-8")
    ).hexdigest()


def stage_candidates(store: "Store", result: ExtractionResult, *,
                     source: str = "", ttl_days: int | None = 30) -> list[str]:
    """Write an ExtractionResult into `capture_candidates`. Returns their ids.

    This is the ONLY sanctioned path from an extractor into storage, and it
    deliberately cannot reach `nodes` or `edges`. Every candidate carries the
    engine and version that proposed it, so promotion can stamp provenance and
    an accepted candidate never passes for a human assertion.

    A duplicate payload is skipped rather than raising: re-running an extractor
    over the same text should be idempotent, not an error.
    """
    staged: list[str] = []
    typed = (
        [(c, c.get("type") or "concept") for c in result.concepts]
        + [(d, "decision") for d in result.decisions]
        + [(q, "question") for q in result.questions]
    )
    for item, node_type in typed:
        title = str(item.get("title") or item.get("question") or "").strip()
        content = str(
            item.get("content") or item.get("rationale")
            or item.get("context") or "").strip()
        if not title:
            continue
        try:
            cid = store.add_capture_candidate(
                title=title,
                content=content,
                node_type=node_type,
                domains=list(item.get("domains") or []),
                source_digest=_digest(result.engine, title, content),
                ttl_days=ttl_days,
            )
            staged.append(cid)
        except Exception as exc:
            # A rejected candidate (duplicate digest, bad type, oversize) must
            # not abort the batch — the queue is advisory by construction.
            log.debug("candidate not staged (%s): %s", type(exc).__name__, exc)
    if staged:
        log.info("staged %d candidate(s) from the %s engine for review "
                 "(source=%s)", len(staged), result.engine, source or "unknown")
    return staged
