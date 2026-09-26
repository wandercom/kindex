"""Bounded display-only identity for health banners; never infer session names."""

import json
import os
from pathlib import Path
import re
import stat
import unicodedata


TITLE_WINDOW_BYTES = 32 * 1024
DESKTOP_REASONS = {
    "missing_hooks": "Active session; no recent Kindex hook activity was observed.",
    "missing_use": "Active work; no recent Kindex use was observed.",
    "review_failures": "Repeated review failures are preventing a fresh review.",
    "undelivered_review": "A review is overdue for delivery to the session.",
    "dismissed_advice": "Advice was repeatedly dismissed; check its relevance.",
    "observation_unavailable": "Session activity could not be checked; Kindex health is unknown.",
}
AGENT_LABELS = {
    "claude": "Claude", "codex": "Codex", "opencode": "OpenCode",
    "antigravity": "Antigravity", "cursor": "Cursor", "unknown": "Unknown agent",
}


def _single_line(value):
    # Remove layout controls, including bidi overrides and Unicode line breaks.
    return " ".join("".join(
        " " if unicodedata.category(char)[0] in {"C", "Z"} else char
        for char in value
    ).split())


def _clip(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _claude_title(project, session_id):
    """Read only this session's explicit custom-title records, at bounded cost."""
    if (not project or not project.startswith("/")
            or any(part in {".", ".."} for part in project.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id or "")):
        return None
    descriptor = None
    try:
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        if not root.is_absolute() or ".." in root.parts:
            return None
        encoded_project = re.sub(r"[^A-Za-z0-9]", "-", project)
        # Opening each component relative to its descriptor prevents both ancestor
        # and final-component symlinks (including replacement races).
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        for component in (*root.parts[1:], "projects", encoded_project):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(session_id + ".jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - TITLE_WINDOW_BYTES)
        data = os.pread(descriptor, TITLE_WINDOW_BYTES, start)
        if start:
            # Only a tail title can be current: an old head title may have been
            # superseded in the unread middle. Drop the partial boundary line.
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        for line in reversed(data.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if (isinstance(row, dict) and row.get("type") == "custom-title"
                    and row.get("sessionId") == session_id
                    and isinstance(row.get("customTitle"), str)):
                return _single_line(row["customTitle"]) or None
    except (OSError, ValueError, RuntimeError, AttributeError):
        # Name enrichment is optional; unavailable or unsafe metadata falls back
        # to the alert's own project and session identity.
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return None


def desktop_text(alert):
    """Return a compact body/subtitle without changing the persisted alert."""
    scope = alert["scope"]
    agent = AGENT_LABELS.get(scope.get("agent"), "Unknown agent")
    project = scope.get("project_path") or ""
    session_id = scope.get("session_id") or ""
    project_name = _single_line(project.rstrip("/").rsplit("/", 1)[-1]) or "Project unavailable"
    short_id = _single_line(session_id)[:8]
    if not project and not session_id:
        subtitle = f"{agent}: agent-wide; session unavailable"
    else:
        name = _claude_title(project, session_id) if scope.get("agent") == "claude" else None
        suffix = f" [{short_id}]" if short_id else " [session unavailable]"
        title = _clip(name or project_name, min(30, 60 - len(agent) - 2 - len(suffix)))
        subtitle = f"{agent}: {title}{suffix}"
    location = _single_line(project)
    if location:
        home = str(Path.home())
        if location.startswith(home + "/"):
            location = "~" + location[len(home):]
        if len(location) > 64:
            location = "…" + location[-63:]
    else:
        location = "Project unavailable"
    return DESKTOP_REASONS[alert["code"]] + "\n" + location, subtitle
