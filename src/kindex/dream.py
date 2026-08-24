"""Dream cycle — post-session knowledge consolidation.

Performs memory consolidation on the knowledge graph:
- Fuzzy deduplication (title similarity + content overlap)
- Suggestion auto-application
- Domain-based edge strengthening

Three invocation modes:
- lightweight: dedup + suggestions only, <5s target
- full: all non-LLM consolidation
- deep: includes LLM-powered cluster summarisation (in dream_deep.py)

Designed to run from CLI (kin dream), cron (daemon.py), or
as a detached subprocess from the Claude Code Stop hook.
"""

from __future__ import annotations

import datetime
import difflib
import fcntl
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

from .schema import EDIT_POLICY

logger = logging.getLogger(__name__)

# Node types that dream must never touch (CD008). Derived from the schema
# edit policy as the single source of truth: additive types (history matters
# — a merge rewrites content the edit policy forbids) plus managed types
# (task/session/coordination — subsystem-owned state lives in extra and a
# merge would destroy members/cursors/messages/locks).
PROTECTED_TYPES = (frozenset(EDIT_POLICY["additive"])
                   | frozenset(EDIT_POLICY["managed"]))

# Similarity thresholds (CD002)
DEFAULT_MERGE_THRESHOLD = 0.95
DEFAULT_SUGGEST_THRESHOLD = 0.85
DEFAULT_MAX_NEW_SUGGESTIONS = 100

LAST_DREAM_STARTED_META = "last_dream_started"
LAST_DREAM_RUN_META = "last_dream_run"
LAST_DREAM_MODE_META = "last_dream_mode"
DEFAULT_DREAM_MIN_INTERVAL = 3600


# ── Locking ───────────────────────────────────────────────────────────


def _lock_path(config: Config) -> Path:
    return config.data_path / "dream.lock"


def _acquire_lock(config: Config) -> int | None:
    """Try to acquire exclusive dream lock. Returns fd or None if locked."""
    lock_file = _lock_path(config)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        return None


def _release_lock(fd: int, config: Config) -> None:
    """Release dream lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def _dream_min_interval(config: Config) -> int:
    return max(
        0,
        int(getattr(config.reminders, "dream_min_interval", DEFAULT_DREAM_MIN_INTERVAL) or 0),
    )


def dream_due(
    store: Store,
    *,
    min_interval_seconds: int,
    now: datetime.datetime | None = None,
) -> dict:
    """Return whether scheduled dream work should run now.

    Uses the last start marker, not only the successful completion marker, so a
    killed or long-running detached dream cannot be immediately respawned by the
    next hook event.
    """
    if min_interval_seconds <= 0:
        return {"due": True}

    now = now or _now()
    last_value = (
        store.get_meta(LAST_DREAM_STARTED_META)
        or store.get_meta(LAST_DREAM_RUN_META)
    )
    last = _parse_timestamp(last_value)
    if last is None:
        return {"due": True}

    elapsed = (now - last).total_seconds()
    if elapsed >= min_interval_seconds:
        return {"due": True, "last_started": last_value, "elapsed_seconds": int(elapsed)}

    next_allowed = last + datetime.timedelta(seconds=min_interval_seconds)
    return {
        "due": False,
        "skipped": "recent",
        "last_started": last_value,
        "next_allowed": next_allowed.isoformat(timespec="seconds"),
        "remaining_seconds": int(min_interval_seconds - elapsed),
    }


def mark_dream_started(store: Store, mode: str, *, when: datetime.datetime | None = None) -> str:
    timestamp = (when or _now()).isoformat(timespec="seconds")
    store.set_meta(LAST_DREAM_STARTED_META, timestamp)
    store.set_meta(LAST_DREAM_MODE_META, mode)
    return timestamp


# ── Similarity ────────────────────────────────────────────────────────


def title_similarity(a: str, b: str) -> float:
    """Normalised title similarity using SequenceMatcher."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def content_overlap(a: str, b: str) -> float:
    """Content similarity via SequenceMatcher on first 500 chars."""
    if not a or not b:
        return 0.0
    a_trunc = a[:500].lower()
    b_trunc = b[:500].lower()
    return difflib.SequenceMatcher(None, a_trunc, b_trunc).ratio()


def combined_similarity(node_a: dict, node_b: dict) -> float:
    """Weighted combination: 70% title, 30% content."""
    t_sim = title_similarity(node_a.get("title", ""), node_b.get("title", ""))
    c_sim = content_overlap(node_a.get("content", ""), node_b.get("content", ""))
    return 0.7 * t_sim + 0.3 * c_sim


