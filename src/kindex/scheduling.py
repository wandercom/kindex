"""Adaptive scheduling — dynamically adjust cron interval based on reminder proximity."""

from __future__ import annotations

import datetime
import platform
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now()


def nearest_reminder_seconds(store: "Store") -> int | None:
    """Return seconds until the nearest pending reminder, or None if none exist."""
    nearest = store.nearest_pending_reminder()
    if nearest is None:
        return None
    try:
        nearest_dt = datetime.datetime.fromisoformat(nearest)
    except (ValueError, TypeError):
        return None
    delta = (nearest_dt - _now_dt()).total_seconds()
    return max(0, int(delta))


def compute_optimal_interval(store: "Store", config: "Config") -> int:
    """Compute the optimal cron check interval based on nearest pending reminder.

    Returns 0 if no reminders are pending (daemon should be disabled).
    """
    if not config.reminders.adaptive_scheduling:
        return config.reminders.check_interval

    secs = nearest_reminder_seconds(store)
    if secs is None:
        return 0  # no pending reminders — disable

    tiers = sorted(config.reminders.schedule_tiers, key=lambda t: t.threshold, reverse=True)
    for tier in tiers:
        if secs > tier.threshold:
            return max(tier.interval, config.reminders.min_interval)

    # Fell through all tiers — use min_interval
    return config.reminders.min_interval


# A store that has not reported within this window no longer holds the
# machine scheduler fast (a removed profile, a deleted project store).
_STORE_REPORT_TTL = 24 * 3600
# With no reminder pending anywhere, maintenance (ingest, embedding, decay,
# dream, watch expiry) still runs on this cadence; the job is never unloaded.
_MAINTENANCE_INTERVAL = 3600


def maintenance_interval(config: "Config") -> int:
    return max(config.reminders.min_interval, _MAINTENANCE_INTERVAL)


def _scheduler_state_path(config: "Config") -> Path:
    """One record for the machine's one scheduler, wherever the store that
    reports lives: a path under a store's own data directory gave each
    profile and project its own record, and an idle pass overrode a busy
    one."""
    import os
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "kindex" / "scheduler-state.json"


class StoreReport(BaseModel):
    interval: int = 0
    at: float = 0.0


class SchedulerState(BaseModel):
    """What each store last asked of the machine scheduler, and what was
    applied."""
    stores: dict[str, StoreReport] = Field(default_factory=dict)
    applied: int | None = None


