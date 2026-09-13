"""Shared advisory lookback for coding hosts; execution is owned by sim.py."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .privacy import redact, redact_text, safe_error


def record_health(scope, kind: str, **details):
    """Health telemetry is evidence only and may never break host execution."""
    if not isinstance(scope, dict):
        return
    try:
        from .supervisor_health import record_automatic
        record_automatic(scope, kind, details)
    except Exception:
        pass


def session_key(scope: dict) -> str:
    sid = scope.get("session_id")
    if not isinstance(sid, str) or not sid.strip() or len(sid) > 200:
        raise ValueError("Supervisor requires an explicit session_id; no shared fallback")
    project = scope.get("project_path")
    if not isinstance(project, str) or not Path(project).is_absolute():
        raise ValueError("Supervisor requires an explicit absolute project_path")
    return "supervisor:" + hashlib.sha256(json.dumps(
        [str(Path(project).resolve()), scope.get("agent", ""), sid]).encode()).hexdigest()


@contextmanager
def store_lock(store, name: str, *, blocking: bool = True):
    """OS releases the lock on worker exit; no stale sentinel or secret file."""
    store.config.data_path.mkdir(parents=True, exist_ok=True)
    path = store.config.data_path / (".supervisor-" + name + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def config_snapshot(config) -> dict:
    return {"data_dir": str(config.data_path), "sim": config.sim.model_dump(),
            "llm": config.llm.model_dump(), "budget": config.budget.model_dump(),
            "active_profile": config.active_profile, "profile_source": config.profile_source,
            "project_path": str(config._project_path) if config._project_path else None}


def restore_config(snapshot: dict):
    from .config import Config
    fields = dict(snapshot)
    project = fields.pop("project_path", None)
    config = Config(**fields)
    if project:
        config._project_path = Path(project)
    return config


def read_state(store, conversation_id: str) -> dict:
    raw = store.get_meta("supervisor.state." + conversation_id)
    return json.loads(raw) if raw else {}


def write_state(store, conversation_id: str, state: str | None, **fields):
    from .sim import _now
    value = json.dumps(redact({**({"state": state} if state is not None else {}), "updated_at": _now(), **fields}))
    store.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=json_patch(meta.value,excluded.value)",
                       ("supervisor.state." + conversation_id, value))
    store.conn.commit()


def _transcript(path: str, limit: int) -> tuple[str, str]:
    """Bounded head goal plus recent transcript, supporting Claude/Codex JSONL."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise ValueError("Supervisor transcript is unavailable")
    with target.open("rb") as stream:
        head = stream.read(min(65536, max(limit, 8192))).decode(errors="replace")
        stream.seek(max(0, target.stat().st_size - limit))
        tail = stream.read(limit).decode(errors="replace")
    goal = ""
    fallback = ""
    native_agy = '"USER_INPUT"' in head or '"PLANNER_RESPONSE"' in head
    if native_agy:
        import re
        recent = []
        for line in tail.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            content = row.get("content", "")
            if not isinstance(content, str) or row.get("type") in ("EPHEMERAL_MESSAGE", "SYSTEM_MESSAGE"):
                continue
            if row.get("type") == "USER_INPUT":
                match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.S)
                content = match.group(1).strip() if match else content
            recent.append(str(row.get("type", "work")) + ": " + content)
        tail = "\n".join(recent)[-limit:]
    for line in head.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") or row.get("payload") or row
        if not isinstance(msg, dict):
            continue
        if row.get("type") == "USER_INPUT" and row.get("source") == "USER_EXPLICIT":
            import re
            content = row.get("content", "")
            if isinstance(content, str):
                match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.S)
                goal = (match.group(1).strip() if match else content)[:2000]
                break
        if msg.get("role") == "user" or row.get("role") == "user" or row.get("type") == "user" or msg.get("type") == "user_message":
            content = msg.get("content") or msg.get("message") or msg.get("text") or ""
            if isinstance(content, list):
                content = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                if row.get("type") == "event_msg" and msg.get("type") == "user_message":
                    goal = content[:2000]
                    break
                if not fallback and not any(marker in content for marker in
                        ("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>")):
                    fallback = content[:2000]
    return redact_text(tail), redact_text(goal or fallback)


