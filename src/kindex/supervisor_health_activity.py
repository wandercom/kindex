"""Bounded native session observation. Persist metadata only, never transcript text.

A transcript may be unavailable or its beginning may lack a project identity.
Those cases are reported as unverified; nearby hooks are never used to invent it.
"""
from datetime import datetime, timezone, timedelta
import json
from itertools import islice
import time
import os
from pathlib import Path
import sqlite3

MAX_FILES = 64
MAX_DIRS = 512
WINDOW = 65536
RECENT = 1200


def _rows(path, head=False):
    with path.open("rb") as stream:
        size = path.stat().st_size
        start = 0 if head else max(0, size - WINDOW)
        stream.seek(start)
        data = stream.read(WINDOW)
    lines = data.splitlines()
    if start:
        lines = lines[1:]
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            yield row


def _recent_dirs(root):
    if not root.is_dir():
        return []
    # The count is capped before recursion. No walk of old transcript contents.
    entries = []
    with os.scandir(root) as scan:
        for index, entry in enumerate(scan):
            if index >= 10000:
                break
            if entry.is_dir(follow_symlinks=False):
                entries.append((entry.stat().st_mtime, Path(entry.path)))
    return [p for _, p in sorted(entries, reverse=True)[:MAX_DIRS]]



def _codex_index(root, now):
    """Resume keeps the original rollout date; native metadata locates that file."""
    database = root.parent / "state_5.sqlite"
    if not database.is_file() or database.is_symlink():
        return []
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    deadline = time.monotonic() + 2
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        rows = connection.execute(
            "SELECT rollout_path FROM threads WHERE updated_at>=? AND updated_at<=? ORDER BY updated_at DESC LIMIT ?",
            (int(now - RECENT), int(now + 60), MAX_FILES),
        ).fetchall()
    finally:
        connection.close()
    candidates = []
    resolved_root = root.resolve()
    for (value,) in rows:
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_absolute() and path.resolve().is_relative_to(resolved_root):
            candidates.append(path)
    return candidates


def _files(agent, now):
    home = Path.home()
    if agent == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))) / "projects"
        candidates = []
        for directory in _recent_dirs(root):
            # Top-level sessions; Claude's subagents are not separate hook sessions.
            candidates.extend(islice(directory.glob("*.jsonl"), max(0, 10000 - len(candidates))))
    elif agent == "codex":
        root = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "sessions"
        day = datetime.fromtimestamp(now, timezone.utc)
        candidates = _codex_index(root, now)
        for offset in range(2):
            directory = root / (day - timedelta(days=offset)).strftime("%Y/%m/%d")
            if directory.is_dir():
                candidates.extend(islice(directory.glob("*.jsonl"), max(0, 10000 - len(candidates))))
    else:
        root = home / ".gemini" / "antigravity-cli" / "brain"
        candidates = [p / ".system_generated/logs/transcript_full.jsonl" for p in _recent_dirs(root)]
    eligible = []
    for path in dict.fromkeys(candidates[:10000]):
        try:
            stat = path.stat()
            if path.is_file() and not path.is_symlink() and now - RECENT <= stat.st_mtime <= now + 60:
                eligible.append((stat.st_mtime, path))
        except OSError:
            continue
    return root, [p for _, p in sorted(eligible, reverse=True)[:MAX_FILES]]


def _ag_projects():
    from .supervisor_health import _db
    path = Path.home() / ".gemini/antigravity-cli/history.jsonl"
    mappings = {}
    def remember(sid, project):
        if isinstance(sid, str) and isinstance(project, str) and Path(project).is_absolute():
            mappings.setdefault(sid, set()).add(str(Path(project).resolve()))
    if path.is_file():
        for row in _rows(path):
            if isinstance(row.get("conversationId"), str) and isinstance(row.get("workspace"), str):
                remember(row["conversationId"], row["workspace"])
    cache = Path.home() / ".gemini/antigravity-cli/cache/conversation_metadata.json"
    if cache.is_file() and not cache.is_symlink():
        # Never read an unbounded native cache or arbitrary prose for identity.
        with cache.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) <= 1024 * 1024:
            from urllib.parse import unquote, urlsplit
            metadata = json.loads(data)
            conversations = metadata.get("conversations", {}) if isinstance(metadata, dict) else {}
            if isinstance(conversations, dict):
                for sid, entry in list(conversations.items())[:1000]:
                    summary = entry.get("summary", {}) if isinstance(entry, dict) else {}
                    if not isinstance(summary, dict) or summary.get("ID") != sid:
                        continue
                    uris = summary.get("WorkspaceURIs", [])
                    if not isinstance(uris, list):
                        continue
                    for uri in uris[:16]:
                        if not isinstance(uri, str):
                            continue
                        parsed = urlsplit(uri)
                        if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
                            remember(sid, unquote(parsed.path))
    # An exact prior hook receipt supplies identity, never activity. This also
    # supports resumed AGY sessions absent from the bounded history tail.
    with _db() as conn:
        rows = conn.execute("SELECT DISTINCT s.session_id,s.project_path FROM scopes s JOIN events e ON e.scope_id=s.id WHERE s.agent='antigravity' AND e.kind='hook' ORDER BY s.last_seen DESC LIMIT 1000").fetchall()
        for sid, project in rows:
            remember(sid, project)
    # Conflicting identities remain unverified; never choose a nearby session.
    return {sid: next(iter(projects)) for sid, projects in mappings.items() if len(projects) == 1}


