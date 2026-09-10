"""Reminder lifecycle and time parsing for Kindex."""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

from .scoping import item_matches_conversation

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


_VALID_PRIORITIES = ("low", "normal", "high", "urgent")
_VALID_WAKE_CLIENTS = ("codex", "opencode")


def _try_repack(store: "Store") -> None:
    """Best-effort repack of cron schedule after reminder state changes."""
    try:
        from .config import load_config
        from .scheduling import repack_schedule
        config = load_config()
        repack_schedule(store, config)
    except Exception:
        pass  # never let scheduling errors break reminder operations

# Day-of-week mappings for rrule BYDAY
_DOW_MAP = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE",
    "thursday": "TH", "friday": "FR", "saturday": "SA", "sunday": "SU",
    "mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH",
    "fri": "FR", "sat": "SA", "sun": "SU",
}

# Frequency unit mappings for rrule
_FREQ_MAP = {
    "minute": "MINUTELY", "minutes": "MINUTELY",
    "hour": "HOURLY", "hours": "HOURLY",
    "day": "DAILY", "days": "DAILY",
    "week": "WEEKLY", "weeks": "WEEKLY",
    "month": "MONTHLY", "months": "MONTHLY",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now()


# ── Time parsing ────────────────────────────────────────────────────


def _parse_time_of_day(text: str) -> tuple[int, int]:
    """Parse time-of-day strings like '9am', '3:30pm', '14:00' into (hour, minute)."""
    text = text.strip().lower()

    # 24-hour: "14:00", "9:30"
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 12-hour with minutes: "3:30pm", "11:45am"
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", text)
    if m:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return h, mi

    # 12-hour without minutes: "9am", "3pm", "12pm"
    m = re.match(r"^(\d{1,2})\s*(am|pm)$", text)
    if m:
        h, ampm = int(m.group(1)), m.group(2)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return h, 0

    # Bare number: treat as hour in 24-hour time
    m = re.match(r"^(\d{1,2})$", text)
    if m:
        return int(m.group(1)), 0

    raise ValueError(f"Cannot parse time: {text!r}")


def _compute_next_due(rrule_str: str, after: datetime.datetime | None = None) -> str:
    """Use dateutil.rrule to compute the next occurrence after a given datetime."""
    from dateutil.rrule import rrulestr

    after = after or _now_dt()
    # rrulestr needs a DTSTART for context
    rule = rrulestr(rrule_str, dtstart=after)
    nxt = rule.after(after)
    if nxt is None:
        raise ValueError(f"No future occurrence for rule: {rrule_str}")
    return nxt.isoformat(timespec="seconds")


def _parse_recurring(text: str) -> tuple[str, str]:
    """Parse recurring natural language into (rrule_string, next_due_iso).

    Handles patterns like:
    - "every 30 minutes", "every 2 hours", "every 3 days"
    - "every day at 9am", "every weekday at 9am"
    - "every monday", "every friday at 5pm"
    - "every monday and wednesday at 3pm"
    - "every month on day 15"
    - "daily", "weekly", "hourly", "monthly"
    """
    t = text.strip().lower()

    # Shorthands: "daily", "weekly", "hourly", "monthly"
    if t in ("daily", "weekly", "hourly", "monthly"):
        freq = {"daily": "DAILY", "weekly": "WEEKLY",
                "hourly": "HOURLY", "monthly": "MONTHLY"}[t]
        rrule = f"FREQ={freq}"
        return rrule, _compute_next_due(rrule)

    # "daily at <time>"
    m = re.match(r"(?:every\s+day|daily)\s+at\s+(.+)", t)
    if m:
        h, mi = _parse_time_of_day(m.group(1))
        rrule = f"FREQ=DAILY;BYHOUR={h};BYMINUTE={mi};BYSECOND=0"
        return rrule, _compute_next_due(rrule)

    # "every weekday at <time>"
    m = re.match(r"every\s+weekday\s+at\s+(.+)", t)
    if m:
        h, mi = _parse_time_of_day(m.group(1))
        rrule = f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR={h};BYMINUTE={mi};BYSECOND=0"
        return rrule, _compute_next_due(rrule)

    # "every weekday" (no time)
    m = re.match(r"every\s+weekday$", t)
    if m:
        rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        return rrule, _compute_next_due(rrule)

    # "every <day(s)> at <time>" — e.g. "every monday and wednesday at 3pm"
    m = re.match(r"every\s+((?:(?:mon|tue|wed|thu|fri|sat|sun)\w*(?:\s+and\s+|\s*,\s*)?)+)\s+at\s+(.+)", t)
    if m:
        days_str, time_str = m.group(1), m.group(2)
        day_names = re.findall(r"(mon|tue|wed|thu|fri|sat|sun)\w*", days_str)
        byday = ",".join(_DOW_MAP[d] for d in day_names if d in _DOW_MAP)
        if byday:
            h, mi = _parse_time_of_day(time_str)
            rrule = f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={h};BYMINUTE={mi};BYSECOND=0"
            return rrule, _compute_next_due(rrule)

    # "every <day>" — e.g. "every monday"
    m = re.match(r"every\s+(mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)$", t)
    if m:
        day = re.match(r"(mon|tue|wed|thu|fri|sat|sun)", m.group(1))
        if day and day.group(1) in _DOW_MAP:
            byday = _DOW_MAP[day.group(1)]
            rrule = f"FREQ=WEEKLY;BYDAY={byday}"
            return rrule, _compute_next_due(rrule)

    # "every N <unit>" — e.g. "every 30 minutes", "every 2 hours"
    m = re.match(r"every\s+(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months)", t)
    if m:
        interval = int(m.group(1))
        freq = _FREQ_MAP.get(m.group(2))
        if freq:
            rrule = f"FREQ={freq};INTERVAL={interval}"
            return rrule, _compute_next_due(rrule)

    # "every N <unit> at <time>"
    m = re.match(r"every\s+(\d+)\s+(day|days|week|weeks)\s+at\s+(.+)", t)
    if m:
        interval = int(m.group(1))
        freq = _FREQ_MAP.get(m.group(2))
        if freq:
            h, mi = _parse_time_of_day(m.group(3))
            rrule = f"FREQ={freq};INTERVAL={interval};BYHOUR={h};BYMINUTE={mi};BYSECOND=0"
            return rrule, _compute_next_due(rrule)

    # "every month on day N"
    m = re.match(r"every\s+month\s+on\s+(?:day\s+)?(\d{1,2})", t)
    if m:
        day_num = int(m.group(1))
        rrule = f"FREQ=MONTHLY;BYMONTHDAY={day_num}"
        return rrule, _compute_next_due(rrule)

    raise ValueError(f"Cannot parse recurring schedule: {text!r}")


def parse_time_spec(text: str) -> tuple[str, str, str]:
    """Parse a time specification into (next_due_iso, schedule_str, reminder_type).

    Supports:
    - Raw rrule: "RRULE:FREQ=DAILY" or "FREQ=DAILY"
    - Cron: "0 9 * * 1-5" (if cronsim available)
    - Recurring NL: "every weekday at 9am", "daily", "every 2 hours"
    - One-shot NL: "in 30 minutes", "tomorrow at 3pm", "next friday"
    """
    text = text.strip()

    # 1. Raw rrule pass-through
    if text.upper().startswith("RRULE:"):
        rrule_str = text[6:]
        return _compute_next_due(rrule_str), rrule_str, "recurring"
    if text.upper().startswith("FREQ="):
        return _compute_next_due(text), text, "recurring"

    # 2. Cron expression (5 space-separated fields)
    if re.match(r"^[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+$", text):
        try:
            from cronsim import CronSim
            it = CronSim(text, _now_dt())
            next_due = next(it)
            return next_due.isoformat(timespec="seconds"), text, "recurring"
        except ImportError:
            raise ValueError(
                f"Cron expressions require the 'cronsim' package: pip install cronsim"
            )

    # 3. Recurring NL — starts with "every" or is a shorthand
    lower = text.lower().strip()
    if lower.startswith("every ") or lower in ("daily", "weekly", "hourly", "monthly"):
        rrule_str, next_due = _parse_recurring(text)
        return next_due, rrule_str, "recurring"

    # Also match "daily at <time>"
    if lower.startswith("daily "):
        rrule_str, next_due = _parse_recurring(text)
        return next_due, rrule_str, "recurring"

    # 4. One-shot via dateparser
    try:
        import dateparser
    except ImportError:
        raise ValueError(
            "Natural language time parsing requires 'dateparser': pip install dateparser"
        )

    parsed = dateparser.parse(text, settings={
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
    })
    if parsed is None:
        raise ValueError(f"Cannot parse time specification: {text!r}")

    # Sanity: must be in the future
    if parsed <= _now_dt():
        # dateparser might return a past date for ambiguous input; nudge forward
        parsed += datetime.timedelta(days=1)

    return parsed.isoformat(timespec="seconds"), "", "once"


# ── Lifecycle functions ─────────────────────────────────────────────


def create_reminder(
    store: Store,
    title: str,
    time_spec: str,
    *,
    body: str = "",
    priority: str = "normal",
    channels: list[str] | None = None,
    tags: str = "",
    related_node_id: str | None = None,
    action_command: str = "",
    action_instructions: str = "",
    action_mode: str = "auto",
    wake_client: str = "",
    wake_session_id: str = "",
    wake_cwd: str = "",
    wake_model: str = "",
    wake_agent: str = "",
    attention_triggers: list[str] | None = None,
    conversation_id: str = "",
    scope: str = "",
) -> str:
    """Create a new reminder. Returns the reminder ID."""
    if priority not in _VALID_PRIORITIES:
        raise ValueError(f"Invalid priority {priority!r}; must be one of {_VALID_PRIORITIES}")
    scope = scope.strip().lower()
    if scope and scope not in {"chat", "global"}:
        raise ValueError("Invalid reminder scope; must be 'chat' or 'global'")
    wake_client = wake_client.strip().lower()
    if wake_client and wake_client not in _VALID_WAKE_CLIENTS:
        raise ValueError(
            f"Invalid wake client {wake_client!r}; must be one of {_VALID_WAKE_CLIENTS}"
        )
    if wake_client:
        if action_mode not in ("auto", wake_client):
            raise ValueError(
                f"Wake client {wake_client!r} requires action_mode 'auto' or {wake_client!r}"
            )
        action_mode = wake_client

    next_due, schedule, reminder_type = parse_time_spec(time_spec)

    extra: dict | None = None
    if (
        action_command
        or action_instructions
        or wake_client
        or attention_triggers
        or conversation_id
        or scope
    ):
        extra = {}
        if action_command or action_instructions or wake_client:
            extra.update({
                "action_command": action_command,
                "action_instructions": action_instructions,
                "action_mode": action_mode,
                "action_status": "pending",
            })
        if wake_client:
            extra.update({
                "wake_client": wake_client,
                "wake_session_id": wake_session_id,
                "wake_cwd": wake_cwd,
                "wake_model": wake_model,
                "wake_agent": wake_agent,
            })
        if attention_triggers:
            extra["attention_triggers"] = attention_triggers
        if conversation_id:
            extra["conversation_id"] = conversation_id
            extra["reminder_scope"] = "chat"
        elif scope:
            extra["reminder_scope"] = scope

    rid = store.add_reminder(
        title,
        next_due,
        body=body,
        priority=priority,
        reminder_type=reminder_type,
        schedule=schedule,
        channels=channels,
        tags=tags,
        related_node_id=related_node_id,
        extra=extra,
    )
    _try_repack(store)
    return rid


def reminder_matches_conversation(
    reminder: dict,
    conversation_id: str | None,
    *,
    include_global: bool = True,
    include_legacy: bool = False,
) -> bool:
    """Return whether a reminder should be visible in a conversation hook."""
    return item_matches_conversation(
        reminder,
        conversation_id,
        include_global=include_global,
        include_legacy=include_legacy,
    )


def filter_reminders_for_conversation(
    reminders: list[dict],
    conversation_id: str | None,
    *,
    include_global: bool = True,
    include_legacy: bool = False,
) -> list[dict]:
    """Filter reminders for a hook-visible reminder board."""
    return [
        reminder for reminder in reminders
        if reminder_matches_conversation(
            reminder,
            conversation_id,
            include_global=include_global,
            include_legacy=include_legacy,
        )
    ]


def scoped_due_reminders(
    store: Store,
    conversation_id: str | None,
    *,
    include_global: bool = True,
    include_legacy: bool = False,
    as_of: str | None = None,
) -> list[dict]:
    """Return due reminders visible to a specific conversation hook."""
    return filter_reminders_for_conversation(
        store.due_reminders(as_of=as_of),
        conversation_id,
        include_global=include_global,
        include_legacy=include_legacy,
    )


def snooze_reminder(
    store: Store,
    reminder_id: str,
    duration_seconds: int | None = None,
    config: Config | None = None,
) -> str:
    """Snooze a reminder. Returns the new snooze_until time."""
    r = store.get_reminder(reminder_id)
    if r is None:
        raise ValueError(f"Reminder not found: {reminder_id}")

    if duration_seconds is None:
        duration_seconds = 900  # 15 min default
        if config:
            duration_seconds = config.reminders.snooze_duration

    snooze_until_dt = _now_dt() + datetime.timedelta(seconds=duration_seconds)
    snooze_until = snooze_until_dt.isoformat(timespec="seconds")
    store.snooze_reminder(reminder_id, snooze_until)
    _try_repack(store)
    return snooze_until


def complete_reminder(store: Store, reminder_id: str) -> None:
    """Mark a one-shot reminder as completed, or advance a recurring one."""
    r = store.get_reminder(reminder_id)
    if r is None:
        raise ValueError(f"Reminder not found: {reminder_id}")

    if r["reminder_type"] == "recurring":
        advance_recurring(store, reminder_id)
    else:
        store.complete_reminder(reminder_id)
    _try_repack(store)


def cancel_reminder(store: Store, reminder_id: str) -> None:
    """Cancel a reminder."""
    r = store.get_reminder(reminder_id)
    if r is None:
        raise ValueError(f"Reminder not found: {reminder_id}")
    store.update_reminder(reminder_id, status="cancelled")
    _try_repack(store)


def advance_recurring(store: Store, reminder_id: str) -> str | None:
    """For recurring reminders: compute and set the next_due from the schedule.

    Returns the new next_due ISO string, or None if no more occurrences.
    """
    r = store.get_reminder(reminder_id)
    if r is None:
        raise ValueError(f"Reminder not found: {reminder_id}")

    schedule = r.get("schedule", "")
    if not schedule:
        # No schedule means one-shot; just complete it
        store.complete_reminder(reminder_id)
        return None

    now = _now_dt()

    # Check if this is a cron expression
    if re.match(r"^[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+$", schedule):
        try:
            from cronsim import CronSim
            it = CronSim(schedule, now)
            next_due_dt = next(it)
            next_due = next_due_dt.isoformat(timespec="seconds")
        except ImportError:
            store.complete_reminder(reminder_id)
            return None
    else:
        # rrule string
        try:
            next_due = _compute_next_due(schedule, after=now)
        except ValueError:
            store.complete_reminder(reminder_id)
            return None

    updates: dict = {
        "next_due": next_due,
        "last_fired": _now(),
        "status": "active",
        "snooze_until": None,
    }
    # Reset the action for the next occurrence. execute_action skips any
    # reminder whose action_status is completed/running, so without this reset
    # a recurring action would execute exactly once and every later occurrence
    # would silently no-op (last action_result is kept as the last-run record).
    # A live "running" marker is left alone — resetting it would let the next
    # occurrence overlap an execution still in flight; if the runner died, the
    # stale-reclaim in execute_action recovers it on the next attempt.
    # "paused" (staleness parking) is also preserved — only a deliberate
    # manual exec may resume a parked job.
    extra = dict(r.get("extra") or {})
    if "action_snooze_until" in extra:
        del extra["action_snooze_until"]
        updates["extra"] = extra
    if extra.get("action_status") and extra["action_status"] not in ("running", "paused"):
        extra["action_status"] = "pending"
        updates["extra"] = extra

    store.update_reminder(reminder_id, **updates)
    return next_due


_CHECK_LOCK_KEY = "reminder_check_lock"
_CHECK_LOCK_TTL = 120  # seconds — well past a normal sweep, short enough that
                       # a crashed holder only delays the next check briefly


def _acquire_check_lock(store: Store, ttl: int = _CHECK_LOCK_TTL) -> str | None:
    """Atomically claim this graph's reminder-sweep lock, or return None.

    Multiple schedulers legitimately run ``check_and_fire`` on the same graph
    (kin cron's step-0/step-10 checks, the dedicated remind-check job, manual
    ``kin remind check``). Without mutual exclusion two concurrent sweeps read
    the same due list and each dispatches the notification and executes the
    action before either writes state — a double-fire. The lock value is
    ``token|expires_iso`` in the meta table; the conditional UPDATE is atomic
    under SQLite's write lock, so exactly one contender wins. An expired value
    (crashed holder) is claimable immediately.
    """
    import uuid

    token = uuid.uuid4().hex
    now = _now_dt()
    expires = (now + datetime.timedelta(seconds=ttl)).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")
    conn = store.conn
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES (?, '')",
        (_CHECK_LOCK_KEY,),
    )
    cur = conn.execute(
        """UPDATE meta SET value = ?
           WHERE key = ?
             AND (value = '' OR substr(value, instr(value, '|') + 1) < ?)""",
        (f"{token}|{expires}", _CHECK_LOCK_KEY, now_iso),
    )
    conn.commit()
    return token if cur.rowcount == 1 else None


