"""Conversation ingest: a transcript becomes dated document nodes, losslessly.

Session ingest (`kin ingest sessions`) keeps a summary of the first 8,000
characters, and `kin add` keeps what its extractor picked out. Questions about
past conversations ask for the details those drop: an amount, a date, a name,
what the assistant recommended. Here every message is kept, whole messages are
packed into `document` nodes of at most `node_chars` characters (the cap file
ingest uses), each node carries the conversation's date as `prov_when`, and the
nodes of one conversation are linked in order so graph expansion can reach a
node's neighbours.

With an LLM configured, `digest_conversation` also reads each conversation once
(and, with `conversations.facts`, writes down its dated facts as nodes):
the user's standing instructions (how the assistant should respond from now on)
become `directive` nodes, which `kin ask` shows with every answer, and a dated
summary becomes a node linked to the conversation, for questions that span many
conversations.

A conversation file is JSON or JSON Lines; each object is
``{"id": ..., "date": ..., "messages": [{"role": ..., "content": ..., "name": ...}]}``,
optionally with ``"expires"`` (a date) or ``"retracted": true``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .store import Store

NODE_CHARS = 4000


def _lines(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        who = m.get("name") or m.get("role") or "user"
        text = str(m.get("content") or "").strip()
        if text:
            out.append(f"{who}: {text}")
    return out


def pack(lines: list[str], limit: int = NODE_CHARS) -> list[str]:
    """Whole messages packed into chunks of at most `limit` characters; a longer message is split."""
    nodes, cur = [], ""
    for line in lines:
        while len(line) > limit:
            if cur:
                nodes.append(cur)
                cur = ""
            nodes.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            nodes.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        nodes.append(cur)
    return nodes


def chunk_id(conversation_id: str, position: int) -> str:
    return "conv-" + hashlib.sha256(f"{conversation_id}|{position}".encode()).hexdigest()[:16]


def ingest_conversation(store: Store, conversation_id: str, messages: list[dict], when: str | None = None,
                        *, node_chars: int = NODE_CHARS, prov_source: str | None = None,
                        expires: str | None = None) -> list[str]:
    """Stores one conversation, reconciled with what an earlier ingest stored for
    the same id: new chunks are added, a chunk whose text, date or expiry
    changed is replaced (and so re-embedded), chunks past the new end are
    removed, and every pair of consecutive chunks is linked. A conversation
    that grew inside its last chunk, or an ingest that stopped part-way, is
    therefore completed rather than skipped. Returns the ids added or replaced."""
    chunks = pack(_lines(messages), node_chars)
    ids = [chunk_id(conversation_id, i) for i in range(len(chunks))]
    changed: list[str] = []
    for i, (nid, text) in enumerate(zip(ids, chunks)):
        extra = {"conversation_id": conversation_id, "position": i}
        if expires:
            extra["expires"] = expires
        node = store.get_node(nid)
        if node is not None:
            same = (node.get("content") == text and node.get("prov_when") == when
                    and (node.get("extra") or {}).get("expires") == extra.get("expires"))
            if same:
                continue
            store.delete_node(nid)
        store.add_node(
            node_id=nid,
            title=text[:60].strip() + ("..." if len(text) > 60 else ""),
            content=text,
            node_type="document",
            prov_source=prov_source or conversation_id,
            prov_activity="conversation-ingest",
            prov_when=when,
            extra=extra,
        )
        changed.append(nid)
    position = len(chunks)
    while store.get_node(chunk_id(conversation_id, position)):
        store.delete_node(chunk_id(conversation_id, position))
        position += 1
    for a, b in zip(ids, ids[1:]):
        if not any(edge.get("to_id") == b for edge in store.edges_from(a)):
            store.add_edge(a, b, edge_type="relates_to", provenance="next in conversation")
    return changed


def _conversation_nodes(store: Store, conversation_id: str, kinds: tuple[str, ...]) -> list[dict]:
    rows = store.conn.execute(
        "SELECT id FROM nodes WHERE json_extract(extra, '$.conversation_id') = ?", (conversation_id,)
    ).fetchall()
    nodes = [store.get_node(r[0]) for r in rows]
    return [n for n in nodes if n and (n.get("extra") or {}).get("kind", "chunk") in kinds]


def _directive_sources(node: dict) -> dict[str, str | None]:
    """conversation id -> its expiry, for a directive a digest recorded. Several
    conversations can give the same directive; it lasts as long as one does."""
    if node.get("prov_activity") != "conversation-digest":
        return {}
    sources = (node.get("extra") or {}).get("conversations")
    if isinstance(sources, dict):
        return dict(sources)
    return {node["prov_source"]: None} if node.get("prov_source") else {}


def _directive_extra(node: dict, sources: dict[str, str | None]) -> dict:
    extra = {k: v for k, v in (node.get("extra") or {}).items() if k not in ("conversations", "expires")}
    extra["conversations"] = sources
    expiries = list(sources.values())
    if expiries and all(expiries):
        extra["expires"] = max(expiries)
    return extra


def _release_directives(store: Store, conversation_id: str) -> int:
    """Drops a conversation's claim on the directives it gave; a directive no
    other conversation gave is removed. Returns the number removed."""
    removed = 0
    for node in store.all_nodes(node_type="directive", limit=100_000):
        sources = _directive_sources(node)
        if conversation_id not in sources:
            continue
        del sources[conversation_id]
        if sources:
            store.update_node(node["id"], extra=_directive_extra(node, sources),
                              prov_source=next(iter(sources)))
        else:
            store.delete_node(node["id"])
            removed += 1
    return removed


def _remove_digest(store: Store, conversation_id: str) -> None:
    """What a digest derived from a conversation, so a changed conversation is digested afresh."""
    for node in _conversation_nodes(store, conversation_id, ("conversation-summary", "conversation-fact")):
        store.delete_node(node["id"])
    _release_directives(store, conversation_id)


def retract_conversation(store: Store, conversation_id: str) -> int:
    """Removes a conversation and everything derived from it: its chunks, summary,
    facts and the directives only it gave. Returns the number of nodes removed."""
    nodes = _conversation_nodes(store, conversation_id, ("chunk", "conversation-summary", "conversation-fact"))
    for node in nodes:
        store.delete_node(node["id"])
    removed = len(nodes) + _release_directives(store, conversation_id)
    done = _digest_record(store)
    if conversation_id in done:
        del done[conversation_id]
        store.set_meta("conversation_digests", json.dumps(done, sort_keys=True))
    return removed


def load_conversations(path: Path) -> list[dict]:
    text = path.read_text()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def ingest_directory(store: Store, directory: Path, verbose: bool = False) -> int:
    """Ingests every conversation in the directory's JSON and JSONL files. A
    conversation may carry `expires` (it stops surfacing after that date) or
    `retracted: true` (it and everything derived from it are removed). The
    file name is kept as provenance; the conversation's id is its identity."""
    count = 0
    for path in sorted(list(directory.rglob("*.json")) + list(directory.rglob("*.jsonl"))):
        for index, conv in enumerate(load_conversations(path)):
            cid = str(conv.get("id") or f"{path.stem}#{index}")
            if conv.get("retracted"):
                count += retract_conversation(store, cid)
                continue
            changed = ingest_conversation(store, cid, conv.get("messages") or [], conv.get("date"),
                                          prov_source=str(path), expires=conv.get("expires"))
            count += len(changed)
            if verbose and changed:
                print(f"  Conversation {cid}: {len(changed)} node(s)")
    return count


