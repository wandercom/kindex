# Claude Hooks 2.0: Kindex revision review

Reviewed 2026-09-06 against `ef99f86ba9a2fb39fd080705395617f42cf6a9b7`, version
0.36.0. Existing `src/kindex/sim.py` changes and the untracked three-product
architecture review were preserved. This document records findings and a proposed
revision; no Kindex runtime changes or release are claimed.

## Outcome and controlling requirements

Function hooks offer a useful host adapter for Kindex: direct context injection,
task interception, session state and UI. The core work is secret handling, task
correctness, explicit knowledge authority and reproducible installation. The new
host API does not supply those contracts automatically.

The user's added compatibility requirement controls the design: keep existing
clients working during the transition, isolate the legacy adapter, and allow a
modern installation to avoid its imports, processes, polling and duplicate events.
A major version can establish these contracts without removing legacy support.

The subsequent coexistence instruction is equally controlling: where Signet-eval
and Kindex overlap, the appropriate authority leads and the other masks its
duplicate behavior. This is per-capability ownership, not installation order.

Preserve the existing product direction: Personal, Company/Guildhall and Codebase
are separate authorities. Coding composes Company + Codebase, with Personal
excluded by default. Writes explicitly target an authority. This work must compose
with the existing Guildhall design rather than introduce another authority router.

## Verified host contract and installation

Homebrew's `claude-code` cask supplied 2.1.236. `brew update` and a normal upgrade
confirmed it was current for that channel. With the user's authorization, the
installation was switched to `claude-code@latest`, which supplies 2.1.263. The old
cask was uninstalled without `--zap`; settings and session data were preserved.
`claude --version` and Homebrew's installed receipt both report 2.1.263.

An isolated `/plugin-types` invocation against the installed binary succeeded with
all MCP servers/tools disabled, no session persistence, and a dummy local API URL.
The generated API explicitly identifies itself as early access and changeable
between releases. Function hooks require `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`.
This review did not globally enable the flag or install a Kindex function plugin.

Authoritative references:

