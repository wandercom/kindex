"""Background ingestion — one-shot cron mode and file change detection."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .privacy import redact_text, safe_error
from .privacy import redacting_print as print

from .routing import (  # noqa: F401  (re-exported for backward compatibility)
    _encode_claude_project_dir,
    _session_cwd,
    _session_profile_owner,
    cwd_profile_owner,
    effective_session_filter,
    profile_session_filter,
)

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


def cron_run(config: "Config", store: "Store", verbose: bool = False) -> dict:
    """One-shot run of all maintenance tasks. Designed for crontab.

    Steps:
    0. Check reminders (FIRST — cheap and local; must never wait behind
       networked ingest/LLM/embedding work that can stall for minutes)
    1. Ingest new projects (scan for CLAUDE.md files)
    2. Ingest new sessions (scan ~/.claude/projects/ for JSONL)
    3. Process inbox items
    4. Apply weight decay
    5. Run doctor checks

    Returns dict of results.
    """
    from .ingest import scan_kin_files, scan_projects, scan_sessions

    results: dict = {}

    # 0. Check reminders before anything that can block. Every later step may
    # hit the network (session ingest, reinforcement/sim/attention drains,
    # embedding drain) — a due reminder must fire within seconds of cron start.
    reminder_results = _check_reminders(config, store, verbose=verbose)
    results["reminders_fired"] = reminder_results.get("fired", 0)
    results["reminders_auto_snoozed"] = reminder_results.get("auto_snoozed", 0)

    # 1. Ingest new projects
    if verbose:
        print("Scanning projects...")
    proj_count = scan_projects(config, store, verbose=verbose)
    kin_count = scan_kin_files(config, store, verbose=verbose)
    results["projects"] = proj_count
    results["kin_updates"] = kin_count

    # 2. Ingest new sessions
    if verbose:
        print("Scanning sessions...")
    session_count = scan_sessions(config, store, limit=50, verbose=verbose)
    results["sessions"] = session_count

    # 3. Process inbox items
    inbox_count = _process_inbox(config, store, verbose=verbose)
    results["inbox"] = inbox_count

    # 4. Apply weight decay (nodes/edges + stigmergic pheromone trails)
    if verbose:
        print("Applying weight decay...")
    decay_count = store.apply_weight_decay()
    results["decayed"] = decay_count
    try:
        results["pheromone_pruned"] = store.decay_pheromone(
            half_life_days=config.attention.pheromone_half_life_days)
        # Drain queued session-end reinforcement (this is the LLM cost — kept
        # off the agent's critical path; the Stop/compact hooks only enqueue).
        from .reinforce import auto_ramp_pheromone_weight, drain_reinforce_queue
        if verbose:
            print("Grading queued sessions (reinforcement)...")
        drained = drain_reinforce_queue(store, config)
        results["reinforce_graded"] = drained.get("graded", 0)
        results["reinforce_pending"] = drained.get("pending", 0)
        # Re-evaluate maturity and auto-ramp the pheromone ranking weight.
        ramp = auto_ramp_pheromone_weight(store, config)
        results["pheromone_weight"] = ramp.get("weight")
        if verbose and ramp.get("ramped"):
            print(f"Pheromone ranking weight -> {ramp.get('weight')} ({ramp.get('reason')})")
    except Exception:
        results["pheromone_pruned"] = 0

    # Drain queued Sim supervisory reviews (opt-in; the LLM/Sim spend, kept off
    # the agent's path — the prompt hook only enqueues window snapshots).
    try:
        from .sim import drain_sim_queue, sim_effective_enabled
        if sim_effective_enabled(store, config):
            if verbose:
                print("Reviewing queued windows (Sim supervisory)...")
            sim_drained = drain_sim_queue(store, config)
            results["sim_reviewed"] = sim_drained.get("reviewed", 0)
            results["sim_flagged"] = sim_drained.get("flagged", 0)
            results["sim_pending"] = sim_drained.get("pending", 0)
    except Exception:
        results["sim_reviewed"] = 0

    # Drain queued attention reviews. Hook-time attention only waits for a fast
    # result; slower LLM arbitration lands here for later prompt pickup.
    try:
        from .attention import drain_attention_queue
        attention_drained = drain_attention_queue(store, config)
        results["attention_reviewed"] = attention_drained.get("reviewed", 0)
        results["attention_flagged"] = attention_drained.get("flagged", 0)
        results["attention_pending"] = attention_drained.get("pending", 0)
    except Exception:
        results["attention_reviewed"] = 0

    # Drain queued node embeddings. Enqueued cheaply on the add/edit/supersede
    # hot path; the (possibly networked) embedding cost runs here, off the
    # agent's critical path.
    try:
        from .vectors import (
            contextual_embeddings_supported,
            drain_embedding_queue,
            embedding_status,
            enqueue_reindex,
        )
        if verbose:
            print("Embedding queued nodes...")
        # Contextual chunk indexes need a one-time rebuild when the model or
        # strategy changes. Trickle stale nodes into the normal queue so large
        # graphs converge over cron cycles instead of blocking one run.
        status = embedding_status(store)
        if (contextual_embeddings_supported(config)
                and int(status.get("queue_pending") or 0) == 0):
            limit = max(1, int(getattr(config.embedding, "reindex_max_jobs", 200) or 200))
            queued = enqueue_reindex(store, stale=True, status="active", limit=limit)
            results["embed_enqueued_stale"] = queued.get("enqueued", 0)
        embed_drained = drain_embedding_queue(store, config)
        results["embedded"] = embed_drained.get("embedded", 0)
        results["embed_pending"] = embed_drained.get("pending", 0)
    except Exception:
        results["embedded"] = 0

    # 5. Run doctor checks
    if verbose:
        print("Running health checks...")
    stats = store.stats()
    orphans = store.orphans()
    results["stats"] = stats
    results["orphan_count"] = len(orphans)

    # 6. Suggest cross-component links
    suggestion_count = _suggest_links(store, verbose=verbose)
    results["link_suggestions"] = suggestion_count

    # 7. Graph hygiene — archive stale orphans, auto-link viable ones
    hygiene = _graph_hygiene(store, verbose=verbose)
    results["orphans_archived"] = hygiene.get("archived", 0)
    results["orphans_linked"] = hygiene.get("linked", 0)

    # 8. Slow graph archival — move deeply decayed nodes to archive
    try:
        from .archive import archive_cycle
        archived_to_slow = archive_cycle(config, store, verbose=verbose)
        results["slow_graph_archived"] = archived_to_slow
    except Exception:
        results["slow_graph_archived"] = 0

    # 9. Watch hygiene — expire overdue watches
    watch_results = _check_watches(store, verbose=verbose)
    results["watches_expired"] = watch_results.get("expired", 0)
    results["watches_notified"] = watch_results.get("notified", 0)

    # 9a. Generic expiry — archive expired non-watch nodes
    expire_results = _expire_nodes(store, verbose=verbose)
    results["nodes_expired"] = expire_results.get("archived", 0)

    # 9b. Collab hygiene — expired locks, conversations, and task claims
    try:
        from .coordination import cleanup_expired_conversations
        from .locks import cleanup_expired_locks
        from .tasks import cleanup_expired_claims
        if verbose:
            print("Sweeping expired locks, conversations, and claims...")
        results["locks_cleared"] = cleanup_expired_locks(store)
        results["conversations_expired"] = cleanup_expired_conversations(store)
        results["claims_released"] = cleanup_expired_claims(store)
    except Exception:
        # Best-effort hygiene — a malformed collab row must not break cron
        results.setdefault("locks_cleared", 0)
        results.setdefault("conversations_expired", 0)
        results.setdefault("claims_released", 0)

    # 9c. Quarantined capture retention — expiry only, never promotion.
    try:
        if verbose:
            print("Pruning expired capture candidates...")
        results["capture_candidates_pruned"] = store.prune_capture_candidates()
    except Exception:
        results["capture_candidates_pruned"] = 0

    # 10. Re-check reminders — anything that came due while maintenance ran.
    late_reminders = _check_reminders(config, store, verbose=verbose)
    results["reminders_fired"] += late_reminders.get("fired", 0)
    results["reminders_auto_snoozed"] += late_reminders.get("auto_snoozed", 0)

    # 11. Lightweight dream — dedup + suggestion auto-apply, on its own cadence
    try:
        from .dream import dream_cycle, dream_due
        min_interval = max(0, int(getattr(config.reminders, "dream_min_interval", 3600) or 0))
        decision = dream_due(store, min_interval_seconds=min_interval)
        if decision.get("due"):
            dream_results = dream_cycle(config, store, mode="lightweight", verbose=verbose)
            results["dream_merged"] = dream_results.get("merged", 0)
            results["dream_suggestions_applied"] = dream_results.get("suggestions_applied", 0)
            if dream_results.get("skipped"):
                results["dream_skipped"] = dream_results["skipped"]
        else:
            results["dream_merged"] = 0
            results["dream_suggestions_applied"] = 0
            results["dream_skipped"] = decision.get("skipped", "not_due")
    except Exception:
        results["dream_merged"] = 0
        results["dream_suggestions_applied"] = 0

    # Update run marker
    set_run_marker(store)

    # Adaptive scheduling: repack cron interval based on nearest reminder
    try:
        from .scheduling import repack_schedule
        repack = repack_schedule(store, config)
        results["repack"] = repack
    except Exception:
        pass  # don't let scheduling errors break cron

    return results


def remind_check_all(base_config: "Config", verbose: bool = False) -> list[dict]:
    """Reminder-only sweep across all configured profiles (plus the legacy
    remainder graph) AND registered project-local ``.kin`` graphs. Cheap and
    local — no ingest, no LLM, no embedding.

    Runs first in ``cron_run_all`` and backs ``kin remind check --all-profiles``
    so due reminders can never be starved by slow maintenance in another
    profile's pass. Project graphs (a ``data_dir`` declared in a repo's
    ``.kin/config``) belong to no profile — they are discovered by cron's
    ``scan_kin_files`` into the ``project_graph_dirs`` meta registry and swept
    here, since nothing else scheduled would ever fire their reminders.
    Returns [{profile, fired, auto_snoozed}] per graph.
    """
    import json as _json

    from .store import Store

    swept_dirs: set[str] = set()
    project_registry: dict[str, str] = {}

    def _one(cfg: "Config", name: str | None) -> dict:
        # Fault-isolated: a corrupt or locked graph must not abort the sweep
        # for every other graph's reminders.
        swept_dirs.add(str(cfg.data_path))
        try:
            store = Store(cfg)
        except Exception as e:
            return {"profile": name, "fired": 0, "auto_snoozed": 0,
                    "error": safe_error(e)}
        try:
            r = _check_reminders(cfg, store, verbose=verbose)
            try:
                raw = _json.loads(store.get_meta("project_graph_dirs") or "{}")
                if isinstance(raw, dict):
                    project_registry.update(raw)
            except Exception:
                pass
        except Exception as e:
            return {"profile": name, "fired": 0, "auto_snoozed": 0,
                    "error": safe_error(e)}
        finally:
            store.close()
        return {"profile": name, "fired": r.get("fired", 0),
                "auto_snoozed": r.get("auto_snoozed", 0)}

    results: list[dict] = []
    profiles = dict(getattr(base_config, "profiles", {}) or {})
    if not profiles:
        results.append(_one(base_config, getattr(base_config, "active_profile", None)))
    else:
        for name, entry in profiles.items():
            cfg = base_config.model_copy(deep=True)
            cfg.data_dir = str(Path(entry.data_dir).expanduser())
            # Anchor scheduler logs to the base dir (see cron_run_all).
            cfg._legacy_data_dir = (
                getattr(base_config, "_legacy_data_dir", None) or base_config.data_dir
            )
            cfg.active_profile = name
            cfg.profile_source = "cron"
            results.append(_one(cfg, name))
        if getattr(base_config, "default_profile", None) is None:
            cfg = base_config.model_copy(deep=True)
            legacy_dir = getattr(base_config, "_legacy_data_dir", None)
            if legacy_dir:
                cfg.data_dir = legacy_dir
            cfg.active_profile = None
            cfg.profile_source = "legacy"
            results.append(_one(cfg, None))

    # Project-local .kin graphs, deduped against the dirs already swept.
    for project_root, data_dir in sorted(project_registry.items()):
        if not Path(data_dir).exists():
            continue  # project deleted since registration — ages out on scan
        cfg = base_config.model_copy(deep=True)
        cfg.data_dir = data_dir
        # Anchor scheduler logs to the base dir (see cron_run_all).
        cfg._legacy_data_dir = (
            getattr(base_config, "_legacy_data_dir", None) or base_config.data_dir
        )
        cfg.active_profile = None
        cfg.profile_source = "project"
        if str(cfg.data_path) in swept_dirs:
            continue
        results.append(_one(cfg, project_root))
    return results


def cron_run_all(base_config: "Config", verbose: bool = False) -> list[dict]:
    """Run the cron maintenance cycle across all configured profiles.

    Every run starts with a reminders-first sweep (``remind_check_all``) so
    no graph's reminders wait behind another graph's maintenance and
    project-local ``.kin`` graphs get serviced at all.

    No profiles configured -> a single legacy pass on base_config (pre-profile
    behavior, preceded by the sweep). With profiles -> one pass per
    profile, each on its own data_dir/Store. Claude session ingestion within
    a pass only takes sessions whose cwd falls under that profile's roots;
    the default profile additionally takes unmatched sessions. With profiles
    but NO default_profile, a final legacy-remainder pass on the legacy
    data_dir ingests the unmatched sessions and keeps the legacy graph's
    maintenance (decay, reminders, watches, dream) running — mirroring the
    interactive legacy-passthrough resolution tier.

    Returns a list of {profile, source, results} dicts, one per pass.
    """
    from .store import Store

    profiles = dict(getattr(base_config, "profiles", {}) or {})

    # Reminders-first sweep across ALL graphs — profiles or legacy, plus
    # registered project-local .kin graphs — before any maintenance pass.
    # Within a pass reminders already run first, but graph B's reminders must
    # not wait behind graph A's ingest/LLM/embedding work, and project graphs
    # have no maintenance pass of their own at all.
    sweep_entries = remind_check_all(base_config, verbose=verbose)
    sweep = {s["profile"]: s for s in sweep_entries}
    # Base-graph sweep entries are keyed by active_profile (a pinned
    # `kin cron --profile X` clears profiles but keeps active_profile='X') —
    # exclude them from the project-graph tally or one firing counts twice.
    known_names = set(profiles) | {None, getattr(base_config, "active_profile", None)}
    project_fired = sum(
        s.get("fired", 0) for s in sweep_entries if s["profile"] not in known_names
    )

    if not profiles:
        store = Store(base_config)
        try:
            results = cron_run(base_config, store, verbose=verbose)
        finally:
            store.close()
        swept = sweep.get(getattr(base_config, "active_profile", None))
        if swept:
            results["reminders_fired"] += swept["fired"]
            results["reminders_auto_snoozed"] += swept["auto_snoozed"]
        if project_fired:
            results["project_graph_reminders_fired"] = project_fired
        return [{
            "profile": getattr(base_config, "active_profile", None),
            "source": getattr(base_config, "profile_source", "legacy"),
            "results": results,
        }]

    default_name = getattr(base_config, "default_profile", None)
    passes: list[dict] = []
    for name, entry in profiles.items():
        cfg = base_config.model_copy(deep=True)
        cfg.data_dir = str(Path(entry.data_dir).expanduser())
        # Keep scheduler logs anchored to the base dir: this pass's config
        # reaches the adaptive repack (scheduling._apply_crontab), and the
        # crontab log target must not drift to a profile dir (issue #15).
        cfg._legacy_data_dir = (
            getattr(base_config, "_legacy_data_dir", None) or base_config.data_dir
        )
        cfg.active_profile = name
        cfg.profile_source = "cron"
        cfg._session_filter = profile_session_filter(profiles, name, default_name)
        if verbose:
            print(f"== Profile pass: {name} ({cfg.data_dir}) ==")
        store = Store(cfg)
        try:
            results = cron_run(cfg, store, verbose=verbose)
        finally:
            store.close()
        swept = sweep.get(name)
        if swept:
            results["reminders_fired"] += swept["fired"]
            results["reminders_auto_snoozed"] += swept["auto_snoozed"]
        passes.append({"profile": name, "source": "cron", "results": results})

    if default_name is None:
        # Legacy-remainder pass: interactive resolution (config tier 6) keeps
        # writing to the legacy data_dir when nothing matches and no default
        # is set, so cron must keep servicing that graph and ingest exactly
        # the sessions no profile owns.
        cfg = base_config.model_copy(deep=True)
        legacy_dir = getattr(base_config, "_legacy_data_dir", None)
        if legacy_dir:
            cfg.data_dir = legacy_dir
        cfg.active_profile = None
        cfg.profile_source = "legacy"
        cfg._session_filter = profile_session_filter(profiles, None, None)
        if verbose:
            print(f"== Legacy remainder pass ({cfg.data_dir}) ==")
        store = Store(cfg)
        try:
            results = cron_run(cfg, store, verbose=verbose)
        finally:
            store.close()
        swept = sweep.get(None)
        if swept:
            results["reminders_fired"] += swept["fired"]
            results["reminders_auto_snoozed"] += swept["auto_snoozed"]
        passes.append({"profile": None, "source": "legacy-remainder",
                       "results": results})
    if project_fired and passes:
        passes[0]["results"]["project_graph_reminders_fired"] = project_fired
    return passes


def _process_inbox(config: "Config", store: "Store", verbose: bool = False) -> int:
    """Process pending inbox items (markdown files in inbox/)."""
    inbox = config.inbox_dir
    if not inbox.exists():
        return 0

    count = 0
    for md_file in sorted(inbox.glob("*.md")):
        try:
            text = md_file.read_text(errors="replace").strip()
        except OSError:
            continue

        if not text or len(text) < 10:
            continue

        from .extract import keyword_extract

        extraction = keyword_extract(text)
        concepts = extraction.get("concepts", [])

        for concept in concepts:
            existing = store.get_node_by_title(concept["title"])
            if existing:
                continue
            store.add_node(
                title=concept["title"],
                content=concept.get("content", text[:500]),
                node_type=concept.get("type", "concept"),
                domains=concept.get("domains", []),
                prov_activity="inbox-ingest",
                prov_source=str(md_file),
            )
            count += 1
            if verbose:
                print(f"  Inbox: {concept['title']}")

        # Move processed file to .processed
        processed_dir = inbox / ".processed"
        processed_dir.mkdir(exist_ok=True)
        md_file.rename(processed_dir / md_file.name)

    return count


def _suggest_links(store: "Store", verbose: bool = False) -> int:
    """Find and store cross-component link suggestions."""
    try:
        from .graph import suggest_cross_component_links

        suggestions = suggest_cross_component_links(store, max_suggestions=5)
        count = 0
        for s in suggestions:
            # Check if this suggestion already exists
            existing = store.pending_suggestions(limit=100)
            already = any(
                (e["concept_a"] == s["concept_a"] and e["concept_b"] == s["concept_b"])
                or (e["concept_a"] == s["concept_b"] and e["concept_b"] == s["concept_a"])
                for e in existing
            )
            if not already:
                store.add_suggestion(
                    concept_a=s["concept_a"],
                    concept_b=s["concept_b"],
                    reason=s["reason"],
                    source="cron-auto-suggest",
                )
                count += 1
                if verbose:
                    print(f"  Suggested: {s['concept_a']} <-> {s['concept_b']}")

        return count
    except Exception:
        return 0


def _graph_hygiene(store: "Store", verbose: bool = False) -> dict:
    """Archive stale orphans and auto-link viable ones.

    Stale: orphan with weight < 0.15 and not updated in 30+ days.
    Viable: orphan whose title FTS-matches existing connected nodes.
    """
    import datetime as _dt

    results = {"archived": 0, "linked": 0}

    try:
        orphans = store.orphans()
    except Exception:
        return results

    if not orphans:
        return results

    now = _dt.datetime.now()
    stale_cutoff = now - _dt.timedelta(days=30)

    for orphan in orphans:
        oid = orphan["id"]
        weight = orphan.get("weight", 0.5) or 0.5
        title = orphan.get("title", "")
        node_type = orphan.get("type", "concept")

        # Skip task/session/checkpoint nodes — they have their own lifecycle
        if node_type in ("task", "session", "checkpoint", "directive", "constraint"):
            continue

        # Parse updated_at
        updated = orphan.get("updated_at", "")
        try:
            updated_dt = _dt.datetime.fromisoformat(updated) if updated else _dt.datetime.min
        except (ValueError, TypeError):
            updated_dt = _dt.datetime.min

        is_stale = weight < 0.15 and updated_dt < stale_cutoff

        if is_stale:
            # Archive: set status to archived, drop weight to floor
            store.update_node(oid, status="archived", weight=0.01)
            results["archived"] += 1
            if verbose:
                print(f"  Archived orphan: {title} (w={weight:.2f})")
            continue

        # Try to auto-link viable orphans via FTS title match
        if not title or len(title) < 3:
            continue

        matches = store.fts_search(title, limit=5)
        for match in matches:
            mid = match["id"]
            if mid == oid:
                continue
            # Only link to nodes that already have edges (not other orphans)
            if (
                not store.edges_from(mid, semantic_only=True)
                and not store.edges_to(mid, semantic_only=True)
            ):
                continue
            # Create a low-weight link
            store.add_edge(
                oid, mid,
                edge_type="relates_to",
                weight=0.2,
                provenance="auto-linked by graph hygiene",
            )
            results["linked"] += 1
            if verbose:
                print(f"  Linked orphan: {title} -> {match.get('title', mid)}")
            break  # One link is enough to de-orphan

    return results


def _check_watches(store: "Store", verbose: bool = False) -> dict:
    """Check watch nodes for expiry and flag triggered ones.

    - Expired watches (past their expires date) get archived.
    - Watches with check_command in extra get flagged for Claude attention.
    """
    import datetime as _dt

    results = {"expired": 0, "notified": 0}

    try:
        # Query all active watch nodes (including those past expiry that
        # haven't been archived yet — active_watches() filters those out)
        watches = store.all_nodes(node_type="watch", status="active", limit=200)
    except Exception:
        return results

    today = _dt.date.today().isoformat()

    for w in watches:
        extra = w.get("extra") or {}
        expires = extra.get("expires", "")
        wid = w["id"]

        # Expire overdue watches
        if expires and expires < today:
            store.update_node(wid, status="archived")
            results["expired"] += 1
            if verbose:
                print(f"  Expired watch: {w['title']} (was due {expires})")
            continue

        # Boost weight of watches approaching expiry (within 3 days)
        if expires:
            try:
                exp_date = _dt.date.fromisoformat(expires)
                days_left = (exp_date - _dt.date.today()).days
                if 0 <= days_left <= 3:
                    # Boost so it surfaces prominently in prime_context
                    current_weight = w.get("weight", 0.5)
                    if current_weight < 0.8:
                        store.update_node(wid, weight=0.9)
                        results["notified"] += 1
                        if verbose:
                            print(f"  Boosted watch: {w['title']} ({days_left}d left)")
            except (ValueError, TypeError):
                pass

    return results


def _expire_nodes(store: "Store", verbose: bool = False) -> dict:
    """Archive non-watch nodes whose extra['expires'] date has passed.

    Watches keep their dedicated lifecycle in _check_watches (near-expiry
    weight boost + archive). Every other expired node is archived with
    extra['expired_at'] stamped for provenance.
    """
    import datetime as _dt
    from .store import node_expired

    results = {"archived": 0}

    try:
        nodes = store.nodes_with_expiry(status="active")
    except Exception:
        return results

    now_iso = _dt.datetime.now().isoformat(timespec="seconds")
    for n in nodes:
        if n.get("type") == "watch":
            continue
        if not node_expired(n):
            continue
        # atomic_archive_expired re-checks node_expired on the fresh extra
        # inside BEGIN IMMEDIATE: an expiry extended (kin edit --expires)
        # between the nodes_with_expiry snapshot and this write is honored
        # — the node stays active and the new expires survives.
        if not store.atomic_archive_expired(n["id"], now_iso):
            continue
        results["archived"] += 1
        if verbose:
            extra = n.get("extra") or {}
            print(f"  Expired node: {n.get('title', n['id'])} "
                  f"(was due {extra.get('expires')})")

    return results


def _check_reminders(config: "Config", store: "Store", verbose: bool = False) -> dict:
    """Run the reminder check cycle."""
    if not config.reminders.enabled:
        return {"fired": 0, "auto_snoozed": 0}

    from .reminders import auto_snooze_stale, check_and_fire

    fired = check_and_fire(store, config)
    auto_snoozed = auto_snooze_stale(store, config)

    if verbose and fired:
        for r in fired:
            print(f"  Fired reminder: {r['title']}")

    return {"fired": len(fired), "auto_snoozed": auto_snoozed}


def last_run_marker(config: "Config") -> str:
    """Read last cron run timestamp from meta table.

    Returns ISO timestamp string, or empty string if never run.
    """
    from .store import Store

    store = Store(config)
    ts = store.get_meta("last_cron_run")
    store.close()
    return ts or ""


def set_run_marker(store: "Store") -> None:
    """Set last cron run timestamp in meta table."""
    now = datetime.datetime.now(tz=None).isoformat(timespec="seconds")
    store.set_meta("last_cron_run", now)


def find_new_sessions(config: "Config", since_iso: str) -> list[Path]:
    """Find JSONL session files modified since the given timestamp.

    Args:
        config: Kindex configuration.
        since_iso: ISO timestamp string. Files modified after this time
                   are returned.

    Returns:
        List of Path objects for new/modified JSONL files.
    """
    projects_dir = config.claude_path / "projects"
    if not projects_dir.exists():
        return []

    try:
        since_dt = datetime.datetime.fromisoformat(since_iso)
    except (ValueError, TypeError):
        # If invalid timestamp, return all files
        since_dt = datetime.datetime.min

    results = []
    for jsonl_path in projects_dir.rglob("*.jsonl"):
        try:
            mtime = datetime.datetime.fromtimestamp(jsonl_path.stat().st_mtime)
            if mtime > since_dt:
                results.append(jsonl_path)
        except OSError:
            continue

    return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)


def incremental_ingest(
    config: "Config", store: "Store", since_iso: str, verbose: bool = False
) -> int:
    """Only ingest sessions newer than since_iso. Returns count.

    This is a lightweight alternative to full scan_sessions that only
    looks at files modified since the given timestamp.
    """
    import json

    new_files = find_new_sessions(config, since_iso)
    if not new_files:
        return 0

    from .extract import keyword_extract

    # Per-profile session routing — same predicate as scan_sessions, so
    # `kin watch` cannot pull foreign-profile sessions into this store.
    session_filter = effective_session_filter(config)

    count = 0
    for jsonl_path in new_files:
        if session_filter is not None and not session_filter(jsonl_path):
            continue
        session_id = jsonl_path.stem[:12]
        session_slug = f"session-{session_id}"

        # Skip already-ingested sessions
        if store.get_node(session_slug):
            continue

        # Extract text from the session
        text = _extract_session_text_quick(jsonl_path)
        if not text or len(text) < 50:
            continue

        project_context = jsonl_path.parent.name

        # Extract knowledge
        extraction = keyword_extract(text)
        concepts = extraction.get("concepts", [])
        if not concepts:
            continue

        # Create session node
        summary = text[:500]
        store.add_node(
            node_id=session_slug,
            title=f"Session: {project_context[:40]}",
            content=summary,
            node_type="session",
            prov_source=str(jsonl_path),
            prov_activity="incremental-ingest",
            extra={"project": project_context},
        )
        count += 1

        if verbose:
            print(f"  Ingested: {session_slug} ({project_context})")

        # Link extracted concepts
        for concept in concepts[:5]:
            existing = store.get_node_by_title(concept["title"])
            if existing:
                store.add_edge(
                    session_slug,
                    existing["id"],
                    edge_type="context_of",
                    weight=0.3,
                    provenance="mentioned in session",
                )

    return count


def _extract_session_text_quick(jsonl_path: Path, max_chars: int = 4000) -> str:
    """Quick text extraction from a JSONL session file."""
    import json

    texts = []
    total_len = 0

    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                if total_len >= max_chars:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                role = entry.get("role", "")
                if role != "assistant":
                    continue

                content = entry.get("content", "")
                if isinstance(content, str):
                    chunk = redact_text(content)[:800]
                    texts.append(chunk)
                    total_len += len(chunk)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            chunk = redact_text(block.get("text", ""))[:800]
                            texts.append(chunk)
                            total_len += len(chunk)
    except OSError:
        return ""

    return "\n".join(texts)