DIGEST_SCHEMA = {
    "name": "conversation_digest",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["directives", "summary"],
        "properties": {
            "directives": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
    },
}

DIRECTIVES_ONLY_SCHEMA = {
    "name": "conversation_digest",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["directives"],
        "properties": {"directives": {"type": "array", "items": {"type": "string"}}},
    },
}

# A summary earns its place only when it compresses: a short conversation is its own summary.
SUMMARY_MIN_TOKENS = 6000

DIGEST_PROMPT = """Below is {what}{date}. Return:

- directives: the user's standing instructions: requests about how the assistant should respond to future requests, stated as such ("always ...", "from now on ...", "whenever I ask about ..."), covering formats, things to always include or avoid, style, units or level of detail. Write each as one imperative sentence that keeps the user's specifics ("Always include type hints in Python examples"). Instructions that configure the task at hand are not standing instructions: how to write this piece, which language or spelling to use for it, a role to play, a game, quiz, exercise or classification to run for the rest of the conversation ("for every sentence I give you, reply only with ...", "respond only with OK until I say done"). Often there are none.
{summary}
Conversation:
{text}"""

# Facts written down once, when the conversation is read: dated, self-contained
# statements a later question can be answered from without re-deriving
# "last Saturday" or a count from raw text at answer time.
FACTS_FIELD = """- facts: everything in the conversation worth remembering about the user and their world, so a question months later can be answered without the original text. One fact per item, each self-contained: name the people, places, items and titles instead of using pronouns, and keep numbers, amounts, counts and units. For each fact give: date, when it happened or was true, as YYYY-MM-DD or a period (2023-05, "the week before 2023-06-12"), resolving relative dates ("last Saturday", "two weeks ago", "tomorrow") against the conversation date, or empty if undated; subject, who it is about ("user", "assistant", or a person's name); text, the fact in one sentence, keeping the original relative phrase when one was used ("the user bought a red kayak for $450 on the Saturday before 2023-05-20"). Include events and activities, purchases, plans with dates, possessions and counts, preferences and opinions, relationships, health, work and places, changes ("the user now walks 10,000 steps a day; earlier it was 5,000"), and the specific recommendations, lists and answers the assistant gave. Leave out small talk and generic advice.
"""

