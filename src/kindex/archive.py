"""Slow graph archive — rotated SQLite files for decayed knowledge.

The fast graph (kindex.db) holds active, high-weight knowledge.
When nodes decay below a threshold and become stale, they are
exported to the slow graph (archive/*.db) and removed from the
fast graph. This keeps the active graph lean while preserving
everything: each archived node and edge keeps its whole stored row,
and a restore puts that row back.

Archives rotate by size (default 50MB) or age (default 1 year),
whichever triggers first — similar to log rotation.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .privacy import redact, safe_error
from .privacy import redacting_print as print

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


DEFAULT_ARCHIVE_MIN_AGE_DAYS = 60
ARCHIVE_DUPLICATE_COUNT_META = "archive_duplicate_count"
ARCHIVE_DUPLICATE_IDS_META = "archive_duplicate_ids"
ARCHIVE_FAILED_COUNT_META = "archive_failed_count"
ARCHIVE_FAILED_IDS_META = "archive_failed_ids"

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
    prov_why TEXT,
    node_row TEXT
);

CREATE TABLE IF NOT EXISTS archived_edges (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT,
    weight REAL,
    provenance TEXT,
    created_at TEXT,
    archived_at TEXT NOT NULL,
    edge_row TEXT
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
    # The whole-row columns arrived after the first archives were written.
    for table, column in (("archived_nodes", "node_row"), ("archived_edges", "edge_row")):
        present = {info[1] for info in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.commit()
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
    failures: list[dict] = []

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
                stored_row = dict(row)
                node = store._row_to_dict(row)
                if redact(node) != node:
                    # Existing evidence is immutable here. Do not copy raw
                    # credentials or silently change digest-bound history.
                    raise ValueError("Archive source requires explicit credential remediation")

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
                        prov_activity, prov_who, prov_why, node_row)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        # The summary columns above serve search and listing;
                        # this is the node as stored, which restore puts back.
                        json.dumps(stored_row),
                    ),
                )

                edges = [
                    dict(edge) for edge in source.execute(
                        "SELECT * FROM edges WHERE from_id = ? OR to_id = ?",
                        (nid, nid),
                    )
                ]
                if redact(edges) != edges:
                    raise ValueError("Archive edges require explicit credential remediation")
                for edge in edges:
                    archive_conn.execute(
                        """INSERT OR REPLACE INTO archived_edges
                           (id, from_id, to_id, type, weight, provenance,
                            created_at, archived_at, edge_row)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(edge["id"]),
                            edge["from_id"],
                            edge["to_id"],
                            edge.get("type") or "relates_to",
                            edge.get("weight", 0),
                            edge.get("provenance") or "",
                            edge.get("created_at") or "",
                            now,
                            json.dumps(edge),
                        ),
                    )

                # Delete inside the held source transaction first, so a delete
                # the store refuses fails before any copy exists; then commit
                # the recoverable copy, and only then the deletion. A crash
                # between the two commits can leave a duplicate, never loss;
                # retrying an otherwise-eligible node is idempotent because
                # archive rows replace by ID. The next archive cycle reports
                # any duplicate that became ineligible rather than guessing
                # that equal IDs prove equal entities.
                source.execute(
                    "DELETE FROM edges WHERE from_id = ? OR to_id = ?",
                    (nid, nid),
                )
                # Ranking signals reference the node; they die with it rather
                # than making the delete fail (and, with it, every later cycle,
                # since the node stays first in line).
                source.execute(
                    "DELETE FROM injection_pheromone WHERE node_id = ?", (nid,)
                )
                source.execute(
                    "DELETE FROM node_coactivation WHERE node_a = ? OR node_b = ?",
                    (nid, nid),
                )
                source.execute("DELETE FROM nodes WHERE id = ?", (nid,))
                archive_conn.commit()
                source.commit()
            except Exception as error:
                # One node that cannot move is reported and skipped; it does
                # not stop the batch behind it.
                source.rollback()
                archive_conn.rollback()
                failures.append({"id": nid, "error": safe_error(error)})
                if verbose:
                    print(f"  Could not archive {nid}: {safe_error(error)}")
                continue
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
        store.set_meta(ARCHIVE_FAILED_COUNT_META, str(len(failures)))
        store.set_meta(ARCHIVE_FAILED_IDS_META, json.dumps(failures[:20]))

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


