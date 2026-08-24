"""Tests for Store.edit_node / supersede_node, edit policy, and profile stamp."""

import re

import pytest

from kindex.config import Config
from kindex.schema import EDIT_POLICY, edit_class_for
from kindex.store import (
    RESERVED_EXTRA_KEYS,
    EditPolicyError,
    LockHeldError,
    ProfileMismatchError,
    Store,
    node_expired,
)


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


# ── Policy resolution ──────────────────────────────────────────────────


class TestEditClassFor:
    def test_editable_types(self):
        for t in EDIT_POLICY["editable"]:
            assert edit_class_for(t) == "editable"

    def test_additive_types(self):
        for t in EDIT_POLICY["additive"]:
            assert edit_class_for(t) == "additive"

    def test_managed_types(self):
        for t in EDIT_POLICY["managed"]:
            assert edit_class_for(t) == "managed"

    def test_unknown_defaults_editable(self):
        assert edit_class_for("widget") == "editable"

    def test_override_wins(self):
        assert edit_class_for("concept", {"concept": "additive"}) == "additive"
        assert edit_class_for("decision", {"decision": "editable"}) == "editable"

    def test_invalid_override_raises(self):
        with pytest.raises(ValueError, match="Unknown edit class"):
            edit_class_for("concept", {"concept": "frozen"})


# ── edit_node policy enforcement ───────────────────────────────────────


class TestEditPolicy:
    def test_editable_full_edit(self, store):
        nid = store.add_node("Old title", content="Old content",
                             node_type="concept", domains=["a", "b"])
        node = store.edit_node(nid, title="New title", content="New content",
                               add_tags=["c"], remove_tags=["a"],
                               intent="testing", aka=["nt"])
        assert node["title"] == "New title"
        assert node["content"] == "New content"
        assert set(node["domains"]) == {"b", "c"}
        assert node["intent"] == "testing"
        # Renames auto-preserve the old title as an alias (union with the
        # explicit aka) so title-keyed dedup keeps matching the node.
        assert node["aka"] == ["nt", "Old title"]

    def test_additive_refuses_replace(self, store):
        nid = store.add_node("We chose X", node_type="decision")
        with pytest.raises(EditPolicyError, match="additive"):
            store.edit_node(nid, title="We chose Y")
        with pytest.raises(EditPolicyError, match="additive"):
            store.edit_node(nid, content="rewritten")
        with pytest.raises(EditPolicyError, match="additive"):
            store.edit_node(nid, add_tags=["t"])

    def test_additive_allows_append_and_expires(self, store):
        nid = store.add_node("Never push to main", content="The rule.",
                             node_type="constraint")
        node = store.edit_node(nid, actor="alice", append="Clarified scope.",
                               expires="2030-01-01")
        assert "Clarified scope." in node["content"]
        assert node["extra"]["expires"] == "2030-01-01"
        assert node["title"] == "Never push to main"  # unchanged

    def test_managed_always_refused(self, store):
        for node_type in ("task", "session"):
            nid = store.add_node(f"A {node_type}", node_type=node_type)
            with pytest.raises(EditPolicyError, match="managed"):
                store.edit_node(nid, append="nope")

    def test_policy_overrides_param(self, store):
        nid = store.add_node("A concept", node_type="concept")
        with pytest.raises(EditPolicyError, match="managed"):
            store.edit_node(nid, title="X", policy_overrides={"concept": "managed"})

    def test_no_fields_raises(self, store):
        nid = store.add_node("N")
        with pytest.raises(ValueError, match="at least one field"):
            store.edit_node(nid)

    def test_missing_node_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.edit_node("nonexistent", title="X")


# ── Diffs in activity log ──────────────────────────────────────────────


class TestEditDiffs:
    def _latest_edit_log(self, store):
        entries = [e for e in store.recent_activity(20) if e["action"] == "edit_node"]
        assert entries, "no edit_node activity logged"
        return entries[0]

    def test_diffs_logged(self, store):
        nid = store.add_node("Before", content="old text", node_type="concept")
        store.edit_node(nid, actor="alice", title="After", content="new text")
        entry = self._latest_edit_log(store)
        diffs = entry["details"]["diffs"]
        assert diffs["title"] == {"old": "Before", "new": "After"}
        assert diffs["content"] == {"old": "old text", "new": "new text"}
        assert entry["actor"] == "alice"

    def test_diff_values_truncated(self, store):
        nid = store.add_node("T", content="x" * 2000, node_type="concept")
        store.edit_node(nid, content="y" * 2000)
        diffs = self._latest_edit_log(store)["details"]["diffs"]
        assert len(diffs["content"]["old"]) == 500
        assert len(diffs["content"]["new"]) == 500

    def test_unchanged_fields_not_in_diffs(self, store):
        nid = store.add_node("Same", content="keep", node_type="concept")
        store.edit_node(nid, title="Same", intent="why")
        diffs = self._latest_edit_log(store)["details"]["diffs"]
        assert "title" not in diffs       # value identical -> no diff
        assert "content" not in diffs
        assert diffs["intent"]["new"] == "why"


