"""Versioned JSON boundary for coding-host adapters.

Kindex owns durable repo knowledge/tasks. signet-eval owns local-model policy
and host redaction. There is deliberately no dependency on the Signet product,
no Personal fallback, and no host-specific execution inside the task service.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import uuid

from .privacy import POLICY_VERSION, redact, redact_text, safe_error

PROTOCOL_VERSION = 1
NATIVE_TASK_TOOLS = frozenset({"TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TodoWrite"})


class IntegrationError(ValueError):
    """A deliberate refusal or invalid protocol reply, not a storage outage."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _digest(value) -> str:
    # serde_json's sorted object keys and compact UTF-8 form, used by Signet-eval.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _host_scope(scope: dict) -> dict:
    return {key: scope[key] for key in ("project_path", "session_id", "agent")}


def project_scope(scope: dict) -> dict:
    if not isinstance(scope, dict) or not isinstance(scope.get("project_path"), str):
        raise IntegrationError("invalid_scope", "Explicit project_path is required")
    if len(scope["project_path"]) > 4096 or "\x00" in scope["project_path"]:
        raise IntegrationError("invalid_scope", "project_path exceeds the supported path boundary")
    for name, value in (("session_id", scope.get("session_id")), ("agent", scope.get("agent", "claude"))):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,200}", value):
            raise IntegrationError("invalid_scope", f"Explicit {name} must be a bounded host identifier")
    if scope.get("profile") not in (None, "", "legacy") or scope.get("include_global") is True:
        raise IntegrationError("invalid_scope", "Modern codebase storage does not accept a Personal, Company, or global profile")
    path = Path(scope["project_path"])
    if not path.is_absolute() or not path.is_dir():
        raise IntegrationError("invalid_scope", "project_path must be an existing absolute directory")
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=3)
    if probe.returncode:
        raise IntegrationError("invalid_scope", "No Git worktree: choose an explicit Kindex profile using the legacy CLI; Personal is not an implicit fallback")
    root = Path(probe.stdout.strip()).resolve()
    return {"project_path": str(root), "session_id": scope["session_id"],
            "agent": scope.get("agent", "claude"), **({"profile": scope["profile"]} if scope.get("profile") else {})}


def open_project_store(scope: dict):
    """Open only .kin/local in the actual worktree, never clone-configured paths.

    Shared .kin artifacts are evidence, not configuration authority. Existing
    legacy profiles remain available through their existing explicit surfaces.
    """
    from .config import Config
    from .store import Store
    scoped = project_scope(scope)
    if scoped.get("profile") not in (None, "legacy"):
        raise ValueError("Modern codebase storage does not accept a Personal or Company profile")
    root = Path(scoped["project_path"])
    kin = root / ".kin"
    local = kin / "local"
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", ".kin/local"],
                             capture_output=True, text=True, timeout=3, check=True)
    if tracked.stdout.strip():
        raise ValueError("Refusing tracked .kin/local storage; clone content is not a local trusted database")
    if kin.is_symlink() or local.is_symlink() or (local / "kindex").is_symlink():
        raise ValueError("Refusing symlinked repo-local Kindex storage")
    for leaf in ("kindex.db", "conv.db", "kindex.db-wal", "kindex.db-shm", "conv.db-wal", "conv.db-shm"):
        target = local / "kindex" / leaf
        if target.is_symlink() or (target.exists() and target.stat().st_nlink > 1):
            raise ValueError("Refusing linked repo-local Kindex database")
    kin.mkdir(exist_ok=True)
    ignore = kin / ".gitignore"
    if ignore.is_symlink():
        raise ValueError("Refusing symlinked .kin/.gitignore")
    existing = ignore.read_text() if ignore.exists() else ""
    if "local/" not in existing.splitlines():
        ignore.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "local/\n")
    config = Config(data_dir=str(local / "kindex"))
    config._project_path = root
    # The modern codebase lane is separate from legacy profile selection.
    return Store(config)


