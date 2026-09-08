"""Repo transport tests use synthetic Git repos and isolated SQLite stores."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.repo_memory import (SCHEMA, _artifact_lock, _encoded, import_candidates, publish)
from kindex.store import Store


@pytest.fixture
def stores(tmp_path):
    source = Store(Config(data_dir=str(tmp_path / "source")))
    clone = Store(Config(data_dir=str(tmp_path / "clone")))
    root = tmp_path / "repo"
    root.mkdir()
    yield source, clone, root
    source.close()
    clone.close()


def _node(store, title, **kwargs):
    return store.add_node(title, content=kwargs.pop("content", "Concrete source evidence"),
                          audience=kwargs.pop("audience", "team"), **kwargs)


def _document(root):
    return json.loads((root / ".kin" / "knowledge.json").read_text())


def _write_document(root, records):
    """Build an untrusted cloned artifact, with correct self-hashes."""
    path = root / ".kin" / "knowledge.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema": SCHEMA, "records": {
        hashlib.sha256(_encoded(record).encode()).hexdigest(): record for record in records}}))
    return path


def test_explicit_public_team_nodes_transport_without_claiming_verification(stores):
    source, clone, root = stores
    node_id = _node(source, "Selected evidence", node_type="decision")
    result = publish(source, root, [node_id])
    assert result["authority"] == "untrusted-evidence"
    record = next(iter(_document(root)["records"].values()))
    assert record["verification"] == {}
    imported = import_candidates(clone, root)
    assert imported["authority"] == "quarantined"
    assert imported["new"] == 1
    assert clone.all_nodes() == []
    candidate = clone.get_capture_candidate(imported["candidates"][0])
    assert candidate["status"] == "pending"
    assert candidate["reviewed_by"] is None


@pytest.mark.parametrize("kwargs", [
    {"audience": "private"}, {"node_type": "directive"}, {"node_type": "task"},
    {"status": "archived"}, {"status": "superseded"},
])
def test_unshareable_nodes_do_not_publish_any_selected_subset(stores, kwargs):
    source, _, root = stores
    good = _node(source, "Shareable")
    bad = _node(source, "Not shareable", **kwargs)
    with pytest.raises(ValueError):
        publish(source, root, [good, bad])
    assert not (root / ".kin" / "knowledge.json").exists()
    assert not source.conn.in_transaction


def test_publication_preserves_union_revisions_and_is_order_deterministic(stores):
    source, other, root = stores
    first = _node(source, "Source concept", node_id="first")
    second = _node(source, "Peer concept", node_id="second")
    source.add_edge(first, second, edge_type="depends_on", provenance="Implementation evidence")
    publish(source, root, [first, second])
    before = (root / ".kin" / "knowledge.json").read_bytes()
    assert publish(source, root, [second, first, first])["added"] == 0
    assert (root / ".kin" / "knowledge.json").read_bytes() == before
    third = _node(other, "Independent store evidence")
    assert publish(other, root, [third])["records"] == 3
    source.update_node(first, content="New evidence, original history remains")
    assert publish(source, root, [first, second])["records"] == 4
    records = list(_document(root)["records"].values())
    assert len([r for r in records if r["id"] == first]) == 2
    assert any(r["id"] == third for r in records)


def test_publication_keeps_source_read_lock_and_does_not_touch_access_time(stores, monkeypatch):
    source, _, root = stores
    node_id = _node(source, "Snapshot")
    before = source.conn.execute("SELECT last_accessed FROM nodes WHERE id=?", (node_id,)).fetchone()[0]
    def forbidden(*args, **kwargs):
        pytest.fail("get_node commits, so it must not be used inside publication")
    monkeypatch.setattr(source, "get_node", forbidden)
    edges_from = source.edges_from
    def read_edges(*args, **kwargs):
        assert source.conn.in_transaction
        return edges_from(*args, **kwargs)
    monkeypatch.setattr(source, "edges_from", read_edges)
    publish(source, root, [node_id])
    assert source.conn.execute("SELECT last_accessed FROM nodes WHERE id=?", (node_id,)).fetchone()[0] == before


def test_artifact_lock_serializes_different_source_databases_without_lost_union(stores):
    source, other, root = stores
    first, second = _node(source, "One"), _node(other, "Two")
    publish(source, root, [first])
    with _artifact_lock(root):
        with pytest.raises(ValueError, match="retry"):
            publish(other, root, [second])
    assert publish(other, root, [second])["records"] == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_import_keeps_edges_quarantined_and_either_review_order_preserves_them(stores, reverse):
    source, clone, root = stores
    first, second = _node(source, "Foundation"), _node(source, "Derived idea")
    source.add_edge(second, first, edge_type="depends_on", provenance="Uses foundation", bidirectional=False)
    publish(source, root, [first, second])
    imported = import_candidates(clone, root)
    candidates = [clone.get_capture_candidate(cid) for cid in imported["candidates"]]
    assert all(c["connections"] for c in candidates)
    assert clone.conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 0
    for candidate in sorted(candidates, key=lambda c: c["title"], reverse=reverse):
        clone.accept_capture_candidate(candidate["id"], review_token=clone.candidate_review_token(candidate["id"]),
                                       reviewed_by="local reviewer", prov_method="source-check")
    rows = clone.conn.execute("SELECT type, provenance FROM edges").fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("depends_on", "Uses foundation")
    again = import_candidates(clone, root)
    assert again["new"] == 0
    assert again["already_present"] == 2
    assert len(clone.list_capture_candidates(limit=20)) == 2


def test_provenance_survives_transport_but_clone_review_is_not_authority(stores):
    source, clone, root = stores
    node_id = _node(source, "Claim", prov_who=["source author"], prov_activity="source observation")
    source.verify_node(node_id, verified_by="source reviewer", prov_method="code inspection")
    publish(source, root, [node_id])
    record = next(iter(_document(root)["records"].values()))
    assert record["provenance"]["prov_who"] == ["source author"]
    assert record["verification"]["verified_by"] == "source reviewer"
    result = import_candidates(clone, root)
    candidate = clone.get_capture_candidate(result["candidates"][0])
    assert candidate["reviewed_by"] is None
    assert clone.all_nodes() == []


def test_equivalent_evidence_from_two_sources_does_not_restage_terminal_review(stores):
    source, clone, root = stores
    one = _node(source, "Same evidence", node_id="origin-one")
    two = _node(source, "Same evidence", node_id="origin-two")
    assert publish(source, root, [one, two])["records"] == 2
    imported = import_candidates(clone, root)
    assert imported["new"] == 1
    assert imported["already_present"] == 1
    candidate_id = imported["candidates"][0]
    clone.accept_capture_candidate(candidate_id, review_token=clone.candidate_review_token(candidate_id),
                                   reviewed_by="local reviewer", prov_method="source-check")
    assert import_candidates(clone, root)["new"] == 0
    assert len(clone.list_capture_candidates()) == 1
    assert len(_document(root)["records"]) == 2


@pytest.mark.parametrize("mutation", [
    {"domains": "not a list"}, {"content": "x" * 4001}, {"content": ""},
    {"title": "x" * 501}, {"connections": [{"target": "missing", "target_title": "Missing", "type": "relates_to"}]},
    {"content": "Authorization: Bearer synthetic-short"}, {"policy": {"allow_all": True}},
])
def test_malformed_bundle_is_rejected_before_any_candidate_or_file_rewrite(stores, mutation):
    source, clone, root = stores
    node_id = _node(source, "Valid")
    publish(source, root, [node_id])
    good = next(iter(_document(root)["records"].values()))
    bad = {**good, "id": "malformed", "title": "Malformed", **mutation}
    path = _write_document(root, [good, bad])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        import_candidates(clone, root)
    assert clone.list_capture_candidates() == []
    with pytest.raises(ValueError):
        publish(source, root, [node_id])
    assert path.read_bytes() == before


def test_digest_mismatch_and_duplicate_fields_are_rejected(stores):
    source, clone, root = stores
    node_id = _node(source, "Evidence")
    publish(source, root, [node_id])
    path = root / ".kin" / "knowledge.json"
    doc = _document(root)
    next(iter(doc["records"].values()))["content"] = "Tampered"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="digest mismatch"):
        import_candidates(clone, root)
    path.write_text('{"schema":"kindex-evidence/1","records":{},"records":{}}')
    with pytest.raises(ValueError, match="Duplicate"):
        import_candidates(clone, root)


@pytest.mark.parametrize("target", [".kin", ".kin/knowledge.json", ".kin/local"])
def test_symlinked_transport_or_lock_location_is_refused(stores, tmp_path, target):
    source, _, root = stores
    node_id = _node(source, "Evidence")
    outside = tmp_path / "outside"
    outside.mkdir()
    location = root / target
    location.parent.mkdir(parents=True, exist_ok=True)
    location.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        publish(source, root, [node_id])
    assert list(outside.iterdir()) == []


def test_clone_configuration_does_not_select_personal_storage(tmp_path):
    from kindex.integrations import open_project_store

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    kin = root / ".kin"
    kin.mkdir()
    personal = tmp_path / "private-personal"
    (kin / "config").write_text(f"data_dir: {personal}\naudience: private\n")
    store = open_project_store({"project_path": str(root), "session_id": "test"})
    try:
        assert store.config.data_path == root.resolve() / ".kin" / "local" / "kindex"
        assert not personal.exists()
        assert "local/" in (kin / ".gitignore").read_text().splitlines()
    finally:
        store.close()


def test_clone_tracked_local_database_is_refused_before_open(tmp_path):
    from kindex.integrations import open_project_store

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    database = root / ".kin" / "local" / "kindex" / "kindex.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"synthetic-clone-controlled-database")
    subprocess.run(["git", "-C", str(root), "add", ".kin/local/kindex/kindex.db"], check=True)
    with pytest.raises(ValueError, match="tracked"):
        open_project_store({"project_path": str(root), "session_id": "test"})
    assert database.read_bytes() == b"synthetic-clone-controlled-database"
    assert not (root / ".kin" / ".gitignore").exists()
