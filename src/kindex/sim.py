"""Async Sim (Jeremy-simulacrum) supervisory check-in.

Sim glances at a conversation WINDOW as a supervisor and, only if its feedback
self-rates at/above a configured threshold, that feedback is injected into the
conversation. Opt-in, disable-able, no training loop: the human plus the
threshold *is* the feedback loop (too chatty -> raise threshold; too quiet ->
lower it; done -> `kin sim disable`).

Design (mirrors reinforce.py's queue/drain so the LLM cost stays off the agent's
critical path):

  enqueue_sim_review()   cheap, SQLite-only, safe in a prompt hook. Snapshots the
                         current conversation window + a fingerprint of its tail.
  drain_sim_queue()      runs in the daemon/cron. Calls Sim on each snapshot,
                         self-rates, and stashes a PENDING injection when the
                         rating clears the threshold. This is where spend lives.
  pop_pending_sim_injection()  cheap, called on the next tick. Surfaces a pending
                         injection through the existing attention inject channel —
                         but only if it is still FRESH (the window Sim reacted to
                         hasn't scrolled away). Stale feedback is dropped, never shown.

The supervisor PROMPT is load-bearing. Un-framed, Sim defaults to maximum
adversarial demolition and rates everything high, which would make the threshold
meaningless. The prompt pins Sim to a default-silent, high-bar supervisor whose
common answer is "nothing to flag" and who only speaks when something would
MATERIALLY change the direction of the work.

The review weighs four lenses — DIRECTION (is the work itself sound), ALIGNMENT
(does it still serve what the USER actually asked, spirit over letter, a newer
input can supersede an older one but an aside shouldn't redirect the whole plan),
TRAJECTORY (if this continues, does it reach the goal or diverge; what are the
side-effects; should the STRATEGY update), and ARCHITECTURE (judged against Pat
Helland's doctrine — one authority per fact — with a calibration guardrail so it
doesn't over-fire on mere layering).

Effort is GRADUATED and self-calibrated by reading the window (Jeremy's
incident-response rule: match spend to confirmed stakes, round up when unsure):

  Tier 0  banter/small-talk  -> a cheap SQLite-only triage (_looks_trivial) skips
                                it at enqueue; no memory/Sim/Advocate spend at all.
  Tier 1  ordinary code/task -> the grounded single-persona review above, anchored
                                on the session's stated focus (captured at enqueue
                                so intent survives window-scroll).
  Tier 2  high-impact moves  -> the review self-rates `stakes` and sets `escalate`;
    (money, big workflow,       by default the note carries a light RECOMMENDATION
     irreversible/architecture) to run a deeper review. Only when sim.advocate is
                                enabled does it actually invoke ~/Code/advocate (the
                                multi-persona engine incl. the Helland seat), verify
                                the findings against the window (drop hallucinations),
                                and fold the survivors in — gated + cooldown-capped.
"""

from __future__ import annotations

import json
import re
import subprocess
from .privacy import redact_text, safe_error
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .attention import (
    AttentionInjection,
    _load_state,
    _save_state,
    _tokens,
    pheromone_context,
)
from .budget import BudgetLedger
from .config import Config

if TYPE_CHECKING:
    from .store import Store

SIM_PURPOSE = "sim"
SIM_QUEUE_META = "sim.queue"          # pending reviews awaiting a drain (LLM spend)
SIM_PENDING_META = "sim.pending"      # graded injections awaiting a cheap pickup

_TAIL_CHARS = 700  # the recent slice Sim is most likely reacting to (used for staleness)

# ── Tier 0 triage: skip confident banter before it costs anything ────────────
_TRIAGE_TAIL = 1200        # judge triviality on the recent tail, not the whole window
_TRIVIAL_MAX_CHARS = 400   # a short tail with no work signal reads as banter

# Concrete work signals: code, files, and the verbs of building/deciding/spending.
# Presence of any of these in the recent tail means there is real work in flight —
# never triage it away. Deliberately excludes soft words ("plan", "should") that
# show up in banter too, so the skip stays conservative (round UP when unsure).
_WORK_SIGNAL_RE = re.compile(
    r"""(?ix)
      ```                                          # fenced code
    | \b(?:def|class|import|return|async|await|function|const|let|var)\b
    | [\w./~-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|cpp|h|json|ya?ml|toml|md|sh|sql|html|css)\b
    | (?:^|\s)/[\w./-]+                             # absolute paths
    | \b(?:error|exception|traceback|failed|failing|bug|test|tests|deploy|
          migrat|refactor|implement|build|compile|commit|merge|rebase|branch|
          schema|database|query|api|endpoint|module|config|architect|
          design|decision|spec|contract|workflow|budget|cost|spend|money|
          payment|invoice|release|publish|delete|drop|rollback|revert)\w*
    """,
    re.VERBOSE,
)

# Confident small-talk markers: if a longer tail carries NO work signal but reads
# like this, it is banter and can be skipped even mid-session.
_BANTER_RE = re.compile(
    r"(?i)\b(?:hi|hey|hello|yo|thanks|thank you|thx|cheers|lol|haha|"
    r"good (?:morning|afternoon|evening|night)|how are you|nice|cool|awesome|"
    r"sounds good|no worries|you're welcome|np|great job|well done)\b"
)

# Decisive / irreversible verbs. A terse approval of an expensive-to-reverse move
# ("sure, ship it", "yeah just drop the table", "go ahead") carries no work-signal
# token and reads like banter — but it is exactly a Tier-2 moment. If any of these
# appear in the tail, NEVER triage the window away, regardless of length or chatter.
_IRREVERSIBLE_RE = re.compile(
    r"(?i)\b(?:ship it|ship|launch|deploy|release|publish|merge|push|"
    r"drop|delete|remove|wipe|erase|purge|reset|overwrite|truncate|"
    r"migrat\w*|rollback|revert|force|approve|sign[ -]?off|"
    r"pay|purchase|buy|send it|go ahead|kick ?off|prod|production|"
    r"terminate|tear ?down|nuke)\b"
)


def _looks_trivial(window: str) -> bool:
    """True when the recent tail is confident banter with nothing at stake.

    Conservative by design (Jeremy's graduated-response rule: round UP when unsure).
    Skips only when there is no work signal, no decisive/irreversible verb, AND the
    tail is either short or clearly small-talk; a long, ambiguous tail — or any tail
    that names an expensive-to-reverse action — is reviewed, not skipped.
    """
    tail = (window or "").strip()[-_TRIAGE_TAIL:]
    if not tail:
        return True  # nothing to review
    if _IRREVERSIBLE_RE.search(tail):
        return False  # a decisive/irreversible move — never triage away, even if terse
    if _WORK_SIGNAL_RE.search(tail):
        return False  # real work in the recent tail — never triage away
    if len(tail) < _TRIVIAL_MAX_CHARS:
        return True  # short + no work signal = banter
    return bool(_BANTER_RE.search(tail))  # long but chatty; otherwise review it


