# Read-only query context and optional handoff

`kin context-query` and the full MCP server's `query_context` prepare exact
current stored-source context from one selected read-only graph. Retained
handoff is optional and has an additional declared byte cost. This operation
makes no delivery/use, action-permission, authenticated-author or future-validity
claim. A stored document can itself be derived; this is not a reconstructed
original external source chain.

```sh
kin context-query 'current deployment policy' --source-bytes 8192 --client codex
kin context-query 'current deployment policy' --source-bytes 8192 --client codex \
  --include-handoff --evidence-session-id SESSION_ID --handoff-bytes 4096
kin context-query 'current deployment policy' --source-bytes 8192 --client codex \
  --include-handoff --evidence-session-id SESSION_ID --handoff-bytes 4096 \
  --selection explicit_selected --evidence-id EVIDENCE_UUID
```

Use the existing configuration, profile, data-directory and project options to
select the graph. The new CLI operation uses canonical configuration
`_project_path` when present, otherwise canonical real cwd. Exact source and
original session project ownership must match. A native session started in a
different exact subdirectory does not silently become repository-root-owned;
select its exact project rather than rewriting its historical owner.

A matching preexisting profile stamp, persisted graph identity and current
schema are required. The adapters open the selected Store with `read_only=True`,
`migrate=False`, `manage_project_storage=False`. Missing graphs/stamps/schema
fail without a writable fallback or initialization. Ordinary SQLite read-only
WAL coordination remains supported; this operation does not use `immutable=1`.
No-profile explicitly selected graphs are supported.

`--client` is a trusted local operator scoping choice. It must agree with any
`KIN_CLIENT` / `KINDEX_CLIENT` configured process binding. Recognized aliases are
normalized; unknown, empty, `plain` and conflicting choices fail. With no client
binding, the explicit conservative mode is `unscoped_only`, which excludes
client-restricted sources. MCP accepts no client or project override; it derives
both from trusted server/request context. These names identify adapter families,
not authenticated individual agents or a multi-user ACL.

Explicit `client:` and `agent:` restrictions and native bare `antigravity` /
`opencode` scope tags are enforced. Native bare topical `claude`, `codex`,
`gemini`, `cursor` and `ag` tags retain their existing meaning. Malformed tags and
unknown explicit restrictions fail closed. Source project/client scope is
checked before text or identifying diagnostics. Original session ownership and
every current pin source use the same filter, including explicitly selected
pins. Current client descriptor and normalized/raw scope-tag hashes are bound
separately; old capture metadata never claimed to bind historical client tags.

The library interface is:

```python
from kindex.query_context import TrustedClientScope, prepare_query_context

result = prepare_query_context(
    readonly_store,
    query="current deployment policy",
    project_path="/selected/project",
    client_scope=TrustedClientScope(mode="client", client="codex"),
    retrieval_max_bytes=8192,
    include_handoff=True,
    evidence_session_id="original-stable-session-id",
    handoff_max_bytes=4096,
)
```

Supply a native validated read-only Store. `open_query_store(config)` provides
non-stamping adapter setup and validates the persisted graph/profile. A profiled
lazy Store that could attempt native profile stamping is rejected before
preparation. An already owned read transaction is preserved; otherwise the
operation owns and closes its one coherent read snapshot. No source, capture,
receipt, access clock, feedback, configuration or health record is written.

Native CLI/MCP privacy controls remain active. If their egress redaction would
change any part of the prepared exact response, this operation fails explicitly
under the current egress privacy policy. It does not return rewritten source text
with stale byte counts, offsets or hashes. This check applies to library output
as well, keeping adapter behavior consistent. Native redaction patterns may also
match harmless text; those false-positive matches can cause the same explicit
refusal. The operation does not weaken the privacy policy to avoid them.

