"""Acceptance oracles for compatible recall and opt-in trusted search."""
from __future__ import annotations

import inspect
import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
from types import SimpleNamespace

import pytest

from kindex.config import Config
from kindex.retrieve import hybrid_search
from kindex.store import Store


AT = "2026-08-18T12:00:00Z"
TOKEN = "stateproofsearchtoken"


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=str(tmp_path)))
    yield value
    value.close()


def _verify(store: Store, node_id: str, **times) -> None:
    store.verify_node(
        node_id,
        verified_by="search tester",
        prov_method="fixture inspection",
        verified_at="2026-08-18T11:00:00Z",
        **times,
    )


def _env(tmp_path):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("KIN_") or key.endswith("_API_KEY"):
            env.pop(key, None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


def _run_clocked_cli(tmp_path, monkeypatch, capsys, *args, data_dir):
    import kindex.cli as cli_mod

    monkeypatch.setattr(cli_mod, "operation_now", lambda: AT)
    monkeypatch.setattr(
        sys, "argv", ["kin", *args, "--data-dir", str(data_dir)]
    )
    try:
        result = cli_mod.main()
        returncode = 0 if result is None else int(result)
    except SystemExit as exc:
        returncode = int(exc.code or 0)
    captured = capsys.readouterr()
    return SimpleNamespace(
        returncode=returncode, stdout=captured.out, stderr=captured.err
    )


def test_p5_1_default_search_remains_legacy_recall(store):
    """P5.1: an active unverified legacy node reaches omitted-flag search;
    globally verified-only default behavior is forbidden, while the same legacy
    ID and ordinary result dictionary are demanded. Inverting the trusted flag
    default is the smallest mutation that turns this green guard red.
    """
    legacy = store.add_node(
        "Legacy recall canary", content=f"{TOKEN} ordinary recall",
        node_id="legacy-recall",
    )
    results = hybrid_search(store, TOKEN, top_k=10, expand_graph=False)
    assert legacy in [row["id"] for row in results]
    assert all(isinstance(row, dict) and "id" in row for row in results)


@pytest.mark.red_now
def test_p5_1_p5_4_explicit_false_is_additive_and_shape_compatible(store):
    """P5.1/P5.4: omitted and explicit-false calls reach the same mixed legacy
    fixture; TypeError, changed IDs/order, or changed row keys are forbidden,
    while byte-equivalent typed results and a false signature default are
    demanded. Changing trusted_only's default is the smallest mutation red.
    """
    for index in range(4):
        store.add_node(
            f"Compatibility result {index}",
            content=f"{TOKEN} compatibility {index}", node_id=f"compat-{index}",
        )
    omitted = hybrid_search(store, TOKEN, top_k=10, expand_graph=False)
    explicit = hybrid_search(
        store, TOKEN, top_k=10, expand_graph=False, trusted_only=False,
        evaluation_time=AT,
    )
    # The first search records each hit's access; when a second boundary
    # falls between the two calls, last_accessed alone differs.
    def comparable(rows):
        return [{k: v for k, v in row.items() if k != "last_accessed"} for row in rows]

    assert comparable(explicit) == comparable(omitted)
    assert [set(row) for row in explicit] == [set(row) for row in omitted]
    signature = inspect.signature(hybrid_search)
    assert signature.parameters["trusted_only"].default is False
    assert signature.parameters["evaluation_time"].default is None


@pytest.mark.red_now
def test_p5_2_trusted_search_backfills_and_preserves_survivor_rank(store):
    """P5.2/A8: twelve equally matching rows establish actual default rank,
    then four rows beyond the first three are verified; underfill/reordering or
    unverified leakage is forbidden, while top-three trusted survivor order is
    demanded. Filtering after the top_k break is the smallest mutation that
    turns red.
    """
    for index in range(12):
        store.add_node(
            f"Backfill candidate {index}",
            content=f"{TOKEN} backfill common payload", node_id=f"backfill-{index}",
            weight=1.0 - index / 100,
        )
    initial = hybrid_search(
        store, TOKEN, top_k=50, expand_graph=False, trusted_only=False
    )
    assert len(initial) >= 8
    selected = [row["id"] for row in initial[4:8]]
    # Fixture-only field update leaves every ranking input untouched, so the
    # trusted survivors remain provably beyond the ordinary top-three break.
    store.conn.executemany(
        "UPDATE nodes SET verified_at=?, verified_by=?, prov_method=? "
        "WHERE id=?",
        [("2026-08-18T11:00:00Z", "search tester", "fixture inspection", node_id)
         for node_id in selected],
    )
    store.conn.commit()
    ranked = hybrid_search(
        store, TOKEN, top_k=50, expand_graph=False, trusted_only=False
    )
    ranked_ids = [row["id"] for row in ranked]
    assert min(ranked_ids.index(node_id) for node_id in selected) >= 3
    expected = [node_id for node_id in ranked_ids if node_id in set(selected)][:3]
    trusted = hybrid_search(
        store, TOKEN, top_k=3, expand_graph=False,
        trusted_only=True, evaluation_time=AT,
    )
    assert [row["id"] for row in trusted] == expected
    assert len(trusted) == 3


@pytest.mark.red_now
def test_p5_2_trusted_search_uses_resume_predicate_for_conflict_and_poison(store):
    """P5.2/P4.5: current-current and current-unverified contradiction pairs
    reach filter_trusted_nodes and hybrid_search; drift between predicates,
    mutual endpoints, or poison suppression is forbidden, while identical
    survivor IDs/order are demanded. Copying a simplified status predicate into
    retrieve.py is the smallest mutation that turns red.
    """
    from kindex.trust import filter_trusted_nodes

    left = store.add_node(
        "Mutual left", content=f"{TOKEN} mutual", node_id="mutual-left"
    )
    right = store.add_node(
        "Mutual right", content=f"{TOKEN} mutual", node_id="mutual-right"
    )
    poison_resistant = store.add_node(
        "Poison resistant", content=f"{TOKEN} poison", node_id="poison-resistant"
    )
    poison = store.add_node(
        "Unverified poison", content=f"{TOKEN} poison", node_id="poison"
    )
    for node_id in (left, right, poison_resistant):
        _verify(store, node_id)
    store.add_edge(left, right, edge_type="contradicts", provenance="explicit")
    store.add_edge(
        poison_resistant, poison, edge_type="contradicts", provenance="poison"
    )
    recall = hybrid_search(
        store, TOKEN, top_k=50, expand_graph=False, trusted_only=False
    )
    expected, counts = filter_trusted_nodes(store, recall, at=AT)
    trusted = hybrid_search(
        store, TOKEN, top_k=50, expand_graph=False,
        trusted_only=True, evaluation_time=AT,
    )
    assert [row["id"] for row in trusted] == [row["id"] for row in expected]
    assert poison_resistant in [row["id"] for row in trusted]
    assert left not in [row["id"] for row in trusted]
    assert right not in [row["id"] for row in trusted]
    assert poison not in [row["id"] for row in trusted]
    assert counts["mutual_contradiction"] == 2
    assert counts["unverified"] >= 1


@pytest.mark.red_now
def test_p5_2_cli_search_and_context_make_trusted_recall_explicit(
        tmp_path, monkeypatch, capsys):
    """P5.2/A9: CLI default/trusted search, context, JSON, and help reach one
    mixed store; hidden default filtering, unparseable JSON, or undisclosed
    omission is forbidden, while recall compatibility and trusted-only reason
    disclosure are demanded. Wiring the flag only in argparse is the smallest
    mutation that turns this red.
    """
    data = tmp_path / "data"
    store = Store(Config(data_dir=str(data)))
    legacy = store.add_node(
        "CLI legacy canary", content=f"{TOKEN} cli", node_id="cli-legacy"
    )
    trusted = store.add_node(
        "CLI trusted canary", content=f"{TOKEN} cli", node_id="cli-trusted"
    )
    _verify(store, trusted)
    store.close()

    default_json = _run_clocked_cli(
        tmp_path, monkeypatch, capsys, "search", TOKEN, "--json", data_dir=data
    )
    assert default_json.returncode == 0, default_json.stderr
    default_rows = json.loads(default_json.stdout)
    assert isinstance(default_rows, list)
    assert {legacy, trusted} <= {row["id"] for row in default_rows}

    trusted_json = _run_clocked_cli(
        tmp_path, monkeypatch, capsys, "search", TOKEN, "--trusted-only", "--json",
        data_dir=data
    )
    assert trusted_json.returncode == 0, trusted_json.stderr
    trusted_rows = json.loads(trusted_json.stdout)
    assert isinstance(trusted_rows, list)
    assert [row["id"] for row in trusted_rows] == [trusted]
    assert set(trusted_rows[0]) == set(
        next(row for row in default_rows if row["id"] == trusted)
    )

    default_human = _run_clocked_cli(
        tmp_path, monkeypatch, capsys, "search", TOKEN, data_dir=data
    )
    trusted_human = _run_clocked_cli(
        tmp_path, monkeypatch, capsys, "search", TOKEN, "--trusted-only",
        data_dir=data
    )
    assert "CLI legacy canary" in default_human.stdout
    assert "CLI legacy canary" not in trusted_human.stdout
    assert "unverified" not in default_human.stdout.lower()
    assert "unverified" in trusted_human.stdout.lower()

    context = _run_clocked_cli(
        tmp_path, monkeypatch, capsys, "context", "--topic", TOKEN,
        "--trusted-only", data_dir=data
    )
    assert context.returncode == 0, context.stderr
    assert "CLI trusted canary" in context.stdout
    assert "CLI legacy canary" not in context.stdout
    assert "unverified" in context.stdout.lower()

    for command in ("search", "context"):
        help_result = subprocess.run(
            [sys.executable, "-m", "kindex.cli", command, "--help"],
            capture_output=True, text=True, timeout=30, env=_env(tmp_path),
        )
        help_text = (help_result.stdout + help_result.stderr).lower()
        assert "--trusted-only" in help_text
        assert "recall" in help_text
        assert "admission" in help_text or "verified" in help_text


@pytest.mark.red_now
def test_p5_2_mcp_search_and_context_flags_are_false_by_default(store, monkeypatch):
    """P5.2/P5.4: MCP search/context are patched to the temp Store and called
    omitted/true; a true default, legacy leakage in trusted mode, or missing
    omission disclosure is forbidden, while additive false signatures and the
    same trusted survivor are demanded. Dropping one adapter forwarding keyword
    is the smallest mutation that turns red.
    """
    pytest.importorskip("mcp", reason="MCP extra required for MCP contract")
    import kindex.mcp_server as mcp_mod

    legacy = store.add_node(
        "MCP legacy canary", content=f"{TOKEN} mcp", node_id="mcp-legacy"
    )
    trusted = store.add_node(
        "MCP trusted canary", content=f"{TOKEN} mcp", node_id="mcp-trusted"
    )
    _verify(store, trusted)
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: AT)

    default_search = mcp_mod.search(TOKEN)
    trusted_search = mcp_mod.search(TOKEN, trusted_only=True)
    assert "MCP legacy canary" in default_search
    assert "MCP legacy canary" not in trusted_search
    assert "MCP trusted canary" in trusted_search
    assert "unverified" in trusted_search.lower()

    trusted_context = mcp_mod.context(topic=TOKEN, trusted_only=True)
    assert "MCP legacy canary" not in trusted_context
    assert "MCP trusted canary" in trusted_context
    assert "unverified" in trusted_context.lower()
    assert inspect.signature(mcp_mod.search).parameters["trusted_only"].default is False
    assert inspect.signature(mcp_mod.context).parameters["trusted_only"].default is False
    assert legacy != trusted


