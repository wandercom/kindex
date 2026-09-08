"""Claude web adapter — ingest Claude.ai web conversations from exported JSON."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .base import AdapterMeta, AdapterOption, IngestResult

if TYPE_CHECKING:
    from ..store import Store

from ..privacy import protect_logger

log = protect_logger(logging.getLogger(__name__))

# Must match fetch_conversations.py's DEFAULT_OUTPUT. These two drifted once
# (fetcher -> ~/Downloads/_Claude, adapter -> ~/_Claude) and a bare
# `kin ingest claude-web` silently ingested a stale store and reported success.
DEFAULT_DIR = Path.home() / "_Claude" / "conversations"


def _extract_text(conv: dict, max_chars: int = 6000) -> str:
    """Extract human/assistant message text from a conversation JSON."""
    parts = []
    for msg in conv.get("chat_messages", []):
        sender = msg.get("sender", "")
        # Try content blocks first, fall back to text field
        text = ""
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        if not text:
            text = msg.get("text", "")
        if text.strip():
            parts.append(f"[{sender}] {text.strip()}")
    combined = "\n\n".join(parts)
    return combined[:max_chars] if combined else ""


def _conversation_summary(conv: dict) -> str:
    """Build a compact summary line for a conversation."""
    name = conv.get("name", "")
    msgs = conv.get("chat_messages", [])
    project = conv.get("_project_name", "")
    created = conv.get("created_at", "")[:10]
    proj_tag = f" [{project}]" if project else ""
    return f"{name}{proj_tag} ({created}, {len(msgs)} msgs)"


class ClaudeWebAdapter:
    meta = AdapterMeta(
        name="claude-web",
        description="Ingest Claude.ai web conversations from exported JSON",
        options=[
            AdapterOption(
                "directory",
                f"Directory containing conversation JSONs (default: {DEFAULT_DIR})",
                default=str(DEFAULT_DIR),
            ),
        ],
    )

    def is_available(self) -> bool:
        return DEFAULT_DIR.exists()

    def ingest(
        self, store: Store, *, limit=50, since=None, verbose=False, **kwargs,
    ) -> IngestResult:
        from ..extract import keyword_extract

        conv_dir = Path(kwargs.get("directory") or DEFAULT_DIR)
        if not conv_dir.exists():
            return IngestResult(errors=[f"Directory not found: {conv_dir}"])

        # Load index for metadata
        index_path = conv_dir / "index.json"
        index = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text())
            except json.JSONDecodeError:
                pass

        existing_titles: list[str] = []

        json_files = sorted(
            conv_dir.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        # Exclude index.json
        json_files = [f for f in json_files if f.stem != "index"]

        created = 0
        updated = 0
        skipped = 0
        errors = []

        for i, jf in enumerate(json_files):
            if created + updated >= limit:
                break

            uuid = jf.stem
            node_id = f"claude-web-{uuid}"

            # Check if already ingested
            existing = store.get_node(node_id)

            try:
                conv = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"{uuid}: {e}")
                continue

            name = conv.get("name", "") or uuid[:12]
            created_at = conv.get("created_at", "")
            project = conv.get("_project_name", "")
            msgs = conv.get("chat_messages", [])

            # Apply since filter
            if since and created_at and created_at[:10] < since:
                skipped += 1
                continue

            # Skip empty conversations
            if not msgs:
                skipped += 1
                continue

            # Extract text and run keyword extraction
            text = _extract_text(conv)
            if not text:
                skipped += 1
                continue

            extraction = keyword_extract(text, existing_titles)

            # Build content: summary + first few extracted concepts
            concepts = extraction.get("concepts", [])
            concept_titles = [c["title"] for c in concepts[:10]]
            summary = conv.get("summary", "")

            content_parts = []
            if summary:
                content_parts.append(summary)
            if concept_titles:
                content_parts.append(f"Key topics: {', '.join(concept_titles)}")
            content_parts.append(f"{len(msgs)} messages")
            content = "\n".join(content_parts)

            # Determine domains from project and extracted concepts
            domains = []
            if project:
                domains.append(project)
            for c in concepts[:5]:
                for d in c.get("domains", []):
                    if d not in domains:
                        domains.append(d)

            extra = {
                "uuid": uuid,
                "message_count": len(msgs),
                "source": "claude.ai",
            }
            if project:
                extra["project"] = project

            if existing:
                # Update if conversation has grown
                old_count = (existing.get("extra") or {}).get("message_count", 0)
                if isinstance(old_count, str):
                    old_count = int(old_count) if old_count.isdigit() else 0
                if len(msgs) > old_count:
                    store.add_node(
                        title=name,
                        content=content,
                        node_id=node_id,
                        node_type="conversation",
                        domains=domains,
                        prov_when=created_at,
                        prov_activity="claude-web-ingest",
                        prov_source=f"claude.ai/{uuid}",
                        extra=extra,
                    )
                    updated += 1
                    if verbose:
                        log.info("Updated: %s", _conversation_summary(conv))
                else:
                    skipped += 1
                continue

            # Create new conversation node
            store.add_node(
                title=name,
                content=content,
                node_id=node_id,
                node_type="conversation",
                domains=domains,
                weight=0.4,
                prov_when=created_at,
                prov_activity="claude-web-ingest",
                prov_source=f"claude.ai/{uuid}",
                extra=extra,
            )
            created += 1

            if verbose:
                log.info("Created: %s", _conversation_summary(conv))

            # Link extracted concepts to existing graph nodes
            for concept in concepts[:5]:
                title_lower = concept["title"].lower()
                matches = store.fts_search(concept["title"], limit=1)
                if matches and matches[0]["title"].lower() == title_lower:
                    try:
                        store.add_edge(
                            node_id, matches[0]["id"],
                            edge_type="discusses",
                            weight=0.3,
                            why=f"Discussed in Claude web conversation: {name}",
                        )
                    except Exception:
                        pass  # duplicate edge, etc.

            # Link to project node if exists
            if project:
                proj_matches = store.fts_search(project, limit=1)
                if proj_matches:
                    try:
                        store.add_edge(
                            node_id, proj_matches[0]["id"],
                            edge_type="part_of",
                            weight=0.5,
                            why=f"Conversation in Claude project: {project}",
                        )
                    except Exception:
                        pass

        return IngestResult(
            created=created, updated=updated, skipped=skipped, errors=errors,
        )


adapter = ClaudeWebAdapter()