def _stored(row: sqlite3.Row, column: str) -> dict | None:
    if column not in row.keys() or not row[column]:
        return None
    try:
        value = json.loads(row[column])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _legacy_node_row(row: sqlite3.Row) -> dict:
    """An archive written before whole rows were kept. Its summary columns
    already hold the stored spellings (JSON text for domains, extra and
    prov_who); every other column takes its default."""
    return {key: row[key] for key in (
        "id", "title", "content", "type", "domains", "extra", "created_at",
        "prov_source", "prov_activity", "prov_who", "prov_why",
    )}


def _legacy_edge_row(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in (
        "from_id", "to_id", "type", "weight", "provenance", "created_at",
    )}


def _insert(store: "Store", table: str, values: dict) -> int:
    columns = {info[1] for info in store.conn.execute(f"PRAGMA table_info({table})")}
    # Known column names only; NULL leaves the column to its default.
    values = {k: v for k, v in values.items() if k in columns and v is not None}
    names = list(values)
    cursor = store.conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [values[name] for name in names],
    )
    return cursor.rowcount


def restore_node(
    config: "Config",
    store: "Store",
    node_id: str,
    verbose: bool = False,
) -> bool:
    """Restore a node from the slow graph back to the fast graph.

    The node comes back as it was stored, with its provenance, audience,
    standing, referent and clocks, returned to active at a fresh weight; the
    restore itself is recorded in the activity log. Every archived edge whose
    other end is in the fast graph comes back with it, from whichever archive
    holds it; an edge to a node that is still archived stays archived until
    that node returns. A node already in the fast graph is not overwritten.
    """
    from .store import _now

    d = archive_dir(config)
    files = sorted(d.glob("*.db"), reverse=True)
    for db_file in files:
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM archived_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        except sqlite3.Error:
            conn.close()
            continue
        if row is None:
            conn.close()
            continue
        try:
            if store.conn.execute(
                "SELECT 1 FROM nodes WHERE id = ?", (node_id,)
            ).fetchone():
                raise ValueError(
                    f"{node_id} is already in the fast graph; the archived copy in "
                    f"{db_file.name} was left in place for review")
            node = _stored(row, "node_row") or _legacy_node_row(row)
            node.update(id=node_id, status="active", weight=0.3, updated_at=_now())
            if redact(node) != node:
                raise ValueError("Archived node requires explicit credential remediation")

            source = store.conn
            source.execute("BEGIN IMMEDIATE")
            restored_edges: dict[Path, list[str]] = {}
            try:
                _insert(store, "nodes", node)
                for edge_file in files:
                    edge_conn = sqlite3.connect(str(edge_file))
                    edge_conn.row_factory = sqlite3.Row
                    try:
                        edge_rows = edge_conn.execute(
                            "SELECT * FROM archived_edges WHERE from_id = ? OR to_id = ?",
                            (node_id, node_id),
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    finally:
                        edge_conn.close()
                    for edge in edge_rows:
                        other = edge["to_id"] if edge["from_id"] == node_id else edge["from_id"]
                        if other != node_id and not source.execute(
                            "SELECT 1 FROM nodes WHERE id = ?", (other,)
                        ).fetchone():
                            continue
                        values = _stored(edge, "edge_row") or _legacy_edge_row(edge)
                        if redact(values) != values:
                            raise ValueError(
                                "Archived edge requires explicit credential remediation")
                        _insert(store, "edges", values)
                        restored_edges.setdefault(edge_file, []).append(edge["id"])
                source.commit()
            except BaseException:
                source.rollback()
                raise
            store._log("restore_node", node_id, node.get("title") or node_id, "",
                       {"archive": db_file.name})
            try:
                from .vectors import enqueue_embedding
                enqueue_embedding(store, node_id)
            except Exception:
                pass

            # The fast graph holds the node now; only then leave the archive.
            conn.execute("DELETE FROM archived_nodes WHERE id = ?", (node_id,))
            conn.commit()
            for edge_file, edge_ids in restored_edges.items():
                edge_conn = conn if edge_file == db_file else sqlite3.connect(str(edge_file))
                edge_conn.executemany(
                    "DELETE FROM archived_edges WHERE id = ?",
                    [(edge_id,) for edge_id in edge_ids],
                )
                edge_conn.commit()
                if edge_conn is not conn:
                    edge_conn.close()

            if verbose:
                print(f"  Restored: {node.get('title') or node_id} from {db_file.name}")
            return True
        finally:
            conn.close()

    return False
