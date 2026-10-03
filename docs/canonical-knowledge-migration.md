# Canonical JSONL knowledge and migration

JSONLs are the canonical bearer of knowledge. SQLite databases, including
`kindex.db`, are disposable caches. The agent is explicitly responsible for
ensuring that all durable knowledge it reads or writes in the database is
maintained losslessly in canonical JSONLs. For Git-backed project graphs, commit
those sources with the related code so collaborators receive the same knowledge.

Worktrees are ephemeral and deletion is outside our control. Best-effort cleanup
is encouraged: when a managed teardown is available, check source coverage and
persist pending knowledge when possible. Users and Git/GitHub tools can bypass
that step, so correctness and recovery must not depend on cleanup, database
preservation, archival, merging, or rescue. Optional database merging may improve
efficiency; it is never a correctness prerequisite.

Data loss is an expected failure mode, not a desired outcome. Agents maintaining
canonical sources reduce it, but prompts alone cannot guarantee persistence or
recovery. The system must reconstruct disposable state from surviving canonical
knowledge when cleanup did not happen. Knowledge that was never persisted to a
surviving source cannot be recovered by inventing evidence; disclose precisely
what is unavailable. Accepted release-documented migration exclusions do not
permit arbitrary ongoing unrecoverability.

## Prompt your agent on an existing installation

Re-run the instruction-file setup for your client to install the updated rules.
Then give your agent this migration request:

> Ensure all existing knowledge in my Kindex `kindex.db` is represented losslessly
> in the canonical JSONLs. Inventory every durable knowledge record and
> relationship, including complete content, metadata, provenance, lifecycle
> state, and source bindings. Compare that inventory with the canonical sources,
> migrate database-only knowledge, and reconcile legacy JSON with JSONL without
> truncation, dropped fields, or silent format selection. Respect audience and
> secret boundaries. Verify complete coverage and source/reference consistency,
> commit the project sources with the code, and keep them current after every
> capture, edit, and link. Report exact unsupported fields, record types,
> conflicts, or reader limitations. List intentional migration exclusions with
> their scope, reasons, and consequences in release notes. Report unexplained
> omissions separately; do not claim excluded knowledge was losslessly migrated.
> Treat SQLite as disposable. Encourage best-effort cleanup, but recover from
> surviving canonical knowledge without depending on teardown. Report knowledge
> that was never persisted and cannot be recovered.

A version upgrade alone does not perform this migration. This change provides
agent instructions and release-note guidance, not a new canonical serializer,
automatic migration, or cache reconstruction implementation.

## Accepted migration loss and compatibility limits

Intentional knowledge loss is acceptable when it is part of the migration's
stated behavior and release notes identify what is omitted, why, the affected
population or record types, and the resulting limitations. Keep a coverage
report for retained knowledge and a separate list of intentional exclusions.
Do not claim excluded knowledge was preserved, or reinterpret an accidental
ongoing writer failure as an intentional migration choice.

The current accepted limits are precise:

- #69 deliberately retains legacy `knowledge.json` precedence. When both formats
  exist, the importer reads JSON only; JSONL-only records are absent from that
  import, but the JSONL bytes are not deleted. There is no automatic union or
  dual-write, and old clients do not gain JSONL support. Agents reconcile sources
  explicitly when migrating. This compatibility choice is not a merge blocker.
- #71 does not backfill captures whose only source bindings are expired session
  handles. Historical provenance may remain unresolved. Reconstruct bindings
  only when canonical evidence verifies the original source/revision; otherwise
  document the missing binding. This does not automatically discard node content.
- #71 now preserves supplied source references when `learn` grounds existing
  concepts or creates valid relationships, through a linked learned-text evidence
  document. Replay retains each call's evidence without overwriting prior concept
  provenance. This fixes incomplete coverage of the new guarantee, not previously
  working structured provenance or an accepted migration exclusion.

## Agent coverage checklist

1. Establish scope and audience. Inventory the selected project graph and its
   existing canonical files. Do not mix a personal/global graph into a project's
   tracked sources. Private knowledge needs appropriately protected canonical
   JSONLs; never publish secrets or private material into a public repository.
2. Inventory all durable knowledge by identity and revision, not just titles or
   counts. Include every knowledge type, full content, tags/domains, meaningful
   metadata and lifecycle state, relationships and their direction/provenance,
   and structured source references. Rebuildable search indexes and caches are
   not additional durable knowledge.