# ── Core operations ──────────────────────────────────────────────────


def find_duplicates(
    store: Store,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    suggest_threshold: float = DEFAULT_SUGGEST_THRESHOLD,
) -> dict:
    """Find near-duplicate node pairs.

    Returns {"merge": [(a, b, score)], "suggest": [(a, b, score)]}.
    """
    nodes = store.all_nodes(status="active", limit=5000)
    # Filter out protected types
    nodes = [n for n in nodes if n.get("type", "concept") not in PROTECTED_TYPES]

    merge_pairs: list[tuple[str, str, float]] = []
    suggest_pairs: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    min_title_for_suggest = max(0.0, (suggest_threshold - 0.3) / 0.7)

    # Group by first 4 chars of lowercase title for O(n*k) instead of O(n^2)
    # Cap bucket size at 50 to bound worst-case pairwise comparisons
    buckets: dict[str, list[dict]] = {}
    for n in nodes:
        title = (n.get("title") or "").lower()
        if len(title) < 4:
            continue
        key = title[:4]
        bucket = buckets.setdefault(key, [])
        if len(bucket) < 50:
            bucket.append(n)

    for bucket_nodes in buckets.values():
        if len(bucket_nodes) < 2:
            continue
        for i, a in enumerate(bucket_nodes):
            for b in bucket_nodes[i + 1:]:
                pair_key = tuple(sorted([a["id"], b["id"]]))
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                t_sim = title_similarity(a.get("title", ""), b.get("title", ""))
                if t_sim < min_title_for_suggest:
                    continue
                c_sim = content_overlap(a.get("content", ""), b.get("content", ""))
                score = 0.7 * t_sim + 0.3 * c_sim
                if score >= merge_threshold:
                    merge_pairs.append((a["id"], b["id"], score))
                elif score >= suggest_threshold:
                    suggest_pairs.append((a["id"], b["id"], score))

    return {"merge": merge_pairs, "suggest": suggest_pairs}


def merge_nodes(store: Store, source_id: str, target_id: str) -> bool:
    """Merge source into target: move edges, merge content, archive source.

    Returns True if merge succeeded.
    """
    source = store.get_node(source_id)
    target = store.get_node(target_id)
    if not source or not target:
        return False
    # Defense in depth: never merge protected types even if a caller bypasses
    # find_duplicates' filter — subsystem-owned/history-bearing nodes survive.
    if (source.get("type", "concept") in PROTECTED_TYPES
            or target.get("type", "concept") in PROTECTED_TYPES):
        return False

    # Move edges from source to target
    for edge in store.edges_from(source_id):
        if edge["to_id"] != target_id:
            store.add_edge(
                target_id, edge["to_id"],
                edge_type=edge.get("type", "relates_to"),
                weight=edge.get("weight", 0.3),
                provenance="dream-cycle merge",
            )
    for edge in store.edges_to(source_id):
        if edge["from_id"] != target_id:
            store.add_edge(
                edge["from_id"], target_id,
                edge_type=edge.get("type", "relates_to"),
                weight=edge.get("weight", 0.3),
                provenance="dream-cycle merge",
            )

    # Merge content if source has unique content
    source_content = source.get("content", "") or ""
    target_content = target.get("content", "") or ""
    if source_content and source_content not in target_content:
        merged = f"{target_content}\n\n[Merged from: {source['title']}]\n{source_content}"
        store.update_node(target_id, content=merged)

    # Boost target weight
    sw = source.get("weight", 0.5) or 0.5
    tw = target.get("weight", 0.5) or 0.5
    store.update_node(target_id, weight=min(1.0, max(tw, sw)))

    # Archive source (CD001: never delete, only archive). Preserve the
    # existing extra (locks, claims, expiry, ...) — only annotate the merge.
    merged_extra = dict(source.get("extra") or {})
    merged_extra.update({"merged_into": target_id, "merged_by": "dream-cycle"})
    store.update_node(
        source_id, status="archived", weight=0.01,
        extra=merged_extra,
    )
    return True


