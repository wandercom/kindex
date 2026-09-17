"""Adaptive scheduling — dynamically adjust cron interval based on reminder proximity."""

from __future__ import annotations

import datetime
import platform
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

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


def repack_schedule(store: "Store", config: "Config") -> dict:
    """Compute optimal interval and apply it if changed. Returns status dict."""
    if not config.reminders.enabled:
        return {"action": "skipped", "reason": "reminders disabled"}

    # Under a config binding, refuse to modify the machine's real scheduler
    # (R1.5: no write outside the binding). The scheduler spans all profiles
    # and is machine-level state outside the bound root.
    from .config import _bound_root
    if _bound_root is not None:
        return {"action": "skipped", "reason": "config binding active"}

    interval = compute_optimal_interval(store, config)

    # Check current interval from meta table
    current = store.get_meta("cron_interval")
    current_int = int(current) if current else None

    if current_int == interval:
        return {"action": "unchanged", "interval": interval}

    result = apply_schedule(interval, config)
    store.set_meta("cron_interval", str(interval))
    result["interval"] = interval
    result["previous"] = current_int
    return result


def apply_schedule(interval: int, config: "Config") -> dict:
    """Apply a new cron interval to the system scheduler (launchd or crontab)."""
    from .config import _bound_root
    if _bound_root is not None:
        return {"action": "skipped", "reason": "config binding active"}
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
        # Disable: unload the plist
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True, timeout=5,
        )
        return {"action": "disabled"}

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

    # Reload: unload + load
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, timeout=5,
    )
    subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, timeout=5,
    )
    return {"action": "updated"}


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
        import shlex

        from .setup import _find_kin_path, scheduler_path
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
        env = f"PATH={shlex.quote(scheduler_path())}"
        new_lines.append(
            f"*/{minutes} * * * * {env} {kin_path} cron >> {log_dir}/cron.log 2>&1")

    new_crontab = "\n".join(new_lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return {"action": "disabled" if interval == 0 else "updated"}
    return {"action": "failed", "error": proc.stderr}
