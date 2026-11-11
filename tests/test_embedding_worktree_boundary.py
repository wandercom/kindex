"""Independent acceptance tests: linked worktrees are never index targets.

Requirement oracle only; implementation and worker patch not inspected. Real
temporary Git worktrees and SQLite/vec stores exercise the document boundary.
The sole remote boundary is a deterministic synthetic HTTP embedding provider.
"""

import io
import json
import os
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from kindex import vectors
from kindex.config import load_config
from kindex.store import Store


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_at = set()
        self.before_response = None

    def __call__(self, request, **_kwargs):
        body = json.loads(request.data)
        self.calls.append(body)
        if self.before_response:
            self.before_response(len(self.calls), body)
        if len(self.calls) in self.fail_at:
            raise HTTPError(request.full_url, 503, "synthetic temporary outage", None, io.BytesIO(b"{}"))
        inputs = body.get("inputs", body.get("input"))
        group = inputs[0] if inputs and isinstance(inputs[0], list) else inputs
        dimensions = body.get("output_dimension", body.get("dimensions", 8))
        rows = [{"index": index, "embedding": [1.0] + [0.0] * (dimensions - 1)}
                for index, _text in enumerate(group)]
        return Response({"data": [{"data": rows}] if "inputs" in body else rows})


def git(path, *arguments):
    return subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "Boundary fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
             "GIT_COMMITTER_NAME": "Boundary fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"},
    ).stdout.strip()