def preflight(config, conversation: str) -> tuple[str, str] | None:
    """Cheap known-unavailable checks; the worker repeats all spend gates."""
    from .budget import BudgetLedger
    from .sim import SIM_PURPOSE
    budget = BudgetLedger(config.ledger_path, config.budget)
    if (not budget.can_spend() or config.sim.max_review_cost <= 0 or
            budget.conversation_spend(conversation, purpose=SIM_PURPOSE) >= config.sim.max_conversation_cost):
        return "budget_exhausted", "budget_unavailable"
    if config.sim.command:
        import shlex
        import shutil
        try:
            command = os.path.expanduser(shlex.split(config.sim.command)[0])
        except (ValueError, IndexError):
            return "unavailable", "command_unavailable"
        if not shutil.which(command):
            return "unavailable", "command_unavailable"
        if "simulacrum" in command and not any(os.environ.get(name) for name in
                ("ANTHROPIC_API_KEY", "WANDER_ANTHROPIC_API_KEY", "JMC_ANTHROPIC_API_KEY")):
            return "unavailable", "credential_unavailable"
        allowance = config.sim.max_review_cost
        if (budget.today_spend + allowance > budget.limits.daily or
                budget.week_spend + allowance > budget.limits.weekly or
                budget.month_spend + allowance > budget.limits.monthly or
                budget.conversation_spend(conversation, purpose=SIM_PURPOSE) + allowance > config.sim.max_conversation_cost):
            return "budget_exhausted", "review_allowance"
    else:
        from .llm import is_configured
        if not is_configured(config):
            return "unavailable", "llm_unavailable"
    return None


def supervisor_tick(store, config, scope: dict, *, text: str, goal=None, initial_goal=None,
                    event_id=None, transcript_path=None, deliver: bool = True,
                    text_is_goal: bool = True) -> dict:
    from .sim import (enqueue_sim_review, pop_pending_sim_injection, format_sim_injection,
                      sim_effective_enabled, spawn_background_drain)
    conversation = session_key(scope)
    record_health(scope, "hook", source="hook")
    diagnostics = {"data_dir": str(config.data_path), "db_path": str(store.db_path),
                   "session_id": scope["session_id"], "agent": scope.get("agent", "")}
    if not sim_effective_enabled(store, config):
        return {"ok": True, "context": "", "supervisor": {**diagnostics, "state": "disabled"}}
    trace, original = "", ""
    if transcript_path:
        try:
            trace, original = _transcript(str(transcript_path), config.sim.window_chars)
        except (OSError, ValueError) as exc:
            write_state(store, conversation, "failed", reason="transcript_unavailable")
            record_health(scope, "review", state="failed", reason="transcript_unavailable", source="hook",
                          event_id=hashlib.sha256(str(transcript_path).encode()).hexdigest() + ":transcript_unavailable")
            return {"ok": True, "context": "Kindex supervisor could not read the session transcript; no fresh lookback was completed.",
                    "supervisor": {**diagnostics, "state": "failed", "reason": "transcript_unavailable", "error": safe_error(exc)}}
    clean = redact_text(text or "")[-config.sim.window_chars:]
    incoming = (trace + "\n" + clean).strip()[-config.sim.window_chars:]
    digest = hashlib.sha256(json.dumps([event_id, incoming], sort_keys=True).encode()).hexdigest()
    queued = False
    with store_lock(store, "session-" + conversation.split(":")[-1]):
        state = read_state(store, conversation)
        previous = state.get("window", "")
        window = incoming if trace or (previous and previous in incoming) else (previous + "\n" + incoming).strip()
        window = window[-config.sim.window_chars:]
        intent = redact_text(str(goal or state.get("goal") or initial_goal or original or
                                 (clean if text_is_goal else "")))[:2000]
        duplicate = state.get("event_digest") == digest
        tick = int(state.get("tick", 0)) + (0 if duplicate else 1)
        with store_lock(store, "queue"):
            injection = pop_pending_sim_injection(store, config, conversation, window, tick=tick, intent=intent) if deliver else None
        lines = format_sim_injection(injection, display=config.sim.display)
        # Status diagnostics contain no transcript, goal, provider key, or command.
        if not duplicate:
            write_state(store, conversation, None, tick=tick,
                        event_digest=digest, window=window, goal=intent)
            unavailable = preflight(config, conversation)
            if unavailable:
                write_state(store, conversation, unavailable[0], reason=unavailable[1])
                record_health(scope, "review", state=unavailable[0], source="hook",
                              reason="budget_exhausted" if unavailable[0] == "budget_exhausted" else "llm_unavailable",
                              event_id=digest + ":preflight:" + unavailable[0])
            else:
                queued = enqueue_sim_review(store, config, conversation, window, tick=tick, intent=intent, scope=scope)
        if injection:
            write_state(store, conversation, "delivered", reason="advisory", delivered_tick=tick)
        latest = read_state(store, conversation)
        if (latest.get("state") == "skipped" and latest.get("reason") in {"cadence", "banter"}
                and latest.get("delivery_drop_tick") == tick):
            write_state(store, conversation, "skipped", reason=latest["delivery_drop_reason"])
            latest = read_state(store, conversation)
    if queued and config.sim.drain_on_tick:
        if not spawn_background_drain(config):
            write_state(store, conversation, "failed", reason="worker_unavailable")
            record_health(scope, "review", state="failed", reason="worker_unavailable", source="hook",
                          event_id=digest + ":worker_unavailable")
            latest = read_state(store, conversation)
    public = {k: v for k, v in latest.items() if k in
              {"state", "reason", "tick", "updated_at", "reviewed_at", "review_tick", "delivered_tick", "accounting_status", "advocate_state", "advocate_reason", "delivery_drop_reason", "delivery_drop_tick"}}
    public.setdefault("state", "skipped")
    context = "\n".join(lines)
    if not context and public["state"] in {"failed", "unavailable", "budget_exhausted"}:
        notice = public["state"] + ":" + public.get("reason", "unknown")
        if latest.get("notice") != notice:
            context = f"Kindex supervisor: {public['state']} ({public.get('reason', 'unknown')}); no fresh lookback completed."
            write_state(store, conversation, None, notice=notice)
    return {"ok": True, "context": context, "supervisor": {**diagnostics, **public}}


