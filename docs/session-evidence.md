# Explicit session evidence

Evidence retention is an opt-in addition to native session resume. Select the
normal graph/project, start a session tag, and keep its stable node ID. A tag name
can be reused; the node ID owns the evidence. Hooks and ordinary search continue
to use their existing behavior.

```sh
kin evidence snapshot --node SOURCE_ID
kin evidence retain --session-id SESSION_ID --node SOURCE_ID \
  --expected-source-sha256 BODY_DIGEST --expected-metadata-sha256 METADATA_DIGEST \
  --start-byte 0 --end-byte 120 --operation-id capture-1 --cue 'decisive detail'
kin evidence inspect --session-id SESSION_ID
kin evidence project --session-id SESSION_ID --max-aux-bytes 4096
kin tag resume TAG --include-evidence --evidence-session-id SESSION_ID \
  --evidence-max-bytes 4096
```

Offsets are half-open UTF-8 **byte** positions in the exact stored content. They
must fall on character boundaries. No newline, Unicode, combining-character or
BOM normalization occurs. A span is at most 2048 bytes; a cue is at most 256 bytes.
Both snapshot digests are checked inside the native write transaction. The
selected Store is the only database used; a saved locator cannot open another.
The locator must also match the selected canonical project and active profile
(an empty profile when none is selected). Mismatches are rejected, including on
replay, rather than silently rewriting the caller's locator. Existing captures
are never rewritten: a stored locator that fails these bindings remains in
storage but cannot be verified or projected in that selected context.

The dedicated `extra.retained_evidence_v2` register stores up to 12 complete
captures and their receipts. It preserves all other session metadata. Different
operation IDs consume separate slots, even for the same span. Capacity failure
never evicts a capture, and stale captures still count. Deliberately start
another session if a new register is needed. Malformed existing data causes a
capture failure without normalization. Literal data requiring Store redaction is
rejected rather than changed.

Replay with the same operation ID and canonical input returns the original
persisted receipt without a write, even if the source has since changed or been
deleted. Different input under that ID fails. A stored receipt attests to the
SQLite commit. It does not attest power-loss survival, filesystem fsync,
whole-source backup, authenticity, delivery, reading, successful use or completed
work. Checksums are local consistency checks, not signatures.

Inspection separates intact historical text from present applicability. Source
edits, deletion, metadata drift, changed scope, native trust denial and expiry
exclude a capture from current projection while preserving its saved text.
Supersession is disclosed but never followed automatically. Without the original
source, the saved full-source digest remains a capture-time binding; only the
retained span and record can be recomputed. Unverified sources can be retained
historically but do not enter current projection. Sources lacking explicit
project metadata are marked `legacy_unscoped`, never claimed as project-owned.

Metadata hashing covers native `type`, `prov_who`, `prov_activity`, `prov_source`,
`prov_when`, `verified_at`, `verified_by`, `prov_method`, `valid_at`, `invalid_at`,
`status`, and `extra.expires`. It also binds applicable `extra.project_path`,
`superseded_by`, `referent_stale`, `expired_at` and `kinbase` markers. Missing
scalar fields are null; absent extra markers remain absent. Access counters and
access timestamps are excluded. Actor labels are unauthenticated attribution.

Use `--kind prerequisite_context --action deploy` for advisory context attached
to an exact lowercase action key. Projection includes it only for `--action
deploy`, before ordinary evidence. It is not an action permission, satisfaction
proof, or automatic blocking rule.

Projection emits a losslessly escaped JSON envelope with
`format="kindex.evidence_projection"` and `version=2`. Its exact top-level keys
are `format`, `version`, `warning`, `scope` and `entries`. The shared `scope`
contains the selected `graph_id`, stable `session_id` and canonical
`project_path`. Every complete entry contains `evidence_id`, a `source`
projection reference (`node_id`, `source_sha256`, `metadata_sha256`), a `span`
(`start_byte`, `end_byte`, `sha256`), exact `text`, `cue`, `kind`, `action`,
`captured_at`, the complete bound `metadata`, and the original `record_sha256`.
The fixed contextual-data warning remains inside the counted envelope.

Full-record current admission happens before compact rendering. The projection
reference is **not** a native `DurableSourceRef`: native database paths/profiles,
capture operation/request fields and duplicate persisted receipts stay in
storage and historical inspection. Resolve the full capture through inspection
using its evidence ID, selected scope and record digest. The compact subset
alone is not a complete independently verifiable original record, and its hashes
remain local consistency bindings rather than signatures. Complete provenance
and time metadata remain visible; literal clauses are never shortened or
replaced by summaries.

Oversized entries are omitted while later fitting entries remain eligible.
Structured omissions distinguish source/trust failures, action mismatch and byte
budget; they remain available even when no complete envelope fits and text is
empty. The cap includes all escaping, warning, scope and entry bytes. Two
arbitrary maximum-length spans are not guaranteed to fit in 4096 bytes. The
read-only `prepared` receipt hashes the exact text and records
`delivery=not_observed` and `use=not_observed`. Stored captures, persisted capture
receipts and `inspect_evidence` retain their original full format.

Resume requires the explicit evidence session ID to match the resolved tag and
project. The evidence byte ceiling is **additional** to the native resume budget.
When disabled, resume delegates to the unchanged native formatter. If nothing
fits, `kin evidence project` still exposes structured omissions. No guessed
most-recent-session injection or cross-session/cross-graph copy is performed.
MCP provides matching `evidence_snapshot`, `evidence_retain`, `evidence_inspect`
and `evidence_project` tools and the same optional `tag_resume` parameters.
