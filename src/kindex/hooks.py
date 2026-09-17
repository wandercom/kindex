"""Hook infrastructure for Claude Code integration.

Provides functions for SessionStart (prime_context), PostSession (capture_session_end),
inbox writes, and CLAUDE.md directive generation.
"""

from __future__ import annotations

import datetime
import json
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

    from .retrieve import GRAPH_DATA_NOTE, graph_text

    lines: list[str] = []
    lines.append("## Kindex Context (auto-primed)")
    lines.append(GRAPH_DATA_NOTE)
    lines.append("")

    # The budget is a ceiling on everything drawn from the graph, at about
    # four characters per token; see _fit_prime_to_budget.
    char_budget = max_tokens * 4

    # -- Key concepts (summarized tier) --
    # Built now, placed once the other sections are known: they are the
    # elastic part of the prime and take what the budget leaves.
    concept_entries: list[str] = []
    concepts_at = len(lines)
    if results:
        for r in results[:6]:
            try:
                title = graph_text(r.get("title", r["id"]), 200, single_line=True)
                ntype = graph_text(r.get("type", "concept"), 40, single_line=True)
                content = graph_text(r.get("content") or "", 120, single_line=True)
                # Search already fenced the neighbours; the client scope that
                # fenced the results applies to the titles named beside them.
                edges = [
                    e for e in r.get("edges_out", [])
                    if not adapter_scoped_out(store.get_node_domains(e.get("to_id")), adapter)
                ]
                connected = ", ".join(
                    graph_text(e.get("to_title") or e.get("to_id") or "", 80, single_line=True)
                    for e in edges[:3])

                entry = f"- **{title}** ({ntype})"
                if content:
                    entry += f": {content}"
                if connected:
                    entry += f" [{connected}]"
                # Imported Kinbase evidence carries its governance label and
                # open Unknowns on the same entry, so the budget keeps or
                # drops them together (context blocks already did this).
                from .kinbase import evidence_note
                caveat = evidence_note(r)
                if caveat:
                    entry += "\n  " + graph_text(caveat).replace("\n", "\n  ")
            except Exception:
                continue  # one malformed node never zeroes the prime
            concept_entries.append(entry)

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
                action = graph_text(extra.get("action", "warn"), 20, single_line=True)
                entry = f"- [{action}] {graph_text(c['title'], 200, single_line=True)}"
            except Exception:
                continue
            lines.append(entry)
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
                parts = [f"! {graph_text(w['title'], 200, single_line=True)}{urgent}"]
                if extra.get("owner"):
                    parts.append(f"@{graph_text(extra['owner'], 60, single_line=True)}")
                if expires:
                    parts.append(f"(expires {graph_text(expires, 30, single_line=True)})")
                entry = f"- {' '.join(parts)}"
            except Exception:
                continue
            lines.append(entry)
        lines.append("")

    if ops["directives"]:
        lines.append("### Directives")
        for d in ops["directives"][:2]:
            try:
                extra = d.get("extra")
                extra = extra if isinstance(extra, dict) else {}
                scope = graph_text(extra.get("scope", ""), 60, single_line=True)
                entry = f"- {graph_text(d['title'], 200, single_line=True)}"
                if scope:
                    entry += f" [scope: {scope}]"
            except Exception:
                continue
            lines.append(entry)
        lines.append("")

    # -- Recent activity summary (since yesterday) --
    # Shielded like the other sections: a failed activity pull skips this
    # section and the rest of the prime still renders.
    try:
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        recent = redact(store.activity_since(yesterday, limit=ACTIVITY_SCAN_LIMIT))
        # Totals are counted in full; only the rows scanned for titles are capped.
        totals = store.activity_counts_since(yesterday)
        if recent:
            lines.append("### Recent activity (last 24h)")
            # Group by action. Notable titles for nodes scoped to a different client
            # are dropped so their titles don't echo into the wrong session (the
            # aggregate counts stay complete — they reveal no titles). A title is
            # shown only for an active, unexpired node that still exists, under
            # its current name: archiving or deleting a node writes the very entry
            # that would otherwise echo its title back into context, and a
            # reminder (not a node) may belong to another conversation.
            scoping = bool(adapter) and adapter != "plain"
            action_counts: dict[str, int] = dict(totals)
            notable: list[str] = []
            for entry in recent:
                action = entry.get("action", "unknown")
                if not totals:
                    action_counts[action] = action_counts.get(action, 0) + 1
                if len(notable) >= 5:
                    continue
                try:
                    target_id = str(entry.get("target_id") or "")
                    if not target_id or action.startswith("delete"):
                        continue
                    target_node = store.peek_node(target_id)
                    if (target_node is None
                            or (target_node.get("status") or "active") != "active"
                            or node_expired(target_node)):
                        continue
                    if scoping:
                        domains = store.get_node_domains(target_id)
                        if adapter_scoped_out(domains, adapter):
                            continue
                    target = graph_text(target_node.get("title") or target_id, 120,
                                        single_line=True)
                    notable.append(f"{graph_text(action, 40, single_line=True)}: {target}")
                except Exception:
                    continue

            summary_parts = [f"{count} {graph_text(action, 40, single_line=True)}"
                             for action, count in action_counts.items()]
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
            tag_name = graph_text(extra.get("tag", active_tag["title"]), 80, single_line=True)
            focus = graph_text(extra.get("current_focus", ""), 200, single_line=True)
            remaining = [graph_text(item, 80, single_line=True)
                         for item in extra.get("remaining", []) or []]
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
                        lines.append(
                            f"  - {graph_text(seg.get('focus', ''), 80, single_line=True)}: "
                            f"{graph_text(seg.get('summary', ''), 80, single_line=True)}")
            lines.append("")
    except Exception as e:
        section_failures.append(("prime.session", e))

    # -- Active collabs (multi-agent coordination) --
    try:
        collab_cfg = config.collab if config else None
        display = str(collab_cfg.display or "full").lower() if collab_cfg else "full"
        if collab_cfg and collab_cfg.enabled and display != "quiet":
            from .config import resolve_agent_id
            from .coordination import active_collabs_for_agent

            skipped: list = []
            collabs = redact(active_collabs_for_agent(
                store, resolve_agent_id(config), skipped=skipped))
            from .coordination import skipped_conversation_error
            section_failures.extend(
                ("prime.collabs", skipped_conversation_error(cid, error))
                for cid, error in skipped)
            if collabs:
                from .coordination import render_field
                lines.append("### Active collabs")
                for c in collabs[:3]:
                    name = render_field(c.get("name", ""))
                    # The id is unambiguous; a name can be reused later.
                    ref = render_field(c.get("node_id") or c.get("name", ""))
                    unread = int(c.get("unread_count", 0) or 0)
                    injects = c.get("inject_messages") or []
                    locked = c.get("locked_resources") or []
                    focus = render_field(c.get("focus") or "")

                    if display == "minimal":
                        parts = [f"{unread} unread"]
                        if injects:
                            parts.append(f"{len(injects)} standing msg")
                        if locked:
                            parts.append(f"{len(locked)} locked")
                        lines.append(
                            f"- {name}: {', '.join(parts)} — coord_read {ref}"
                        )
                        continue

                    head = f"- **{name}** — {unread} unread"
                    if focus:
                        head += f" (focus: {focus})"
                    lines.append(head)
                    for m in injects[:3]:
                        text = render_field(m.get("text", ""), 200)
                        set_by = render_field(m.get("set_by") or "")
                        who = f" (from {set_by})" if set_by else ""
                        lines.append(f"  COLLAB MSG: {text}{who}")
                    for r in locked[:3]:
                        lines.append(
                            f"  Locked: {render_field(r.get('title') or r.get('node_id', ''))} "
                            f"(held by {render_field(r.get('holder', ''))})"
                        )
                    lines.append(f"  Check the collab: coord_read {ref}")
                if len(collabs) > 3:
                    lines.append(f"- +{len(collabs) - 3} more")
                lines.append("")
    except Exception as error:
        # Don't break priming; the skipped section is recorded with the others.
        section_failures.append(("prime.collabs", error))

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
                        f"- {prefix}{p_marker}{action_marker}: "
                        f"{graph_text(r['title'], 200, single_line=True)} "
                        f"(due: {r['next_due'][:16]}, id: {r['id']})"
                    )
                    if action_marker:
                        lines.append("  " + reminder_action_summary(extra))
                        # No call to run it: the preview is not the action,
                        # and injected context must not ask for an execution.
                        lines.append(
                            f"  Review it with `kin remind show --reminder-id {r['id']} --json`; "
                            f"dismiss with `kin remind done --reminder-id {r['id']}` "
                            f"or `kin remind snooze --reminder-id {r['id']}`"
                        )
                    else:
                        lines.append(
                            f"  Use `kin remind done --reminder-id {r['id']}` or "
                            f"`kin remind snooze --reminder-id {r['id']}`"
                        )
                lines.append("")
    except Exception as e:
        section_failures.append(("prime.reminders", e))

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
                    f"- [{p_label}]{scope} {graph_text(t['title'], 200, single_line=True)}"
                    f"{graph_text(due_str, 30, single_line=True)} (id: {t['id']})"
                )
            lines.append(
                "  Use `task_done <id>` to complete, `task_add` to create new tasks"
            )
            lines.append("")
    except Exception as e:
        section_failures.append(("prime.tasks", e))

    lines = _fit_prime_to_budget(
        lines[:concepts_at], concept_entries if results else None,
        lines[concepts_at:], char_budget)

    # -- Session directives (gated by reminders.remind_kindex_usage) --
    # Kindex's own fixed instructions, not graph content: outside the budget.
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


