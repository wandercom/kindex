# Practical memory: implementation contract v1

**Frozen by the root Validator under the owner’s implementation authorization. This is not a claim of human review or Sim approval.**
This supersedes proposal v1 while preserving it. One opt-in nativebase service,
one CLI namespace, no migration, no new store, no dependency on the full v4 patch,
no default behavior changes, no automatic effects or model calls.

## Five APIs

All functions take `(store, request: dict)` and return JSON dictionaries. CLI is
`kin practical-memory <prepare|persist|index|audit|ack> --request-json ABS_FILE --json`.
Input does not select a database or executable. Strict JSON/primitive types,
unknown-field rejection, safe typed errors; no coercion of strings/bools to ints.

| Function/command | Request fields, all required | Effect |
| --- | --- | --- |
| `prepare` | `scope,evaluation_time,query,features,top_k,retained_ids,budgets,total_max_bytes` | Read-only, bounded combined context. |
| `persist` | `scope,evaluation_time,operation_id,prepared,entry_ids` | Persist selected complete groups actually represented in a prepared response, plus its exact payload receipt. |
| `index` | `scope,evaluation_time,operation_id,node_id,index_kind,expected_source_sha256,expected_metadata_sha256,expected_index_sha256,groups` | CAS one source-owned derived span-index manifest. |
| `audit` | `scope,evaluation_time,answer,constraint_ids,total_max_bytes` | Read-only source-backed checklist packet; no semantic verdict. |
| `ack` | `scope,evaluation_time,operation_id,projection_sha256,host_receipt,payload_sha256` | Record the host's assertion that it received a persisted payload. No other effect. |

## Scope, time and commitments

Exact scope keys:

```json
{"graph_id":"canonical-UUID","profile":"","project_path":"/canonical/project","client":"codex","session_id":"opaque-native-node-ID"}
```

Graph/profile must equal selected native Store identity/preexisting profile stamp;
empty profile means unprofiled. Project is absolute/canonical, equal selected
config project when set. Session is an existing session node with that exact
project. No cwd widening or nearest-owner fallback. Client is one of
`claude,codex,gemini,opencode,cursor,antigravity`. Source nodes require exact
canonical `extra.project_path`; this new lane omits legacy unscoped sources.
Node IDs are opaque strings, not universally UUIDs.

Validate original decoded `domains` as a string list before native `tags`
fallback; reject malformed scope markers. Use native client-marker semantics and
recheck current source/owner applicability for the receiving client. Unscoped
client tags remain usable by any recognized client. Actor/client/episode labels
are trusted host conventions, never authentication or action permission.

Evaluation time is required canonical UTC RFC3339 (`Z`, reproduced by native
normalization). One instant per operation. Reuse native explicit verification,
status, contradiction and stale-referent admission. Validity is
`[valid_at,invalid_at)`, inclusive start/exclusive end, null unbounded. Native
`extra.expires` is a valid ISO date; today remains live, strictly past expires.
Malformed relevant scope/time metadata denies the source without repairing it.

`C(x)` means UTF-8 sorted-key JSON, compact separators, no ASCII escaping or
nonfinite values. `D(kind,x) = SHA256(UTF8("kindex.practical_memory.v1/" + kind +
"\n") || C(x))`. Kinds:
`metadata,source,index,group,projection,request,receipt,answer,constraints`.
Body/span/text hashes use literal UTF-8 bytes directly.

`source_sha256 = SHA256(content UTF-8)`. Relevant metadata is the fixed-key object
below, missing scalar values null; preserve stored scalar/list types/order:

```text
type,title,prov_who,prov_when,prov_activity,prov_source,status,audience,standing,
verified_at,verified_by,prov_method,valid_at,invalid_at,
referent,asserted_at,true_of,domains,
extra:{project_path,expires,superseded_by,referent_stale,expired_at,kinbase}
```

Extra includes only listed keys present. `metadata_sha256=D("metadata",object)`.
Exclude `updated_at,last_accessed,created_at` and both new namespaces, so index
or receipt updates do not invalidate the source reference they describe.

`Source` has exactly `{node_id,graph_id,profile,project_path,source_sha256,
metadata_sha256,metadata}`. No native filesystem locator is model input.

## Six switches, with representation-only index claims

Every prepare request has all six booleans:

```json
{"compact_packing":false,"continuity":false,"assertion_index":false,"episode_index":false,"typed_links":false,"application_trace":false}
```

