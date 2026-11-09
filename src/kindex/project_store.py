"""Resolve the two historical repo-local layouts without moving graph data."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
from typing import Literal

from pydantic import BaseModel


class ProjectKnowledgeState(BaseModel):
    state: Literal["wip", "established"] = "wip"
    data_dir: str | None = None


def project_knowledge_path() -> Path:
    """Machine-local consent, separate from discovery and cloneable artifacts."""
    return project_graph_registry_path().with_name("project-knowledge.json")


def project_knowledge_state(root: Path) -> dict:
    """Primary checkouts retain indexing; temporary workspaces never do.

    Explicit decisions belong to an exact resolved root. Registry discovery
    and copied .kin/config files are not classification authority.
    """
    if is_transient_workspace(root):
        return ProjectKnowledgeState().model_dump()
    root = Path(root).expanduser().resolve()
    try:
        records = json.loads(project_knowledge_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        records = {}
    except (OSError, ValueError, AttributeError):
        return ProjectKnowledgeState().model_dump()
    if not isinstance(records, dict):
        return ProjectKnowledgeState().model_dump()
    if str(root) in records:
        try:
            return ProjectKnowledgeState.model_validate(records[str(root)]).model_dump()
        except ValueError:
            return ProjectKnowledgeState().model_dump()
    return ProjectKnowledgeState(
        state="established" if (root / ".git").is_dir() else "wip").model_dump()


def is_linked_worktree(root: Path) -> bool:
    """Git's workspace identity, not its directory name, determines WIP."""
    root = Path(root).expanduser().resolve()
    root = _enclosing_worktree(root) or root
    if not (root / ".git").is_file():
        return False
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute",
             "--git-dir", "--git-common-dir"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=env, timeout=3)
        directories = result.stdout.strip().splitlines()
        if result.returncode or len(directories) != 2:
            return True  # an unverifiable gitfile cannot authorize paid work
        return Path(directories[0]).resolve() != Path(directories[1]).resolve()
    except (OSError, subprocess.TimeoutExpired):
        return True


def project_is_established(root: Path, data_dir: Path | None = None) -> bool:
    """Explicit approvals bind paid work to storage; canonical defaults persist."""
    decision = project_knowledge_state(root)
    if decision["state"] != "established":
        return False
    if data_dir is not None and is_transient_workspace(data_dir):
        return False
    return (data_dir is None or decision["data_dir"] is None
            or str(Path(data_dir).expanduser().resolve()) == decision["data_dir"])


def is_transient_workspace(path: Path) -> bool:
    """Hard exclusion for Git worktrees and Factory's independent clones.

    Factory places .kindex-no-index at each run/workspace root before agents
    start. The presence marker survives archive/restore, is inherited by all
    descendants, and must be excluded from accepted snapshots.
    Neither explicit establishment nor a nested directory overrides it.
    """
    # Both names carry policy: a canonical alias into a task tree and an
    # alias out of a marked tree are excluded. Resolving first loses the
    # latter's lexical ancestors and silently bypasses its marker.
    lexical = Path(path).expanduser().absolute()
    for path in dict.fromkeys((lexical, lexical.resolve())):
        for root in (path, *path.parents):
            if (root / ".kindex-no-index").exists() or (root / ".kindex-no-index").is_symlink():
                return True
            if (root / ".git").is_file() and is_linked_worktree(root):
                return True
    return False


def established_session_cwd(cwd: str) -> bool:
    return session_cwd_status(cwd) == "established"


def session_cwd_status(cwd: str) -> str:
    """Only a known, established workspace may supply discovered sessions.

    A nested worktree/repo or .kin boundary cannot borrow ancestor approval.
    Missing or lossy encoded paths are not evidence of establishment.
    """
    if not cwd or not Path(cwd).is_absolute():
        return "unknown"
    if is_transient_workspace(Path(cwd)):
        return "transient"
    path = Path(cwd).resolve()
    try:
        decisions = json.loads(project_knowledge_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        decisions = {}
    except (OSError, ValueError):
        return "wip"
    if not isinstance(decisions, dict):
        return "wip"
    for root in (path, *path.parents):
        established = project_is_established(root)
        if str(root) in decisions or (root / ".git").exists() or (root / ".kin").exists():
            return "established" if established else "wip"
    return "unknown"


def content_indexing_refusal(config=None, *, source_paths=()) -> str | None:
    """Shared content-indexing boundary; never apply this to query embeddings.

    Config.data_path identifies the graph; Config._project_path identifies its
    selected workspace even when the graph is stored elsewhere. Source paths
    cover explicit file/repository inputs destined for another graph. Unscoped
    callers use cwd. Local capture and generated-file exports do not call this.
    """
    from .config import active_root

    paths = list(source_paths)
    if config is None:
        paths.append(Path.cwd())
    else:
        # data_path resolves aliases; retain the operator-supplied storage
        # name as well so a marked lexical ancestor cannot disappear.
        paths.append(config.data_path)
        if active_root() is None:
            paths.append(getattr(config, "data_dir", None))
        paths.extend((getattr(config, "_project_path", None),
                      getattr(config, "_project_input_path", None)))
    if any(is_transient_workspace(Path(path)) for path in paths if path):
        return ("Content indexing is disabled for transient workspaces; merge accepted "
                "artifacts into the canonical checkout before indexing them")
    return None


def set_project_knowledge_state(
        root: Path, state: str, data_dir: Path | None = None) -> dict:
    """Explicitly approve or pause one workspace without modifying its graph.

    The storage binding prevents discovery of a different data_dir from
    inheriting paid-maintenance permission. Existing nodes and queues survive
    either transition. Establishing is not an ingest or embedding operation.
    """
    from .budget import _lock_file, _unlock_file

    if state == "established" and is_transient_workspace(root):
        raise ValueError("Transient workspaces remain WIP; index the canonical checkout after merging")
    root = Path(root).expanduser().resolve()
    if state == "established" and not root.is_dir():
        raise ValueError("Project path must be an existing directory")
    decision = ProjectKnowledgeState(state=state)
    if state == "wip":
        directory = None  # revocation must work even if storage is conflicted
    elif data_dir is not None:
        directory = Path(data_dir).expanduser()
        directory = root / directory if not directory.is_absolute() else directory
        if is_transient_workspace(directory):
            raise ValueError("Transient workspace storage cannot be established")
        directory = directory.resolve()
    else:
        directory = project_data_path(root)
    if directory is not None:
        if is_transient_workspace(directory):
            raise ValueError("Transient workspace storage cannot be established")
        refuse_tracked_store(directory)
        decision.data_dir = str(directory)
    path = project_knowledge_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock_file(fd)
        # Unlike best-effort discovery, an explicit change must surface errors
        # and must not overwrite an unreadable consent file.
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(records, dict):
            raise ValueError("Invalid project knowledge registry; preserve and repair it first")
        if state == "wip":
            previous = records.get(str(root))
            if isinstance(previous, dict) and isinstance(previous.get("data_dir"), str):
                decision.data_dir = previous["data_dir"]
        records[str(root)] = decision.model_dump()
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False,
                prefix=".project-knowledge.") as out:
            json.dump(records, out, sort_keys=True)
        os.replace(out.name, path)
    finally:
        _unlock_file(fd)
        os.close(fd)
    if state == "established":
        register_project_graph(root, directory)
    return decision.model_dump()


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
                "OR key = 'embed.queue' OR key LIKE 'ingest.session_provenance.%' LIMIT 1").fetchone())
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


