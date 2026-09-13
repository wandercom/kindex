"""Independent mail_enabled gate: monitoring may run without mail transport.

Oracle: user-ratified separate mail gate and parent-declared public installer plan.
No source inspection or test execution by the Tester.
"""
import json
import plistlib
import subprocess
import sys

import pytest

from test_health import b, issues


def install_plan(b):
    result = subprocess.run([sys.executable, "-m", "kindex.supervisor_health",
                             "install", "--dry-run", "--json"],
                            text=True, capture_output=True, env=b.env)
    assert result.returncode == 0, result.stderr  # Public dry-run installer API.
    return json.loads(result.stdout)


@pytest.mark.parametrize("mail_setting", ["omitted", False, True])
def test_health_mail_gate_is_separate_from_monitoring_and_defaults_off(b, mail_setting):
    """Ratified mail gate: absent/false prevents transport while issues still record.

    Explicit true is a positive countercheck, and installer commands reflect the
    same gate. Mutation: equate monitor enabled with mail consent, ignore false,
    hide issues when mail is off, or suppress mail even after explicit opt-in.
    """
    if mail_setting == "omitted":
        del b.config["mail_enabled"]
        b.configure()
    else:
        b.configure(mail_enabled=mail_setting)
    expected_mail = mail_setting is True
    b.failures()
    first = b.check(notify=True)
    second = b.check(notify=True, offset=1)
    status = b.command("status")
    assert first["enabled"] is True
    assert second["enabled"] is True
    assert issues(first, "review_failures")
    failure = issues(second, "review_failures")[0]
    assert first["mail_enabled"] is expected_mail
    assert second["mail_enabled"] is expected_mail
    assert status["mail_enabled"] is expected_mail
    assert len(b.mail()) == (1 if expected_mail else 0)
    assert (failure["last_notified"] is not None) is expected_mail
    plan = install_plan(b)
    assert plan["mail_enabled"] is expected_mail
    assert ("--notify" in plan["command"]) is expected_mail


def test_health_install_preview_preserves_explicit_mail_off_config_and_existing_plist(b):
    """Ratified install/reinstall plan: explicit mail-off survives without writes.

    Mutation: enable mail on reinstall, add --notify anyway, or write during preview.
    """
    b.configure(mail_enabled=False)
    config_path = b.state / "config.json"
    config_before = config_path.read_bytes()
    plist_path = b.home / "Library/LaunchAgents/com.kindex.supervisor-health.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps({
        "Label": "com.kindex.supervisor-health",
        "ProgramArguments": ["/usr/bin/true"],
        "RunAtLoad": False,
    }))
    plist_before = plist_path.read_bytes()
    plan = install_plan(b)
    assert plan["mail_enabled"] is False
    assert "--notify" not in plan["command"]
    assert config_path.read_bytes() == config_before
    assert plist_path.read_bytes() == plist_before
    assert b.mail() == []
