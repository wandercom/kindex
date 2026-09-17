"""Action execution for actionable reminders.

Reminders can optionally carry a shell command and/or natural-language
instructions.  When due, the daemon (or manual ``kin remind exec``) runs:

* **shell** — ``subprocess.run(command, shell=True)``
* **claude** — ``claude -p <prompt>`` with assembled context
* **codex** — ``codex exec`` (optionally ``resume``) with assembled context
* **opencode** — ``opencode run`` (optionally resuming a session) with context
* **auto** (default) — shell if only command, claude if instructions present

Action metadata lives in the reminder's ``extra`` JSON field (no schema
migration required).
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .privacy import redact_text, safe_error

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


# ── Field helpers ──────────────────────────────────────────────────


def get_action_fields(reminder: dict) -> dict:
    """Extract action fields from a reminder's extra dict.

    Returns a dict with normalised keys; missing keys get safe defaults.
    """
    extra = reminder.get("extra") or {}
    return {
        "action_command": extra.get("action_command", ""),
        "action_instructions": extra.get("action_instructions", ""),
        "action_mode": extra.get("action_mode", "auto"),
        "action_status": extra.get("action_status", "pending"),
        "action_result": extra.get("action_result", ""),
        "wake_client": extra.get("wake_client", ""),
        "wake_session_id": extra.get("wake_session_id", ""),
        "wake_cwd": extra.get("wake_cwd", ""),
        "wake_model": extra.get("wake_model", ""),
        "wake_agent": extra.get("wake_agent", ""),
    }


def has_action(reminder: dict) -> bool:
    """True if the reminder has any action defined (command or instructions)."""
    fields = get_action_fields(reminder)
    return bool(
        fields["action_command"]
        or fields["action_instructions"]
        or fields["wake_client"]
    )


def resolve_mode(fields: dict) -> str:
    """Resolve ``auto`` mode into ``shell`` or ``claude``.

    auto = shell when only a command is present, claude when instructions exist.
    """
    mode = fields.get("action_mode", "auto")
    if mode != "auto":
        return mode
    if fields.get("wake_client"):
        return fields["wake_client"]
    if fields.get("action_instructions"):
        return "claude"
    return "shell"


# ── Execution ──────────────────────────────────────────────────────


# A failing action is retried on later sweeps, but not forever: each
# occurrence gets this many attempts before it is set aside as exhausted.
MAX_ACTION_ATTEMPTS = 3


def execute_action(
    store: Store,
    reminder: dict,
    config: Config,
    *,
    timeout: int = 300,
    manual: bool = False,
) -> dict:
    """Execute a reminder's action.  Returns ``{"status": ..., "output": ...}``.

    Updates the reminder's ``extra`` with ``action_status`` and ``action_result``.
    ``manual=True`` marks a deliberate user invocation (``kin remind exec`` /
    MCP ``remind_exec``): it may resume a ``paused`` or ``exhausted`` action,
    which automated sweeps must skip.

    The action is claimed from the stored row, not from ``reminder``: a sweep
    passes a snapshot that can be minutes old, and a run finished meanwhile
    (a manual exec, another sweep) must not run again.
    """
    if not has_action(reminder):
        return {"status": "skipped", "reason": "no action defined"}
    claimed = _claim_action(store, reminder["id"], manual=manual, timeout=timeout)
    if "skipped" in claimed:
        return {"status": "skipped", "reason": claimed["skipped"]}
    reminder = claimed["reminder"]
    fields = get_action_fields(reminder)
    mode = resolve_mode(fields)
    rid = reminder["id"]

    try:
        if mode == "shell":
            result = _run_shell(fields["action_command"], timeout=timeout)
        elif mode == "claude":
            result = _run_claude(reminder, fields, config, store, timeout=timeout)
        elif mode == "codex":
            result = _run_codex(reminder, fields, store, timeout=timeout)
        elif mode == "opencode":
            result = _run_opencode(reminder, fields, store, timeout=timeout)
        else:
            result = {"ok": False, "output": f"Unknown mode: {mode}", "terminal": True}
    except Exception as e:
        result = {"ok": False, "output": safe_error(e)}

    if result["ok"]:
        status = "completed"
    elif result.get("terminal") or claimed["attempts"] >= MAX_ACTION_ATTEMPTS:
        # Retrying cannot help (a spend or turn limit, an unknown mode) or
        # has not: stop until this occurrence ends or someone runs it by hand.
        status = "exhausted"
    else:
        status = "failed"
    try:
        _update_action_status(store, rid, reminder, status, result["output"])
    except Exception as e:
        return {"status": "failed", "output": safe_error(e)}
    return {"status": status, "output": result["output"]}


def _claim_action(store: Store, reminder_id: str, *, manual: bool, timeout: int) -> dict:
    """Mark the stored action running if it may run now, in one write.

    Returns ``{"reminder": fresh_row, "attempts": n}`` when claimed, or
    ``{"skipped": reason}``. Attempts count per occurrence (``next_due``).
    """
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None:
            conn.rollback()
            return {"skipped": "reminder no longer exists"}
        fresh = store._reminder_to_dict(row)
        if fresh.get("status") in ("cancelled", "completed"):
            conn.rollback()
            return {"skipped": f"reminder is {fresh['status']}"}
        status = get_action_fields(fresh)["action_status"]
        if status == "completed":
            conn.rollback()
            return {"skipped": "already completed"}
        if status in ("paused", "exhausted") and not manual:
            conn.rollback()
            if status == "paused":
                return {"skipped": "paused (stale); run kin remind exec to resume"}
            return {"skipped": "attempts exhausted for this occurrence; run kin remind exec to retry"}
        if status == "running" and not _running_is_stale(fresh, timeout):
            conn.rollback()
            return {"skipped": "already running"}
        extra = dict(fresh.get("extra") or {})
        occurrence = fresh.get("next_due") or ""
        if extra.get("action_attempt_occurrence") != occurrence or manual:
            extra["action_attempts"] = 0
            extra["action_attempt_occurrence"] = occurrence
        if int(extra.get("action_attempts") or 0) >= MAX_ACTION_ATTEMPTS:
            # A worker that died mid-run (a stale "running") used its
            # attempt; a sweep does not start one past the cap.
            extra["action_status"] = "exhausted"
            store.update_reminder(reminder_id, extra=extra)  # commits
            return {"skipped": "attempts exhausted for this occurrence; "
                               "run kin remind exec to retry"}
        extra["action_attempts"] = int(extra.get("action_attempts") or 0) + 1
        extra["action_status"] = "running"
        extra["action_executed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        store.update_reminder(reminder_id, extra=extra)  # commits the claim
    except BaseException:
        conn.rollback()
        raise
    fresh["extra"] = extra
    return {"reminder": fresh, "attempts": extra["action_attempts"]}


# ── Internal helpers ───────────────────────────────────────────────


def _running_is_stale(reminder: dict, timeout: int) -> bool:
    """True when a ``running`` action_status is a leftover from a dead run.

    A cron process killed mid-action (launchd unload, crash, hang) leaves the
    reminder stuck at ``running`` and, since running actions are skipped, no
    occurrence would ever execute again. Any well-behaved run finishes within
    its subprocess ``timeout``, so a running marker older than twice that (plus
    slack) cannot belong to a live run and is safe to reclaim. A missing or
    unparseable ``action_executed_at`` is also reclaimed — there is no evidence
    of a live run to protect.
    """
    stamp = (reminder.get("extra") or {}).get("action_executed_at", "")
    try:
        executed = datetime.datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return True
    age = (datetime.datetime.now() - executed).total_seconds()
    return age > (2 * timeout + 60)


def _update_action_status(
    store: Store, rid: str, reminder: dict, status: str, result: str,
) -> None:
    """Write ``action_status`` and ``action_result`` into the reminder's extra."""
    store.update_reminder_action(
        rid, status=status, result=redact_text(result)[:4000],
        executed_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )


