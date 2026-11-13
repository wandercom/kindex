"""Explicit, backed-up replacement of derived vectors in one selected graph."""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from collections.abc import Callable

from . import vectors
from .privacy import safe_error, redacting_print as print

_PROBE = "Kindex embedding rebuild readiness probe."


def _stored_identity(store) -> dict:
    tables = {row[0] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    metadata = (dict(store.conn.execute("SELECT key, value FROM vec_meta"))
                if "vec_meta" in tables else {})
    return {"index_initialized": "node_vectors" in tables,
            "stored_fingerprint": metadata.get("embedding_fingerprint"),
            "stored_dimensions": metadata.get("embedding_dim")}


def _approval_state(store) -> tuple:
    # The writer lock prevents paid writers during approval. data_version and
    # total_changes also catch graph/metadata edits on other/this connection.
    return (str(store.db_path.resolve()), vectors.embedding_fingerprint(store.config),
            _stored_identity(store), store.conn.execute("PRAGMA data_version").fetchone()[0],
            store.conn.total_changes)


def _check_target(store) -> None:
    reason = vectors._embedding_policy_refusal(store.config)
    if reason:
        raise ValueError(reason)
    if getattr(store, "read_only", False):
        raise ValueError("Rebuild requires a writable selected graph")
    if store.conn.in_transaction:
        raise ValueError("Commit or roll back the current transaction before rebuilding")
    provider, model, dimensions, key_env = vectors._resolve_embedding_config(store.config)
    if provider not in vectors.PROVIDER_DEFAULTS or not model or dimensions <= 0:
        raise ValueError("Unsupported embedding provider, model, or dimensions")
    if provider != "local" and not os.environ.get(key_env, "").strip():
        raise ValueError(f"Required embedding credential is not set: {key_env}")
    if provider == "local" and vectors._get_model(model) is None:
        raise ValueError("Configured local embedding model is unavailable")
    if not vectors._check_vec():
        raise ValueError("sqlite-vec is unavailable; install kindex[vectors]")
    # Validate the target backend/dimensions without touching the retained index.
    import sqlite_vec
    with sqlite3.connect(":memory:") as connection:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute(f"CREATE VIRTUAL TABLE probe USING vec0(embedding float[{dimensions}])")
    store.conn.enable_load_extension(True)
    try:
        sqlite_vec.load(store.conn)
    finally:
        store.conn.enable_load_extension(False)


def rebuild_embeddings(store, confirm: Callable[[dict], bool], *, verbose: bool = False) -> dict:
    """Preview, confirm, preflight, snapshot, reset, then use ordinary reindexing.

    Only this entry point authorizes replacement. Indexing, search and drain
    keep their existing mismatch refusal. Approval never changes configuration.
    """
    plan = {"database": str(store.db_path.resolve()), "backup": None}
    try:
        with vectors._embedding_writer_lock(store) as acquired:
            if not acquired:
                return {**plan, "status": "refused", "message": "Another embedding writer is active"}
            _check_target(store)
            nodes = [node for node in vectors.select_reindex_nodes(store)
                     if vectors._embedding_text_for_node(node)]
            plan.update(vectors.estimate_reindex_cost(nodes, store.config))
            plan.update(_stored_identity(store))
            plan["target_fingerprint"] = plan["fingerprint"]
            probe_tokens = vectors._estimate_tokens(_PROBE)
            plan["preflight_estimated_tokens"] = probe_tokens
            plan["estimated_tokens"] += probe_tokens
            if plan["estimated_cost_usd"] is not None:
                plan["estimated_cost_usd"] += probe_tokens / 1_000_000 * plan["price_per_million_tokens"]
            approved = _approval_state(store)
            if not confirm(plan):
                return {**plan, "status": "cancelled", "message": "Rebuild was not confirmed; index preserved"}
            if approved != _approval_state(store):
                return {**plan, "status": "refused", "message": "Graph or embedding configuration changed; preview again"}
            _check_target(store)
            # This paid probe is permitted only after approval. A bad credential,
            # unsupported model or invalid vector response cannot erase the index.
            sample = vectors._embed_text_or_raise(_PROBE, store.config)
            dimensions = vectors._get_embedding_dim(store.config)
            if (sample is None or len(sample) != dimensions
                    or not all(math.isfinite(value) for value in sample)):
                raise ValueError("Target embedding readiness probe failed or returned invalid dimensions")
            if approved != _approval_state(store):
                raise ValueError("Graph or embedding configuration changed during preflight; preview again")
            _check_target(store)
            from .snapshots import _default_state_root, snapshot_connection
            # Hold the SQLite write reservation across backup/reset. Use a
            # separate read connection for backup: backing up the connection
            # owning a write transaction would wait on its own transaction.
            store.conn.execute("BEGIN IMMEDIATE")
            try:
                if approved != _approval_state(store):
                    raise ValueError("Graph or embedding configuration changed before backup; preview again")
                with sqlite3.connect(store.db_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                    backup = snapshot_connection(
                        store.db_path, source, "embedding-rebuild",
                        state_dir=_default_state_root() / "embedding-rebuilds", keep=None)
                plan["backup"] = str(backup)
                print(f"Embedding rebuild backup: {backup}", file=sys.stderr, flush=True)
                if approved != _approval_state(store):
                    raise ValueError("Graph or embedding configuration changed before reset; preview again")
                for table in ("node_vectors", "node_vector_meta", "vec_meta", "embedding_checkpoints"):
                    store.conn.execute(f"DROP TABLE IF EXISTS {table}")
                from .grounding import CALIBRATION_META_PREFIX
                provider, model, _, _ = vectors._resolve_embedding_config(store.config)
                store.conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)",
                                   (vectors.EMBED_QUEUE_META, vectors.EMBED_QUARANTINE_META,
                                    f"{CALIBRATION_META_PREFIX}{provider}:{model}"))
                if not vectors.ensure_vec_table(store):
                    raise RuntimeError("Target vector index could not be initialized")
                from .snapshots import RESTORE_HINT
                store._log_in_transaction(store.conn, "embedding_rebuild", details={
                    "backup": str(backup), "restore": RESTORE_HINT,
                    "stored_fingerprint": plan["stored_fingerprint"],
                    "target_fingerprint": plan["target_fingerprint"],
                })
                store.conn.commit()
            except BaseException:
                store.conn.rollback()
                raise
            result = vectors.reindex_now(store, verbose=verbose)
            result.update(database=plan["database"], backup=plan["backup"],
                          stored_fingerprint=plan["stored_fingerprint"],
                          target_fingerprint=plan["target_fingerprint"])
            result["resume"] = "kin embed reindex --stale"
            return result
    except Exception as error:
        return {**plan, "status": "failed", "message": safe_error(error),
                "resume": "kin embed reindex --stale" if plan["backup"] else None}