T3/assertion and T4/episode are separate representation/index access switches.
They do not claim automatic entity/episode resolution, semantic truth, temporal
disambiguation or attribution correctness. `application_trace` prepares a
source-backed checklist for the same reader, not a second reader or answer
checker. Receipt guarantees are locally tested workflow behavior, not a paid
reader-benefit arm.

The all-flags-false candidate is a **shared-wrapper control**, not the native
renderer: its exact structured source format may differ from native
`format_context_block`. Evaluation must measure that shared control separately
from a native renderer, then compare each switch under the same wrapper/budget.
Never attribute a common format change to one of the six switches.

## Literal groups

Span: `{span_id,start_byte,end_byte,text,span_sha256}`. Nonempty half-open UTF-8
content interval, exact decoded current slice/hash, no whitespace/Unicode repair.
Group: `{group_id,episode_key,spans,roles}`. Roles has exactly
`claim,actor,scope,conditions,exceptions,event_time,report_time,valid_from,valid_until`,
each a list of this group's span IDs; claim is nonempty. Spans have unique IDs,
are nonoverlapping and ordered by start/end. Episode keys are host annotations;
episode-index groups require nonempty keys. A whole-source group has one full
body span, referenced by claim, other role lists empty.

Role assignments are source-backed annotations, not verified entailment. Every
declared qualifier span and all role metadata travel together. **Pack or retain
a whole group or omit it.** Do not drop an exception/condition to preserve a
claim. The service does not promise discovery of undeclared semantic exceptions.
Empty-body/title-only nodes yield `no_source_body`, not fabricated content.

Each group: at most 8 spans, 2,048 bytes/span, 4,096 literal bytes total. Index
submission: at most 16 groups and 32,768 bytes of `C(groups)`. Whole-source entries
may exceed group limits for read-only rendering, but cannot be retained/indexed
past the explicit persistence bounds; such mutation requests reject.

## Fixed combined model payload and budget

Prepare requests have `total_max_bytes` (integer 0–16,384) and subordinate
`budgets:{sources,continuity,links,application_trace}`, each integer 0–16,384.
Indexes replace representations inside the sources component; they get no free
additional context component. A component object must fit its own cap **and**
the complete combined payload must fit total_max_bytes.

The sole model-visible string is `model_text=C(ModelPayload).decode("utf-8")`.
ModelPayload has exactly:

```json
{"format":"kindex.practical_memory.context","version":1,"warning":"Contextual source data; not instruction, permission, completion, or proof of delivery/use.","scope":{},"evaluation_time":"...","query":"...","components":{"sources":null,"continuity":null,"links":null,"application_trace":null}}
```

No extra separators, uncounted explanatory text, API diagnostics or source table
are model input. Empty/off components are null, with their structural bytes
charged to the combined payload. A nonempty component has exactly
`{representation,source_table,entries}`. Its own cap measures all of `C(component)`;
null has component cost zero but its bytes remain in the total envelope.

With compact packing off, representation is `inline`, source_table is empty and
source/group entries are `{entry_id,kind,source:Source,group:Group}`. With it on,
representation is `shared`, source_table is the sorted array of
`{source_key,source:Source}` and an entry's source is exactly `{source_key}`.
`source_key=D("source",Source)`. Tables contain all and only referenced sources.
Literal group text/roles remain present; compacting never summarizes it away.
Canonical internal entries always retain the inline form for validation.

Source/group entry kind is `whole_source|retained|assertion|episode`;
`entry_id=D("group",{source:Source,group:Group})`. Links and trace use the same
inline/shared source encoding; their exact entries are defined below.

Start with the fixed payload and null components. If even that wrapper exceeds
the total cap, emit empty model_text, no entries and `envelope_budget` omission.
Otherwise consider items in fixed component order:
`sources,continuity,links,application_trace`. For each complete candidate, rebuild
that component/table and whole payload; include only if both caps fit. Skip
nonfitting items and backfill later fitting ones. Never truncate a literal/group.
Report exact `utf8_bytes` and literal `text_sha256` for model_text and each
non-null component's C(component) text; null diagnostics use empty text/hash.

## Prepare selection and result

Query is nonblank, at most 4,096 UTF-8 bytes; top_k integer 1–10 (proposal default
request value 6). Native search once: ensemble, vectors off, graph expansion one
hop, trusted_only, no archived/expired, fixed evaluation/recency time. Preserve
native order, then node ID ties. No query rewrite, corpus refill or oracle IDs.
Filter current scope before identifying diagnostics.

