"""Conversations adapter — ingest conversation transcripts losslessly (see kindex.conversations)."""

from __future__ import annotations

from pathlib import Path

from .base import AdapterMeta, AdapterOption, IngestResult


class ConversationsAdapter:
    meta = AdapterMeta(
        name="conversations",
        description="Ingest conversation transcripts (JSON/JSONL) as dated document nodes",
        options=[AdapterOption("directory", "Directory of conversation files", required=True)],
    )

    def is_available(self) -> bool:
        return True

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        from ..conversations import ingest_directory

        directory = kwargs.get("directory")
        if not directory:
            return IngestResult(created=0, errors=["--directory is required"])
        return IngestResult(created=ingest_directory(store, Path(directory).expanduser(), verbose=verbose))


adapter = ConversationsAdapter()
