# PRD: Lineage Provenance, Grounded Claims, Merge Receipts, Graph Metrics

**Status:** Proposal (not scheduled). **Date:** 2026-08-24.
**Source:** Gap analysis of kindex against "Graph Engineering: The Karpathy Loop"
(unaffiliated July 2026 synthesis of Karpathy's autoresearch/AgentHub and Anthropic's
Dynamic Workflows + Knowledge Graph Construction Cookbook), revised after founder review.

## Context for reviewers

Kindex is a persistent knowledge graph (SQLite store, MCP server, hybrid FTS5+graph
search) used as external memory across Claude sessions. Typed nodes (concept, decision,
question, task, skill, constraint, directive, watch, checkpoint), typed weighted edges
with provenance reasons, admission control (`verify`/`invalidate`/`trusted_only`),
quarantined automatic captures, session tags, and a per-repo `.kin/` export
(`index.json` + `knowledge.jsonl`) that travels in git. A custom structured merge
driver (`kin_merge.py`) already handles `.kin` merges; the authoritative SQLite DB is
**not** in git — `.kin` is a projection of it.

The factory (`~/Code/factory`) is a separate evidence-ledger build system whose runs
currently die on commissioning defects and intent-authoring gaps; a standing need is a
queryable record of *why runs die* (gate-failure Pareto) so findings route into repair.

Founder corrections already applied to this proposal:

1. "Kindex has no DAG" was wrong as stated. `.kin/` lives inside a git repo: the work
   DAG exists and kindex already ships merge tooling for its projection. The actual gap
   is narrower — no *edges between* the knowledge layer and the lineage layer.
2. `ask` vs `ground` needs a precise, implementable difference, not a slogan.
3. Reversibility claims must account for `.kin`-in-git already existing.
4. Graph-quality measurement may not belong in kindex at all.

Founder additions (round 2): R0 (referent binding, two clocks) as lead priority — "the
difference between a graph that remembers and a graph that's right"; R2b (standing
contradiction detection); R3 endorsed as-is. None urgent relative to the factory
commissioning problem.

**Question for reviewers:** for each R0–R4, is this a *material* improvement to kindex,
and does it belong in kindex or elsewhere (the factory, or nowhere)?

## Review outcome (2026-08-24) — authoritative over the sections below

Reviewed by Advocate (six personas, claude-sonnet-5) and the Jeremy-simulacrum
(claude-sonnet-5). Convergent verdicts; the sections below are preserved as reviewed,
this section states the revised plan.

Materiality criterion (simulacrum): *a change is material if kindex currently produces
a wrong or stale answer a consumer can't detect, and the change makes that failure
detectable or impossible; anything that adds structure without changing what kindex can
be wrong about is resume-driven.*

1. **R0 ships first, unconditionally.** Both reviewers independently: the only item
   fixing a correctness bug ("kindex lying to you silently"); Advocate's top finding —
   commit digests are unstable under rebase/squash/force-push — is *solved by* R0's
   content-hash anchoring (digest primary, commit as hint, never ground truth). Two
   clocks stay: staleness is a divergence measurement, not a boolean.
2. **R1 shrinks to provenance pointers only: `{repo, commit_digest}` (content-anchored),
   optionally `run_id`/`session_id` as opaque strings.** The `experiment` node type and
   gate/disposition vocabulary are **cut** from kindex — three independent findings
   (Sage, SME, simulacrum) converge: factory vocabulary in the fact store violates R4's
   own separation principle; the experiment/run graph is the factory's log, queried in
   the factory. Kindex holds facts with pointers, not other systems' process taxonomy.
   (Advocate's transient-vs-substantive run-death classification requirement therefore
   also lands in the factory — it is the flake-attribution problem again.)
3. **R2 and R2b merge into one item, renamed `contradiction_check`.** Simulacrum:
   `supported` and `unknown` are the same epistemic state from kindex's side — the
   value is the `contradicted` branch, so name it honestly. v1 input contract is a
   structured triple, not prose (Advocate: entity extraction from free text without a
   model is unimplementable as specified). Required additions: defined verdict
   precedence for mixed evidence (any admitted contradiction wins), a confidence field,
   a logged human override path (doctrine without an enforcement mechanism erodes),
   an ambiguous-entity outcome (`unknown` + candidates), and one authority path —
   the standing sweep rides meditate's Analyst nomination gate; a second independent
   demotion authority would let two systems disagree about trust state.
   Edge-ID retrofit of `search`/`ask` splits into its own additive-only line item.