#: Rows of activity the prime reads for its 24-hour summary.
ACTIVITY_SCAN_LIMIT = 500

#: The prime's sections in the order they give way to the budget: the first
#: loses entries first; due reminders go last. Matched by heading prefix.
PRIME_TRIM_ORDER = (
    "### Recent activity", "### Active session", "### Tasks", "### Directives",
    "### Watches", "### Active collabs", "### Active constraints", "### Reminders",
)
#: The share of the budget key concepts keep however full the other sections are.
PRIME_CONCEPT_FLOOR = 0.4


def _prime_sections(lines: list[str]) -> list[dict]:
    """Split rendered prime lines into sections of whole entries. An entry is
    a "- " line with the indented lines under it; any other line is its own."""
    sections: list[dict] = []
    for line in lines:
        if line.startswith("### ") or not sections:
            sections.append({"heading": line, "entries": [], "omitted": 0})
            continue
        entries = sections[-1]["entries"]
        if line == "":
            continue
        if line.startswith("  ") and entries:
            entries[-1].append(line)
        else:
            entries.append([line])
    return sections


def _render_prime_section(section: dict) -> list[str]:
    if not section["entries"]:
        return []
    out = [section["heading"]]
    for entry in section["entries"]:
        out.extend(entry)
    out.append("")
    return out


