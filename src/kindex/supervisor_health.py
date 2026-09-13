"""Private evidence of supervisor operation, checked independently of agent hooks.

The registry contains identifiers, timestamps, counters and fixed codes only. It is
not a second knowledge graph. Delivery and observed calls do not establish value.
Notifications are opt-in, local root mail, with durable failure/cooldown state.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

KINDS = {"hook", "use", "review", "delivery", "feedback", "activity"}
TOOLS = {"search", "context", "ask", "show", "add", "edit", "learn", "link",
         "task_add", "task_list", "task_done", "task_update", "task_claim",
         "task_release", "task_get", "task_execute", "tag_start", "tag_resume",
         "tag_update", "status", "list_nodes", "suggest", "graph_stats",
         "watch_add", "watch_resolve", "coord_read", "coord_post"}
DEFAULTS = {"enabled": False, "mail_enabled": False, "active_seconds": 1200, "hook_grace_seconds": 300,
            "use_grace_seconds": 1800, "queue_grace_seconds": 900,
            "failure_threshold": 3, "dismissed_threshold": 3,
            "consecutive_checks": 2, "cooldown_seconds": 21600, "sendmail_path": "/usr/sbin/sendmail"}


def _time(value=None):
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def _diagnostic():
    import shlex
    import sys
    return shlex.join([sys.executable, "-m", "kindex.supervisor_health", "status", "--json"])


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def health_dir():
    return Path(os.environ.get("KIN_HEALTH_DIR", str(Path.home() / ".kindex" / "health"))).expanduser()


def settings():
    path = health_dir() / "config.json"
    result = dict(DEFAULTS)
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Health configuration must be an object")
        for key, default in DEFAULTS.items():
            if key not in data:
                continue
            value = data[key]
            if key in {"enabled", "mail_enabled"}:
                if not isinstance(value, bool):
                    raise ValueError("Health enablement settings must be boolean")
            elif key == "sendmail_path":
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError("Health sendmail_path must be absolute")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("Health thresholds must be positive numbers")
            result[key] = value
    return result


@contextmanager
def _db():
    root = health_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("Health directory must not be a symlink")
    os.chmod(root, 0o700)
    path = root / "health.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS scopes (
                id TEXT PRIMARY KEY, project_path TEXT NOT NULL, agent TEXT NOT NULL,
                session_id TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
                at REAL NOT NULL, details TEXT NOT NULL, event_key TEXT UNIQUE);
            CREATE INDEX IF NOT EXISTS event_scope_time ON events(scope_id,at);
            CREATE TABLE IF NOT EXISTS issues (
                id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                consecutive INTEGER NOT NULL, active INTEGER NOT NULL,
                last_notified REAL, transport_error TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        with conn:
            yield conn
    finally:
        conn.close()


def _scope(scope):
    project = scope.get("project_path", "")
    if not isinstance(project, str) or not Path(project).is_absolute():
        raise ValueError("Health requires an absolute project_path")
    agent = scope.get("agent", "")
    aliases = {"claude-code": "claude", "claude_code": "claude", "google-antigravity": "antigravity", "cursor-agent": "cursor"}
    agent = aliases.get(agent, agent)
    if agent not in {"claude", "codex", "opencode", "antigravity", "cursor", "unknown"}:
        agent = "unknown"
    sid = scope.get("session_id", "")
    if not isinstance(sid, str) or not sid or len(sid) > 256 or any(ord(c) < 32 for c in sid):
        raise ValueError("Health requires a bounded explicit session_id")
    return {"project_path": str(Path(project).resolve()), "agent": agent, "session_id": sid}


def _details(kind, raw):
    """Closed fields: never serialize arbitrary tool arguments or provider errors."""
    clean = {}
    if raw.get("state") == "reviewed_quiet":
        raw = {**raw, "state": "quiet"}
    if kind == "activity":
        clean["active"] = raw.get("active", True) is True
    choices = {"state": {"queued", "completed", "quiet", "failed", "unavailable", "budget_exhausted", "skipped", "disabled", "discarded"},
               "verdict": {"useful", "dismissed", "acted_on"},
               "outcome": {"success", "failed", "observed"},
               "initiator": {"agent", "automatic"},
               "source": {"mcp", "native", "hook", "supervisor", "worker", "explicit"}}
    for key, allowed in choices.items():
        if raw.get(key) in allowed:
            clean[key] = raw[key]
    if raw.get("review_id") is not None:
        clean["review_id"] = hashlib.sha256(str(raw["review_id"]).encode()).hexdigest()
    if raw.get("tool") in TOOLS:
        clean["tool"] = raw["tool"]
    # Reasons are deliberately closed; unknown provider strings are not retained.
    reasons = {"llm_unavailable", "worker_unavailable", "transcript_unavailable", "review_failed",
               "budget_exhausted", "timeout", "invalid_response", "no_findings", "advisory",
               "missing_credentials", "disabled", "provider_error", "stale", "superseded"}
    if raw.get("reason") in reasons:
        clean["reason"] = raw["reason"]
    return clean


def record(scope, kind, details=None):
    """Record one scoped event. Optional timestamp/event_id allow deduplicated observation."""
    if kind not in KINDS:
        raise ValueError("Unknown health event kind")
    scope = _scope(scope)
    raw = details or {}
    at = _time(raw.get("timestamp", raw.get("at")))
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    event_id = raw.get("event_id")
    event_key = hashlib.sha256(json.dumps([key, kind, str(event_id)]).encode()).hexdigest() if event_id is not None else None
    clean = _details(kind, raw)
    with _db() as conn:
        conn.execute("INSERT INTO scopes VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen), last_seen=max(last_seen,excluded.last_seen)",
                     (key, scope["project_path"], scope["agent"], scope["session_id"], at, at))
        conn.execute("INSERT OR IGNORE INTO events(scope_id,kind,at,details,event_key) VALUES (?,?,?,?,?)",
                     (key, kind, at, json.dumps(clean), event_key))
    return {"scope_id": key, "kind": kind, "at": _iso(at)}


def record_automatic(scope, kind, details=None):
    """Hooks/MCP never create default HOME state until the monitor is opted in.

    An explicit diagnostic directory is an isolated recording opt-in. Public
    record/check/status are deliberate operator calls and remain available when
    notifications are disabled.
    """
    if not os.environ.get("KIN_HEALTH_DIR") and not settings()["enabled"]:
        return {"recorded": False, "reason": "disabled"}
    return record(scope, kind, details)


def record_mcp(tool, outcome="success"):
    """MCP process identity is explicit or unattributed; never borrow a nearby session."""
    if tool not in TOOLS:
        return
    sid = os.environ.get("KIN_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_SESSION_ID")
    agent = os.environ.get("KIN_CLIENT") or os.environ.get("KINDEX_CLIENT") or "unknown"
    project = os.environ.get("KIN_PROJECT_PATH") or os.getcwd()
    record_automatic({"project_path": project, "agent": agent, "session_id": sid or "unattributed"},
           "use", {"tool": tool, "outcome": outcome, "source": "mcp", "initiator": "agent"})


def _summarize(scope, rows, now, cfg):
    events = [{**dict(row), "details": json.loads(row["details"])} for row in rows]
    by_kind = {kind: [e for e in events if e["kind"] == kind] for kind in KINDS}
    last = {kind: (group[-1]["at"] if group else None) for kind, group in by_kind.items()}
    all_activity = by_kind["activity"]
    activity = [e for e in all_activity if e["details"].get("source") == "native"]
    native_active = bool(activity and activity[-1]["details"].get("active", True) and 0 <= now - activity[-1]["at"] <= cfg["active_seconds"])
    active = bool(all_activity and all_activity[-1]["details"].get("active", True) and 0 <= now - all_activity[-1]["at"] <= cfg["active_seconds"])
    uses = [e for e in by_kind["use"] if e["details"].get("outcome") in {"success", "observed"} and e["details"].get("initiator", "agent") == "agent"]
    feedback = by_kind["feedback"]
    value = "unverified"
    if feedback:
        value = {"useful": "explicitly_useful", "acted_on": "explicitly_acted_on", "dismissed": "explicitly_dismissed"}.get(feedback[-1]["details"].get("verdict"), "unverified")
    summary = {"scope_id": scope["id"], "project_path": scope["project_path"], "agent": scope["agent"],
               "session_id": scope["session_id"], "active": active,
               "activity_evidence": "native_observed" if any(e["details"].get("source") == "native" for e in activity) else "reported" if all_activity else "not_observed",
               "last": {k: _iso(v) for k, v in last.items()},
               "counts": {k: len(v) for k, v in by_kind.items()}, "value": value,
               "use_evidence": "observed" if uses else "not_observed"}
    found = []
    def issue(code, reason):
        found.append({"id": scope["id"] + ":" + code, "code": code, "reason": reason,
                      "scope": {k: summary[k] for k in ("project_path", "agent", "session_id")},
                      "evidence": {"last": summary["last"], "counts": summary["counts"]},
                      "diagnostic": _diagnostic()})
    # Reported activity can accompany real review/queue/feedback failures.
    # Only independently observed activity supports missed-hook/use claims.
    if native_active:
        first_active = activity[-1]["at"]
        for before, after in zip(reversed(activity[:-1]), reversed(activity[1:])):
            if after["at"] - before["at"] > cfg["active_seconds"] or not before["details"].get("active", True):
                break
            first_active = before["at"]
        # Checker wall time cannot manufacture work after the last native event.
        observed_through = activity[-1]["at"]
        hook_at = last["hook"]
        # A receipt can precede the first observed native response by a few
        # seconds; allow the same bounded grace when associating that receipt.
        has_episode_hook = hook_at is not None and hook_at >= first_active - cfg["hook_grace_seconds"]
        if not has_episode_hook:
            hook_missing = now - first_active >= cfg["hook_grace_seconds"]
        else:
            hook_missing = observed_through - hook_at >= cfg["hook_grace_seconds"]
        if hook_missing:
            issue("missing_hooks", "Native session activity was observed but a corresponding recent Kindex hook invocation was not.")
        if observed_through - max(uses[-1]["at"] if uses else 0, first_active) >= cfg["use_grace_seconds"]:
            issue("missing_use", "Sustained active work has no recent successful or natively observed agent-initiated Kindex use; intent is not inferred.")
    reviews = by_kind["review"]
    outcomes = [e for e in reviews if e["details"].get("state") in {"completed", "quiet", "failed", "unavailable", "budget_exhausted"}]
    failures = 0
    for event in reversed(outcomes):
        if event["details"]["state"] in {"completed", "quiet"}:
            break
        failures += 1
    if failures >= cfg["failure_threshold"] and active:
        issue("review_failures", "Consecutive review failures prevent a fresh lookback.")
    deliveries = by_kind["delivery"]
    def settled(event, candidates):
        review_id = event["details"].get("review_id")
        return any(candidate["at"] >= event["at"] and (
            candidate["details"].get("review_id") == review_id if review_id else True
        ) for candidate in candidates)
    discarded = [e for e in reviews if e["details"].get("state") == "discarded"]
    pending = [e for e in reviews if e["details"].get("state") == "queued" and not settled(e, outcomes + deliveries + discarded)]
    completed = [e for e in reviews if e["details"].get("state") == "completed" and
                 e["details"].get("reason") == "advisory" and not settled(e, deliveries + discarded)]
    if active and (pending or completed) and now - min(e["at"] for e in pending + completed) >= cfg["queue_grace_seconds"]:
        issue("undelivered_review", "A queued review or completed advisory remains undelivered beyond the grace period.")
    dismissed = 0
    for event in reversed(feedback):
        if event["details"].get("verdict") != "dismissed":
            break
        dismissed += 1
    if dismissed >= cfg["dismissed_threshold"] and active:
        issue("dismissed_advice", "Repeated explicit dismissals warrant review of relevance; delivery alone does not demonstrate value.")
    summary["feedback_counts"] = {verdict: sum(e["details"].get("verdict") == verdict for e in feedback)
                                  for verdict in ("useful", "dismissed", "acted_on")}
    summary["consecutive_review_failures"] = failures
    summary["completed_reviews"] = sum(e["details"].get("state") in {"completed", "quiet"} for e in reviews)
    return summary, found


def _sendmail(issue):
    """Return transport acceptance, not a claim of mailbox delivery. Never shell out."""
    from email.message import EmailMessage
    message = EmailMessage()
    message["To"] = "root"
    message["From"] = "kindex@localhost"
    message["Subject"] = "Kindex health: " + issue["code"]
    message.set_content("Local Kindex health diagnostic. No conversation content is included.\n\n" +
                        json.dumps(issue, indent=2, ensure_ascii=True) +
                        "\n\nTransport acceptance does not prove root mailbox delivery.\n")
    try:
        result = subprocess.run([settings()["sendmail_path"], "-i", "--", "root"], input=message.as_bytes(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"accepted": False, "error": type(exc).__name__}
    return {"accepted": result.returncode == 0, "error": None if result.returncode == 0 else "sendmail_exit_" + str(result.returncode)}


def check_health(now=None, notify=False):
    now = _time(now)
    cfg = settings()
    from .supervisor_health_activity import observe_activity
    coverage = observe_activity(now)
    summaries, found = [], []
    for agent, evidence in coverage.items():
        if evidence.get("state") == "unavailable" or evidence.get("errors", 0) or evidence.get("unidentified", 0):
            found.append({"id": "observer:" + agent, "code": "observation_unavailable",
                          "reason": "Native session observation failed or recent activity could not be scoped; Kindex use and hook health are unverified for this evidence.",
                          "scope": {"project_path": None, "agent": agent, "session_id": None},
                          "evidence": evidence, "diagnostic": _diagnostic()})
    with _db() as conn:
        for scope in conn.execute("SELECT * FROM scopes ORDER BY last_seen DESC LIMIT 1000").fetchall():
            rows = conn.execute("SELECT * FROM (SELECT * FROM events WHERE scope_id=? AND at<=? ORDER BY at DESC,id DESC LIMIT 5000) ORDER BY at,id", (scope["id"], now)).fetchall()
            summary, issues = _summarize(scope, rows, now, cfg)
            summaries.append(summary)
            found.extend(issues)
        current = {issue["id"] for issue in found}
        for old in conn.execute("SELECT id FROM issues WHERE active=1").fetchall():
            if old["id"] not in current:
                conn.execute("UPDATE issues SET active=0,consecutive=0 WHERE id=?", (old["id"],))
        for issue in found:
            conn.execute("INSERT INTO issues(id,first_seen,last_seen,consecutive,active) VALUES (?,?,?,1,1) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, consecutive=CASE WHEN active=1 THEN consecutive+1 ELSE 1 END,active=1",
                         (issue["id"], now, now))
            state = dict(conn.execute("SELECT * FROM issues WHERE id=?", (issue["id"],)).fetchone())
            due = state["consecutive"] >= cfg["consecutive_checks"] and (state["last_notified"] is None or now - state["last_notified"] >= cfg["cooldown_seconds"])
            issue.update({"consecutive_checks": state["consecutive"], "notification_due": due,
                          "last_notified": _iso(state["last_notified"]), "transport_error": state["transport_error"]})
            if notify and cfg["enabled"] and cfg["mail_enabled"] and due:
                result = _sendmail(issue)
                if result["accepted"]:
                    conn.execute("UPDATE issues SET last_notified=?,transport_error=NULL WHERE id=?", (now, issue["id"]))
                    issue.update({"last_notified": _iso(now), "transport_accepted": True, "transport_error": None, "notification_due": False})
                else:
                    conn.execute("UPDATE issues SET transport_error=? WHERE id=?", (result["error"], issue["id"]))
                    issue.update({"transport_accepted": False, "transport_error": result["error"]})
        result = {"checked_at": _iso(now), "enabled": cfg["enabled"], "mail_enabled": cfg["mail_enabled"], "issues": found, "sessions": summaries,
                  "coverage": coverage, "value": "Explicit feedback is required; activity, review completion and delivery alone do not establish usefulness."}
        conn.execute("INSERT INTO meta VALUES ('last_check',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(result),))
        # Bounded retention, with persistent notification state across checker restarts.
        conn.execute("DELETE FROM events WHERE at<?", (now - 30 * 86400,))
        conn.execute("DELETE FROM scopes WHERE last_seen<?", (now - 30 * 86400,))
        conn.execute("DELETE FROM issues WHERE active=0 AND last_seen<?", (now - 30 * 86400,))
    return result


def status():
    """Read evidence without incrementing checker failure counts or sending mail."""
    now, cfg = _time(), settings()
    summaries = []
    with _db() as conn:
        for scope in conn.execute("SELECT * FROM scopes ORDER BY last_seen DESC LIMIT 1000").fetchall():
            rows = conn.execute("SELECT * FROM (SELECT * FROM events WHERE scope_id=? AND at<=? ORDER BY at DESC,id DESC LIMIT 5000) ORDER BY at,id", (scope["id"], now)).fetchall()
            summary, _ = _summarize(scope, rows, now, cfg)
            summaries.append(summary)
        row = conn.execute("SELECT value FROM meta WHERE key='last_check'").fetchone()
    previous = json.loads(row[0]) if row else {}
    return {"enabled": cfg["enabled"], "mail_enabled": cfg["mail_enabled"], "sessions": summaries, "issues": previous.get("issues", []),
            "coverage": previous.get("coverage", {}), "checked_at": previous.get("checked_at"),
            "monitor": ("stale" if now - _time(previous["checked_at"]) > 180 else "checked") if row else "not_observed"}



def _atomic_write(path, content):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Refusing symlink configuration")
    if path.exists():
        backup = path.with_name(path.name + ".backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(path.read_bytes())
    fd, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_monitor(*, uninstall=False, dry_run=False):
    """Explicit local installation; never called by hooks or merely importing Kindex."""
    import plistlib
    import sys
    if sys.platform != "darwin":
        raise ValueError("Independent launchd installation requires macOS")
    root = health_dir().resolve()
    label = "com.kindex.supervisor-health"
    plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    command = [sys.executable, "-m", "kindex.supervisor_health", "check", "--json", "--quiet"]
    configuration = root / "config.json"
    old = json.loads(configuration.read_text()) if configuration.exists() else {}
    if not isinstance(old, dict):
        raise ValueError("Health configuration must be an object")
    updated = {**old, "enabled": not uninstall}
    mail_enabled = old.get("mail_enabled") is True
    if mail_enabled:
        command.append("--notify")
    plan = {"action": "uninstall" if uninstall else "install", "dry_run": dry_run,
            "plist": str(plist), "health_dir": str(root), "command": command, "interval_seconds": 60,
            "mail_enabled": mail_enabled}
    if dry_run:
        return plan
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    domain = "gui/" + str(os.getuid())
    if uninstall:
        result = subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label],
                                capture_output=True, timeout=15, check=False)
        # A service which is already absent is an idempotent uninstall.
        if result.returncode not in (0, 3, 113):
            raise ValueError("launchctl bootout failed")
        _atomic_write(configuration, json.dumps(updated, indent=2).encode())
        if plist.exists():
            _atomic_write(plist, plist.read_bytes())
            plist.unlink()
        return {**plan, "enabled": False}
    for name in ("stdout.log", "stderr.log"):
        fd = os.open(root / name, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
    payload = {"Label": label, "ProgramArguments": command, "StartInterval": 60,
               "RunAtLoad": True, "WorkingDirectory": str(Path.home()),
               "EnvironmentVariables": {"KIN_HEALTH_DIR": str(root), "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
               "StandardOutPath": str(root / "stdout.log"), "StandardErrorPath": str(root / "stderr.log")}
    previous_plist = plist.read_bytes() if plist.exists() else None
    _atomic_write(configuration, json.dumps(updated, indent=2).encode())
    _atomic_write(plist, plistlib.dumps(payload))
    subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label],
                   capture_output=True, timeout=15, check=False)
    result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)],
                            capture_output=True, timeout=15, check=False)
    if result.returncode:
        _atomic_write(configuration, json.dumps(old, indent=2).encode())
        if previous_plist is not None:
            _atomic_write(plist, previous_plist)
            subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)],
                           capture_output=True, timeout=15, check=False)
        else:
            plist.unlink(missing_ok=True)
        raise ValueError("launchctl bootstrap failed; previous files restored")
    return {**plan, "enabled": True, "launchd_accepted": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "check", "feedback", "install", "uninstall"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Persist checker results without routine log output")
    parser.add_argument("--now", help="Local diagnostic clock override (ISO timestamp)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", help="Absolute project path for explicit feedback")
    parser.add_argument("--agent", choices=("claude", "codex", "opencode", "antigravity", "cursor"))
    parser.add_argument("--session", help="Explicit host session ID for feedback")
    parser.add_argument("--verdict", choices=("useful", "dismissed", "acted_on"))
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            result = check_health(now=args.now, notify=args.notify)
        elif args.command == "status":
            result = status()
        elif args.command == "feedback":
            if not all((args.project, args.agent, args.session, args.verdict)):
                raise ValueError("Feedback requires project, agent, session and verdict")
            result = record({"project_path": args.project, "agent": args.agent, "session_id": args.session},
                            "feedback", {"verdict": args.verdict, "source": "explicit"})
        else:
            result = install_monitor(uninstall=args.command == "uninstall", dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        result = {"ok": False, "error": type(exc).__name__, "monitor": "unavailable"}
        print(json.dumps(result))
        return 1
    if not args.quiet:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
