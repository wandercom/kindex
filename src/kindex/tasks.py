"""Graph-connected task lifecycle for Kindex.

Tasks are first-class graph nodes (type='task') with structured metadata
in the extra JSON field. They surface contextually via BFS traversal
through the knowledge graph -- linked concepts propagate task visibility.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

PRIORITY_LABELS = {1: "urgent", 2: "high", 3: "normal", 4: "low", 5: "someday"}
LABEL_TO_PRIORITY = {v: k for k, v in PRIORITY_LABELS.items()}
PRIORITY_WEIGHTS = {1: 0.9, 2: 0.7, 3: 0.5, 4: 0.3, 5: 0.1}

VALID_STATUSES = ("open", "in_progress", "done", "cancelled")
VALID_SCOPES = ("global", "contextual")
VALID_EFFORTS = ("small", "medium", "large")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now()


def _claim_expires_at(ttl_minutes: int | None) -> str:
    ttl = 120 if ttl_minutes is None else ttl_minutes
    return (_now_dt() + datetime.timedelta(minutes=ttl)).isoformat(timespec="seconds")


def _claim_expired(claim: dict | None) -> bool:
    if not claim:
        return False
    expires = claim.get("expires_at")
    if not expires:
        return False
    try:
        expiry = datetime.datetime.fromisoformat(expires)
        return expiry <= datetime.datetime.now(expiry.tzinfo)
    except (ValueError, TypeError):
        return False


def _parse_priority(val: int | str) -> int:
    """Accept int 1-5 or label string, return int."""
    if isinstance(val, str):
        val = int(val) if val.isdigit() else LABEL_TO_PRIORITY.get(val.lower(), 3)
    return max(1, min(5, int(val)))


def compute_task_weight(priority: int, due: str | None = None) -> float:
    """Compute node weight from priority and due-date urgency.

    Returns float in [0.01, 1.0]. Higher = more important.
    """
    base = PRIORITY_WEIGHTS.get(priority, 0.5)

    if due:
        try:
            due_dt = datetime.datetime.fromisoformat(due)
            hours_until = (due_dt - datetime.datetime.now(due_dt.tzinfo)).total_seconds() / 3600
            if hours_until <= 0:
                base += 0.2  # overdue
            elif hours_until <= 24:
                base += 0.15  # due today
            elif hours_until <= 72:
                base += 0.05  # due within 3 days
        except (ValueError, TypeError):
            pass

    return round(max(0.01, min(1.0, base)), 4)


# ── CRUD ──────────────────────────────────────────────────────────────


def normalize_due(value: str | None) -> str | None:
    """Normalize the documented natural-language and ISO task due dates."""
    if not value:
        return None
    try:
        datetime.datetime.fromisoformat(value)
        return value
    except (ValueError, TypeError):
        from .reminders import parse_time_spec
        due, _, kind = parse_time_spec(value)
        if kind != "once":
            raise ValueError("Task due date must be a one-time date")
        return due


@contextmanager
def transaction(store: Store):
    """Join a task-service transaction, or atomically commit a legacy operation."""
    conn = store.conn
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if own:
            conn.commit()
    except BaseException:
        if own:
            conn.rollback()
        raise


def get_task(store: Store, task_id: str) -> dict | None:
    row = store.conn.execute(
        "SELECT * FROM nodes WHERE id = ? AND type = 'task'", (task_id,)
    ).fetchone()
    return store._row_to_dict(row) if row else None


def _write_task(store: Store, node: dict, *, create: bool = False) -> dict:
    """Persist all task state and its audit row in the caller's transaction."""
    from .privacy import redact, redact_text
    node["title"] = redact_text(node["title"])
    node["content"] = redact_text(node.get("content") or "")
    extra = redact(node.get("extra") or {})
    status = extra.get("task_status", "open")
    node["status"] = "archived" if status in ("done", "cancelled") else "active"
    extra["task_version"] = int(extra.get("task_version", 0)) + 1
    node["extra"] = extra
    weight = 0.01 if node["status"] == "archived" else compute_task_weight(
        extra.get("priority", 3), extra.get("due"))
    now = _now()
    domains = redact(node.get("domains") or [])
    values = (node["title"], node["content"], weight, json.dumps(domains),
              node["status"], node.get("audience", "private"), json.dumps(extra), now)
    if create:
        store.conn.execute(
            """INSERT INTO nodes (title,content,weight,domains,status,audience,extra,
               updated_at,id,type,created_at,last_accessed,prov_when)
               VALUES (?,?,?,?,?,?,?,?,?,'task',?,?,?)""",
            (*values, node["id"], now, now, now))
    else:
        store.conn.execute(
            """UPDATE nodes SET title=?,content=?,weight=?,domains=?,status=?,
               audience=?,extra=?,updated_at=? WHERE id=?""", (*values, node["id"]))
    store._log_in_transaction(store.conn, "add_node" if create else "update_node",
                              node["id"], node["title"],
                              details={"task_version": extra["task_version"]})
    from .vectors import enqueue_embedding
    enqueue_embedding(store, node["id"], commit=False)
    return get_task(store, node["id"])


