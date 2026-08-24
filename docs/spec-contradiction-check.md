# Spec: `contradiction_check` — structured contradiction detection (v1 contract)

**Status:** Specification only — no implementation is authorized by this document.
**Date:** 2026-08-25.
**Source:** `docs/prd-lineage-grounding-2026-08.md` R2 + R2b, as revised by the
2026-08-24 review outcome (point 3), which is authoritative: R2 (`ground`) and R2b
(standing conflict sweep) merge into one item, renamed `contradiction_check`.

## Why this tool, and why this name

`ask` is retrieval: "what relates to X?" It cannot answer "is claim X contradicted —
and by exactly what?" The rename from `ground` is honest labeling: from kindex's side,
`supported` and `unknown` are the **same epistemic state** — kindex is context, never
authority, so graph support authorizes nothing. The entire value of the tool is the
`contradicted` branch: a cheap, early, machine-readable denial signal (the
refutation-only doctrine that makes it safe to wire into factory pre-checks).

## v1 input contract — a structured triple, never prose

Free-text claim parsing without a model is unimplementable as specified (review
finding); model-assisted parsing may come later **behind this same contract**.

```json
{
  "subject":   {"id": "<node id>"} | {"title": "<title or alias>"},
  "predicate": "<relationship word, e.g. depends_on, implements, relates_to>",
  "object":    {"id": "<node id>"} | {"title": "<title or alias>"}
}
```

- Entity resolution: exact id, else case-insensitive title, else `aka` alias — the
  existing alias machinery, no new resolver.
- The predicate vocabulary is the existing edge-type vocabulary plus free-form
  relationship words; an unrecognized predicate is not an error (it simply finds no
  supporting edge and cannot force `contradicted` on its own).

## Output contract

```json
{
  "verdict": "supported" | "contradicted" | "unknown",
  "confidence": <float 0..1, present ONLY when verdict == "contradicted">,
  "supporting_paths": [<edge ids>],
  "contradicting_nodes": [<admitted node ids>],
  "missing_edges": [["<subject>", "<predicate>", "<object>"]],
  "candidates": {"subject": [...], "object": [...]},
  "overrides": [<logged override records for this claim digest>]
}
```

- **Edge ids are not yet stable citations.** v1 emits the store's current edge ids;
  stable, citable edge identifiers (and their retrofit into `search`/`ask` output) are
  a **separate, additive-only future line item** — nothing in this contract may block
  on it, and consumers must not persist v1 edge ids as durable references.

## Verdict semantics

| Verdict | Meaning | Authority effect |
|---|---|---|
| `contradicted` | At least one **admitted** node/edge contradicts the claim | A cheap early denial signal — the only branch with decision value |
| `supported` | Admitted edges match the triple; nothing admitted contradicts | **Authorizes nothing.** Same epistemic state as `unknown` from kindex's side |
| `unknown` | No admitted evidence either way, or ambiguous entities | Authorizes nothing |

**Admission is the existing predicate, not a new one:** a node may contradict only if
`trust.node_trust_decision` admits it — active, verify-passed, currently valid,
non-invalidated, **not demoted for a stale referent** (R0: a claim whose referent
moved loses its power to contradict until re-verified), and not itself suppressed.
This is where kindex is already ahead of the source paper; the tool reuses it
wholesale.

### Verdict precedence (mixed evidence)

**Any admitted contradicting node forces `contradicted`** — regardless of how much
supporting evidence exists, its weight, or its recency. Rationale: the tool is a
refutation channel; averaging support against refutation would convert it into an
authorization channel by the back door. Supporting evidence is still reported in
`supporting_paths` so the human sees the tension.

### Ambiguous entities

If subject or object resolves to **two or more** candidate nodes, the verdict is
`unknown` with the `candidates` map populated (ids + titles + types) and no
confidence value. The tool never guesses an entity; adversarial alias collisions are
a gold-set case, not a heuristic.

## Confidence

