"""Run batch0 acceptance suite -- S4: cadence-independent weight decay.

Tester-lane authored, blind to the implementation; the oracle is the signed
run artifacts only. Citation shorthand:

  spec@1f0cdd71  = product-specification.md
      sha256 1f0cdd7134ad671d3795252cbaedde858ae82dd0ef4d5a65d7d09a048bca617b
  arch@59540239  = architecture.md
      sha256 595402395f445c619122ede5435a303b31e8ed9df5d4c1f96e2db73a6ec4f1e6
  strat@e58068c2 = testing-strategy.md
      sha256 e58068c20913db342c6e24af5896f5f24d429ecc699103a8a787fda0372eece5

Markers per strat@e58068c2 T0.2: ``red_now`` = asserts FIXED behavior,
expected to fail on unfixed base 8c5cc925648c; unmarked = green-now guard.

Time control per strat@e58068c2 T0.4: backdated timestamps are written
directly into the fixture DB rows (SQL UPDATE on nodes/edges/meta) -- no
sleeps, no clock mocking. The half-life constant H = 90 days comes from
spec@1f0cdd71 S4 ("the stated 90-day half-life"); the decay checkpoint is
"one meta-table key (`decay.last_run` or equivalent)" (arch@59540239 state
notes), so the checkpoint key is DISCOVERED by diffing meta keys across the
first run rather than hardcoding an implementation-chosen name -- if no new
key appears, the test fails loudly (surface mismatch, report upward), never
guesses.
"""
from __future__ import annotations

import datetime as dt

import pytest

from kindex.config import Config
from kindex.store import Store

HALF_LIFE_DAYS = 90  # spec@1f0cdd71 S4: "the stated 90-day half-life"


def _mk_store(tmp_path, name="data"):
    cfg = Config(data_dir=str(tmp_path / name))
    return Store(cfg)


def _shift_timestamp(value: str, days: float) -> str:
    """Rewrite a stored timestamp ``days`` back, preserving its format
    (epoch float, or ISO with 'T' or space separator, with/without
    microseconds). Fixture mechanics per T0.4, not an oracle."""
    v = str(value).strip()
    try:
        return str(float(v) - days * 86400.0)
    except ValueError:
        pass
    normalized = v.replace("Z", "+00:00")
    if "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    parsed = dt.datetime.fromisoformat(normalized)
    shifted = parsed - dt.timedelta(days=days)
    sep = "T" if "T" in v else " "
    out = shifted.isoformat(sep=sep)
    if "." not in v and "." in out:
        out = out.split(".")[0]
    return out