3. Compare database knowledge with source records field by field. Preserve
   database-only knowledge in canonical JSONLs. Preserve independent source
   knowledge too: an incomplete local cache must not overwrite it. Do not resolve
   conflicting identities or edits solely by newest timestamp. Reconcile
   canonical JSONL records and source references explicitly before regenerating
   snapshots from the reconciled knowledge and code. The snapshot merge driver
   selects same-ID index conflicts by timestamp (ties keep ours) and can select
   one side of code-map collisions; it is not a lossless conflict archive.
4. Reconcile legacy JSON explicitly. Existing `knowledge.json` remains the
   selected runtime artifact where present; new `repo-memory` publications in
   #69 use `knowledge.jsonl`. If both contain records, the current importer
   intentionally selects JSON only. Compare and reconcile both for the intended
   migration target; list any deliberate exclusions in release notes. Do not
   treat the unselected file as empty. Verify retained-record coverage before
   retiring a superseded source artifact.
5. Verify relationships and evidence references against canonical sources.
   Preserve source identity and the relevant evidence revision/digest; a cache
   path, cache UUID, or same-ID node in another worktree cannot establish the
   historical evidence. Report unresolved bindings instead of substituting an
   unrelated node or relabeling provenance to match a rebuilt cache.
6. Validate JSONL parsing, record coverage, metadata equality, and relationship
   endpoints. Check source-to-source consistency after clone, checkout, and
   merge. If a supported source reader/rebuilder is available, verify equivalent
   knowledge reconstruction; if it cannot represent a type or field, report the
   precise limit separately and do not claim end-to-end reconstruction passed.
7. Commit the canonical project files with related code. Repeat the coverage and
   reference checks as captures, edits, and links occur, including relationship
   changes that create no new nodes. Completion requires a coverage report for
   retained knowledge, an explicit list of release-documented intentional
   exclusions, and an account of any remaining reader limitations.

## Recovery target and current runtime gaps

This PR does not implement the full recovery target. Existing tools can recover
some represented content, but the following source and consumer gaps remain:

- `index.json` cannot reconstruct complete node content or provenance because it
  stores summaries. `repo-memory` handles selected shareable evidence and creates
  quarantined candidates; it is not a full graph reconstruction path.
- Graph-transfer JSON/JSONL import can rebuild represented node/edge data, but
  filters `extra` to lifecycle fields on both export and import, omitting
  `extra.source_refs`. It does not establish complete canonical coverage or
  repair saved source bindings. Unsupported or omitted knowledge remains unavailable.
- #71 resolves saved SQLite paths and verifies cache UUIDs. It has no canonical
  JSONL source discovery/rebinding path; a rebuilt cache gets a new UUID and does
  not automatically restore traversal of historical bindings. Complete surviving
  sources therefore do not yet establish end-to-end source-reference recovery.

These are concrete runtime recovery gaps, independently of whether cleanup was
attempted. They require separately scoped source/rebuild/reference work; do not
claim that an agent prompt or best-effort cleanup implements that guarantee.

## Current tooling limits and ongoing defects

- `.kin/index.json` contains selected summaries, not complete knowledge.
- `kin repo-memory publish` transports explicitly selected active shareable
  concepts, decisions, and questions and selected-peer relationships. It omits
  other types and metadata, including `extra.source_refs`; its quarantined import
  is not a complete database rebuild. Do not use it alone as proof of migration.
- #69's deliberate JSON precedence is an accepted compatibility limit, with the
  excluded-import population and reconciliation steps documented above. An
  optional coexistence diagnostic is an enhancement, not a required repair.
- The relationship-only `learn` coverage gap is fixed in #71. Source records
  live on learned-text evidence documents linked to relationship endpoints;
  canonical serialization/recovery limitations remain independently of that fix.
- #71's resolver inspects saved SQLite locators read-only; it does not resolve
  canonical JSONL source bindings. `database_missing` after deletion is expected
  cache unavailability, not a requirement to rescue that cache or proof that the
  derived claim is false.

The agent must expose these limits and maintain canonical knowledge without
claiming guarantees the current serializer, importer, or resolver does not
provide. Runtime changes to close those gaps belong in separately scoped work.
