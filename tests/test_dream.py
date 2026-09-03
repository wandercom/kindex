"""Tests for dream module — knowledge consolidation."""

import datetime
import json
import os

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


# ── Similarity functions ──────────────────────────────────────────────


class TestSimilarity:
    def test_title_similarity_identical(self):
        from kindex.dream import title_similarity
        assert title_similarity("hello world", "hello world") == 1.0

    def test_title_similarity_case_insensitive(self):
        from kindex.dream import title_similarity
        assert title_similarity("Hello World", "hello world") == 1.0

    def test_title_similarity_different(self):
        from kindex.dream import title_similarity
        score = title_similarity("alpha beta", "gamma delta")
        assert score < 0.8

    def test_title_similarity_close(self):
        from kindex.dream import title_similarity
        score = title_similarity("Pact testing patterns", "Pact testing pattern")
        assert score > 0.9

    def test_title_similarity_empty(self):
        from kindex.dream import title_similarity
        assert title_similarity("", "hello") == 0.0
        assert title_similarity("hello", "") == 0.0

    def test_content_overlap_identical(self):
        from kindex.dream import content_overlap
        assert content_overlap("some content here", "some content here") == 1.0

    def test_content_overlap_empty(self):
        from kindex.dream import content_overlap
        assert content_overlap("", "hello") == 0.0

    def test_combined_similarity(self):
        from kindex.dream import combined_similarity
        a = {"title": "Pact testing", "content": "Test patterns for Pact"}
        b = {"title": "Pact testing", "content": "Test patterns for Pact"}
        assert combined_similarity(a, b) == 1.0


# ── Find duplicates ──────────────────────────────────────────────────


class TestFindDuplicates:
    def test_finds_near_duplicate_titles(self, store):
        from kindex.dream import find_duplicates
        # Use distinct IDs but very similar titles/content to trigger dedup
        store.add_node("Pact testing patterns overview", node_id="dup-a",
                       content="Patterns for testing with Pact framework")
        store.add_node("Pact testing patterns overview guide", node_id="dup-b",
                       content="Patterns for testing with Pact framework details")

        result = find_duplicates(store)
        assert len(result["merge"]) > 0 or len(result["suggest"]) > 0

    def test_no_duplicates_in_different_nodes(self, store):
        from kindex.dream import find_duplicates
        store.add_node("Alpha concept", content="About alpha")
        store.add_node("Zeta concept", content="About zeta")

        result = find_duplicates(store)
        assert len(result["merge"]) == 0

    def test_skips_protected_types(self, store):
        from kindex.dream import find_duplicates
        store.add_node("Security rule A", content="Rule A", node_type="constraint")
        store.add_node("Security rule A", content="Rule A copy", node_type="constraint")

        result = find_duplicates(store)
        assert len(result["merge"]) == 0
        assert len(result["suggest"]) == 0

    def test_skips_short_titles(self, store):
        from kindex.dream import find_duplicates
        store.add_node("ABC", node_id="short-a", content="Short title")
        store.add_node("ABC", node_id="short-b", content="Another short")

        result = find_duplicates(store)
        # Titles < 4 chars are excluded from bucketing
        assert len(result["merge"]) == 0


# ── Merge nodes ──────────────────────────────────────────────────────


