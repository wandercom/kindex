"""Kindex MCP Server — agent plugin for the knowledge graph.

Exposes Kindex tools, resources, and prompts via the Model Context Protocol.
Run with: kin-mcp (stdio transport, for Claude Code, Codex, Gemini CLI,
Google Antigravity, OpenCode, Cursor, and other MCP clients)
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import sqlite3
import sys
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "Error: the 'mcp' package is not installed.\n"
        "Install with: pip install kindex[mcp]  (or: uv tool install kindex[mcp])\n"
        "See: https://github.com/jmcentire/kindex#installation",
        file=sys.stderr,
    )
    sys.exit(1)

mcp = FastMCP(
    "kindex",
    instructions=(
        "Kindex is a persistent knowledge graph that remembers across sessions. "
        "You MUST use these tools proactively — the user depends on this graph as external memory.\n\n"

        "## Session lifecycle\n"
        "1. START: `tag_start` or `tag_resume` to name this session\n"
        "2. ORIENT: `search` the current topic to see what's already known\n"
        "3. DURING: capture as you go (see node types below)\n"
        "4. END: `tag_update` with action='end' and a summary\n\n"

        "## What to capture (use `add` with the right node_type)\n"
        "- concept: patterns, facts, key files, domain terms, how things work\n"
        "- decision: architectural choices, trade-offs, why X over Y\n"
        "- question: open problems, things to investigate later\n"
        "- task: actionable work items — link to related concepts so they surface contextually\n"
        "- skill: demonstrated abilities with evidence\n"
        "- constraint: invariants that MUST hold (hard rules, with trigger/action: warn|verify|block)\n"
        "- directive: behavioral guidelines, style rules (soft rules with scope)\n"
        "- watch: things that need monitoring — known instabilities, flaky tests, "
        "APIs that might break, items needing periodic attention (set owner + expires)\n"
        "- checkpoint: pre-flight checklists — things to verify before an event\n\n"

        "## When to use each tool\n"
        "- `search`: ALWAYS before adding — if a matching node already exists, "
        "prefer `edit`/`supersede` over `add` (edit, don't re-add)\n"
        "- `add`: capture NEW discoveries as they happen — don't batch, don't wait\n"
        "- `edit`: correct or extend an EXISTING node instead of re-adding a near "
        "duplicate (additive types — decision/constraint/directive/checkpoint/watch — "
        "accept append/expires only)\n"
        "- `supersede`: replace an additive node when its content must actually change — "
        "creates a fresh node linked via a supersedes edge, history preserved\n"
        "- `candidate_list`/`candidate_show`: inspect quarantined automatic captures; "
        "use `candidate_accept` or `candidate_reject` only after explicit review\n"
        "- `verify`/`invalidate`: assert verification and valid-time state; use "
        "`trusted_only=True` on search/context for admission-controlled recall\n"
        "- `link`: when you notice two concepts relate — specify the relationship type "
        "(relates_to, depends_on, implements, contradicts, blocks, context_of)\n"
        "- `learn`: after reading long files/outputs — bulk-extracts multiple concepts at once\n"
        "- `task_add`: for work items — ALWAYS link to relevant concepts via link_to parameter\n"
        "- `task_claim`/`task_release`: coordinate shared work with expiring task claims\n"
        "- `task_done`/`task_list`: manage tasks — they surface contextually via graph proximity\n"
        "- `coord_start`/`coord_post`/`coord_read`/`coord_end`: short-lived agent coordination "
        "state; capture durable discoveries separately with `add` or `learn`\n"
        "- `coord_join`: become a member of a conversation — members get a read cursor "
        "(unread tracking) and receive standing messages in their session context\n"
        "- `coord_attach`: share a graph node with a conversation as a resource — "
        "members see who holds it when it is locked\n"
        "- `coord_inject`: set/clear/list standing messages pushed into member sessions "
        "until cleared (use for 'don't touch X until Y lands')\n"
        "- `lock_acquire`/`lock_release`: advisory node locks — `edit` refuses foreign "
        "locks; locks expire by TTL, so an expired lock never blocks anyone\n"
        "- `watch_add`: for ONGOING monitoring — flaky tests, unstable APIs, items to revisit. "
        "Set owner and expires. Watches surface in every session's context automatically.\n"
        "- `watch_resolve`: when a watched issue is fixed or no longer relevant\n"
        "- `remind_create`: for TIME-BASED triggers — use `action` for shell commands, "
        "`instructions` for Claude, or `wake` for Codex/OpenCode headless wakeups\n"
        "- `suggest`: check for bridge opportunities between disconnected graph clusters\n"
        "- `graph_heal`: diagnose graph health — find orphans, bridges, fading nodes\n"
        "- `graph_merge`: merge duplicate nodes (moves edges, archives source)\n"
        "- `ask`: query the graph conversationally\n\n"

        "## Linking strategy\n"
        "The graph's value is in connections. When you add a node, think: what does this relate to? "
        "Use `link` aggressively. Edge types: relates_to (general), depends_on (prerequisite), "
        "implements (realization), contradicts (tension), blocks (impediment), "
        "context_of (background), answers (resolves a question), supersedes (replaces)."
    ),
)

# ── Lazy singleton ────────────────────────────────────────────────────

_store = None
_config = None


def _reset_singletons():
    """Clear the store/config singletons so the next _get_store re-resolves.

    Registered with config._register_cache_invalidate so bind_root/unbind_root
    invalidate the MCP module-level cache (R1.1, R1.2, AMENDMENT 1 contract).
    """
    global _store, _config
    if _store is not None:
        try:
            _store.close()
        except Exception:
            pass
    _store = None
    _config = None


# Register the MCP singleton reset with config's cache-invalidation callback
# so bind_root/unbind_root clear the MCP store/config singletons (R1.2).
from .config import _register_cache_invalidate as _config_register_cb  # noqa: E402
_config_register_cb(_reset_singletons)


class MemoryUnavailableError(RuntimeError):
    """Store open/init failed; tools degrade to a typed error string
    instead of an unhandled exception."""

    def __init__(self, cause: BaseException):
        self.error_class = type(cause).__name__
        super().__init__(f"memory unavailable ({self.error_class})")


def _tool(*dargs, **dkwargs):
    """mcp.tool() plus the memory-unavailable guard: a broken store turns
    into a typed tool result on every tool, never a protocol error."""
    def decorate(fn):
        @functools.wraps(fn)
        def guarded(*a, **kw):
            try:
                return fn(*a, **kw)
            except MemoryUnavailableError as e:
                return f"Error: memory unavailable ({e.error_class})"
            except sqlite3.Error as e:
                # The store opened but a query hit a broken/locked DB
                # mid-session — same degraded contract as an open failure
                # (the open-time probe in _get_store catches corruption
                # that SQLite would otherwise defer past open).
                try:
                    from .config import record_degraded
                    record_degraded("mcp", e, config=_config)
                except Exception:
                    pass
                return f"Error: memory unavailable ({type(e).__name__})"
        return mcp.tool(*dargs, **dkwargs)(guarded)
    return decorate


def _get_store():
    """Lazy-init Store and Config singletons.

    A config-load or store-open failure records a degraded-ledger event
    and raises MemoryUnavailableError for the _tool guard; the failed
    singleton stays unset so a later call may recover.
    """
    global _store, _config
    if _store is None:
        from .config import load_config, record_degraded
        from .store import Store

        try:
            _config = load_config()
        except Exception as e:
            record_degraded("mcp", e)
            raise MemoryUnavailableError(e) from e
        try:
            _store = Store(_config)
            # Force the deferred SQLite open + schema check now: a corrupt
            # DB file must surface HERE as an open/init failure, not as a
            # raw sqlite3 error on the first query inside a tool.
            _store.conn
        except Exception as e:
            broken, _store = _store, None
            try:
                if broken is not None:
                    broken.close()
            except Exception:
                pass
            record_degraded("mcp", e, config=_config)
            raise MemoryUnavailableError(e) from e
        atexit.register(_store.close)
    return _store, _config


def _get_config():
    """Config from the lazy singleton (shares _get_store init)."""
    _, config = _get_store()
    return config


def _default_agent(agent: str = "") -> str:
    """Explicit agent name, or the resolved stable agent identity."""
    if agent and agent.strip():
        return agent.strip()
    from .config import resolve_agent_id
    return resolve_agent_id(_get_config())


def _mcp_client() -> str | None:
    """Client identity used to scope pulled context, from the KIN_CLIENT env.

    A per-client MCP config (this server runs one instance per client) can set
    KIN_CLIENT so context tools drop nodes scoped to a different client. Unset
    means no scoping — the unchanged default.
    """
    from .agent_adapters import normalize_adapter
    raw = os.environ.get("KIN_CLIENT") or os.environ.get("KINDEX_CLIENT")
    return normalize_adapter(raw) if raw else None


def _scope_results(results: list[dict], client: str | None) -> list[dict]:
    """Drop search hits scoped to a different client, mirroring prime's retrieval
    filter so context tools scope both search hits and operational nodes."""
    if not client:
        return results
    from .agent_adapters import adapter_scoped_out
    return [r for r in results if not adapter_scoped_out(r.get("tags"), client)]


def _json(obj: Any, **kw) -> str:
    """JSON serialize with date/path handling."""
    import datetime
    from pathlib import Path

    def default(o):
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(f"Not JSON serializable: {type(o)}")

    return json.dumps(obj, default=default, **kw)


def operation_now() -> str:
    """One normalized UTC instant for a time-dependent MCP operation.

    The seam is intentionally monkeypatchable in-process and is not exposed as
    an MCP argument.
    """
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _state_error(exc: ValueError) -> str:
    """Stable machine prefix for state-resilience adapter errors."""
    from .store import (
        CandidateNotFoundError,
        CandidateStateError,
        InvalidIntervalError,
        StaleReviewError,
        TitleCollisionError,
    )

    if isinstance(exc, CandidateNotFoundError):
        code = "candidate_not_found"
    elif isinstance(exc, CandidateStateError):
        code = "candidate_state"
    elif isinstance(exc, StaleReviewError):
        code = "stale_review"
    elif isinstance(exc, TitleCollisionError):
        code = "title_collision"
    elif isinstance(exc, InvalidIntervalError):
        code = "invalid_interval"
    else:
        code = "invalid_input"
    return f"Error: {code}: {exc}"


def _node_summary(node: dict) -> str:
    """One-line summary of a node."""
    ntype = node.get("type", "concept")
    title = node.get("title", node.get("id", "?"))
    weight = node.get("weight", 0)
    return f"[{ntype}] {title} (w={weight:.2f}, id={node['id']})"


def _node_detail(store, node: dict) -> str:
    """Multi-line detail view of a node with edges."""
    lines = [
        f"# {node.get('title', node['id'])}",
        f"Type: {node.get('type', 'concept')}  |  Weight: {node.get('weight', 0):.2f}  |  "
        f"Audience: {node.get('audience', 'private')}",
        f"ID: {node['id']}",
    ]
    if node.get("domains"):
        lines.append(f"Tags: {', '.join(node.get('tags') or node.get('domains') or [])}")
    if node.get("aka"):
        lines.append(f"AKA: {', '.join(node['aka'])}")
    if node.get("content"):
        lines.append(f"\n{node['content']}")

    edges = store.edges_from(node["id"], semantic_only=True)
    if edges:
        lines.append(f"\n## Connections ({len(edges)})")
        for e in edges[:20]:
            lines.append(f"  -> {e.get('to_title', e['to_id'])} ({e['type']}, w={e['weight']:.2f})")

    prov_parts = []
    if node.get("prov_who"):
        prov_parts.append(f"who={node['prov_who']}")
    if node.get("prov_activity"):
        prov_parts.append(f"activity={node['prov_activity']}")
    if node.get("prov_source"):
        prov_parts.append(f"source={node['prov_source']}")
    if prov_parts:
        lines.append(f"\nProvenance: {', '.join(prov_parts)}")

    extra = node.get("extra")
    if extra and isinstance(extra, dict):
        state = extra.get("current_state")
        if state:
            lines.append(f"\nState: {_json(state)}")

    return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────


@_tool()
def search(query: str, top_k: int = 10, tags: str = "",
           include_archived: bool = False,
           trusted_only: bool = False) -> str:
    """Search the knowledge graph with hybrid FTS5 + graph traversal.

    USE THIS: before starting work on a topic, before adding nodes (to avoid
    duplicates), and whenever you need context about a concept.

    Uses Reciprocal Rank Fusion to merge full-text and graph results.
    Returns ranked nodes with scores.

    Args:
        query: Search query text.
        top_k: Maximum results to return.
        tags: Comma-separated tags to filter results (only nodes with these tags).
        include_archived: Include archived nodes (fenced from default search).
        trusted_only: Admit only current, explicitly verified, non-contradicted
            knowledge. False preserves ordinary legacy-compatible recall.
    """
    store, _ = _get_store()
    from .retrieve import hybrid_search

    fence_stats: dict = {}
    grounding: dict = {}
    fetch_k = top_k * 3 if tags else top_k
    evaluation_time = operation_now() if trusted_only else None
    results = hybrid_search(store, query, top_k=fetch_k,
                            include_archived=include_archived,
                            fence_stats=fence_stats,
                            trusted_only=trusted_only,
                            evaluation_time=evaluation_time,
                            grounding=grounding)

    # The tag filter applies identically to results and fenced candidates
    # so the fence note reflects the same filter set the results use.
    fenced_nodes = fence_stats.get("fenced_nodes", [])
    if tags:
        filter_tags = {t.strip().lower() for t in tags.split(",") if t.strip()}

        def _tag_match(r):
            return bool(filter_tags
                        & {d.lower() for d in (r.get("domains") or r.get("tags") or [])})

        results = [r for r in results if _tag_match(r)]
        results = results[:top_k]
        fenced_nodes = [r for r in fenced_nodes if _tag_match(r)]

    # The fence note is derived in a single place both surfaces call (R3.1).
    from .retrieve import build_fence_note
    fence_note = build_fence_note(results, fenced_nodes, top_k,
                                  include_archived,
                                  candidate_count=fence_stats.get("candidate_count", 0))
    trust_note = ""
    if trusted_only:
        from .retrieve import build_trust_note
        trust_note = build_trust_note(fence_stats.get("trusted_omissions"))

    ground_note = ""
    verdict = grounding.get("verdict")
    if verdict is not None:
        try:
            ground_note = verdict.note()
        except Exception:
            ground_note = ""

    if not results:
        notes = [note for note in (fence_note, trust_note, ground_note) if note]
        return "No results found." + (f"\n{' '.join(notes)}" if notes else "")

    from .retrieve import _node_age_str, _staleness_caveat

    lines = []
    if ground_note:
        lines.append(ground_note)
    lines.append(f"Found {len(results)} results for '{query}':\n")
    for i, r in enumerate(results, 1):
        score = r.get("rrf_score", 0) or r.get("confidence", 0)
        age = _node_age_str(r)
        caveat = _staleness_caveat(r)
        age_tag = f", {age}" if age else ""
        lines.append(f"{i}. [{r.get('type', 'concept')}] {r.get('title', r['id'])} "
                      f"(score={score:.3f}, id={r['id']}{age_tag}){caveat}")
        content = (r.get("content") or "")[:150]
        if content:
            lines.append(f"   {content}")
    if fence_note:
        lines.append(fence_note)
    if trust_note:
        lines.append(trust_note)
    return "\n".join(lines)


@_tool()
def add(
    text: str,
    node_type: str = "concept",
    tags: str = "",
    domains: str = "",
    audience: str = "private",
    referent: str = "",
    referent_digest: str = "",
    referent_scope: str = "",
    asserted_at: str = "",
    true_of: str = "",
) -> str:
    """Add a knowledge node to the graph. ALWAYS `search` first to avoid duplicates.

    Choose the right node_type — it determines how the node behaves:
    - concept: facts, patterns, key files, domain terms (default, most common)
    - decision: "we chose X over Y because..." — architectural choices with rationale
    - question: open problems to investigate later — gets surfaced until answered
    - task: actionable work — prefer `task_add` instead (supports priority/due/linking)
    - skill: demonstrated ability with evidence
    - constraint: hard rule that MUST hold — set trigger/action in text (e.g. "never push to main without tests")
    - directive: soft behavioral guideline with scope (e.g. "use snake_case in Python modules")
    - watch: something needing periodic attention — flaky test, unstable API, known tech debt.
      Set owner and expiry in text. Gets surfaced in every session until resolved or expired.
    - checkpoint: pre-flight checklist item — verify before a specific event

    Args:
        text: The knowledge to capture (becomes title + content). Be specific.
        node_type: See types above. Default: concept.
        tags: Comma-separated tags for contextual surfacing (e.g. "kindex,python").
        domains: Alias for tags (deprecated, use tags instead).
        audience: Visibility scope (private, team, org, public). Default: private.
        referent: Path or URL the claim describes (R0 binding). File paths
            are hashed now — use an absolute path (the MCP server's cwd is
            not the project's) or supply referent_digest.
        referent_digest: Explicit content digest (required for url/repo
            scope; sha256 hex, or 7-64 hex commit for repo scope).
        referent_scope: file | url | repo (default: url for URLs, file
            otherwise).
        asserted_at: RFC3339 claim time (default: now when binding).
        true_of: RFC3339 instant the referent was observed in the digested
            state (default: asserted_at).
    """
    store, config = _get_store()
    from .extract import keyword_extract

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    domain_list = [d.strip() for d in domains.split(",") if d.strip()] if domains else []

    binding: dict = {}
    if referent:
        from pathlib import Path

        from .referent import ReferentError, hash_file, validate_referent

        scope = referent_scope or ("url" if "://" in referent else "file")
        digest = referent_digest
        if not digest:
            if scope != "file":
                return (f"Error: referent_digest is required for scope "
                        f"'{scope}'")
            try:
                digest = hash_file(Path(referent))
            except OSError as e:
                return (f"Error: cannot hash referent file '{referent}' "
                        f"({e}); pass an absolute path or referent_digest")
        key = "url" if scope == "url" else "path"
        ref = {key: referent, "content_digest": digest, "digest_scope": scope}
        try:
            validate_referent(ref)
        except ReferentError as e:
            return f"Error: {e}"
        binding["referent"] = ref
    if asserted_at:
        binding["asserted_at"] = asserted_at
    if true_of:
        binding["true_of"] = true_of

    # Create the node
    title = text[:60].strip()
    try:
        nid = store.add_node(
            title=title,
            content=text,
            node_type=node_type,
            domains=domain_list,
            tags=tag_list,
            audience=audience,
            prov_activity="mcp-add",
            **binding,
        )
    except ValueError as e:
        return f"Error: {e}"

    # Try auto-linking
    existing_titles = [n["title"] for n in store.all_nodes(limit=200)]
    extraction = keyword_extract(text, existing_titles=existing_titles)
    link_count = 0
    for conn in extraction.get("connections", []):
        target = store.get_node_by_title(conn.get("to_title", ""))
        if target and target["id"] != nid:
            store.add_edge(nid, target["id"], edge_type="relates_to", weight=0.4,
                           provenance="auto-linked via MCP")
            link_count += 1

    return f"Created node: {nid} ({node_type})" + (
        f" with {link_count} auto-link(s)" if link_count else ""
    )


@_tool()
def edit(node_id: str, title: str = "", content: str = "", append: str = "",
         add_tags: str = "", remove_tags: str = "", intent: str = "",
         expires: str = "", force: bool = False) -> str:
    """Edit a node in place (policy-aware). Accepts a node ID or exact title.

    Edit classes by node type:
    - editable (concept, document, artifact, skill, person, project, question):
      every field below is allowed
    - additive (decision, constraint, directive, checkpoint, watch): history
      matters — only append and expires; use `supersede` to replace
    - managed (task, session, coordination): refused — use the dedicated
      task/session/coordination tools

    Args:
        node_id: Node ID or exact title.
        title: Replace the title.
        content: Replace the content.
        append: Append a dated addendum block to the content.
        add_tags: Comma-separated tags to add.
        remove_tags: Comma-separated tags to remove.
        intent: Replace the intent.
        expires: Set an expiry date (YYYY-MM-DD) — expired nodes stop surfacing
            and are archived by the daemon.
        force: Override a foreign advisory lock.
    """
    store, config = _get_store()
    from .store import EditPolicyError, LockHeldError

    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"

    fields = {
        "title": title or None,
        "content": content or None,
        "append": append or None,
        "add_tags": [t.strip() for t in add_tags.split(",") if t.strip()] or None,
        "remove_tags": [t.strip() for t in remove_tags.split(",") if t.strip()] or None,
        "intent": intent or None,
        "expires": expires or None,
    }
    provided = {k: v for k, v in fields.items() if v is not None}
    if not provided:
        return ("Error: edit requires at least one field (title, content, "
                "append, add_tags, remove_tags, intent, expires).")

    try:
        updated = store.edit_node(
            node["id"],
            actor=_default_agent(),
            force=force,
            policy_overrides=config.edit_policy or None,
            **provided,
        )
    except (EditPolicyError, LockHeldError, ValueError) as e:
        return f"Error: {e}"

    return (f"Edited {updated.get('title', '')} ({updated['id']}) — "
            f"fields: {', '.join(sorted(provided))}")


@_tool()
def supersede(node_id: str, new_text: str, expires: str = "", reason: str = "") -> str:
    """Replace a node with a fresh one, preserving history. Accepts ID or title.

    Creates a new node (same type/tags/audience/intent), links it with a
    `supersedes` edge, marks the old node status='superseded', and migrates
    its retrieval pheromone. Use this instead of `edit` for additive types
    (decision, constraint, directive, checkpoint, watch) when the content
    must change rather than grow.

    Args:
        node_id: Node ID or exact title of the node to replace.
        new_text: Full replacement text (becomes title + content).
        expires: Optional expiry date for the new node (YYYY-MM-DD).
        reason: Why the node is being replaced (kept as provenance).
    """
    store, config = _get_store()
    from .store import LockHeldError

    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"

    try:
        new = store.supersede_node(
            node["id"], new_text,
            actor=_default_agent(),
            expires=expires or None,
            reason=reason or None,
            policy_overrides=config.edit_policy or None,
        )
    except (LockHeldError, ValueError) as e:
        return f"Error: {e}"

    return f"Superseded {node['title']} ({node['id']}) -> new node {new['id']}"


@_tool()
def context(
    topic: str = "",
    level: str = "abridged",
    max_tokens: int = 0,
    trusted_only: bool = False,
) -> str:
    """Get a formatted context block for injection into conversation.

    Args:
        topic: Topic to search for (auto-detects from cwd if empty).
        level: Context tier (full, abridged, summarized, executive, index).
        max_tokens: Token budget (overrides level with auto-selection if set).
        trusted_only: Admit only current, explicitly verified,
            non-contradicted knowledge. False preserves ordinary recall.
    """
    store, _ = _get_store()
    from .retrieve import build_trust_note, format_context_block, hybrid_search
    from .store import node_expired, node_retired

    evaluation_time = None
    fence_stats: dict = {}
    if trusted_only:
        evaluation_time = operation_now()

    client = _mcp_client()
    if topic:
        results = hybrid_search(
            store,
            topic,
            top_k=15,
            trusted_only=trusted_only,
            evaluation_time=evaluation_time,
            fence_stats=fence_stats,
        )
    else:
        # Fall back to recent high-weight nodes (skip expired and
        # retired knowledge — archived/superseded stays retired here too)
        recent = store.recent_nodes(n=15)
        if trusted_only:
            from .trust import parse_rfc3339
            today = parse_rfc3339(
                evaluation_time, field="evaluation_time"
            ).date().isoformat()
            expired_count = sum(
                1 for result in recent if node_expired(result, today=today)
            )
            inactive_count = sum(
                1 for result in recent
                if not node_expired(result, today=today) and node_retired(result)
            )
            results = [
                result for result in recent
                if not node_expired(result, today=today) and not node_retired(result)
            ]
        else:
            results = [
                result for result in recent
                if not node_expired(result) and not node_retired(result)
            ]
        if trusted_only:
            from .trust import filter_trusted_nodes
            results, omissions = filter_trusted_nodes(
                store, results, at=evaluation_time
            )
            omissions["invalidated"] = (
                omissions.get("invalidated", 0) + expired_count
            )
            omissions["inactive"] = omissions.get("inactive", 0) + inactive_count
            fence_stats["trusted_omissions"] = omissions
    results = _scope_results(results, client)

    if not results:
        result = "No relevant knowledge found."
        if trusted_only:
            result += "\n" + build_trust_note(fence_stats.get("trusted_omissions"))
        return result

    kwargs = {"level": level}
    if max_tokens > 0:
        kwargs = {"max_tokens_approx": max_tokens}

    result = format_context_block(
        store,
        results,
        query=topic,
        adapter=client,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
        **kwargs,
    )
    if trusted_only:
        result = result.rstrip() + "\n" + build_trust_note(
            fence_stats.get("trusted_omissions")
        )
    return result


@_tool()
def candidate_list(status: str = "", limit: int = 20) -> Any:
    """List quarantined automatic-capture receipts without payload fields.

    Args:
        status: Optional pending/conflicted/accepted/rejected/expired filter.
        limit: Maximum receipts to return.
    """
    store, _ = _get_store()
    try:
        return store.list_capture_candidates(status=status, limit=limit)
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def candidate_show(candidate_id: str) -> Any:
    """Show the exact untrusted candidate payload plus its freshness token.

    The returned token proves snapshot freshness only. It is not caller
    authentication, authorization, or proof of reviewer identity.
    """
    store, _ = _get_store()
    candidate = store.get_capture_candidate(candidate_id)
    if candidate is None:
        from .store import CandidateNotFoundError
        return _state_error(CandidateNotFoundError(f"Candidate not found: {candidate_id}"))
    candidate["review_token"] = store.candidate_review_token(candidate_id)
    return candidate


@_tool()
def candidate_accept(
    candidate_id: str,
    review_token: str,
    reviewed_by: str,
    prov_method: str,
    valid_at: str = "",
    invalid_at: str = "",
) -> Any:
    """Accept a fresh candidate with asserted reviewer and verification method."""
    store, _ = _get_store()
    operation_instant = operation_now()
    try:
        return store.accept_capture_candidate(
            candidate_id,
            review_token=review_token,
            reviewed_by=reviewed_by,
            prov_method=prov_method,
            valid_at=valid_at or None,
            invalid_at=invalid_at or None,
            now=operation_instant,
        )
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def candidate_reject(
    candidate_id: str,
    reviewed_by: str,
    disposition_code: str,
) -> Any:
    """Reject and minimize a live capture candidate.

    Args:
        candidate_id: Exact capture-candidate ID.
        reviewed_by: Asserted reviewer identifier.
        disposition_code: Bounded machine disposition code.
    """
    store, _ = _get_store()
    operation_instant = operation_now()
    try:
        return store.reject_capture_candidate(
            candidate_id,
            reviewed_by=reviewed_by,
            disposition_code=disposition_code,
            now=operation_instant,
        )
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def candidate_prune() -> Any:
    """Expire due pending/conflicted candidates; never promotes knowledge."""
    store, _ = _get_store()
    operation_instant = operation_now()
    try:
        return {"pruned": store.prune_capture_candidates(now=operation_instant)}
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def candidate_erase(candidate_id: str) -> Any:
    """Erase a candidate or minimized review receipt by exact ID."""
    store, _ = _get_store()
    return {"id": candidate_id, "erased": store.erase_capture_candidate(candidate_id)}


@_tool()
def verify(
    node_id: str,
    verified_by: str,
    prov_method: str,
    verified_at: str = "",
    valid_at: str = "",
    invalid_at: str = "",
) -> Any:
    """Assert node verification and an optional half-open valid interval.

    Reviewer identity is asserted audit text within the local trust boundary;
    this tool does not authenticate it.
    """
    store, _ = _get_store()
    operation_instant = operation_now()
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if node is None:
        return "Error: invalid_input: Node not found: " + node_id
    try:
        return store.verify_node(
            node["id"],
            verified_by=verified_by,
            prov_method=prov_method,
            verified_at=verified_at or operation_instant,
            valid_at=valid_at or None,
            invalid_at=invalid_at or None,
        )
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def invalidate(
    node_id: str,
    invalidated_by: str,
    disposition_code: str,
    invalid_at: str = "",
) -> Any:
    """Set a node's exclusive valid-time end without deleting it.

    Args:
        node_id: Durable node ID or exact title.
        invalidated_by: Asserted invalidating actor.
        disposition_code: Bounded machine invalidation reason.
        invalid_at: Optional timezone-aware RFC 3339 exclusive end time.
    """
    store, _ = _get_store()
    operation_instant = operation_now()
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if node is None:
        return "Error: invalid_input: Node not found: " + node_id
    try:
        return store.invalidate_node(
            node["id"],
            invalidated_by=invalidated_by,
            disposition_code=disposition_code,
            invalid_at=invalid_at or operation_instant,
        )
    except ValueError as exc:
        return _state_error(exc)


@_tool()
def show(node_id: str) -> str:
    """Show full details of a node including edges and provenance.

    Args:
        node_id: Node ID or title to look up.
    """
    store, _ = _get_store()
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"
    return _node_detail(store, node)


@_tool()
def link(
    node_a: str,
    node_b: str,
    relationship: str = "relates_to",
    weight: float = 0.5,
    reason: str = "",
) -> str:
    """Create an edge between two nodes. Links are the graph's primary value — use liberally.

    Choose the right relationship type:
    - relates_to: general connection (default)
    - depends_on: A requires B to function
    - implements: A is a concrete realization of B
    - contradicts: A and B are in tension
    - blocks: A prevents progress on B
    - answers: A resolves question B
    - supersedes: A replaces B
    - context_of: A provides background for B
    - spawned_from: A was derived from B
    - exemplifies: A is an example of B

    Args:
        node_a: Source node ID or title.
        node_b: Target node ID or title.
        relationship: Edge type (see above). Default: relates_to.
        weight: Edge strength 0.0-1.0. Use 0.7+ for strong connections, 0.3-0.5 for weak ones.
        reason: Why this connection exists (stored as provenance — always provide this).
    """
    store, _ = _get_store()
    a = store.get_node(node_a) or store.get_node_by_title(node_a)
    b = store.get_node(node_b) or store.get_node_by_title(node_b)
    if not a:
        return f"Source node not found: {node_a}"
    if not b:
        return f"Target node not found: {node_b}"

    store.add_edge(a["id"], b["id"], edge_type=relationship, weight=weight,
                   provenance=reason or "linked via MCP")
    return f"Linked: {a['title']} -> {b['title']} ({relationship}, w={weight})"


@_tool()
def list_nodes(
    node_type: str = "",
    status: str = "",
    audience: str = "",
    tags: str = "",
    limit: int = 100,
) -> str:
    """List nodes in the knowledge graph with optional filters.

    Args:
        node_type: Filter by type (concept, decision, skill, person, project, etc.).
        status: Filter by status (active, archived, deprecated).
        audience: Filter by audience (private, team, org, public).
        tags: Filter by tags (comma-separated, AND logic — node must have all).
        limit: Maximum number of nodes to return.
    """
    store, _ = _get_store()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    nodes = store.all_nodes(
        node_type=node_type or None,
        status=status or None,
        audience=audience or None,
        tags=tag_list,
        limit=limit,
    )
    if not nodes:
        return "No nodes found matching filters."

    lines = [f"{len(nodes)} node(s):\n"]
    for n in nodes:
        lines.append(_node_summary(n))
    return "\n".join(lines)


@_tool()
def status() -> str:
    """Get knowledge graph health and statistics.

    Returns node/edge counts, type distribution, orphan count,
    and active operational nodes (constraints, watches, directives).
    """
    store, _ = _get_store()
    stats = store.stats()
    op = store.operational_summary()

    lines = [
        "# Kindex Status\n",
        f"Nodes: {stats['semantic_nodes']} semantic",
        f"Edges: {stats['edges']} semantic",
        f"Orphans: {stats['orphans']} semantic",
    ]
    stored_nodes = stats["stored_nodes"]
    stored_edges = stats["stored_edges"]
    excluded_nodes = stored_nodes - stats["semantic_nodes"]
    excluded_edges = stored_edges - stats["edges"]
    if excluded_nodes or excluded_edges:
        lines.extend([
            f"Stored nodes: {stored_nodes} ({excluded_nodes} lifecycle)",
            f"Stored edges: {stored_edges} ({excluded_edges} excluded)",
            f"  Domain-derived: {stats.get('ignored_domain_edges', 0)}",
            f"  Session-linked: {stats.get('ignored_session_edges', 0)}",
        ])
        if stats.get("ignored_other_edges", 0):
            lines.append(f"  Unresolved: {stats['ignored_other_edges']}")

    type_counts = stats.get("types", {})
    if type_counts:
        lines.append("\n## Node Types")
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {c}")

    constraints = op.get("constraints", [])
    if constraints:
        lines.append(f"\n## Active Constraints ({len(constraints)})")
        for c in constraints[:10]:
            lines.append(f"  - {c.get('title', c['id'])}")

    watches = op.get("watches", [])
    if watches:
        lines.append(f"\n## Active Watches ({len(watches)})")
        for w in watches[:10]:
            lines.append(f"  - {w.get('title', w['id'])}")

    return "\n".join(lines)


@_tool()
def ask(question: str) -> str:
    """Ask a question of the knowledge graph.

    Classifies the question type (factual, procedural, decision, exploratory),
    searches for relevant knowledge, and returns a formatted answer.

    Args:
        question: Natural language question.
    """
    store, config = _get_store()
    from .retrieve import format_context_block, hybrid_search

    # Simple question classification
    q_lower = question.lower()
    if any(p in q_lower for p in ["how do i", "how to", "steps to", "guide to"]):
        qtype = "procedural"
    elif any(p in q_lower for p in ["should i", "which is better", " vs ", "pros and cons"]):
        qtype = "decision"
    elif any(p in q_lower for p in ["what is", "who is", "when did", "define"]):
        qtype = "factual"
    else:
        qtype = "exploratory"

    top_k = {"factual": 5, "procedural": 8, "decision": 10, "exploratory": 12}.get(qtype, 10)
    client = _mcp_client()
    grounding: dict = {}
    results = _scope_results(
        hybrid_search(store, question, top_k=top_k, grounding=grounding), client)

    if not results:
        return f"[{qtype}] No relevant knowledge found for: {question}"

    # The verdict rides through format_context_block, which is the single place
    # rows become context text — so `ask` does not re-implement the gate, it
    # just hands the verdict to the canonical renderer.
    level = "full" if qtype in ("procedural", "decision") else "abridged"
    block = format_context_block(store, results, query=question, level=level,
                                 adapter=client, grounding=grounding)
    return f"[{qtype} question]\n\n{block}"


@_tool()
def suggest(limit: int = 10) -> str:
    """Show pending bridge opportunity suggestions.

    These are potential connections between concepts that Kindex detected
    but hasn't confirmed yet.

    Args:
        limit: Maximum suggestions to show.
    """
    store, _ = _get_store()
    suggestions = store.pending_suggestions(limit=limit)
    if not suggestions:
        return "No pending suggestions."

    lines = [f"{len(suggestions)} pending suggestion(s):\n"]
    for s in suggestions:
        lines.append(f"  #{s['id']}: {s['concept_a']} <-> {s['concept_b']}")
        if s.get("reason"):
            lines.append(f"      Reason: {s['reason']}")
    return "\n".join(lines)


def _is_substantive_concept(concept: dict) -> bool:
    """Reject empty or title-only extraction results.

    The keyword-extraction fallback emits bare capitalized phrases / quoted
    terms with no content and no domains. Historically these were created as
    title-only concept nodes that linked to nothing and steadily inflated the
    orphan count (the mcp-learn orphan bug). A concept is only worth a node if
    it carries some information beyond a short title.
    """
    title = (concept.get("title") or "").strip()
    if len(title) < 3:
        return False
    content = (concept.get("content") or "").strip()
    domains = concept.get("domains") or []
    return bool(content) or bool(domains)


@_tool()
def learn(text: str) -> str:
    """Extract knowledge from text and add it to the graph.

    USE THIS: after reading long files, articles, command outputs, or completing
    complex multi-step tasks. Summarize what happened and pass the text here
    for automatic concept extraction and linking.

    Analyzes the text for concepts, decisions, questions, and connections.
    Creates nodes and links automatically. Every extracted concept is grounded
    to a source node so it can never orphan.

    Args:
        text: Text to extract knowledge from (session notes, documentation, etc.).
    """
    store, config = _get_store()
    from .budget import BudgetLedger
    from .extract import extract

    ledger = BudgetLedger(config.ledger_path, config.budget)
    existing = [n["title"] for n in store.all_nodes(limit=200)]

    extraction = extract(text, existing, config, ledger)

    linked = 0

    # Reject low-information / empty extraction results before creating nodes.
    concepts = [c for c in extraction.get("concepts", [])[:10]
                if _is_substantive_concept(c)]
    rejected = len(extraction.get("concepts", [])[:10]) - len(concepts)

    created_ids: list[str] = []
    grounded_ids: list[str] = []  # created + matched-existing, all linked to source

    for concept in concepts:
        existing_node = store.get_node_by_title(concept["title"])
        if existing_node:
            grounded_ids.append(existing_node["id"])
            continue
        nid = store.add_node(
            title=concept["title"],
            content=concept.get("content", ""),
            node_type=concept.get("type", "concept"),
            domains=concept.get("domains", []),
            prov_activity="mcp-learn",
        )
        created_ids.append(nid)
        grounded_ids.append(nid)

    created = len(created_ids)

    # Ground every concept to a source node so newly created concepts never
    # orphan, even when no connections are extracted. This is the structural
    # fix for the mcp-learn orphan bug: provenance lives in the graph, not just
    # in prov_activity.
    if created_ids:
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        source_id = store.add_node(
            title=f"Learned: {first_line[:72]}" if first_line else "Learned note",
            content=text.strip()[:500],
            node_type="document",
            prov_activity="mcp-learn-source",
            prov_why="source text passed to learn()",
        )
        for cid in grounded_ids:
            store.add_edge(source_id, cid,
                           edge_type="context_of",
                           weight=0.4,
                           provenance="extracted via learn()")

    for conn in extraction.get("connections", []):
        a = store.get_node_by_title(conn.get("from_title", ""))
        b = store.get_node_by_title(conn.get("to_title", ""))
        if a and b and a["id"] != b["id"]:
            store.add_edge(a["id"], b["id"],
                           edge_type=conn.get("type", "relates_to"),
                           weight=0.4,
                           provenance=conn.get("why", "extracted via MCP"))
            linked += 1

    decisions = extraction.get("decisions", [])
    questions = extraction.get("questions", [])
    bridges = extraction.get("bridge_opportunities", [])

    # Store bridge suggestions
    for bridge in bridges[:5]:
        store.add_suggestion(
            concept_a=bridge.get("concept_a", ""),
            concept_b=bridge.get("concept_b", ""),
            reason=bridge.get("potential_link", ""),
            source="mcp-learn",
        )

    parts = [f"Extracted: {created} concept(s), {linked} link(s)"]
    if rejected:
        parts.append(f"{rejected} low-info concept(s) rejected")
    if decisions:
        parts.append(f"{len(decisions)} decision(s)")
    if questions:
        parts.append(f"{len(questions)} question(s)")
    if bridges:
        parts.append(f"{len(bridges)} bridge suggestion(s)")
    return ", ".join(parts)


@_tool()
def graph_stats() -> str:
    """Get graph analytics: density, components, centrality, and communities."""
    store, _ = _get_store()
    from .graph import store_bridges, store_centrality, store_communities, store_stats

    stats = store_stats(store)
    centrality = store_centrality(store, method="betweenness", top_k=5)
    communities = store_communities(store)

    lines = [
        "# Graph Analytics\n",
        f"Nodes: {stats['semantic_nodes']} semantic",
        f"Edges: {stats['edges']} semantic",
        f"Density: {stats.get('density', 0):.4f}",
        f"Components: {stats.get('components', 0)}",
        f"Avg Degree: {stats.get('avg_degree', 0):.1f}",
    ]
    stored_nodes = stats["stored_nodes"]
    stored_edges = stats["stored_edges"]
    excluded_nodes = stored_nodes - stats["semantic_nodes"]
    excluded_edges = stored_edges - stats["edges"]
    if excluded_nodes or excluded_edges:
        lines.extend([
            f"Stored nodes: {stored_nodes} ({excluded_nodes} lifecycle)",
            f"Stored edges: {stored_edges} ({excluded_edges} excluded)",
            f"  Domain-derived: {stats.get('ignored_domain_edges', 0)}",
            f"  Session-linked: {stats.get('ignored_session_edges', 0)}",
        ])
        if stats.get("ignored_other_edges", 0):
            lines.append(f"  Unresolved: {stats['ignored_other_edges']}")
    if stats.get("truncated"):
        lines.append(f"\n*Note: graph analysis used a subset of nodes. "
                     f"Density/centrality/community stats are approximate.*")

    if centrality:
        lines.append("\n## Top Nodes (Betweenness Centrality)")
        for nid, title, score in centrality:
            lines.append(f"  {title}: {score:.4f}")

    if communities:
        lines.append(f"\n## Communities ({len(communities)})")
        for i, comm in enumerate(communities[:5], 1):
            members = ", ".join(n.get("title", n["id"]) for n in comm[:5])
            lines.append(f"  Cluster {i} ({len(comm)} nodes): {members}")

    return "\n".join(lines)


@_tool()
def graph_heal() -> str:
    """Diagnose and report graph health issues with actionable recommendations.

    Reports:
    - Orphan nodes (no connections) — candidates for linking or archival
    - Disconnected components — candidates for cross-component links
    - Low-weight nodes approaching archive threshold
    - Bridge edges (single points of failure in the graph)

    Use this to understand what needs attention, then use `link`, `add`,
    or other tools to fix issues.
    """
    # Read-only by design: graph_heal performs no merges today. If a merge
    # is ever added here, it must call snapshots.snapshot_db first
    # (PRD lineage item 2 — pre-merge snapshot stopgap).
    store, _ = _get_store()
    from .graph import store_bridges, store_stats

    lines = ["# Graph Health Report\n"]

    # Stats overview
    stats = store_stats(store)
    lines.append(f"Semantic nodes: {stats['semantic_nodes']}, "
                 f"semantic edges: {stats['edges']}, "
                 f"Components: {stats.get('components', 0)}, "
                 f"Density: {stats.get('density', 0):.4f}\n")

    # Orphans
    orphans = store.orphans()
    if orphans:
        lines.append(f"## Orphans ({len(orphans)} — need links or archival)")
        for o in orphans[:10]:
            weight = o.get('weight', 0)
            lines.append(f"  - [{o.get('type', '?')}] {o['title']} "
                         f"(id={o['id']}, w={weight:.2f})")
            if weight < 0.15:
                lines.append(f"    -> Low weight, candidate for archival")
            else:
                lines.append(f"    -> Use `link` to connect to related nodes")
        if len(orphans) > 10:
            lines.append(f"  ... and {len(orphans) - 10} more")
    else:
        lines.append("## Orphans: None (healthy)")

    # Bridges (single points of failure)
    bridges = store_bridges(store, top_k=5)
    if bridges:
        lines.append(f"\n## Bridge Edges (critical connections)")
        for b in bridges:
            lines.append(f"  - {b['from_title']} <-> {b['to_title']} "
                         f"(betweenness: {b['betweenness']:.4f})")
        lines.append("  -> Consider adding parallel links to reduce fragility")

    # Low-weight nodes approaching archive
    try:
        low = store.conn.execute(
            """SELECT id, title, type, weight FROM nodes
               WHERE status = 'active' AND weight < 0.1
               ORDER BY weight ASC LIMIT 10"""
        ).fetchall()
        if low:
            lines.append(f"\n## Fading Nodes ({len(low)} below 0.1 weight)")
            for r in low:
                lines.append(f"  - [{r['type']}] {r['title']} "
                             f"(id={r['id']}, w={r['weight']:.3f})")
            lines.append("  -> Access these nodes to refresh weight, or let them fade to archive")
    except Exception:
        pass

    # Component info
    if stats.get('components', 0) > 1:
        lines.append(f"\n## Disconnected Components: {stats['components']}")
        lines.append("  -> Use `suggest` to find cross-component link candidates")

    return "\n".join(lines)


@_tool()
def stale_check(base_dir: str = "", rebind: str = "") -> str:
    """Re-hash referent-bound nodes; demote stale ones from trusted recall.

    R0 staleness sweep: every active node carrying a file-scope referent is
    re-hashed against its recorded content digest. A mismatch (or missing
    file) records a demotion marker — the node drops out of trusted_only
    recall, shows " [stale-referent]" in search/context output, and becomes
    a re-verification candidate. Content is never deleted or rewritten.
    A fresh re-hash clears a previously recorded marker.

    Args:
        base_dir: Resolve relative referent paths against this directory
            (default: the server's cwd — prefer absolute referent paths).
        rebind: Node ID to deliberately re-verify instead of sweeping:
            re-hash its referent and rebind to the current state (moves
            true_of to now, keeps asserted_at, clears the stale marker).
    """
    store, _ = _get_store()
    from .referent import rebind as rebind_fn
    from .referent import stale_sweep

    base = base_dir or None
    if rebind:
        try:
            node = rebind_fn(store, rebind, base)
        except Exception as e:
            return f"Error: {e}"
        ref = node.get("referent") or {}
        return (f"Rebound {node['id']} to "
                f"{(ref.get('content_digest') or '')[:12]} "
                f"(true_of {node.get('true_of')}); stale marker cleared.")

    report = stale_sweep(store, base)
    lines = [
        f"Checked {report['checked']} referent-bound node(s): "
        f"{report['fresh']} fresh, {len(report['stale'])} stale, "
        f"{len(report['missing'])} missing, "
        f"{report['unhashable']} unhashable"
    ]
    for kind in ("stale", "missing"):
        for e in report[kind]:
            lines.append(f"  [{kind}] {e['id']}  {e['title'][:60]}")
    for e in report["cleared"]:
        lines.append(f"  [cleared] {e['id']}  {e['title'][:60]}")
    if report["stale"] or report["missing"]:
        lines.append(
            "Demoted from trusted recall (re-verification candidates). "
            "After confirming a claim still holds, use "
            "stale_check(rebind=<id>).")
    return "\n".join(lines)


@_tool()
def graph_merge(source_id: str, target_id: str, keep: str = "target",
                force: bool = False) -> str:
    """Merge two nodes that represent the same concept.

    Moves all edges from the source node to the target node, then archives
    the source. Use when you find duplicate or near-duplicate nodes.

    Policy-aware like `edit`: managed types (task, session, coordination)
    are always refused — their tooling owns them. Additive types (decision,
    constraint, directive, checkpoint, watch) are refused unless force=True
    — prefer `supersede` so history survives. A foreign advisory lock on
    either node blocks the merge unless force=True.

    Args:
        source_id: Node to merge FROM (will be archived).
        target_id: Node to merge INTO (will receive edges).
        keep: Which node to keep: 'target' (default) or 'source'.
        force: Merge additive types / override a foreign lock.
    """
    store, config = _get_store()
    from .schema import edit_class_for
    from .store import LockHeldError

    if keep == "source":
        source_id, target_id = target_id, source_id

    source = store.get_node(source_id)
    target = store.get_node(target_id)
    if not source:
        return f"Source node not found: {source_id}"
    if not target:
        return f"Target node not found: {target_id}"

    # Edit-policy chokepoint: graph_merge rewrites target content and
    # archives the source, so it honors the same class policy as edit.
    for node in (source, target):
        ntype = node.get("type", "concept")
        cls = edit_class_for(ntype, config.edit_policy or None)
        if cls == "managed":
            return (f"Error: node {node['id']} is type '{ntype}' (managed) — "
                    f"merge is not allowed; use the dedicated "
                    f"task/session/coordination tools")
        if cls == "additive" and not force:
            return (f"Error: node {node['id']} is type '{ntype}' (additive — "
                    f"history matters); use supersede to replace it, or pass "
                    f"force=True to merge anyway")

    # Advisory locks on either node block the merge (force overrides).
    actor = _default_agent()
    try:
        store._check_lock(source, actor, force)
        store._check_lock(target, actor, force)
    except LockHeldError as e:
        return f"Error: {e}"

    # Reviewed stopgap (PRD lineage item 2): snapshot the DB before any
    # automated destructive merge so a false merge is recoverable.
    # Fail-closed: no snapshot, no merge.
    from .snapshots import snapshot_db
    try:
        snapshot_path = snapshot_db(store, "graph-merge")
    except Exception as exc:
        return (f"Error: pre-merge DB snapshot failed ({exc}); merge refused "
                f"(fail-closed). Free the snapshot directory and retry.")

    # Move edges from source to target
    moved = 0
    for edge in store.edges_from(source_id, semantic_only=True):
        if edge["to_id"] != target_id:
            store.add_edge(target_id, edge["to_id"],
                           edge_type=edge.get("type", "relates_to"),
                           weight=edge.get("weight", 0.3),
                           provenance=f"merged from {source['title']}")
            moved += 1
    for edge in store.edges_to(source_id, semantic_only=True):
        if edge["from_id"] != target_id:
            store.add_edge(edge["from_id"], target_id,
                           edge_type=edge.get("type", "relates_to"),
                           weight=edge.get("weight", 0.3),
                           provenance=f"merged from {source['title']}")
            moved += 1

    # Merge content if source has content the target lacks
    source_content = source.get("content", "")
    target_content = target.get("content", "")
    if source_content and source_content not in (target_content or ""):
        merged_content = f"{target_content}\n\n[Merged from: {source['title']}]\n{source_content}"
        store.update_node(target_id, content=merged_content)

    # Boost target weight
    source_weight = source.get("weight", 0.5)
    target_weight = target.get("weight", 0.5)
    store.update_node(target_id, weight=min(1.0, max(target_weight, source_weight)))

    # Archive source — preserve its extra (lock, claim, expiry, ...) and
    # only annotate the merge, mirroring supersede_node.
    source_extra = dict(source.get("extra") or {})
    source_extra["merged_into"] = target_id
    store.update_node(source_id, status="archived", weight=0.01,
                      extra=source_extra)

    # Migrate retrieval pheromone so learned trails follow the merge
    # (same statement as supersede_node).
    store.conn.execute(
        "UPDATE OR REPLACE injection_pheromone SET node_id = ? "
        "WHERE node_id = ?",
        (target_id, source_id),
    )
    store.conn.commit()

    return (f"Merged '{source['title']}' into '{target['title']}': "
            f"{moved} edges moved, source archived. "
            f"Pre-merge snapshot: {snapshot_path}")


@_tool()
def dream(
    mode: str = "lightweight",
    dry_run: bool = False,
) -> str:
    """Run knowledge consolidation (dream cycle).

    Performs memory consolidation: fuzzy deduplication, suggestion
    auto-application, and bounded domain-link proposals for review. Like sleep
    consolidating memory — replay, strengthen, prune.

    Args:
        mode: 'lightweight' (fast, <5s), 'full' (non-LLM), or 'deep' (LLM clusters).
        dry_run: If True, report what would happen without making changes.
    """
    store, config = _get_store()

    from .dream import dream_cycle

    results = dream_cycle(config, store, mode=mode, dry_run=dry_run)

    if results.get("skipped"):
        return f"Dream skipped: {results['skipped']}"

    lines = [f"Dream ({results.get('mode', mode)}) complete:"]
    lines.append(f"  Merged: {results.get('merged', 0)}")
    lines.append(f"  Suggested: {results.get('suggested', 0)}")
    lines.append(f"  Suggestions applied: {results.get('suggestions_applied', 0)}")
    proposals = results.get("domain_link_proposals", [])
    if "domain_link_proposals" in results:
        capped = " (capped)" if results.get("domain_link_proposals_capped") else ""
        lines.append(f"  Domain proposals: {len(proposals)}{capped}")
        if dry_run:
            for proposal in proposals:
                lines.append(
                    f"    [{proposal['domain']}] {proposal['from_title']} "
                    f"<-> {proposal['to_title']}"
                )
    created = results.get("domain_link_suggestions_created", 0)
    if created:
        lines.append(f"  Domain suggestions: {created} queued for review")
    if "domain_link_suggestions_pending" in results:
        lines.append(
            "  Domain review queue: "
            f"{results['domain_link_suggestions_pending']}/"
            f"{results.get('domain_link_proposal_limit', 0)}"
        )
    if "cluster_summaries" in results:
        lines.append(f"  Cluster summaries: {results['cluster_summaries']}")
    return "\n".join(lines)


@_tool()
def changelog(since: str = "", days: int = 7) -> str:
    """Show recent changes to the knowledge graph.

    Args:
        since: ISO date/timestamp to look back from (e.g. '2026-02-20').
        days: Look back N days from now (default 7, ignored if 'since' is set).
    """
    store, _ = _get_store()
    import datetime

    if since:
        since_iso = since
    else:
        dt = datetime.datetime.now(tz=None) - datetime.timedelta(days=days)
        since_iso = dt.isoformat(timespec="seconds")

    entries = store.activity_since(since_iso)
    if not entries:
        return f"No changes since {since_iso}."

    def _compact(value, limit=60):
        if value is None or value == "":
            return "(none)"
        s = value if isinstance(value, str) else _json(value)
        s = " ".join(s.split())
        return s if len(s) <= limit else s[:limit - 1] + "…"

    lines = [f"{len(entries)} change(s) since {since_iso}:\n"]
    for e in entries[:50]:
        ts = e.get("timestamp", "?")[:19]
        action = e.get("action", "?")
        target = e.get("target_title") or e.get("target_id") or "?"
        actor = e.get("actor", "")
        details = e.get("details") or {}
        actor_str = f" by {actor}" if actor else ""
        lines.append(f"  {ts} {action} {target}{actor_str}")
        # Compact per-field diff lines for edits
        diffs = details.get("diffs") if isinstance(details, dict) else None
        if isinstance(diffs, dict):
            for field, change in diffs.items():
                if not isinstance(change, dict):
                    continue
                lines.append(f"    {field}: {_compact(change.get('old'))} "
                             f"-> {_compact(change.get('new'))}")
    return "\n".join(lines)


@_tool()
def ingest(source: str, limit: int = 0, repo: str = "", since: str = "") -> str:
    """Ingest knowledge from external sources.

    Args:
        source: Adapter name (github, linear, files, commits, projects, sessions) or 'all'.
        limit: Maximum items to ingest per adapter (0 = each adapter's own default;
            code is unlimited, network/LLM adapters cap at 50).
        repo: GitHub owner/repo for github adapter (e.g. 'jmcentire/kindex').
        since: ISO date — only ingest items after this date.
    """
    from .adapters.pipeline import IngestConfig, run_adapter, run_all
    from .adapters.registry import discover, get

    store, cfg = _get_store()
    config = IngestConfig(since=since or None,
                          limit=limit if limit > 0 else None,
                          verbose=False)
    extra: dict = {"_config": cfg}
    if repo:
        extra["repo"] = repo

    if source == "all":
        results = run_all(store, config, **extra)
        lines = []
        for name, result in sorted(results.items()):
            lines.append(f"  {name}: {result}")
        total = sum(r.created + r.updated for r in results.values())
        lines.append(f"\nTotal: {total} node(s) across {len(results)} adapter(s)")
        return "\n".join(lines)

    adapter = get(source)
    if not adapter:
        names = ", ".join(sorted(discover().keys()))
        return f"Unknown adapter '{source}'. Available: {names}, all"

    result = run_adapter(adapter, store, config, **extra)
    if result.errors:
        return f"Errors: {'; '.join(result.errors)}"
    return f"{adapter.meta.name}: {result}"


# ── Resources ─────────────────────────────────────────────────────────


@mcp.resource("kindex://status")
def resource_status() -> str:
    """Current knowledge graph statistics."""
    store, _ = _get_store()
    stats = store.stats()
    return _json(stats, indent=2)


@mcp.resource("kindex://node/{node_id}")
def resource_node(node_id: str) -> str:
    """Full details of a specific knowledge node."""
    store, _ = _get_store()
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"
    return _node_detail(store, node)


@mcp.resource("kindex://recent")
def resource_recent() -> str:
    """Recently active nodes in the knowledge graph."""
    store, _ = _get_store()
    nodes = store.recent_nodes(n=20)
    lines = [_node_summary(n) for n in nodes]
    return "\n".join(lines) if lines else "No recent nodes."


@mcp.resource("kindex://orphans")
def resource_orphans() -> str:
    """Nodes with no connections (candidates for linking or removal)."""
    store, _ = _get_store()
    orphans = store.orphans()
    if not orphans:
        return "No orphan nodes."
    lines = [_node_summary(n) for n in orphans]
    return f"{len(orphans)} orphan(s):\n" + "\n".join(lines)


# ── Prompts ───────────────────────────────────────────────────────────


@mcp.prompt()
def prime(topic: str = "") -> str:
    """Generate a full context priming block for the current session.

    Args:
        topic: Optional topic to focus on.
    """
    store, _ = _get_store()
    from .retrieve import format_context_block, hybrid_search
    from .store import node_expired, node_retired

    client = _mcp_client()
    if topic:
        results = hybrid_search(store, topic, top_k=15)
    else:
        results = [r for r in store.recent_nodes(n=15)
                   if not node_expired(r) and not node_retired(r)]
    results = _scope_results(results, client)

    if not results:
        return "No knowledge available for priming."

    stats = store.stats()
    header = (
        f"# Kindex Context\n\n"
        f"Graph: {stats.get('node_count', 0)} nodes, {stats.get('edge_count', 0)} edges\n\n"
    )
    block = format_context_block(store, results, query=topic, level="full", adapter=client)
    return header + block


@mcp.prompt()
def orient() -> str:
    """Quick orientation: graph stats, recent activity, and key nodes."""
    store, _ = _get_store()
    from .graph import store_stats

    from .store import node_retired

    stats = store_stats(store)
    recent = [n for n in store.recent_nodes(n=10) if not node_retired(n)]
    op = store.operational_summary()

    lines = [
        "# Kindex Orientation\n",
        f"Graph: {stats['semantic_nodes']} semantic nodes, "
        f"{stats['edges']} semantic edges, "
        f"{stats.get('components', 0)} component(s)\n",
    ]

    if recent:
        lines.append("## Recently Active")
        for n in recent[:10]:
            lines.append(f"  - {_node_summary(n)}")

    constraints = op.get("constraints", [])
    if constraints:
        lines.append(f"\n## Active Constraints ({len(constraints)})")
        for c in constraints[:5]:
            lines.append(f"  - {c.get('title', c['id'])}")

    watches = op.get("watches", [])
    if watches:
        lines.append(f"\n## Active Watches ({len(watches)})")
        for w in watches[:5]:
            lines.append(f"  - {w.get('title', w['id'])}")

    return "\n".join(lines)


# ── Session tags ──────────────────────────────────────────────────────


@_tool()
def tag_start(name: str, description: str = "", focus: str = "",
              remaining: str = "") -> str:
    """Start a new session tag for tracking work context.

    Args:
        name: Human-readable tag name (e.g. 'auth-refactor').
        description: What this session is about.
        focus: Current focus area.
        remaining: Comma-separated list of remaining items.
    """
    store, _ = _get_store()
    from .sessions import start_tag
    import os
    remaining_list = [r.strip() for r in remaining.split(",") if r.strip()] if remaining else []
    try:
        nid = start_tag(store, name, description=description, focus=focus,
                        remaining=remaining_list, project_path=os.getcwd())
        return f"Started session tag: {name} ({nid})"
    except ValueError as e:
        return f"Error: {e}"


def _reinforce_on_end(
    store,
    config,
    tag_name: str,
    summary: str,
    *,
    project_path: str | None = None,
) -> str:
    """Private helper (NOT an MCP tool — it takes store/config directly).

    Silently queue this session for later reinforcement grading (no LLM, no
    output here — the grading runs off the critical path in cron). Uses the
    summary + segment history as the trace; a Stop hook's full transcript, if
    present, supersedes it. Returns '' always — kin stays transparent. Never raises.
    """
    try:
        if not config.attention.reinforce_enabled:
            return ""
        from .attention import resolve_conversation_id
        from .sessions import get_tag
        from .reinforce import enqueue_reinforce

        conversation_id = resolve_conversation_id(fallback_to_cwd=False)
        if not conversation_id:
            return ""

        parts = [summary] if summary else []
        tag = get_tag(store, tag_name, project_path=project_path)
        if tag:
            parts.append(tag.get("content", "") or "")
            for seg in (tag.get("extra") or {}).get("segments", []) or []:
                if seg.get("focus"):
                    parts.append(f"focus: {seg['focus']}")
                if seg.get("summary"):
                    parts.append(seg["summary"])
        trace = "\n".join(p for p in parts if p).strip()
        if trace:
            enqueue_reinforce(store, conversation_id, trace=trace)
    except Exception:
        pass
    return ""


@_tool()
def tag_update(name: str = "", focus: str = "", description: str = "",
               remaining: str = "", add_remaining: str = "",
               done: str = "", summary: str = "",
               action: str = "update") -> str:
    """Update, segment, pause, or end a session tag.

    Args:
        name: Tag name (auto-detects active tag if empty).
        focus: New focus area (used for update and segment actions).
        description: Updated description.
        remaining: Replace remaining items (comma-separated).
        add_remaining: Add items to remaining (comma-separated).
        done: Remove items from remaining (comma-separated).
        summary: Summary for segment/pause/end actions.
        action: One of: update, segment, pause, end.
    """
    store, config = _get_store()
    from .sessions import (update_tag, add_segment, pause_tag,
                           complete_tag, get_active_tag, get_tag)
    import os
    project_path = os.getcwd()

    if not name:
        active = get_active_tag(store, project_path=os.getcwd())
        if not active:
            return "No active session tag found. Start one with tag_start."
        name = (active.get("extra") or {}).get("tag", active["title"])

    try:
        if action == "update":
            update_tag(
                store, name,
                focus=focus or None,
                description=description or None,
                remaining=[r.strip() for r in remaining.split(",") if r.strip()] if remaining else None,
                append_remaining=[r.strip() for r in add_remaining.split(",") if r.strip()] if add_remaining else None,
                remove_remaining=[r.strip() for r in done.split(",") if r.strip()] if done else None,
                project_path=project_path,
            )
            return f"Updated tag: {name}"
        elif action == "segment":
            add_segment(
                store,
                name,
                new_focus=focus or "New segment",
                summary=summary,
                project_path=project_path,
            )
            return f"New segment on {name}: {focus}"
        elif action == "pause":
            pause_tag(store, name, summary=summary, project_path=project_path)
            return f"Paused: {name}"
        elif action == "end":
            complete_tag(store, name, summary=summary, project_path=project_path)
            note = _reinforce_on_end(
                store,
                config,
                name,
                summary,
                project_path=project_path,
            )
            return f"Completed: {name}{note}"
        return f"Unknown action: {action}"
    except ValueError as e:
        return f"Error: {e}"


@_tool()
def tag_resume(name: str = "", tokens: int = 1500) -> str:
    """Resume a session tag — get full context for continuing work.

    Args:
        name: Tag name to resume (shows active/paused tags if empty).
        tokens: Exact UTF-8 byte budget for the resume block. The argument name
            is retained for compatibility; provider-token guarantees require a
            direct library caller to supply that provider's exact counter.
    """
    store, _ = _get_store()
    from .sessions import format_resume_context, list_tags, resume_tag
    import os

    project_path = os.getcwd()

    if not name:
        tags = list_tags(
            store, status="active", project_path=project_path, limit=5
        )
        tags += list_tags(
            store, status="paused", project_path=project_path, limit=5
        )
        if not tags:
            return "No active or paused session tags."
        lines = ["Available session tags:\n"]
        for t in tags:
            extra = t.get("extra") or {}
            lines.append(f"  [{extra.get('session_status', '?')}] "
                         f"{extra.get('tag', t['title'])}: "
                         f"{extra.get('current_focus', '')[:60]}")
        return "\n".join(lines)

    try:
        resume_tag(store, name, project_path=project_path)
        return format_resume_context(
            store,
            name,
            max_tokens=tokens,
            evaluation_time=operation_now(),
            project_path=project_path,
        )
    except ValueError as e:
        return f"Error: {e}"


# ── Tasks ─────────────────────────────────────────────────────────────


@_tool()
def task_add(text: str, priority: int = 3, due: str = "",
             scope: str = "contextual", link_to: str = "",
             effort: str = "") -> str:
    """Add a task to the knowledge graph.

    Tasks are graph-connected -- link them to concepts, projects, or other
    nodes so they surface contextually when you're working in related areas.
    Use link_to to connect tasks to existing graph nodes by ID or title.

    Args:
        text: Task title/description.
        priority: 1=urgent 2=high 3=normal 4=low 5=someday.
        due: Optional due date ('tomorrow', '2026-03-15', 'in 3 days').
        scope: 'global' (always visible) or 'contextual' (surfaces by proximity).
        link_to: Comma-separated node IDs or titles to link this task to.
        effort: Optional effort estimate (small, medium, large).
    """
    store, _ = _get_store()
    from .tasks import create_task
    links = [s.strip() for s in link_to.split(",") if s.strip()] if link_to else None
    task_id = create_task(
        store, text,
        priority=priority,
        due=due or None,
        scope=scope,
        effort=effort or None,
        link_to=links,
    )
    node = store.get_node(task_id)
    extra = node.get("extra", {}) if node else {}
    p_label = {1: "urgent", 2: "high", 3: "normal", 4: "low", 5: "someday"}.get(
        extra.get("priority", 3), "normal")
    due_info = f", due: {extra.get('due', '')}" if extra.get("due") else ""
    return f"Created task: {task_id} [{p_label}]{due_info} — {text}"


@_tool()
def task_list(status: str = "open", scope: str = "",
              priority: str = "") -> str:
    """List tasks, optionally filtered.

    Args:
        status: Filter: open, in_progress, done, all. Default: open.
        scope: Filter: global, contextual, or empty for both.
        priority: Max priority level to show (1-5). Empty for all.
    """
    store, _ = _get_store()
    from .tasks import list_tasks, format_task_list
    tasks = list_tasks(
        store,
        status=status,
        scope=scope or None,
    )
    if priority:
        try:
            max_pri = int(priority)
            tasks = [t for t in tasks
                     if (t.get("extra") or {}).get("priority", 3) <= max_pri]
        except ValueError:
            pass
    if not tasks:
        return "No tasks found."
    return format_task_list(tasks)


@_tool()
def task_done(id: str) -> str:
    """Mark a task as completed.

    Args:
        id: Task node ID.
    """
    store, _ = _get_store()
    from .tasks import complete_task
    result = complete_task(store, id)
    if result:
        return f"Completed: {result['title']} ({id})"
    return f"Task not found: {id}"


@_tool()
def task_claim(id: str, agent: str = "", ttl_minutes: int = 120,
               note: str = "", force: bool = False) -> str:
    """Claim a task for an agent with an expiry.

    Args:
        id: Task node ID.
        agent: Agent/sub-agent name claiming the task (default: resolved agent id).
        ttl_minutes: Claim TTL. Expired claims can be taken over.
        note: Optional claim note.
        force: Override an existing unexpired claim.
    """
    store, _ = _get_store()
    from .tasks import claim_task
    try:
        result = claim_task(
            store, id, _default_agent(agent),
            ttl_minutes=ttl_minutes,
            note=note,
            force=force,
        )
    except ValueError as e:
        return f"Could not claim task: {e}"
    if not result:
        return f"Task not found: {id}"
    claim = (result.get("extra") or {}).get("claim") or {}
    return (
        f"Claimed task: {result['title']} ({id}) by {claim.get('agent')} "
        f"until {claim.get('expires_at')}"
    )


@_tool()
def task_release(id: str, agent: str = "", force: bool = False) -> str:
    """Release a task claim.

    Args:
        id: Task node ID.
        agent: Agent name. Required to match the claim unless force is true.
        force: Release even if the agent does not match.
    """
    store, _ = _get_store()
    from .tasks import release_task_claim
    try:
        result = release_task_claim(store, id, agent=agent, force=force)
    except ValueError as e:
        return f"Could not release task claim: {e}"
    if not result:
        return f"Task not found: {id}"
    return f"Released task claim: {result['title']} ({id})"


# ── Coordination ─────────────────────────────────────────────────────


@_tool()
def coord_start(name: str, task_id: str = "", agent: str = "",
                ttl_minutes: int = 240) -> str:
    """Start a short-lived coordination conversation.

    Args:
        name: Conversation name.
        task_id: Optional related task ID.
        agent: Agent creating the conversation (default: resolved agent id).
        ttl_minutes: Conversation TTL. Expired conversations are cleaned up.
    """
    store, _ = _get_store()
    from .coordination import create_conversation
    try:
        conv_id = create_conversation(
            store,
            name,
            task_id=task_id or None,
            ttl_minutes=ttl_minutes,
            created_by=_default_agent(agent),
        )
    except ValueError as e:
        return f"Could not start coordination conversation: {e}"
    return f"Started coordination conversation: {conv_id} ({name})"


@_tool()
def coord_join(name: str, agent: str = "") -> str:
    """Join a coordination conversation as a member.

    Members get read cursors (unread tracking) and receive standing inject
    messages in their session context. Idempotent.

    Args:
        name: Conversation ID or name.
        agent: Joining agent name (default: resolved agent id).
    """
    store, _ = _get_store()
    from .coordination import join_conversation
    try:
        member = join_conversation(store, name, _default_agent(agent))
    except ValueError as e:
        return f"Could not join conversation: {e}"
    return f"Joined {name} as {member['agent']}"


@_tool()
def coord_post(conversation: str, agent: str = "", message: str = "",
               to: str = "") -> str:
    """Post a message to a coordination conversation.

    Args:
        conversation: Conversation ID or name.
        agent: Posting agent name (default: resolved agent id).
        message: Message body.
        to: Optional target agent — message is delivered (unread counts,
            injection) only to them; broadcast when empty.
    """
    store, _ = _get_store()
    from .coordination import post_message
    try:
        msg = post_message(store, conversation, _default_agent(agent), message,
                           to=to or None)
    except ValueError as e:
        return f"Could not post coordination message: {e}"
    return f"Posted coordination message #{msg['id']} to {conversation}"


@_tool()
def coord_read(conversation: str, since_id: int = 0, limit: int = 50,
               agent: str = "") -> str:
    """Read messages from a coordination conversation.

    Advances the reading agent's member cursor (unread tracking).

    Args:
        conversation: Conversation ID or name.
        since_id: Only return messages with a higher id.
        limit: Maximum messages to return.
        agent: Reading agent name (default: resolved agent id).
    """
    store, _ = _get_store()
    from .coordination import format_messages, read_messages
    try:
        payload = read_messages(store, conversation, since_id=since_id,
                                limit=limit, agent=_default_agent(agent))
    except ValueError as e:
        return f"Could not read coordination conversation: {e}"
    return format_messages(payload)


@_tool()
def coord_attach(name: str, node_id: str) -> str:
    """Attach a graph node to a conversation as a shared resource.

    Attached resources surface in members' contexts when locked, so agents
    see who holds what.

    Args:
        name: Conversation ID or name.
        node_id: Node ID or title to attach.
    """
    store, _ = _get_store()
    from .coordination import attach_resource
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    try:
        resources = attach_resource(store, name,
                                    node["id"] if node else node_id)
    except ValueError as e:
        return f"Could not attach resource: {e}"
    return f"Attached. Resources on {name}: {', '.join(resources)}"


@_tool()
def coord_inject(name: str, action: str = "list", text: str = "",
                 to: str = "", message_id: int = 0) -> str:
    """Manage standing inject messages on a coordination conversation.

    Inject messages are pushed into member agents' session context by the
    prime/prompt hooks until cleared.

    Args:
        name: Conversation ID or name.
        action: set, clear, or list.
        text: Message text (for set).
        to: Optional target agent (for set); broadcast when empty.
        message_id: Specific inject message id to clear (0 = clear all).
    """
    store, _ = _get_store()
    from .coordination import (
        clear_inject_messages,
        list_inject_messages,
        set_inject_message,
    )
    try:
        if action == "set":
            entry = set_inject_message(store, name, text, _default_agent(),
                                       to=to or None)
            target = f" -> {entry['to']}" if entry.get("to") else ""
            return f"Set inject message #{entry['id']}{target} on {name}"
        if action == "clear":
            count = clear_inject_messages(
                store, name, message_id=message_id or None)
            return f"Cleared {count} inject message(s) on {name}"
        if action == "list":
            msgs = list_inject_messages(store, name)
            if not msgs:
                return f"No inject messages on {name}."
            lines = [f"Inject messages on {name}:"]
            for m in msgs:
                target = f" -> {m['to']}" if m.get("to") else ""
                lines.append(f"  #{m.get('id')} {m.get('created_at')} "
                             f"{m.get('set_by')}{target}: {m.get('text')}")
            return "\n".join(lines)
    except ValueError as e:
        return f"Could not manage inject messages: {e}"
    return f"Unknown inject action: {action} (use set, clear, or list)"


@_tool()
def coord_list(status: str = "active", task_id: str = "") -> str:
    """List coordination conversations.

    Args:
        status: active, ended, or all.
        task_id: Optional related task ID filter.
    """
    store, _ = _get_store()
    from .coordination import format_conversations, list_conversations
    conversations = list_conversations(
        store,
        status=status,
        task_id=task_id or None,
    )
    return format_conversations(conversations)


@_tool()
def coord_end(conversation: str, summary: str = "") -> str:
    """End a coordination conversation and clear transient messages.

    Args:
        conversation: Conversation ID or name.
        summary: Optional retained summary.
    """
    store, _ = _get_store()
    from .coordination import end_conversation
    result = end_conversation(store, conversation, summary=summary)
    if not result:
        return f"Conversation not found: {conversation}"
    return f"Ended coordination conversation: {conversation}"


# ── Locks ────────────────────────────────────────────────────────────


@_tool()
def lock_acquire(node_id: str, ttl_minutes: int = 60, note: str = "",
                 force: bool = False) -> str:
    """Acquire an advisory lock on a node for the current agent.

    Locks are advisory: they signal "I am working on this" to other agents
    (edit refusal, collab context). Expired locks never block anyone.

    Args:
        node_id: Node ID or title to lock.
        ttl_minutes: Lock TTL. Re-acquiring refreshes it.
        note: Why the node is locked.
        force: Take over a foreign unexpired lock.
    """
    store, _ = _get_store()
    from .locks import lock_node
    from .store import LockHeldError
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"
    try:
        lock = lock_node(store, node["id"], _default_agent(),
                         ttl_minutes=ttl_minutes, note=note, force=force)
    except (LockHeldError, ValueError) as e:
        return f"Could not lock node: {e}"
    return (f"Locked {node['title']} ({node['id']}) for {lock['agent']} "
            f"until {lock['expires_at']}")


@_tool()
def lock_release(node_id: str, force: bool = False) -> str:
    """Release an advisory lock held on a node.

    Args:
        node_id: Node ID or title to unlock.
        force: Clear a lock held by another agent.
    """
    store, _ = _get_store()
    from .locks import unlock_node
    from .store import LockHeldError
    node = store.get_node(node_id) or store.get_node_by_title(node_id)
    if not node:
        return f"Node not found: {node_id}"
    try:
        cleared = unlock_node(store, node["id"], _default_agent(), force=force)
    except LockHeldError as e:
        return f"Could not unlock node: {e}"
    if cleared:
        return f"Unlocked {node['title']} ({node['id']})"
    return f"No lock on {node['title']} ({node['id']})"


# ── Watches ──────────────────────────────────────────────────────────


@_tool()
def watch_add(text: str, owner: str = "", expires: str = "",
              link_to: str = "") -> str:
    """Create a watch node for something needing periodic attention.

    Watches surface in every session's context. Use for:
    - Flaky tests, unstable APIs, known tech debt
    - Pending decisions, things to revisit
    - Anything Claude should keep an eye on

    Args:
        text: What to watch (becomes the node title).
        owner: Who owns this watch (person or team).
        expires: When this watch expires (YYYY-MM-DD). Auto-archived after expiry.
        link_to: Comma-separated node IDs/titles to link this watch to.
    """
    store, _ = _get_store()
    import os

    extra = {"watch_status": "active"}
    if owner:
        extra["owner"] = owner
    if expires:
        extra["expires"] = expires

    domains = []
    project_path = os.environ.get("PWD", "")
    if project_path:
        extra["project_path"] = project_path

    nid = store.add_node(
        title=text,
        content="",
        node_type="watch",
        domains=domains,
        prov_activity="mcp-watch-add",
        extra=extra,
    )

    # Link to specified nodes
    if link_to:
        for ref in link_to.split(","):
            ref = ref.strip()
            if not ref:
                continue
            target = store.get_node(ref) or store.get_node_by_title(ref)
            if target:
                store.add_edge(nid, target["id"], edge_type="relates_to",
                               weight=0.5, provenance="watch context")

    exp = f", expires {expires}" if expires else ""
    own = f", owner: {owner}" if owner else ""
    return f"Watch created: {text} (id={nid}{exp}{own})"


@_tool()
def watch_list(status: str = "active") -> str:
    """List watch nodes.

    Args:
        status: Filter by status: active (default), archived, all.
    """
    store, _ = _get_store()
    if status == "all":
        watches = store.all_nodes(node_type="watch", limit=50)
    elif status == "archived":
        watches = store.all_nodes(node_type="watch", status="archived", limit=50)
    else:
        watches = store.active_watches()

    if not watches:
        return "No watches found."

    lines = [f"Watches ({len(watches)}):"]
    for w in watches:
        extra = w.get("extra") or {}
        parts = [f"- {w['title']} (id={w['id']})"]
        if extra.get("owner"):
            parts.append(f"@{extra['owner']}")
        if extra.get("expires"):
            parts.append(f"expires {extra['expires']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


@_tool()
def watch_resolve(id: str, reason: str = "") -> str:
    """Resolve/archive a watch node.

    Args:
        id: Watch node ID.
        reason: Why this watch is being resolved.
    """
    store, _ = _get_store()
    node = store.get_node(id)
    if not node or node.get("type") != "watch":
        return f"Watch not found: {id}"

    def _mutate(extra: dict) -> None:
        extra["resolved_reason"] = reason
        extra["watch_status"] = "resolved"

    # Atomic extra mutation first, then the status/weight flip without
    # passing extra — a concurrent extra writer is never clobbered.
    store.atomic_extra_update(id, _mutate)
    store.update_node(id, status="archived", weight=0.01)
    return f"Resolved watch: {node['title']} ({id})"


# ── Reminders ─────────────────────────────────────────────────────────


@_tool()
def remind_create(text: str, when: str, priority: str = "normal",
                  channels: str = "", action: str = "",
                  instructions: str = "", conversation_id: str = "",
                  scope: str = "chat", wake: str = "",
                  wake_session: str = "", wake_cwd: str = "",
                  wake_model: str = "", wake_agent: str = "") -> str:
    """Create a reminder with natural language time parsing.

    Args:
        text: What to be reminded about.
        when: When to fire: 'in 30 minutes', 'tomorrow at 3pm', 'every weekday at 9am'.
        priority: Priority level (low, normal, high, urgent).
        channels: Comma-separated notification channels (system, slack, email, claude).
        action: Shell command to execute when the reminder fires (optional).
        instructions: Natural language instructions for Claude to follow when due (optional).
        conversation_id: Optional chat/session id for scoped hook injection.
        scope: Reminder visibility for hook injection: chat or global.
        wake: Wake an agent when due: codex or opencode.
        wake_session: Optional host session id to resume; use 'last' for latest.
        wake_cwd: Optional working directory for the wake run.
        wake_model: Optional model override for the wake run.
        wake_agent: Optional OpenCode agent override for the wake run.
    """
    store, config = _get_store()
    from .reminders import create_reminder
    channel_list = [c.strip() for c in channels.split(",") if c.strip()] if channels else None
    if scope == "chat" and not conversation_id:
        try:
            from .attention import resolve_conversation_id
            conversation_id = resolve_conversation_id(fallback_to_cwd=False)
        except Exception:
            conversation_id = ""
    effective_scope = "global" if scope == "global" else ("chat" if conversation_id else "")
    try:
        rid = create_reminder(
            store, text, when,
            priority=priority, channels=channel_list,
            action_command=action,
            action_instructions=instructions,
            wake_client=wake,
            wake_session_id=wake_session,
            wake_cwd=wake_cwd,
            wake_model=wake_model,
            wake_agent=wake_agent,
            conversation_id=conversation_id,
            scope=effective_scope,
        )
        r = store.get_reminder(rid)
        action_info = ""
        if wake:
            action_info = f", action: wake:{wake}"
        elif action or instructions:
            action_info = f", action: {'shell' if action and not instructions else 'claude'}"
        return f"Created reminder: {rid} (next due: {r['next_due']}{action_info})"
    except ValueError as e:
        return f"Error: {e}"


@_tool()
def remind_list(status: str = "active", priority: str = "") -> str:
    """List reminders with optional filters.

    Args:
        status: Filter by status (active, snoozed, fired, all).
        priority: Filter by priority (low, normal, high, urgent).
    """
    store, _ = _get_store()
    from .reminders import format_reminder_list
    s = None if status == "all" else status
    p = priority or None
    reminders = store.list_reminders(status=s, priority=p)
    if not reminders:
        return "No reminders found."
    return format_reminder_list(reminders)


@_tool()
def remind_snooze(id: str, duration: str = "") -> str:
    """Snooze a reminder.

    Args:
        id: Reminder ID.
        duration: Snooze duration (e.g. '15m', '1h'). Uses config default if empty.
    """
    store, config = _get_store()
    from .reminders import parse_duration, snooze_reminder
    dur = parse_duration(duration) if duration else None
    try:
        new_time = snooze_reminder(store, id, dur, config)
        return f"Snoozed until: {new_time}"
    except ValueError as e:
        return f"Error: {e}"


@_tool()
def remind_done(id: str) -> str:
    """Mark a reminder as completed.

    Args:
        id: Reminder ID.
    """
    store, _ = _get_store()
    from .reminders import complete_reminder
    try:
        complete_reminder(store, id)
        return f"Completed reminder: {id}"
    except ValueError as e:
        return f"Error: {e}"


@_tool()
def remind_check() -> str:
    """Check for due reminders and fire notifications.

    Runs the reminder check cycle: finds due reminders, sends notifications,
    handles auto-snooze for stale fired reminders.
    """
    store, config = _get_store()
    from .reminders import auto_snooze_stale, check_and_fire
    fired = check_and_fire(store, config)
    snoozed = auto_snooze_stale(store, config)
    if not fired and snoozed == 0:
        return "No due reminders."
    parts = []
    if fired:
        parts.append(f"{len(fired)} reminder(s) fired")
        for r in fired:
            parts.append(f"  - {r['title']} [{r.get('priority', 'normal')}]")
    if snoozed:
        parts.append(f"{snoozed} auto-snoozed")
    return "\n".join(parts)


@_tool()
def remind_exec(id: str) -> str:
    """Manually trigger a reminder's action.

    Args:
        id: Reminder ID.
    """
    store, config = _get_store()
    from .actions import execute_action, has_action
    r = store.get_reminder(id)
    if not r:
        return f"Error: Reminder not found: {id}"
    if not has_action(r):
        return f"Error: Reminder {id} has no action defined."
    result = execute_action(store, r, config, manual=True)
    return f"Action {result['status']}: {result.get('output', '')[:500]}"


# ── Modes ─────────────────────────────────────────────────────────────


@_tool()
def mode_activate(name: str, session_context: str = "") -> str:
    """Activate a conversation mode. Returns the priming artifact to inject.

    Modes are state inductions, not instructions. They shift how you think,
    not what you think about. Based on research showing induced understanding
    outperforms direct instruction by 5.4x.

    Built-in modes: collaborate, code, create, research, chat.
    Custom modes can be created with mode_create.

    Args:
        name: Mode name (e.g. 'collaborate', 'code', 'create').
        session_context: Optional prior session context to resume from.
    """
    store, _ = _get_store()
    from .modes import activate_mode
    return activate_mode(store, name, session_context=session_context or None)


@_tool()
def mode_list() -> str:
    """List available conversation modes (built-in and custom)."""
    store, _ = _get_store()
    from .modes import list_modes, format_mode_list, DEFAULT_MODES
    modes = list_modes(store)
    return format_mode_list(modes, defaults=DEFAULT_MODES)


@_tool()
def mode_show(name: str) -> str:
    """Show details of a conversation mode including its primer, boundary, and permissions.

    Args:
        name: Mode name.
    """
    store, _ = _get_store()
    from .modes import get_mode, format_mode_detail, DEFAULT_MODES
    mode = get_mode(store, name)
    default = DEFAULT_MODES.get(name) if not mode else None
    return format_mode_detail(name, mode=mode, default=default)


@_tool()
def mode_create(name: str, primer: str, boundary: str, permissions: str,
                description: str = "", link_to: str = "") -> str:
    """Create a custom conversation mode from a primer, boundary, and permissions.

    A primer is a state induction (~80 words) that establishes how to think.
    A boundary defines what quality means for this mode.
    Permissions state what's explicitly allowed (tangents, pushback, etc).

    Args:
        name: Mode name (short, lowercase, no spaces).
        primer: The mode-setting passage. Under 80 words. Not instructions.
        boundary: 2-3 sentences defining output quality for this mode.
        permissions: What's explicitly permitted to keep the conversation alive.
        description: Optional one-line description.
        link_to: Comma-separated node IDs/titles to link this mode to.
    """
    store, _ = _get_store()
    from .modes import create_mode
    links = [s.strip() for s in link_to.split(",") if s.strip()] if link_to else None
    mode_id = create_mode(
        store, name,
        primer=primer,
        boundary=boundary,
        permissions=permissions,
        description=description,
        link_to=links,
    )
    return f"Created mode: {name} ({mode_id})"


@_tool()
def mode_export(name: str) -> str:
    """Export a mode as a portable, PII-free artifact (JSON).

    Args:
        name: Mode name to export.
    """
    store, _ = _get_store()
    from .modes import export_mode
    import json
    artifact = export_mode(store, name)
    if not artifact:
        return f"Mode not found: {name}"
    return json.dumps(artifact, indent=2)


@_tool()
def mode_import(artifact_json: str) -> str:
    """Import a mode from a portable artifact (JSON string).

    Args:
        artifact_json: JSON string of the mode artifact.
    """
    store, _ = _get_store()
    from .modes import import_mode
    import json
    try:
        artifact = json.loads(artifact_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    try:
        mode_id = import_mode(store, artifact)
        return f"Imported mode: {artifact.get('name', '?')} ({mode_id})"
    except ValueError as e:
        return f"Error: {e}"


@_tool()
def mode_seed() -> str:
    """Seed the default conversation modes into the graph. Idempotent."""
    store, _ = _get_store()
    from .modes import seed_defaults
    created = seed_defaults(store)
    if created:
        return f"Seeded {len(created)} modes: {', '.join(created)}"
    return "All default modes already exist."


# ── Entry point ───────────────────────────────────────────────────────


def main():
    """Run the Kindex MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
