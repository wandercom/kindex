"""Explicit local host adapters shared by CLI and MCP.

Requests name evidence, never permissions. Host configuration and environment
select the receiving project/session; no provider or scheduler is invoked.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from . import practical_memory as memory
from . import correction_references as references
from .agent_adapters import ADAPTER_SCOPE_NAMES, normalize_adapter
from .config import _resolve_path
from .store import Store


class PracticalMemoryCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    action: Literal["prepare", "audit", "organize", "persist", "index", "ack"]
    request: dict[str, Any]


class CorrectionReferenceCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    operation: Literal["reference", "expand", "correction_add", "correction_retire", "dream_import"]
    request: dict[str, Any]
    metadata_encoding: Literal["full", "lossless_common"] = "full"
    incoming_later: bool = False


class EvidenceEnvelope(RootModel[dict[str, Any]]):
    """Strict boundary; version-specific canonical validators own the wire shape."""
    model_config = ConfigDict(strict=True, frozen=True)


def _config(config, project_path, error):
    cfg = config.model_copy(deep=True)
    cfg._stamp_on_open = False
    selected = project_path or os.environ.get("KIN_PROJECT_PATH") or os.environ.get("KIN_PROJECT")
    if selected:
        project = _resolve_path(selected)
        if project.is_file():
            raise error
        cfg._project_path = project
    return cfg


def _envelope(result, canonical):
    # Do not model_dump/coerce canonical wire bytes or opaque versioned fields.
    EvidenceEnvelope.model_validate(result, strict=True)
    canonical(result)
    return result


def _host_identity(error):
    """Bind native session identity separately from provider conversation IDs."""
    session_id = os.environ.get("KIN_SESSION_ID")
    codex = os.environ.get("CODEX_THREAD_ID")
    claude = os.environ.get("CLAUDE_SESSION_ID")
    if not session_id or (codex and claude):
        raise error
    inferred = normalize_adapter("codex") if codex else normalize_adapter("claude") if claude else None
    declared = normalize_adapter(os.environ["KIN_CLIENT"]) if os.environ.get("KIN_CLIENT") else None
    if inferred and declared and inferred != declared:
        raise error
    client = inferred or declared
    if client not in ADAPTER_SCOPE_NAMES:
        raise error
    return session_id, client


def run_practical_memory(config, action, request, *, project_path=None):
    try:
        call = PracticalMemoryCall(action=action, request=request)
    except ValidationError:
        raise memory.PracticalMemoryError("invalid_request") from None
    memory._c(request)
    cfg = _config(config, project_path, memory.PracticalMemoryError("scope_mismatch"))
    session_id, client = _host_identity(memory.PracticalMemoryError("scope_mismatch"))
    memory._scope_shape(request.get("scope"))
    if (getattr(cfg, "_project_path", None) is None
            or request["scope"]["session_id"] != session_id
            or request["scope"]["client"] != client):
        raise memory.PracticalMemoryError("scope_mismatch")
    store = Store(cfg, read_only=True, migrate=False, manage_project_storage=False)
    try:
        memory._scope_shape(request.get("scope"))
        memory._scope(store, request["scope"])
        if call.action not in ("prepare", "audit", "organize"):
            store.close()
            store = Store(cfg, migrate=False, manage_project_storage=False)
        return _envelope(getattr(memory, call.action)(store, request), memory._c)
    finally:
        store.close()


def run_correction_references(config, operation, request, *, project_path=None,
                              metadata_encoding="full", incoming_later=False,
                              recorded_at=None):
    try:
        call = CorrectionReferenceCall(operation=operation, request=request,
            metadata_encoding=metadata_encoding, incoming_later=incoming_later)
    except ValidationError:
        raise references.ReferenceError("invalid_request") from None
    references._j(request)
    if operation != "expand" and metadata_encoding != "full":
        raise references.ReferenceError("invalid_request")
    if operation not in ("reference", "expand") and incoming_later:
        raise references.ReferenceError("invalid_request")
    cfg = _config(config, project_path, references.ReferenceError("invalid_context"))
    project, actor = getattr(cfg, "_project_path", None), getattr(cfg, "user", None)
    if project is None or type(actor) is not str or not actor.strip():
        raise references.ReferenceError("invalid_context")
    project = str(project)
    session_id, client = _host_identity(references.ReferenceError("invalid_context"))
    instant = recorded_at or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    store = Store(cfg, read_only=True, migrate=False, manage_project_storage=False)
    try:
        graph = store.conn.execute("SELECT value FROM meta WHERE key='graph_id'").fetchone()
        if graph is None:
            raise references.ReferenceError("invalid_context")
        partitions = [{"partition_id": "current", "kind": "repo", "project_path": project, "kinbase": None}]
        policy = {"policy_id": "cli-current-session-v1", "graph_id": graph[0],
                  "profile": cfg.active_profile or "", "session_id": session_id,
                  "current_project": project, "partitions": partitions, "allowed_actor_ids": [actor]}
        shape = {key: policy[key] for key in ("graph_id", "profile", "session_id", "current_project", "partitions")}
        shape.update(client=client, correction_policy={"policy_id": policy["policy_id"],
                     "policy_sha256": references._h(references._j(policy)), "allowed_actor_ids": [actor]})
        context = references.ReadContext(**shape)
        references._owner(store, context.to_dict())
        if call.operation == "reference":
            result = references.resolve_references(store, request, context=context, incoming_later=incoming_later)
        elif call.operation == "expand":
            request = {**request, "include_history": request.get("include_history", False)}
            result = references.expand_references(store, request, context=context,
                metadata_encoding=metadata_encoding, incoming_later=incoming_later)
        else:
            context = references.WriteContext(**shape, actor_id=actor, recorded_at=instant, explicit_action=True)
            store.close()
            store = Store(cfg, migrate=False, manage_project_storage=False)
            if call.operation == "correction_add":
                result = references.record_correction(store, request, context=context)
            elif call.operation == "correction_retire":
                references._obj(request, ("assertion_id", "reason"))
                result = references.retire_correction(store, request["assertion_id"], context=context,
                                                     reason=request["reason"], recorded_at=instant)
            else:
                result = references.record_dream_index(store, request, context=context)
        return _envelope(result, references._j)
    finally:
        store.close()