def _capture_intent(store: "Store") -> str:
    """Snapshot the active session's stated focus as an intent anchor.

    Captured at ENQUEUE time (when this conversation is the one in flight) and
    carried in the job, so the alignment lens still knows what the user set out to
    do even after the original ask has scrolled out of the window. Point-in-time
    input, not authority — the user's live inputs in the window can supersede it.
    Fail-safe: returns '' on any error.
    """
    try:
        from .sessions import get_active_tag

        tag = get_active_tag(store)
        if not tag:
            return ""
        extra = tag.get("extra") or {}
        focus = (extra.get("focus") or "").strip()
        segments = extra.get("segments") or []
        if isinstance(segments, list) and segments:
            latest = segments[-1] or {}
            focus = (latest.get("focus") or focus).strip()
        return focus[:500]
    except Exception:
        return ""


def _now() -> str:
    from .store import _now as store_now

    return store_now()


# ── observability: count the SILENT suppression paths ───────────────────────
# The expansion adds paths that suppress without a user-visible symptom (triage
# skips, sub-threshold reviews, verify drops, focus-stale drops, escalation
# failures). Without a counter, "the feature went quiet for the wrong reason" is
# invisible. These are cheap best-effort tallies surfaced by `kin sim status`.

_SIM_COUNTERS_META = "sim.counters"
_SIM_COUNTER_KEYS = (
    "triaged_skips", "sub_threshold", "verify_drops",
    "focus_stale_drops", "escalation_failures",
)


def _bump_counter(store: "Store", key: str, n: int = 1) -> None:
    try:
        raw = store.get_meta(_SIM_COUNTERS_META)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        data[key] = int(data.get(key, 0)) + n
        store.set_meta(_SIM_COUNTERS_META, json.dumps(data))
    except Exception:
        pass


def sim_counters(store: "Store") -> dict:
    try:
        raw = store.get_meta(_SIM_COUNTERS_META)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return {k: int(data.get(k, 0)) for k in _SIM_COUNTER_KEYS}


# ── runtime enable/disable (the `kin sim` kill switch) ──────────────────────

_SIM_ENABLED_META = "sim.enabled"