def _renew_check_lock(store: Store, token: str,
                      ttl: int = _CHECK_LOCK_TTL) -> bool:
    """Extend a held lock's expiry. Returns False if the lock was lost.

    A sweep can legitimately outlive the base TTL — a single reminder action
    has a 300s subprocess budget and a sweep may run several sequentially. The
    holder heartbeats before each reminder so the lock never looks abandoned
    while work is in flight; only a genuinely dead holder stops renewing.
    """
    expires = (_now_dt() + datetime.timedelta(seconds=ttl)).isoformat(
        timespec="seconds"
    )
    cur = store.conn.execute(
        "UPDATE meta SET value = ? WHERE key = ? AND value LIKE ?",
        (f"{token}|{expires}", _CHECK_LOCK_KEY, f"{token}|%"),
    )
    store.conn.commit()
    return cur.rowcount == 1


def _release_check_lock(store: Store, token: str) -> None:
    store.conn.execute(
        "UPDATE meta SET value = '' WHERE key = ? AND value LIKE ?",
        (_CHECK_LOCK_KEY, f"{token}|%"),
    )
    store.conn.commit()


def _action_is_stale(reminder: dict, config: Config) -> bool:
    """True when a due reminder's action is too overdue to auto-execute.

    A deliberate snooze defers the action deadline; automatic notification
    retries do not. Legacy snoozes without provenance retain their existing
    semantics, since they may be deliberate long deferrals.
    """
    max_overdue = int(getattr(config.reminders, "max_action_overdue", 86400) or 0)
    if max_overdue <= 0:
        return False
    try:
        anchor = datetime.datetime.fromisoformat(reminder["next_due"])
        snooze = (reminder.get("extra") or {}).get(
            "action_snooze_until", reminder.get("snooze_until")
        )
        if snooze:
            anchor = max(anchor, datetime.datetime.fromisoformat(snooze))
        return (_now_dt() - anchor).total_seconds() > max_overdue
    except (KeyError, ValueError, TypeError):
        return True  # no parseable trigger time — no basis to claim it's fresh


