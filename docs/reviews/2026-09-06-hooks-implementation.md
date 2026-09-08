# Hooks 2 implementation and local installation

2026-09-06. This supersedes the installation status in the earlier
[source audit](2026-09-06-claude-hooks-2-review.md), not its retained red probes.
Development changes are based on Kindex `ef99f86ba9a2fb39fd080705395617f42cf6a9b7`
and signet-eval `7da3b17440b43fb49e152acbffb30f082ae72ed7`. No commit, public
release, website deployment, or 1.0 compatibility certification is claimed.
Existing `sim.py` composition work and the three-product architecture note were
preserved. Source versions remain 0.36.0 and 3.12.2, respectively.

## Implemented contract

- Kindex owns durable tasks and repository knowledge. signet-eval owns local-model
  policy and host redaction; separate outward-world Signet is not a dependency.
- Modern and legacy Claude adapters are explicitly selected. Modern installation
  retires exact owned command handlers; it does not import their implementation.
  Kindex advisory context no longer emits a permission auto-allow.
- Typed task operations commit effect, replay receipt and applicable outcome
  outbox atomically. Signet admission binds original native source fields and
  sanitized semantic target. Expired admission does not erase confirmation of an
  already committed effect. Explicit recovery delivers old-session outcomes
  without repeating effects; audit evidence is caller-reported, not attestation.
- Native task-list tools route to Kindex or explicitly refuse unsupported fields.
  They do not fall back to an ephemeral list when a handled operation fails.
  Agent execution controls remain separate. Stable caller operation IDs, not
  payload-derived IDs, are required to retry uncertain mutations.
- Modern storage is the actual worktree's ignored `.kin/local/kindex`, with no
  implicit Personal or clone-configured path fallback. `.kin/knowledge.json`
  carries explicitly selected public/team evidence and relationships, preserving
  the union of versions. Import quarantines candidates for local review; it does
  not import Company authority or equate content hashes with signatures.
- Kindex's shared sanitizer covers named persistence, logging, CLI/MCP output,
  export and model/embedding payload boundaries. It preserves ordinary hashes and
  IDs. Historical secret-bearing records are not silently rewritten or promoted.
  The separate Rust signet-eval sanitizer protects its sinks and supported host
  prompt/tool-result projections. There is no secret reinsertion cache.

## Review dispositions

Constrain, Sim and Advocate were actually executed. They are advisory evidence,
not approval substitutes. The implementation review packet was frozen initially;
subsequent repairs changed the source. The final checks below bind the repaired
files, not a claim that a stale review packet magically reviewed later changes.

Constrain completed two implementation challenge rounds, session
`8d33ef42-3a1d-4570-ad65-ff54c2467be4`. Its initial low-token invocation returned no
text and was retried with a larger bounded budget. No synthesis or completion
certificate is claimed. Accepted challenges drove explicit operation identity,
error taxonomy, native field validation, tracked-local-store refusal, provenance
and bounded outcome recovery.

The root Advocate run completed four personas without provider errors, producing
67 raw observations, not 67 verified defects. A separate privacy run completed
two personas with 28 raw observations. Concrete repaired findings include:

- Sanitized-placeholder spoofing, escaped and numeric sensitive fields, logging
  extras, and a measured quadratic URL-userinfo scan. The failing 100 KB case went
  from 6.56 seconds to 0.0017 seconds after fixing the actual expression.
- Read helpers committing inside task/publication transactions; source snapshot
  and artifact locks now survive the whole mutation/publication boundary.
- Cross-store publication races, omitted relationship proposals, terminal-import
  duplication and partial validation before candidate staging.
- Protocol/readiness validation, pinned executable failure, source-input policy
  binding, ownership changes, expired read identity and poisoned outbox starvation.
- Partial plugin asset copying, unknown legacy wrappers, and historical output
  projection redaction before truncation.