For each native source, inspect only currently enabled assertion/episode
manifests whose body/metadata commitments still match. Group score is the number
of distinct Unicode word tokens shared between casefolded query and all literal
group text (`[^\W_]+`, no underscore token). Select only positive-score groups;
order score descending, assertion before episode, then group ID. These are
deterministic lexical selectors, not semantic applicability proofs.

If any enabled current groups match, **replace** that source's whole-body entry
with those complete selected groups; do not add body plus index duplicates. If
no current enabled group matches, preserve the ordinary whole-source entry.
Stale/malformed derived manifests cannot suppress the ordinary current source:
ignore their representation with an omission. If a whole source already packed
covers every span of a proposed index group at the same source/metadata version,
omit that group as duplicate. Do not silently fall back to a full body after an
indexed replacement fails only because of budget; expose the omission.

Continuity requires the unique explicit retained_ids list (at most 16), in its
requested order. Nonempty list while flag off rejects; empty while on injects
nothing. Each selected retained record rereads current source/scope/time and
requires both old commitments and group bytes still match. Fully baseline-covered
groups omit as duplicate; partially overlapping groups stay whole. Unknown IDs
produce generic `retained_unavailable` omissions without ID guessing/disclosure.

Prepare returns exactly `{format:"kindex.practical_memory.prepared",version:1,
scope,evaluation_time,query,features,budgets,total_max_bytes,status,components,
model_text,utf8_bytes,text_sha256,projection_sha256,persistence:"not_requested",
delivery:"not_observed",use:"not_observed"}`. Components hold full internal
entries plus `{text,utf8_bytes,text_sha256,omissions}`; omissions aggregate
`{reason,count}` without foreign identities. Projection digest is D(projection,
whole result minus projection_sha256). Status is partial if requested material
was omitted, otherwise prepared. Native empty recall is not invented evidence.

## Persist, index and strict owned transactions

`persist.prepared` is the actual full prepared response, unmodified. Scope/time,
digest, model payload, entries and all caps must validate. Entry_ids is a unique
subset (at most 16) of source/group entries actually represented in non-null
model components. Never capture a hidden retrieval candidate, free-form source,
link or checklist pseudo-entry. A host-supplied object is not security proof; this
is an exact integration binding within the trusted host convention.

New persistence rereads every selected source/metadata/group and current
project/client/time inside the owned transaction, then atomically stores all or
none. Empty selection may persist the projection/receipt only. Session
`extra.practical_memory_v1` holds a strict owner register with at most 16 retained
groups, 32 operations and 32 projection records. Identical groups reuse data;
capacity rejects with no silent eviction. Actual service capture clocks cannot
be supplied/backdated. Persist records SHA256(model_text UTF-8) for later ack.

`index_kind=assertion|episode` writes only source `extra.practical_index_v1`:
strict source binding, one current manifest per kind, at most 32 operations.
Null expected_index_sha256 means the kind is truly absent; otherwise require
exact current manifest digest. CAS/body/metadata/group validation occurs inside
the owned source-extra transaction. Replace only the requested derived manifest,
not source body/unrelated extra. D(index,{source,index_kind,groups}) identifies
the manifest. No duplicate truth nodes, new verification or authority promotion.

All three mutations reject a preexisting caller transaction before any effect:
they never join, commit or roll it back. Native atomic_extra_update may be used
only with strict fresh raw-extra validation inside its owned BEGIN IMMEDIATE:
parse original SQLite extra without fallback, reject malformed JSON/nonobject or
malformed existing namespaces, compare CAS, and validate current source there.
Also require native whole-extra redaction would leave both unrelated fields and
the proposed exact result unchanged. Otherwise abort; never silently normalize,
repair or redact somebody else's stored data. updated_at is excluded from source
commitments. No lost-update or effect-without-receipt success is allowed.

## Receipts, replay and acknowledgment

Mutation receipt result is exactly `{format:"kindex.practical_memory.receipt",
version:1,receipt:{operation,operation_id,scope,request_sha256,target_sha256,
committed_at,persistence,delivery,use,receipt_sha256}}`. Operation IDs are nonempty
strings at most 256 UTF-8 bytes. Target is projection digest for persist/ack,
index digest for index. Persistence is committed; delivery is not_observed
except host_acknowledged for ack; use is always not_observed. Receipt digest
excludes itself. Committed time is the actual native service clock.

