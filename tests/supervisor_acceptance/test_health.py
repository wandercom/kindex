"""Independent H1-H5 acceptance tests; authored without product-source access.

Oracle: health-contract.md H1-H5 plus Validator's shared public API/fixtures.
Every assert below is governed by the citing test docstring or helper comment.
The Validator, not the Tester, executes these tests.
"""
import datetime as dt
from email import message_from_string
from email.utils import getaddresses
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


class Boundary:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.state = root / "health"
        self.project = root / "project"
        for path in (self.home, self.state, self.project):
            path.mkdir()
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.scope = {"project_path": str(self.project), "agent": "claude", "session_id": "independent-session"}
        self.mail_log = root / "transport.jsonl"
        self.fail = root / "transport-fails"
        self.sendmail = root / "fake-sendmail"
        self.sendmail.write_text(
            "#!" + sys.executable + "\n"
            "import json, pathlib, sys\n"
            f"log = pathlib.Path({str(self.mail_log)!r})\n"
            "with log.open('a') as out:\n"
            " out.write(json.dumps({'argv':sys.argv[1:],'message':sys.stdin.read()})+'\\n')\n"
            f"if pathlib.Path({str(self.fail)!r}).exists():\n"
            " sys.stderr.write('synthetic transport failure\\n')\n"
            " sys.exit(75)\n"
        )
        self.sendmail.chmod(0o700)
        inherited_names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
        self.env = {name: os.environ[name] for name in inherited_names if name in os.environ}
        self.env.update(HOME=str(self.home), KIN_HEALTH_DIR=str(self.state),
                        CODEX_HOME=str(self.home / ".codex"),
                        CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
                        XDG_DATA_HOME=str(self.home / ".local/share"),
                        XDG_CONFIG_HOME=str(self.home / ".config"))
        self.config = dict(enabled=True, mail_enabled=True, desktop_enabled=False, active_seconds=7200,
                           hook_grace_seconds=300, use_grace_seconds=1800,
                           queue_grace_seconds=900, failure_threshold=3,
                           dismissed_threshold=3, consecutive_checks=2,
                           cooldown_seconds=21600, sendmail_path=str(self.sendmail))
        self.configure()

    def iso(self, offset=0):
        return (self.now + dt.timedelta(seconds=offset)).isoformat()

    def configure(self, **changes):
        self.config.update(changes)
        (self.state / "config.json").write_text(json.dumps(self.config))

    def record(self, kind, offset=0, scope=None, **details):
        # Shared public record API. Each process is fresh (H1/H4 persistence).
        payload = {"scope": scope or self.scope, "kind": kind,
                   "details": {"at": self.iso(offset), **details}}
        result = subprocess.run(
            [sys.executable, "-c", "import json,sys; from kindex.supervisor_health import record; p=json.load(sys.stdin); record(p['scope'],p['kind'],p['details'])"],
            input=json.dumps(payload), text=True, capture_output=True, env=self.env,
        )
        assert result.returncode == 0, result.stderr  # Public API success contract.

    def command(self, command, notify=False, offset=0):
        args = [sys.executable, "-m", "kindex.supervisor_health", command, "--json"]
        if command == "check":
            args += ["--now", self.iso(offset)]
        if notify:
            args += ["--notify"]
        result = subprocess.run(args, text=True, capture_output=True, env=self.env)
        assert result.returncode in (0, 1), result.stderr  # Shared CLI: JSON report, not invocation failure.
        return json.loads(result.stdout)

    def check(self, **kwargs):
        return self.command("check", **kwargs)

    def mail(self):
        return [json.loads(line) for line in self.mail_log.read_text().splitlines()] if self.mail_log.exists() else []

    def seed_active(self):
        self.record("activity", -60, active=True, source="explicit")
        self.record("hook", -50)
        self.record("use", -40, tool="search", initiator="agent", outcome="success")

    def failures(self):
        self.seed_active()
        for n in range(3):
            self.record("review", -30 + n, state="failed")

    def native_claude(self, age=400):
        # Validator-supplied public Claude host fixture; no product internals.
        path = self.home / ".claude/projects" / str(self.project).replace("/", "-") / "independent-session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "uuid": "native-event-1",
                                   "sessionId": self.scope["session_id"],
                                   "cwd": str(self.project), "timestamp": self.iso(-age),
                                   "message": {"role": "user", "content": "SYNTHETIC_PRIVATE_TRANSCRIPT_47"}}) + "\n")
        stamp = (self.now - dt.timedelta(seconds=age)).timestamp()
        os.utime(path, (stamp, stamp))
        return path


