# Periodic reviews and independent health checks

Kindex can periodically review ongoing work against the user's goal, recent actions,
outstanding tasks, constraints, and validation evidence. The shared supervisor is
used by Claude, Codex, OpenCode, Antigravity, and Cursor. Reviews are advisory; they do not
authorize actions or certify completion.

## Configure the supervisor

Enable `sim` in trusted user configuration (`~/.config/kindex/kin.yaml`). Repository
configuration cannot select supervisor executables or override its spending policy.
The default cadence is six eligible events, not six wall-clock minutes. Review work
runs in a detached worker; a subsequent host event picks up a fresh result.

### Adjust one conversation while it is running

Use the host's raw session ID with `--scope instance`. Supervisor overrides must
be written to trusted user configuration with `--global`; project YAML cannot
authorize review spending. For example, increase review frequency and the total
conversation allowance for important work:

```sh
kin agent-config set sim.tick_interval 3 --client codex --scope instance --instance SESSION_ID --global
kin agent-config set sim.max_conversation_cost 3 --client codex --scope instance --instance SESSION_ID --global
kin agent-config show --client codex --instance SESSION_ID --config ~/.config/kindex/kin.yaml --json
```

For casual conversation, the same commands can set the interval to `30` and the
conversation allowance to `0.10`. These are examples, not built-in modes. Change
either setting independently at any time; the next hook reads the updated user
configuration. Already admitted reviews retain their configuration snapshot.
The explicit config path in `show` avoids project layering; `--global` selects
the write destination for `set` but does not restrict configuration reads.
Changing the allowance does not reset recorded spending, so lowering it below
what the conversation already spent stops further reviews.

The interval counts eligible host events, including tool events where the host
emits them; it is not a number of messages or a timer. Trivial windows may be
skipped, and quiet reviews do not produce an interruption.

`budget.daily`, `budget.weekly`, and `budget.monthly` also constrain reviews. They
apply to each store's ledger, not a machine-wide total across project stores.
The conversation allowance is cumulative across days. Native LLM reviews record
token-based costs; external commands reserve their configured allowance per
attempt, including failed attempts.

The diligence check asks for a small representative pilot with an explicit expected
outcome before increasing concurrency or committing a long run. Progress must be
measured against that outcome: for example, the unclassified count decreases and
completed documents stay completed across restart/rebuild. Busy workers, repeated
passes, logs, and elapsed time are not evidence of progress. Missing evidence calls
for the smallest useful check; a failed expectation calls for revising or stopping
the run before scaling.

```yaml
sim:
  enabled: true
  command: /absolute/path/to/simulacrum-command
  command_timeout: 90
  display: minimal
```

The command receives the supervisor brief on stdin and must return the structured
review format described by that brief. With no command, the configured Kindex LLM
must be enabled and available. Missing credentials, exhausted budget, failed reviews,
pending work, completed quiet reviews, and delivered advice have distinct states.

Review claims survive worker interruption. Recovery runs on the next worker
invocation: saved results can finish without repeating the provider call, while
an interrupted call with an unknown spending outcome records
`interrupted_spend_unknown` and requires fresh direction. Work admitted behind an
active worker has a waiting successor. The health checker reports stalled reviews
while they await recovery.

Advocate escalation is separately opt-in under `sim.advocate`, requires a working
command and verification provider, and is subject to conversation budget and
cooldown. External command allowances are admission reservations, **not measured
provider invoices or hard limits on an opaque subprocess**. Review availability
does not establish that a particular plan has been reviewed.

Install the relevant adapters, then start a new host session:

```sh
kin setup-hooks --mode modern       # qualified Claude function-hook version only
kin setup-codex-hooks
kin setup-opencode-hooks
kin setup-antigravity-hooks
kin setup-cursor-hooks
```

Antigravity must supply a workspace. For an unregistered directory, launch
`agy --add-dir "$PWD"`; an empty or ambiguous workspace is diagnosed rather than
replaced with the hook process's configuration directory.

Cursor observes prompt/response events and delivers through supported
`additional_context` events or one bounded completed-stop `followup_message`.
Cancelled/error stops and subsequent automatic loop turns do not consume advice.
An observational `afterMCPExecution` hook records Kindex use only when the native
event identifies the Kindex server; a generic `MCP:search` name is insufficient.