class _StateLock:
    def __init__(self, path: Path):
        self.path = path.with_name(path.name + ".lock")
        self.fd = None

    def __enter__(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        import os
        os.close(self.fd)  # closing releases the lock
        return False


def _read_state(path: Path) -> SchedulerState:
    try:
        return SchedulerState.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return SchedulerState()


def _write_state(path: Path, state: SchedulerState) -> None:
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(state.model_dump_json())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def repack_schedule(store: "Store", config: "Config") -> dict:
    """Record this store's wanted interval and apply the machine's.

    The scheduler is one machine-wide job serving every profile and project
    store, so the interval applied is the shortest any live store wants, kept
    in a machine-level record. Each pass used to apply its own store's
    interval (and compare it with that store's own last value), so a profile
    with no pending reminder unloaded the job every other profile relied on,
    and nothing reloaded it. With no reminder pending anywhere the job keeps
    a maintenance cadence instead of being unloaded.
    """
    if not config.reminders.enabled:
        return {"action": "skipped", "reason": "reminders disabled"}

    # Under a config binding, refuse to modify the machine's real scheduler
    # (R1.5: no write outside the binding). The scheduler spans all profiles
    # and is machine-level state outside the bound root.
    from .config import _bound_root
    if _bound_root is not None:
        return {"action": "skipped", "reason": "config binding active"}

    import time

    wanted = compute_optimal_interval(store, config)
    if store.get_meta("cron_interval") != str(wanted):
        store.set_meta("cron_interval", str(wanted))  # this store's own want

    path = _scheduler_state_path(config)
    with _StateLock(path):
        state = _read_state(path)
        now = time.time()
        stores = {key: report for key, report in state.stores.items()
                  if now - report.at < _STORE_REPORT_TTL}
        stores[str(store.db_path)] = StoreReport(interval=wanted, at=now)
        live = [report.interval for report in stores.values() if report.interval > 0]
        interval = min(live) if live else maintenance_interval(config)
        previous = state.applied
        if previous == interval:
            _write_state(path, SchedulerState(stores=stores, applied=previous))
            return {"action": "unchanged", "interval": interval}
        result = apply_schedule(interval, config)
        applied = interval if result.get("action") in ("updated", "unchanged") else previous
        _write_state(path, SchedulerState(stores=stores, applied=applied))
    result["interval"] = interval
    result["previous"] = previous
    return result


def applied_interval(config: "Config") -> int | None:
    """The interval last applied to the machine scheduler, if one was."""
    state = _read_state(_scheduler_state_path(config))
    return state.applied if state.applied and state.applied > 0 else None


CRON_LABEL = "com.kindex.cron"
# One helper at a time holds "$4" (a directory, so taking it is atomic). It
# waits, at most an hour (the maintenance cadence), until launchd reports the
# job not running, then reloads it, or only unloads it when "$3" is
# "unload". A reload request that finds the lock held exits: the holder
# re-reads the plist after releasing the lock and reloads again if it changed
# since its own load, so the newest interval is the one launchd runs. Two
# helpers reloading side by side unloaded the run the other had just started.
RELOAD_WHEN_IDLE = r"""
label="$1"; plist="$2"; mode="$3"; lock="$4"
acquire() {
  mkdir "$lock" 2>/dev/null && return 0
  # A lock older than the longest wait belongs to a helper that died.
  [ -n "$(find "$lock" -maxdepth 0 -mmin +90 2>/dev/null)" ] || return 1
  rm -rf "$lock" && mkdir "$lock" 2>/dev/null
}
acquire || exit 0
while :; do
  n=0
  while launchctl list "$label" 2>/dev/null | grep -q '"PID" = '; do
    n=$((n + 1)); [ "$n" -ge 3600 ] && break; sleep 1
  done
  loaded=$(cksum < "$plist" 2>/dev/null)
  launchctl unload "$plist" >/dev/null 2>&1
  if [ "$mode" = unload ]; then rmdir "$lock" 2>/dev/null; exit 0; fi
  launchctl load "$plist" >/dev/null 2>&1
  rmdir "$lock" 2>/dev/null
  [ "$(cksum < "$plist" 2>/dev/null)" = "$loaded" ] && exit 0
  acquire || exit 0
done
"""


def _reload_lock(config: "Config") -> Path:
    path = _scheduler_state_path(config).with_name("launchd-reload.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _reload_when_idle(plist_path: Path, config: "Config", mode: str = "reload") -> None:
    """Reload (or unload) the maintenance job once it is not running.

    The maintenance run is this job, and it repacks at the end of the run:
    ``launchctl unload`` from inside it ended the run before the ``load``
    that followed, so the job was left unloaded, the applied interval was
    never recorded, and every later load did both again. A helper in its own
    session is not part of the job's process group, so it outlives the run
    and reloads the job once launchd reports it idle.
    """
    subprocess.Popen(
        ["/bin/sh", "-c", RELOAD_WHEN_IDLE, "kindex-reload",
         CRON_LABEL, str(plist_path), mode, str(_reload_lock(config))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def scheduler_writes_disabled() -> bool:
    """``KIN_NO_SCHEDULER_WRITES`` set to 1, true or yes."""
    import os

    return os.environ.get("KIN_NO_SCHEDULER_WRITES", "").strip() in ("1", "true", "yes")


def apply_schedule(interval: int, config: "Config") -> dict:
    """Apply a new cron interval to the system scheduler (launchd or crontab).

    ``KIN_NO_SCHEDULER_WRITES=1`` leaves the machine scheduler untouched
    (test suites and sandboxes, whose child processes inherit it).
    """
    from .config import _bound_root
    if _bound_root is not None:
        return {"action": "skipped", "reason": "config binding active"}
    if scheduler_writes_disabled():
        return {"action": "skipped", "reason": "scheduler writes disabled"}
    if platform.system() == "Darwin":
        return _apply_launchd(interval, config)
    return _apply_crontab(interval, config)


def _apply_launchd(interval: int, config: "Config") -> dict:
    """Update the launchd plist with a new interval."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kindex.cron.plist"

    if not plist_path.exists():
        if interval == 0:
            return {"action": "already_disabled"}
        # No plist exists — can't apply. User needs to run setup-cron first.
        return {"action": "skipped", "reason": "no plist installed"}

    if interval == 0:
        _reload_when_idle(plist_path, config, mode="unload")
        return {"action": "disabled", "reload": "when-idle"}

    # Read current plist, update the interval
    content = plist_path.read_text()
    new_content = re.sub(
        r"(<key>StartInterval</key>\s*<integer>)\d+(</integer>)",
        rf"\g<1>{interval}\g<2>",
        content,
    )

    if new_content == content:
        # Pattern not found — malformed plist
        return {"action": "skipped", "reason": "plist format unrecognized"}

    plist_path.write_text(new_content)
    _reload_when_idle(plist_path, config)
    return {"action": "updated", "reload": "when-idle"}


def _apply_crontab(interval: int, config: "Config") -> dict:
    """Update the crontab entry with a new interval."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return {"action": "skipped", "reason": "no crontab"}

    lines = result.stdout.splitlines()
    # Replace only the maintenance line. The dedicated "remind check" line must
    # survive repacks — it is the guarantee that reminders fire even when the
    # maintenance job is slow, disabled, or stalled.
    from .setup import is_kindex_cron_line
    new_lines = [l for l in lines
                 if "remind check" in l or not is_kindex_cron_line(l)]

    if interval > 0:
        from .setup import _find_kin_path, cron_path_assignment
        kin_path = _find_kin_path()
        # Base-dir logs: repacks run once per profile pass, and the log
        # target must not drift to whichever profile's pass last changed
        # the interval (issue #15).
        log_dir = config.scheduler_log_path
        try:
            # A '>>' redirect into a missing dir kills the job silently.
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # best-effort: never let a log-dir failure break the repack
        # Convert interval to cron minutes (minimum 1)
        minutes = max(1, interval // 60)
        env = cron_path_assignment()
        new_lines.append(
            f"*/{minutes} * * * * {env} {kin_path} cron >> {log_dir}/cron.log 2>&1")

    new_crontab = "\n".join(new_lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return {"action": "disabled" if interval == 0 else "updated"}
    return {"action": "failed", "error": proc.stderr}