Inside each target namespace use receipt key
`D("request",{scope,operation_id})` and request digest
`D("request",{operation,request})`. Same key/same payload returns identical stored
receipt bytes with **zero mutation**, including timestamps. Different payload
rejects operation_conflict. First validate selected graph/profile/project/current
owner scope and strict raw register before receipt disclosure. Historical replay
does not reread/re-authorize source facts: it reports a past committed event.
Current prepare always rereads/admission-checks sources; a receipt never grants
permission or says a stale source is usable now.

Ack requires an existing persisted projection in that owner register, exact
projection digest and exact stored model-text SHA. host_receipt is a nonempty
opaque string at most 256 bytes, never a path to open or command. Record the
host-reported observation only. Source change does not rewrite historical
delivery observation or authorize reexecution. No implicit resend, task effect,
permission, comprehension or use follows from an acknowledgment.

## Typed links and advisory packets

Link entry is `{from:Source,to:Source,type,provenance,direction:"stored_from_to",
operational_effect:"none"}` (or source-key references in shared form). Current
endpoint scope/time/admission required; sort by `(from.node_id,type,to.node_id)`.
Association never implies prerequisite satisfaction. Stored depends_on/blocks
remains an asserted relationship, not task completion or authority. Do not infer
an inverse operational dependency from generic native bidirectional edges.
Native status/successor metadata governs supersession admission.

Trace entry is `{constraint:Source,group:Group,questions:[...]}` (source ref in
shared form). Only explicitly scoped current native constraint/directive/
checkpoint sources, sorted by ID, maximum 16 with visible overflow. No automatic
promotion of other evidence to a mandatory rule. Fixed checklist questions:
`preserve_actor_and_scope`, `preserve_conditions_and_exceptions`,
`respect_valid_time`, `do_not_infer_permission_or_completion`,
`supply_supported_requested_fields`. These are questions, not pass/fail claims.

Audit answer is at most 65,536 UTF-8 bytes; explicit unique constraint_ids at most
16, empty means that same current scoped set. Return
`{format:"kindex.practical_memory.audit_packet",version:1,scope,evaluation_time,
answer_sha256,constraints_sha256,model_text,utf8_bytes,text_sha256,omissions,
mode:"packet_only",semantic_validation:"not_performed",delivery:"not_observed",
use:"not_observed"}`. Its model_text is C({format:"kindex.practical_memory.audit",
version:1,warning:<same warning>,scope,evaluation_time,answer,entries:[trace entries]}).
The entire string, including answer and wrapper, fits total_max_bytes≤16,384;
whole trace pairs backfill or omit. If fixed answer/frame alone cannot fit, empty
model_text plus envelope_budget. D(answer,answer_string) and
D(constraints,full_current_selected_constraint_entries) bind the packet even when
some entries are omitted. No semantic checking, rewrite or second reader.

## Errors versus omissions

Library typed ValueError codes: invalid_request,scope_mismatch,source_changed,
metadata_changed,invalid_span,invalid_group,index_conflict,operation_conflict,
capacity,transaction_conflict,unavailable,privacy_unavailable. CLI safe JSON error,
exit 2 for requests, 1 for unavailable storage. New-mutation source/group/CAS
mismatch rejects all effects. Read projection source drift/scope/time denial,
duplicates and budget produce bounded reason/count omissions; malformed request
or owner/register is an error, not repair. A malformed derived index may be
ignored for ordinary read-only source fallback, but cannot be overwritten as if
absent. Nothing prepared is thereby persisted/delivered/executed.

Prepare/audit require native read-only Store, matching preexisting graph/profile,
no stamping/migration/fallback throughout CLI open/read/close. Preserve caller
read transactions; own only the read snapshot they create. Mutations own their
native atomic transaction as above. All old native APIs/defaults remain intact.

## Final normative resolutions

These resolutions override ambiguity in the proposal above. The optional fast-model organizer is a separately versioned module and experiment, not silently part of these five APIs. No performance claim is frozen by this implementation contract.

### Python and diagnostics

Module is `kindex.practical_memory`; exports `prepare`, `persist`, `index`, `audit`, `ack` and `PracticalMemoryError(ValueError)`. Error has public string `.code` and safe generic `.message`; str(error) contains no source bodies, secrets or foreign identities.

