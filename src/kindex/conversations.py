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
    """Each message as "role: text", or "role (name): text" for a named speaker,
    so who spoke and in which role are both kept."""
    out = []
    for m in messages:
        role = str(m.get("role") or "user").strip() or "user"
        name = " ".join(str(m.get("name") or "").split())
        who = f"{role} ({name})" if name and name.lower() != role.lower() else role
        text = str(m.get("content") or "").strip()
        if text:
            out.append(f"{who}: {text}")
    return out


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_expires(expires: str | None) -> str | None:
    """An expiry is a calendar date, YYYY-MM-DD; anything else ("never", a
    typo) would keep the data forever, so it is refused."""
    if expires is None:
        return None
    value = str(expires).strip()
    if not _ISO_DATE.match(value):
        raise ValueError(f"expires must be a date as YYYY-MM-DD, not {value!r}")
    from datetime import date as _date

    _date.fromisoformat(value)  # rejects 2024-13-40
    return value


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
    expires = _check_expires(expires)
    chunks = pack(_lines(messages), node_chars)
    if not chunks:
        # Nothing left to store: the conversation and what was derived from it go.
        if store.get_node(chunk_id(conversation_id, 0)):
            retract_conversation(store, conversation_id)
        return []
    ids = [chunk_id(conversation_id, i) for i in range(len(chunks))]
    changed: list[str] = []
    for i, (nid, text) in enumerate(zip(ids, chunks)):
        # The given date is kept as given: an undated conversation's chunks get
        # the time of writing as their provenance, which is not a revision.
        extra = {"conversation_id": conversation_id, "position": i, "date": when}
        if expires:
            extra["expires"] = expires
        node = store.get_node(nid)
        if node is not None:
            old = node.get("extra") or {}
            old_when = old["date"] if "date" in old else node.get("prov_when")
            if node.get("content") == text and old_when == when and old.get("expires") == extra.get("expires"):
                continue
        # One upsert: a write that fails leaves the earlier chunk and its links in place.
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
    for row in store.conn.execute(
            "SELECT id FROM nodes WHERE json_extract(extra, '$.kind') = 'entity-profile'").fetchall():
        profile = store.get_node(row[0])
        if profile and conversation_id in ((profile.get("extra") or {}).get("conversations") or []):
            store.delete_node(profile["id"])  # rebuilt from what remains by the next digest
            removed += 1
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


def _on_or_after(when, since) -> bool:
    if not since:
        return True
    if not when:
        return True  # an undated conversation cannot be placed before `since`
    from dateutil import parser

    try:
        day = parser.parse(str(when), fuzzy=True).date()
        start = since if hasattr(since, "year") else parser.parse(str(since)).date()
        return day >= (start.date() if hasattr(start, "date") else start)
    except (ValueError, OverflowError):
        return True


def ingest_directory(store: Store, directory: Path, verbose: bool = False, *, limit: int | None = None,
                     since=None, errors: list[str] | None = None) -> int:
    """Ingests every conversation in the directory's JSON and JSONL files. A
    conversation may carry `expires` (YYYY-MM-DD: it stops surfacing after that
    date) or `retracted: true` (it and everything derived from it are
    removed). The file name is kept as provenance; the conversation's id is its
    identity. `since` skips conversations dated before it; `limit` stops after
    that many conversations were written (unchanged ones do not count, so a
    later run continues). A conversation that cannot be stored is reported in
    `errors` and leaves the graph as it was. Returns the nodes written."""
    count = written = 0
    for path in sorted(list(directory.rglob("*.json")) + list(directory.rglob("*.jsonl"))):
        for index, conv in enumerate(load_conversations(path)):
            if limit is not None and written >= limit:
                return count
            cid = str(conv.get("id") or f"{path.stem}#{index}")
            if conv.get("retracted"):
                count += retract_conversation(store, cid)
                continue
            if not _on_or_after(conv.get("date"), since):
                continue
            try:
                changed = ingest_conversation(store, cid, conv.get("messages") or [], conv.get("date"),
                                              prov_source=str(path), expires=conv.get("expires"))
            except ValueError as exc:
                if errors is not None:
                    errors.append(f"{path.name}: conversation {cid}: {exc}")
                continue
            count += len(changed)
            written += bool(changed)
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