@pytest.mark.red_now
def test_p5_3_trust_review_and_migration_hot_paths_make_no_model_call(
        tmp_path, monkeypatch):
    """P5.3: network/subprocess tripwires surround fresh migration, candidate
    review, verification, invalidation, trusted search, and resume; any provider
    or model launch is forbidden, while all local typed results are demanded.
    Adding an LLM adjudication call to any hot path is the smallest mutation red.
    """
    def forbidden(*_args, **_kwargs):
        raise AssertionError("state-resilience hot path attempted external work")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    import kindex.retrieve as retrieve_mod
    import kindex.vectors as vectors_mod

    vector_stub = lambda *_args, **_kwargs: []
    monkeypatch.setattr(vectors_mod, "vector_search", vector_stub)
    if hasattr(retrieve_mod, "vector_search"):
        monkeypatch.setattr(retrieve_mod, "vector_search", vector_stub)

    local = Store(Config(data_dir=str(tmp_path / "no-model")))
    try:
        node_id = local.add_node(
            "No-model trusted fact", content=f"{TOKEN} local", node_id="no-model"
        )
        local.verify_node(
            node_id, verified_by="tester", prov_method="fixture", verified_at=AT
        )
        candidate_id = local.add_capture_candidate(
            title="No-model candidate", content="local review payload",
            source_digest=hashlib.sha256(b"local transcript").hexdigest(),
            now=AT,
        )
        accepted = local.accept_capture_candidate(
            candidate_id,
            review_token=local.candidate_review_token(candidate_id),
            reviewed_by="tester", prov_method="fixture", now=AT,
        )
        assert accepted["created_node_id"]
        results = hybrid_search(
            local, TOKEN, top_k=10, expand_graph=False,
            trusted_only=True, evaluation_time=AT,
        )
        assert [row["id"] for row in results] == [node_id]
        from kindex.sessions import format_resume_context, link_node_to_tag, start_tag

        start_tag(local, "no-model-resume", focus="local only")
        link_node_to_tag(local, "no-model-resume", node_id)
        output = format_resume_context(
            local, "no-model-resume", max_tokens=4096, evaluation_time=AT
        )
        assert "No-model trusted fact" in output
        local.invalidate_node(
            node_id, invalidated_by="tester", disposition_code="obsolete",
            invalid_at=AT,
        )
    finally:
        local.close()
