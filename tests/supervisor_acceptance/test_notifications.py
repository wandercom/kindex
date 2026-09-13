"""Independent N1-N5 acceptance; public boundaries and synthetic native transport.

No product source inspected or tests executed by the Tester. The baseline
default-submission case reaches existing check behavior before any new inbox API.
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_health import Boundary, issues, session


PRIVATE_ERROR = "SYNTHETIC_NATIVE_PRIVATE_ERROR_714"


@pytest.fixture
def box(tmp_path):
    b = Boundary(tmp_path)
    b.env["CURSOR_CONFIG_DIR"] = str(b.home / ".cursor")
    b.desktop_log = b.root / "desktop-attempts.jsonl"
    b.desktop_fail = b.root / "desktop-fails"
    b.desktop_command = b.root / "fake desktop; literal executable"
    b.desktop_command.write_text(
        "#!" + sys.executable + "\n"
        "import json, pathlib, sys\n"
        f"with pathlib.Path({str(b.desktop_log)!r}).open('a') as out:\n"
        " out.write(json.dumps({'argv':sys.argv[1:]})+'\\n')\n"
        f"if pathlib.Path({str(b.desktop_fail)!r}).exists():\n"
        f" sys.stderr.write({PRIVATE_ERROR!r})\n"
        " sys.exit(71)\n"
    )
    b.desktop_command.chmod(0o700)
    # Exercise the ratified defaults, not inherited fake-mail opt-in settings.
    b.config.pop("desktop_enabled", None)
    b.config.pop("mail_enabled", None)
    b.configure(enabled=True, desktop_command=str(b.desktop_command))
    b.state.chmod(0o700)
    (b.state / "config.json").chmod(0o600)
    return b


def native_attempts(b):
    return [json.loads(line) for line in b.desktop_log.read_text().splitlines()] if b.desktop_log.exists() else []


def public_cli(b, *args):
    command = [sys.executable, "-m", "kindex.supervisor_health", *args, "--json"]
    if args and args[0] == "install" and "--dry-run" in args:
        # Simulated macOS plan qualification only; no native operation is performed.
        command = [sys.executable, "-c",
                   "from kindex import supervisor_health as health; import sys; sys.platform='darwin'; health.main()",
                   *args, "--json"]
    result = subprocess.run(command,
                            text=True, capture_output=True, env=b.env)
    assert result.returncode == 0, result.stderr  # N4 public CLI must be available.
    return json.loads(result.stdout)


def inbox(b):
    return public_cli(b, "inbox")


def acknowledge(b, alert_id):
    return public_cli(b, "ack", "--id", alert_id)


def sustain(b, *, notify=True):
    b.failures()
    first = b.check(notify=notify)
    second = b.check(notify=notify, offset=1)
    return first, second


def only_alert(report):
    assert len(report["alerts"]) == 1  # N1/N3: one scoped occurrence, not duplicates.
    return report["alerts"][0]


def test_default_desktop_submission_reaches_existing_checker_behavior(box):
    """N1/N2 behavioral red target: enabled monitoring surfaces a sustained issue.

    Mutation: retain check issues but never submit the default native alert.
    Submission assertions precede new CLI/schema assertions to avoid null-red claims.
    """
    first, second = sustain(box)
    assert issues(first, "review_failures")
    problem = issues(second, "review_failures")[0]
    assert len(native_attempts(box)) == 1
    assert box.mail() == []
    report = inbox(box)
    alert = only_alert(report)
    assert alert["state"] == "unread"
    assert report["unread_count"] == 1
    for field in ("issue_id", "code", "scope", "reason", "evidence", "diagnostic"):
        expected = problem["id"] if field == "issue_id" else problem[field]
        assert alert[field] == expected
    assert alert["acknowledged_at"] is None
    assert alert["resolved_at"] is None
    assert alert["transports"]["desktop"]["accepted_at"] is not None
    assert alert["transports"]["mail"]["accepted_at"] is None
    assert second["unread_count"] == 1
    assert {"desktop", "mail"} <= set(second["transports"])
    assert session(second)["value"] == "unverified"


def test_inbox_is_durable_even_when_notification_was_not_requested(box):
    """N1/N4: durable discovery survives checker restart independently of banners.

    Mutation: create inbox only inside transport code, or keep records in memory.
    """
    _, checked = sustain(box, notify=False)
    assert issues(checked, "review_failures")
    first = inbox(box)
    original = only_alert(first)
    later = inbox(box)  # Public helper launches a fresh process each time.
    assert only_alert(later)["id"] == original["id"]
    assert later["unread_count"] == 1
    assert original["state"] == "unread"
    assert original["transports"]["desktop"]["accepted_at"] is None
    assert original["transports"]["desktop"]["attempted_at"] is None
    assert native_attempts(box) == []
    assert box.mail() == []
    status = box.command("status")
    assert status["unread_count"] == 1
    assert {"desktop", "mail"} <= set(status["transports"])


def test_ack_and_cooldown_persist_without_resolving_the_problem(box):
    """N1/N3: cooldown and explicit ack suppress repeats, not underlying evidence.

    Mutation: forget cooldown on restart, equate ack with resolution, or notify an
    acknowledged occurrence again after cooldown.
    """
    box.configure(cooldown_seconds=30)
    sustain(box)
    original = only_alert(inbox(box))
    repeated = box.check(notify=True, offset=2)
    assert issues(repeated, "review_failures")
    assert len(native_attempts(box)) == 1
    answer = acknowledge(box, original["id"])
    assert answer["ok"] is True
    assert answer["alert"]["id"] == original["id"]
    assert answer["alert"]["state"] == "acknowledged"
    assert answer["alert"]["acknowledged_at"] is not None
    assert answer["alert"]["resolved_at"] is None
    fresh = inbox(box)
    assert only_alert(fresh)["id"] == original["id"]
    assert fresh["unread_count"] == 0
    after_cooldown = box.check(notify=True, offset=32)
    assert issues(after_cooldown, "review_failures")
    assert only_alert(inbox(box))["state"] == "acknowledged"
    assert len(native_attempts(box)) == 1
    assert session(after_cooldown)["value"] == "unverified"


def test_resolution_retains_history_and_new_occurrence_can_alert_again(box):
    """N3: a resolved occurrence remains history; recurrence gets a fresh alert ID.

    Mutation: delete history, never resolve inbox, or let old cooldown suppress a
    genuinely new occurrence.
    """
    box.native_claude(age=800)
    box.check(notify=True, offset=-400)
    first_problem = box.check(notify=True, offset=-399)
    assert issues(first_problem, "missing_hooks")
    original = only_alert(inbox(box))
    assert len(native_attempts(box)) == 1
    box.record("hook", -398)
    healthy = box.check(notify=True, offset=-398)
    assert not issues(healthy, "missing_hooks")
    resolved = only_alert(inbox(box))
    assert resolved["id"] == original["id"]
    assert resolved["state"] == "resolved"
    assert resolved["resolved_at"] is not None
    assert inbox(box)["unread_count"] == 0
    fresh_path = box.native_claude(age=1)
    fresh_mtime = fresh_path.stat().st_mtime
    fresh_event = json.loads(fresh_path.read_text())
    fresh_event["uuid"] = "native-event-recurrence-2"
    fresh_path.write_text(json.dumps(fresh_event) + "\n")
    os.utime(fresh_path, (fresh_mtime, fresh_mtime))
    box.check(notify=True)
    recurrence = box.check(notify=True, offset=1)
    assert issues(recurrence, "missing_hooks")
    history = inbox(box)
    assert len(history["alerts"]) == 2
    old = next(alert for alert in history["alerts"] if alert["id"] == original["id"])
    current = next(alert for alert in history["alerts"] if alert["id"] != original["id"])
    assert old["state"] == "resolved"
    assert current["state"] == "unread"
    assert current["issue_id"] == original["issue_id"]
    assert history["unread_count"] == 1
    assert len(native_attempts(box)) == 2


def test_native_failure_keeps_durable_unread_record_and_retries(box):
    """N1/N2: failed native submission is visible, sanitized and retryable.

    Mutation: mark success before acceptance, lose inbox on failure, suppress retry,
    or persist raw provider stderr.
    """
    box.desktop_fail.touch()
    _, failed_report = sustain(box)
    failed_inbox = inbox(box)
    original = only_alert(failed_inbox)
    receipt = original["transports"]["desktop"]
    assert receipt["attempted_at"] is not None
    assert receipt["accepted_at"] is None
    assert receipt["error"]
    assert original["state"] == "unread"
    assert failed_inbox["unread_count"] == 1
    assert len(native_attempts(box)) == 1
    assert PRIVATE_ERROR not in json.dumps(failed_report)
    assert PRIVATE_ERROR not in json.dumps(failed_inbox)
    box.desktop_fail.unlink()
    box.check(notify=True, offset=2)
    recovered = only_alert(inbox(box))
    assert recovered["id"] == original["id"]
    assert recovered["transports"]["desktop"]["accepted_at"] is not None
    assert not recovered["transports"]["desktop"]["error"]
    assert recovered["state"] == "unread"
    assert recovered["acknowledged_at"] is None
    assert len(native_attempts(box)) == 2


def test_desktop_off_respected_and_root_mail_requires_separate_opt_in(box):
    """N2/N4: desktop off does not defeat deliberate independent root-mail consent.

    Mutation: force desktop on, disable all transports, or equate mail with reading.
    Default mail-off is separately exercised by the default-submission case.
    """
    box.configure(desktop_enabled=False, mail_enabled=True)
    sustain(box)
    assert native_attempts(box) == []
    assert len(box.mail()) == 1
    alert = only_alert(inbox(box))
    assert alert["transports"]["desktop"]["accepted_at"] is None
    assert alert["transports"]["mail"]["accepted_at"] is not None
    assert alert["state"] == "unread"
    assert alert["acknowledged_at"] is None


def test_notification_payload_is_private_bounded_and_not_executable_code(box):
    """N2/N3: closed issue projection, private files, bounded payload, literal argv.

    Mutation: include raw prompt/error/credentials, use unsafe file modes, embed
    hostile identity in AppleScript source, or invoke the command via unquoted shell.
    """
    secret = "SYNTHETIC_PRIVATE_NOTIFICATION_CONTENT_932"
    marker = box.root / "DESKTOP_INJECTION_CANARY"
    box.scope["session_id"] = '\"; do shell script \"touch ' + str(marker) + '\" --'
    box.seed_active()
    for n in range(3):
        box.record("review", -30 + n, state="failed", prompt=secret * 4000,
                   provider_error=secret, credentials=secret, api_key=secret)
    box.check(notify=True)
    report = box.check(notify=True, offset=1)
    assert issues(report, "review_failures")
    attempts = native_attempts(box)
    assert len(attempts) == 1
    archive = inbox(box)
    alert = only_alert(archive)
    assert secret not in json.dumps(report)
    assert secret not in json.dumps(archive)
    assert secret not in json.dumps(attempts)
    programs = [attempts[0]["argv"][i + 1] for i, arg in enumerate(attempts[0]["argv"][:-1]) if arg == "-e"]
    assert programs  # Ratified osascript-shaped fixed-program plus data-argv boundary.
    assert all("DESKTOP_INJECTION_CANARY" not in program for program in programs)
    assert not marker.exists()
    projection = {key: alert[key] for key in ("id", "issue_id", "code", "scope", "reason", "evidence", "diagnostic")}
    assert len(json.dumps(projection).encode()) <= 16 * 1024
    state_files = [path for path in box.state.rglob("*") if path.is_file()]
    assert state_files
    for path in state_files:
        assert secret.encode() not in path.read_bytes()
        assert path.stat().st_mode & 0o077 == 0, str(path)


def test_install_defaults_and_explicit_off_preserve_config_inbox_and_plist(box):
    """N2/N3/N4: default desktop schedules notify; both-off schedules checks only.

    Mutation: require root mail for scheduling, ignore off, or overwrite history /
    config / existing LaunchAgent during preview.
    """
    sustain(box, notify=False)
    original = only_alert(inbox(box))
    acknowledge(box, original["id"])
    config_path = box.state / "config.json"
    config_before = config_path.read_bytes()
    plist = box.home / "Library/LaunchAgents/com.kindex.supervisor-health.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "com.kindex.supervisor-health", "ProgramArguments": ["/usr/bin/true"]}))
    plist_before = plist.read_bytes()
    default_plan = public_cli(box, "install", "--dry-run")
    assert "--notify" in default_plan["command"]
    assert config_path.read_bytes() == config_before
    assert plist.read_bytes() == plist_before
    box.configure(desktop_enabled=False, mail_enabled=False)
    off_config = config_path.read_bytes()
    off_plan = public_cli(box, "install", "--dry-run")
    assert "--notify" not in off_plan["command"]
    assert config_path.read_bytes() == off_config
    assert plist.read_bytes() == plist_before
    retained = only_alert(inbox(box))
    assert retained["id"] == original["id"]
    assert retained["state"] == "acknowledged"
    assert native_attempts(box) == []
    assert box.mail() == []