OpenCode supplies the advisory as a marked, synthetic text part on the current
primary user message. Its auxiliary title requests cannot consume the sole copy.
The generated advisory is excluded from the user's goal and subsequent work
history collected for review.

Project operations select the same existing durable store whether it is at
`.kin/local/kindex.db` or `.kin/local/kindex/kindex.db`. Candidates, reminders, and
other durable work count as populated data. If both stores contain durable work,
Kindex reports a conflict and preserves both. Explicit personal/company profiles
remain separate; this is not a global merge of memory scopes.

An implicit default cannot silently hide a populated home graph when a project
store appears. Kindex reports the ambiguous scope and preserves both stores.
Use `--project-path /path/to/repo` for project work or `--data-dir ~/.kindex`
for the home graph. Explicit configuration and named profiles retain their
selected scope.

## Independently monitor operation

Native subscription reviewers deliberately disable supervision hooks. Kindex registers
their exact native host, session ID, and scratch workspace in the private health
registry, so they do not produce missing-hook or missing-use alerts. Registration
does not manufacture activity or hook receipts, and ordinary sessions remain
monitored. Existing false alerts resolve through normal inbox reconciliation.


Health monitoring is opt-in and stores private metadata under `~/.kindex/health`,
separate from the knowledge graph. It records scoped hook invocations, agent use,
review outcomes, delivery, and explicit feedback. Native transcript/index metadata
lets the checker notice active sessions whose hooks never fired. It retains
identifiers, times, fixed codes, and counts; it does not copy conversation content
or arbitrary tool arguments into the registry or notifications.

Native observation is bounded. If a directory, file, or session limit prevents a
complete scan, the checker reports unavailable coverage with `scan_limit` rather
than treating the unexamined activity as absent.

```sh
python3 -m kindex.supervisor_health install --dry-run
python3 -m kindex.supervisor_health install
python3 -m kindex.supervisor_health status --json
python3 -m kindex.supervisor_health check --json
```

On macOS, installation creates `com.kindex.supervisor-health` as a user launchd
agent, running every 60 seconds independently of the coding agent. Installation
backs up existing settings. Sustained issues enter a durable local inbox. Desktop alerts are enabled by default on macOS when monitoring is installed; root mail remains off. `uninstall` removes the service and disables monitoring while
preserving evidence. `KIN_HEALTH_DIR` provides an explicit isolated diagnostic
registry; it is also an opt-in for automatic recording in that process.

The trusted health `config.json` defaults are:

| Setting | Default |
| --- | --- |
| `enabled` | `false`; installation enables it |
| `desktop_enabled` | `true`; native desktop alerts when monitoring is enabled |
| `desktop_command` | `/usr/bin/osascript`; trusted absolute executable |
| `mail_enabled` | `false`; separate explicit opt-in for root mail |
| `active_seconds` | 1200 |
| `hook_grace_seconds` | 300 |
| `use_grace_seconds` | 1800 |
| `queue_grace_seconds` | 900 |
| `failure_threshold` | 3 |
| `dismissed_threshold` | 3 |
| `consecutive_checks` | 2 |
| `cooldown_seconds` | 21600 |
| `sendmail_path` | `/usr/sbin/sendmail` |

Issue codes distinguish missing hooks, missing observed use, consecutive review
failures, undelivered reviews, repeated dismissals, and unavailable native
observation. Missing-hook/use allegations require native activity evidence. Idle
sessions with a valid hook do not acquire missing-hook alerts merely as time
passes. A native event with no corresponding hook can establish never-fired after
the grace period. Missing-use claims require native activity to advance through
the use grace. Reported activity can support diagnostics about directly recorded
review failures. Stale or superseded reviews get terminal discard receipts; these
settle only the matching review and do not claim delivery or usefulness. A quiet completed review is valid silence.

MCP calls without an explicit host/session identity remain unattributed. Native
tool-call records can supply attribution; arbitrary code-wrapper text is not
interpreted as proof that its embedded calls executed. Missing evidence means
unverified, not proof that an agent deliberately ignored Kindex.

