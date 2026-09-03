"""Slow graph archive — rotated SQLite files for decayed knowledge.

The fast graph (kindex.db) holds active, high-weight knowledge.
When nodes decay below a threshold and become stale, they are
exported to the slow graph (archive/*.db) and removed from the
fast graph. This keeps the active graph lean while preserving
everything.

Archives rotate by size (default 50MB) or age (default 1 year),
whichever triggers first — similar to log rotation.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


DEFAULT_ARCHIVE_MIN_AGE_DAYS = 60
ARCHIVE_DUPLICATE_COUNT_META = "archive_duplicate_count"
ARCHIVE_DUPLICATE_IDS_META = "archive_duplicate_ids"

# Archive schema — flat snapshot, no FTS, no triggers
_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_nodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT,
    type TEXT,
    status TEXT,
    weight REAL,
    domains TEXT,
    extra TEXT,
    created_at TEXT,
    updated_at TEXT,
    archived_at TEXT NOT NULL,
    prov_source TEXT,
    prov_activity TEXT,
    prov_who TEXT,
    prov_why TEXT
);

CREATE TABLE IF NOT EXISTS archived_edges (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT,
    weight REAL,
    provenance TEXT,
    created_at TEXT,
    archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Defaults
DEFAULT_MAX_SIZE_MB = 50
DEFAULT_MAX_AGE_DAYS = 365


def archive_dir(config: "Config") -> Path:
    """Return the archive directory path, creating it if needed."""
    d = config.data_path / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_archive_path(config: "Config") -> Path:
    """Get or create the current (active) archive file."""
    d = archive_dir(config)
    current = d / "current.db"
    return current


def _open_archive(path: Path) -> sqlite3.Connection:
    """Open an archive SQLite file and ensure schema exists."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_ARCHIVE_SCHEMA)
    # Set creation timestamp if new
    cur = conn.execute("SELECT value FROM archive_meta WHERE key='created_at'")
    if cur.fetchone() is None:
        now = datetime.datetime.now(tz=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO archive_meta (key, value) VALUES ('created_at', ?)",
            (now,),
        )
        conn.commit()
    return conn