def check_and_fire(
    store: Store,
    config: Config,
) -> list[dict]:
    """Main check cycle: find due reminders and fire notifications.

    If a reminder has an action and ``config.reminders.action_enabled`` is True,
    executes the action.  Successful actions auto-complete the reminder.

    Only one sweep runs per graph at a time (see ``_acquire_check_lock``); a
    contender that loses the lock returns [] — the holder is already firing
    everything due.

    Returns list of fired reminders.
    """
    if not config.reminders.enabled:
        return []

    token = _acquire_check_lock(store)
    if token is None:
        return []
    try:
        return _check_and_fire_locked(store, config, token)
    finally:
        _release_check_lock(store, token)


def _check_and_fire_locked(
    store: Store,
    config: Config,
    token: str,
) -> list[dict]:
    from .notify import dispatch, is_user_idle

    # Skip all notifications if user is idle beyond threshold
    idle = is_user_idle(config)

    due = store.due_reminders()
    fired = []

    for r in due:
        if idle:
            # Don't fire; leave as-is so it fires when user returns
            continue

        # Heartbeat before each reminder: dispatch + action can take minutes
        # (300s subprocess budget apiece), far past the base TTL. Losing the
        # lock means a contender legitimately reclaimed it — stop rather than
        # risk double-firing what it is now processing.
        if not _renew_check_lock(store, token):
            break

        # Determine channels for this reminder
        channels = r.get("channels") or []
        if not channels:
            channels = None  # will use defaults

        # Dispatch notification
        dispatch(r, config, channel_names=channels)

        # Execute action if present, enabled, and not stale. The staleness
        # guard is a hard safety line: a wake reminder minutes overdue is a
        # live workflow and executes; one overdue by more than
        # max_action_overdue means the workflow it belonged to is long gone —
        # auto-executing it (worst case: a first-ever scheduler install
        # discovering months of due pollers) detonates a swarm of headless
        # agents. Stale ones notify only; run `kin remind exec` to run one
        # deliberately.
        from .actions import execute_action, has_action

        stale = _action_is_stale(r, config)
        if config.reminders.action_enabled and not stale:
            if has_action(r):
                result = execute_action(store, r, config)
                if result.get("status") == "completed":
                    if r["reminder_type"] == "recurring":
                        advance_recurring(store, r["id"])
                    else:
                        store.complete_reminder(r["id"])
                    fired.append(r)
                    continue

        if r["reminder_type"] == "recurring":
            # Advance to next occurrence
            advance_recurring(store, r["id"])
            if stale and has_action(r):
                # Advancing re-arms the action for an occurrence one period
                # away — which would resurrect a stale poller swarm, merely
                # time-shifted. Park it instead: notify-only every occurrence
                # until a deliberate `kin remind exec` resumes the job.
                _pause_action(store, r["id"])
        else:
            # Mark as fired (pending user action)
            store.update_reminder(r["id"], status="fired", last_fired=_now())

        fired.append(r)

    return fired