def _run_process(
    cmd,
    *,
    shell: bool = False,
    input_text: str | None = None,
    timeout: int = 300,
    grace: float = 2.0,
    max_bytes: int | None = None,
) -> tuple[int | None, str, str]:
    """Run ``cmd`` and return ``(returncode, stdout, stderr)``; the return
    code is None when the run timed out and its process group was killed.

    Output is decoded leniently: an undecodable byte is not a failed run.
    The run ends when the command exits, not when every descendant has
    closed its pipes, so a command that starts a background service
    finishes; output that follows within ``grace`` seconds is kept.
    With ``max_bytes`` each stream keeps only its first ``max_bytes`` (the
    rest is still drained, so the child never blocks on a full pipe).
    """
    import selectors
    import signal

    proc = subprocess.Popen(
        cmd, shell=shell, start_new_session=True,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    def kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    chunks: dict = {proc.stdout: [], proc.stderr: []}
    held: dict = {proc.stdout: 0, proc.stderr: 0}
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        selector = selectors.DefaultSelector()
        for stream in chunks:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        # The prompt is fed as the child reads it, alongside its output:
        # writing it all first deadlocks a child that fills its output pipe
        # before reading its input.
        pending = memoryview(input_text.encode("utf-8", errors="replace")) if input_text is not None else None
        if pending is not None:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE)
        exited_at = None
        while selector.get_map():
            now = time.monotonic()
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            if exited_at is not None and now - exited_at > grace:
                break  # a descendant still holds the pipes
            if now >= deadline:
                timed_out = exited_at is None
                break
            for key, _ in selector.select(timeout=min(0.1, max(0.0, deadline - now))):
                if key.fileobj is proc.stdin:
                    try:
                        pending = pending[os.write(proc.stdin.fileno(), pending[:65536]):]
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if data:
                    room = None if max_bytes is None else max_bytes - held[key.fileobj]
                    if room is None or room > 0:
                        kept = data if room is None else data[:room]
                        chunks[key.fileobj].append(kept)
                        held[key.fileobj] += len(kept)
                else:
                    selector.unregister(key.fileobj)
        selector.close()
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if not timed_out and proc.poll() is None:
            # The command closed its output but is still running: the
            # deadline still applies.
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    except BaseException:
        # Interrupted (Ctrl-C) or failed: the detached group must not
        # outlive the runner.
        kill_group()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        for stream in chunks:
            stream.close()
    if timed_out:
        kill_group()
    try:
        returncode = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        returncode = None
    decode = lambda parts: b"".join(parts).decode("utf-8", errors="replace")  # noqa: E731
    return (None if timed_out else returncode,
            decode(chunks[proc.stdout]), decode(chunks[proc.stderr]))


