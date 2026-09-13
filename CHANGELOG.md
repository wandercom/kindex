# Changelog

All notable changes to Kindex are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.37.0] - 2026-09-12

### Added
- Shared periodic goal, trajectory, adherence, and validation reviews for Claude,
  Codex, OpenCode, Antigravity, and Cursor. Trusted configuration controls review
  providers, cadence, budgets, and optional Advocate escalation.
- Independent health monitoring distinguishes hook execution, agent use, review
  outcomes, advice delivery, and explicit usefulness feedback. A durable inbox
  and native macOS desktop alerts surface sustained issues without a mail daemon;
  root mail remains a separate opt-in. Acknowledgment, recurrence, cooldowns, and
  transport retries persist across checker restarts.
- Kinbase interoperability verifies signed source bytes, preserves source identity
  and effective times, and distinguishes raw evidence from reduced snapshots.
  Schema 13 adds provenance-limited standing. Shared exports scrub nested private
  paths and contact data without rewriting signed source events.

### Fixed
- Project hooks and MCP resolve the same durable project store; conflicting
  populated stores are diagnosed and preserved instead of silently selecting an
  empty graph. Explicit personal and company scopes remain separate.
- Implicit home/project ambiguity requires an explicit scope selection. Modern
  Claude sessions reset their review windows and fence stale asynchronous work.
- Shared graph exports redact local `file:` URLs, including nested metadata and
  identity keys, while preserving HTTPS evidence and stable associations.
- JSON/JSONL graph transfers are atomic, preserve lifecycle and provenance, and
  keep imported verification as evidence rather than granting local authority.
  Replays are idempotent; conflicting claims fail without partial writes.
- Doctor checks actual FTS postings, repairs corruption transactionally, and keeps
  repeated repairs idempotent.
- Stale, evicted, and superseded reviews receive exact terminal receipts. Idle
  sessions do not acquire false missing-hook alerts solely from elapsed time.
- Trusted historical recall applies the requested evaluation date consistently.
  Native session scans report incomplete coverage when a scan limit is reached.

### Qualification boundaries
- Native advisory delivery was exercised in Claude, Codex, OpenCode, and
  Antigravity. Cursor's adapter and session metadata have automated coverage;
  authenticated model delivery and IDE activity discovery remain unverified.
- Desktop submission and advisory delivery do not prove a person read the output
  or found it useful. Explicit feedback records that distinction.

See [supervision and notifications](docs/supervisor-health.md).

## [0.36.2] - 2026-09-10

### Added
- Optional Claude function-hook adapter, qualified against 2.1.263, with repo-local
  task routing, contextual retrieval, quarantined turn capture and visible owner
  health. Modern and legacy adapters are separately selected; no legacy handlers
  run through the modern adapter.
- Versioned typed task service with scoped operation IDs, atomic mutation/receipt/
  audit-outbox commits, optimistic versions, durable replay, cancellation and
  explicit reconciliation. Native task-state tools do not fall back to Claude's
  ephemeral list. Agent execution controls are unaffected.
- signet-eval capability negotiation and pre-effect semantic admission. Kindex owns
  task/knowledge data; signet-eval owns local coding-model policy and host redaction.
  Signet's outward-world product is not an installation dependency.
- `.kin/knowledge.json` transport for explicitly selected shareable semantic
  evidence and relationships. Clone imports remain quarantined; no implicit
  Personal fallback or unsigned Company authority.

### Fixed
- Legacy Claude hooks migrate from explicitly supported prior executable locations
  while preserving custom commands.
- Full pytest validation runs on pull requests and main pushes using the same job
  that gates release builds and PyPI publication.
- Automatic reminder snoozes preserve the action execution cutoff, preventing
  stale actions from being revived by notification retries.
- Claude advisory context no longer emits a permission auto-allow.
- Shared credential sanitizer protects Kindex-owned persistence, logging, export,
  candidate, model and embedding boundaries without entropy-based destruction of
  ordinary hashes/IDs. Historical cleanup and host pre-hook logs remain separate.
- Task reopen/terminal-claim transitions, claim ownership, scoped pagination,
  dependency validation, atomic linked creation, date parsing and CLI error codes.
- Hook installation tracks exact owned commands, preserves foreign siblings,
  reports unknown wrappers, backs up settings and uses correct Claude timeout units.

See [function-hook boundaries and migration](docs/claude-function-hooks.md).

## [0.36.0] - 2026-09-02

### Added
- **Real paused-session resume and reversible lifecycle retirement.** `kin tag
  resume` and MCP `tag_resume` now reactivate a paused tag for the exact project
  before rendering its bounded trusted context. Completed, unlinked session leaves
  become eligible for the slow archive after 60 days during `kin cron` step 8 or
  `kin archive run`, and restore with their lifecycle history intact. Active,
  paused, linked, and newer sessions remain in the fast graph. Archive cycles
  and `kin archive list` report IDs found in both fast and slow stores after an
  interrupted move; both copies are preserved rather than reconciled from ID
  equality alone.
