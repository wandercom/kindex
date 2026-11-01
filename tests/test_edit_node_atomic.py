"""Behavioral regression tests for atomic Store.edit_node.

Real SQLite triggers and deterministic two-handle interleavings exercise
all-or-nothing writes, transaction cleanup, committed-state validation,
and concurrent appends. Read committed outcomes through an independent handle.
Most cases expose baseline defects; the explicitly labeled embedding companion
preserves an existing behavior and does not prove the exception-warning branch.
"""

import json
import logging
import sqlite3

import pytest

import kindex.vectors as vectors
from kindex.config import Config
from kindex.locks import lock_node, unlock_node
from kindex.store import EditPolicyError, LockHeldError, Store


@pytest.fixture
def stores(tmp_path):
    """Two independent Store handles on one disposable database."""
    a = Store(Config(data_dir=str(tmp_path)))
    b = Store(Config(data_dir=str(tmp_path)))
    yield a, b
    a.close()
    b.close()


def _edit_rows(store, nid):
    rows = store.conn.execute(
        "SELECT target_title, actor, details FROM activity_log "
        "WHERE action = 'edit_node' AND target_id = ? ORDER BY id",
        (nid,),
    ).fetchall()
    return [
        {
            "target_title": r["target_title"],
            "actor": r["actor"],
            "details": json.loads(r["details"]),
        }
        for r in rows
    ]


def _queue(store):
    return json.loads(store.get_meta(vectors.EMBED_QUEUE_META) or "[]")


def _raw_row(store, nid):
    """The committed nodes row minus last_accessed (R1.2 excludes it)."""
    return store.conn.execute(
        "SELECT type, title, content, aka, intent, domains, status, "
        "extra, updated_at FROM nodes WHERE id = ?",
        (nid,),
    ).fetchone()


def _row_tuple(store, nid):
    row = _raw_row(store, nid)
    return None if row is None else tuple(row)


class _InterleaveAtFirstBegin:
    """Run fn once, at A's first traced BEGIN (contract seam).

    Records fn's result or exception instead of raising inside the
    sqlite3 trace callback, so the test can assert B actually committed
    before judging A's outcome (reach, per Testing Strategy ground
    rules). A's connection is warmed by the seeding calls before this
    callback is installed, so the first BEGIN seen belongs to the
    edit_node call under test.
    """

    def __init__(self, fn):
        self._fn = fn
        self.fired = False
        self.b_result = None
        self.b_error = None

    def __call__(self, sql):
        if self.fired:
            return
        if sql.lstrip().upper().startswith("BEGIN"):
            self.fired = True
            try:
                self.b_result = self._fn()
            except BaseException as exc:  # recorded, asserted by the test
                self.b_error = exc

    def assert_b_committed(self):
        assert self.fired, "interleave seam never fired: no BEGIN traced"
        assert self.b_error is None, (
            f"handle B failed at the seam, oracle would be vacuous: "
            f"{self.b_error!r}"
        )


def _run_interleaved(a, interleave, call):
    """Install the seam on A's connection, run A's edit, always remove it."""
    a.conn.set_trace_callback(interleave)
    try:
        return call()
    finally:
        a.conn.set_trace_callback(None)


# ── R1.2: a failed field write leaves the whole edit unapplied ────────


