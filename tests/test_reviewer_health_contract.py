"""Independent H1--H3 reviewer-session health contract; Validator executes.

Authored against HEAD baseline health interfaces, not the implementation under
test. Native source enumeration is replaced; parser and health reconciliation
are real. Registration itself must never manufacture health evidence.
"""
import importlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from kindex import supervisor_health as health
from kindex import supervisor_health_activity as activity


NOW = 1_800_000_000.0


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "health"
    root.mkdir()
    monkeypatch.setenv("KIN_HEALTH_DIR", str(root))
    (root / "config.json").write_text(json.dumps({
        "enabled": True, "desktop_enabled": False, "mail_enabled": False,
        "active_seconds": 300, "hook_grace_seconds": 20,
        "use_grace_seconds": 20, "consecutive_checks": 1,
    }))
    files = {name: [] for name in ("codex", "claude", "antigravity")}
    monkeypatch.setattr(activity, "_files", lambda agent, now: (tmp_path, files[agent]))
    monkeypatch.setattr(activity, "_ag_projects", lambda: {})
    empty = lambda now: {"state": "not_observed", "sessions": 0}
    monkeypatch.setattr(activity, "_opencode", empty)
    monkeypatch.setattr(activity, "_cursor", empty)
    return {"root": root, "project": str(tmp_path / "project"), "files": files,
            "tmp": tmp_path}


def scope(box, sid="native-reviewer", agent="codex", project=None):
    return {"agent": agent, "session_id": sid,
            "project_path": project or box["project"]}


def active_window(identity):
    for offset in (60, 10):
        health.record(identity, "activity", {"at": NOW - offset, "source": "native",
                      "event_id": f"activity-{offset}", "active": True})


def events(box):
    with sqlite3.connect(box["root"] / "health.sqlite3") as conn:
        return conn.execute("SELECT scope_id,kind,at,details,event_key FROM events ORDER BY id").fetchall()


def missing(result):
    return {(issue["scope"]["agent"], issue["scope"]["session_id"],
             issue["scope"]["project_path"], issue["code"])
            for issue in result["issues"] if issue["code"] in {"missing_hooks", "missing_use"}}


def test_h1_registration_persists_without_synthesizing_events(box):
    identity = scope(box)
    active_window(identity)
    before = events(box)
    health.register_reviewer_session(**identity)
    health.register_reviewer_session(**identity)
    assert events(box) == before, "H1: registration metadata cannot invent hook/use/activity receipts"
    importlib.reload(health)
    report = health.check_health(now=NOW, notify=False)
    assert not missing(report), "H1: native reviewer identity remains excluded after module restart"
    assert events(box) == before
    assert not missing(health.status()), "H2: status agrees with the reconciled health check"


def test_h2_registration_exclusion_uses_exact_identity_and_preserves_other_sessions(box):
    reviewer = scope(box)
    normal = [scope(box, sid="ordinary-conversation"),
              scope(box, agent="claude"),
              scope(box, project=str(box["tmp"] / "other-project"))]
    for identity in [reviewer, *normal]:
        active_window(identity)
    health.register_reviewer_session(**reviewer)
    result = health.check_health(now=NOW, notify=False)
    expected = {(identity["agent"], identity["session_id"], identity["project_path"], code)
                for identity in normal for code in ("missing_hooks", "missing_use")}
    assert missing(result) == expected, "H2: exemption cannot spread to a neighboring session, host, or project"
    assert missing(health.status()) == expected


def agy_trace(box, sid):
    # Existing Antigravity native layout: path.parents[2] names the session.
    path = box["tmp"] / sid / "logs" / "run" / "events.jsonl"
    path.parent.mkdir(parents=True)
    stamp = datetime.fromtimestamp(NOW - 10, timezone.utc).isoformat()
    path.write_text(json.dumps({"type": "PLANNER_RESPONSE", "timestamp": stamp,
                               "content": "Synthetic reviewer response; deliberately no project metadata."}) + "\n")
    return path


def test_h2_registered_antigravity_without_project_metadata_is_not_unidentified(box):
    identity = scope(box, agent="antigravity")
    reviewer_file = agy_trace(box, identity["session_id"])
    box["files"]["antigravity"].append(reviewer_file)
    health.register_reviewer_session(**identity)
    first = health.check_health(now=NOW, notify=False)
    assert first["coverage"]["antigravity"].get("unidentified", 0) == 0
    assert not [issue for issue in first["issues"]
                if issue["code"] == "observation_unavailable" and issue["scope"]["agent"] == "antigravity"]
    assert not missing(first)
    ordinary_file = agy_trace(box, "ordinary-unidentified-session")
    box["files"]["antigravity"].append(ordinary_file)
    second = health.check_health(now=NOW + 1, notify=False)
    assert second["coverage"]["antigravity"]["unidentified"] == 1
    assert any(issue["code"] == "observation_unavailable" and issue["scope"]["agent"] == "antigravity"
               for issue in second["issues"]), "H2: a registered reviewer cannot mask other unidentified work"


def test_h3_registration_resolves_existing_alerts_without_erasing_history(box):
    identity = scope(box)
    active_window(identity)
    before_report = health.check_health(now=NOW, notify=False)
    assert len(missing(before_report)) == 2, "Fixture must first establish genuine missing hook/use alerts"
    before_alerts = health.inbox()["alerts"]
    assert len(before_alerts) == 2
    original_ids = {alert["id"] for alert in before_alerts}
    evidence = events(box)
    health.register_reviewer_session(**identity)
    result = health.check_health(now=NOW + 1, notify=False)
    assert not missing(result)
    after = health.inbox()
    assert {alert["id"] for alert in after["alerts"]} == original_ids
    assert all(alert["state"] == "resolved" and alert["resolved_at"] is not None
               for alert in after["alerts"]), "H3: registration resolves the existing occurrence, preserving its record"
    assert after["unread_count"] == 0
    assert events(box) == evidence, "H3: reconciliation must not fake a hook or delete native evidence"


def test_a_launcher_registers_its_automation_session_through_the_cli(box, monkeypatch, capsys):
    """Kinbase's Antigravity classifier runs leave transcripts with no
    workspace; the launcher registers each one with `kin supervisor-register`."""
    import sys

    from kindex import cli

    identity = scope(box, sid="classifier-turn", agent="antigravity")
    box["files"]["antigravity"].append(agy_trace(box, identity["session_id"]))
    unregistered = health.check_health(now=NOW, notify=False)
    assert unregistered["coverage"]["antigravity"]["unidentified"] == 1

    monkeypatch.setattr(sys, "argv", [
        "kin", "supervisor-register", "--agent", "antigravity",
        "--session", identity["session_id"], "--project", identity["project_path"], "--json",
    ])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["registered"] is True and out["session_id"] == "classifier-turn"

    registered = health.check_health(now=NOW + 1, notify=False)
    assert registered["coverage"]["antigravity"].get("unidentified", 0) == 0
    assert not [issue for issue in registered["issues"]
                if issue["code"] == "observation_unavailable"]


def test_supervisor_register_refuses_a_bad_identity_without_a_traceback(box, monkeypatch, capsys):
    import sys

    from kindex import cli

    monkeypatch.setattr(sys, "argv", [
        "kin", "supervisor-register", "--agent", "antigravity",
        "--session", "bad\nid", "--project", box["project"],
    ])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: session not registered")
    assert "Traceback" not in err
