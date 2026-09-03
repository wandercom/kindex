# Kindex Human Guide

This guide is for humans installing and operating Kindex. The AI-facing
operating contract lives in [mcp-agent-guide.md](mcp-agent-guide.md).

## Public Docs

- Canonical public website: <https://kindex.tools/>
- GitHub Pages docs build from this repo: <https://jmcentire.github.io/kindex/>
- Source and releases: <https://github.com/jmcentire/kindex>
- PyPI package: <https://pypi.org/project/kindex/>

The canonical `kindex.tools` site is served by the companion `Kindex-Tools`
Fly static app. This repo's Pages workflow publishes the `docs/` directory,
including `docs/.well-known/`, to the GitHub Pages docs URL so the repo-local
documentation and MCP server-card metadata stay in sync with the release.

## Install

Pick one installer:

```bash
pip install 'kindex[mcp]'
uv tool install 'kindex[mcp]'
uvx --from 'kindex[mcp]' kin-mcp --help
git clone https://github.com/jmcentire/kindex && cd kindex && make install
```

Then initialize the graph:

```bash
kin init
```

Use extras when you need more than the base CLI:

```bash
pip install 'kindex[mcp,llm,reminders]'
pip install 'kindex[all]'
```

## Connect Your Agent

Install both the MCP server and the instruction file for each client you use:

```bash
# Claude Code
claude mcp add --scope user --transport stdio kindex -- kin-mcp
kin setup-claude-md --install
kin setup-hooks

# Codex
kin setup-codex-mcp
kin setup-codex-hooks
kin setup-agents-md --install --global

# Gemini CLI
kin setup-gemini-mcp
kin setup-gemini-md --install

# Google Antigravity
kin setup-antigravity-mcp
kin setup-antigravity-hooks
kin setup-antigravity-md --install

# OpenCode
kin setup-opencode-mcp
kin setup-agents-md --install --global

# Cursor
kin setup-cursor-mcp
kin setup-cursor-rules --install
```

After setup, start a fresh agent session and confirm the `kindex` MCP tools are
visible. The instruction files are what make agents use Kindex proactively:
start/resume a tag, search before adding, capture durable decisions, and end
the tag with a summary.

## Review Automatic Capture Before Promotion

Automatic pre-compact extraction is quarantined. A successful hook stages
candidate records and creates no durable knowledge nodes or edges. Review them
through the CLI or the matching MCP tools (`candidate_list`,
`candidate_show`, `candidate_accept`, `candidate_reject`, `candidate_prune`,
and `candidate_erase`):

```bash
kin candidate list --status pending
kin candidate show <candidate-id>
kin candidate accept <candidate-id> \
  --review-token <token> --by "reviewer" --method "manual-review"
kin candidate reject <candidate-id> --by "reviewer" --code not_relevant
kin candidate prune
kin candidate erase <candidate-id>
```

`candidate show` returns the exact untrusted proposal and a deterministic
freshness token. Accept recomputes that token inside its write transaction and
refuses stale, expired, contradictory, or same-title state. The token is not
authentication or proof of reviewer identity; `--by` and `--method` are
asserted audit text within the local trust boundary. Accepted, rejected, and
expired candidates have their raw payload removed, while explicit erase also
removes the minimal receipt. Candidate source material is stored only as a
digest.

Candidate retention defaults to seven days and must be positive:

```yaml
capture:
  candidate_ttl_days: 7
```

## Use Trusted Projections Deliberately

Ordinary search and context keep their existing recall behavior, including
legacy unverified nodes. Add `--trusted-only` when the result must be an
admission-controlled projection:

```bash
kin verify <node-id> --by "reviewer" --method "source-check" \
  --valid-at 2026-08-18T12:00:00Z
kin search "deployment constraints" --trusted-only
kin context --topic deployment --trusted-only
kin invalidate <node-id> --by "reviewer" --code superseded \
  --at 2026-09-01T00:00:00Z
```

Time arguments must be timezone-aware RFC 3339 values. Trusted projections
admit only active, explicitly verified, currently valid nodes that are not in a
current explicit contradiction. They report omitted knowledge by machine
reason; Kindex does not infer semantic contradictions or select a winning
claim.

`kin tag resume` and MCP `tag_resume` use trusted admission by default. Their
legacy `tokens` argument is an exact UTF-8 byte budget, not an estimated model
token count. A direct library caller can supply the target provider's exact
counter when a provider-token guarantee is required. The complete resume block,
including warnings and omission notices, remains inside the selected budget;
a non-positive budget emits an empty block.

As of v0.36.0, resume also changes a paused tag back to active for the exact
project. Completed tags remain completed. After 60 days, completed, unlinked
session tags can move from the fast graph (the live SQLite database queried by
normal commands) to the slow archive (separate SQLite files searched explicitly
with `kin archive search`). Active, paused, knowledge-linked, and newer tags do
not move. Restore one with `kin archive restore`. `kin archive list` warns if an
interrupted move left an ID in both stores; Kindex preserves both copies because
ID equality alone cannot prove which one is stale.

