"""R0 referent binding + two clocks (PRD lineage-grounding, lead item).

Authority: docs/prd-lineage-grounding-2026-08.md R0 section + Review outcome
point 1 (content-hash anchoring primary; two clocks stay; staleness is a
divergence measurement; detection never deletes or rewrites content).
Falsifiability: each test names the mutation that reddens it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.referent import (
    ReferentError,
    check_node_referent,
    hash_file,
    rebind,
    stale_sweep,
    validate_referent,
)
from kindex.store import Store
from kindex.trust import TRUST_REASONS, node_trust_decision


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    s = Store(cfg)
    yield s
    s.close()


def _bind_file(store, path, node_id="bound", title="Claim about a file",
               **kwargs):
    return store.add_node(
        title, content="module X parses Y",
        node_id=node_id,
        referent={"path": str(path), "content_digest": hash_file(path),
                  "digest_scope": "file"},
        **kwargs,
    )


def _verify(store, node_id):
    store.verify_node(node_id, verified_by="reviewer",
                      prov_method="source-check")


# ── validation ──────────────────────────────────────────────────────────


def test_validate_referent_shapes():
    """Exactly one of path|url; digest matches scope; unknown scope rejected.

    Mutation that reddens this: dropping any validation branch admits the
    malformed shape.
    """
    good = validate_referent(
        {"path": "src/x.py", "content_digest": "AB" * 32})
    assert good["digest_scope"] == "file"
    assert good["content_digest"] == "ab" * 32  # normalized lowercase

    url = validate_referent(
        {"url": "https://e.co/doc", "content_digest": "cd" * 32})
    assert url["digest_scope"] == "url"

    repo = validate_referent(
        {"path": ".", "content_digest": "abc1234", "digest_scope": "repo"})
    assert repo["content_digest"] == "abc1234"  # commit hint length ok

    with pytest.raises(ReferentError):
        validate_referent({"path": "a", "url": "b",
                           "content_digest": "ab" * 32})
    with pytest.raises(ReferentError):
        validate_referent({"content_digest": "ab" * 32})
    with pytest.raises(ReferentError):
        validate_referent({"path": "a", "content_digest": "xyz"})
    with pytest.raises(ReferentError):
        validate_referent({"path": "a", "content_digest": "ab" * 32,
                           "digest_scope": "tree"})
    with pytest.raises(ReferentError):
        validate_referent({"path": "a", "content_digest": "abc1234"})  # short sha for file


# ── T3.1 schema round-trip + migration ─────────────────────────────────


def test_add_node_round_trips_referent_and_clocks(store, tmp_path):
    """add_node stores the binding; get_node returns the parsed dict + clocks.

    Mutation that reddens this: dropping referent from _row_to_dict's JSON
    parse returns a string, or dropping the columns loses the values.
    """
    f = tmp_path / "mod.py"
    f.write_text("def parse(): ...\n")
    _bind_file(store, f,
               asserted_at="2026-08-20T10:00:00Z",
               true_of="2026-08-01T09:00:00Z")
    node = store.get_node("bound")
    assert node["referent"]["content_digest"] == hash_file(f)
    assert node["referent"]["digest_scope"] == "file"
    assert node["asserted_at"] == "2026-08-20T10:00:00Z"
    assert node["true_of"] == "2026-08-01T09:00:00Z"


def test_clock_defaults(store, tmp_path):
    """true_of defaults to asserted_at; asserted_at defaults to now-when-bound.

    Mutation that reddens this: defaulting true_of to None leaves the second
    clock empty on a bound claim.
    """
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    _bind_file(store, f)
    node = store.get_node("bound")
    assert node["asserted_at"]  # defaulted to now
    assert node["true_of"] == node["asserted_at"]

    store.add_node("Unbound claim", node_id="plain")
    plain = store.get_node("plain")
    assert plain["referent"] is None
    assert plain["asserted_at"] is None and plain["true_of"] is None


def test_add_node_rejects_malformed_binding(store):
    """Fail-closed at the store boundary.

    Mutation that reddens this: skipping validation stores garbage silently.
    """
    with pytest.raises(ValueError):
        store.add_node("bad", referent={"path": "x", "content_digest": "nope"})
    with pytest.raises(ValueError):
        store.add_node("bad2", asserted_at="2026-08-20 10:00")  # naive/legacy


def test_v8_database_migrates_to_v9(tmp_path):
    """A stamped-v8 database gains the three columns and the v9 stamp,
    preserving legacy rows.

    Mutation that reddens this: dropping the v9 block from _migrate_schema
    leaves the stamp at 8 and no referent column.
    """
    db = tmp_path / "kindex.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE nodes (
               id TEXT PRIMARY KEY, type TEXT NOT NULL DEFAULT 'concept',
               title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
               extra TEXT NOT NULL DEFAULT '{}'
           );
           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
           INSERT INTO meta VALUES ('schema_version', '8');
           INSERT INTO nodes (id, title, content)
               VALUES ('legacy', 'Legacy', 'preserve me');
        """)
    conn.commit()
    conn.close()

    store = Store(Config(data_dir=str(tmp_path)))
    try:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(nodes)")}
        assert {"referent", "asserted_at", "true_of"} <= cols
        stamp = store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert stamp[0] == "9"
        row = store.conn.execute(
            "SELECT content, referent FROM nodes WHERE id='legacy'").fetchone()
        assert row[0] == "preserve me" and row[1] is None
    finally:
        store.close()