4. **R3 confirmed material** ("the absence of an undo button on a destructive
   operation") with hardening required: merge + receipt atomic in one transaction;
   receipts carry edge-level diffs (node/alias metadata is insufficient for lossless
   reversal); reversal refuses or reconciles when the surviving node was touched since
   (including chained merges); `merge_reverse` must be R0-aware (restore per-node clock
   state, never average it). **Immediate stopgap, independent of R3's timeline:**
   a pre-merge DB snapshot or confidence circuit-breaker on `dream`/`graph_heal`
   auto-merges — the corruption path is live today.
5. **R4 is conditional on a second consumer.** The split pays only if something besides
   the factory reads the instrument data. Candidate second consumers exist today —
   meditate (kindex is already a required evidence source) and session priming — so
   the split likely stands, but name the consumer before building the boundary.
6. **Prerequisite for any schema change:** a `.kin` schema-version marker and
   unknown-field passthrough in `kin_merge.py`, with a mixed-version merge test —
   older drivers must not corrupt newer fields.

Revised order: R0 → contradiction_check (R2+R2b) → R3 (gated on R0) → R4 (if the
second consumer is named) → edge-ID retrofit. Cut: R1's experiment graph (re-litigate
only if the factory's own run log proves insufficient).

## R0 — Bind assertions to their referent (founder addition; lead priority)

Every node describing code (or any external referent) carries a content hash of what it
describes, so the fact goes *verifiably* stale the moment the referent moves. Two clocks,
not one: `asserted_at` (when the claim was made) and `true_of` (the referent state —
content digest, or commit for repo-scope claims — it was true of). Today the record
proves who claimed what and nothing detects a claim that was true when made and has
since gone false. This is the derived-not-asserted move (already applied to factory
independence tiers) aimed at context instead of authority: staleness becomes a
computable property, not a `[verify: may be outdated]` heuristic.

- Data-model change first: optional `referent: {path|url, content_digest, digest_scope}`
  plus the two timestamps, on any node type.
- A cheap `stale` check (daemon or on-recall): re-hash the referent; mismatch demotes
  the node from trusted recall (integrates with existing `trusted_only`) and surfaces
  it as a re-verification candidate — it does not delete or rewrite history.
- `.kin` artifact nodes (the code-module index) adopt this first; they already name
  files but carry no digest.

Distinct from R1: R1 answers "what produced/evidences this claim" (lineage into the
git DAG); R0 answers "is this claim still true of the thing it describes" (freshness
against the referent). R0 is the difference between a graph that remembers and a graph
that is right.

## R1 — Lineage provenance: connect the graph to the DAG it already lives in

**Not** "build a DAG." Git is the DAG. The gap: a kindex node cannot cite the commit,
run, or session that produced or evidences it, and there is no node type for an
experiment/run outcome. `knowledge.jsonl` entries are flat
`{audience, content, tags, title, type}` — runs are mentioned in prose, unqueryably.

Proposal:

- Optional `provenance` field on nodes: `{repo, commit_digest, run_id, session_id}`.
- New node type `experiment` (managed-class): hypothesis, configuration digest, metric,
  disposition (`kept`/`discarded`/`died:<gate>`), one per run/attempt.
- New edge types: `produced_by` (claim → experiment/commit), `evidenced_by`,
  `refuted_by`.
- `.kin` export carries provenance fields so the projection stays lossless.

Queries this enables (impossible today): "which runs produced this claim," "which gate
kills the most factory runs" (the gate-failure Pareto becomes one query), "which
lineages stagnated," "which claims lost their producing commit" (staleness detection).

Acceptance: an experiment node auto-created per factory run death with its killing gate;
Pareto query returns correct counts on a synthetic fixture; export/import round-trips
provenance.

## R2 — `ground(claim)`: structured verification distinct from `ask`

`ask` today (`mcp_server.py:976`) is retrieval + formatting: keyword-classify the
question, `hybrid_search`, return a formatted context block. It answers "what is
related to X?" It cannot answer "is claim X supported, contradicted, or unevidenced —
and by exactly what?"

Proposal — new MCP tool `ground(claim)`:

- Resolve the claim's entities against the graph (aliases included).
- Return machine-readable JSON:
  `{verdict: supported|contradicted|unknown, supporting_paths: [edge ids],
  contradicting_nodes: [verified node ids], missing_edges: [entity, predicate, entity]}`.
- Only `verify`-admitted, non-invalidated nodes may contradict (reuse the existing
  admission control; this is where kindex is already ahead of the source paper).
- Stable edge identifiers become citable in output (also retrofit `search`/`ask`).

Authority doctrine (factory compatibility): **refutation-only**. A `contradicted`
verdict is a cheap early denial signal; a `supported` verdict authorizes nothing —
kindex remains context, never authority. This asymmetry is what makes the tool safe to
wire into factory pre-checks.

Acceptance: a gold set of claims with known supported/contradicted/unknown verdicts,
including adversarial alias cases; `ground` returns correct verdicts with correct cited
paths; `ask` output unchanged.

## R2b — Contradiction detection as a standing query (founder addition)

`ground` checks one claim on demand. Separately, nothing today *looks for* conflicts:
two contradicting nodes are both served to recall, and a stale architectural claim
misleads every downstream agent identically — the one kindex failure mode with real
blast radius. Proposal: a standing conflict sweep (daemon or `kin doctor`) that
surfaces contradicting node pairs ranked by attestation strength (verification state,
provenance, R0 freshness), so the better-attested node wins recall and the loser
becomes a supersession/invalidation candidate.

**Build note — do not write this from scratch.** `~/Code/meditate` (v0.5) already ships
an evidence-grounded read-only Analyst that nominates exactly these classes —
contradiction, supersession, under/over-specification — over directive prose, with
local citation validation and a nomination-only boundary (the Analyst cannot draft,
choose, or write). It already consumes kindex as a required evidence source. The
cheapest correct shape: point meditate's Analyst at kindex's additive nodes
(decisions/constraints/directives) as a target class, with findings landing as kindex
*candidates* (the existing quarantine), never as direct mutations. Kindex's job is to
store and rank the conflict; meditate's job is to find it.

Carry-over constraint: meditate's fail-closed secret-sanitization boundary (kindex
constraint `5e48daf20c91`) applies to any evidence packet that includes node content,
even though curated nodes are lower-risk than raw history.