# ── Re-embedding ───────────────────────────────────────────────────────


class TestEditReembed:
    @pytest.fixture
    def embed_calls(self, monkeypatch):
        calls = []
        import kindex.vectors as vectors
        monkeypatch.setattr(vectors, "is_available", lambda: True)
        monkeypatch.setattr(
            vectors, "upsert_embedding",
            lambda store, node_id, text: calls.append((node_id, text)) or True,
        )
        return calls

    def _drain(self, store):
        from kindex.vectors import drain_embedding_queue
        return drain_embedding_queue(store)

    def test_reembed_on_content_change(self, store, embed_calls):
        nid = store.add_node("Title", content="body", node_type="concept")
        self._drain(store)        # flush the add_node enqueue
        embed_calls.clear()
        store.edit_node(nid, content="new body")
        # Embedding is deferred — nothing runs until the daemon drains the queue.
        assert embed_calls == []
        self._drain(store)
        assert embed_calls == [(nid, "Title new body")]

    def test_reembed_on_append(self, store, embed_calls):
        nid = store.add_node("D", content="base", node_type="decision")
        self._drain(store)
        embed_calls.clear()
        store.edit_node(nid, append="addendum text")
        self._drain(store)
        assert len(embed_calls) == 1
        assert embed_calls[0][0] == nid
        assert "addendum text" in embed_calls[0][1]

    def test_no_reembed_on_tag_only_change(self, store, embed_calls):
        nid = store.add_node("Tagged", node_type="concept")
        self._drain(store)
        embed_calls.clear()
        store.edit_node(nid, add_tags=["new-tag"])
        self._drain(store)
        assert embed_calls == []

    def test_embed_failure_does_not_break_edit(self, store, monkeypatch):
        import kindex.vectors as vectors
        monkeypatch.setattr(vectors, "is_available", lambda: True)

        def boom(*a, **kw):
            raise RuntimeError("embed exploded")

        monkeypatch.setattr(vectors, "upsert_embedding", boom)
        nid = store.add_node("Robust", content="a", node_type="concept")
        node = store.edit_node(nid, content="b")
        assert node["content"] == "b"
        # A provider that raises during the deferred drain must not propagate.
        vectors.drain_embedding_queue(store)


# ── Reserved extra keys ────────────────────────────────────────────────


class TestReservedKeys:
    def test_constant_contents(self):
        assert RESERVED_EXTRA_KEYS == {
            "claim", "lock", "coord_status", "session_status", "task_status",
            "current_state", "messages", "members", "resources",
            "inject_messages",
            # R0 staleness demotion marker (PRD lineage-grounding): written
            # by the referent sweep / bind_referent only; a generic edit
            # must not fabricate or clear a demotion.
            "referent_stale",
        }

    def test_expires_merge_preserves_reserved_keys(self, store):
        nid = store.add_node(
            "Guarded", node_type="watch",
            extra={"lock": {"agent": "a"}, "current_state": {"k": 1},
                   "owner": "me"},
        )
        node = store.edit_node(nid, actor="a", expires="2031-06-01")
        assert node["extra"]["lock"] == {"agent": "a"}
        assert node["extra"]["current_state"] == {"k": 1}
        assert node["extra"]["owner"] == "me"
        assert node["extra"]["expires"] == "2031-06-01"


# ── Lock enforcement on edit ───────────────────────────────────────────