- **Bounded, reviewable Dream domain links.** Dream, Kindex's background
  knowledge-consolidation pass, now emits sparse representative
  proposals instead of writing every shared-domain pair as a semantic edge. Dry-run
  output lists the exact proposed pairs. The pending queue is capped per graph by
  `reminders.dream_max_domain_link_suggestions` (default 50), rotates across
  domains, and never recreates an accepted or rejected pair.

### Changed
- **Breaking metric semantics: graph health now measures semantic topology.**
  Session lifecycle nodes and legacy `dream-cycle domain co-membership` edges are
  retained for history but excluded from traversal, components, bridges, and
  orphan counts. CLI and MCP output distinguish semantic node/edge counts from
  stored counts and emit `metrics_schema: 2`; consumers of the old metrics
  should recalibrate their baselines.
- **Deep Dream suppresses redundant summaries across runs.** Candidate clusters are
  deduplicated by overlap and stable member signature before generation, and active
  summary nodes' recorded members prevent the same cluster from being summarized
  again. Candidate and representative ordering is stable across weight decay.
- **Schema v12 creates an automatic recovery point before migration.** The first
  open of an older graph uses SQLite's backup API to snapshot the complete
  pre-migration state under the database's dedicated `snapshots/.../migrations/`
  directory, validates its SQLite integrity and source schema, records the path
  durably in database metadata (and in `kin changelog` on normal stores), and
  refuses to migrate if any safety step fails. Snapshot directories/files are
  owner-only, failed partial files are removed, concurrent new-version processes
  serialize through a dedicated rollback-journal SQLite lock and recheck the
  schema, and migration snapshots are not subject to
  the ten-file rotation used for automated-merge snapshots. The migration
  canonicalizes project paths and preserves duplicate active sessions as paused
  history, marks them
  `duplicate-active-session-migration-v12`, then creates the partial unique
  active-tag index and suggestion-pair index. Suggestions also persist whether
  endpoints are titles or immutable node IDs; ambiguous title resolution is
  refused instead of selecting a row arbitrarily. `kin tag list --status paused`
  exposes the reason, while `kin status` exposes the recovery point. A Kindex
  build also refuses to open a schema newer than it understands instead of
  operating on it silently. Do not run v0.35.x against a migrated database as a
  rollback: stop all Kindex processes and restore the recorded pre-migration
  snapshot after moving the live database's `-wal` and `-shm` sidecars aside.

### Removed
- **New Dream domain-clique writes.** Existing `dream-cycle domain co-membership`
  rows remain stored for audit, but v0.36.0 neither creates more nor treats them
  as semantic topology. Review and accept bounded domain-link suggestions instead.

### Fixed
- Session lookup filters status and exact project before applying limits, reused
  names resolve deterministically, and schema enforcement prevents concurrent
  duplicate active tags without replacing the incumbent row.
- Hook lifecycle calls carry one sampled project path and record degraded failures
  instead of silently losing session state.
- Fuzzy and domain Dream suggestions respect all prior resolution states, so
  accepted and rejected pairs stay resolved.
- Suggestion acceptance uses its durable endpoint identity rather than guessing
  between a node ID and a display title, and refuses duplicate-title ambiguity.

## [0.35.0] - 2026-09-02

