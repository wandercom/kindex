"""Typed, scoped task operations for host adapters and durable replay.

SQLite owns tasks and successful operation receipts. No host task list is kept.
Legacy CLI/MCP surfaces can continue using tasks.py directly; adapters must supply
the actual event scope and stable operation IDs, never a process-cwd fallback.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import tasks
from .privacy import redact, safe_error

MUTATIONS = frozenset({"create", "update", "complete", "cancel", "claim", "release", "reconcile"})
_FIELDS = {"title", "content", "status", "priority", "due", "owner", "active_form", "effort", "dependencies"}
_ARGUMENTS = {
    "create": _FIELDS | {"scope", "link_to", "domains", "external_id", "namespace"},
    "update": _FIELDS | {"id", "expected_version"},
    "get": {"id"}, "list": {"status", "limit", "cursor"},
    "complete": {"id", "expected_version"}, "cancel": {"id", "expected_version"},
    "claim": {"id", "expected_version", "ttl_minutes", "note", "force"},
    "release": {"id", "expected_version", "force"},
    "reconcile": {"namespace", "items", "cancel_missing"},
}


class TaskServiceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise TaskServiceError("invalid_argument", f"{label} must be a nonempty string of at most 256 characters")
    if any(ord(c) < 32 for c in value):
        raise TaskServiceError("invalid_argument", f"{label} must not contain control characters")
    return value


def _operation_key(scoped: dict, operation_id: str) -> str:
    return hashlib.sha256(_json({"scope": scoped, "operation_id": operation_id}).encode()).hexdigest()


def _request_digest(operation: str, clean: dict, scoped: dict) -> str:
    return hashlib.sha256(_json({"operation": operation, "args": clean, "scope": scoped}).encode()).hexdigest()


def lookup_completed(store, operation: str, args: dict, scope: dict) -> dict | None:
    """Confirm a committed effect without reauthorizing or repeating it.

    Authorization expiry affects new effects, never reads of committed receipts.
    The exact sanitized operation and scoped input still must match. A conflicting
    operation-ID reuse raises TaskServiceError; a different scope has no receipt.
    """
    if operation not in MUTATIONS:
        return None
    if not isinstance(args, dict):
        raise TaskServiceError("invalid_argument", "args must be an object")
    scoped, clean = _scope(store, scope), redact(args)
    operation_id = _identifier(clean.get("operation_id"), "operation_id")
    key = "task.operation." + _operation_key(scoped, operation_id)
    row = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    receipt = json.loads(row["value"])
    if receipt["request_digest"] != _request_digest(operation, clean, scoped):
        raise TaskServiceError("operation_conflict", "operation_id already committed with different arguments")
    return {**receipt["result"], "replayed": True}


def _authorization(value: Any, operation_id: str) -> tuple[dict | None, dict | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or value.get("operation_id") != operation_id:
        raise TaskServiceError("authorization_mismatch", "Authorization receipt must identify this operation")
    stable = {key: value.get(key) for key in ("operation_id", "input_digest", "scope_digest", "policy_revision")}
    for key, item in stable.items():
        _identifier(item, f"authorization_receipt.{key}")
    return redact(value), stable


def _scope(store, supplied: dict) -> dict:
    if not isinstance(supplied, dict):
        raise TaskServiceError("invalid_scope", "Explicit project and session scope is required")
    project = supplied.get("project_path")
    if not isinstance(project, str) or not project or not Path(project).is_absolute():
        raise TaskServiceError("invalid_scope", "project_path must be an explicit absolute path")
    session = _identifier(supplied.get("session_id"), "session_id")
    profile = store.config.active_profile or "legacy"
    if supplied.get("profile") not in (None, "", profile):
        raise TaskServiceError("scope_mismatch", "The requested profile does not match the open task store")
    return {"project_path": str(Path(project).resolve()), "session_id": session,
            "profile": profile, "agent": supplied.get("agent") or "",
            "include_global": supplied.get("include_global") is True}


def task_record(node: dict) -> dict:
    """Stable host-neutral task representation; status uses Kindex vocabulary."""
    extra = node.get("extra") or {}
    return redact({
        "id": node["id"], "title": node.get("title", ""),
        "content": node.get("content", ""), "status": extra.get("task_status", "open"),
        "version": int(extra.get("task_version", 0)), "priority": extra.get("priority", 3),
        "due": extra.get("due"), "owner": extra.get("owner", ""),
        "dependencies": extra.get("dependencies", []), "claim": extra.get("claim"),
        "project_path": extra.get("project_path"),
        "session_id": extra.get("conversation_id"), "scope": extra.get("scope", "contextual"),
        "external_id": extra.get("external_id", ""),
        "active_form": extra.get("active_form", ""),
    })


def _in_scope(node: dict, scope: dict, store=None) -> bool:
    extra = node.get("extra") or {}
    if scope["include_global"] and extra.get("scope") == "global":
        return True
    project = extra.get("project_path")
    if not project and store is not None:
        from .project_store import is_project_store
        if is_project_store(store, scope["project_path"]):
            return True  # historical unscoped tasks in this repo's own database
    return bool(project) and str(Path(project).resolve()) == scope["project_path"]


def _get(store, task_id: Any, scope: dict) -> dict:
    node = tasks.get_task(store, _identifier(task_id, "task id"))
    if not node or not _in_scope(node, scope, store):
        raise TaskServiceError("not_found", "Task not found in the requested scope")
    return node


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise TaskServiceError("invalid_argument", f"{field} must be a list of nonempty strings")
    return list(dict.fromkeys(value))


def _validate_fields(args: dict) -> None:
    for field in ("title", "content", "owner", "active_form"):
        if field in args and not isinstance(args[field], str):
            raise TaskServiceError("invalid_argument", f"{field} must be text")
    if "priority" in args and (type(args["priority"]) is not int or not 1 <= args["priority"] <= 5):
        raise TaskServiceError("invalid_argument", "priority must be an integer from 1 to 5")
    if "status" in args and args["status"] not in tasks.VALID_STATUSES:
        raise TaskServiceError("invalid_argument", "Invalid task status")
    if "scope" in args and args["scope"] not in tasks.VALID_SCOPES:
        raise TaskServiceError("invalid_argument", "Invalid task scope")
    if "effort" in args and args["effort"] not in (*tasks.VALID_EFFORTS, "", None):
        raise TaskServiceError("invalid_argument", "Invalid effort")
    if "expected_version" in args and (type(args["expected_version"]) is not int or args["expected_version"] < 0):
        raise TaskServiceError("invalid_argument", "expected_version must be a nonnegative integer")


def _apply(store, operation: str, args: dict, scope: dict) -> dict:
    unknown = set(args) - _ARGUMENTS[operation] - {"operation_id"}
    if unknown:
        raise TaskServiceError("unsupported_argument", "Unsupported task arguments: " + ", ".join(sorted(unknown)))
    if operation == "list":
        status = args.get("status", "all")
        if status not in (*tasks.VALID_STATUSES, "all"):
            raise TaskServiceError("invalid_argument", "Invalid task status")
        limit = args.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise TaskServiceError("invalid_argument", "limit must be between 1 and 500")
        cursor = args.get("cursor", "")
        if not isinstance(cursor, str):
            raise TaskServiceError("invalid_argument", "cursor must be text")
        found = sorted((node for node in tasks.list_tasks(store, status=status, limit=None)
                        if _in_scope(node, scope, store) and node["id"] > cursor), key=lambda node: node["id"])
        return {"tasks": [task_record(node) for node in found[:limit]],
                "next_cursor": found[limit - 1]["id"] if len(found) > limit else None}
    if operation == "get":
        return {"task": task_record(_get(store, args.get("id"), scope))}
    if operation == "reconcile":
        return _reconcile(store, args, scope)
    _validate_fields(args)
    if "dependencies" in args:
        for dependency in _strings(args["dependencies"], "dependencies"):
            _get(store, dependency, scope)
    if operation == "create":
        if not args.get("title", "").strip():
            raise TaskServiceError("invalid_argument", "Task title is required")
        if args.get("scope") == "global" and not scope["include_global"]:
            raise TaskServiceError("scope_mismatch", "Global task creation requires explicit global scope")
        if args.get("external_id"):
            _identifier(args["external_id"], "external_id")
            namespace = _identifier(args.get("namespace"), "namespace")
            for prior in tasks.list_tasks(store, status="all", limit=None):
                extra = prior.get("extra") or {}
                if (_in_scope(prior, scope, store) and extra.get("conversation_id") == scope["session_id"]
                        and extra.get("task_namespace") == namespace and extra.get("external_id") == args["external_id"]):
                    raise TaskServiceError("external_id_conflict", "External task ID already exists in this collection")
        task_id = tasks.create_task(
            store, args["title"], content=args.get("content", ""),
            priority=args.get("priority", 3), due=args.get("due"),
            scope=args.get("scope", "contextual"), effort=args.get("effort"),
            link_to=_strings(args.get("link_to", []), "link_to"),
            domains=_strings(args.get("domains", []), "domains"),
            project_path=scope["project_path"], session_id=scope["session_id"],
            owner=args.get("owner", ""), dependencies=args.get("dependencies", []),
            external_id=args.get("external_id", ""), namespace=args.get("namespace", ""))
        if args.get("active_form") or args.get("status", "open") != "open":
            tasks.update_task(store, task_id, task_status=args.get("status", "open"),
                              active_form=args.get("active_form", ""))
        return {"task": task_record(tasks.get_task(store, task_id))}
    node = _get(store, args.get("id"), scope)
    task_id = node["id"]
    if "expected_version" in args and args["expected_version"] != int((node.get("extra") or {}).get("task_version", 0)):
        raise TaskServiceError("version_conflict", "Task changed; read it again before updating")
    if operation == "update":
        fields = {key: args[key] for key in ("title", "content", "priority", "due", "owner", "active_form", "effort", "dependencies") if key in args}
        if "status" in args:
            fields["task_status"] = args["status"]
        result = tasks.update_task(store, task_id, **fields)
    elif operation in ("complete", "cancel"):
        result = tasks.update_task(store, task_id, task_status="done" if operation == "complete" else "cancelled")
    elif operation in ("claim", "release"):
        agent = _identifier(scope.get("agent"), "scope.agent")
        if operation == "claim":
            ttl = args.get("ttl_minutes", 120)
            if type(ttl) is not int or ttl <= 0:
                raise TaskServiceError("invalid_argument", "ttl_minutes must be positive")
            result = tasks.claim_task(store, task_id, agent, ttl_minutes=ttl,
                                      note=args.get("note", ""), force=args.get("force") is True)
        else:
            result = tasks.release_task_claim(store, task_id, agent=agent, force=args.get("force") is True)
    else:
        raise TaskServiceError("invalid_operation", "Unknown task operation")
    return {"task": task_record(result)}


def _reconcile(store, args: dict, scope: dict) -> dict:
    """Reconcile only this session's named TodoWrite collection, atomically."""
    namespace = _identifier(args.get("namespace", "todos"), "namespace")
    items = args.get("items")
    if not isinstance(items, list) or len(items) > 500:
        raise TaskServiceError("invalid_argument", "items must be a list of at most 500 tasks")
    external_ids = []
    for item in items:
        if not isinstance(item, dict):
            raise TaskServiceError("invalid_argument", "Each item must be an object")
        if set(item) - _FIELDS - {"external_id", "expected_version"}:
            raise TaskServiceError("unsupported_argument", "Unsupported task collection item fields")
        external_ids.append(_identifier(item.get("external_id"), "external_id"))
    if len(set(external_ids)) != len(external_ids):
        raise TaskServiceError("invalid_argument", "Duplicate external_id in task collection")
    owned = {node["extra"]["external_id"]: node for node in tasks.list_tasks(store, status="all", limit=None)
             if _in_scope(node, scope, store) and (node.get("extra") or {}).get("conversation_id") == scope["session_id"]
             and (node.get("extra") or {}).get("task_namespace") == namespace
             and (node.get("extra") or {}).get("external_id")}
    results = []
    for item in items:
        existing = owned.get(item["external_id"])
        fields = dict(item)
        if existing:
            fields.pop("external_id")
            fields["id"] = existing["id"]
            results.append(_apply(store, "update", fields, scope)["task"])
        else:
            fields["namespace"] = namespace
            results.append(_apply(store, "create", fields, scope)["task"])
    cancelled = []
    if args.get("cancel_missing") is True:
        for external_id, node in owned.items():
            if external_id not in external_ids and node["extra"].get("task_status") in ("open", "in_progress"):
                cancelled.append(task_record(tasks.cancel_task(store, node["id"])))
    return {"tasks": results, "cancelled": cancelled, "namespace": namespace}


