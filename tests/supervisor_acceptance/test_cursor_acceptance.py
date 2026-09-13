"""Independent Cursor extension oracle; existing four-host tests are untouched.

Requirements: Validator-ratified Cursor transport/installer dispatch and public
https://cursor.com/docs/hooks (common schema and hook-specific response fields).
No implementation source was consulted and no tests run by the Tester.
"""
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from test_supervisor_acceptance import NOTE, WORK, sandbox


EVENTS = {"sessionStart", "beforeSubmitPrompt", "postToolUse", "afterAgentResponse", "stop", "afterMCPExecution"}


@pytest.fixture
def cursor(sandbox):
    # Borrowed fixture already allowlists its environment; add isolated host state.
    sandbox.env.update(CODEX_HOME=str(sandbox.home / ".codex"),
                       CLAUDE_CONFIG_DIR=str(sandbox.home / ".claude"),
                       CURSOR_CONFIG_DIR=str(sandbox.home / ".cursor"),
                       XDG_DATA_HOME=str(sandbox.home / ".local/share"),
                       KIN_HEALTH_DIR=str(sandbox.root / "health"))
    sandbox.configure(mode="flagged")
    return sandbox


def native_payload(cursor, event, **extra):
    return {"conversation_id": "cursor-independent-session",
            "workspace_roots": [str(cursor.project)],
            "generation_id": "generation-1", "hook_event_name": event, **extra}


def invoke_native(cursor, payload, *, diagnostic=False):
    args = ["supervisor-hook", "--adapter", "cursor", "--config", str(cursor.config)]
    if diagnostic:
        args.append("--json")
    result = cursor.cli(*args, payload=payload)
    if not diagnostic:
        assert result.returncode == 0, result.stderr  # Shared Cursor native CLI contract.
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)  # Shared Cursor output is native JSON, not prose.
    return parsed


def status(cursor):
    result = cursor.cli("sim", "status", "--project-path", str(cursor.project),
                        "--config", str(cursor.config), "--json")
    assert result.returncode == 0, result.stderr  # Shared non-consuming status API.
    return json.loads(result.stdout)


def ready_advice(cursor):
    initial = invoke_native(cursor, native_payload(cursor, "beforeSubmitPrompt",
                            event_id="prime-event", prompt=WORK))
    assert NOTE not in json.dumps(initial)  # Observer-only event cannot deliver advice.
    ready = cursor.until(lambda: status(cursor),
                         lambda report: report["pending"] >= 1 and any(
                             row.get("reason") == "review_complete_pending_delivery"
                             for row in report["sessions"]),
                         "public non-consuming ready-advice signal")
    assert ready["pending"] >= 1
    assert cursor.requests()  # Reachability: actual synthetic provider review happened.
    # Future provider calls cannot recreate NOTE and disguise destructive consumption.
    cursor.configure(mode="failure")


def delivery_payload(cursor, event, **extra):
    if event == "postToolUse":
        extra = {"tool_name": "Shell", "tool_use_id": "synthetic-tool-1",
                 "tool_input": {"command": "synthetic-validation"},
                 "tool_output": json.dumps({"exitCode": 1, "stdout": "parser_negative_case failed"}),
                 **extra}
    elif event == "stop":
        extra = {"status": "completed", "loop_count": 0, **extra}
    elif event == "afterMCPExecution":
        extra = {"mcp_server_name": "unrelated-synthetic-server", "tool_name": "search", "result_json": {}, **extra}
    return native_payload(cursor, event, event_id="delivery-event", **extra)


@pytest.mark.parametrize("event,field", [("sessionStart", "additional_context"),
                                        ("postToolUse", "additional_context"),
                                        ("stop", "followup_message")])
