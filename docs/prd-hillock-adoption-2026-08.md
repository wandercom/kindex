# Hillock adoption plan for Kindex

**Status:** IMPLEMENTED on `feat/hillock-adoption` · **Date:** 2026-08-31 · **Baseline:** kindex 0.33.0 (`676214f`)
**Subject:** [Hillock](https://github.com/roandejager/Hillock) v0.6.0 — TALON extraction, HYDRA gating, VSA hypervectors, Hebbian plasticity

---

## 1. Headline

Three of Hillock's four proposals target problems Kindex does not have. The fourth targets a
real, already-known Kindex defect that a ~150-line change fixes without importing any of
Hillock's machinery.

Measured on the live graph today (`~/Personal/Conv/kindex.db`, 1.34 GB):

| Measurement | Value |
|---|---|
| Active nodes | 23,866 (28,797 total) |
| Edges | 113,355 (avg degree 7.8, mean out-fanout 5.0, max 849) |
| Connected components | 6,563 — the graph is heavily fragmented |
| **Nodes with embeddings** | **393 — 1.6% coverage** |
| **`injection_pheromone` rows** | **0 — the reinforcement loop has never deposited** |
| `capture_candidates` rows | 0 — the quarantine lane is unused |
| 1-hop expansion (today's behavior) | 28 ms |
| 2-hop recursive CTE | 27 ms |
| 3-hop recursive CTE (496 nodes) | 29 ms |

Traversal timings include `sqlite3` process startup; the three are indistinguishable from
each other and from noise.

**Kindex's condition is not missing mechanisms. It is mechanisms that are built and not
running.** The semantic channel covers 1.6% of the graph. The stigmergic learning engine —
Kindex's own, stronger answer to Hebbian plasticity — has fired zero times despite being
enabled by default with three call sites. Adding a VSA reservoir and a torch-based extractor
on top of that would produce a fourth subsystem that does not run.

---

## 1a. As built

Everything below shipped except the TALON engine itself, which is deliberately
left for a measured decision (see W2). Tests: **1,926 passing**.

| Item | Outcome |
|---|---|
| W0a embed backfill | **393 → 23,746 embedded** (1.6% → ~99.5%). Cost **$0.42**, not $5.75, after cleanup |
| W0b pheromone loop | **Root-caused and fixed** — see below; it was never a wiring gap |
| W1 grounding gate | Shipped. Live floor **0.2149**; real queries score 0.48–0.56, null queries 0.17–0.19 |
| W3 multi-hop | Shipped. **3 hops in 0.7 ms**, 4.8× the reach of 1 hop |
| W4 co-activation | Shipped, inert until warm, never touching `edges.weight` |
| W2 | Gate + boundary shipped; the 2.5 GB engine deliberately **not** built |
| W5 panel models | Constrain → `claude-opus-5`; Simulacrum → sonnet-5/opus-5; retired pin removed |
| W6 remote | Confirmed clean — nothing to resolve |

### Four defects found along the way

The plan said Kindex's problem was mechanisms built and not running. That turned
out to understate it — the causes were individually diagnosable, and all four
shared a shape: **a failure that reported success.**

1. **The pheromone loop was dead on every upgraded install.** The `missed`
   column was added to the v7 `CREATE TABLE IF NOT EXISTS` *after* v7 shipped,
   so any store that had already run v7 never got the column and never would.
   `deposit_pheromone` raised `no such column: missed`, the attention hook
   swallowed it with `except Exception: pass`, and the session state recorded
   the deposit as successful anyway — for three months, across 157 sessions.
   Fixed by a v10 migration, a column-level (not table-level) drift check in
   `kin doctor`, and replacing the silent swallow with a logged, counted
   failure that no longer lets state claim a deposit the store refused.

2. **Five nodes held 86% of the corpus by bytes.** `dream.merge_nodes` appends
   source content into the target with no cap, and `content_overlap` compares
   only the first 500 chars — where generated files are identical. Minified
   Astro symbols (`class Ha`, `class Za`), one handler class defined in twenty
   files, a vendored LICENSE, and generated Prisma schemas are all mutually
   similar *by construction*, so each merge was a false positive that grew the
   target. The result was a 35 MB "concept". Fixed with size and absorption
   caps that refuse rather than truncate, and the five nodes archived — which
   is what took the backfill from $5.75 to $0.42.

3. **LLM extraction was dead too.** `extract.py` did a bare
   `os.environ.get(config.llm.api_key_env)` while `llm.py` correctly parsed the
   comma-separated fallback list the config documents, so a config of
   `"JMC_OPENAI_API_KEY,OPENAI_API_KEY"` looked up an env var of that literal
   name and silently fell back to keyword extraction forever. It also hardcoded
   the Anthropic SDK while ignoring `llm.provider`. Both were the same shape:
   two authorities for one fact. Fixed by delegating to `llm.py`.

4. **My own first grounding gate would have been the fifth.** `sqlite-vec`'s
   `vec0` returns **L2 distance, not cosine**, so the obvious `1 - d`
   conversion clamped the entire useful range (0.94–1.32) to zero, calibrated a
   floor of exactly `0.0`, and produced a gate that could never fire. Caught
   because the calibration printed a suspiciously round number. The correct
   identity for unit-normalised vectors is `cos = 1 - d²/2`.

---

## 2. What Hillock's own numbers say

Self-reported for v0.6.0, over one fixed 32-sentence document, 22 answerable + 10 hard-negative
queries:

| Metric | Hillock v0.6.0 |
|---|---|
| Extraction precision | **13.8%** |
| Extraction recall | 59.1% |
| Retrieval accuracy | 54.5% |
| Hard-negative block rate | 60.0% |
| Pooled gate accuracy | 56.2% |

The author is candid: *"directional, not final"*, *"not enough to claim statistical robustness
yet."* Take him at his word. 13.8% precision means roughly six of every seven extracted triples
are wrong. For a graph whose entire value proposition is that you can trust what it says, that
is not an ingestion engine — it is a candidate generator, and it must be placed where a
candidate generator belongs.

**The architectural ideas are worth stealing. The measured quality is not evidence for them.**

Two hard blockers on the code itself:

- **AGPL-3.0 vs Kindex's MIT.** Nothing may be vendored. Ideas are not copyrightable;
  implementations are. Clean-room only.
- **~2.5 GB of dependencies.** PyTorch + spaCy + GLiREL-Large + fastcoref, against Kindex's
  current pure-Python core plus optional `sqlite-vec`. This is dispositive for anything
  proposed as core.

---

## 3. Helland framing — one authority per fact

The rule that generates the rest of this document: when the design is hard, you are missing an
*authority*, not a service. Name the owner and the tangle resolves.

| Fact | Authority | Hillock's placement | Correct placement |
|---|---|---|---|
| "This claim is true" | the node, under `verify` / `--referent` binding | TALON writes triples directly | Extraction is an **input**, never an authority → `capture_candidates` |
| "A path exists A→B" | the `edges` table | VSA reservoir answers multi-hop | A reservoir is a **cache**. Caches narrow; authorities decide. |
| "The graph knows nothing here" | retrieval, about **its own** confidence | HYDRA hard-refuses before the LLM | Adopt. Retrieval is authoritative about itself, not about what the caller should do. |
| "This node was useful here" | `injection_pheromone` — outcome-grounded | Hebbian co-occurrence | Already owned, and Kindex's version is **stronger**. |

### The pushback on Hebbian plasticity

Hillock strengthens a link because two nodes were *retrieved together*. Kindex strengthens
because the agent *demonstrably used* the injection, graded: deposit 1.0, confirmed use 3.0,
user correction 4.0, counterfactual admission 1.5, with a 14-day half-life and an auto-ramp
into the ensemble (`config.py:337-354`).

Co-occurrence is not usefulness. Swapping in naive co-activation would teach the graph the
retriever's own biases and then call the result evidence — a learned correction that hides the
defect it measures. Kindex's design already sits on the right side of this. Do not trade down.

---

## 4. The work

### W0 — Make the built machinery run *(no new concepts; highest value)*

**W0a. Backfill embeddings.** `kin embed plan` → `enqueue` → `drain` over ~23.5K unembedded
nodes on `voyage-context-4`. Until this lands, every claim about Kindex's semantic search is a
claim about 1.6% of the graph, and W1's similarity floor has almost nothing to gate. Estimate
the Voyage spend before starting and stage it through the existing budget ledger.

**W0b. Diagnose the dead pheromone loop.** `pheromone_enabled` defaults True and three call
sites exist — `attention.py:398`, `reinforce.py:449`, `sim.py:537` — yet the table has zero
rows. Either the hook path never reaches `deposit_pheromone`, or `attention.py:1018`'s
`pheromone_deposits` state is not surviving to `reinforce.py:228`. This is the existing
Hebbian engine and it is off. Fixing it likely delivers most of what W4 proposes to build.

These are prerequisites. W1 is close to meaningless without W0a, and W4 is redundant work if
W0b turns out to be one bug.

---

### W1 — Retrieval verdict and a calibrated similarity floor *(the one real steal)*

This is Kindex's own known defect. Graph node `f16e36d93067` recorded it on 2026-08-11; it is
**still live at 0.33.0**, re-verified today:

- `vectors.py:vector_search` returns top-k nearest neighbors for any query with no distance
  threshold anywhere in the function.
- `mcp_server.py:1024 ask()` reaches its `"No relevant knowledge found"` branch **only when the
  result list is empty** — which vector search makes unreachable once embeddings exist.
- `mcp_server.py:397` renders titles and content snippets into context before any caller can
  judge them. Contamination is text entering context, not intent; "ignore the results" is not
  enforceable.

**Design:**

1. `vector_search(..., min_similarity: float | None)` drops rows below the floor.
2. `hybrid_search` returns a first-class `RetrievalVerdict` — `grounded` / `weak` /
   `ungrounded` — derived from best-channel confidence. Retrieval states its own confidence;
   the caller decides what to do with it. That boundary is the Helland-correct one.
3. **Calibrate; do not hardcode 0.55.** Hillock's threshold is tuned to its own 10,000-D
   bipolar space. Cosine distributions differ per provider and model — `voyage-context-4` is
   not MiniLM. Key the floor in config by `provider:model` and ship `kin embed calibrate`,
   which samples the graph, computes the null-query similarity distribution, and sets the floor
   at a chosen percentile. A borrowed constant is a guess wearing a number's clothes.
4. Enforce at the **injection surfaces**, not just the search function: `mcp_server.search`
   rendering, `ask`, `kin prime`, and the hooks.
5. **Fail-open on FTS, fail-closed only on the vector channel.** BM25 already carries an
   implicit lexical floor — no term match, no row. Vector search has none. Keep the `tags`
   post-filter as the hard deterministic gate it already is; it is the one path that provably
   returns nothing.

**Cost:** ~150 LOC plus tests. Zero new dependencies.

---

### W3 — Multi-hop reach, in SQL, not hypervectors

The measurement settles the VSA question. A 3-hop recursive CTE across all 113,355 edges runs
in 29 ms — the same as 1-hop, inside process-startup noise. Kindex's retrieval latency is
dominated by the Voyage HTTP round-trip. A bit-packed 10,000-D reservoir would be optimizing a
term that is already zero.

But Hillock is pointing at something real, and it is **reach, not speed.** Kindex expands
*one hop from the top five FTS hits* (`retrieve.py:317-322`). That is shallow.

**The honest steal:** replace the hand-rolled 1-hop loop with a recursive CTE — configurable
depth (default 2), a per-hop decay factor, and a **beam cap, which is required**: max fanout is
849, so an uncapped 3-hop from a hub explodes. ~40 lines of SQL, no new dependency, and it is
the actual multi-hop capability Hillock is selling.

One honest bound on the payoff: the graph has 6,563 connected components. Deeper traversal
increases reach *within* a component and cannot bridge across them. Cross-component connection
is what the dream cycle's domain edges and the suggestion queue are for — a different problem,
and not one Hillock addresses either.

**The decisive argument, which is not about speed at all: VSA loses the path.** Traversal
returns `A —(cites)→ B —(authored_by)→ C`. A hypervector returns a vector that decodes to
something *near* C. Kindex's entire value proposition is auditable provenance — referent
binding, `verify`/`invalidate`, `changelog`, supersede history. Superposition is the wrong
substrate for a graph that must answer "why", dedupe, let a user drill in, or explain a result
to someone who does not trust it. You would trade traceable provenance for a constant factor
that measurement already shows is zero.

Two supporting points worth recording, because they defuse the usual pitch. First, "traversal
does not scale with |V|" is mostly false: BFS cost is governed by frontier size (b^d), not by
|V|. What actually degrades with |V| is index locality — cache misses, disk, shard round-trips
— and VSA does not fix that. Second, VSA cleanup is an approximate-nearest-neighbour search
over the whole item codebook, i.e. O(|V|) unless indexed — at which point you have reintroduced
the very index you were escaping, with worse constants and lossy results. You did not remove
the scan; you moved it and made it probabilistic. And the capacity ceiling is hard: 10,000-D
bipolar bundling holds low hundreds of superposed items before crosstalk swamps recall, and
each unbind compounds the noise, so multi-hop degrades as a silent accuracy cliff rather than
gracefully.

**Park the VSA reservoir.** If a future graph ever makes traversal the bottleneck, the
Helland-correct shape is written down now so nobody has to re-derive it: the reservoir is
derived, never authoritative; rebuilt by the dream cycle; marked stale on edge writes; and
**every hit is confirmed against `edges` before a node can enter context.** Write the design.
Do not build it.

---

### W2 — Deterministic extraction as an optional candidate producer

**Placement is the whole design.** Output lands in `capture_candidates`, whose schema comment
(`schema.py:191`) already states it is *"deliberately separate from nodes/edges/FTS so no query
can accidentally"* surface it. 13.8% precision is survivable in a review queue and fatal in the
graph.

- `pip install kindex[talon]` — **never core.** Core stays torch-free. If the extra is absent,
  `extract.engine: deterministic` warns and falls back to `keyword_extract`; it never crashes.
- Anti-corruption layer: an `Extractor` protocol with `llm_extract` and `deterministic_extract`
  returning the same `ExtractionResult`. **Neither writes nodes.**
- Payoff if it works: `kin learn` and `ingest` currently spend LLM tokens on recall. A
  deterministic pre-pass can do the recall job cheaply and leave the LLM a much smaller
  precision job — or be skipped entirely for bulk imports.

**Gate on measurement, not on the README.** Kindex has no eval harness today. Before this
becomes any kind of default, build a fixed eval set from the local corpus — 166 articles, 10
book projects, and 23.8K existing nodes as ground truth — and require deterministic extraction
to beat the existing `keyword_extract` baseline on candidate-acceptance rate. If it does not
beat keyword extraction, 2.5 GB bought nothing.

---

### W4 — Co-activation as its own channel *(only after W0b)*

If W0b restores the pheromone loop, the one incremental idea worth taking from Hillock is
*pair*-level: when nodes are injected together **and** `reinforce.py` confirms the session
actually used them, deposit on the pair.

- Store in its **own table** (raw signal); expose as its **own ensemble channel** with its own
  auto-ramp.
- **Never fold into `edges.weight`.** That column is topology asserted by a human or an agent.
  Merging a learned correction into it destroys the provenance separation and makes the graph
  unable to tell you what it was told from what it inferred. Raw signal and applied correction
  stay separate facts.
- Here, take Hillock's bounded update `w ← w + η(1−w)`: it is genuinely better than Kindex's
  unbounded-additive deposit, because it cannot run away.

---

### W5 — Panel tools: billing and backing models

`WANDER_ANTHROPIC_API_KEY` is present in `~/.profile`, and all three tools already resolve it
first. **Billing is already correct; no change is needed there.**

| Tool | Current | Action |
|---|---|---|
| **Advocate** | `claude-opus-5` (`provider.py:333`), Claude-5 thinking handling and legacy-ID alias map present, Wander-first keys (`provider.py:65`) | **Nothing to do** |
| **Constrain** (live) | `claude-sonnet-4-6` (`backends/anthropic.py:14`) | Bump to `claude-opus-5` — volume is low, interview quality is the product |
| **Constrain** (pact pipeline) | `claude-sonnet-4-20250514` — **retired** | Fix `pact.yaml:1` and `sops.md:37`; regenerate or delete the stale `src_constrain_engine` contract |
| **Simulacrum** | `claude-sonnet-4-6` (`run.py:60`), Wander-first keys (`run.py:87`) | Classifier → `claude-sonnet-5`; specialist → `claude-opus-5` |

**On the defunct pin.** `claude-sonnet-4-20250514` is retired and appears in `pact.yaml:1`,
`sops.md:37`, `contracts/src_constrain_engine/interface.py:6`, and
`src/src_constrain_engine/engine.py:20`. That package is **not shipped** — `pyproject.toml:28`
packages only `src/constrain` — so it is off the runtime path. But it is the pact pipeline's
model pin, so the next pact run would request a retired model. Fix the config, not just the
dead constant.

**On Simulacrum's thinking config.** `run.py` currently sends `thinking: {"type": "disabled"}`
for all 5-family models. On Opus 5 that is accepted only at effort `high` or below, and it
forfeits the model's default adaptive thinking. Better: drop the disable, set
`output_config: {effort: "medium"}`, and raise `max_tokens` above 1500 — the graph already
records that ceiling truncating long multi-part evaluations. The GENERALIST branch stays on its
OpenAI fine-tune by design; `WANDER_ANTHROPIC_API_KEY` governs every Anthropic call, not that
one.

---

### W6 — Remote state

`jmcentire/kindex` has **zero open PRs, zero open issues, and one branch (`main`).** All 13 PRs
are merged or closed; issues 14–17 were closed 2026-07-16. Local `main` is level with
`origin/main` at `676214f`. Nothing on the remote requires resolution — consistent with the
branch-hygiene decision already recorded in the graph (2026-08-20).

The only uncommitted work is a three-line explanatory comment in
`src/kindex/adapters/claude_web.py` documenting a past drift between the adapter's
`DEFAULT_DIR` and an external `fetch_conversations.py`. One loose end: that comment names a file
that does not live in this repo. Either commit it as-is or point it at where
`fetch_conversations.py` actually lives, so the next reader can check the invariant it asserts.

---

## 5. Sequencing

| Phase | Work | New dependencies |
|---|---|---|
| **0 — this week** | W0a embed backfill · W0b pheromone diagnosis · W5 model bumps · W6 commit the comment | none |
| **1** | W1 retrieval verdict + calibrated floor *(gated on W0a)* | none |
| **2** | W3 recursive-CTE multi-hop with beam cap | none |
| **3** | W2 `[talon]` extra *(gated on an eval harness that beats the keyword baseline)* | ~2.5 GB, optional extra only |
| **Not scheduled** | VSA reservoir — design recorded, not built, pending a measurement that shows traversal is a bottleneck | — |

Phases 0 through 2 add no dependencies at all. Kindex stays nimble because the only heavyweight
piece is quarantined behind an extra, and the extra has to earn its place against a baseline
before anyone turns it on.