def _signet(command: str, payload: dict, *, timeout: float = 5) -> dict | None:
    pinned = os.environ.get("KIN_SIGNET_EXECUTABLE")
    if pinned is not None:
        path = Path(pinned)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise IntegrationError("owner_unavailable", "Pinned signet-eval executable is unavailable; reinstall the selected adapter")
        binary = str(path)
    else:
        binary = shutil.which("signet-eval")
    if not binary:
        return None
    result = subprocess.run([binary, "integration", command], input=json.dumps(payload),
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        if command == "describe" and any(word in result.stderr.lower() for word in ("unrecognized", "unexpected", "unknown")):
            raise IntegrationError("unsupported_owner", "Installed signet-eval lacks this integration protocol; upgrade signet-eval or use kin setup-hooks --mode legacy")
        raise RuntimeError("signet-eval integration failed; inspect its policy and installation before retrying")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise IntegrationError("invalid_protocol", "Invalid signet-eval integration reply")
    return value


def _envelope(value: dict) -> None:
    if (not isinstance(value, dict) or type(value.get("protocol_version")) is not int
            or value["protocol_version"] != PROTOCOL_VERSION or value.get("owner") != "signet-eval"):
        raise IntegrationError("invalid_protocol", "Unqualified signet-eval protocol or owner")


def describe(scope: dict) -> dict:
    state = _signet("describe", {"protocol_version": 1, **_host_scope(scope)})
    owner = "kindex"
    if state is not None:
        _envelope(state)
        capability = state.get("capabilities", {}).get("task_enforcement") if isinstance(state.get("capabilities"), dict) else None
        if (type(state.get("active")) is not bool or not isinstance(capability, dict)
                or type(capability.get("ready")) is not bool
                or type(capability.get("protocol_version")) is not int
                or capability["protocol_version"] != PROTOCOL_VERSION
                or state.get("scope") != _host_scope(scope)
                or not isinstance(state.get("policy_revision"), str) or not state["policy_revision"]
                or type(state.get("valid_until")) is not int or state["valid_until"] < time.time()):
            raise IntegrationError("invalid_protocol", "Invalid or expired signet-eval capability declaration")
        if state["active"]:
            if not capability["ready"]:
                raise IntegrationError("owner_unavailable", "Active signet-eval task enforcement is not ready; no fallback authority selected")
            if not isinstance(capability.get("targets"), list) or not all(isinstance(t, str) for t in capability["targets"]):
                raise IntegrationError("invalid_protocol", "Signet-eval did not declare supported task operations")
            owner = "signet-eval"
        elif state.get("reason") not in ("disabled", "paused"):
            raise IntegrationError("owner_unavailable", "Signet-eval could not qualify its policy; no fallback authority selected")
    return {"protocol_version": 1, "task_owner": "kindex", "policy_owner": owner,
            "host_redaction_owner": "signet-eval" if state and state.get("active") else None,
            "host_redaction_qualified": False, "sink_redaction": POLICY_VERSION,
            "signet_eval": state, "project_path": scope.get("project_path"),
            "storage": ".kin/local/kindex", "personal_fallback": False,
            "limitations": ["Function hooks cannot redact Claude's earlier raw prompt enqueue log",
                            "Hook loading/failure remains a host trust boundary"]}


def _deliver_outcome(store, item, *, timeout=None) -> bool:
    from .task_service import acknowledge_outcome
    payload = {"protocol_version": 1, "operation_id": item["operation_id"],
               "receipt": item["authorization_receipt"], "task_receipt": item["result"]}
    result = _signet("record-result", payload, **({"timeout": timeout} if timeout is not None else {}))
    if result:
        _envelope(result)
    if (result and result.get("status") == "recorded"
            and result.get("operation_id") == item["operation_id"]
            and result.get("outcome_digest") == _digest(item["result"])):
        return acknowledge_outcome(store, item["scope"], item["operation_id"])
    return False


def _flush(store, scope):
    from .task_service import pending_outcomes
    pending = pending_outcomes(store, scope)
    if not pending:
        return 0
    # Rotate attempts: a permanently rejected outcome must not starve later
    # receipts. The original pending receipt remains intact for operator repair.
    cursor_key = "integration.outcome_cursor." + _digest(_host_scope(scope))
    last = store.get_meta(cursor_key)
    offset = next((index + 1 for index, item in enumerate(pending) if item["operation_id"] == last), 0)
    selected = pending[offset % len(pending)]
    store.set_meta(cursor_key, selected["operation_id"])
    # One delivery per request bounds host latency. Signet-eval record-result
    # deduplicates a repeated identical receipt after crash-before-ack.
    try:
        delivered = _deliver_outcome(store, selected)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, sqlite3.Error):
        delivered = False
    return len(pending) - int(delivered)


