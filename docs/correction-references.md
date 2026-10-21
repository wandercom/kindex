# Correction references and advisory dreaming

This isolated opt-in extension preserves ordinary Kindex search, trust, session,
source and practical-memory behavior. Exact references reduce delivered text;
they do not establish that a source is true. Declared corrections take precedence
for navigation only after current access, version, actor, time and scoped conflict
checks. Generated connections remain navigation hypotheses.

This documents the additive v8 interface, contract SHA256
`ca81d4fed5dda967358ba495a4cedab51e687f272eb92d28f8677e6e6024370c`,
with protocol-commitment addendum SHA256
`53f20125909a06964d767c61a2074a9cdf005a058de42d045636c3329cf18797`.
Installing this patch does not enable an automatic workflow or deploy anything.
All delivered envelopes include this warning:

> References and generated connections are advisory navigation, not truth,
> permission, completion, or verified applicability.

## Explicit host context

`kindex.correction_references` supplies frozen keyword-only `ReadContext` and
`WriteContext` objects. Each constructor validates and copies input containers;
nested containers are immutable. `.to_dict()` returns a fresh JSON copy. Contexts
are a trusted host convention, not authentication against a same-process caller.
The host selects the existing graph, profile, exact session/project, client,
authorized partitions and current correction policy. A partition label alone
grants no access. The current policy's digest binds its complete declared fields.

Personal/repository sources require their exact authorized project. Materialized
Kinbase evidence requires its exact authorized origin and native importer fields.
The library's separately labeled `synthetic_fixture` route is for isolated
qualification, and provides no cryptographic or governance verification. Initial
CLI commands expose only one current repository partition; broader contexts and
Kinbase selection are library-only. No remote refresh or automatic scope discovery
is supplied.

All read operations require an explicitly selected
`Store(read_only=True, migrate=False, manage_project_storage=False)` with
`PRAGMA query_only=1`. They use one SQLite snapshot, retain caller-owned
transactions and never initialize an absent registry. Writes require an explicitly
selected writable Store. Before opening an unopened Store for writing, the module
qualifies an existing current-schema graph/profile/owner through a read-only
connection. It then opens with SQLite URI `mode=rw`, which refuses an absent
database atomically, without invoking native writable initialization,
migration, trigger repair, profile stamping or directory management. Schema, owner
and actor are rechecked inside the transaction before registry DDL or writes.
Existing caller connections use savepoints; caller transactions are never committed
or rolled back by the module.

An absent database is never created, including deletion between qualification and
write-open. Replacement by a different existing file is still subject to the
transactional graph/profile/schema/owner checks. This does not authenticate the
filesystem against a malicious local process or promise persistence after a
concurrent unlink of an already-open database. There is no fallback connection,
directory creation or automatic schema initialization on failure.

CLI and MCP use the shared `practical_memory_host` adapter. The MCP
`correction_references(operation, request_json, metadata_encoding="full",
incoming_later=False)` tool accepts the same strict request and result formats.
Operations are `reference`, `expand`, `correction_add`, `correction_retire`, and
`dream_import`; writes require an explicit operation. Current-session config and
host environment choose the scope, never grant fields supplied in the JSON.
The MCP path resolves configuration without opening the writable singleton or
recording tool-health writes. Contexts and host boundaries use strict Pydantic
models; version-specific validators preserve the established canonical wire
format and reject invalid nested fields without coercion.

## Library operations

| Operation | Purpose |
| --- | --- |
| `capture_reference(store, node_id, context=..., evaluation_time=...)` | Bind exact body bytes and relevant metadata, with current scoped applicability. |
| `record_correction(store, assertion, context=...)` | Store one immutable explicit source-backed correction declaration. |
| `retire_correction(store, assertion_id, context=..., reason=..., recorded_at=...)` | Append an authorized retirement after rechecking both current bindings, including on replay. |
| `resolve_references(store, request, context=...)` | Return budgeted body-free reference stubs and scoped correction descriptors. |
| `expand_references(store, request, context=...)` | Deliver complete required current evidence groups, with separate optional navigation space. |
| `compile_dream_index(snapshot, proposals)` | Purely validate and bind a bounded source snapshot and proposal batch. |
| `record_dream_index(store, compiled, context=...)` | Atomically store a structurally valid batch after current source access/version checks. |

Context construction is keyword-only. Its required fields are `graph_id`,
`profile` (empty string is valid), `client`, `session_id`, `current_project`,
`correction_policy` and `partitions`. A repository partition is exactly
`{partition_id, kind: "repo", project_path, kinbase: null}`. Paths are absolute and
canonical. The policy is `{policy_id, policy_sha256, allowed_actor_ids}`; its digest
is ordinary SHA256 of canonical JSON of exactly
`{policy_id, graph_id, profile, session_id, current_project, partitions,
allowed_actor_ids}`. The values must match the context. `WriteContext` additionally
requires `actor_id`, aware RFC3339 `recorded_at` and `explicit_action=True`.