### Added
- **OpenCode SessionStart hook parity.** OpenCode sessions got the Kindex MCP tools
  but no hooks, so nothing primed automatically — because the MCP server's cwd is not
  the project's, MCP cannot know which repo a session is in. Hooks can: they run in
  the agent's process at the project cwd. `kin setup-opencode-hooks` now installs an
  auto-loaded OpenCode plugin (`~/.config/opencode/plugin/kindex.js`) that runs
  `kin prime` **in the working directory** once per session and injects the result
  into the system prompt via `experimental.chat.system.transform` (the OpenCode
  analog of Claude's SessionStart context), plus `experimental.session.compacting`
  for compaction context — matching the priming Claude and Codex already get. New
  `--adapter opencode` on `prime`/`attention-hook`/`agent-prime-hook`/`agent-stop-hook`
  emits plain text for the plugin to inject.

### Changed
- **Sim supervisory review now weighs alignment and trajectory, with graduated
  effort.** The optional async Sim check-in judged only whether the work itself was
  sound; it now also asks whether the work still serves what the **user** actually
  asked (spirit over letter; a newer instruction can supersede an older one, an aside
  should not) and where the current course **leads** (does it reach the goal or
  diverge; what are the side-effects). Effort is self-calibrated by reading the
  window: banter is skipped before it costs anything; ordinary work gets the grounded
  single-persona review; a high-stakes, hard-to-reverse move (money, a large workflow,
  an architecture lock-in) is a **reversibility trip-wire** that can recommend — or,
  opt-in, run and verify — a deeper Advocate/Helland review. Notes are framed as
  considerations to weigh, not verdicts.

### Fixed
- **`.kin` artifacts are now anchored at the git root with repo-relative paths.**
  `kin export code-map` auto-detects the git root of the cwd and relativizes against
  it, so a tracked `.kin/code-map.json` never carries machine-local absolute paths —
  in-repo absolute provenance is **recovered** as a repo-relative path instead of
  dropped, and nothing outside the repo leaks. `kin index` writes `.kin/` at the git
  **root** (not the cwd), so running it from a subdirectory no longer scatters `.kin/`
  directories through the tree.

## [0.34.0] - 2026-08-31

### Added
- **Retrieval grounding — the graph can now say it knows nothing.** `vector_search`
  returned the top-k nearest neighbours for any query however nonsensical, and
  `ask()` could only reach its "no relevant knowledge" branch when the result list
  was empty — which vector search made unreachable. New `kindex.grounding` emits a
  `RetrievalVerdict` (`grounded` / `weak` / `ungrounded` / `uncalibrated`) that
  `hybrid_search` reports via a `grounding=` out-param and `format_context_block`
  stamps onto the block — the single place rows become context text, so the verdict
  is a gate rather than something each caller must remember. The similarity floor is
  an **immutable versioned calibration record** keyed by `provider:model` carrying
  the corpus it was measured against; config holds only the percentile policy, never
  the number. New `kin embed calibrate [--show] [--percentile]`. **Shadow mode is the
  default** (`grounding.enforce: false`): the verdict is computed and reported while
  every row still flows, because silent false negatives are worse than loud false
  positives. Near-misses are recorded so a disputed refusal has evidence.
- **Bounded multi-hop graph expansion.** `hybrid_search` walked exactly one hop from
  the top five FTS hits regardless of `graph_hops`. New `Store.expand_multihop`
  honours the requested depth with per-hop score decay and a **mandatory beam** whose
  ordering is total and stable (score desc, then node id asc) — an unspecified beam
  makes traversal nondeterministic, which in a provenance-first graph is worse than
  slow. Measured on a 113k-edge graph: 3 hops in 0.7 ms, 4.8x the reach of 1 hop.
  New `ranking.hop_decay`, `ranking.graph_beam`.
- **Learned pair co-activation** (schema v11, `node_coactivation`). A third retrieval
  channel with its own table, its own auto-ramp, and a bounded update
  `w <- w + eta*(1-w)` that saturates instead of running away. Deposits are gated on
  **confirmed use**, never co-retrieval. It is never folded into `edges.weight`,
  which is asserted topology — merging a learned correction there would destroy the
  told/inferred distinction.
- **Extraction engines and an eval gate.** New `kindex.extractors` defines an
  `Extractor` protocol with a shared `ExtractionResult`; `stage_candidates` is the
  only sanctioned path from an extractor into storage and writes to
  `capture_candidates`, never `nodes`/`edges`. New `kin extract eval|engines` scores
  engines against the local corpus with a **two-part gate**: grounding precision as a
  hallucination floor, title recall as the discriminator — either alone is gameable.
  New optional `kindex[talon]` extra for LLM-free deterministic extraction, excluded
  from `all` and degrading to keyword extraction with a warning when absent.
- `kin doctor` now reports **column-level schema drift**, oversized nodes, and
  silently-recovered failure counters.
- `Store.bump_meta_counter`, `Store.schema_drift`.

### Fixed
- **The stigmergic pheromone channel was dead on every upgraded install.** The
  `missed` column was added to the v7 `CREATE TABLE IF NOT EXISTS` after v7 shipped,
  so any store that had already run v7 never received it and never would — while
  `schema_version` still read current. `deposit_pheromone` raised
  `no such column: missed`, the attention hook swallowed it with
  `except Exception: pass`, and session state recorded the deposit as successful
  anyway. Fixed by schema v10 (idempotent, atomic, `PRAGMA`-verified), a
  **column-level** drift check (a table-existence check is blind to this failure
  class), a logged and counted failure in place of the silent swallow, and a caller
  that records only what the store accepted.
- **Unbounded dream-cycle merge growth.** `merge_nodes` appended source content into
  the target with no cap, and `content_overlap` compares only the first 500 chars —
  where machine-generated files are identical. Minified symbols, one class defined in
  twenty files, a vendored LICENSE and generated schemas are mutually similar by
  construction, so each merge was a false positive that grew the target without
  bound. Added size and absorption caps that **refuse rather than truncate**, with
  refusals counted for `kin doctor`.
- **LLM extraction was silently disabled on multi-key configs.** `extract.py` did a
  bare `os.environ.get(config.llm.api_key_env)` while `llm.py` correctly parses the
  comma-separated fallback list the config documents, so a config naming two env vars
  matched nothing and fell back to keyword extraction forever. It also hardcoded the
  Anthropic SDK while ignoring `llm.provider`. Both now delegate to `llm.py`.
- `vector_search` accepts `min_similarity` and exposes `vec_similarity`.

### Changed
- SQLite schema v9 -> v11.
- `similarity_from_distance` uses the L2 identity `cos = 1 - d^2/2`. `sqlite-vec`'s
  `vec0` returns **L2 distance, not cosine**; the naive `1 - d` collapses the entire
  useful range to zero and calibrates a floor that can never fire.

## [0.33.0] - 2026-08-24

### Added
- **Referent binding + two clocks (R0).** A node can bind the external thing its
  claim describes — `{path|url, content_digest, digest_scope}` — plus
  `asserted_at` (claim time) and `true_of` (when the referent was observed in
  the digested state). `kin add --referent` / MCP `add(referent=...)` bind at
  capture; `kin stale` / MCP `stale_check` re-hash file-scope referents, demote
  moved-or-missing referents from `trusted_only` recall (new machine reason
  `stale_referent`), mark them `[stale-referent]` in search/context, and list
  them as re-verification candidates; `--rebind` re-verifies (moves `true_of`,
  never re-dates the claim). Detection never deletes or rewrites content.
  Export/import and the `.kin` index carry the binding (absolute local paths
  redacted from the git-tracked projection, digest kept).
- **Pre-merge DB snapshots.** Every automated destructive merge (`graph_merge`,
  dream-cycle auto-merges) first copies the SQLite store via the backup API to
  `$XDG_STATE_HOME/kindex/snapshots/` (ten kept per database) and logs a
  `db_snapshot` changelog entry with a restore hint. Fail-closed: no snapshot,
  no merge.
- **`.kin` schema versioning.** `.kin/index.json` advances to schema v2:
  unknown top-level fields now pass through the `kin merge-kin` driver via a
  3-way field merge, and a side declaring a newer schema version makes the
  driver decline (normal git conflict) instead of silently rewriting it.

### Changed
- The SQLite schema advances from v8 to v9 (atomic, rollback-safe): nodes gain
  nullable `referent`, `asserted_at`, `true_of`.

### Fixed
- `_migrate_v8` stamped the code's current `SCHEMA_VERSION` instead of the
  literal `8`, which would have marked v9+ migrations applied before they ran
  on any multi-step upgrade.

### Documentation
- `docs/spec-contradiction-check.md`: reviewed v1 contract for the
  `contradiction_check` tool (spec only). `docs/prd-lineage-grounding-2026-08.md`:
  the reviewed PRD behind this line of work. Human/MCP guides and README cover
  referent binding, staleness, and snapshot restore.

## [0.32.0] - 2026-08-18

### Added
- Automatic hook capture now enters a quarantined candidate queue instead of writing directly to durable memory. Candidates expose exact review payloads and freshness tokens, require explicit accept or reject decisions, expire on a configurable TTL, and can be erased completely.
- Nodes can carry typed verification provenance, asserted valid-time intervals, and explicit invalidation records. Search and context expose opt-in trusted-only projections, while session resume admits trusted state by default and stays inside deterministic character and token budgets.
- CLI and MCP surfaces now expose candidate review, verification, invalidation, and trusted retrieval operations with one captured operation clock per time-dependent request.

### Changed
- The SQLite schema advances from v7 to v8 through an atomic, rollback-safe migration that preserves existing graphs while introducing trust and candidate state.
- PreCompact extraction stages untrusted proposals for review and never promotes automatic model output directly into graph nodes or edges.

### Fixed
- Release and test targets now pin imports to this checkout's absolute `src` directory, so a stale editable-install `.pth` cannot make subprocess tests execute an older Kindex or make distribution verification look for the previous version's wheel.

### Documentation
- README, human guide, LLM guides, MCP server card, and public site material now document the reviewed-memory boundary and bounded trusted resume behavior.

## [0.30.1] - 2026-08-11

### Fixed
- The `all` extra still carried an unbounded `mcp[cli]>=1.26.0`, so `pip install kindex[all]` continued to resolve mcp 2.0.0 and produce an MCP server that could not start — the same defect 0.30.0 fixed for the `mcp` extra alone. Both extras are now pinned below 2.0, and the release isolation gate exercises the `[all]` install path so this class of gap cannot recur unnoticed.

## [0.30.0] - 2026-08-11

### Fixed
- **End-of-session capture actually runs.** The installed Stop hook passed `--text "Session ended"`, which preempted the hook envelope on stdin, so `kin compact-hook` extracted knowledge from a 13-character literal instead of the session transcript — and could spend an LLM call doing it. Stdin envelopes (parseable JSON carrying `hook_event_name` and `transcript_path`) now take precedence over `--text`, `kin setup` installs the Stop entry without `--text`, and re-running setup migrates existing broken entries. Hook-shaped JSON without a transcript pointer is treated as metadata, never routed into extraction.
- **Memory failure degrades the turn instead of crashing it.** Hook-surface commands (`prime`, `compact-hook`, guards, scheduler entries) previously tracebacked with a nonzero exit on any store failure, visible only in transient hook stderr. They now emit a shaped degraded output, exit 0, and append one JSON event per failed invocation to `degraded.jsonl` in the base data directory — a plain file append that works when SQLite is what broke. `kin status` and `kin doctor` surface the 7-day count; `doctor` warns when it is non-zero. Priming tolerates a poisoned node per section rather than losing the whole context block, and every MCP tool returns `Error: memory unavailable (<ErrClass>)` instead of a protocol error, including for corruption SQLite only surfaces at first query.
- **Archived and superseded nodes stay out of retrieval.** Search fenced only superseded nodes, so archived content remained a first-class candidate in FTS, vector, and hybrid results, in context formatters, and in topicless MCP context/prime/orient pulls. All default retrieval paths now fence both states; `--include-archived` / `include_archived=True` restores the previous behavior, and a short result set says how many results were fenced so the escape hatch is discoverable. `hybrid_search` backfills after drop-filtering instead of silently returning fewer results than requested.
- **Weight decay is cadence-independent.** Each cron pass re-applied a decay factor computed from a node's full age to its already-decayed weight, so the effective half-life depended on how often cron ran — at a five-minute cadence, weights collapsed roughly fifty times faster than the documented 90 days. Decay now folds only the interval since the last recorded run (`decay.last_run`), the first run after upgrade establishes the checkpoint without decaying anything, and the read/decay/stamp sequence is serialized.

### Changed
- The `mcp` extra is pinned to `mcp[cli]>=1.26.0,<2.0`. The unbounded pin resolved to mcp 2.0.0, which is incompatible with the MCP server module, so fresh `pip install kindex[mcp]` installs produced a server that could not start.

## [0.27.1] - 2026-07-02

### Added
- Reminder actions can now wake headless Codex or OpenCode runs when due. `kin remind create --wake codex|opencode` stores first-class wake metadata, can resume a host session via `--session last` or an explicit session id, and passes through working directory/model/agent options where the host CLI supports them.
- MCP `remind_create` exposes matching `wake`, `wake_session`, `wake_cwd`, `wake_model`, and `wake_agent` arguments so agent clients can create Codex/OpenCode wake reminders directly.

### Documentation
- README, website docs, MCP agent guide, and session guidance now distinguish daemon-triggered headless wakeups from same-thread TUI interruption.

## [0.27.0] - 2026-07-01

### Changed
- Voyage embeddings now default to `voyage-context-4` (1024 dimensions) for users configured with `embedding.provider: voyage`. Contextual Voyage models use the `/v1/contextualizedembeddings` endpoint with document/query `input_type`; explicitly configured standard Voyage models such as `voyage-3.5` continue to use the regular `/v1/embeddings` endpoint.
- Embedding maintenance now tracks provider/model/strategy fingerprints, supports provider-gated contextual chunk groups for Voyage context models, stores multiple chunk vectors per node, and aggregates chunk hits back to parent nodes during vector search. `kin embed` gained `status`, `plan`, `enqueue`, `drain`, and `reindex` subcommands with tag/type/project/`.kin`/stale selectors so large graphs can be updated gradually instead of one all-or-nothing run. Cron auto-enqueues bounded stale batches only when the configured model supports contextual embeddings.

## [0.26.1] - 2026-06-27

### Changed
- `kin index` now registers the `.kin` structured merge driver automatically on first run — when inside a git repo and not already registered in that clone — so a freshly written `.kin/index.json` is conflict-safe without a separate setup step. Idempotent and guarded; opt out with `kin index --no-merge-driver`. `kin setup-merge` remains the explicit (re)install path.

### Documentation
- The AI-usage instruction files (`kin setup-claude-md` / `setup-agents-md`), the README, the website, and the project `CLAUDE.md` now document the `.kin` merge driver in the `.kin/` contract: generated `.kin/index.json` and `.kin/code-map.json` are never hand-resolved — `kin index` wires the union merge driver that resolves them.

## [0.26.0] - 2026-06-27

### Added
- **Structured merge driver for `.kin` artifacts.** `.kin/index.json` and `.kin/code-map.json` are generated, id-keyed JSON snapshots — git's line-based merge conflicts on them needlessly. The new `kin merge-kin` git merge driver does a structured 3-way **union** instead: for `index.json`, union nodes by id (newer `updated_at` wins, base detects deletions) and recompute the derived header; for `code-map.json`, union nodes/edges/layer members and recompute the tour. This is lossless across machines (regenerating `index.json` from one machine's local DB would drop the other branch's nodes), and the result is byte-identical to what `kin index` would emit, so a later regeneration produces no spurious diff. Install per repo with `kin setup-merge`, which registers the driver in `.git/config` and points `.kin/index.json` / `.kin/code-map.json` at it via `.gitattributes` (repos without the driver registered fall back to git's default merge).