@pytest.mark.parametrize(
    "planned_case",
    ["R1.2-content-fail-leaves-expiry"],
    ids=["R1.2-content-fail-leaves-expiry"],
)
def test_r1_2_content_fail_leaves_expiry(planned_case, stores):
    """cites: product-spec R1.2/R1.5 + testing-strategy R1.2.

    Red at base for the partial-edit reason: baseline commits the expiry
    before the failing content UPDATE, so extra.expires changes although
    the call failed. Named-test for Gate D (T31/T42): mutating the fix
    to commit the expiry separately again must turn THIS test red.
    Review 4 strengthening (assertions added, none moved): after the
    failure, handle A commits and then completes a valid retry, and the
    failed values are re-asserted absent through B - a failed edit left
    pending in A's open transaction would be made durable by that
    commit/retry and turn this red, although B alone could not see it
    before.
    Falsifier: any column (except last_accessed), activity row or queue
    entry differing after the failed call, no sqlite3.Error raised, or
    the failed content/expiry becoming durable after A's later commit
    or retry.
    """
    a, b = stores
    nid = a.add_node("Content fail", content="original body",
                     node_type="concept")
    a.set_meta(vectors.EMBED_QUEUE_META, "[]")
    before = _row_tuple(b, nid)
    a.conn.execute(
        "CREATE TRIGGER fail_content_write BEFORE UPDATE OF content "
        "ON nodes WHEN NEW.content <> OLD.content "
        "BEGIN SELECT RAISE(ABORT, 'injected content failure'); END"
    )
    a.conn.commit()
    try:
        with pytest.raises(sqlite3.Error, match="injected content failure"):
            a.edit_node(nid, content="replacement body",
                        expires="2031-01-01")
    finally:
        a.conn.execute("DROP TRIGGER IF EXISTS fail_content_write")
        a.conn.commit()
    assert _row_tuple(b, nid) == before
    assert b.peek_node(nid)["extra"].get("expires") is None
    assert _edit_rows(b, nid) == []
    assert _queue(b) == []
    # Review 4: the assertions above already ran after the finally
    # block's commit on A; make the flush explicit and add a committing
    # retry on the same handle, then re-check the failed values through
    # B. Nothing of the failed edit may become durable at either point.
    a.conn.commit()
    assert _row_tuple(b, nid) == before
    a.edit_node(nid, intent="after failure")
    seen = b.peek_node(nid)
    assert seen["content"] == "original body"
    assert seen["extra"].get("expires") is None
    assert seen["intent"] == "after failure"
    assert len(_edit_rows(b, nid)) == 1  # the retry's row only
    assert _queue(b) == []


