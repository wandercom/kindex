# Reading Kinbase from Kindex

Install the optional verifier and sync a repository into your Kindex graph:

```bash
pip install 'kindex[kinbase]'
kin kinbase sync --repo /path/to/repo --mode raw --json
kin search "the decision" --top-k 1
```

`--data-dir`, `--config`, `--profile`, and `--project-path` work like other Kindex
commands. The matching MCP tool is `kinbase_sync(repo, mode="auto",
binary="kinbase")`; Python callers can use
`kindex.kinbase.sync_kinbase(store, repo, mode="raw")`.

## Two different reads

- **Raw** walks `.kin/events/<2 hex>/<2 hex>/<60 hex>.json`. It verifies the
  RFC 8785 canonical content address and Ed25519 domain-separated signature,
  including `signer` in the signed payload. Facts use `fact-event`; questions
  use `unknown-event`. Raw rows are labelled **signed evidence; governance not
  evaluated**. Raw mode does not resolve supersession, authority membership,
  temporal decay, or path governance.
- **Reduced** invokes `kinbase explain KEY --repo PATH --decision
  'Kindex read-only synchronization' --json` for every key in the verified
  local event inventory. It retains the exact-key reduction receipt, including
  `as_of`, reducer version, authority cursor, conflict state, and unknowns.
  Coverage is **local event keys**, not the complete Company corpus. Explain
  can refresh Kinbase's authority cache. Kindex does not invoke `project`,
  whose shipping implementation can submit authority questions.
- **Auto** chooses reduced when the requested binary is available and raw
  otherwise. A failed reduced invocation is an error; it never silently
  substitutes raw evidence. Use `--binary /absolute/path/to/kinbase` when
  Kinbase is not on PATH.

Both modes require the verifier extra because reduced key enumeration starts
from verified local documents. Neither installs imported facts as Kindex policy
or admits them to `trusted_only` recall. Authenticating source bytes is not a
Kindex verification or a policy for refreshing an authority snapshot. Imported
operational atom kinds are represented as concepts, with their original kind
preserved in metadata.

## Standing and questions

Kindex stores `standing` as a native column, defaulting existing nodes to
`unruled`. Its precedence is authoritative > ratified > enforced > exemplary >
prevalent > present > unruled. Search applies that order before candidate limits
and before hybrid scoring. A ratified relevant fact therefore outranks any number
of present observations. Legacy nodes retain their existing relative order.

Import always caps standing by provenance: human reaches authoritative;
transcript and human_review reach prevalent; agents, bots, and unknown provenance
reach present. Missing standing remains unruled. The original claim, provenance,
logical key, anchors, governance paths, evidence references, validity, signer,
and source identity remain in `extra.kinbase`. Reduced metadata falls back to a
raw document only when its event ID identifies exactly one verified local event.

Unknowns are question nodes linked to facts sharing the same repository and
logical key. Search and every context tier display their question, owner, and
status alongside the selected fact even with `--top-k 1`.
`UNKNOWN_OWNER_UNRESOLVED` remains an actionable ownership question.

## Refresh and export

The sync report counts imported, unchanged, quarantined, and deactivated nodes.
Corrupt JSON, bad signatures, wrong addresses, unsupported schemas, and symlinked
events are reported without repair. Missing or unreadable source directories and
failed explain responses abort before updating Kindex. A complete readable
inventory deactivates previously imported documents that disappeared or became
invalid. Reduced omissions are labelled `not-in-reduced-view`, not asserted to be
superseded. Switching modes refreshes the same source rather than accumulating
raw and reduced copies. All updates are committed in one SQLite transaction.

Event identities include the canonical repository path and immutable document
identity. Multiple revisions of a logical key and identical documents in different
repositories remain distinct. Rejected, proposed, and superseded facts stay
inspectable via `--include-archived`; validity windows fence ordinary retrieval.

JSON/JSONL graph export and import preserve standing and the Kinbase metadata
that explains it. The unsigned `.kin/knowledge.json` publication path refuses
Kinbase imports because that transport cannot preserve their signed-source
semantics. Store credentials are redacted as usual: the verification receipt
applies to the original external bytes, not to a possibly redacted cached copy.
Kindex never writes `.kin/events/`; new assertions belong in Kindex.