### Changed
- `.kin/index.json` no longer carries a volatile `source_updated_at` timestamp. It changed on every regeneration — churning git history and conflicting on every concurrent merge — while the commit time already records snapshot freshness and each node keeps its own `updated_at`.

## [0.25.6] - 2026-06-27

### Fixed
- Client scoping now also covers the pull-based context surfaces. `format_context_block`'s full/abridged tiers drop operational nodes — constraints, watches, directives — scoped to a different client when a client is known, so the MCP `context`/`ask` tools no longer surface (for example) an Antigravity-scoped constraint to a Claude session. The MCP server resolves its client from the `KIN_CLIENT` environment variable (a per-client MCP config can set it); unset means no scoping — the unchanged default. Human-facing `kin context` / `kin status` continue to show every node.
- `prime`'s 24h "Recent activity" section no longer echoes the titles of nodes scoped to a different client; the aggregate activity counts remain complete.

## [0.25.5] - 2026-06-27

### Fixed
- **Attention and context injections are now scoped to the running agent client.** A graph node tagged for a specific client — e.g. an `antigravity` directive documenting Antigravity's nested `toolCall`/`toolCall.args` PreToolUse hook protocol — previously surfaced as a tool-boundary advisory and as SessionStart context in *every* client, so Claude and Codex received instructions about a hook schema they do not use. The running client is now threaded from the hook through both the synchronous and asynchronous (queue → drain, including the status-retry hop) attention pipeline into candidate selection, and through `prime` / `agent-prime-hook` SessionStart context, so any node scoped to a different client is dropped.
- Client scope is declared with an explicit `client:<name>` / `agent:<name>` tag (authoritative for any known client) or a bare tag for a coined client name (`antigravity`, `opencode`). Names that double as topical subjects — `claude`, `codex`, `gemini`, `cursor`, and the 2-char `ag` alias — are **not** inferred from a bare tag, so a node tagged `gemini` about the Gemini API is never hidden from a Claude session. Nodes that name no client are unaffected and surface everywhere. A `plain`/unlabeled hook caller scopes as Claude (the default install).

