"""Short-lived coordination state for agents.

Coordination nodes are operational state, not durable knowledge. They live in
the same Store so MCP clients and CLI users can share them, but cleanup/end
paths clear message bodies and archive expired conversations.
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

_DEFAULT_TTL_MINUTES = 240


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now()


def _now() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _expires_at(ttl_minutes: int | None) -> str:
    ttl = _DEFAULT_TTL_MINUTES if ttl_minutes is None else ttl_minutes
    return (_now_dt() + datetime.timedelta(minutes=ttl)).isoformat(timespec="seconds")


def _ttl_from_extra(extra: dict) -> int:
    ttl = extra.get("ttl_minutes")
    if isinstance(ttl, int):
        return ttl

    # Legacy conversations did not store ttl_minutes. Preserve the configured
    # lifetime when it can be inferred from the original timestamps.
    try:
        created = datetime.datetime.fromisoformat(extra.get("created_at", ""))
        expires = datetime.datetime.fromisoformat(extra.get("expires_at", ""))
        inferred = int((expires - created).total_seconds() / 60)
        if inferred > 0:
            return inferred
    except (TypeError, ValueError):
        pass
    return _DEFAULT_TTL_MINUTES


def render_field(value: object, limit: int = 80) -> str:
    """Peer-supplied collab text as it may appear in hook context: one line,
    bounded, and unable to open or close a tag (an author, lock holder or
    message could otherwise end the context envelope or forge a heading)."""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("<", "\u2039").replace(">", "\u203a").replace("#", "\uff03")


def _slug(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    return name.strip("-")


def _is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.datetime.fromisoformat(value) <= _now_dt()
    except ValueError:
        return False


def _member_entry(agent: str) -> dict:
    return {"agent": agent, "joined_at": _now(), "last_read_id": 0}


def _members_of(extra: dict) -> list[dict]:
    return [m for m in (extra.get("members") or []) if isinstance(m, dict)]


def create_conversation(
    store: Store,
    name: str,
    *,
    task_id: str | None = None,
    ttl_minutes: int = 240,
    project_path: str | None = None,
    created_by: str = "",
) -> str:
    """Create a short-lived coordination conversation."""
    conv_name = _slug(name)
    if not conv_name:
        raise ValueError("Conversation name cannot be empty")

    extra = {
        "coord_kind": "conversation",
        "coord_status": "active",
        "name": conv_name,
        "task_id": task_id or "",
        "project_path": project_path or "",
        "created_by": created_by,
        "created_at": _now(),
        "ttl_minutes": ttl_minutes,
        "expires_at": _expires_at(ttl_minutes),
        "messages": [],
        "members": [_member_entry(created_by)] if created_by else [],
        "resources": [],
        "inject_messages": [],
    }
    # One live conversation per name: a second one with the same name took
    # over every name-addressed post, read, inject and end, while members
    # of the first kept seeing it in their hooks with nothing unread.
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _live_named(store, conv_name)
        if existing:
            raise ValueError(
                f"A conversation named '{conv_name}' is already active "
                f"({existing[0]['id']}); join it or end it first")
        conv_id = store.add_node(
            conv_name,
            node_type="coordination",
            content="Short-lived agent coordination state.",
            weight=0.01,
            prov_activity="coordination",
            prov_source=project_path or "",
            prov_who=[created_by] if created_by else [],
            extra=extra,
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if task_id and store.get_node(task_id):
        store.add_edge(conv_id, task_id, "context_of", weight=0.4)
    return conv_id


def get_conversation(store: Store, ref: str) -> dict | None:
    """Find a coordination conversation by id or normalized name."""
    node = store.get_node(ref)
    if node and node.get("type") == "coordination":
        return node

    name = _slug(ref)
    named = _named(store, name)
    live = [row for row in named if _is_live(row)]
    if len(live) > 1:
        raise ValueError(
            f"More than one active conversation is named '{name}' "
            f"({', '.join(row['id'] for row in live)}); use its id")
    if live:
        return live[0]
    # An expired one is still found, so the caller can say it expired.
    return named[0] if named else None


def _named(store: Store, name: str) -> list[dict]:
    rows = store.all_nodes(node_type="coordination", status="active", limit=500)
    return [
        row for row in rows
        if (row.get("extra") or {}).get("coord_kind") == "conversation"
        and (row.get("extra") or {}).get("name") == name
    ]


def _is_live(row: dict) -> bool:
    extra = row.get("extra") or {}
    return (extra.get("coord_status", "active") == "active"
            and not _is_expired(extra.get("expires_at")))


def _live_named(store: Store, name: str) -> list[dict]:
    """Active, unexpired conversations with this name."""
    return [row for row in _named(store, name) if _is_live(row)]


def list_conversations(
    store: Store,
    *,
    status: str = "active",
    project_path: str | None = None,
    task_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List coordination conversations."""
    node_status = "active" if status == "active" else None
    rows = store.all_nodes(node_type="coordination", status=node_status, limit=500)
    result = []
    for row in rows:
        extra = row.get("extra") or {}
        if extra.get("coord_kind") != "conversation":
            continue
        if status != "all" and extra.get("coord_status") != status:
            continue
        if project_path and extra.get("project_path") != project_path:
            continue
        if task_id and extra.get("task_id") != task_id:
            continue
        result.append(row)
    result.sort(key=lambda r: (r.get("updated_at") or ""), reverse=True)
    return result[:limit]