# ── R1.4: the changelog row is required, not best-effort ──────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R1.4-audit-fail-append-unapplied"],
    ids=["R1.4-audit-fail-append-unapplied"],
)
def test_r1_4_audit_fail_append_unapplied(planned_case, stores):
    """cites: product-spec R1.4/R1.5 + testing-strategy R1.4.

    Red at base: baseline swallows the audit failure and returns with
    the append applied and zero edit_node rows.
    Review 4 strengthening (assertions added, none moved): the field
    UPDATE succeeds before the database rejects the activity row, so
    this is the case where a failed edit could sit pending in A's open
    transaction, invisible to B. After the failure, A commits whatever
    it may still hold and the row is re-read through B: a pending
    UPDATE made durable by that commit turns this red.
    Falsifier: the call returning a node dict, the addendum visible
    through the second handle after the audit write failed, or the
    addendum becoming durable after A's subsequent commit.
    """
    a, b = stores
    nid = a.add_node("Audit append", content="original body",
                     node_type="concept")
    before = _row_tuple(b, nid)
    a.conn.execute(
        "CREATE TEMP TRIGGER fail_audit_write BEFORE INSERT "
        "ON activity_log WHEN NEW.action = 'edit_node' "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    with pytest.raises(sqlite3.Error, match="injected audit failure"):
        a.edit_node(nid, append="must not land")
    assert _row_tuple(b, nid) == before
    assert "must not land" not in b.peek_node(nid)["content"]
    assert _edit_rows(b, nid) == []
    # Review 4: flush anything A left pending, then re-check through B.
    a.conn.commit()
    assert _row_tuple(b, nid) == before
    assert "must not land" not in b.peek_node(nid)["content"]
    assert _edit_rows(b, nid) == []


@pytest.mark.parametrize(
    "planned_case",
    ["R1.4-audit-fail-expiry-only-unapplied"],
    ids=["R1.4-audit-fail-expiry-only-unapplied"],
)
def test_r1_4_audit_fail_expiry_only_unapplied(planned_case, stores):
    """cites: product-spec R1.4/R1.5 + testing-strategy R1.4.

    Red at base: an expiry-only edit commits the expiry and returns
    although its changelog row was never written.
    Review 4 strengthening (assertions added, none moved): after the
    failure, A commits whatever it may still hold and the row is
    re-read through B, so an expiry left pending in A's open
    transaction cannot become durable unobserved.
    Falsifier: extra.expires changed, no sqlite3.Error raised, or the
    expiry becoming durable after A's subsequent commit.
    """
    a, b = stores
    nid = a.add_node("Audit expiry", content="original body",
                     node_type="watch")
    before = _row_tuple(b, nid)
    a.conn.execute(
        "CREATE TEMP TRIGGER fail_audit_write BEFORE INSERT "
        "ON activity_log WHEN NEW.action = 'edit_node' "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    with pytest.raises(sqlite3.Error, match="injected audit failure"):
        a.edit_node(nid, expires="2031-01-01")
    assert _row_tuple(b, nid) == before
    assert b.peek_node(nid)["extra"].get("expires") is None
    assert _edit_rows(b, nid) == []
    # Review 4: flush anything A left pending, then re-check through B.
    a.conn.commit()
    assert _row_tuple(b, nid) == before
    assert b.peek_node(nid)["extra"].get("expires") is None
    assert _edit_rows(b, nid) == []


# ── R1.6: the same handle stays usable after a failed edit ────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R1.6-handle-usable-after-failure"],
    ids=["R1.6-handle-usable-after-failure"],
)
def test_r1_6_handle_usable_after_failure(planned_case, stores):
    """cites: product-spec R1.6 + testing-strategy R1.6.

    The first half re-uses the R1.4 failure (red at base: that call
    returns instead of raising). Falsifier for the reuse half: a
    transaction left open by the failure, so the retry on the same
    handle fails or its commit is invisible to the second handle.
    Review 4 strengthening (assertions added, none moved): the failed
    addendum is re-asserted absent through B after the drop-trigger
    commit on A and again after the successful retry - that commit is
    exactly where a still-pending failed UPDATE would become durable.
    Falsifier (added): "first attempt" present in the committed content
    at either re-check.
    """
    a, b = stores
    nid = a.add_node("Reuse", content="original body", node_type="concept")
    a.conn.execute(
        "CREATE TRIGGER fail_audit_once BEFORE INSERT ON activity_log "
        "WHEN NEW.action = 'edit_node' "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    a.conn.commit()
    with pytest.raises(sqlite3.Error, match="injected audit failure"):
        a.edit_node(nid, append="first attempt")
    assert "first attempt" not in b.peek_node(nid)["content"]
    a.conn.execute("DROP TRIGGER fail_audit_once")
    a.conn.commit()
    # Review 4: that commit on A is where a still-pending failed UPDATE
    # would become durable; re-check through B before and after retry.
    assert "first attempt" not in b.peek_node(nid)["content"]
    node = a.edit_node(nid, append="second attempt")
    assert "second attempt" in node["content"]
    assert "second attempt" in b.peek_node(nid)["content"]
    assert "first attempt" not in b.peek_node(nid)["content"]
    assert len(_edit_rows(b, nid)) == 1


# ── R2.1/R2.2: interleaved appends never silently lose an addendum ────


@pytest.mark.parametrize(
    "planned_case",
    ["R2.1-interleaved-append-no-silent-loss"],
    ids=["R2.1-interleaved-append-no-silent-loss"],
)
def test_r2_1_interleaved_append_no_silent_loss(planned_case, stores):
    """cites: product-spec R2.1/R2.2 + testing-strategy R2.1.

    Red at base (Validator-observed): B's append commits, A returns
    successfully, and B's addendum is lost. Named-test for Gate D:
    computing A's append from a pre-transaction read must turn THIS
    test red. The raised branch is not vacuous: B's success is asserted
    first, and the branch then requires A's addendum to be absent with
    an explicit exception (R2.2 allows either outcome once the lock is
    held; silent loss is the only forbidden one).
    Falsifier: A returned while either addendum is missing, or A raised
    while its addendum (or a partial edit) is present.
    """
    a, b = stores
    nid = a.add_node("Contested", content="base body", node_type="concept")
    b.peek_node(nid)  # warm B's connection before the seam is installed
    interleave = _InterleaveAtFirstBegin(
        lambda: b.edit_node(nid, actor="agent-b",
                            append="bravo addendum from B")
    )
    a_result, a_error = None, None
    try:
        a_result = _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="alpha addendum from A"),
        )
    except Exception as exc:  # explicit-conflict branch, judged below
        a_error = exc
    interleave.assert_b_committed()
    content = b.peek_node(nid)["content"]
    assert "bravo addendum from B" in content
    if a_error is None:
        assert "alpha addendum from A" in content
        assert "alpha addendum from A" in a_result["content"]
    else:
        assert isinstance(a_error, sqlite3.Error)
        assert "alpha addendum from A" not in content
        assert [r for r in _edit_rows(b, nid)
                if r["actor"] == "agent-a"] == []