A query is nonempty and at most 4096 UTF-8 bytes. `top_k` / `--top-k` is a strict
integer from 1 through 10 (default 6). Both byte ceilings are strict integers
from 0 through 16384. Boolean or coerced numeric inputs are rejected. Evidence
IDs are canonical UUID strings, unique and limited to twelve. A handoff always
requires the original stable session ID, even with zero handoff bytes. Unknown
MCP arguments, JSON-stringified lists and coerced primitive values are rejected
by this new tool's own strict framework argument model before its handler.

Native source ranking runs once with RRF, vectors disabled, one graph hop and
trusted current sources at one evaluation time (`--at` / `evaluation_time` may
supply timezone-aware RFC3339). Only returned project/client-admitted candidates
provide source bytes, in native order. This wrapper neither refills from the
corpus nor promises cross-SQLite-version tie stability. Incidental native
wall-clock age, attached unknowns and detailed fence records are excluded.

Whole exact source bodies are preferred when their complete entries fit.
Otherwise blank-line paragraphs become UTF-8-safe windows of at most 768 raw
bytes. Positive overlap counts distinct casefolded Unicode alphanumeric query
tokens. Score descending, then original byte offset, chooses up to two fitting
windows per source; emitted windows keep document order. Literal bytes and
half-open offsets stay exact. Whole entries fit or are omitted, with backfill.
This heuristic is bounded selection, not a completeness guarantee.

The baseline is prepared independently of handoff and preserves its exact text,
hash, order, omissions and source budget when handoff is enabled. Default
`query_candidates` handoff selects original captures whose current sources occur
in the admitted native candidates; it imposes no lexical overlap test on the
span. A low-overlap exception can qualify, as can an irrelevant span from a broad
matched source. `evidence_ids=None` scans the bounded register; `[]` selects none.
`explicit_selected` requires nonempty IDs and separately labels caller relevance
selection, including sources outside native candidates. It grants no broader
project/client/trust/authority scope or automatic recall claim.

Original full capture/receipt validation, current body/metadata/time/trust checks
and exact prerequisite action matching remain in force. Stale or unauthorized
history does not enter the new context. Fully duplicate pins are omitted as
`already_in_baseline` only when the same source/metadata version and entire
capture interval are contained in an actually emitted baseline interval. Partial
overlap never truncates a retained pin. Corrupt or unavailable handoff, unknown
IDs and wrong session ownership retain the prepared baseline and expose a typed
handoff disposition without historical contents. Denied scope details are
counts, or a generic reason for an explicitly requested evidence ID.

Both components count their complete canonical escaped JSON envelopes, including
warning, scope, full metadata, source/span hashes, separators and literals. No
budget is borrowed, subtracted or transferred between components. Empty text has
zero bytes and its own matching SHA256; structured omissions remain available.
Retrieval text uses `kindex.query_sources` version 1; handoff text uses
`kindex.query_handoff` version 1 and visibly declares its selection route. Each
entry includes current client binding. Compact handoff entries also retain
capture/evidence/record/span/source bindings and full metadata; full native
locators and persisted receipts remain available through historical inspection.

The response format is `kindex.query_context`, version 1, with query, evaluation
time, selected scope, retrieval, handoff, status and `delivery`/`use` both
`not_observed`. Components include text, exact bytes/hash, structured included
spans, bounded omission records/counts and selection. Retrieval exposes only
admitted scoped candidates and native ranks; handoff status distinguishes
`disabled`, `prepared`, `partial` and `unavailable`. Overall `partial` preserves
successful baseline preparation when a requested piece is omitted/denied or
handoff is unavailable. No hit can be a prepared empty result. Checksums are
local consistency commitments, not signatures. Re-prepare before operational use
when state may have changed; a later denial cannot prove an earlier consumer
received a correction or undo its beliefs.

Existing search/context/resume APIs, old evidence projection format and all
hooks retain their defaults. This feature supports explicit CLI and full MCP
preparation only; MCP-lite and automatic hook delivery are outside this pilot.
