"""Adapter registry — discovers built-in and third-party adapters.

Built-in adapters are registered explicitly at import time.
Third-party adapters are discovered via entry points::

    # In a third-party pyproject.toml:
    [project.entry-points."kindex.adapters"]
    jira = "kindex_adapter_jira:adapter"

Then ``kin ingest jira`` just works.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Adapter

from ..privacy import protect_logger

log = protect_logger(logging.getLogger(__name__))

_BUILTIN: dict[str, Adapter] = {}
_cache: dict[str, Adapter] | None = None

EP_GROUP = "kindex.adapters"


def register(adapter: Adapter) -> None:
    """Register a built-in adapter."""
    _BUILTIN[adapter.meta.name] = adapter
    # Invalidate cache so next discover() picks it up
    global _cache
    _cache = None


def discover() -> dict[str, Adapter]:
    """Return all adapters: built-in + entry-point discovered.

    Results are cached after first call. Use ``reset()`` to clear.
    """
    global _cache
    if _cache is not None:
        return dict(_cache)

    adapters = dict(_BUILTIN)

    # Discover third-party adapters via entry points
    try:
        eps = entry_points(group=EP_GROUP)
    except TypeError:
        # Python < 3.12 fallback
        eps = entry_points().get(EP_GROUP, [])  # type: ignore[arg-type]

    for ep in eps:
        if ep.name in adapters:
            continue  # built-in takes precedence
        try:
            obj = ep.load()
            # Accept either an Adapter instance or a module with .adapter attr
            if hasattr(obj, "meta") and hasattr(obj, "ingest"):
                adapters[ep.name] = obj
            elif hasattr(obj, "adapter"):
                adapters[ep.name] = obj.adapter
            else:
                log.warning("Entry point %s does not expose an Adapter", ep.name)
        except Exception:
            log.warning("Failed to load adapter entry point: %s", ep.name, exc_info=True)

    _cache = adapters
    return dict(adapters)


def get(name: str) -> Adapter | None:
    """Get a specific adapter by name."""
    return discover().get(name)


def available() -> dict[str, Adapter]:
    """Return only adapters whose prerequisites are met."""
    return {k: v for k, v in discover().items() if v.is_available()}


def reset() -> None:
    """Clear the discovery cache (useful for testing)."""
    global _cache
    _cache = None


def _register_builtins() -> None:
    """Register all built-in adapters. Called once at import time."""
    # Lazy imports to avoid circular dependencies and heavy imports at startup
    try:
        from .github import adapter as github
        register(github)
    except Exception:
        pass

    try:
        from .linear import adapter as linear
        register(linear)
    except Exception:
        pass

    try:
        from .git_hooks import adapter as commits
        register(commits)
    except Exception:
        pass

    try:
        from .files import adapter as files
        register(files)
    except Exception:
        pass

    try:
        from .projects import adapter as projects
        register(projects)
    except Exception:
        pass

    try:
        from .sessions import adapter as sessions
        register(sessions)
    except Exception:
        pass

    try:
        from .codex_sessions import adapter as codex_sessions
        register(codex_sessions)
    except Exception:
        pass

    try:
        from .claude_web import adapter as claude_web
        register(claude_web)
    except Exception:
        pass

    try:
        from .code import adapter as code
        register(code)
    except Exception:
        pass

    try:
        from .understand_anything import adapter as understand_anything
        register(understand_anything)
    except Exception:
        pass


_register_builtins()
