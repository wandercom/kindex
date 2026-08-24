"""Tests for the structured .kin merge driver (kin merge-kin)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.kin_merge import (
    dumps_code_map,
    dumps_kin,
    load_json,
    merge_code_map,
    merge_for,
    merge_index,
    merge_kin_files,
)


def _node(nid, updated_at, domains=None, title=None):
    return {
        "id": nid,
        "title": title or nid.upper(),
        "type": "concept",
        "domains": domains or [],
        "updated_at": updated_at,
        "weight": 0.5,
    }


def _index(nodes):
    return {
        "domains": sorted({d for n in nodes for d in n.get("domains") or []}),
        "node_count": len(nodes),
        "nodes": sorted(nodes, key=lambda n: n["id"]),
        "repo": "owner/repo",
        "version": 1,
    }


# ── merge_index ──────────────────────────────────────────────────────────

def test_merge_index_unions_disjoint_additions():
    base = _index([_node("a", "2026-01-01")])
    ours = _index([_node("a", "2026-01-01"), _node("b", "2026-02-01")])
    theirs = _index([_node("a", "2026-01-01"), _node("c", "2026-03-01")])
    out = merge_index(base, ours, theirs)
    assert [n["id"] for n in out["nodes"]] == ["a", "b", "c"]
    assert out["node_count"] == 3


def test_merge_index_collision_keeps_newer_updated_at():
    base = _index([_node("a", "2026-01-01")])
    ours = _index([_node("a", "2026-02-01", title="OURS")])
    theirs = _index([_node("a", "2026-05-01", title="THEIRS")])
    out = merge_index(base, ours, theirs)
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["title"] == "THEIRS"  # newer updated_at wins


def test_merge_index_honors_deletion_when_other_side_unchanged():
    base = _index([_node("a", "2026-01-01"), _node("x", "2026-01-01")])
    ours = _index([_node("a", "2026-01-01"), _node("x", "2026-01-01")])  # unchanged
    theirs = _index([_node("a", "2026-01-01")])  # deleted x
    out = merge_index(base, ours, theirs)
    assert [n["id"] for n in out["nodes"]] == ["a"]


def test_merge_index_keeps_edited_node_over_delete():
    base = _index([_node("a", "2026-01-01"), _node("x", "2026-01-01")])
    ours = _index([_node("a", "2026-01-01"), _node("x", "2026-09-01", title="EDITED")])
    theirs = _index([_node("a", "2026-01-01")])  # deleted x, but ours edited it
    out = merge_index(base, ours, theirs)
    ids = {n["id"]: n for n in out["nodes"]}
    assert "x" in ids and ids["x"]["title"] == "EDITED"


def test_merge_index_recomputes_header_and_drops_volatile_field():
    ours = _index([_node("a", "2026-01-01", domains=["x"])])
    theirs = _index([_node("b", "2026-02-01", domains=["y"])])
    out = merge_index(None, ours, theirs)
    assert out["domains"] == ["x", "y"]
    assert out["node_count"] == 2
    assert "source_updated_at" not in out
    assert set(out) == {"domains", "node_count", "nodes", "repo", "version"}


def test_merge_index_is_idempotent_on_unchanged_doc():
    """Merging an unchanged canonical doc against itself yields no diff."""
    doc = _index([_node("a", "2026-01-01", domains=["d1"]),
                  _node("b", "2026-02-01", domains=["d2"])])
    assert dumps_kin(merge_index(doc, doc, doc)) == dumps_kin(doc)


def test_merge_index_preserves_write_kin_index_node_shape():
    """Merged node entries keep the exact shape write_kin_index emits."""
    from kindex.ingest import _kin_index_node
    sample = _kin_index_node({
        "id": "n", "title": "N", "type": "concept",
        "domains": ["d"], "weight": 0.5, "updated_at": "2026-01-01",
    })
    out = merge_index(None, _index([sample]), None)
    assert set(out["nodes"][0]) == set(sample)


# ── merge_code_map ───────────────────────────────────────────────────────

def _code_map(nodes, edges=None, layers=None, langs=None):
    return {
        "version": 1,
        "project": {"name": "p", "languages": sorted(langs or []), "gitCommitHash": "abc"},
        "nodes": nodes,
        "edges": edges or [],
        "layers": layers or [],
        "tour": [],
    }


def test_merge_code_map_unions_nodes_edges_and_layer_members():
    ours = _code_map(
        nodes=[{"id": "m1", "filePath": "a.py"}],
        edges=[{"source": "m1", "target": "m2", "type": "imports"}],
        layers=[{"id": "L", "name": "Core", "nodeIds": ["m1"]}],
        langs=["python"],
    )
    theirs = _code_map(
        nodes=[{"id": "m1", "filePath": "a.py"}, {"id": "m2", "filePath": "b.py"}],
        edges=[{"source": "m1", "target": "m3", "type": "imports"}],
        layers=[{"id": "L", "name": "Core", "nodeIds": ["m2"]}],
        langs=["python", "rust"],
    )
    out = merge_code_map(None, ours, theirs)
    assert {n["id"] for n in out["nodes"]} == {"m1", "m2"}
    assert len(out["edges"]) == 2
    layer = next(l for l in out["layers"] if l["id"] == "L")
    assert layer["nodeIds"] == ["m1", "m2"]  # member union, restricted to present nodes
    assert out["project"]["languages"] == ["python", "rust"]
    assert out["tour"][0]["order"] == 1  # tour recomputed from merged layers


def test_merge_code_map_round_trips_canonical_bytes():
    """A canonical code-map merged against itself is byte-identical (no regen churn).

    Mirrors `kin export code-map`: nodes ordered by (filePath, type, id), no
    sort_keys, insertion-order keys preserved.
    """
    doc = {
        "version": 1,
        "project": {
            "name": "p", "description": "d", "languages": ["python"],
            "frameworks": [], "analyzedAt": "t", "gitCommitHash": "h",
        },
        "nodes": [
            {"id": "m1", "type": "module", "name": "A", "filePath": "a.py",
             "summary": "", "tags": [], "complexity": 1},
            {"id": "m2", "type": "module", "name": "B", "filePath": "b.py",
             "summary": "", "tags": [], "complexity": 1},
        ],
        "edges": [{"source": "m1", "target": "m2", "type": "imports"}],
        "layers": [{"id": "L", "name": "Core", "nodeIds": ["m1", "m2"]}],
        "tour": [{"order": 1, "title": "Core",
                  "description": "Review 2 node(s) in the Core layer.",
                  "nodeIds": ["m1", "m2"]}],
    }
    assert dumps_code_map(merge_code_map(None, doc, doc)) == dumps_code_map(doc)


# ── schema versioning + unknown-field passthrough (PRD lineage item 1) ──
#
# Authority: docs/prd-lineage-grounding-2026-08.md, Review outcome point 6:
# a .kin schema-version marker and unknown-field passthrough in kin_merge.py,
# with a mixed-version merge test — older drivers must not corrupt newer
# fields. Falsifiability: each test names the mutation that reddens it.


def _index_v2(nodes, **extra_fields):
    """A v2-schema index side: v1 shape + version 2 + optional unknown fields."""
    doc = _index(nodes)
    doc["version"] = 2
    doc.update(extra_fields)
    return doc


def test_mixed_version_merge_preserves_newer_side_fields():
    """Old-schema side x new-schema side: nothing of the new side is dropped.

    Mutation that reddens this: rebuilding the header from the fixed
    pre-item-1 key set (the old merge_index body) drops `lineage` and fails.
    """
    referent_node = dict(
        _node("r", "2026-08-01"),
        referent={"path": "src/x.py", "content_digest": "ab" * 32,
                  "digest_scope": "file"},
        asserted_at="2026-08-01T00:00:00Z",
        true_of="2026-08-01T00:00:00Z",
    )
    base = _index([_node("a", "2026-01-01")])
    ours = _index([_node("a", "2026-01-01"), _node("b", "2026-02-01")])  # v1
    theirs = _index_v2([_node("a", "2026-01-01"), referent_node],
                       lineage={"tool": "future-kin"})
    for old_side, new_side in ((ours, theirs), (theirs, ours)):
        out = merge_index(base, old_side, new_side)
        assert out["version"] == 2
        assert out["lineage"] == {"tool": "future-kin"}
        merged_r = next(n for n in out["nodes"] if n["id"] == "r")
        assert merged_r["referent"]["content_digest"] == "ab" * 32
        assert merged_r["asserted_at"] == "2026-08-01T00:00:00Z"
        assert merged_r["true_of"] == "2026-08-01T00:00:00Z"
        assert {n["id"] for n in out["nodes"]} == {"a", "b", "r"}


def test_passthrough_field_three_way_semantics():
    """Changed-vs-base wins; both-changed keeps ours (deterministic).

    Mutation that reddens this: always preferring ours (ignoring base) makes
    the only-theirs-changed case return 'old' and fails the first assert.
    """
    base = _index_v2([], note="old")
    ours = _index_v2([], note="old")
    theirs = _index_v2([], note="new")
    assert merge_index(base, ours, theirs)["note"] == "new"

    ours2 = _index_v2([], note="ours-edit")
    assert merge_index(base, ours2, theirs)["note"] == "ours-edit"


def test_passthrough_field_honors_deletion():
    """A field unchanged on one side and deleted on the other stays deleted.

    Mutation that reddens this: unioning fields without consulting base
    resurrects `note` and fails.
    """
    base = _index_v2([], note="old")
    ours = _index_v2([], note="old")   # unchanged
    theirs = _index_v2([])             # deleted note
    out = merge_index(base, ours, theirs)
    assert "note" not in out


def test_volatile_legacy_field_never_resurrects():
    """`source_updated_at` stays dropped even though passthrough now exists.

    Preserves the ratified intent of the pre-item-1 fixed-key-set test: the
    legacy volatile timestamp must not churn git history. Mutation that
    reddens this: passing through without the drop-list.
    """
    ours = _index_v2([], source_updated_at="2026-08-24T00:00:00Z")
    theirs = _index_v2([], source_updated_at="2026-08-23T00:00:00Z")
    out = merge_index(None, ours, theirs)
    assert "source_updated_at" not in out


def test_merge_index_v2_idempotent_with_unknown_fields():
    """An unchanged v2 doc with unknown fields merges to identical bytes."""
    doc = _index_v2([_node("a", "2026-01-01")], lineage={"k": "v"})
    assert dumps_kin(merge_index(doc, doc, doc)) == dumps_kin(doc)


def test_merge_index_rejects_newer_or_malformed_schema_version():
    """A side newer than this driver raises; the driver declines end-to-end.

    Mutation that reddens this: removing the version guard silently merges
    the v3 file and returns non-None from merge_kin_files.
    """
    from kindex.kin_merge import UnsupportedKinSchemaError

    v3 = _index([]); v3["version"] = 3
    with pytest.raises(UnsupportedKinSchemaError):
        merge_index(None, v3, _index([]))
    stringy = _index([]); stringy["version"] = "2"
    with pytest.raises(UnsupportedKinSchemaError):
        merge_index(None, stringy, _index([]))


def test_merge_kin_files_declines_newer_schema_without_rewrite(tmp_path):
    """Driver-level fail-closed proof: newer-schema side -> None, file intact."""
    v3 = _index([_node("a", "2026-01-01")]); v3["version"] = 3
    base = tmp_path / "base"; ours = tmp_path / "ours"; theirs = tmp_path / "theirs"
    base.write_text(dumps_kin(_index([])))
    ours.write_text(dumps_kin(v3))
    theirs.write_text(dumps_kin(_index([])))
    before = ours.read_text()
    assert merge_kin_files(".kin/index.json", str(base), str(ours), str(theirs)) is None
    assert ours.read_text() == before  # decline never rewrites the work tree side


def test_merge_code_map_passes_through_unknown_fields():
    """Unknown top-level code-map fields survive the merge.

    Mutation that reddens this: rebuilding the code-map dict from its six
    known keys only (the old merge_code_map body).
    """
    ours = _code_map([{"id": "m1", "filePath": "a.py"}])
    theirs = _code_map([{"id": "m1", "filePath": "a.py"}])
    theirs["annotations"] = {"m1": "hot path"}
    out = merge_code_map(None, ours, theirs)
    assert out["annotations"] == {"m1": "hot path"}


# ── load_json / merge_for / merge_kin_files ─────────────────────────────

def test_load_json_handles_empty_missing_and_invalid(tmp_path):
    assert load_json(tmp_path / "nope.json") is None  # missing
    empty = tmp_path / "empty.json"
    empty.write_text("   \n")
    assert load_json(empty) is None  # empty -> absent side
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        load_json(bad)


def test_merge_for_declines_unknown_filename():
    assert merge_for("something-else.json", None, {}, {}) is None
    assert merge_for(".kin/index.json", None, _index([]), _index([])) is not None


def test_merge_kin_files_end_to_end_for_index(tmp_path):
    base = tmp_path / "base"; ours = tmp_path / "ours"; theirs = tmp_path / "theirs"
    base.write_text(dumps_kin(_index([_node("a", "2026-01-01")])))
    ours.write_text(dumps_kin(_index([_node("a", "2026-01-01"), _node("b", "2026-02-01")])))
    theirs.write_text(dumps_kin(_index([_node("a", "2026-01-01"), _node("c", "2026-03-01")])))
    merged = merge_kin_files(".kin/index.json", str(base), str(ours), str(theirs))
    assert merged is not None
    ids = [n["id"] for n in json.loads(merged)["nodes"]]
    assert ids == ["a", "b", "c"]


def test_merge_kin_files_declines_unknown_path(tmp_path):
    f = tmp_path / "f.json"; f.write_text("{}")
    assert merge_kin_files("README.md", str(f), str(f), str(f)) is None


# ── real git merge with the driver registered (the proof) ───────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_git_merge_resolves_via_driver(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".kin").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    driver = f"{sys.executable} -m kindex.cli merge-kin %O %A %B %P"
    _git(repo, "config", "merge.kindex.driver", driver)
    (repo / ".gitattributes").write_text(".kin/index.json merge=kindex\n")
    idx = repo / ".kin" / "index.json"

    idx.write_text(dumps_kin(_index([_node("a", "2026-01-01")])))
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    default_branch = _git(repo, "branch", "--show-current").stdout.strip() or "master"

    _git(repo, "checkout", "-qb", "feature")
    idx.write_text(dumps_kin(_index([_node("a", "2026-01-01"), _node("b", "2026-02-01")])))
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "add b")

    _git(repo, "checkout", "-q", default_branch)
    idx.write_text(dumps_kin(_index([_node("a", "2026-01-01"), _node("c", "2026-03-01")])))
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "add c")

    result = _git(repo, "merge", "--no-edit", "feature")
    assert result.returncode == 0, f"merge failed: {result.stdout}\n{result.stderr}"
    text = idx.read_text()
    assert "<<<<<<<" not in text  # no conflict markers
    ids = [n["id"] for n in json.loads(text)["nodes"]]
    assert ids == ["a", "b", "c"]  # lossless union, both sides preserved


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_setup_merge_driver_install_is_idempotent_and_reversible(tmp_path):
    from kindex.setup import (
        git_repo_root, install_merge_driver, uninstall_merge_driver,
    )
    repo = tmp_path / "r"; repo.mkdir()
    _git(repo, "init", "-q")
    root = git_repo_root(repo)
    assert root is not None

    install_merge_driver(root)
    assert "merge-kin %O %A %B %P" in _git(repo, "config", "merge.kindex.driver").stdout
    attrs = (repo / ".gitattributes").read_text()
    assert ".kin/index.json merge=kindex" in attrs
    assert ".kin/code-map.json merge=kindex" in attrs

    install_merge_driver(root)  # idempotent — no duplicate lines
    assert (repo / ".gitattributes").read_text().count("index.json merge=kindex") == 1

    uninstall_merge_driver(root)
    assert _git(repo, "config", "merge.kindex.driver").returncode != 0
    assert "merge=kindex" not in (repo / ".gitattributes").read_text()


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_kin_index_auto_registers_merge_driver(tmp_path):
    repo = tmp_path / "repo"; _init_repo(repo)
    data = tmp_path / "data"

    def run_index(*extra):
        return subprocess.run(
            [sys.executable, "-m", "kindex.cli", "index",
             "--output-dir", str(repo), "--data-dir", str(data), *extra],
            capture_output=True, text=True, timeout=60,
        )

    r = run_index()
    assert r.returncode == 0, r.stderr
    assert "merge-kin %O %A %B %P" in _git(repo, "config", "merge.kindex.driver").stdout
    assert "merge=kindex" in (repo / ".gitattributes").read_text()
    # Idempotent: a second index is guarded (already registered) — no re-announce / dup.
    r2 = run_index()
    assert r2.returncode == 0
    assert "merge-driver:" not in r2.stdout
    assert (repo / ".gitattributes").read_text().count("index.json merge=kindex") == 1


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_kin_index_respects_no_merge_driver_flag(tmp_path):
    repo = tmp_path / "repo"; _init_repo(repo)
    data = tmp_path / "data"
    r = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "index", "--output-dir", str(repo),
         "--data-dir", str(data), "--no-merge-driver"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert _git(repo, "config", "merge.kindex.driver").returncode != 0
    assert not (repo / ".gitattributes").exists()