def join_conversation(store: Store, conversation: str, agent: str) -> dict:
    """Join a live conversation as a member. Idempotent; returns the member entry."""
    agent = (agent or "").strip()
    if not agent:
        raise ValueError("Agent is required to join a conversation")
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")

    joined: dict = {}

    def _mutate(extra: dict) -> None:
        if extra.get("coord_status") != "active":
            raise ValueError(f"Conversation is not active: {conversation}")
        members = _members_of(extra)
        for member in members:
            if member.get("agent") == agent:
                joined["member"] = member
                extra["members"] = members
                return
        entry = _member_entry(agent)
        members.append(entry)
        extra["members"] = members
        joined["member"] = entry

    store.atomic_extra_update(node["id"], _mutate)
    return joined["member"]


def post_message(
    store: Store,
    conversation: str,
    author: str,
    body: str,
    *,
    ttl_minutes: int | None = None,
    to: str | None = None,
) -> dict:
    """Append a message to a live coordination conversation.

    ``to`` targets a specific agent (broadcast when omitted); targeting is
    advisory — everyone can read the room, but unread counts and injection
    only deliver targeted messages to their addressee.
    """
    if not author.strip():
        raise ValueError("Message author is required")
    if not body.strip():
        raise ValueError("Message body is required")
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")

    snapshot = node.get("extra") or {}
    if snapshot.get("coord_status") != "active":
        raise ValueError(f"Conversation is not active: {conversation}")
    if _is_expired(snapshot.get("expires_at")):
        end_conversation(store, node["id"], summary="Expired before post")
        raise ValueError(f"Conversation expired: {conversation}")

    posted: dict = {}

    def _mutate(extra: dict) -> None:
        if extra.get("coord_status") != "active":
            raise ValueError(f"Conversation is not active: {conversation}")
        messages = list(extra.get("messages") or [])
        message = {
            "id": max(
                (int(m.get("id", 0)) for m in messages if isinstance(m, dict)),
                default=0,
            ) + 1,
            "at": _now(),
            "author": author.strip(),
            "body": body.strip(),
        }
        if to and to.strip():
            message["to"] = to.strip()
        messages.append(message)
        extra["messages"] = messages
        ttl = ttl_minutes if ttl_minutes is not None else _ttl_from_extra(extra)
        extra["ttl_minutes"] = ttl
        extra["expires_at"] = _expires_at(ttl)
        posted["message"] = message

    store.atomic_extra_update(node["id"], _mutate)
    return posted["message"]