def _tool_name(raw):
    from .supervisor_health import TOOLS
    if not isinstance(raw, str) or "kindex" not in raw.lower():
        return None
    for name in sorted(TOOLS, key=len, reverse=True):
        if raw.endswith("__" + name) or raw.endswith("_" + name) or raw.endswith("." + name):
            return name
    return None


def _jsonl(agent, path, now, projects):
    from .supervisor_health import _time, record
    head, tail = list(_rows(path, head=True)), list(_rows(path))
    project = sid = None
    if agent == "antigravity":
        sid = path.parents[2].name
        project = projects.get(sid)
    for row in head:
        payload = row.get("payload", {}) if row.get("type") == "session_meta" else row
        if not isinstance(payload, dict):
            continue
        project = project or payload.get("cwd") or payload.get("project_path")
        sid = sid or payload.get("sessionId") or payload.get("session_id")
        if row.get("type") == "session_meta":
            sid = sid or payload.get("id")
        if project and sid:
            break
    scoped = isinstance(project, str) and Path(project).is_absolute() and bool(sid)
    scope = {"project_path": project, "agent": agent, "session_id": sid}
    observed = False
    for index, row in enumerate(tail):
        raw_time = row.get("timestamp") or row.get("created_at")
        if raw_time is None:
            continue
        try:
            at = _time(raw_time)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (now - RECENT <= at <= now + 60):
            continue
        kind = row.get("type", "")
        payload = row.get("payload") or row.get("message") or row
        if not isinstance(payload, dict):
            continue
        relevant = (kind in {"user", "assistant", "response_item", "USER_INPUT", "PLANNER_RESPONSE"} or
                    (kind == "event_msg" and payload.get("type") in {"user_message", "agent_message", "task_started", "task_complete"}))
        if not relevant:
            continue
        if not scoped:
            return False
        key = str(row.get("uuid") or row.get("step_index") or row.get("timestamp") or index)
        record(scope, "activity", {"at": at, "source": "native", "event_id": "native:" + key,
                                   "active": payload.get("type") != "task_complete"})
        observed = True
        calls = list(row.get("tool_calls") or [])
        if payload.get("type") == "function_call":
            calls.append(payload)
        content = payload.get("content")
        if isinstance(content, list):
            calls.extend(c for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = _tool_name(call.get("name"))
            if name:
                record(scope, "use", {"at": at, "tool": name, "outcome": "observed", "source": "native",
                                      "initiator": "agent", "event_id": "native:" + key + ":" + str(call.get("id") or call.get("call_id") or name)})
    return True if observed else None


def _opencode(now):
    from .supervisor_health import record
    path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/opencode.db"
    if not path.is_file():
        return {"state": "not_observed", "sessions": 0}
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    deadline = time.monotonic() + 2
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        # Only identity and timestamp columns are selected; no prompts or titles.
        rows = conn.execute("SELECT s.id,s.directory,max(m.time_updated) FROM session s JOIN message m ON m.session_id=s.id WHERE m.time_updated>=? GROUP BY s.id ORDER BY max(m.time_updated) DESC LIMIT ?",
                            (int((now - RECENT) * 1000), MAX_FILES)).fetchall()
        for sid, project, at in rows:
            scope = {"project_path": project, "agent": "opencode", "session_id": sid}
            record(scope, "activity", {"at": at / 1000, "source": "native", "event_id": "native:" + str(at)})
            # JSON projection retains names/status only; tool arguments never leave DB.
            calls = conn.execute("SELECT id,time_updated,json_extract(data,'$.tool'),json_extract(data,'$.state.status') FROM part WHERE session_id=? AND time_updated>=? AND json_extract(data,'$.type')='tool' ORDER BY time_updated DESC LIMIT 128",
                                 (sid, int((now - RECENT) * 1000))).fetchall()
            for ident, timestamp, tool, state in calls:
                name = _tool_name(tool)
                if name:
                    record(scope, "use", {"at": timestamp / 1000, "tool": name, "source": "native", "initiator": "agent",
                                          "outcome": "success" if state == "completed" else "failed" if state == "error" else "observed",
                                          "event_id": "native:" + ident + ":" + str(state)})
        return {"state": "observed" if rows else "not_observed", "sessions": len(rows)}
    finally:
        conn.close()



def _cursor(now):
    """Cursor CLI native sidecar metadata; project hashes are never decoded.

    Official CLI chat-session-meta schema v1 includes explicit cwd and updatedAtMs.
    The CLI's exported transcript JSONL lacks timestamps, so it cannot independently
    establish activity or time individual Kindex calls. IDE coverage is unverified.
    """
    from .supervisor_health import record
    config = os.environ.get("CURSOR_CONFIG_DIR")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config) if config and config.strip() else Path(xdg) / "cursor" if xdg and xdg.strip() else Path.home() / ".cursor"
    chats = root / "chats"
    result = {"state": "not_observed", "sessions": 0, "unidentified": 0,
              "errors": 0, "source_present": chats.is_dir(),
              "sources": {"cli": "session_metadata", "ide": "unverified", "native_use": "unverified"}}
    candidates = []
    scanned = 0
    for project in _recent_dirs(chats):
        with os.scandir(project) as directories:
            for entry in directories:
                scanned += 1
                if scanned > 10000:
                    break
                if not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path) / "meta.json"
                try:
                    if path.is_file() and not path.is_symlink():
                        modified = path.stat().st_mtime
                        if now - RECENT <= modified <= now + 60:
                            candidates.append((modified, path))
                except OSError:
                    result["errors"] += 1
        if scanned > 10000:
            break
    for _, path in sorted(candidates, reverse=True)[:MAX_FILES]:
        try:
            with path.open("rb") as stream:
                data = stream.read(WINDOW + 1)
            if len(data) > WINDOW:
                result["errors"] += 1
                continue
            metadata = json.loads(data)
            if not isinstance(metadata, dict):
                result["errors"] += 1
                continue
            updated = metadata.get("updatedAtMs")
            if isinstance(updated, bool) or not isinstance(updated, (int, float)):
                continue
            at = updated / 1000
            # A touched old sidecar is not fresh native activity.
            if not (now - RECENT <= at <= now + 60) or metadata.get("hasConversation") is not True or metadata.get("isSubagent") is True:
                continue
            cwd = metadata.get("cwd")
            if metadata.get("schemaVersion") != 1 or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                result["unidentified"] += 1
                continue
            scope = {"agent": "cursor", "project_path": cwd, "session_id": path.parent.name}
            record(scope, "activity", {"at": at, "source": "native", "active": True,
                                      "event_id": "native-meta:" + str(updated)})
            result["sessions"] += 1
        except (OSError, ValueError, sqlite3.Error):
            result["errors"] += 1
    if result["sessions"]:
        result["state"] = "observed"
    return result


def observe_activity(now):
    """Observe at most 64 recent sessions per host, 64 KiB at each file edge."""
    result = {}
    for agent in ("claude", "codex", "antigravity"):
        count = unidentified = errors = 0
        try:
            root, files = _files(agent, now)
            projects = _ag_projects() if agent == "antigravity" else {}
            for path in files:
                try:
                    observed = _jsonl(agent, path, now, projects)
                    if observed is True:
                        count += 1
                    elif observed is False:
                        unidentified += 1
                except (OSError, ValueError, sqlite3.Error):
                    errors += 1
            result[agent] = {"state": "observed" if count else "not_observed", "sessions": count,
                             "unidentified": unidentified, "errors": errors, "source_present": root.exists()}
        except (OSError, ValueError, sqlite3.Error):
            result[agent] = {"state": "unavailable", "sessions": 0}
    try:
        result["opencode"] = _opencode(now)
    except (OSError, ValueError, sqlite3.Error):
        result["opencode"] = {"state": "unavailable", "sessions": 0}
    try:
        result["cursor"] = _cursor(now)
    except (OSError, ValueError, sqlite3.Error):
        result["cursor"] = {"state": "unavailable", "sessions": 0}
    return result
