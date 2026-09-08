"""Regression tests for the supersede lifecycle fix cluster.

Covers: superseded nodes excluded from retrieval (fts/hybrid/prime),
embedding cleanup on supersede/delete, vacuum sweep of superseded nodes,
status guards (double-supersede, archived), the managed-type policy gate
on supersede, title-rename aka preservation, single changelog entry per
edit, and write_kin_index slug/git-failure hardening.
"""

from __future__ import annotations

import json
import types

import pytest

from kindex.config import Config
from kindex.store import EditPolicyError, Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


def _make_plain_vector_table(store):
    """Simulate node_vectors without sqlite-vec (plain table, same shape)."""
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS node_vectors "
        "(node_id TEXT PRIMARY KEY, embedding BLOB)"
    )
    store.conn.commit()


# ── idx 24: superseded nodes stop surfacing in retrieval ───────────────


class TestSupersededExcludedFromRetrieval:
    def test_fts_search_excludes_superseded(self, store):
        old_id = store.add_node("Token cache policy",
                                content="Use Redis db 9 for token cache")
        new = store.supersede_node(old_id, "Token cache policy: use Postgres")
        ids = {r["id"] for r in store.fts_search("token cache policy")}
        assert old_id not in ids
        assert new["id"] in ids

    def test_fts_like_fallback_excludes_superseded(self, store):
        old_id = store.add_node("Fallback target",
                                content="fallbackphrase original")
        new = store.supersede_node(old_id,
                                   "Fallback target fallbackphrase v2")
        # Force the LIKE fallback: dropping the FTS table makes the MATCH
        # query raise OperationalError (no writes happen after this).
        store.conn.execute("DROP TABLE IF EXISTS nodes_fts")
        store.conn.commit()
        ids = {r["id"] for r in store.fts_search("fallbackphrase")}
        assert old_id not in ids
        assert new["id"] in ids

    def test_hybrid_search_excludes_superseded_surfaces_replacement(self, store):
        from kindex.retrieve import hybrid_search

        old_id = store.add_node("OAuth token rotation",
                                content="Rotate every 90 days")
        new = store.supersede_node(
            old_id, "OAuth token rotation: rotate every 30 days")
        results = hybrid_search(store, "OAuth token rotation")
        ids = {r["id"] for r in results}
        assert old_id not in ids
        assert new["id"] in ids

    def test_hybrid_search_follows_successor_via_graph_expansion(self, store):
        """A superseded node pulled in by graph expansion swaps to its
        successor when the successor isn't a candidate of its own."""
        from kindex.retrieve import hybrid_search

        hub = store.add_node("Quantum widgets hub", content="hub of widgets")
        old_id = store.add_node("Quantum widget spec", content="old spec")
        store.add_edge(hub, old_id, edge_type="relates_to", weight=0.8)
        new = store.supersede_node(
            old_id, "Completely different replacement zebra text")
        results = hybrid_search(store, "quantum widgets")
        ids = [r["id"] for r in results]
        assert old_id not in ids
        assert new["id"] in ids
        assert len(ids) == len(set(ids))  # no duplicates from chain-follow

    def test_prime_context_excludes_superseded_text(self, store):
        from kindex.hooks import prime_context

        old_id = store.add_node("Token storage", content="Use Redis db 9")
        store.supersede_node(old_id, "Token storage: use Postgres now")
        out = prime_context(store, topic="token storage")
        assert "Redis db 9" not in out
        assert "Postgres" in out

    def test_supersede_deletes_old_embedding(self, store, monkeypatch):
        import kindex.vectors as vectors

        deleted = []
        monkeypatch.setattr(
            vectors, "delete_embedding",
            lambda s, nid: deleted.append(nid) or True,
        )
        old_id = store.add_node("Embedded", content="text")
        store.supersede_node(old_id, "Embedded v2")
        assert deleted == [old_id]

    def test_delete_embedding_helper(self, store):
        from kindex.vectors import delete_embedding

        _make_plain_vector_table(store)
        store.conn.execute(
            "INSERT INTO node_vectors (node_id, embedding) VALUES (?, ?)",
            ("n1", b"\x00\x01"),
        )
        store.conn.commit()
        assert delete_embedding(store, "n1") is True
        row = store.conn.execute(
            "SELECT * FROM node_vectors WHERE node_id = 'n1'").fetchone()
        assert row is None
        assert delete_embedding(store, "n1") is False  # already gone

    def test_delete_embedding_no_table_is_safe(self, store):
        from kindex.vectors import delete_embedding

        assert delete_embedding(store, "whatever") is False