> [!WARNING]
> Before upgrading, stop every Kindex daemon, MCP server, and older CLI
> process. A v0.35.x process does not reject schema v12 and can write
> non-canonical session paths after migration.

Dream is Kindex's background knowledge-consolidation pass: it finds related
nodes, safely merges strong duplicates, and stages weaker links for review.
Before schema migration, the first v0.36.0 process uses SQLite's backup API to
create a recovery snapshot under
`$XDG_STATE_HOME/kindex/snapshots/<db>-<hash>/migrations/` (defaulting below
`~/.local/state/kindex/snapshots/`). The owner-private snapshot is integrity- and
source-version-checked; partial failures are removed. Concurrent v0.36+
processes serialize migration through a dedicated rollback-journal SQLite lock
and recheck the schema after waiting. These
migration recovery points are not subject to the ten-file rotation used for
automated-merge snapshots. Migration refuses to run if a safety step fails.
`kin status` exposes the recovery path, and normal stores also record it in
`kin changelog`. The path names the latest validated migration attempt; earlier
attempts remain in the same `migrations/` directory. Schema v12 migrates
atomically, preserves rows, and stores suggestion endpoint identity explicitly
as `title` or `node_id`; ambiguous title acceptance is refused.

### Roll Back a Schema Migration

Do not open the migrated database with v0.35.x: that version has no
forward-schema guard. Roll back in this order:

1. While v0.36 is installed, run `kin status` and record the `Recovery` path.
2. Stop every Kindex process again.
3. Move the live database's `-wal` and `-shm` sidecars aside.
4. Copy the recorded migration snapshot over the live database.
5. Only then install or run v0.35.x and verify the graph.