Capture returns `reference`, `metadata`, `applicability`, `evaluation_time` and
`context_sha256`, under schema `reference-capture/1`. A reference contains exactly
`node_id`, `graph_id`, `profile`, `partition_id`, `body_sha256` and `metadata_sha256`.
Use the returned object unchanged in subsequent requests. A typical library read
uses the already authorized host context and explicitly selected read-only Store:

```python
from kindex.correction_references import capture_reference, expand_references

captured = capture_reference(
    store, source_id, context=context, evaluation_time=evaluation_time,
)
result = expand_references(store, {
    "schema": "expand-request/1",
    "evaluation_time": evaluation_time,
    "query": "",
    "entry_refs": [captured["reference"]],
    "mode": "none",
    "optional_limit": 0,
    "base_budget_bytes": 8192,
    "optional_budget_bytes": 0,
    "include_history": False,
}, context=context)
```

The corresponding resolve request uses schema `reference-request/1` and replaces
the three expansion fields (`base_budget_bytes`, `optional_budget_bytes`,
`include_history`) with `reference_budget_bytes`. It returns compact `model_text`,
UTF-8 bytes/digest, entries, correction descriptors, priority refs, optional route
descriptors, omissions and inspected counts. Expansion returns `base_text` and
`optional_text`, each with its bytes/digest, plus included refs, omissions/counts
and separately counted `assertion_receipts`. Evaluation time is a required aware
RFC3339 timestamp; validity intervals are half-open and retain fractional precision.

Correction input has schema `correction-assertion/1` and exactly `from_ref`,
`to_ref`, `subject_key`, `claim_key`, `effect`, `effective_from`, `effective_until`,
`condition`, `from_spans`, `to_spans`. The correcting source is `from_ref`; effects
are `replace`, `qualify` or `retract`, and condition is `unconditional` or
`unresolved`. Interval endpoints are nullable. Each support array contains one to
four exact UTF-8 spans `{start_byte, end_byte, literal}`, with half-open offsets and
at most 2048 bytes per span. CLI retirement input is exactly `{assertion_id, reason}`;
its time and actor come from the host. Registry write receipts distinguish
`committed` from `pending_caller`, and persistence never means delivered or acted on.

Pure compilation accepts a `dream-snapshot/1` object with exactly `graph_id`,
`profile`, `context`, `cutoff`, `sources`, and a `dream-proposals/1` object with
exactly `snapshot_sha256`, `generator_protocol_sha256`, `proposals`. Snapshot sources
are sorted by native ID and contain `{reference, metadata, body, episode_id}`.
Each proposal has `proposal_id`, `relation`, `motif`, `supports`, `differences` and
`transfer_checks`; it cites exactly two distinct cross-episode sources using
`{reference, spans}`. Relations are `analogous_mechanism`,
`contrasting_conditions` or `shared_constraint_pattern`. Compilation returns the
original snapshot/proposals plus a digest-bound `batch_id`, under schema
`dream-index/1`; CLI import accepts that compiled object unchanged.

Bounds are explicit: up to eight partitions/entry refs/optional candidates;
32 visited nodes, 64 corrections, 64 contradiction pairs, correction depth eight;
query at most 2048 bytes; reference budget 512–16384 bytes; expansion base budget
1–65536 bytes and optional budget 0–32768 bytes, together at most 65536. A dream
snapshot has 2–54 complete sources of at most 1800 body bytes, and at most 131072
canonical bytes total. A batch has at most 24 proposals, three per source pair.
Motif and each difference/check are nonempty and at most 512 bytes; difference and
check arrays each have one to four entries. Query recall scans at most 32 batches
and 256 proposals, reporting an explicit limit rather than claiming completeness.

Inputs and result shapes are strict objects. Unknown/missing keys,
nonfinite values, malformed UTF-8 and invalid intervals fail; source text is never
trimmed or repaired. `ReferenceError.code` is public, with a fixed safe exception
message. SQLite operational errors propagate without retry. Native privacy
redaction remains enabled: if it would alter any exact result, the operation fails
with `privacy_unavailable`. Harmless strings matching a native secret pattern can
therefore be refused too.

References bind native ID, graph, profile, partition, full body SHA256 and relevant
metadata SHA256. Canonical JSON is sorted, compact UTF-8 with no newline, no Unicode
normalization and no nonfinite values. Hash framing and the exact metadata subset
are defined in the contract. Source or provenance changes invalidate the reference;
there is no lazy rebinding by ID/title. An exact-entry request performs no native
search. Query-only requests perform one bounded native nonvector candidate search,
followed by explicit scoped admission; that candidate policy changes no ordinary
search defaults and cannot promise complete recall.

## Correction and conflict behavior