def hook_request(payload: dict, adapter: str, *, config=None, project_path=None) -> dict:
    from .attention import extract_conversation_text, resolve_conversation_id
    from .integrations import open_project_store, project_scope
    from .agent_settings import apply_agent_overrides
    from .store import Store
    # Explicitly disabled callers do not need a Git worktree or a session.
    # Read an existing override only; the disabled check must not create a DB.
    from .config import trusted_supervisor_config, resolve_project_root
    from .project_store import project_data_path
    import sqlite3
    from .agent_adapters import hook_project_path
    cursor_event = str(payload.get("hook_event_name") or "") if adapter == "cursor" else ""
    deliver = True
    if adapter == "cursor":
        # A tool's cwd may differ from the native workspace. User hook processes
        # themselves run in ~/.cursor, which is never a project fallback.
        native_scope = {"workspace_roots": payload.get("workspace_roots")}
        project_path = hook_project_path(native_scope, project_path)
        if payload.get("conversation_id") and payload.get("session_id") and payload["conversation_id"] != payload["session_id"]:
            raise ValueError("Cursor supplied conflicting conversation and session IDs")
        deliver = cursor_event in {"sessionStart", "postToolUse"} or (
            cursor_event == "stop" and payload.get("status") == "completed" and
            isinstance(payload.get("loop_count", 0), int) and payload.get("loop_count", 0) == 0)
    if adapter == "antigravity" and not (project_path or payload.get("cwd") or payload.get("project_path")
                                         or payload.get("workspacePaths") or payload.get("workspace_paths")):
        raise ValueError("Antigravity supplied no workspace; launch agy --add-dir <project> or select a project")
    if adapter == "antigravity" or payload.get("workspacePaths"):
        project_path = hook_project_path(payload, project_path)
    root = resolve_project_root(project_path or payload.get("cwd") or payload.get("project_path"))
    identity_payload = {key: value for key, value in payload.items()
                        if key not in ("transcript_path", "transcriptPath")}
    sid = ((payload.get("conversation_id") or payload.get("session_id") or "") if adapter == "cursor" else
           resolve_conversation_id(None, identity_payload, fallback_to_cwd=False))
    if adapter == "cursor" and cursor_event in {"postToolUse", "afterMCPExecution"}:
        receipt_scope = project_scope({"project_path": str(root), "agent": "cursor", "session_id": sid})
        _record_cursor_use(receipt_scope, payload)
        if cursor_event == "afterMCPExecution":
            # The generic postToolUse event owns cadence and context injection;
            # this event supplies the MCP server provenance that it omits.
            record_health(receipt_scope, "hook", source="hook")
            return {"ok": True, "context": "", "supervisor": {"state": "skipped", "reason": "use_receipt"}}
    preliminary = config or trusted_supervisor_config(root, str(project_data_path(root)))
    if not preliminary.sim.enabled:
        enabled = False
        for name in ("kindex.db", "conv.db"):
            database = preliminary.data_path / name
            if database.is_file():
                conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                try:
                    row = conn.execute("SELECT value FROM meta WHERE key='sim.enabled'").fetchone()
                    enabled = bool(row and str(row[0]).lower() in ("true", "1", "yes", "on"))
                finally:
                    conn.close()
                break
        if not enabled:
            if sid:
                disabled_scope = {"project_path": str(root), "agent": adapter, "session_id": sid}
                record_health(disabled_scope, "hook", source="hook", state="disabled")
                record_health(disabled_scope, "review", source="hook", state="disabled", reason="disabled", event_id="disabled")
            return {"ok": True, "context": "", "supervisor": {"state": "disabled"}}
    scope = project_scope({"session_id": sid, "agent": adapter,
                           "project_path": str(Path(project_path or payload.get("cwd") or
                                                    payload.get("project_path") or os.getcwd()).resolve())})
    store = Store(config) if config is not None else open_project_store(scope)
    try:
        cfg = apply_agent_overrides(config or store.config, client=adapter, instance_key=sid)
        text = extract_conversation_text(None, payload)
        event_id = payload.get("event_id")
        if adapter == "cursor":
            event_id = json.dumps([payload.get("generation_id"), cursor_event,
                                   payload.get("tool_use_id"), payload.get("loop_count")])
            if cursor_event == "postToolUse":
                text += "\ntool_output: " + str(payload.get("tool_output") or "")
        return supervisor_tick(store, cfg, scope, text=text,
                               goal=payload.get("goal") or payload.get("focus"), initial_goal=payload.get("initial_goal"),
                               event_id=event_id, deliver=deliver,
                               text_is_goal=adapter != "cursor" or cursor_event == "beforeSubmitPrompt",
                               transcript_path=payload.get("transcript_path") or payload.get("transcriptPath"))
    finally:
        store.close()


