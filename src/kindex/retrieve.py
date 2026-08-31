"""Hybrid retrieval engine — FTS5 + graph traversal + Reciprocal Rank Fusion.

Supports five context tiers, each optimized for a different token budget:

  full        ~4000 tokens — everything: all nodes, edges, provenance, open questions
  abridged    ~1500 tokens — key nodes, trimmed content, edges preserved
  summarized  ~750 tokens  — paragraph-form synthesized narrative per domain cluster
  executive   ~200 tokens  — 2-3 sentences per active thread
  index       ~100 tokens  — node titles and edge types only, no content

Auto-selects based on estimated available token budget when level is not specified.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from .agent_adapters import adapter_scoped_out

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .store import Store

# Node types whose content is likely to go stale (references code state, file paths)
_STALE_PRONE_TYPES = {"artifact", "document"}
# Node types whose content is durable (rationale, rules, guidelines)
_STALE_RESISTANT_TYPES = {"decision", "constraint", "directive", "skill"}

# Context tier token budgets (approximate)
TIER_BUDGETS = {
    "full": 4000,
    "abridged": 1500,
    "summarized": 750,
    "executive": 200,
    "index": 100,
}

TIER_ORDER = ["full", "abridged", "summarized", "executive", "index"]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (---...---) from content."""
    return _FRONTMATTER_RE.sub("", text).lstrip()