class TestMergeNodes:
    def test_merge_moves_edges(self, store):
        from kindex.dream import merge_nodes
        a = store.add_node("Source node", content="Source content")
        b = store.add_node("Target node", content="Target content")
        c = store.add_node("Connected node", content="Third")
        store.add_edge(a, c, edge_type="relates_to")

        merge_nodes(store, a, b)

        # Source should be archived
        source = store.get_node(a)
        assert source["status"] == "archived"

        # Target should now have edge to c
        edges = store.edges_from(b)
        to_ids = {e["to_id"] for e in edges}
        assert c in to_ids

    def test_merge_archives_source(self, store):
        from kindex.dream import merge_nodes
        a = store.add_node("Source", content="S")
        b = store.add_node("Target", content="T")

        merge_nodes(store, a, b)

        source = store.get_node(a)
        assert source["status"] == "archived"
        assert source["weight"] == 0.01

    def test_merge_combines_content(self, store):
        from kindex.dream import merge_nodes
        a = store.add_node("Source", content="Unique source info")
        b = store.add_node("Target", content="Target info")

        merge_nodes(store, a, b)

        target = store.get_node(b)
        assert "Unique source info" in target["content"]
        assert "Target info" in target["content"]

    def test_merge_boosts_weight(self, store):
        from kindex.dream import merge_nodes
        a = store.add_node("Source", content="S", weight=0.9)
        b = store.add_node("Target", content="T", weight=0.3)

        merge_nodes(store, a, b)

        target = store.get_node(b)
        assert target["weight"] >= 0.9

    def test_merge_nonexistent_returns_false(self, store):
        from kindex.dream import merge_nodes
        b = store.add_node("Target", content="T")
        assert merge_nodes(store, "nonexistent", b) is False


# ── Auto-apply suggestions ───────────────────────────────────────────


class TestAutoApplySuggestions:
    def test_applies_when_titles_similar(self, store):
        from kindex.dream import auto_apply_suggestions
        a = store.add_node("Kindex architecture overview")
        b = store.add_node("Kindex architecture overview details")
        store.add_suggestion(a, b, reason="test", source="test")

        count = auto_apply_suggestions(store)
        assert count >= 1

        # Edge should exist
        edges = store.edges_from(a)
        to_ids = {e["to_id"] for e in edges}
        assert b in to_ids

    def test_skips_when_titles_dissimilar(self, store):
        from kindex.dream import auto_apply_suggestions
        a = store.add_node("Alpha concept")
        b = store.add_node("Zeta completely different")
        store.add_suggestion(a, b, reason="test", source="test")

        count = auto_apply_suggestions(store)
        assert count == 0

    def test_skips_archived_nodes(self, store):
        from kindex.dream import auto_apply_suggestions
        a = store.add_node("Same title", status="archived")
        b = store.add_node("Same title nearby")
        store.add_suggestion(a, b, reason="test", source="test")

        count = auto_apply_suggestions(store)
        assert count == 0


# ── Dream cycles ─────────────────────────────────────────────────────


class TestDreamLightweight:
    def test_runs_on_empty_store(self, config, store):
        from kindex.dream import dream_lightweight
        results = dream_lightweight(config, store)
        assert results["merged"] == 0
        assert results["suggested"] == 0
        assert results["suggestions_applied"] == 0

    def test_dry_run_no_changes(self, config, store):
        from kindex.dream import dream_lightweight
        store.add_node("Duplicate concept here", content="Content A")
        store.add_node("Duplicate concept here", content="Content B")

        results = dream_lightweight(config, store, dry_run=True)
        # Dry run should count but not actually merge
        # Both nodes should still be active
        nodes = store.all_nodes(status="active")
        titles = [n["title"] for n in nodes]
        assert titles.count("Duplicate concept here") == 2

    def test_dedupes_existing_suggestion_outside_recent_window(
        self,
        config,
        store,
        monkeypatch,
    ):
        import kindex.dream as dream

        a = store.add_node("Alpha anchor")
        b = store.add_node("Zeta unrelated bridge")
        sid = store.add_suggestion(a, b, reason="old duplicate", source="test")
        store.conn.execute(
            "UPDATE suggestions SET created_at = '2000-01-01 00:00:00' WHERE id = ?",
            (sid,),
        )
        store.conn.commit()
        for i in range(250):
            store.add_suggestion(f"new-{i}", f"other-{i}", reason="newer filler")

        monkeypatch.setattr(
            dream,
            "find_duplicates",
            lambda _store: {"merge": [], "suggest": [(a, b, 0.9)]},
        )

        results = dream.dream_lightweight(config, store)
        row = store.conn.execute(
            """
            SELECT COUNT(*) AS count FROM suggestions
             WHERE (concept_a = ? AND concept_b = ?)
                OR (concept_a = ? AND concept_b = ?)
            """,
            (a, b, b, a),
        ).fetchone()

        assert results["suggested"] == 0
        assert results["suggestion_existing"] == 1
        assert row["count"] == 1

    def test_does_not_recreate_resolved_suggestion(self, config, store, monkeypatch):
        import kindex.dream as dream

        a = store.add_node("Alpha anchor")
        b = store.add_node("Alpha anchored")
        suggestion_id = store.add_suggestion(a, b, source="dream-cycle")
        store.update_suggestion(suggestion_id, "rejected")
        monkeypatch.setattr(
            dream,
            "find_duplicates",
            lambda _store: {"merge": [], "suggest": [(a, b, 0.9)]},
        )

        results = dream.dream_lightweight(config, store)

        assert results["suggested"] == 0
        assert results["suggestion_existing"] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM suggestions"
        ).fetchone()[0] == 1

    def test_caps_new_suggestion_writes(self, config, store, monkeypatch):
        import kindex.dream as dream

        config.reminders.dream_max_new_suggestions = 2
        monkeypatch.setattr(
            dream,
            "find_duplicates",
            lambda _store: {
                "merge": [],
                "suggest": [
                    ("a1", "b1", 0.9),
                    ("a2", "b2", 0.9),
                    ("a3", "b3", 0.9),
                ],
            },
        )

        results = dream.dream_lightweight(config, store)

        assert results["suggested"] == 2
        assert results["suggestion_candidates"] == 3
        assert results["suggestion_cap"] == 2
        assert results["suggestion_capped"] is True