# ── idx 26: superseded nodes vacuum to the slow graph ──────────────────


class TestSupersededVacuum:
    def _age_and_fade(self, store, nid):
        store.conn.execute(
            "UPDATE nodes SET weight = 0.02, "
            "updated_at = '2025-01-01T00:00:00' WHERE id = ?", (nid,))
        store.conn.commit()

    def test_find_archivable_includes_superseded(self, store):
        from kindex.archive import find_archivable_nodes

        old_id = store.add_node("Faded rule", node_type="constraint")
        store.supersede_node(old_id, "Fresh rule")
        self._age_and_fade(store, old_id)
        assert old_id in find_archivable_nodes(store)

    def test_archive_cycle_sweeps_superseded(self, tmp_path):
        from kindex.archive import archive_cycle

        cfg = Config(data_dir=str(tmp_path))
        store = Store(cfg)
        try:
            old_id = store.add_node("Sweep me", node_type="concept")
            store.supersede_node(old_id, "Sweep me v2")
            self._age_and_fade(store, old_id)
            count = archive_cycle(cfg, store)
            assert count >= 1
            assert store.get_node(old_id) is None  # gone from fast graph
        finally:
            store.close()

    def test_delete_node_drops_embedding(self, store):
        _make_plain_vector_table(store)
        nid = store.add_node("Vec node", content="x")
        store.conn.execute(
            "INSERT INTO node_vectors (node_id, embedding) VALUES (?, ?)",
            (nid, b"\x00"),
        )
        store.conn.commit()
        store.delete_node(nid)
        row = store.conn.execute(
            "SELECT * FROM node_vectors WHERE node_id = ?", (nid,)).fetchone()
        assert row is None


# ── idx 29: status guards on edit/supersede ────────────────────────────


class TestStatusGuards:
    def test_double_supersede_raises_naming_successor(self, store):
        a = store.add_node("Original", node_type="concept")
        b = store.supersede_node(a, "First replacement")
        with pytest.raises(EditPolicyError, match=b["id"]):
            store.supersede_node(a, "Second replacement")
        # First successor pointer preserved, no competing successor created
        assert store.get_node(a)["extra"]["superseded_by"] == b["id"]
        actives = [n for n in store.all_nodes(status="active")
                   if (n.get("extra") or {}).get("supersedes") == a]
        assert [n["id"] for n in actives] == [b["id"]]

    def test_edit_on_superseded_raises_naming_successor(self, store):
        a = store.add_node("Old fact", node_type="concept")
        b = store.supersede_node(a, "New fact")
        with pytest.raises(EditPolicyError, match=b["id"]):
            store.edit_node(a, title="zombie edit")
        # force does NOT bypass the superseded guard
        with pytest.raises(EditPolicyError, match=b["id"]):
            store.edit_node(a, title="zombie edit", force=True)

    def test_archived_refused_without_force(self, store):
        a = store.add_node("Archived one", node_type="concept")
        store.update_node(a, status="archived")
        with pytest.raises(EditPolicyError, match="archived"):
            store.edit_node(a, title="nope")
        with pytest.raises(EditPolicyError, match="archived"):
            store.supersede_node(a, "nope")

    def test_archived_allowed_with_force(self, store):
        a = store.add_node("Archived two", node_type="concept")
        store.update_node(a, status="archived")
        node = store.edit_node(a, title="revived", force=True)
        assert node["title"] == "revived"

        b = store.add_node("Archived three", node_type="concept")
        store.update_node(b, status="archived")
        new = store.supersede_node(b, "Replacement of archived", force=True)
        assert new["extra"]["supersedes"] == b

    def test_status_recheck_inside_transaction(self, store, monkeypatch):
        """A stale pre-supersede read must not slip past the outer guard:
        the in-transaction re-SELECT catches the flipped status."""
        a = store.add_node("Race target", node_type="concept")
        stale = dict(store.get_node(a))  # snapshot while still active
        b = store.supersede_node(a, "Winner replacement")

        real_get = store.get_node
        monkeypatch.setattr(
            store, "get_node",
            lambda nid: dict(stale) if nid == a else real_get(nid),
        )
        with pytest.raises(EditPolicyError, match=b["id"]):
            store.supersede_node(a, "Loser replacement")
        monkeypatch.undo()
        # No competing successor was inserted
        successors = [n for n in store.all_nodes(status="active")
                      if (n.get("extra") or {}).get("supersedes") == a]
        assert [n["id"] for n in successors] == [b["id"]]


