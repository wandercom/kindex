# Claude function hooks (development preview)

Kindex owns persistent knowledge and task state. **signet-eval** owns policy and
host-wide redaction for local coding agents. **Signet** is the separate product
for models acting outside the local coding environment; it is not a third
installation dependency for this integration.

The optional function-hook adapter is qualified against Claude Code **2.1.263**.
The host interface is early access, not Kindex's stable core API. The installer
sets `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` in Claude's user settings. A normal stable Homebrew cask
may lag the `claude-code@latest` channel.

## Install or roll back

```sh
# With the development signet-eval binary that includes the integration protocol:
signet-eval integration install-modern
kin setup-hooks --mode modern --dry-run
kin setup-hooks --mode modern
claude
kin integration-doctor

# Return to the compatible command-hook adapter:
kin setup-hooks --mode legacy
```

The modern plugin is installed at `~/.claude/skills/kindex-modern/`, following
Claude's [skills-directory plugin contract](https://code.claude.com/docs/en/plugins-reference#skills-directory-plugins).
Restart or `/reload-plugins` after changing the plugin. Function-hook activation
is a launch setting: installation alone does not prove it is running. The
installer removes exact owned legacy handlers, preserves foreign sibling
handlers, disables known legacy plugin identities, and backs up settings before
changing them. Unknown wrappers need explicit operator review. Modern execution
does not import or invoke the old shell hooks. Legacy remains the default during
the transition and can be uninstalled independently.

The signet-eval binary embeds its plugin; no Signet checkout or third package is
needed. Its installer keeps backups under `~/.claude/signet-adapter-backups/` and
does not change the enforcement disabled marker. Kindex keeps its own adapter and
settings backups. Restore the matching settings and plugin backups to undo a
Signet adapter migration; `kin setup-hooks --mode legacy` handles the Kindex lane.
An old signet-eval binary without `integration` must be upgraded first, not treated
as absent. A reviewed custom Kindex wrapper can be retired with the exact
`--retire-command COMMAND` option; unrelated handlers are preserved.

## Ownership and persistence

| Concern | Owner |
| --- | --- |
| Task state, claims, dependencies, versions, operation replay | Kindex |
| Local-model admission policy and host interception | signet-eval |
| Kindex's own storage, log and model-egress credential minimization | Kindex |
| Company knowledge authority | Guildhall, when separately available/configured |

Each coding session binds the actual Git worktree and session explicitly. The
modern lane uses `.kin/local/kindex`; `.kin/.gitignore` excludes `local/`. It does
not read clone-controlled `data_dir` values or silently fall back to Personal
memory. Tracked local databases and linked storage targets are refused. Existing
profile-based CLI and legacy MCP interfaces remain available; modern mode does
not silently move their data. Worktree-local tasks are intentionally not shared
with another worktree's local database.

Supported native `TaskCreate`, `TaskGet`, `TaskList`, and `TaskUpdate` operations
route to Kindex. `TodoWrite` is denied with a durable alternative. Unsupported
native metadata/dependency merge requests are denied rather than partly applied;
use the registered Kindex task tool's explicit fields and `expected_version`.
`TaskStop`, `TaskOutput`, and `Agent` are execution controls, not task-list tools,
and this adapter does not intercept them. Native lists over 500 items direct the
caller to the paginated typed service. Cancelled tasks are absent from the native
view and remain available as cancelled through Kindex.

Typed mutations accept `operation_id`; supply the same ID to retry an uncertain
outcome. The host call ID is the default, not a guarantee that another model call
will reuse it. A different payload under the same scoped ID is rejected. Task
effect, receipt, and any signet-eval outcome-delivery entry commit in one SQLite
transaction. Delivery is retried without repeating the task effect. Receipt
lookup confirms a past effect even after admission expires; it does not grant a
new effect or certify execution independently. These are local process records,
not Guildhall signatures or a defense against another process with the same
OS user's privileges.

`kin integration-reconcile` retries pending outcome delivery from earlier
sessions in the same worktree using their original receipts. It does not
authorize new task effects. Recovery is bounded to 16 attempts and a 20-second
delivery deadline; pending or corrupt records remain visible for operator repair.
Receipts and pending outcomes currently have no automatic retention pruning.

When signet-eval is active and ready, the bridge requests authorization for the
exact semantic Kindex operation before mutation. A missing/malformed owner or
owner loss blocks the operation; it never executes an ephemeral Claude fallback.
Changing a selected owner requires explicit plugin reload. An explicitly disabled
signet-eval remains disabled; installation does not re-enable it.

## Knowledge with the repository

Prompt-time retrieval uses the prompt against the repo-local graph. The status
row reports actual retrieval and open-task results, not a claim of completeness.
Turn summaries enter `capture_candidates`, not trusted graph nodes. Large captures
are bounded and marked truncated; review the full source before promotion.
Candidate review uses Kindex's existing exact-content promotion contract.

This preview's automatic lifecycle coverage is prompt retrieval and turn-end
candidate capture. It does not run the legacy Stop dream/reinforcement jobs,
PreCompact capture, reminder stop-guard, or coordination polling. Their existing
CLI/MCP/background interfaces remain available; use legacy mode if those automatic
events are required. Modern mode intentionally does not silently keep their shell
handlers alive. Reminder/coordination UI and full lifecycle parity remain separate
qualification work before choosing a new default.

```sh
# Explicit public/team nodes only; no private graph dump:
kin repo-memory publish NODE_ID ANOTHER_NODE_ID
# In another clone: stage evidence for review, never install it as policy:
kin repo-memory import
```

`.kin/knowledge.json` transports semantic text and selected-peer relationships.
Publication preserves a content-addressed union instead of regenerating from an
incomplete database. Different versions of one logical node remain distinct
records. Hashes check bytes, not trust. Imports are quarantined candidates and
cannot immediately influence authoritative context. This is not a replacement
for Guildhall's signed repository manifests/events. No Company federation or
cross-worktree synchronization is implied.

## Redaction boundaries

Kindex minimizes recognized credential formats and explicit sensitive fields
before its named storage/logging/export/model-egress boundaries. Hashes, task IDs,
email addresses, and IP addresses are not erased merely for length or entropy.
Literal secrets in executable stored actions or identity-bearing references may
be refused rather than silently changing their meaning. Prefer environment
references and a secret manager for execution credentials.

This does **not** promise every Claude log is secret-free. Actual 2.1.263 probes
show raw prompt enqueue records preceding function hooks. Hook-load failures are
also controlled by the host. Kindex does not rewrite that host history, rotate
credentials, or sanitize arbitrary external application logs. Its sink sanitizer
remains active even with signet-eval installed; Kindex does not register a duplicate
host redactor. Historical secrets require a separately authorized remediation.

## Version and Rust boundary

A future 1.0 can promise Kindex's typed operations and data formats; it must not
promise that Anthropic's early-access API is stable. Adapter qualification is
versioned separately and a host upgrade requires requalification. This preview
does not cut or publish a 1.0 release. The enforcement/redaction host adapter is
in the existing Rust signet-eval product; Kindex's task/graph engine stays Python
pending measured reasons to port it. No second Rust task or knowledge database
is introduced.
