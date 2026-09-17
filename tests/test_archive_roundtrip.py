"""Archiving to the slow graph and restoring loses nothing.

The archive kept 15 of the node's 28 columns and deleted the source, so
audience, standing, aka, intent, referent, the clocks and the verification
fields were gone even if nobody restored; restore rebuilt the node through
add_node with defaults, reset created_at and the provenance, and passed the
archived prov_who (JSON text) as a list, which minted a person node per
character. Edges came back at weight 0.2 with new provenance, and an edge to a
node that was still archived was dropped.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from kindex.archive import (
    _current_archive_path,
    _open_archive,
    archive_nodes,
    restore_node,
)
from kindex.config import Config
from kindex.store import Store

# Columns a restore deliberately sets: it returns the node to the fast graph.
RESTORE_SETS = {"status", "weight", "updated_at", "last_accessed"}


@pytest.fixture
def world(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    yield cfg, store
    store.close()


def stored_row(store: Store, node_id: str) -> dict:
    row = store.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else {}


def edge_rows(store: Store, node_id: str) -> list[dict]:
    rows = store.conn.execute(
        "SELECT from_id, to_id, type, weight, provenance, created_at, updated_at "
        "FROM edges WHERE from_id = ? OR to_id = ? ORDER BY from_id, to_id, type",
        (node_id, node_id),
    ).fetchall()
    return [dict(row) for row in rows]


def person_titles(store: Store) -> list[str]:
    return sorted(row[0] for row in store.conn.execute(
        "SELECT title FROM nodes WHERE type = 'person'"))


def rich_node(store: Store, node_id: str = "rich") -> None:
    store.add_node(
        "Retired deploy rule", content="Deploys went through the old pipeline.",
        node_id=node_id, aka=["legacy deploy"], intent="trying to document the old path",
        domains=["deploy"], audience="team", standing="prevalent",
        prov_who=["alice"], prov_activity="design-review", prov_why="audit",
        prov_source="docs/deploy.md", extra={"note": "kept"},
        referent={"path": "docs/deploy.md", "content_digest": "0" * 64,
                  "digest_scope": "file"},
        asserted_at="2024-01-02T03:04:05Z",
    )
    store.conn.execute(
        "UPDATE nodes SET created_at = '2024-01-01T00:00:00', prov_when = '2024-01-01T00:00:00', "
        "verified_at = '2024-02-01T00:00:00', verified_by = 'bob', prov_method = 'review', "
        "valid_at = '2024-01-01T00:00:00', status = 'superseded', weight = 0.01 WHERE id = ?",
        (node_id,),
    )
    store.conn.commit()


def test_a_restored_node_is_the_node_that_was_archived(world):
    cfg, store = world
    store.add_node("Pipeline", node_id="peer")
    rich_node(store)
    store.add_edge("rich", "peer", edge_type="relates_to", weight=0.77, provenance="curated")
    before = stored_row(store, "rich")
    edges_before = edge_rows(store, "rich")
    persons_before = person_titles(store)

    assert archive_nodes(cfg, store, ["rich"]) == 1
    assert stored_row(store, "rich") == {}
    assert restore_node(cfg, store, "rich") is True

    after = stored_row(store, "rich")
    assert {k: v for k, v in after.items() if k not in RESTORE_SETS} == \
        {k: v for k, v in before.items() if k not in RESTORE_SETS}
    assert (after["status"], after["weight"]) == ("active", 0.3)
    assert json.loads(after["prov_who"]) == ["alice"]
    assert edge_rows(store, "rich") == edges_before
    assert person_titles(store) == persons_before, "no person node was minted"


def test_an_archive_from_before_whole_rows_restores_prov_who_as_a_list(world):
    cfg, store = world
    store.add_node("Old fact", node_id="legacy", prov_who=["alice"], prov_activity="manual")
    persons_before = person_titles(store)
    archive = _open_archive(_current_archive_path(cfg))
    archive.execute(
        """INSERT INTO archived_nodes (id, title, content, type, status, weight, domains,
               extra, created_at, updated_at, archived_at, prov_source, prov_activity,
               prov_who, prov_why)
           VALUES ('legacy-archived', 'Old fact', '', 'concept', 'archived', 0.01, '[]',
               '{}', '2024-01-01T00:00:00', '2024-01-01T00:00:00', '2025-01-01T00:00:00',
               'src', 'manual', '["alice"]', 'why')"""
    )
    archive.commit()
    archive.close()

    assert restore_node(cfg, store, "legacy-archived") is True
    row = stored_row(store, "legacy-archived")
    assert json.loads(row["prov_who"]) == ["alice"]
    assert (row["prov_activity"], row["prov_why"], row["created_at"]) == \
        ("manual", "why", "2024-01-01T00:00:00")
    assert person_titles(store) == persons_before


def test_an_edge_to_a_node_still_archived_waits_for_it(world):
    cfg, store = world
    store.add_node("A", node_id="a")
    store.add_node("B", node_id="b")
    store.add_edge("a", "b", edge_type="depends_on", weight=0.66, provenance="curated")
    expected = edge_rows(store, "a")
    assert archive_nodes(cfg, store, ["a"]) == 1
    # A rotation in between: the second node lands in another archive file.
    current = _current_archive_path(cfg)
    current.rename(current.with_name("archive_20250101_000000.db"))
    assert archive_nodes(cfg, store, ["b"]) == 1

    assert restore_node(cfg, store, "a") is True
    assert edge_rows(store, "a") == [], "b is still archived"
    assert restore_node(cfg, store, "b") is True
    assert edge_rows(store, "a") == expected


def test_ranking_signals_do_not_block_archival(world):
    cfg, store = world
    for node_id in ("sig-a", "sig-b", "sig-c"):
        store.add_node(node_id, node_id=node_id)
    store.deposit_pheromone("sig-a", context="deploy")
    store.deposit_coactivation("sig-a", "sig-b", context="deploy")
    assert archive_nodes(cfg, store, ["sig-a", "sig-c"]) == 2
    assert stored_row(store, "sig-a") == {}
    assert store.get_meta("archive_failed_count") == "0"


def test_one_node_that_cannot_move_does_not_stop_the_batch(world):
    cfg, store = world
    store.add_node("Fine", node_id="fine")
    store.add_node("Blocked", node_id="blocked")
    # A child table the archive does not know about keeps the delete from
    # succeeding, as the ranking tables once did.
    store.conn.execute(
        "CREATE TABLE pinned (node_id TEXT NOT NULL REFERENCES nodes(id))")
    store.conn.execute("INSERT INTO pinned VALUES ('blocked')")
    store.conn.commit()

    assert archive_nodes(cfg, store, ["blocked", "fine"]) == 1
    assert stored_row(store, "fine") == {}
    assert stored_row(store, "blocked")["id"] == "blocked"
    failed = json.loads(store.get_meta("archive_failed_ids"))
    assert [item["id"] for item in failed] == ["blocked"]
    with sqlite3.connect(_current_archive_path(cfg)) as conn:
        assert conn.execute(
            "SELECT 1 FROM archived_nodes WHERE id = 'blocked'").fetchone() is None


def test_restore_never_overwrites_a_live_node(world):
    cfg, store = world
    store.add_node("Archived version", node_id="twin")
    assert archive_nodes(cfg, store, ["twin"]) == 1
    store.add_node("Live version", node_id="twin")
    with pytest.raises(ValueError, match="already in the fast graph"):
        restore_node(cfg, store, "twin")
    assert stored_row(store, "twin")["title"] == "Live version"
    with sqlite3.connect(_current_archive_path(cfg)) as conn:
        assert conn.execute(
            "SELECT 1 FROM archived_nodes WHERE id = 'twin'").fetchone() is not None
