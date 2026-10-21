# Opt-in practical memory

`kindex.practical_memory` adds `prepare`, `persist`, `index`, `audit`, and `ack`.
Each takes `(store, request)` and returns strict JSON data. The corresponding CLI
is `kin practical-memory COMMAND --request-json /absolute/request.json --json`,
with the existing explicit config, profile, project and data-directory selectors.
The MCP `practical_memory(action, request_json)` tool uses the same strict JSON
request/result contracts through a shared host adapter. It resolves config without
initializing a writable singleton; read calls do not record tool-health writes.
No existing command, default retrieval format or task workflow changes. The v4
patch is not required. These explicit operations remain experimental; installing
them does not activate a workflow or establish organizer effectiveness.

The exact request/response specification is frozen in
[practical-memory-contract-v1.md](practical-memory-contract-v1.md). Every request includes
exact graph/profile/project/client/session scope and canonical UTC evaluation time.
The selected config project and receiving session must match exactly; a session
owned by a subdirectory does not silently become owned by its repository root.
This CLI preserves native configuration discovery but binds receiving ownership
to the exact explicit selector, in order `--project-path`, `KIN_PROJECT_PATH`,
then `KIN_PROJECT`. Without one, the selected native config project applies.
An explicit file path is refused rather than silently selecting its parent.
Client and actor labels are trusted local host annotations, not authentication.
Source nodes must explicitly name that same canonical project and pass native
verification, status, time, contradiction, expiry and client admission.

`prepare` and `audit` require a native `Store` opened with `read_only=True`,
`migrate=False`, `manage_project_storage=False`, with config `_stamp_on_open=False`.
The CLI uses this lane throughout read opening, rendering and closing. It refuses
missing identities, profiles and current schemas rather than creating, stamping,
migrating or falling back. Library calls preserve a caller-owned read transaction;
they open and close only their own read snapshot.

A prepare request supplies six independent booleans: `compact_packing`,
`continuity`, `assertion_index`, `episode_index`, `typed_links`, and
`application_trace`. It supplies every component budget (`sources`, `continuity`,
`links`, `application_trace`) and one total byte cap, all at most 16,384. Only
`model_text` is model input: it is one canonical JSON payload whose entire UTF-8
encoding fits the total cap. All wrappers, source tables and literal text count.
The separate diagnostics are not additional model context. Whole groups pack or
omit; no exception or condition span is trimmed to preserve a claim. Nonfitting
items are skipped so later complete items can backfill. Sources with current
positively matching enabled index groups use those groups instead of their full
body. Stale or invalid derived indexes fall back to ordinary current source text.

The all-flags-false payload is a shared-wrapper control, not the native renderer.
A reader comparison must measure that common formatting effect separately from
the six switches. Assertion and episode indexes provide explicitly annotated
source-span representations, not automatic semantic/entity/episode resolution.
The service cannot discover undeclared semantic exceptions or certify role labels.

`persist` accepts the full unchanged prepared object and explicit entry IDs from
its actual packed source/group entries. Hidden candidates, links and checklist
entries are not retainable. A host-supplied prepared response is an integration
binding within the trusted host convention, not proof of authenticated execution.
Current source body, metadata, scope, admission and every literal interval are
rechecked inside the owned transaction. Full-body read entries may be large;
persisted/indexed groups have the tighter span/group limits in the contract.
Continuity requires explicit retained IDs and always rereads current sources.

`index` CAS-updates one assertion or episode representation in the source's
`extra.practical_index_v1`. It preserves each successful submitted version and
receipt in bounded logical history, with one current pointer per kind. Indexing
never creates truth nodes, verifies a source, or changes its body. This in-row
history and labels are locally trusted metadata, not tamperproof evidence.

Mutations open unopened stores with SQLite `mode=rw`, never create missing
databases, and bypass native initialization/repair. Strict Pydantic register
models and canonical nested validators preserve the versioned JSON wire format
without coercion. Mutations reject a preexisting caller transaction. Fresh raw extra JSON and both
new namespaces are validated inside native `BEGIN IMMEDIATE`; malformed state is
never silently repaired. They use native atomic extra updates, reject capacity
rather than evict, and keep effect plus receipt in one commit. The same operation
key and exact request return the historical receipt with no timestamp or other
mutation. Conflicting requests reject. Replay validates current owner scope, but
does not re-authorize old source facts; current preparation always readmits them.

`ack` records only the host's assertion about the exact hash of a previously
persisted payload. Persistence and acknowledgment receipts remain separate and
immutable. Prepared is not persisted; persisted is not delivered; host-reported
delivery is not proof of model receipt, comprehension or use. Late acknowledgments
attach only to the original payload. No resend, task execution or network delivery
occurs.

Typed links preserve stored direction and provenance only among admitted recall
and explicitly selected continuity sources. Association does not imply prerequisite
satisfaction. Dependency and supersession annotations grant no permission or
completion; native task/source lifecycle services retain their roles.
Application trace and `audit` pack applicability questions backed by whole current
constraint/directive/checkpoint sources. They do not produce a semantic verdict,
rewrite an answer, or invoke a second reader.

Native privacy filtering remains enabled. If it would rewrite an exact payload or
stored extra, the operation fails with `privacy_unavailable` instead of returning
incorrect literal hashes/offsets or silently redacting unrelated fields. Harmless
strings that match native credential patterns can also be refused.

Failures are `PracticalMemoryError` with safe `.code` and `.message` strings.
The CLI emits a safe JSON error and exit 2 for request/refusal errors, or exit 1
for unavailable storage. Read omissions are bounded reason/count aggregates;
foreign source identities and hidden candidates do not appear in diagnostics.

## Receiving host identity

CLI and MCP adapters require explicit `KIN_SESSION_ID` naming the native Kindex
session node. Provider conversation UUIDs in `CODEX_THREAD_ID` and
`CLAUDE_SESSION_ID` are a separate identity domain: they identify the client but
never replace the native owner ID and need not equal it. Simultaneous provider
families are refused. With neither provider marker, `KIN_CLIENT` must name a
supported client; when both an explicit client and provider marker exist they
must agree. Missing, unknown or conflicting inputs fail closed. Request scope
must match the native session and host client; JSON cannot choose another session,
client or receiving project. Library APIs still require a trusted host to build
validated scope/context objects. These local labels are not authentication
against a malicious process on the same account.

The two MCP tools preserve raw argument types and forbid unknown arguments before
calling the shared host adapter. A string such as `"true"` is not accepted as an
`incoming_later` boolean. Existing MCP tools retain their previous validation.