# ── idx 4: managed-type policy gate on supersede ───────────────────────


class TestSupersedePolicyGate:
    @pytest.mark.parametrize("node_type", ["task", "session", "coordination"])
    def test_managed_types_refused(self, store, node_type):
        nid = store.add_node(f"A {node_type}", node_type=node_type)
        with pytest.raises(EditPolicyError, match="managed"):
            store.supersede_node(nid, "replacement")
        assert store.get_node(nid)["status"] == "active"  # untouched

    def test_policy_override_honored(self, store):
        nid = store.add_node("A doc", node_type="document")
        with pytest.raises(EditPolicyError, match="managed"):
            store.supersede_node(nid, "replacement",
                                 policy_overrides={"document": "managed"})

    def test_additive_and_editable_still_allowed(self, store):
        for node_type in ("decision", "concept"):
            nid = store.add_node(f"OK {node_type}", node_type=node_type)
            new = store.supersede_node(nid, f"Replaces the {node_type}")
            assert new["extra"]["supersedes"] == nid


# ── idx 32: title rename preserves old title as aka ────────────────────


class TestRenameAkaPreservation:
    def test_rename_keeps_old_title_reachable(self, store):
        nid = store.add_node("Auth flow", content="x", node_type="concept")
        node = store.edit_node(nid, title="OAuth2 + OIDC flow")
        assert "Auth flow" in node["aka"]
        # The title-dedup gate (hooks/daemon/dream_deep) still hits
        found = store.get_node_by_title("Auth flow")
        assert found is not None and found["id"] == nid

    def test_rename_unions_with_explicit_aka(self, store):
        nid = store.add_node("Old name", node_type="concept")
        node = store.edit_node(nid, title="New name", aka=["alias-1"])
        assert set(node["aka"]) == {"alias-1", "Old name"}

    def test_case_only_rename_adds_no_alias(self, store):
        nid = store.add_node("rest api", node_type="concept")
        node = store.edit_node(nid, title="REST API")
        assert node["aka"] == []
        # case-insensitive title match already covers the old spelling
        assert store.get_node_by_title("rest api")["id"] == nid

    def test_chained_renames_accumulate_aliases(self, store):
        nid = store.add_node("Name v1", node_type="concept")
        store.edit_node(nid, title="Name v2")
        node = store.edit_node(nid, title="Name v3")
        assert set(node["aka"]) == {"Name v1", "Name v2"}
        assert store.get_node_by_title("Name v1")["id"] == nid

    def test_existing_alias_not_duplicated(self, store):
        nid = store.add_node("Dup source", aka=["dup source"],
                             node_type="concept")
        node = store.edit_node(nid, title="Renamed dup")
        assert [a.lower() for a in node["aka"]].count("dup source") == 1


# ── idx 6 + 22: single changelog entry per edit ────────────────────────