#: Room kept for the line naming what the budget left out.
_OMISSION_RESERVE = 200


def _fit_prime_to_budget(head: list[str], concepts: list[str] | None,
                         tail: list[str], budget: int) -> list[str]:
    """Assemble the prime inside ``budget`` characters.

    Key concepts take what the other sections leave, but never less than
    ``PRIME_CONCEPT_FLOOR`` of the budget; the other sections then give up
    entries in ``PRIME_TRIM_ORDER`` until the whole fits, and one closing
    line says what was left out. Only key concepts were bounded before, so a
    busy graph produced a prime far over its budget.
    """
    def size(block: list[str]) -> int:
        return sum(len(line) + 1 for line in block)

    sections = _prime_sections(tail)
    head_size = size(head)

    def tail_size() -> int:
        return sum(size(_render_prime_section(s)) for s in sections)

    heading = "### Key concepts"

    def allocate(room: int) -> tuple[list[str], int]:
        chosen: list[str] = []
        used = 0
        for index, entry in enumerate(concepts or []):
            if used + len(entry) + 1 > room:
                return chosen, len(concepts) - index
            chosen.append(entry)
            used += len(entry) + 1
        return chosen, 0

    def render_concepts(chosen: list[str]) -> list[str]:
        return [heading, *chosen, ""] if concepts is not None else []

    room = max(budget - head_size - tail_size() - len(heading) - 2,
               int(budget * PRIME_CONCEPT_FLOOR))
    chosen, concepts_left = allocate(room)
    total = head_size + size(render_concepts(chosen)) + tail_size()
    if total > budget or concepts_left:
        # Something will be left out, so the closing line's room is kept
        # before anything else is placed.
        room = max(budget - head_size - tail_size() - len(heading) - 2 - _OMISSION_RESERVE,
                   int(budget * PRIME_CONCEPT_FLOOR))
        chosen, concepts_left = allocate(room)
        total = head_size + size(render_concepts(chosen)) + tail_size()
    limit = budget - _OMISSION_RESERVE if total > budget or concepts_left else budget
    for prefix in PRIME_TRIM_ORDER:
        for section in sections:
            if total <= limit:
                break
            if not section["heading"].startswith(prefix):
                continue
            while total > limit and section["entries"]:
                before = size(_render_prime_section(section))
                section["entries"].pop()
                section["omitted"] += 1
                total += size(_render_prime_section(section)) - before
    # The concept floor gives way last, so the whole never passes the budget.
    while total > limit and chosen:
        total -= len(chosen.pop()) + 1
        concepts_left += 1
    concept_lines = render_concepts(chosen)
    rendered = list(head) + concept_lines
    for section in sections:
        rendered.extend(_render_prime_section(section))
    left_out = [f"{concepts_left} key concepts"] if concepts_left else []
    left_out += [f"{section['omitted']} of {section['heading'].lstrip('# ').split(':')[0]}"
                 for section in sections if section["omitted"]]
    if left_out:
        line = ("_(Left out for the token budget: " + "; ".join(left_out)
                + ". Ask with `context` or `search` for more.)_")
        rendered.append(line[:_OMISSION_RESERVE - 1])
    return rendered


