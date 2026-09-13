"""Durable health inbox and fixed-command native desktop notification submission.

Transport acceptance never proves a banner was visible or a person read it.
"""
from datetime import datetime, timezone
import json
import math
import shlex
import subprocess
import sys
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PAYLOAD_BYTES = 16 * 1024
SUBMISSION_TIMEOUT_SECONDS = 10
CLAIM_LEASE_SECONDS = 2 * SUBMISSION_TIMEOUT_SECONDS
TRANSPORTS = ("desktop", "mail")
CODES = {"missing_hooks", "missing_use", "review_failures", "undelivered_review",
         "dismissed_advice", "observation_unavailable"}
REASONS = {
    "missing_hooks": "Native session activity was observed but a corresponding recent Kindex hook invocation was not.",
    "missing_use": "Sustained active work has no recent successful or natively observed agent-initiated Kindex use; intent is not inferred.",
    "review_failures": "Consecutive review failures prevent a fresh lookback.",
    "undelivered_review": "A queued review or completed advisory remains undelivered beyond the grace period.",
    "dismissed_advice": "Repeated explicit dismissals warrant review of relevance; delivery alone does not demonstrate value.",
    "observation_unavailable": "Native session observation failed or recent activity could not be scoped; Kindex use and hook health are unverified for this evidence.",
}
AGENTS = {"claude", "codex", "opencode", "antigravity", "cursor", "unknown"}
SCRIPT = '''on run argv
    display notification (item 1 of argv) with title "Kindex needs attention" subtitle (item 2 of argv)
end run'''


IssueCode = Literal["missing_hooks", "missing_use", "review_failures", "undelivered_review", "dismissed_advice", "observation_unavailable"]
EvidenceKind = Literal["hook", "use", "review", "delivery", "feedback", "activity"]


class _PersistentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class AlertScope(_PersistentModel):
    project_path: str | None
    session_id: str | None
    agent: Literal["claude", "codex", "opencode", "antigravity", "cursor", "unknown"]


class NativeSources(_PersistentModel):
    cli: Literal["session_metadata", "unverified"] | None = None
    ide: Literal["session_metadata", "unverified"] | None = None
    native_use: Literal["session_metadata", "unverified"] | None = None


class AlertEvidence(_PersistentModel):
    last: dict[EvidenceKind, str | None] | None = None
    counts: dict[EvidenceKind, int] | None = None
    sessions: int | None = Field(default=None, ge=0)
    unidentified: int | None = Field(default=None, ge=0)
    errors: int | None = Field(default=None, ge=0)
    state: Literal["observed", "not_observed", "unavailable"] | None = None
    source_present: bool | None = None
    sources: NativeSources | None = None
    reason: Literal["scan_limit"] | None = None
    omitted: Literal["payload_limit"] | None = None

    @field_validator("last")
    @classmethod
    def timestamps(cls, value):
        from .trust import parse_rfc3339
        for timestamp in (value or {}).values():
            if timestamp is not None:
                parse_rfc3339(timestamp, field="notification evidence timestamp")
        return value

    @field_validator("counts")
    @classmethod
    def nonnegative_counts(cls, value):
        if any(count < 0 for count in (value or {}).values()):
            raise ValueError("Notification counts must be nonnegative")
        return value