class TestSingleEditLogEntry:
    def test_one_activity_entry_per_edit(self, store):
        nid = store.add_node("Logged once", content="a", node_type="concept")
        store.edit_node(nid, content="b")
        entries = [e for e in store.recent_activity(50)
                   if e.get("target_id") == nid
                   and e["action"] in ("edit_node", "update_node")]
        assert [e["action"] for e in entries] == ["edit_node"]

    def test_edit_log_carries_node_type(self, store):
        nid = store.add_node("Typed entry", node_type="concept")
        store.edit_node(nid, title="Typed entry v2")
        entry = [e for e in store.recent_activity(20)
                 if e["action"] == "edit_node"][0]
        assert entry["details"]["type"] == "concept"
        assert "diffs" in entry["details"]

    def test_plain_update_node_still_logs(self, store):
        nid = store.add_node("Plain update", node_type="concept")
        store.update_node(nid, weight=0.9)
        actions = [e["action"] for e in store.recent_activity(10)
                   if e.get("target_id") == nid]
        assert "update_node" in actions


# ── idx 16: write_kin_index slug shape + git-failure hardening ─────────


class TestWriteKinIndexHardening:
    def test_exact_slug_shape_filters_collisions(self, store, tmp_path,
                                                 monkeypatch):
        import kindex.ingest as ingest

        mine_mod = "code-mod-api-" + "a" * 12
        mine_sym = "code-sym-api-" + "0123456789ab"
        other_repo = "code-mod-api-server-" + "b" * 12  # hyphen-extension repo
        bad_shape = "code-mod-api-deadbeef"             # not 12 hex chars
        for nid, title in [(mine_mod, "mine.py"), (mine_sym, "Mine.fn"),
                           (other_repo, "secret_payments.py"),
                           (bad_shape, "junk")]:
            store.add_node(title, node_id=nid, node_type="artifact", audience="team")

        monkeypatch.setattr(ingest, "_detect_repo_for_index", lambda d: "api")
        out = tmp_path / "proj"
        out.mkdir()
        path = ingest.write_kin_index(store, out)
        index = json.loads(path.read_text())
        ids = {n["id"] for n in index["nodes"]}
        assert ids == {mine_mod, mine_sym}
        assert index["repo"] == "api"

    def _fail_run(self, monkeypatch, exc=None, rc=None, stderr=""):
        import subprocess

        if exc is not None:
            def fake(*a, **kw):
                raise exc
        else:
            def fake(*a, **kw):
                return types.SimpleNamespace(returncode=rc, stdout="",
                                             stderr=stderr)
        monkeypatch.setattr(subprocess, "run", fake)

    def test_git_error_inside_repo_aborts(self, store, tmp_path, monkeypatch):
        from kindex.ingest import write_kin_index

        repo = tmp_path / "myrepo"
        (repo / ".git").mkdir(parents=True)
        self._fail_run(monkeypatch, exc=FileNotFoundError("git not found"))
        with pytest.raises(RuntimeError, match="refusing"):
            write_kin_index(store, repo)
        assert not (repo / ".kin" / "index.json").exists()

    def test_dubious_ownership_inside_repo_aborts(self, store, tmp_path,
                                                  monkeypatch):
        from kindex.ingest import write_kin_index

        repo = tmp_path / "cirepo"
        (repo / ".git").mkdir(parents=True)
        self._fail_run(monkeypatch, rc=128,
                       stderr="fatal: detected dubious ownership")
        with pytest.raises(RuntimeError, match="dubious ownership"):
            write_kin_index(store, repo)

    def test_git_error_outside_repo_falls_back(self, store, tmp_path,
                                               monkeypatch):
        from kindex.ingest import write_kin_index

        store.add_node("Team note", node_type="concept", audience="team")
        plain = tmp_path / "notarepo"
        plain.mkdir()
        self._fail_run(monkeypatch, exc=FileNotFoundError("git not found"))
        path = write_kin_index(store, plain)
        index = json.loads(path.read_text())
        assert index["repo"] is None  # genuine non-repo: global head is fine

    def test_not_a_repo_rc128_falls_back(self, store, tmp_path, monkeypatch):
        from kindex.ingest import write_kin_index

        plain = tmp_path / "stillnotarepo"
        plain.mkdir()
        self._fail_run(monkeypatch, rc=128,
                       stderr="fatal: not a git repository")
        path = write_kin_index(store, plain)
        assert json.loads(path.read_text())["repo"] is None