class TestEditLocks:
    def test_foreign_lock_refused(self, store):
        from kindex.locks import lock_node
        nid = store.add_node("Locked", node_type="concept")
        lock_node(store, nid, "agent-a")
        with pytest.raises(LockHeldError, match="agent-a"):
            store.edit_node(nid, actor="agent-b", title="steal")

    def test_holder_may_edit(self, store):
        from kindex.locks import lock_node
        nid = store.add_node("Mine", node_type="concept")
        lock_node(store, nid, "agent-a")
        node = store.edit_node(nid, actor="agent-a", title="updated")
        assert node["title"] == "updated"

    def test_force_overrides_lock(self, store):
        from kindex.locks import lock_node
        nid = store.add_node("Contested", node_type="concept")
        lock_node(store, nid, "agent-a")
        node = store.edit_node(nid, actor="agent-b", force=True, title="forced")
        assert node["title"] == "forced"

    def test_expired_lock_ignored(self, store):
        nid = store.add_node("Stale", node_type="concept")
        store.atomic_extra_update(nid, lambda e: e.update({"lock": {
            "agent": "ghost", "acquired_at": "2020-01-01T00:00:00",
            "expires_at": "2020-01-01T01:00:00", "note": ""}}) or None)
        node = store.edit_node(nid, actor="agent-b", title="fresh")
        assert node["title"] == "fresh"

    def test_anonymous_actor_blocked_by_lock(self, store):
        from kindex.locks import lock_node
        nid = store.add_node("Held", node_type="concept")
        lock_node(store, nid, "agent-a")
        with pytest.raises(LockHeldError):
            store.edit_node(nid, title="anon edit")


# ── Append format ──────────────────────────────────────────────────────


class TestAppendFormat:
    def test_append_with_actor(self, store):
        nid = store.add_node("Base node", content="Base", node_type="concept")
        node = store.edit_node(nid, actor="alice", append="More")
        assert re.fullmatch(
            r"Base\n\n\[addendum \d{4}-\d{2}-\d{2} \d{2}:\d{2} alice\]\nMore",
            node["content"],
        )

    def test_append_without_actor(self, store):
        nid = store.add_node("B", content="Base", node_type="concept")
        node = store.edit_node(nid, append="More")
        assert re.fullmatch(
            r"Base\n\n\[addendum \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\nMore",
            node["content"],
        )

    def test_append_to_empty_content(self, store):
        nid = store.add_node("Empty", content="", node_type="concept")
        node = store.edit_node(nid, append="First note")
        assert re.fullmatch(
            r"\[addendum \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\nFirst note",
            node["content"],
        )

    def test_two_appends_stack(self, store):
        nid = store.add_node("Stack", content="Base", node_type="decision")
        store.edit_node(nid, append="one")
        node = store.edit_node(nid, append="two")
        assert node["content"].count("[addendum") == 2
        assert node["content"].endswith("two")


# ── Expiry validation + node_expired ───────────────────────────────────


class TestExpiry:
    def test_valid_expires(self, store):
        nid = store.add_node("E", node_type="watch")
        node = store.edit_node(nid, expires="2030-12-01")
        assert node["extra"]["expires"] == "2030-12-01"

    @pytest.mark.parametrize("bad", [
        "tomorrow", "2030-13-01", "2030-02-30", "2030-1-1",
        "2030/12/01", "20301201", "2030-12-01T00:00:00",
    ])
    def test_invalid_expires(self, store, bad):
        nid = store.add_node("E2", node_type="watch")
        with pytest.raises(ValueError, match="expires"):
            store.edit_node(nid, expires=bad)

    def test_node_expired(self):
        assert node_expired({"extra": {}}) is False
        assert node_expired({"extra": {"expires": "2099-01-01"}}) is False
        assert node_expired({"extra": {"expires": "2020-01-01"}}) is True
        # expiring today is still live (matches active_watches semantics)
        assert node_expired({"extra": {"expires": "2026-06-11"}},
                            today="2026-06-11") is False
        assert node_expired({"extra": {"expires": "2026-06-10"}},
                            today="2026-06-11") is True
        assert node_expired({}) is False


# ── supersede_node ─────────────────────────────────────────────────────


