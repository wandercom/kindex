"""Pre-merge SQLite snapshots — the reviewed stopgap for automated merges.

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
    "to restore: stop any kindex daemon/MCP server, copy the snapshot file "
    "over the live DB path, and reopen"
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
    target_dir = snapshot_dir_for(db_path, state_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    slug = _REASON_SLUG_RE.sub("-", reason).strip("-") or "merge"
    target = target_dir / f"{stamp}-{slug}.sqlite3"
    dest = sqlite3.connect(str(target))
    try:
        store.conn.backup(dest)
    finally:
        dest.close()
    _rotate(target_dir, keep)
    store._log(
        "db_snapshot", "", "", "",
        details={"path": str(target), "reason": reason, "restore": RESTORE_HINT},
    )
    return target
