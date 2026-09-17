"""Resolve the two historical repo-local layouts without moving graph data."""
import os
from pathlib import Path
import sqlite3
import subprocess


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


# Variables that point git at some other repository, index or pathspec
# grammar than the directory being asked about (a hook environment sets them).
_GIT_REDIRECT_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
})


def _enclosing_worktree(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def tracked_store_refusal(directory: Path) -> str | None:
    """Why a store directory must not be opened, when a git clone delivered it.

    Files a repository tracks arrive with every clone, so a tracked store is
    somebody else's content, not the user's own database: its reminders can
    carry shell actions and its rows are injected into the agent's context.
    The modern lane already refuses a tracked .kin/local; this is the same
    rule for every store a repository can name. A directory that does not
    exist, or lies in no worktree, was not delivered by a clone. Inside a
    worktree, a store git cannot vouch for (git missing, timed out, or
    refusing the repository) is refused: the answer must come from git.
    """
    directory = Path(directory).expanduser()
    if not directory.exists():
        return None
    real = directory.resolve()
    worktree = _enclosing_worktree(real)
    if worktree is None:
        return None
    relative = real.relative_to(worktree).as_posix()
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_ENV}
    try:
        # Never let a child inherit an MCP server's JSON-RPC stdin; literal
        # pathspecs so a directory name is never read as a glob or magic.
        listed = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(worktree), "ls-files", "-z", "--", relative],
            capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        return (f"Refusing Kindex storage at {directory}: git could not confirm that "
                f"{worktree} does not track it ({type(error).__name__})")
    if listed.returncode != 0:
        reason = listed.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        return (f"Refusing Kindex storage at {directory}: git could not confirm that "
                f"{worktree} does not track it ({reason[0] if reason else listed.returncode})")
    if listed.stdout.strip(b"\0"):
        return (f"Refusing tracked Kindex storage at {directory}: {worktree} tracks it, and "
                "files a clone delivers are not a local trusted database. If this store is "
                f"your own, stop tracking it (git -C {worktree} rm -r --cached -- {relative}); "
                "the files stay on disk")
    return None


def refuse_tracked_store(directory: Path) -> None:
    reason = tracked_store_refusal(directory)
    if reason:
        raise ValueError(reason)


def ensure_local_ignored(data_path: Path) -> None:
    """Keep a repository's .kin/local out of version control.

    Applies only to the two canonical layouts (<repo>/.kin/local and
    <repo>/.kin/local/kindex); a custom data_dir is the user's own choice.
    Idempotent and append-only. A symlinked .kin is left alone (git does not
    look inside it, and a write would land wherever it points); a symlinked
    .gitignore is refused.
    """
    data_path = Path(data_path)
    if data_path.name == "local" and data_path.parent.name == ".kin":
        kin = data_path.parent
    elif (data_path.name == "kindex" and data_path.parent.name == "local"
          and data_path.parent.parent.name == ".kin"):
        kin = data_path.parent.parent
    else:
        return
    if kin.is_symlink():
        return
    ignore = kin / ".gitignore"
    if ignore.is_symlink():
        raise ValueError("Refusing symlinked .kin/.gitignore")
    kin.mkdir(parents=True, exist_ok=True)
    existing = ignore.read_bytes() if ignore.exists() else b""
    if b"local/" in (line.rstrip(b"\r") for line in existing.splitlines()):
        return
    fd = os.open(str(ignore), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, (b"\n" if existing and not existing.endswith(b"\n") else b"") + b"local/\n")
    finally:
        os.close(fd)


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
    refuse_tracked_store(local)
    return populated[0] if populated else local / "kindex"


def is_project_store(store, project: str) -> bool:
    local = Path(project).resolve() / ".kin" / "local"
    return store.config.data_path.resolve() in (local, local / "kindex")
