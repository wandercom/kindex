"""Local, read-only embedding coverage diagnostics, separate from queue work.

Fresh means current redacted title/content, current fingerprint, a complete
contiguous chunk set, and an actual vector row for every chunk. Node updated_at
is intentionally irrelevant: metadata-only edits do not invalidate embeddings.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from .privacy import safe_error


def embedding_coverage(store) -> dict:
    """Assess all eligible nodes once; never initialize or repair an index.

    Counts partition eligible nodes into fresh, missing metadata, stale/incomplete,
    and unassessed. Null counts/percent mean assessment was unavailable, not zero.
    The retained-vector timestamp is not an attempt log or a progress guarantee.
    """
    from .vectors import (
        _embedding_rows_fresh, _embedding_text_for_node, _get_embedding_dim,
        _hash_text, embedding_fingerprint, select_reindex_nodes,
    )

    current = embedding_fingerprint(store.config)
    result = {
        "coverage_state": "unavailable",
        "eligible_nodes": None,
        "fresh_nodes": None,
        "missing_nodes": None,
        "stale_or_incomplete_nodes": None,
        "unassessed_nodes": None,
        "coverage_percent": None,
        "coverage_error": None,
        "stored_fingerprint": None,
        "stored_dimensions": None,
        "fingerprint_mismatch": None,
        "latest_retained_embedding_at": None,
        "retained_embedding_timestamp_state": "unavailable",
        "retained_embedding_timestamp_scope": "retained vector rows, not attempt history",
    }
    try:
        # Same eligibility and redaction as indexing, without its optional stale
        # selector (which would repeat one metadata/vector query per node).
        eligible = {}
        for node in select_reindex_nodes(store):
            text = _embedding_text_for_node(node)
            if text:
                eligible[node["id"]] = _hash_text(text)
        result["eligible_nodes"] = len(eligible)
        result["unassessed_nodes"] = len(eligible)
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('node_vectors', 'node_vector_meta', 'vec_meta')")}
        if "vec_meta" in tables:
            identity = dict(store.conn.execute(
                "SELECT key, value FROM vec_meta "
                "WHERE key IN ('embedding_fingerprint', 'embedding_dim')"))
            result["stored_fingerprint"] = identity.get("embedding_fingerprint")
            result["stored_dimensions"] = identity.get("embedding_dim")
        result["fingerprint_mismatch"] = bool("node_vectors" in tables and (
            result["stored_fingerprint"] != current
            or result["stored_dimensions"] != str(_get_embedding_dim(store.config))))

        # Read metadata without loading a model or touching the provider. Even
        # with an unavailable backend, absence of metadata is a known gap.
        metadata = []
        if "node_vector_meta" in tables:
            metadata = store.conn.execute(
                "SELECT node_id, vector_id, text_hash, fingerprint, chunk_index, "
                "chunk_count, updated_at FROM node_vector_meta").fetchall()
        indexed_ids = {row["node_id"] for row in metadata}
        result["missing_nodes"] = len(eligible.keys() - indexed_ids)
        result["unassessed_nodes"] = len(eligible) - result["missing_nodes"]
        missing_tables = {"node_vector_meta", "node_vectors"} - tables
        if missing_tables:
            if not eligible and not metadata and "node_vectors" not in tables:
                result.update(coverage_state="empty", fresh_nodes=0,
                              stale_or_incomplete_nodes=0, unassessed_nodes=0,
                              retained_embedding_timestamp_state="none")
                return result
            result["coverage_error"] = "Missing embedding index table(s): " + ", ".join(sorted(missing_tables))
            return result

        # Load the extension once for reading an existing virtual table. Never
        # call ensure_vec_table: diagnosis must not create/reset vector state.
        try:
            store.conn.execute("SELECT vec_version()").fetchone()
        except sqlite3.OperationalError:
            import sqlite_vec
            store.conn.enable_load_extension(True)
            try:
                sqlite_vec.load(store.conn)
            finally:
                store.conn.enable_load_extension(False)
        vector_ids = {row[0] for row in store.conn.execute("SELECT node_id FROM node_vectors")}
        by_node = {}
        latest = None
        # Retained vectors without metadata have an unknown update time; they
        # must not be presented as if no vector had ever been retained.
        unknown_timestamp = bool(vector_ids - {row["vector_id"] for row in metadata})
        for row in metadata:
            retained = row["vector_id"] in vector_ids
            by_node.setdefault(row["node_id"], []).append(dict(row) | {
                "stored_vector": row["vector_id"] if retained else None,
            })
            if retained:
                try:
                    timestamp = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                    # Legacy SQLite timestamps use UTC without an explicit offset.
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    timestamp = timestamp.astimezone(timezone.utc)
                    latest = timestamp if latest is None else max(latest, timestamp)
                except (AttributeError, TypeError, ValueError):
                    unknown_timestamp = True
        result["retained_embedding_timestamp_state"] = (
            "unknown" if unknown_timestamp else "known" if latest else "none")
        if latest is not None and not unknown_timestamp:
            result["latest_retained_embedding_at"] = latest.isoformat()
        fresh = sum(_embedding_rows_fresh(by_node.get(node_id, []), text_hash, current)
                    for node_id, text_hash in eligible.items())
        stale = len(eligible) - result["missing_nodes"] - fresh
        result.update(
            coverage_state="empty" if not eligible else "complete" if fresh == len(eligible) else "incomplete",
            fresh_nodes=fresh, stale_or_incomplete_nodes=stale, unassessed_nodes=0,
            coverage_percent=round(100 * fresh / len(eligible), 2) if eligible else None,
        )
    except Exception as exc:
        result["coverage_error"] = f"Embedding coverage could not be assessed: {safe_error(exc)}"
    return result


def embedding_doctor_findings(status: dict) -> tuple[list[str], list[str]]:
    """Return actionable issues and informational warnings without remediation."""
    issues = []
    warnings = []
    if status["fingerprint_mismatch"]:
        issues.append(
            "Embedding fingerprint mismatch: the stored index does not match the current "
            "provider/model/dimensions/strategy. Restore the matching embedding configuration. "
            "Keep the existing index and pending work; migration requires a separately reviewed procedure.")
    if status["coverage_state"] == "unavailable":
        issues.append(f"Embedding coverage unavailable: {status['coverage_error']}. "
                      "Inspect the local embedding backend/schema before reindexing.")
    elif status["coverage_state"] == "incomplete":
        issues.append(
            f"Embedding coverage: {status['fresh_nodes']}/{status['eligible_nodes']} current "
            f"({status['coverage_percent']:.2f}%); {status['missing_nodes']} missing, "
            f"{status['stale_or_incomplete_nodes']} stale/incomplete. "
            "After resolving configuration/provider blockers, review `kin embed plan --stale` "
            "and explicitly enqueue/drain the required work.")
    if status["quarantined"]:
        issues.append(f"{status['quarantined']} quarantined embedding node(s), even if the queue is empty. "
                      "Inspect `kin embed status` quarantine reasons and resolve them before retrying.")
    if status["queue_pending"]:
        warnings.append(f"{status['queue_pending']} embedding node(s) pending; queue emptiness alone "
                        "does not establish coverage. Inspect `kin embed status` before draining.")
    if status["retained_embedding_timestamp_state"] == "unknown":
        warnings.append("Latest retained embedding timestamp is unknown: missing or invalid timestamp metadata. "
                        "Content freshness is assessed independently.")
    return issues, warnings


def embedding_health_lines(status: dict) -> list[str]:
    """Human-readable summary, including healthy and unavailable states."""
    state = status["coverage_state"]
    if state == "empty":
        coverage = "0/0 current (n/a: no eligible nonempty nodes)"
    elif state == "unavailable":
        coverage = (f"unavailable (eligible={status['eligible_nodes']}, "
                    f"missing={status['missing_nodes']}, unassessed={status['unassessed_nodes']})")
    else:
        coverage = (f"{status['fresh_nodes']}/{status['eligible_nodes']} current "
                    f"({status['coverage_percent']:.2f}%), {status['missing_nodes']} missing, "
                    f"{status['stale_or_incomplete_nodes']} stale/incomplete")
    timestamp_state = status["retained_embedding_timestamp_state"]
    latest = status["latest_retained_embedding_at"] or {
        "none": "none retained", "unknown": "unknown (missing or invalid metadata)",
        "unavailable": "unavailable",
    }.get(timestamp_state, "unknown")
    return [
        f"Embedding coverage: {coverage}",
        f"Embedding work: {status['queue_pending']} queued, {status['quarantined']} quarantined",
        f"Embedding fingerprint (current): {status['fingerprint']}",
        f"Embedding fingerprint (stored): {status['stored_fingerprint'] or 'not recorded'}",
        f"Latest retained embedding update: {latest} (not attempt history)",
        "Freshness uses current text and configuration, not time since the last embedding.",
    ]