def reconcile_outcomes(scope: dict, max_attempts: int = 16) -> dict:
    """Retry only stored outcome delivery across this worktree's old sessions.

    No adjudication, task effects, or ownership grants. Each original receipt
    retains its original scope. Work is capped at 16 attempts and a 20s deadline;
    pending and damaged records remain visible for subsequent operator repair.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 16:
        raise IntegrationError("invalid_argument", "max_attempts must be between 1 and 16")
    deadline = time.monotonic() + 20
    scoped = project_scope(scope)
    store = open_project_store(scoped)
    try:
        pending, errors, invalid = [], [], 0
        for row in store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'task.outcome.%' ORDER BY key"):
            try:
                item = json.loads(row["value"])
                if not isinstance(item, dict) or not isinstance(item.get("scope"), dict) or type(item.get("acknowledged")) is not bool:
                    raise ValueError("Invalid outcome")
                if item["acknowledged"] or item["scope"].get("project_path") != scoped["project_path"]:
                    continue
                if (not isinstance(item.get("operation_id"), str) or not isinstance(item.get("authorization_receipt"), dict)
                        or not isinstance(item.get("result"), dict)):
                    raise ValueError("Invalid outcome")
                pending.append((row["key"], item))
            except (ValueError, TypeError):
                invalid += 1
                if len(errors) < 16:
                    errors.append({"code": "invalid_outcome", "record": row["key"][-12:]})
        cursor_key = "integration.reconcile_cursor." + _digest(scoped["project_path"])
        last = store.get_meta(cursor_key)
        start = next((i + 1 for i, (key, _) in enumerate(pending) if key == last), 0)
        ordered = pending[start:] + pending[:start]
        attempted = delivered = 0
        for key, item in ordered[:max_attempts]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            store.set_meta(cursor_key, key)
            attempted += 1
            try:
                if _deliver_outcome(store, item, timeout=min(5, remaining)):
                    delivered += 1
                else:
                    errors.append({"code": "audit_not_recorded", "operation_id": item["operation_id"]})
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, sqlite3.Error):
                errors.append({"code": "audit_delivery_failed", "operation_id": item["operation_id"]})
        left = len(pending) - delivered + invalid
        return redact({"ok": not errors, "complete": left == 0, "pending": left,
                       "delivered": delivered, "attempted": attempted, "errors": errors,
                       "time_budget_exhausted": time.monotonic() >= deadline,
                       "project_path": scoped["project_path"]})
    finally:
        store.close()


def execute_task(store, operation, args, scope, *, source_tool="kindex.task", expected_owner=None, source_input=None):
    from .task_service import execute, MUTATIONS, lookup_completed, TaskServiceError
    # Exact source input crosses only the trusted local policy stdin, so source
    # rules retain native names (subject/taskId) and secret-value predicates.
    # Neither package may log/store that source object. Task effects are redacted
    # separately; Signet persists digests, not raw source or target parameters.
    source_input = args if source_input is None else source_input
    if not isinstance(source_input, dict):
        raise IntegrationError("invalid_argument", "Task source input must be an object")
    args = redact(args)
    if operation in MUTATIONS:
        try:
            previous = lookup_completed(store, operation, args, scope)
        except TaskServiceError as error:
            return {"ok": False, "error": {"code": error.code, "message": safe_error(error)}}
        if previous is not None:
            # Confirm an already committed effect even when its admission has
            # expired. This is a read, not a new grant or repeated effect.
            return _with_audit_status(store, scope, previous)
    state = describe(scope)
    owner = state["policy_owner"]
    if expected_owner is not None and expected_owner != owner:
        raise IntegrationError("owner_changed", "Selected policy owner is no longer active or changed; refresh the session explicitly")
    actual_scope = dict(scope)
    if owner == "signet-eval":
        if operation not in state["signet_eval"]["capabilities"]["task_enforcement"]["targets"]:
            raise IntegrationError("unsupported_operation", "Selected signet-eval owner does not support this task operation")
        revision = state["signet_eval"]["policy_revision"]
        operation_id = args.get("operation_id")
        if not operation_id:
            # Read adjudications still have identity but never mutate a task.
            operation_id = "read-" + uuid.uuid4().hex
        request = {"protocol_version": 1, **_host_scope(scope), "operation_id": operation_id,
                   "policy_revision": revision, "source_tool": source_tool,
                   "source_input": source_input,
                   "target_tool": f"kindex.task.{operation}",
                   "input": {"operation": operation, "args": args, "scope": scope, "source_input": source_input}}
        decision = _signet("adjudicate", request)
        if decision is not None:
            _envelope(decision)
        if not decision or decision.get("decision") != "allow":
            return {"ok": False, "error": {"code": "policy_denied", "message": "signet-eval did not authorize this task operation"}}
        receipt = decision.get("receipt")
        _envelope(receipt)
        if (receipt.get("operation_id") != operation_id or receipt.get("policy_revision") != revision
                or decision.get("policy_revision") != revision
                or receipt.get("scope") != _host_scope(scope)
                or receipt.get("scope_digest") != _digest(_host_scope(scope))
                or receipt.get("input_digest") != _digest(request["input"])
                or ("source_input_digest" in receipt and receipt["source_input_digest"] != _digest(source_input))
                or receipt.get("source_tool") != source_tool or receipt.get("target_tool") != request["target_tool"]
                or type(receipt.get("expires_at")) is not int or receipt["expires_at"] < time.time()):
            raise IntegrationError("authorization_mismatch", "Signet-eval authorization does not match this operation, input, scope, or validity window")
        if operation in MUTATIONS:
            actual_scope["authorization_receipt"] = receipt
    result = execute(store, operation, args, actual_scope)
    if owner == "signet-eval":
        return _with_audit_status(store, scope, result)
    return result


def _with_audit_status(store, scope, result):
    try:
        pending = _flush(store, scope)
        if pending:
            result["audit_pending"] = pending
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, sqlite3.Error):
        # The task effect already committed. Delivery failure cannot turn that
        # fact into an operation failure or trigger a second task effect.
        result["audit_pending"] = None
        result["audit_error"] = "Audit delivery status unavailable; the confirmed task result remains durable"
    return result


def _native(store, name, value, scope, operation_id, owner):
    supported_fields = {
        "TaskCreate": {"subject", "description", "activeForm"},
        "TaskGet": {"taskId"}, "TaskList": set(),
        "TaskUpdate": {"taskId", "subject", "description", "activeForm", "status", "owner"},
        "TodoWrite": {"todos"},
    }
    if not isinstance(value, dict):
        raise IntegrationError("invalid_argument", "Native task arguments must be an object")
    if name in supported_fields and value.keys() - supported_fields[name]:
        raise IntegrationError("unsupported_operation", "Unmapped native task fields; use the typed Kindex task tool")
    args = {"operation_id": operation_id}
    if name == "TaskCreate":
        operation = "create"
        args.update(title=value["subject"], content=value["description"], active_form=value.get("activeForm", ""))
    elif name == "TaskGet":
        operation = "get"
        args["id"] = value["taskId"]
    elif name == "TaskList":
        operation = "list"
        args.update(status="all", limit=500)
    elif name == "TaskUpdate":
        operation = "update"
        args["id"] = value["taskId"]
        for native, field in {"subject": "title", "description": "content", "activeForm": "active_form", "owner": "owner"}.items():
            if native in value:
                args[field] = value[native]
        if "status" in value:
            args["status"] = {"pending": "open", "in_progress": "in_progress", "completed": "done", "deleted": "cancelled"}[value["status"]]
    elif name == "TodoWrite":
        # TodoWrite has no stable IDs. Do not invent identity from position or
        # cancel durable tasks when it replaces its ephemeral array.
        raise IntegrationError("policy_denied", "TodoWrite is disabled. Use the durable Kindex task tool (create/list/update); tasks persist across sessions and compaction")
    else:
        raise IntegrationError("unsupported_operation", "Unsupported native task tool")
    result = execute_task(store, operation, args, scope, source_tool=name, expected_owner=owner, source_input=value)
    if not result.get("ok"):
        return result
    def native(task):
        return {"id": task["id"], "subject": task["title"], "description": task["content"],
                "status": {"open": "pending", "in_progress": "in_progress", "done": "completed"}[task["status"]],
                "owner": task.get("owner", ""), "blocks": [], "blockedBy": task.get("dependencies", [])}
    if name == "TaskCreate":
        output = {"task": {"id": result["task"]["id"], "subject": result["task"]["title"]}}
    elif name == "TaskGet":
        output = {"task": None if result["task"]["status"] == "cancelled" else native(result["task"])}
    elif name == "TaskList":
        if result.get("next_cursor"):
            raise IntegrationError("requires_pagination", "More than 500 tasks: use the paginated Kindex task tool")
        output = {"tasks": [native(task) for task in result["tasks"] if task["status"] != "cancelled"]}
    else:
        output = {"success": True, "taskId": result["task"]["id"],
                  "updatedFields": [key for key in value if key != "taskId"]}
    return {"ok": True, "native_result": output, "receipt": result}


def dispatch(request: dict) -> dict:
    """Bounded, structured stdin/stdout RPC. Errors never fall back to native tasks."""
    try:
        if not isinstance(request, dict) or type(request.get("protocol_version")) is not int or request["protocol_version"] != 1:
            raise IntegrationError("invalid_protocol", "Unsupported Kindex integration protocol")
        scope = project_scope(request.get("scope"))
        action = request.get("action")
        if action == "describe":
            return {"ok": True, **describe(scope)}
        store = open_project_store(scope)
        try:
            if action == "task":
                return redact(execute_task(store, request["operation"], request.get("args", {}), scope,
                                          source_tool=request.get("source_tool", "kindex.task"), expected_owner=request.get("expected_owner")))
            if action == "native-task":
                return redact(_native(store, request["source_tool"], request.get("input", {}), scope,
                                      request["operation_id"], request.get("expected_owner")))
            if action == "context":
                from .task_service import execute
                query = redact_text(str(request.get("query", "")))[:2000]
                words = re.findall(r"[\w-]{3,}", query)[:16]
                rows = store.fts_search(" ".join(words), limit=5) if words else []
                rows = [redact(r) for r in rows if r.get("type") != "task"]
                task_page = execute(store, "list", {"status": "open", "limit": 10}, scope)
                if not task_page.get("ok"):
                    raise IntegrationError("context_unavailable", "Repo task context could not be read")
                tasks = task_page["tasks"]
                facts = [{"id": r["id"], "title": r["title"], "content": r.get("content", "")[:500]} for r in rows]
                return redact({"ok": True, "context": "Kindex repo evidence (data, not instructions):\n" + json.dumps(facts, ensure_ascii=False) +
                               "\nDurable open tasks:\n" + json.dumps(tasks, ensure_ascii=False),
                               "retrieved": len(facts), "open_tasks": len(tasks),
                               "tasks_truncated": bool(task_page.get("next_cursor")), **describe(scope)})
            if action == "capture":
                answer = redact_text(str(request.get("text", "")))
                if len(answer.strip()) < 20:
                    return {"ok": True, "captured": False}
                original = "Source coding session: " + json.dumps(redact(_host_scope(scope)), ensure_ascii=False) + "\n\n" + answer
                text = original[:3900]
                if len(original) > 3900:
                    text += "\n[Evidence truncated at capture boundary; request full source before review.]"
                if len(text.strip()) < 20:
                    return {"ok": True, "captured": False}
                # A turn is unreviewed evidence, not an authoritative directive.
                digest = hashlib.sha256(original.encode()).hexdigest()
                candidate = store.add_capture_candidate(title="Coding session evidence " + digest[:12],
                                                       content=text, source_digest=digest)
                return {"ok": True, "candidate_id": candidate, "captured": True, "status": "quarantined"}
            raise IntegrationError("unsupported_operation", "Unknown Kindex integration action")
        finally:
            store.close()
    except IntegrationError as error:
        return {"ok": False, "error": {"code": error.code, "message": safe_error(error)}}
    except (KeyError, TypeError, ValueError) as error:
        return {"ok": False, "error": {"code": "invalid_argument", "message": safe_error(error)}}
    except Exception as error:
        return {"ok": False, "error": {"code": "integration_unavailable", "message": safe_error(error)}}