def _dependencies(store: Store, task_id: str, refs: list[str]) -> list[str]:
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValueError("Task dependencies must be a list of IDs")
    result = list(dict.fromkeys(refs))
    for ref in result:
        if ref == task_id or get_task(store, ref) is None:
            raise ValueError(f"Invalid task dependency: {ref}")
        pending, seen = [ref], set()
        while pending:
            current = pending.pop()
            if current == task_id:
                raise ValueError("Task dependencies must not contain a cycle")
            if current in seen:
                continue
            seen.add(current)
            node = get_task(store, current)
            if node:
                pending.extend((node.get("extra") or {}).get("dependencies", []))
    return result


def create_task(
    store: Store,
    title: str,
    *,
    content: str = "",
    priority: int | str = 3,
    due: str | None = None,
    scope: str = "contextual",
    effort: str | None = None,
    link_to: list[str] | None = None,
    domains: list[str] | None = None,
    project_path: str | None = None,
    session_id: str | None = None,
    owner: str = "",
    dependencies: list[str] | None = None,
    audience: str = "private",
    external_id: str = "",
    namespace: str = "",
) -> str:
    """Create a task node and optionally link it to existing nodes."""
    pri = _parse_priority(priority)
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Task title is required")
    due = normalize_due(due)

    extra = {
        "task_status": "open",
        "priority": pri,
        "scope": scope if scope in VALID_SCOPES else "contextual",
    }
    if due:
        extra["due"] = due
    if effort and effort in VALID_EFFORTS:
        extra["effort"] = effort
    if project_path:
        extra["project_path"] = str(Path(project_path).resolve())
    if session_id:
        extra["conversation_id"] = session_id
    if owner:
        extra["owner"] = owner
    if external_id:
        extra["external_id"] = external_id
    if namespace:
        extra["task_namespace"] = namespace
    with transaction(store):
        # Validate every target before minting a node; failed linking is atomic.
        targets = []
        for ref in link_to or []:
            # Store.get_node touches last_accessed and commits. Task mutation
            # must retain its write lock through the operation receipt.
            row = store.conn.execute("SELECT id FROM nodes WHERE id=?", (ref,)).fetchone()
            target = {"id": row["id"]} if row else store.get_node_by_title(ref)
            if not target:
                raise ValueError(f"Task link target not found: {ref}")
            targets.append(target["id"])
        task_id = uuid.uuid4().hex[:12]
        extra["dependencies"] = _dependencies(store, task_id, dependencies or [])
        _write_task(store, {"id": task_id, "title": title, "content": content,
                           "domains": domains or [], "audience": audience,
                           "extra": extra}, create=True)
        for target in set(targets):
            for left, right in ((task_id, target), (target, task_id)):
                store.conn.execute(
                    """INSERT INTO edges (from_id,to_id,type,weight,created_at)
                       VALUES (?,?,'context_of',0.6,?)""", (left, right, _now()))
        return task_id


def complete_task(store: Store, task_id: str) -> dict | None:
    """Mark a task as done."""
    return update_task(store, task_id, task_status="done")


def cancel_task(store: Store, task_id: str) -> dict | None:
    """Cancel a task."""
    return update_task(store, task_id, task_status="cancelled")


def update_task(store: Store, task_id: str, **fields) -> dict | None:
    """Update task-specific fields: priority, task_status, due, effort, scope."""
    with transaction(store):
        node = get_task(store, task_id)
        if not node:
            return None
        before = json.dumps(node, sort_keys=True)
        extra = node.get("extra") or {}
        expected = fields.get("expected_version")
        if expected is not None and expected != int(extra.get("task_version", 0)):
            raise ValueError("Task version conflict")
        if "priority" in fields:
            extra["priority"] = _parse_priority(fields["priority"])
        if "task_status" in fields:
            status = fields["task_status"]
            if status not in VALID_STATUSES:
                raise ValueError(f"Invalid task status: {status}")
            if status == "done":
                extra.setdefault("completed_at", _now())
            else:
                extra.pop("completed_at", None)
            if status in ("done", "cancelled", "open"):
                extra.pop("claim", None)
            extra["task_status"] = status
        if "due" in fields:
            due = normalize_due(fields["due"])
            if due:
                extra["due"] = due
            else:
                extra.pop("due", None)
        if "effort" in fields:
            extra["effort"] = fields["effort"]
        if "scope" in fields and fields["scope"] in VALID_SCOPES:
            extra["scope"] = fields["scope"]
        for field in ("owner", "active_form"):
            if field in fields:
                extra[field] = fields[field]
        if "dependencies" in fields:
            extra["dependencies"] = _dependencies(store, task_id, fields["dependencies"])
        for field in ("title", "content"):
            if field in fields:
                if field == "title" and not str(fields[field]).strip():
                    raise ValueError("Task title is required")
                node[field] = fields[field]
        node["extra"] = extra
        if json.dumps(node, sort_keys=True) == before:
            return node
        return _write_task(store, node)