_SPEAKER = re.compile(r"^(user|assistant|system)(?: \([^)\n]*\))?: ", re.M)


def user_messages(text: str) -> str:
    """The user's messages from conversation text written as "role: message" or
    "role (name): message" lines (the forms ingest_conversation stores); empty
    when the text has no user role."""
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


# ── Turn-level claims (conversations.claims) ──
#
# A per-conversation fact list loses which message said what and in what order.
# Claims are read message by message instead: every claim names the message it
# came from, so `kin ask` can show it with that message's place in the
# conversation and its words, find the first or the latest of several similar
# statements, and keep what was said after a moment out of an answer about it.

CLAIM_WINDOW = 40            # messages per claims call (a longer conversation takes several calls)
CLAIM_MESSAGE_CHARS = 4000   # the most of one message a claims call reads
CLAIM_EXCERPT_CHARS = 280    # the source words kept with a claim

CLAIMS_PROMPT = """Below are numbered messages from a conversation, each labelled with its date and speaker. List the atomic claims a question asked months later could be answered from, each tied to the number of the message it comes from:
- what a speaker said about themselves, other people and their world: facts, numbers, amounts, versions, names, dates, plans and deadlines, decisions, preferences, and changes to earlier values (kind "update": "the estate tax estimate is now 12%, earlier 15%");
- what a speaker asked about or asked for, with the specifics they gave (values, examples, formulas, items, constraints) (kind "request");
- an assistant's specific recommendations, steps, lists, answers and figures (kind "recommendation"), one claim per distinct step or point, keeping the specifics.
Each claim is one self-contained sentence: name the people, places and things instead of using pronouns, keep exact values and units, and resolve relative dates against the message's date. For each give: m, the message number; kind (fact, plan, decision, preference, update, request, recommendation); subject, who it is about ("user", "assistant", or a person's name); date, when it happened or is due as YYYY-MM-DD (or a period), empty if undated; text, the claim.

Messages:
{messages}"""

CLAIMS_SCHEMA = {"name": "conversation_claims", "schema": {
    "type": "object", "additionalProperties": False, "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["m", "kind", "subject", "date", "text"],
        "properties": {"m": {"type": "integer"}, "kind": {"type": "string"}, "subject": {"type": "string"},
                       "date": {"type": "string"}, "text": {"type": "string"}}}}}}}

_ROLE_START = re.compile(r"^(user|assistant|system)(?: \(([^)\n]{1,40})\))?: ")
_NAME_START = re.compile(r"^([A-Z][^:\n]{0,39}): ")
_CLAIM_WORD = re.compile(r"[a-z0-9][a-z0-9.%$'-]*")


def _chunk_rows(store: Store, ids: list[str]) -> list[dict]:
    """The stored chunks, in conversation order: by recorded position, else by insertion."""
    rows = []
    for nid in ids:
        row = store.conn.execute("SELECT rowid, id, content, prov_when, extra FROM nodes WHERE id = ?",
                                 (nid,)).fetchone()
        if row:
            extra = json.loads(row[4] or "{}")
            rows.append({"rowid": row[0], "id": row[1], "content": row[2] or "", "when": row[3],
                         "position": extra.get("position"), "expires": extra.get("expires")})
    rows.sort(key=lambda r: (r["position"] if isinstance(r["position"], int) else 10**9, r["rowid"]))
    for i, r in enumerate(rows):
        if not isinstance(r["position"], int):
            r["position"] = i
    return rows


