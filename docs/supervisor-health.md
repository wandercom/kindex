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

## Independently monitor operation

Health monitoring is opt-in and stores private metadata under `~/.kindex/health`,
separate from the knowledge graph. It records scoped hook invocations, agent use,
review outcomes, delivery, and explicit feedback. Native transcript/index metadata
lets the checker notice active sessions whose hooks never fired. It retains
identifiers, times, fixed codes, and counts; it does not copy conversation content
or arbitrary tool arguments into the registry or mail.

```sh
python3 -m kindex.supervisor_health install --dry-run
python3 -m kindex.supervisor_health install
python3 -m kindex.supervisor_health status --json
python3 -m kindex.supervisor_health check --json
```

On macOS, installation creates `com.kindex.supervisor-health` as a user launchd
agent, running every 60 seconds independently of the coding agent. Installation
backs up existing settings. Monitoring works with mail disabled. `uninstall` removes the service and disables monitoring while
preserving evidence. `KIN_HEALTH_DIR` provides an explicit isolated diagnostic
registry; it is also an opt-in for automatic recording in that process.

The trusted health `config.json` defaults are:

| Setting | Default |
| --- | --- |
| `enabled` | `false`; installation enables it |
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

## Value and local root mail

Delivery and a reviewer's self-rating do not demonstrate usefulness. Value remains
unverified until explicit feedback is recorded:

```sh
python3 -m kindex.supervisor_health feedback \
  --project "$PWD" --agent claude --session SESSION_ID --verdict useful
```

Other verdicts are `dismissed` and `acted_on`. These are attributed feedback, not an
independent guarantee of usefulness. Apply them to the actual host session whose
advice was evaluated.

Root mail is optional. Only explicit `mail_enabled: true` in the trusted health
configuration allows `check --notify` to send sustained issues to local system
mail for **root only**. Installation and reinstallation preserve that choice and
omit `--notify` when mail is off. A command-line `--notify` cannot override the
mail-disabled setting. The local user has declined Postfix: monitoring remains
on, root mail is off, and no replacement desktop alert transport is claimed. Recipient arguments are fixed and no shell is used.
Mail includes scope identifiers, counts, times, issue codes, and a diagnostic
command. Cooldowns persist across restarts; transport errors remain visible and
retryable.

A successful `sendmail` exit means acceptance, not mailbox delivery or human
reading. Local mail service must be running. On macOS, `mailq` can reveal a stopped
Postfix service; starting it may require `sudo /usr/sbin/postfix start` in an
administrator's terminal. Inspect `~/.kindex/health/stderr.log` and launchd status
if the checker itself stops; `status` reports a stale last check rather than
claiming the monitor is healthy.
