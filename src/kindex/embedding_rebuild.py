"""Explicit, backed-up replacement of derived vectors in one selected graph."""
from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing

from . import vectors
from .privacy import safe_error, redacting_print as print

_CHANGED = ("Graph or embedding configuration changed; pause other Kindex clients/daemons "
            "that write to this graph and preview again")


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


def rebuild_embeddings(store, confirm: Callable[[dict], bool], *, verbose: bool = False) -> dict:
    """Preview, confirm, preflight, snapshot, reset, then drain tracked work.

    Only this entry point authorizes replacement. Indexing, search and drain
    keep their existing mismatch refusal. Approval never changes configuration.
    """
    plan = {"database": str(store.db_path.resolve()), "backup": None}
    reset_committed = False
    try:
        if not store.db_path.is_file():
            return {**plan, "status": "refused", "message":
                    "Rebuild requires an existing database; check the selected graph path"}
        with vectors.embedding_writer_lock(store) as acquired:
            if not acquired:
                return {**plan, "status": "refused", "message": "Another embedding writer is active"}
            vectors.validate_embedding_rebuild_target(store)
            nodes, estimate = vectors.plan_embedding_rebuild(store)
            plan.update(estimate)
            plan.update(_stored_identity(store))
            plan["target_fingerprint"] = plan["fingerprint"]
            approved = _approval_state(store)
            if not confirm(plan):
                return {**plan, "status": "cancelled", "message": "Rebuild was not confirmed; index preserved"}
            if approved != _approval_state(store):
                return {**plan, "status": "refused", "message": _CHANGED}
            vectors.validate_embedding_rebuild_target(store)
            # This paid probe is permitted only after approval. A bad credential,
            # unsupported model or invalid vector response cannot erase the index.
            vectors.probe_embedding_rebuild_target(store)
            if approved != _approval_state(store):
                raise ValueError(_CHANGED)
            vectors.validate_embedding_rebuild_target(store)
            from .snapshots import default_state_root, snapshot_connection
            # Hold the SQLite write reservation across backup/reset. Use a
            # separate read connection for backup: backing up the connection
            # owning a write transaction would wait on its own transaction.
            store.conn.execute("BEGIN IMMEDIATE")
            try:
                if approved != _approval_state(store):
                    raise ValueError(_CHANGED)
                with closing(sqlite3.connect(store.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    backup = snapshot_connection(
                        store.db_path, source, "embedding-rebuild",
                        state_dir=default_state_root() / "embedding-rebuilds", keep=None)
                plan["backup"] = str(backup)
                print(f"Embedding rebuild backup: {backup}", file=sys.stderr, flush=True)
                if approved != _approval_state(store):
                    raise ValueError(_CHANGED)
                for table in ("node_vectors", "node_vector_meta", "vec_meta", "embedding_checkpoints"):
                    store.conn.execute(f"DROP TABLE IF EXISTS {table}")
                from .grounding import CALIBRATION_META_PREFIX
                store.conn.execute("DELETE FROM meta WHERE key = ?",
                                   (f"{CALIBRATION_META_PREFIX}{plan['provider']}:{plan['model']}",))
                # Quarantine is scoped to text + fingerprint. Keep true claims;
                # the drain ignores obsolete claims and tracks new rejections.
                # Stage every selected node atomically with reset, without the
                # ordinary bounded enqueue cap silently dropping rebuild work.
                vectors.stage_embedding_rebuild_queue(store, [node["id"] for node in nodes])
                if not vectors.ensure_vec_table(store):
                    raise RuntimeError("Target vector index could not be initialized")
                from .snapshots import RESTORE_HINT
                store._log_in_transaction(store.conn, "embedding_rebuild", details={
                    "backup": str(backup), "restore": RESTORE_HINT,
                    "stored_fingerprint": plan["stored_fingerprint"],
                    "target_fingerprint": plan["target_fingerprint"],
                })
                store.conn.commit()
                reset_committed = True
            except BaseException:
                store.conn.rollback()
                raise
            result = {**plan, **vectors.drain_embedding_queue(
                store, max_jobs=len(nodes), time_budget=float("inf"))}
            if result["status"] == "empty":
                result["status"] = "ok"
            if result["status"] == "deferred" and result.get("pending"):
                result["status"] = "failed"
            if result.get("quarantined") and result.get("coverage_complete") is False:
                result["status"] = "failed"
                result["message"] = ("Quarantined inputs remain; inspect kin embed status and "
                                     "resolve rejected input before retrying. Reindex --stale "
                                     "does not bypass deterministic provider limits.")
            if verbose:
                print(f"Embedded: {result.get('embedded', 0)} nodes", file=sys.stderr)
            result.update(database=plan["database"], backup=plan["backup"],
                          queue_pending=result.get("pending"),
                          stored_fingerprint=plan["stored_fingerprint"],
                          target_fingerprint=plan["target_fingerprint"])
            unknown_pending_failure = (result.get("queue_pending") is None
                                       and result["status"] != "ok")
            if result.get("queue_pending") or unknown_pending_failure:
                result["resume"] = "kin embed drain"
            if unknown_pending_failure:
                result["message"] = ("Queue state could not be verified; inspect kin embed status "
                                     "before resuming queued work.")
            return result
    except Exception as error:
        return {**plan, "status": "failed", "message": safe_error(error),
                "resume": "kin embed drain" if reset_committed and plan.get("nodes") else None}
