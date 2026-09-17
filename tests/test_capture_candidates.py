"""Acceptance oracles for quarantined automatic capture and review."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kindex.config import Config
from kindex.store import Store


NOW = "2026-08-18T12:00:00Z"
SOURCE_DIGEST = hashlib.sha256(b"test-owned transcript").hexdigest()
RAW_FIELDS = {"title", "content", "node_type", "domains", "connections"}


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=str(tmp_path)))
    yield value
    value.close()


def _add_candidate(store: Store, **overrides) -> str:
    values = {
        "title": "Candidate claim",
        "content": "A reviewable claim from the test transcript.",
        "node_type": "concept",
        "domains": ["resilience", "testing"],
        "connections": [],
        "source_digest": SOURCE_DIGEST,
        "now": NOW,
    }
    values.update(overrides)
    return store.add_capture_candidate(**values)


def _candidate_rows(store: Store) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) FROM capture_candidates"
    ).fetchone()[0]


def _assert_minimized(candidate: dict) -> None:
    for field in RAW_FIELDS:
        assert candidate[field] is None


@pytest.mark.red_now
def test_p2_1_p2_2_add_is_canonical_complete_and_reviewable(store):
    """P2.1/P2.2/A5: direct add reaches one persisted candidate; a durable
    node/edge, unstable digest, or missing review field is forbidden, while a
    canonical pending record and deterministic token are demanded. Replacing
    candidate insertion with add_node is the smallest reversion that turns red.
    """
    candidate_id = _add_candidate(
        store,
        domains=["testing", "resilience", "testing"],
        connections=[{
            "from_title": "Candidate claim",
            "to_title": "Unresolved endpoint",
            "type": "relates_to",
            "why": "test proposal",
        }],
    )
    assert store.all_nodes(limit=100) == []
    assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    shown = store.get_capture_candidate(candidate_id)
    assert shown is not None
    assert shown["status"] == "pending"
    assert shown["title"] == "Candidate claim"
    assert shown["content"].startswith("A reviewable claim")
    assert shown["node_type"] == "concept"
    assert shown["domains"] == ["resilience", "testing"]
    assert shown["connections"] == [{
        "from_title": "Candidate claim",
        "to_title": "Unresolved endpoint",
        "type": "relates_to",
        "why": "test proposal",
    }]
    assert shown["source_digest"] == SOURCE_DIGEST
    assert re.fullmatch(r"[0-9a-f]{64}", shown["payload_digest"])
    assert shown["created_at"] == NOW
    assert shown["updated_at"] == NOW
    assert shown["expires_at"] == "2026-08-25T12:00:00Z"
    first_token = store.candidate_review_token(candidate_id)
    assert re.fullmatch(r"[0-9a-f]{64}", first_token)
    assert store.candidate_review_token(candidate_id) == first_token


@pytest.mark.red_now
def test_p2_2_list_redacts_payload_while_get_returns_exact_subject(store):
    """P2.2/T4.4/A5: add then list/get reaches both Store projections; title,
    content, type, domains, and connections in list are forbidden, while get
    returning the exact payload and the separate token method remaining usable
    are demanded. Returning the full row from list is the smallest mutation red.
    """
    candidate_id = _add_candidate(store)
    listed = store.list_capture_candidates(limit=20)
    assert len(listed) == 1 and listed[0]["id"] == candidate_id
    assert RAW_FIELDS.isdisjoint(listed[0])
    shown = store.get_capture_candidate(candidate_id)
    assert shown is not None
    for field in RAW_FIELDS:
        assert field in shown
    token = store.candidate_review_token(candidate_id)
    assert re.fullmatch(r"[0-9a-f]{64}", token)


@pytest.mark.red_now
def test_p2_3_candidate_is_absent_from_every_knowledge_projection(store):
    """P2.3/I1: a distinctive pending payload reaches node, FTS, hybrid,
    context, resume, and dream interfaces; any payload visibility or promotion
    is forbidden and zero durable nodes are demanded. Treating pending status
    as a node status is the smallest mutation that turns red.
    """
    title = "Quarantine canary zqxjstate"
    candidate_id = _add_candidate(
        store, title=title, content="zqxjstate hidden candidate payload"
    )
    from kindex.dream import dream_lightweight
    from kindex.retrieve import format_context_block, hybrid_search
    from kindex.sessions import format_resume_context, start_tag

    start_tag(store, "quarantine-session", focus="safe durable state")
    assert store.get_node(candidate_id) is None
    assert all(row["id"] != candidate_id for row in store.all_nodes(limit=500))
    assert store.fts_search("zqxjstate") == []
    results = hybrid_search(store, "zqxjstate", expand_graph=True)
    assert results == []
    assert "zqxjstate" not in format_context_block(
        store, results, query="zqxjstate"
    )
    assert "zqxjstate" not in format_resume_context(
        store, "quarantine-session", max_tokens=4096, evaluation_time=NOW
    )
    dream_lightweight(store.config, store)
    assert store.all_nodes(limit=500) == [
        row for row in store.all_nodes(limit=500) if row["type"] == "session"
    ]
    assert store.get_capture_candidate(candidate_id)["status"] == "pending"


@pytest.mark.red_now
@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"content": ""},
        {"node_type": "not-a-node-type"},
        {"title": "bad\x00title"},
        {"content": "bad\x1bcontent"},
        {"source_digest": "not-a-sha256"},
        {"ttl_days": 0},
        {"connections": [{
            "from_title": "Candidate claim", "to_title": "Target",
            "type": "not-an-edge", "why": "bad",
        }]},
    ],
)
def test_p2_4_invalid_candidate_input_leaves_zero_rows(store, overrides):
    """P2.4/A5: each malformed case reaches candidate validation; a partial
    row or accepted forbidden value is forbidden, while ValueError and zero
    candidate rows are demanded. Removing the corresponding validation branch
    is the smallest mutation that turns this parameter red.
    """
    with pytest.raises(ValueError):
        _add_candidate(store, **overrides)
    assert _candidate_rows(store) == 0


@pytest.mark.red_now
def test_p2_4_database_abort_leaves_no_partial_candidate(store):
    """P2.4: a SQLite abort trigger makes the write failure reachable; any
    partial candidate or returned success ID is forbidden, while a propagated
    database error and zero rows are demanded. Splitting payload fields across
    commits is the smallest mutation that turns red.
    """
    store.conn.execute(
        "CREATE TRIGGER fail_candidate_insert BEFORE INSERT ON "
        "capture_candidates BEGIN SELECT RAISE(ABORT, 'forced'); END"
    )
    store.conn.commit()
    with pytest.raises(Exception):
        _add_candidate(store)
    assert _candidate_rows(store) == 0


@pytest.mark.red_now
def test_p2_1_duplicate_live_payload_deduplicates_but_erasure_reopens(store):
    """P2.1/A2: identical canonical adds and terminal erase are reachable;
    two live review subjects are forbidden, while one reused ID then a new ID
    after erase are demanded. Dropping the partial payload-digest uniqueness
    rule is the smallest mutation that turns red.
    """
    first = _add_candidate(store, domains=["testing", "resilience"])
    duplicate = _add_candidate(
        store, domains=["resilience", "testing", "resilience"]
    )
    assert duplicate == first
    assert _candidate_rows(store) == 1
    rejected = store.reject_capture_candidate(
        first, reviewed_by="tester", disposition_code="not_supported", now=NOW
    )
    assert rejected["status"] == "rejected"
    assert store.erase_capture_candidate(first) is True
    second = _add_candidate(store)
    assert second != first
    assert _candidate_rows(store) == 1


@pytest.mark.red_now
def test_p3_4_p3_7_accept_atomically_creates_one_verified_node_and_edges(store):
    """P3.4/P3.7: a reviewed candidate with one resolvable and one missing
    endpoint reaches accept; overwrite/implicit endpoint/raw receipt are
    forbidden, while exactly one verified node and only resolvable edges are
    demanded. Replacing the transaction with add-or-replace is the smallest
    mutation that turns this red.
    """
    target = store.add_node(
        "Existing endpoint", content="must survive", node_id="endpoint"
    )
    store.verify_node(
        target, verified_by="reviewer", prov_method="direct inspection",
        verified_at=NOW,
    )
    candidate_id = _add_candidate(
        store,
        connections=[
            {
                "from_title": "Candidate claim", "to_title": "Existing endpoint",
                "type": "relates_to", "why": "resolvable",
            },
            {
                "from_title": "Candidate claim", "to_title": "Missing endpoint",
                "type": "relates_to", "why": "must not mint",
            },
        ],
    )
    token = store.candidate_review_token(candidate_id)
    accepted = store.accept_capture_candidate(
        candidate_id,
        review_token=token,
        reviewed_by="accepting tester",
        prov_method="fixture comparison",
        valid_at="2026-08-18T07:00:00-05:00",
        invalid_at="2026-08-19T12:00:00Z",
        now=NOW,
    )
    assert accepted["status"] == "accepted"
    created_id = accepted["created_node_id"]
    assert created_id and created_id != target
    created = store.get_node(created_id)
    assert created["title"] == "Candidate claim"
    assert created["verified_by"] == "accepting tester"
    assert created["prov_method"] == "fixture comparison"
    assert created["verified_at"] == NOW
    assert created["valid_at"] == NOW
    assert created["invalid_at"] == "2026-08-19T12:00:00Z"
    assert store.get_node(target)["content"] == "must survive"
    assert store.get_node_by_title("Missing endpoint") is None
    assert len([
        node for node in store.all_nodes(limit=100)
        if node["title"] == "Candidate claim"
    ]) == 1
    related = store.edges_from(created_id) + store.edges_to(created_id)
    assert related
    assert all(
        {edge["from_id"], edge["to_id"]} <= {created_id, target}
        for edge in related
    )
    _assert_minimized(store.get_capture_candidate(candidate_id))


@pytest.mark.red_now
def test_p3_7_edge_failure_rolls_back_node_edges_and_candidate(store):
    """P3.7: a resolvable proposal plus aborting edge trigger reaches the
    promotion transaction; a node, edge, or terminal candidate is forbidden,
    while the original pending subject is demanded. Committing node insertion
    before edge insertion is the smallest mutation that turns red.
    """
    target = store.add_node("Atomic target", node_id="atomic-target")
    candidate_id = _add_candidate(
        store,
        connections=[{
            "from_title": "Candidate claim", "to_title": "Atomic target",
            "type": "relates_to", "why": "force edge path",
        }],
    )
    token = store.candidate_review_token(candidate_id)
    store.conn.execute(
        "CREATE TRIGGER fail_edge_insert BEFORE INSERT ON edges "
        "BEGIN SELECT RAISE(ABORT, 'forced edge failure'); END"
    )
    store.conn.commit()
    with pytest.raises(Exception):
        store.accept_capture_candidate(
            candidate_id, review_token=token, reviewed_by="tester",
            prov_method="atomicity test", now=NOW,
        )
    assert store.get_node_by_title("Candidate claim") is None
    assert store.get_node(target) is not None
    assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert store.get_capture_candidate(candidate_id)["status"] == "pending"


@pytest.mark.red_now
def test_p3_2_relevant_graph_changes_make_review_token_stale(store):
    """P3.2: same-title, referenced verification/validity, and contradiction
    mutations each occur after show, including field-only SQL fixture changes
    that deliberately preserve updated_at; promotion under an old token is
    forbidden and StaleReviewError is demanded. Omitting any relevant-state
    component from token JSON is the smallest mutation that turns its case red.
    """
    from kindex.store import StaleReviewError

    # Same-title graph state.
    same = _add_candidate(store, title="Stale same title")
    same_token = store.candidate_review_token(same)
    store.add_node("Stale same title", node_id="same-title-node")
    with pytest.raises(StaleReviewError):
        store.accept_capture_candidate(
            same, review_token=same_token, reviewed_by="tester",
            prov_method="stale test", now=NOW,
        )

    # Referenced verification state, mutated without the updated_at shortcut.
    verification_ref = store.add_node(
        "Referenced verification state", node_id="referenced-verification"
    )
    store.verify_node(
        verification_ref, verified_by="original reviewer",
        prov_method="fixture", verified_at=NOW,
    )
    referenced_verification = _add_candidate(
        store, title="Referenced verification candidate",
        connections=[{
            "from_title": "Referenced verification candidate",
            "to_title": "Referenced verification state",
            "type": "relates_to", "why": "token input",
        }],
    )
    verification_token = store.candidate_review_token(referenced_verification)
    store.conn.execute(
        "UPDATE nodes SET verified_by=? WHERE id=?",
        ("changed reviewer", verification_ref),
    )
    store.conn.commit()
    with pytest.raises(StaleReviewError):
        store.accept_capture_candidate(
            referenced_verification, review_token=verification_token,
            reviewed_by="tester",
            prov_method="stale test", now=NOW,
        )

    # Referenced validity state, also mutated without changing updated_at.
    validity_ref = store.add_node(
        "Referenced validity state", node_id="referenced-validity"
    )
    store.verify_node(
        validity_ref, verified_by="tester", prov_method="fixture",
        verified_at=NOW,
    )
    referenced_validity = _add_candidate(
        store, title="Referenced validity candidate",
        connections=[{
            "from_title": "Referenced validity candidate",
            "to_title": "Referenced validity state",
            "type": "relates_to", "why": "token input",
        }],
    )
    validity_token = store.candidate_review_token(referenced_validity)
    store.conn.execute(
        "UPDATE nodes SET valid_at=? WHERE id=?",
        ("2026-08-18T12:00:01Z", validity_ref),
    )
    store.conn.commit()
    with pytest.raises(StaleReviewError):
        store.accept_capture_candidate(
            referenced_validity, review_token=validity_token,
            reviewed_by="tester", prov_method="stale test", now=NOW,
        )

    # Contradiction edge rows among referenced durable nodes.
    left = store.add_node("Referenced left", node_id="left")
    right = store.add_node("Referenced right", node_id="right")
    edged = _add_candidate(
        store, title="Edge-state candidate",
        connections=[
            {"from_title": "Edge-state candidate", "to_title": "Referenced left",
             "type": "relates_to", "why": "token input"},
            {"from_title": "Edge-state candidate", "to_title": "Referenced right",
             "type": "relates_to", "why": "token input"},
        ],
    )
    edge_token = store.candidate_review_token(edged)
    store.add_edge(left, right, edge_type="contradicts", provenance="new fact")
    with pytest.raises(StaleReviewError):
        store.accept_capture_candidate(
            edged, review_token=edge_token, reviewed_by="tester",
            prov_method="stale test", now=NOW,
        )


@pytest.mark.red_now
def test_p3_5_status_and_expiry_are_rechecked_inside_accept(store):
    """P3.5: rejection and exact expiry equality are reachable before accept;
    node creation from terminal/expired state is forbidden and typed state
    errors are demanded. Removing the in-transaction status/expiry check is the
    smallest mutation that turns red.
    """
    from kindex.store import CandidateStateError

    rejected_id = _add_candidate(store, title="Rejected candidate")
    rejected_token = store.candidate_review_token(rejected_id)
    store.reject_capture_candidate(
        rejected_id, reviewed_by="tester", disposition_code="reject", now=NOW
    )
    with pytest.raises(CandidateStateError):
        store.accept_capture_candidate(
            rejected_id, review_token=rejected_token, reviewed_by="tester",
            prov_method="state test", now=NOW,
        )

    expiring_id = _add_candidate(
        store, title="Expiring candidate", ttl_days=1
    )
    expiring_token = store.candidate_review_token(expiring_id)
    with pytest.raises(CandidateStateError):
        store.accept_capture_candidate(
            expiring_id, review_token=expiring_token, reviewed_by="tester",
            prov_method="expiry test", now="2026-08-19T12:00:00Z",
        )
    assert store.get_node_by_title("Rejected candidate") is None
    assert store.get_node_by_title("Expiring candidate") is None


@pytest.mark.red_now
def test_p3_4_accept_reject_audit_values_and_time_are_validated(store):
    """P3.4/P4.3: blank audit text and malformed/naive/leap/invalid intervals
    reach accept validation; promotion is forbidden and ValueError is demanded.
    Stripping one non-empty validation check is the smallest mutation red.
    """
    cases = [
        {"reviewed_by": "   ", "prov_method": "method"},
        {"reviewed_by": "who", "prov_method": "\t"},
        {"reviewed_by": "who", "prov_method": "method",
         "valid_at": "2026-08-18T12:00:00"},
        {"reviewed_by": "who", "prov_method": "method",
         "valid_at": "2026-08-18T11:59:60Z"},
        {"reviewed_by": "who", "prov_method": "method",
         "valid_at": "2026-08-19T00:00:00Z",
         "invalid_at": "2026-08-19T00:00:00Z"},
    ]
    for index, case in enumerate(cases):
        candidate_id = _add_candidate(store, title=f"Audit case {index}")
        token = store.candidate_review_token(candidate_id)
        with pytest.raises(ValueError):
            store.accept_capture_candidate(
                candidate_id, review_token=token, now=NOW, **case
            )
        assert store.get_capture_candidate(candidate_id)["status"] == "pending"
        assert store.get_node_by_title(f"Audit case {index}") is None


@pytest.mark.red_now
def test_p3_6_collision_denied_even_with_fresh_token(store):
    """P3.6: an exact same-title node is created before a fresh token; overwrite
    or a second same-title node is forbidden and TitleCollisionError is
    demanded. Deleting the collision check is the smallest mutation that turns
    red.
    """
    from kindex.store import TitleCollisionError

    candidate_id = _add_candidate(store, title="Collision boundary")
    existing = store.add_node(
        "Collision boundary", content="original", node_id="collision-original"
    )
    fresh = store.candidate_review_token(candidate_id)
    with pytest.raises(TitleCollisionError):
        store.accept_capture_candidate(
            candidate_id, review_token=fresh, reviewed_by="tester",
            prov_method="collision test", now=NOW,
        )
    assert store.get_node(existing)["content"] == "original"
    assert len([
        row for row in store.all_nodes(limit=100)
        if row["title"] == "Collision boundary"
    ]) == 1


@pytest.mark.red_now
def test_p3_6_conflict_uses_current_verified_counterpart_only(store):
    """P3.6/P4.5: verified, invalidated, and never-verified counterparts reach
    explicit contradiction proposals; a node/snippet during current conflict is
    forbidden, while ID/code evidence then promotion after invalidation or for
    an unverified endpoint is demanded. Removing independent endpoint trust
    evaluation is the smallest mutation that turns this red.
    """
    target = store.add_node(
        "Current counterclaim", content="sensitive counter text", node_id="counter"
    )
    store.verify_node(
        target, verified_by="reviewer", prov_method="fixture", verified_at=NOW
    )
    candidate_id = _add_candidate(
        store, title="Contradicting candidate",
        connections=[{
            "from_title": "Contradicting candidate",
            "to_title": "Current counterclaim", "type": "contradicts",
            "why": "explicit conflict",
        }],
    )
    conflicted = store.accept_capture_candidate(
        candidate_id,
        review_token=store.candidate_review_token(candidate_id),
        reviewed_by="tester", prov_method="conflict review", now=NOW,
    )
    assert conflicted["status"] == "conflicted"
    assert target in conflicted["conflict_ids"]
    assert conflicted["conflict_codes"]
    assert "sensitive counter text" not in json.dumps(conflicted["conflict_codes"])
    assert store.get_node_by_title("Contradicting candidate") is None

    store.invalidate_node(
        target, invalidated_by="tester", disposition_code="superseded",
        invalid_at=NOW,
    )
    accepted = store.accept_capture_candidate(
        candidate_id,
        review_token=store.candidate_review_token(candidate_id),
        reviewed_by="tester", prov_method="conflict resolved", now=NOW,
    )
    assert accepted["status"] == "accepted"

    unverified = store.add_node(
        "Unverified counterclaim", content="not admitted", node_id="unverified"
    )
    unblocked_id = _add_candidate(
        store, title="Unblocked candidate",
        connections=[{
            "from_title": "Unblocked candidate",
            "to_title": "Unverified counterclaim", "type": "contradicts",
            "why": "poison probe",
        }],
    )
    unblocked = store.accept_capture_candidate(
        unblocked_id,
        review_token=store.candidate_review_token(unblocked_id),
        reviewed_by="tester", prov_method="poison review", now=NOW,
    )
    assert unblocked["status"] == "accepted"
    assert store.get_node(unverified)["verified_at"] is None


@pytest.mark.red_now
def test_p3_9_reject_prune_minimize_and_erase_receipts(store):
    """P3.9/I4: reject and equality-boundary prune reach terminal receipts;
    retained raw payload/snippets are forbidden, while digests/status and exact
    erase are demanded. Stopping the terminal NULL update is the smallest
    mutation that turns red.
    """
    rejected_id = _add_candidate(
        store, title="Reject secret", content="SECRET_REJECT_PAYLOAD"
    )
    rejected = store.reject_capture_candidate(
        rejected_id, reviewed_by="tester", disposition_code="not_current", now=NOW
    )
    assert rejected["status"] == "rejected"
    _assert_minimized(rejected)
    assert rejected["source_digest"] == SOURCE_DIGEST
    assert "SECRET_REJECT_PAYLOAD" not in json.dumps(rejected)

    expiring_id = _add_candidate(
        store, title="Expire secret", content="SECRET_EXPIRE_PAYLOAD", ttl_days=1
    )
    assert store.prune_capture_candidates(
        now="2026-08-19T12:00:00Z"
    ) == 1
    expired = store.get_capture_candidate(expiring_id)
    assert expired["status"] == "expired"
    _assert_minimized(expired)
    assert "SECRET_EXPIRE_PAYLOAD" not in json.dumps(expired)
    assert store.erase_capture_candidate(rejected_id) is True
    assert store.get_capture_candidate(rejected_id) is None
    assert store.erase_capture_candidate(rejected_id) is False


def _race(root, setup, left_op, right_op):
    cfg = Config(data_dir=str(root))
    initial = Store(cfg)
    candidate_id, token = setup(initial)
    initial.close()
    barrier = threading.Barrier(2)

    def invoke(operation):
        local = Store(Config(data_dir=str(root)))
        try:
            barrier.wait(timeout=10)
            try:
                value = operation(local, candidate_id, token)
                return ("value", value)
            except ValueError as exc:
                return ("typed_error", type(exc).__name__)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, (left_op, right_op)))
    final = Store(Config(data_dir=str(root)))
    return final, candidate_id, outcomes


@pytest.mark.red_now
def test_p3_8_terminal_races_have_at_most_one_winner_and_no_lock_leak(tmp_path):
    """P3.8: two independent Store connections race accept/reject,
    accept/prune, and accept/accept through a barrier; two terminal winners,
    two nodes, or raw sqlite lock leakage are forbidden, while one terminal
    receipt and at most one node are demanded. Removing both the in-transaction
    live-state precheck and conditional terminal predicate is the smallest
    behavioral mutation that turns red.
    """
    def setup(store, *, ttl_days=7):
        candidate_id = _add_candidate(
            store, title="Race candidate", ttl_days=ttl_days
        )
        return candidate_id, store.candidate_review_token(candidate_id)

    def accept(store, candidate_id, token):
        return store.accept_capture_candidate(
            candidate_id, review_token=token, reviewed_by="racer",
            prov_method="race test", now=NOW,
        )["status"]

    def reject(store, candidate_id, _token):
        return store.reject_capture_candidate(
            candidate_id, reviewed_by="racer", disposition_code="race", now=NOW
        )["status"]

    def prune(store, _candidate_id, _token):
        return f"pruned:{store.prune_capture_candidates(now='2026-08-19T12:00:00Z')}"

    cases = [
        ("accept-reject", lambda s: setup(s), accept, reject),
        ("accept-prune", lambda s: setup(s, ttl_days=1), accept, prune),
        ("accept-accept", lambda s: setup(s), accept, accept),
    ]
    for name, fixture, left, right in cases:
        final, candidate_id, outcomes = _race(
            tmp_path / name, fixture, left, right
        )
        try:
            candidate = final.get_capture_candidate(candidate_id)
            assert candidate["status"] in {"accepted", "rejected", "expired"}
            node_count = len([
                row for row in final.all_nodes(limit=100)
                if row["title"] == "Race candidate"
            ])
            assert node_count <= 1
            terminal_values = [
                value for kind, value in outcomes
                if kind == "value" and value in {
                    "accepted", "rejected", "pruned:1"
                }
            ]
            assert len(terminal_values) == 1, outcomes
            final.add_node(f"No lock leak {name}")
        finally:
            final.close()


def _hook_env(tmp_path):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("KIN_") or key.endswith("_API_KEY"):
            env.pop(key, None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


@pytest.mark.red_now
def test_p2_5_compact_candidate_write_failure_is_host_fail_soft(tmp_path):
    """P2.5: an abort trigger makes compact-hook candidate persistence fail;
    nonzero host exit, success wording, partial candidate, or fallback node is
    forbidden, while exit zero and empty stores are demanded. Restoring direct
    add_node fallback is the smallest reversion that turns red.
    """
    data = tmp_path / "data"
    store = Store(Config(data_dir=str(data)))
    store.conn.execute(
        "CREATE TRIGGER fail_hook_candidate BEFORE INSERT ON capture_candidates "
        "BEGIN SELECT RAISE(ABORT, 'forced hook failure'); END"
    )
    store.conn.commit()
    store.close()
    text = (
        "We learned that the compact quarantine invariant protects durable "
        "knowledge from an automatic transcript extraction failure."
    )
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "compact-hook",
         "--data-dir", str(data)],
        input=text, capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path), env=_hook_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    for success_word in ("successfully", "staged", "captured", "learned", "added"):
        assert success_word not in combined
    check = Store(Config(data_dir=str(data)))
    try:
        assert check.all_nodes(limit=100) == []
        assert check.list_capture_candidates(limit=100) == []
    finally:
        check.close()


@pytest.mark.red_now
def test_i2_compact_hook_has_no_direct_persistence_or_accept_reachability():
    """I2/A6: AST traversal reaches every call in cmd_compact_hook; direct
    add_node, add_edge, or candidate acceptance is forbidden and only staging
    reachability is demanded. Reintroducing one forbidden call is the smallest
    mutation that turns this guard red.
    """
    from kindex.cli import cmd_compact_hook

    tree = ast.parse(inspect.getsource(cmd_compact_hook))
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "add_capture_candidate" in called
    assert {"add_node", "add_edge", "accept_capture_candidate"}.isdisjoint(called)
