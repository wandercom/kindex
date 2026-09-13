"""Resolve the two historical repo-local layouts without moving graph data."""
from pathlib import Path
import sqlite3


class ProjectStoreConflict(ValueError):
    pass


# Independent user work and its audit history can exist without any graph node.
# Rejected/expired candidates and completed reminders still retain dispositions;
# they must not become invisible merely because their active work is finished.
# FTS shadow tables, learned ranking caches and schema metadata are not evidence
# that a second store owns user work.
_DURABLE_TABLES = (
    "nodes", "edges", "capture_candidates", "reminders", "suggestions", "activity_log",
)


def database_has_durable_work(path: Path) -> bool:
    """Inspect existing work without constructing a Store or creating a DB."""
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in _DURABLE_TABLES):
                return True
            return "meta" in tables and bool(conn.execute(
                "SELECT 1 FROM meta WHERE key LIKE 'task.%' OR key LIKE 'sim.%' "
                "OR key LIKE 'supervisor.%' OR key LIKE 'attention.%' OR key LIKE 'reinforce.%' "
                "OR key = 'embed.queue' LIMIT 1").fetchone())
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ValueError(f"Cannot inspect Kindex database {path}; repair it before selecting a store") from exc


def durable_store_paths(directory: Path) -> list[Path]:
    """Return populated physical databases in an existing store directory."""
    return [path for name in ("kindex.db", "conv.db")
            if database_has_durable_work(path := directory / name)]


def project_data_path(root: Path) -> Path:
    root = root.resolve()
    local = root / ".kin" / "local"
    candidates = (local, local / "kindex")
    populated = []
    for directory in candidates:
        for parent in (root / ".kin", local, directory):
            if parent.is_symlink():
                raise ValueError("Refusing symlinked repo-local Kindex storage")
        has_data = False
        for name in ("kindex.db", "conv.db"):
            path = directory / name
            for suffix in ("", "-wal", "-shm"):
                leaf = Path(str(path) + suffix)
                if leaf.is_symlink() or (leaf.exists() and leaf.stat().st_nlink > 1):
                    raise ValueError("Refusing linked repo-local Kindex database")
            has_data |= database_has_durable_work(path)
        if has_data:
            populated.append(directory)
    if len(populated) > 1:
        raise ProjectStoreConflict(
            f"Conflicting populated Kindex stores: {local / 'kindex.db'} and {local / 'kindex' / 'kindex.db'}. "
            "Both are preserved; reconcile explicitly before continuing.")
    return populated[0] if populated else local / "kindex"


def is_project_store(store, project: str) -> bool:
    local = Path(project).resolve() / ".kin" / "local"
    return store.config.data_path.resolve() in (local, local / "kindex")
