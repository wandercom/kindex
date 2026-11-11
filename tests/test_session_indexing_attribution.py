"""Independent session-attribution acceptance tests.

Oracle: unknown attribution defers visibly; uniquely verified canonical sources
remain eligible; linked worktrees/markers always exclude. No implementation read.
Original transcript bytes are retained, including encoded-only/no-cwd fixtures.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kindex import vectors
from kindex.config import Config, ProfileEntry
from kindex.ingest import scan_codex_sessions, scan_sessions
from kindex.routing import _encode_claude_project_dir
from kindex.session_provenance import session_deferrals
from kindex.store import Store


BODY = ("PRIVATE-TRANSCRIPT-CONTENT: Stigmergy is coordination through environmental traces. "
        "Ambient Structure Discovery uses these stigmergic principles.")


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "core.hooksPath=/dev/null",
         "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
        check=True, capture_output=True, text=True,
    )


def canonical(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "commit", "--allow-empty", "-q", "-m", "Synthetic canonical identity")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    import kindex.config as config_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(home / "health"))
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    monkeypatch.setenv("VOYAGE_API_KEY", "synthetic-test-key")
    for name in ("KIN_PROJECT", "KIN_PROJECT_PATH", "KIN_PROFILE", "KINDEX_REVIEW_WORKER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [])
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    projects = tmp_path / "projects"
    projects.mkdir()
    config = Config(data_dir=str(tmp_path / "graph"), claude_dir=str(tmp_path / "claude"),
                    codex_dir=str(tmp_path / "codex"), project_dirs=[str(projects)],
                    llm={"enabled": False}, embedding={"provider": "voyage", "dimensions": 8})
    calls = []

    def provider(request, **_kwargs):
        body = json.loads(request.data)
        calls.append(body)
        inputs = body.get("inputs", body.get("input"))
        group = inputs[0] if isinstance(inputs[0], list) else inputs
        dimensions = body.get("output_dimension", body.get("dimensions", 8))
        rows = [{"index": index, "embedding": [1.0] + [0.0] * (dimensions - 1)}
                for index, _text in enumerate(group)]
        return Response({"data": [{"data": rows}] if "inputs" in body else rows})

    monkeypatch.setattr(vectors.urllib.request, "urlopen", provider)
    stores = []

    def open_store(selected=config):
        store = Store(selected)
        stores.append(store)
        return store

    yield SimpleNamespace(root=tmp_path, projects=projects, config=config, open=open_store, calls=calls)
    for store in stores:
        store.close()


def transcript(world, *, kind="claude", cwd=None, encoded=None, sid="sourceabcdef"):
    if kind == "claude":
        directory = world.config.claude_path / "projects" / (encoded or "opaque-project-name")
        directory.mkdir(parents=True, exist_ok=True)
        entry = {"role": "assistant", "content": BODY}
        if cwd is not None:
            entry["cwd"] = str(cwd)
        path = directory / f"{sid}.jsonl"
        path.write_text(json.dumps(entry) + "\n")
    else:
        directory = Path(world.config.codex_dir) / "sessions" / "2026" / "11" / "09"
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"id": f"rollout-{sid}"}
        if cwd is not None:
            metadata["cwd"] = str(cwd)
        records = [{"type": "session_meta", "payload": metadata},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": BODY}]}}]
        path = directory / f"rollout-2026-11-09-{sid}.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def scan(world, store, kind="claude"):
    function = scan_sessions if kind == "claude" else scan_codex_sessions
    return function(world.config, store, limit=50)


def assert_deferred(store, source, reason="unknown_workspace", disposition="deferred"):
    records = session_deferrals(store)
    assert len(records) == 1
    assert records[0]["source"] == str(source.resolve())
    assert records[0]["reason"] == reason
    assert records[0]["disposition"] == disposition
    assert BODY not in json.dumps(records)
    return records[0]


def test_verified_encoded_only_canonical_sessions_keep_original_profile_routing(world):
    from kindex.daemon import cron_run_all

    roots = [canonical(world.projects / "work" / "repo"),
             canonical(world.projects / "personal" / "notes"),
             canonical(world.projects / "elsewhere" / "misc")]
    sources = [transcript(world, encoded=_encode_claude_project_dir(str(root)), sid=sid)
               for root, sid in zip(roots, ["workaaaa1111", "persbbbb2222", "straycccc333"])]
    original = {source: source.read_bytes() for source in sources}
    assert all("cwd" not in json.loads(source.read_text()) for source in sources)
    world.config.profiles = {
        "work": ProfileEntry(data_dir=str(world.root / "work-graph"), roots=[str(world.projects / "work")]),
        "personal": ProfileEntry(data_dir=str(world.root / "personal-graph"), roots=[str(world.projects / "personal")]),
    }
    world.config.default_profile = "personal"
    passes = cron_run_all(world.config)
    assert [entry["profile"] for entry in passes] == ["work", "personal"]
    work = world.open(Config(data_dir=str(world.root / "work-graph")))
    personal = world.open(Config(data_dir=str(world.root / "personal-graph")))
    assert [node["id"] for node in work.all_nodes(node_type="session")] == ["session-workaaaa1111"]
    assert sorted(node["id"] for node in personal.all_nodes(node_type="session")) == [
        "session-persbbbb2222", "session-straycccc333"]
    assert work.get_meta("kin_profile") == "work"
    assert personal.get_meta("kin_profile") == "personal"
    assert session_deferrals(work) == session_deferrals(personal) == []
    assert {source: source.read_bytes() for source in sources} == original
    assert not world.config.data_path.exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("source_kind", ["existing-nongit", "wholly-missing", "unknown-cwd"])
def test_unknown_source_defers_durably_without_touching_transcript(world, kind, source_kind):
    from kindex.project_store import database_has_durable_work

    cwd = world.projects / "unverified-source"
    if source_kind == "existing-nongit":
        cwd.mkdir()
    elif source_kind == "unknown-cwd":
        cwd = None
    source = transcript(world, kind=kind, cwd=cwd)
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    store = world.open()
    assert scan(world, store, kind) == 0
    record = assert_deferred(store, source)
    assert record["cwd"] == (str(cwd) if cwd else "")
    assert store.all_nodes(node_type="session") == []
    database = Path(store.conn.execute("PRAGMA database_list").fetchone()[2])
    assert database_has_durable_work(database), "deferred source attribution was treated as disposable"
    assert world.calls == []
    store.close()
    reopened = world.open()
    assert_deferred(reopened, source)
    assert scan(world, reopened, kind) == 0
    assert_deferred(reopened, source)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_missing_leaf_under_verified_canonical_checkout_remains_eligible(world, kind):
    root = canonical(world.projects / "canonical")
    source = transcript(world, kind=kind, cwd=root / "removed" / "leaf")
    original = source.read_bytes()
    store = world.open()
    assert scan(world, store, kind) == 1
    assert len(store.all_nodes(node_type="session")) == 1
    assert session_deferrals(store) == []
    assert source.read_bytes() == original


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_unknown_source_retries_unchanged_transcript_when_canonical_evidence_arrives(world, kind):
    root = world.projects / "later-canonical"
    root.mkdir()
    source = transcript(world, kind=kind, cwd=root)
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    store = world.open()
    assert scan(world, store, kind) == 0
    assert_deferred(store, source)
    store.close()
    canonical(root)
    reopened = world.open()
    assert scan(world, reopened, kind) == 1
    assert session_deferrals(reopened) == []
    assert len(reopened.all_nodes(node_type="session")) == 1
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


def test_deferred_attribution_is_visible_in_status_json(world, monkeypatch, capsys):
    from kindex import cli

    source = transcript(world)
    store = world.open()
    assert scan(world, store) == 0
    assert_deferred(store, source)
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["kin", "status", "--data-dir", world.config.data_dir, "--json"])
    cli.main()
    visible = capsys.readouterr()
    payload = json.loads(visible.out)
    assert payload["session_provenance"]
    assert BODY not in visible.out + visible.err


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_known_transient_cwd_cannot_fallback_and_stays_excluded_after_deletion(world, kind):
    main = canonical(world.projects / "main")
    linked = world.projects / "linked-worktree"
    git(main, "worktree", "add", "-q", "--detach", str(linked))
    source = transcript(world, kind=kind, cwd=linked, encoded=_encode_claude_project_dir(str(main)))
    original = source.read_bytes()
    store = world.open()
    assert scan(world, store, kind) == 0
    assert_deferred(store, source, "transient_workspace", "excluded")
    git(main, "worktree", "remove", "--force", str(linked))
    store.close()
    reopened = world.open()
    assert scan(world, reopened, kind) == 0
    assert_deferred(reopened, source, "transient_workspace", "excluded")
    assert reopened.all_nodes(node_type="session") == []
    assert world.calls == []
    assert source.read_bytes() == original


def test_ambiguous_encoded_canonical_and_transient_candidates_defer(world):
    canonical_root = canonical(world.projects / "a-b" / "c")
    main = canonical(world.projects / "main")
    transient = world.projects / "a" / "b-c"
    transient.parent.mkdir()
    git(main, "worktree", "add", "-q", "--detach", str(transient))
    encoded = _encode_claude_project_dir(str(canonical_root))
    assert encoded == _encode_claude_project_dir(str(transient))
    world.config.project_dirs = [str(canonical_root.parent), str(transient.parent)]
    source = transcript(world, encoded=encoded)
    original = source.read_bytes()
    store = world.open()
    assert scan(world, store) == 0
    assert_deferred(store, source, "ambiguous_workspace")
    assert store.all_nodes(node_type="session") == []
    assert world.calls == []
    assert source.read_bytes() == original


@pytest.mark.parametrize("change", ["canonical-disappears", "marker-overrides"])
def test_observed_canonical_attribution_survives_history_but_not_explicit_exclusion(world, change):
    root = canonical(world.projects / "historical-canonical")
    source = transcript(world, cwd=root)
    original = source.read_bytes()
    store = world.open()
    assert scan(world, store) == 1
    session = store.all_nodes(node_type="session")[0]
    # Re-ingest from the unchanged original transcript to test retained source
    # attribution, rather than merely finding an already materialized node.
    store.delete_node(session["id"])
    if change == "canonical-disappears":
        # Only this empty synthetic Git checkout is removed, never its graph
        # or transcript, both of which live in separate temporary directories.
        shutil.rmtree(root)
    else:
        (root / ".kindex-no-index").write_text("Explicit current exclusion.\n")
    store.close()
    reopened = world.open()
    count = scan(world, reopened)
    if change == "canonical-disappears":
        assert count == 1
        assert len(reopened.all_nodes(node_type="session")) == 1
        assert session_deferrals(reopened) == []
    else:
        assert count == 0
        assert reopened.all_nodes(node_type="session") == []
        assert_deferred(reopened, source, "transient_workspace", "excluded")
    assert source.read_bytes() == original


@pytest.mark.parametrize("change", ["replace", "append"])
def test_historical_canonical_evidence_cannot_authorize_different_transcript_bytes(world, change):
    root = canonical(world.projects / "historical-canonical")
    source = transcript(world, cwd=root)
    store = world.open()
    assert scan(world, store) == 1
    store.delete_node(store.all_nodes(node_type="session")[0]["id"])
    shutil.rmtree(root)  # Empty synthetic checkout; graph and transcript remain.
    if change == "replace":
        entry = json.loads(source.read_text())
        entry["content"] += " A different source replaced this transcript."
        source.write_text(json.dumps(entry) + "\n")
    else:
        with source.open("a") as stream:
            stream.write(json.dumps({"role": "assistant", "content": "New unverified historical material."}) + "\n")
    replacement = source.read_bytes()
    store.close()
    reopened = world.open()
    assert scan(world, reopened) == 0
    assert_deferred(reopened, source)
    assert reopened.all_nodes(node_type="session") == []
    assert source.read_bytes() == replacement
    assert world.calls == []


def test_watch_deferral_recovers_through_explicit_scan_without_new_transcript_timestamp(world):
    from kindex.daemon import incremental_ingest

    root = world.projects / "later-canonical"
    root.mkdir()
    source = transcript(world, cwd=root)
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    store = world.open()
    assert incremental_ingest(world.config, store, "2000-01-01T00:00:00") == 0
    assert_deferred(store, source)
    canonical(root)
    # Watch retains its mtime watermark. The documented recovery is an explicit
    # session rescan, which must recover without rewriting the original source.
    assert incremental_ingest(world.config, store, "2999-01-01T00:00:00") == 0
    assert scan(world, store) == 1
    assert session_deferrals(store) == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


@pytest.mark.parametrize("command", [["status"], ["ingest", "sessions"]], ids=["status", "ingest"])
def test_cli_makes_unknown_deferral_and_recovery_visible(world, command, monkeypatch, capsys):
    from kindex import cli
    import kindex.config as config_module

    source = transcript(world)
    (world.root / "home" / ".claude").symlink_to(world.config.claude_path, target_is_directory=True)
    store = world.open()
    if command == ["status"]:
        assert scan(world, store) == 0
    config_file = world.root / "test-config.yaml"
    config_file.write_text(json.dumps({"claude_dir": world.config.claude_dir,
                                      "project_dirs": world.config.project_dirs,
                                      "llm": {"enabled": False}}))
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [config_file])
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["kin", *command, "--data-dir", world.config.data_dir])
    cli.main()
    visible = capsys.readouterr()
    text = (visible.out + visible.err).lower()
    assert_deferred(store, source)
    assert "session" in text
    assert any(term in text for term in ("defer", "unattributed", "unknown"))
    assert "1" in text
    assert any(term in text for term in ("recover", "attribut", "establish", "verify"))
    assert BODY.lower() not in text


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("location", ["marked", "linked-worktree"])
def test_transcript_source_location_excludes_even_when_recorded_cwd_is_canonical(world, kind, location):
    allowed_cwd = canonical(world.projects / "canonical-cwd")
    excluded = world.projects / "excluded-transcript-location"
    if location == "marked":
        excluded.mkdir()
        (excluded / ".kindex-no-index").write_text("Exclude content under this source root.\n")
    else:
        git(allowed_cwd, "worktree", "add", "-q", "--detach", str(excluded))
    world.config.claude_dir = str(excluded / "claude")
    world.config.codex_dir = str(excluded / "codex")
    source = transcript(world, kind=kind, cwd=allowed_cwd)
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    store = world.open()
    assert scan(world, store, kind) == 0, "canonical cwd bypassed excluded transcript source"
    assert_deferred(store, source, "transient_workspace", "excluded")
    assert store.all_nodes(node_type="session") == []
    assert world.calls == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original
    store.close()
    reopened = world.open()
    assert scan(world, reopened, kind) == 0
    assert_deferred(reopened, source, "transient_workspace", "excluded")


def test_encoded_discovery_cannot_discard_marked_lexical_root_before_symlink_resolution(world):
    source_parent = world.projects / "physical-canonical-parent"
    allowed = canonical(source_parent / "repo")
    marked = world.root / "marked-discovery"
    marked.mkdir()
    (marked / ".kindex-no-index").write_text("Configured discovery is excluded.\n")
    alias = marked / "outside-alias"
    alias.symlink_to(source_parent, target_is_directory=True)
    world.config.project_dirs = [str(alias)]
    source = transcript(world, encoded=_encode_claude_project_dir(str(allowed)))
    original = source.read_bytes()
    assert "cwd" not in json.loads(source.read_text())
    store = world.open()
    assert scan(world, store) == 0, "marked discovery alias authorized its resolved canonical target"
    assert_deferred(store, source, "discovery_scope_excluded")
    assert store.all_nodes(node_type="session") == []
    assert world.calls == []
    assert source.read_bytes() == original


@pytest.mark.parametrize("current_exclusion", [None, "marker", "linked-worktree"],
                         ids=["restored-canonical-evidence", "current-marker-wins", "current-worktree-wins"])
def test_restoring_verified_bytes_after_deferred_replacement_retains_last_canonical_evidence(world, current_exclusion):
    root = canonical(world.projects / "historical-source")
    source = transcript(world, cwd=root)
    verified_bytes = source.read_bytes()
    store = world.open()
    assert scan(world, store) == 1
    store.delete_node(store.all_nodes(node_type="session")[0]["id"])
    shutil.rmtree(root)  # Only empty synthetic checkout; graph/transcript stay.
    replacement = json.loads(verified_bytes)
    replacement["content"] += " Replacement bytes have no verified canonical attribution."
    source.write_text(json.dumps(replacement) + "\n")
    assert scan(world, store) == 0
    assert_deferred(store, source)
    assert store.all_nodes(node_type="session") == []
    store.close()

    source.write_bytes(verified_bytes)
    if current_exclusion == "marker":
        root.mkdir()
        (root / ".kindex-no-index").write_text("Current exclusion overrides retained evidence.\n")
    elif current_exclusion == "linked-worktree":
        other = canonical(world.projects / "new-canonical-main")
        git(other, "worktree", "add", "-q", "--detach", str(root))
    reopened = world.open()
    count = scan(world, reopened)
    if current_exclusion is None:
        assert count == 1, "deferring replacement B discarded last verified evidence for restored A"
        assert len(reopened.all_nodes(node_type="session")) == 1
        assert session_deferrals(reopened) == []
    else:
        assert count == 0
        assert reopened.all_nodes(node_type="session") == []
        assert_deferred(reopened, source, "transient_workspace", "excluded")
    assert source.read_bytes() == verified_bytes
    assert world.calls == []


@pytest.mark.parametrize("entrypoint", ["claude", "codex", "incremental"])
def test_configured_transcript_root_keeps_marked_ancestry_through_outward_alias(world, entrypoint):
    from kindex.daemon import incremental_ingest

    allowed_cwd = canonical(world.projects / "canonical-session-cwd")
    physical_home = world.root / "ordinary-transcript-home"
    physical_home.mkdir()
    marked = world.root / "marked-transcript-scope"
    marked.mkdir()
    (marked / ".kindex-no-index").write_text("Configured source-root ancestry must remain excluded.\n")
    alias = marked / "root-alias"
    alias.symlink_to(physical_home, target_is_directory=True)
    kind = "codex" if entrypoint == "codex" else "claude"
    if kind == "codex":
        world.config.codex_dir = str(alias)
    else:
        world.config.claude_dir = str(alias)
    source = transcript(world, kind=kind, cwd=allowed_cwd)
    assert source.resolve().is_relative_to(physical_home.resolve())
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    store = world.open()
    if entrypoint == "incremental":
        count = incremental_ingest(world.config, store, "2000-01-01T00:00:00")
    else:
        count = scan(world, store, kind)
    assert count == 0, "scanner resolved away the configured transcript-root exclusion"
    original_records = session_deferrals(store)
    assert len(original_records) == 1
    assert original_records[0]["source"] == str(source.resolve())
    assert store.all_nodes(node_type="session") == []
    assert world.calls == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original
    store.close()
    # The transcript itself is canonical; only the prior invocation's lexical
    # source scope was excluded. A canonical invocation must remain recoverable.
    if kind == "codex":
        world.config.codex_dir = str(physical_home)
    else:
        world.config.claude_dir = str(physical_home)
    reopened = world.open()
    if entrypoint == "incremental":
        recovered = incremental_ingest(world.config, reopened, "2000-01-01T00:00:00")
    else:
        recovered = scan(world, reopened, kind)
    assert recovered == 1, "scope-only refusal permanently poisoned canonical source attribution"
    assert original_records[0]["reason"] == "source_scope_excluded"
    assert original_records[0]["disposition"] == "deferred"
    assert len(reopened.all_nodes(node_type="session")) == 1
    assert session_deferrals(reopened) == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


@pytest.mark.parametrize("matching", [True, False], ids=["matching-excluded-scope", "unrelated-excluded-scope"])
@pytest.mark.parametrize("remove_checkout", [False, True], ids=["checkout-present", "historical-checkout"])
def test_cached_canonical_attribution_honors_only_applicable_excluded_discovery_scope(world, matching, remove_checkout):
    parent = world.projects / "canonical-parent"
    root = canonical(parent / "recorded-project")
    source = transcript(world, encoded=_encode_claude_project_dir(str(root)))
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    world.config.project_dirs = [str(parent)]
    store = world.open()
    assert scan(world, store) == 1
    store.delete_node(store.all_nodes(node_type="session")[0]["id"])
    if remove_checkout:
        shutil.rmtree(root)  # Graph and transcript are outside this fixture repo.
    marked = world.root / "marked-discovery-scope"
    marked.mkdir()
    (marked / ".kindex-no-index").write_text("Discovery through this scope is excluded.\n")
    target = parent
    if not matching:
        target = world.projects / "unrelated-parent"
        canonical(target / "unrelated-project")
    alias = marked / "selected-root"
    alias.symlink_to(target, target_is_directory=True)
    world.config.project_dirs = [str(alias)]
    store.close()
    reopened = world.open()
    count = scan(world, reopened)
    if matching:
        assert count == 0, "cached canonical evidence bypassed its currently excluded discovery scope"
        assert reopened.all_nodes(node_type="session") == []
        assert_deferred(reopened, source, "discovery_scope_excluded")
        reopened.close()
        world.config.project_dirs = [str(parent)]
        recovered = world.open()
        assert scan(world, recovered) == 1
        assert session_deferrals(recovered) == []
    else:
        assert count == 1, "an unrelated excluded root blocked legitimate retained canonical history"
        assert len(reopened.all_nodes(node_type="session")) == 1
        assert session_deferrals(reopened) == []
    assert world.calls == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


def test_actual_transient_source_remains_sticky_when_marker_is_removed(world):
    cwd = canonical(world.projects / "allowed-cwd")
    source_root = world.root / "actual-transient-source"
    source_root.mkdir()
    marker = source_root / ".kindex-no-index"
    marker.write_text("This actual source is transient.\n")
    world.config.claude_dir = str(source_root / "claude")
    source = transcript(world, cwd=cwd)
    original = source.read_bytes()
    store = world.open()
    assert scan(world, store) == 0
    assert_deferred(store, source, "transient_workspace", "excluded")
    marker.unlink()
    store.close()
    reopened = world.open()
    assert scan(world, reopened) == 0
    assert_deferred(reopened, source, "transient_workspace", "excluded")
    assert reopened.all_nodes(node_type="session") == []
    assert source.read_bytes() == original


def test_cached_logical_alias_evidence_matches_excluded_physical_scope_and_recovers(world):
    physical_parent = world.projects / "canonical-parent"
    canonical(physical_parent / "repo")
    logical_parent = world.root / "harmless-logical-parent"
    logical_parent.symlink_to(physical_parent, target_is_directory=True)
    source = transcript(world, encoded=_encode_claude_project_dir(str(logical_parent / "repo")))
    original = (source.read_bytes(), source.stat().st_mtime_ns)
    world.config.project_dirs = [str(logical_parent)]
    first = world.open()
    assert scan(world, first) == 1
    first.delete_node(first.all_nodes(node_type="session")[0]["id"])
    first.close()

    marked = world.root / "marked-alias-parent"
    marked.mkdir()
    (marked / ".kindex-no-index").write_text("The physical discovery target is excluded in this scope.\n")
    excluded_alias = marked / "different-logical-spelling"
    excluded_alias.symlink_to(physical_parent, target_is_directory=True)
    assert _encode_claude_project_dir(str(excluded_alias / "repo")) != source.parent.name
    assert _encode_claude_project_dir(str(physical_parent / "repo")) != source.parent.name
    world.config.project_dirs = [str(excluded_alias)]
    blocked = world.open()
    assert scan(world, blocked) == 0, "cached logical spelling bypassed exclusion of the same physical discovery source"
    assert_deferred(blocked, source, "discovery_scope_excluded")
    assert blocked.all_nodes(node_type="session") == []
    blocked.close()

    world.config.project_dirs = [str(logical_parent)]
    recovered = world.open()
    assert scan(world, recovered) == 1
    assert len(recovered.all_nodes(node_type="session")) == 1
    assert session_deferrals(recovered) == []
    assert world.calls == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


def test_digest_bound_history_works_without_python311_file_digest(world, monkeypatch):
    import hashlib

    monkeypatch.delattr(hashlib, "file_digest", raising=False)
    root = canonical(world.projects / "python310-canonical")
    source = transcript(world, cwd=root)
    verified_bytes = source.read_bytes()
    store = world.open()
    assert scan(world, store) == 1
    store.delete_node(store.all_nodes(node_type="session")[0]["id"])
    shutil.rmtree(root)  # Synthetic checkout only; graph and transcript remain.
    store.close()

    reopened = world.open()
    assert scan(world, reopened) == 1, "unchanged historical source lost digest-backed recovery on older stdlib"
    assert session_deferrals(reopened) == []
    assert source.read_bytes() == verified_bytes
    reopened.delete_node(reopened.all_nodes(node_type="session")[0]["id"])
    replacement = json.loads(verified_bytes)
    replacement["content"] += " Changed bytes must still require fresh attribution."
    source.write_text(json.dumps(replacement) + "\n")
    assert scan(world, reopened) == 0, "older-stdlib compatibility bypassed digest verification"
    assert_deferred(reopened, source)
    assert reopened.all_nodes(node_type="session") == []
    assert world.calls == []