Present **only** on the `contradicted` branch (the other branches would dress
non-authority in false precision). Deterministic given graph state — v1 uses **no
model call**. It must be monotonic in:

- verification recency of the contradicting node(s);
- R0 referent freshness of the contradicting node(s) (a fresh-referent contradictor
  outranks an unbound one; a stale one is already inadmissible);
- weight of the contradiction edge(s);
- resolution directness (direct `contradicts` edge > derived path).

Exact coefficients are an implementation decision; the monotonicity constraints and
determinism are contract.

## Logged human override path

Doctrine without an enforcement mechanism erodes (review finding). When a human
rejects a verdict:

- The override is **recorded**: activity-log entry (`contradiction_override`)
  carrying the claim-triple digest, the verdict overridden, the actor, the reason,
  and the timestamp.
- The tool **never silently flips**: subsequent runs of the same claim digest return
  the machine verdict unchanged, with the override record(s) attached in
  `overrides`. A human reads both; an agent consuming the tool must treat an
  unresolved verdict/override disagreement as human-escalation, not as either answer.
- Overrides expire with the evidence they judged: any change to the admitted
  contradicting set for that claim clears the attachment (the override was about a
  specific evidence state).

## The standing sweep — single demotion authority

`contradiction_check` is on-demand and **never demotes anything**. The standing
conflict sweep (R2b) is NOT a kindex daemon feature written from scratch:

- **meditate's Analyst** (v0.5: evidence-grounded, read-only, nomination-only — it
  cannot draft, choose, or write) is pointed at kindex's additive node classes
  (decisions / constraints / directives) as a target class.
- Findings land as kindex **candidates** (the existing quarantine) — never as direct
  mutations. Human review at the candidate gate decides supersession/invalidation.
- **The Analyst nomination gate is the SINGLE authority for contradiction-driven
  demotion nominations.** A second independent demotion authority would let two
  systems disagree about trust state (review finding). `contradiction_check` reports;
  the Analyst nominates; a human disposes.
- Boundary with R0 (already landed): the mechanical stale-referent sweep is a
  *different, non-overlapping channel* — it demotes on a measured digest divergence,
  no semantic judgment involved. Semantic contradiction demotion has exactly one
  path: Analyst nomination → candidate → human.
- Carry-over constraint: meditate's fail-closed secret-sanitization boundary (kindex
  constraint `5e48daf20c91`) applies to any evidence packet that includes node
  content.

## Non-goals (v1)

- No prose/claim parsing and no LLM calls — pure graph traversal behind this
  contract.
- No change to the authority doctrine: `supported` authorizes nothing, ever.
- No demotion, supersession, or invalidation performed by the tool.
- No stable edge-identifier guarantee (separate future line item).
- No second DAG or new resolver machinery.

## Acceptance criteria (for the future implementation, not this document)

1. A checked-in gold set of claims with known supported / contradicted / unknown
   verdicts, **including adversarial alias cases**, passes with correct cited paths.
2. Precedence: a claim with 10 admitted supporting edges and 1 admitted
   contradicting node returns `contradicted`.
3. Admission: an unverified, invalidated, expired, or stale-referent-demoted
   contradictor cannot force `contradicted` (each denial reason its own test).
4. Ambiguity: an aliased entity resolving to two nodes returns `unknown` +
   candidates, no confidence.
5. Override: recorded, surfaced on re-run, cleared when the evidence set changes;
   the machine verdict itself never flips.
6. `ask` and `search` output remain byte-unchanged (this is a new tool, not a
   retrofit).
7. Confidence monotonicity property tests over synthetic graphs.

## Open questions (carried, not resolved here)

- Predicate negation: should the triple support an explicit `negated` flag so
  "A does NOT depend on B" is checkable directly, or is that v2 surface?
- Should `contradiction_check` accept a batch of triples (factory pre-check
  ergonomics) in v1, or is one-claim-per-call enough?
- Retention policy for override records (forever vs expiry-with-archive) — same
  open question R3 carries for merge receipts.