def _printable(value: object) -> str:
    return "".join(char if char.isprintable() else "?" for char in str(value))


def untrack_command(worktree: Path, relative: str) -> str:
    """The shell command that stops a worktree tracking a directory while
    keeping its files. Both paths come from the repository, so both are
    quoted, and the pathspec is literal."""
    return shlex.join(["git", "--literal-pathspecs", "-C", str(worktree),
                       "rm", "-r", "--cached", "--", relative])


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
        return (f"Refusing Kindex storage at {_printable(directory)}: git could not confirm "
                f"that {_printable(worktree)} does not track it ({type(error).__name__})")
    if listed.returncode != 0:
        reason = listed.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        return (f"Refusing Kindex storage at {_printable(directory)}: git could not confirm "
                f"that {_printable(worktree)} does not track it "
                f"({_printable(reason[0]) if reason else listed.returncode})")
    if listed.stdout.strip(b"\0"):
        return (f"Refusing tracked Kindex storage at {_printable(directory)}: "
                f"{_printable(worktree)} tracks it, and files a clone delivers are not a "
                "local trusted database. If this store is your own, stop tracking it "
                "(the files stay on disk):\n  "
                + _printable(untrack_command(worktree, relative)))
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
    if kin.is_file():
        # A legacy .kin file is read in place, but a store cannot be created
        # beneath it: opening the store is the write that upgrades it to
        # .kin/config.
        from .config import _maybe_upgrade_kin_file
        _maybe_upgrade_kin_file(kin)
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


def project_graph_registry_path() -> Path:
    """The machine's record of repo-local graphs opened outside any profile,
    beside the scheduler state."""
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    state = Path(base) if base else Path.home() / ".local" / "state"
    return state / "kindex" / "project-graphs.json"


def existing_local_store(root: Path) -> Path | None:
    """The repo-local graph directory under ``root``, chosen as the store
    itself chooses it (the populated layout wins over an empty database), or
    None when there is no database or the layouts conflict."""
    local = root / ".kin" / "local"
    if not any((directory / name).is_file()
               for directory in (local, local / "kindex")
               for name in ("kindex.db", "conv.db")):
        return None
    try:
        return project_data_path(root)
    except ValueError:  # a conflict or a refused layout is not maintained blind
        return None


def register_project_graph(root: Path, data_dir: Path) -> None:
    """Record a repo-local graph so scheduled maintenance can find it.
    Best effort: an unwritable state directory never blocks the store."""
    from .budget import _lock_file, _unlock_file
    from .scheduling import scheduler_writes_disabled

    # Whoever keeps kindex away from the machine's scheduler (a test run)
    # keeps it away from the scheduler's registry too.
    if scheduler_writes_disabled() or is_transient_workspace(root) or is_transient_workspace(data_dir):
        return
    path = project_graph_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The budget ledger's portable lock: flock, or msvcrt on Windows.
        fd = os.open(str(path.with_name(path.name + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_file(fd)
            try:
                _merge_project_graph(path, root, data_dir)
            finally:
                _unlock_file(fd)
        finally:
            os.close(fd)
    except OSError:
        return


def _merge_project_graph(path: Path, root: Path, data_dir: Path) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    if current.get(str(root)) == str(data_dir):
        return
    current[str(root)] = str(data_dir)
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False,
            prefix=".project-graphs.") as out:
        json.dump(current, out, sort_keys=True)
    os.replace(out.name, path)


def registered_project_graphs() -> dict[str, str]:
    try:
        current = json.loads(project_graph_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in current.items()} if isinstance(current, dict) else {}


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