@pytest.mark.parametrize(
    "planned_case",
    ["R2.2-interleaved-append-on-committed-content"],
    ids=["R2.2-interleaved-append-on-committed-content"],
)
def test_r2_2_interleaved_append_on_committed_content(planned_case, stores):
    """cites: product-spec R2.2/R3.3 + testing-strategy R2.2.

    Red at base: A overwrites content computed from its stale read, so
    B's addendum is gone and A's content diff old is the stale base.
    Falsifier: when A returned, B's addendum not preceding A's, either
    addendum lacking its own header, or A's content diff old not
    containing B's addendum. When A raised an explicit conflict, the
    falsifier is any trace of A's addendum or an agent-a row.
    """
    a, b = stores
    nid = a.add_node("Ordered", content="base body", node_type="concept")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: b.edit_node(nid, actor="agent-b",
                            append="bravo addendum from B")
    )
    a_error = None
    try:
        _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="alpha addendum from A"),
        )
    except Exception as exc:
        a_error = exc
    interleave.assert_b_committed()
    content = b.peek_node(nid)["content"]
    a_rows = [r for r in _edit_rows(b, nid) if r["actor"] == "agent-a"]
    if a_error is None:
        assert content.count("[addendum") == 2
        assert content.index("bravo addendum from B") < content.index(
            "alpha addendum from A"
        )
        assert len(a_rows) == 1
        old = a_rows[0]["details"]["diffs"]["content"]["old"]
        assert "bravo addendum from B" in old
    else:
        assert isinstance(a_error, sqlite3.Error)
        assert "alpha addendum from A" not in content
        assert "bravo addendum from B" in content
        assert a_rows == []


# ── R2.3: write-lock timeout is an explicit conflict, nothing applied ──


@pytest.mark.parametrize(
    "planned_case",
    ["R2.3-write-lock-timeout-explicit-conflict"],
    ids=["R2.3-write-lock-timeout-explicit-conflict"],
)
def test_r2_3_write_lock_timeout_explicit_conflict(planned_case, tmp_path):
    """cites: product-spec R2.3/R1 + testing-strategy R2.3.

    Phase 1 (explicit conflict): a raw third connection holds
    BEGIN IMMEDIATE; an edit on a handle with sqlite_timeout=0.1 raises
    sqlite3.OperationalError and applies nothing.
    Phase 2 (the discriminating half): the raw writer is acquired from
    the trace callback just after the expiry commit. At base the expiry
    is already committed by then, so the append times out with the
    expiry applied - a partial edit. A correct single-transaction edit
    has no intermediate COMMIT, so phase 2 either raises with nothing
    applied or returns with everything applied; partial is the only
    forbidden outcome. Phase 2 runs on its own fresh handle and node so
    handle state left by phase 1's failure (R1.6's subject) cannot mask
    it, and it asserts the seam actually armed before judging the
    outcome (Review 1 repair).
    Falsifier: phase 1 applying anything or raising a non-Operational
    error; phase 2's seam not arming, or phase 2 ending with exactly one
    of {append, expiry} applied.
    """
    a = Store(Config(data_dir=str(tmp_path)), sqlite_timeout=0.1)
    b = Store(Config(data_dir=str(tmp_path)))
    try:
        nid = a.add_node("Locked db", content="base body",
                         node_type="concept")
        raw = sqlite3.connect(str(a.db_path))
        try:
            raw.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError):
                a.edit_node(nid, append="phase one addendum")
        finally:
            raw.rollback()
            raw.close()
        seen = b.peek_node(nid)
        assert "phase one addendum" not in seen["content"]
        assert _edit_rows(b, nid) == []

        # Phase 2 is independent of phase 1: its own fresh handle (phase 1's
        # failure may leave handle a inside an open transaction at base -
        # R1.6's subject, not R2.3's) and its own fresh node.
        a2 = Store(Config(data_dir=str(tmp_path)), sqlite_timeout=0.1)
        try:
            nid2 = b.add_node("Locked db phase two", content="base body",
                              node_type="concept")
            a2.peek_node(nid2)  # warm a2's connection before installing seam
            raw2 = sqlite3.connect(str(a2.db_path))
            # Acquire the raw writer on the first statement after the COMMIT
            # that follows the statement carrying the expiry value. Traced
            # SQL is parameter-expanded, so the expiry write is recognizable
            # by the date literal it carries ("after the expiry commit",
            # testing-strategy R2.3) - earlier unrelated commits (baseline
            # access-time bookkeeping) do not arm the seam. At head the only
            # COMMIT after that statement is the edit's own final commit, so
            # the writer can at most block the post-commit enqueue, and the
            # fully-applied branch below is what a correct edit shows.
            state = {"saw_expiry": False, "armed": False, "acquired": False}

            def acquire_after_expiry_commit(sql):
                if state["acquired"]:
                    return
                if state["armed"]:
                    state["acquired"] = True
                    raw2.execute("BEGIN IMMEDIATE")
                    return
                if "2031-09-09" in sql:
                    state["saw_expiry"] = True
                elif state["saw_expiry"] and sql.lstrip().upper().startswith(
                    "COMMIT"
                ):
                    state["armed"] = True

            a_result, a_error = None, None
            a2.conn.set_trace_callback(acquire_after_expiry_commit)
            try:
                a_result = a2.edit_node(nid2, append="phase two addendum",
                                        expires="2031-09-09")
            except Exception as exc:
                a_error = exc
            finally:
                a2.conn.set_trace_callback(None)
                raw2.rollback()
                raw2.close()
            # Reach: the seam must have armed - the expiry-carrying
            # statement and its COMMIT were both traced on a2's connection.
            # Without this, a failure before the expiry write could make
            # the "raised and applied nothing" branch pass vacuously.
            assert state["saw_expiry"], (
                "phase 2 seam never saw the expiry-carrying statement"
            )
            assert state["armed"], (
                "phase 2 seam never armed: no COMMIT followed the "
                "expiry-carrying statement"
            )
            seen = b.peek_node(nid2)
            has_append = "phase two addendum" in seen["content"]
            has_expiry = seen["extra"].get("expires") == "2031-09-09"
            if a_error is None:
                assert has_append and has_expiry, (
                    "returned edit must be fully applied"
                )
                assert a_result["extra"]["expires"] == "2031-09-09"
            else:
                assert isinstance(a_error, sqlite3.Error)
                assert not has_append and not has_expiry, (
                    "failed edit must apply nothing, not the expiry half"
                )
                assert _edit_rows(b, nid2) == []
        finally:
            a2.close()
    finally:
        a.close()
        b.close()


