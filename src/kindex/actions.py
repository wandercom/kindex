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
import subprocess
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
    MCP ``remind_exec``): it may resume a ``paused`` action, which automated
    sweeps must skip.
    """
    fields = get_action_fields(reminder)
    if not has_action(reminder):
        return {"status": "skipped", "reason": "no action defined"}

    if fields["action_status"] == "completed":
        return {"status": "skipped", "reason": "already completed"}
    if fields["action_status"] == "paused" and not manual:
        # Parked by the staleness guard — only a deliberate exec resumes it.
        return {"status": "skipped", "reason": "paused (stale); run kin remind exec to resume"}
    if fields["action_status"] == "running" and not _running_is_stale(reminder, timeout):
        return {"status": "skipped", "reason": "already running"}

    mode = resolve_mode(fields)
    rid = reminder["id"]

    # Mark as running (race guard for concurrent daemon cycles)
    _update_action_status(store, rid, reminder, "running", "")

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
            result = {"ok": False, "output": f"Unknown mode: {mode}"}

        status = "completed" if result["ok"] else "failed"
        _update_action_status(store, rid, reminder, status, result["output"])
        return {"status": status, "output": result["output"]}

    except Exception as e:
        _update_action_status(store, rid, reminder, "failed", safe_error(e))
        return {"status": "failed", "output": safe_error(e)}


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


def _run_shell(command: str, *, timeout: int = 300) -> dict:
    """Run a shell command.  Returns ``{"ok": bool, "output": str}``."""
    try:
        proc = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        return {"ok": proc.returncode == 0, "output": redact_text(output.strip())}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Timed out after {timeout}s"}


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

    cmd = ["claude", "-p", prompt]

    model = config.reminders.channels.claude.headless_model
    if model:
        cmd.extend(["--model", model])

    budget = config.reminders.channels.claude.max_budget_usd
    if budget:
        cmd.extend(["--max-turns", "5"])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {"ok": proc.returncode == 0, "output": redact_text(proc.stdout.strip())[:4000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"claude -p timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "output": "claude CLI not found in PATH"}


def _run_codex(
    reminder: dict,
    fields: dict,
    store: Store,
    *,
    timeout: int = 300,
) -> dict:
    """Launch a headless Codex wake via ``codex exec``."""
    prompt = _build_agent_prompt(reminder, fields, store)
    cmd = ["codex", "exec"]

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

    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        return {"ok": proc.returncode == 0, "output": redact_text(output.strip())[:4000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"codex exec timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "output": "codex CLI not found in PATH"}


def _run_opencode(
    reminder: dict,
    fields: dict,
    store: Store,
    *,
    timeout: int = 300,
) -> dict:
    """Launch a headless OpenCode wake via ``opencode run``."""
    prompt = _build_agent_prompt(reminder, fields, store)
    cmd = ["opencode", "run"]

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

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        return {"ok": proc.returncode == 0, "output": redact_text(output.strip())[:4000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"opencode run timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "output": "opencode CLI not found in PATH"}