- [Anthropic function-hooks proposal and demos](https://github.com/anthropics/claude-code/issues/91870).
- [Current hook reference](https://code.claude.com/docs/en/hooks).
- [Homebrew stable cask](https://formulae.brew.sh/cask/claude-code) and
  [latest cask](https://formulae.brew.sh/cask/claude-code@latest).

Important distinctions from actual declarations and synthetic runtime probes:

| Surface | Observed contract | Kindex implication |
| --- | --- | --- |
| `prompt.submit` | Rewrites the model request and main user transcript row, but a preceding queue/enqueue transcript record retained the original prompt in the tested headless path | Cannot promise all-transcript secret removal |
| `tool.call` | Returning a fresh, valid sanitized result worked for Bash; changing the tool name was rejected and original Bash ran | Task translation must short-circuit and explicitly invoke Kindex |
| Invalid result shape | Incomplete replacement Bash result became an error result in the probe | Validate schemas; do not conflate every error with the same fallback |
| Hook exception | A throwing prompt hook was skipped and the raw prompt proceeded | Host hooks alone do not establish mandatory enforcement |
| `turn.complete` | Rewriting the event did not replace the original assistant transcript/output in the probe | Do not claim assistant-output sanitation through this event |
| `$.tool.call` / `$.mcp.call` | Tool call retains permission flow; direct MCP call has no permission prompt because plugin installation is the grant | Treat direct calls as explicit capabilities, with narrow destinations |
| `$.store` | Persists plaintext JSON in the Claude config directory | Store neither secret mappings nor authoritative task state there |
| `session.repo().root` | Returns the main worktree root for linked worktrees | Bind the actual operation's worktree separately |
| Ordinary `PostToolUse` | Current docs say original output is captured for telemetry before rewriting | A successful replacement is not proof that all sinks saw sanitized text |
| `MessageDisplay` | Display-only replacement | Cosmetic masking is not transcript protection |

All runtime tests used synthetic strings and a local mock provider. They do not
establish coverage of every interactive, Desktop, streaming or telemetry path.

## Source findings, in implementation priority

### 1. Advice currently changes permission state

`src/kindex/agent_adapters.py:160` emits `permissionDecision: allow` whenever a
Claude PreToolUse advisory is rendered. The advisory path should be structurally
limited to context; authorization belongs to a separately defined policy path.
A synthetic call reproduced the envelope. This is a source/contract finding,
not a demonstrated bypass of every other host deny rule.

### 2. Secret handling is missing at Kindex-owned boundaries

There is no common sanitizer before persistence and provider egress. Synthetic
canaries survived degraded logs (`config.py:1032`), inbox (`hooks.py:629`), graph and
activity records (`store.py:1545`, `:1284`), candidates (`store.py:2504`), archives
(`archive.py:228`), attention metadata queues (`attention.py:1127`), and mocked
extraction/reinforcement provider requests. Reminder outputs are another source
path (`actions.py:161`). Snapshots, WAL and FTS must be in the coverage inventory.

The existing public/org export sanitizer only touches content (`cli.py:1683`);
exported titles and referents remain unsanitized (`:1795`, `:1804`). Its generic
40-character token regex (`:1693`) also erases legitimate SHA256 evidence. These
are defects in existing export behavior. Other findings describe gaps against
the requested new protection, not evidence of an actual credential incident.

Use one versioned, field-aware policy before controlled writes and network calls,
plus an explicit outward projection. Sanitize free text; validate structural
identifiers instead of corrupting them. Entropy is a signal, not sufficient proof
that a hash or ID is a secret. Email/IP filtering needs its own configurable policy.
On sanitizer failure, reject the affected write/egress and emit fixed safe
diagnostics. Preserve useful offline reads where safe; do not deadlock the agent.

Preserve transactions: candidate promotion writes direct SQL atomically, whereas
the public `add_node` commits. Share pure normalization/validation rather than
blindly funneling all writes through a committing method. Sanitize before binding
new digests, and distinguish original evidence from its sanitized projection.

Historical cleanup requires a separate inventory and migration. Installing a hook
does not remove secrets from old transcripts, Git history, backups or third-party
logs. Secret-handle substitution, if later added, should use ephemeral,
destination-bound capabilities; a general Bash reinsertion mechanism is not
required for Kindex's memory role.

### 3. Repair tasks before making them Claude's sole task authority

Native task tools are currently neither blocked nor redirected. MCP exposes
add/list/done/claim/release (`mcp_server.py:1998`), but lacks complete task-specific
get/update/cancel, dependency and operation-identity contracts. Core supports more
operations than MCP. README currently permits temporary host-local tasks
(`README.md:286`), so migration must change documented behavior explicitly.

Isolated probes reproduced these existing defects:

- Reopening a completed task leaves the node archived, so open listing loses it.
- Claiming a completed task creates archived/in-progress state.
- Updating status to done retains a claim.
- MCP release with omitted agent skips the ownership comparison.
- Contextual tasks lack a conversation binding and disappear from strict prompt
  scope; project filtering matches `repo2` using the prefix `repo`.
- Retries duplicate creates; unknown links silently create orphans; `tomorrow`
  remains literal despite the MCP description promising date parsing.

See `tasks.py:132-255`, `:302-326`, `scoping.py:64`, and
`mcp_server.py:2019-2025`, `:2112-2123`. Listing caps precede some filters, so the
adapter also needs complete scoped pagination and truthful errors.

Expose one typed task service to CLI, MCP and hooks. Mutations need a stable
caller-supplied operation key, a queryable result receipt committed with the
mutation, defined retry retention, and atomic per-task version/claim transitions.
Do not use fuzzy title matching to recover a lost response.

The modern adapter should intercept exactly `TaskCreate`, `TaskUpdate`, `TaskGet`,
`TaskList` and `TodoWrite`. Keep `Agent`, legacy subagent `Task`, `TaskOutput` and
`TaskStop` distinct. A TodoWrite snapshot may only reconcile its explicitly owned
set; omission must not delete unrelated durable tasks. The UI projects Kindex
state. Migration of existing native tasks is explicit and receipt-backed.

### 4. `.kin/` presence does not mean portable semantic knowledge

`config.load_config` selects one data directory (`config.py:848`); MCP caches the
Store for the process (`mcp_server.py:164`). Neither expresses per-operation
Company + Codebase composition. `write_kin_index` (`ingest.py:1076`) exports only
repository-shaped code IDs in Git repositories. `_kin_index_node` (`:983`) omits
content and edges.

Synthetic proof: adding a team decision with rationale to a temporary graph and
exporting inside a Git repo produced zero nodes. A matching private code-node ID
was exported; basename slug alone cannot distinguish unrelated same-named repos.
Existing tests intentionally exercise the current restricted code projection.

The revision needs versioned semantic Codebase transport carrying admitted facts,
rationale, relationships and provenance, plus a consumer path in another clone.
Use the existing owner-issued repository identity design and a separate actual
worktree binding. A clone-controlled audience flag, root path or unsigned ID is
not authority to publish Personal/Company content.

Keep structured merging. Regenerating from one clone's incomplete SQLite graph
can discard remote-only knowledge. Immutable admissible events can be unioned and
projected deterministically; serialization order does not establish truth or
override a conflicting authority. Preserve old artifacts as legacy evidence
during explicit migration; do not silently reinterpret them as trusted new data.

### 5. Installation and lifecycle behavior need one specification

Packaged `hooks/hooks.json` and `setup.py` define different event coverage.
Installer ownership uses broad substrings such as dream/reinforce and can replace
whole entries containing unrelated sibling hooks (`setup.py:234-260`); uninstall
uses similar matching (`cli.py:5874`). Generate exact owned entries from one
manifest, preserving user overlays and third-party hooks. Detect duplicate plugin
and machine installs. Verify timeout units against each host schema; current
Claude values resemble milliseconds but the documented field is seconds.

`Stop` is turn completion, not session close. Capture and reinforcement have
overlapping triggers, advisory errors can disappear silently, and some work starts
before the internal attention deadline. Preserve quarantined capture and existing
quiet-mode behavior. Add an integration doctor for runtime/API capability,
selected adapter, duplicate handlers, graph destination, task availability and
semantic export readiness. Its output must distinguish unsupported, degraded,
unverified and operational states.

## Proposed compatible architecture

| Layer | Owns | Compatibility boundary |
| --- | --- | --- |
| Shared core | Sanitization policy, typed task transitions, source authority, capture admission, retrieval, durable receipts | Versioned API and data; no host-specific event semantics |
| Modern Claude adapter | Function middleware, structured tool results, UI and lifecycle translation | Optional installation; version/capability qualified |
| Legacy adapters | Existing command-hook envelopes and supported non-function hosts | Separate optional payload and execution path |
| CLI/MCP | Stable public access to the same services | Existing entrypoints remain usable; additive APIs first |

Select an adapter when installing/starting a session. Exactly one adapter owns an
event in that session. Do not install both and rely on every handler exiting
quickly; that still incurs legacy overhead. Do not switch task authority after a
failed mutation. A requested but unsupported modern mode reports that condition;
an explicitly selected compatibility mode can use the legacy path.

Backward compatibility preserves usable data and clients, not unsafe permission
grants or raw-secret logging. Old clients without operation IDs can continue under
their documented legacy semantics while the new bridge uses the stronger contract.
Do not invent historical idempotency or trust receipts. Do not globally quarantine
all existing useful memory merely because a new sanitizer marker is absent.

### Signet-eval coexistence

| Concern | Lead when both are active | What the other masks or retains |
| --- | --- | --- |
| Execution permission, dangerous operations and workflow guards | Signet-eval | Kindex masks duplicate host guards; its advice never grants permission |
| Requirement to use durable tasks | Signet-eval | Kindex supplies typed task operations, state and receipts; no second task policy engine |
| Task lifecycle, claims, dependencies and persistence | Kindex | signet-eval authorizes actions and consumes receipts; it does not mirror task state |
| Requirement for relevant retrieval/capture | Signet-eval policy using Kindex evidence | Kindex performs retrieval/capture and masks duplicate nags or blockers |
| Host-wide secret interception and enforcement audit | Signet-eval, once qualified | Kindex masks overlapping host redactors/audits, while retaining storage/egress protection |
| Graph admission, authority routing and repo projection | Kindex | signet-eval checks permission to act; it does not decide which knowledge is true or own a second graph |

This is a proposed contract, not a capability already implemented by either tool.
Kindex's own sink validation remains required for CLI/MCP/cron inputs and old data.
It protects a different boundary from host interception. Share policy definitions
and sanitized-envelope metadata where practical; do not copy signet-eval's rule engine
or trust an unverified caller's claim that input was sanitized.

Source audit of Signet-eval at `7da3b174` confirms existing overlap. Its locked
`prefer_persistent_task_store` rule (`src/policy.rs:1697-1713`) uses `^Task.*$`, also
denying execution-management tools while omitting TodoWrite. Its Kindex-engagement
rule accepts tag_start/tag_resume or search/context/ask in a recent action window
(`:1672-1677`). The vault matches tool or parameter substrings (`src/vault.rs:625`),
and allowed actions are recorded before tool execution (`src/hook.rs:560-586`).
That ledger is not proof that a search succeeded or retrieved relevant evidence.
signet-eval also currently logs raw parameter/preflight snippets (`src/hook.rs:509-515`,
`:574-585`); its secret-file rule (`src/policy.rs:1774-1780`) is not a transcript
sanitizer. Assigning it future host redaction ownership requires implementation
and qualification, not merely detecting its installation.

Define explicit configured ownership with protocol version, host/session/scope,
policy revision and capability semantics. Installed executable presence or a
healthy process alone cannot authorize masking. Conflicting or unsupported
ownership prevents activation of the contested mode; do not quietly select a
winner, disable both, or change task authority after a failed mutation. A session
uses one selected adapter and owner set; changing owners requires explicit
reconfiguration at a boundary without an in-flight mutation.
Owner readiness is checked for the affected operation class, with a bounded
validity interval and matching policy revision on each adjudication. Prior
installation/startup success is insufficient. If a previously selected signet-eval
owner expires or changes revision, Kindex-controlled delegated mutations become
unavailable until reconfigured; Kindex does not silently unmask its own enforcer.
This does not assert control over host actions that bypass a failed hook.

Honor signet-eval denial before mutation. Its current native Task deny would block a
transparent Kindex bridge; until signet-eval supports explicit delegated task intents,
keep the deny and direct Claude to Kindex tools. Do not reorder hooks to bypass
signet-eval. Future translation must expose both original intent and actual operation
to the selected enforcer, binding the result to the durable operation receipt.
Direct `$.mcp.call` and `$.process.run` must not bypass that policy path; the latter
even documents that its Git subprocesses disable repository Git hooks.

Use Kindex success receipts, not PreToolUse allow records, as workflow evidence.
Missing policy evaluation means no delegated mutation acknowledgement. Missing
post-commit receipt means unknown outcome and lookup by operation ID, not retry
under another owner. A failed host hook can still fall open: neither program may
claim global enforcement merely because ownership was configured at startup.
For delegated mutations, record the policy decision and operation intent before
execution. Commit Kindex's mutation, result receipt and pending audit-delivery
record together, then reconcile delivery after restart. signet-eval and Kindex do not
need a distributed transaction; pending delivery and unknown outcomes remain
explicit. Kindex sink containment is labeled separately from signet-eval adjudication
so successful sanitization cannot be mistaken for execution authorization.

No implicit new payload store is proposed. Erasure-sensitive evidence remains in
the existing explicitly owned private/company source and is excluded from Git;
shared rationale must be a useful, independently safe projection. If a referenced
source is unavailable or revoked, preserve that state without treating missing
evidence as verified truth. Payloads and diagnostics sent to signet-eval are named
egress surfaces with explicit fields/retention, not an exempt internal channel.
Standalone cron has no host-policy guarantee; task transactions still serialize
across processes and safe logs expose its own outcome without inventing a user
notification recipient.

A 1.0 release is a reasonable target for the defined public contract, migration
and supported adapter matrix. Retiring legacy support is a separate later change.
No version bump was made during this review.
Modern activation must verify that no owned legacy handler remains active for
its selected events; successful installation of new files alone is insufficient.

## Kindex-specific capability opportunities

| Capability | Useful new behavior | Boundary to preserve |
| --- | --- | --- |
| Retrieval | Inject relevant graph evidence when prompt, affected files or cwd changes | Bounded results, explicit source/validity; search counts do not prove relevance |
| Tasks | Native-looking task interactions backed by durable Kindex records | One authority; atomic mutations and explicit unavailable outcomes |
| Capture | Turn/compact/end capture into the existing review queue | Deduplicate event replay; never auto-promote merely to satisfy a capture quota |
| Repo knowledge | Show destination and pending semantic projection; include relevant changes in commit workflow | Explicit sharing authority; no automatic unrelated graph publication |
| UI | Active authority/worktree, task progress, relevant reminders, pending reviews, degraded state | Read projection, bounded refresh, quiet mode; no second state owner |
| Questions | Ask when missing authority or an unresolved constraint blocks a dependent action | Interruption budget; no quizzes or forced dialogue on every edit |
| Coordination | Project existing claims, active sessions and directed updates | Preserve expiry/ownership and recipient scope |
| Reminders | Surface due items and use existing host-wake mechanisms | Do not confuse a UI row with reliable background delivery |
| Maintenance | Batch extraction, indexing and reinforcement outside latency-sensitive events | Bounded queue, visible failures, no per-tool LLM dependency |

## Rust assessment

The function-hook adapter is TypeScript. Rust could own selected core components
or an eventual shared engine, but it does not solve missing authority or storage
contracts. The separately recorded Guildhall Rust direction remains its own work.

Seven warm import-only subprocess samples measured median 77.5 ms for config,
26.7 ms for CLI and 26.9 ms for hooks on this workstation. These are not complete
hook benchmarks. Measure real calls per turn, process/import cost, Store open and
query, sanitizer/search compute, and p50/p95/maximum before choosing a port.
Compare the existing persistent MCP topology against per-call subprocess cost.

If Rust is selected, preserve one owner for each rule, use protocol golden fixtures
and differential tests, and migrate through the adapter/service boundary. Avoid
simultaneous host-API, storage-format and full-language rewrites without separate
compatibility evidence.

## Implementation order and release evidence

1. Correct advice permission output, export field coverage and task transition
   defects with focused regression cases; consolidate exact hook ownership.
2. Add the common sanitizer and typed task/receipt contract with isolated sink,
   concurrency, lost-response and restart proofs.
3. Define and qualify the signet-eval/Kindex ownership contract, then ship the optional
   modern adapter against a qualified API, preserving the
   separate legacy path. Prove zero legacy event execution/import/process overhead
   when only modern integration is selected.
4. Integrate explicit Codebase authority and semantic Git transport with Guildhall;
   demonstrate a second clone retrieves the decision and rationale before a
   dependent edit, with Personal and wrong-repo decoys excluded.
5. Finish migration, docs, package/plugin contents, doctor and capability tests;
   then run full release gates and decide the major version. Include cold/warm
   latency, offline/degraded operation and supported-host compatibility.

Host evidence must include success, malformed hook output, hook exceptions,
timeouts, disabled hooks, replay/resume, streaming and parallel tools. A startup
probe demonstrates one run, not permanent enforcement. A detector depending on
the same failed hook cannot promise one-turn detection. Strong native-task
restriction requires separately qualified host policy/tool availability; otherwise
advertise the integration's demonstrated limits.

## Review and verification record

- Constrain: native conversation engine, isolated session
  `111dfa59-29c2-4f99-816c-8e746403731c`; two understand and three challenge rounds
  completed. Accepted emitter-only advice, exact installation ownership, separate
  negative-fixture processes, explicit adapter/task cutover, sanitization before
  signing/embedding, and capability-specific signet-eval handoff. The subsequent
  synthesis call failed with `API returned no text content`; no synthesized
  artifact or Constrain completion certificate is claimed. The saved interview
  and this source-grounded review remain the usable output.
- Simulacrum skill/CLI: three actual adversarial passes. Accepted fail-open limits,
  explicit operation receipts, sink inventory, per-operation authority and negative
  controls. Follow-up conceded rejection of regenerate-only merging, first-commit
  identity, and a cross-authority truth order. The follow-up incorrectly inferred
  that ignored tool-name changes prevent output redaction; the valid Bash-result
  probe disproves that. Its blanket quarantine of legacy rows is not adopted.
  Activity log already has `INTEGER PRIMARY KEY AUTOINCREMENT` (`schema.py:191`);
  no redundant sequence was justified.
  The final coexistence pass added per-operation owner readiness/revision,
  unavailable state after owner loss, distinct containment/adjudication evidence,
  transactional receipts with restart reconciliation, host-version/sink-qualified
  secrecy claims, and verification that legacy handlers are inactive. These are
  requirements for the proposed implementation, not claims about today's hooks.
- Advocate: four personas completed without errors; 52 raw observations were
  checked and reduced to five security finding groups. Invented export fields,
  unsupported live leaks and transaction-breaking suggestions were rejected.
- Existing focused suites: 71 index/profile/merge/project-config/session-routing
  tests and 38 core task tests passed (2 CLI task tests deselected). These passing
  tests do not cover or refute the synthetic counterexamples above. Full suite,
  release and end-to-end Kindex function-plugin behavior were not run or claimed.
- No real credentials or user transcript corpus were scanned. An automatic
  approval hook rejected a read-only inspection of installed Claude settings and
  a temporary installer-probe patch as configuration changes. Source inspection
  and isolated runtime tests completed the review, but machine-specific hook
  inventory remains unverified.

Local evidence:

- `/tmp/kindex-hooks-review.16OMxt/`: brief, storage repro, Constrain session and
  Sim follow-up packet.
- `/tmp/kindex-secret-audit.kUleBB/`: synthetic canaries, actual-source Advocate
  packet, JSON report and explicit dispositions.
- `/tmp/kindex-task-hook-audit.ouvzRs/`: task repros and detailed source audit.
- `/tmp/kin-hooks-contract.yp9P9o/`: official API declarations, mock provider
  traces and function-hook probes.
- `/tmp/kindex-brew-verify.3JBpKC/`: declarations generated by the upgraded
  Homebrew installation, in isolation.

These temporary paths aid local reproduction; they are not shipped artifacts.
The source findings and accepted design changes are also captured in Kindex.
