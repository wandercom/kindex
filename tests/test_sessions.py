"""Tests for session tag management: start, update, segment, pause, resume, complete."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.schema import SCHEMA_VERSION
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestStartTag:
    def test_start_creates_session_node(self, store):
        from kindex.sessions import get_tag, normalize_project_path, start_tag

        nid = start_tag(store, "my-feature", description="Working on feature X",
                        focus="Initial setup", project_path="/tmp/project")
        tag = get_tag(store, "my-feature")
        assert tag is not None
        assert tag["type"] == "session"
        extra = tag["extra"]
        assert extra["tag"] == "my-feature"
        assert extra["session_status"] == "active"
        assert extra["current_focus"] == "Initial setup"
        assert extra["project_path"] == normalize_project_path("/tmp/project")
        assert len(extra["segments"]) == 1
        assert extra["segments"][0]["focus"] == "Initial setup"

    def test_start_normalizes_name(self, store):
        from kindex.sessions import start_tag, get_tag

        start_tag(store, "My Feature Name!")
        tag = get_tag(store, "my-feature-name")
        assert tag is not None
        assert tag["extra"]["tag"] == "my-feature-name"

    def test_start_duplicate_active_raises(self, store):
        from kindex.sessions import start_tag

        start_tag(store, "test-tag")
        with pytest.raises(ValueError, match="Active tag already exists"):
            start_tag(store, "test-tag")

    def test_start_allows_reuse_of_completed_name(self, store):
        from kindex.sessions import complete_tag, get_tag, start_tag, update_tag

        first = start_tag(store, "reusable", focus="old")
        complete_tag(store, "reusable")
        second = start_tag(store, "reusable", focus="new")
        update_tag(store, "reusable", focus="current")

        assert get_tag(store, "reusable")["id"] == second
        assert store.get_node(first)["extra"]["current_focus"] == "old"
        assert store.get_node(second)["extra"]["current_focus"] == "current"

    def test_same_name_is_scoped_to_project(self, store):
        from kindex.sessions import get_tag, start_tag

        first = start_tag(store, "shared", project_path="/project/a")
        second = start_tag(store, "shared", project_path="/project/b")

        assert get_tag(store, "shared", project_path="/project/a")["id"] == first
        assert get_tag(store, "shared", project_path="/project/b")["id"] == second

    @pytest.mark.skipif(os.name == "nt", reason="symlink setup differs on Windows")
    def test_project_identity_resolves_symlinks_and_trailing_segments(
        self, store, tmp_path
    ):
        from kindex.sessions import get_tag, start_tag

        real = tmp_path / "real-project"
        real.mkdir()
        alias = tmp_path / "project-alias"
        alias.symlink_to(real, target_is_directory=True)

        first = start_tag(
            store,
            "canonical-project",
            project_path=str(alias / "."),
        )

        assert get_tag(
            store, "canonical-project", project_path=str(real)
        )["id"] == first
        assert store.get_node(first)["extra"]["project_path"] == os.path.realpath(real)
        with pytest.raises(ValueError, match="Active tag already exists"):
            start_tag(
                store,
                "canonical-project",
                project_path=str(real),
            )

    def test_start_empty_name_raises(self, store):
        from kindex.sessions import start_tag

        with pytest.raises(ValueError, match="cannot be empty"):
            start_tag(store, "")

    def test_start_with_remaining(self, store):
        from kindex.sessions import start_tag, get_tag

        start_tag(store, "with-remaining", remaining=["item1", "item2", "item3"])
        tag = get_tag(store, "with-remaining")
        assert tag["extra"]["remaining"] == ["item1", "item2", "item3"]

    def test_start_without_focus_has_no_segment(self, store):
        from kindex.sessions import start_tag, get_tag

        start_tag(store, "no-focus")
        tag = get_tag(store, "no-focus")
        assert tag["extra"]["segments"] == []


class TestUpdateTag:
    def test_update_focus(self, store):
        from kindex.sessions import start_tag, update_tag, get_tag

        start_tag(store, "test-update", focus="Initial")
        update_tag(store, "test-update", focus="Updated focus")
        tag = get_tag(store, "test-update")
        assert tag["extra"]["current_focus"] == "Updated focus"

    def test_update_remaining(self, store):
        from kindex.sessions import start_tag, update_tag, get_tag

        start_tag(store, "test-remaining", remaining=["a", "b", "c"])
        update_tag(store, "test-remaining", remaining=["x", "y"])
        tag = get_tag(store, "test-remaining")
        assert tag["extra"]["remaining"] == ["x", "y"]

    def test_append_remaining(self, store):
        from kindex.sessions import start_tag, update_tag, get_tag

        start_tag(store, "test-append", remaining=["a"])
        update_tag(store, "test-append", append_remaining=["b", "c"])
        tag = get_tag(store, "test-append")
        assert tag["extra"]["remaining"] == ["a", "b", "c"]

    def test_remove_remaining(self, store):
        from kindex.sessions import start_tag, update_tag, get_tag

        start_tag(store, "test-remove", remaining=["a", "b", "c"])
        update_tag(store, "test-remove", remove_remaining=["b"])
        tag = get_tag(store, "test-remove")
        assert tag["extra"]["remaining"] == ["a", "c"]

    def test_update_description(self, store):
        from kindex.sessions import start_tag, update_tag, get_tag

        start_tag(store, "test-desc", description="Original")
        update_tag(store, "test-desc", description="Updated description")
        tag = get_tag(store, "test-desc")
        assert tag["content"] == "Updated description"

    def test_update_nonexistent_raises(self, store):
        from kindex.sessions import update_tag

        with pytest.raises(ValueError, match="not found"):
            update_tag(store, "nonexistent", focus="Whatever")


class TestSegments:
    def test_add_segment_closes_previous(self, store):
        from kindex.sessions import start_tag, add_segment, get_tag

        start_tag(store, "test-seg", focus="First focus")
        add_segment(store, "test-seg", new_focus="Second focus",
                    summary="Finished first task")
        tag = get_tag(store, "test-seg")
        segments = tag["extra"]["segments"]
        assert len(segments) == 2
        assert segments[0]["ended_at"] is not None
        assert segments[0]["summary"] == "Finished first task"
        assert segments[1]["ended_at"] is None
        assert segments[1]["focus"] == "Second focus"

    def test_segment_updates_current_focus(self, store):
        from kindex.sessions import start_tag, add_segment, get_tag

        start_tag(store, "test-focus-seg", focus="Old")
        add_segment(store, "test-focus-seg", new_focus="New topic")
        tag = get_tag(store, "test-focus-seg")
        assert tag["extra"]["current_focus"] == "New topic"

    def test_multiple_segments_accumulate(self, store):
        from kindex.sessions import start_tag, add_segment, get_tag

        start_tag(store, "multi-seg", focus="Seg 1")
        add_segment(store, "multi-seg", new_focus="Seg 2", summary="Done 1")
        add_segment(store, "multi-seg", new_focus="Seg 3", summary="Done 2")
        tag = get_tag(store, "multi-seg")
        segments = tag["extra"]["segments"]
        assert len(segments) == 3
        assert segments[0]["ended_at"] is not None
        assert segments[1]["ended_at"] is not None
        assert segments[2]["ended_at"] is None

    def test_segment_with_decisions(self, store):
        from kindex.sessions import start_tag, add_segment, get_tag

        start_tag(store, "dec-seg", focus="Design phase")
        add_segment(store, "dec-seg", new_focus="Implementation",
                    summary="Design complete", decisions=["Use REST", "PostgreSQL"])
        tag = get_tag(store, "dec-seg")
        assert "Use REST" in tag["extra"]["segments"][0]["decisions"]
        assert "PostgreSQL" in tag["extra"]["segments"][0]["decisions"]


class TestPauseAndComplete:
    def test_pause_sets_status(self, store):
        from kindex.sessions import start_tag, pause_tag, get_tag

        start_tag(store, "pause-test")
        pause_tag(store, "pause-test")
        tag = get_tag(store, "pause-test")
        assert tag["extra"]["session_status"] == "paused"
        assert tag["extra"]["paused_at"] is not None

    def test_pause_with_summary(self, store):
        from kindex.sessions import start_tag, pause_tag, get_tag

        start_tag(store, "pause-sum", focus="Working on it")
        pause_tag(store, "pause-sum", summary="Stopped at step 3")
        tag = get_tag(store, "pause-sum")
        assert tag["extra"]["segments"][0]["summary"] == "Stopped at step 3"

    def test_complete_sets_status(self, store):
        from kindex.sessions import start_tag, complete_tag, get_tag

        start_tag(store, "complete-test")
        complete_tag(store, "complete-test")
        tag = get_tag(store, "complete-test")
        assert tag["extra"]["session_status"] == "completed"
        assert tag["extra"]["completed_at"] is not None

    def test_complete_closes_open_segment(self, store):
        from kindex.sessions import start_tag, complete_tag, get_tag

        start_tag(store, "close-seg", focus="Working")
        complete_tag(store, "close-seg", summary="All done")
        tag = get_tag(store, "close-seg")
        seg = tag["extra"]["segments"][0]
        assert seg["ended_at"] is not None
        assert seg["summary"] == "All done"

    def test_complete_nonexistent_raises(self, store):
        from kindex.sessions import complete_tag

        with pytest.raises(ValueError, match="not found"):
            complete_tag(store, "nonexistent")

    def test_resume_reactivates_paused_tag(self, store):
        from kindex.sessions import get_tag, pause_tag, resume_tag, start_tag

        start_tag(store, "resume-state", project_path="/project")
        pause_tag(store, "resume-state", project_path="/project")

        resumed = resume_tag(store, "resume-state", project_path="/project")

        assert resumed["extra"]["session_status"] == "active"
        assert resumed["extra"]["paused_at"] is None
        assert resumed["extra"]["paused_reason"] is None
        assert get_tag(
            store, "resume-state", project_path="/project"
        )["extra"]["session_status"] == "active"

    def test_completed_tag_cannot_be_resumed(self, store):
        from kindex.sessions import complete_tag, resume_tag, start_tag

        start_tag(store, "finished", project_path="/project")
        complete_tag(store, "finished", project_path="/project")

        with pytest.raises(ValueError, match="completed"):
            resume_tag(store, "finished", project_path="/project")


class TestGetTag:
    def test_get_by_name(self, store):
        from kindex.sessions import start_tag, get_tag

        start_tag(store, "find-me")
        tag = get_tag(store, "find-me")
        assert tag is not None
        assert tag["extra"]["tag"] == "find-me"

    def test_get_active_tag(self, store):
        from kindex.sessions import start_tag, get_active_tag

        start_tag(store, "active-one", project_path="/tmp/proj")
        active = get_active_tag(store, project_path="/tmp/proj")
        assert active is not None
        assert active["extra"]["tag"] == "active-one"

    def test_get_active_tag_none_when_paused(self, store):
        from kindex.sessions import start_tag, pause_tag, get_active_tag

        start_tag(store, "paused-one", project_path="/tmp/proj")
        pause_tag(store, "paused-one")
        active = get_active_tag(store, project_path="/tmp/proj")
        assert active is None

    def test_get_nonexistent_returns_none(self, store):
        from kindex.sessions import get_tag

        assert get_tag(store, "does-not-exist") is None


class TestListTags:
    def test_list_all(self, store):
        from kindex.sessions import start_tag, list_tags

        start_tag(store, "tag-a")
        start_tag(store, "tag-b")
        tags = list_tags(store)
        assert len(tags) == 2

    def test_list_filtered_by_status(self, store):
        from kindex.sessions import start_tag, pause_tag, list_tags

        start_tag(store, "active-tag")
        start_tag(store, "paused-tag")
        pause_tag(store, "paused-tag")
        active = list_tags(store, status="active")
        paused = list_tags(store, status="paused")
        assert len(active) == 1
        assert active[0]["extra"]["tag"] == "active-tag"
        assert len(paused) == 1
        assert paused[0]["extra"]["tag"] == "paused-tag"

    def test_list_filtered_by_project(self, store):
        from kindex.sessions import start_tag, list_tags

        start_tag(store, "proj-a", project_path="/proj/a")
        start_tag(store, "proj-b", project_path="/proj/b")
        tags = list_tags(store, project_path="/proj/a")
        assert len(tags) == 1
        assert tags[0]["extra"]["tag"] == "proj-a"

    def test_status_filter_is_applied_before_limit(self, store):
        from kindex.sessions import complete_tag, list_tags, start_tag

        active_id = start_tag(store, "older-active")
        store.conn.execute(
            "UPDATE nodes SET updated_at = '2020-01-01T00:00:00' WHERE id = ?",
            (active_id,),
        )
        for index in range(3):
            name = f"newer-completed-{index}"
            start_tag(store, name)
            complete_tag(store, name)

        active = list_tags(store, status="active", limit=1)

        assert [tag["id"] for tag in active] == [active_id]


class TestResumeContext:
    def test_format_resume_context(self, store):
        from kindex.sessions import start_tag, format_resume_context

        start_tag(store, "resume-test", description="Test session",
                  focus="Building feature", remaining=["item1", "item2"])
        ctx = format_resume_context(store, "resume-test")
        assert "resume-test" in ctx
        assert "Building feature" in ctx
        assert "item1" in ctx
        assert "item2" in ctx

    def test_resume_includes_segments(self, store):
        from kindex.sessions import start_tag, add_segment, format_resume_context

        start_tag(store, "seg-resume", focus="Phase 1")
        add_segment(store, "seg-resume", new_focus="Phase 2", summary="Phase 1 done")
        ctx = format_resume_context(store, "seg-resume")
        assert "Phase 1" in ctx
        assert "Phase 2" in ctx

    def test_resume_includes_linked_nodes(self, store):
        from kindex.sessions import start_tag, link_node_to_tag, format_resume_context

        start_tag(store, "linked-resume", focus="Exploring")
        nid = store.add_node("Important Concept", node_type="concept",
                             content="A key concept")
        store.verify_node(
            nid,
            verified_by="test reviewer",
            prov_method="fixture inspection",
            verified_at="2025-01-01T00:00:00Z",
        )
        link_node_to_tag(store, "linked-resume", nid)
        ctx = format_resume_context(store, "linked-resume")
        assert "Important Concept" in ctx

    def test_resume_nonexistent_tag(self, store):
        from kindex.sessions import format_resume_context

        ctx = format_resume_context(store, "nope")
        assert "not found" in ctx


class TestLinkNode:
    def test_link_node_adds_to_list(self, store):
        from kindex.sessions import start_tag, link_node_to_tag, get_tag

        start_tag(store, "link-test", focus="Testing")
        nid = store.add_node("Test Node", node_type="concept")
        link_node_to_tag(store, "link-test", nid)
        tag = get_tag(store, "link-test")
        assert nid in tag["extra"]["linked_nodes"]

    def test_link_node_creates_edge(self, store):
        from kindex.sessions import start_tag, link_node_to_tag, get_tag

        start_tag(store, "edge-test", focus="Testing")
        nid = store.add_node("Edge Node", node_type="concept")
        link_node_to_tag(store, "edge-test", nid)
        tag = get_tag(store, "edge-test")
        edges = store.edges_from(nid)
        assert any(e["to_id"] == tag["id"] for e in edges)

    def test_link_node_updates_segment_artifacts(self, store):
        from kindex.sessions import start_tag, link_node_to_tag, get_tag

        start_tag(store, "artifact-test", focus="Working")
        nid = store.add_node("Artifact", node_type="concept")
        link_node_to_tag(store, "artifact-test", nid)
        tag = get_tag(store, "artifact-test")
        assert nid in tag["extra"]["segments"][0]["artifacts"]

    def test_link_duplicate_is_idempotent(self, store):
        from kindex.sessions import start_tag, link_node_to_tag, get_tag

        start_tag(store, "dup-link", focus="Testing")
        nid = store.add_node("Dupe Node", node_type="concept")
        link_node_to_tag(store, "dup-link", nid)
        link_node_to_tag(store, "dup-link", nid)
        tag = get_tag(store, "dup-link")
        assert tag["extra"]["linked_nodes"].count(nid) == 1


class TestTagCLI:
    def test_tag_start(self, tmp_path):
        d = str(tmp_path)
        r = run("tag", "start", "cli-test", "--focus", "CLI testing",
                "--description", "Testing CLI", data_dir=d)
        assert r.returncode == 0
        assert "cli-test" in r.stdout

    def test_tag_list(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "list-test", data_dir=d)
        r = run("tag", "list", data_dir=d)
        assert r.returncode == 0
        assert "list-test" in r.stdout

    def test_tag_show(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "show-test", "--focus", "Showing", data_dir=d)
        r = run("tag", "show", "show-test", data_dir=d)
        assert r.returncode == 0
        assert "show-test" in r.stdout
        assert "Showing" in r.stdout

    def test_tag_list_and_show_expose_pause_reason(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "paused-reason", data_dir=d)
        run("tag", "pause", "paused-reason", data_dir=d)

        listed = run("tag", "list", "--status", "paused", data_dir=d)
        shown = run("tag", "show", "paused-reason", data_dir=d)

        assert "reason=user" in listed.stdout
        assert "Pause reason: user" in shown.stdout

    def test_tag_resume(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "resume-cli", "--focus", "Resuming",
            "--description", "Resume test", data_dir=d)
        run("tag", "pause", "resume-cli", data_dir=d)
        r = run("tag", "resume", "resume-cli", data_dir=d)
        assert r.returncode == 0
        assert "resume-cli" in r.stdout
        assert "Resuming" in r.stdout
        shown = run("tag", "show", "resume-cli", data_dir=d)
        assert "Status: active" in shown.stdout

    def test_tag_pause(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "pause-cli", data_dir=d)
        r = run("tag", "pause", "pause-cli", "--summary", "Pausing now", data_dir=d)
        assert r.returncode == 0
        assert "Paused" in r.stdout

    def test_tag_end(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "end-cli", data_dir=d)
        r = run("tag", "end", "end-cli", "--summary", "All done", data_dir=d)
        assert r.returncode == 0
        assert "Completed" in r.stdout

    def test_tag_segment(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "seg-cli", "--focus", "Phase 1", data_dir=d)
        r = run("tag", "segment", "seg-cli", "--focus", "Phase 2",
                "--summary", "Phase 1 done", data_dir=d)
        assert r.returncode == 0
        assert "Phase 2" in r.stdout

    def test_tag_update_with_remaining(self, tmp_path):
        d = str(tmp_path)
        run("tag", "start", "remain-cli", "--remaining", "a,b,c", data_dir=d)
        r = run("tag", "update", "remain-cli", "--done", "b", data_dir=d)
        assert r.returncode == 0
        assert "Updated" in r.stdout

    def test_tag_start_no_name_errors(self, tmp_path):
        d = str(tmp_path)
        r = run("tag", "start", data_dir=d)
        assert "Usage" in r.stderr or r.returncode != 0


class TestStoreSessionMethods:
    def test_get_session_tags(self, store):
        from kindex.sessions import start_tag

        start_tag(store, "store-test-a", project_path="/proj/a")
        start_tag(store, "store-test-b", project_path="/proj/b")
        tags = store.get_session_tags()
        assert len(tags) == 2

    def test_get_session_tags_by_status(self, store):
        from kindex.sessions import start_tag, pause_tag

        start_tag(store, "status-a")
        start_tag(store, "status-b")
        pause_tag(store, "status-b")
        active = store.get_session_tags(status="active")
        assert len(active) == 1

    def test_get_session_tag_by_name(self, store):
        from kindex.sessions import start_tag

        start_tag(store, "by-name-test")
        tag = store.get_session_tag_by_name("by-name-test")
        assert tag is not None
        assert tag["extra"]["tag"] == "by-name-test"

    def test_get_session_tag_by_name_not_found(self, store):
        tag = store.get_session_tag_by_name("nonexistent")
        assert tag is None

    def test_active_name_and_project_are_unique_in_storage(self, store):
        extra = {
            "tag": "unique",
            "project_path": "/project",
            "session_status": "active",
        }
        first = store.add_node("unique", node_type="session", extra=extra)

        with pytest.raises(sqlite3.IntegrityError):
            store.add_node("unique", node_type="session", extra=extra)

        rows = store.get_session_tags(status="active", project_path="/project")
        assert [row["id"] for row in rows] == [first]


def test_session_uniqueness_migration_pauses_older_duplicates(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    _ = store.conn
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("DROP INDEX idx_suggestions_pair")
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    extra = {
        "tag": "duplicate",
        "project_path": "/project",
        "session_status": "active",
    }
    first = store.add_node("duplicate", node_type="session", extra=extra)
    second = store.add_node("duplicate", node_type="session", extra=extra)
    store.conn.execute(
        "UPDATE nodes SET updated_at = '2026-01-01T00:00:00' WHERE id = ?",
        (first,),
    )
    store.conn.execute(
        "UPDATE nodes SET updated_at = '2026-02-01T00:00:00' WHERE id = ?",
        (second,),
    )
    store.conn.commit()
    store.close()

    migrated = Store(cfg)
    rows = migrated.conn.execute(
        "SELECT id, extra FROM nodes WHERE type = 'session' ORDER BY id"
    ).fetchall()
    extras = {row["id"]: json.loads(row["extra"]) for row in rows}
    statuses = {node_id: extra["session_status"] for node_id, extra in extras.items()}

    assert statuses == {first: "paused", second: "active"}
    assert extras[first]["paused_reason"] == (
        "duplicate-active-session-migration-v12"
    )
    assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert migrated.conn.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type = 'index' AND name = 'idx_suggestions_pair'"""
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        migrated.add_node("duplicate", node_type="session", extra=extra)
    migrated.close()

    listed = run("tag", "list", "--status", "paused", data_dir=str(tmp_path))
    assert "reason=duplicate-active-session-migration-v12" in listed.stdout


def test_session_uniqueness_migration_has_total_tiebreak(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)
    _ = store.conn
    store.conn.execute("DROP INDEX idx_session_active_tag_project")
    store.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
    extra = {
        "tag": "tie",
        "project_path": "/project",
        "session_status": "active",
    }
    for index in range(11):
        store.add_node(
            "tie",
            node_id=f"duplicate-{index:02d}",
            node_type="session",
            extra=extra,
        )
    store.conn.execute(
        """UPDATE nodes
              SET updated_at = '2026-01-01T00:00:00',
                  created_at = '2026-01-01T00:00:00'
            WHERE type = 'session'"""
    )
    store.conn.commit()
    store.close()

    migrated = Store(cfg)
    active = migrated.get_session_tags(
        status="active", project_path="/project", limit=20
    )
    paused = migrated.get_session_tags(
        status="paused", project_path="/project", limit=20
    )

    assert [row["id"] for row in active] == ["duplicate-10"]
    assert len(paused) == 10
    assert all(
        row["extra"]["paused_reason"]
        == "duplicate-active-session-migration-v12"
        for row in paused
    )
    migrated.close()
