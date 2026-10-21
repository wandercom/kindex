# Bounded optional metadata encoding and incoming notes

This candidate extends the frozen v8 library explicitly. Native retrieval and
writer APIs, source metadata, correction policy and default results are unchanged.
No scheduler, provider call, migration, scope discovery or source repair is added.

```python
resolve_references(store, request, *, context, incoming_later=False)
expand_references(store, request, *, context,
                  metadata_encoding="full", incoming_later=False)
decode_evidence_text(text)
```

The request objects and host context remain v8 objects. `metadata_encoding` accepts
only `full` or `lossless_common`; `incoming_later` must be an actual boolean. Invalid
options fail before storage access. Factoring supports modes `none` and `direct`;
incoming navigation requires `direct`. A nondefault option with `dream` fails.
The CLI exposes `--incoming-later` on `reference` and `expand`, and
`--metadata-encoding full|lossless_common` on `expand`. Defaults are unchanged.
The MCP `correction_references` tool exposes the same explicit options.

Nondefault expansion returns `evidence-result/2` with
`options: {metadata_encoding, incoming_later}`. Incoming reference resolution
returns `reference-result/2` with `options: {incoming_later: true}`. Default results
keep their original schemas and fields. Existing capacities, source order,
correction/conflict groups, receipt semantics and separate budgets remain in force.

## Complete metadata, different representation

For each tentative complete envelope, `lossless_common` constructs both ordinary
full JSON and recursive common/delta JSON. Factored envelopes add
`metadata_encoding: "recursive-common-with-source-delta/1"` and
`metadata_common`. Sources retain their exact reference, applicability and whole
body; `metadata_delta` replaces only their `metadata` field. Deep-merging common
with each source delta restores every metadata field, including the complete v8
extra object. Neither source nor assertion text is summarized or clipped.

Common extraction examines all keys present in every metadata object. Object
values recurse, retaining even an empty common object. Lists/scalars/null remain
atomic and are shared only when their canonical JSON bytes match. Missing keys,
empty objects/lists, null, true, 1 and 1.0 remain distinct. Delta construction and
deep merge never mutate their inputs.

Every required-group, reference-only fallback, history and optional trial measures
the actual selected complete canonical encoding. Factoring is selected only when
strictly shorter; full wins ties and negative savings, and envelopes without
sources remain full. Base and optional envelopes each carry their own complete
common metadata. Encoding markers, references, assertions and connections count
inside the original caps. There is no provenance sidecar or extra model budget.

The pure decoder accepts empty text as `None`, or canonical full/factored evidence
JSON as the complete original full v8 envelope. It rejects duplicate/noncanonical
JSON, nonfinite or malformed UTF-8 data, malformed source/ref/envelope shapes,
duplicate identities and mismatched reconstructed body/metadata hashes, using
`ReferenceError.code == "invalid_evidence_text"`. It reads no Store and infers no
current access, truth or applicability. Obtain text through scoped APIs; a valid
hash is not authorization.

Within one envelope, every occurrence of a native node ID must carry the same
complete canonical reference across source/reference rows, assertion endpoints and
connection supports. Recomputing a connection ID cannot legitimize contradictory
source-version bindings. If a referenced body is absent, its external reference
still receives only format/coherence validation, not source or authority proof.

Opted-in expansion applies native privacy to the actual complete result and to an
internal complete representation with emitted metadata independently reconstructed
by the decoder. Non-model outer fields remain in that privacy surface. It also
checks reconstructed full envelopes. If native redaction would change either
representation, it fails `privacy_unavailable`; it never hides or removes a source
to bypass refusal. Only actual emitted text contributes to returned text bytes and
digests. The reconstruction is internal, never an off-budget returned payload.

## Actual incoming later-note edges

With `incoming_later=True`, existing outgoing direct candidates retain precedence.
The new tail uses only actual stored `later -> seed` native `relates_to` or
`context_of` rows, sorted by base seed priority, original edge type, then later
source ID. There is no reversed `depends_on`, `contradicts` or correction edge,
second hop, generated relationship or extra slot/budget.

Both endpoints require existing host scope, exact current version and privacy
checks. The later note must be nonhistorical under the existing eligibility and
scoped-conflict rules. An exactly bound historically accessible seed may remain a
historical reference; navigation does not promote it or require its old body.
Both `prov_when` values must be aware RFC3339 instants satisfying
`seed < later <= evaluation_time`. No timestamp fallback, equal/future chronology
or malformed time qualifies. Provenance chronology is not a semantic event date,
correction, permission, completion or supersession claim.

Deduplication uses the newly navigated ID: outgoing's native target or incoming's
native later-source endpoint. Eligibility filtering precedes deduplication and
the unchanged common `optional_limit`. Hidden endpoints produce no visible counts,
identities or diagnostics. Every selected source receives existing bounded
correction/qualification/conflict closure before optional packing. Required current
evidence remains atomic, with no old-only fallback; a historical seed alone is not
a required current instruction.

Connections retain `route: "incoming"`, actual relation, original directed support
order `[later_source, earlier_seed]`, full references, empty spans, null motif and
`status: "declared_link"`. IDs use the v8 framed hash named `incoming` over
`{from_ref: later_ref, to_ref: seed_ref, relation}`. Reference optional descriptors
also use incoming/declared_link. Sources/edges/registry rows are not changed.

These opt-ins concern representation integrity and bounded navigation only.
Compression does not prove model interpretation, token savings or answer quality;
an edge and chronology do not establish correctness. Missing links/times, the
outgoing-first cap, original query seeding and inherited v8 scope/shared-correction
limitations remain. No automatic workflow is enabled.

Incoming enumeration admits at most 1,024 distinct matching edge rows across all
base seeds, before endpoint probing. A SQL LIMIT reads at most the remaining
allowance plus one sentinel; overflow fails the entire operation with generic
`unavailable`, without source identities or degree counts. It does not silently
rank a truncated tail. This is a bound on Python row allocation and endpoint
probes, not SQLite's internal index scan/sort work or a timing noninterference
guarantee. Inherited outgoing enumeration is unchanged and remains sensitive to
large edge sets.

Fresh synthetic practical qualification retained 20/36 facts in both full and
packed arms; packing saved 3.58% bytes and increased local expansion time. This
supports a representation tradeoff, not improved recall or reader usefulness.
