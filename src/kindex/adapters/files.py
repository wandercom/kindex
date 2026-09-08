"""File watcher adapter — track registered files for changes."""

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .base import AdapterMeta, AdapterOption, IngestResult
from ..privacy import redacting_print as print

if TYPE_CHECKING:
    from ..store import Store


def sha256_file(filepath: str | Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_registered_files(store: "Store", verbose: bool = False) -> int:
    """Scan all nodes with registered file_paths for changes.

    For each node with extra.file_paths:
    - Check if files exist
    - Compute SHA-256 hash
    - Compare with stored hash (extra.file_hashes)
    - If changed, update node content with file excerpt
    - Update stored hash

    Returns count of nodes updated.
    """
    nodes = store.all_nodes(limit=10000)
    count = 0

    for node in nodes:
        extra = node.get("extra") or {}
        paths = extra.get("file_paths", [])
        if not paths:
            continue

        hashes = extra.get("file_hashes", {})
        changed = False
        new_content_parts = []

        for path_str in paths:
            path = Path(path_str)
            if not path.exists():
                if verbose:
                    print(f"  Missing: {path_str} (referenced by {node['title']})")
                continue

            current_hash = sha256_file(path)
            stored_hash = hashes.get(path_str, "")

            if current_hash != stored_hash:
                hashes[path_str] = current_hash
                changed = True

                # Read file content (first 2000 chars)
                try:
                    content = path.read_text(errors="replace")[:2000]
                    new_content_parts.append(f"## {path.name}\n{content}")
                except OSError:
                    pass

                if verbose:
                    print(f"  Changed: {path_str}")

        if changed:
            extra["file_hashes"] = hashes
            updates = {"extra": extra}
            if new_content_parts:
                # Append file content to node
                existing_content = node.get("content", "")
                file_section = "\n\n---\n\n".join(new_content_parts)
                # Replace old file section if exists
                if "## Registered files" in existing_content:
                    existing_content = existing_content.split("## Registered files")[0].strip()
                updates["content"] = existing_content + "\n\n## Registered files\n\n" + file_section

            store.update_node(node["id"], **updates)
            count += 1

    return count


def ingest_directory(store: "Store", directory: str | Path,
                     extensions: list[str] | None = None,
                     verbose: bool = False) -> int:
    """Ingest files from a directory as document nodes.

    Args:
        store: Kindex store
        directory: Directory path to scan
        extensions: File extensions to include (e.g. [".md", ".txt", ".py"])
                    Defaults to markdown and text files.
        verbose: Print progress

    Returns count of nodes created.
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return 0

    if extensions is None:
        extensions = [".md", ".txt", ".rst", ".org"]

    count = 0

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        # Skip hidden/dot files
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue

        node_id = f"file-{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"

        if store.get_node(node_id):
            continue

        try:
            content = path.read_text(errors="replace")[:4000]
        except OSError:
            continue

        title = path.stem.replace("-", " ").replace("_", " ").title()

        file_hash = sha256_file(path)

        store.add_node(
            node_id=node_id,
            title=title,
            content=content,
            node_type="document",
            prov_source=str(path),
            prov_activity="file-ingest",
            extra={"file_paths": [str(path)], "file_hashes": {str(path): file_hash}},
        )
        count += 1
        if verbose:
            print(f"  File: {title} ({path.name})")

    return count


# ── Adapter protocol wrapper ────────────────────────────────────────


class FilesAdapter:
    meta = AdapterMeta(
        name="files",
        description="Scan registered files for changes and ingest directories",
        options=[
            AdapterOption("directory", "Directory to ingest (optional; default scans registered files)"),
        ],
    )

    def is_available(self) -> bool:
        return True

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        directory = kwargs.get("directory")
        if directory:
            created = ingest_directory(store, directory, verbose=verbose)
            return IngestResult(created=created)
        else:
            updated = scan_registered_files(store, verbose=verbose)
            return IngestResult(updated=updated)


adapter = FilesAdapter()