Cursor CLI activity is observed through its native session metadata, including
resumed sessions. Cursor IDE discovery and timestamped native tool-use discovery
are separately reported as unverified; actual scoped hook/MCP receipts can still
record operation. A configured Cursor hook or successful CLI installation alone
does not establish an authenticated native model run.

## Value and notifications

Delivery and a reviewer's self-rating do not demonstrate usefulness. Value remains
unverified until explicit feedback is recorded:

```sh
python3 -m kindex.supervisor_health feedback \
  --project "$PWD" --agent claude --session SESSION_ID --verdict useful
```

Other verdicts are `dismissed` and `acted_on`. These are attributed feedback, not an
independent guarantee of usefulness. Apply them to the actual host session whose
advice was evaluated.

Sustained issues create durable inbox entries even when a check does not request
notifications. Each occurrence has a stable alert ID and stays unread until
acknowledged or resolved. Acknowledgment stops repeat alerts for that occurrence;
it does not mark the underlying health problem fixed. A recurrence after resolution
gets a new alert ID. Records are retained for 30 days.

```sh
python3 -m kindex.supervisor_health inbox --json
python3 -m kindex.supervisor_health ack --id ALERT_ID --json
python3 -m kindex.supervisor_health check --notify --json
```

The existing checker submits native macOS notifications without Postfix, root
access, or another account. A banner contains fixed issue information and an alert
ID; private scope details remain in the owner-only inbox. Native transport errors
are explicit and retryable. Submission acceptance does not prove a banner appeared
or that a person read it; desktop permissions and notification settings still apply.
On an unsupported platform, the native transport reports that state and retains
the inbox entry. No additional background service is installed for notifications.

Set `desktop_enabled: false` to disable desktop submissions. Root mail is separately
optional: only explicit `mail_enabled: true` permits the checker to submit messages
to local **root**. Installation preserves both choices and schedules `--notify`
when either transport is enabled. A command-line `--notify` cannot override a
disabled transport. Root mail requires a working local mail service; a successful
`sendmail` exit means acceptance, not mailbox delivery or human reading.

Inspect `~/.kindex/health/stderr.log` and launchd status if the checker itself stops;
`status` reports a stale last check rather than claiming the monitor is healthy.

## Subscription reviewer backends

`sim.backend` selects `api` (the package default), `antigravity`, `codex`, or
`claude`. Subscription backends use the native account login through a named tmux
scratch session. Install tmux and the selected CLI, and log in using the native
subscription account first. Missing executables, API authentication, provider
quota failures, malformed responses, and interrupted attempts are visible
failures. There is no automatic backend, model, or API fallback.

```yaml
sim:
  enabled: true
  backend: antigravity
  agent_model: gemini-3.8-flash-medium
  agent_effort: medium
  max_conversation_reviews: 100
  max_daily_reviews: 500
  agent_timeout: 90
  budget_warning_fraction: 0.8
```

`sim.model` applies only to the API path; `sim.agent_model` and
`sim.agent_effort` select the native reviewer. Codex's empty model uses its native
default; Claude's empty model selects Haiku. Antigravity's empty model selects
Gemini 3.8 Flash with the configured effort suffix. An explicit Antigravity model
already includes its effort choice. Unsupported native settings fail visibly.

Change a running conversation through the existing trusted overrides:

```sh
kin agent-config set sim.backend codex --client claude --scope instance --instance SESSION_ID --global
kin agent-config set sim.agent_effort high --client claude --scope instance --instance SESSION_ID --global
kin agent-config set sim.max_conversation_reviews 200 --client claude --scope instance --instance SESSION_ID --global
kin agent-config set sim.max_daily_reviews 800 --client claude --global
```

The `--client` identifies the host being supervised; `sim.backend` independently
selects its reviewer. Limits are local attempted-review counts. Conversation
counts persist across days and backend changes, and the UTC daily count covers
all conversations, subscription backends, and Ollama reviews in the project store. An attempt is
reserved atomically before transport; failed and unknown attempts remain counted.
Changes take effect on the next admission without resetting accounting. Existing
sim queue claims remain the only dispatch authority, and already admitted work
retains its configuration snapshot.