def initialize_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / ".kin").mkdir()
    (path / ".kin" / "config").write_text("name: boundary-fixture\ndata_dir: .kin/local/kindex\n")
    (path / ".gitignore").write_text(".kin/local/\n")
    (path / "README.md").write_text("Canonical project source.\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "Synthetic baseline")


@pytest.fixture
def world(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    import kindex.config as config_module

    home = tmp_path / "home"
    home.mkdir()
    global_config = home / "kin.yaml"
    global_config.write_text(
        "embedding:\n  provider: voyage\n  model: voyage-context-4\n  dimensions: 8\n"
        "  chunk_chars: 4\n  chunk_overlap_chars: 0\n  max_group_chunks: 2\n"
        "llm:\n  enabled: false\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(home / "health"))
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    monkeypatch.setenv("VOYAGE_API_KEY", "synthetic-test-key")
    for variable in ("KIN_PROFILE", "KIN_PROJECT", "KIN_PROJECT_PATH", "KINDEX_REVIEW_WORKER"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [global_config])
    provider = Provider()
    monkeypatch.setattr(vectors.urllib.request, "urlopen", provider)

    main = tmp_path / "canonical"
    initialize_repo(main)
    active = tmp_path / "active-worktree"
    git(main, "worktree", "add", "-q", "-b", "active-feature", str(active))
    (active / "README.md").write_text("Active unfinished source.\n")
    finished = tmp_path / "finished-worktree"
    git(main, "worktree", "add", "-q", "-b", "finished-feature", str(finished))
    (finished / "finished.txt").write_text("Completed work.\n")
    git(finished, "add", "finished.txt")
    git(finished, "commit", "-q", "-m", "Completed feature")
    git(main, "merge", "--ff-only", "finished-feature")
    assert git(main, "rev-parse", "HEAD") == git(finished, "rev-parse", "HEAD")
    assert git(finished, "status", "--porcelain") == ""
    assert (active / ".git").is_file() and (finished / ".git").is_file()
    assert (main / ".git").is_dir()

    marked_parent = tmp_path / "wip-parent"
    marked_parent.mkdir()
    (marked_parent / ".kindex-no-index").write_text("Temporary project boundary.\n")
    marked = marked_parent / "nested" / "marked-project"
    initialize_repo(marked)
    direct_marker = tmp_path / "direct-marker"
    initialize_repo(direct_marker)
    (direct_marker / ".kindex-no-index").write_text("Do not index.\n")

    # Explicit project ownership must work even from an unrelated cwd.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    instances = []

    def open_store(project):
        config = load_config(project_path=str(project), data_dir=str(project / ".kin" / "local" / "kindex"))
        assert config.data_path.resolve().is_relative_to(tmp_path.resolve())
        store = Store(config)
        instances.append(store)
        return store

    yield SimpleNamespace(main=main, active=active, finished=finished, marked=marked,
                          direct_marker=direct_marker, open=open_store, provider=provider)
    for store in instances:
        store.close()


def queue(store):
    return json.loads(store.get_meta(vectors.EMBED_QUEUE_META) or "[]")


def vector_rows(store):
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='node_vectors'").fetchone():
        return []
    import sqlite_vec
    store.conn.enable_load_extension(True)
    sqlite_vec.load(store.conn)
    store.conn.enable_load_extension(False)
    return [(row[0], bytes(row[1])) for row in store.conn.execute(
        "SELECT node_id, embedding FROM node_vectors ORDER BY node_id")]


def invoke_surface(surface, store, node):
    if surface == "drain":
        return vectors.drain_embedding_queue(store)
    if surface == "enqueue_reindex":
        result = vectors.enqueue_reindex(store)
        vectors.drain_embedding_queue(store)
        return result
    if surface == "reindex_now":
        return vectors.reindex_now(store)
    if surface == "index_all":
        return vectors.index_all_nodes(store)
    if surface == "upsert":
        row = store.get_node(node)
        return vectors.upsert_embedding(store, node, f"{row['title']} {row['content']}")
    if surface == "embed_text":
        return vectors.embed_text("Local knowledge Edited local body", config=store.config, input_type="document")
    if surface == "embed_document_chunks":
        return vectors.embed_document_chunks("Local knowledge Edited local body", store.config)
    raise AssertionError(f"Unknown test surface: {surface}")


EXCLUDED = ["active", "finished", "marked", "direct_marker"]
SURFACES = ["drain", "enqueue_reindex", "reindex_now", "index_all", "upsert",
            "embed_text", "embed_document_chunks"]


@pytest.mark.parametrize("project_kind", EXCLUDED)
@pytest.mark.parametrize("surface", SURFACES)
def test_excluded_project_refuses_every_document_indexing_surface(world, project_kind, surface):
    store = world.open(getattr(world, project_kind))
    node = store.add_node("Local knowledge", content="Original local body")
    store.update_node(node, content="Edited local body")
    assert store.get_node(node)["content"] == "Edited local body"
    before_queue = queue(store)
    assert node in before_queue
    result = invoke_surface(surface, store, node)
    assert world.provider.calls == [], f"{surface} sent excluded {project_kind} text to provider"
    if surface == "upsert":
        assert result is False
    elif surface == "index_all":
        assert result == 0
    elif surface in ("embed_text", "embed_document_chunks"):
        assert not result
    else:
        assert result.get("status") not in (None, "ok", "empty"), "exclusion was not reported"
    assert queue(store) == before_queue, "exclusion discarded local queued work"
    assert vector_rows(store) == []
    store.close()
    reopened = world.open(getattr(world, project_kind))
    assert reopened.get_node(node)["content"] == "Edited local body"
    assert queue(reopened) == before_queue


@pytest.mark.parametrize("surface", SURFACES)
def test_canonical_main_checkout_remains_eligible(world, surface):
    store = world.open(world.main)
    node = store.add_node("Main knowledge", content="Canonical project body")
    result = invoke_surface(surface, store, node)
    assert world.provider.calls
    assert all(call["input_type"] == "document" for call in world.provider.calls)
    if surface in ("embed_text", "embed_document_chunks"):
        assert result
        return
    assert vector_rows(store)
    # Direct indexing APIs need not consume the separately maintained queue.
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(store)
    assert len(world.provider.calls) == paid
    assert vectors.embedding_status(store)["coverage_complete"] is True


@pytest.mark.parametrize("project_kind", EXCLUDED)
def test_exclusion_preserves_query_embeddings_and_vector_search(world, project_kind):
    store = world.open(getattr(world, project_kind))
    node = store.add_node("Searchable local memory", content="Local knowledge remains usable")
    # Pre-existing local vectors are searchable without indexing documents.
    assert vectors.ensure_vec_table(store)
    store.conn.execute("INSERT INTO node_vectors(node_id,embedding) VALUES (?,?)",
                       (node, struct.pack("8f", 1.0, *([0.0] * 7))))
    store.conn.commit()
    query = vectors.embed_text("find local knowledge", config=store.config, input_type="query")
    assert query == [1.0] + [0.0] * 7
    results = vectors.vector_search(store, "find local knowledge")
    assert node in json.dumps(results), "existing local vectors became unsearchable"
    assert len(world.provider.calls) >= 2
    assert all(call["input_type"] == "query" for call in world.provider.calls)
    assert store.get_node(node)["content"] == "Local knowledge remains usable"


@pytest.mark.parametrize("project_kind", ["active", "finished"])
def test_existing_fresh_vectors_do_not_bypass_worktree_exclusion_or_consume_queue(world, project_kind):
    canonical = world.open(world.main)
    node = canonical.add_node("Already embedded", content="Same previously paid content")
    vectors.drain_embedding_queue(canonical)
    original_vectors = vector_rows(canonical)
    assert original_vectors
    excluded = world.open(getattr(world, project_kind))
    canonical.conn.backup(excluded.conn)  # Copy only this synthetic test graph.
    vectors.enqueue_embedding(excluded, node)
    paid = len(world.provider.calls)
    vectors.drain_embedding_queue(excluded)
    assert len(world.provider.calls) == paid
    assert node in queue(excluded), "fresh cache consumed work despite excluded owner"
    assert vector_rows(excluded) == original_vectors


def test_marker_added_after_partial_paid_work_blocks_retry_and_preserves_checkpoint(world):
    store = world.open(world.main)
    node = store.add_node("T", content="abcdefghijklmnop")
    world.provider.fail_at.add(2)
    result = vectors.drain_embedding_queue(store)
    assert result["embedded"] == 0
    assert len(world.provider.calls) == 2
    assert node in queue(store)
    before = list(store.conn.iterdump())
    (world.main / ".kindex-no-index").write_text("WIP: document indexing excluded.\n")
    store.close()
    reopened = world.open(world.main)
    vectors.drain_embedding_queue(reopened)
    assert len(world.provider.calls) == 2
    assert node in queue(reopened)
    assert vector_rows(reopened) == []
    # The exclusion guard must not erase already durable paid checkpoint rows.
    after = set(reopened.conn.iterdump())
    insert_rows = [line for line in before if line.startswith("INSERT INTO")]
    assert set(insert_rows).issubset(after)


def test_marker_created_during_request_stops_following_groups_and_publication(world):
    store = world.open(world.main)
    node = store.add_node("T", content="abcdefghijklmnop")

    def mark_after_start(call, _body):
        if call == 1:
            (world.main / ".kindex-no-index").write_text("Document indexing revoked.\n")

    world.provider.before_response = mark_after_start
    result = vectors.drain_embedding_queue(store)
    assert len(world.provider.calls) == 1, "new exclusion did not stop later paid groups"
    assert result["embedded"] == 0
    assert result["status"] != "ok"
    assert node in queue(store)
    assert vector_rows(store) == []


def test_worktree_data_path_is_excluded_even_when_selected_project_is_canonical(world):
    config = load_config(project_path=str(world.main), data_dir=str(world.active / ".kin" / "local" / "alternate"))
    store = Store(config)
    try:
        node = store.add_node("Local worktree memory", content="Explicit data directory")
        result = vectors.drain_embedding_queue(store)
        assert world.provider.calls == []
        assert result["status"] not in ("ok", "empty")
        assert node in queue(store)
    finally:
        store.close()


def symlink_source(world, direction):
    if direction == "marked_to_canonical":
        alias = world.marked / "escape-to-main"
        alias.symlink_to(world.main, target_is_directory=True)
    else:
        alias = world.main / "alias-to-marked"
        alias.symlink_to(world.marked, target_is_directory=True)
    return alias


@pytest.mark.parametrize("direction", ["marked_to_canonical", "canonical_to_marked"])
def test_shared_refusal_helper_checks_lexical_and_resolved_source_ancestors(world, direction):
    from kindex.project_store import content_indexing_refusal

    source = symlink_source(world, direction)
    config = world.open(world.main).config
    assert content_indexing_refusal(config, source_paths=(source,)), "symlink erased marked source ancestry"


@pytest.mark.parametrize("direction", ["marked_to_canonical", "canonical_to_marked"])
@pytest.mark.parametrize("surface", ["files", "code", "git"])
def test_explicit_ingestion_preserves_marked_ancestry_through_symlinks(world, direction, surface):
    from kindex.adapters.code import ingest_code
    from kindex.adapters.files import ingest_directory
    from kindex.adapters.git_hooks import ingest_recent_commits

    # Both targets contain ingestible evidence, so a lost guard has real work.
    (world.main / "sample.py").write_text("def canonical_example():\n    return 42\n")
    (world.marked / "sample.py").write_text("def excluded_example():\n    return 17\n")
    source = symlink_source(world, direction)
    store = world.open(world.main)
    local = store.add_node("Local note", content="Preserved local knowledge")
    before_ids = {node["id"] for node in store.all_nodes()}
    before_queue = queue(store)
    try:
        if surface == "files":
            result = ingest_directory(store, source)
            assert result == 0
        elif surface == "code":
            result = ingest_code(store, source)
            assert result.created == 0
            assert result.errors, "code ingestion failed to report exclusion"
        else:
            result = ingest_recent_commits(store, str(source))
            assert result == 0
    except ValueError as error:
        # File/git APIs may communicate refusal as a public validation error.
        assert "index" in str(error).lower()
    assert {node["id"] for node in store.all_nodes()} == before_ids
    assert store.get_node(local)["content"] == "Preserved local knowledge"
    assert queue(store) == before_queue
    assert world.provider.calls == []


@pytest.mark.parametrize("direction", ["marked_to_canonical", "canonical_to_marked"])
@pytest.mark.parametrize("surface", ["drain", "upsert", "embed_text"])
def test_data_directory_alias_preserves_marker_boundary_and_local_use(world, direction, surface):
    from kindex.project_store import content_indexing_refusal

    if direction == "marked_to_canonical":
        target = world.main.parent / "external-graph"
        target.mkdir()
        alias = world.marked / "external-graph-alias"
    else:
        target = world.marked / "private-graph"
        target.mkdir()
        alias = world.main / "marked-graph-alias"
    alias.symlink_to(target, target_is_directory=True)
    config = load_config(project_path=str(world.main), data_dir=str(alias))
    store = Store(config)
    try:
        node = store.add_node("Local alias memory", content="Original local text")
        store.update_node(node, content="Edited local text")
        assert store.get_node(node)["content"] == "Edited local text"
        assert content_indexing_refusal(config), "selected data path lost marker ancestry"
        before = queue(store)
        invoke_surface(surface, store, node)
        assert world.provider.calls == []
        assert queue(store) == before
        assert vectors.embed_text("query local memory", config=config, input_type="query")
        assert all(call["input_type"] == "query" for call in world.provider.calls)
    finally:
        store.close()
    reopened = Store(config)
    try:
        assert reopened.get_node(node)["content"] == "Edited local text"
        assert node in queue(reopened)
    finally:
        reopened.close()


@pytest.mark.parametrize("project_kind", ["active", "marked"])
def test_cron_selected_excluded_project_with_external_graph_preserves_all_paid_queues(world, project_kind, monkeypatch):
    import httpx
    from kindex.attention import (ATTENTION_QUEUE_META, _prepare_attention_job,
                                 _save_state, enqueue_attention_review)
    from kindex.daemon import cron_run
    from kindex.reinforce import REINFORCE_QUEUE_META, enqueue_reinforce

    graph = world.main.parent / "external-cron-graph"
    config = load_config(project_path=str(world.main), data_dir=str(graph))
    config.llm.enabled = True
    config.attention.enabled = True
    config.attention.tick_interval = 1
    config.attention.max_check_cost = 0.05
    config.attention.max_conversation_cost = 0.5
    config.budget.daily = 1.0
    config.budget.weekly = 5.0
    config.budget.monthly = 10.0
    config.project_dirs = []
    config.claude_dir = str(world.main.parent / "empty-claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    outbound = []

    def refuse_network(*_args, **_kwargs):
        outbound.append("provider-http-request")
        raise AssertionError("Excluded maintenance attempted provider work")

    monkeypatch.setattr(httpx.Client, "send", refuse_network)
    monkeypatch.setattr("kindex.notify.dispatch", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda _config: False)
    store = Store(config)
    try:
        node = store.add_node("Deploy checklist", content="Verify local work before deploy",
                              node_type="directive", extra={"attention_triggers": ["deploy"]})
        prepared = _prepare_attention_job(store, config, "deploy this", "local-conversation", force=True)
        assert prepared["status"] == "queued"
        assert enqueue_attention_review(store, config, prepared["job"])
        _save_state(store, "local-conversation", {
            "conversation_id": "local-conversation", "ticks": 1,
            "injected": {f"node:{node}": "2026-01-01T00:00:00"},
            "pheromone_deposits": {f"node:{node}": {"context": ""}},
        })
        assert enqueue_reinforce(store, "local-conversation", trace="The checklist was used")
        queue_keys = [vectors.EMBED_QUEUE_META, ATTENTION_QUEUE_META, REINFORCE_QUEUE_META]
        before = {key: store.get_meta(key) for key in queue_keys}
        assert all(json.loads(value) for value in before.values())
        # Simulate selecting a transient project while retaining an explicitly
        # located graph outside that project's directory tree.
        config._project_path = getattr(world, project_kind)
        cron_run(config, store, verbose=False)
        assert world.provider.calls == []
        assert outbound == []
        assert {key: store.get_meta(key) for key in queue_keys} == before
        assert store.get_node(node)["content"] == "Verify local work before deploy"
    finally:
        store.close()


@pytest.mark.parametrize("selector", ["project_path", "KIN_PROJECT_PATH", "KIN_PROJECT"])
@pytest.mark.parametrize("direction", ["marked_to_canonical", "canonical_to_marked"])
def test_selected_project_alias_preserves_marker_after_config_resolution(world, selector, direction, monkeypatch):
    selected = symlink_source(world, direction)
    external_graph = world.main.parent / "selected-project-external-graph"
    if selector == "project_path":
        config = load_config(project_path=str(selected), data_dir=str(external_graph))
    else:
        monkeypatch.setenv(selector, str(selected))
        config = load_config(data_dir=str(external_graph))
    assert config.data_path.resolve() == external_graph.resolve()
    store = Store(config)
    try:
        node = store.add_node("Selected-path local memory", content="Original local note")
        store.update_node(node, content="Edited local note")
        before = queue(store)
        assert node in before
        result = vectors.drain_embedding_queue(store)
        assert world.provider.calls == [], "drain erased selected marked ancestor and sent document requests"
        assert result["status"] not in ("ok", "empty")
        assert vectors.upsert_embedding(store, node, "Selected-path local memory Edited local note") is False
        assert not vectors.embed_text("Selected project document", config=config, input_type="document")
        assert world.provider.calls == [], "resolved canonical root bypassed selected marked ancestor"
        assert queue(store) == before
        assert vector_rows(store) == []
        assert vectors.embed_text("find local note", config=config, input_type="query")
        assert all(call["input_type"] == "query" for call in world.provider.calls)
        assert store.get_node(node)["content"] == "Edited local note"
    finally:
        store.close()


@pytest.mark.parametrize("project_kind", ["active", "marked"])
def test_positional_drain_override_cannot_replace_excluded_scope_with_store_scope(world, project_kind):
    store = world.open(world.main)
    node = store.add_node("Queued canonical memory", content="Local work awaiting document indexing")
    before = queue(store)
    override = load_config(project_path=str(getattr(world, project_kind)), data_dir=store.config.data_dir)
    assert vectors.embedding_fingerprint(override) == vectors.embedding_fingerprint(store.config)
    result = vectors.drain_embedding_queue(store, override)
    assert world.provider.calls == [], "matching embedding settings erased excluded positional scope"
    assert result["status"] not in ("ok", "empty")
    assert queue(store) == before
    assert vector_rows(store) == []
    assert store.get_node(node)["content"] == "Local work awaiting document indexing"
    store.close()
    reopened = world.open(world.main)
    assert queue(reopened) == before


@pytest.mark.parametrize("scanner", ["scan_projects", "scan_kin_files"])
@pytest.mark.parametrize("marked_alias", [False, True], ids=["canonical-root", "marked-alias-root"])
def test_automatic_scan_checks_configured_root_before_resolving_alias(world, scanner, marked_alias):
    from kindex.ingest import scan_kin_files, scan_projects

    parent = world.main.parent / "canonical-scan-root"
    source = parent / "source-project"
    initialize_repo(source)
    (source / "CLAUDE.md").write_text("# Scan boundary source\n\nCanonical project knowledge sentinel.\n")
    (source / ".kin" / "config").write_text(
        "name: scan-boundary-source\ntitle: Scan boundary source\n"
        "description: Canonical project knowledge sentinel.\ndomains: [testing]\n"
    )
    selected_root = parent
    if marked_alias:
        selected_root = world.marked / "canonical-parent-alias"
        selected_root.symlink_to(parent, target_is_directory=True)
    store = world.open(world.main)
    local = store.add_node("Retained local note", content="Do not alter this local capture")
    before_queue = queue(store)
    store.config.project_dirs = [str(selected_root)]
    result = {"scan_projects": scan_projects, "scan_kin_files": scan_kin_files}[scanner](store.config, store)
    if marked_alias:
        assert result == 0, "scan normalized away the configured marked root"
        assert store.all_nodes(node_type="project") == []
        assert queue(store) == before_queue
        assert world.provider.calls == []
    else:
        assert result > 0, "ordinary canonical root should still ingest"
        assert store.all_nodes(node_type="project")
        assert "Canonical project knowledge sentinel" in json.dumps(store.all_nodes())
    assert store.get_node(local)["content"] == "Do not alter this local capture"


@pytest.mark.parametrize("content", ["abcdefghijklmnop", "abcd"], ids=["between-groups", "before-publication"])
def test_marker_on_positional_override_during_request_stops_work_and_retains_checkpoint(world, content):
    store = world.open(world.main)
    node = store.add_node("T", content=content)
    selected = world.main.parent / "override-canonical"
    initialize_repo(selected)
    override = load_config(project_path=str(selected), data_dir=store.config.data_dir)
    assert vectors.embedding_fingerprint(override) == vectors.embedding_fingerprint(store.config)

    def revoke_override(call, _body):
        if call == 1:
            (selected / ".kindex-no-index").write_text("Selected override is now excluded.\n")

    world.provider.before_response = revoke_override
    result = vectors.drain_embedding_queue(store, override)
    assert len(world.provider.calls) == 1, "new override marker did not stop later paid groups"
    paid_group = world.provider.calls[0]
    assert result["embedded"] == 0, "generation published after override scope was excluded"
    assert result["status"] not in ("ok", "empty")
    assert node in queue(store)
    assert vector_rows(store) == []
    assert not (world.main / ".kindex-no-index").exists()
    store.close()

    reopened = world.open(world.main)
    world.provider.before_response = None
    refused = vectors.drain_embedding_queue(reopened, override)
    assert refused["status"] not in ("ok", "empty")
    assert len(world.provider.calls) == 1
    assert node in queue(reopened)
    assert vector_rows(reopened) == []
    # The unchanged canonical store remains an eligible owner. Safely retained
    # paid work can finish there without repeating its already returned group.
    vectors.drain_embedding_queue(reopened)
    assert world.provider.calls.count(paid_group) == 1
    assert node not in queue(reopened)
    assert vector_rows(reopened)