def test_cursor_ready_advice_uses_the_native_delivery_slot(cursor, event, field):
    """Ratified Cursor transport: ready advice enters each host-supported slot.

    Mutation: emit only diagnostic context, use another host's field, or drop advice.
    """
    ready_advice(cursor)
    response = invoke_native(cursor, delivery_payload(cursor, event))
    assert NOTE in response.get(field, "")
    other = "additional_context" if field == "followup_message" else "followup_message"
    assert not response.get(other)


@pytest.mark.parametrize("event", ["beforeSubmitPrompt", "afterAgentResponse"])
def test_cursor_observer_events_do_not_consume_ready_advice(cursor, event):
    """Ratified Cursor guard: observing is not an advice-consuming delivery.

    Mutation: drain ready queue while generating an ignored observer response.
    """
    ready_advice(cursor)
    extra = {"prompt": WORK} if event == "beforeSubmitPrompt" else {"text": WORK}
    observed = invoke_native(cursor, native_payload(cursor, event, event_id="observer-event", **extra))
    assert not observed.get("additional_context")
    assert not observed.get("followup_message")
    assert NOTE not in json.dumps(observed)
    assert status(cursor)["pending"] >= 1
    delivered = invoke_native(cursor, delivery_payload(cursor, "postToolUse"))
    assert NOTE in delivered.get("additional_context", "")


@pytest.mark.parametrize("completion,loop_count", [("completed", 1), ("aborted", 0),
                                                    ("cancelled", 0), ("error", 0)])
def test_cursor_stop_guards_preserve_advice_without_an_auto_loop(cursor, completion, loop_count):
    """Ratified stop guard: only completed first stop may auto-submit advice.

    Mutation: return followup or drain advice on continuation/cancel/error.
    """
    ready_advice(cursor)
    blocked = invoke_native(cursor, delivery_payload(cursor, "stop", status=completion, loop_count=loop_count))
    assert not blocked.get("followup_message")
    assert NOTE not in json.dumps(blocked)
    assert status(cursor)["pending"] >= 1
    delivered = invoke_native(cursor, delivery_payload(cursor, "postToolUse"))
    assert NOTE in delivered.get("additional_context", "")


def test_cursor_stop_without_ready_advice_does_not_fabricate_followup(cursor):
    """Ratified stop transport: no advice means no automatic continuation.

    Mutation: return generic proactive followup even when supervisor is disabled.
    """
    cursor.configure(enabled=False)
    response = invoke_native(cursor, delivery_payload(cursor, "stop"))
    assert not response.get("followup_message")
    assert cursor.requests() == []


@pytest.mark.parametrize("defect", ["missing_workspace", "empty_workspace", "ambiguous_workspace", "missing_session"])
def test_cursor_refuses_missing_or_ambiguous_native_identity(cursor, defect):
    """Ratified identity: no cwd/environment fallback for incomplete native scope.

    Mutation: select cwd/KIN_PROJECT or create a synthetic session identity.
    """
    payload = native_payload(cursor, "beforeSubmitPrompt", prompt=WORK, cwd=str(cursor.project))
    if defect == "missing_workspace":
        del payload["workspace_roots"]
    elif defect == "empty_workspace":
        payload["workspace_roots"] = []
    elif defect == "ambiguous_workspace":
        other = cursor.root / "another-project"
        other.mkdir()
        payload["workspace_roots"].append(str(other))
    else:
        del payload["conversation_id"]
    response = invoke_native(cursor, payload, diagnostic=True)
    assert response["ok"] is False
    assert response.get("error") or response.get("supervisor", {}).get("reason")
    assert cursor.requests() == []


def write_existing_hooks(cursor, raw=None):
    path = cursor.home / ".cursor/hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"version": 1, "hooks": {
        "beforeSubmitPrompt": [{"command": "/usr/bin/true", "timeout": 8}],
        "afterFileEdit": [{"command": "/usr/bin/true"}],
    }}
    path.write_bytes(raw if raw is not None else json.dumps(original, indent=2).encode())
    return path, original