def _truthy(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def set_sim_enabled(store: "Store", enabled: bool) -> None:
    """Runtime override that wins over config.sim.enabled (global kill switch)."""
    store.set_meta(_SIM_ENABLED_META, "true" if enabled else "false")


def clear_sim_override(store: "Store") -> None:
    """Drop the runtime override so config.sim.enabled governs again."""
    store.set_meta(_SIM_ENABLED_META, "")


def sim_effective_enabled(store: "Store", config: Config) -> bool:
    """Effective on/off: config default, overridden by the runtime kill switch."""
    override = _truthy(store.get_meta(_SIM_ENABLED_META))
    return override if override is not None else bool(config.sim.enabled)


def sim_status(store: "Store", config: Config) -> dict:
    override = _truthy(store.get_meta(_SIM_ENABLED_META))
    return {
        "enabled": override if override is not None else bool(config.sim.enabled),
        "config_default": bool(config.sim.enabled),
        "runtime_override": override,
        "threshold": config.sim.threshold,
        "tick_interval": config.sim.tick_interval,
        "model": config.sim.model or config.llm.model,
        "command": config.sim.command or "(LLM supervisor)",
        "guidance": get_sim_guidance(store) or "(none)",
        "triage_banter": bool(config.sim.triage_banter),
        "advocate": (
            "on" if (config.sim.advocate.enabled and config.sim.advocate.command)
            else "recommend-only"
        ),
        "pending": len(_read_meta_list(store, SIM_PENDING_META)),
        "queued": len(_read_meta_list(store, SIM_QUEUE_META)),
        "claimed": bool(store.get_meta("sim.claim")),
        # Visibility into the silent-suppression paths the expansion added, so a
        # feature that has gone quiet for the WRONG reason is observable.
        "suppressed": sim_counters(store),
        "sessions": [{"conversation_id": row["key"].removeprefix("supervisor.state."),
                      **{k: v for k, v in json.loads(row["value"]).items()
                         if k in {"state", "reason", "tick", "reviewed_at", "updated_at"}}}
                     for row in store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'supervisor.state.%' ORDER BY key LIMIT 50")],
    }


# ── operator guidance (steers the lens, not the bar; clears on restart) ──────

_SIM_GUIDANCE_META = "sim.guidance"


def set_sim_guidance(store: "Store", text: str) -> None:
    """Set session-scoped guidance that steers what Sim weighs. Persists across
    messages; cleared at session start (see clear_sim_guidance)."""
    store.set_meta(_SIM_GUIDANCE_META, (text or "").strip())


def get_sim_guidance(store: "Store") -> str:
    return (store.get_meta(_SIM_GUIDANCE_META) or "").strip()


def clear_sim_guidance(store: "Store") -> bool:
    """Clear guidance. Returns True if something was actually cleared (so the
    SessionStart hook can print a one-line 'sim guidance cleared' notice)."""
    had = bool(get_sim_guidance(store))
    if had:
        store.set_meta(_SIM_GUIDANCE_META, "")
    return had


# ── window fingerprinting (staleness) ───────────────────────────────────────

def _tail_fingerprint(window: str) -> list[str]:
    """Salient tokens from the tail of a window — what Sim is reacting to.

    Used to detect staleness: if the tail Sim reviewed no longer overlaps the
    current tail, the conversation has moved on and the feedback is dropped.
    """
    tail = (window or "")[-_TAIL_CHARS:]
    return sorted(t for t in _tokens(tail) if len(t) > 3)


def _overlap(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ── prompt ──────────────────────────────────────────────────────────────────

def build_sim_grounding(store: "Store", window: str, config: Config) -> str:
    """Retrieve captured knowledge relevant to the window so Sim reviews WITH the
    graph's context instead of blind: top related concepts/decisions plus active
    constraints and watches. Char-capped by sim.grounding_chars (0 = disabled).
    Fail-safe — returns '' when disabled, empty, or retrieval errors (never raises),
    so it can run on the daemon drain without risk.
    """
    budget = getattr(config.sim, "grounding_chars", 0)
    if budget <= 0 or not (window or "").strip():
        return ""
    parts: list[str] = []
    used = 0
    seen: set[str] = set()

    def _add(line: str, key: str = "") -> bool:
        nonlocal used
        k = (key or line).strip().lower()
        if not line or k in seen or used + len(line) + 1 > budget:
            return False
        seen.add(k)
        parts.append(line)
        used += len(line) + 1
        return True

    try:
        from .tasks import list_tasks
        for task in list_tasks(store, status="all", project_path=str(config._project_path) if config._project_path else None, limit=None):
            if (task.get("extra") or {}).get("task_status", "open") in ("open", "in_progress"):
                _add(f"- [outstanding task] {task.get('title', '')[:160]}", key=task.get("id", ""))
                if len(parts) >= 3:
                    break
    except Exception:
        pass

    try:
        ops = store.operational_summary()
    except Exception:
        ops = {}
    for c in (ops.get("constraints") or [])[:3]:
        action = (c.get("extra") or {}).get("action", "warn")
        title = c.get("title", "")[:180]
        if not _add(f"- [constraint:{action}] {title}", key=title):
            break
    for w in (ops.get("watches") or [])[:3]:
        title = w.get("title", "")[:120]
        if not _add(f"- [watch] {title}", key=title):
            break

    try:
        from .retrieve import hybrid_search
        for r in hybrid_search(store, (window or "")[-2000:], top_k=6):
            ntype = r.get("type", "concept")
            if ntype in ("constraint", "watch"):
                continue  # surfaced with action/owner detail by operational_summary below
            title = r.get("title") or r.get("id") or ""
            content = (r.get("content") or "").strip().replace("\n", " ")[:140]
            if not _add(f"- [{ntype}] {title}" + (f": {content}" if content else ""), key=title):
                break
    except Exception:
        pass

    return "\n".join(parts)


def build_supervisor_prompt(
    window: str,
    max_chars: int,
    guidance: str = "",
    grounding: str = "",
    intent: str = "",
) -> str:
    guidance_block = ""
    if guidance.strip():
        guidance_block = (
            "\nOPERATOR GUIDANCE (the people running this session asked you to weight "
            "your attention toward the following). Let it steer WHAT you look for — but "
            "do NOT lower the bar: still stay silent unless something would materially "
            f"change the direction of the work.\n  >>> {guidance.strip()}\n"
        )
    grounding_block = ""
    if grounding.strip():
        grounding_block = (
            "\nWHAT KINDEX ALREADY KNOWS about this work (captured concepts and decisions, "
            "and especially active constraints/watches). Use it to catch a vital consideration "
            "the session may be missing — a constraint being violated, a known watch, a prior "
            "decision being contradicted — not to nitpick. Same bar: stay silent unless it "
            f"would materially change the direction.\n{grounding.strip()}\n"
        )
    intent_block = ""
    if intent.strip():
        intent_block = (
            "\nWHAT THE USER SET OUT TO DO (the session's stated focus — an intent anchor that "
            "may have scrolled out of the window). The user's ACTUAL inputs in the window are "
            "authoritative and a newer instruction can supersede this; use it only to judge "
            f"whether the work still serves the user's real goal.\n  >>> {intent.strip()}\n"
        )
    return f"""You are glancing in as a supervisor on an IN-PROGRESS work session between an agent and a user. This is low-stakes by default: the work is unfinished and the people are competent. Your DEFAULT action is to say NOTHING — most windows deserve silence.
{guidance_block}{grounding_block}{intent_block}
The single most valuable thing you can catch is an agent COMPETENTLY DOING THE WRONG THING — executing well, but on a goal that has drifted from what the user actually wants, or heading somewhere the user won't like. Agents lose track of the goal and leap from a conclusion straight to implementation; your job is to make them PAUSE AND CONSIDER, not to hand down verdicts.

CALIBRATE YOUR SCRUTINY TO WHAT IS ACTUALLY AT STAKE — decide this by reading the window:
  - Light back-and-forth, banter, or small talk: nothing is at stake. Return rating 0.0 and an empty note.
  - Ordinary code or task work: engage normally and hold it to the bar below.
  - A heavy, hard-to-reverse, or expensive move (spending money, kicking off a large workflow, a migration, a public or irreversible action, a decision costly to unwind): look hardest here, and route it to a deeper review (see ESCALATE).

Speak ONLY if something would MATERIALLY CHANGE THE DIRECTION of the work. Weigh three lenses; if you speak, your rating is the rating of the SINGLE MOST CONSEQUENTIAL lens — do NOT average across them:
  DIRECTION — the work itself: a claim about to be committed that is wrong, a contradiction the work hasn't noticed, a materially better path not being considered.
  ALIGNMENT — does the work still serve what the USER actually asked? Judge intent and goals, not the letter: a newer instruction can supersede an older one, but tell a real course-change from a passing aside that shouldn't redirect the whole plan. Ask WHY the user wants this, and whether the current actions still serve that why. Hold this lens to a HIGHER bar before firing: a wrong "you've drifted" note is corrosive — it makes a correct read look doubtful — so fire it only when you are fairly sure the agent has actually left the user's goal.
  TRAJECTORY — if this course continues, where does it land? Will it actually reach the goal, or is it divergent or counter-productive? Name the after-effects, side-effects, and implications; if they are undesired, the STRATEGY — not just the next step — should change.

DILIGENCE — has the plan actually been challenged, tested, and validated against the goal and constraints? Distinguish evidence of completed validation from promised checks. Identify forgotten outstanding work or a missing check whose absence changes whether continuing helps or detracts. Do not invent mandatory process or treat missing evidence as proof that a check failed.

For long-running or repeated work, apply the user's standing expectation: start with a bite-sized representative pilot, state its expected outcome, and quickly compare authoritative before/after counters before adding workers or extending the run. Does remaining work decrease? Does completed work stay completed after a restart or rebuild, instead of being redone? Activity, logs, CPU usage, restarts, and elapsed time do not establish progress. If a pilot misses its expected outcome, consider stopping or revising the approach before repeating or scaling it. If evidence is absent, request the smallest missing check rather than asserting failure. Judge the actual system's completion and validity rules; do not invent universal receipt semantics, binary-hash invalidation rules, or mandatory approval gates.

ESCALATE is a TRIP-WIRE you trip, not a verdict you render. When a decision that is EXPENSIVE TO REVERSE is being locked in — an entity/identity/boundary choice, a wire format, a migration, a spend, a large workflow — you usually CANNOT judge it from this window alone, because the constraints that make it right or wrong (scale, latency, lifetime, what is already committed elsewhere) are not in front of you. Do NOT render the architectural verdict yourself. Instead set stakes "high" and escalate true and name what a fully-framed deeper review — the Advocate panel including the Pat Helland architecture seat — should scrutinize. Detecting that an expensive-to-reverse decision is in play is your job; judging it is theirs.

Do NOT nitpick wording, demand definitions for their own sake, or manufacture criticism to seem useful. Comment on the DIRECTION of the work, not any single sentence — your note may be read several messages later, so it must survive the conversation moving on. Whatever you say is a CONSIDERATION for the agent to weigh, not a fact — it may be wrong.

Rate how strongly this warrants interrupting the people:
  0.0 = nothing to flag (the common case — return this and an empty note)
  0.5 = a refinement they'd appreciate but could skip
  0.8 = would likely change their next move
  1.0 = they are about to make a real mistake; speak now

Also report:
  - "dimension": the lens that drove your rating — "direction", "alignment", "trajectory", or "" if nothing. Use "reversibility" only when you are escalating.
  - "stakes": how consequential the current move is — "low", "medium", or "high" ("high" = money, a large workflow, or an expensive-to-reverse decision).
  - "escalate": true ONLY to route an expensive-to-reverse decision to the deeper Advocate/Helland review — never to render your own architectural verdict.
  - "escalate_reason": one phrase naming what that deeper review should scrutinize; "" if not escalating.

CONVERSATION WINDOW:
{(window or "")[:max_chars]}

Return JSON only:
{{"rating": 0.0, "note": "<one or two sentences, directional; empty if rating is low>", "basis": "<what in the window triggered this, brief>", "dimension": "", "stakes": "low", "escalate": false, "escalate_reason": ""}}
"""


# ── Sim invocation ──────────────────────────────────────────────────────────

@dataclass
class _SimResult:
    rating: float
    note: str
    basis: str
    dimension: str = ""
    stakes: str = "low"
    escalate: bool = False
    escalate_reason: str = ""
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


def _parse_sim(text: str) -> dict[str, Any]:
    from .attention import _parse_json_response

    return _parse_json_response(text)


def call_sim(
    config: Config,
    ledger: BudgetLedger,
    window: str,
    conversation_id: str,
    *,
    client: Any | None = None,
    guidance: str = "",
    grounding: str = "",
    intent: str = "",
) -> tuple[_SimResult | None, dict]:
    """Run one supervisory review. Returns (result, accounting).

    If sim.command is configured, Sim is invoked as a subprocess (prompt on
    stdin, response on stdout) — this is how the real Jeremy-simulacrum is wired
    in. Otherwise the configured LLM client runs the supervisor prompt, which
    keeps the feature portable and testable without the skill installed.
    """
    sc = config.sim
    model = sc.model or config.llm.model
    prompt = build_supervisor_prompt(
        window, sc.window_chars, guidance=guidance, grounding=grounding, intent=intent
    )

    if not ledger.can_spend():
        return None, {"status": "over_global_budget"}

    # Budget gate (only meaningful for the LLM path; subprocess Sim is its own cost).
    from .llm import estimate_cost

    est = estimate_cost(model, len(prompt) // 4, sc.max_output_tokens)
    conversation_spend = ledger.conversation_spend(conversation_id, purpose=SIM_PURPOSE)
    if not sc.command:
        if est > sc.max_review_cost:
            return None, {"status": "estimate_exceeds_review_budget", "estimate": est}
        if conversation_spend + est > sc.max_conversation_cost:
            return None, {
                "status": "estimate_exceeds_conversation_budget",
                "estimate": est,
                "conversation_spend": round(conversation_spend, 6),
            }

    # ── subprocess Sim ──────────────────────────────────────────────────────
    if sc.command:
        import os
        import shlex
        import shutil
        first = os.path.expanduser(shlex.split(sc.command)[0])
        if not shutil.which(first):
            return None, {"status": "command_unavailable"}
        if "simulacrum" in first and not any(os.environ.get(name) for name in
                ("ANTHROPIC_API_KEY", "WANDER_ANTHROPIC_API_KEY", "JMC_ANTHROPIC_API_KEY")):
            return None, {"status": "credential_unavailable"}
        # Opaque subprocesses cannot report reliable token usage. Reserve the
        # configured per-call allowance before launch, including failed calls.
        allowance = float(sc.max_review_cost)
        if allowance <= 0:
            return None, {"status": "estimate_exceeds_review_budget"}
        if conversation_spend + allowance > sc.max_conversation_cost:
            return None, {"status": "estimate_exceeds_conversation_budget"}
        if (ledger.today_spend + allowance > ledger.limits.daily or
                ledger.week_spend + allowance > ledger.limits.weekly or
                ledger.month_spend + allowance > ledger.limits.monthly):
            return None, {"status": "over_global_budget"}
        ledger.record(allowance, model="sim-command-reservation", purpose=SIM_PURPOSE,
                      conversation_id=conversation_id, estimate=allowance)
        try:
            import os

            proc = subprocess.run(
                os.path.expanduser(sc.command),
                shell=True,
                input=redact_text(prompt),
                capture_output=True,
                text=True,
                timeout=sc.command_timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, {"status": "sim_command_error", "error": safe_error(exc)}
        if proc.returncode != 0:
            return None, {"status": "sim_command_failed", "error": redact_text(proc.stderr)[:200]}
        parsed = _parse_sim(redact_text(proc.stdout))
        result = _result_from_parsed(parsed)
        return result, {"status": "ok" if result is not None else "invalid_output", "via": "command"}

    # ── LLM-as-supervisor ───────────────────────────────────────────────────
    if client is None:
        from .llm import get_client

        client = get_client(config)
    if client is None:
        return None, {"status": "llm_unavailable", "estimate": est}

    try:
        response = client.messages.create(
            model=model,
            max_tokens=sc.max_output_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm import calculate_cost

        cost = calculate_cost(model, response.usage)
        ledger.record(
            cost["amount"], model=model, purpose=SIM_PURPOSE,
            tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
            cache_creation_tokens=cost.get("cache_creation_tokens", 0),
            cache_read_tokens=cost.get("cache_read_tokens", 0),
            conversation_id=conversation_id, estimate=est,
        )
        parsed = _parse_sim(response.content[0].text)
    except Exception as exc:
        return None, {"status": "llm_error", "error": safe_error(exc)}

    result = _result_from_parsed(parsed)
    if result:
        result.cost = cost["amount"]
        result.tokens_in = cost["tokens_in"]
        result.tokens_out = cost["tokens_out"]
    return result, {"status": "ok" if result is not None else "invalid_output", "via": "llm", "cost": cost}


def _result_from_parsed(parsed: dict[str, Any]) -> _SimResult | None:
    import math
    if not isinstance(parsed, dict) or "rating" not in parsed or not isinstance(parsed.get("note"), str):
        return None
    try:
        rating = float(parsed["rating"])
    except (TypeError, ValueError):
        return None
    if isinstance(parsed["rating"], bool) or not math.isfinite(rating) or not 0 <= rating <= 1:
        return None
    note = str(parsed.get("note") or "").strip()
    basis = str(parsed.get("basis") or "").strip()
    dimension = str(parsed.get("dimension") or "").strip().lower()
    stakes = str(parsed.get("stakes") or "low").strip().lower()
    if stakes not in ("low", "medium", "high"):
        stakes = "low"
    escalate = bool(parsed.get("escalate", False)) and stakes == "high"
    escalate_reason = str(parsed.get("escalate_reason") or "").strip()
    return _SimResult(
        rating=rating, note=note, basis=basis,
        dimension=dimension, stakes=stakes,
        escalate=escalate, escalate_reason=escalate_reason,
    )


# ── queue (enqueue cheap, drain in daemon) ──────────────────────────────────

def _read_meta_list(store: "Store", key: str) -> list[dict]:
    try:
        raw = store.get_meta(key)
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def enqueue_sim_review(
    store: "Store",
    config: Config,
    conversation_id: str,
    window: str,
    *,
    tick: int,
    intent: str | None = None,
    scope: dict | None = None,
) -> bool:
    """Snapshot a window for later supervisory review. Cheap, SQLite-only.

    Deduped by conversation (a fresher window replaces a staler one). Gated to
    roughly every `tick_interval` ticks so we don't queue every prompt.
    """
    if not sim_effective_enabled(store, config) or not conversation_id \
            or not (window or "").strip():
        return False
    interval = max(1, int(config.sim.tick_interval or 1))
    if tick % interval != 0:
        from .supervisor import read_state, write_state
        if read_state(store, conversation_id).get("state") not in (
                "reviewed_quiet", "queued", "reviewing", "failed", "unavailable", "budget_exhausted", "delivered"):
            write_state(store, conversation_id, "skipped", reason="cadence")
        return False
    # Tier 0: confident banter is skipped before it costs anything (round UP when
    # unsure — only a confidently-trivial window is dropped).
    if config.sim.triage_banter and _looks_trivial(window):
        _bump_counter(store, "triaged_skips")
        from .supervisor import write_state
        write_state(store, conversation_id, "skipped", reason="banter")
        return False

    from .supervisor import store_lock, config_snapshot, write_state
    with store_lock(store, "queue"):
        import hashlib
        subject = hashlib.sha256(json.dumps([window, intent, config_snapshot(config)], sort_keys=True).encode()).hexdigest()
        admission_key = "sim.admission." + conversation_id
        if store.get_meta(admission_key) == subject:
            return False
        store.set_meta(admission_key, subject)
        previous_queue = _read_meta_list(store, SIM_QUEUE_META)
        queue = [j for j in previous_queue if j.get("conversation_id") != conversation_id]
        queue.append({
            "conversation_id": conversation_id,
            "scope": scope,
            "review_id": subject,
            "window": window[-config.sim.window_chars:],
            "fingerprint": _tail_fingerprint(window),
            "intent": _capture_intent(store) if intent is None else intent,
            "guidance": get_sim_guidance(store) if intent is None else "",
            "config": config_snapshot(config),
            "tick": tick,
            "at": _now(),
        })
        retained = queue[-config.sim.max_queue:]
        store.set_meta(SIM_QUEUE_META, json.dumps(retained))
        retained_ids = {(j.get("conversation_id"), j.get("review_id")) for j in retained}
        for previous in previous_queue:
            if (previous.get("conversation_id"), previous.get("review_id")) not in retained_ids:
                _record_advisory_discard(previous, reason="superseded", source="hook")
        write_state(store, conversation_id, "queued", reason="cadence", review_tick=tick)
        from .supervisor import record_health
        record_health(scope, "review", state="queued", source="hook", event_id=subject + ":queued", review_id=subject)
    return True


def drain_sim_queue(
    store: "Store",
    config: Config,
    *,
    client: Any | None = None,
    ledger: BudgetLedger | None = None,
    max_jobs: int = 5,
    background: bool = False,
) -> dict:
    """Drain a bounded batch; native admission retains one waiting successor."""
    from contextlib import ExitStack
    from .supervisor import store_lock
    with ExitStack() as held:
        try:
            if background:
                # At most one process waits for the active worker. Other wakeups
                # coalesce into it; the waiter releases this gate once active.
                with store_lock(store, "wake", blocking=False):
                    held.enter_context(store_lock(store, "worker", blocking=True))
            else:
                held.enter_context(store_lock(store, "worker", blocking=False))
        except BlockingIOError:
            return {"status": "reviewing", "reviewed": 0, "pending": 0}
        result = _drain_claimed(store, config, client=client, ledger=ledger, max_jobs=max_jobs)
        if background and result["status"] == "ok":
            with store_lock(store, "queue"):
                remaining = bool(_read_meta_list(store, SIM_QUEUE_META))
            if remaining and not spawn_background_drain(config):
                result["status"] = "handoff_failed"
        return result


def _drain_claimed(store, config, *, client=None, ledger=None, max_jobs=5):
    from dataclasses import asdict
    from .supervisor import restore_config, write_state
    from .sim_queue import (CLAIM_META, SavedSim, acknowledge, claim_next,
                            flush_receipts, save_claim)
    flush_receipts(store)
    if not sim_effective_enabled(store, config):
        return {"status": "disabled", "reviewed": 0, "pending": 0}
    reviewed = flagged = 0
    for _ in range(max(0, max_jobs)):
        claim, recovered = claim_next(store)
        if claim is None:
            break
        job = claim.job
        conv, window = job.get("conversation_id"), job.get("window") or ""

        def finish(state, reason, health_state=None, health_reason="review_failed"):
            acknowledge(store, claim, state=state, reason=reason,
                        health_state=health_state or state, health_reason=health_reason)
            flush_receipts(store)

        if recovered and claim.phase in {"sim_started", "advocate_started"}:
            # A dead worker may already have paid. Keep its saved Sim result in
            # the receipt, report uncertainty, and never replay either provider.
            finish("failed", "interrupted_spend_unknown")
            continue
        if not conv or not isinstance(window, str) or not window.strip():
            finish("failed", "invalid_job")
            continue
        try:
            cfg = restore_config(job["config"]) if job.get("config") else config
        except Exception:
            finish("failed", "invalid_snapshot")
            continue
        if cfg.data_path != store.config.data_path:
            finish("failed", "store_mismatch")
            continue
        if not sim_effective_enabled(store, cfg):
            finish("disabled", "kill_switch", health_state="discarded", health_reason="superseded")
            continue
        write_state(store, conv, "reviewing", reason="claimed", review_tick=job.get("tick", 0))
        intent = job.get("intent") or ""
        try:
            if claim.phase == "ready":
                finish("queued", "review_complete_pending_delivery", "completed", "advisory")
                flagged += 1
                continue
            budget = ledger or BudgetLedger(cfg.ledger_path, cfg.budget)
            grounding = build_sim_grounding(store, window, cfg)
            if claim.result is None:
                # Durable dispatch boundary comes before any possible provider
                # spend. Recovery earlier than this point can safely resume.
                claim.phase = "sim_started"
                save_claim(store, claim)
                result, acct = call_sim(cfg, budget, window, conv, client=client,
                                       guidance=job.get("guidance", ""), grounding=grounding, intent=intent)
                status = acct.get("status", "unknown")
                if status != "ok" or result is None:
                    state = ("budget_exhausted" if "budget" in status else
                             "unavailable" if "unavailable" in status else "failed")
                    finish(state, status, health_reason="budget_exhausted" if state == "budget_exhausted" else
                           "llm_unavailable" if state == "unavailable" else "review_failed")
                    continue
                claim.result = SavedSim(**asdict(result))
                claim.phase = "sim_saved"
                save_claim(store, claim)
            else:
                result = _SimResult(**claim.result.model_dump())
            reviewed += 1
            if not result.note or result.rating < cfg.sim.threshold:
                _bump_counter(store, "sub_threshold")
                finish("reviewed_quiet", "below_threshold", health_reason="no_findings")
                continue
            item = {
                "conversation_id": conv, "scope": job.get("scope"), "review_id": job.get("review_id"),
                "note": result.note, "basis": result.basis,
                "rating": round(result.rating, 3), "dimension": result.dimension,
                "stakes": result.stakes, "escalate": result.escalate,
                "escalate_reason": result.escalate_reason, "intent": intent,
                "fingerprint": job.get("fingerprint") or [], "tick": job.get("tick", 0), "at": _now(),
            }
            if result.escalate and not _advocate_gate_open(store, cfg, conv, job.get("tick", 0)):
                state, reason = (("disabled", "recommend_only") if not cfg.sim.advocate.enabled else
                                 ("unavailable", "command_unavailable") if not cfg.sim.advocate.command else
                                 ("skipped", "cooldown"))
                write_state(store, conv, None, advocate_state=state, advocate_reason=reason)
            if result.escalate and _advocate_gate_open(store, cfg, conv, job.get("tick", 0)):
                claim.phase = "advocate_started"
                save_claim(store, claim)
                ran, survivors = maybe_escalate_to_advocate(
                    store, cfg, budget, window, grounding, intent, result, conv, client=client)
                if ran:
                    _mark_advocate_run(store, conv, int(job.get("tick", 0)))
                    if survivors:
                        item["advocate"] = survivors
                    else:
                        _bump_counter(store, "escalation_failures")
            claim.pending = item
            claim.phase = "ready"
            save_claim(store, claim)
            finish("queued", "review_complete_pending_delivery", "completed", "advisory")
            flagged += 1
        except Exception as exc:
            # A completed transaction may already have removed this exact claim;
            # never overwrite its successful receipt after a later output error.
            if not store.get_meta(CLAIM_META):
                raise
            write_state(store, conv, "failed", reason="review_error", error=safe_error(exc))
            finish("failed", "review_error")
    return {"status": "ok", "reviewed": reviewed, "flagged": flagged,
            "pending": len(_read_meta_list(store, SIM_PENDING_META))}


# ── Tier 2: deep escalation to Advocate (opt-in, gated, verified) ───────────
#
# The default path is LIGHT: a high-stakes review sets `escalate` and the note
# carries a recommendation. Only when sim.advocate.enabled is turned on does a
# high-stakes escalation actually invoke ~/Code/advocate (the multi-persona
# adversarial engine, including the Helland seat). Because Advocate is multi-call,
# its findings are run through an adversarial verify pass before surfacing — the
# 2026-06-09 head-to-head experiment showed an unverified multi-call path smuggles
# in confident hallucinations the worth-gate misses.

_SIM_ADVOCATE_META = "sim.advocate.last"  # {conversation_id: tick} cooldown ledger


def _advocate_last_ticks(store: "Store") -> dict[str, int]:
    try:
        raw = store.get_meta(_SIM_ADVOCATE_META)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _advocate_gate_open(store: "Store", config: Config, conversation_id: str, tick: int) -> bool:
    """True when a deep escalation is permitted: enabled, wired, and off cooldown."""
    ac = config.sim.advocate
    if not ac.enabled or not ac.command or not conversation_id:
        return False
    last = _advocate_last_ticks(store).get(conversation_id)
    if last is not None and int(tick) - int(last) < ac.cooldown_ticks:
        return False
    return True


def _mark_advocate_run(store: "Store", conversation_id: str, tick: int) -> None:
    try:
        ledger = _advocate_last_ticks(store)
        ledger[conversation_id] = int(tick)
        # keep the cooldown ledger bounded
        if len(ledger) > 50:
            ledger = dict(sorted(ledger.items(), key=lambda kv: kv[1])[-50:])
        store.set_meta(_SIM_ADVOCATE_META, json.dumps(ledger))
    except Exception:
        pass


def build_advocate_prompt(window: str, grounding: str, intent: str, result: _SimResult) -> str:
    """The brief handed to Advocate for a deep architectural review. Ships the FULL
    frame (window + kindex grounding + intent + what Sim flagged) so the reviewers
    are never context-starved — the root cause of the WanderOTA over-fire — and
    carries the Helland calibration guardrail so the Helland seat does not treat
    every service that touches a value as a claimant."""
    parts = [
        "Deep adversarial review requested by the supervisor because this looks "
        "high-stakes and hard to reverse. Scrutinize the DIRECTION being locked in.",
        f"\nWHAT THE SUPERVISOR FLAGGED: {result.note}",
    ]
    if result.escalate_reason:
        parts.append(f"WHAT A DEEPER REVIEW SHOULD SCRUTINIZE: {result.escalate_reason}")
    if intent.strip():
        parts.append(f"\nWHAT THE USER SET OUT TO DO: {intent.strip()}")
    if grounding.strip():
        parts.append(f"\nWHAT KINDEX ALREADY KNOWS:\n{grounding.strip()}")
    parts.append(
        "\nARCHITECTURAL STANDARD: Pat Helland's doctrine — one authority per fact, "
        "references point inward, reconcile in settlement. CALIBRATE: 'one authority "
        "per fact' is a forcing function, not a generative principle; it constrains "
        "WHO OWNS a fact, not what counts as one fact versus two. A service that USES "
        "a value to compute is not a claimant on it. Flag genuine two-owner / unnamed-"
        "authority problems, not mere layering."
    )
    parts.append(f"\nCONVERSATION WINDOW:\n{window}")
    return "\n".join(parts)


_ADVOCATE_DROP_SEVERITIES = {"low", "info"}  # keep only what could change direction


def _parse_advocate_findings(text: str) -> list[str]:
    """Pull short finding strings out of Advocate's JSON output.

    Matches the REAL ~/Code/advocate schema (verified 2026-09-02): `advocate
    review -o <file>` writes a full Review model dump — findings are NESTED under
    `persona_reports[].findings[]`, each {persona, severity, dimension, title,
    detail, evidence, recommendation}. Low/info findings are dropped (they don't
    clear the "materially change direction" bar). Also accepts the simpler shapes
    (a bare list, or {"findings": [...]}) so a wrapper can pre-flatten if it wants.

    Robust to a leading banner (the wrapper is expected to emit JSON only, but the
    tool's human report can leak in): if a straight parse fails, retry on the
    substring from the first '{' to the last '}'. Returns [] on anything
    unparseable — fail-closed, never surface garbage."""
    data = _loads_lenient(text)
    if data is None:
        return []

    def _fmt(obj: dict) -> str:
        sev = str(obj.get("severity") or "").strip().lower()
        if sev in _ADVOCATE_DROP_SEVERITIES:
            return ""
        persona = str(obj.get("persona") or "").strip()
        title = str(
            obj.get("title") or obj.get("summary")
            or obj.get("detail") or obj.get("finding") or obj.get("message") or ""
        ).strip()
        if not title:
            return ""
        label = " · ".join(x for x in (sev, persona) if x)
        return (f"[{label}] {title}" if label else title)[:240]

    out: list[str] = []

    # Real Advocate Review: dig persona_reports[].findings[].
    if isinstance(data, dict) and isinstance(data.get("persona_reports"), list):
        for report in data["persona_reports"]:
            if not isinstance(report, dict):
                continue
            for f in report.get("findings") or []:
                if isinstance(f, dict):
                    s = _fmt(f)
                    if s:
                        out.append(s)
        return out

    # Simpler shapes: {"findings": [...]} / {"results": [...]} / a bare list.
    if isinstance(data, dict):
        data = data.get("findings") or data.get("results") or []
    if not isinstance(data, list):
        return []
    for item in data:
        if isinstance(item, str):
            s = item.strip()[:240]
        elif isinstance(item, dict):
            s = _fmt(item)
        else:
            s = ""
        if s:
            out.append(s)
    return out


def _loads_lenient(text: str) -> Any:
    """json.loads, falling back to the first '{'..last '}' slice if a banner leaked
    in ahead of the JSON. Returns None on failure."""
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _verify_findings(
    config: Config,
    ledger: BudgetLedger,
    window: str,
    grounding: str,
    findings: list[str],
    conversation_id: str,
    *,
    client: Any | None = None,
    store: "Store | None" = None,
) -> list[str]:
    """Adversarial verify pass with a GROUNDING check, not a vibe check.

    The verifier must, for each finding it keeps, cite a verbatim QUOTE from the
    conversation window that supports it; we mechanically drop any kept finding
    whose quote does not actually appear in the window. This is the answer to the
    "verify launders authority" failure — a second skeptical LLM is just another
    generator, so we require it to point at evidence and we check the citation.

    One batched LLM call. Fail-CLOSED: if verification can't run, return [] rather
    than surface unverified claims."""
    if not findings:
        return []
    if not ledger.can_spend():
        return []
    sc = config.sim
    model = sc.model or config.llm.model
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(findings))
    prompt = (
        "You are verifying findings from a deep review before they interrupt the "
        "people. For EACH finding, decide whether it genuinely holds against the "
        "conversation and the known context below — a real problem in THIS work, not "
        "a plausible-sounding generic concern and not a hallucination. Be skeptical; "
        "when a finding is not clearly supported, DROP it. To KEEP a finding you must "
        "cite a short VERBATIM quote copied from the CONVERSATION WINDOW that "
        "demonstrates the problem — if you cannot quote the window, drop it.\n\n"
        f"KNOWN CONTEXT:\n{grounding.strip() or '(none)'}\n\n"
        f"CONVERSATION WINDOW:\n{window[:sc.window_chars]}\n\n"
        f"FINDINGS:\n{numbered}\n\n"
        'Return JSON only: {"keep": [{"i": <finding index>, '
        '"quote": "<verbatim span copied from the window>"}]}'
    )
    if client is None:
        from .llm import get_client

        client = get_client(config)
    if client is None:
        return []
    try:
        response = client.messages.create(
            model=model,
            max_tokens=sc.max_output_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm import calculate_cost

        cost = calculate_cost(model, response.usage)
        ledger.record(
            cost["amount"], model=model, purpose=SIM_PURPOSE,
            tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
            cache_creation_tokens=cost.get("cache_creation_tokens", 0),
            cache_read_tokens=cost.get("cache_read_tokens", 0),
            conversation_id=conversation_id, estimate=0.0,
        )
        parsed = _parse_sim(response.content[0].text)
    except Exception:
        return []
    keep = parsed.get("keep")
    if not isinstance(keep, list):
        return []
    win_norm = _normalize(window)
    survivors: list[str] = []
    for entry in keep:
        # Accept the grounded shape {"i","quote"}; tolerate a bare index too, but a
        # bare index has no citation to check, so it is dropped (fail-closed).
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("i"))
            finding = findings[idx]
        except (ValueError, TypeError, IndexError):
            continue
        quote = _normalize(str(entry.get("quote") or ""))
        if len(quote) >= 8 and quote in win_norm:
            survivors.append(finding)
    dropped = len(findings) - len(survivors)
    if dropped > 0 and store is not None:
        _bump_counter(store, "verify_drops", dropped)
    return survivors


def maybe_escalate_to_advocate(
    store: "Store",
    config: Config,
    ledger: BudgetLedger,
    window: str,
    grounding: str,
    intent: str,
    result: _SimResult,
    conversation_id: str,
    *,
    client: Any | None = None,
) -> tuple[bool, list[str]]:
    """Run the deep Advocate/Helland review and return (ran, verified_survivors).

    `ran` is True once the Advocate subprocess actually executed (and thus spent on
    its persona calls), so the caller can advance the cooldown and NOT re-pay for
    the same expensive call on the next high-stakes tick — even when verification
    then drops everything. `ran` is False on the light/no-op paths (disabled,
    unaffordable, launch error) where nothing was spent and a retry is fine.

    Admission gate: `max_cost` is enforced as "don't START unless today's remaining
    budget covers a worst-case run" — it is NOT a hard cap on the opaque subprocess
    (we only learn Advocate's real cost after it returns). The global BudgetLedger
    is the one authority on whether there is money to spend. Never raises.
    """
    from .supervisor import write_state
    ac = config.sim.advocate
    if not ac.command or not ac.enabled:
        write_state(store, conversation_id, None, advocate_state="disabled", advocate_reason="recommend_only")
        return False, []
    import os
    import shlex
    import shutil
    try:
        available = bool(shutil.which(os.path.expanduser(shlex.split(ac.command)[0])))
    except (ValueError, IndexError):
        available = False
    if not available:
        write_state(store, conversation_id, None, advocate_state="unavailable", advocate_reason="command_unavailable")
        return False, []
    allowance = float(ac.max_cost)
    if (allowance <= 0 or not ledger.can_spend() or
            ledger.today_spend + allowance > ledger.limits.daily or
            ledger.week_spend + allowance > ledger.limits.weekly or
            ledger.month_spend + allowance > ledger.limits.monthly or
            ledger.conversation_spend(conversation_id, purpose=SIM_PURPOSE) + allowance > config.sim.max_conversation_cost):
        write_state(store, conversation_id, None, advocate_state="budget_exhausted", advocate_reason="escalation_allowance")
        return False, []
    if client is None:
        from .llm import get_client
        # Explicit Advocate opt-in includes its required verification pass, but
        # must not enable classification/extraction or other global LLM features.
        verification_config = config.model_copy(deep=True)
        verification_config.llm.enabled = True
        client = get_client(verification_config)
    if client is None:
        write_state(store, conversation_id, None, advocate_state="unavailable", advocate_reason="verification_provider")
        return False, []
    ledger.record(allowance, model="advocate-command-reservation", purpose=SIM_PURPOSE,
                  conversation_id=conversation_id, estimate=allowance)
    write_state(store, conversation_id, None, advocate_state="reviewing", advocate_reason="reserved")
    prompt = build_advocate_prompt(window, grounding, intent, result)
    try:
        import os

        proc = subprocess.run(
            os.path.expanduser(ac.command),
            shell=True, input=redact_text(prompt), capture_output=True, text=True,
            timeout=ac.timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        write_state(store, conversation_id, None, advocate_state="failed", advocate_reason="command_error")
        return True, []  # A timed-out subprocess may already have spent: consume cooldown.
    # From here Advocate executed and spent on its persona calls: ran=True even if
    # the exit code is non-zero (partial persona failure still writes findings) or
    # verification later drops everything.
    if proc.returncode:
        write_state(store, conversation_id, None, advocate_state="failed", advocate_reason="command_failed")
        return True, []
    findings = _parse_advocate_findings(redact_text(proc.stdout))
    if not findings:
        write_state(store, conversation_id, None, advocate_state="reviewed_quiet", advocate_reason="no_findings")
        return True, []
    survivors = _verify_findings(
        config, ledger, window, grounding, findings, conversation_id,
        client=client, store=store,
    )
    write_state(store, conversation_id, None, advocate_state="completed" if survivors else "reviewed_quiet",
                advocate_reason="verified_findings" if survivors else "no_verified_findings")
    return True, survivors[: ac.max_findings]


# ── pickup (cheap, on the tick) ─────────────────────────────────────────────

def _compose_sim_message(mine: dict) -> str:
    """Build the surfaced message: the directional note, plus — when the review was
    high-stakes — either the verified Advocate survivors (deep path ran) or a light
    recommendation to run one (default path). The pure note stays untouched for any
    caller that just wants the signal."""
    note = str(mine.get("note") or "").strip()
    advocate = mine.get("advocate") or []
    if isinstance(advocate, list) and advocate:
        # Provenance intact and framed as claims to check, not confirmed facts —
        # so a "verified" finding doesn't launder its way past the agent's judgment.
        lines = "\n".join(f"  - {f}" for f in advocate)
        return (
            f"{note}\n\nAdvocate (incl. Helland seat) raised these to CHECK against "
            f"the actual design — claims, not confirmed facts:\n{lines}"
        )
    if mine.get("escalate") and str(mine.get("stakes")) == "high":
        reason = str(mine.get("escalate_reason") or "").strip()
        tail = f" ({reason})" if reason else ""
        return (
            f"{note}\n[Noted: expensive to reverse{tail}. A NOTE, not a gate — these are "
            f"professional engineers who know what a one-way door is. Do not stop and ask "
            f"for confirmation on reversal cost alone; an Advocate/Helland review is "
            f"available if the DECISION itself looks doubtful.]"
        )
    return note


def _record_advisory_discard(item: dict, *, reason: str, source: str) -> None:
    """A deliberate durable removal settles delivery debt, never delivery/value."""
    from .supervisor import record_health
    review_id = item.get("review_id")
    if not isinstance(review_id, str) or not review_id:
        return
    record_health(item.get("scope"), "review", state="discarded", reason=reason,
                  source=source, review_id=review_id, event_id=review_id + ":discarded:" + reason)


def pop_pending_sim_injection(
    store: "Store",
    config: Config,
    conversation_id: str,
    current_window: str,
    *,
    tick: int,
    intent: str | None = None,
) -> AttentionInjection | None:
    """Surface a pending Sim injection if one is fresh. Cheap, no LLM.

    Freshness: the pending injection is dropped (not shown) if it is older than
    `max_stale_ticks` ticks OR the tail Sim reacted to no longer overlaps the
    current tail — i.e. the conversation has scrolled past what Sim flagged.
    """
    if not sim_effective_enabled(store, config) or not conversation_id:
        return None
    pending_list = _read_meta_list(store, SIM_PENDING_META)
    if not pending_list:
        return None

    mine = next((p for p in pending_list if p.get("conversation_id") == conversation_id), None)
    if not mine:
        return None

    # Always consume it: either we surface it now or it's stale — never re-queue.
    rest = [p for p in pending_list if p.get("conversation_id") != conversation_id]
    store.set_meta(SIM_PENDING_META, json.dumps(rest))
    # Only acknowledge discard/delivery after the pending update succeeds.
    for previous in pending_list:
        if (previous is not mine and previous.get("conversation_id") == conversation_id
                and previous.get("review_id") != mine.get("review_id")):
            _record_advisory_discard(previous, reason="superseded", source="hook")

    age = tick - int(mine.get("tick", tick))
    if age > config.sim.max_stale_ticks:
        from .supervisor import write_state
        write_state(store, conversation_id, "skipped", reason="stale_age",
                    delivery_drop_reason="stale_age", delivery_drop_tick=tick)
        _record_advisory_discard(mine, reason="stale", source="hook")
        return None
    overlap = _overlap(mine.get("fingerprint") or [], _tail_fingerprint(current_window))
    if overlap < config.sim.min_overlap:
        from .supervisor import write_state
        write_state(store, conversation_id, "skipped", reason="stale_window",
                    delivery_drop_reason="stale_window", delivery_drop_tick=tick)
        _record_advisory_discard(mine, reason="stale", source="hook")
        return None

    # Focus-staleness drop: if the user's stated goal changed between enqueue and
    # now, an alignment note judged against the DEAD goal would fight the user's
    # (correct, newly-redirected) work. The note was reasoned from a superseded
    # intent — drop it rather than surface a directive pointing back at the old goal.
    enqueue_intent = _normalize(str(mine.get("intent") or ""))
    if enqueue_intent:
        current_intent = _normalize(_capture_intent(store) if intent is None else intent)
        if current_intent and current_intent != enqueue_intent:
            _bump_counter(store, "focus_stale_drops")
            from .supervisor import write_state
            write_state(store, conversation_id, "skipped", reason="stale_goal",
                        delivery_drop_reason="stale_goal", delivery_drop_tick=tick)
            _record_advisory_discard(mine, reason="superseded", source="hook")
            return None

    injection = AttentionInjection(
        id=f"sim:{conversation_id}",
        title="Sim (supervisory)",
        message=_compose_sim_message(mine),
        reason=mine.get("basis", ""),
        confidence=float(mine.get("rating", 0.0)),
    )

    if config.sim.deposit_pheromone:
        _deposit_sim_pheromone(store, config)
    from .supervisor import record_health
    record_health(mine.get("scope"), "delivery", source="hook", reason="advisory",
                  event_id=mine.get("review_id"), review_id=mine.get("review_id"))
    return injection


def _deposit_sim_pheromone(store: "Store", config: Config) -> None:
    """Advisory only — Sim injections lay a coarse trail like attention does.

    Sim feedback is not a graph node, so there is no node id to track per-item;
    we record the event on the conversation state for later inspection rather
    than on a node trail. Kept deliberately minimal and failure-safe.
    """
    try:
        ctx = pheromone_context(config)  # reserved for future conditioned trails
        _ = ctx
    except Exception:
        pass


def format_sim_injection(
    injection: AttentionInjection | None,
    *,
    display: str = "minimal",
) -> list[str]:
    """Render a Sim injection for the prompt hook output.

    display:
      full    — labelled block with basis + advisory footer
      minimal — a single "Sim:" prefixed line (the note is the signal)
      quiet   — same minimal text (the user-facing block is suppressed at the
                hook layer via suppressOutput; the model still receives this)
    """
    if not injection or not injection.message:
        return []
    note = injection.message.strip()
    if display == "quiet":
        # Agent-facing and invisible to the user. Framed as a CONSIDERATION to weigh
        # (a guess that may be wrong), not a verdict — so it prompts the agent to
        # pause and consider rather than obey. The agent routes it: act on it
        # silently if it holds up, and surface it to the user ONLY if it genuinely
        # needs their decision.
        return [
            "[SIM — a supervisory consideration for you, the agent; the user does NOT "
            "see this. It's a guess and may be wrong — weigh it, don't obey it. If you "
            "can act on it yourself, do so silently. Surface it to the user only if it "
            f"genuinely needs their judgment.] {note}"
        ]
    if display == "minimal":
        return [f"Sim: {note}"]
    lines = ["KINDEX · SIM"]
    conf = injection.confidence
    marker = f" ({conf:.2f})" if isinstance(conf, (int, float)) and conf else ""
    lines.append(f"  - {note}{marker}")
    if injection.reason:
        lines.append(f"    Basis: {injection.reason}")
    lines.append("  (Sim is advisory — ignore it freely. `kin sim disable` to stop.)")
    return lines


# ── background self-drain (no daemon required) ──────────────────────────────

def spawn_background_drain(config: Config) -> bool:
    """Fire-and-forget a detached `kin sim drain` so reviews run off the agent's
    critical path without a daemon. The prompt hook stays fast; the review lands
    a few seconds later and the next tick picks it up. Best-effort; never raises.
    """
    if not config.sim.drain_on_tick:
        return False
    import os
    import subprocess
    import sys
    from pathlib import Path

    from .supervisor import config_snapshot
    try:
        # The workspace is data, not import authority. Isolated mode ignores
        # cwd, PYTHONPATH and user-site additions on every supported Python.
        # Pin the package root already executing this hook so editable installs
        # work as well as wheels, without trusting a same-named workspace tree.
        package_root = str(Path(__file__).resolve().parent.parent)
        bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                     "from kindex.supervisor import worker_main; worker_main()")
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap, package_root],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE, text=True, start_new_session=True,
            cwd=str(config._project_path or config.data_path),
            env={key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}},
        )
        process.stdin.write(json.dumps(config_snapshot(config)))
        process.stdin.close()
        return True
    except Exception:
        return False