#: Characters of a reminder action shown in the prime.
ACTION_PREVIEW_CHARS = 80


def reminder_action_summary(extra: dict) -> str:
    """What `kin remind exec` would run, stated without pretending a preview
    is the whole action: the resolved mode, the full action's size and
    digest, and a one-line preview marked when it is cut."""
    import hashlib

    from .actions import resolve_mode
    from .retrieve import graph_text

    mode = resolve_mode(extra)
    command = str(extra.get("action_command") or "")
    instructions = str(extra.get("action_instructions") or "")
    # The digest covers everything exec may use, so a changed command under
    # unchanged instructions (or the reverse) changes it too.
    digest = hashlib.sha256(
        json.dumps({"mode": mode, "command": command, "instructions": instructions},
                   sort_keys=True).encode("utf-8")).hexdigest()[:12]
    shown = instructions if (mode == "claude" and instructions) else (command or instructions)
    sizes = ", ".join(
        f"{name} {len(value)} chars"
        for name, value in (("instructions", instructions), ("command", command)) if value
    )
    preview = graph_text(shown, ACTION_PREVIEW_CHARS, single_line=True).replace("`", "\u02cb")
    cut = " …(truncated)" if len(" ".join(shown.split())) > ACTION_PREVIEW_CHARS else ""
    return (f"Action ({mode}, {sizes}, sha256:{digest}): "
            f"{preview}{cut}")


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