def read_messages(
    store: Store,
    conversation: str,
    *,
    since_id: int | None = None,
    limit: int = 50,
    agent: str | None = None,
) -> dict:
    """Read messages from a coordination conversation.

    When ``agent`` is given and is a member and ``since_id`` is omitted,
    delivery is contiguous from their read cursor (oldest first, up to
    ``limit``), so a backlog larger than ``limit`` is never skipped —
    repeated reads paginate forward and the cursor (``last_read_id``)
    advances only past messages actually returned. An explicit ``since_id``
    is honoured as given (it used to be raised to the cursor, so a re-read
    from 0 found nothing) and leaves the cursor alone. Agentless (or
    non-member) reads keep the newest window and never touch cursors.
    """
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")
    extra = node.get("extra") or {}

    agent = (agent or "").strip()
    mine = None
    if agent:
        mine = next(
            (m for m in _members_of(extra) if m.get("agent") == agent), None)

    cursor_read = mine is not None and since_id is None
    floor = int(since_id or 0)
    if cursor_read:
        floor = int(mine.get("last_read_id", 0) or 0)
    all_messages = [m for m in extra.get("messages", []) if isinstance(m, dict)]
    messages = [m for m in all_messages if int(m.get("id", 0)) > floor]
    total = len(messages)
    if mine is not None:
        # Member read: oldest-first slice from the floor — contiguous.
        returned = messages[:limit]
    else:
        # Agentless/legacy read: newest window, no cursor to maintain.
        returned = messages[-limit:]

    if cursor_read and returned:
        max_seen = max(int(m.get("id", 0)) for m in returned)
        if max_seen > int(mine.get("last_read_id", 0) or 0):

            def _mutate(e: dict) -> None:
                members = _members_of(e)
                for member in members:
                    if member.get("agent") == agent:
                        if max_seen > int(member.get("last_read_id", 0) or 0):
                            member["last_read_id"] = max_seen
                            e["members"] = members
                        return

            store.atomic_extra_update(node["id"], _mutate)

    return {
        "id": node["id"],
        "name": extra.get("name", node["title"]),
        "status": extra.get("coord_status", "active"),
        "task_id": extra.get("task_id", ""),
        "expires_at": extra.get("expires_at", ""),
        "messages": returned,
        "total": total,
        "remaining": total - len(returned),
        "remaining_kind": "newer" if mine is not None else "older",
        "already_read": (len(all_messages) - total) if cursor_read else 0,
    }


def attach_resource(store: Store, conversation: str, node_id: str) -> list:
    """Attach a graph node to a conversation as a shared resource.

    Validates the node exists; idempotent. Returns the resource id list.
    """
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")
    resource = store.get_node(node_id)
    if not resource:
        raise ValueError(f"Node not found: {node_id}")
    rid = resource["id"]

    result: dict = {}

    def _mutate(extra: dict) -> None:
        resources = list(extra.get("resources") or [])
        if rid not in resources:
            resources.append(rid)
        extra["resources"] = resources
        result["resources"] = resources

    store.atomic_extra_update(node["id"], _mutate)
    return result["resources"]