def test_cursor_installer_dryrun_idempotence_preservation_and_uninstall(cursor):
    """Cursor installer contract: preserve unrelated hooks through full round-trip.

    Mutation: rewrite on dry-run, omit a native event, duplicate installation,
    replace unrelated commands, or delete unrelated commands on uninstall.
    """
    path, original = write_existing_hooks(cursor)
    before = path.read_bytes()
    cursor_files_before = {str(file.relative_to(path.parent)): file.read_bytes()
                           for file in path.parent.rglob("*") if file.is_file()}
    preview = cursor.cli("setup-cursor-hooks", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert path.read_bytes() == before
    assert {str(file.relative_to(path.parent)): file.read_bytes()
            for file in path.parent.rglob("*") if file.is_file()} == cursor_files_before
    install = cursor.cli("setup-cursor-hooks")
    assert install.returncode == 0, install.stderr
    installed = json.loads(path.read_text())
    assert installed["version"] == 1
    for event in EVENTS:
        previous = original["hooks"].get(event, [])
        assert len(installed["hooks"][event]) == len(previous) + 1
        assert all(entry in installed["hooks"][event] for entry in previous)
        new_entries = [entry for entry in installed["hooks"][event] if entry not in previous]
        assert isinstance(new_entries[0]["command"], str)
        assert new_entries[0]["command"].strip()
    assert installed["hooks"]["afterFileEdit"] == original["hooks"]["afterFileEdit"]
    # Exercise the installed native command from Cursor's documented user-hook cwd.
    # Empty isolated profiles make the authorized bash wrapper deterministic.
    (cursor.home / ".profile").write_text("")
    (cursor.home / ".bash_profile").write_text("")
    cursor.configure(enabled=False)
    for event in EVENTS:
        previous = original["hooks"].get(event, [])
        entry = next(entry for entry in installed["hooks"][event] if entry not in previous)
        argv = shlex.split(entry["command"])
        assert Path(argv[0]).name == "bash"
        assert "-lc" in argv
        wrapped = shlex.split(argv[argv.index("-lc") + 1])
        assert "supervisor-hook" in wrapped
        assert wrapped[wrapped.index("--adapter") + 1] == "cursor"
        invoked = subprocess.run(argv, input=json.dumps(delivery_payload(cursor, event)),
                                 text=True, capture_output=True, env=cursor.env,
                                 cwd=path.parent, timeout=25)
        assert invoked.returncode == 0, invoked.stderr
        native = json.loads(invoked.stdout)
        assert isinstance(native, dict)
        assert not native.get("followup_message")
    repeated = cursor.cli("setup-cursor-hooks")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(path.read_text()) == installed
    uninstall = cursor.cli("setup-cursor-hooks", "--uninstall")
    assert uninstall.returncode == 0, uninstall.stderr
    restored = json.loads(path.read_text())
    assert restored["version"] == 1
    assert restored["hooks"]["beforeSubmitPrompt"] == original["hooks"]["beforeSubmitPrompt"]
    assert restored["hooks"]["afterFileEdit"] == original["hooks"]["afterFileEdit"]
    for event in EVENTS - {"beforeSubmitPrompt"}:
        assert restored["hooks"].get(event, []) == []


@pytest.mark.parametrize("raw", [b'{"version":1,"hooks":', b'{"version":9,"hooks":{}}'])
def test_cursor_installer_refuses_malformed_or_unknown_version_without_mutation(cursor, raw):
    """Cursor installer contract: unsafe config is diagnosed and preserved.

    Mutation: silently repair malformed JSON, overwrite unknown schema, or edit
    config before refusing an operation.
    """
    path, _ = write_existing_hooks(cursor, raw)
    for flags in ((), ("--dry-run",), ("--uninstall",)):
        result = cursor.cli("setup-cursor-hooks", *flags)
        assert result.returncode != 0
        assert (result.stdout + result.stderr).strip()
        assert path.read_bytes() == raw