## R3 — Merge receipts in the store layer (narrowed)

`.kin`-in-git already gives history and rollback **for the projection**, via the
existing `kin merge-kin` driver. But `graph_merge`, `dream`, and `graph_heal` mutate
the SQLite DB, which is not in git: a false entity merge there contaminates every
downstream traversal and is not recoverable from git without lossy re-ingestion.

Proposal (smaller than the paper's version, because git covers the rest):

- Every automated merge writes a receipt: surviving id, absorbed ids, retained aliases,
  rationale, confidence, initiating run/session.
- A `merge_reverse(receipt_id)` operation restores the absorbed node and its edges.
- Metric: false-merge rate over sampled receipts; a component-count collapse alerts as
  possible over-merging.

Acceptance: merge → reverse round-trips a synthetic fixture losslessly; receipts appear
in `changelog`; dream/heal merges carry receipts with no API change.

## R4 — Graph quality measurement: split placement

Answering "should this live in kindex": **the instruments yes, the improvement loop no.**

- In kindex: a gold set for `learn`/`ingest` extraction (entity/relation F1,
  schema-valid response rate), resolution metrics (pairwise precision, compression
  ratio, false-merge rate from R3), and daemon trend monitors (isolated-node spike =
  resolution regression; sudden component collapse = over-merging). Kindex must be
  *measurable* by its own test suite.
- In the factory: the "graph autoresearch" ratchet loop that tunes extraction prompts
  and ontology against that gold set is a bounded-change/measurable-metric/keep-or-
  revert process — exactly a factory run with kindex as a data-only target pack. It
  should not be a kindex daemon feature.

Acceptance (kindex side only): `make eval` reports extraction F1 and resolution
precision against the checked-in gold set; trend monitors fire on synthetic
regressions.

## Non-goals

- No second DAG structure inside kindex (git is the DAG; R1 only adds references into it).
- No change to the authority doctrine: kindex informs, signed artifacts authorize.
- No LLM calls inside `ground` v1 (pure graph traversal; model-assisted claim parsing
  can come later behind the same contract).

## Open questions

- Should `provenance.commit_digest` bind whole-repo state or file-level digests?
- Does `experiment` belong in the main store or a per-repo `.kin` shard only?
- Receipt retention policy for R3 (forever vs expiry-with-archive)?