This is distinct from [recovering a bad automated merge](#recover-from-a-bad-automated-merge),
which restores a rotating merge snapshot without changing package versions.
Graph-health metrics now describe semantic topology; use the explicit stored
counts when auditing retained session or legacy Dream rows. Machine-readable
stats mark the new contract as `metrics_schema: 2`. List sessions paused by
migration with `kin tag list --status paused`; their reason is
`duplicate-active-session-migration-v12`.

Dream's domain-review queue is capped by
`reminders.dream_max_domain_link_suggestions` (default 50). The option stays in
the reminder section because scheduled and Stop-hook Dream runs share that
maintenance configuration.

## Bind Claims to What They Describe

A claim about code (or any external referent) can carry a content digest of the
thing it describes, plus two clocks: `asserted_at` (when you made the claim)
and `true_of` (when the referent was observed in that state). The moment the
referent moves, the claim is *verifiably* stale instead of heuristically old:

```bash
# Bind at capture (the file is hashed now; binding implies direct creation)
kin add "auth middleware validates JWT audience" --referent src/auth/mw.py

# URL or repo-state claims carry an explicit digest
kin add "The v2 API paginates by cursor" \
  --referent https://api.example.com/docs --referent-digest <sha256> \
  --referent-scope url

# Sweep: re-hash every bound claim
kin stale

# A stale/missing referent demotes the node from --trusted-only recall and
# marks it [stale-referent] in ordinary search/context. Content is never
# deleted or rewritten. After re-checking the claim against the new state:
kin stale --rebind <node-id>
```

Rebinding moves `true_of` to now and records the new digest; `asserted_at`
never changes — the divergence between the two clocks stays honest. If the
file returns to its recorded state, the next sweep clears the demotion by
itself.

## Calibrate the Grounding Floor

Vector search returns the nearest neighbours for any query, however unrelated.
Without a floor, a question your graph knows nothing about still pulls real
nodes into an agent's context — and the graph has no way to say "I don't know."

Calibrate once per embedding model, and again after any large backfill:

```bash
# Measure the null-query similarity distribution against your own corpus
kin embed calibrate

# Read the current record without recalibrating
kin embed calibrate --show
```

The floor is stored as an immutable, versioned record keyed by
`provider:model`, carrying the corpus it was measured against. That is what
makes staleness detectable: a floor calibrated when 2% of your graph was
embedded describes a distribution that no longer exists after a backfill, and
Kindex reports `uncalibrated` rather than quietly trusting it. Your config
holds the policy only — never the number:

```yaml
grounding:
  enabled: true
  enforce: false          # shadow mode
  floor_percentile: 95.0
  weak_margin: 1.15
  recalibrate_coverage_delta: 0.25
```

**Leave `enforce: false` until you have watched it.** Today's failure mode is
loud and self-correcting — you see an irrelevant result and ignore it.
Enforcing creates the quiet one: an empty context and an agent proceeding
without knowledge that was actually in the graph. Run shadow mode, read the
`[grounding: UNGROUNDED …]` notes on real queries, then decide.

If a refusal ever looks wrong, the verdict records the near-miss scores that
produced it, so you can compare them against the floor instead of guessing.

## Tune Retrieval Reach

Graph expansion honours `--hops` with per-hop decay and a mandatory beam:

```yaml
ranking:
  hop_decay: 0.5      # a 2-hop neighbour cannot outrank a 1-hop one
  graph_beam: 200     # required: real graphs have 800+ fan-out hubs
```

The beam ordering is total and stable, so the same query returns the same
neighbourhood — adding an edge elsewhere in a hub's neighbourhood cannot
silently change a result.

## Choose an Extraction Engine

Automatic extraction is an input, not an authority: engine output goes to the
`capture_candidates` quarantine for review and never writes nodes directly.

```bash
kin extract engines                                  # what is available
kin extract eval --engines keyword,llm --limit 200   # score them on YOUR corpus
```

The gate is two-part — grounding precision as a floor (don't invent) and title
recall as the discriminator (find what a curator would record). Either metric
alone is gameable, so both must clear.

An optional LLM-free deterministic engine installs separately:

```bash
pip install 'kindex[talon]'   # ~2.5 GB; excluded from kindex[all] by design
```

Measure before enabling it. An engine that cannot beat keyword extraction on
your own corpus has not earned the install size, whatever its own benchmark
reports.

## Recover From a Bad Automated Merge

Before any automated destructive merge (`graph_merge`, dream-cycle
auto-merges), Kindex snapshots the live SQLite database with the SQLite backup
API to `$XDG_STATE_HOME/kindex/snapshots/<db>-<hash>/` (default
`~/.local/state/kindex/snapshots/`), keeping the ten newest per database. The
merge is fail-closed: if the snapshot cannot be written, the merge is refused
or skipped rather than run unprotected. Each snapshot is also recorded as a
`db_snapshot` entry in `kin changelog` with its path.

To restore after a false merge:

```bash
# 1. Stop anything holding the DB (daemon, MCP server, open CLI sessions)
# 2. Find the snapshot taken just before the bad merge
ls ~/.local/state/kindex/snapshots/*/
# 3. Move stale SQLite sidecars aside before replacing the live DB
mv ~/.kindex/kindex.db-wal ~/.kindex/kindex.db-wal.pre-restore 2>/dev/null || true
mv ~/.kindex/kindex.db-shm ~/.kindex/kindex.db-shm.pre-restore 2>/dev/null || true
# 4. Copy the snapshot over the live DB (default below), then restart
cp ~/.local/state/kindex/snapshots/<db-dir>/<stamp>-graph-merge.sqlite3 \
   ~/.kindex/kindex.db
```

Restoring rolls the whole graph back to the snapshot instant; re-apply any
wanted changes made after it. Reversible per-merge receipts (restore just the
merged node) are the R3 line item in
`docs/prd-lineage-grounding-2026-08.md`.

## Use Reminders

Reminders are stored in Kindex and fired by a checker. Creating a reminder does
not itself wake a running agent.

```bash
# Install periodic checks once (maintenance job + dedicated reminder checker)
kin setup-cron

# Create a normal reminder
kin remind create "Check deploy" --at "in 30 minutes" --priority high

# Run due reminders manually
kin remind check

# Sweep every profile and registered project graph
kin remind check --all-profiles
```

The dedicated reminder checker runs `kin remind check --all-profiles` on its
own schedule, so due reminders fire even when a maintenance run is slow or
stalled. Within `kin cron` itself, reminders are checked before any ingest,
LLM, or embedding work.

Wake reminders can start headless Codex or OpenCode follow-up turns when the
checker runs:

```bash
kin remind create "Continue rollout check" --at "in 10 minutes" \
  --wake codex --session last --cwd "$PWD" \
  --instructions "Check the rollout and fix any new failures."

kin remind create "Continue OpenCode build" --at "in 10 minutes" \
  --wake opencode --session last --cwd "$PWD" --wake-agent build \
  --instructions "Continue the build triage."
```

Boundary: these wakeups run `codex exec` or `opencode run` from the
daemon/cron context. They do not interrupt an idle terminal UI unless that host
adds a same-thread wake API.

## Keep Project Context Portable

If a repo tracks `.kin/`, treat it as shipped project state:

- Commit `.kin/config` when project policy, domains, or inheritance matter.
- Regenerate `.kin/index.json` with `kin index`; do not hand-edit conflicts.
- Regenerate `.kin/code-map.json` with `kin export code-map`.
- Keep private runtime data in `~/.kindex` or ignored `.kin/local`.

Run these before committing code that changes project structure:

```bash
kin ingest code --directory .
kin index
kin export code-map --directory . --project-name kindex --output .kin/code-map.json
```

## Release Surface Checklist

Before calling a release done, verify each public surface:

```bash
python3 -m pytest
mcp-publisher validate server.json
git describe --tags --exact-match HEAD
gh release view vX.Y.Z --repo jmcentire/kindex
python3 -m pip index versions kindex
curl -fsSL https://kindex.tools/ | grep 'vX.Y.Z'
curl -fsSL https://kindex.tools/.well-known/mcp/server-card.json | grep 'X.Y.Z'
```

If `mcp-publisher publish server.json` returns an expired-token error, refresh
the local registry login before publishing:

```bash
mcp-publisher login github
mcp-publisher publish server.json
```