def execute(store, operation: str, args: dict, scope: dict) -> dict:
    """Execute once; retries return the committed result without reapplying it.

    Mutations require args.operation_id. That ID is bound to operation, sanitized
    arguments and explicit scope. Reusing it for different input is a conflict.
    Task writes, activity and the successful receipt commit in one transaction.
    Error responses do not persist partial work. get/list need no operation ID.
    """
    operation_id = redact(args.get("operation_id")) if isinstance(args, dict) else None
    try:
        if operation not in MUTATIONS | {"get", "list"}:
            raise TaskServiceError("invalid_operation", "Unknown task operation")
        if not isinstance(args, dict):
            raise TaskServiceError("invalid_argument", "args must be an object")
        scoped = _scope(store, scope)
        clean = redact(args)
        if operation not in MUTATIONS:
            return {"ok": True, **_apply(store, operation, clean, scoped)}
        if store.conn.in_transaction:
            raise TaskServiceError("transaction_conflict", "Task service must own its commit transaction")
        operation_id = _identifier(clean.get("operation_id"), "operation_id")
        auth, auth_binding = _authorization(scope.get("authorization_receipt"), operation_id)
        suffix = _operation_key(scoped, operation_id)
        key = "task.operation." + suffix
        digest = _request_digest(operation, clean, scoped)
        with tasks.transaction(store):
            saved = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if saved:
                receipt = json.loads(saved["value"])
                if receipt["request_digest"] != digest:
                    raise TaskServiceError("operation_conflict", "operation_id already committed with different arguments")
                if auth_binding is not None and receipt.get("authorization_binding") != auth_binding:
                    raise TaskServiceError("authorization_mismatch", "Operation already committed under different authorization")
                return {**receipt["result"], "replayed": True}
            result = redact({"ok": True, "operation_id": operation_id, "replayed": False,
                             **_apply(store, operation, clean, scoped)})
            store.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                               (key, _json({"request_digest": digest, "result": result,
                                            "authorization_binding": auth_binding})))
            if auth is not None:
                store.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (
                    "task.outcome." + suffix,
                    _json({"operation_id": operation_id, "scope": scoped,
                           "authorization_receipt": auth, "result": result,
                           "acknowledged": False})))
        return result
    except TaskServiceError as exc:
        return {"ok": False, "operation_id": operation_id,
                "error": {"code": exc.code, "message": safe_error(exc)}}
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "operation_id": operation_id,
                "error": {"code": "invalid_argument", "message": safe_error(exc)}}
    except sqlite3.Error:
        return {"ok": False, "operation_id": operation_id,
                "error": {"code": "unavailable", "message": "Durable task storage is unavailable; no task fallback was created"}}


def pending_outcomes(store, scope: dict) -> list[dict]:
    """Undelivered effect outcomes; retry delivery without repeating the effect."""
    scoped = _scope(store, scope)
    result = []
    for row in store.conn.execute(
        """SELECT value FROM meta WHERE key LIKE 'task.outcome.%'
           AND json_extract(value,'$.acknowledged')=0
           AND json_extract(value,'$.scope')=? ORDER BY key""", (_json(scoped),)):
        outcome = json.loads(row["value"])
        if outcome["scope"] == scoped and not outcome["acknowledged"]:
            result.append(outcome)
    return result


def acknowledge_outcome(store, scope: dict, operation_id: str) -> bool:
    """Mark delivery acknowledged, retaining the original receipt and effect."""
    scoped = _scope(store, scope)
    key = "task.outcome." + _operation_key(scoped, _identifier(operation_id, "operation_id"))
    with tasks.transaction(store):
        row = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        outcome = json.loads(row["value"])
        outcome["acknowledged"] = True
        store.conn.execute("UPDATE meta SET value=? WHERE key=?", (_json(outcome), key))
    return True