class TestSupersede:
    def test_supersede_basics(self, store):
        old_id = store.add_node(
            "Old decision", content="We chose X.", node_type="decision",
            domains=["arch"], intent="pick a db", audience="team", weight=0.8,
        )
        new = store.supersede_node(old_id, "We now choose Y because Z.",
                                   actor="alice", reason="X deprecated")
        assert new["id"] != old_id
        assert new["type"] == "decision"
        assert new["domains"] == ["arch"]
        assert new["intent"] == "pick a db"
        assert new["audience"] == "team"
        assert new["status"] == "active"
        assert new["content"] == "We now choose Y because Z."
        assert new["extra"]["supersedes"] == old_id

        old = store.get_node(old_id)
        assert old["status"] == "superseded"
        assert old["extra"]["superseded_by"] == new["id"]

    def test_supersedes_edge_created(self, store):
        old_id = store.add_node("Rule v1", node_type="constraint")
        new = store.supersede_node(old_id, "Rule v2")
        edges = store.edges_from(new["id"])
        assert any(e["to_id"] == old_id and e["type"] == "supersedes"
                   for e in edges)

    def test_pheromone_migrated(self, store):
        old_id = store.add_node("Trail", node_type="concept")
        store.deposit_pheromone(old_id, context="", amount=1.5)
        store.deposit_pheromone(old_id, context="projx", amount=0.7)
        new = store.supersede_node(old_id, "Trail v2")
        rows = store.conn.execute(
            "SELECT node_id, context FROM injection_pheromone "
            "WHERE node_id IN (?, ?)", (old_id, new["id"]),
        ).fetchall()
        assert {(r["node_id"], r["context"]) for r in rows} == {
            (new["id"], ""), (new["id"], "projx"),
        }

    def test_expires_on_new_node(self, store):
        old_id = store.add_node("W", node_type="watch")
        new = store.supersede_node(old_id, "W2", expires="2030-01-01")
        assert new["extra"]["expires"] == "2030-01-01"

    def test_invalid_expires_rejected(self, store):
        old_id = store.add_node("W", node_type="watch")
        with pytest.raises(ValueError, match="expires"):
            store.supersede_node(old_id, "W2", expires="soon")
        assert store.get_node(old_id)["status"] == "active"  # nothing happened

    def test_empty_text_rejected(self, store):
        old_id = store.add_node("N", node_type="concept")
        with pytest.raises(ValueError, match="new_text"):
            store.supersede_node(old_id, "   ")

    def test_missing_node_raises(self, store):
        with pytest.raises(KeyError):
            store.supersede_node("nope", "text")

    def test_lock_refusal_and_force(self, store):
        from kindex.locks import lock_node
        old_id = store.add_node("Held", node_type="concept")
        lock_node(store, old_id, "agent-a")
        with pytest.raises(LockHeldError):
            store.supersede_node(old_id, "takeover", actor="agent-b")
        new = store.supersede_node(old_id, "takeover", actor="agent-b",
                                   force=True)
        assert new["extra"]["supersedes"] == old_id

    def test_superseded_excluded_from_active_queries(self, store):
        old_id = store.add_node("Active rule", node_type="constraint")
        store.supersede_node(old_id, "Replacement rule")
        active = store.all_nodes(node_type="constraint", status="active")
        ids = {n["id"] for n in active}
        assert old_id not in ids
        assert all(c["id"] != old_id for c in store.active_constraints())


# ── Profile stamp ──────────────────────────────────────────────────────


class _ProfiledConfig(Config):
    """Stage-2 Config will add active_profile; Store reads it via getattr."""
    active_profile: str | None = None


class TestProfileStamp:
    def test_stamp_on_first_open(self, tmp_path):
        s = Store(_ProfiledConfig(data_dir=str(tmp_path), active_profile="work"))
        s.add_node("hello")
        assert s.get_meta("kin_profile") == "work"
        s.close()

    def test_mismatch_raises(self, tmp_path):
        s = Store(_ProfiledConfig(data_dir=str(tmp_path), active_profile="work"))
        s.add_node("hello")
        s.close()
        s2 = Store(_ProfiledConfig(data_dir=str(tmp_path),
                                   active_profile="personal"))
        with pytest.raises(ProfileMismatchError) as exc:
            s2.stats()
        msg = str(exc.value)
        assert "work" in msg and "personal" in msg
        assert str(s2.db_path) in msg

    def test_matching_profile_reopens(self, tmp_path):
        s = Store(_ProfiledConfig(data_dir=str(tmp_path), active_profile="work"))
        nid = s.add_node("persisted")
        s.close()
        s2 = Store(_ProfiledConfig(data_dir=str(tmp_path),
                                   active_profile="work"))
        assert s2.get_node(nid)["title"] == "persisted"
        s2.close()

    def test_no_profile_no_stamp(self, tmp_path):
        s = Store(Config(data_dir=str(tmp_path)))
        s.add_node("legacy")
        assert s.get_meta("kin_profile") is None
        s.close()

    def test_legacy_config_opens_stamped_db(self, tmp_path):
        s = Store(_ProfiledConfig(data_dir=str(tmp_path), active_profile="work"))
        s.add_node("stamped")
        s.close()
        s2 = Store(Config(data_dir=str(tmp_path)))
        assert s2.stats()["nodes"] == 1  # no expected profile -> no check
        s2.close()