# ── R3.1: a lock committed mid-edit is honored ────────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.1-lock-committed-mid-edit-honored"],
    ids=["R3.1-lock-committed-mid-edit-honored"],
)
def test_r3_1_lock_committed_mid_edit_honored(planned_case, stores):
    """cites: product-spec R3.1 + testing-strategy R3.1.

    Red at base (Validator-observed): A applies its append over B's
    committed lock because the non-expiry path checks a stale snapshot.
    Reachability: B's lock_node must succeed at the seam, and the
    LockHeldError - not an earlier unrelated refusal - is the outcome,
    with B's lock still intact afterwards.
    Falsifier: A's addendum present, LockHeldError not raised, or B's
    lock disturbed by A's refused call.
    """
    a, b = stores
    nid = a.add_node("Lock race", content="base body", node_type="concept")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: lock_node(b, nid, agent="agent-b")
    )
    with pytest.raises(LockHeldError):
        _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="alpha addendum from A"),
        )
    interleave.assert_b_committed()
    seen = b.peek_node(nid)
    assert "alpha addendum from A" not in seen["content"]
    assert seen["extra"]["lock"]["agent"] == "agent-b"
    assert [r for r in _edit_rows(b, nid) if r["actor"] == "agent-a"] == []


# ── R3.0: refusals never come from a stale read ───────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.0-unlock-or-unarchive-mid-edit-applies"],
    ids=["R3.0-unlock-or-unarchive-mid-edit-applies"],
)
def test_r3_0_unlock_or_unarchive_mid_edit_applies(planned_case, stores):
    """cites: product-spec R3.0 + testing-strategy R3.0/R3.1 variant.

    Red at base: A refuses on the lock/archive state it read before B
    removed it. Both sub-cases set the refusing state before A's call
    and have B remove it at the seam, so a refusal can only come from
    the stale read.
    Falsifier: LockHeldError/EditPolicyError raised although the
    committed state at write-lock time no longer refuses, or the
    addendum missing after a returned call.
    """
    a, b = stores
    # Unlock sub-case.
    nid = a.add_node("Unlock race", content="base body",
                     node_type="concept")
    lock_node(b, nid, agent="agent-b")
    interleave = _InterleaveAtFirstBegin(
        lambda: unlock_node(b, nid, agent="agent-b")
    )
    node = _run_interleaved(
        a, interleave,
        lambda: a.edit_node(nid, actor="agent-a",
                            append="applied after unlock"),
    )
    interleave.assert_b_committed()
    assert interleave.b_result is True
    assert "applied after unlock" in node["content"]
    assert "applied after unlock" in b.peek_node(nid)["content"]

    # Un-archive sub-case.
    nid = a.add_node("Unarchive race", content="base body",
                     node_type="concept")
    b.update_node(nid, status="archived")
    interleave = _InterleaveAtFirstBegin(
        lambda: b.update_node(nid, status="active")
    )
    node = _run_interleaved(
        a, interleave,
        lambda: a.edit_node(nid, actor="agent-a",
                            append="applied after unarchive"),
    )
    interleave.assert_b_committed()
    assert "applied after unarchive" in node["content"]
    assert "applied after unarchive" in b.peek_node(nid)["content"]