class TestDreamFull:
    def test_runs_on_empty_store(self, config, store):
        from kindex.dream import dream_full
        results = dream_full(config, store)
        assert results["merged"] == 0
        assert results["domain_link_suggestions_created"] == 0
        assert results["domain_link_proposals"] == []


class TestDreamCycle:
    def test_cycle_sets_meta(self, tmp_path):
        from kindex.dream import dream_cycle
        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        results = dream_cycle(cfg, s, mode="lightweight")
        assert results["mode"] == "lightweight"
        assert "timestamp" in results

        last = s.get_meta("last_dream_run")
        assert last is not None
        started = s.get_meta("last_dream_started")
        assert started is not None

        s.close()

    def test_cycle_skips_when_locked(self, tmp_path):
        """Simulate lock contention."""
        import fcntl

        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        # Acquire lock externally
        lock_file = cfg.data_path / "dream.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        from kindex.dream import dream_cycle
        results = dream_cycle(cfg, s, mode="lightweight")
        assert results.get("skipped") == "locked"

        # Cleanup
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        s.close()

    def test_detach_dream_throttles_recent_spawn(self, tmp_path, monkeypatch):
        from kindex.dream import detach_dream

        class FakeProc:
            pid = 12345

        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc()

        cfg = Config(data_dir=str(tmp_path))
        cfg.reminders.dream_min_interval = 3600
        monkeypatch.setattr("kindex.setup._find_kin_path", lambda: "/tmp/kin")
        monkeypatch.setattr("kindex.dream.subprocess.Popen", fake_popen)

        first = detach_dream(cfg, mode="lightweight")
        second = detach_dream(cfg, mode="lightweight")

        assert first["detached"] is True
        assert first["pid"] == 12345
        assert second["detached"] is False
        assert second["skipped"] == "recent"
        assert len(calls) == 1

    def test_detach_dream_force_bypasses_throttle(self, tmp_path, monkeypatch):
        from kindex.dream import detach_dream

        class FakeProc:
            pid = 12345

        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc()

        cfg = Config(data_dir=str(tmp_path))
        cfg.reminders.dream_min_interval = 3600
        monkeypatch.setattr("kindex.setup._find_kin_path", lambda: "/tmp/kin")
        monkeypatch.setattr("kindex.dream.subprocess.Popen", fake_popen)

        detach_dream(cfg, mode="lightweight")
        forced = detach_dream(cfg, mode="lightweight", force=True)

        assert forced["detached"] is True
        assert len(calls) == 2