def _backdate_node(store, node_id, days):
    row = store.conn.execute(
        "SELECT last_accessed FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    assert row and row[0], f"node {node_id} has no last_accessed to shift"
    store.conn.execute(
        "UPDATE nodes SET last_accessed = ? WHERE id = ?",
        (_shift_timestamp(row[0], days), node_id))
    store.conn.commit()


def _backdate_edge_created(store, from_id, days):
    rows = store.conn.execute(
        "SELECT rowid, created_at FROM edges WHERE from_id = ?",
        (from_id,)).fetchall()
    assert rows, f"no edges from {from_id} to backdate"
    for rowid, created in rows:
        store.conn.execute(
            "UPDATE edges SET created_at = ? WHERE rowid = ?",
            (_shift_timestamp(created, days), rowid))
    store.conn.commit()


def _meta_keys(store):
    tables = [r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE '%meta%'").fetchall()]
    assert len(tables) == 1, (
        f"expected exactly one meta table, found {tables} -- declared "
        f"surface mismatch, report upward")
    t = tables[0]
    return {r[0] for r in store.conn.execute(f"SELECT key FROM {t}")}


def _discover_checkpoint_key(before_keys, after_keys):
    """The decay checkpoint is whatever single meta key the first decay run
    minted (arch@59540239: '`decay.last_run` or equivalent')."""
    new = sorted(after_keys - before_keys)
    decayish = [k for k in new if "decay" in k.lower()] or new
    assert len(decayish) == 1, (
        f"could not identify a single decay checkpoint key; new meta keys "
        f"after first decay run: {new} -- surface mismatch, report upward")
    return decayish[0]


def _weight(store, node_id):
    return store.get_node(node_id)["weight"]


# -- R4.2: safe cold start -------------------------------------------------


@pytest.mark.red_now
def test_r4_2_first_run_stamps_checkpoint_and_applies_no_decay(tmp_path):
    """spec@1f0cdd71 R4.2 (first run after upgrade stamps the key and
    applies no decay -- never retro-punishes); strat@e58068c2 oracle row
    R4.2 (red_now).

    Fresh DB, one node backdated 90 days: the FIRST-ever decay run leaves
    its weight untouched and mints the meta checkpoint.

    Red if: the first run multiplies in the full elapsed-age factor (the S4
    defect retro-punishes: weight drops to ~0.5 immediately), or no
    checkpoint key appears in the meta table.
    """
    s = _mk_store(tmp_path)
    try:
        s.add_node("Decay cold start", content="untouched for 90 days",
                   node_id="cold1", weight=1.0)
        _backdate_node(s, "cold1", 90)
        before = _meta_keys(s)
        s.apply_weight_decay()
        after = _meta_keys(s)
        w = _weight(s, "cold1")
        assert abs(w - 1.0) < 1e-9, (
            f"first-ever decay run must not decay (safe cold start); "
            f"weight went to {w}")
        _discover_checkpoint_key(before, after)  # asserts it exists
    finally:
        s.close()


# -- R4.1: the closed form, and idempotence -------------------------------


@pytest.mark.red_now
def test_r4_1_closed_form_half_life_and_second_run_noop(tmp_path):
    """spec@1f0cdd71 R4.1 (w = w0 * 0.5^((T - max(A, S))/H)) + R4.3 floor;
    strat@e58068c2 oracle row R4.1 first row (red_now): backdated 90 days,
    one measured run -> ~= w0 * 0.5 (5% tolerance); an immediate second run
    changes nothing (delta < 0.001).

    Fixture per T0.4 (SQL UPDATE on nodes/edges/META): run #0 mints the
    checkpoint, then the checkpoint AND the node are backdated 90 days so
    the measured run covers one half-life -- the strategy's fixture note
    names meta among the backdatable rows precisely because the R4.2
    cold-start stamp would otherwise make one-run decay unobservable.

    A floor probe rides along: w0=0.015 with last_accessed far in the past
    decays over the same 90-day window to raw 0.0075 < 0.01, so its
    post-run weight must respect the 0.01 floor (and, if the
    negligible-delta skip fires instead, it simply stays at 0.015 -- both
    outcomes satisfy the floor invariant asserted).

    Red if: the checkpoint surface is absent (base: no new meta key ->
    loud fixture failure), decay still compounds per run (second run moves
    the weight again), the half-life is off by >5%, or the floor clamp is
    gone (probe sinks below 0.01).
    """
    s = _mk_store(tmp_path)
    try:
        s.add_node("Decay half life", content="one half-life elapsed",
                   node_id="half1", weight=1.0)
        s.add_node("Decay floor probe", content="ancient tiny node",
                   node_id="floor1", weight=0.015)
        _backdate_node(s, "floor1", 3600)

        before = _meta_keys(s)
        s.apply_weight_decay()          # run #0: mints the checkpoint
        after = _meta_keys(s)
        key = _discover_checkpoint_key(before, after)

        stamp = s.get_meta(key)
        assert stamp, f"checkpoint key {key!r} holds no value"
        s.set_meta(key, _shift_timestamp(stamp, HALF_LIFE_DAYS))
        _backdate_node(s, "half1", HALF_LIFE_DAYS)

        s.apply_weight_decay()          # the measured run
        w1 = _weight(s, "half1")
        assert abs(w1 - 0.5) <= 0.025, (
            f"expected ~= w0*0.5 after one 90-day half-life, got {w1}")
        wf = _weight(s, "floor1")
        assert wf >= 0.01 - 1e-9, f"floor 0.01 violated: {wf}"
        assert wf <= 0.015 + 1e-9, f"floor probe gained weight: {wf}"

        s.apply_weight_decay()          # immediate second run: no-op
        w2 = _weight(s, "half1")
        assert abs(w2 - w1) < 0.001, (
            f"decay still compounds per run: {w1} -> {w2}")
    finally:
        s.close()


@pytest.mark.red_now
def test_r4_1_cadence_independence_nodes_and_edges(tmp_path):
    """spec@1f0cdd71 R4.1 (weight after ANY number of runs is the same
    closed form) + R4.2 (edges decay over (max(created_at, previous_run),
    now]); strat@e58068c2 oracle row R4.1 second row (red_now): same
    backdated fixture in two DBs, one run vs five runs, equal final
    weights. This is the test that fails ON the S4 defect itself.

    Red if: decay compounds per run -- DB-Y's five runs multiply the
    elapsed-age factor five times (base: node 0.5 vs ~0.03; edge
    likewise) and the equality assertion fails.
    """
    def build(name):
        s = _mk_store(tmp_path, name)
        s.add_node("Cadence node", content="ninety days untouched",
                   node_id="a", weight=1.0)
        s.add_node("Cadence peer", content="edge target", node_id="b",
                   weight=1.0)
        s.add_edge("a", "b", weight=0.9, provenance="cadence fixture")
        _backdate_node(s, "a", 90)
        _backdate_edge_created(s, "a", 90)
        return s

    x = build("db-x")
    y = build("db-y")
    try:
        x.apply_weight_decay()
        for _ in range(5):
            y.apply_weight_decay()

        wx, wy = _weight(x, "a"), _weight(y, "a")
        assert abs(wx - wy) < 1e-6, (
            f"cadence-dependent node decay: 1 run -> {wx}, 5 runs -> {wy}")
        ex = x.edges_from("a")[0]["weight"]
        ey = y.edges_from("a")[0]["weight"]
        assert abs(ex - ey) < 1e-6, (
            f"cadence-dependent edge decay: 1 run -> {ex}, 5 runs -> {ey}")
    finally:
        x.close()
        y.close()


# -- R4.3: floor and negligible-delta skip preserved (green guard) --------


def test_r4_3_floor_and_negligible_skip_preserved(tmp_path):
    """spec@1f0cdd71 R4.3; strat@e58068c2 oracle row R4.3 (green-now).

    (a) A just-accessed node does not change across a decay run (the
    negligible-delta skip; also true on base, where ~zero elapsed age gives
    factor ~1). (b) A node with an ancient last_accessed never lands below
    the 0.01 floor, run twice (on base the full-age factor floors it; after
    the fix the cold-start stamp leaves it untouched -- both satisfy the
    floor invariant).

    Red if: a run applies a nonzero per-run factor to a just-accessed node
    (cadence-dependent per-run multiplier), or a decayed weight lands below
    the 0.01 floor.
    """
    s = _mk_store(tmp_path)
    try:
        s.add_node("Fresh node", content="accessed right now",
                   node_id="fresh1", weight=0.8)
        s.add_node("Ancient node", content="stale for a decade",
                   node_id="old1", weight=0.011)
        _backdate_node(s, "old1", 3600)

        s.apply_weight_decay()
        assert abs(_weight(s, "fresh1") - 0.8) < 1e-9, (
            "a just-accessed node must not change on a decay run")
        assert _weight(s, "old1") >= 0.01 - 1e-9, "floor 0.01 violated"

        s.apply_weight_decay()
        assert abs(_weight(s, "fresh1") - 0.8) < 1e-9
        assert _weight(s, "old1") >= 0.01 - 1e-9
    finally:
        s.close()


# -- P6/I6: decay makes no mutation to the authorized v8 schema -----------


def _schema_inventory(store):
    tables = tuple(sorted(r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")))
    columns = {
        table: tuple(sorted(r[1] for r in store.conn.execute(
            f"PRAGMA table_info({table})")))
        for table in tables
    }
    indexes = tuple(sorted((r[0], r[1] or "") for r in store.conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_autoindex%'")))
    return tables, columns, indexes


@pytest.mark.red_now
def test_p6_i6_decay_preserves_current_declared_v8_inventory(tmp_path):
    """State-resilience P6.1/I6 and T1: a fresh v8 Store is inventoried before
    and after real decay; a version-7 oracle, missing declared v8 fields, or any
    DDL drift is forbidden, while exact before/after stability is demanded.
    Restoring the superseded batch0 v7 constant is the smallest reversion red.

    The checkpoint still executes as a meta row; this assertion authorizes only
    the frozen state-resilience v8 inventory, not later decay-owned schema.
    """
    s = _mk_store(tmp_path)
    try:
        # >= 8, stamped consistently with the code: the durable intent is the
        # v8 inventory below plus zero decay-owned DDL (the before/after
        # fingerprint). The original =="8" pinned the version current at
        # authoring; later user-authorized migrations (v9 referent binding,
        # PRD lineage-grounding R0) advance the stamp. A v7 reversion is
        # still red.
        from kindex.schema import SCHEMA_VERSION as _current_schema
        stamp = s.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert int(stamp) >= 8 and stamp == str(_current_schema)
        assert {"verified_at", "verified_by", "prov_method", "valid_at",
                "invalid_at"} <= {
            r[1] for r in s.conn.execute("PRAGMA table_info(nodes)")
        }
        assert "updated_at" in {
            r[1] for r in s.conn.execute("PRAGMA table_info(edges)")
        }
        assert "kind" in {
            r[1] for r in s.conn.execute("PRAGMA table_info(suggestions)")
        }
        assert "capture_candidates" in {
            r[0] for r in s.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        before = _schema_inventory(s)
        s.add_node("Schema probe", content="probe", node_id="p1", weight=0.5)
        s.apply_weight_decay()  # the checkpoint must be a meta ROW, not DDL
        assert _schema_inventory(s) == before
    finally:
        s.close()


# -- edge case: clock skew (future last_accessed) -------------------------


def test_r4_edge_future_last_accessed_no_crash_no_boost(tmp_path):
    """spec@1f0cdd71 R4.1 (the interval (max(A, S), now] is empty when A is
    in the future -- nothing to decay); strat@e58068c2 edge-case list
    ("decay on a node with last_accessed in the future -- must not raise,
    must not boost") (green-now: observed at the 0.29.0 authoring baseline
    that base neither raises nor boosts on a future last_accessed, so this
    guards identical behavior; Validator re-verifies at base per T0.5).
    On the fixed build it additionally exercises the empty/negative
    interval branch of the new fold.

    Red if: a future timestamp makes the (old or new) decay computation
    raise, or a negative elapsed age turns 0.5^(negative) into a weight
    BOOST.
    """
    s = _mk_store(tmp_path)
    try:
        s.add_node("Clock skew node", content="from the future",
                   node_id="skew1", weight=0.5)
        _backdate_node(s, "skew1", -30)  # 30 days in the future

        s.apply_weight_decay()           # must not raise
        w1 = _weight(s, "skew1")
        assert w1 <= 0.5 + 1e-9, f"future last_accessed boosted weight: {w1}"

        s.apply_weight_decay()           # second run: still sane
        w2 = _weight(s, "skew1")
        assert w2 <= 0.5 + 1e-9, f"future last_accessed boosted weight: {w2}"
    finally:
        s.close()