# ── R3.2: lifecycle changes committed mid-edit are honored ────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.2-supersede-committed-mid-edit-honored"],
    ids=["R3.2-supersede-committed-mid-edit-honored"],
)
def test_r3_2_supersede_committed_mid_edit_honored(planned_case, stores):
    """cites: product-spec R3.2 + testing-strategy R3.2.

    Red at base (Validator-observed): A edits a node B has just
    superseded. Falsifier: A's addendum on the superseded node, or the
    refusal not naming B's successor id.
    """
    a, b = stores
    nid = a.add_node("Supersede race", content="base body",
                     node_type="decision")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: b.supersede_node(nid, "Replacement decision text",
                                 actor="agent-b")
    )
    with pytest.raises(EditPolicyError) as excinfo:
        _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="alpha addendum from A"),
        )
    interleave.assert_b_committed()
    new_id = interleave.b_result["id"]
    assert new_id in str(excinfo.value)
    seen = b.peek_node(nid)
    assert seen["status"] == "superseded"
    assert "alpha addendum from A" not in seen["content"]


@pytest.mark.parametrize(
    "planned_case",
    ["R3.2-archive-committed-mid-edit-honored"],
    ids=["R3.2-archive-committed-mid-edit-honored"],
)
def test_r3_2_archive_committed_mid_edit_honored(planned_case, stores):
    """cites: product-spec R3.2/R4.2 + testing-strategy R3.2.

    Red at base: A appends although B archived the node after A's
    snapshot read. The force half then pins the preserved half of the
    rule on the same node: with force the edit applies.
    Falsifier: the unforced addendum present, EditPolicyError not
    raised, or the forced edit refusing afterwards.
    """
    a, b = stores
    nid = a.add_node("Archive race", content="base body",
                     node_type="concept")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: b.update_node(nid, status="archived")
    )
    with pytest.raises(EditPolicyError):
        _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="unforced addendum"),
        )
    interleave.assert_b_committed()
    seen = b.peek_node(nid)
    assert seen["status"] == "archived"
    assert "unforced addendum" not in seen["content"]
    node = a.edit_node(nid, actor="agent-a", force=True,
                       append="forced addendum")
    assert "forced addendum" in node["content"]


# ── R3.3: tag merges and diff olds come from the committed row ────────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.3-diff-old-from-committed-row"],
    ids=["R3.3-diff-old-from-committed-row"],
)
def test_r3_3_diff_old_from_committed_row(planned_case, stores):
    """cites: product-spec R3.3 + testing-strategy R3.3.

    Red at base: A's tag write is computed from its stale tag list, so
    B's tag is dropped and A's tags diff old lacks it. (The content half
    of R3.3 is the R2.2 guard's diff-old assertion.)
    Falsifier: final tags missing either side's tag, or A's tags diff
    old not containing B's tag.
    """
    a, b = stores
    nid = a.add_node("Tag race", content="base body", node_type="concept",
                     domains=["seed"])
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: b.edit_node(nid, actor="agent-b", add_tags=["b-tag"])
    )
    _run_interleaved(
        a, interleave,
        lambda: a.edit_node(nid, actor="agent-a", add_tags=["a-tag"]),
    )
    interleave.assert_b_committed()
    domains = b.peek_node(nid)["domains"]
    assert "b-tag" in domains
    assert "a-tag" in domains
    a_rows = [r for r in _edit_rows(b, nid) if r["actor"] == "agent-a"]
    assert len(a_rows) == 1
    assert "b-tag" in a_rows[0]["details"]["diffs"]["tags"]["old"]


