# Shared supervision and Kinbase interoperability

Periodic review adapters share one supervisor for Claude, Codex, OpenCode,
Antigravity, and Cursor. Review state distinguishes disabled, unavailable,
budget-suppressed, queued, failed, quiet-completed and delivered outcomes.
Project hooks and MCP use one durable project-store resolver; conflicting stores
are diagnosed and preserved. Explicit personal and company scopes stay separate.
An implicit default reports ambiguity when a new project store would hide a
populated home graph. Explicit project, profile, and data-directory choices keep
their intended scope. Modern Claude review windows are isolated per session.

Reviews compare the goal with recent actions and validation evidence, including
small representative pilots and durable progress across restart or rebuild.
Independent health records distinguish invocation, agent use, review, delivery,
and explicit usefulness feedback. Cursor authenticated delivery is unverified.

Kinbase imports verify signed source bytes without rewriting source events.
Raw evidence and reduced snapshots remain distinct; standing is capped by
provenance, and unknown-owner questions remain visible beside contested facts.
Graph transfers must preserve standing and Kinbase provenance metadata while
scrubbing private paths and nested contact data from shared exports.

Local source corpora, private validation artifacts and team-only publication
records are excluded from this public repository's release inputs.

Sustained health issues enter a private durable inbox independently of external
notification success. Native macOS alerts reuse the existing user checker;
root mail remains opt-in. Per-occurrence acknowledgment and per-transport retry
and cooldown state survive restarts. Submission acceptance is not proof of a
visible banner, human reading, or useful advice.

The release reconciles upstream reminder concurrency, legacy-hook migration and
CI work, doctor FTS integrity repairs, and transactional graph transfer. Background
supervision imports the already-running trusted Kindex package under Python
isolated mode so a reviewed workspace cannot shadow worker modules.
Durable claims recover on the next worker invocation; uncertain paid attempts
become explicit failures without replay. A waiting worker handles new admissions
behind an active review. Native scan limits are reported as incomplete coverage,
and persisted alert payloads are validated before display.
