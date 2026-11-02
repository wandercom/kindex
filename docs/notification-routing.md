# Notification output and agent reentry

A notification is a notice that an item is due. A wake runs an agent. Configuring
one never enables or disables the other, and a notification never claims that an
action executed.

## Output destinations

New configurations discard notifications by default:

```yaml
reminders:
  default_channels: [output]
  output_destination: /dev/null
  output_timeout: 10
```

`output_destination` accepts:

- `/dev/null` (portable discard, including on Windows).
- An absolute file or character-device path. Files are appended as UTF-8 text,
  created with mode 0600 subject to umask; existing permissions are preserved.
  Parent directories must already exist. There is no rotation or retention policy.
- `fd:N`, an inherited descriptor in the checker process. The descriptor is
  duplicated, never closed or reconfigured. Regular files and nonblocking character devices/pipes are supported;
  blocking device/pipe descriptors are refused to avoid hanging the checker. A descriptor from a different process is not addressable this way.
- An `http://` or `https://` URL. Kindex sends one JSON POST with `id`, `text`,
  `priority`, and `due`. Only 2xx responses count as accepted. Redirects are refused.
  Credentials in URL user-info and URL fragments are rejected. Use HTTPS for
  remote destinations. The entire POST, including DNS/TLS and response headers,
  runs in a bounded child process. Custom authentication headers are not currently supported.
  Per-reminder URLs that storage redaction would alter (such as query tokens) are
  rejected; locally configured `output_destination` can retain an exact URL.

A per-reminder destination overrides channel selection, including legacy channel
choices. CLI and MCP expose the same setting:

```bash
kin remind create "Review build failures" --at "in 20 minutes" \
  --destination /absolute/path/notifications.log
kin remind create "Check release" --at "in 20 minutes" \
  --destination https://your-service.example/notifications \
  --wake codex --session YOUR_SESSION_ID --cwd "$PWD"
```

MCP: `remind_create(text="Check release", when="in 20 minutes",
 destination="/absolute/path/notifications.log", wake="codex",
 wake_session="YOUR_SESSION_ID", wake_cwd="/absolute/project")`.

An output failure is stored in the reminder's `extra.notification_results` and
never falls back to a bell, system notification or another sink. It does not
prevent a separately requested wake. Notification retries follow the existing
reminder snooze lifecycle; HTTP acceptance does not prove the receiver processed
it, and an interrupted write/POST can have an uncertain outcome. Receivers should
deduplicate using `id` and `due` when repeated delivery matters.

Existing explicit channel settings are preserved. `system`, `slack`, `email`,
`telegram`, `claude` and `terminal` remain available. Their existing terminal
fallback applies only when the selected channel list excludes `output`.
System-channel success means the helper accepted the notification, not that it
was displayed. Nonzero `osascript` exits and timeouts are recorded as failures.
The `claude` notification channel is a hook-injection marker, not a wake or a
confirmed delivery receipt. To migrate an existing installation to the new sink,
set both `default_channels: [output]` and `output_destination`.

## Sessions and reentry

Supported `--wake` values and their headless operations:

| Client | Headless operation | Explicit session |
| --- | --- | --- |
| Codex | `codex exec` | `resume ID`; same-ID `codex queue --thread ID` after a recognized active-writer conflict |
| Claude | `claude -p` | `--resume ID` |
| Antigravity | `agy -p` | `--conversation ID` |
| Cursor | `cursor-agent -p` | `--resume ID` |
| OpenCode | `opencode run` | `--session ID` |
| Muse | `muse exec` | `--session-id ID` |

The installed Antigravity, Cursor and Muse CLI must advertise the session flag
in its help before Kindex submits a prompt. Missing or unsupported CLIs fail
explicitly. No permission, workspace-trust or sandbox bypass flags are added.
Existing Claude budget limits continue to apply.

`--session ID` preserves that exact ID; it never falls back to a new session.
Omit it only when a new headless turn is intended. The working directory is
resolved and stored when the reminder is created. `--session last` is resolved
at creation from the current Codex/OpenCode session (or the latest Codex rollout);
other clients require an explicit ID. Legacy rows still containing an unresolved
`last` are refused rather than resuming whichever session happens to be newest.
`--wake-agent` remains OpenCode-only.

Resume is not an active-session messaging API. Kindex does not implement Claude
inbox sockets, Cursor Cloud steer, OpenCode server messages, Antigravity stream
ownership, or Muse interactive peer messaging. Use resume for a session that is
available for a headless turn. A busy/unsupported target is not replaced with a
fresh session. The only active-writer fallback is Codex's documented queue path,
aligned with PR #99, keeping the same session, cwd and model.

A completed CLI turn is recorded as `completed`; that does not certify that the
agent accomplished its task. Codex queue acceptance is `queued`, not completed.
A timed-out agent/queue, unsuccessful queue without a receipt, or abandoned wake
worker is `submission_unknown`. Neither state is automatically resent, even by
manual `remind exec`, for the same occurrence. A recurring reminder advances to
its next scheduled occurrence and retains `extra.last_action_delivery` with the
previous occurrence, delivery state, result and attempt time. Later occurrences
are distinct requests; they do not replay the earlier prompt. This may overlap
unconfirmed prior work, so inspect the target and cancel the recurring reminder
if later work must wait for confirmation. One-shot reminders remain held until
you acknowledge with `kin remind done --reminder-id ID`, or cancel and create a
new explicit wake after inspection proves a resend is appropriate. There is no
receipt-polling worker.

CLI help and official references supporting these transports:

- [Claude CLI](https://code.claude.com/docs/en/cli-reference)
- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)
- [Cursor CLI](https://prod.cursor.com/docs/cli/using)
- [OpenCode CLI](https://opencode.ai/docs/cli/)
- [Muse automation](https://dev.meta.ai/docs/muse-code/extending)