# ── R3.4: a node deleted mid-edit makes the call raise KeyError ───────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.4-delete-committed-mid-edit-keyerror"],
    ids=["R3.4-delete-committed-mid-edit-keyerror"],
)
def test_r3_4_delete_committed_mid_edit_keyerror(planned_case, stores):
    """cites: product-spec R3.4 + testing-strategy R3.4.

    Red at base: A's UPDATE affects no row, yet an edit_node activity
    row is written and the call returns instead of raising KeyError.
    Falsifier: no KeyError, an edit_node row for the deleted id, or the
    node reappearing.
    """
    a, b = stores
    nid = a.add_node("Delete race", content="base body",
                     node_type="concept")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(lambda: b.delete_node(nid))
    with pytest.raises(KeyError):
        _run_interleaved(
            a, interleave,
            lambda: a.edit_node(nid, actor="agent-a",
                                append="alpha addendum from A"),
        )
    interleave.assert_b_committed()
    assert b.peek_node(nid) is None
    assert _edit_rows(b, nid) == []


# ── R3.5: a concurrent edit can turn A's call into a no-op ────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R3.5-concurrent-same-value-no-row"],
    ids=["R3.5-concurrent-same-value-no-row"],
)
def test_r3_5_concurrent_same_value_no_row(planned_case, stores):
    """cites: product-spec R3.5 + testing-strategy R3.5.

    Red at base: B sets the very title A requests, yet A still writes
    an edit_node row computed from its stale read.
    Queue note (T29 weaker check): B's title change legitimately queues
    this same node id during A's call, so A's own enqueue can only be
    observed as "the id appears exactly once"; the row-by-actor
    assertion is the discriminating half.
    Falsifier: an agent-a edit_node row, or A's call failing instead of
    returning the node.
    """
    a, b = stores
    nid = a.add_node("Original title", content="base body",
                     node_type="concept")
    b.peek_node(nid)
    a.set_meta(vectors.EMBED_QUEUE_META, "[]")
    interleave = _InterleaveAtFirstBegin(
        lambda: b.edit_node(nid, actor="agent-b", title="Shared title")
    )
    node = _run_interleaved(
        a, interleave,
        lambda: a.edit_node(nid, actor="agent-a", title="Shared title"),
    )
    interleave.assert_b_committed()
    assert node["title"] == "Shared title"
    rows = _edit_rows(b, nid)
    assert [r["actor"] for r in rows] == ["agent-b"]
    assert _queue(b).count(nid) == 1


# ── R4.8: a failed embedding enqueue warns and keeps the edit ─────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.8-embedding-failure-warns-and-keeps-edit"],
    ids=["R4.8-embedding-failure-warns-and-keeps-edit"],
)
def test_r4_8_embedding_failure_warns_and_keeps_edit(
    planned_case, stores, caplog
):
    """cites: product-spec R4.8 + testing-strategy R4.8.

    Red at base: the enqueue failure is silently dropped, so no WARNING
    names the node although the refresh was missed.
    Failure injected at the database seam (contract: the queue write is
    an INSERT OR REPLACE of meta key 'embed.queue' on store.conn, and
    fires on every enqueue). The trigger is installed after the queue
    reset and dropped before the second handle verifies the commit.
    Falsifier: the edit rolled back or raising because of the enqueue,
    or no WARNING record from logger kindex.store naming the node id.
    """
    a, b = stores
    nid = a.add_node("Embed fail", content="base body",
                     node_type="concept")
    a.set_meta(vectors.EMBED_QUEUE_META, "[]")
    a.conn.execute(
        "CREATE TRIGGER fail_embed_queue BEFORE INSERT ON meta "
        "WHEN NEW.key = 'embed.queue' "
        "BEGIN SELECT RAISE(ABORT, 'injected embed queue failure'); END"
    )
    a.conn.commit()
    try:
        with caplog.at_level(logging.WARNING, logger="kindex.store"):
            node = a.edit_node(nid, title="Embed fail v2")
    finally:
        a.conn.execute("DROP TRIGGER IF EXISTS fail_embed_queue")
        a.conn.commit()
    assert node["title"] == "Embed fail v2"
    assert b.peek_node(nid)["title"] == "Embed fail v2"
    assert len(_edit_rows(b, nid)) == 1
    warnings = [
        r for r in caplog.records
        if r.name == "kindex.store" and r.levelno >= logging.WARNING
        and nid in r.getMessage()
    ]
    assert warnings, (
        "no kindex.store WARNING naming the node id for the failed "
        f"enqueue; records: {[r.getMessage() for r in caplog.records]}"
    )


