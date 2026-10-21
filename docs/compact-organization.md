# Additive compact organization

V7 adds the pure `kindex.evidence_organizer` module and
`practical_memory.organize(store, request)`. Existing v6 APIs, CLI actions,
storage, source lifecycle and native defaults are unchanged. The complete
contract is [compact-organization-contract-v1.md](compact-organization-contract-v1.md), SHA256
`5893d607bc5969e24a3a8f0abb704c741712d4eb8b8d2501f2e3bc0772623f16`.
No model request, provider, persistence, host acknowledgment or automatic action
is part of this operation. V6 and its failed evaluation protocol remain intact.

## Pure interface and exact payload

`organize({query,records,protected_ids,budget_bytes,order})` accepts at most 32
whole-source records, each at most 65,536 literal UTF-8 bytes. The strict record
shape is `{id,source,kind,text,roles,links}`. Source has native opaque node ID,
whole-body SHA256, interval `[0,UTF8-length)`, and exact nullable UTC validity
bounds. Fractional seconds (1..6 digits) remain verbatim. All six role lists
contain literal substrings; each maps to its first occurrence's UTF-8 half-open
byte interval. Repeated role values and their order survive roundtrip. Roles,
conditions and exceptions are never selectively truncated.

The sole model input is the canonical compact `model_text`. Its fixed schema
legend is delivered once:

```
record: [id,source,kind,category,text,roles,links]
source: [node_id,source_sha256,start_byte,end_byte,valid_from,valid_until]
roles: [actor,scope,conditions,exceptions,event_time,report_time]
role_range: [start_byte,end_byte]
link: [to,relation]
```

Records, sources, role ranges and links use these positional arrays. Text occurs
once per record; category is a fixed advisory mapping from its host kind.
Source IDs, versions, validity and original directed links remain in the actual
delivered payload. They are contextual data, not authority or conflict resolution.
The fixed warning explicitly states that exact matches and graph links establish
neither authority nor conflict resolution. The legend is never caller-defined.

`decode(model_text)` returns `{query,records}` exactly for selected records,
including original source/role/link objects. It rejects noncanonical JSON,
duplicate keys, unknown schema/fields/version, invalid Unicode/boundaries,
body/hash mismatch, non-first role offsets and invalid categories/relations.
Links may target omitted records. An empty envelope-budget result has no payload
to decode and is refused. Library failures are `OrganizerError(ValueError)` with
safe `.code == "invalid_request"`; no input text or identity is echoed.

No deduplication merges different IDs or versions. No text is rewritten,
normalized, summarized, truncated or semantically extracted. Callers of this
pure interface must independently admit every source; it is not a security or
applicability boundary.

## Atomic components and budget

Only literal `supersedes` and `contradicts` links create undirected packing
membership. Original directed links stay unchanged; no inverse/transitive
semantic edge or winner is inferred. Inbound corrections, forks, chains and
cycles co-deliver whole. Association/dependency links do not create components.

A protected reference protects its complete component. Protected components pack
first by earliest protected input member. Other components use native input
order, or deterministic kind/relevance order with native input position breaking
ties. Relevance is distinct casefolded Unicode token overlap, not truth.
Members of each component retain input order. Every component packs whole or is
omitted whole, including unprotected ones; later fitting components may backfill.

The entire canonical payload, including query, warning, legend and metadata,
fits `budget_bytes` (0..16,384). `body_bytes` counts original raw text UTF-8 once;
JSON escapes count as overhead. `body_bytes + overhead_bytes == utf8_bytes`.
Diagnostics and references outside `model_text` are not delivered evidence.
Omissions are explicit `component_budget` or `protected_component_budget` per ID,
plus a null-ID `envelope_budget` if the fixed empty payload cannot fit.
`protected_complete` is true only when all requested pure IDs are delivered;
an empty protected list is vacuously complete. No capacity guarantee is implied.

## Native API and CLI

```
practical_memory.organize(store, {
  "scope": {"graph_id": "...", "profile": "", "project_path": "/exact/project",
            "client": "codex", "session_id": "opaque-native-id"},
  "evaluation_time": "2026-10-20T00:00:00Z",
  "query": "...", "top_k": 6, "protected_node_ids": [],
  "total_max_bytes": 8192, "order": "native"
})
kin practical-memory organize --request-json /absolute/request.json --json
```

The illustration is not an executable release. Use the same native read-only
Store, exact graph/profile/project/client/session binding and fixed evaluation
instant as v6. CLI preserves the exact explicit receiving-project selection and
never reopens writable storage for organize. The operation uses one native
hybrid search, ensemble ranking, vectors off, one graph hop, trusted-only,
without archived/expired sources. It does not call `prepare`, prepack, directly
fetch protected nodes or refill the pool. Every unique returned hit passes the
existing current scope/time/trust admission again in the same read snapshot.
Caller-owned read transactions remain owned by the caller.

Protected IDs must be unique, at most 32, at most 128 UTF-8 bytes each, and appear
literally in the query with `(?<!\w)ESCAPED_ID(?!\w)` Unicode word boundaries.
They establish relevance only. Unknown or unavailable references are never an
admission bypass. The wrapper derives principle from constraint/directive,
episode from episode/checkpoint and fact otherwise. Native role lists are empty:
whole body metadata does not establish annotated role spans. No fixture-only
kind/role annotations are consumed. Native relation names have no aliases;
`relates_to`, for example, is not rewritten as `associated_with`.

Before removing empty, oversized or otherwise unrenderable sources, the wrapper
computes correction/contradiction membership over **all** natively admitted
hits. One unrenderable member removes its entire component, so an oversized
correction cannot leave a stale old source alone. Excessive links, unsupported
IDs or incompatible reference/time formatting safely remove their component;
no correction edge is silently dropped to make it fit. Native lifecycle may
already exclude a superseded old body; that admission is never widened.

Native `retrieval_ids` lists only `_admit`-accepted hits in original order, never
foreign rejected IDs. `admitted_ids` and `source_references` then identify the
further renderable subset, including records that may later be omitted by the
pure byte budget. The organizer result says which records actually reached the
payload. Native admission/format omissions aggregate reason/count, including
`no_source_body`, `source_oversized`, `source_unrenderable`,
`component_unrenderable` and `protected_unavailable` as applicable, alongside
existing admission reasons. No omitted source body or foreign identity appears
in these aggregates. `unavailable_protected_ids` echoes only caller-supplied IDs
in caller order. Overall protection also requires no unavailable protected ID.

The full native response passes the existing privacy boundary. If native egress
would rewrite exact payload or metadata bytes, it fails rather than advertising
incorrect hashes; harmless matching credential-shaped strings can also be
refused. Persistence/delivery/use remain not requested/not observed. An organized
result is not a v6 prepared persistence object and does not create receipts or
acknowledgments. Source-only mechanics and byte integrity do not establish reader
accuracy, model qualification, efficacy, authority or operational completion.