# ── T3.2 synthetic stale detection ─────────────────────────────────────


def test_sweep_detects_divergence_and_records_marker(store, tmp_path):
    """Fresh file -> fresh; mutated file -> stale with both digests; deleted
    file -> referent-missing. Content is never rewritten.

    Mutation that reddens this: comparing the recorded digest to itself
    (instead of re-hashing) reports fresh forever.
    """
    f = tmp_path / "mod.py"
    f.write_text("original\n")
    _bind_file(store, f)
    recorded = hash_file(f)

    report = stale_sweep(store)
    assert report["checked"] == 1 and report["fresh"] == 1
    assert not report["stale"] and not report["missing"]
    assert not (store.get_node("bound").get("extra") or {}).get("referent_stale")

    f.write_text("the referent moved\n")
    report = stale_sweep(store)
    assert [e["id"] for e in report["stale"]] == ["bound"]
    node = store.get_node("bound")
    marker = node["extra"]["referent_stale"]
    assert marker["reason"] == "digest-mismatch"
    assert marker["expected_digest"] == recorded
    assert marker["actual_digest"] == hash_file(f)
    assert marker["detected_at"]
    assert node["content"] == "module X parses Y"  # never rewritten
    assert node["referent"]["content_digest"] == recorded  # binding untouched

    f.unlink()
    report = stale_sweep(store)
    assert [e["id"] for e in report["missing"]] == ["bound"]
    assert store.get_node("bound")["extra"]["referent_stale"]["reason"] == (
        "referent-missing")


def test_sweep_skips_unhashable_scopes(store):
    """url/repo referents are recorded but never auto-judged in v1.

    Mutation that reddens this: treating a url referent as a missing file
    would demote it.
    """
    store.add_node(
        "URL claim", node_id="url-claim",
        referent={"url": "https://e.co/spec", "content_digest": "ab" * 32})
    report = stale_sweep(store)
    assert report["unhashable"] == 1
    assert not report["stale"] and not report["missing"]
    assert not (store.get_node("url-claim").get("extra") or {}).get(
        "referent_stale")


# ── T3.3 trusted_only demotion + recall marker ─────────────────────────


def test_stale_marker_demotes_from_trusted_recall(store, tmp_path):
    """A verified bound node is admitted until its referent moves; then the
    recorded marker denies it with reason stale_referent, the non-trusted
    surface still shows it marked, and a rebind re-admits it.

    Mutation that reddens this: dropping the marker check from
    _base_trust_decision keeps the stale node admitted.
    """
    from kindex.retrieve import _staleness_caveat

    f = tmp_path / "mod.py"
    f.write_text("original\n")
    _bind_file(store, f)
    _verify(store, "bound")

    assert "stale_referent" in TRUST_REASONS
    assert node_trust_decision(store, store.get_node("bound")).eligible

    f.write_text("moved\n")
    stale_sweep(store)
    node = store.get_node("bound")
    decision = node_trust_decision(store, node)
    assert not decision.eligible
    assert decision.reason == "stale_referent"
    # Ordinary recall still serves it, visibly marked (recorded fact beats
    # the age heuristics).
    assert _staleness_caveat(node) == " [stale-referent]"

    rebind(store, "bound", tmp_path)
    node = store.get_node("bound")
    assert node_trust_decision(store, node).eligible
    assert _staleness_caveat(node) == ""  # marker cleared, node is fresh


