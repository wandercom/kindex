"""Preserved public behavior of Store.edit_node.

Characterize edit classes, force, aliases, reserved metadata, return and error
shapes, changelog entries, and embedding refresh using real disposable SQLite.
These expectations also pass on the pre-atomic-edit baseline.
"""

import json
import re
import sqlite3

import pytest

import kindex.vectors as vectors
from kindex.config import Config
from kindex.locks import lock_node
from kindex.store import (
    RESERVED_EXTRA_KEYS,
    EditPolicyError,
    LockHeldError,
    Store,
)


@pytest.fixture
def stores(tmp_path):
    """Two independent Store handles on one disposable database."""
    a = Store(Config(data_dir=str(tmp_path)))
    b = Store(Config(data_dir=str(tmp_path)))
    yield a, b
    a.close()
    b.close()


def _edit_rows(store, nid):
    """All edit_node activity rows for nid, oldest first, details parsed."""
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


def _reset_queue(store):
    store.set_meta(vectors.EMBED_QUEUE_META, "[]")


def _raw_extra(store, nid):
    row = store.conn.execute(
        "SELECT extra FROM nodes WHERE id = ?", (nid,)
    ).fetchone()
    return row["extra"]


class _InterleaveAtFirstBegin:
    """Run fn once, at A's first traced BEGIN (contract seam).

    Same seam as the defect-guard module; duplicated here so each module
    is self-contained (importing a test module would re-register its
    tests). Records fn's result/exception so the test can assert B
    actually committed before judging A's outcome.
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
    a.conn.set_trace_callback(interleave)
    try:
        return call()
    finally:
        a.conn.set_trace_callback(None)


# ── R1.3: a failed expiry write leaves the whole edit unapplied ───────


@pytest.mark.parametrize(
    "planned_case",
    ["R1.3-expiry-fail-leaves-content"],
    ids=["R1.3-expiry-fail-leaves-content"],
)
def test_r1_3_expiry_fail_leaves_content(planned_case, stores):
    """cites: product-spec R1.3 + testing-strategy R1.3 (characterization).

    Baseline writes the expiry first, so an expiry failure already leaves
    nothing applied at base; the atomic edit must preserve that.
    Falsifier (T31): an implementation that applies the append and then
    fails the expiry, or swallows the expiry error, leaves the addendum
    visible through the second handle or returns a node dict.
    Reach: the raised error carries the trigger's own message, proving the
    injected failure fired on the expiry write.
    """
    a, b = stores
    nid = a.add_node("Expiry fail", content="original body",
                     node_type="concept")
    before = b.peek_node(nid)
    a.conn.execute(
        "CREATE TRIGGER fail_expiry_write BEFORE UPDATE OF extra ON nodes "
        "WHEN NEW.extra <> OLD.extra "
        "BEGIN SELECT RAISE(ABORT, 'injected expiry failure'); END"
    )
    a.conn.commit()
    try:
        with pytest.raises(sqlite3.Error, match="injected expiry failure"):
            a.edit_node(nid, append="must not land", expires="2031-01-01")
    finally:
        a.conn.execute("DROP TRIGGER IF EXISTS fail_expiry_write")
        a.conn.commit()
    after = b.peek_node(nid)
    assert after["content"] == before["content"]
    assert "must not land" not in after["content"]
    assert after["extra"] == before["extra"]
    assert after["updated_at"] == before["updated_at"]
    assert _edit_rows(b, nid) == []


# ── R1.1 (supporting): a successful multi-field edit is fully visible ──


def test_r1_1_successful_edit_all_fields_visible_via_second_handle(stores):
    """cites: product-spec R1.1 + testing-strategy R1.1.

    The discriminating half of R1.1 is the failure-injection cases; this
    row's reach is the second handle seeing every changed column after one
    call, with exactly one edit_node row.
    Falsifier: a field of the same call invisible to the second handle, or
    more than one changelog row for the single call.
    """
    a, b = stores
    nid = a.add_node("Before", content="base body", node_type="concept",
                     domains=["keep"])
    _reset_queue(a)
    a.edit_node(nid, actor="agent-a", title="After", append="added bit",
                add_tags=["fresh"], expires="2031-06-01")
    seen = b.peek_node(nid)
    assert seen["title"] == "After"
    assert "added bit" in seen["content"]
    assert set(seen["domains"]) == {"keep", "fresh"}
    assert seen["extra"]["expires"] == "2031-06-01"
    assert len(_edit_rows(b, nid)) == 1
    assert _queue(b) == [nid]


# ── R2.2 companion: sequential appends through two handles ────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R2.2-sequential-appends-both-retained"],
    ids=["R2.2-sequential-appends-both-retained"],
)
def test_r2_2_sequential_appends_both_retained(planned_case, stores):
    """cites: product-spec R2.2 + testing-strategy R2.2 (characterization).

    Falsifier: the second handle's append overwriting the first handle's
    committed append instead of extending it, or addendum order inverted.
    """
    a, b = stores
    nid = a.add_node("Sequential", content="base body", node_type="concept")
    b.edit_node(nid, actor="agent-b", append="first sequential addendum")
    a.edit_node(nid, actor="agent-a", append="second sequential addendum")
    content = b.peek_node(nid)["content"]
    assert content.count("[addendum") == 2
    assert content.index("first sequential addendum") < content.index(
        "second sequential addendum"
    )


# ── R4.1: edit classes preserved ──────────────────────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.1-edit-classes-preserved"],
    ids=["R4.1-edit-classes-preserved"],
)
def test_r4_1_edit_classes_preserved(planned_case, stores):
    """cites: product-spec R4.1/R3.6 + testing-strategy R4.1.

    Falsifier: a managed type accepting any edit, an additive type
    accepting a replacing field, an editable type refusing a field, a
    policy_overrides re-mapping being ignored, any refusal that still
    mutates the row, or an activity row whose type is not the node's type.
    """
    a, b = stores
    # Managed: every edit refused, row unchanged.
    for node_type in ("task", "session", "coordination"):
        nid = a.add_node(f"Managed {node_type}", content="body",
                         node_type=node_type)
        with pytest.raises(EditPolicyError):
            a.edit_node(nid, append="nope")
        assert b.peek_node(nid)["content"] == "body"
        assert _edit_rows(b, nid) == []

    # Additive: replacing fields refused (row unchanged); append+expires ok.
    nid = a.add_node("A decision", content="We chose X.",
                     node_type="decision")
    for kwargs in (
        {"title": "We chose Y"},
        {"content": "rewritten"},
        {"add_tags": ["t"]},
        {"intent": "why"},
        {"aka": ["alias"]},
    ):
        with pytest.raises(EditPolicyError):
            a.edit_node(nid, **kwargs)
    seen = b.peek_node(nid)
    assert seen["title"] == "A decision"
    assert seen["content"] == "We chose X."
    assert seen["intent"] in (None, "")
    assert _edit_rows(b, nid) == []
    node = a.edit_node(nid, actor="alice", append="Clarified.",
                       expires="2030-01-01")
    assert "Clarified." in node["content"]
    assert node["extra"]["expires"] == "2030-01-01"
    # R3.6 companion: the activity row's type is the node's type.
    rows = _edit_rows(b, nid)
    assert len(rows) == 1
    assert rows[0]["details"]["type"] == "decision"

    # Editable: every field accepted in one call.
    nid = a.add_node("Concept", content="c", node_type="concept")
    node = a.edit_node(nid, title="Concept v2", content="c2",
                       add_tags=["t1"], intent="i", aka=["cv2"],
                       expires="2030-05-05")
    assert node["title"] == "Concept v2"
    assert node["content"] == "c2"
    assert node["intent"] == "i"
    assert "t1" in node["domains"]
    assert node["extra"]["expires"] == "2030-05-05"

    # policy_overrides re-maps classes exactly as at baseline.
    nid = a.add_node("Remapped", node_type="concept")
    with pytest.raises(EditPolicyError):
        a.edit_node(nid, title="X",
                    policy_overrides={"concept": "managed"})
    nid = a.add_node("Promoted", node_type="decision")
    node = a.edit_node(nid, title="Promoted v2",
                       policy_overrides={"decision": "editable"})
    assert node["title"] == "Promoted v2"


# ── R4.2: force semantics preserved ───────────────────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.2-force-semantics-preserved"],
    ids=["R4.2-force-semantics-preserved"],
)
def test_r4_2_force_semantics_preserved(planned_case, stores):
    """cites: product-spec R4.2 + testing-strategy R4.2/R3.1 companions.

    Falsifier: force failing to bypass a foreign lock or the archived
    refusal, force bypassing the superseded refusal, the lock holder or
    an expired lock being treated as foreign, or any refused call
    mutating the row.
    """
    a, b = stores
    # Foreign lock: refused without force, holder allowed, force bypasses.
    nid = a.add_node("Locked", content="body", node_type="concept")
    lock_node(b, nid, "agent-b")
    with pytest.raises(LockHeldError):
        a.edit_node(nid, actor="agent-a", title="steal")
    assert b.peek_node(nid)["title"] == "Locked"
    node = a.edit_node(nid, actor="agent-b", title="holder edit")
    assert node["title"] == "holder edit"
    node = a.edit_node(nid, actor="agent-a", force=True, title="forced")
    assert node["title"] == "forced"

    # Archived: refused without force, applied with force.
    nid = a.add_node("Archived", content="body", node_type="concept")
    a.update_node(nid, status="archived")
    with pytest.raises(EditPolicyError):
        a.edit_node(nid, append="nope")
    assert "nope" not in b.peek_node(nid)["content"]
    node = a.edit_node(nid, force=True, append="forced append")
    assert "forced append" in node["content"]

    # Superseded: refused even with force, naming the successor.
    nid = a.add_node("Old rule", content="v1", node_type="constraint")
    new = a.supersede_node(nid, "Rule v2 text", actor="alice")
    with pytest.raises(EditPolicyError) as excinfo:
        a.edit_node(nid, force=True, append="too late")
    assert new["id"] in str(excinfo.value)
    assert "too late" not in b.peek_node(nid)["content"]


# ── R4.3: rename aliases preserved ────────────────────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.3-rename-alias-preserved"],
    ids=["R4.3-rename-alias-preserved"],
)
def test_r4_3_rename_alias_preserved(planned_case, stores):
    """cites: product-spec R4.3 + testing-strategy R4.3.

    Falsifier: a rename losing the old title as an alias, duplicating it
    when already present under different case, or a case-only rename
    adding an alias.
    """
    a, b = stores
    nid = a.add_node("Alpha", node_type="concept")
    node = a.edit_node(nid, title="Beta")
    assert node["aka"] == ["Alpha"]

    nid = a.add_node("Gamma", node_type="concept")
    a.edit_node(nid, aka=["GAMMA"])
    node = a.edit_node(nid, title="Delta")
    assert node["aka"] == ["GAMMA"]  # case-insensitive duplicate not added

    nid = a.add_node("Epsilon", node_type="concept")
    node = a.edit_node(nid, title="EPSILON")
    assert node["title"] == "EPSILON"
    assert node["aka"] == []  # case-only rename adds nothing
    assert b.peek_node(nid)["aka"] == []


# ── R4.4: reserved and unrelated extra keys untouched ─────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.4-reserved-extra-untouched"],
    ids=["R4.4-reserved-extra-untouched"],
)
def test_r4_4_reserved_extra_untouched(planned_case, stores):
    """cites: product-spec R4.4 + testing-strategy R4.4.

    Falsifier: an expiry edit dropping, adding or altering any reserved
    or unrelated extra key, i.e. the merge writing a rebuilt extra object
    instead of changing only expires in the committed one.
    """
    a, b = stores
    nid = a.add_node("Guarded", content="body", node_type="watch")
    seeded = {key: {"sentinel": key} for key in RESERVED_EXTRA_KEYS}
    # The lock key must be a lock the editing actor holds, or the edit is
    # refused for an unrelated reason and never reaches the merge.
    seeded["lock"] = {"agent": "agent-a",
                      "expires_at": "2099-01-01T00:00:00"}
    seeded["owner"] = "me"
    seeded["custom"] = [1, 2, {"nested": True}]
    a.update_node(nid, extra=dict(seeded))
    node = a.edit_node(nid, actor="agent-a", expires="2031-06-01")
    expected = dict(seeded)
    expected["expires"] = "2031-06-01"
    assert node["extra"] == expected
    assert json.loads(_raw_extra(b, nid)) == expected


# ── R4.5: return shape and argument-error order ───────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.5-return-shape-and-error-order"],
    ids=["R4.5-return-shape-and-error-order"],
)
def test_r4_5_return_shape_and_error_order(planned_case, stores):
    """cites: product-spec R4.5/R3.0 + architecture A2 steps 1,4,9,12.

    Error order is derived from A2: unknown id, then no field (step 1);
    then edit class, lifecycle, lock, expiry format (step 4). Two faults
    are combined per call so the asserted type discriminates the order.
    Falsifier: a later-order error winning over an earlier one, any of
    these calls writing, or the returned dict differing from a fresh
    peek_node in keys or substantive values.
    Mask (T29): last_accessed and weight are access-time bookkeeping
    (R1.2 excludes last_accessed); both are checked only for presence of
    the key, every other key compares exactly.
    """
    a, b = stores
    nid = a.add_node("Shape", content="body", node_type="concept")
    ret = a.edit_node(nid, actor="alice", title="Shape v2")
    peeked = b.peek_node(nid)
    assert set(ret) == set(peeked)
    for key in peeked:
        if key in ("last_accessed", "weight"):
            continue
        assert ret[key] == peeked[key], key

    def assert_unchanged(node_id, snapshot):
        assert b.peek_node(node_id)["updated_at"] == snapshot["updated_at"]
        assert b.peek_node(node_id)["content"] == snapshot["content"]
        assert _edit_rows(b, node_id) == []

    with pytest.raises(KeyError):  # unknown id beats no-field
        a.edit_node("nonexistent-node-id")
    snap = b.peek_node(nid)
    with pytest.raises(ValueError):  # no field given
        a.edit_node(nid)

    archived = a.add_node("Order archived", content="body",
                          node_type="concept")
    a.update_node(archived, status="archived")
    snap_arch = b.peek_node(archived)
    with pytest.raises(EditPolicyError):  # lifecycle beats expiry format
        a.edit_node(archived, append="x", expires="not-a-date")
    assert_unchanged(archived, snap_arch)

    locked = a.add_node("Order locked", content="body", node_type="concept")
    lock_node(b, locked, "agent-b")
    snap_lock = b.peek_node(locked)
    with pytest.raises(LockHeldError):  # lock beats expiry format
        a.edit_node(locked, actor="agent-a", append="x",
                    expires="not-a-date")
    assert_unchanged(locked, snap_lock)

    managed = a.add_node("Order managed", content="body", node_type="task")
    snap_man = b.peek_node(managed)
    with pytest.raises(EditPolicyError):  # edit class beats expiry format
        a.edit_node(managed, append="x", expires="not-a-date")
    assert_unchanged(managed, snap_man)

    plain = a.add_node("Order plain", content="body", node_type="concept")
    snap_plain = b.peek_node(plain)
    with pytest.raises(ValueError):  # malformed expiry alone
        a.edit_node(plain, expires="not-a-date")
    assert_unchanged(plain, snap_plain)
    assert snap["content"] == "body"  # the successful edit above stands


def test_r4_5_class_and_lifecycle_beat_lock(stores):
    """cites: product-spec R4.5 + architecture A2 step 4 (edit class, then
    lifecycle, then lock, then expiry format); Review 2 item 2.
    Green-now characterization (post-freeze addition from Review 2's
    adequacy finding, T41): the order asserted is the baseline order A2
    preserves, so it holds at base and at head.

    The companion R4.5 test pairs class/lifecycle/lock only against a
    malformed expiry. Here a foreign unexpired lock is combined with a
    class refusal (managed type; additive type with a disallowed field)
    and with a lifecycle refusal (archived; superseded), so the raised
    type discriminates which check runs first: EditPolicyError must win
    over LockHeldError in every combination, applying nothing.
    Reach: each lock is a real lock_node by agent-b, asserted present in
    the committed extra before A's call, so the lock check was genuinely
    armed had it run first.
    Falsifier: LockHeldError winning over the class or lifecycle
    refusal in any combination, or any refused call mutating the row or
    writing an edit_node row.
    """
    a, b = stores

    def assert_refused_unchanged(node_id, snapshot, call):
        with pytest.raises(EditPolicyError):
            call()
        after = b.peek_node(node_id)
        assert after["content"] == snapshot["content"]
        assert after["title"] == snapshot["title"]
        assert after["updated_at"] == snapshot["updated_at"]
        assert _edit_rows(b, node_id) == []

    # Class (managed) beats lock.
    nid = a.add_node("Managed locked", content="body", node_type="task")
    lock_node(b, nid, agent="agent-b")
    snap = b.peek_node(nid)
    assert snap["extra"]["lock"]["agent"] == "agent-b"
    assert_refused_unchanged(
        nid, snap,
        lambda: a.edit_node(nid, actor="agent-a", append="nope"),
    )

    # Class (additive field violation) beats lock.
    nid = a.add_node("Additive locked", content="We chose X.",
                     node_type="decision")
    lock_node(b, nid, agent="agent-b")
    snap = b.peek_node(nid)
    assert snap["extra"]["lock"]["agent"] == "agent-b"
    assert_refused_unchanged(
        nid, snap,
        lambda: a.edit_node(nid, actor="agent-a", title="We chose Y"),
    )

    # Lifecycle (archived) beats lock.
    nid = a.add_node("Archived locked", content="body",
                     node_type="concept")
    lock_node(b, nid, agent="agent-b")
    b.update_node(nid, status="archived")
    snap = b.peek_node(nid)
    assert snap["extra"]["lock"]["agent"] == "agent-b"
    assert snap["status"] == "archived"
    assert_refused_unchanged(
        nid, snap,
        lambda: a.edit_node(nid, actor="agent-a", append="nope"),
    )

    # Lifecycle (superseded) beats lock. Seeded deterministically: the
    # node is superseded first, then a foreign lock is merged into its
    # extra (preserving superseded_by) via the contract's seeding path,
    # so both refusals are armed regardless of whether supersede itself
    # retains a prior lock.
    nid = a.add_node("Superseded locked", content="v1",
                     node_type="constraint")
    new = b.supersede_node(nid, "Rule v2 text", actor="agent-b")
    merged = dict(b.peek_node(nid)["extra"])
    merged["lock"] = {"agent": "agent-b",
                      "expires_at": "2099-01-01T00:00:00"}
    b.update_node(nid, extra=merged)
    snap = b.peek_node(nid)
    assert snap["status"] == "superseded"
    assert snap["extra"]["superseded_by"] == new["id"]
    assert snap["extra"]["lock"]["agent"] == "agent-b"
    with pytest.raises(EditPolicyError) as excinfo:
        a.edit_node(nid, actor="agent-a", append="nope")
    assert new["id"] in str(excinfo.value)
    after = b.peek_node(nid)
    assert after["content"] == snap["content"]
    assert after["updated_at"] == snap["updated_at"]
    assert _edit_rows(b, nid) == []


# ── R4.6: exactly one changelog row per applied edit, none for no-ops ──


@pytest.mark.parametrize(
    "planned_case",
    ["R4.6-single-changelog-row"],
    ids=["R4.6-single-changelog-row"],
)
def test_r4_6_single_changelog_row(planned_case, stores):
    """cites: product-spec R4.6 + testing-strategy R4.6.

    Falsifier: zero or two rows for one applied edit, a row for a
    no-change edit, a wrong target_title/actor/type, or diffs naming
    fields the call did not change. updated_at is deliberately not
    asserted for no-ops (R4.6 leaves it unconstrained).
    """
    a, b = stores
    nid = a.add_node("Before", content="old text", node_type="concept")
    a.edit_node(nid, actor="alice", title="After", content="new text")
    rows = _edit_rows(b, nid)
    assert len(rows) == 1
    assert rows[0]["actor"] == "alice"
    assert rows[0]["target_title"] == "After"
    assert rows[0]["details"]["type"] == "concept"
    diffs = rows[0]["details"]["diffs"]
    assert diffs["title"] == {"old": "Before", "new": "After"}
    assert diffs["content"] == {"old": "old text", "new": "new text"}
    assert set(diffs) <= {"title", "content", "intent", "aka", "tags",
                          "expires"}

    # No-change edits: same title, then the current expiry.
    _reset_queue(a)
    node = a.edit_node(nid, title="After")
    assert node["title"] == "After"
    assert len(_edit_rows(b, nid)) == 1
    assert _queue(b) == []
    a.edit_node(nid, expires="2030-02-02")
    assert len(_edit_rows(b, nid)) == 2
    _reset_queue(a)
    a.edit_node(nid, expires="2030-02-02")
    assert len(_edit_rows(b, nid)) == 2
    assert _queue(b) == []


# ── R4.11: baseline value computation preserved ───────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.11-value-computation-preserved"],
    ids=["R4.11-value-computation-preserved"],
)
def test_r4_11_value_computation_preserved(planned_case, stores):
    """cites: product-spec R4.11 + testing-strategy R4.11.

    Each expected value is derived by applying the stated baseline rule:
    content replaces then append extends; empty append/tag lists change
    nothing; tags are removed then added; a rename's alias lands after an
    explicit aka; malformed extra JSON reads as {} and extra is written
    only when the expiry changes; a rename from an empty title adds no
    empty alias. Every sub-case differs under the obvious alternative
    rule (append-then-replace, add-then-remove, alias-before-aka), so
    each assertion discriminates.
    Falsifier: any sub-case result matching the alternative rule.
    """
    a, b = stores
    # content + append in one call: replacement first, addendum on top.
    nid = a.add_node("Combo", content="original", node_type="concept")
    node = a.edit_node(nid, actor="alice", content="Replacement",
                       append="extra bit")
    assert re.fullmatch(
        r"Replacement\n\n\[addendum \d{4}-\d{2}-\d{2} \d{2}:\d{2} alice\]\n"
        r"extra bit",
        node["content"],
    )

    # Empty append changes no content (carried by a real title change).
    node = a.edit_node(nid, title="Combo v2", append="")
    assert node["content"] == b.peek_node(nid)["content"]
    assert node["content"].count("[addendum") == 1

    # Empty tag lists change no tags; remove-then-add order is exact:
    # [a,b,c] remove b -> [a,c]; add b -> [a,c,b]; add d -> [a,c,b,d].
    # Add-then-remove would drop b entirely, giving [a,c,d].
    nid = a.add_node("Tags", node_type="concept", domains=["a", "b", "c"])
    node = a.edit_node(nid, intent="tagging", add_tags=[], remove_tags=[])
    assert node["domains"] == ["a", "b", "c"]
    node = a.edit_node(nid, remove_tags=["b"], add_tags=["b", "d"])
    assert node["domains"] == ["a", "c", "b", "d"]

    # title + explicit aka: the rename alias lands after the explicit aka.
    nid = a.add_node("Old name", node_type="concept")
    node = a.edit_node(nid, title="New name", aka=["nn"])
    assert node["aka"] == ["nn", "Old name"]

    # Malformed extra JSON: read as {}, written only on an expiry change.
    nid = a.add_node("Bad extra", content="body", node_type="concept")
    b.conn.execute("UPDATE nodes SET extra = '{not json' WHERE id = ?",
                   (nid,))
    b.conn.commit()
    node = a.edit_node(nid, title="Bad extra v2")
    assert node["title"] == "Bad extra v2"
    assert _raw_extra(b, nid) == "{not json"  # untouched without expiry
    nid2 = a.add_node("Bad extra 2", content="body", node_type="concept")
    b.conn.execute("UPDATE nodes SET extra = '[1,2]' WHERE id = ?",
                   (nid2,))
    b.conn.commit()
    node = a.edit_node(nid2, expires="2031-03-03")
    assert node["extra"] == {"expires": "2031-03-03"}  # non-object -> {}

    # Rename from an empty title: no empty-string alias is fabricated.
    nid = a.add_node("Temp title", node_type="concept")
    b.conn.execute("UPDATE nodes SET title = '' WHERE id = ?", (nid,))
    b.conn.commit()
    node = a.edit_node(nid, title="Named now")
    assert node["aka"] == []


# ── R4.7: embedding refresh queued only for title/content changes ─────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.7-embedding-enqueue-on-title-content-only"],
    ids=["R4.7-embedding-enqueue-on-title-content-only"],
)
def test_r4_7_embedding_enqueue_on_title_content_only(planned_case, stores):
    """cites: product-spec R4.7 + testing-strategy R4.7.

    The queue is reset after seeding (add_node also queues), so each
    assertion observes only the edit under test. The failed-edit half of
    R4.7 is asserted by the R1.2/R1.4 guards (queue unchanged).
    Falsifier: a tags/intent-only edit queueing the node, a title or
    content edit not queueing it, or the id appearing more than once.
    """
    a, b = stores
    nid = a.add_node("Queue", content="body", node_type="concept")

    _reset_queue(a)
    a.edit_node(nid, title="Queue v2")
    assert _queue(b) == [nid]

    _reset_queue(a)
    a.edit_node(nid, append="more body")
    assert _queue(b) == [nid]

    _reset_queue(a)
    a.edit_node(nid, add_tags=["tag-only"])
    assert _queue(b) == []

    _reset_queue(a)
    a.edit_node(nid, intent="intent only")
    assert _queue(b) == []


# ── R4.9: redaction and FTS preserved ─────────────────────────────────


@pytest.mark.parametrize(
    "planned_case",
    ["R4.9-redaction-preserved"],
    ids=["R4.9-redaction-preserved"],
)
def test_r4_9_redaction_preserved(planned_case, stores):
    """cites: product-spec R4.9 + testing-strategy R4.9.

    Oracle is agreement between two paths sharing one decision (T1.5):
    the text edit_node stores for a credential-shaped input must equal
    what update_node stores for the same input, and neither may retain
    the credential. The credential shape is one the existing privacy
    suite lists as supported ("ghp_" + 36 chars, tests/test_privacy.py).
    Reach: the update_node result differs from the raw input, proving
    redaction actually fired on this input.
    Falsifier: the credential surviving in stored content or in the
    activity row details, the two paths redacting differently, or FTS
    not finding the newly committed benign word.
    """
    a, b = stores
    credential = "ghp_" + "x" * 36
    payload = f"zebraquinoa token {credential} end"

    via_update = a.add_node("Via update", content="seed",
                            node_type="concept")
    a.update_node(via_update, content=payload)
    expected = b.peek_node(via_update)["content"]
    assert credential not in expected
    assert expected != payload

    via_edit = a.add_node("Via edit", content="seed", node_type="concept")
    a.edit_node(via_edit, append=payload)
    stored = b.peek_node(via_edit)["content"]
    assert credential not in stored
    assert stored.endswith(expected)
    rows = _edit_rows(b, via_edit)
    assert len(rows) == 1
    assert credential not in json.dumps(rows[0]["details"])

    hits = a.fts_search("zebraquinoa")
    assert any(hit["id"] == via_edit for hit in hits)


# ── R4.4 interleaving variant: a mid-edit lock survives the expiry merge ──


def test_r4_4_lock_committed_mid_edit_survives_expiry_write(stores):
    """cites: product-spec R4.4/R3.3 + testing-strategy R4.4 (variant).

    Characterization (moved from the defect-guard module per Review 1:
    the Validator observed this green at base, because baseline's expiry
    write already starts from the committed extra). It is a preservation
    guard: it fails if the expiry merge ever moves back to a pre-lock
    snapshot. Assertions unchanged by the move; the R4.4 planned-case
    id is carried by test_r4_4_reserved_extra_untouched above.
    B's lock lands in extra at the seam; A (force=True, so the lock does
    not refuse it) writes a new expiry.
    Falsifier: extra.lock missing or altered after A's expiry write, as
    a merge computed from A's pre-lock snapshot would leave it.
    """
    a, b = stores
    nid = a.add_node("Expiry race", content="base body",
                     node_type="concept")
    b.peek_node(nid)
    interleave = _InterleaveAtFirstBegin(
        lambda: lock_node(b, nid, agent="agent-b")
    )
    node = _run_interleaved(
        a, interleave,
        lambda: a.edit_node(nid, actor="agent-a", force=True,
                            append="forced addendum",
                            expires="2031-04-04"),
    )
    interleave.assert_b_committed()
    expected_lock = interleave.b_result
    assert node["extra"]["expires"] == "2031-04-04"
    assert node["extra"]["lock"] == expected_lock
    assert b.peek_node(nid)["extra"]["lock"] == expected_lock
    assert "forced addendum" in b.peek_node(nid)["content"]