FACT_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["date", "subject", "text"],
    "properties": {"date": {"type": "string"}, "subject": {"type": "string"}, "text": {"type": "string"}},
}


def _digest_schema(summary: bool, facts: bool) -> dict:
    props = {"directives": {"type": "array", "items": {"type": "string"}}}
    if summary:
        props["summary"] = {"type": "string"}
    if facts:
        props["facts"] = {"type": "array", "items": FACT_ITEM}
    return {"name": "conversation_digest",
            "schema": {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}}


SUMMARY_FIELD = """- summary: 4 to 8 sentences on what the conversation covered: the user's situation, the facts, figures, decisions and plans they gave, and what the assistant advised, with dates (resolve "last week" or "tomorrow" against the conversation date).
"""


_SPEAKER = re.compile(r"^(user|assistant|system): ", re.M)


def user_messages(text: str) -> str:
    """The user's messages from conversation text written as "role: message" lines
    (the form ingest_conversation stores); empty when the text has no user role."""
    parts = _SPEAKER.split(text)
    # split() yields [before, role, body, role, body, ...]
    return "\n".join(f"user: {body.strip()}" for role, body in zip(parts[1::2], parts[2::2])
                     if role == "user" and body.strip())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class DigestStatus:
    DONE = "done"                # the conversation was read and its outputs written
    UNAVAILABLE = "unavailable"  # no model, no credentials or no budget: retry later
    FAILED = "failed"            # the call or its answer failed: retry later


def digest_conversation(store: Store, conversation_id: str, text: str, when: str | None, config, ledger=None,
                        *, effort: str = "low", link_to: list[str] | None = None,
                        summary_min_tokens: int | None = None, expires: str | None = None) -> dict:
    """One model pass over a conversation: its standing instructions become `directive`
    nodes, and, for a conversation of at least `summary_min_tokens`, its summary a
    `document` node linked to the conversation's nodes (with `conversations.facts`,
    its dated facts too). `status` says whether it was done (see DigestStatus);
    nothing is written unless it was."""
    from .answer import BudgetExhausted, _call
    from .llm import get_client

    out = {"status": DigestStatus.UNAVAILABLE, "directives": [], "summary": None, "facts": []}
    if not config.llm.enabled or (ledger is not None and not ledger.can_spend()):
        return out
    client = get_client(config, timeout=config.ask.timeout_seconds, retries=3)
    if client is None:
        return out
    threshold = SUMMARY_MIN_TOKENS if summary_min_tokens is None else summary_min_tokens
    summarize = len(text) // 4 >= threshold
    facts = bool(getattr(getattr(config, "conversations", None), "facts", False))
    what = "a conversation between a user and an assistant"
    if not summarize and not facts:
        # Directives are the user's: a short conversation is read from the user's side only.
        mine = user_messages(text)
        if mine:
            text, what = mine, "what a user wrote in a conversation with an assistant"
    fields = (SUMMARY_FIELD if summarize else "") + (FACTS_FIELD if facts else "")
    schema = _digest_schema(summarize, True) if facts else (DIGEST_SCHEMA if summarize else DIRECTIVES_ONLY_SCHEMA)
    try:
        raw = _call(client, config, system=None, ledger=ledger, purpose="conversation-digest",
                    user=DIGEST_PROMPT.format(what=what, date=f" on {when}" if when else "", text=text,
                                              summary=fields),
                    effort=effort, max_tokens=24000 if facts else 8000, json_schema=schema)
        digest = json.loads(raw)
        if not isinstance(digest, dict):
            raise ValueError("digest is not an object")
    except BudgetExhausted:
        return out
    except Exception:
        out["status"] = DigestStatus.FAILED
        return out
    derived_extra = {"expires": expires} if expires else {}
    existing = {_norm(n.get("title") or ""): n for n in store.all_nodes(node_type="directive", limit=100_000)}
    for d in digest.get("directives") or []:
        d = str(d).strip()
        if not d:
            continue
        node = existing.get(_norm(d))
        if node is not None:
            sources = _directive_sources(node)
            if sources and conversation_id not in sources:
                sources[conversation_id] = expires
                node["extra"] = _directive_extra(node, sources)
                store.update_node(node["id"], extra=node["extra"])
            continue
        sources = {conversation_id: expires}
        nid = store.add_node(
            title=d, content="", node_type="directive", prov_source=conversation_id,
            prov_activity="conversation-digest", prov_when=when, extra=_directive_extra({}, sources))
        existing[_norm(d)] = store.get_node(nid)
        out["directives"].append(nid)
    summary = str(digest.get("summary") or "").strip() if summarize else ""
    if summary:
        nid = "convsum-" + hashlib.sha256(conversation_id.encode()).hexdigest()[:16]
        if not store.get_node(nid):
            store.add_node(node_id=nid, title=f"Summary of a conversation{f' on {when}' if when else ''}",
                           content=summary, node_type="document", prov_source=conversation_id,
                           prov_activity="conversation-digest", prov_when=when,
                           extra={"conversation_id": conversation_id, "kind": "conversation-summary",
                                  **derived_extra})
            for target in link_to or []:
                store.add_edge(nid, target, edge_type="relates_to", provenance="summary of conversation")
        out["summary"] = nid
    for i, fact in enumerate((digest.get("facts") or []) if facts else []):
        text_ = str((fact or {}).get("text") or "").strip() if isinstance(fact, dict) else ""
        if not text_:
            continue
        nid = "convfact-" + hashlib.sha256(f"{conversation_id}|{i}".encode()).hexdigest()[:16]
        if store.get_node(nid):
            continue
        store.add_node(node_id=nid, title=text_[:60].strip() + ("..." if len(text_) > 60 else ""),
                       content=text_, node_type="document", prov_source=conversation_id,
                       prov_activity="conversation-digest", prov_when=when,
                       extra={"conversation_id": conversation_id, "kind": "conversation-fact",
                              "fact_date": str(fact.get("date") or "").strip(),
                              "subject": str(fact.get("subject") or "").strip(), **derived_extra})
        for target in link_to or []:
            store.add_edge(nid, target, edge_type="relates_to", provenance="fact from conversation")
        out["facts"].append(nid)
    out["status"] = DigestStatus.DONE
    return out