def test_stale_node_loses_contradiction_power(store, tmp_path):
    """A demoted node cannot suppress the node it contradicts.

    Mutation that reddens this: checking the marker only on the primary node
    (not the contradictor) keeps the suppression alive.
    """
    f = tmp_path / "mod.py"
    f.write_text("original\n")
    _bind_file(store, f, node_id="challenger", title="Old architecture claim")
    _verify(store, "challenger")
    store.add_node("Current architecture claim", node_id="incumbent")
    _verify(store, "incumbent")
    store.add_edge("challenger", "incumbent", edge_type="contradicts")

    assert node_trust_decision(
        store, store.get_node("incumbent")).reason == "mutual_contradiction"

    f.write_text("moved\n")
    stale_sweep(store)
    assert node_trust_decision(store, store.get_node("incumbent")).eligible


# ── T3.6 marker clearing + rebind semantics ────────────────────────────


def test_fresh_rehash_clears_marker(store, tmp_path):
    """Restoring the file to the recorded state self-heals the demotion.

    Mutation that reddens this: never clearing leaves the node demoted
    after the referent returns.
    """
    f = tmp_path / "mod.py"
    f.write_text("original\n")
    _bind_file(store, f)
    f.write_text("moved\n")
    stale_sweep(store)
    assert store.get_node("bound")["extra"].get("referent_stale")

    f.write_text("original\n")
    report = stale_sweep(store)
    assert [e["id"] for e in report["cleared"]] == ["bound"]
    assert not store.get_node("bound")["extra"].get("referent_stale")


def test_rebind_moves_true_of_not_asserted_at(store, tmp_path):
    """Rebinding re-observes the referent: true_of advances, the claim's own
    date stays, the digest tracks the new state.

    Mutation that reddens this: resetting asserted_at on rebind re-dates the
    claim (the two-clock divergence disappears).
    """
    f = tmp_path / "mod.py"
    f.write_text("original\n")
    _bind_file(store, f, asserted_at="2026-08-01T00:00:00Z")
    f.write_text("new state\n")
    stale_sweep(store)

    node = rebind(store, "bound", tmp_path)
    assert node["asserted_at"] == "2026-08-01T00:00:00Z"
    assert node["true_of"] != "2026-08-01T00:00:00Z"
    assert node["referent"]["content_digest"] == hash_file(f)
    assert not node["extra"].get("referent_stale")


def test_check_node_referent_statuses(store, tmp_path):
    """Direct check helper covers fresh/stale/missing/unhashable."""
    f = tmp_path / "m.py"
    f.write_text("a\n")
    node = {"referent": {"path": str(f), "content_digest": hash_file(f),
                         "digest_scope": "file"}}
    assert check_node_referent(node).status == "fresh"
    f.write_text("b\n")
    assert check_node_referent(node).status == "stale"
    f.unlink()
    assert check_node_referent(node).status == "missing"
    assert check_node_referent({"referent": None}).status == "unhashable"


# ── T3.4 export / import / .kin projection ─────────────────────────────


def _run_cli(*args, data_dir):
    cmd = [sys.executable, "-m", "kindex.cli", *args, "--data-dir", data_dir]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_export_import_round_trips_binding(tmp_path):
    """kin export --format jsonl carries referent+clocks; importing into a
    fresh store preserves them.

    Mutation that reddens this: dropping the fields from cmd_export or
    cmd_import_graph loses the binding across the round trip.
    """
    f = tmp_path / "mod.py"
    f.write_text("content\n")
    src_dir = str(tmp_path / "src-data")
    store = Store(Config(data_dir=src_dir))
    _bind_file(store, f, asserted_at="2026-08-20T10:00:00Z",
               true_of="2026-08-02T00:00:00Z")
    store.close()

    r = _run_cli("export", "--audience", "private", "--format", "jsonl",
                 data_dir=src_dir)
    assert r.returncode == 0, r.stderr
    items = [json.loads(line) for line in r.stdout.strip().splitlines()]
    bound = next(i for i in items if i["id"] == "bound")
    assert bound["referent"]["content_digest"] == hash_file(f)
    assert bound["asserted_at"] == "2026-08-20T10:00:00Z"
    assert bound["true_of"] == "2026-08-02T00:00:00Z"

    export_file = tmp_path / "graph.jsonl"
    export_file.write_text(r.stdout)
    dst_dir = str(tmp_path / "dst-data")
    r2 = _run_cli("import", str(export_file), data_dir=dst_dir)
    assert r2.returncode == 0, r2.stderr
    dst = Store(Config(data_dir=dst_dir))
    try:
        node = dst.get_node("bound")
        assert node["referent"]["content_digest"] == hash_file(f)
        assert node["asserted_at"] == "2026-08-20T10:00:00Z"
        assert node["true_of"] == "2026-08-02T00:00:00Z"
    finally:
        dst.close()