def conversation_messages(chunks: list[dict]) -> list[dict]:
    """The messages of a conversation's chunks, in order: each with the position of
    the chunk it starts in, that chunk's id, its speaker and its text. A line opens
    a message when it starts with a role ("user: ", "assistant (Ana): ") or with a
    name that opens lines more than once ("Caroline: "), so a quoted "Note: " inside
    a message does not split it; a chunk that starts mid-message continues it."""
    names: dict[str, int] = {}
    for c in chunks:
        for line in c["content"].split("\n"):
            m = _NAME_START.match(line)
            if m and not _ROLE_START.match(line):
                names[m.group(1)] = names.get(m.group(1), 0) + 1
    speakers = {n for n, k in names.items() if k > 1}
    out: list[dict] = []
    for c in chunks:
        for line in c["content"].split("\n"):
            role = _ROLE_START.match(line)
            name = None if role else _NAME_START.match(line)
            if role or (name and name.group(1) in speakers):
                m = role or name
                out.append({"position": c["position"], "chunk": c["id"], "when": c["when"],
                            "expires": c.get("expires"), "speaker": m.group(0)[:-2],
                            "role": role.group(1) if role else "", "text": line[m.end():]})
            elif out:
                out[-1]["text"] += "\n" + line
    for m in out:
        m["text"] = m["text"].strip()
    return [m for m in out if m["text"]]


def _claim_excerpt(text: str, claim: str) -> str:
    """The source words around the densest run of the claim's words."""
    if len(text) <= CLAIM_EXCERPT_CHARS:
        return text
    keys = {w for w in _CLAIM_WORD.findall(claim.lower()) if len(w) > 2}
    best, at = -1, 0
    for start in range(0, len(text) - CLAIM_EXCERPT_CHARS + 1, 40):
        score = sum(w in keys for w in _CLAIM_WORD.findall(text[start:start + CLAIM_EXCERPT_CHARS].lower()))
        if score > best:
            best, at = score, start
    return ("…" if at else "") + text[at:at + CLAIM_EXCERPT_CHARS].strip() + "…"


def claim_windows(messages: list[dict], window: int = CLAIM_WINDOW) -> list[list[dict]]:
    """Messages grouped for claims calls: about `window` at a time, closing a group
    at a conversation boundary once it is full (short conversations share a call),
    or mid-conversation once it reaches twice that."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for m in messages:
        if cur and (len(cur) >= window and m["conversation_id"] != cur[-1]["conversation_id"]
                    or len(cur) >= 2 * window):
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return groups


def _read_claims(client, config, ledger, window: list[dict], effort: str) -> list[tuple[dict, dict]] | None:
    """One claims call over a window of messages: (claim, its message) pairs; None
    when the call or its answer failed (the caller retries later)."""
    from .answer import BudgetExhausted, _call

    listing = "\n\n".join(f"[{i}] ({m['when'] or 'undated'}) {m['speaker']}: {m['text'][:CLAIM_MESSAGE_CHARS]}"
                          for i, m in enumerate(window, 1))
    try:
        raw = _call(client, config, system=None, ledger=ledger, purpose="conversation-claims",
                    user=CLAIMS_PROMPT.format(messages=listing), effort=effort, max_tokens=32000,
                    json_schema=CLAIMS_SCHEMA)
        claims = json.loads(raw).get("claims")
        if not isinstance(claims, list):
            raise ValueError("claims is not a list")
    except BudgetExhausted:
        raise
    except Exception:
        return None
    pairs = []
    for c in claims:
        i = c.get("m") if isinstance(c, dict) else None
        if isinstance(i, int) and 1 <= i <= len(window) and str(c.get("text") or "").strip():
            pairs.append((c, window[i - 1]))
    return pairs


def _write_claims(store: Store, conversation_id: str, pairs: list[tuple[dict, dict]]) -> list[str]:
    """Replaces the conversation's facts (or earlier claims) with these claims, linked
    to their source chunks, and queues them for embedding in one write."""
    from .vectors import enqueue_embeddings

    for node in _conversation_nodes(store, conversation_id, ("conversation-fact",)):
        store.delete_node(node["id"])
    ids = []
    for k, (c, msg) in enumerate(pairs):
        text = str(c["text"]).strip()
        nid = "convclaim-" + hashlib.sha256(f"{conversation_id}|{k}".encode()).hexdigest()[:16]
        extra = {"conversation_id": conversation_id, "kind": "conversation-fact", "turn_claim": True,
                 "fact_date": str(c.get("date") or "").strip(),
                 "subject": str(c.get("subject") or ("user" if msg["role"] == "user" else msg["speaker"])).strip(),
                 "claim_kind": str(c.get("kind") or "").strip(),
                 "source": {"position": msg["position"], "role": msg["role"] or msg["speaker"],
                            "excerpt": _claim_excerpt(msg["text"], text)}}
        if msg.get("expires"):
            extra["expires"] = msg["expires"]
        store.add_node(node_id=nid, title=text[:60].strip() + ("..." if len(text) > 60 else ""),
                       content=text, node_type="document", prov_source=conversation_id,
                       prov_activity="conversation-digest", prov_when=msg["when"], extra=extra,
                       queue_embedding=False)
        store.add_edge(nid, msg["chunk"], edge_type="relates_to", provenance="claim from message")
        ids.append(nid)
    enqueue_embeddings(store, ids, max_queue=max(100_000, 4 * len(ids)))
    return ids


def _claims_key(chunks: list[dict]) -> str:
    """What a conversation's claims were read from: a changed key means read them again."""
    return hashlib.sha256(json.dumps([[c["content"], c["when"]] for c in chunks]).encode()).hexdigest()[:24]