def _resolve_cli(name: str) -> str | None:
    """An agent CLI's absolute path. A scheduler starts jobs with a bare
    system PATH, so the usual install locations are searched as well."""
    found = shutil.which(name)
    if found:
        return found
    extra = [
        "/opt/homebrew/bin", "/usr/local/bin",
        str(Path.home() / ".local" / "bin"), str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".bun" / "bin"), str(Path.home() / ".cargo" / "bin"),
    ]
    return shutil.which(name, path=os.pathsep.join(extra))


def _missing_cli(name: str) -> dict:
    return {"ok": False, "terminal": True,
            "output": f"{name} CLI not found (searched PATH={os.environ.get('PATH', '')} "
                      "and the usual install directories)"}


def _run_shell(command: str, *, timeout: int = 300) -> dict:
    """Run a shell command.  Returns ``{"ok": bool, "output": str}``."""
    returncode, stdout, stderr = _run_process(command, shell=True, timeout=timeout)
    if returncode is None:
        return {"ok": False, "output": f"Timed out after {timeout}s"}
    output = stdout
    if stderr:
        output += "\n[stderr]\n" + stderr
    return {"ok": returncode == 0, "output": redact_text(output.strip())}


def _build_agent_prompt(reminder: dict, fields: dict, store: Store) -> str:
    """Assemble the prompt string for a headless agent wake/action."""
    parts = [f"# Reminder Action: {reminder['title']}"]
    if reminder.get("body"):
        parts.append(f"\n{reminder['body']}")
    if fields["action_instructions"]:
        parts.append(f"\n## Instructions\n{fields['action_instructions']}")
    if fields["action_command"]:
        parts.append(
            f"\n## Shell Command Available\n```\n{fields['action_command']}\n```"
        )
        parts.append("You may run this command if it helps accomplish the instructions.")

    # Include related knowledge node when present
    related_id = reminder.get("related_node_id")
    if related_id:
        node = store.get_node(related_id)
        if node:
            content = (node.get("content") or "")[:500]
            parts.append(f"\n## Related Knowledge\n**{node['title']}**: {content}")

    return redact_text("\n".join(parts))