def test_r4_8_embedding_enqueue_raises_warns_and_keeps_edit(stores, caplog):
    """cites: product-spec R4.8 ("raises, or reports failure by returning
    false") + architecture A2 step 11; Review 2 item 1, Review 3
    reclassification. Green-now characterization (post-freeze addition
    from Review 2's adequacy finding, T41): a characterization of the
    channel-independent invariant - the edit is kept and the node is
    queued or warned about. It is NOT a defect guard for the raises
    channel: the Validator observed at base (freeze acff7b62) that the
    probe below did not raise under the malformed embed.queue state,
    the fallback branch ran, and the node was queued, so this test
    passes at base and at candidate. Recorded finding: the R4.8 raises
    channel is not producible through real boundaries without patching
    enqueue_embedding (forbidden, T16) - every database-seam failure is
    a write failure, which the contract states enqueue reports as
    False, and the malformed-queue state is recovered from, not raised
    on. The assertions below are unchanged by the reclassification;
    should the probe ever raise under some future state, the WARNING
    branch still judges the raises channel on its own terms.

    The companion R4.8 test exercises the reports-failure channel (a
    meta trigger makes the queue write fail, which the contract states
    enqueue reports as False). This test targets the raises channel
    without patching (T16): the embed.queue meta value in the real
    disposable database is seeded malformed ('{not json'), a state the
    queue reader must survive at the database boundary.
    Reach is proven, not assumed: the test first probes the public
    enqueue path (vectors.enqueue_embedding, the exact call edit_node
    makes after commit per the contract) on the second handle under the
    seeded state and records whether queueing raised there. The seeded
    state is then restored and the edit runs on the first handle.
    - If the probe raised, queueing demonstrably raises on this seam:
      the edit must still return the node, stay committed via the
      second handle, and emit a kindex.store WARNING naming the node.
      An implementation whose post-commit handler only warns on a
      False return (or propagates, or swallows) fails here.
    - If the probe did not raise (the observed outcome at base), this
      seam does not produce the raises case (queueing recovered or
      reported False); the test then asserts only the
      channel-independent invariant - edit kept, and the node either
      queued or warned about.
    Falsifier: the edit raising or rolling back because queueing raised;
    queueing having raised (probe) with no WARNING naming the node; or
    the node neither queued nor warned about.
    """
    a, b = stores
    nid = a.add_node("Embed raise", content="base body",
                     node_type="concept")
    # Probe the real enqueue path on handle B under the seeded state.
    b.set_meta(vectors.EMBED_QUEUE_META, "{not json")
    probe_error = None
    try:
        vectors.enqueue_embedding(b, nid)
    except Exception as exc:  # the raises channel, recorded as reach
        probe_error = exc
    finally:
        b.conn.rollback()  # release anything the probe left pending
    # Restore the exact seeded state (the probe may have rewritten it).
    a.set_meta(vectors.EMBED_QUEUE_META, "{not json")
    with caplog.at_level(logging.WARNING, logger="kindex.store"):
        node = a.edit_node(nid, title="Embed raise v2")
    assert node["title"] == "Embed raise v2"
    assert b.peek_node(nid)["title"] == "Embed raise v2"
    assert len(_edit_rows(b, nid)) == 1
    warnings = [
        r for r in caplog.records
        if r.name == "kindex.store" and r.levelno >= logging.WARNING
        and nid in r.getMessage()
    ]
    if probe_error is not None:
        assert warnings, (
            "queueing raised on the probe "
            f"({probe_error!r}) but no kindex.store WARNING naming the "
            f"node id followed the edit; records: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
    else:
        raw = b.get_meta(vectors.EMBED_QUEUE_META) or "[]"
        try:
            queued = nid in json.loads(raw)
        except ValueError:
            queued = False
        assert queued or warnings, (
            "queueing neither queued the node nor warned about it "
            f"(probe did not raise; queue meta now {raw!r})"
        )
