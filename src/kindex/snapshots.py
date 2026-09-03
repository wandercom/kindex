"""SQLite recovery snapshots for automated merges and schema migrations.

PRD lineage-grounding (docs/prd-lineage-grounding-2026-08.md) review outcome
point 4: ``graph_merge`` and dream-cycle auto-merges mutate the SQLite store,
which is NOT in git — a false entity merge there contaminates every downstream
traversal and is otherwise unrecoverable without lossy re-ingestion. Until R3
merge receipts land, every automated destructive merge snapshots the DB first
so a false merge is recoverable by copying the snapshot back over the live DB.

Invariant upheld: fail-closed. ``snapshot_db`` never swallows a failure —
callers must treat "no snapshot" as "no merge". A merge that proceeds
unprotected would silently reopen the exact corruption path this module
exists to close.

Snapshots live under ``$XDG_STATE_HOME/kindex/snapshots/`` (default
``~/.local/state/kindex/snapshots/``), one subdirectory per database file so
rotation in one profile never evicts another profile's snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

# Keep roughly ten restore points per database (reviewed default).
DEFAULT_KEEP = 10

_REASON_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")

RESTORE_HINT = (
    "to restore: stop every kindex daemon/MCP/CLI process, move the live "
    "DB's -wal and -shm sidecars aside, copy the snapshot over the live DB "
    "path, and reopen"
)


def _default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "kindex" / "snapshots"


def snapshot_dir_for(db_path: Path, state_dir: Path | str | None = None) -> Path:
    """Per-database snapshot directory (name + 8-hex path digest)."""
    root = Path(state_dir) if state_dir is not None else _default_state_root()
    ident = hashlib.sha256(str(db_path).encode("utf-8")).hexdigest()[:8]
    return root / f"{db_path.stem}-{ident}"


def _rotate(target_dir: Path, keep: int) -> None:
    """Delete all but the ``keep`` newest snapshots (timestamp-prefixed names
    sort lexically = chronologically)."""
    snapshots = sorted(target_dir.glob("*.sqlite3"))
    excess = snapshots if keep <= 0 else snapshots[:-keep]
    for old in excess:
        old.unlink(missing_ok=True)


def snapshot_db(
    store: "Store",
    reason: str,
    *,
    state_dir: Path | str | None = None,
    keep: int = DEFAULT_KEEP,
) -> Path:
    """Snapshot the live DB via the SQLite backup API; return the new path.

    The backup API is transaction-safe against writes in flight. Rotation
    keeps the ``keep`` newest snapshots for this database. A ``db_snapshot``
    activity-log entry (visible in ``kin changelog``) records the path,
    reason, and restore hint.

    Raises ``OSError`` / ``sqlite3.Error`` on failure — deliberately not
    swallowed, so callers fail closed (refuse the merge) instead of merging
    unprotected.
    """
    db_path = Path(store.db_path)
    target = snapshot_connection(
        db_path,
        store.conn,
        reason,
        state_dir=state_dir,
        keep=keep,
    )
    store._log(
        "db_snapshot", "", "", "",
        details={"path": str(target), "reason": reason, "restore": RESTORE_HINT},
    )
    return target


def snapshot_connection(
    db_path: Path | str,
    connection: sqlite3.Connection,
    reason: str,
    *,
    state_dir: Path | str | None = None,
    keep: int = DEFAULT_KEEP,
) -> Path:
    """Snapshot an open SQLite connection without requiring a current schema.

    Schema migration uses this lower-level form before it can assume that the
    activity log or any other current table exists. Callers that own a full
    ``Store`` should use ``snapshot_db`` so the snapshot is logged immediately.
    """
    db_path = Path(db_path)
    target_dir = snapshot_dir_for(db_path, state_dir)
    return _snapshot_connection_to_dir(connection, target_dir, reason, keep)


def snapshot_schema_migration(
    db_path: Path | str,
    connection: sqlite3.Connection,
    current_version: int,
    target_version: int,
    *,
    state_dir: Path | str | None = None,
) -> tuple[Path, str]:
    """Create a pinned recovery snapshot before a schema migration.

    Migration snapshots live outside the rotating automated-merge pool because
    they are the supported downgrade recovery point for that schema transition.
    """
    db_path = Path(db_path)
    reason = f"schema-v{current_version}-to-v{target_version}"
    target_dir = snapshot_dir_for(db_path, state_dir) / "migrations"
    return (
        _snapshot_connection_to_dir(
            connection,
            target_dir,
            reason,
            keep=None,
            expected_schema_version=current_version,
        ),
        reason,
    )


def _snapshot_connection_to_dir(
    connection: sqlite3.Connection,
    target_dir: Path,
    reason: str,
    keep: int | None,
    expected_schema_version: int | None = None,
) -> Path:
    """Write and validate one private SQLite backup in an explicit directory."""
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    slug = _REASON_SLUG_RE.sub("-", reason).strip("-") or "merge"
    target = target_dir / f"{stamp}-{slug}.sqlite3"
    # sqlite3.connect creates with the process umask. Reserve the path first so
    # a permissive umask never leaves even a brief world-readable corpus copy.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    target.chmod(0o600)
    dest: sqlite3.Connection | None = None
    try:
        dest = sqlite3.connect(str(target))
        connection.backup(dest)
        _validate_snapshot(dest, expected_schema_version)
    except BaseException:
        if dest is not None:
            dest.close()
        target.unlink(missing_ok=True)
        Path(f"{target}-wal").unlink(missing_ok=True)
        Path(f"{target}-shm").unlink(missing_ok=True)
        raise
    else:
        dest.close()
    if keep is not None:
        _rotate(target_dir, keep)
    return target


def _validate_snapshot(
    connection: sqlite3.Connection,
    expected_schema_version: int | None = None,
) -> None:
    """Refuse a backup that SQLite cannot read back as internally sound."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        detail = "; ".join(str(row[0]) for row in rows[:5]) or "no result"
        raise sqlite3.DatabaseError(
            f"snapshot integrity check failed: {detail}"
        )
    if expected_schema_version is None:
        return
    has_meta = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        if expected_schema_version == 1:
            return  # pre-versioning stores are treated as schema v1
        raise sqlite3.DatabaseError("snapshot schema_version table is missing")
    row = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None or int(row[0]) != expected_schema_version:
        actual = row[0] if row is not None else "missing"
        raise sqlite3.DatabaseError(
            "snapshot schema version mismatch: "
            f"expected {expected_schema_version}, found {actual}"
        )