def _build_claude_prompt(reminder: dict, fields: dict, store: Store) -> str:
    """Assemble the prompt string for a headless ``claude -p`` invocation."""
    return _build_agent_prompt(reminder, fields, store)


def _run_claude(
    reminder: dict,
    fields: dict,
    config: Config,
    store: Store,
    *,
    timeout: int = 300,
) -> dict:
    """Launch ``claude -p`` with assembled context.  Returns ``{"ok": bool, "output": str}``."""
    prompt = _build_claude_prompt(reminder, fields, store)

    claude = _resolve_cli("claude")
    if claude is None:
        return _missing_cli("claude")
    cmd = [claude, "-p", prompt, "--output-format", "json"]

    model = config.reminders.channels.claude.headless_model
    if model:
        cmd.extend(["--model", model])

    # The configured spend cap is the cap. A five-turn limit stood in for it,
    # failed every task needing a sixth turn, and was retried all day.
    budget = config.reminders.channels.claude.max_budget_usd
    if budget:
        cmd.extend(["--max-budget-usd", str(budget)])

    returncode, stdout, stderr = _run_process(cmd, timeout=timeout)
    if returncode is None:
        return {"ok": False, "output": f"claude -p timed out after {timeout}s"}
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return {"ok": returncode == 0, "output": redact_text((stdout or stderr).strip())[:4000]}
    text = str(report.get("result") or "")
    subtype = str(report.get("subtype") or "")
    if report.get("is_error") or returncode != 0:
        detail = text or subtype or stderr.strip() or f"exit {returncode}"
        return {"ok": False, "terminal": subtype.startswith("error_max"),
                "output": redact_text(detail)[:4000]}
    return {"ok": True, "output": redact_text(text.strip())[:4000]}


def _run_codex(
    reminder: dict,
    fields: dict,
    store: Store,
    *,
    timeout: int = 300,
) -> dict:
    """Launch a headless Codex wake via ``codex exec``."""
    prompt = _build_agent_prompt(reminder, fields, store)
    codex = _resolve_cli("codex")
    if codex is None:
        return _missing_cli("codex")
    cmd = [codex, "exec"]

    if fields.get("wake_cwd"):
        cmd.extend(["--cd", fields["wake_cwd"]])
    if fields.get("wake_model"):
        cmd.extend(["--model", fields["wake_model"]])

    session_id = str(fields.get("wake_session_id") or "").strip()
    if session_id:
        cmd.append("resume")
        if session_id.lower() in {"last", "--last"}:
            cmd.append("--last")
        else:
            cmd.append(session_id)
        cmd.append("-")
    else:
        cmd.append("-")

    returncode, stdout, stderr = _run_process(cmd, input_text=prompt, timeout=timeout)
    if returncode is None:
        return {"ok": False, "output": f"codex exec timed out after {timeout}s"}
    output = stdout
    if stderr:
        output += "\n[stderr]\n" + stderr
    return {"ok": returncode == 0, "output": redact_text(output.strip())[:4000]}


def _run_opencode(
    reminder: dict,
    fields: dict,
    store: Store,
    *,
    timeout: int = 300,
) -> dict:
    """Launch a headless OpenCode wake via ``opencode run``."""
    prompt = _build_agent_prompt(reminder, fields, store)
    opencode = _resolve_cli("opencode")
    if opencode is None:
        return _missing_cli("opencode")
    cmd = [opencode, "run"]

    session_id = str(fields.get("wake_session_id") or "").strip()
    if session_id:
        if session_id.lower() in {"last", "--last", "continue", "--continue"}:
            cmd.append("--continue")
        else:
            cmd.extend(["--session", session_id])
    if fields.get("wake_cwd"):
        cmd.extend(["--dir", fields["wake_cwd"]])
    if fields.get("wake_model"):
        cmd.extend(["--model", fields["wake_model"]])
    if fields.get("wake_agent"):
        cmd.extend(["--agent", fields["wake_agent"]])

    cmd.append(prompt)

    returncode, stdout, stderr = _run_process(cmd, timeout=timeout)
    if returncode is None:
        return {"ok": False, "output": f"opencode run timed out after {timeout}s"}
    output = stdout
    if stderr:
        output += "\n[stderr]\n" + stderr
    return {"ok": returncode == 0, "output": redact_text(output.strip())[:4000]}