class TestDreamIdempotent:
    """CD004: Running dream N times produces same result as running once."""

    def test_double_merge_is_idempotent(self, tmp_path):
        from kindex.dream import dream_cycle
        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)

        s.add_node("Identical concept", content="Same stuff")
        s.add_node("Identical concept", content="Same stuff too")

        r1 = dream_cycle(cfg, s, mode="lightweight")
        r2 = dream_cycle(cfg, s, mode="lightweight")

        # Second run should find nothing to merge (first was already archived)
        assert r2.get("merged", 0) == 0
        s.close()


class TestProtectedTypes:
    """CD008: Dream must never modify constraint/directive/checkpoint nodes."""

    def test_constraints_not_merged(self, config, store):
        from kindex.dream import find_duplicates
        store.add_node("Rule A", content="Same rule", node_type="constraint")
        store.add_node("Rule A", content="Same rule", node_type="constraint")

        result = find_duplicates(store)
        assert len(result["merge"]) == 0

    def test_directives_not_merged(self, config, store):
        from kindex.dream import find_duplicates
        store.add_node("Directive X", content="Same", node_type="directive")
        store.add_node("Directive X", content="Same", node_type="directive")

        result = find_duplicates(store)
        assert len(result["merge"]) == 0

    def test_checkpoints_not_merged(self, config, store):
        from kindex.dream import find_duplicates
        store.add_node("Checkpoint Z", content="Same", node_type="checkpoint")
        store.add_node("Checkpoint Z", content="Same", node_type="checkpoint")

        result = find_duplicates(store)
        assert len(result["merge"]) == 0


# ── Domain link proposals ────────────────────────────────────────────


class TestDomainLinkProposals:
    def test_proposes_sparse_representative_links(self, store):
        from kindex.dream import propose_domain_links

        ids = [
            store.add_node(f"Concept {index}", domains=["security"])
            for index in range(5)
        ]

        proposals, capped = propose_domain_links(store, limit=20)

        assert len(proposals) == len(ids) - 1
        assert capped is False
        assert len({proposal["from_id"] for proposal in proposals}) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0

    def test_proposal_budget_is_hard_and_deterministic(self, store):
        from kindex.dream import propose_domain_links

        for index in range(10):
            store.add_node(f"Concept {index}", domains=["security"])

        first, first_capped = propose_domain_links(store, limit=3)
        second, second_capped = propose_domain_links(store, limit=3)

        assert first == second
        assert len(first) == 3
        assert first_capped is second_capped is True

    def test_proposal_budget_round_robins_across_domains(self, store):
        from kindex.dream import propose_domain_links

        for domain in ("alpha", "beta", "gamma"):
            for index in range(3):
                store.add_node(
                    f"{domain} {index}",
                    node_id=f"{domain}-{index}",
                    domains=[domain],
                )

        proposals, capped = propose_domain_links(store, limit=3)

        assert [proposal["domain"] for proposal in proposals] == [
            "alpha",
            "beta",
            "gamma",
        ]
        assert capped is True

    def test_representative_is_stable_when_weights_change(self, store):
        from kindex.dream import propose_domain_links

        store.add_node("First", node_id="a", domains=["security"], weight=0.1)
        store.add_node("Second", node_id="b", domains=["security"], weight=0.9)
        store.add_node("Third", node_id="c", domains=["security"], weight=0.5)
        before, _ = propose_domain_links(store)

        store.update_node("a", weight=1.0)
        store.update_node("b", weight=0.01)
        store.update_node("c", weight=0.02)
        after, _ = propose_domain_links(store)

        assert before == after
        assert {proposal["from_id"] for proposal in after} == {"a"}

    def test_full_dry_run_reports_pairs_without_writes(self, config, store):
        from kindex.dream import dream_full

        store.add_node("Concept A", domains=["security"])
        store.add_node("Concept B", domains=["security"])

        results = dream_full(config, store, dry_run=True)

        assert len(results["domain_link_proposals"]) == 1
        assert results["domain_link_suggestions_created"] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        assert store.conn.execute(
            "SELECT COUNT(*) FROM suggestions"
        ).fetchone()[0] == 0

    def test_full_creates_reviewable_suggestion_not_edge(self, config, store):
        from kindex.dream import DOMAIN_SUGGESTION_SOURCE, dream_full

        a = store.add_node("Concept A", domains=["security"])
        b = store.add_node("Concept B", domains=["security"])

        first = dream_full(config, store)
        second = dream_full(config, store)

        assert first["domain_link_suggestions_created"] == 1
        assert second["domain_link_suggestions_created"] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        suggestion = store.conn.execute(
            "SELECT * FROM suggestions WHERE concept_a IN (?, ?)", (a, b)
        ).fetchone()
        assert suggestion["source"] == DOMAIN_SUGGESTION_SOURCE

    def test_domain_suggestions_are_never_auto_applied(self, store):
        from kindex.dream import DOMAIN_SUGGESTION_SOURCE, auto_apply_suggestions

        a = store.add_node("Matching concept")
        b = store.add_node("Matching concept details")
        store.add_suggestion(a, b, source=DOMAIN_SUGGESTION_SOURCE)

        assert auto_apply_suggestions(store) == 0
        assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0

    def test_pending_domain_queue_has_a_global_cap(self, config, store):
        from kindex.dream import DOMAIN_SUGGESTION_SOURCE, dream_full

        config.reminders.dream_max_domain_link_suggestions = 3
        for index in range(10):
            store.add_node(f"Concept {index}", domains=["security"])

        first = dream_full(config, store)
        second = dream_full(config, store)

        assert first["domain_link_suggestions_created"] == 3
        assert second["domain_link_suggestions_created"] == 0
        assert second["domain_link_suggestions_pending"] == 3
        pending = store.conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE status = 'pending' AND source = ?",
            (DOMAIN_SUGGESTION_SOURCE,),
        ).fetchone()[0]
        assert pending == 3

    def test_rejected_domain_pair_is_not_reproposed(self, config, store):
        from kindex.dream import dream_full

        store.add_node("Concept A", node_id="a", domains=["security"])
        store.add_node("Concept B", node_id="b", domains=["security"])
        first = dream_full(config, store)
        suggestion = store.pending_suggestions()[0]
        store.update_suggestion(suggestion["id"], "rejected")

        second = dream_full(config, store)

        assert first["domain_link_suggestions_created"] == 1
        assert second["domain_link_proposals"] == []
        assert second["domain_link_suggestions_created"] == 0