@pytest.fixture
def b(tmp_path):
    return Boundary(tmp_path)


def issues(report, code):
    return [issue for issue in report["issues"] if issue["code"] == code]


def summaries(report):
    value = report["sessions"]
    return list(value.values()) if isinstance(value, dict) else value


def session(report, session_id="independent-session"):
    matches = [row for row in summaries(report)
               if row.get("scope", row).get("session_id") == session_id]
    assert len(matches) == 1  # H1: scoped evidence must survive and be independently reportable.
    return matches[0]


def test_disabled_default_cannot_send_mail(b):
    """H5 defaults off; H4 root-mail control. Mutation: enable absent config."""
    # Keep fake transport configured; omit only enabled, so no real mail is possible.
    del b.config["enabled"]
    b.configure()
    b.failures()
    b.check(notify=True)
    result = b.check(notify=True)
    assert result["enabled"] is False
    assert b.mail() == []


def test_evidence_survives_restart_and_stays_scoped(b):
    """H1 persistence/scope, H2 value. Mutation: merge sessions or drop events."""
    b.seed_active()
    other = {**b.scope, "agent": "codex", "session_id": "other-session"}
    b.record("review", scope=other, state="quiet")
    result = b.command("status")
    first, second = session(result), session(result, "other-session")
    assert first["counts"]["hook"] == 1
    assert first["counts"]["use"] == 1
    assert first["last"]["hook"] is not None
    assert dt.datetime.fromisoformat(first["last"]["hook"].replace("Z", "+00:00")) == b.now - dt.timedelta(seconds=50)
    assert second["counts"].get("hook", 0) == 0
    assert second["completed_reviews"] == 1
    assert first["value"] == "unverified"


def test_duplicate_event_id_is_not_counted_twice(b):
    """Shared event_id dedup API; H1 evidence counts. Mutation: append replay."""
    for _ in range(2):
        b.record("hook", event_id="same-event")
    assert session(b.command("status"))["counts"]["hook"] == 1


@pytest.mark.parametrize("state", ["quiet", "reviewed_quiet"])
def test_quiet_reviews_are_completed_and_not_failures(b, state):
    """H2 valid quiet work. Mutation: classify quiet as failure/incomplete."""
    b.seed_active()
    for n in range(3):
        b.record("review", -20 + n, state=state)
    result = b.check()
    assert not issues(result, "review_failures")
    assert not issues(result, "undelivered_review")
    assert session(result)["completed_reviews"] == 3
    assert session(result)["value"] == "unverified"


def test_delivery_and_self_rating_do_not_prove_value(b):
    """H2 evidence honesty. Mutation: delivery or self-rating becomes useful."""
    b.seed_active()
    b.record("review", -20, state="completed", reason="advisory", usefulness=1, value="explicitly_useful")
    b.record("delivery", -10, usefulness=1, value="explicitly_useful")
    assert session(b.check())["value"] == "unverified"


@pytest.mark.parametrize("verdict,expected", [("useful", "explicitly_useful"),
                                             ("acted_on", "explicitly_acted_on"),
                                             ("dismissed", "explicitly_dismissed")])