def auto_apply_suggestions(store: Store) -> int:
    """Apply pending suggestions where nodes clearly relate.

    Returns count of suggestions applied.
    """
    suggestions = store.pending_suggestions(limit=100)
    applied = 0

    for s in suggestions:
        concept_a = s.get("concept_a", "")
        concept_b = s.get("concept_b", "")

        # Resolve to actual nodes
        node_a = store.get_node(concept_a) or store.get_node_by_title(concept_a)
        node_b = store.get_node(concept_b) or store.get_node_by_title(concept_b)

        if not node_a or not node_b:
            continue
        if node_a.get("status") != "active" or node_b.get("status") != "active":
            continue

        # Check title similarity for auto-apply confidence
        sim = title_similarity(
            node_a.get("title", ""), node_b.get("title", ""),
        )
        if sim < 0.7:
            continue

        # Check edge doesn't already exist
        existing_out = {e["to_id"] for e in store.edges_from(node_a["id"])}
        existing_in = {e["from_id"] for e in store.edges_to(node_a["id"])}
        if node_b["id"] in existing_out or node_b["id"] in existing_in:
            store.update_suggestion(s["id"], "accepted")
            applied += 1
            continue

        store.add_edge(
            node_a["id"], node_b["id"],
            edge_type="relates_to",
            weight=0.4,
            provenance="dream-cycle auto-apply",
        )
        store.update_suggestion(s["id"], "accepted")
        applied += 1

    return applied


