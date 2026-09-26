"""Independent popup contract tests through the native transport boundary.

Oracle: the user's request, "I need to know which session is breaking the rules
in that popup. I can't click on them or otherwise get more info", and the
Validator's delegated contract:
P1 agent, recognizable session title when available, short session ID, compact
   project location, and a plain-English issue appear directly in the popup;
P2 exact Claude custom-title records only, matching sessionId, configurable or
   default transcript root, bounded lookup. Validator freshness clarification:
   short files permit titles anywhere; long files permit only the latest tail
   title, because an unread middle may contain a rename after a head title;
P3 missing/malformed/mismatched/unsafe/symlink metadata fails soft; no prompt or
   summary titles; other hosts use project plus ID; null scope is honest;
P4 long/control text is bounded and leaves the ID visible; dynamic data stays
   in argv, never AppleScript source; no alert UUID or CLI boilerplate.

No implementation source was inspected and no tests were run by the author.
Rendering cases are red-now targets. The static-program transport assertion is
a green-now guard. Each docstring names a falsifying mutation.
Only subprocess.run is mocked: it records the actual transport arguments and
returns an explicit successful OS submission (which does not prove visibility).
HOME/config roots are synthetic; no real session files or network are used.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kindex.supervisor_notifications import submit_desktop


SESSION_ID = "c41ab829-154b-4a23-98a1-7e73600499de"
ALERT_ID = "98aebbad-765a-4dc9-a5cf-a910be537baa"


@pytest.fixture
def popup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    project = home / "work" / "billing_service.v2"
    project.mkdir(parents=True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    return SimpleNamespace(home=home, project=project, calls=calls)


def alert(popup, *, agent="claude", session_id=SESSION_ID, project=None):
    return {
        "id": ALERT_ID,
        "code": "missing_use",
        "scope": {
            "agent": agent,
            "project_path": str(project or popup.project),
            "session_id": session_id,
        },
        "reason": "No Kindex use was recorded for this active session.",
        "evidence": {},
    }


def transcript(popup, *, root=None, session_id=SESSION_ID):
    # P2 supplies the native path contract; this does not call product helpers.
    key = re.sub(r"[^a-zA-Z0-9]", "-", str(popup.project))
    path = (root or popup.home / ".claude") / "projects" / key / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_rows(path, *rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def title_row(title, session_id=SESSION_ID):
    return {"type": "custom-title", "customTitle": title, "sessionId": session_id}


def render(popup, item):
    before = len(popup.calls)
    result = submit_desktop(item, {"desktop_command": "/synthetic/osascript"})
    assert result["accepted"] is True  # Reach the real rendering/transport path.
    assert len(popup.calls) == before + 1
    argv, options = popup.calls[-1]
    assert argv[:2] == ["/synthetic/osascript", "-e"]
    assert argv[3] == "--"
    assert len(argv) == 6
    assert options.get("shell", False) is False
    return "\n".join(argv[4:]), argv[2]


def assert_identity(text, popup, *, agent="claude", session_id=SESSION_ID):
    # P1: independent identity facts, no exact sentence or truncation policy.
    assert agent.casefold() in text.casefold()
    assert session_id[:8] in text
    assert popup.project.name in text
    assert str(popup.home) not in text  # Compact location, not a full home path.
    assert ALERT_ID not in text
    assert "supervisor_health" not in text
    assert "--json" not in text


def test_popup_names_the_exact_claude_session_and_explains_issue(popup):
    """P1/P2 red-now: omit title/ID or render only a machine issue code."""
    write_rows(transcript(popup), title_row("Repair billing retry rules"))
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Repair billing retry rules" in text
    assert "missing_use" not in text
    assert "kindex" in text.casefold()
    assert re.search(r"\b(no|not|missing|without|unrecorded)\b", text, re.I)
    assert re.search(r"use|activity|search|context|retriev", text, re.I)


@pytest.mark.parametrize("position", ["head", "tail"])
def test_long_custom_root_transcript_only_uses_tail_title(popup, monkeypatch, position):
    """P2 freshness: ignore config root, miss a tail title, or trust a head title."""
    custom_root = popup.home / "claude-custom"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_root))
    write_rows(transcript(popup), title_row("Wrong default-root session"))
    chosen = json.dumps(title_row("Investigate invoice rounding")) + "\n"
    # Large irrelevant middle prevents a small head-only scan finding a tail title.
    middle = json.dumps({"type": "progress", "data": "x" * (2 * 1024 * 1024)}) + "\n"
    path = transcript(popup, root=custom_root)
    path.write_text(chosen + middle if position == "head" else middle + chosen)
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    if position == "tail":
        assert "Investigate invoice rounding" in text
    else:
        assert "Investigate invoice rounding" not in text
    assert "Wrong default-root session" not in text


def test_unread_middle_rename_does_not_expose_stale_head_title(popup):
    """P2 freshness red-now: display a head title superseded in unread middle."""
    old = json.dumps(title_row("Old checkout investigation")) + "\n"
    rename = json.dumps(title_row("Renamed invoice repair")) + "\n"
    padding = json.dumps({"type": "progress", "data": "x" * (2 * 1024 * 1024)}) + "\n"
    transcript(popup).write_text(old + padding + rename + padding)
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Old checkout investigation" not in text
    assert "Renamed invoice repair" not in text  # No eligible tail title: fallback.


@pytest.mark.parametrize("position", ["head", "middle", "tail"])
def test_short_transcript_title_is_eligible_anywhere(popup, position):
    """P2 freshness guard: apply long-file head rejection to a fully read file."""
    progress = {"type": "progress", "data": "small synthetic event"}
    rows = [progress, progress]
    rows.insert({"head": 0, "middle": 1, "tail": 2}[position],
                title_row("Short session invoice repair"))
    write_rows(transcript(popup), *rows)
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Short session invoice repair" in text


def test_latest_matching_tail_rename_wins(popup):
    """P2 freshness guard: select the first title in the tail rather than latest."""
    padding = {"type": "progress", "data": "x" * (2 * 1024 * 1024)}
    write_rows(transcript(popup), padding,
               title_row("Previous tail title"),
               title_row("Current invoice repair"),
               title_row("Foreign final title", "other-session"))
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Current invoice repair" in text
    assert "Previous tail title" not in text
    assert "Foreign final title" not in text


@pytest.mark.parametrize("metadata", ["missing", "malformed", "wrong_session", "wrong_type", "prompts_only"])
def test_unusable_metadata_falls_back_without_borrowing_content(popup, metadata):
    """P3 red-now: fail submission, borrow a foreign title, or expose a prompt."""
    path = transcript(popup)
    forbidden = "PRIVATE CONTENT MUST NOT BECOME A TITLE"
    if metadata == "malformed":
        path.write_text('{"type":"custom-title","customTitle":"' + forbidden)
    elif metadata == "wrong_session":
        write_rows(path, title_row(forbidden, "7d38f26e-other-session"))
    elif metadata == "wrong_type":
        write_rows(path, {"type": "user", "customTitle": forbidden, "sessionId": SESSION_ID})
    elif metadata == "prompts_only":
        write_rows(path,
                   {"type": "user", "sessionId": SESSION_ID,
                    "message": {"role": "user", "content": forbidden}},
                   {"type": "summary", "sessionId": SESSION_ID, "summary": forbidden})
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert forbidden not in text


def test_nearby_transcript_cannot_supply_a_title(popup):
    """P2/P3 red-now: search neighboring sessions instead of the exact filename."""
    write_rows(transcript(popup, session_id="different-session"),
               title_row("Wrong neighboring session"))
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Wrong neighboring session" not in text


@pytest.mark.parametrize("link_part", ["file", "project_directory"])
def test_symlink_metadata_is_not_used_for_session_identity(popup, link_part):
    """P3 red-now: follow a symlink to a title outside the exact native location."""
    path = transcript(popup)
    outside = popup.home / "untrusted-transcripts"
    outside.mkdir()
    target = outside / path.name
    write_rows(target, title_row("Untrusted linked session"))
    if link_part == "file":
        path.symlink_to(target)
    else:
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    text, _ = render(popup, alert(popup))
    assert_identity(text, popup)
    assert "Untrusted linked session" not in text


def test_unsafe_session_path_cannot_escape_project_metadata(popup):
    """P3 red-now: use an unchecked session ID as a relative filesystem path."""
    path = transcript(popup)
    sid = "../escaped-session"
    write_rows(path.parent.parent / "escaped-session.jsonl",
               title_row("Escaped transcript content", sid))
    text, _ = render(popup, alert(popup, session_id=sid))
    assert popup.project.name in text
    assert "claude" in text.casefold()
    assert "Escaped transcript content" not in text


@pytest.mark.parametrize("agent", ["codex", "cursor", "antigravity"])
def test_other_hosts_identify_project_and_session_without_claude_title(popup, agent):
    """P1/P3 red-now: omit other hosts' session ID or use a Claude-only title."""
    write_rows(transcript(popup), title_row("Claude-only title"))
    text, _ = render(popup, alert(popup, agent=agent))
    assert_identity(text, popup, agent=agent)
    assert "Claude-only title" not in text


