"""Run-v8 acceptance oracles for schedule-independent weight decay.

Authority: product specification ``58916cd0...`` R2.1--R2.5 and I1;
architecture ``de100e45...`` decay constraints; strategy ``ede46bf4...``.
Time is controlled by a mocked ``kindex.store.datetime`` clock.  Fixture rows
and decay metadata are therefore written only by the system under test; each
schedule advances wall time and invokes decay at that instant.
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

import kindex.store as store_mod
from kindex.config import Config
from kindex.store import Store


HALF_LIFE_DAYS = 90
HOURS_PER_DAY = 24
ABS_TOL = 5e-4


def _store(tmp_path, name: str) -> Store:
    return Store(Config(data_dir=str(tmp_path / name)))


def _shift_timestamp(value: str, *, hours: float) -> str:
    """Move an epoch-or-ISO timestamp backward by ``hours``."""
    raw = str(value).strip()
    try:
        return str(float(raw) - hours * 3600.0)
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    if "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    parsed = dt.datetime.fromisoformat(normalized)
    shifted = parsed - dt.timedelta(hours=hours)
    separator = "T" if "T" in raw else " "
    rendered = shifted.isoformat(sep=separator)
    if "." not in raw and "." in rendered:
        rendered = rendered.split(".", 1)[0]
    if raw.endswith("Z"):
        rendered = rendered.replace("+00:00", "Z")
    return rendered


def _set_node_age(store: Store, node_id: str, *, hours: float) -> None:
    row = store.conn.execute(
        "SELECT last_accessed FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    assert row and row[0], f"node {node_id!r} lacks a timestamp"
    store.conn.execute(
        "UPDATE nodes SET last_accessed = ? WHERE id = ?",
        (_shift_timestamp(row[0], hours=hours), node_id),
    )
    store.conn.commit()


def _weight(store: Store, node_id: str) -> float:
    row = store.conn.execute(
        "SELECT weight FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    assert row is not None
    return float(row[0])


def _meta_keys(store: Store) -> set[str]:
    return {row[0] for row in store.conn.execute("SELECT key FROM meta")}


class _Clock(dt.datetime):
    """Test clock injected at the declared store datetime seam."""

    current = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value if tz is not None else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.current.replace(tzinfo=None)


def _clocked(monkeypatch):
    """Install a deterministic wall clock and return its advancement helper."""
    clock = type("RunClock", (_Clock,), {})
    monkeypatch.setattr(store_mod, "datetime", clock)

    def advance(hours: float) -> None:
        clock.current = clock.current + dt.timedelta(hours=hours)

    return clock, advance


def _cold_start(store: Store) -> str:
    before = _meta_keys(store)
    store.apply_weight_decay()
    after = _meta_keys(store)
    created = sorted(
        key for key in (_meta_keys(store) - before) if "decay" in key.lower()
    )
    assert created, f"first decay run must mint a decay checkpoint; created={created}"
    preferred = [
        key for key in after
        if key == "decay.last_run" or ("decay" in key.lower() and "last" in key.lower())
    ]
    candidates = preferred or created
    timestamped = [key for key in candidates if store.get_meta(key)]
    assert timestamped, f"decay checkpoint candidates have no value: {candidates}"
    return sorted(timestamped)[0]


def _advance(
        store: Store,
        checkpoint: str,
        *,
        hours: float,
        node_half_life_days: int = HALF_LIFE_DAYS,
) -> None:
    """Legacy state-shift helper retained for the non-repaired guard rows."""
    current = store.get_meta(checkpoint)
    assert current, f"missing checkpoint {checkpoint!r}"
    store.set_meta(checkpoint, _shift_timestamp(current, hours=hours))
    store.apply_weight_decay(node_half_life_days=node_half_life_days)


def _scheduled_weight(
        tmp_path,
        name: str,
        intervals: list[float],
        monkeypatch,
) -> float:
    total = sum(intervals)
    _, advance = _clocked(monkeypatch)
    store = _store(tmp_path, name)
    try:
        store.add_node(
            "Schedule probe",
            content="a non-noop decay fixture",
            node_id="probe",
            weight=1.0,
        )
        checkpoint = _cold_start(store)
        for interval in intervals:
            advance(interval)
            store.apply_weight_decay()
        return _weight(store, "probe")
    finally:
        store.close()


def _closed_form(initial: float, hours: float, half_life_days: float = 90) -> float:
    return max(0.01, initial * 0.5 ** (hours / (half_life_days * 24.0)))


@pytest.mark.red_now
def test_r2_1_phase_shifted_schedules_end_at_same_closed_form(
        tmp_path, monkeypatch):
    """R2.1 headline: 24h+47h and 23h+46h+47h agree at hour 47.

    Reversion: reinstating the one-day fold gate leaves the two paths near
    0.9923 and 0.9853 and turns this red.  Reachability/non-degeneracy: each DB
    first mints a real checkpoint, its node is then 47 hours old, and the
    mocked clock advances by [24, 23] versus [23, 23, 1] hours while the system
    writes its own checkpoint after each call.  Both schedules contain a fold
    on base and neither is a cold-start no-op.  Equality to the independently
    computed closed form (absolute
    tolerance 5e-4), not merely equality to each other, discriminates a common
    no-op.  Base evidence: 0.30.1 produces the named 0.9923/0.9853 phase split,
    failing for the staircase reason in R2.1.
    """
    schedule_a = _scheduled_weight(
        tmp_path, "schedule-a", [24, 23], monkeypatch
    )
    schedule_b = _scheduled_weight(
        tmp_path, "schedule-b", [23, 23, 1], monkeypatch
    )
    expected = _closed_form(1.0, 47)

    assert math.isclose(schedule_a, expected, abs_tol=ABS_TOL), (
        f"24h+47h phase ended at {schedule_a:.6f}, expected {expected:.6f}; "
        f"other phase={schedule_b:.6f}"
    )
    assert math.isclose(schedule_b, expected, abs_tol=ABS_TOL), (
        f"23h+46h+47h phase ended at {schedule_b:.6f}, expected {expected:.6f}; "
        f"other phase={schedule_a:.6f}"
    )
    assert math.isclose(schedule_a, schedule_b, abs_tol=1e-6)


@pytest.mark.red_now
def test_r2_1_many_subday_runs_equal_one_long_fold(tmp_path, monkeypatch):
    """R2.1 sub-day clause: 71 hourly runs equal one 71-hour fold.

    Reversion: a 24-hour gate folds only the completed 48 hours of the hourly
    schedule while the single run folds all 71, turning this red.
    Reachability/non-degeneracy: both nodes are 71 hours old after a real cold
    checkpoint; one path calls decay once after a 71-hour shift and the other
    calls it 71 times after one-hour shifts.  The long path must move below
    0.99, proving the fixture reaches the fold, and both are compared with the
    closed form to reject two equal no-ops.  Base evidence: 0.30.1 retains the
    final 23-hour staircase residue on the hourly path, the sub-day R2.1 defect.
    """
    one_fold = _scheduled_weight(tmp_path, "one-fold", [71], monkeypatch)
    hourly = _scheduled_weight(tmp_path, "hourly", [1] * 71, monkeypatch)
    expected = _closed_form(1.0, 71)

    assert one_fold < 0.99, f"reachability control: long fold was a no-op ({one_fold})"
    assert math.isclose(one_fold, expected, abs_tol=ABS_TOL)
    assert math.isclose(hourly, expected, abs_tol=ABS_TOL), (
        f"71 hourly runs ended at {hourly:.6f}; one fold={one_fold:.6f}, "
        f"closed form={expected:.6f}"
    )


def test_r2_2_immediate_second_run_changes_nothing(tmp_path):
    """R2.2: an immediate second run is a no-op without gating the first.

    Reversion: applying a per-invocation multiplier, failing to advance the
    checkpoint, or rounding a zero interval into decay turns this guard red.
    Reachability/non-degeneracy: after cold start, a 48-hour-old node and
    checkpoint drive a measured first fold; ``first < 0.99`` proves it moved.
    The immediate second call then has an empty interval, so the 1e-7 equality
    assertion discriminates accidental compounding.  Base evidence: 0.30.1
    passes this green-now contract; R2.2 must remain green after removing the
    schedule gate.
    """
    store = _store(tmp_path, "immediate")
    try:
        store.add_node("Immediate probe", node_id="probe", weight=1.0)
        checkpoint = _cold_start(store)
        _set_node_age(store, "probe", hours=48)
        _advance(store, checkpoint, hours=48)
        first = _weight(store, "probe")
        assert first < 0.99, f"reachability control: measured fold was a no-op ({first})"
        store.apply_weight_decay()
        second = _weight(store, "probe")
        assert math.isclose(second, first, abs_tol=1e-7), (
            f"immediate second run compounded decay: {first} -> {second}"
        )
    finally:
        store.close()


def test_r2_3_first_ever_run_stamps_checkpoint_and_decays_nothing(tmp_path):
    """R2.3: cold start establishes accounting without retroactive decay.

    Reversion: folding from ``last_accessed`` before a checkpoint exists, or
    omitting the checkpoint row, turns this guard red.  Reachability/non-
    degeneracy: a node is backdated a full 90-day half-life before its first
    call, so an incorrect retroactive fold would visibly halve it; a new
    decay-named meta row proves the cold-start branch ran.  Base evidence:
    0.30.1 passes this green-now behavior and must continue to do so under
    R2.3.
    """
    store = _store(tmp_path, "cold")
    try:
        store.add_node("Cold probe", node_id="cold", weight=1.0)
        _set_node_age(store, "cold", hours=90 * 24)
        before = _meta_keys(store)
        store.apply_weight_decay()
        after = _meta_keys(store)
        created = [key for key in after - before if "decay" in key.lower()]
        assert created, f"cold start did not mint a checkpoint: {created}"
        assert math.isclose(_weight(store, "cold"), 1.0, abs_tol=1e-9)
    finally:
        store.close()


@pytest.mark.red_now
def test_r2_4_floor_adjacent_weight_cannot_starve_under_daily_cadence(
        tmp_path, monkeypatch):
    """R2.4: small per-run deltas accumulate instead of freezing forever.

    Reversion: advancing global accounting whenever an individual write is
    below its threshold leaves this 0.0101 weight unchanged and turns this red.
    Reachability/non-degeneracy: after cold start the node is 30 days old and
    the real checkpoint is advanced through thirty one-day calls using the
    public 1000-day half-life argument.  Each base delta rounds below storage
    precision, while the accumulated closed form is below the 0.01 floor, so a
    correct implementation must produce the floor and base freezes at 0.0101.
    The initial/final inequality and exact floor assertion discriminate both a
    no-op and an over-decay.  Base evidence: 0.30.1 fails by permanent
    floor-adjacent starvation, precisely R2.4.
    """
    store = _store(tmp_path, "starvation")
    try:
        _, advance = _clocked(monkeypatch)
        initial = 0.0101
        store.add_node("Floor-adjacent probe", node_id="probe", weight=initial)
        checkpoint = _cold_start(store)
        for _ in range(30):
            advance(24)
            store.apply_weight_decay(node_half_life_days=1000)
        final = _weight(store, "probe")
        assert final < initial - 5e-5, (
            f"floor-adjacent weight starved across 30 daily runs: {initial} -> {final}"
        )
        assert math.isclose(final, 0.01, abs_tol=1e-9), (
            f"30-day closed form is below floor; expected 0.01, got {final}"
        )
    finally:
        store.close()


def test_r2_5_floor_fresh_and_future_timestamp_guards(tmp_path):
    """R2.5: floor holds; fresh/future access is neither decayed nor boosted.

    Reversion: removing the clamp, using a negative age exponent, or ignoring
    ``max(last_accessed, checkpoint)`` turns this green-now guard red.
    Reachability/non-degeneracy: one measured 30-day fold contains three
    distinct rows: an ancient 0.011 node whose raw result crosses the floor, a
    just-accessed 0.8 node, and a node explicitly shifted 30 days into the
    future.  Their independent assertions prove the decay path ran and
    discriminate floor violation, fresh decay, and clock-skew boost.  Base
    evidence: 0.30.1 passes these preserved edge contracts.
    """
    store = _store(tmp_path, "guards")
    try:
        store.add_node("Ancient", node_id="old", weight=0.011)
        store.add_node("Fresh", node_id="fresh", weight=0.8)
        store.add_node("Future", node_id="future", weight=0.5)
        checkpoint = _cold_start(store)
        _set_node_age(store, "old", hours=360 * 24)
        _set_node_age(store, "future", hours=-30 * 24)
        _advance(store, checkpoint, hours=30 * 24)

        old = _weight(store, "old")
        fresh = _weight(store, "fresh")
        future = _weight(store, "future")
        assert math.isclose(old, 0.01, abs_tol=1e-9), f"floor violated: {old}"
        assert math.isclose(fresh, 0.8, abs_tol=1e-9), (
            f"just-accessed node changed: {fresh}"
        )
        assert future <= 0.5 + 1e-9, f"future last_accessed boosted weight: {future}"
        assert future >= 0.5 - 1e-9, f"future last_accessed was decayed: {future}"
    finally:
        store.close()


def test_r2_1_external_weight_raise_is_preserved_before_next_decay(
        tmp_path, monkeypatch):
    """R2.1 current-head regression: decay must not erase an external raise.

    Regression guard: reusing a per-row fold snapshot as the next starting
    weight can turn the raised ``0.9`` back into the old trajectory.  This guard
    is intentionally green-now because the Tester lane cannot observe the
    implementation head; Validator independently reproduced the regression
    there and confirmed the fixed behavior.

    Reachability/non-degeneracy: a fixed wall clock is installed at the
    declared ``kindex.store.datetime`` seam.  The first call is a real cold
    start, the clock advances 130 days, and a real decay fold moves the node to
    approximately 0.3674.  The weight raise then goes through the public
    The supported ``Store.update_node(weight=...)`` path supplies the external
    raise; reinforcement APIs update pheromone rows rather than node weight.
    The test then
    by another one-day clock advance and decay call.  The expected 0.8931 proves
    the second fold starts from the externally raised 0.9 rather than a stale
    snapshot; no direct SQL fixture mutation is involved.
    """
    _, advance = _clocked(monkeypatch)
    store = _store(tmp_path, "external-raise")
    try:
        store.add_node("External raise probe", node_id="probe", weight=1.0)
        store.apply_weight_decay()  # cold-start checkpoint, system-owned state
        advance(130 * 24)
        store.apply_weight_decay()
        assert math.isclose(_weight(store, "probe"), 0.3674, abs_tol=0.025)

        store.update_node("probe", weight=0.9)
        assert math.isclose(_weight(store, "probe"), 0.9, abs_tol=1e-9)
        advance(24)
        store.apply_weight_decay()
        assert math.isclose(_weight(store, "probe"), 0.8931, abs_tol=0.025), (
            "decay must apply the elapsed factor to the externally raised weight"
        )
    finally:
        store.close()


def test_r2_1_decay_checkpoint_never_moves_backwards(tmp_path, monkeypatch):
    """R2.1: a backwards wall clock cannot rewind the decay checkpoint."""
    clock, advance = _clocked(monkeypatch)
    store = _store(tmp_path, "checkpoint-monotonic")
    try:
        store.add_node("Checkpoint monotonic", content="probe", node_id="probe")
        checkpoint = _cold_start(store)
        advance(24)
        store.apply_weight_decay()
        forward = store.get_meta(checkpoint)
        weight_after_forward = _weight(store, "probe")
        clock.current -= dt.timedelta(hours=48)
        store.apply_weight_decay()
        backward_run = store.get_meta(checkpoint)
        assert str(backward_run) >= str(forward), (
            f"decay checkpoint rewound from {forward!r} to {backward_run!r}"
        )
        assert _weight(store, "probe") == weight_after_forward, (
            "backwards time must retain cold-start protection and not decay again"
        )
    finally:
        store.close()


def _schema_inventory(conn):
    tables = tuple(sorted(row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    )))
    columns = {
        table: tuple(sorted(row[1] for row in conn.execute(
            f"PRAGMA table_info({table})"
        )))
        for table in tables
    }
    indexes = tuple(sorted(
        (row[0], row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex%'"
        )
    ))
    return tables, columns, indexes


@pytest.mark.red_now
def test_i6_schema_v8_inventory_is_unchanged_by_decay(tmp_path):
    """P6.1/I6/T1: decay reaches a declared version-8 Store; version drift,
    missing v8 fields, or any before/after DDL difference is forbidden, while
    exact v8 inventory stability is demanded. Reinstating the historical global
    version-7 oracle is the smallest reversion that turns red.

    Reachability/non-degeneracy: inventory is captured from a fresh v8 Store,
    then a real node and decay checkpoint execute before the exact second
    inventory. The assertion permits the user-authorized v8 migration and no
    decay-owned schema change.
    """
    store = _store(tmp_path, "schema")
    try:
        # >= 8, stamped consistently with the code: the durable intent is
        # (a) the v8 trust/ledger inventory exists and (b) decay itself
        # causes no DDL (the fingerprint equality below). The original
        # literal ==8 pinned the version current at authoring time; later
        # user-authorized migrations (v9 referent binding, PRD
        # lineage-grounding R0) legitimately advance it. Reverting to the
        # historical v7 schema still turns this red.
        assert store_mod.SCHEMA_VERSION >= 8
        version = store.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version and version[0] == str(store_mod.SCHEMA_VERSION)
        assert {"verified_at", "verified_by", "prov_method", "valid_at",
                "invalid_at"} <= {
            row[1] for row in store.conn.execute("PRAGMA table_info(nodes)")
        }
        assert "updated_at" in {
            row[1] for row in store.conn.execute("PRAGMA table_info(edges)")
        }
        assert "kind" in {
            row[1] for row in store.conn.execute("PRAGMA table_info(suggestions)")
        }
        assert "capture_candidates" in {
            row[0] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        before = _schema_inventory(store.conn)
        store.add_node("Schema probe", node_id="schema", weight=0.5)
        store.apply_weight_decay()
        after = _schema_inventory(store.conn)
        assert after == before
    finally:
        store.close()