def _pause_action(store: Store, reminder_id: str) -> None:
    """Mark a reminder's action paused — sweeps skip it, manual exec resumes."""
    r = store.get_reminder(reminder_id)
    if r is None:
        return
    extra = dict(r.get("extra") or {})
    extra["action_status"] = "paused"
    store.update_reminder(reminder_id, extra=extra)


def auto_snooze_stale(store: Store, config: Config) -> int:
    """Find reminders in 'fired' status past auto_snooze_timeout, and snooze them.

    Returns count of auto-snoozed reminders.
    """
    if not config.reminders.enabled:
        return 0

    timeout = config.reminders.auto_snooze_timeout
    snooze_duration = config.reminders.snooze_duration
    now = _now_dt()
    cutoff = (now - datetime.timedelta(seconds=timeout)).isoformat(timespec="seconds")

    rows = store.conn.execute(
        "SELECT * FROM reminders WHERE status = 'fired' AND last_fired <= ?",
        (cutoff,),
    ).fetchall()

    count = 0
    for row in rows:
        r = store._reminder_to_dict(row)
        # Re-check right before writing: the user (or another checker) may
        # have completed/cancelled it since the SELECT — a blind snooze would
        # resurrect it.
        current = store.get_reminder(r["id"])
        if not current or current["status"] != "fired":
            continue
        snooze_until = (now + datetime.timedelta(seconds=snooze_duration)).isoformat(
            timespec="seconds"
        )
        if store.snooze_reminder(r["id"], snooze_until, automatic=True):
            count += 1

    return count