def test_agent_wide_issue_does_not_invent_a_session(popup):
    """P3 red-now: stringify None or imply a known session on an agent-wide issue."""
    item = alert(popup)
    item["scope"] = {"agent": "claude", "project_path": None, "session_id": None}
    text, _ = render(popup, item)
    assert "claude" in text.casefold()
    assert re.search(r"(?:agent|host|all)[ -]wide|session.{0,20}(?:unavailable|unknown|not available)|no.{0,10}session", text, re.I)
    assert "None" not in text
    assert ALERT_ID not in text
    assert SESSION_ID[:8] not in text


def test_long_control_text_keeps_useful_identity_within_popup(popup):
    """P1/P4 red-now: truncate away ID/location or pass control/huge text through."""
    title = "Invoice investigation " + "long detail " * 1000 + "\x00\x1b\r\t\n"
    write_rows(transcript(popup), title_row(title))
    item = alert(popup)
    item["reason"] = "No Kindex use " + "verbose reason " * 1000 + "\x00\x1b"
    text, _ = render(popup, item)
    assert_identity(text, popup)
    assert "Invoice investigation" in text
    assert len(text) < 1000  # Loose usability ceiling, not an implementation limit.
    assert not any(ord(char) < 32 and char != "\n" for char in text)


def test_untrusted_title_remains_literal_data_in_fixed_program(popup):
    """P4 green-now transport guard: interpolate a dynamic title into AppleScript."""
    _, ordinary_program = render(popup, alert(popup))
    title = 'Invoice " & do shell script "POPUP_INJECTION_CANARY" & "'
    write_rows(transcript(popup), title_row(title))
    text, hostile_program = render(popup, alert(popup))
    assert hostile_program == ordinary_program
    assert "POPUP_INJECTION_CANARY" not in hostile_program
    # P1 red-now companion assertion proves the hostile title reaches the boundary.
    assert "Invoice" in text
    assert SESSION_ID[:8] in text