class TestDeepDreamClusters:
    def test_overlap_dedup_removes_subsets_and_near_duplicates(self):
        from kindex.dream_deep import _dedupe_overlapping_clusters

        def cluster(*ids):
            return [{"id": node_id} for node_id in ids]

        clusters = [
            cluster("a", "b", "c", "d", "e"),
            cluster("a", "b", "c", "d"),
            cluster("a", "b", "c", "d", "f"),
            cluster("w", "x", "y", "z"),
        ]

        deduped = _dedupe_overlapping_clusters(clusters)

        assert [[node["id"] for node in group] for group in deduped] == [
            ["a", "b", "c", "d", "e"],
            ["w", "x", "y", "z"],
        ]

    def test_existing_domain_edges_do_not_create_clusters(self, store):
        from kindex.dream_deep import _find_clusters
        from kindex.schema import LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE

        ids = [
            store.add_node(f"Concept {index}", domains=["security"])
            for index in range(4)
        ]
        for index in range(len(ids) - 1):
            store.add_edge(
                ids[index],
                ids[index + 1],
                provenance=LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,
            )

        assert _find_clusters(store, min_size=4, max_hops=2) == []

    def test_cluster_signature_prevents_regeneration(self, config, store, monkeypatch):
        import kindex.dream_deep as deep

        cluster = [
            store.get_node(store.add_node(f"Node {index}"))
            for index in range(4)
        ]
        calls = []
        monkeypatch.setattr(deep, "_find_clusters", lambda *args, **kwargs: [cluster])

        def summarise(_cluster, timeout=300):
            calls.append(timeout)
            return {"title": f"Summary {len(calls)}", "content": "Summary"}

        monkeypatch.setattr(deep, "_llm_summarise_cluster", summarise)

        first = deep.dream_deep(config, store)
        second = deep.dream_deep(config, store)

        assert first["cluster_summaries"] == 1
        assert second["cluster_summaries"] == 0
        assert len(calls) == 1

    def test_existing_overlapping_summary_prevents_regeneration(
        self, config, store, monkeypatch
    ):
        import kindex.dream_deep as deep

        cluster = [
            store.get_node(store.add_node(f"Node {index}", node_id=f"node-{index}"))
            for index in range(5)
        ]
        store.add_node(
            "Existing summary",
            prov_source=deep.CLUSTER_SUMMARY_SOURCE,
            extra={"cluster_members": [f"node-{index}" for index in range(4)]},
        )
        monkeypatch.setattr(deep, "_find_clusters", lambda *args, **kwargs: [cluster])
        monkeypatch.setattr(
            deep,
            "_llm_summarise_cluster",
            lambda *_args, **_kwargs: pytest.fail("overlapping cluster regenerated"),
        )

        result = deep.dream_deep(config, store)

        assert result["cluster_summaries"] == 0