def claim_task(
    store: Store,
    task_id: str,
    agent: str,
    *,
    ttl_minutes: int = 120,
    note: str = "",
    force: bool = False,
) -> dict | None:
    """Claim a task for an agent with an expiry to avoid stale locks."""
    if not agent.strip():
        raise ValueError("Agent is required")
    with transaction(store):
        node = get_task(store, task_id)
        if not node:
            return None
        extra = node.get("extra") or {}
        if extra.get("task_status") in ("done", "cancelled"):
            raise ValueError("Cannot claim a completed or cancelled task; reopen it first")
        existing = extra.get("claim")
        if existing and not _claim_expired(existing) and not force:
            owner = existing.get("agent", "unknown")
            raise ValueError(f"Task already claimed by {owner}")
        extra["task_status"] = "in_progress"
        extra["claim"] = {
            "agent": agent.strip(),
            "claimed_at": _now(),
            "expires_at": _claim_expires_at(ttl_minutes),
            "note": note,
        }

        node["extra"] = extra
        return _write_task(store, node)


def release_task_claim(store: Store, task_id: str, *, agent: str = "", force: bool = False) -> dict | None:
    """Release a task claim. Non-forced releases must match the claiming agent."""
    with transaction(store):
        node = get_task(store, task_id)
        if not node:
            return None
        extra = node.get("extra") or {}
        claim = extra.get("claim")
        if not claim:
            return node
        if claim.get("agent") != agent and not force:
            raise ValueError(
                f"Task claimed by {claim.get('agent', 'unknown')}")
        extra.pop("claim", None)
        if extra.get("task_status") == "in_progress":
            extra["task_status"] = "open"

        node["extra"] = extra
        return _write_task(store, node)


def cleanup_expired_claims(store: Store) -> int:
    """Release expired task claims and return the number cleaned up.

    list_tasks is only a prefilter: each release re-checks expiry inside
    the atomic update (mirrors locks.cleanup_expired_locks) so a claim
    refreshed mid-sweep is not dropped.
    """
    count = 0
    for task in list_tasks(store, status="in_progress", limit=None):
        if not _claim_expired((task.get("extra") or {}).get("claim")):
            continue

        with transaction(store):
            fresh = get_task(store, task["id"])
            if fresh and _claim_expired((fresh.get("extra") or {}).get("claim")):
                release_task_claim(store, task["id"], force=True)
                count += 1
    return count


# ── Queries ───────────────────────────────────────────────────────────


def list_tasks(
    store: Store,
    *,
    status: str = "open",
    scope: str | None = None,
    domain: str | None = None,
    project_path: str | None = None,
    limit: int | None = 20,
    conversation_id: str | None = None,
    max_priority: int | None = None,
) -> list[dict]:
    """List tasks with filters. Returns sorted by weight DESC, then due date."""
    if status not in (*VALID_STATUSES, "all"):
        raise ValueError(f"Invalid task status: {status}")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("Task list limit must be positive")
    node_status = "active" if status in ("open", "in_progress") else None
    if status == "all":
        node_status = None
    # Filter before limiting: a global high-priority backlog must not hide local work.
    query = "SELECT * FROM nodes WHERE type = 'task'"
    params = []
    if node_status:
        query += " AND status = ?"
        params.append(node_status)
    tasks = [store._row_to_dict(row) for row in store.conn.execute(query, params)]

    result = []
    for t in tasks:
        extra = t.get("extra") or {}
        ts = extra.get("task_status", "open")

        # Status filter
        if status != "all" and ts != status:
            continue

        # Scope filter
        if scope and extra.get("scope") != scope:
            continue
        if max_priority is not None and extra.get("priority", 3) > max_priority:
            continue
        if conversation_id and extra.get("conversation_id") != conversation_id:
            continue

        # Domain filter
        if domain:
            node_domains = t.get("domains") or []
            if domain.lower() not in [d.lower() for d in node_domains]:
                continue

        # Project path filter
        if project_path and extra.get("project_path"):
            current = Path(project_path).resolve()
            target = Path(extra["project_path"]).resolve()
            if current != target and target not in current.parents:
                continue

        result.append(t)

    # Sort: weight DESC, then due date ASC (None last)
    def sort_key(t):
        extra = t.get("extra") or {}
        due = extra.get("due") or "9999"
        return (-t.get("weight", 0), due)

    result.sort(key=sort_key)
    return result if limit is None else result[:limit]


