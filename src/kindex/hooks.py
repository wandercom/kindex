"""Hook infrastructure for Claude Code integration.

Provides functions for SessionStart (prime_context), PostSession (capture_session_end),
inbox writes, and CLAUDE.md directive generation.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .privacy import redact, redact_text

if TYPE_CHECKING:
    from .config import Config
    from .store import Store
    from .budget import BudgetLedger


def _record_section_degraded(section: str, error: Exception,
                             config: "Config | None") -> None:
    """Ledger a shielded prime-section failure; never raises."""
    try:
        from .config import record_degraded
        record_degraded(section, error, config=config)
    except Exception:
        pass


def prime_context(
    store: Store,
    topic: str | None = None,
    max_tokens: int = 750,
    config: Config | None = None,
    conversation_id: str | None = None,
    adapter: str | None = None,
) -> str:
    """Generate compact context injection (~500-750 tokens) for SessionStart hook.

    - Auto-detects topic from current working directory if not provided
    - Uses hybrid_search to find relevant nodes
    - Formats as summarized/executive tier
    - Includes active operational nodes (constraints, watches)
    - Includes recent activity summary (what changed since yesterday)

    Returns a string suitable for CLAUDE.md injection.
    """
    from .agent_adapters import adapter_scoped_out
    from .retrieve import detect_domain_from_path, hybrid_search
    from .store import node_expired

    # Section shields: a failure in one section degrades that section and
    # the rest still renders. Ledger events are DEFERRED — one failed hook
    # invocation yields exactly one degraded event (the top-level
    # catch-all's), so section events are written only when this function
    # completes, i.e. the partial-success case. If every store-backed
    # section failed the store is unreadable: re-raise so the catch-all
    # owns the single event and the degraded output line.
    section_failures: list[tuple[str, Exception]] = []
    search_failed = ops_failed = False

    # Auto-detect topic from cwd if not provided. Topic detection reads
    # the store; a failure degrades to the directory-name fallback.
    if not topic:
        cwd = os.getcwd()
        try:
            domains = detect_domain_from_path(store, cwd)
        except Exception as e:
            section_failures.append(("prime.topic", e))
            domains = []
        if domains:
            topic = " ".join(domains)
        else:
            # Use the directory name as a fallback search term
            topic = os.path.basename(cwd)

    # Search for relevant nodes (expired and other-client nodes never
    # surface). Per-node filtering: one bad node is skipped, never allowed
    # to zero the section, and a failed search still primes the rest.
    try:
        raw_results = hybrid_search(store, topic, top_k=8)
    except Exception as e:
        section_failures.append(("prime.search", e))
        search_failed = True
        raw_results = []
    results = []
    for r in raw_results:
        try:
            if not node_expired(r) and not adapter_scoped_out(r.get("tags"), adapter):
                results.append(redact(r))
        except Exception:
            continue

    lines: list[str] = []
    lines.append("## Kindex Context (auto-primed)")
    lines.append("")

    # Budget: roughly 3 chars per token, target ~2000-2250 chars for 750 tokens
    char_budget = max_tokens * 3
    used = sum(len(l) for l in lines)

    # -- Key concepts (summarized tier) --
    if results:
        lines.append("### Key concepts")
        for r in results[:6]:
            try:
                title = r.get("title", r["id"])
                ntype = r.get("type", "concept")
                content = (r.get("content") or "")[:120]
                edges = r.get("edges_out", [])
                connected = ", ".join(str(e.get("to_title") or e.get("to_id") or "")
                                      for e in edges[:3])

                entry = f"- **{title}** ({ntype})"
                if content:
                    entry += f": {content}"
                if connected:
                    entry += f" [{connected}]"
            except Exception:
                continue  # one malformed node never zeroes the prime

            if used + len(entry) + 1 > char_budget - 400:
                break
            lines.append(entry)
            used += len(entry) + 1

        lines.append("")

    # -- Active operational nodes (expired ones are skipped in every section) --
    # Client-scoped nodes (e.g. an Antigravity hook-protocol directive) are dropped
    # when a different client is priming, mirroring the attention-hook scoping.
    # Per-node filtering here too: a malformed operational node is skipped,
    # never allowed to take the section (or the prime) down with it.
    ops = {"constraints": [], "checkpoints": [], "watches": [], "directives": []}
    try:
        ops_raw = store.operational_summary()
    except Exception as e:
        section_failures.append(("prime.operational", e))
        ops_failed = True
        ops_raw = {}
    for k, v in ops_raw.items():
        kept = []
        for n in v:
            try:
                if not node_expired(n) and not adapter_scoped_out(n.get("tags"), adapter):
                    kept.append(redact(n))
            except Exception:
                continue
        ops[k] = kept

    if ops["constraints"]:
        lines.append("### Active constraints")
        for c in ops["constraints"][:3]:
            try:
                extra = c.get("extra")
                extra = extra if isinstance(extra, dict) else {}
                action = extra.get("action", "warn")
                entry = f"- [{action}] {c['title']}"
            except Exception:
                continue
            lines.append(entry)
            used += len(entry) + 1
        lines.append("")

    if ops["watches"]:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        lines.append("### Watches")
        for w in ops["watches"][:5]:
            try:
                extra = w.get("extra")
                extra = extra if isinstance(extra, dict) else {}
                expires = extra.get("expires", "")
                urgent = ""
                if expires:
                    try:
                        days_left = (_dt.date.fromisoformat(expires) - _dt.date.today()).days
                        if days_left <= 0:
                            urgent = " [OVERDUE]"
                        elif days_left <= 3:
                            urgent = f" [{days_left}d left]"
                    except (ValueError, TypeError):
                        pass
                parts = [f"! {w['title']}{urgent}"]
                if extra.get("owner"):
                    parts.append(f"@{extra['owner']}")
                if expires:
                    parts.append(f"(expires {expires})")
                entry = f"- {' '.join(parts)}"
            except Exception:
                continue
            lines.append(entry)
            used += len(entry) + 1
        lines.append("")

    if ops["directives"]:
        lines.append("### Directives")
        for d in ops["directives"][:2]:
            try:
                extra = d.get("extra")
                extra = extra if isinstance(extra, dict) else {}
                scope = extra.get("scope", "")
                entry = f"- {d['title']}"
                if scope:
                    entry += f" [scope: {scope}]"
            except Exception:
                continue
            lines.append(entry)
            used += len(entry) + 1
        lines.append("")

    # -- Recent activity summary (since yesterday) --
    # Shielded like the other sections: a failed activity pull skips this
    # section and the rest of the prime still renders.
    try:
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        recent = redact(store.activity_since(yesterday))
        if recent:
            lines.append("### Recent activity (last 24h)")
            # Group by action. Notable titles for nodes scoped to a different client
            # are dropped so their titles don't echo into the wrong session (the
            # aggregate counts stay complete — they reveal no titles). Titles of
            # non-active nodes are dropped too: archiving a node writes the very
            # activity entry that would otherwise echo its title back into context.
            scoping = bool(adapter) and adapter != "plain"
            action_counts: dict[str, int] = {}
            notable: list[str] = []
            for entry in recent:
                action = entry.get("action", "unknown")
                action_counts[action] = action_counts.get(action, 0) + 1
                if len(notable) >= 5:
                    continue
                try:
                    target = entry.get("target_title") or entry.get("target_id", "")
                    if not target:
                        continue
                    target_id = str(entry.get("target_id") or "")
                    if target_id:
                        target_node = store.get_node(target_id)
                        if target_node is not None and (
                                target_node.get("status") or "active") != "active":
                            continue
                    if scoping:
                        domains = store.get_node_domains(target_id)
                        if adapter_scoped_out(domains, adapter):
                            continue
                    notable.append(f"{action}: {target}")
                except Exception:
                    continue

            summary_parts = [f"{count} {action}" for action, count in action_counts.items()]
            lines.append(f"- Activity: {', '.join(summary_parts)}")
            for n in notable[:3]:
                lines.append(f"  - {n}")
            lines.append("")
    except Exception as e:
        section_failures.append(("prime.activity", e))

    # -- Active session tag --
    try:
        from .sessions import get_active_tag

        active_tag = redact(get_active_tag(store, project_path=os.getcwd()))
        if active_tag and node_expired(active_tag):
            active_tag = None
        if active_tag:
            extra = active_tag.get("extra") or {}
            tag_name = extra.get("tag", active_tag["title"])
            focus = extra.get("current_focus", "")
            remaining = extra.get("remaining", [])
            segments = extra.get("segments", [])

            lines.append(f"### Active session: {tag_name}")
            if focus:
                lines.append(f"**Focus:** {focus}")
            if remaining:
                lines.append(f"**Remaining:** {', '.join(remaining[:5])}")
            if segments:
                past = [s for s in segments if s.get("ended_at")]
                if past:
                    lines.append(f"**Previous segments:** {len(past)}")
                    for seg in past[-2:]:
                        lines.append(f"  - {seg['focus']}: {seg.get('summary', '')[:80]}")
            lines.append("")
    except Exception:
        pass  # Don't break priming if sessions module has issues

    # -- Active collabs (multi-agent coordination) --
    try:
        collab_cfg = config.collab if config else None
        display = str(collab_cfg.display or "full").lower() if collab_cfg else "full"
        if collab_cfg and collab_cfg.enabled and display != "quiet":
            from .config import resolve_agent_id
            from .coordination import active_collabs_for_agent

            collabs = redact(active_collabs_for_agent(store, resolve_agent_id(config)))
            if collabs:
                lines.append("### Active collabs")
                for c in collabs[:3]:
                    name = c.get("name", "")
                    unread = int(c.get("unread_count", 0) or 0)
                    injects = c.get("inject_messages") or []
                    locked = c.get("locked_resources") or []
                    focus = (c.get("focus") or "")[:80]

                    if display == "minimal":
                        parts = [f"{unread} unread"]
                        if injects:
                            parts.append(f"{len(injects)} standing msg")
                        if locked:
                            parts.append(f"{len(locked)} locked")
                        lines.append(
                            f"- {name}: {', '.join(parts)} — coord_read {name}"
                        )
                        continue

                    head = f"- **{name}** — {unread} unread"
                    if focus:
                        head += f" (focus: {focus})"
                    lines.append(head)
                    for m in injects[:3]:
                        text = " ".join(str(m.get("text", "")).split())[:200]
                        set_by = (m.get("set_by") or "").strip()
                        who = f" (from {set_by})" if set_by else ""
                        lines.append(f"  COLLAB MSG: {text}{who}")
                    for r in locked[:3]:
                        lines.append(
                            f"  Locked: {r.get('title') or r.get('node_id', '')} "
                            f"(held by {r.get('holder', '')})"
                        )
                    lines.append(f"  Check the collab: coord_read {name}")
                if len(collabs) > 3:
                    lines.append(f"- +{len(collabs) - 3} more")
                lines.append("")
    except Exception:
        pass  # Don't break priming

    # -- Due/upcoming reminders --
    try:
        if config and config.reminders.enabled:
            from .reminders import filter_reminders_for_conversation, scoped_due_reminders
            upcoming_window = datetime.datetime.now() + datetime.timedelta(hours=1)
            upcoming_iso = upcoming_window.isoformat(timespec="seconds")

            include_legacy = conversation_id is None
            due_now = scoped_due_reminders(
                store,
                conversation_id,
                include_global=True,
                include_legacy=include_legacy,
            )
            upcoming = [
                r for r in filter_reminders_for_conversation(
                    store.list_reminders(status="active"),
                    conversation_id,
                    include_global=True,
                    include_legacy=include_legacy,
                )
                if r["next_due"] <= upcoming_iso
            ]
            due_ids = {d["id"] for d in due_now}
            all_reminders = redact(due_now + [r for r in upcoming if r["id"] not in due_ids])

            if all_reminders:
                lines.append("### Reminders")
                for r in all_reminders[:5]:
                    prefix = "**DUE NOW**" if r["id"] in due_ids else "upcoming"
                    p_marker = f" [{r['priority']}]" if r.get("priority", "normal") != "normal" else ""
                    extra = r.get("extra") or {}
                    action_marker = ""
                    if extra.get("action_command") or extra.get("action_instructions"):
                        a_status = extra.get("action_status", "pending")
                        action_marker = f" [action: {a_status}]"
                    lines.append(
                        f"- {prefix}{p_marker}{action_marker}: {r['title']} "
                        f"(due: {r['next_due'][:16]}, id: {r['id']})"
                    )
                    if extra.get("action_command"):
                        lines.append(f"  Action: `{extra['action_command']}`")
                    if extra.get("action_instructions"):
                        lines.append(f"  Instructions: {extra['action_instructions'][:80]}")
                    if action_marker:
                        lines.append(
                            f"  Use `kin remind exec {r['id']}` to run action, "
                            f"`kin remind done {r['id']}` to dismiss, "
                            f"or `kin remind snooze {r['id']}`"
                        )
                    else:
                        lines.append(
                            f"  Use `kin remind done {r['id']}` or `kin remind snooze {r['id']}`"
                        )
                lines.append("")
    except Exception:
        pass  # Don't break priming

    # -- Contextual tasks --
    try:
        from .tasks import nearby_tasks, list_tasks, format_task_list

        # Gather seeds from FTS results already computed above
        seed_ids = [r["id"] for r in results[:5]] if results else []

        # Find contextual tasks near these seeds
        context_tasks = nearby_tasks(store, seed_ids, max_hops=2) if seed_ids else []

        # Also include global tasks
        global_tasks = list_tasks(store, status="open", scope="global", limit=5)
        seen_ids = {t["id"] for t in context_tasks}
        for gt in global_tasks:
            if gt["id"] not in seen_ids:
                context_tasks.append(gt)

        context_tasks = [redact(t) for t in context_tasks if not node_expired(t)]

        if context_tasks:
            lines.append("### Tasks")
            for t in context_tasks[:5]:
                extra = t.get("extra") or {}
                p = extra.get("priority", 3)
                p_label = {1: "P1/urgent", 2: "P2/high", 3: "P3", 4: "P4/low", 5: "P5"}.get(p, "P3")
                due = extra.get("due", "")
                due_str = f" (due: {due[:10]})" if due else ""
                scope = " [global]" if extra.get("scope") == "global" else ""
                lines.append(
                    f"- [{p_label}]{scope} {t['title']}{due_str} (id: {t['id']})"
                )
            lines.append(
                "  Use `task_done <id>` to complete, `task_add` to create new tasks"
            )
            lines.append("")
    except Exception:
        pass  # Don't break priming if tasks module has issues

    # -- Session directives (gated by reminders.remind_kindex_usage) --
    if config is None or config.reminders.remind_kindex_usage:
        lines.append("### Session directives")
        lines.append("You MUST use kindex MCP tools proactively — this is the user's external memory.")
        lines.append("**Capture as you go** — don't batch, don't wait until the end:")
        lines.append("- `search` FIRST to see what's known, and before every `add` to avoid duplicates")
        lines.append("- `add` discoveries, decisions (--type decision), questions (--type question)")
        lines.append("- `watch_add` for things needing monitoring (flaky tests, unstable APIs, pending items)")
        lines.append("- `watch_resolve` when a watched issue is fixed or no longer relevant")
        lines.append("- `link` aggressively — the graph's value is in connections, not isolated nodes")
        lines.append("- `learn` after reading long files/outputs to bulk-extract knowledge")
        lines.append("- `task_add` for work items — ALWAYS use link_to to connect to related concepts")
        lines.append("- `task_done` to complete tasks, `task_list` to see what's open")
        lines.append("- `remind_create` for time-based triggers (with `action`, `instructions`, or `wake`)")
        lines.append("- `tag_start`/`tag_update` to track session focus and progress")
        lines.append("**Project graph (`.kin/`)** — keep it with the code:")
        lines.append("- Look for a `.kin/` directory in the tree of the files you touch — not just your cwd root — and honor its config/index.")
        lines.append("- When you `git add`/commit, stage the matching `.kin/` changes (config, index.json) alongside the code so the graph travels with the work.")
        lines.append("")

    if search_failed and ops_failed:
        # Both core calls failed: no memory content could be primed, so
        # this is a failed invocation, not a partial prime. Re-raise and
        # let the top-level catch-all own the single degraded event and
        # the degraded output line. (activity_since swallows its own
        # errors, so the auxiliary sections can't gate this decision.)
        raise next(err for name, err in section_failures if name == "prime.search")

    # Partial success: the command completes, so the failed sections'
    # events are written now (and only now — never alongside a catch-all
    # event for the same invocation).
    for section, err in section_failures:
        _record_section_degraded(section, err, config)

    return redact_text("\n".join(lines) + "\n")


def capture_session_end(
    store: Store,
    config: Config,
    ledger: BudgetLedger,
    session_text: str | None = None,
) -> int:
    """Capture discoveries at session end.

    - Extracts knowledge from the session text using the extract pipeline
    - Creates new nodes and edges automatically
    - Stores bridge_opportunities in the suggestions table
    - Returns count of items captured
    """
    if not session_text or len(session_text.strip()) < 20:
        return 0

    from .extract import extract

    existing = [n["title"] for n in store.all_nodes(limit=200)]
    extraction = extract(session_text, existing, config, ledger)

    count = 0
    created_ids: list[str] = []

    # Add extracted concepts
    for concept in extraction.get("concepts", []):
        # Keyword fallback emits title-only concepts (useful for linking,
        # not worth minting) — never create content-empty nodes here.
        if not (concept.get("content") or "").strip():
            continue
        if store.get_node_by_title(concept["title"]):
            continue
        nid = store.add_node(
            title=concept["title"],
            content=concept.get("content", ""),
            node_type=concept.get("type", "concept"),
            domains=concept.get("domains", []),
            prov_activity="session-end-hook",
            prov_source="post-session",
        )
        created_ids.append(nid)
        count += 1

    # Add extracted decisions
    for decision in extraction.get("decisions", []):
        nid = store.add_node(
            title=decision["title"],
            content=decision.get("rationale", ""),
            node_type="decision",
            prov_activity="session-end-hook",
            prov_source="post-session",
        )
        created_ids.append(nid)
        count += 1

    # Add extracted questions
    for question in extraction.get("questions", []):
        nid = store.add_node(
            title=question["question"],
            content=question.get("context", ""),
            node_type="question",
            status="open-question",
            prov_activity="session-end-hook",
            prov_source="post-session",
        )
        created_ids.append(nid)
        count += 1

    # Add connections
    for conn in extraction.get("connections", []):
        from_node = store.get_node_by_title(conn.get("from_title", ""))
        to_node = store.get_node_by_title(conn.get("to_title", ""))
        if from_node and to_node:
            store.add_edge(
                from_node["id"], to_node["id"],
                edge_type=conn.get("type", "relates_to"),
                provenance="session-end-hook",
            )
            count += 1

    # Store bridge opportunities as suggestions
    for bridge in extraction.get("bridge_opportunities", []):
        concept_a = bridge.get("concept_a", "")
        concept_b = bridge.get("concept_b", "")
        reason = bridge.get("potential_link", "")
        if concept_a and concept_b:
            store.add_suggestion(
                concept_a=concept_a,
                concept_b=concept_b,
                reason=reason,
                source="session-end-hook",
            )
            count += 1

    # Link co-created nodes to prevent orphans
    if len(created_ids) > 1:
        for i in range(len(created_ids) - 1):
            store.add_edge(created_ids[i], created_ids[i + 1],
                           provenance="co-created-session-end")

    # Link captured nodes to active session tag
    if created_ids:
        try:
            from .sessions import get_active_tag, link_node_to_tag
            import os

            project_path = os.getcwd()
            active_tag = get_active_tag(store, project_path=project_path)
            if active_tag:
                tag_name = (active_tag.get("extra") or {}).get("tag", active_tag["title"])
                for nid in created_ids:
                    link_node_to_tag(
                        store, tag_name, nid, project_path=project_path
                    )
        except Exception as exc:
            # Hooks stay non-blocking, but a failed lifecycle link must remain
            # visible in the degraded-event ledger.
            _record_section_degraded("session-end-tag-link", exc, config)

    return count


def write_inbox_item(
    config: Config,
    content: str,
    source: str = "",
    topic_hint: str = "",
) -> Path:
    """Write an inbox item to the inbox directory.

    Uses atomic writes (write to tmp then rename).
    Format: YAML frontmatter + markdown body.

    Returns the path to the created file.
    """
    content = redact_text(content)
    source = redact_text(source)
    topic_hint = redact_text(topic_hint)
    inbox_dir = config.inbox_dir
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Generate a unique filename
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:6]
    filename = f"{ts}-{uid}.md"
    target = inbox_dir / filename

    # Build YAML frontmatter
    frontmatter_lines = [
        "---",
        f"created: {datetime.datetime.now().isoformat(timespec='seconds')}",
    ]
    if source:
        frontmatter_lines.append(f"source: {source}")
    if topic_hint:
        frontmatter_lines.append(f"topic_hint: {topic_hint}")
    frontmatter_lines.append("processed: false")
    frontmatter_lines.append("---")

    file_content = "\n".join(frontmatter_lines) + "\n\n" + content + "\n"

    # Atomic write: write to tmp file then rename
    tmp_dir = config.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(tmp_dir), suffix=".md", prefix="inbox-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(file_content)
        # Atomic rename (same filesystem)
        os.rename(tmp_path, str(target))
    except Exception:
        # Cleanup on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return target


def generate_session_directive(store: Store) -> str:
    """Generate CLAUDE.md text that instructs Claude Code to write back discoveries.

    Returns markdown string with instructions for capturing knowledge during sessions.
    """
    lines = [
        "## Kindex: Knowledge Capture (REQUIRED)",
        "",
        "You MUST use kindex MCP tools throughout this session to capture knowledge.",
        "Prefer MCP tools (`add`, `search`, `link`, `learn`) over CLI when available.",
        "",
        "### What to capture",
        "- **Discoveries**: patterns, findings, aha moments -- `add` as concept",
        "- **Decisions**: choices made and why -- `add` as decision",
        "- **Key files**: what a file does, why it exists -- `add` with file path",
        "- **Notable outputs**: test results, errors, metrics -- `add` as concept",
        "- **New terms**: domain jargon, recurring themes -- `add` as concept",
        "- **Open questions**: things to investigate later -- `add` as question",
        "- **Connections**: when two concepts relate -- `link` with reason",
        "",
        "### Rules",
        "- Always `search` before `add` to avoid duplicates",
        "- Use `learn` for bulk extraction from long text/files",
        "- Use `tag_start`/`tag_update` to track session context",
        "- Use `remind_create` with `action`/`instructions`/`wake` for deferred tasks",
        "",
        "### Project graph (`.kin/`)",
        "- Honor `.kin/` for the files you touch — look up the directory tree, not just the repo root.",
        "- Stage and commit `.kin/` changes (config, index.json) together with the related code.",
        "",
    ]

    # Add current graph context summary
    stats = store.stats()
    if stats["nodes"] > 0:
        lines.append(f"*Current graph: {stats['nodes']} nodes, {stats['edges']} edges.*")

        # Show pending suggestions count
        pending = store.pending_suggestions(limit=1)
        if pending:
            lines.append(f"*Run `kin suggest` to review bridge opportunities.*")
        lines.append("")

    return "\n".join(lines) + "\n"
