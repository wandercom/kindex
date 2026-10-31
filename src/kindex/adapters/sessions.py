"""Sessions adapter — ingest Claude Code conversation sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import platform_io
from .base import AdapterMeta, AdapterOption, IngestResult

if TYPE_CHECKING:
    from ..store import Store


class SessionsAdapter:
    meta = AdapterMeta(
        name="sessions",
        description="Ingest Claude Code conversation sessions",
        options=[
            AdapterOption("limit", "Max sessions to scan", default=10),
        ],
    )

    def is_available(self) -> bool:
        from pathlib import Path

        return platform_io.expanduser(Path("~/.claude")).exists()

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        from ..config import load_config
        from ..ingest import scan_sessions

        cfg = kwargs.get("_config") or load_config()
        created = scan_sessions(cfg, store, limit=limit, verbose=verbose)
        return IngestResult(created=created)


adapter = SessionsAdapter()
