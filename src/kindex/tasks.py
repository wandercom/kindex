"""Graph-connected task lifecycle for Kindex.

Tasks are first-class graph nodes (type='task') with structured metadata
in the extra JSON field. They surface contextually via BFS traversal
through the knowledge graph -- linked concepts propagate task visibility.
"""

from __future__ import annotations

import datetime
from collections import deque
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
        return datetime.datetime.fromisoformat(expires) <= _now_dt()
    except ValueError:
        return False


def _parse_priority(val: int | str) -> int:
    """Accept int 1-5 or label string, return int."""
    if isinstance(val, str):
        val = LABEL_TO_PRIORITY.get(val.lower(), 3)
    return max(1, min(5, int(val)))


def compute_task_weight(priority: int, due: str | None = None) -> float:
    """Compute node weight from priority and due-date urgency.

    Returns float in [0.01, 1.0]. Higher = more important.
    """
    base = PRIORITY_WEIGHTS.get(priority, 0.5)

    if due:
        try:
            due_dt = datetime.datetime.fromisoformat(due)
            hours_until = (due_dt - datetime.datetime.now()).total_seconds() / 3600
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
) -> str:
    """Create a task node and optionally link it to existing nodes."""
    pri = _parse_priority(priority)
    weight = compute_task_weight(pri, due)

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
        extra["project_path"] = project_path

    task_id = store.add_node(
        title,
        content=content,
        node_type="task",
        weight=weight,
        domains=domains,
        extra=extra,
    )

    # Link to specified nodes
    if link_to:
        for ref in link_to:
            target = store.get_node(ref) or store.get_node_by_title(ref)
            if target:
                store.add_edge(task_id, target["id"], "context_of", weight=0.6)

    return task_id


def complete_task(store: Store, task_id: str) -> dict | None:
    """Mark a task as done."""
    node = store.get_node(task_id)
    if not node or node.get("type") != "task":
        return None

    extra = node.get("extra") or {}
    extra["task_status"] = "done"
    extra["completed_at"] = _now()
    extra.pop("claim", None)

    store.update_node(task_id, status="archived", extra=extra, weight=0.01)
    return store.get_node(task_id)


def cancel_task(store: Store, task_id: str) -> dict | None:
    """Cancel a task."""
    node = store.get_node(task_id)
    if not node or node.get("type") != "task":
        return None

    extra = node.get("extra") or {}
    extra["task_status"] = "cancelled"
    extra.pop("claim", None)

    store.update_node(task_id, status="archived", extra=extra, weight=0.01)
    return store.get_node(task_id)


def update_task(store: Store, task_id: str, **fields) -> dict | None:
    """Update task-specific fields: priority, task_status, due, effort, scope."""
    node = store.get_node(task_id)
    if not node or node.get("type") != "task":
        return None

    node_updates = {}

    def _mutate(extra: dict) -> None:
        # Field changes apply to the fresh in-transaction extra so a claim
        # (or any extra key) committed concurrently is never clobbered by
        # this caller's stale snapshot.
        if "priority" in fields:
            extra["priority"] = _parse_priority(fields["priority"])
        if "task_status" in fields and fields["task_status"] in VALID_STATUSES:
            extra["task_status"] = fields["task_status"]
            if fields["task_status"] == "done":
                extra["completed_at"] = _now()
                node_updates["status"] = "archived"
        if "due" in fields:
            extra["due"] = fields["due"]
        if "effort" in fields:
            extra["effort"] = fields["effort"]
        if "scope" in fields and fields["scope"] in VALID_SCOPES:
            extra["scope"] = fields["scope"]

        # Recalculate weight from the merged state
        weight = compute_task_weight(extra.get("priority", 3), extra.get("due"))
        if extra.get("task_status") in ("done", "cancelled"):
            weight = 0.01
        node_updates["weight"] = weight

    store.atomic_extra_update(task_id, _mutate)
    # Weight/status follow-up deliberately does not pass extra.
    store.update_node(task_id, **node_updates)
    return store.get_node(task_id)


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
    node = store.get_node(task_id)
    if not node or node.get("type") != "task":
        return None
    if not agent.strip():
        raise ValueError("Agent is required")

    def _mutate(extra: dict) -> None:
        # The conflict check runs inside BEGIN IMMEDIATE on the fresh extra
        # (mirrors locks.lock_node) so two agents cannot both acquire the
        # claim from stale snapshots — the loser's ValueError propagates
        # and rolls back.
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

    store.atomic_extra_update(task_id, _mutate)
    return store.get_node(task_id)


def release_task_claim(store: Store, task_id: str, *, agent: str = "", force: bool = False) -> dict | None:
    """Release a task claim. Non-forced releases must match the claiming agent."""
    node = store.get_node(task_id)
    if not node or node.get("type") != "task":
        return None

    def _mutate(extra: dict) -> None:
        # Agent-match check on the fresh in-transaction claim: a claim
        # re-acquired by another agent after this caller's snapshot must
        # not be silently dropped.
        claim = extra.get("claim")
        if not claim:
            return
        if agent and claim.get("agent") != agent and not force:
            raise ValueError(
                f"Task claimed by {claim.get('agent', 'unknown')}")
        extra.pop("claim", None)
        if extra.get("task_status") == "in_progress":
            extra["task_status"] = "open"

    store.atomic_extra_update(task_id, _mutate)
    return store.get_node(task_id)


def cleanup_expired_claims(store: Store) -> int:
    """Release expired task claims and return the number cleaned up.

    list_tasks is only a prefilter: each release re-checks expiry inside
    the atomic update (mirrors locks.cleanup_expired_locks) so a claim
    refreshed mid-sweep is not dropped.
    """
    count = 0
    for task in list_tasks(store, status="in_progress", limit=500):
        if not _claim_expired((task.get("extra") or {}).get("claim")):
            continue

        released = {"done": False}

        def _mutate(extra: dict, _flag: dict = released) -> None:
            if _claim_expired(extra.get("claim")):
                extra.pop("claim", None)
                extra["task_status"] = "open"
                _flag["done"] = True

        store.atomic_extra_update(task["id"], _mutate)
        if released["done"]:
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
    limit: int = 20,
) -> list[dict]:
    """List tasks with filters. Returns sorted by weight DESC, then due date."""
    node_status = "active" if status in ("open", "in_progress") else None
    if status == "all":
        node_status = None
    tasks = store.all_nodes(node_type="task", status=node_status, limit=500)

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

        # Domain filter
        if domain:
            node_domains = t.get("domains") or []
            if domain.lower() not in [d.lower() for d in node_domains]:
                continue

        # Project path filter
        if project_path and extra.get("project_path"):
            if not project_path.startswith(extra["project_path"]):
                continue

        result.append(t)

    # Sort: weight DESC, then due date ASC (None last)
    def sort_key(t):
        extra = t.get("extra") or {}
        due = extra.get("due", "9999")
        return (-t.get("weight", 0), due)

    result.sort(key=sort_key)
    return result[:limit]


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