# ── Protected types: managed/additive state survives dream (idx 25) ──


class TestDreamProtectsManagedState:
    def test_protected_types_derive_from_edit_policy(self):
        """PROTECTED_TYPES is the schema EDIT_POLICY additive+managed union."""
        from kindex.dream import PROTECTED_TYPES
        from kindex.schema import EDIT_POLICY

        expected = frozenset(EDIT_POLICY["additive"]) | frozenset(
            EDIT_POLICY["managed"])
        assert PROTECTED_TYPES == expected
        # The managed class — the original gap — is explicitly covered.
        assert {"coordination", "task", "session"} <= PROTECTED_TYPES

    def test_near_identical_coordination_slugs_survive_dream(self, config, store):
        """Repro: 'sprint-14-planning' vs 'sprint-15-planning' share identical
        content, so pre-fix dream_lightweight auto-merged one mid-collab,
        destroying members/cursors/messages and breaking get_conversation."""
        from kindex.coordination import (
            create_conversation,
            get_conversation,
            join_conversation,
            post_message,
        )
        from kindex.dream import dream_lightweight

        create_conversation(store, "sprint-14-planning", created_by="agent-a")
        create_conversation(store, "sprint-15-planning", created_by="agent-b")
        join_conversation(store, "sprint-14-planning", "agent-b")
        post_message(store, "sprint-14-planning", "agent-a", "claimed parser")

        results = dream_lightweight(config, store)
        assert results["merged"] == 0

        for name in ("sprint-14-planning", "sprint-15-planning"):
            conv = get_conversation(store, name)
            assert conv is not None, f"{name} no longer resolvable"
            assert conv["status"] == "active"
            assert conv["extra"]["coord_status"] == "active"
        extra = get_conversation(store, "sprint-14-planning")["extra"]
        assert [m["agent"] for m in extra["members"]] == ["agent-a", "agent-b"]
        assert len(extra["messages"]) == 1

    def test_merge_nodes_refuses_protected_types(self, store):
        """Defense in depth: merge_nodes itself refuses protected types."""
        from kindex.dream import merge_nodes

        a = store.add_node("Task one alpha", node_type="task",
                           extra={"task_status": "open"})
        b = store.add_node("Task one alpha2", node_type="task",
                           extra={"task_status": "open"})
        c = store.add_node("Plain concept", node_type="concept")

        assert merge_nodes(store, a, b) is False
        assert merge_nodes(store, c, a) is False  # protected target too
        assert store.get_node(a)["status"] == "active"
        assert store.get_node(a)["extra"]["task_status"] == "open"

    def test_merge_preserves_source_extra(self, store):
        """Archiving a merged source annotates extra instead of replacing it."""
        from kindex.dream import merge_nodes

        a = store.add_node("Source w extra", content="S",
                           extra={"custom_key": "keepme"})
        b = store.add_node("Target node x", content="T")

        assert merge_nodes(store, a, b) is True
        extra = store.get_node(a)["extra"]
        assert extra["custom_key"] == "keepme"
        assert extra["merged_into"] == b
        assert extra["merged_by"] == "dream-cycle"
