"""H3 appendix from Validator-supplied public Codex host contracts only.

No product source inspected and no tests executed by the independent Tester.
Original health acceptance tests remain unchanged.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3

from test_health import b, issues, session, summaries


SID = "8a32b116-c615-4bbf-8239-2770d5347b83"


def native_codex(b, *, resumed=True, outside=False):
    """Public state_5.sqlite/rollout fixture supplied by the Validator."""
    codex_home = Path(b.env["CODEX_HOME"])
    codex_home.mkdir(parents=True, exist_ok=True)
    created = b.now - dt.timedelta(days=4)
    event_time = b.now - dt.timedelta(seconds=400) if resumed else created
    rollout_root = b.root / "outside-codex" if outside else codex_home
    rollout = (rollout_root / "sessions" / created.strftime("%Y/%m/%d") /
               f"rollout-{created.strftime('%Y-%m-%dT%H-%M-%S')}-{SID}.jsonl")
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": created.isoformat(), "type": "session_meta",
         "payload": {"id": SID, "cwd": str(b.project)}},
        {"timestamp": event_time.isoformat(), "type": "event_msg",
         "payload": {"type": "user_message", "message": "SYNTHETIC_PRIVATE_CODEX_RESUMPTION_219"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Mtime is independently fresh for both real-resumption and stale-event cases.
    os.utime(rollout, (b.now.timestamp(), b.now.timestamp()))
    with sqlite3.connect(codex_home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, cwd TEXT, archived INTEGER)")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                   (SID, str(rollout), int(created.timestamp()),
                    int(event_time.timestamp()), str(b.project), 0))
    return rollout


def test_codex_resumed_old_rollout_detects_missed_hooks(b):
    """H3: active resumed conversations retain old paths but require hook detection.

    Mutation: inspect only today's/recent date directories or session creation.
    """
    native_codex(b)
    report = b.check()
    scoped_issues = [issue for issue in issues(report, "missing_hooks")
                     if issue["scope"]["session_id"] == SID]
    assert len(scoped_issues) == 1
    assert scoped_issues[0]["scope"] == {
        "project_path": str(b.project), "agent": "codex", "session_id": SID,
    }
    assert session(report, SID)["activity_evidence"] == "native_observed"
    assert "SYNTHETIC_PRIVATE_CODEX_RESUMPTION_219" not in json.dumps(report)


def test_codex_fresh_file_mtime_does_not_make_stale_events_active(b):
    """H3: idle/stale event evidence does not become activity from file mtime.

    Mutation: use fresh file mtime as activity despite stale native event/index.
    """
    native_codex(b, resumed=False)
    report = b.check()
    assert not [issue for issue in issues(report, "missing_hooks")
                if issue["scope"]["session_id"] == SID]


def test_codex_index_outside_home_cannot_supply_native_activity(b):
    """H3 native-source boundary: index rollout outside CODEX_HOME is inadmissible.

    Mutation: follow arbitrary indexed rollout paths and admit their activity.
    Limit: output rejection alone cannot establish that no file read occurred.
    """
    native_codex(b, outside=True)
    report = b.check()
    assert not [issue for issue in issues(report, "missing_hooks")
                if issue["scope"]["session_id"] == SID]
    matches = [row for row in summaries(report)
               if row.get("scope", row).get("session_id") == SID]
    assert all(row["activity_evidence"] != "native_observed" for row in matches)
