"""Deep dream — LLM-powered cluster consolidation.

Separated from dream.py to structurally enforce CD003:
lightweight/detach mode cannot accidentally invoke LLM calls
because the import boundary prevents it.

Only invoked when user explicitly passes --deep flag.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

from .dream import PROTECTED_TYPES, dream_full

from .privacy import protect_logger, redact_text
from .privacy import redacting_print as print

logger = protect_logger(logging.getLogger(__name__))

DEFAULT_CLUSTER_OVERLAP_THRESHOLD = 0.6
CLUSTER_SUMMARY_SOURCE = "dream-deep-cluster-summary"


def dream_deep(
    config: Config,
    store: Store,
    *,
    verbose: bool = False,
    dry_run: bool = False,
    timeout: int = 300,
) -> dict:
    """Deep dream: full cycle + LLM-powered cluster consolidation.

    Uses claude -p for semantic analysis. Only invoked via --deep flag.
    """
    results = dream_full(config, store, verbose=verbose, dry_run=dry_run)

    # Find dense clusters for summarisation
    clusters = _find_clusters(store, min_size=4, max_hops=2)
    summaries_created = 0
    existing_signatures: set[str] = set()
    existing_member_sets: list[set[str]] = []
    for row in store.conn.execute(
        """SELECT extra FROM nodes
             WHERE status = 'active' AND prov_source = ? AND json_valid(extra)""",
        (CLUSTER_SUMMARY_SOURCE,),
    ).fetchall():
        extra = json.loads(row["extra"] or "{}")
        signature = extra.get("cluster_signature")
        if isinstance(signature, str) and signature:
            existing_signatures.add(signature)
        members = extra.get("cluster_members")
        if isinstance(members, list):
            member_ids = {member for member in members if isinstance(member, str)}
            if member_ids:
                existing_member_sets.append(member_ids)

    for cluster in clusters:
        if summaries_created >= 5:
            break
        signature = _cluster_signature(cluster)
        member_ids = {node["id"] for node in cluster}
        if signature in existing_signatures or any(
            _clusters_overlap(member_ids, existing)
            for existing in existing_member_sets
        ):
            continue
        if dry_run:
            logger.info("Would summarise cluster: %s", [n["title"] for n in cluster])
            summaries_created += 1
            continue

        summary = _llm_summarise_cluster(cluster, timeout=timeout)
        if not summary:
            continue

        # Check for existing summary node (idempotency — CD004)
        existing = store.get_node_by_title(summary["title"])
        if existing:
            continue

        nid = store.add_node(
            title=summary["title"],
            content=summary["content"],
            node_type="concept",
            prov_activity="dream-cycle",
            prov_source=CLUSTER_SUMMARY_SOURCE,
            weight=max(n.get("weight", 0.5) for n in cluster),
            extra={
                "cluster_signature": signature,
                "cluster_members": sorted(node["id"] for node in cluster),
            },
        )
        for member in cluster:
            store.add_edge(
                nid, member["id"],
                edge_type="context_of",
                weight=0.5,
                provenance="dream-cycle cluster summary",
            )
        summaries_created += 1
        existing_signatures.add(signature)
        existing_member_sets.append(member_ids)
        if verbose:
            print(f"  Cluster summary: {summary['title']} ({len(cluster)} members)")

    results["cluster_summaries"] = summaries_created
    return results


def _find_clusters(
    store: Store, min_size: int = 4, max_hops: int = 2,
) -> list[list[dict]]:
    """Find dense clusters of related nodes sharing domains."""
    nodes = store.all_nodes(status="active", limit=2000)
    nodes = [
        node
        for node in nodes
        if node.get("type", "concept") not in PROTECTED_TYPES
        and node.get("prov_source") != CLUSTER_SUMMARY_SOURCE
    ]

    # Build adjacency from edges
    adj: dict[str, set[str]] = {}
    for n in nodes:
        nid = n["id"]
        adj.setdefault(nid, set())
        for e in store.edges_from(nid, semantic_only=True):
            adj[nid].add(e["to_id"])
            adj.setdefault(e["to_id"], set()).add(nid)

    node_map = {n["id"]: n for n in nodes}
    visited_clusters: set[frozenset[str]] = set()
    clusters: list[list[dict]] = []

    for seed in nodes:
        sid = seed["id"]
        if sid not in adj:
            continue

        # BFS to max_hops
        seen = {sid}
        frontier = {sid}
        for _hop in range(max_hops):
            next_frontier: set[str] = set()
            for nid in frontier:
                for neighbor in adj.get(nid, set()):
                    if neighbor not in seen and neighbor in node_map:
                        seen.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier

        if len(seen) < min_size:
            continue

        # Check domain overlap with seed
        seed_domains = _parse_domains(seed)
        if not seed_domains:
            continue

        # Filter to members sharing at least one domain
        cluster_ids = set()
        for nid in seen:
            n = node_map.get(nid)
            if not n:
                continue
            if _parse_domains(n) & seed_domains:
                cluster_ids.add(nid)

        if len(cluster_ids) < min_size:
            continue

        key = frozenset(cluster_ids)
        if key in visited_clusters:
            continue
        visited_clusters.add(key)

        clusters.append([node_map[nid] for nid in sorted(cluster_ids)])

    return _dedupe_overlapping_clusters(clusters)[:10]


def _dedupe_overlapping_clusters(
    clusters: list[list[dict]],
    threshold: float = DEFAULT_CLUSTER_OVERLAP_THRESHOLD,
) -> list[list[dict]]:
    """Keep deterministic clusters while suppressing subsets and high overlap."""
    ordered = sorted(
        (
            sorted(cluster, key=lambda node: node["id"])
            for cluster in clusters
        ),
        key=lambda cluster: (
            -len(cluster),
            tuple(node["id"] for node in cluster),
        ),
    )
    kept: list[list[dict]] = []
    kept_ids: list[set[str]] = []
    for cluster in ordered:
        ids = {node["id"] for node in cluster}
        if not ids:
            continue
        overlaps = False
        for existing in kept_ids:
            if _clusters_overlap(ids, existing, threshold):
                overlaps = True
                break
        if not overlaps:
            kept.append(cluster)
            kept_ids.append(ids)
    return kept


def _clusters_overlap(
    left: set[str],
    right: set[str],
    threshold: float = DEFAULT_CLUSTER_OVERLAP_THRESHOLD,
) -> bool:
    """Return whether two non-empty member sets describe the same cluster."""
    if not left or not right:
        return False
    shared = len(left & right)
    return (
        shared == min(len(left), len(right))
        or shared / len(left | right) >= threshold
    )


def _cluster_signature(cluster: list[dict]) -> str:
    """Return a stable identity for an exact set of cluster members."""
    member_ids = "\n".join(sorted(node["id"] for node in cluster))
    return hashlib.sha256(member_ids.encode("utf-8")).hexdigest()


def _parse_domains(node: dict) -> set[str]:
    """Extract domain set from a node, handling string or list."""
    domains = node.get("domains") or []
    if isinstance(domains, str):
        try:
            domains = json.loads(domains)
        except (json.JSONDecodeError, TypeError):
            domains = []
    return set(domains)


def _llm_summarise_cluster(
    cluster: list[dict], timeout: int = 300,
) -> dict | None:
    """Use claude -p to generate a summary node for a cluster."""
    titles = [redact_text(n.get("title", "")) for n in cluster]
    contents = [redact_text(n.get("content", ""))[:200] for n in cluster]

    prompt = (
        "You are summarising a cluster of related knowledge graph nodes.\n"
        "Generate a single summary concept that captures what these nodes "
        "collectively represent.\n\n"
        "Nodes:\n"
    )
    for t, c in zip(titles, contents):
        prompt += f"- {t}: {c}\n"
    prompt += (
        "\nRespond with exactly two lines:\n"
        "TITLE: <concise title for the summary concept>\n"
        "CONTENT: <1-2 sentence description>\n"
    )

    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning("claude -p failed: %s", proc.stderr[:200])
            return None

        output = redact_text(proc.stdout.strip())
        title = ""
        content = ""
        for line in output.splitlines():
            if line.startswith("TITLE:"):
                title = line[6:].strip()
            elif line.startswith("CONTENT:"):
                content = line[8:].strip()

        if not title:
            return None
        return {"title": title, "content": content}
    except subprocess.TimeoutExpired:
        logger.warning("claude -p timed out after %ds", timeout)
        return None
    except FileNotFoundError:
        logger.warning("claude CLI not found in PATH")
        return None
