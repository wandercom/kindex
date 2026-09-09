"""Exercise the CLI transfer boundary, not a hand-written Store round trip."""

import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store
from kindex.trust import node_trust_decision


def cli(data_dir, *args):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args,
         "--data-dir", str(data_dir), "--config", "/dev/null"],
        capture_output=True, text=True, timeout=30,
    )


def snapshot(store):
    return {
        table: [tuple(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("nodes", "edges", "activity_log")
    }


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_cli_transfer_preserves_lifecycle_without_importing_authority(tmp_path, fmt):
    source_dir, dest_dir = tmp_path / "source", tmp_path / "dest"
    source = Store(Config(data_dir=str(source_dir)))
    for node_id, status, extra in (
        ("retired", "superseded", {"superseded_by": "current"}),
        ("expired", "archived", {"expires": "2020-01-01", "expired_at": "2020-01-02"}),
        ("stale", "active", {"referent_stale": {"reason": "digest-mismatch"}}),
        ("current", "active", {}),
    ):
        source.add_node(
            "Shared title", node_id=node_id, content=f"Claim {node_id}",
            status=status, extra=extra, audience="team",
            prov_source="https://github.com/wandercom/kindex/blob/main/README.md",
            referent={"url": "https://example.com/evidence", "content_digest": "a" * 64},
            asserted_at="2026-01-01T00:00:00Z", true_of="2025-12-31T00:00:00Z",
        )
        source.verify_node(node_id, verified_by="source-reviewer", prov_method="human review",
                           verified_at="2026-01-02T00:00:00Z",
                           valid_at="2026-01-01T00:00:00Z",
                           invalid_at="2026-02-01T00:00:00Z" if node_id == "retired" else None)
    source.add_edge("current", "retired", "supersedes", bidirectional=False)
    source.add_edge("stale", "current", "contradicts", bidirectional=False)
    originals = {n["id"]: n for n in source.all_nodes()}
    source.close()

    exported = cli(source_dir, "export", "--audience", "private", "--format", fmt)
    assert exported.returncode == 0, exported.stderr
    transfer = tmp_path / f"graph.{fmt}"
    transfer.write_text(exported.stdout)
    imported = cli(dest_dir, "import", str(transfer))
    assert imported.returncode == 0, imported.stderr
    dest = Store(Config(data_dir=str(dest_dir)))
    assert set(dest.node_ids()) == set(originals)
    for node in dest.all_nodes():
        original = originals[node["id"]]
        for field in ("content", "status", "audience", "prov_source", "referent",
                      "asserted_at", "true_of", "valid_at", "invalid_at", "created_at", "updated_at"):
            assert node[field] == original[field], field
        assert node["verified_at"] is None
        assert node["verified_by"] is None
        assert node["prov_method"] is None
        for key, value in original["extra"].items():
            assert node["extra"][key] == value
        assert node["extra"]["imported_verification"]["verified_by"] == "source-reviewer"
        assert not node_trust_decision(dest, node).eligible
    assert [(e["to_id"], e["type"]) for e in dest.edges_from("stale")] == [("current", "contradicts")]
    before = snapshot(dest)
    replay = cli(dest_dir, "import", str(transfer))
    assert replay.returncode == 0, replay.stderr
    assert snapshot(dest) == before
    dest.close()


def test_cli_import_conflict_is_atomic_and_does_not_concatenate(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("Existing", content="Original claim", node_id="existing")
    before = snapshot(store)
    transfer = tmp_path / "graph.json"
    transfer.write_text(json.dumps([
        {"id": "new", "title": "New"},
        {"id": "existing", "title": "Existing", "content": "Opposite claim"},
    ]))
    result = cli(data_dir, "import", str(transfer))
    assert result.returncode != 0
    assert "conflict" in result.stderr.lower()
    assert snapshot(store) == before
    store.close()


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_edge_hydration_and_legacy_replay_do_not_clear_state(tmp_path, mode):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("First", node_id="a", content="Keep this", status="archived",
                   extra={"expires": "2020-01-01"})
    store.add_node("Second", node_id="b")
    before = [tuple(row) for row in store.conn.execute("SELECT * FROM nodes ORDER BY id")]
    transfer = tmp_path / "edges.json"
    transfer.write_text(json.dumps([{"id": "a", "title": "First", "edges": [{"to": "b"}]}]))
    result = cli(data_dir, "import", str(transfer), "--mode", mode)
    assert result.returncode == 0, result.stderr
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM nodes ORDER BY id")] == before
    assert len(store.edges_from("a")) == len(store.edges_from("b")) == 1
    before = snapshot(store)
    assert cli(data_dir, "import", str(transfer), "--mode", mode).returncode == 0
    assert snapshot(store) == before
    store.close()


def test_legacy_title_ids_forward_edges_and_missing_clocks_are_stable(tmp_path):
    data_dir = tmp_path / "data"
    transfer = tmp_path / "legacy.json"
    transfer.write_text(json.dumps([
        {"title": "First", "edges": [{"to": "Second"}],
         "referent": {"url": "https://example.com", "content_digest": "a" * 64}},
        {"title": "Second"},
    ]))
    assert cli(data_dir, "import", str(transfer)).returncode == 0
    store = Store(Config(data_dir=str(data_dir)))
    before = snapshot(store)
    assert len(store.node_ids()) == 2
    assert all(n["asserted_at"] is None and n["true_of"] is None for n in store.all_nodes())
    assert cli(data_dir, "import", str(transfer)).returncode == 0
    assert snapshot(store) == before
    exported = cli(data_dir, "export", "--audience", "private", "--format", "json")
    assert exported.returncode == 0, exported.stderr
    transfer.write_text(exported.stdout)
    replay = cli(tmp_path / "second-dest", "import", str(transfer))
    assert replay.returncode == 0, replay.stderr
    store.close()


@pytest.mark.parametrize("invalid", [
    {"audience": "unknown"}, {"extra": {"expires": "2026-99-01"}},
    {"invalid_at": "yesterday"}, {"weight": float("nan")},
    {"referent": {"path": "missing-digest"}}, {"edges": [{"to": "absent"}]},
    {"extra": {"referent_stale": "nope"}}, {"edges": "not-a-list"},
])
def test_invalid_import_rolls_back_every_record(tmp_path, invalid):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    before = snapshot(store)
    transfer = tmp_path / "bad.json"
    transfer.write_text(json.dumps([{"id": "first", "title": "First"},
                                   {"id": "second", "title": "Second", **invalid}]))
    result = cli(data_dir, "import", str(transfer))
    assert result.returncode != 0
    assert snapshot(store) == before
    store.close()


@pytest.mark.parametrize("incoming", [
    {"status": "active"}, {"audience": "public"}, {"invalid_at": None},
    {"invalid_at": "2027-01-01T00:00:00Z"}, {"valid_at": None},
    {"invalid_at": "2020-01-01T00:00:00.1Z"},
    {"extra": {"expires": "2027-01-01"}}, {"extra": {"referent_stale": {}}},
])
def test_replace_cannot_revive_or_publish_old_claims(tmp_path, incoming):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("Old", node_id="old", status="archived", audience="private",
                   extra={"expires": "2020-01-01", "referent_stale": {"reason": "digest-mismatch"}})
    store.verify_node("old", verified_by="reviewer", prov_method="inspection",
                      valid_at="2019-01-01T00:00:00Z", invalid_at="2020-01-01T00:00:00Z")
    before = snapshot(store)
    transfer = tmp_path / "replace.json"
    transfer.write_text(json.dumps([{"id": "old", **incoming}]))
    assert cli(data_dir, "import", str(transfer), "--mode", "replace").returncode != 0
    assert snapshot(store) == before
    store.close()


def test_replacement_revokes_local_verification_and_dry_run_is_read_only(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("Claim", node_id="claim", content="Original")
    store.verify_node("claim", verified_by="reviewer", prov_method="inspection")
    before = snapshot(store)
    transfer = tmp_path / "replace.json"
    transfer.write_text(json.dumps([{"id": "claim", "content": "Replacement"}]))
    assert cli(data_dir, "import", str(transfer), "--mode", "replace", "--dry-run").returncode == 0
    assert snapshot(store) == before
    assert cli(data_dir, "import", str(transfer), "--mode", "replace").returncode == 0
    node = store.get_node("claim")
    assert node["content"] == "Replacement"
    assert node["verified_at"] is None
    assert not node_trust_decision(store, node).eligible
    store.close()


@pytest.mark.parametrize("audience", ["org", "public"])
def test_shared_exports_preserve_urls_but_not_private_metadata(tmp_path, audience):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.add_node("Private", node_id="hidden-id", audience="private")
    store.add_node(
        "Published", node_id="published", audience="public", status="superseded",
        prov_who=["private-actor"], prov_activity="private-actor capture", intent="private-actor thought",
        prov_source="https://github.com/wandercom/kindex/blob/main/README.md",
        referent={"path": "/Users/private-actor/work/code.py", "content_digest": "a" * 64},
        extra={"superseded_by": "hidden-id", "actor": "private-actor",
               "action_command": "echo never-import-this", "lock": {"agent": "private-actor"},
               "referent_stale": {"reason": "digest-mismatch", "actor": "private-actor"},
               "imported_verification": {"verified_by": "private-actor", "prov_method": "private-actor inspected"}},
    )
    store.verify_node("published", verified_by="private-actor", prov_method="private-actor inspected")
    store.add_edge("published", "hidden-id")
    store.close()
    exported = cli(data_dir, "export", "--audience", audience, "--format", "json")
    assert exported.returncode == 0, exported.stderr
    assert "private-actor" not in exported.stdout
    assert "hidden-id" not in exported.stdout
    assert "never-import-this" not in exported.stdout
    record, = json.loads(exported.stdout)
    assert record["prov_source"] == "https://github.com/wandercom/kindex/blob/main/README.md"
    assert record["referent"]["content_digest"] == "a" * 64
    assert record["referent"]["path_redacted"]
    transfer = tmp_path / "public.json"
    transfer.write_text(exported.stdout)
    assert cli(tmp_path / "dest", "import", str(transfer)).returncode == 0
    dest = Store(Config(data_dir=str(tmp_path / "dest")))
    node = dest.get_node("published")
    assert node["status"] == "superseded"
    assert node["referent"] is None
    assert node["extra"]["imported_referent"]["content_digest"] == "a" * 64
    dest.close()


def test_import_cannot_install_runtime_controls_or_forge_verification(tmp_path):
    from kindex.graph_transfer import import_records

    store = Store(Config(data_dir=str(tmp_path / "data")))
    import_records(store, [{
        "id": "claim", "title": "Claim", "verified_by": "arbitrary-imported-user",
        "verified_at": "2026-01-01T00:00:00Z", "prov_method": "claimed inspection",
        "extra": {"actor": "arbitrary-imported-user", "lock": {"agent": "attacker"},
                  "action_command": "echo never-run", "task_status": "active"},
    }])
    node = store.get_node("claim")
    assert set(node["extra"]) == {"imported_verification"}
    assert node_trust_decision(store, node).reason == "unverified"
    store.close()


def test_import_audit_failure_rolls_back_nodes_and_edges(tmp_path, monkeypatch):
    from kindex.graph_transfer import import_records

    store = Store(Config(data_dir=str(tmp_path / "data")))
    before = snapshot(store)
    log = store._log_in_transaction

    def fail_on_edges(conn, action, *args, **kwargs):
        if action == "import_edges":
            raise RuntimeError("simulated audit failure")
        log(conn, action, *args, **kwargs)

    monkeypatch.setattr(store, "_log_in_transaction", fail_on_edges)
    with pytest.raises(RuntimeError, match="audit failure"):
        import_records(store, [{"id": "a", "title": "A", "edges": [{"to": "b"}]},
                               {"id": "b", "title": "B"}])
    assert snapshot(store) == before
    store.close()


def test_export_does_not_silently_truncate_large_graphs(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(Config(data_dir=str(data_dir)))
    store.conn.executemany("INSERT INTO nodes (id, title) VALUES (?, ?)",
                           ((f"node-{i}", f"Node {i}") for i in range(10001)))
    store.conn.commit()
    store.close()
    exported = cli(data_dir, "export", "--audience", "private", "--format", "json")
    assert exported.returncode == 0, exported.stderr
    assert len(json.loads(exported.stdout)) == 10001