### Performance
- Node embedding is deferred off the `add`/`edit`/`supersede` hot path, so those operations return without blocking on vector generation (#9).

## [0.25.4] - 2026-06-23

### Fixed
- `learn` (MCP) no longer creates unlinked, title-only concept nodes that inflated the orphan count. The keyword-extraction fallback emits content-less concepts whose connections never resolve to edges; these accumulated as orphans on repeated ingestion. `learn` now rejects low-information concepts (no content and no domains) and grounds every surviving concept to a freshly created source node via `context_of` edges, so extracted concepts can never orphan.
- Flaky test `test_capture_session_end_with_existing_nodes` is now deterministic — it mocks extraction to exercise the dedup path instead of depending on live LLM output.

## [0.25.3] - 2026-06-14

### Fixed
- Attention hooks now return within a bounded internal deadline instead of consuming Codex's 5-second hook timeout while waiting on LLM arbitration.
- Slow attention reviews are queued asynchronously and injected later only when the result remains relevant to the conversation.
- Hook setup now migrates attention commands to `--deadline-ms 3500`, leaving room inside the host hook timeout for process and SQLite overhead.

## [0.25.2] - 2026-06-13

### Fixed
- Antigravity quiet-mode prompt checks now preserve the Antigravity hook protocol instead of emitting a Claude `hookSpecificOutput` envelope that agy rejects.
- macOS system reminder delivery now honors `reminders.channels.system.enabled=false`, so disabling system notifications actually suppresses desktop popups.
- Codex no longer receives unsupported `suppressOutput` fields from Kindex quiet-mode hook output; Codex still receives the required SessionStart prime context.

## [0.25.0] - 2026-06-12

### Added
- **Google Antigravity support** — `kin setup-antigravity-mcp` writes Kindex MCP config to both Antigravity global MCP config locations (`~/.gemini/config/mcp_config.json` and `~/.gemini/antigravity-cli/mcp_config.json`), while `kin setup-antigravity-hooks` installs PreInvocation context priming, PreToolUse attention and config-write permission gating, and Stop-time reinforcement enqueue.
- **Agent adapter layer** — hook output and tool payload translation now lives behind a client adapter boundary, with Antigravity `injectSteps`, PreToolUse `allow`/`force_ask`, and nested `toolCall` payload parsing alongside existing Claude/Codex envelopes.
- **Per-agent tuning** — `agents.clients.<client>` and `agents.instances.<client>:<instance>` overlays can tune Kindex behavior by client family or individual conversation/instance. `kin agent-config show|set` writes only approved behavior keys (`attention.*`, `sim.*`, `collab.*`, `hooks.prime_tokens`) so agents can propose tuning through the host permission flow without silently mutating arbitrary config.

### Changed
- Agent setup docs now cover Claude Code, Codex, Gemini CLI, Google Antigravity, OpenCode, and Cursor consistently across README, `/docs`, and `kindex.tools` surfaces.

## [0.23.0] - 2026-06-09

### Added
- **Grounded Sim** — the supervisory check-in now reviews WITH relevant graph context instead of blind. `build_sim_grounding` injects top related concepts/decisions (hybrid search) plus active constraints/watches into the supervisor prompt, deduped and char-capped by `sim.grounding_chars` (default `1500`; `0` disables). Sim can now flag a constraint being violated, a known watch, or a decision being contradicted that the conversation window alone wouldn't reveal. (Outcome of a Sim-vs-multi-lens review experiment: grounding the single persona beat building a panel.)

### Fixed
- Hermetic test fixture now covers the default embedding provider (Voyage): provider keys are derived from `vectors.PROVIDER_DEFAULTS`, so `VOYAGE_API_KEY` (and future providers) can't leak in from the environment and trigger live embedding calls during tests.

## [0.22.0] - 2026-06-08

### Added
- **Codex SessionStart parity** — `kin setup-codex-hooks` now installs a SessionStart hook so Codex sessions begin with the same auto-primed context and "use kindex" directive as Claude Code. `kin prime` gained `--adapter {plain,claude,codex}`; `--adapter codex` emits the `hookSpecificOutput.additionalContext` envelope Codex ingests.
- **`reminders.remind_kindex_usage`** (default `true`) — toggle the injected "use kindex" session directive; set `false` per-project in `.kin/config [reminders]` to suppress the nudge.
- **Project-graph (`.kin/`) guidance** in the session directive — agents are told to discover the `.kin/` directory for the files they touch (not just the cwd root) and to stage/commit `.kin/` changes alongside the related code.
- **Stigmergic pheromone ranking and session-end reinforcement** — injection trails (deposit / reinforce / decay) feed an auto-ramping ranking signal, and an opt-in session-end grader reinforces the injections the agent actually used (`attention.pheromone_*`, `attention.reinforce_*`).
- **Sim supervisory check-in** (opt-in) — an async supervisor that periodically reviews the conversation window and surfaces guidance through the attention channel (`kin sim`, `[sim]` config).

### Fixed
- Test suite is now hermetic: provider API keys no longer leak from the ambient environment into tests, fixing a flaky extraction-dedup test and preventing accidental live-API calls (and spend) during `pytest`.

## [0.21.3] - 2026-05-30

### Changed
- Generated `.kin/index.json` now uses canonical stable ordering, sorted domains/JSON keys, and source-derived time metadata instead of wall-clock generation time.
- Code-map export now sorts nodes and edges canonically and derives `analyzedAt` from Git commit time or latest code-node time.
- Current-user provenance now prefers repo-local Git `user.name`, then global Git `user.name`, then OS username.

### Fixed
- Repeated `.kin` snapshot exports of unchanged source no longer churn Git diffs because of run-time timestamps or unstable ordering.

## [0.21.2] - 2026-05-28

### Fixed
- Lightweight dream no longer repeatedly scans and sorts the pending suggestion backlog while creating duplicate suggestions.
- Scheduled dream runs now cap new suggestion writes per run with `reminders.dream_max_new_suggestions` (default `100`).

### Changed
- Schema v6 adds suggestion indexes for recent pending reads and pair-existence checks.
- Dream duplicate detection skips content similarity work when title similarity is too low to meet the configured threshold.

## [0.20.0] - 2026-05-19

### Added
- Short-lived agent coordination plane with `coord_start`, `coord_post`, `coord_read`, `coord_list`, and `coord_end` MCP tools plus matching `kin coord` CLI commands.
- Expiring task claims with `task_claim` and `task_release` MCP tools plus `kin task claim`, `kin task release`, and cleanup support.

### Changed
- MCP agent guidance now distinguishes operational coordination messages from durable knowledge capture.
- Task formatting shows active claim ownership when present.
- Default embedding provider is now Voyage so vector search uses the pure-HTTP first-class provider by default; local `sentence-transformers` remains available via `embedding.provider: local`.

## [0.19.0] - 2026-05-15

### Added
- Project-scoped config resolution via explicit `--project-path`, `KIN_PROJECT`, git worktree root, then cwd.
- `work_policy` config model and `kin policy [show|check]` for opt-in project policy enforcement.
- Git hook install now adds a pre-commit policy check and pre-push policy check before surfacing constraints.
- `.kin/.gitignore` pattern for tracking project context while ignoring local/private runtime state.

### Changed
- `.kin/config` is now treated as a git-shipped project contract rather than local-only cache.
- MCP agent guidance now tells agents to read tracked `.kin/config`, check policy when shell access exists, and enforce Linear only when the repo opts in.
- MCP server metadata updated to current package version and tracked `.kin` behavior.

## [0.18.0] - 2026-05-03

### Added
- Codex support: `kin setup-codex-mcp` registers `kin-mcp` in `~/.codex/config.toml`.
- Codex-facing `AGENTS.md` directives via `kin setup-agents-md`, including proactive search, capture, tasks, and session lifecycle guidance.
- `codex-sessions` adapter for ingesting saved Codex JSONL sessions from `~/.codex/sessions`.
- Agent-facing MCP usage guide at `docs/mcp-agent-guide.md`.

### Changed
- README, static docs, privacy copy, and MCP server description now present Kindex as an MCP memory layer for Claude Code, Codex, and other MCP-capable agents.

## [0.17.0] - 2026-04-11

### Added
- **Voyage AI embedding provider** (`vectors.py::_embed_voyage`) — Anthropic's officially recommended embeddings provider. Pure-HTTP via `urllib.request`, no native dependencies. Default model `voyage-3.5` (1024-dim), supports `voyage-3-large`, `voyage-3.5-lite`, `voyage-finance-2`, `voyage-law-2`, `voyage-code-3`. Configure via `embedding.provider: voyage` in `kin.yaml` and set `VOYAGE_API_KEY` in the environment. Generous free tier (200M tokens) makes it effectively free for typical use.

### Changed
- **`vectors` extra no longer pulls `sentence-transformers`.** The code already handled the import gracefully (try/except ImportError with fallback to FTS5), but the pyproject declaration was unconditionally installing it and transitively pulling `torch` and `scikit-learn`. On macOS, those two wheels ship incompatible `libomp.dylib` install names and crash Python with an OpenMP duplicate-registration abort when both load together. Users who want local embeddings now opt in explicitly: `pip install sentence-transformers`. API-based providers (voyage, openai, gemini) are the first-class path and require no native deps.
- `all` extra similarly no longer pulls `sentence-transformers`.

### Fixed
- `__version__` in `src/kindex/__init__.py` was stuck at 0.16.1 despite 0.16.2 shipping; now synced to 0.17.0.
- README version badge was stuck at 0.16.1; now 0.17.0.

## [0.15.0] - 2026-03-24

### Added
- YAML frontmatter on all skill SKILL.md files (kindex-capture, kindex-learn, kindex-prime) so they register properly when loaded as a Claude Code plugin
- `UserPromptSubmit` hook in plugin hooks.json (migrated from global settings)

### Changed
- Plugin version synced with package version (was 0.4.0, now matches 0.15.0)

## [0.14.1] - 2026-03-24

### Added
- `dream` MCP tool — knowledge consolidation available natively in Claude Code sessions
- CHANGELOG.md — retroactive changelog covering all releases from 0.4.0

## [0.14.0] - 2026-03-24

### Added
- **Dream cycle** (`kin dream`) — post-session knowledge consolidation: fuzzy dedup, suggestion auto-apply, domain edge strengthening
- `dream_deep.py` — LLM-powered cluster summarization via `claude -p` (structurally separated)
- `dream` MCP tool — consolidation available natively in Claude Code sessions
- Dream integrated into cron cycle (step 11, lightweight mode)
- Stop hook spawns detached dream on session exit (`start_new_session=True`)
- File locking (`fcntl.flock`) prevents concurrent dream cycles
- Pact contract-first artifacts (task.md, sops.md, constraints.yaml)
- 32 new tests (980 total)

## [0.13.0] - 2026-03-24

### Added
- `.kin/` directory migration — old `.kin` config files auto-upgrade to `.kin/config`
- Repo-scoped index writes (filter by code-mod/code-sym prefix)

### Fixed
- `.kin` directory vs config file collision in `config.py`

## [0.12.0] - 2026-03-23

### Added
- Multi-provider embedding support (local sentence-transformers, OpenAI, Gemini)
- Configurable via `embedding.provider` in kin.yaml

## [0.11.0] - 2026-03-22

### Added
- **Conversation modes** — reusable session-priming artifacts based on research (5.4x improvement over direct instruction)
- Five built-in modes: collaborate, code, create, research, chat
- `kin mode [activate|list|show|create|export|import|seed]`
- PII-free export for team sharing

## [0.10.0] - 2026-03-21

### Added
- **Code adapter** — ingest repository structure via ctags, cscope, tree-sitter
- Module nodes (artifact) with structural summaries
- Symbol nodes (concept) with method signatures
- Import/inheritance/call-graph edges
- Incremental re-ingest via file hashing

### Fixed
- `.kin/` directory collision with `.kin` config file

## [0.9.0] - 2026-03-18

### Added
- `--tags` support for `add`, `search`, and `list` across CLI and MCP
- Task lifecycle with graph-connected work items
- Slow graph archive with rotation (50MB or 365d)
- Graph health tools (`graph_heal`, `graph_merge`, `suggest`)
- Mechanical smoke tests (258 tests, 34 files via Pact adopt)
- Claude-web adapter for ingesting Claude.ai conversations

### Fixed
- Missing mcp dependency error now shows graceful message
- CI installs mcp dependency for smoke tests

## [0.7.0] - 2026-03-14

### Added
- **Reminders** — natural language scheduling, recurring rules, multi-channel notifications
- Actionable reminders with shell commands and `claude -p` execution
- Stop guard — blocks session exit when actionable reminders pending
- Adaptive scheduling (launchd/crontab interval adjusts to nearest reminder)

## [0.5.0] - 2026-03-10

### Added
- **Session tags** — named work context handles replacing resume files
- `kin tag [start|update|segment|pause|end|resume|list|show]`
- 16 MCP tools

## [0.4.2] - 2026-03-08

### Added
- Cache-optimized LLM retrieval — three-tier prompt architecture with Anthropic caching
- Layered config (global -> local merge, like git config)
- Adapter protocol for extensible ingestion with entry-point discovery

### Fixed
- FTS5 search failing on natural language questions
- Terminal block formatting (white-space: pre)

## [0.4.1] - 2026-03-07

### Fixed
- Schema migration for existing databases
- Server.json description within 100-char registry limit

## [0.4.0] - 2026-03-06

### Added
- Initial public release
- SQLite + FTS5 knowledge graph
- Hybrid search (FTS5 + graph BFS + RRF merge)
- Five context tiers (full, abridged, summarized, executive, index)
- MCP server for Claude Code integration
- CLI with core commands (search, add, context, show, list, ask)
- Node types: concept, document, session, person, project, decision, question, artifact, skill
- Operational types: constraint, directive, checkpoint, watch
- Weight decay and audience scoping
