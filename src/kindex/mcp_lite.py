"""An explicit, fixed repository capability surface for MCP.

No ambient config is loaded. Each call opens only the pinned repository's local
graph and supplies dependencies through a ContextVar, so multiple servers may
coexist with the unrestricted server in one process. This is an application
boundary, not an OS sandbox for the host or the Kinbase executable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import functools
import inspect
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import platform_io
from .config import Config, activate_secret_mask, user_secret_mask
from . import mcp_server
from .privacy import redact, redact_serialized, safe_error
from .project_store import project_data_path
from .store import Store


# Opt-in, not all tools minus a denylist. New full-server tools do not silently
# become repository capabilities. In particular task_execute resolves its own
# scope, and ingestion/reminders/modes/maintenance can traverse or execute.
LOCAL_TOOLS = (
    "search", "add", "edit", "supersede", "show", "context", "list_nodes",
    "status", "learn", "link", "verify", "invalidate", "changelog",
    "tag_start", "tag_update", "tag_resume",
    "task_add", "task_list", "task_get", "task_update", "task_cancel",
    "task_done", "task_claim", "task_release",
    "coord_start", "coord_join", "coord_post", "coord_read", "coord_attach",
    "coord_inject", "coord_list", "coord_end", "lock_acquire", "lock_release",
)
KINBASE_TOOLS = ("kinbase_sync", "kinbase_status", "kinbase_explain", "kinbase_submit")
KINBASE_READ_TOOLS = ("kinbase_sync", "kinbase_explain")


class ScopeRefused(ValueError):
    """A requested path or capability is outside this server's fixed scope."""


class _RepoConfig(Config):
    @property
    def data_path(self) -> Path:
        # Do not inherit even config.bind_root's process-global path rewrite.
        return Path(self.data_dir)


@dataclass(frozen=True)
class _Dependencies:
    repo: Path
    store: Store
    config: Config


def _refuse_links(path: Path) -> None:
    """Refuse linked storage before a reader follows it, including sidecars."""
    if path.is_symlink():
        raise ScopeRefused(f"Refusing symlink in repository scope: {path}")
    if path.exists() and not path.is_dir() and path.stat().st_nlink > 1:
        raise ScopeRefused(f"Refusing hardlinked file in repository scope: {path}")


def _check_tree(path: Path) -> None:
    _refuse_links(path)
    if not path.exists():
        return

    def failed(error):
        raise ScopeRefused("Cannot inspect repository storage") from error

    for directory, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        for name in (*dirs, *files):
            _refuse_links(Path(directory) / name)


class _RepoScope:
    def __init__(self, repo: str | Path):
        self.requested = platform_io.expanduser(Path(repo)).absolute()
        self.repo = self.requested.resolve(strict=True)
        if not self.repo.is_dir():
            raise ScopeRefused("--repo must name an existing repository directory")
        stat = self.repo.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.graph_id = secrets.token_hex(12)
        self._check_storage()
        self.data_path = project_data_path(self.repo)

    def _check_storage(self) -> None:
        if self.requested.resolve(strict=True) != self.repo:
            raise ScopeRefused("The bound repository path has changed")
        stat = self.repo.stat()
        if (stat.st_dev, stat.st_ino) != self.identity:
            raise ScopeRefused("The bound repository directory has changed")
        kin = self.repo / ".kin"
        _refuse_links(kin)
        if kin.exists() and not kin.is_dir():
            raise ScopeRefused("kindex-lite requires .kin to be a directory")
        _refuse_links(kin / ".gitignore")
        _check_tree(kin / "local")

    def validate(self, *, kinbase: bool = False) -> None:
        self._check_storage()
        if project_data_path(self.repo) != self.data_path:
            raise ScopeRefused("The repository's selected graph has changed; restart kindex-lite")
        if kinbase:
            # Kinbase owns its own config and signed-event interpretation, but
            # it must not receive a .kin tree redirected through filesystem links.
            _check_tree(self.repo / ".kin")

    def path(self, value: str, *, root_only: bool = False) -> str:
        candidate = platform_io.expanduser(Path(value))
        resolved = (candidate if candidate.is_absolute() else self.repo / candidate).resolve()
        if not resolved.is_relative_to(self.repo) or (root_only and resolved != self.repo):
            raise ScopeRefused("Path is outside the bound repository scope")
        # A hardlinked referent can also disclose bytes belonging to another
        # directory. Ordinary symlinks wholly within the repository are okay.
        _refuse_links(resolved)
        return str(resolved)

    def config(self) -> Config:
        config = _RepoConfig(
            data_dir=str(self.data_path), project_dirs=[str(self.repo)],
            user="kindex-lite", agent_id="kindex-lite",
            llm={"enabled": False, "api_key_env": ""},
        )
        config._project_path = self.repo
        config.attention.reinforce_enabled = False
        # These paths are not used by the allowlist. Keep even accidental
        # future consumers away from ambient host history directories.
        for field in ("claude_dir", "codex_dir", "gemini_dir", "antigravity_dir",
                      "antigravity_cli_dir", "opencode_dir", "cursor_dir"):
            setattr(config, field, str(self.data_path / "disabled-host-history"))
        return config

    def wrap(self, handler):
        signature = inspect.signature(handler)

        @functools.wraps(handler)
        def scoped(*args, **kwargs):
            store = None
            token = None
            try:
                self.validate(kinbase=handler.__name__ in KINBASE_TOOLS)
                arguments = signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                values = arguments.arguments
                if values.get("graph") not in (None, "", "auto", "project"):
                    raise ScopeRefused("Only the bound project graph is available")
                for parameter in ("repo", "project_path", "base_dir"):
                    if parameter in values:
                        values[parameter] = self.path(values[parameter] or str(self.repo), root_only=True)
                referent = values.get("referent")
                if referent:
                    # Match add's effective scope, not the string's prefix:
                    # an explicit file scope hashes even an HTTP-looking path.
                    referent_scope = values.get("referent_scope") or (
                        "url" if "://" in referent else "file")
                    if referent_scope != "url":
                        values["referent"] = self.path(referent)
                config = self.config()
                # Schema migrations make machine-level recovery snapshots.
                # Fresh/current databases work; an old schema fails closed.
                store = Store(config, migrate=False)
                store._mcp_graph_scope = self.graph_id
                token = mcp_server._request_scope.set(_Dependencies(self.repo, store, config))
                result = handler(*arguments.args, **arguments.kwargs)
                return redact_serialized(result) if isinstance(result, str) else redact(result)
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
                return f"Error: repository scope unavailable: {safe_error(error)}"
            finally:
                if token is not None:
                    mcp_server._request_scope.reset(token)
                if store is not None:
                    store.close()

        return scoped