def _record_cursor_use(scope: dict, payload: dict):
    """Record only native MCP provenance; never search command/code strings."""
    from .supervisor_health import TOOLS
    name = payload.get("tool_name")
    if not isinstance(name, str):
        return
    tool = None
    if payload.get("hook_event_name") == "afterMCPExecution":
        if payload.get("mcp_server_name") == "kindex" and name in TOOLS:
            tool = name
    else:
        # Generic Cursor MCP hooks may expose just MCP:search. Without the
        # server identity that is ambiguous and must not count as Kindex use.
        raw = name.removeprefix("MCP:")
        for prefix in ("mcp__kindex__", "kindex.", "kindex_"):
            if raw.startswith(prefix) and raw[len(prefix):] in TOOLS:
                tool = raw[len(prefix):]
                break
    if not tool:
        return
    raw_result = payload.get("result_json") if payload.get("hook_event_name") == "afterMCPExecution" else payload.get("tool_output")
    outcome = "observed"
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if isinstance(result, dict):
            if any(result.get(flag) for flag in ("isError", "error", "rejected", "permissionDenied")):
                outcome = "failed"
            elif isinstance(result.get("content"), list) and result.get("isError") is False:
                outcome = "success"
    except (ValueError, TypeError):
        pass
    event = hashlib.sha256(json.dumps([payload.get("generation_id"), payload.get("hook_event_name"),
        payload.get("tool_use_id"), tool, payload.get("tool_input"), raw_result,
        payload.get("duration")], sort_keys=True).encode()).hexdigest()
    record_health(scope, "use", source="hook", initiator="agent", tool=tool, outcome=outcome, event_id=event)


def worker_main():
    from .store import Store
    from .sim import drain_sim_queue
    snapshot = json.loads(sys.stdin.read(1024 * 1024))
    config = restore_config(snapshot)
    store = Store(config)
    try:
        drain_sim_queue(store, config)
    finally:
        store.close()


if __name__ == "__main__":
    worker_main()