def _should_rotate(path: Path, max_size_mb: int = DEFAULT_MAX_SIZE_MB,
                   max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> bool:
    """Check if the current archive needs rotation."""
    if not path.exists():
        return False

    # Size check
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb >= max_size_mb:
        return True

    # Age check
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT value FROM archive_meta WHERE key='created_at'")
        row = cur.fetchone()
        conn.close()
        if row:
            created = datetime.datetime.fromisoformat(row["value"])
            age = (datetime.datetime.now() - created).days
            if age >= max_age_days:
                return True
    except Exception:
        pass

    return False


def _rotate_archive(config: "Config") -> Path | None:
    """Rotate current.db to a timestamped name. Returns the rotated path."""
    current = _current_archive_path(config)
    if not current.exists():
        return None

    now = datetime.datetime.now(tz=None)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    rotated = current.parent / f"archive_{stamp}.db"
    current.rename(rotated)
    return rotated


def archive_nodes(
    config: "Config",
    store: "Store",
    node_ids: list[str],
    verbose: bool = False,
) -> int:
    """Move nodes (and their edges) from the fast graph to the slow archive.

    Returns count of nodes archived.
    """
    if not node_ids:
        return 0

    # Check rotation first
    current_path = _current_archive_path(config)
    if _should_rotate(current_path):
        rotated = _rotate_archive(config)
        if verbose and rotated:
            print(f"  Rotated archive: {rotated.name}")

    archive_conn = _open_archive(_current_archive_path(config))
    now = datetime.datetime.now(tz=None).isoformat(timespec="seconds")
    count = 0

    try:
        for nid in node_ids:
            source = store.conn
            source.execute("BEGIN IMMEDIATE")
            try:
                row = source.execute(
                    "SELECT * FROM nodes WHERE id = ?", (nid,)
                ).fetchone()
                if row is None:
                    source.rollback()
                    continue
                node = store._row_to_dict(row)

                # Session lifecycle is not a force-delete surface. Re-check
                # its completed/unlinked facts while holding the source write
                # lock, so a link committed after candidate selection wins and
                # keeps the session in the fast graph.
                if node.get("type") == "session":
                    extra_value = node.get("extra")
                    linked = (
                        extra_value.get("linked_nodes")
                        if isinstance(extra_value, dict)
                        else None
                    )
                    has_edge = source.execute(
                        "SELECT 1 FROM edges "
                        "WHERE from_id = ? OR to_id = ? LIMIT 1",
                        (nid, nid),
                    ).fetchone()
                    if (
                        not isinstance(linked, list)
                        or linked
                        or extra_value.get("session_status") != "completed"
                        or has_edge is not None
                    ):
                        source.rollback()
                        continue

                def _ser(val):
                    if isinstance(val, (list, dict)):
                        return json.dumps(val)
                    return val or ""

                domains = _ser(node.get("domains", []))
                extra = _ser(node.get("extra", {}))
                prov_who = _ser(node.get("prov_who", ""))

                # One archive transaction owns the node and every copied edge.
                # The source BEGIN IMMEDIATE above prevents another writer from
                # adding an edge between this read and the source deletion.
                archive_conn.execute("BEGIN IMMEDIATE")
                archive_conn.execute(
                    """INSERT OR REPLACE INTO archived_nodes
                       (id, title, content, type, status, weight, domains, extra,
                        created_at, updated_at, archived_at, prov_source,
                        prov_activity, prov_who, prov_why)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        nid,
                        node.get("title", ""),
                        node.get("content", ""),
                        node.get("type", "concept"),
                        node.get("status", "archived"),
                        node.get("weight", 0),
                        domains,
                        extra,
                        node.get("created_at", ""),
                        node.get("updated_at", ""),
                        now,
                        node.get("prov_source", "") or "",
                        node.get("prov_activity", "") or "",
                        prov_who,
                        node.get("prov_why", "") or "",
                    ),
                )

                edges = store.edges_from(nid) + store.edges_to(nid)
                for edge in edges:
                    archive_conn.execute(
                        """INSERT OR REPLACE INTO archived_edges
                           (id, from_id, to_id, type, weight, provenance,
                            created_at, archived_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            edge.get("id", ""),
                            edge.get("from_id", ""),
                            edge.get("to_id", ""),
                            edge.get("type", "relates_to"),
                            edge.get("weight", 0),
                            edge.get("provenance", ""),
                            edge.get("created_at", ""),
                            now,
                        ),
                    )

                # Commit the recoverable copy before deleting the authority.
                # A crash between commits can leave a duplicate, never loss;
                # retrying an otherwise-eligible node is idempotent because
                # archive rows replace by ID. The next archive cycle reports
                # any duplicate that became ineligible rather than guessing
                # that equal IDs prove equal entities.
                archive_conn.commit()
                source.execute(
                    "DELETE FROM edges WHERE from_id = ? OR to_id = ?",
                    (nid, nid),
                )
                source.execute("DELETE FROM nodes WHERE id = ?", (nid,))
                source.commit()
            except BaseException:
                source.rollback()
                archive_conn.rollback()
                raise

            try:
                from .vectors import delete_embedding
                delete_embedding(store, nid)
            except Exception:
                pass
            store._log("delete_node", nid, node.get("title", nid))
            count += 1

            if verbose:
                print(f"  Archived to slow graph: {node.get('title', nid)}")
    finally:
        archive_conn.close()

    return count


def find_archivable_nodes(
    store: "Store",
    weight_threshold: float = 0.05,
    min_age_days: int = DEFAULT_ARCHIVE_MIN_AGE_DAYS,
    limit: int = 50,
) -> list[str]:
    """Find nodes eligible for archival to slow graph.

    Criteria:
    - Semantic nodes are archived/superseded, low-weight, and old.
    - Completed session tags are old, unlinked, and have no graph edges.
    - Active/paused sessions and other lifecycle nodes stay in the fast store.
    """
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=min_age_days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    rows = store.conn.execute(
        """SELECT n.id FROM nodes n
            WHERE n.updated_at < ?
              AND (
                    (
                        n.status IN ('archived', 'superseded')
                        AND n.weight <= ?
                        AND n.type NOT IN ('task', 'session', 'checkpoint',
                                           'coordination')
                    )
                    OR
                    (
                        n.type = 'session'
                        AND json_valid(n.extra)
                        AND json_extract(n.extra, '$.session_status') = 'completed'
                        AND json_type(n.extra, '$.linked_nodes') = 'array'
                        AND COALESCE(
                            json_array_length(
                                json_extract(n.extra, '$.linked_nodes')
                            ),
                            0
                        ) = 0
                        AND NOT EXISTS (
                            SELECT 1 FROM edges e
                             WHERE e.from_id = n.id OR e.to_id = n.id
                        )
                    )
              )
            ORDER BY n.weight ASC, n.updated_at ASC, n.id ASC
            LIMIT ?""",
        (cutoff_iso, weight_threshold, limit),
    ).fetchall()

    return [r["id"] for r in rows]


def find_archive_duplicates(
    config: "Config",
    store: "Store",
    *,
    sample_limit: int = 50,
) -> dict:
    """Report IDs present in both fast and slow stores without deleting either.

    A duplicate is the safe residue of archive-first/source-delete-second, but
    ID equality alone cannot prove it came from that crash window rather than
    an import collision. Fast-store presence therefore triggers visibility,
    not an automatic destructive reconciliation.
    """
    count = 0
    samples: list[dict] = []
    for db_file in sorted(archive_dir(config).glob("*.db")):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            conn.execute(
                "ATTACH DATABASE ? AS fast_graph",
                (str(store.db_path),),
            )
            count += conn.execute(
                """SELECT COUNT(*)
                     FROM archived_nodes archived
                     JOIN fast_graph.nodes live ON live.id = archived.id"""
            ).fetchone()[0]
            remaining = sample_limit - len(samples)
            if remaining > 0:
                rows = conn.execute(
                    """SELECT archived.id,
                              archived.title AS archived_title,
                              live.title AS live_title,
                              archived.archived_at
                         FROM archived_nodes archived
                         JOIN fast_graph.nodes live ON live.id = archived.id
                        ORDER BY archived.id
                        LIMIT ?""",
                    (remaining,),
                ).fetchall()
                samples.extend({
                    "id": row["id"],
                    "live_title": row["live_title"],
                    "archived_title": row["archived_title"],
                    "archive_file": db_file.name,
                    "archived_at": row["archived_at"],
                } for row in rows)
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
    return {"count": count, "samples": samples}


def _record_archive_duplicate_state(
    config: "Config",
    store: "Store",
    *,
    verbose: bool,
) -> dict:
    duplicates = find_archive_duplicates(config, store)
    store.set_meta(ARCHIVE_DUPLICATE_COUNT_META, str(duplicates["count"]))
    store.set_meta(
        ARCHIVE_DUPLICATE_IDS_META,
        json.dumps([item["id"] for item in duplicates["samples"]]),
    )
    if verbose and duplicates["count"]:
        sample = ", ".join(
            item["id"] for item in duplicates["samples"][:5]
        )
        print(
            "  Warning: "
            f"{duplicates['count']} node ID(s) exist in both fast and slow "
            f"graphs ({sample}). Copies were preserved for manual review."
        )
    return duplicates


def archive_cycle(
    config: "Config",
    store: "Store",
    verbose: bool = False,
) -> int:
    """Run one archive cycle: find archivable nodes, move to slow graph.

    Designed to be called from cron_run.
    """
    node_ids = find_archivable_nodes(store)
    archived = (
        archive_nodes(config, store, node_ids, verbose=verbose)
        if node_ids
        else 0
    )
    _record_archive_duplicate_state(config, store, verbose=verbose)
    return archived


def list_archives(config: "Config") -> list[dict]:
    """List all archive files with metadata."""
    d = archive_dir(config)
    result = []
    for db_file in sorted(d.glob("*.db")):
        info = {
            "path": str(db_file),
            "name": db_file.name,
            "size_mb": round(db_file.stat().st_size / (1024 * 1024), 2),
        }
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            node_count = conn.execute("SELECT COUNT(*) as c FROM archived_nodes").fetchone()["c"]
            edge_count = conn.execute("SELECT COUNT(*) as c FROM archived_edges").fetchone()["c"]
            created = conn.execute(
                "SELECT value FROM archive_meta WHERE key='created_at'"
            ).fetchone()
            conn.close()
            info["nodes"] = node_count
            info["edges"] = edge_count
            info["created_at"] = created["value"] if created else ""
        except Exception:
            info["nodes"] = 0
            info["edges"] = 0
        result.append(info)
    return result


def search_archives(
    config: "Config",
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search across all archive files by title/content LIKE match.

    Returns archived nodes matching the query, newest archives first.
    """
    d = archive_dir(config)
    results = []
    pattern = f"%{query}%"

    for db_file in sorted(d.glob("*.db"), reverse=True):
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM archived_nodes
                   WHERE title LIKE ? OR content LIKE ?
                   ORDER BY archived_at DESC LIMIT ?""",
                (pattern, pattern, limit - len(results)),
            ).fetchall()
            conn.close()
            for r in rows:
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "type": r["type"],
                    "weight": r["weight"],
                    "archived_at": r["archived_at"],
                    "archive_file": db_file.name,
                })
        except Exception:
            continue

        if len(results) >= limit:
            break

    return results


