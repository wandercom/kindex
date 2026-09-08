"""Session-end stigmergic reinforcement — the feedback loop that turns
injection-relevance into injection-*usefulness*.

The attention hook deposits pheromone whenever it injects a node (see
attention.py). On its own that is survivorship-biased: it can only reward what
got surfaced, and it cannot tell "relevant" from "load-bearing". This module
closes the loop at session end by grading the actual session trace, and — just
as importantly — by hunting for what *would* have helped but was never injected.

Two arms, both grounded in observable evidence rather than self-report:

  ARM A  observed reinforcement — for each injected node, did the agent ACT on
         it? The grader must quote where, or return ignored. Confirmed use
         deposits a strong reinforcement; ignored injections get nothing and
         decay away.

  ARM B  counterfactual / missed injection — what knowledge would have helped
         that wasn't surfaced? This is where most of the signal lives (in the
         mined history, 54/79 sessions injected nothing at all). Each miss is
         tied to concrete evidence, matched against the graph, and deposits a
         counterfactual trace so it is more likely to surface next time. A real
         need that matches no node is logged as a knowledge gap.

Signal weighting (categories chosen by the grader, weights set in code so they
stay tunable and not LLM-decided):

  used                  -> pheromone_reinforce      (confirmed use of an injection)
  user_correction       -> pheromone_correction     (HEAVIEST — the user pushing
                            back is the strongest ground truth we get)
  agent_admission       -> pheromone_counterfactual ("I should have…" — a real
                            but lighter signal)
  inferred              -> pheromone_counterfactual (grader infers from an error
                            or rework, no explicit correction/admission)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .attention import injection_node_id, pheromone_context
from .privacy import protect_logger, redact_text
from .budget import BudgetLedger
from .config import Config

log = protect_logger(logging.getLogger(__name__))

if TYPE_CHECKING:
    from .store import Store

REINFORCE_PURPOSE = "reinforce"

# meta key holding the auto-learned ranking weight (0 = inert / not yet mature)
PHEROMONE_WEIGHT_META = "pheromone.effective_weight"

# meta key holding the pending session-end grading queue (enqueue cheap, drain in cron)
REINFORCE_QUEUE_META = "reinforce.queue"


def enqueue_reinforce(store: "Store", conversation_id: str,
                      transcript_path: str = "", trace: str = "",
                      max_queue: int = 50) -> bool:
    """Record a session for later grading. Super lightweight: a little SQLite,
    no LLM, no output — safe to call from Stop / PreCompact / tag-end hooks.

    Deduped by conversation_id; the richest source wins (a transcript_path is
    preferred over an inline summary, whichever hook supplied it).
    """
    if not conversation_id:
        return False
    try:
        raw = store.get_meta(REINFORCE_QUEUE_META)
        queue = json.loads(raw) if raw else []
        if not isinstance(queue, list):
            queue = []
    except Exception:
        queue = []
    prior = next((j for j in queue if j.get("conversation_id") == conversation_id), {})
    queue = [j for j in queue if j.get("conversation_id") != conversation_id]
    queue.append({
        "conversation_id": conversation_id,
        "transcript_path": transcript_path or prior.get("transcript_path", ""),
        "trace": trace or prior.get("trace", ""),
        "at": _now(),
    })
    try:
        store.set_meta(REINFORCE_QUEUE_META, json.dumps(queue[-max_queue:]))
        return True
    except Exception:
        return False


def _bounded_trace(transcript_path: str, max_chars: int) -> str:
    """Read the tail of a transcript file (recent activity is what matters)."""
    import os
    try:
        if not transcript_path or not os.path.exists(transcript_path):
            return ""
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", errors="replace") as fh:
            if size > max_chars:
                fh.seek(size - max_chars)
            return redact_text(fh.read())
    except Exception:
        return ""


def drain_reinforce_queue(store: "Store", config: Config, *, max_jobs: int = 5,
                          client: Any | None = None,
                          max_trace_chars: int = 40000) -> dict:
    """Grade queued sessions. This is where the LLM cost lives — runs in cron,
    off the agent's critical path. Idempotent per conversation.
    """
    if not config.attention.reinforce_enabled:
        return {"status": "disabled", "graded": 0, "pending": 0}
    try:
        raw = store.get_meta(REINFORCE_QUEUE_META)
        queue = json.loads(raw) if raw else []
        if not isinstance(queue, list):
            queue = []
    except Exception:
        queue = []
    if not queue:
        return {"status": "empty", "graded": 0, "pending": 0}

    remaining: list[dict] = []
    graded = 0
    results: list[dict] = []
    for job in queue:
        if graded >= max_jobs:
            remaining.append(job)
            continue
        conv = job.get("conversation_id")
        trace = _bounded_trace(job.get("transcript_path", ""), max_trace_chars) \
            or job.get("trace", "")
        if not conv or not trace.strip():
            continue  # nothing to grade — drop (don't wedge the queue)
        res = reinforce_session(store, config, conv, trace, client=client)
        status = res.get("status")
        if status in ("over_global_budget", "llm_unavailable", "estimate_exceeds_budget"):
            remaining.append(job)  # transient — retry next cron
            continue
        if status == "ok":
            graded += 1
        results.append({"conversation_id": conv, "status": status})

    try:
        store.set_meta(REINFORCE_QUEUE_META, json.dumps(remaining))
    except Exception:
        pass
    return {"status": "ok", "graded": graded, "pending": len(remaining), "results": results}


def learned_pheromone_weight(store: "Store") -> float:
    """The auto-ramped ranking weight (0 if not yet mature). Read by retrieval."""
    try:
        raw = store.get_meta(PHEROMONE_WEIGHT_META)
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


def auto_ramp_pheromone_weight(store: "Store", config: Config) -> dict:
    """Re-evaluate pheromone maturity and ramp the learned ranking weight.

    Pure SQL, no LLM — cheap enough to run on every reinforce and every cron.
    Gate on distinct warm GRADED nodes AND total warm graded signal; then ramp
    linearly toward target between min_signal and full_signal. The measure uses
    decayed strength, so a graph whose trails cooled ramps back toward 0.
    """
    ac = config.attention
    if not ac.pheromone_autoramp_enabled:
        return {"weight": learned_pheromone_weight(store), "ramped": False,
                "reason": "autoramp_disabled"}

    stats = store.pheromone_stats(half_life_days=ac.pheromone_half_life_days)
    nodes = stats["warm_graded_nodes"]
    signal = stats["warm_signal"]

    if nodes < ac.pheromone_min_nodes or signal < ac.pheromone_min_signal:
        weight = 0.0
        reason = f"immature (nodes={nodes}/{ac.pheromone_min_nodes}, signal={signal}/{ac.pheromone_min_signal})"
    else:
        span = max(0.001, ac.pheromone_full_signal - ac.pheromone_min_signal)
        frac = min(1.0, (signal - ac.pheromone_min_signal) / span)
        weight = round(ac.pheromone_target_weight * frac, 4)
        reason = f"mature (nodes={nodes}, signal={signal}, frac={frac:.2f})"

    prev = learned_pheromone_weight(store)
    if abs(weight - prev) > 1e-6:
        store.set_meta(PHEROMONE_WEIGHT_META, str(weight))
    return {"weight": weight, "previous": prev,
            "ramped": abs(weight - prev) > 1e-6, "reason": reason, "stats": stats}

# Grader categories -> which config weight + which counter they drive.
# (weight_attr, deposit_kind) where deposit_kind in {"reinforce", "missed"}.
_CATEGORY_RULES: dict[str, tuple[str, str]] = {
    "used": ("pheromone_reinforce", "reinforce"),
    "user_correction": ("pheromone_correction", "reinforce"),
    "agent_admission": ("pheromone_counterfactual", "missed"),
    "inferred": ("pheromone_counterfactual", "missed"),
}


@dataclass
class ReinforceOutcome:
    node_id: str
    title: str
    category: str
    amount: float
    confidence: float
    evidence: str
    injected: bool  # True = observed (arm A), False = counterfactual (arm B)


def _load_state(store: "Store", conversation_id: str) -> dict[str, Any]:
    from .attention import _load_state as _ls
    return _ls(store, conversation_id)


def _save_state(store: "Store", conversation_id: str, state: dict[str, Any]) -> None:
    from .attention import _save_state as _ss
    _ss(store, conversation_id, state)


def _injected_nodes(store: "Store", state: dict[str, Any]) -> list[dict]:
    """Resolve the graph nodes injected this session (deduped, bare ids)."""
    ids: list[str] = []
    seen: set[str] = set()
    deposits = state.get("pheromone_deposits") or {}
    injected = state.get("injected") or {}
    for raw_id in list(deposits.keys()) + list(injected.keys()):
        bare = injection_node_id(raw_id)
        if bare and bare not in seen:
            seen.add(bare)
            ids.append(bare)
    nodes = []
    for nid in ids:
        node = store.get_node(nid)
        if node:
            nodes.append(node)
    return nodes


def build_reinforce_prompt(trace: str, injected: list[dict], max_chars: int) -> str:
    catalog = [
        {"id": n["id"], "type": n.get("type", ""),
         "title": n.get("title", ""), "content": (n.get("content") or "")[:300]}
        for n in injected
    ]
    return f"""You are grading a finished work session to learn which knowledge is USEFUL when surfaced to an agent. Judge from observable evidence in the trace ONLY — never from your own opinion that something "seems useful". Prefer silence over a weak guess.