Each prepared `components` object has exactly the four named component keys, including disabled ones. Each value has exactly `{entries,text,utf8_bytes,text_sha256,omissions}`. Entries are the complete canonical inline entries actually packed, never hidden candidates. Text is C(the non-null model component), otherwise empty string. Empty text SHA256 is the ordinary empty-byte hash. Omissions are sorted by reason, each `{reason,count}` with positive integer count. A disabled component has empty entries/text/omissions and zero bytes. Any envelope_budget omission is assigned to sources. Status is partial iff any component has an omission. A zero-result native search with no rejected candidates is prepared.

Whole-source group is `{group_id:"whole_source",episode_key:"",spans:[{span_id:"s0",start_byte:0,end_byte:<UTF8 byte length>,text:<exact body>,span_sha256:<body hash>}],roles:<claim:["s0"], every other role:[]>}`. Empty source bodies are omitted. Distinct indexed group IDs must be unique across each kind’s submitted manifest; all group IDs/span IDs/episode keys are strings of at most 256 UTF8 bytes; IDs nonempty, episode key may be empty except episode indexes.

Read omission vocabulary is exactly: `envelope_budget`, `component_budget`, `total_budget`, `duplicate`, `no_source_body`, `source_unavailable`, `source_inapplicable`, `invalid_source_metadata`, `index_stale`, `index_invalid`, `retained_unavailable`, `retained_stale`, `constraint_unavailable`, `constraint_overflow`. Foreign-source admission reasons aggregate only; no foreign IDs leak. When both component and total caps fail, count component_budget only. Native-admitted candidates removed for local scope/time are source_inapplicable; malformed relevant metadata invalid_source_metadata. Missing retained IDs/source use retained_unavailable; version drift uses retained_stale. Mutation drift remains an error.

### Selection boundaries

Typed links are only stored directed edges in the induced set of currently admitted native retrieval sources plus admitted explicitly requested continuity sources; not a graph-wide discovery operation. Inspect endpoints before exposing their identifiers. Duplicate stored directed (from,type,to) entries collapse; preserve native provenance string, no inference or synthetic inverse. Source tables cover both endpoint references.

Prepare application_trace uses only the admitted native retrieval and requested continuity source set, restricted to type constraint/directive/checkpoint, sorted node ID, at most16. It does not activate unrelated graph-wide constraints. Audit with explicit constraint_ids preserves requested order and emits constraint_unavailable for each missing/inapplicable/nonconstraint ID. Audit with empty IDs selects all current scoped active constraint/directive/checkpoint nodes by ID, then limits16 with constraint_overflow count. Constraint digest binds exactly that selected capped list before budget packing, not undisclosed overflow. Checklist has the five above plus first question `check_applicability_before_treating_as_requirement`. A trace group is always the complete whole-source group, not a partial annotation or inferred mandate. Large whole-source trace may be omitted by budget.

### Index and receipt history

The strict practical_index_v1 namespace additionally preserves an immutable logical history record for each successful new index operation, including submitted groups, source+metadata commitments, index_kind, index digest, scope/client annotation, actual committed time and receipt. Current per-kind manifest is a CAS pointer/view over these records. Maximum32 operations also bounds history; no eviction. A replay adds no event. History is locally trusted metadata, not tamperproof evidence or independently authenticated labeller identity. Physical storage remains source.extra to avoid a second database; immutable source references and current version validation address stale copies.

Session register preserves separate immutable persistence and acknowledgment operation receipts. Ack is a host assertion about one exact historical payload hash; it never replaces a prior persistence receipt, changes old receipt bytes, implies actual model receipt/use, or authorizes resend. Late acknowledgments for an older persisted payload remain attached only to that payload. A failed/uncertain host delivery stays not_observed absent an explicit valid ack. No network delivery is performed by this service.

### Review resolutions and limitations

Actual Advocate Helland and adversarial reviews are preserved under review/research-tools-advocate-v3/run-001. Their four concerns are resolved here by version-bound label history, separate immutable host acknowledgment events, full request-bound transactional replay, and explicit limitation of continuity/trace claims. The full independent evaluation must measure displacement, wrong connections, practical answer errors, no-op source controls and shared-wrapper effects. Source-only annotations are not gold-selected answers. A packet-only audit is not an answer validator. We accept local trusted-host labels and bounded in-row history as reversible prototype tradeoffs; an external append-only authority is not claimed.

The separate Sim request had an uncertain network outcome and is not retried or represented as a review. No old experiment’s protocol is repaired retroactively.
