"""Codex sessions adapter — ingest Codex conversation sessions."""

from __future__ import annotations

from .. import platform_io
from .base import AdapterMeta, AdapterOption, IngestResult


class CodexSessionsAdapter:
    meta = AdapterMeta(
        name="codex-sessions",
        description="Ingest Codex conversation sessions",
        options=[
            AdapterOption("limit", "Max sessions to scan", default=10),
        ],
    )

    def is_available(self) -> bool:
        from ..config import load_config

        cfg = load_config()
        return platform_io.expanduser(cfg.codex_path / "sessions").exists()

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        from ..config import load_config
        from ..ingest import scan_codex_sessions

        cfg = kwargs.get("_config") or load_config()
        created = scan_codex_sessions(cfg, store, limit=limit, verbose=verbose)
        return IngestResult(created=created)


adapter = CodexSessionsAdapter()