class AlertPayload(_PersistentModel):
    code: IssueCode
    scope: AlertScope
    reason: str
    evidence: AlertEvidence
    diagnostic: str

    @model_validator(mode="after")
    def fixed_reason(self):
        if self.reason != REASONS[self.code]:
            raise ValueError("Notification reason does not match its issue code")
        if len(self.model_dump_json(exclude_unset=True).encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Notification payload exceeds its size limit")
        return self


def _read_payload(encoded):
    if not isinstance(encoded, str) or len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Invalid persisted notification payload")
    try:
        return AlertPayload.model_validate_json(encoded)
    except ValueError:
        # Do not echo malformed persisted content through validation diagnostics.
        raise ValueError("Invalid persisted notification payload") from None


class AlertRecord(_PersistentModel):
    id: str = Field(min_length=1, max_length=128)
    issue_id: str = Field(min_length=1, max_length=512)
    created_at: float
    updated_at: float
    acknowledged_at: float | None
    resolved_at: float | None
    payload: AlertPayload

    @field_validator("payload", mode="before")
    @classmethod
    def persisted_payload(cls, value):
        return _read_payload(value) if isinstance(value, str) else value


class TransportRecord(_PersistentModel):
    alert_id: str = Field(min_length=1, max_length=128)
    name: Literal["desktop", "mail"]
    attempted_at: float | None
    accepted_at: float | None
    error: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    claim_until: float | None
    claim_token: str | None = Field(default=None, max_length=128)


def _record(model, row):
    try:
        return model.model_validate(dict(row))
    except ValueError:
        raise ValueError("Invalid persisted notification record") from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def ensure_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS health_alerts (
            id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, acknowledged_at REAL, resolved_at REAL,
            payload TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_health_alert
            ON health_alerts(issue_id) WHERE resolved_at IS NULL;
        CREATE TABLE IF NOT EXISTS health_alert_transports (
            alert_id TEXT NOT NULL, name TEXT NOT NULL, attempted_at REAL,
            accepted_at REAL, error TEXT, claim_until REAL, claim_token TEXT,
            PRIMARY KEY(alert_id,name));
    ''')
    # Serialize the additive migration so simultaneous first runs cannot race
    # ALTER TABLE. Existing in-flight legacy calls get one bounded grace window.
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(health_alert_transports)")}
        if "claim_token" not in columns:
            conn.execute("ALTER TABLE health_alert_transports ADD COLUMN claim_token TEXT")
            conn.execute("UPDATE health_alert_transports SET claim_until=? WHERE claim_until IS NOT NULL",
                         (time.time() + CLAIM_LEASE_SECONDS,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise



def diagnostic_command():
    return shlex.join([sys.executable, "-m", "kindex.supervisor_health", "status", "--json"])


def _timestamp(value):
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _projection(issue):
    """Copy the checker's closed diagnostic projection, never arbitrary event details."""
    code = issue.get("code")
    if code not in CODES:
        raise ValueError("Unknown notification issue code")
    scope = issue.get("scope", {})
    safe_scope = {"project_path": scope.get("project_path"),
                  "session_id": scope.get("session_id"),
                  "agent": scope.get("agent") if scope.get("agent") in AGENTS else "unknown"}
    for key in ("project_path", "session_id"):
        value = safe_scope[key]
        if value is not None and not isinstance(value, str):
            raise ValueError("Invalid notification scope")
    evidence = issue.get("evidence", {})
    safe_evidence = {}
    if isinstance(evidence, dict):
        for key in ("last", "counts"):
            values = evidence.get(key)
            if isinstance(values, dict):
                safe_evidence[key] = {kind: value for kind, value in values.items()
                                     if kind in {"hook", "use", "review", "delivery", "feedback", "activity"}
                                     and (value is None or isinstance(value, (int, float)) and math.isfinite(value)
                                          or isinstance(value, str) and len(value) <= 40 and _timestamp(value))}
        for key in ("sessions", "unidentified", "errors"):
            if isinstance(evidence.get(key), int):
                safe_evidence[key] = evidence[key]
        if evidence.get("state") in {"observed", "not_observed", "unavailable"}:
            safe_evidence["state"] = evidence["state"]
        if evidence.get("reason") == "scan_limit":
            safe_evidence["reason"] = "scan_limit"
        if isinstance(evidence.get("source_present"), bool):
            safe_evidence["source_present"] = evidence["source_present"]
        sources = evidence.get("sources")
        if isinstance(sources, dict):
            safe_evidence["sources"] = {key: value for key, value in sources.items()
                                        if key in {"cli", "ide", "native_use"}
                                        and value in {"session_metadata", "unverified"}}
    payload = {"code": code, "scope": safe_scope, "reason": REASONS[code],
               "evidence": safe_evidence,
               "diagnostic": diagnostic_command()}
    encoded = json.dumps(payload, ensure_ascii=True)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        payload["evidence"] = {"omitted": "payload_limit"}
        encoded = json.dumps(payload, ensure_ascii=True)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Notification identity exceeds payload limit")
    return AlertPayload.model_validate(payload).model_dump_json(exclude_unset=True)


def submit_desktop(alert, cfg):
    """Use a fixed script; all dynamic text is data passed in argv, never source."""
    executable = cfg["desktop_command"]
    if executable == "/usr/bin/osascript" and sys.platform != "darwin":
        return {"accepted": False, "error": "unsupported_platform"}
    subtitle = alert["scope"]["agent"] + ": " + alert["code"]
    body = "A sustained issue is in your local Kindex inbox. Alert " + alert["id"] + ". Run python3 -m kindex.supervisor_health inbox --json."
    try:
        result = subprocess.run([executable, "-e", SCRIPT, "--", body, subtitle],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=SUBMISSION_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return {"accepted": False, "error": "timeout"}
    except OSError as error:
        return {"accepted": False, "error": type(error).__name__}
    return {"accepted": result.returncode == 0,
            "error": None if result.returncode == 0 else "desktop_exit_" + str(result.returncode)}


def _alert(conn, row):
    record = _record(AlertRecord, row)
    value = record.payload.model_dump(mode="json", exclude_unset=True)
    value.update({"id": record.id, "issue_id": record.issue_id,
                  "created_at": _iso(record.created_at), "updated_at": _iso(record.updated_at),
                  "acknowledged_at": _iso(record.acknowledged_at), "resolved_at": _iso(record.resolved_at),
                  "state": "resolved" if record.resolved_at is not None else
                           "acknowledged" if record.acknowledged_at is not None else "unread",
                  "transports": {}})
    for name in TRANSPORTS:
        row = conn.execute("SELECT * FROM health_alert_transports WHERE alert_id=? AND name=?", (record.id, name)).fetchone()
        transport = _record(TransportRecord, row) if row else None
        value["transports"][name] = {"attempted_at": _iso(transport.attempted_at) if transport else None,
                                     "accepted_at": _iso(transport.accepted_at) if transport else None,
                                     "error": transport.error if transport else None}
    return value


def inbox_snapshot(conn):
    rows = conn.execute("SELECT * FROM health_alerts ORDER BY created_at DESC,id LIMIT 1000").fetchall()
    unread = conn.execute("SELECT count(*) FROM health_alerts WHERE acknowledged_at IS NULL AND resolved_at IS NULL").fetchone()[0]
    return {"alerts": [_alert(conn, row) for row in rows], "unread_count": unread}


def transport_snapshot(conn, cfg):
    result = {}
    for name in TRANSPORTS:
        row = conn.execute("SELECT * FROM health_alert_transports WHERE name=? AND attempted_at IS NOT NULL ORDER BY attempted_at DESC LIMIT 1", (name,)).fetchone()
        record = _record(TransportRecord, row) if row else None
        enabled = cfg["enabled"] and cfg[name + "_enabled"]
        result[name] = {"enabled": enabled,
                        "attempted_at": _iso(record.attempted_at) if record else None,
                        "accepted_at": _iso(record.accepted_at) if record else None,
                        "error": record.error if record else None,
                        "state": "disabled" if not enabled else "not_attempted" if not record else
                                 "failed" if record.error else "submitted" if record.accepted_at else "attempting"}
    return result


def acknowledge(conn, alert_id, now):
    row = conn.execute("SELECT * FROM health_alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "not_found", "id": alert_id}
    conn.execute("UPDATE health_alerts SET acknowledged_at=coalesce(acknowledged_at,?),updated_at=? WHERE id=?", (now, now, alert_id))
    row = conn.execute("SELECT * FROM health_alerts WHERE id=?", (alert_id,)).fetchone()
    return {"ok": True, "alert": _alert(conn, row)}


def reconcile(conn, issues, now, cfg, notify, mail_sender):
    current = {issue["id"] for issue in issues}
    for row in conn.execute("SELECT id,issue_id FROM health_alerts WHERE resolved_at IS NULL").fetchall():
        if row["issue_id"] not in current:
            conn.execute("UPDATE health_alerts SET resolved_at=?,updated_at=? WHERE id=?", (now, now, row["id"]))
    if cfg["enabled"]:
        for issue in issues:
            if issue["consecutive_checks"] < cfg["consecutive_checks"]:
                continue
            payload = _projection(issue)
            existing = conn.execute("SELECT id FROM health_alerts WHERE issue_id=? AND resolved_at IS NULL", (issue["id"],)).fetchone()
            if existing:
                conn.execute("UPDATE health_alerts SET payload=?,updated_at=? WHERE id=?", (payload, now, existing["id"]))
            else:
                alert_id = str(uuid.uuid4())
                conn.execute("INSERT INTO health_alerts VALUES (?,?,?,?,NULL,NULL,?)", (alert_id, issue["id"], now, now, payload))
                for name in TRANSPORTS:
                    conn.execute("INSERT INTO health_alert_transports(alert_id,name) VALUES (?,?)", (alert_id, name))
    # The durable inbox exists before any external transport is attempted.
    conn.commit()
    if notify and cfg["enabled"]:
        rows = conn.execute("SELECT * FROM health_alerts WHERE resolved_at IS NULL AND acknowledged_at IS NULL ORDER BY created_at LIMIT 1000").fetchall()
        for row in rows:
            alert = _alert(conn, row)
            for name in TRANSPORTS:
                if not cfg[name + "_enabled"]:
                    continue
                # Lease clocks are real wall time, sampled per attempt. The
                # diagnostic --now controls evidence/cooldown, never ownership.
                conn.execute("BEGIN IMMEDIATE")
                lease_now = time.time()
                claim_token = str(uuid.uuid4())
                updated = conn.execute("UPDATE health_alert_transports SET attempted_at=?,claim_until=?,claim_token=? WHERE alert_id=? AND name=? AND (claim_until IS NULL OR claim_until<=?) AND (accepted_at IS NULL OR accepted_at<=?) AND EXISTS (SELECT 1 FROM health_alerts WHERE id=? AND acknowledged_at IS NULL AND resolved_at IS NULL)",
                                       (now, lease_now + CLAIM_LEASE_SECONDS, claim_token, row["id"], name, lease_now, now - cfg["cooldown_seconds"], row["id"]))
                conn.commit()
                if not updated.rowcount:
                    continue
                result = submit_desktop(alert, cfg) if name == "desktop" else mail_sender(alert)
                if result["accepted"]:
                    completed = conn.execute("UPDATE health_alert_transports SET accepted_at=?,error=NULL,claim_until=NULL,claim_token=NULL WHERE alert_id=? AND name=? AND claim_token=?",
                                             (now, row["id"], name, claim_token))
                    if completed.rowcount and name == "mail":
                        conn.execute("UPDATE issues SET last_notified=?,transport_error=NULL WHERE id=?", (now, row["issue_id"]))
                else:
                    completed = conn.execute("UPDATE health_alert_transports SET error=?,claim_until=NULL,claim_token=NULL WHERE alert_id=? AND name=? AND claim_token=?",
                                             (result["error"], row["id"], name, claim_token))
                    if completed.rowcount and name == "mail":
                        conn.execute("UPDATE issues SET transport_error=? WHERE id=?", (result["error"], row["issue_id"]))
                conn.commit()
    conn.execute("DELETE FROM health_alert_transports WHERE alert_id IN (SELECT id FROM health_alerts WHERE resolved_at<?)", (now - 30 * 86400,))
    conn.execute("DELETE FROM health_alerts WHERE resolved_at<?", (now - 30 * 86400,))
    return {"unread_count": inbox_snapshot(conn)["unread_count"], "transports": transport_snapshot(conn, cfg)}