def strengthen_domain_edges(
    store: Store,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Find active nodes sharing domains but lacking edges; create weak links."""
    import json

    nodes = store.all_nodes(status="active", limit=2000)
    # Build domain -> node_ids index
    domain_index: dict[str, list[dict]] = {}
    for n in nodes:
        if n.get("type", "concept") in PROTECTED_TYPES:
            continue
        domains = n.get("domains") or []
        if isinstance(domains, str):
            try:
                domains = json.loads(domains)
            except (json.JSONDecodeError, TypeError):
                domains = []
        for d in domains:
            domain_index.setdefault(d, []).append(n)

    created = 0
    seen_pairs: set[tuple[str, str]] = set()

    for domain, members in domain_index.items():
        if len(members) < 2 or len(members) > 50:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = tuple(sorted([a["id"], b["id"]]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Check if edge already exists
                existing = {e["to_id"] for e in store.edges_from(a["id"])}
                if b["id"] in existing:
                    continue
                existing_rev = {e["from_id"] for e in store.edges_to(a["id"])}
                if b["id"] in existing_rev:
                    continue

                if dry_run:
                    created += 1
                    continue

                store.add_edge(
                    a["id"], b["id"],
                    edge_type="relates_to",
                    weight=0.15,
                    provenance="dream-cycle domain co-membership",
                )
                created += 1
                if verbose and created <= 10:
                    print(f"  Domain link: {a['title']} <-> {b['title']} ({domain})")

    return created


# ── Dream cycles ─────────────────────────────────────────────────────


def dream_lightweight(
    config: Config,
    store: Store,
    *,
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Fast dream: dedup detection + suggestion auto-apply.

    Target: <5s for graphs under 5000 nodes (CD009).
    No LLM calls (CD003).
    """
    results: dict = {}

    # Fuzzy dedup
    dupes = find_duplicates(store)

    # Auto-merge high-confidence pairs. Reviewed stopgap (PRD lineage
    # item 2): snapshot the DB before any automated destructive merge so a
    # false merge is recoverable. Fail-closed: if the snapshot cannot be
    # taken, the merges are skipped — housekeeping never proceeds
    # unprotected.
    merged = 0
    merges_skipped_unprotected = 0
    merge_pairs = dupes["merge"]
    snapshot_ok = True
    if merge_pairs and not dry_run:
        try:
            from .snapshots import snapshot_db
            snapshot_db(store, "dream-merge")
        except Exception as exc:
            snapshot_ok = False
            merges_skipped_unprotected = len(merge_pairs)
            logger.warning(
                "pre-merge snapshot failed (%s); skipping %d auto-merge(s)",
                exc, len(merge_pairs),
            )
    for source_id, target_id, score in merge_pairs:
        if dry_run:
            logger.info("Would merge %s -> %s (score=%.3f)", source_id, target_id, score)
            merged += 1
            continue
        if not snapshot_ok:
            continue
        if merge_nodes(store, source_id, target_id):
            merged += 1
            if verbose:
                print(f"  Merged: {source_id} -> {target_id} (score={score:.3f})")

    # Create suggestions for near-misses. This path runs from hooks, so keep
    # writes bounded even if the graph has a large duplicate backlog.
    max_new_suggestions = max(
        0,
        int(
            getattr(
                config.reminders,
                "dream_max_new_suggestions",
                DEFAULT_MAX_NEW_SUGGESTIONS,
            )
            or 0
        ),
    )
    suggested = 0
    existing_suggestions = 0
    suggestion_candidates = len(dupes["suggest"])
    suggestion_capped = False
    for a_id, b_id, score in dupes["suggest"]:
        if dry_run:
            suggested += 1
            continue
        if suggested >= max_new_suggestions:
            suggestion_capped = True
            break
        if store.suggestion_exists(a_id, b_id):
            existing_suggestions += 1
            continue
        store.add_suggestion(
            concept_a=a_id, concept_b=b_id,
            reason=f"Fuzzy match (score={score:.3f})",
            source="dream-cycle",
        )
        suggested += 1

    # Auto-apply pending suggestions
    applied = 0
    if not dry_run:
        applied = auto_apply_suggestions(store)

    results["merged"] = merged
    results["merges_skipped_unprotected"] = merges_skipped_unprotected
    results["suggested"] = suggested
    results["suggestion_candidates"] = suggestion_candidates
    results["suggestion_existing"] = existing_suggestions
    results["suggestion_cap"] = max_new_suggestions
    results["suggestion_capped"] = suggestion_capped
    results["suggestions_applied"] = applied

    return results


def dream_full(
    config: Config,
    store: Store,
    *,
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Full dream cycle: lightweight + edge strengthening.

    No LLM calls.
    """
    results = dream_lightweight(config, store, verbose=verbose, dry_run=dry_run)

    # Strengthen edges between nodes that share domains
    strengthened = strengthen_domain_edges(store, dry_run=dry_run, verbose=verbose)
    results["edges_strengthened"] = strengthened

    return results


# ── Entry points ─────────────────────────────────────────────────────


def dream_cycle(
    config: Config,
    store: Store,
    *,
    mode: str = "full",
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run a dream cycle with file locking.

    Args:
        mode: 'lightweight', 'full', or 'deep'.
        verbose: Print progress.
        dry_run: Report without making changes.

    Returns dict of results, or {"skipped": "locked"} if another cycle is running.
    """
    fd = _acquire_lock(config)
    if fd is None:
        if verbose:
            print("Dream cycle already running (locked). Skipping.")
        return {"skipped": "locked"}

    try:
        if dry_run:
            started = _now().isoformat(timespec="seconds")
        else:
            started = mark_dream_started(store, mode)

        if mode == "lightweight":
            results = dream_lightweight(config, store, verbose=verbose, dry_run=dry_run)
        elif mode == "deep":
            from .dream_deep import dream_deep
            results = dream_deep(config, store, verbose=verbose, dry_run=dry_run)
        else:
            results = dream_full(config, store, verbose=verbose, dry_run=dry_run)

        results["mode"] = mode
        results["started_at"] = started
        results["timestamp"] = _now().isoformat(timespec="seconds")

        # Store last dream marker
        if not dry_run:
            store.set_meta(LAST_DREAM_RUN_META, results["timestamp"])
            store.set_meta(LAST_DREAM_MODE_META, mode)

        return results
    finally:
        _release_lock(fd, config)


def detach_dream(config: Config, mode: str = "lightweight", *, force: bool = False) -> dict:
    """Spawn a detached dream subprocess if the scheduled cadence allows it.

    Uses start_new_session=True so the child survives parent exit (CD005).
    """
    from .store import Store
    from .setup import _find_kin_path

    min_interval = _dream_min_interval(config)
    fd = _acquire_lock(config)
    if fd is None:
        return {"detached": False, "skipped": "locked", "mode": mode}

    store = Store(config)
    try:
        decision = dream_due(store, min_interval_seconds=min_interval)
        if not force and not decision.get("due", False):
            return {
                "detached": False,
                "mode": mode,
                "min_interval_seconds": min_interval,
                **decision,
            }
        started = mark_dream_started(store, mode)
    finally:
        store.close()
        _release_lock(fd, config)

    kin_path = _find_kin_path()
    log_dir = config.data_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dream.log"

    cmd = [kin_path, "dream", f"--{mode}"]

    with open(log_file, "a") as log_fd:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

    return {
        "detached": True,
        "pid": proc.pid,
        "mode": mode,
        "started_at": started,
        "min_interval_seconds": min_interval,
    }