def _digest_record(store: Store) -> dict[str, str | None]:
    """conversation id -> the digest key it was digested under. A record written
    before keys existed (a plain list) is read as digested under an unknown key."""
    raw = json.loads(store.get_meta("conversation_digests") or "{}")
    return {cid: None for cid in raw} if isinstance(raw, list) else dict(raw)


def _digest_key(nodes: list[dict], config) -> str:
    """The conversation's revision and the digest features in effect: either
    changing means the conversation should be digested again."""
    features = {"facts": bool(getattr(getattr(config, "conversations", None), "facts", False)),
                "summary_min_tokens": SUMMARY_MIN_TOKENS, "version": 2}
    revision = [(n.get("content") or "", n.get("prov_when") or "", (n.get("extra") or {}).get("expires"))
                for n in nodes]
    return hashlib.sha256(json.dumps([features, revision], sort_keys=True).encode()).hexdigest()[:24]


def backfill_digests(store: Store, config, ledger=None, *, max_chars: int = 400_000,
                     activities: tuple[str, ...] = ("conversation-ingest",)) -> int:
    """Digests the conversations in the graph that are new, have changed, or were
    digested with different features, one call per conversation.

    Conversations are the document nodes whose `prov_activity` is in `activities`,
    grouped by their conversation id (`extra.conversation_id`, else `prov_source`
    for nodes stored before ids were recorded). A conversation is recorded as
    digested only when its digest succeeded; one that failed or could not run is
    retried next time. Returns the number of conversations digested."""
    convs: dict[str, list[dict]] = {}
    for node in store.all_nodes(node_type="document", limit=1_000_000):
        if node.get("prov_activity") not in activities:
            continue
        extra = node.get("extra") or {}
        if extra.get("kind") in ("conversation-summary", "conversation-fact"):
            continue
        convs.setdefault(extra.get("conversation_id") or node.get("prov_source") or node["id"], []).append(node)
    done = _digest_record(store)
    count = 0
    for cid, nodes in convs.items():
        nodes.sort(key=lambda n: ((n.get("extra") or {}).get("position", 0), n.get("created_at") or ""))
        key = _digest_key(nodes, config)
        if cid in done and done[cid] in (None, key):
            continue
        if cid in done:
            _remove_digest(store, cid)
        text = "\n".join(n.get("content") or "" for n in nodes)[:max_chars]
        result = digest_conversation(store, cid, text, nodes[0].get("prov_when"), config, ledger,
                                     link_to=[n["id"] for n in nodes],
                                     expires=(nodes[0].get("extra") or {}).get("expires"))
        if result["status"] == DigestStatus.UNAVAILABLE:
            break  # no model or budget: everything after this would fail the same way
        if result["status"] != DigestStatus.DONE:
            continue
        done[cid] = key
        store.set_meta("conversation_digests", json.dumps(done, sort_keys=True))
        count += 1
    return count