Refuted findings included candidates entering node FTS, an absent task/outbox
transaction, a 500-item cursor off-by-one, three describe calls per operation,
and native cancelled tasks being reported completed. Tests exercise the actual
paths. Recommendations to broadly delete unknown wrappers, replace operation
IDs with request digests, quarantine every legacy row, or introduce a second
Company authority were rejected against the controlling compatibility contract.

Sim challenged blanket secrecy, API-stability claims, unsigned receipt authority
and the difference between replay confirmation and current admission. The result
is explicit boundaries, not a new security daemon or a premature Rust rewrite.

The code-review skill organized the review around intent, architecture, tests,
correctness, separation, redundancy, clarity and scope. The ask-cpa skill applied
simplicity, evidence, conventions and process in order. Predicted feedback:

1. [ESTABLISHED] "Keep one owner for each rule." Policy stays in signet-eval;
   Kindex retains the only task/graph database and its own sink hygiene.
2. [ESTABLISHED] "Show the actual host, not only mocks." Real Claude loading and
   a real-model retrieval-to-task workflow were exercised; failures were retained.
3. [ESTABLISHED] "Don't hide partial success." Confirmed task state survives audit
   delivery failure, while pending outcomes and unsupported native fields remain
   explicit. Custom tools return the host's required text-result shape.
4. [ESTABLISHED] "Make the migration reversible." Exact handler retirement,
   staged assets, settings backups and preservation of disabled state were tested.
5. [GUESS] "Is this ready to become the default?" Not yet: early-access host
   qualification and missing modern lifecycle parity are documented separately.

Predicted verdict: "The tested preview is usable; don't call it the 1.0 release."
This is a prediction, not human approval.

## Verification

- Kindex final full suite: **2162 passed in 89.55 seconds** with the checkout explicitly
  selected through `PYTHONPATH`. Focused counts overlap and are not added to this.
  A clean-room package check found the source archive included an ignored local
  lock file. Explicit source-distribution exclusions now fence `.kin/local/**`,
  with a regression check; no database bytes were in the initial artifact.
- signet-eval: **247 Rust tests passed**, release build, formatting and diff checks
  passed. Its checked-in `tests/claude_function_host.py` runs actual Claude 2.1.263
  with a deterministic loopback model provider in isolated configurations.
- Final wheel and source archive exclude local runtime state. All four modern
  assets, including the hidden plugin manifest, ship correctly. A fresh external
  virtual environment with isolated Python and no editable-path contamination
  passed `pip check`, **149 installed-wheel tests**, and CLI durable create/replay/
  list plus sanitized-capture checks. All 70 packaged files match checkout and
  installed bytes. Evidence: `/tmp/kindex-dist-final.CPPtJw/verification.json`.
  Wheel SHA256: `173b085698622a6b526d1eabb58b28692b375acca0de10b479d29f34205c222e`.
  Source archive SHA256: `b82eaa1201224270c803af00669b0ac70cb3994dc27ec68d681d0a4f0148b497`.
- Actual-host cases: prompt/tool-result masks, disabled neutrality, both plugin
  orders, semantic target denial, owner loss after selection, original native
  subject denial, Unicode digest agreement, settings-only feature activation,
  and the embedded Signet installer without a source checkout.
- Real Claude 2.1.263 with Haiku explicitly called Kindex memory, TaskCreate and
  TaskList. A separate process confirmed one durable task with the unseen seeded
  repository requirement; zero tool errors and no native task store. Two attempts
  cost $0.04130545 total. The first exposed the custom-tool result-shape defect;
  the repaired frozen-plugin rerun passed.
- A real second Git clone transported two selected team records without local
  SQLite state. Import produced zero nodes/edges and zero context hits before
  explicit review. Afterwards the decision rationale, directed relationship and
  provenance survived, and context retrieved both records. Private/Personal and
  wrong-repo decoys stayed absent; a clone-configured Personal path was ignored.
  Evidence: `/tmp/kindex-clone-latency.9o1iZ9/summary.json`.
