"""Run-v8 acceptance oracles for degraded-ledger losslessness.

Authority: product specification ``58916cd0...`` R4.1--R4.2, architecture
``de100e45...`` V4 atomicity, and strategy ``ede46bf4...``.  The R4.1 test
uses separate OS processes.  One process pauses at the destructive rename
boundary; another invokes the same public append path while the old inode is
still visible.  This deterministically reaches the race described by R4.1.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import threading
import time
import fcntl
from pathlib import Path

import pytest

import kindex.config as config_mod


def _touches_ledger(src, dst, ledger: Path) -> bool:
    target = ledger.resolve()
    candidates = []
    for value in (src, dst):
        try:
            candidates.append(Path(value).resolve())
        except (TypeError, ValueError):
            continue
    return any(path == target or path.name.startswith(target.name) for path in candidates)


def _returns_within(seconds: float, fn, *args, **kwargs) -> None:
    """Run ``fn`` on a daemon thread and fail if it has not returned in time.

    A caller that blocks is exactly the regression these oracles guard, so a
    block must fail the test rather than hang the run.
    """
    errors: list[BaseException] = []

    def target():
        try:
            fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the test thread
            errors.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{fn.__name__} did not return within {seconds}s"
    if errors:
        raise errors[0]


def _cap_worker(data_dir: str, ready, release) -> None:
    """Pause a cap rewrite immediately before its inode replacement."""
    ledger = Path(data_dir) / "degraded.jsonl"
    real_replace = os.replace
    real_rename = os.rename

    def gated_replace(src, dst, *args, **kwargs):
        if _touches_ledger(src, dst, ledger):
            ready.set()
            if not release.wait(30):
                raise TimeoutError("test did not release cap replacement")
        return real_replace(src, dst, *args, **kwargs)

    def gated_rename(src, dst, *args, **kwargs):
        if _touches_ledger(src, dst, ledger):
            ready.set()
            if not release.wait(30):
                raise TimeoutError("test did not release cap rename")
        return real_rename(src, dst, *args, **kwargs)

    os.replace = gated_replace
    os.rename = gated_rename
    config_mod.record_degraded(
        "v8-cap-event",
        RuntimeError("cap process fixture"),
        override_dir=data_dir,
    )


def _append_worker(data_dir: str, started) -> None:
    started.set()
    config_mod.record_degraded(
        "v8-concurrent-event",
        RuntimeError("concurrent append fixture"),
        override_dir=data_dir,
    )


def _events(data_dir: Path) -> list[dict]:
    """Parse active and rotated ledger segments; empty lock files are harmless."""
    parsed = []
    segments = sorted(
        path for path in data_dir.glob("degraded.jsonl*") if path.is_file()
    )
    assert segments, "ledger operation left no active or rotated segment"
    for segment in segments:
        lines = [line for line in segment.read_text().splitlines() if line.strip()]
        parsed.extend(json.loads(line) for line in lines)
    return parsed


def _recorded_loss_count(events: list[dict]) -> int:
    """Recognize explicit numeric loss events without prescribing one schema."""
    total = 0
    explicit_keys = (
        "lost_events", "loss_count", "dropped_events", "dropped_count",
        "events_lost", "events_dropped",
    )
    for event in events:
        for key in explicit_keys:
            value = event.get(key)
            if isinstance(value, int) and value > 0:
                total += value
        descriptor = " ".join(
            str(event.get(key, ""))
            for key in ("event", "type", "cmd", "msg", "message")
        ).lower()
        if "lost" not in descriptor and "dropped" not in descriptor:
            continue
        generic = event.get("count")
        if isinstance(generic, int) and generic > 0:
            total += generic
        for match in re.finditer(r"(?:lost|dropped)\D{0,20}(\d+)", descriptor):
            total += int(match.group(1))
    return total


@pytest.mark.red_now
def test_r4_1_concurrent_cap_append_is_present_or_counted(
        tmp_path, monkeypatch):
    """R4.1: a real-process append at cap replacement is kept or counted.

    Reversion: the old read-tail/write-temp/``os.replace`` sequence silently
    drops ``v8-concurrent-event`` when the paused older snapshot wins, turning
    this red.  Reachability/non-degeneracy: a >1 MiB valid ledger forces the
    cap branch; the cap process sets ``ready`` only from the actual rename/
    replace call, then a second process invokes ``record_degraded`` while that
    boundary is paused.  Both exit codes, both unique command IDs, and JSON
    parsing prove the operations ran and detect torn lines.  A missing unique
    event is accepted only when a numeric loss event accounts for it, so the
    assertion discriminates the base's silent loss.  Base evidence: 0.30.1
    loses the concurrent ID after replacing the old inode and records no loss,
    exactly R4.1's cap-vs-append defect.
    """
    controlled_home = tmp_path / "home"
    controlled_home.mkdir()
    monkeypatch.setenv("HOME", str(controlled_home))
    data_dir = tmp_path / "ledger"
    data_dir.mkdir()
    ledger = data_dir / "degraded.jsonl"
    seeds = [
        json.dumps({
            "ts": "2026-08-11T00:00:00Z",
            "cmd": "seed",
            "error_class": "SeedError",
            "msg": "x" * 180,
            "i": index,
        })
        for index in range(6000)
    ]
    ledger.write_text("\n".join(seeds) + "\n")
    assert ledger.stat().st_size > 1_000_000, (
        "reachability control: ledger is not large enough to force cap rewrite"
    )

    methods = mp.get_all_start_methods()
    context = mp.get_context("fork" if "fork" in methods else "spawn")
    rewrite_ready = context.Event()
    release_rewrite = context.Event()
    append_started = context.Event()
    cap = context.Process(
        target=_cap_worker,
        args=(str(data_dir), rewrite_ready, release_rewrite),
    )
    cap.start()
    try:
        assert rewrite_ready.wait(20), (
            "cap process never reached a ledger rename/replace boundary"
        )
        concurrent = context.Process(
            target=_append_worker,
            args=(str(data_dir), append_started),
        )
        concurrent.start()
        assert append_started.wait(10), "concurrent append process did not start"
        # On the unfixed path this lets the append and its own cap finish before
        # the older paused snapshot replaces them.  On a locking fix the append
        # blocks here and runs after release.  This is the race under test.
        time.sleep(0.2)
        release_rewrite.set()
        concurrent.join(30)
        cap.join(30)
        assert concurrent.exitcode == 0, (
            f"concurrent ledger process exited {concurrent.exitcode}"
        )
        assert cap.exitcode == 0, f"cap ledger process exited {cap.exitcode}"
    finally:
        release_rewrite.set()
        if cap.is_alive():
            cap.join(5)

    parsed = _events(data_dir)  # any torn/interleaved line in any segment raises
    commands = {str(event.get("cmd", "")) for event in parsed}
    expected = {"v8-cap-event", "v8-concurrent-event"}
    missing = expected - commands
    assert "v8-cap-event" in commands, (
        "cap event itself is absent; fixture did not produce the measured rewrite"
    )
    if missing:
        counted = _recorded_loss_count(parsed)
        assert counted >= len(missing), (
            f"silent concurrent ledger loss: missing={sorted(missing)}, "
            f"recorded_loss_count={counted}"
        )


def test_r4_2_unbuildable_ledger_target_never_raises(tmp_path):
    """R4.2: ledger recording swallows every failure of its own write path.

    Reversion: removing the outer fail-open boundary lets mkdir/open propagate
    ``NotADirectoryError`` and turns this red.  Reachability/non-degeneracy: a
    regular file is used as the parent of the requested ledger directory, a
    deterministic failure under every uid (unlike chmod); the target cannot be
    created, and the direct declared ``record_degraded`` call reaches its
    directory/write path.  Returning normally is the discriminating assertion.
    Base evidence/ruling: Validator reclassified this row green-now after this
    exact fixture passed at base ``d4ecf5cf``; batch0 already made
    ``record_degraded`` swallow its own errors.  It remains a standing guard
    because a future cap/locking refactor could quietly break that invariant.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("regular file blocks child creation")
    impossible = blocker / "ledger-dir"
    config_mod.record_degraded(
        "v8-unwritable",
        RuntimeError("original failure must remain nonfatal"),
        override_dir=str(impossible),
    )
    assert not impossible.exists()