def _node_age_days(node: dict) -> int | None:
    """Days since node was last updated. None if no timestamp."""
    ts = node.get("updated_at") or node.get("created_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return max(0, (datetime.now() - dt).days)
    except (ValueError, TypeError):
        return None


def _node_age_str(node: dict) -> str:
    """Human-readable age string for a node."""
    days = _node_age_days(node)
    if days is None:
        return ""
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


# Patterns that indicate content references mutable state (file paths, versions, APIs)
_STALE_CONTENT_PATTERNS = re.compile(
    r'(?:'
    r'(?:^|[\s/])(?:src|lib|pkg|packages|node_modules)/\S+'  # file paths
    r'|v\d+\.\d+(?:\.\d+)?'                                  # version numbers
    r'|line[s]?\s+\d+'                                        # line references
    r'|:\d+(?::\d+)?'                                         # file:line:col
    r'|(?:api|endpoint|route)\s+\S+/\S+'                      # API routes
    r'|(?:pip|npm|cargo)\s+install'                            # install commands
    r')',
    re.IGNORECASE | re.MULTILINE,
)


def _has_mutable_references(content: str) -> bool:
    """Check if content references things likely to change (paths, versions, APIs)."""
    return bool(_STALE_CONTENT_PATTERNS.search(content))


def _staleness_caveat(node: dict) -> str:
    """Staleness warning based on age, type, AND content analysis.

    A recorded stale-referent marker (R0: the referent sweep re-hashed the
    thing this claim describes and it moved or vanished) outranks every
    age/content heuristic — it is a measured fact, not a guess.
    """
    if (node.get("extra") or {}).get("referent_stale"):
        return " [stale-referent]"
    days = _node_age_days(node)
    if days is None or days <= 1:
        return ""
    ntype = node.get("type", "concept")
    if ntype in _STALE_RESISTANT_TYPES:
        return ""
    content = node.get("content") or ""
    # Content-based: even "concept" nodes are stale-prone if they reference
    # file paths, version numbers, line numbers, or API routes
    if ntype in _STALE_PRONE_TYPES:
        return " [verify: may be outdated]"
    if days > 7 and _has_mutable_references(content):
        return " [verify: references code/versions that may have changed]"
    if days > 30:
        return " [verify: may be outdated]"
    return ""


# Default RRF k — overridden by config.ranking.rrf_k when store is available.
# Lower k = sharper discrimination between ranks. 30 is tuned for knowledge graphs.
_RRF_K_DEFAULT = 30


def _rrf_merge(*ranked_lists: list[tuple[str, float]], k: int = _RRF_K_DEFAULT) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion across multiple ranked result lists.

    Each input is [(node_id, score), ...] in descending score order.
    Returns merged [(node_id, rrf_score)] sorted by rrf_score descending.
    """
    scores: dict[str, float] = defaultdict(float)

    for ranked in ranked_lists:
        for rank, (nid, _) in enumerate(ranked):
            scores[nid] += 1.0 / (k + rank + 1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _normalize_scores(ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Min-max normalize scores to [0, 1]. Preserves ordering."""
    if not ranked:
        return []
    scores = [s for _, s in ranked]
    lo, hi = min(scores), max(scores)
    span = hi - lo if hi != lo else 1.0
    return [(nid, (s - lo) / span) for nid, s in ranked]


# Fallback ensemble weights — overridden by config.ranking when store is available.
_ENSEMBLE_WEIGHTS_DEFAULT = {
    "fts": 0.40,
    "vector": 0.30,
    "graph": 0.15,
    "node_weight": 0.10,
    "recency": 0.05,
}


def _recency_score(store: Store, node_ids: set[str]) -> list[tuple[str, float]]:
    """Score nodes by recency — recently updated nodes score higher."""
    results = []
    now = datetime.now()
    for nid in node_ids:
        node = store.get_node(nid)
        if not node:
            continue
        ts = node.get("updated_at") or node.get("created_at")
        if not ts:
            results.append((nid, 0.0))
            continue
        try:
            days = max(0, (now - datetime.fromisoformat(ts)).days)
            # Exponential decay: half-life ~30 days
            results.append((nid, 2.0 ** (-days / 30.0)))
        except (ValueError, TypeError):
            results.append((nid, 0.0))
    return results


def _node_weight_scores(store: Store, node_ids: set[str]) -> list[tuple[str, float]]:
    """Score nodes by their stored weight (already [0, 1] range)."""
    results = []
    for nid in node_ids:
        node = store.get_node(nid)
        if node:
            results.append((nid, node.get("weight", 0.5)))
    return results


def _learned_pheromone_weight(store: Store) -> float:
    """Auto-ramped pheromone ranking weight from the maturity gate (0 if immature)."""
    try:
        from .reinforce import learned_pheromone_weight
        return learned_pheromone_weight(store)
    except Exception:
        return 0.0


def _learned_coactivation_weight(store: Store) -> float:
    """Auto-ramped pair co-activation weight (0 until the channel is warm)."""
    try:
        from .reinforce import learned_coactivation_weight
        return learned_coactivation_weight(store)
    except Exception:
        return 0.0


def _coactivation_scores(store: Store, node_ids: set[str]) -> list[tuple[str, float]]:
    """Learned pair co-activation per node — a channel of its OWN.

    Deliberately separate from both the graph channel (asserted topology) and
    the pheromone channel (node-level usefulness). Keeping the raw signal and
    this applied correction apart is what lets the channel be retired without
    rewriting history.
    """
    acfg = getattr(store.config, "attention", None)
    half_life = getattr(acfg, "coactivation_half_life_days", 14.0)
    min_events = getattr(acfg, "coactivation_min_events", 3)
    project_path = getattr(store.config, "_project_path", None)
    context = ""
    if project_path:
        import os
        context = os.path.basename(str(project_path).rstrip("/")) or ""
    scores = store.coactivation_scores(
        node_ids, context=context, half_life_days=half_life,
        min_events=min_events)
    if not scores and context:
        # Fall back to the global trail while a per-project trail is cold.
        scores = store.coactivation_scores(
            node_ids, context="", half_life_days=half_life,
            min_events=min_events)
    return scores


def _pheromone_scores(store: Store, node_ids: set[str]) -> list[tuple[str, float]]:
    """Decayed injection-usefulness pheromone per node, conditioned on project."""
    acfg = getattr(store.config, "attention", None)
    half_life = getattr(acfg, "pheromone_half_life_days", 14.0)
    min_deposits = getattr(acfg, "pheromone_min_deposits", 5)
    project_path = getattr(store.config, "_project_path", None)
    context = ""
    if project_path:
        import os
        context = os.path.basename(str(project_path).rstrip("/")) or ""
    return store.pheromone_scores(
        node_ids, context=context,
        half_life_days=half_life, min_deposits=min_deposits,
    )


def _weighted_ensemble(
    sources: dict[str, list[tuple[str, float]]],
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Weighted ensemble merge — each source normalized to [0,1], then combined.

    When only one source contributes, passes through its normalized scores
    directly (avoids RRF compression on single-source results).
    Returns [(node_id, confidence)] sorted descending.
    """
    w = weights or _ENSEMBLE_WEIGHTS_DEFAULT
    active = {k: v for k, v in sources.items() if v}

    if not active:
        return []

    # Single source: pass through normalized scores weighted to [0, weight]
    if len(active) == 1:
        key, ranked = next(iter(active.items()))
        return _normalize_scores(ranked)

    # Multi-source: normalize each, weighted sum
    all_ids: set[str] = set()
    normalized: dict[str, dict[str, float]] = {}
    for key, ranked in active.items():
        normed = _normalize_scores(ranked)
        normalized[key] = {nid: s for nid, s in normed}
        all_ids.update(nid for nid, _ in normed)

    combined: dict[str, float] = defaultdict(float)
    total_weight = sum(w.get(k, 0) for k in active)
    for nid in all_ids:
        for key in active:
            score = normalized.get(key, {}).get(nid, 0.0)
            combined[nid] += score * w.get(key, 0) / max(total_weight, 0.01)

    return sorted(combined.items(), key=lambda x: x[1], reverse=True)


def hybrid_search(
    store: Store,
    query: str,
    top_k: int = 10,
    expand_graph: bool = True,
    graph_hops: int = 1,
    ranking: str = "ensemble",
    *,
    include_expired: bool = False,
    include_archived: bool = False,
    fence_stats: dict | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
    grounding: dict | None = None,
) -> list[dict]:
    """Hybrid search combining FTS5 + graph expansion + vector search.

    1. FTS5 full-text search (BM25)
    2. Graph traversal from FTS hits (if expand_graph=True)
    3. Vector search with optional transmogrifier register normalization
    4. Merge via weighted ensemble (default) or RRF (fallback)

    Args:
        ranking: 'ensemble' (weighted, with confidence) or 'rrf' (legacy).
        include_expired: When False (default), nodes whose extra['expires']
            is in the past are filtered out — expired knowledge stops
            surfacing in search/context/ask everywhere, matching the primed
            session context. Daemon/maintenance callers may opt in.
        include_archived: When False (default), archived nodes are fenced
            from results no matter which mode surfaced them; True restores
            the pre-fence behavior for callers that need retired content.
        fence_stats: Optional dict the caller owns; on return its "fenced"
            key holds how many archived/superseded candidates were dropped
            while assembling results (feeds the CLI/MCP fence note).
        trusted_only: Admission-control results through the same explicit
            verification, valid-time, and contradiction predicate as resume.
            False preserves ordinary recall behavior for legacy callers.
        evaluation_time: One RFC 3339/datetime instant used by trusted filtering.
        grounding: Optional dict the caller owns; on return its "verdict" key
            holds the RetrievalVerdict describing how confident retrieval is in
            its own vector results (grounded / weak / ungrounded /
            uncalibrated). Retrieval is authoritative about its own confidence,
            not about what the caller should do with it — so this is reported,
            and enforcement is separately gated by config.grounding.enforce.

    Candidate window: FTS5 fetches up to 3*top_k candidates, graph expansion
    walks 1 hop from the top 5 FTS hits, and vector search fetches up to
    top_k. After merge, results are consumed in ranked order until top_k
    live (non-expired, non-fenced) results or the candidate window is
    exhausted. A short result set when the candidate window is exhausted
    is genuine exhaustion, not a silent under-fill.

    Returns list of node dicts with 'confidence' and 'rrf_score' keys.
    """
    # Mode 1: FTS5 search (raw query — register is intentional signal for
    # keywords). Per-row scoring guard: one malformed node (NULL weight,
    # garbage rank) is skipped, never allowed to zero the result set.
    fts_results = store.fts_search(query, limit=top_k * 3,
                                   include_archived=include_archived)
    fts_ranked: list[tuple[str, float]] = []
    for r in fts_results:
        try:
            fts_ranked.append((r["id"], abs(r.get("rank") or 0) + (r.get("weight") or 0)))
        except Exception:
            continue

    # Mode 2: Graph expansion from FTS hits.
    # Was one hop from the top five FTS hits — shallow for no reason: measured
    # on 113,355 edges, 3 hops costs the same wall-clock as 1. `graph_hops`
    # now actually controls depth, with a mandatory, deterministically-ordered
    # beam (max out-fanout is 849, so an uncapped walk from a hub explodes).
    graph_ranked: list[tuple[str, float]] = []
    if expand_graph and fts_ranked:
        seeds = {nid: score for nid, score in fts_ranked[:5]}
        try:
            graph_ranked = store.expand_multihop(
                seeds,
                max_hops=max(1, graph_hops),
                hop_decay=getattr(store.config.ranking, "hop_decay", 0.5),
                beam=getattr(store.config.ranking, "graph_beam", 200),
            )
        except Exception:
            graph_ranked = []

    # Mode 3: Vector search (if available)
    # Transmogrifier normalizes register for embeddings only — FTS5 stays raw
    vec_ranked: list[tuple[str, float]] = []
    verdict = None
    try:
        from .grounding import evaluate as _evaluate_grounding
        from .grounding import similarity_from_distance
        from .vectors import _resolve_embedding_config, is_available, vector_search
        if is_available():
            vec_query = query
            try:
                from transmogrifier.core import Transmogrifier
                _transmog = Transmogrifier()
                result = _transmog.translate(query)
                if not result.skipped and result.output_text:
                    vec_query = result.output_text
            except (ImportError, Exception):
                pass
            # Fetch unfiltered, then judge. Judging the full set is what makes
            # shadow mode possible and what gives the verdict its near-misses:
            # filtering first would destroy the evidence for the decision.
            vec_results = vector_search(store, vec_query, top_k=top_k)
            vec_hits = [(r["id"], similarity_from_distance(r.get("vec_distance")))
                        for r in vec_results]
            try:
                provider, model, _, _ = _resolve_embedding_config(store.config)
                verdict = _evaluate_grounding(
                    store, store.config, vec_hits, provider=provider, model=model)
            except Exception:
                verdict = None
            # Enforcement is a separate act from judgement, and it is opt-in.
            # Shadow mode reports the verdict while every row still flows, so
            # the gate can be measured against real traffic before it is
            # allowed to withhold anything.
            if (verdict is not None and verdict.enforced
                    and verdict.floor is not None):
                vec_results = [r for r in vec_results
                               if similarity_from_distance(r.get("vec_distance"))
                               >= verdict.floor]
            if verdict is not None and verdict.should_warn:
                _log.info("grounding %s for query %r: %s",
                          verdict.verdict, query[:80], verdict.reason)
            vec_ranked = [(r["id"], 1.0 / (1.0 + r.get("vec_distance", 1.0)))
                          for r in vec_results]
    except Exception:
        pass

    if grounding is not None and verdict is not None:
        grounding["verdict"] = verdict

    # Read ranking config from store (falls back to defaults if unavailable)
    rcfg = getattr(store.config, "ranking", None)
    cfg_weights = rcfg.ensemble_weights if rcfg else _ENSEMBLE_WEIGHTS_DEFAULT
    cfg_rrf_k = rcfg.rrf_k if rcfg else _RRF_K_DEFAULT

    # Merge results
    if ranking == "ensemble":
        # Collect all candidate node IDs for weight/recency scoring
        all_ids: set[str] = set()
        for ranked in (fts_ranked, graph_ranked, vec_ranked):
            all_ids.update(nid for nid, _ in ranked)

        sources: dict[str, list[tuple[str, float]]] = {"fts": fts_ranked}
        if graph_ranked:
            sources["graph"] = graph_ranked
        if vec_ranked:
            sources["vector"] = vec_ranked
        if all_ids:
            # Ranking-signal sources degrade independently: losing one
            # (bad rows, broken table) drops that signal, not retrieval.
            try:
                sources["node_weight"] = _node_weight_scores(store, all_ids)
            except Exception:
                pass
            try:
                sources["recency"] = _recency_score(store, all_ids)
            except Exception:
                pass
            # Stigmergic injection-usefulness — separate channel from topology.
            # Weight is the user's explicit override (ranking.pheromone_weight>0)
            # else the auto-ramped learned weight (0 until trails mature).
            try:
                phero_weight = cfg_weights.get("pheromone", 0) or _learned_pheromone_weight(store)
                if phero_weight > 0:
                    phero = _pheromone_scores(store, all_ids)
                    if phero:
                        sources["pheromone"] = phero
                        cfg_weights = {**cfg_weights, "pheromone": phero_weight}
            except Exception:
                pass
            # Learned pair co-activation — its own channel with its own ramp,
            # never folded into edge weight.
            try:
                co_weight = (cfg_weights.get("coactivation", 0)
                             or _learned_coactivation_weight(store))
                if co_weight > 0:
                    co = _coactivation_scores(store, all_ids)
                    if co:
                        sources["coactivation"] = co
                        cfg_weights = {**cfg_weights, "coactivation": co_weight}
            except Exception:
                pass

        merged = _weighted_ensemble(sources, weights=cfg_weights)
    else:
        # Legacy RRF fallback
        ranked_lists = [fts_ranked]
        if graph_ranked:
            ranked_lists.append(graph_ranked)
        if vec_ranked:
            ranked_lists.append(vec_ranked)
        merged = _rrf_merge(*ranked_lists, k=cfg_rrf_k) if len(ranked_lists) > 1 else fts_ranked

    # Fetch full nodes, drawing from the merged candidate list until top_k
    # results or exhaustion — drop-filtering used to happen after slicing
    # top_k, silently returning short result sets. Superseded nodes never
    # surface — follow extra['superseded_by'] to the live replacement when
    # it isn't already a candidate of its own, otherwise drop the stale
    # entry. Archived nodes are fenced unless the caller opted in. Ranking
    # order of survivors is unchanged: candidates are consumed in merged
    # order and never re-scored.
    from .store import node_expired

    candidate_ids = {nid for nid, _ in merged}
    results = []
    seen: set[str] = set()
    fenced_nodes: dict[str, dict] = {}
    trust_omissions: Counter[str] = Counter()
    trusted_at = None
    trusted_today = None
    if trusted_only:
        from .trust import _operation_time

        trusted_at = _operation_time(evaluation_time)
        trusted_today = trusted_at.date().isoformat()
    for nid, score in merged:
        if len(results) >= top_k:
            break
        try:
            node = store.get_node(nid)
            orig = node
            hops = 0
            while node is not None and node.get("status") == "superseded":
                # When include_archived is True, the caller opted into
                # retired content — show the superseded node itself,
                # do not redirect to its successor (R3.1 identity: the
                # flag must reveal exactly what the default withheld).
                if include_archived:
                    break  # fall through to normal inclusion path
                successor = (node.get("extra") or {}).get("superseded_by")
                if not successor or hops >= 5:
                    # No reachable successor. Fence it — but only counted
                    # if the caller could ever see it: an expired candidate
                    # stays invisible with or without the escape hatch.
                    if include_expired or not node_expired(orig):
                        fenced_nodes[nid] = orig
                    node = None
                    break
                if successor in candidate_ids:
                    # Dedup, not a fence: the successor ranks as its own candidate.
                    node = None
                    break
                node = store.get_node(successor)
                hops += 1
            if node is None:
                continue
            if (trusted_only or not include_expired) and node_expired(
                node, today=trusted_today if trusted_only else None
            ):
                if trusted_only:
                    trust_omissions["invalidated"] += 1
                continue
            if not include_archived and node.get("status") == "archived":
                fenced_nodes[node["id"]] = node
                continue
            if trusted_only:
                from .trust import node_trust_decision

                decision = node_trust_decision(store, node, at=trusted_at)
                if not decision.eligible:
                    trust_omissions[decision.reason] += 1
                    continue
            if node["id"] in seen:
                continue
            seen.add(node["id"])
            node["confidence"] = round(score, 4)
            node["rrf_score"] = round(score, 6)  # backward compat
            node["edges_out"] = store.edges_from(node["id"])[:5]
            results.append(node)
        except Exception:
            continue  # one malformed candidate never zeroes retrieval

    if fence_stats is not None:
        if not include_archived and len(results) < top_k:
            # Archived FTS matches were fenced upstream (inside fts_search)
            # and never became candidates, so the loop above cannot have
            # counted them — with the result set short, any of them would
            # have ranked. One extra FTS query, only on this short path.
            # Expiry parity with the real results: an expired archived hit
            # would stay invisible even through the escape hatch, so it
            # never counts toward the note.
            try:
                # R3.1 identity (Amendment 4): the fenced set is exactly
                # what the default query withheld AND what include_archived
                # reveals. Derive it purely from the FTS matches the flag
                # would surface minus what the default already showed.
                # No redirect exception: a superseded node whose successor
                # doesn't match the query is still withheld by default and
                # revealed by the flag, so it must be counted.
                unfenced = store.fts_search(query, limit=10000,
                                            include_archived=True)
                for r in unfenced:
                    if r["id"] in fenced_nodes:
                        continue
                    if not (include_expired or not node_expired(r)):
                        continue
                    status = r.get("status")
                    if status in ("archived", "superseded"):
                        fenced_nodes[r["id"]] = r
            except Exception:
                pass
        fence_stats["fenced"] = len(fenced_nodes)
        # Callers that post-filter results (tags/owner) apply the same
        # predicates to these before deciding on the fence note.
        fence_stats["fenced_nodes"] = list(fenced_nodes.values())
        # Report the total candidate pool size so build_fence_note can
        # distinguish "corpus exhausted" (candidates ≤ top_k) from
        # "window truncated" (candidates > top_k but results < top_k
        # because candidates were filtered/expired/fenced).
        fence_stats["candidate_count"] = len(candidate_ids)
        if trusted_only:
            fence_stats["trusted_omissions"] = dict(sorted(trust_omissions.items()))

    return results


def build_trust_note(omissions: dict[str, int] | None) -> str:
    """Human-only disclosure for admission-controlled recall."""
    omissions = omissions or {}
    labels = (
        ("legacy/unverified", "unverified"),
        ("not-yet-valid", "not_yet_valid"),
        ("invalidated/expired", "invalidated"),
        ("mutual contradiction", "mutual_contradiction"),
        ("inactive", "inactive"),
    )
    parts = [
        f"{label}={omissions.get(reason, 0)}"
        for label, reason in labels
        if omissions.get(reason, 0)
    ]
    if not parts:
        return "(trusted-only admission: no candidates omitted)"
    return f"(trusted-only omissions: {'; '.join(parts)})"


def build_fence_note(
    results: list[dict],
    fenced_nodes: list[dict],
    top_k: int,
    include_archived: bool,
    candidate_count: int = 0,
) -> str:
    """Build the fence note string from the actual fenced set (R3.1, R5.1).

    Single source of truth for both CLI and MCP surfaces. The wording names
    exactly the categories present in the fenced set — derived from the
    actual node statuses, not a fixed string.

    The candidate window disclosure is emitted when the window was genuinely
    the limiting factor — i.e., the candidate pool exceeded top_k but the
    results are still short because candidates were filtered, expired, or
    fenced. When the corpus is simply exhausted (candidate pool ≤ top_k),
    the window played no part and stays silent (R5.1).

    Returns an empty string when results fill top_k (no note needed).
    """
    if len(results) >= top_k:
        return ""
    parts = []
    fenced = len(fenced_nodes)
    if not include_archived and fenced:
        statuses = {n.get("status", "archived") for n in fenced_nodes}
        cats = "/".join(sorted(statuses))
        parts.append(f"{fenced} {cats} results fenced; use "
                     f"--include-archived / include_archived=True to see them")
    # Emit the window disclosure when the window was the limiting factor:
    # the candidate pool exceeded top_k but results are short (candidates
    # were filtered/expired/fenced). Stay silent when the corpus was
    # simply exhausted (candidate pool ≤ top_k).
    if candidate_count > top_k or fenced:
        parts.append("candidate window (3*top_k FTS + top_k vector)")
    return f"({'; '.join(parts)})" if parts else ""


def auto_select_tier(available_tokens: int | None = None) -> str:
    """Select the best context tier for the given token budget.

    If available_tokens is None, defaults to 'abridged' (safe middle ground).
    """
    if available_tokens is None:
        return "abridged"
    for tier in TIER_ORDER:
        if TIER_BUDGETS[tier] <= available_tokens:
            return tier
    return "index"


def _estimate_tokens(text: str) -> int:
    """Estimate token count without external dependencies.

    Uses word-based heuristic: ~1.3 tokens per whitespace-delimited word
    for English prose, which is more accurate than fixed char ratios
    across mixed content (code, structured data, natural language).
    Falls back to char/4 for very short text.
    """
    words = text.split()
    if len(words) < 5:
        return max(1, len(text) // 4)
    return int(len(words) * 1.3)


def format_context_block(
    store: Store,
    results: list[dict],
    query: str = "",
    level: str | None = None,
    max_tokens_approx: int | None = None,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
    grounding: dict | None = None,
) -> str:
    """Format search results as a context block for CLAUDE.md injection.

    Supports five tiers: full, abridged, summarized, executive, index.
    Auto-selects tier based on max_tokens_approx if level is not specified.
    Enforces token budget: if output exceeds the tier budget, progressively
    drops results until it fits.

    When ``adapter`` names a client, operational nodes scoped to a different
    client are dropped from the full/abridged tiers; with no adapter every node
    surfaces (the right default for human-facing ``kin context``).

    ``grounding`` is the dict a caller passed to ``hybrid_search``. This is the
    single place rows become text destined for a context window, so the verdict
    is stamped onto the block HERE rather than in each caller. Contamination is
    text entering context, not intent — and a verdict every caller must
    remember to honour is a comment, not a gate. Callers that omit it get the
    legacy behaviour, so the note is additive, never a silent drop.
    """
    if not results:
        return "## Kindex: No relevant context found.\n"

    if level is None:
        level = auto_select_tier(max_tokens_approx)

    budget = max_tokens_approx or TIER_BUDGETS.get(level, 1500)
    if trusted_only:
        from .trust import _operation_time

        evaluation_time = _operation_time(evaluation_time)
    formatter_fn = _TIER_FORMATTERS.get(level, _format_abridged)
    if trusted_only:
        formatter = partial(
            formatter_fn,
            adapter=adapter,
            trusted_only=True,
            evaluation_time=evaluation_time,
        )
    else:
        # Preserve the exact legacy call shape for ordinary recall, including
        # callers that provide a compatible custom tier formatter.
        formatter = partial(formatter_fn, adapter=adapter)

    # The grounding note is prepended to whichever body wins the budget loop,
    # and its own cost is charged against the budget — a warning that gets
    # trimmed away is worse than no warning.
    note = ""
    verdict = (grounding or {}).get("verdict")
    if verdict is not None:
        try:
            note = verdict.note()
        except Exception:
            note = ""
    prefix = f"{note}\n\n" if note else ""

    # Try with all results, then progressively trim until within budget
    for n in range(len(results), 0, -1):
        output = formatter(store, results[:n], query)
        if _estimate_tokens(prefix + output) <= budget:
            return prefix + output

    # Even one result exceeds budget — return truncated
    output = formatter(store, results[:1], query)
    max_chars = budget * 4 - len(prefix)
    if len(output) > max_chars:
        output = output[:max_chars] + "\n\n*[truncated to fit token budget]*"
    return prefix + output


def _gather_domains(results: list[dict]) -> set[str]:
    domains: set[str] = set()
    for r in results:
        for d in (r.get("domains") or []):
            domains.add(d)
    return domains


def _trusted_context_nodes(
    store: Store,
    nodes: list[dict],
    *,
    trusted_only: bool,
    evaluation_time: str | datetime | None,
) -> list[dict]:
    """Apply trusted admission to formatter-owned auxiliary pulls."""
    if not trusted_only:
        return nodes
    from .store import node_expired
    from .trust import _operation_time, filter_trusted_nodes

    at = _operation_time(evaluation_time)
    today = at.date().isoformat()
    current = [node for node in nodes if not node_expired(node, today=today)]
    trusted, _ = filter_trusted_nodes(store, current, at=at)
    return trusted


def _append_operational(
    store: Store,
    lines: list[str],
    verbose: bool = False,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> None:
    """Append current operational nodes, optionally trust-admitted."""
    ops = store.operational_summary()
    if adapter is not None:
        ops = {
            key: [
                node for node in values
                if not adapter_scoped_out(node.get("tags"), adapter)
            ]
            for key, values in ops.items()
        }
    if trusted_only:
        ops = {
            key: _trusted_context_nodes(
                store,
                values,
                trusted_only=True,
                evaluation_time=evaluation_time,
            )
            for key, values in ops.items()
        }

    def _extra_of(node: dict) -> dict:
        extra = node.get("extra")
        return extra if isinstance(extra, dict) else {}

    if ops["constraints"]:
        lines.append("\n### Active constraints")
        for constraint in ops["constraints"][:5 if verbose else 3]:
            try:
                extra = _extra_of(constraint)
                lines.append(f"- [{extra.get('action', 'warn')}] {constraint['title']}")
                if verbose and extra.get("trigger"):
                    lines.append(f"  trigger: {extra['trigger']}")
            except Exception:
                continue

    if ops["watches"]:
        lines.append("\n### Watches")
        for watch in ops["watches"][:5 if verbose else 3]:
            try:
                extra = _extra_of(watch)
                values = [f"! {watch['title']}"]
                if extra.get("owner"):
                    values.append(f"@{extra['owner']}")
                if extra.get("expires"):
                    values.append(f"(expires {extra['expires']})")
                lines.append(f"- {' '.join(values)}")
            except Exception:
                continue

    if verbose and ops["checkpoints"]:
        lines.append("\n### Checkpoints")
        for checkpoint in ops["checkpoints"][:5]:
            try:
                trigger = _extra_of(checkpoint).get("trigger", "")
                lines.append(
                    f"- [ ] {checkpoint['title']}"
                    + (f" (trigger: {trigger})" if trigger else "")
                )
            except Exception:
                continue

    if verbose and ops["directives"]:
        lines.append("\n### Directives")
        for directive in ops["directives"][:5]:
            try:
                scope = _extra_of(directive).get("scope", "")
                lines.append(
                    f"- {directive['title']}"
                    + (f" [scope: {scope}]" if scope else "")
                )
            except Exception:
                continue


# ── Full tier ─────────────────────────────────────────────────────────

def _format_full(
    store: Store,
    results: list[dict],
    query: str,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> str:
    """Full context — everything Kindex knows about the active domain."""
    all_domains = _gather_domains(results)

    lines = [
        "## Relevant Context (Kindex — auto-loaded)",
        f"**Level:** full | **Query:** {query}",
        f"**Active tags:** [{', '.join(sorted(all_domains)[:8])}]",
        "",
        "### Key concepts",
    ]

    for r in results:
        title = r.get("title", r["id"])
        node_type = r.get("type", "concept")
        content = _strip_frontmatter(r.get("content") or "")[:600]
        weight = r.get("weight", 0)
        edges_out = r.get("edges_out", [])

        age = _node_age_str(r)
        caveat = _staleness_caveat(r)
        age_tag = f", {age}" if age else ""
        lines.append(f"\n#### [{node_type}] {title} (w={weight:.2f}{age_tag}){caveat}")
        if content:
            lines.append(content)

        # Provenance
        prov = []
        if r.get("prov_source"):
            prov.append(f"source: {r['prov_source']}")
        if r.get("prov_when"):
            prov.append(f"when: {r['prov_when'][:10]}")
        if r.get("prov_activity"):
            prov.append(f"via: {r['prov_activity']}")
        if prov:
            lines.append(f"*Provenance: {', '.join(prov)}*")

        if r.get("aka"):
            lines.append(f"*AKA: {', '.join(r['aka'])}*")

        if edges_out:
            connected = [f"{e.get('to_title', e['to_id'])} [{e['type']}]" for e in edges_out[:8]]
            lines.append(f"*Connects: {', '.join(connected)}*")

    # Open questions
    questions = store.all_nodes(node_type="question", status="active", limit=5)
    questions = _trusted_context_nodes(
        store,
        questions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if questions:
        lines.append("\n### Open questions")
        for q in questions:
            lines.append(f"- {q['title']}")
            if q.get("content"):
                lines.append(f"  Context: {_strip_frontmatter(q['content'])[:200]}")

    # Recent decisions (active only — retired decisions stay retired)
    decisions = store.all_nodes(node_type="decision", status="active", limit=5)
    decisions = _trusted_context_nodes(
        store,
        decisions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if decisions:
        lines.append("\n### Recent decisions")
        for d in decisions:
            when = d.get("prov_when", "")[:10]
            lines.append(f"- {when}: {d['title']}")
            if d.get("content"):
                lines.append(f"  Rationale: {_strip_frontmatter(d['content'])[:200]}")

    # Operational nodes
    _append_operational(
        store,
        lines,
        verbose=True,
        adapter=adapter,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )

    return "\n".join(lines) + "\n"


# ── Abridged tier ─────────────────────────────────────────────────────

def _format_abridged(
    store: Store,
    results: list[dict],
    query: str,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> str:
    """Abridged — key nodes, trimmed content, edges preserved."""
    all_domains = _gather_domains(results)

    lines = [
        "## Relevant Context (Kindex — auto-loaded)",
        f"**Level:** abridged | **Active tags:** [{', '.join(sorted(all_domains)[:8])}]",
        "",
        "### Key concepts",
    ]

    char_budget = 6000  # ~1500 tokens
    used = sum(len(l) for l in lines)

    for r in results:
        title = r.get("title", r["id"])
        node_type = r.get("type", "concept")
        content_preview = _strip_frontmatter(r.get("content") or "")[:200]
        edges_out = r.get("edges_out", [])
        connected = ", ".join(e.get("to_title", e["to_id"]) for e in edges_out[:3])

        age = _node_age_str(r)
        caveat = _staleness_caveat(r)
        age_suffix = f" [{age}]" if age else ""
        block = f"- **{title}** ({node_type}{age_suffix}){caveat}: {content_preview}"
        if connected:
            block += f"\n  *Connected to: {connected}*"
        block += "\n"

        if used + len(block) > char_budget:
            break
        lines.append(block)
        used += len(block)

    # Open questions (brief)
    questions = store.all_nodes(node_type="question", status="active", limit=3)
    questions = _trusted_context_nodes(
        store,
        questions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if questions:
        lines.append("\n### Open questions")
        for q in questions:
            lines.append(f"- {q['title']}")

    # Recent decisions (brief; active only)
    decisions = store.all_nodes(node_type="decision", status="active", limit=3)
    decisions = _trusted_context_nodes(
        store,
        decisions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if decisions:
        lines.append("\n### Recent decisions")
        for d in decisions:
            when = d.get("prov_when", "")[:10]
            lines.append(f"- {when}: {d['title']}")

    # Active constraints and watches (brief)
    _append_operational(
        store,
        lines,
        verbose=False,
        adapter=adapter,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )

    return "\n".join(lines) + "\n"


# ── Summarized tier ───────────────────────────────────────────────────

def _format_summarized(
    store: Store,
    results: list[dict],
    query: str,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> str:
    """Summarized — paragraph-form narrative per domain cluster."""
    all_domains = _gather_domains(results)

    lines = [
        "## Kindex Context (summarized)",
        f"**Tags:** {', '.join(sorted(all_domains)[:6])}",
        "",
    ]

    # Group results by domain
    domain_groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        domains = r.get("domains") or ["general"]
        for d in domains[:1]:  # primary domain only
            domain_groups[d].append(r)

    for domain, nodes in domain_groups.items():
        titles = [n.get("title", n["id"]) for n in nodes[:5]]
        # Build a synthesized sentence about this cluster
        summaries = []
        for n in nodes[:3]:
            content = _strip_frontmatter(n.get("content") or "")[:150]
            if content:
                summaries.append(f"{n['title']}: {content}")

        lines.append(f"**{domain}:** {'; '.join(summaries)}")
        lines.append("")

    # Open questions as a single line
    questions = store.all_nodes(node_type="question", status="active", limit=2)
    questions = _trusted_context_nodes(
        store,
        questions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if questions:
        q_titles = [q["title"] for q in questions]
        lines.append(f"**Open questions:** {'; '.join(q_titles)}")

    return "\n".join(lines) + "\n"


# ── Executive tier ────────────────────────────────────────────────────

def _format_executive(
    store: Store,
    results: list[dict],
    query: str,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> str:
    """Executive — 2-3 sentences per active thread. Minimum to orient."""
    all_domains = _gather_domains(results)
    domain_str = ", ".join(sorted(all_domains)[:4])

    # One sentence per top result
    summaries = []
    for r in results[:5]:
        title = r.get("title", r["id"])
        content = _strip_frontmatter(r.get("content") or "")[:80]
        if content:
            summaries.append(f"{title} — {content}")
        else:
            summaries.append(title)

    block = f"Kindex [{domain_str}]: {'. '.join(summaries)}."

    questions = store.all_nodes(node_type="question", status="active", limit=1)
    questions = _trusted_context_nodes(
        store,
        questions,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
    )
    if questions:
        block += f" Open: {questions[0]['title']}"

    return block + "\n"


# ── Index tier ────────────────────────────────────────────────────────

def _format_index(
    store: Store,
    results: list[dict],
    query: str,
    adapter: str | None = None,
    trusted_only: bool = False,
    evaluation_time: str | datetime | None = None,
) -> str:
    """Index — node titles and edge types only. Just the map."""
    titles = []
    for r in results:
        title = r.get("title", r["id"])
        node_type = r.get("type", "concept")
        edges = r.get("edges_out", [])
        if edges:
            edge_types = set(e["type"] for e in edges[:3])
            titles.append(f"{title}({node_type})→[{','.join(edge_types)}]")
        else:
            titles.append(f"{title}({node_type})")
    return f"Kindex index: {' | '.join(titles)}\n"


_TIER_FORMATTERS = {
    "full": _format_full,
    "abridged": _format_abridged,
    "summarized": _format_summarized,
    "executive": _format_executive,
    "index": _format_index,
}


def generate_codebook(store: Store, min_weight: float = 0.5) -> tuple[str, str]:
    """Generate deterministic codebook of high-value nodes.

    Returns (text, sha256_hash). Sorted by node ID for prefix cache stability.
    Excludes session nodes. Includes: index, truncated ID, type, weight, domains, title.
    """
    import hashlib

    from .store import node_retired

    nodes = store.all_nodes(limit=5000)
    eligible = [n for n in nodes
                if n.get("type") != "session" and not node_retired(n)
                and (n.get("weight") or 0) >= min_weight]
    eligible.sort(key=lambda n: n["id"])

    lines = []
    for i, n in enumerate(eligible, 1):
        tags = ",".join(n.get("tags") or n.get("domains") or [])[:40]
        title = (n.get("title") or n["id"])[:80]
        lines.append(
            f"#{i:03d} id:{n['id'][:8]} type:{n.get('type', 'concept')} "
            f"w:{n.get('weight', 0):.2f} tags:[{tags}] \"{title}\""
        )

    header = f"[CODEBOOK v1 | {len(eligible)} entries]"
    text = header + "\n" + "\n".join(lines)
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    return text, h


def build_codebook_index(codebook_text: str) -> dict[str, str]:
    """Parse codebook text into {truncated_id: entry_number} mapping."""
    index: dict[str, str] = {}
    for line in codebook_text.split("\n"):
        if not line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            entry_num = parts[0]  # e.g. "#042"
            for part in parts:
                if part.startswith("id:"):
                    index[part[3:]] = entry_num
                    break
    return index


def predict_tier2(
    store: Store,
    query: str,
    search_results: list[dict],
    top_k: int = 8,
) -> list[dict]:
    """Expand search results with graph-predicted neighbors.

    Uses 1-hop edges from top hits to predict related nodes the user
    might ask about next. Returns merged list sorted by node ID for
    deterministic prefix ordering.
    """
    from .store import node_retired

    hit_ids = {r["id"] for r in search_results}
    predicted: dict[str, dict] = {}

    for hit in search_results[:3]:
        for edge in store.edges_from(hit["id"])[:5]:
            tid = edge["to_id"]
            if tid not in hit_ids and tid not in predicted:
                node = store.get_node(tid)
                if node and node.get("type") != "session" and not node_retired(node):
                    predicted[tid] = node

    merged = list(search_results[:top_k])
    for node in sorted(predicted.values(), key=lambda n: n["id"]):
        if len(merged) >= top_k:
            break
        merged.append(node)
    return merged


def format_tier2(
    results: list[dict],
    codebook_index: dict[str, str],
    max_tokens: int = 4000,
) -> str:
    """Format tier 2 context with codebook back-references.

    Results sorted by node ID for deterministic prefix ordering.
    Content trimmed to fit within max_tokens budget.
    """
    results_sorted = sorted(results, key=lambda r: r["id"])
    char_budget = max_tokens * 4
    lines: list[str] = ["## Relevant Context\n"]
    used = 0

    for r in results_sorted:
        entry = codebook_index.get(r["id"][:8], "?")
        title = r.get("title") or r["id"]
        content = _strip_frontmatter(r.get("content") or "")[:1000]
        edges = r.get("edges_out") or []

        block_lines = [f"### {entry} {title}"]
        if content:
            block_lines.append(content)
        if edges:
            refs = []
            for e in edges[:5]:
                t_entry = codebook_index.get(e["to_id"][:8], "?")
                refs.append(f"{t_entry} {e.get('to_title', e['to_id'])} (w={e['weight']:.1f})")
            block_lines.append(f"Connects: {', '.join(refs)}")
        block_lines.append("")

        block = "\n".join(block_lines)
        if used + len(block) > char_budget:
            break
        lines.append(block)
        used += len(block)

    return "\n".join(lines)


def detect_domain_from_path(store: Store, cwd: str) -> list[str]:
    """Given a working directory, find relevant domain nodes.

    Searches for nodes whose prov_source matches the path.
    """
    # Search for nodes referencing this path
    results = store.fts_search(cwd, limit=5)
    domains: set[str] = set()
    for r in results:
        for d in (r.get("domains") or []):
            domains.add(d)
    return sorted(domains)