def restore_node(
    config: "Config",
    store: "Store",
    node_id: str,
    verbose: bool = False,
) -> bool:
    """Restore a node from the slow graph back to the fast graph.

    Searches all archives for the node ID, restores it with a fresh
    weight, and removes it from the archive.
    """
    d = archive_dir(config)

    for db_file in sorted(d.glob("*.db"), reverse=True):
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM archived_nodes WHERE id = ?", (node_id,)
            ).fetchone()

            if row is None:
                conn.close()
                continue

            # Restore to fast graph
            domains = []
            try:
                domains = json.loads(row["domains"]) if row["domains"] else []
            except (json.JSONDecodeError, TypeError):
                pass

            extra = {}
            try:
                extra = json.loads(row["extra"]) if row["extra"] else {}
            except (json.JSONDecodeError, TypeError):
                pass

            store.add_node(
                title=row["title"],
                content=row["content"] or "",
                node_id=row["id"],
                node_type=row["type"] or "concept",
                domains=domains,
                prov_source=row["prov_source"] or "",
                prov_activity="restored-from-archive",
                prov_who=row["prov_who"] or "",
                prov_why=f"Restored from {db_file.name}",
                extra=extra,
            )
            # Give restored node a moderate weight
            store.update_node(node_id, weight=0.3, status="active")

            # Restore edges where both endpoints exist in fast graph
            edge_rows = conn.execute(
                "SELECT * FROM archived_edges WHERE from_id = ? OR to_id = ?",
                (node_id, node_id),
            ).fetchall()
            for edge in edge_rows:
                other = edge["to_id"] if edge["from_id"] == node_id else edge["from_id"]
                if store.get_node(other):
                    store.add_edge(
                        edge["from_id"], edge["to_id"],
                        edge_type=edge["type"] or "relates_to",
                        weight=0.2,
                        provenance=f"restored from archive",
                    )

            # Remove from archive
            conn.execute("DELETE FROM archived_nodes WHERE id = ?", (node_id,))
            conn.execute(
                "DELETE FROM archived_edges WHERE from_id = ? OR to_id = ?",
                (node_id, node_id),
            )
            conn.commit()
            conn.close()

            if verbose:
                print(f"  Restored: {row['title']} from {db_file.name}")
            return True

        except Exception:
            continue

    return False