One canonical stored assertion yields both outgoing `corrects(new)` and incoming
`corrected_by(old)` views, using the same ID. No inverse edge or mutable native
source is written. Correction declarations retain exact support spans, effect,
claim, actor and interval. The stored writer context digest is historical
provenance; current reads recheck the current policy and actor rather than treating
that digest as a perpetual grant or requiring an unavailable historical context.

Resolve preserves original source applicability separately from per-claim paths.
Expansion prioritizes current terminal corrections and keeps obsolete/intermediate
sources as references. Qualification adds the corrected target basis. An unknown
condition adds unresolved evidence over any simultaneously effective corrected
basis. Recursive selection keeps exact partition/subject/claim lanes: a correction
to a different claim cannot replace the whole body required for another claim.
Their required bases are unioned. Forks and cycles never choose a semantic winner. Future corrections remain
descriptors without activating the future corrector's contradictions. Expiry,
retirement or inapplicability never silently resurrects an obsolete instruction.

Native contradictions are inspected in both directions only after scope/client
and nonrecursive current-tier checks. Their accessible-current components remain
atomic quoted evidence; they are not independently established incompatibilities.
Correction traversal also closes every newly admitted contradiction endpoint.
Hidden/noncurrent neighbors cannot exhaust visible conflict ceilings. Declared
contradictions are not a basis for changing ordinary native trust defaults.

Accessible malformed correction JSON/ref fields yield only generic unresolved
state, with no old payload. Rows with inaccessible current endpoints are ignored.
Historical payload access requires exact current version bindings as well as
scope. When stored refs differ in graph/profile/partition routing, the row is
ignored before visible counts or status. For the same authorized endpoint IDs and
routing labels but changed hashes, only generic unresolved state may remain. No
historical refs, IDs, claim keys, support literals or full receipts are rendered
from that row. Stored hashes lack historical scope preimages; this version cannot
authorize or reconstruct that historical payload, especially when a host reuses a
partition label for another project. It makes no complete historical-access or
constant-compute/timing-noninterference claim.

Required bodies are complete, atomic groups under the base envelope budget.
Overflow preserves bounded references/status, never an obsolete-only fallback.
Optional history is considered after required groups. Optional direct/dream
routes have a separate budget and never steal base space. Byte receipts count
actual canonical envelope UTF-8; full assertion receipts are outside delivered
text and separately counted. Cheap reference delivery still requires reading and
hashing original sources.

## Optional dreaming

Mode `none` delivers no optional stream or later native-original backfill.
`direct` follows only actual outgoing native navigation rows. `dream` alternates
that stream with frozen seed-linked proposals, ranked deterministically by query
token overlap and proposal ID. Both supports, exact versions, current access and
required correction/conflict evidence must remain available. A motif alone is
never delivered as a substitute for required evidence. Motif-only recall without
an admitted base endpoint is deliberately unsupported.

Expanded generated connections have status `generated_hypothesis`. Compact
reference optional descriptors use `navigation_hypothesis`; native direct links
use `declared_link`. These are distinct presentation states, never semantic
acceptance, correction, permission or operational authority.

Recall verifies the frozen batch digest and bounded snapshot/proposal structure,
then checks selected support bindings against the current read snapshot. It does
not call the compiler or rebuild the index at query time. The pure compiler
validates structure, exact support bytes and digests, not the semantic quality or
applicability of an analogy. It validates the supplied
64-hex protocol commitment and preserves it in the batch digest; the host must
separately verify protocol bytes, prompt/schema receipts and pre-query chronology.
There is no product model call, provider client, scheduler, semantic acceptance
mutation or automatic correction from a generated connection.

## CLI

Every leaf requires `--request-json /absolute/path.json` and accepts native
`--config`, `--data-dir`, `--profile`, `--project-path` and `--json` flags:

```text
kin practical-memory reference --request-json /absolute/reference.json
kin practical-memory expand --request-json /absolute/expand.json
kin practical-memory correction add --request-json /absolute/assertion.json
kin practical-memory correction retire --request-json /absolute/retirement.json
kin practical-memory dream-index import --request-json /absolute/compiled.json
```

The CLI derives scope, policy and actor from selected native host context, never
request/source fields. Project precedence is explicit `--project-path`,
`KIN_PROJECT_PATH`, `KIN_PROJECT`, then selected native configured project; exact
existing session ownership is required. `KIN_SESSION_ID` names that native
owner, while provider thread markers identify only the client. A nonempty
selected `config.user` is required. Retirement time
is the single host operation instant. CLI expansion defaults `include_history` to
false; library callers provide that boolean explicitly. Commands never create a
missing owner, select a latest session or perform a writable fallback on read
failure. Successes and handled operation errors are one sorted, compact UTF-8 JSON
object; `--json` does not change the data. Native parser usage errors retain native
CLI behavior. MCP exposes the same operations through `correction_references`.

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