def _record_claims(store: Store, done: dict[str, str]) -> None:
    store.set_meta("conversation_claims", json.dumps(done, sort_keys=True))


def _claims_record(store: Store) -> dict[str, str]:
    """conversation id -> the claims key its turn-level claims were read under."""
    return json.loads(store.get_meta("conversation_claims") or "{}")


def claims_enabled(config) -> bool:
    return bool(getattr(getattr(config, "conversations", None), "claims", False))


def claims_apply(messages: list[dict]) -> bool:
    """Claims are read for user/assistant transcripts. Dialogue between named people
    keeps its per-conversation facts: measured on such dialogue, claims in their place
    answered worse (they lose the inference a whole-conversation read keeps)."""
    return any(m["role"] in ("user", "assistant") for m in messages)


def digest_conversation(store: Store, conversation_id: str, text: str, when: str | None, config, ledger=None,
                        *, effort: str = "low", link_to: list[str] | None = None,
                        summary_min_tokens: int | None = None, expires: str | None = None) -> dict:
    """One model pass over a conversation: its standing instructions become `directive`
    nodes, and, for a conversation of at least `summary_min_tokens`, its summary a
    `document` node linked to the conversation's nodes (with `conversations.facts`,
    its dated facts too; with `conversations.claims`, turn-level claims read message
    by message in their place). `status` says whether it was done (see DigestStatus);
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
    chunks = _chunk_rows(store, link_to or []) if claims_enabled(config) else []
    messages = conversation_messages(chunks)
    claims = claims_enabled(config) and claims_apply(messages)
    facts = bool(getattr(getattr(config, "conversations", None), "facts", False)) and not claims
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
    pairs: list[tuple[dict, dict]] = []
    if claims:
        for m in messages:
            m["conversation_id"] = conversation_id
        try:
            for window in claim_windows(messages):
                got = _read_claims(client, config, ledger, window, effort)
                if got is None:
                    out["status"] = DigestStatus.FAILED
                    return out
                pairs += got
        except BudgetExhausted:
            return out
    # Whatever an earlier, interrupted run of this digest wrote is replaced as a
    # whole, so its outputs are never mixed with these.
    _remove_digest(store, conversation_id)
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
    if claims:
        out["facts"] = _write_claims(store, conversation_id, pairs)
        # Recorded so backfill_claims (`kin digest --claims`) does not read them again.
        done = _claims_record(store)
        done[conversation_id] = _claims_key(chunks)
        _record_claims(store, done)
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
    if claims_enabled(config):
        # Only when on, so turning claims on re-digests and every existing key stays valid.
        features["claims"] = 1
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
    if profiles_enabled(config) and (getattr(getattr(config, "conversations", None), "facts", False)
                                     or claims_enabled(config)):
        build_profiles(store, config, ledger)
    return count


def backfill_claims(store: Store, config, ledger=None, *, effort: str = "low", workers: int = 1,
                    activities: tuple[str, ...] = ("conversation-ingest",)) -> int:
    """Gives conversations that were digested before `conversations.claims` turn-level
    claims in place of their per-conversation facts, without digesting them again (their
    summaries and directives stay). Short conversations share a claims call; a
    conversation is recorded as done only when all of its claims were read, so one
    that failed is retried next time. With `workers` > 1 the claims calls run concurrently
    (the store is written once they are all read). Returns the number of conversations given claims."""
    from .answer import BudgetExhausted
    from .llm import get_client

    if not config.llm.enabled or (ledger is not None and not ledger.can_spend()):
        return 0
    client = get_client(config, timeout=config.ask.timeout_seconds, retries=3)
    if client is None:
        return 0
    convs: dict[str, list[str]] = {}
    first: dict[str, int] = {}
    for row in store.conn.execute(
            "SELECT rowid, id, prov_source, extra, prov_activity FROM nodes WHERE type = 'document'").fetchall():
        extra = json.loads(row[3] or "{}")
        if row[4] not in activities or extra.get("kind") in ("conversation-summary", "conversation-fact"):
            continue
        cid = extra.get("conversation_id") or row[2] or row[1]
        convs.setdefault(cid, []).append(row[1])
        first[cid] = min(first.get(cid, row[0]), row[0])
    done = _claims_record(store)
    todo, keys = [], {}
    for cid in sorted(convs, key=lambda c: first[c]):
        chunks = _chunk_rows(store, convs[cid])
        keys[cid] = _claims_key(chunks)
        if done.get(cid) == keys[cid]:
            continue
        messages = conversation_messages(chunks)
        if not claims_apply(messages):
            continue  # named dialogue keeps its facts
        for m in messages:
            m["conversation_id"] = cid
            todo.append(m)
    by_conv: dict[str, list[tuple[dict, dict]]] = {}
    failed: set[str] = set()
    windows = claim_windows(todo)

    def read(window):
        try:
            return window, _read_claims(client, config, ledger, window, effort)
        except BudgetExhausted:
            return window, None

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(read, windows))
    else:
        results = [read(w) for w in windows]
    for window, got in results:
        if got is None:
            failed |= {m["conversation_id"] for m in window}
            continue
        for c, m in got:
            by_conv.setdefault(m["conversation_id"], []).append((c, m))
    count = 0
    for cid in dict.fromkeys(m["conversation_id"] for m in todo):
        if cid in failed:
            continue
        _write_claims(store, cid, by_conv.get(cid, []))
        done[cid] = keys[cid]
        count += 1
    _record_claims(store, done)
    return count


PROFILE_MIN_FACTS = 8
PROFILE_MAX_SUBJECTS = 6
PROFILE_MAX_CHARS = 160_000

PROFILE_PROMPT = """Below are dated facts recorded from conversations about {subject}{who}. Write a profile of {subject} that lets someone answer questions about them later without the conversations.