def test_explicit_feedback_is_reported(b, verdict, expected):
    """H2 explicit feedback. Mutation: ignore verdict or fabricate another."""
    b.seed_active()
    b.record("feedback", verdict=verdict)
    assert session(b.check())["value"] == expected


def test_idle_session_does_not_raise_missing_hook(b):
    """H3 idle exemption. Mutation: stale timestamp alone triggers alarm."""
    b.record("activity", -4000, active=False, source="explicit")
    result = b.check()
    assert not issues(result, "missing_hooks")
    assert b.mail() == []


def test_explicit_activity_is_not_native_proof_of_missed_hooks(b):
    """H3 distinguish reported vs observed. Mutation: trust report as native."""
    b.record("activity", -400, active=True, source="explicit")
    result = b.check()
    assert not issues(result, "missing_hooks")
    assert session(result)["activity_evidence"] == "reported"


def test_native_session_without_any_hook_is_discovered_independently(b):
    """H3 central red-now target. Mutation: enumerate registry/hooks only."""
    b.native_claude(age=400)
    result = b.check()
    found = issues(result, "missing_hooks")
    assert found
    assert found[0]["scope"] == b.scope
    assert session(result)["activity_evidence"] == "native_observed"
    assert "SYNTHETIC_PRIVATE_TRANSCRIPT_47" not in json.dumps(result)


def test_native_hook_grace_suppresses_early_allegation(b):
    """H3/H4 hook grace. Mutation: native activity immediately alarms."""
    b.native_claude(age=30)
    assert not issues(b.check(), "missing_hooks")


def test_automatic_retrieval_does_not_mask_missing_agent_use(b):
    """H1 automatic/agent distinction; H2 missing use. Mutation: count all use."""
    b.record("activity", -2000, active=True, source="native")
    b.record("hook", -1990)
    b.record("use", -10, tool="context", initiator="automatic", outcome="success")
    # Native work must advance beyond grace; idle wall time alone is insufficient.
    b.record("activity", -2, active=True, source="native")
    assert issues(b.check(), "missing_use")
    b.record("use", -1, tool="search", initiator="agent", outcome="success")
    result = b.check()
    assert not issues(result, "missing_use")
    assert session(result)["use_evidence"] == "observed"


def test_queued_review_grace_then_delivery_resolves(b):
    """H2 prolonged queues/H4 grace. Mutation: queue never ages or delivery ignored."""
    b.seed_active()
    b.record("review", state="queued")
    assert not issues(b.check(offset=899), "undelivered_review")
    assert issues(b.check(offset=901), "undelivered_review")
    b.record("review", 902, state="completed", reason="advisory")
    b.record("delivery", 903)
    assert not issues(b.check(offset=904), "undelivered_review")


def test_repeated_dismissal_threshold(b):
    """H2 repeated dismissed advice. Mutation: threshold off or dismissal ignored."""
    b.seed_active()
    for n in range(2):
        b.record("feedback", -20 + n, verdict="dismissed")
    assert not issues(b.check(), "dismissed_advice")
    b.record("feedback", -1, verdict="dismissed")
    assert issues(b.check(), "dismissed_advice")