SESSION TRACE (what the agent and user actually did):
{trace[:max_chars]}

KNOWLEDGE THAT WAS INJECTED into this session (arm A — did the agent act on each?):
{json.dumps(catalog, ensure_ascii=False)}

Produce findings of two kinds.

ARM A — for each injected item, decide:
  - "used": the agent demonstrably acted on it (quote the evidence). category="used".
  - if the USER corrected the agent on the very thing this item warns about, category="user_correction" (this is the strongest signal).
  - otherwise omit it (ignored items must NOT appear — silence lets them decay).

ARM B — knowledge that WOULD have helped but was NOT in the injected list. Tie each to concrete evidence in the trace:
  - "user_correction": the user corrected/pushed back and a piece of durable knowledge would have prevented it. (heaviest)
  - "agent_admission": the agent itself said something like "I should have…", "I forgot to…". (a real but lighter signal)
  - "inferred": an error, rework, or wrong turn that durable knowledge would have prevented, with no explicit correction/admission.
  For each ARM B finding give a short search "query" naming the knowledge that was missing.

Return JSON only:
{{
  "observed": [
    {{"id": "<injected item id>", "category": "used|user_correction", "confidence": 0.0, "evidence": "quote from trace"}}
  ],
  "missed": [
    {{"category": "user_correction|agent_admission|inferred", "query": "what knowledge was missing", "confidence": 0.0, "evidence": "quote from trace"}}
  ]
}}"""


def _parse(text: str) -> dict[str, Any]:
    from .attention import _parse_json_response
    return _parse_json_response(text)


def reinforce_session(
    store: "Store",
    config: Config,
    conversation_id: str,
    trace: str,
    *,
    client: Any | None = None,
    ledger: BudgetLedger | None = None,
) -> dict:
    """Grade a finished session and deposit usefulness signal across sessions.

    Idempotent per conversation: a session is graded at most once (guarded by
    state['reinforced_at']). Returns an accounting dict; never raises for
    advisory failures.
    """
    if not config.attention.reinforce_enabled:
        return {"status": "disabled", "outcomes": []}
    if not conversation_id or not (trace or "").strip():
        return {"status": "no_trace", "outcomes": []}

    state = _load_state(store, conversation_id)
    if state.get("reinforced_at"):
        return {"status": "already_reinforced", "outcomes": []}

    injected = _injected_nodes(store, state)

    ledger = ledger or BudgetLedger(config.ledger_path, config.budget)
    if not ledger.can_spend():
        return {"status": "over_global_budget", "outcomes": []}

    if client is None:
        from .llm import get_client
        client = get_client(config)
    if client is None:
        return {"status": "llm_unavailable", "outcomes": []}

    prompt = redact_text(build_reinforce_prompt(
        redact_text(trace), injected, config.attention.max_context_chars * 3))

    from .llm import estimate_cost
    est = estimate_cost(config.llm.model, len(prompt) // 4,
                        config.attention.max_output_tokens * 2)
    if est > config.attention.reinforce_max_cost:
        return {"status": "estimate_exceeds_budget", "estimate": est, "outcomes": []}

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=config.attention.max_output_tokens * 2,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm import calculate_cost
        cost = calculate_cost(config.llm.model, response.usage)
        ledger.record(
            cost["amount"], model=config.llm.model, purpose=REINFORCE_PURPOSE,
            tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
            cache_creation_tokens=cost.get("cache_creation_tokens", 0),
            cache_read_tokens=cost.get("cache_read_tokens", 0),
            conversation_id=conversation_id,
        )
        parsed = _parse(response.content[0].text)
    except Exception as exc:
        return {"status": "llm_error", "error": str(exc), "outcomes": []}

    ctx = pheromone_context(config)
    half = config.attention.pheromone_half_life_days
    floor = config.attention.reinforce_min_confidence
    outcomes: list[ReinforceOutcome] = []
    gaps: list[str] = []

    injected_by_id = {n["id"]: n for n in injected}

    # ── ARM A: observed reinforcement ───────────────────────────────────
    for item in parsed.get("observed", []) or []:
        if not isinstance(item, dict):
            continue
        bare = injection_node_id(str(item.get("id") or ""))
        node = injected_by_id.get(bare) if bare else None
        if not node:
            continue
        category = str(item.get("category") or "used")
        if category not in _CATEGORY_RULES:
            category = "used"
        conf = _confidence(item)
        if conf < floor:
            continue
        _deposit(store, config, node["id"], ctx, category, half)
        outcomes.append(ReinforceOutcome(
            node["id"], node.get("title", ""), category,
            _amount(config, category), conf,
            str(item.get("evidence", ""))[:200], injected=True))

    # ── ARM B: counterfactual / missed injection ────────────────────────
    for item in parsed.get("missed", []) or []:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        category = str(item.get("category") or "inferred")
        if category not in _CATEGORY_RULES:
            category = "inferred"
        conf = _confidence(item)
        if conf < floor:
            continue
        matches = _match_missing(store, query, config.attention.reinforce_counterfactual_top_k)
        if matches:
            for node in matches:
                if node["id"] in injected_by_id:
                    continue  # it WAS injected — not a miss
                _deposit(store, config, node["id"], ctx, category, half)
                outcomes.append(ReinforceOutcome(
                    node["id"], node.get("title", ""), category,
                    _amount(config, category), conf,
                    str(item.get("evidence", ""))[:200], injected=False))
        elif config.attention.reinforce_gap_as_question:
            # A real need that matches no node — that's a knowledge gap.
            gaps.append(query)
            store.add_suggestion(
                concept_a=query, concept_b="(missing)",
                reason=f"Would have helped this session but no node covers it: {str(item.get('evidence',''))[:160]}",
                source=f"reinforce:{conversation_id}")

    # ── Learned PAIR co-activation ──────────────────────────────────────
    # Only nodes the grader CONFIRMED were used, and only from ARM A (things
    # actually injected). Co-retrieval is not usefulness — depositing on it
    # would teach the graph the retriever's own biases and call that evidence.
    # Counterfactual "missed" outcomes are excluded: they were never injected
    # together, so there is no co-activation to observe.
    coactivated = _deposit_coactivation(store, config, outcomes, ctx)

    state["reinforced_at"] = _now()
    state["reinforce_counts"] = {
        "observed": sum(1 for o in outcomes if o.injected),
        "counterfactual": sum(1 for o in outcomes if not o.injected),
        "gaps": len(gaps),
    }
    _save_state(store, conversation_id, state)

    # New signal just landed — re-evaluate maturity and ramp the ranking weight.
    ramp = auto_ramp_pheromone_weight(store, config)
    co_ramp = auto_ramp_coactivation_weight(store, config)

    return {
        "status": "ok",
        "outcomes": [vars(o) for o in outcomes],
        "gaps": gaps,
        "injected_count": len(injected),
        "pheromone_weight": ramp.get("weight"),
        "pheromone_ramped": ramp.get("ramped"),
        "coactivated_pairs": coactivated,
        "coactivation_weight": co_ramp.get("weight"),
        "coactivation_ramped": co_ramp.get("ramped"),
    }


def _confidence(item: dict) -> float:
    try:
        return float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _amount(config: Config, category: str) -> float:
    attr, _ = _CATEGORY_RULES[category]
    return float(getattr(config.attention, attr))


def _deposit(store: "Store", config: Config, node_id: str, ctx: str,
             category: str, half_life: float) -> None:
    _, kind = _CATEGORY_RULES[category]
    amount = _amount(config, category)
    kwargs = {"amount": amount, "half_life_days": half_life,
              "reinforce": kind == "reinforce", "missed": kind == "missed"}
    try:
        store.deposit_pheromone(node_id, context="", **kwargs)
        if ctx:
            store.deposit_pheromone(node_id, context=ctx, **kwargs)
    except Exception:
        pass


def _deposit_coactivation(store: "Store", config: Config,
                          outcomes: list["ReinforceOutcome"], ctx: str) -> int:
    """Deposit pair co-activation across CONFIRMED-USED injected nodes.

    Returns the number of pairs deposited. Only ``injected=True`` outcomes in
    the reinforce categories qualify: a counterfactual "this would have helped"
    node was never injected, so there is no co-activation to observe, and
    co-retrieval alone is not evidence of anything.

    Pair count is quadratic in the confirmed set, so it is capped — a session
    that used thirty nodes should not write 435 rows and swamp the channel.
    """
    if not getattr(config.attention, "coactivation_enabled", False):
        return 0

    used = [o.node_id for o in outcomes
            if o.injected and _CATEGORY_RULES.get(o.category, ("", ""))[1] == "reinforce"]
    # Deterministic order so a capped session always deposits the same pairs.
    used = sorted(set(used))
    if len(used) < 2:
        return 0

    eta = config.attention.coactivation_eta
    half = config.attention.coactivation_half_life_days
    cap = config.attention.coactivation_max_pairs

    deposited = 0
    for i, a in enumerate(used):
        for b in used[i + 1:]:
            if deposited >= cap:
                return deposited
            try:
                store.deposit_coactivation(a, b, context="", eta=eta,
                                           half_life_days=half)
                if ctx:
                    store.deposit_coactivation(a, b, context=ctx, eta=eta,
                                               half_life_days=half)
                deposited += 1
            except Exception:
                log.warning(
                    "co-activation deposit failed for pair (%s, %s) — the "
                    "learned pair channel is not recording", a, b)
    return deposited


def auto_ramp_coactivation_weight(store: "Store", config: Config) -> dict:
    """Lift co-activation into ranking once the channel is warm.

    Mirrors the pheromone ramp, and keeps the same separation it does: the
    RAW signal lives in ``node_coactivation``; the APPLIED correction is this
    ranking weight. Keeping them apart is what lets you retire the channel
    without rewriting history, and what stops a learned weight from hiding the
    thing it measures.
    """
    acfg = config.attention
    if not getattr(acfg, "coactivation_autoramp_enabled", False):
        return {"weight": 0.0, "ramped": False}
    stats = store.coactivation_stats(
        half_life_days=acfg.coactivation_half_life_days)
    if (stats["warm_pairs"] < acfg.coactivation_min_warm_pairs
            or stats["signal"] < acfg.coactivation_min_signal):
        store.set_meta("coactivation.learned_weight", "0")
        return {"weight": 0.0, "ramped": False, **stats}

    span = max(1e-9, acfg.coactivation_full_signal - acfg.coactivation_min_signal)
    frac = min(1.0, (stats["signal"] - acfg.coactivation_min_signal) / span)
    weight = round(acfg.coactivation_target_weight * frac, 4)
    store.set_meta("coactivation.learned_weight", str(weight))
    return {"weight": weight, "ramped": weight > 0, **stats}


def learned_coactivation_weight(store: "Store") -> float:
    """Read the auto-ramped co-activation ranking weight (0 when immature)."""
    try:
        return float(store.get_meta("coactivation.learned_weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _match_missing(store: "Store", query: str, top_k: int) -> list[dict]:
    try:
        from .retrieve import hybrid_search
        return hybrid_search(store, query, top_k=top_k, expand_graph=False)
    except Exception:
        return []


def _now() -> str:
    from .store import _now as store_now
    return store_now()