def create_server(repo: str | Path, *, allow_kinbase_submit: bool = False,
                  no_kinbase: bool = False) -> FastMCP:
    """Create a server bound to one existing repository, never ambient graphs.

    The canonical local layout is selected once. Every call revalidates that
    selection and filesystem links; conflicting populated layouts are refused.
    Repo/global YAML, profiles and graph-routing environment variables are not
    loaded. A missing local graph initializes on its first storage operation.
    Kinbase defaults to source reads. Its write-capable status and submission
    tools require explicit launcher opt-in; no_kinbase omits all Kinbase tools.
    The one user setting read is ``secret_mask``: it only removes known secret
    values, so it applies here as everywhere else Kindex writes.
    """
    if allow_kinbase_submit and no_kinbase:
        raise ValueError("allow_kinbase_submit and no_kinbase are mutually exclusive")
    activate_secret_mask(user_secret_mask())
    scope = _RepoScope(repo)
    if no_kinbase:
        kinbase_tools = ()
        kinbase_instructions = "Kinbase tools are disabled for this server. "
    elif allow_kinbase_submit:
        kinbase_tools = KINBASE_TOOLS
        kinbase_instructions = (
            "Kinbase sync/explain/submit/status are bound to this repository. Submissions "
            "send explicit AI evidence through native ingestion, whose receipt reports "
            "derivation/admission. No bulk admission or ratification is requested. Kinbase status "
            "may close overdue apologies through its existing signed-write behavior. "
        )
    else:
        kinbase_tools = KINBASE_READ_TOOLS
        kinbase_instructions = (
            "Kinbase sync/explain read source evidence for this repository; sync updates "
            "only the local Kindex projection. Kinbase submissions and write-capable status "
            "are unavailable: unfinished work cannot be submitted through this server. "
        )
    server = FastMCP(
        "kindex-lite",
        instructions=(
            f"Repository scope: {scope.repo}. Graph: {scope.data_path}. "
            "Only this local graph is available; scope cannot be changed by a tool call. "
            "Use tag_start/tag_resume, search before adding, and tag_update to end sessions. "
            "Search uses local text and graph retrieval; learn uses keyword extraction. "
            "Ambient configuration and host conversation history are not loaded. "
            + kinbase_instructions +
            "This capability boundary does not sandbox other AI tools or the Kinbase executable."
        ),
    )
    for name in (*LOCAL_TOOLS, *kinbase_tools):
        # The full server's decorators record host health. The lite surface
        # supplies its own error/redaction guard and no host health writer.
        handler = inspect.unwrap(getattr(mcp_server, name))
        server.add_tool(scope.wrap(handler), name=name)

    @server.tool()
    def scope_info() -> dict:
        """Report this server's fixed repository, graph and permitted Kinbase operations."""
        try:
            scope.validate()
            return {"ok": True, "repo": str(scope.repo), "data_dir": str(scope.data_path),
                    "graph": "project", "kinbase_operations": list(kinbase_tools),
                    "kinbase_submissions_allowed": allow_kinbase_submit,
                    "ambient_config": False, "os_sandbox": False}
        except (ValueError, OSError) as error:
            return {"ok": False, "error": safe_error(error)}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Kindex MCP bound to one repository")
    parser.add_argument("--repo", required=True, help="Existing repository directory")
    kinbase = parser.add_mutually_exclusive_group()
    kinbase.add_argument("--allow-kinbase-submit", action="store_true",
                         help="Enable Kinbase submission and write-capable status tools")
    kinbase.add_argument("--no-kinbase", action="store_true",
                         help="Disable all Kinbase tools")
    args = parser.parse_args()
    try:
        server = create_server(args.repo, allow_kinbase_submit=args.allow_kinbase_submit,
                               no_kinbase=args.no_kinbase)
    except (ValueError, OSError) as error:
        parser.error(safe_error(error))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