@pytest.mark.red_now
def test_r4_1_record_degraded_is_lossless_while_lock_is_held(tmp_path):
    """R4.1: degraded recording must not block or lose an event on contention."""
    data_dir = tmp_path / "ledger"
    data_dir.mkdir()
    lock_path = data_dir / "degraded.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        started = time.monotonic()
        _returns_within(
            10, config_mod.record_degraded,
            "v8-lock-held", RuntimeError("lock contention"), override_dir=str(data_dir),
        )
        assert time.monotonic() - started < 2.0, "degraded recording blocked on its lock"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    events = _events(data_dir)
    assert any(event.get("cmd") == "v8-lock-held" for event in events), (
        "degraded event was lost while another process held the lock"
    )


def test_r4_2_uncreatable_lock_file_remains_fail_open(tmp_path):
    """R4.2: an unusable lock-file path must not block the caller."""
    data_dir = tmp_path / "ledger"
    data_dir.mkdir()
    (data_dir / "degraded.lock").mkdir()
    _returns_within(
        10, config_mod.record_degraded,
        "v8-lock-path-blocked", RuntimeError("lock path is a directory"),
        override_dir=str(data_dir),
    )
    events = _events(data_dir)
    assert any(event.get("cmd") == "v8-lock-path-blocked" for event in events), (
        "degraded event was lost when the lock file could not be opened"
    )