# ── Graph traversal ───────────────────────────────────────────────────


def store_bfs(
    store: Store,
    seeds: list[str],
    max_hops: int = 2,
    min_weight: float = 0.1,
    type_filter: str | None = None,
) -> list[dict]:
    """BFS over Store edges with multiplicative weight decay.

    Traverses through ALL node types but can filter results to only
    include nodes of a specific type. This allows discovery through
    intermediate concepts (kitchen -> cooking-dinner -> stir-soup).

    Returns list of dicts with id, title, type, depth, proximity,
    sorted by proximity descending.
    """
    visited = set(seeds)
    frontier = deque((seed, 0, 1.0) for seed in seeds)
    results = []

    while frontier:
        node_id, depth, cum_weight = frontier.popleft()
        if depth >= max_hops:
            continue

        # Follow edges in both directions
        edges = store.edges_from(node_id, semantic_only=True)
        try:
            edges += store.edges_to(node_id, semantic_only=True)
        except AttributeError:
            pass

        for edge in edges:
            # Determine the neighbor
            target = edge["to_id"] if edge.get("from_id") == node_id else edge.get("from_id", edge["to_id"])
            new_weight = cum_weight * edge.get("weight", 0.5)

            if target in visited or new_weight < min_weight:
                continue
            visited.add(target)

            node = store.get_node(target)
            if not node:
                continue

            if not type_filter or node.get("type") == type_filter:
                results.append({
                    "id": target,
                    "title": node.get("title", ""),
                    "type": node.get("type", ""),
                    "depth": depth + 1,
                    "proximity": new_weight,
                    "weight": node.get("weight", 0),
                    "extra": node.get("extra") or {},
                })

            # Always continue traversal regardless of type match
            frontier.append((target, depth + 1, new_weight))

    results.sort(key=lambda r: r["proximity"], reverse=True)
    return results


def nearby_tasks(
    store: Store,
    seed_ids: list[str],
    max_hops: int = 2,
) -> list[dict]:
    """Find open/in-progress tasks reachable from seed nodes via BFS.

    Returns tasks sorted by proximity * weight (graph-boosted priority).
    """
    if not seed_ids:
        return []

    hits = store_bfs(store, seed_ids, max_hops=max_hops, type_filter="task")

    # Filter to actionable tasks
    results = []
    for h in hits:
        ts = h["extra"].get("task_status", "open")
        if ts in ("open", "in_progress"):
            h["score"] = h["proximity"] * h["weight"]
            results.append(h)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ── Formatting ────────────────────────────────────────────────────────


def format_task(task: dict) -> str:
    """Format a single task for display."""
    extra = task.get("extra") or {}
    pri = extra.get("priority", 3)
    p_label = PRIORITY_LABELS.get(pri, "normal")
    ts = extra.get("task_status", "open")
    due = extra.get("due", "")
    effort = extra.get("effort", "")
    scope = extra.get("scope", "contextual")

    lines = [
        f"  {task.get('id', '?')}: {task.get('title', '?')} ({ts})",
        f"    Priority: {p_label} ({pri})  |  Scope: {scope}  |  Weight: {task.get('weight', 0):.2f}",
    ]
    if due:
        lines.append(f"    Due: {due}")
    if effort:
        lines.append(f"    Effort: {effort}")
    claim = extra.get("claim") or {}
    if claim:
        lines.append(
            f"    Claimed by: {claim.get('agent', '?')} until {claim.get('expires_at', '?')}"
        )
    if task.get("content"):
        lines.append(f"    Notes: {task['content'][:120]}")
    return "\n".join(lines)


def format_task_list(tasks: list[dict]) -> str:
    """Format a list of tasks for display."""
    if not tasks:
        return "No tasks found."
    lines = []
    for t in tasks:
        extra = t.get("extra") or {}
        pri = extra.get("priority", 3)
        p_tag = f"P{pri}"
        ts = extra.get("task_status", "open")
        due = extra.get("due", "")
        due_str = f" due:{due[:10]}" if due else ""
        scope = extra.get("scope", "contextual")
        scope_tag = " [global]" if scope == "global" else ""
        claim = extra.get("claim") or {}
        claim_str = f" claimed:{claim.get('agent')}" if claim else ""
        proximity = t.get("proximity")
        prox_str = f" prox={proximity:.2f}" if proximity is not None else ""

        lines.append(
            f"  [{p_tag}] {t.get('title', '?')}{due_str}{scope_tag}{claim_str}{prox_str}"
            f"  w={t.get('weight', 0):.2f}  {t.get('id', '?')[:12]}"
        )
    return "\n".join(lines)