def test_kin_index_carries_binding_and_redacts_absolute_paths(store, tmp_path):
    """.kin/index.json keeps referent+clocks (v2 passthrough keeps them safe
    in merges); an absolute local path is redacted with digest kept.

    Mutation that reddens this: dropping the fields from _kin_index_node, or
    exporting the absolute path verbatim.
    """
    from kindex.ingest import write_kin_index

    abs_file = tmp_path / "abs.py"
    abs_file.write_text("abs\n")
    _bind_file(store, abs_file, node_id="abs-bound", audience="team")

    rel_digest = "ef" * 32
    store.add_node(
        "Relative claim", node_id="rel-bound", audience="team",
        referent={"path": "src/mod.py", "content_digest": rel_digest,
                  "digest_scope": "file"},
        asserted_at="2026-08-20T10:00:00Z")

    out_dir = tmp_path / "proj"
    out_dir.mkdir()
    path = write_kin_index(store, out_dir)
    data = json.loads(path.read_text())
    assert data["version"] == 2
    nodes = {n["id"]: n for n in data["nodes"]}

    rel = nodes["rel-bound"]
    assert rel["referent"] == {"path": "src/mod.py",
                               "content_digest": rel_digest,
                               "digest_scope": "file"}
    assert rel["asserted_at"] == "2026-08-20T10:00:00Z"
    assert rel["true_of"] == "2026-08-20T10:00:00Z"

    redacted = nodes["abs-bound"]["referent"]
    assert "path" not in redacted
    assert redacted["path_redacted"] is True
    assert redacted["content_digest"] == hash_file(abs_file)
    assert str(abs_file) not in path.read_text()


# ── CLI + MCP surfaces ─────────────────────────────────────────────────


def test_cli_add_and_stale_flow(tmp_path):
    """kin add --referent binds (direct creation), kin stale detects and
    demarks, kin stale --rebind re-verifies.

    Mutation that reddens this: the extraction pipeline rewriting the bound
    claim (no direct-creation branch) changes the stored title/content.
    """
    f = tmp_path / "watched.py"
    f.write_text("v1\n")
    d = str(tmp_path / "data")

    r = _run_cli("add", "The parser in watched.py handles escapes",
                 "--referent", str(f), data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "bound to" in r.stdout

    store = Store(Config(data_dir=d))
    try:
        rows = store.conn.execute(
            "SELECT id FROM nodes WHERE referent IS NOT NULL").fetchall()
        assert len(rows) == 1
        nid = rows[0][0]
        node = store.get_node(nid)
        assert node["content"] == "The parser in watched.py handles escapes"
        assert node["referent"]["content_digest"] == hash_file(f)
    finally:
        store.close()

    f.write_text("v2\n")
    r = _run_cli("stale", data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "1 stale" in r.stdout
    assert "re-verification candidates" in r.stdout

    r = _run_cli("stale", "--rebind", nid, data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "Rebound" in r.stdout

    r = _run_cli("stale", data_dir=d)
    assert "1 fresh" in r.stdout and "0 stale" in r.stdout


def test_mcp_add_binding_and_stale_check(tmp_path, monkeypatch):
    """MCP add binds a referent; stale_check sweeps, marks search output,
    and rebinds.

    Mutation that reddens this: search output missing [stale-referent], or
    stale_check not recording the demotion marker.
    """
    pytest.importorskip("mcp", reason="mcp not installed")
    import kindex.mcp_server as mcp_mod
    from kindex.mcp_server import add as mcp_add
    from kindex.mcp_server import search as mcp_search
    from kindex.mcp_server import stale_check

    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", cfg)

    f = tmp_path / "svc.py"
    f.write_text("handler v1\n")
    result = mcp_add("The svc handler retries twice", referent=str(f))
    assert "Error" not in result

    f.write_text("handler v2\n")
    out = stale_check()
    assert "1 stale" in out
    assert "re-verification candidates" in out

    found = mcp_search("svc handler retries")
    assert "[stale-referent]" in found

    nid = store.conn.execute(
        "SELECT id FROM nodes WHERE referent IS NOT NULL").fetchone()[0]
    out = stale_check(rebind=nid)
    assert "Rebound" in out
    assert "1 fresh" in stale_check()
    store.close()
