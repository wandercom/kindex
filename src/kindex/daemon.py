"""Background ingestion — one-shot cron mode and file change detection."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import platform_io
from .privacy import safe_error
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


def _step_failed(results: dict, step: str, error: BaseException) -> None:
    """A maintenance step that failed is reported in `errors` and the degraded
    ledger. It used to report 0 with no signal, so a failing drain looked like
    an empty queue."""
    errors = results.setdefault("errors", {})
    message = safe_error(error)
    errors[step] = f"{errors[step]}\n{message}" if step in errors else message
    try:
        from .config import record_degraded
        record_degraded("cron", error)
    except Exception:
        pass


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
    inbox_count = _process_inbox(config, store, verbose=verbose, results=results)
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
    except Exception as error:
        _step_failed(results, "reinforce", error)
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
    except Exception as error:
        _step_failed(results, "sim", error)
        results["sim_reviewed"] = 0

    # Drain queued attention reviews. Hook-time attention only waits for a fast
    # result; slower LLM arbitration lands here for later prompt pickup.
    try:
        from .attention import drain_attention_queue
        attention_drained = drain_attention_queue(store, config)
        results["attention_reviewed"] = attention_drained.get("reviewed", 0)
        results["attention_flagged"] = attention_drained.get("flagged", 0)
        results["attention_pending"] = attention_drained.get("pending", 0)
    except Exception as error:
        _step_failed(results, "attention", error)
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
        status = embedding_status(store, coverage=False)
        if (contextual_embeddings_supported(config)
                and int(status.get("queue_pending") or 0) == 0):
            limit = max(1, int(getattr(config.embedding, "reindex_max_jobs", 200) or 200))
            queued = enqueue_reindex(store, stale=True, status="active", limit=limit)
            results["embed_enqueued_stale"] = queued.get("enqueued", 0)
        embed_drained = drain_embedding_queue(store, config, report_coverage=False)
        results["embedded"] = embed_drained.get("embedded", 0)
        results["embed_pending"] = embed_drained.get("pending", 0)
    except Exception as error:
        _step_failed(results, "embedding", error)
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
    try:
        results["suggestions_pruned"] = store.prune_suggestions()
    except Exception as error:
        _step_failed(results, "suggestions", error)
        results["suggestions_pruned"] = 0
    try:
        results["activity_pruned"] = store.prune_activity()
    except Exception as error:
        _step_failed(results, "activity", error)
        results["activity_pruned"] = 0
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
    except Exception as error:
        # Not silence: the cycle as a whole failed (the archive file, not one
        # node; archive_nodes reports those itself).
        results["slow_graph_archived"] = 0
        results["slow_graph_error"] = safe_error(error)

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
    except Exception as error:
        # Best-effort hygiene — a malformed collab row must not break cron
        _step_failed(results, "collab_hygiene", error)
        results.setdefault("locks_cleared", 0)
        results.setdefault("conversations_expired", 0)
        results.setdefault("claims_released", 0)

    # 9c. Quarantined capture retention — expiry only, never promotion.
    try:
        if verbose:
            print("Pruning expired capture candidates...")
        results["capture_candidates_pruned"] = store.prune_capture_candidates()
    except Exception as error:
        _step_failed(results, "capture_candidates", error)
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
    except Exception as error:
        _step_failed(results, "dream", error)
        results["dream_merged"] = 0
        results["dream_suggestions_applied"] = 0

    # Update run marker
    set_run_marker(store)

    # Adaptive scheduling: repack cron interval based on nearest reminder
    try:
        from .scheduling import repack_schedule
        repack = repack_schedule(store, config)
        results["repack"] = repack
    except Exception as error:
        # A scheduling error must not break cron; it is still reported.
        _step_failed(results, "repack", error)

    return results


class _ProjectGraphRefused(ValueError):
    """A fixed diagnostic generated from the registered-store trust check."""

    def __init__(self, *, tracked: bool):
        super().__init__(
            "Refusing tracked Kindex storage: registered project graph is tracked; no maintenance performed"
            if tracked else
            "Refusing Kindex storage: cannot confirm registered project graph is untracked; no maintenance performed")


def _pass_failed(results: dict, step: str, error: BaseException) -> None:
    """Report the failing operation, never arbitrary store/provider content."""
    if isinstance(error, _ProjectGraphRefused):
        _step_failed(results, step, error)
    else:
        _step_failed(results, step, RuntimeError(f"{step} failed ({type(error).__name__})"))


def _collect_project_registry(store: "Store", registry: dict[str, str]) -> None:
    import json

    raw = json.loads(store.get_meta("project_graph_dirs") or "{}")
    if isinstance(raw, dict):
        registry.update({root: directory for root, directory in raw.items()
                         if isinstance(root, str) and isinstance(directory, str)})


def _existing_project_graph(root: str, directory: str) -> bool:
    return (Path(root).is_dir() and Path(directory).is_dir()
            and any((Path(directory) / name).is_file() for name in ("kindex.db", "conv.db")))


def _project_config(base_config: "Config", root: str, directory: str) -> "Config":
    from .config import (_PROJECT_LAYER_UNTRUSTED_KEYS, activate_secret_mask, load_config)
    from .project_store import tracked_store_refusal

    refusal = tracked_store_refusal(Path(directory))
    if refusal:
        # Do not forward git stderr, arbitrary directory names or config content.
        raise _ProjectGraphRefused(tracked=refusal.startswith("Refusing tracked Kindex storage"))
    try:
        cfg = load_config(project_path=root, data_dir=directory)
    finally:
        # Resolving another graph must not replace this run's trusted mask.
        activate_secret_mask(base_config.secret_mask)
    # The base invocation owns execution, privacy and spend authority. Project
    # layers still resolve inheritance, local policy and relative paths normally.
    trusted = base_config.model_copy(deep=True)
    for field in _PROJECT_LAYER_UNTRUSTED_KEYS:
        if field in type(cfg).model_fields:
            setattr(cfg, field, getattr(trusted, field))
    reminder_nudge = cfg.reminders.remind_kindex_usage
    cfg.reminders = trusted.reminders
    cfg.reminders.remind_kindex_usage = reminder_nudge
    cfg.data_dir = str(platform_io.expanduser(Path(directory)).resolve())
    cfg._project_path = Path(root).resolve()
    cfg.active_profile = None
    cfg.profile_source = "project"
    cfg._stamp_on_open = False
    cfg._legacy_data_dir = base_config._legacy_data_dir or base_config.data_dir
    return cfg


def remind_check_all(base_config: "Config", verbose: bool = False) -> list[dict]:
    """Local reminders-first sweep, once per canonical profile/project graph."""
    from .store import Store

    swept_dirs: set[str] = set()
    project_registry: dict[str, str] = {}
    results: list[dict] = []

    def _one(cfg: "Config", name: str | None) -> None:
        directory = str(cfg.data_path.resolve())
        if directory in swept_dirs:
            return
        swept_dirs.add(directory)
        result = {"profile": name, "fired": 0, "auto_snoozed": 0,
                  "data_dir": directory, "source": cfg.profile_source}
        results.append(result)
        try:
            store = Store(cfg)
        except Exception as error:
            _pass_failed(result, "store_open", error)
        else:
            try:
                try:
                    result.update(_check_reminders(cfg, store, verbose=verbose))
                except Exception as error:
                    _pass_failed(result, "reminders", error)
                try:
                    _collect_project_registry(store, project_registry)
                except Exception as error:
                    _pass_failed(result, "project_registry", error)
                if cfg.profile_source == "project":
                    try:
                        housekeeping = _project_housekeeping(store)
                        if housekeeping.get("errors"):
                            result.setdefault("errors", {}).update(housekeeping["errors"])
                    except Exception as error:
                        _pass_failed(result, "housekeeping", error)
            finally:
                try:
                    store.close()
                except Exception as error:
                    _pass_failed(result, "store_close", error)
        if result.get("errors"):
            result["error"] = next(iter(result["errors"].values()))

    profiles = dict(getattr(base_config, "profiles", {}) or {})
    if not profiles:
        _one(base_config, getattr(base_config, "active_profile", None))
    else:
        for name, entry in profiles.items():
            cfg = base_config.model_copy(deep=True)
            cfg.data_dir = str(platform_io.expanduser(Path(entry.data_dir)))
            cfg._legacy_data_dir = base_config._legacy_data_dir or base_config.data_dir
            cfg.active_profile = name
            cfg.profile_source = "cron"
            _one(cfg, name)
        if base_config.default_profile is None:
            cfg = base_config.model_copy(deep=True)
            cfg.data_dir = base_config._legacy_data_dir or base_config.data_dir
            cfg.active_profile = None
            cfg.profile_source = "legacy"
            _one(cfg, None)

    from .project_store import registered_project_graphs
    for root, directory in registered_project_graphs().items():
        project_registry.setdefault(root, directory)
    for root, directory in sorted(project_registry.items()):
        try:
            if not _existing_project_graph(root, directory):
                continue
            if str(Path(directory).resolve()) in swept_dirs:
                continue
            cfg = _project_config(base_config, root, directory)
        except Exception as error:
            result = {"profile": root, "source": "project", "data_dir": directory,
                      "fired": 0, "auto_snoozed": 0}
            _pass_failed(result, "project_config", error)
            result["error"] = next(iter(result["errors"].values()))
            results.append(result)
            continue
        _one(cfg, root)
    return results


def _project_housekeeping(store: "Store") -> dict:
    """Existing local project hygiene, with isolated, observable failures."""
    from .locks import cleanup_expired_locks
    from .tasks import cleanup_expired_claims

    results: dict = {}
    for name, step in (
            ("capture_candidates_pruned", lambda: store.prune_capture_candidates()),
            ("nodes_expired", lambda: _expire_nodes(store).get("archived", 0)),
            ("claims_released", lambda: cleanup_expired_claims(store)),
            ("locks_cleared", lambda: cleanup_expired_locks(store))):
        try:
            results[name] = step()
        except Exception as error:
            _pass_failed(results, name, error)
    return results


def _project_maintenance(config: "Config", store: "Store", verbose: bool = False) -> dict:
    """Service existing queues only; never scan or ingest another workspace.

    The elapsed budget is soft: one already-started provider call can overrun
    it by its existing timeout/retry allowance. Subsequent drains are deferred.
    """
    import time

    from .attention import drain_attention_queue
    from .reinforce import drain_reinforce_queue
    from .vectors import drain_embedding_queue

    results = _project_housekeeping(store)
    deadline = time.monotonic() + max(0, config.embedding.drain_time_budget)
    for name, drain, fields in (
            ("reinforce", drain_reinforce_queue, {"graded": "reinforce_graded", "pending": "reinforce_pending"}),
            ("attention", drain_attention_queue, {"reviewed": "attention_reviewed", "flagged": "attention_flagged", "pending": "attention_pending"}),
            ("embedding", drain_embedding_queue, {"embedded": "embedded", "pending": "embed_pending"})):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results[name + "_deferred"] = "time_budget"
            continue
        try:
            kwargs = ({"max_jobs": max(0, config.embedding.reindex_max_jobs),
                       "time_budget": remaining, "report_coverage": False}
                      if name == "embedding" else {"max_jobs": 1})
            drained = drain(store, config, **kwargs)
            for source, target in fields.items():
                results[target] = drained.get(source, 0)
            status = drained.get("status")
            if status is not None:
                known = {"ok", "empty", "disabled", "unavailable", "failed", "error", "persistence_error"}
                status = status if isinstance(status, str) and status in known else "unknown"
                results[name + "_status"] = status
            if status in {"unavailable", "failed", "error", "persistence_error"}:
                # Failure statuses are evidence even when a drain returns counters
                # instead of raising (in particular, failed queue persistence).
                _step_failed(results, name, RuntimeError(f"{name} queue drain failed ({status})"))
        except Exception as error:
            _pass_failed(results, name, error)
    try:
        set_run_marker(store)
    except Exception as error:
        _pass_failed(results, "run_marker", error)
    return results


def _cron_lock_path(base_config: "Config"):
    """Beside the scheduler state, keyed by the base graph; creating it must
    not create a data directory no pass would otherwise touch."""
    import hashlib

    from .project_store import project_graph_registry_path

    key = hashlib.sha256(str(base_config.data_path).encode()).hexdigest()[:16]
    return project_graph_registry_path().parent / f"cron-{key}.lock"


def _try_cron_lock(base_config: "Config"):
    """A non-blocking whole-run lock, None when the lock file cannot be
    opened, or False when another run holds it. Overlapping crontab runs raced on
    session ingest and every unlocked queue."""
    import os

    try:
        path = _cron_lock_path(base_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        platform_io.lock(fd, blocking=False)
    except OSError:
        os.close(fd)
        return False
    return fd


def cron_run_all(base_config: "Config", verbose: bool = False) -> list[dict]:
    """Run the cron maintenance cycle unless another run is already going."""
    import os

    lock = _try_cron_lock(base_config)
    if lock is False:
        return [{"profile": None, "source": "cron",
                 "results": {"skipped": "cron_already_running"}}]
    try:
        return _cron_run_all(base_config, verbose=verbose)
    finally:
        if lock is not None:
            os.close(lock)


def _cron_run_all(base_config: "Config", verbose: bool = False) -> list[dict]:
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
    sweep_entries = remind_check_all(base_config, verbose=verbose)
    sweep = {entry["profile"]: entry for entry in sweep_entries}
    known_names = set(profiles) | {None, base_config.active_profile}
    project_fired = sum(entry.get("fired", 0) for entry in sweep_entries
                        if entry["profile"] not in known_names)
    project_registry = {entry["profile"]: entry["data_dir"] for entry in sweep_entries
                        if entry.get("source") == "project" and entry.get("data_dir")}
    serviced_dirs: set[str] = set()
    passes: list[dict] = []

    def _one(cfg: "Config", name: str | None, source: str) -> None:
        directory = str(cfg.data_path.resolve())
        if directory in serviced_dirs:
            return
        serviced_dirs.add(directory)
        results: dict = {}
        try:
            store = Store(cfg)
        except Exception as error:
            _pass_failed(results, "store_open", error)
        else:
            try:
                try:
                    _collect_project_registry(store, project_registry)
                except Exception as error:
                    _pass_failed(results, "project_registry", error)
                try:
                    maintenance = (_project_maintenance(cfg, store, verbose=verbose)
                                   if source == "project" else cron_run(cfg, store, verbose=verbose))
                    errors = results.get("errors", {})
                    results.update(maintenance)
                    if errors:
                        results.setdefault("errors", {}).update(errors)
                except Exception as error:
                    _pass_failed(results, "maintenance", error)
            finally:
                try:
                    store.close()
                except Exception as error:
                    _pass_failed(results, "store_close", error)
        swept = sweep.get(name)
        results["reminders_fired"] = results.get("reminders_fired", 0) + (swept or {}).get("fired", 0)
        results["reminders_auto_snoozed"] = results.get("reminders_auto_snoozed", 0) + (swept or {}).get("auto_snoozed", 0)
        if swept and swept.get("errors"):
            results.setdefault("errors", {}).update({"sweep_" + key: value
                                                   for key, value in swept["errors"].items()})
        passes.append({"profile": name, "source": source, "results": results})

    if not profiles:
        _one(base_config, base_config.active_profile, base_config.profile_source)
    else:
        for name, entry in profiles.items():
            cfg = base_config.model_copy(deep=True)
            cfg.data_dir = str(platform_io.expanduser(Path(entry.data_dir)))
            cfg._legacy_data_dir = base_config._legacy_data_dir or base_config.data_dir
            cfg.active_profile = name
            cfg.profile_source = "cron"
            cfg._session_filter = profile_session_filter(profiles, name, base_config.default_profile)
            _one(cfg, name, "cron")
        if base_config.default_profile is None:
            cfg = base_config.model_copy(deep=True)
            cfg.data_dir = base_config._legacy_data_dir or base_config.data_dir
            cfg.active_profile = None
            cfg.profile_source = "legacy"
            cfg._session_filter = profile_session_filter(profiles, None, None)
            _one(cfg, None, "legacy-remainder")

    from .project_store import registered_project_graphs
    for root, directory in registered_project_graphs().items():
        project_registry.setdefault(root, directory)
    for root, directory in sorted(project_registry.items()):
        try:
            if not _existing_project_graph(root, directory):
                continue
            if str(Path(directory).resolve()) in serviced_dirs:
                continue
            cfg = _project_config(base_config, root, directory)
        except Exception as error:
            results = {}
            _pass_failed(results, "project_config", error)
            swept = sweep.get(root) or {}
            results.update(reminders_fired=swept.get("fired", 0),
                           reminders_auto_snoozed=swept.get("auto_snoozed", 0))
            passes.append({"profile": root, "source": "project", "results": results})
            continue
        _one(cfg, root, "project")
    if project_fired and passes:
        passes[0]["results"]["project_graph_reminders_fired"] = project_fired
    return passes


def _process_inbox(config: "Config", store: "Store", verbose: bool = False,
                   results: dict | None = None) -> int:
    """Process pending inbox items (markdown files in inbox/)."""
    inbox = config.inbox_dir
    if not inbox.exists():
        return 0

    count = 0
    if results is None:
        results = {}
    for md_file in sorted(inbox.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            error = ValueError(
                f"Inbox item {md_file} is not UTF-8; file left unprocessed. "
                "Back it up and convert a copy after confirming its encoding."
            )
            _step_failed(results, "inbox", error)
            results["inbox_decode_errors"] = results.get("inbox_decode_errors", 0) + 1
            continue
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
            # Any earlier suggestion of the pair, in any state, answers it:
            # checking only the 100 newest pending rows re-inserted the same
            # pairs every pass once other sources had written 100 more, and
            # re-suggested pairs the user had rejected.
            if not store.suggestion_exists(s["concept_a"], s["concept_b"], status=None):
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

    from .ingest import session_transcripts

    return [
        path for path, mtime in session_transcripts(projects_dir)
        if datetime.datetime.fromtimestamp(mtime) > since_dt
    ]


def incremental_ingest(
    config: "Config", store: "Store", since_iso: str, verbose: bool = False
) -> int:
    """Only ingest sessions newer than since_iso. Returns count.

    This is a lightweight alternative to full scan_sessions that only
    looks at files modified since the given timestamp.
    """
    new_files = find_new_sessions(config, since_iso)
    if not new_files:
        return 0

    from .extract import keyword_extract
    from .ingest import _extract_session_text

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

        # Extract text from the session (the same reader as scan_sessions;
        # a copy of it read only the old top-level role and found nothing in
        # current transcripts).
        text = _extract_session_text(jsonl_path, max_chars=4000)
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