def set_inject_message(
    store: Store,
    conversation: str,
    text: str,
    set_by: str,
    to: str | None = None,
) -> dict:
    """Set a standing inject message — surfaced into member sessions by hooks."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Inject message text is required")
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")

    created: dict = {}

    def _mutate(extra: dict) -> None:
        msgs = [m for m in (extra.get("inject_messages") or [])
                if isinstance(m, dict)]
        entry = {
            "id": max((int(m.get("id", 0)) for m in msgs), default=0) + 1,
            "text": text,
            "to": (to or "").strip(),
            "set_by": (set_by or "").strip(),
            "created_at": _now(),
        }
        msgs.append(entry)
        extra["inject_messages"] = msgs
        created["entry"] = entry

    store.atomic_extra_update(node["id"], _mutate)
    return created["entry"]


def clear_inject_messages(
    store: Store, conversation: str, message_id: int | None = None
) -> int:
    """Clear one (by id) or all standing inject messages. Returns count cleared."""
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")

    cleared = {"count": 0}

    def _mutate(extra: dict) -> None:
        msgs = [m for m in (extra.get("inject_messages") or [])
                if isinstance(m, dict)]
        if message_id is None:
            kept: list[dict] = []
        else:
            kept = [m for m in msgs if int(m.get("id", 0)) != int(message_id)]
        cleared["count"] = len(msgs) - len(kept)
        extra["inject_messages"] = kept

    store.atomic_extra_update(node["id"], _mutate)
    return cleared["count"]


def list_inject_messages(store: Store, conversation: str) -> list:
    """List standing inject messages for a conversation."""
    node = get_conversation(store, conversation)
    if not node:
        raise ValueError(f"Conversation not found: {conversation}")
    extra = node.get("extra") or {}
    return [m for m in (extra.get("inject_messages") or []) if isinstance(m, dict)]


def active_collabs_for_agent(store: Store, agent: str) -> list[dict]:
    """Live collab summaries for an agent — feeds prime/prompt-check hooks.

    One scan over active coordination nodes; each conversation is processed
    inside its own try/except so a malformed one cannot break the hook path.
    Returns, per conversation the agent is a member of:
    {name, node_id, task_id, focus, unread_count, inject_messages,
     locked_resources, members}.
    """
    from .store import active_lock

    agent = (agent or "").strip()
    if not agent:
        return []

    collabs = []
    rows = store.all_nodes(node_type="coordination", status="active", limit=500)
    for row in rows:
        try:
            extra = row.get("extra") or {}
            if extra.get("coord_kind") != "conversation":
                continue
            if extra.get("coord_status") != "active":
                continue
            if _is_expired(extra.get("expires_at")):
                continue
            members = _members_of(extra)
            me = next((m for m in members if m.get("agent") == agent), None)
            if me is None:
                continue

            last_read = int(me.get("last_read_id", 0) or 0)
            unread = 0
            for msg in extra.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                if int(msg.get("id", 0)) <= last_read:
                    continue
                to = (msg.get("to") or "").strip()
                if to and to != agent:
                    continue
                unread += 1

            injects = [
                m for m in (extra.get("inject_messages") or [])
                if isinstance(m, dict)
                and (not (m.get("to") or "").strip() or m.get("to") == agent)
            ]

            locked_resources = []
            for rid in extra.get("resources") or []:
                resource = store.get_node(rid)
                if not resource:
                    continue
                lock = active_lock(resource)
                if lock:
                    locked_resources.append({
                        "node_id": rid,
                        "title": resource.get("title", rid),
                        "holder": lock.get("agent", ""),
                    })

            task_id = extra.get("task_id") or ""
            focus = ""
            if task_id:
                task = store.get_node(task_id)
                focus = task["title"] if task else task_id

            collabs.append({
                "name": extra.get("name", row.get("title", "")),
                "node_id": row["id"],
                "task_id": task_id,
                "focus": focus,
                "unread_count": unread,
                "inject_messages": injects,
                "locked_resources": locked_resources,
                "members": [m.get("agent", "") for m in members],
            })
        except Exception:
            continue  # malformed conversation — never break the hook path
    collabs.sort(key=lambda c: c["name"])
    return collabs


def end_conversation(store: Store, conversation: str, *, summary: str = "") -> dict | None:
    """Archive a coordination conversation and clear transient message bodies."""
    node = get_conversation(store, conversation)
    if not node:
        return None
    extra = dict(node.get("extra") or {})
    extra["coord_status"] = "ended"
    extra["ended_at"] = _now()
    extra["message_count"] = len(extra.get("messages") or [])
    extra["messages"] = []
    content = summary or node.get("content") or "Ended coordination conversation."
    store.update_node(node["id"], status="archived", content=content, extra=extra)
    return store.get_node(node["id"])


def cleanup_expired_conversations(store: Store) -> int:
    """Archive expired coordination conversations and clear messages."""
    count = 0
    for node in list_conversations(store, status="active", limit=500):
        extra = node.get("extra") or {}
        if _is_expired(extra.get("expires_at")):
            end_conversation(store, node["id"], summary="Expired coordination conversation.")
            count += 1
    return count


def format_conversations(conversations: list[dict]) -> str:
    if not conversations:
        return "No coordination conversations found."
    lines = []
    for conv in conversations:
        extra = conv.get("extra") or {}
        messages = extra.get("messages") or []
        task = f" task:{extra.get('task_id')[:12]}" if extra.get("task_id") else ""
        lines.append(
            f"  [{extra.get('coord_status', '?')}] {extra.get('name', conv['title'])}"
            f"{task} messages:{len(messages)} expires:{extra.get('expires_at', '')}"
            f" {conv['id'][:12]}"
        )
    return "\n".join(lines)


def format_messages(payload: dict) -> str:
    messages = payload.get("messages") or []
    if not messages:
        earlier = int(payload.get("already_read") or 0)
        if earlier:
            return (f"No new messages ({earlier} already read; "
                    "pass since_id=0 to read them again).")
        return "No messages."
    lines = [
        f"Conversation: {payload.get('name')} ({payload.get('status')})",
        f"Expires: {payload.get('expires_at', '')}",
    ]
    for msg in messages:
        target = f" -> {msg['to']}" if msg.get("to") else ""
        lines.append(
            f"  #{msg.get('id')} {msg.get('at')} {msg.get('author')}{target}: {msg.get('body')}"
        )
    remaining = int(payload.get("remaining") or 0)
    if remaining > 0:
        kind = payload.get("remaining_kind") or "more"
        hint = ("read again to continue" if kind == "newer"
                else "pass since_id or a larger limit to see them")
        lines.append(
            f"  (showing {len(messages)} of {payload.get('total', len(messages))}"
            f" — {remaining} {kind} message(s) not shown; {hint})"
        )
    return "\n".join(lines)