# ── Formatting ──────────────────────────────────────────────────────


def format_reminder(reminder: dict) -> str:
    """Format a single reminder for display."""
    lines = []
    status = reminder.get("status", "active")
    priority = reminder.get("priority", "normal")
    p_marker = f" [{priority}]" if priority != "normal" else ""
    lines.append(f"  {reminder['id']}: {reminder['title']}{p_marker} ({status})")
    lines.append(f"    Due: {reminder['next_due']}")
    if reminder.get("schedule"):
        lines.append(f"    Schedule: {reminder['schedule']}")
    if reminder.get("body"):
        lines.append(f"    {reminder['body']}")
    if status == "snoozed" and reminder.get("snooze_until"):
        lines.append(f"    Snoozed until: {reminder['snooze_until']}")
    if reminder.get("snooze_count", 0) > 0:
        lines.append(f"    Snoozed {reminder['snooze_count']} time(s)")

    # Action info
    extra = reminder.get("extra") or {}
    if (
        extra.get("action_command")
        or extra.get("action_instructions")
        or extra.get("wake_client")
    ):
        a_status = extra.get("action_status", "pending")
        a_mode = extra.get("action_mode", "auto")
        lines.append(f"    Action [{a_mode}]: {a_status}")
        if extra.get("wake_client"):
            target = extra["wake_client"]
            if extra.get("wake_session_id"):
                target += f" session={extra['wake_session_id']}"
            lines.append(f"      Wake: {target}")
            if extra.get("wake_cwd"):
                lines.append(f"      Cwd: {extra['wake_cwd']}")
            if extra.get("wake_model"):
                lines.append(f"      Model: {extra['wake_model']}")
            if extra.get("wake_agent"):
                lines.append(f"      Agent: {extra['wake_agent']}")
        if extra.get("action_command"):
            lines.append(f"      Command: {extra['action_command']}")
        if extra.get("action_instructions"):
            lines.append(f"      Instructions: {extra['action_instructions'][:80]}")
        if extra.get("action_result") and a_status in ("completed", "failed"):
            lines.append(f"      Result: {extra['action_result'][:120]}")

    return "\n".join(lines)


def format_reminder_list(reminders: list[dict]) -> str:
    """Format a list of reminders for display."""
    if not reminders:
        return "No reminders."
    return "\n".join(format_reminder(r) for r in reminders)


def parse_duration(text: str) -> int:
    """Parse a duration string like '15m', '1h', '30s', '2h30m' into seconds."""
    total = 0
    for match in re.finditer(r"(\d+)\s*(h|m|s|hr|min|sec|hour|minute|second)", text.lower()):
        val = int(match.group(1))
        unit = match.group(2)[0]
        if unit == "h":
            total += val * 3600
        elif unit == "m":
            total += val * 60
        elif unit == "s":
            total += val
    if total:
        return total
    # Bare number: treat as seconds
    if text.strip().isdigit():
        return int(text.strip())
    return 900  # default 15 min
