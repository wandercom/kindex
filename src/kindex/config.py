"""Configuration loading — finds and merges config from multiple sources."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml
from pydantic import BaseModel, Field, PrivateAttr


# ── Config-resolution test seam (V1, AMENDMENT 1 contract) ──────────────
# A process-local binding that overrides HOME and all config/data paths.
# When active, every config resolution resolves beneath the bound root;
# module-level caches (MCP singletons) are invalidated on bind and release
# so post-import callers see the binding immediately (R1.1, R1.2, R1.4).
# See architecture.md AMENDMENT 1 for the full contract.

_bound_root: Path | None = None
_cache_invalidate_callbacks: list[Callable[[], None]] = []


def _register_cache_invalidate(fn: Callable[[], None]) -> None:
    """Register a callback fired on bind/unbind to clear module-level caches."""
    _cache_invalidate_callbacks.append(fn)


def _fire_cache_invalidate() -> None:
    for fn in list(_cache_invalidate_callbacks):
        try:
            fn()
        except Exception:
            pass


def bind_root(root: str | os.PathLike) -> None:
    """Bind config resolution to an explicit root directory (R1.1–R1.5).

    While a binding is active, every config resolution — data dir, config
    file, and any derived path — resolves beneath ``root``. ``HOME``, the
    environment, and the user config file are not consulted for path
    resolution while bound. Module-level caches (including the MCP store
    singleton) are invalidated immediately, so callers that imported the
    module before binding see the new root on their next resolution.

    Raises ``RuntimeError`` if a binding is already active — nested binding
    is a test bug, not a feature. Use ``bound_root`` for scoped binding that
    restores the prior state on exit.
    """
    global _bound_root
    if _bound_root is not None:
        raise RuntimeError(
            f"bind_root called while already bound to {_bound_root}; "
            f"unbind_root first or use bound_root()"
        )
    _bound_root = Path(root).resolve()
    _fire_cache_invalidate()


def unbind_root() -> None:
    """Release the active config binding, restoring normal precedence (R1.4).

    A no-op when no binding is active — never an error.
    """
    global _bound_root
    if _bound_root is None:
        return
    _bound_root = None
    _fire_cache_invalidate()


def active_root() -> Path | None:
    """Return the active binding root, or ``None`` when unbound (R1.5)."""
    return _bound_root


@contextlib.contextmanager
def bound_root(root: str | os.PathLike) -> Iterator[Path]:
    """Context manager that binds ``root`` on entry and releases on exit (R1.4).

    Restores any *previously active* binding on exit (not just clearing to
    None), so nested ``bound_root`` blocks work correctly. Releases even
    if the body raises.
    """
    global _bound_root
    prior = _bound_root
    if prior is not None:
        # Clear the prior binding so bind_root doesn't raise; we restore it
        # on exit.
        _bound_root = None
        _fire_cache_invalidate()
    bound = Path(root).resolve()
    _bound_root = bound
    _fire_cache_invalidate()
    try:
        yield bound
    finally:
        _bound_root = prior
        _fire_cache_invalidate()


# Config layers, loaded bottom-up and merged (like git config).
# Global (user-level) is loaded first, then local (project-level) overrides.
# These are the UNBOUND defaults; when a binding is active, _bound_global_paths
# and _bound_local_paths resolve them under the bound root instead.
_GLOBAL_PATHS = [
    Path.home() / ".config" / "kindex" / "kin.yaml",  # XDG-ish
    Path.home() / ".config" / "conv" / "conv.yaml",   # legacy
]
_LOCAL_PATHS = [
    Path(".kin") / "config",                           # cwd (repo-local, .kin/ directory)
    Path("kin.yaml"),                                  # cwd (explicit)
    Path("conv.yaml"),                                 # legacy
]
# Flat list for backward compat (used by config set to find first existing)
_SEARCH_PATHS = _LOCAL_PATHS + _GLOBAL_PATHS


def _effective_global_paths() -> list[Path]:
    """Global config paths, under the bound root when bound."""
    if _bound_root is not None:
        return [
            _bound_root / ".config" / "kindex" / "kin.yaml",
            _bound_root / ".config" / "conv" / "conv.yaml",
        ]
    return list(_GLOBAL_PATHS)


def _effective_local_paths() -> list[Path]:
    """Local config paths, under the bound root when bound."""
    if _bound_root is not None:
        return [
            _bound_root / ".kin" / "config",
            _bound_root / "kin.yaml",
            _bound_root / "conv.yaml",
        ]
    return list(_LOCAL_PATHS)


def _effective_search_paths() -> list[Path]:
    """Flat search list (backward compat), bound-aware."""
    return _effective_local_paths() + _effective_global_paths()


def _effective_home() -> Path:
    """HOME for resolution, under the bound root when bound (R1.5)."""
    if _bound_root is not None:
        return _bound_root
    return Path.home()


def _resolve_path(path: str | Path) -> Path:
    """Expand ~ and resolve, using the bound root as HOME when bound (R1.5).

    When a binding is active, ALL paths resolve beneath the bound root:
    - ``~`` expands to the bound root instead of the real HOME
    - absolute paths already under the root are returned as-is
    - absolute paths OUTSIDE the root are anchored under the root
      (e.g. ``/tmp/foo`` becomes ``<root>/tmp/foo``)
    - relative traversals (``../escaped``) are collapsed so they cannot
      escape above the root
    This ensures no config resolution can escape the binding (R1.5).
    When unbound, behavior is byte-identical to ``Path(path).expanduser().resolve()``.
    """
    s = str(path)
    if s.startswith("~"):
        resolved = (_effective_home() / s[1:].lstrip("/")).resolve()
    else:
        resolved = Path(s).expanduser().resolve()
    if _bound_root is not None:
        root = _bound_root
        try:
            resolved.relative_to(root)
        except ValueError:
            # Path is outside the bound root. Anchor it under the root,
            # stripping parent traversals and leading separators so the
            # result cannot escape above the root (R1.5). Use the
            # expanded path (with ~ resolved) for the parts, not the
            # raw string, so ~ doesn't become a literal component.
            anchored_source = str(resolved) if s.startswith("~") else s
            parts = []
            for part in anchored_source.replace("\\", "/").split("/"):
                if part in ("", ".", ".."):
                    continue
                parts.append(part)
            anchored = root.joinpath(*parts) if parts else root
            resolved = anchored.resolve()
            # Final containment invariant: if the resolved path is
            # somehow still outside the root, clamp to the root itself.
            try:
                resolved.relative_to(root)
            except ValueError:
                resolved = root
    return resolved


def _contained_resolve(path: str | Path) -> Path | None:
    """Resolve a path and check containment AFTER resolution (R1.5).

    Symlinks defeat pre-resolution containment because .resolve() follows
    them outside the root. This helper resolves the path, then checks:
    if a binding is active and the resolved path is outside the root,
    return None (unusable candidate). When unbound, return the resolved
    path unchanged.
    """
    resolved = Path(str(path)).expanduser().resolve()
    if _bound_root is not None:
        try:
            resolved.relative_to(_bound_root)
        except ValueError:
            return None  # Symlink escaped the root — unusable
    return resolved


class ProfileEntry(BaseModel):
    """A named graph profile: its own data_dir plus the directory roots
    whose sessions/work route to it."""
    data_dir: str
    roots: list[str] = Field(default_factory=list)


class CollabConfig(BaseModel):
    """Multi-agent collaboration surfaces (conversations, locks, injection)."""
    enabled: bool = True
    display: str = "full"            # full | minimal | quiet
    prompt_cooldown_minutes: int = 10


class AgentOverrideConfig(BaseModel):
    """Behavior overrides scoped to one client family or one client instance."""
    attention: dict[str, Any] = Field(default_factory=dict)
    sim: dict[str, Any] = Field(default_factory=dict)
    collab: dict[str, Any] = Field(default_factory=dict)
    hooks: dict[str, Any] = Field(default_factory=dict)


class AgentInstanceConfig(AgentOverrideConfig):
    """Instance-scoped overrides, optionally tied to a specific client."""
    client: str = ""


class AgentsConfig(BaseModel):
    """Client/instance-specific Kindex behavior overlays.

    Root config remains the global/project default. These overlays only tune
    agent-facing behavior such as injection cadence, display, and hook budgets.
    """
    clients: dict[str, AgentOverrideConfig] = Field(default_factory=dict)
    instances: dict[str, AgentInstanceConfig] = Field(default_factory=dict)


class EmbeddingConfig(BaseModel):
    provider: str = "voyage"     # "voyage", "openai", "gemini", "local"
    model: str = ""              # empty = provider default
    api_key_env: str = ""        # empty = provider default env var
    dimensions: int = 0          # 0 = provider default
    strategy: str = ""           # empty/auto, single, or contextual
    chunk_chars: int = 6000      # local chunk target for contextual strategies
    chunk_overlap_chars: int = 600
    max_group_chunks: int = 20   # chunks sent together for contextual embedding
    reindex_max_jobs: int = 200  # cron drain cap for queued embedding work
    reindex_max_queue: int = 100000
    drain_time_budget: int = 120  # wall-clock seconds cap per cron embedding drain


class GroundingConfig(BaseModel):
    """Policy for the retrieval-confidence gate.

    This holds POLICY ONLY — never the floor itself. The floor is a derived
    fact about an external system (the embedding provider's similarity
    distribution over this corpus), so it lives as an immutable, versioned
    calibration record keyed by ``provider:model``, not as a config value a
    stale edit can quietly falsify. `kin embed calibrate` writes the record;
    retrieval reads the newest one for the active model.

    A borrowed constant is a guess wearing a number's clothes — and so is a
    computed constant with no record of the corpus it was computed against.
    """

    enabled: bool = True
    # Shadow mode is the default ON PURPOSE. Enforcing turns "irrelevant
    # results contaminate context" — annoying, visible, self-correcting — into
    # "empty context, agent proceeds without knowledge that was actually
    # there". Silent false negatives are strictly worse than loud false
    # positives for a tool whose value is that you can trust it. So the verdict
    # is computed and reported first, and only enforced once a real query log
    # shows what it would have dropped.
    enforce: bool = False
    # Percentile of the null-query similarity distribution the floor sits at.
    floor_percentile: float = 95.0
    # Verdict boundary: results clearing the floor but bunched near it are
    # `weak`, not `grounded`. Expressed as a multiplier on the floor.
    weak_margin: float = 1.15
    # Refuse to trust a calibration taken against a materially different
    # corpus. Coverage drifting past this fraction re-opens the question.
    recalibrate_coverage_delta: float = 0.25


class LLMConfig(BaseModel):
    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    api_key_env: str = "ANTHROPIC_API_KEY"
    cache_control: bool = True
    codebook_min_weight: float = 0.5
    tier2_max_tokens: int = 4000


class BudgetConfig(BaseModel):
    daily: float = 0.50
    weekly: float = 2.00
    monthly: float = 5.00


class CaptureConfig(BaseModel):
    """Quarantined automatic-capture retention settings."""

    candidate_ttl_days: int = Field(default=7, gt=0)


class AttentionConfig(BaseModel):
    enabled: bool = False
    tick_interval: int = 3
    max_candidates: int = 6
    min_confidence: float = 0.65
    display: str = "minimal"     # how reminders render: full | minimal | quiet
                                 # full = header+Source+Reason+budget; minimal = bare
                                 # lines; quiet = feed the model, suppress the user block
    max_context_chars: int = 1800
    max_candidate_chars: int = 500
    max_output_tokens: int = 300
    cooldown_seconds: int = 1800
    max_check_cost: float = 0.01
    max_conversation_cost: float = 0.25
    # Tool calls that are Kindex's own noise or pure inspection — attention never
    # fires on these. Names support fnmatch globs (e.g. "mcp__kindex__*"). Edits,
    # writes, web calls and everything else are real actions and DO fire, so that
    # "when you do X, always Y" reminders can trigger on arbitrary work.
    skip_tools: list[str] = Field(default_factory=lambda: [
        "Read", "Grep", "Glob", "LS", "NotebookRead", "TodoWrite",
        "view_file", "list_dir", "find_by_name", "grep_search", "read_url_content",
        "mcp__kindex__*",
    ])
    # For Bash, attention skips ONLY commands that are purely read-only
    # inspection. Anything else (curl, deploys, edits, arbitrary actions) fires —
    # an allowlist would silently drop reminders for commands we didn't predict.
    readonly_bash_commands: list[str] = Field(default_factory=lambda: [
        "ls", "cat", "head", "tail", "less", "more", "bat", "tac",
        "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd",
        "pwd", "echo", "printf", "which", "type", "whoami", "id",
        "env", "printenv", "wc", "sort", "uniq", "cut", "column", "tr",
        "awk", "stat", "file", "tree", "du", "df", "ps", "date", "cal",
        "uname", "hostname", "cd", "true", "false", "test", "diff",
        "jq", "yq", "xxd", "od", "basename", "dirname", "realpath",
    ])
    # `git` and `kin` are read-only only for these subcommands; any other
    # subcommand (push, commit, index, export, …) is an action and fires.
    readonly_git_subcommands: list[str] = Field(default_factory=lambda: [
        "status", "log", "diff", "show", "branch", "rev-parse", "describe",
        "blame", "ls-files", "shortlog", "reflog", "whatchanged", "remote",
        "config", "stash", "tag",
    ])
    readonly_kin_subcommands: list[str] = Field(default_factory=lambda: [
        "search", "show", "status", "list", "context", "ask", "graph-stats",
        "changelog", "list-nodes", "prime", "policy",
        "coord read", "coord list", "profile list", "profile which", "whoami",
    ])
    # Stigmergic injection pheromone (deposited when a node is injected,
    # reinforced when the agent actually used the injection, decayed over time).
    pheromone_enabled: bool = True       # accumulate trails (ranking use is gated by ranking.pheromone_weight)
    pheromone_deposit: float = 1.0       # laid per injection
    pheromone_reinforce: float = 3.0     # confirmed use of an injection (used ≈ 4× bare)
    pheromone_correction: float = 4.0    # HEAVIEST: a user correction grounds the signal (real ground truth)
    pheromone_counterfactual: float = 1.5  # would-have-helped / agent "I should have…" admission — a signal, not too heavy
    pheromone_half_life_days: float = 14.0   # aggressive: ignored trails die in weeks
    pheromone_min_deposits: int = 5      # conditioned trail must clear this before it overrides the global trail
    # Auto-ramp: lift pheromone into ranking automatically once trails are warm,
    # so users never flip a bit. Measured on GRADED, decayed signal (not bare
    # deposits) so it ramps down when the work moves on. Writes a learned weight
    # to meta; ranking.pheromone_weight (if >0) is a manual override that wins.
    # Learned PAIR co-activation (W4). Deposited only when session grading
    # CONFIRMS the injected nodes were used — co-retrieval is not usefulness.
    # Bounded update w <- w + eta*(1-w), so a pair saturates instead of
    # running away. Kept in its own table and its own ensemble channel; it is
    # never folded into edges.weight, which is asserted topology.
    coactivation_enabled: bool = True
    coactivation_eta: float = 0.15
    coactivation_half_life_days: float = 14.0
    coactivation_min_events: int = 3
    coactivation_max_pairs: int = 45   # cap pairs per session: 10 nodes choose 2
    coactivation_autoramp_enabled: bool = True
    coactivation_target_weight: float = 0.08
    coactivation_min_warm_pairs: int = 12
    coactivation_min_signal: float = 6.0
    coactivation_full_signal: float = 30.0
    pheromone_autoramp_enabled: bool = True
    pheromone_target_weight: float = 0.12    # mature target weight in the ensemble
    pheromone_min_nodes: int = 8             # distinct warm graded nodes before any ramp
    pheromone_min_signal: float = 12.0       # warm graded strength before any ramp
    pheromone_full_signal: float = 60.0      # warm graded strength at which weight hits target
    # Session-end reinforcement (LLM-grades-the-trace) budget + behavior.
    reinforce_enabled: bool = True
    reinforce_max_cost: float = 0.05     # cap per session-end grading call
    reinforce_min_confidence: float = 0.55  # grader confidence floor to act on a finding
    reinforce_counterfactual_top_k: int = 3  # graph matches considered per missed-opportunity query
    reinforce_gap_as_question: bool = True   # log a knowledge-gap when a real need matches no node


class AdvocateConfig(BaseModel):
    """Opt-in deep escalation to ~/Code/advocate (multi-persona adversarial review,
    including the Helland architectural seat).

    OFF by default — the light path is a recommendation folded into Sim's note.
    When enabled, a high-stakes escalation runs Advocate on the flagged window,
    then VERIFIES the findings against the window (dropping hallucinations, per the
    2026-06-09 head-to-head experiment's hard requirement for autonomous multi-call
    surfacing) and folds only the survivors into the injected note. Hard-capped and
    cooldown-gated so the heavy path can never run away.
    """
    enabled: bool = False
    # Shell command that runs Advocate: the review brief arrives on stdin and the
    # Advocate JSON is expected on stdout. The real ~/Code/advocate writes JSON to
    # a FILE (-o), not stdout, so the verified wrapper (persona ids checked against
    # advocate v0.1.5 — the Helland seat is `helland`) is:
    #   sh -c 'f=$(mktemp); advocate review --stdin --no-color \
    #            -p helland -p sage -p adversarial -o "$f" >/dev/null 2>&1; \
    #            cat "$f"; rm -f "$f"'
    # The parser reads the nested persona_reports[].findings[] of that JSON.
    command: str = ""
    max_cost: float = 2.0          # hard ceiling for one escalation's verify spend
    cooldown_ticks: int = 30       # minimum ticks between escalations per conversation
    max_findings: int = 5          # cap on surfaced (verified) survivor findings
    timeout: int = 300             # subprocess timeout for the Advocate run


class SimConfig(BaseModel):
    """Optional async Sim (Jeremy-simulacrum) supervisory check-in.

    Sim periodically reviews a conversation WINDOW as a supervisor and, if its
    feedback self-rates at/above `threshold`, the feedback is injected into the
    conversation via the same channel as attention reminders. Opt-in and
    disable-able; the human + threshold is the whole feedback loop (no training).

    Runs OFF the agent's critical path: the prompt-tick only enqueues a window
    snapshot (cheap, SQLite-only); the LLM/Sim spend happens in the daemon drain;
    the next tick picks up any pending injection (cheap) and surfaces it if still
    fresh. Mirrors reinforce.py's queue/drain pattern.

    Graduated effort (the tool calibrates spend to what's actually at stake by
    reading the window): Tier 0 banter is skipped at enqueue by a cheap triage;
    Tier 1 ordinary code/task work gets the grounded single-persona review across
    the direction/alignment/trajectory/architecture lenses; Tier 2 high-impact
    moves (money, a large workflow, an irreversible or architecture-locking
    decision) self-assess `stakes` and may recommend — or, if `advocate.enabled`,
    run — a deeper Advocate/Helland review.
    """
    enabled: bool = False
    tick_interval: int = 6          # enqueue a review roughly every ~6 ticks
    threshold: float = 0.7          # self-rating at/above this injects (0.0-1.0)
    window_chars: int = 12000       # conversation-window size handed to Sim
    grounding_chars: int = 1500     # budget for injected kindex knowledge (concepts +
                                    # constraints/watches) so Sim reviews grounded, not
                                    # blind; 0 disables grounding
    max_review_cost: float = 0.05   # cap per Sim review call
    max_conversation_cost: float = 0.50  # cumulative Sim spend cap per conversation
    max_output_tokens: int = 400
    max_queue: int = 20             # pending reviews retained (dedup by conversation)
    max_stale_ticks: int = 4        # drop a pending injection older than this many ticks
    min_overlap: float = 0.18       # token-overlap floor between reviewed tail and current tail
    deposit_pheromone: bool = True  # lay an injection trail like attention does
    # Supervisor model. Empty = fall back to llm.model (the cheap attention judge,
    # often too weak for a demanding lens). Set a stronger model for real review.
    model: str = ""
    # Self-drain: when no daemon is draining the queue, the prompt tick
    # fire-and-forgets a detached `kin sim drain` so Sim works without cron.
    drain_on_tick: bool = True
    display: str = "minimal"        # how Sim feedback renders: full | minimal | quiet
    # How to invoke Sim. Empty = use the configured LLM client with the
    # supervisor prompt (portable, testable). Set to a shell command that reads
    # the prompt on stdin and writes the response on stdout to wire in the real
    # Jeremy-simulacrum, e.g. "~/.claude/skills/simulacrum/run.py".
    command: str = ""
    command_timeout: int = 60
    # Tier 0 triage: skip enqueuing a review for confident banter/small-talk so the
    # machinery never spends on light back-and-forth. Rounds UP when unsure — only a
    # confidently-trivial window is skipped. Disable to review every gated tick.
    triage_banter: bool = True
    # Deep escalation to ~/Code/advocate (with the Helland seat). Off by default.
    advocate: AdvocateConfig = Field(default_factory=AdvocateConfig)


class RankingConfig(BaseModel):
    rrf_k: int = 30               # RRF smoothing parameter (lower = sharper discrimination)
    fts_weight: float = 0.40      # FTS5 BM25 signal weight
    vector_weight: float = 0.30   # Vector similarity signal weight
    graph_weight: float = 0.15    # Graph expansion signal weight
    node_weight: float = 0.10     # Stored node weight signal
    recency_weight: float = 0.05  # Recency decay signal weight
    pheromone_weight: float = 0.0  # Injection-usefulness signal — opt-in: accumulate trails, then enable once warm
    # Graph expansion. Score attenuates by hop_decay per hop so a 2-hop
    # neighbour cannot outrank a 1-hop one on edge weight alone. graph_beam is
    # mandatory, not tuning: observed max out-fanout is 849, so an uncapped
    # multi-hop walk from a hub explodes.
    hop_decay: float = 0.5
    graph_beam: int = 200
    # Learned pair co-activation — opt-in like pheromone: accumulate first,
    # rank later, and never fold into edge weight.
    coactivation_weight: float = 0.0

    @property
    def ensemble_weights(self) -> dict[str, float]:
        weights = {
            "fts": self.fts_weight,
            "vector": self.vector_weight,
            "graph": self.graph_weight,
            "node_weight": self.node_weight,
            "recency": self.recency_weight,
        }
        if self.pheromone_weight > 0:
            weights["pheromone"] = self.pheromone_weight
        if self.coactivation_weight > 0:
            weights["coactivation"] = self.coactivation_weight
        return weights


class DefaultsConfig(BaseModel):
    hops: int = 2
    min_weight: float = 0.1
    mode: str = "bfs"


class SystemChannelConfig(BaseModel):
    enabled: bool = True
    sound: str = "default"


class SlackChannelConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class EmailChannelConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass_keychain: str = ""
    from_addr: str = ""
    to_addr: str = ""


class ClaudeChannelConfig(BaseModel):
    enabled: bool = True
    headless_model: str = ""          # model for claude -p; empty = Claude default
    max_budget_usd: float = 0.50      # spending cap per headless invocation


class TelegramChannelConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""           # from @BotFather
    chat_id: str = ""             # user or group chat ID
    bot_token_keychain: str = ""  # macOS Keychain service name (alternative to plaintext)


class ChannelsConfig(BaseModel):
    system: SystemChannelConfig = Field(default_factory=SystemChannelConfig)
    slack: SlackChannelConfig = Field(default_factory=SlackChannelConfig)
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)
    claude: ClaudeChannelConfig = Field(default_factory=ClaudeChannelConfig)
    telegram: TelegramChannelConfig = Field(default_factory=TelegramChannelConfig)


class ScheduleTier(BaseModel):
    threshold: int   # seconds until nearest reminder
    interval: int    # check interval to use


_DEFAULT_TIERS = [
    ScheduleTier(threshold=604800, interval=86400),   # > 7 days -> daily
    ScheduleTier(threshold=86400, interval=3600),     # > 1 day -> hourly
    ScheduleTier(threshold=3600, interval=600),       # > 1 hour -> 10 min
    ScheduleTier(threshold=0, interval=300),          # <= 1 hour -> 5 min
]


class ReminderConfig(BaseModel):
    enabled: bool = True
    # Inject the "use kindex" session directive at prime time (SessionStart hook).
    # On by default so kindex owns this reminder itself (rather than relying on an
    # external session-start injector). Set false per-project in .kin/config
    # [reminders] to suppress the nudge for repos that don't want it.
    remind_kindex_usage: bool = True
    check_interval: int = 300
    default_channels: list[str] = Field(default_factory=lambda: ["system"])
    snooze_duration: int = 900
    auto_snooze_timeout: int = 300
    idle_suppress_after: int = 600
    action_enabled: bool = True        # enable action execution on reminder fire
    # Never auto-execute an action overdue by more than this many seconds —
    # notify only. A freshly-due reentry wake (minutes overdue) executes
    # immediately; a backlog of stale pollers discovered by a first-ever
    # scheduler install must not detonate as a swarm of headless agents.
    # 0 disables the guard.
    max_action_overdue: int = 86400
    stop_guard_enabled: bool = False   # block Claude exit for pending actions (noisy; opt-in)
    dream_on_stop_enabled: bool = True  # run throttled knowledge consolidation when Claude exits
    dream_min_interval: int = 3600      # seconds between scheduled/hook dream runs
    dream_max_new_suggestions: int = 100  # cap suggestion writes per dream run
    stop_guard_window: int = 7200      # seconds (2h) — block exit if actions due within
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    adaptive_scheduling: bool = True   # dynamically adjust cron interval
    min_interval: int = 300            # floor for adaptive scheduling (5 min)
    schedule_tiers: list[ScheduleTier] = Field(default_factory=lambda: list(_DEFAULT_TIERS))


class LinearPolicyConfig(BaseModel):
    enabled: bool = False
    require_issue: bool = False
    team: str = ""


class GitPolicyConfig(BaseModel):
    block_commit_without_tag: bool = False
    block_commit_without_linear: bool = False
    block_push_without_tag: bool = False
    block_push_without_linear: bool = False


class WorkPolicyConfig(BaseModel):
    require_active_tag: bool = False
    linear: LinearPolicyConfig = Field(default_factory=LinearPolicyConfig)
    git: GitPolicyConfig = Field(default_factory=GitPolicyConfig)


class CodeIngestConfig(BaseModel):
    """Project-scoped `kin ingest code` options, set in .kin/config.

    unity: opt-in Unity preset — index serialized asset files
    (.unity/.prefab/.asset/...) and attach .meta GUIDs to module nodes.
    include_extensions: generic escape hatch mapping extra extensions to
    language labels, e.g. {".shader": "Unity Shader"}.
    """

    unity: bool = False
    include_extensions: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    _project_path: Path | None = PrivateAttr(default=None)
    # Per-pass session routing predicate (set by daemon.cron_run_all and
    # cli.cmd_cron); callable(jsonl_path) -> bool. Never loaded from yaml.
    # When None, ingest builds one from profiles/active_profile (see
    # routing.effective_session_filter).
    _session_filter: Any = PrivateAttr(default=None)
    # False when an explicit --data-dir overrides a profile-resolved
    # data_dir: the store must NOT stamp an unstamped database with the
    # active profile (it still hard-refuses an existing mismatched stamp).
    _stamp_on_open: bool = PrivateAttr(default=True)
    # The pre-activation data_dir, recorded by _activate_profile so the
    # cron legacy-remainder pass can find the legacy graph even when this
    # invocation resolved to a profile.
    _legacy_data_dir: str | None = PrivateAttr(default=None)

    data_dir: str = "~/.kindex"
    user: str = ""  # current user identity (auto-detected if empty)
    project_dirs: list[str] = Field(default_factory=lambda: ["~/Code", "~/Personal"])
    claude_dir: str = "~/.claude"
    codex_dir: str = "~/.codex"
    gemini_dir: str = "~/.gemini"
    antigravity_dir: str = "~/.gemini/config"
    antigravity_cli_dir: str = "~/.gemini/antigravity-cli"
    opencode_dir: str = "~/.config/opencode"
    cursor_dir: str = "~/.cursor"
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
    sim: SimConfig = Field(default_factory=SimConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    reminders: ReminderConfig = Field(default_factory=ReminderConfig)
    work_policy: WorkPolicyConfig = Field(default_factory=WorkPolicyConfig)
    code_ingest: CodeIngestConfig = Field(default_factory=CodeIngestConfig)
    # Sequestered multi-profile storage. Profiles live in the GLOBAL kin.yaml:
    #   profiles: {work: {data_dir: ~/.kindex-work, roots: [~/Work]}}
    #   default_profile: personal
    # No profiles configured => byte-identical legacy single-graph behavior.
    profiles: dict[str, ProfileEntry] = Field(default_factory=dict)
    default_profile: str | None = None
    # Stable agent identity for collab/locks (KIN_AGENT_ID env overrides).
    agent_id: str | None = None
    # Per-node-type edit class overrides: {node_type: editable|additive|managed}.
    edit_policy: dict[str, str] = Field(default_factory=dict)
    collab: CollabConfig = Field(default_factory=CollabConfig)
    # Runtime profile resolution result (set by load_config, not yaml input).
    active_profile: str | None = None
    profile_source: str = "legacy"   # flag | env | kin | roots | default | legacy

    @property
    def current_user(self) -> str:
        """Resolve current user identity. Config > git > OS."""
        if self.user:
            return self.user
        return _detect_user(self._project_path)

    @property
    def data_path(self) -> Path:
        return _resolve_path(self.data_dir)

    @property
    def scheduler_log_path(self) -> Path:
        """Log dir for machine-level schedulers (cron/launchd).

        The scheduled jobs span ALL profiles (kin cron, kin remind check
        --all-profiles), so their logs always live under the BASE
        (pre-profile-activation) data dir — never whichever profile
        happened to resolve from the cwd when setup-cron ran (issue #15).
        """
        base = self._legacy_data_dir or self.data_dir
        return _resolve_path(base) / "logs"

    @property
    def topics_dir(self) -> Path:
        return self.data_path / "topics"

    @property
    def skills_dir(self) -> Path:
        return self.data_path / "skills"

    @property
    def inbox_dir(self) -> Path:
        return self.data_path / "inbox"

    @property
    def ledger_path(self) -> Path:
        return self.data_path / "budget.yaml"

    @property
    def tmp_dir(self) -> Path:
        return self.data_path / ".tmp"

    @property
    def claude_path(self) -> Path:
        return _resolve_path(self.claude_dir)

    @property
    def codex_path(self) -> Path:
        return _resolve_path(self.codex_dir)

    @property
    def gemini_path(self) -> Path:
        return _resolve_path(self.gemini_dir)

    @property
    def antigravity_path(self) -> Path:
        return _resolve_path(self.antigravity_dir)

    @property
    def antigravity_cli_path(self) -> Path:
        return _resolve_path(self.antigravity_cli_dir)

    @property
    def opencode_path(self) -> Path:
        return _resolve_path(self.opencode_dir)

    @property
    def cursor_path(self) -> Path:
        return _resolve_path(self.cursor_dir)

    @property
    def resolved_project_dirs(self) -> list[Path]:
        return [_resolve_path(d) for d in self.project_dirs]


def _contain_data_dir(cfg: Config) -> Config:
    """Rewrite cfg.data_dir to an absolute path under the bound root when
    a binding is active (R1.1, R1.5). When unbound, data_dir is unchanged
    (R1.3 byte-identical). This ensures that any code reading data_dir
    directly — not just through data_path — stays inside the binding.
    """
    if _bound_root is None:
        return cfg
    resolved = _resolve_path(cfg.data_dir)
    cfg.data_dir = str(resolved)
    return cfg


def _detect_user(project_path: str | Path | None = None) -> str:
    """Auto-detect user identity from repo-local/global git config or OS username.

    Under an active binding, git config reads outside the root (global
    gitconfig, repo-local config above the root), so the git probes are
    skipped and the OS username is used (R1.5: no read outside the binding).
    """
    import subprocess

    # Under a binding, skip git probes — they read outside the root (R1.5).
    if _bound_root is not None:
        return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    commands: list[list[str]] = []
    if project_path:
        repo_path = str(Path(project_path).expanduser().resolve())
        # `git config user.name` follows git's normal precedence: local, then global.
        commands.append(["git", "-C", repo_path, "config", "user.name"])
    commands.append(["git", "config", "--global", "user.name"])

    for command in commands:
        try:
            # Config probes may run inside stdio MCP servers; never let child
            # Git processes inherit the JSON-RPC stdin pipe.
            result = subprocess.run(
                command,
                capture_output=True, text=True, timeout=2,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().lower().replace(" ", "-")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Fall back to OS username
    return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


def _attach_project_path(cfg: Config, project_root: Path) -> Config:
    cfg._project_path = project_root
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(
    config_path: str | Path | None = None,
    project_path: str | Path | None = None,
    profile: str | None = None,
) -> Config:
    """Load config with layered merging: code defaults → global → local.

    Like git config: global (~/.config/kindex/kin.yaml) is loaded first,
    then local (.kin/config / kin.yaml / conv.yaml in the current project)
    merges over it. Project resolution is explicit path, KIN_PROJECT, git
    worktree root, then cwd.
    An explicit config_path bypasses layering and loads only that file.

    Profile resolution (only when profiles are configured OR an explicit
    profile/env is given): explicit `profile` param > KIN_PROFILE env >
    `profile:` key from the .kin chain > longest-prefix cwd match against
    profile roots > default_profile > legacy (active_profile stays None and
    data_dir is untouched).
    """
    project_root = resolve_project_root(project_path)

    if config_path:
        p = _resolve_path(config_path)
        if p.exists():
            data = yaml.safe_load(p.read_text()) or {}
            kin_profile = data.pop("profile", None)
            cfg = _resolve_profile(Config(**data), profile, kin_profile)
            cfg = _contain_data_dir(cfg)
            return _attach_project_path(cfg, project_root)
        return _attach_project_path(
            _contain_data_dir(_resolve_profile(Config(), profile, None)),
            project_root)

    # Layer 1: global config (user-level)
    merged: dict = {}
    for p in _effective_global_paths():
        p = _contained_resolve(p)
        if p is not None and p.is_file():
            data = yaml.safe_load(p.read_text()) or {}
            merged = _deep_merge(merged, data)
            break  # use first global found

    project_layers = _project_config_paths(project_root)

    # Layer 2: local config (project-level) merges over global
    kin_profile = merged.pop("profile", None)
    for p in project_layers:
        if p.is_file():
            data = _load_kin_config_with_inheritance(p)
            if "profile" in data:
                kin_profile = data.pop("profile")
            # A relative data_dir in a project config means "inside this
            # project" — anchor it to the config's root now. Config.data_path
            # resolves against process cwd, which is wrong for any invocation
            # that isn't sitting at the project root: a scheduler/hook run
            # with --project-path would silently open <cwd>/.kindex-data, and
            # a run from a subdirectory would split the graph.
            raw_dd = data.get("data_dir")
            if raw_dd and not Path(str(raw_dd)).expanduser().is_absolute():
                config_root = p.parent.parent if p.parent.name == ".kin" else p.parent
                data["data_dir"] = str(config_root / Path(str(raw_dd)).expanduser())
            merged = _deep_merge(merged, data)
            break  # use first local found

    cfg = Config(**merged) if merged else Config()
    cfg = _resolve_profile(cfg, profile, kin_profile)
    cfg = _contain_data_dir(cfg)
    return _attach_project_path(cfg, project_root)


def _resolve_profile(cfg: Config, explicit: str | None,
                     kin_profile: str | None) -> Config:
    """Resolve the active profile on a freshly loaded Config (in place).

    No profiles configured AND no explicit/env request => legacy single-graph
    passthrough: active_profile stays None, data_dir untouched.
    Any explicit reference to an unknown profile raises ValueError.
    """
    env_profile = os.environ.get("KIN_PROFILE") or None
    if not cfg.profiles and not explicit and not env_profile:
        return cfg  # legacy: byte-identical to pre-profile behavior

    # Explicit tiers: flag > env > .kin chain key
    for name, source in ((explicit, "flag"), (env_profile, "env"),
                         (kin_profile, "kin")):
        if name:
            return _activate_profile(cfg, str(name), source)

    # Roots tier: longest-prefix match of cwd against any profile's roots
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is not None:
        best: tuple[int, str] | None = None
        for name, entry in cfg.profiles.items():
            for root in entry.roots:
                rp = Path(root).expanduser()
                try:
                    rp = rp.resolve()
                except OSError:
                    continue
                if cwd == rp or rp in cwd.parents:
                    plen = len(str(rp))
                    if best is None or plen > best[0]:
                        best = (plen, name)
        if best is not None:
            return _activate_profile(cfg, best[1], "roots")

    if cfg.default_profile:
        return _activate_profile(cfg, cfg.default_profile, "default")

    return cfg  # profiles exist but nothing matched -> legacy passthrough


def _activate_profile(cfg: Config, name: str, source: str) -> Config:
    if name not in cfg.profiles:
        known = ", ".join(sorted(cfg.profiles)) or "(none)"
        raise ValueError(
            f"Unknown kindex profile '{name}' (from {source}); "
            f"known profiles: {known}"
        )
    cfg._legacy_data_dir = cfg.data_dir
    cfg.data_dir = str(Path(cfg.profiles[name].data_dir).expanduser())
    cfg.active_profile = name
    cfg.profile_source = source
    return cfg


# ── Degraded-event ledger ─────────────────────────────────────────────
# Hook-surface failures append one JSON line each. The write path is a
# plain file append in the BASE (pre-profile) data dir — same anchor rule
# as scheduler_log_path (issue #15) — so it still works when SQLite is
# what broke.

_DEGRADED_LEDGER_NAME = "degraded.jsonl"
_DEGRADED_MAX_BYTES = 1024 * 1024
_DEGRADED_KEEP_LINES = 200


def degraded_ledger_path(config: Config | None = None,
                         override_dir: str | None = None) -> Path:
    """Path of degraded.jsonl under the base (pre-profile) data dir.

    override_dir is an explicit --data-dir: like the store itself it wins
    over config resolution so hermetic runs stay hermetic.
    """
    if override_dir:
        base = override_dir
    elif config is not None:
        base = config._legacy_data_dir or config.data_dir
    else:
        base = "~/.kindex"
    return _resolve_path(base) / _DEGRADED_LEDGER_NAME


def record_degraded(cmd: str, error: BaseException,
                    config: Config | None = None,
                    override_dir: str | None = None) -> None:
    """Append one degraded event. Never raises — the ledger must not
    become a second failure mode (R4.2).

    The append is always a plain O_APPEND write — no lock, never blocks,
    never loses an event. The size cap is the only operation that needs
    a lock; if the cap cannot acquire it non-blocking, the cap is skipped
    (the file grows past 1MB temporarily, which is acceptable). An append
    that lands during a cap rewrite either survives the rewrite or is
    recovered: the cap keeps a handle to the old inode after os.replace
    and re-reads its tail for any lines that landed after the cap's read
    (R4.1: no silent loss).
    """
    import json
    from datetime import datetime

    try:
        path = degraded_ledger_path(config, override_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "cmd": cmd,
            "profile": config.active_profile if config is not None else None,
            "profile_source": (config.profile_source
                               if config is not None else "unknown"),
            "error_class": type(error).__name__,
            "msg": str(error)[:200],
        }
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

        # Append — always plain O_APPEND, no lock, never blocks.
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

        # Cap — try non-blocking lock; decline if unavailable.
        _try_cap_degraded_ledger(path)
    except Exception:
        pass


def _try_cap_degraded_ledger(path: Path) -> None:
    """Try to cap the ledger under a non-blocking lock. If the lock is
    unavailable, decline (the file grows temporarily). When the cap runs,
    it reads the file, trims, and os.replace. An append that lands after
    the read but before the replace goes to the old inode — the cap
    recovers it by re-reading the old inode's tail after the replace
    (R4.1: no silent loss). Never blocks."""
    import fcntl

    try:
        if path.stat().st_size <= _DEGRADED_MAX_BYTES:
            return
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return  # Lock unavailable — decline the cap, never block

        # Keep a handle to the current inode so we can detect appends
        # that land after our read but before os.replace.
        old_fd = os.open(str(path), os.O_RDONLY)
        try:
            data = os.read(old_fd, _DEGRADED_MAX_BYTES * 2)
            read_size = len(data)
            lines = data.splitlines(keepends=True)
            trimmed = b"".join(lines[-_DEGRADED_KEEP_LINES:])
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_bytes(trimmed)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

            # Check if appends landed after our read (old inode grew).
            # The old fd still points to the pre-replace inode.
            import os as _os
            old_size = _os.fstat(old_fd).st_size
            if old_size > read_size:
                # Appends landed after our read — recover them.
                _os.lseek(old_fd, read_size, _os.SEEK_SET)
                tail = _os.read(old_fd, old_size - read_size)
                if tail.strip():
                    new_fd = os.open(str(path), os.O_WRONLY | os.O_APPEND, 0o600)
                    try:
                        os.write(new_fd, tail)
                    finally:
                        os.close(new_fd)
        finally:
            os.close(old_fd)
    except Exception:
        pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(lock_fd)


def read_degraded_events(config: Config | None = None,
                         days: int = 7,
                         override_dir: str | None = None) -> list[dict]:
    """Degraded events from the last `days` days, oldest first.

    Absent file or unreadable content mean zero events, never an error;
    malformed lines are skipped.
    """
    import json
    from datetime import datetime, timedelta

    try:
        path = degraded_ledger_path(config, override_dir)
        if not path.exists():
            return []
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        events = []
        for raw in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict) and str(event.get("ts", "")) >= cutoff:
                events.append(event)
        return events
    except Exception:
        return []


def resolve_agent_id(config: Config) -> str:
    """Stable agent identity for collab/locks/claims.

    Precedence: KIN_AGENT_ID env > config.agent_id > user@shorthost.
    """
    import socket

    env = os.environ.get("KIN_AGENT_ID")
    if env:
        return env
    configured = getattr(config, "agent_id", None)
    if configured:
        return configured
    host = socket.gethostname().split(".")[0]
    return f"{config.current_user}@{host}"


def resolve_project_root(project_path: str | Path | None = None) -> Path:
    """Resolve the project root for config/policy lookup.

    Resolution order:
    1. explicit project_path
    2. KIN_PROJECT
    3. git worktree root for cwd
    4. cwd

    When a config binding is active, the bound root wins over cwd, and
    explicit paths / KIN_PROJECT are contained under the root via
    _resolve_path so no path outside the binding is read (R1.5).
    """
    raw = project_path or os.environ.get("KIN_PROJECT")
    if raw:
        start = _resolve_path(raw)
    elif _bound_root is not None:
        start = _bound_root
    else:
        start = Path.cwd()
    start = start.resolve()
    if start.is_file():
        start = start.parent

    # Under an active binding, skip the git probe entirely — it shells
    # out with the starting directory and walks parent directories on the
    # real filesystem, which is a forbidden read outside the binding (R1.5).
    # The exit clamp below guarantees the returned value is contained.
    if _bound_root is not None:
        git_root = None
    else:
        git_root = _git_root(start)
    result = git_root or start

    # Containment invariant (R1.5): if a binding is active, the project
    # root must never resolve outside the bound root. Clamp at the exit
    # so every input shape — explicit arg, env var, git walk, cwd — is
    # contained by one check, not by per-input special cases.
    if _bound_root is not None:
        try:
            result.relative_to(_bound_root)
        except ValueError:
            result = _bound_root
    return result


def _git_root(start: Path) -> Path | None:
    import subprocess
    try:
        # Config probes may run inside stdio MCP servers; never let child
        # Git processes inherit the JSON-RPC stdin pipe.
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return None


def _project_config_paths(project_root: Path) -> list[Path]:
    # Prefer git/project root, then parent .kin/config walk for non-git trees,
    # then legacy cwd-local files for backward compatibility.
    candidates: list[Path] = [
        project_root / ".kin" / "config",
        project_root / "kin.yaml",
        project_root / "conv.yaml",
    ]

    # When bound, the parent walk must not escape the bound root (R1.5).
    walk_limit = _bound_root
    current = project_root
    for _ in range(10):
        kin_entry = current / ".kin"
        if kin_entry.is_file():
            upgraded = _maybe_upgrade_kin_file(kin_entry)
            if upgraded:
                candidates.append(upgraded)
        elif kin_entry.is_dir():
            candidates.append(kin_entry / "config")
        parent = current.parent
        if parent == current:
            break
        if walk_limit is not None and parent != walk_limit:
            try:
                if walk_limit not in parent.resolve().parents:
                    break
            except OSError:
                break
        current = parent

    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        resolved = _contained_resolve(path)
        if resolved is None:
            continue  # Symlink escaped the root — unusable
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _load_kin_config_with_inheritance(path: Path) -> dict:
    """Load a .kin config, resolving inherits and merging ancestors."""
    if path.name != "config" or path.parent.name != ".kin":
        return yaml.safe_load(path.read_text()) or {}

    chain = _resolve_kin_chain(path)
    return _merge_kin_chain(chain)


def _resolve_kin_chain(path: Path, remaining: int = 5, seen: set[str] | None = None) -> list[dict]:
    seen = seen or set()
    resolved = resolve_kin_config(path)
    key = str(resolved)
    if remaining <= 0 or key in seen or not resolved.is_file():
        return []
    seen.add(key)

    data = yaml.safe_load(resolved.read_text()) or {}
    data["_source"] = key
    chain = [data]

    for parent_ref in data.get("inherits", []):
        parent = _resolve_path(resolved.parent / parent_ref)
        chain.extend(_resolve_kin_chain(parent, remaining - 1, seen))
    return chain


def _merge_kin_chain(chain: list[dict]) -> dict:
    merged: dict = {}
    for layer in reversed(chain):
        clean = {k: v for k, v in layer.items() if not k.startswith("_") and k != "inherits"}
        merged = _deep_merge_with_lists(merged, clean)
    return merged


def _deep_merge_with_lists(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge_with_lists(merged[key], val)
        elif key in merged and isinstance(merged[key], list) and isinstance(val, list):
            seen: set[str] = set()
            items = []
            for item in merged[key] + val:
                marker = str(item)
                if marker not in seen:
                    seen.add(marker)
                    items.append(item)
            merged[key] = items
        else:
            merged[key] = val
    return merged


def _maybe_upgrade_kin_file(path: Path) -> Path | None:
    """If path is a plain file named .kin, migrate it to .kin/config.

    Returns the new config path, or None if no upgrade was needed.
    Under an active binding, refuse to upgrade — it mutates the filesystem
    and the path may be a symlink or traversal outside the root (R1.5).
    """
    if not path.is_file() or path.name != ".kin":
        return None
    if _bound_root is not None:
        try:
            path.resolve().relative_to(_bound_root)
        except ValueError:
            return None  # Outside the binding — refuse
    try:
        content = path.read_bytes()
        path.unlink()
        kin_dir = path.parent / ".kin"
        kin_dir.mkdir(exist_ok=True)
        config_path = kin_dir / "config"
        config_path.write_bytes(content)
        return config_path
    except OSError:
        return None


def resolve_kin_config(path: Path) -> Path:
    """Resolve a .kin reference to the actual config file.

    Handles both old-style (.kin file) and new-style (.kin/config).
    Auto-upgrades old files on discovery.
    """
    resolved = _contained_resolve(path)
    if resolved is None:
        # Symlink escaped the root — return a path that won't match any
        # file or dir so callers skip it (R1.5).
        return Path("/dev/null/__binding_escaped__")
    path = resolved
    if path.is_file():
        if path.name == ".kin":
            upgraded = _maybe_upgrade_kin_file(path)
            return upgraded if upgraded else path
        return path
    if path.is_dir() and path.name == ".kin":
        return path / "config"
    return path