def test_notification_streak_cooldown_restart_and_actionable_root_mail(b):
    """H4 consecutive checks, persistent cooldown, root-only actionable mail.

    Mutation: send on first check, forget cooldown on restart, change recipient,
    omit scope/evidence/diagnostic, or treat transport request as non-persistent.
    """
    b.failures()
    first = b.check(notify=True)
    first_issue = issues(first, "review_failures")[0]
    assert first_issue["consecutive_checks"] == 1
    assert b.mail() == []
    second = b.check(notify=True, offset=1)
    issue = issues(second, "review_failures")[0]
    assert issue["last_notified"] is not None
    assert len(b.mail()) == 1
    required = {"id", "code", "reason", "scope", "evidence", "diagnostic",
                "consecutive_checks", "notification_due", "last_notified", "transport_error"}
    assert required <= issue.keys()
    assert {"checked_at", "enabled", "issues", "sessions", "coverage"} <= second.keys()
    assert issue["scope"] == b.scope
    assert issue["evidence"]
    assert issue["reason"]
    assert "kindex" in issue["diagnostic"]
    mail = b.mail()[0]
    envelope_root = "root" in mail["argv"]
    headers = message_from_string(mail["message"])
    header_recipients = getaddresses(headers.get_all("To", []) + headers.get_all("Cc", []) + headers.get_all("Bcc", []))
    header_root = "-t" in mail["argv"] and [address for _, address in header_recipients] == ["root"]
    assert envelope_root or header_root
    assert not any("@" in arg for arg in mail["argv"])
    # H4 constrains the readable mail content; MIME transport encoding may wrap it.
    body = "\n".join(
        part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        for part in headers.walk() if part.get_content_type() == "text/plain"
    )
    assert b.scope["project_path"] in body
    assert b.scope["session_id"] in body
    assert b.scope["agent"] in body
    assert issue["diagnostic"] in body
    b.check(notify=True, offset=2)
    assert len(b.mail()) == 1


def test_transport_failure_is_visible_and_retryable(b):
    """H4 accept-before-mark. Mutation: mark notified despite send failure."""
    b.failures()
    b.fail.touch()
    b.check(notify=True)
    failed = issues(b.check(notify=True, offset=1), "review_failures")[0]
    assert failed["transport_error"]
    assert failed["last_notified"] is None
    assert len(b.mail()) == 1
    b.fail.unlink()
    recovered = issues(b.check(notify=True, offset=2), "review_failures")[0]
    assert len(b.mail()) == 2
    assert recovered["last_notified"] is not None
    assert not recovered["transport_error"]


def test_issue_cooldowns_are_independent(b):
    """H4 per-issue cooldown. Mutation: one mail suppresses all issue codes."""
    b.failures()
    b.check(notify=True)
    b.check(notify=True, offset=1)
    assert len(b.mail()) == 1
    for n in range(3):
        b.record("feedback", 2 + n, verdict="dismissed")
    b.check(notify=True, offset=5)
    report = b.check(notify=True, offset=6)
    assert len(b.mail()) == 2
    assert issues(report, "dismissed_advice")[0]["last_notified"] is not None


def test_sendmail_path_is_executed_without_shell_interpolation(b):
    """H4 executable transport/no shell. Mutation: shell command string dispatch."""
    literal_path = b.root / "fake sendmail; literal executable"
    b.sendmail.rename(literal_path)
    b.configure(sendmail_path=str(literal_path))
    b.failures()
    b.check(notify=True)
    report = b.check(notify=True, offset=1)
    assert len(b.mail()) == 1
    assert issues(report, "review_failures")[0]["last_notified"] is not None


def test_private_content_is_not_persisted_or_mailed(b):
    """H1/H4 privacy reaches ingestion and mail, with actual failure evidence.

    Mutation: persist raw detail dictionaries or interpolate untrusted metadata.
    """
    sentinel = "SYNTHETIC_PRIVATE_PROMPT_SECRET_938"
    b.seed_active()
    for n in range(3):
        b.record("review", -20 + n, state="failed", prompt=sentinel,
                 credentials=sentinel, api_key=sentinel, details={"token": sentinel})
    b.check(notify=True)
    result = b.check(notify=True, offset=1)
    assert issues(result, "review_failures")
    assert len(b.mail()) == 1
    assert sentinel not in json.dumps(result)
    assert sentinel not in b.mail()[0]["message"]
    for path in b.state.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), str(path)


def test_check_does_not_rewrite_user_config(b):
    """H5 config preservation. Mutation: replace user config with defaults."""
    b.configure(user_extension={"untouched": [1, 2, 3]})
    before = (b.state / "config.json").read_bytes()
    b.failures()
    b.check()
    assert (b.state / "config.json").read_bytes() == before