First, what was recorded, as short bullets with dates, under these headings where there is anything to say: Identity and background; Relationships (partner and relationship status, family, friends, pets); Work and education; Home and places (where they live, places they have been); Interests and activities; Preferences and dislikes; Values, beliefs and personality; Health and habits; Plans and goals; Possessions. When something changed, give the latest state and the earlier one with dates.

Then "Likely inferences": conclusions a careful reader would draw that the facts do not state outright (for example "likely single: said adoption would be hard as a single parent (2023-05-25)", "lives in Connecticut: adopted from a shelter in Stamford"), each marked likely and with its clue.

Use only these facts. At most 450 words.

Facts:
{facts}"""


def _profile_subjects(store: Store) -> dict[str, list[dict]]:
    rows = store.conn.execute(
        "SELECT id FROM nodes WHERE json_extract(extra, '$.kind') = 'conversation-fact'").fetchall()
    from .store import node_expired

    by: dict[str, list[dict]] = {}
    for node in (store.get_node(r[0]) for r in rows):
        if not node or node_expired(node):
            continue
        subject = " ".join(str((node.get("extra") or {}).get("subject") or "").split())
        if subject and subject.lower() not in ("assistant", "unknown", "none", "n/a"):
            by.setdefault(subject.lower(), []).append(node)
    return by


def profiles_enabled(config) -> bool:
    """Whether a digest also writes per-person profiles (`conversations.profiles`)."""
    return bool(getattr(getattr(config, "conversations", None), "profiles", False))


def build_profiles(store: Store, config, ledger=None) -> int:
    """A profile of each person the conversation facts are mostly about (the
    user, or the people in a conversation between friends), written once from
    their facts and kept until those facts change. `kin ask` shows the profiles
    of the people a question names, which answers a judgement ("would she be
    considered religious?") or a request for advice from a few hundred tokens
    instead of every excerpt. Returns the number written."""
    from .answer import BudgetExhausted, _call
    from .llm import get_client

    if not config.llm.enabled or (ledger is not None and not ledger.can_spend()):
        return 0
    subjects = _profile_subjects(store)
    ranked = sorted((k for k, v in subjects.items() if len(v) >= PROFILE_MIN_FACTS),
                    key=lambda k: -len(subjects[k]))[:PROFILE_MAX_SUBJECTS]
    existing = {
        str((n.get("extra") or {}).get("entity") or "").lower(): n
        for n in (store.get_node(r[0]) for r in store.conn.execute(
            "SELECT id FROM nodes WHERE json_extract(extra, '$.kind') = 'entity-profile'").fetchall())
        if n
    }
    for name, node in existing.items():
        if name not in ranked:
            store.delete_node(node["id"])
    client = None
    written = 0
    for subject in ranked:
        facts = sorted(subjects[subject], key=lambda n: (str((n.get("extra") or {}).get("fact_date") or ""),
                                                          n.get("prov_when") or ""))
        # The facts' revision, not only their ids: an edited fact keeps its id.
        key = hashlib.sha256(json.dumps(sorted(
            (n["id"], n.get("content") or "", (n.get("extra") or {}).get("fact_date") or "",
             (n.get("extra") or {}).get("expires") or "") for n in facts)).encode()).hexdigest()[:24]
        old = existing.get(subject)
        if old and (old.get("extra") or {}).get("key") == key:
            continue
        if client is None:
            client = get_client(config, timeout=config.ask.timeout_seconds, retries=3)
            if client is None:
                return written
        display = next((str((n.get("extra") or {}).get("subject")) for n in facts), subject)
        lines = [f"- {(n.get('extra') or {}).get('fact_date') or n.get('prov_when') or 'undated'}: "
                 f"{n.get('content') or n.get('title') or ''}" for n in facts]
        text = "\n".join(lines)[-PROFILE_MAX_CHARS:]
        who = " (the user)" if subject == "user" else ""
        try:
            profile = _call(client, config, system=None, ledger=ledger, purpose="conversation-profile",
                            user=PROFILE_PROMPT.format(subject=display, who=who, facts=text),
                            effort="low", max_tokens=8000)
        except BudgetExhausted:
            return written
        except Exception:
            continue
        if not profile.strip():
            continue
        expiries = [(n.get("extra") or {}).get("expires") for n in facts if (n.get("extra") or {}).get("expires")]
        extra = {"kind": "entity-profile", "entity": display, "key": key,
                 "conversations": sorted({str((n.get("extra") or {}).get("conversation_id") or n.get("prov_source"))
                                          for n in facts})}
        if expiries:
            extra["expires"] = min(expiries)  # rebuilt without the expired facts by the next digest
        nid = "convprofile-" + hashlib.sha256(subject.encode()).hexdigest()[:16]
        if store.get_node(nid):
            store.delete_node(nid)
        store.add_node(node_id=nid, title=f"Profile of {display}", content=profile.strip(), node_type="document",
                       prov_source="conversation-profile", prov_activity="conversation-digest",
                       prov_when=max((n.get("prov_when") or "" for n in facts), default=None) or None, extra=extra)
        written += 1
    return written