- Workstation-only fresh CLI RPC samples on that two-node/one-task graph with
  explicitly disabled Signet: 12 warm context calls median 158.31 ms, nearest-rank
  p95/max 403.53 ms; 12 native TaskList calls median 157.53 ms, p95/max 785.50 ms.
  First-after-setup calls were 149.46/148.01 ms, not OS-cache-cold measurements.
  These small samples are not production or large-graph performance evidence.
- Both worktrees passed `git diff --check`. The actual repository database is
  ignored, while the selected ownership/storage/compatibility decisions are in
  `.kin/knowledge.json` for Git transport.

Local review artifacts: `/tmp/kindex-hooks-implementation-review.oG8khh/`,
`/tmp/kindex-secret-audit.kUleBB/implementation-review-disposition.md`,
`/tmp/kindex-task-hook-audit.ouvzRs/advocate-triage.md`, and
`/tmp/kindex-real-model-rerun.Iwg2O3/summary.json`. These are local evidence, not
durable distributed test fixtures. Signet qualification is recorded in its own
`docs/reviews/2026-09-06-function-hooks.md`.

## Local installation and the earlier blocker

Homebrew now supplies `claude-code@latest` **2.1.263**. The updated local
signet-eval binary embeds its plugin and was installed at
`/Users/jmcentire/.cargo/bin/signet-eval`; the prior binary is retained under
`signet-eval-backups/signet-eval-before-hooks-2-20260906-9c4852eb` beside it.
The installed binary SHA256 is
`e4bab003039b0d4d7baadf277fa62b8ca322a3a398fb99d158f91cf54ba464d9`.

Both `signet-eval-functions@skills-dir` and `kindex-modern@skills-dir` appear loaded
in `claude plugin list`. The function flag is set in user settings. One exact
Signet and seven exact Kindex legacy handlers were retired, including the
separately inspected custom prompt-recall wrapper. Four unrelated archive,
prompt-lifecycle and project Stop handlers remain. Their files were not deleted.
The current `kin` installation imports this development checkout.

Settings backups are at
`~/.claude/signet-adapter-backups/38d8491fd2397d0ac532c1a503436eaa/settings.json`
and `~/.claude/settings.kindex-backup-d4a1c05f73c3.json`.
The original `~/.signet/disabled` marker remains byte-identical. Consequently,
Signet policy and host redaction are **not currently active**. Kindex's sink
sanitizer is active. `kin integration-doctor` reports modern mode, repo-local
storage, no Personal fallback and the explicitly disabled owner. Restart Claude
to establish new-session hook state; listing plugins is not proof about an
already-running session.

The earlier approval blocker was signet-eval's locked `protect_hook_config` rule,
invoked through the Codex PreToolUse/PermissionRequest adapters in
`~/.codex/hooks.json`. Its settings-name check treated read-only inspection as
configuration mutation. Exact native Read/Grep/Glob tools are now exempt; Write,
Edit and arbitrary Bash text remain guarded. The disabled switch already unblocked
this work. It was not removed, and Codex adapter configuration was not altered.

## Remaining boundaries before a default/major release

The implementation is a locally installed preview, not every opportunity in the
audit. Automatic modern lifecycle coverage is prompt retrieval and turn capture;
legacy dream/reinforcement, PreCompact capture, reminder stop-guard and coordination
polling are not covertly retained. Their CLI/MCP/background interfaces still exist.
Full lifecycle parity, broader host/platform/streaming qualification and production
latency evidence remain separate work. Guildhall's signed Company/repository
authority is not implemented or replaced here. No public version bump was cut.

Host pre-hook raw prompt enqueue logs, assistant output and third-party telemetry
remain outside the redaction guarantee. Arbitrary encoded secrets, hostile
same-user processes, and historical cleanup are also outside scope. Hook loader
failure remains a host boundary; an absent or skipped plugin cannot enforce its
own failure policy. Receipts have no automatic pruning yet. These limits are not
disguised by passing unit tests or the one successful real-model workflow.
