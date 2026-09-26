# Desktop health popup identity

The desktop banner must identify the agent session without requiring a click or
an inbox command. It shows the agent, a readable session name or project name,
a short native session ID, a plain-language issue, and a compact project path.
Alert occurrence UUIDs remain in the durable inbox rather than consuming banner space.

`src/kindex/supervisor_display.py` owns presentation only. For Claude, it reads
explicit custom-title records matching the exact session from the final 32 KiB
of the exact native transcript. Small transcripts are read in full. Missing,
malformed, mismatched, or symlinked metadata falls back to project and session ID.
An old head title cannot be used when a later rename may exist in unread content.
Other hosts use project and session ID. Prompts and summaries are never used as names.

Transport submission still passes dynamic text as literal argv to fixed AppleScript.
Monitoring thresholds, inbox state, acknowledgements, and retries are unchanged.
OS acceptance does not establish that a person saw the banner; long or wide titles
may still be truncated by the desktop UI.

`tests/test_desktop_session_identity.py` checks the rendering and metadata boundaries.
Existing notification acceptance tests cover durable inbox and transport behavior.
