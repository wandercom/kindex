"""Independent advancing-activity grace and terminal-review receipt appendix.

Oracle: parent-ratified H3 refinement and public discarded review receipt schema.
No implementation source read and no tests executed by the Tester.
"""
import pytest

from test_health import b, issues, session


@pytest.mark.parametrize("evidence_kind,code", [("hook", "missing_hooks"), ("use", "missing_use")])
@pytest.mark.parametrize("native_advance,should_raise", [(6, False), (301, True)])
def test_health_grace_tracks_native_activity_advance_not_idle_wall_time(b, evidence_kind, code,
                                                                      native_advance, should_raise):
    """Ratified H3 refinement: elapsed native work determines the grace gap.

    At a check ten minutes after prior hook/use, native work six seconds later
    remains within grace; native work 301 seconds later exceeds 300-second grace.
    Both native events remain within the 20-minute observation window.

    Mutation: subtract hook/use from checker wall time, or ignore advancing work.
    """
    b.configure(active_seconds=1200, hook_grace_seconds=300, use_grace_seconds=300)
    b.record("activity", -600, active=True, source="native")
    b.record("hook", -600)
    if evidence_kind == "use":
        b.record("use", -600, tool="search", initiator="agent", outcome="success")
    b.native_claude(age=600 - native_advance)
    report = b.check()
    assert session(report)["activity_evidence"] == "native_observed"
    relevant = [issue for issue in issues(report, code)
                if issue["scope"]["session_id"] == b.scope["session_id"]]
    assert bool(relevant) is should_raise


@pytest.mark.parametrize("initial_state", ["queued", "completed"])
@pytest.mark.parametrize("terminal_reason", ["stale", "superseded"])
def test_discard_receipts_settle_only_matching_review_without_delivery_or_value(b, initial_state, terminal_reason):
    """Ratified receipt: review discarded/stale|superseded settles its review_id.

    Two overdue review IDs exist. Discarding the first leaves the second due;
    an unknown ID has no effect; discarding the second clears the issue.
    No receipt represents delivery or explicit human feedback.

    Mutation: globally clear queues, settle arbitrary IDs, ignore discard, or
    manufacture a delivery/usefulness signal from a terminal receipt.
    """
    b.seed_active()
    for review_id, offset in (("review-alpha", -1001), ("review-beta", -1000)):
        details = {"state": initial_state, "review_id": review_id}
        if initial_state == "completed":
            details["reason"] = "advisory"
        b.record("review", offset, **details)
    assert issues(b.check(), "undelivered_review")

    b.record("review", -5, state="discarded", reason=terminal_reason, review_id="review-alpha")
    assert issues(b.check(), "undelivered_review")

    b.record("review", -4, state="discarded", reason=terminal_reason, review_id="unknown-review")
    assert issues(b.check(), "undelivered_review")

    b.record("review", -3, state="discarded", reason=terminal_reason, review_id="review-beta")
    settled = b.check()
    assert not issues(settled, "undelivered_review")
    summary = session(settled)
    assert summary["counts"].get("delivery", 0) == 0
    assert summary["last"].get("delivery") is None
    assert summary["value"] == "unverified"