Native usage is retained as reported and may be cumulative for a resumed session;
it is not summed or converted to an invoice. Provider quotas are independent and
reported as unknown by Kindex's allowance diagnostics. API dollar limits,
including a configured $5 daily project allowance, remain on the API path only.
Subscription reviews do not debit that ledger or invoke paid Advocate escalation.
Both paths emit a low-allowance notice at the configured warning fraction, even
when an advisory is delivered at the same event. Notices rearm after the allowance
changes or usage falls below the threshold.

Native resume IDs are stored separately for each resolved project data directory,
conversation, and backend, and reused explicitly across tmux process restarts.
Review prompts travel through stdin, including Antigravity's streaming input.
Claude emits streaming initialization so its native ID can be checkpointed before
a turn completes. A native client can emit an ID before saving its conversation;
if interrupted in that window, an exact resume can still fail. Kindex retains the
ID and reports the failure without silently creating a replacement session.
Persisted records are schema-validated, and optional health
registration failures do not discard a valid review or its native checkpoint.
Review scratch files are private; each native process has a closed environment
without ambient API keys and a bounded runtime and output. Native tool controls,
restricted customizations, and a dedicated Antigravity agent reduce the available
review actions. They do not isolate every readable file in the user's OS account.
`KINDEX_REVIEW_WORKER=1` prevents recursive Kindex hooks. Native reviewer session
IDs are registered with the health monitor so intentionally disabled reviewer
hooks are not mistaken for missing hooks in a human work session.

The tmux process exists only during an active review; Kindex does not keep an idle
reviewer running. Its private `subscription-review/<conversation-hash>-<backend>/active.json`
under the project data directory contains the exact tmux socket, session name,
and attach argument list while running. Native conversation state persists after
that process exits, and the next review resumes its stored native ID.

Native session identity is checkpointed when the CLI emits its initial session
ID, before the review finishes. A timeout or interrupted first turn retains that
known ID for the next freshly admitted review. The interrupted sim job is never
replayed. Checkpoints bind the exact scratch workspace, backend, conversation,
and any previously known ID; a conflicting checkpoint fails closed.

Subscription reviewer grounding uses only local full-text search, graph expansion,
and local ranking signals. It skips both query embeddings and register translation,
so parent-process API credentials cannot cause paid grounding requests. API reviews
retain their configured hybrid retrieval behavior.

## Offline Ollama reviews

Use `sim.backend: ollama` to run reviews with installed local model weights. This
is separate from the installed Antigravity, Codex, and Claude clients, which use
their providers' services. Install Ollama and explicitly download a suitable model
before enabling this backend; Kindex does not download models automatically.

```yaml
sim:
  enabled: true
  backend: ollama
  ollama_url: http://127.0.0.1:11434
  ollama_model: qwen3:0.6b
  max_conversation_reviews: 100
  max_daily_reviews: 500
  agent_timeout: 90
  max_output_tokens: 500
```

The small model above is useful for checking installation and transport. Review
quality depends on the chosen model and available hardware; a successful response
does not establish that its advice is useful. Select a model you have evaluated
for your work.

Only HTTP loopback endpoints are accepted. Before sending a prompt, Kindex checks
the installed-model inventory for local weights and rejects cloud-backed, missing,
or unrecognized entries. It bypasses proxy settings, does not follow redirects,
and has no cloud or API fallback. Grounding uses local full-text search and graph
data. The local Ollama service is trusted: these checks do not sandbox the daemon
or establish network isolation for the whole computer.

Ollama shares the native clients' durable attempted-review limits and low-allowance
notices. Failed attempts count. It does not read or debit the API dollar ledger or
invoke paid Advocate escalation. `agent_timeout` bounds the complete local request,
including the inventory check, and `max_output_tokens` bounds generated output.
Reported token counts are retained without assigning a dollar cost.

Change a conversation while it is running using trusted user overrides:

```sh
kin agent-config set sim.ollama_model qwen3:0.6b --client codex --scope instance --instance SESSION_ID --global
kin agent-config set sim.backend ollama --client codex --scope instance --instance SESSION_ID --global
```

The next admitted review uses the new settings; existing claims retain their
snapshot and counts are preserved. Set `sim.backend` to `api`, `antigravity`,
`codex`, or `claude` to switch back. Set `sim.enabled` to `false` at the same scope
to disable reviews. Adding Ollama support does not change an existing backend
selection.
