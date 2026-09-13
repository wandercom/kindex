# Kindex

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![v0.36.2](https://img.shields.io/badge/version-0.36.2-purple.svg)](https://github.com/wandercom/kindex/releases)
[![PyPI](https://img.shields.io/pypi/v/kindex.svg)](https://pypi.org/project/kindex/)
[![MCP Market](https://img.shields.io/badge/MCP%20Market-kindex-blue.svg)](https://mcpmarket.com/server/kindex)
[![Tests](https://github.com/wandercom/kindex/actions/workflows/ci.yml/badge.svg)](https://github.com/wandercom/kindex/actions/workflows/ci.yml)
[![MCP Plugin](https://img.shields.io/badge/MCP-Plugin-orange.svg)](#install-as-agent-mcp-plugin)

**Every agent is smart inside its own silo. Kindex lets them work together.**

Kindex does one thing. It knows what you know.

Claude Code, Codex, Gemini CLI, Google Antigravity, OpenCode, Cursor, and other MCP-capable agents each remember for themselves, and none of them can read the others. Kindex is the local knowledge graph they all read and write: continuity across sessions and restarts, handoffs between vendors, shared decisions and constraints, and live coordination while several of them work at once. Available as a **free MCP plugin** or standalone CLI.

> **Memory plugins capture what happened. Kindex captures what it means and how it connects.** Most memory tools are session archives with search. Kindex is a weighted knowledge graph with typed nodes, provenance, and decay, that surfaces constraints and decisions and manages exactly how much context to inject based on your available token budget.

Kindex is the index for an individual and a codebase: your own graph, and the repository's git-tracked `.kin/`. [Kinbase](https://kinbase.tools) is the company product above it, in development: engineering direction, architecture, standards, ownership, and history, held by named authorities and composed into every coding session. Same engine, same protocol, physically separate stores.

Docs: [kindex.tools](https://kindex.tools/) is the canonical public site, served by the companion Fly static app. This repo also publishes its `docs/` directory at [wandercom.github.io/kindex](https://wandercom.github.io/kindex/). Human setup lives in [docs/human-guide.md](docs/human-guide.md); agent operating rules live in [docs/mcp-agent-guide.md](docs/mcp-agent-guide.md).

## Install

Development preview: [Claude function hooks and signet-eval coexistence](docs/claude-function-hooks.md)
adds repo-local durable tasks, secret minimization at Kindex-owned boundaries,
and separately selectable modern/legacy adapters. The default remains compatible
legacy hooks. Function hooks are qualified on Claude Code 2.1.263; this is not a
promise that all Claude logs can be redacted or a published 1.0 release.

Pick whichever installer you already use. They all install the same `kin` and `kin-mcp` binaries.

```bash
# pip
pip install 'kindex[mcp]'

# uv (single binary, no virtualenv)
uv tool install 'kindex[mcp]'

# uvx (no install — runs from cache, useful for one-off MCP invocation)
uvx --from 'kindex[mcp]' kin-mcp --help

# from source
git clone https://github.com/wandercom/kindex && cd kindex && make install
```

### Upgrading to v0.36.0

> [!WARNING]
> Before upgrading, stop every Kindex daemon, MCP server, and older CLI
> process. A v0.35.x process does not reject schema v12 and can write
> non-canonical session paths after migration.

Dream is Kindex's background knowledge-consolidation pass: it finds related
nodes, safely merges strong duplicates, and stages weaker links for review.
The first v0.36.0 process to open an older graph creates a transaction-safe
pre-migration snapshot under
`$XDG_STATE_HOME/kindex/snapshots/<db>-<hash>/migrations/` (defaulting below
`~/.local/state/kindex/snapshots/`) before atomically migrating schema v11 to
v12. The owner-private snapshot passes SQLite integrity and source-version
checks, and a partial file is deleted if creation or validation fails.
Concurrent v0.36+ processes serialize this step through a dedicated
rollback-journal SQLite lock and recheck the schema after waiting. Migration
recovery points are retained outside the rotating ten-file
automated-merge snapshot pool. Older duplicate active session tags become paused
history, one active tag per normalized project and name is enforced, and Dream
suggestions carry an explicit title-or-node-ID identity contract and title
ambiguity is refused. No node or edge rows are deleted; legacy
domain-co-membership edges remain available as stored history but no longer
participate in semantic traversal or health metrics. `kin status` exposes the
durably recorded recovery path; normal stores also record it in `kin changelog`.

#### Rolling back the schema migration

Do not open the migrated database with v0.35.x: that version has no
forward-schema guard and can write old, non-canonical session identity. Instead:

1. While v0.36 is still installed, run `kin status` and record the `Recovery`
   path. It names the latest validated migration attempt; older attempts remain
   in the same `migrations/` directory.
2. Stop every Kindex process again.
3. Move the live database's `-wal` and `-shm` sidecars aside.
4. Copy the recorded snapshot over the live database.
5. Only then install or run v0.35.x and verify the graph.

This differs from [recovering a bad automated merge](docs/human-guide.md#recover-from-a-bad-automated-merge),
which does not change package versions. Graph-health consumers must also
recalibrate: existing node/edge/orphan/component outputs now describe the
semantic graph, while explicit stored counts expose retained lifecycle and
legacy rows. Machine-readable stats identify this contract as `metrics_schema: 2`.

Then initialize the graph:

```bash
kin init
```

Extras — combine in one install (`'kindex[mcp,llm,reminders]'`) or use `'kindex[all]'`:

| Extra | Adds |
|-------|------|
| `mcp` | `kin-mcp` MCP server (for Claude Code, Codex, Gemini, Antigravity, OpenCode, Cursor, etc.) |
| `llm` | Anthropic-powered extraction (`kin learn`, `kin ask`) |
| `vectors` | sqlite-vec for semantic similarity search |
| `reminders` | Natural-language time parsing for `kin remind` |
| `all` | Everything above |

> Homebrew and apt packages aren't published yet. Use `pip`, `uv tool`, `uvx`, or source until they are.

## Install as Agent MCP Plugin

Each agent reads MCP servers from a different config file. The `kin setup-*-mcp` commands write the right shape into the right path; the manual snippet is shown alongside in case you'd rather edit the file yourself.

### Claude Code

```bash
claude mcp add --scope user --transport stdio kindex -- kin-mcp
kin init
```

Or add `.mcp.json` to any repo for project-scope access:
```json
{ "mcpServers": { "kindex": { "command": "kin-mcp" } } }
```

The MCP server exposes 50+ native tools to supported clients: `search`, `add`, `context`, `show`, `ask`, `learn`, `link`, `edit`, `supersede`, `list_nodes`, `status`, `suggest`, `candidate_*`, `verify`, `invalidate`, `stale_check`, `graph_stats`, `graph_merge`, `dream`, `changelog`, `ingest`, `tag_start`, `tag_update`, `tag_resume`, `task_claim`, `coord_*`, `lock_acquire`, `lock_release`, `remind_*`, `mode_*`, and more.

For coding agents, install both the MCP server and the instruction file. The
instruction file tells the model how to use kindex: start a session tag, read
tracked `.kin/config`, check project policy, search before adding, capture
durable decisions, and end the tag with a summary.

### Codex

```bash
kin setup-codex-mcp
kin setup-codex-hooks
kin setup-agents-md --install --global
kin ingest codex-sessions   # optional: backfill saved Codex sessions
```

`setup-codex-hooks` installs a **SessionStart** hook (alongside the prompt/tool attention hooks), so Codex begins each session with the same auto-primed context and "use kindex" / `.kin` directive as Claude Code.

Or hand-edit `~/.codex/config.toml`:
```toml
[mcp_servers.kindex]
command = "kin-mcp"
```

### Gemini CLI

```bash
kin setup-gemini-mcp
kin setup-gemini-md --install
```

Or hand-edit `~/.gemini/settings.json`:
```json
{ "mcpServers": { "kindex": { "command": "kin-mcp", "args": [] } } }
```

### Google Antigravity

```bash
kin setup-antigravity-mcp
kin setup-antigravity-hooks
kin setup-antigravity-md --install
```

`setup-antigravity-mcp` writes the standalone MCP config shape used by
Antigravity's editor/shared config and CLI config. `setup-antigravity-hooks`
installs PreInvocation priming/prompt checks, PreToolUse advisory attention and
permission gating for Kindex config writes, and Stop-time reinforcement enqueue.

Or hand-edit `~/.gemini/config/mcp_config.json` and
`~/.gemini/antigravity-cli/mcp_config.json`:
```json
{ "mcpServers": { "kindex": { "command": "kin-mcp", "args": [] } } }
```

### OpenCode

```bash
kin setup-opencode-mcp
kin setup-agents-md --install --global
```

Or hand-edit `~/.config/opencode/opencode.json`:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kindex": { "type": "local", "command": ["kin-mcp"], "enabled": true }
  }
}
```

OpenCode reads `AGENTS.md` natively, so install the MCP server and the shared `AGENTS.md` instructions together.
OpenCode also supports plugins, but Kindex currently uses MCP + instructions there rather than prompt-time attention injection.

### Cursor

```bash
kin setup-cursor-mcp
kin setup-cursor-rules --install   # writes ~/.cursor/rules/kindex.mdc
```

Or hand-edit `~/.cursor/mcp.json`:
```json
{ "mcpServers": { "kindex": { "type": "stdio", "command": "kin-mcp" } } }
```

Cursor integration is MCP + always-applied rules. Cursor rules provide prompt-level guidance, but Kindex does not currently install a Cursor prompt-submit hook because Cursor does not expose the same hook surface as Claude Code or Codex CLI.

## Why Kindex

### Context-aware by design
Five context tiers auto-select based on available tokens. When other plugins dump everything into context, Kindex gives you 200 tokens of executive summary or 4000 tokens of deep context — whatever fits. **Your plugin doesn't eat the context window.**

| Tier | Budget | Use Case |
|------|--------|----------|
| full | ~4000 tokens | Session start, deep work |
| abridged | ~1500 tokens | Mid-session reference |
| summarized | ~750 tokens | Quick orientation |
| executive | ~200 tokens | Post-compaction re-injection |
| index | ~100 tokens | Existence check only |

### Knowledge graph, not log file
Nodes have types, weights, domains, and audiences. Edges carry provenance and decay over time. The graph understands what matters — not just what was said.

### Operational guardrails
Constraints block deploys. Directives encode preferences. Watches flag attention items. Checkpoints run pre-flight. No other memory plugin has this.

### Cache-optimized LLM retrieval
Three-tier prompt architecture with Anthropic prompt caching. Stable knowledge (codebook) is cached at 10% cost. Query-relevant context is predicted via graph expansion and cached per-topic. Only the question pays full price. Transparent — `kin ask` just works better and cheaper.

### Team and org ready
`.kin` inheritance chains let a service repo inherit from a platform context, which inherits from an org voice. Private/team/org/public scoping with PII stripping on export. Enterprise-ready from day one.

## In Practice

A 162-file fantasy novel vault — characters, locations, magic systems, plot outlines — ingested in one pass. Cross-referenced by content mentions. Searched in milliseconds.

```
$ kin status
Nodes:     192
Edges:     11,802
Orphans:   3

$ time kin search "the Baker"
# Kindex: 10 results for "the Baker"

## [document] The Baker - Hessa's Profile and Message Broker System (w=0.70)
  → Thieves Guild, Five Marks, Thieves Guild Operations

## [person] Mia and The Baker (Hessa) -- Relationship (w=0.70)
  → Sebastian and Mia, Mia -- Motivations and Goals

0.142 total

$ kin graph stats
Nodes:      192
Edges:      11,802
Density:    0.3218
Components: 5
Avg degree: 122.94
```

192 nodes. 11,802 edges. 5 context tiers. Hybrid FTS5 + graph traversal in 142ms.

## Getting Agents to Actually Use It

Installing the MCP plugin gives the agent the tools. But agents won't use them proactively unless you tell them to. Kindex ships with recommended instruction blocks that turn passive tools into active habits. For the full agent playbook, see [docs/mcp-agent-guide.md](docs/mcp-agent-guide.md).

```bash
# Claude Code
kin setup-claude-md --install

# Codex (and OpenCode — both honor AGENTS.md)
kin setup-agents-md --install --global

# Gemini CLI
kin setup-gemini-md --install

# Google Antigravity
kin setup-antigravity-md --install

# Cursor — writes ~/.cursor/rules/kindex.mdc with alwaysApply: true
kin setup-cursor-rules --install
```

This adds session lifecycle rules (start/orient/during/segment/end), explicit capture triggers (discoveries, decisions, tasks, key files, notable outputs), and search-before-add discipline. The difference between "the agent has a knowledge graph" and "the agent actively maintains a knowledge graph" is this block.

For durable work, agents should use Kindex's persistent task and knowledge
surfaces rather than host-session-only task state. Use `task_add`, `task_list`,
and `task_done` for work that must survive the current conversation; search
before adding knowledge; prefer `edit` or `supersede` over duplicate nodes; and
treat tracked `.kin` files as shipped project state, not local cache.
If the host also exposes session-local task tools, use those only for temporary
planning; durable work belongs in Kindex.

The Claude SessionStart hook (`kin setup-hooks`) and Codex hooks (`kin setup-codex-hooks`) reinforce these directives at the start of supported sessions with a "Session directives" block that reminds the agent to use kindex MCP tools throughout the session.

### What gets captured

With the directives active, the agent will:
- **Search** the graph before starting work and before adding nodes
- **Add** discoveries, decisions, key files, notable outputs, and new terms as they emerge
- **Link** related concepts when connections are found
- **Learn** from long files and outputs via bulk extraction
- **Tag** sessions to track work context across conversations
- **Remind** with actions for deferred tasks (shell commands or headless agent wakeups)

### Actionable Reminders

Reminders can carry shell commands, natural-language instructions, or a headless
agent wakeup. When due, the daemon executes them automatically — simple commands
run directly, complex Claude tasks launch `claude -p`, Codex wakeups run
`codex exec`, and OpenCode wakeups run `opencode run`. Wakeups can resume a
known host session id, or `last` for the latest session when the host supports
that. This starts/resumes a headless turn from Kindex's daemon/cron context; it
does not interrupt an idle TUI unless the host itself exposes a same-thread
automation/server wake path. A Stop hook guard can block Claude from exiting when
actionable reminders are pending, but it is opt-in because Claude displays
visible "Blocked by hook" output when a Stop hook blocks.

Important boundary: `remind_create` records the reminder. Something must later
run `kin remind check`, `kin remind exec`, `kin cron`, or an installed
`kin setup-cron` schedule for due reminders to fire. Wake reminders are a
Kindex capability for Codex and OpenCode because those clients expose
headless commands; they are not a reentrant scheduler for an already-idle
interactive session.

Hook-time reminder injection uses a scoped reminder board. When a client supplies a chat/session id (`conversation_id`, `chat_id`, `session_id`, `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`, `OPENCODE_SESSION_ID`, `CURSOR_SESSION_ID`, etc.), Kindex injects only reminders scoped to that id plus reminders explicitly marked `--scope global`. Legacy unscoped reminders still work for manual `kin prompt-check`, daemon checks, and notifications, but they are not injected into an identified chat by default.

```bash
# Kill a cloud instance in 1 hour (but download results first)
kin remind create "Kill vast.ai instance" --at "in 1 hour" \
  --action "vastai destroy instance 12345" \
  --instructions "Download results from /workspace/ before killing"

# Wake a headless Codex or OpenCode turn when due
kin remind create "Continue rollout check" --at "in 10 minutes" \
  --wake codex --session last --cwd "$PWD" \
  --instructions "Check the rollout and fix any new failures."
kin remind create "Continue OpenCode build" --at "in 10 minutes" \
  --wake opencode --session last --cwd "$PWD" --wake-agent build \
  --instructions "Continue the build triage."

# Chat-scoped or intentionally global hook-visible reminders
kin remind create "Deploy checklist" --at "tomorrow 9am" \
  --conversation-id "$CLAUDE_SESSION_ID" --attention-trigger deploy
kin remind create "Monthly billing review" --at "next Monday 9am" --scope global

# Manual trigger
kin remind exec --reminder-id <id>
```

### Dream — Knowledge Consolidation

Kindex can run fuzzy deduplication, auto-apply high-confidence pending suggestions, and stage bounded domain-link proposals for review. Resolved fuzzy matches are not recreated. The pending domain-review queue is capped per graph (`reminders.dream_max_domain_link_suggestions`, 50 by default), proposals are round-robin across domains, and rejected pairs stay rejected. The setting lives under `reminders` because scheduled and Stop-hook Dream runs use the reminder/maintenance configuration. Shared domains are never materialized directly as semantic edges. Like memory consolidation during sleep — replay important paths and prune noise without turning tags into topology.

```bash
# See exact merge and domain-link proposals (no changes)
kin dream --dry-run

# Run full consolidation
kin dream

# Fast path: dedup + suggestions only
kin dream --lightweight

# Include LLM-powered cluster summarisation
kin dream --deep

# Fork and return immediately; repeated detached starts are throttled
kin dream --detach --lightweight
```

Default triggers are manual CLI, periodic cron (step 11 of `kin cron`), and a throttled detached Stop hook. File locking prevents concurrent cycles, and `reminders.dream_min_interval` prevents hooks or cron from relaunching dream repeatedly after a recent start. Set `reminders.dream_on_stop_enabled: false` to disable Stop-time detached dream while leaving manual and cron dream available.

### Conversation Modes

Modes are reusable conversation-priming artifacts that induce a processing mode in an AI session. Based on research showing that induced understanding outperforms direct instruction by 5.4x, and that 15 tokens of mode-setting capture 98.8% of achievable priming benefit.

Five built-in modes: `collaborate`, `code`, `create`, `research`, `chat`. Create custom modes from any session and export them for team sharing (PII-free).

```bash
# Seed default modes
kin mode seed

# Activate a mode — outputs the priming artifact
kin mode activate collaborate

# Create a custom mode
kin mode create debug-session \
  --primer "We're hunting a bug. Precision over speed..." \
  --boundary "Show your reasoning chain. Name assumptions." \
  --permissions "Speculate about root causes freely."

# Export for team sharing (PII-stripped)
kin mode export collaborate > collaborate.json

# Import a teammate's mode
kin mode import their-mode.json
```

Modes are not instructions — they're state inductions. A primer establishes *how to think*, a boundary defines *what quality means*, and permissions state *what's allowed*. The AI shifts processing mode rather than following a checklist.

## Quick Start

```bash
# Add knowledge (with optional tags)
kin add "Stigmergy is coordination through environmental traces" --tags biology,coordination

# Search with hybrid FTS5 + graph traversal
kin search stigmergy
kin search coordination --tags biology   # filter results by tag

# Ask questions (with automatic classification)
kin ask "How does weight decay work?"

# Get context for AI injection
kin context --topic stigmergy --level full

# List and filter by tags
kin list --tags python,ml              # nodes tagged with both
kin list --type concept --tags ai      # combine type and tag filters

# Track operational rules
kin add "Never break the API contract" --type constraint --trigger pre-deploy --action block

# Check status before deploy
kin status --trigger pre-deploy

# Ingest from all sources
kin ingest all

# Session tags — named work context handles
kin tag start auth-refactor --focus "OAuth2 flow" --remaining "tokens,tests"
kin tag segment --focus "Token storage" --summary "Flow design done"
kin tag pause auth-refactor --summary "Waiting for review"
kin tag resume auth-refactor   # reactivate and render admission-controlled context
kin tag end --summary "All done"

# After 60 days, `kin cron` step 8 (or `kin archive run`) moves completed,
# unlinked session tags from the fast graph (the live database) into the slow
# archive (separate SQLite files searched explicitly). Active, paused, and
# artifact-linked sessions stay in the fast graph.
kin archive run
kin archive list  # warns if an interrupted move left an ID in both stores
kin archive search auth-refactor
kin archive restore <session-node-id>

# Reminders — never forget, never nag
kin remind create "standup" --at "every weekday at 9am" --priority high
kin remind create "reply to Kevin" --at "in 30 minutes" --priority urgent
kin remind list
kin remind snooze --reminder-id <id> --duration 1h
kin remind done --reminder-id <id>
```

## Trust, Capture Review, and Bounded Resume

Kindex keeps automatic extraction separate from durable knowledge. The
pre-compact hook stages review candidates; it does not create graph nodes or
edges. Inspect and resolve candidates explicitly:

```bash
kin candidate list --status pending
kin candidate show <candidate-id>                 # exact untrusted payload + freshness token
kin candidate accept <candidate-id> \
  --review-token <token> --by "reviewer" --method "manual-review"
kin candidate reject <candidate-id> --by "reviewer" --code not_relevant
kin candidate prune                               # expire candidates whose TTL elapsed
kin candidate erase <candidate-id>                # remove any candidate or receipt
```

The review token detects changes between show and accept. It is not
authentication, authorization, or proof of reviewer identity. Candidate source
text is retained only as a SHA-256 digest, and accepted, rejected, or expired
candidates are reduced to minimal receipts. Human candidate output is visibly
delimited because its content is untrusted.

Verification is also explicit and records asserted local audit text:

```bash
kin verify <node-id> --by "reviewer" --method "source-check" \
  --valid-at 2026-08-18T12:00:00Z
kin invalidate <node-id> --by "reviewer" --code superseded \
  --at 2026-09-01T00:00:00Z

# Ordinary search/context remain legacy-compatible recall.
kin search deployment
kin search deployment --trusted-only
kin context --topic deployment --trusted-only
```

Trusted-only search/context and `kin tag resume` admit only active, explicitly
verified, currently valid, non-contradicted knowledge. Resume output includes an
authority warning and machine-reason omission counts. Its legacy `--tokens`
option is an exact UTF-8 byte budget by default; direct library callers can pass
a provider's exact token counter when they require a provider-token guarantee.
Non-positive resume budgets return no output.

### Referent Binding and Verifiable Staleness

A node can bind the external thing its claim describes — a file, URL, or repo
state — with a content digest and two clocks: `asserted_at` (when the claim was
made) and `true_of` (when the referent was observed in the digested state).
Staleness then stops being a heuristic and becomes a measurement:

```bash
# Bind at capture time (file paths are hashed now; binding implies direct
# creation, so the exact claim text is what gets bound)
kin add "The parser in src/parse.py handles escapes" --referent src/parse.py

# Re-hash every bound claim; a moved or missing referent demotes the node
# from trusted recall and lists it as a re-verification candidate
kin stale

# After confirming a claim still holds for the new state, rebind it
# (true_of moves to now; the claim is never re-dated)
kin stale --rebind <node-id>
```

Detection never deletes or rewrites content: a stale claim stays recallable,
visibly marked `[stale-referent]` in search and context output, and drops out
of `--trusted-only` projections until re-verified. URL and repo-scope bindings
are recorded with explicit digests (`--referent-digest`) and surfaced but never
auto-fetched.

Kindex also snapshots the SQLite store (rotating, ten per database) before any
automated destructive merge, so a false `graph_merge` or dream-cycle merge is
recoverable — see the human guide's restore section.

Automatic candidates expire after seven days by default. Configure a positive
retention period in global or project config:

```yaml
capture:
  candidate_ttl_days: 7
```

### Grounded Retrieval — When the Graph Knows Nothing

Vector search returns the nearest neighbours for *any* query, however
unrelated. Without a floor a near-null question still pulls real nodes into an
agent's context, and the graph can never say "I don't know."

Kindex calibrates a similarity floor against your own corpus and reports a
verdict with every result set:

```bash
# Measure the null-query similarity distribution and record the floor
kin embed calibrate

# Inspect the current record without recalibrating
kin embed calibrate --show
```

The floor is never a config value. It is an immutable, versioned record keyed
by `provider:model` that carries the corpus it was measured against — node
count, embedding count, sample size, timestamp — so a floor calibrated when 2%
of your graph was embedded is *detected* as stale rather than silently trusted.
Config holds only the policy:

```yaml
grounding:
  enabled: true
  enforce: false          # shadow mode: report the verdict, drop nothing
  floor_percentile: 95.0
  weak_margin: 1.15
  recalibrate_coverage_delta: 0.25
```

Verdicts are `grounded`, `weak`, `ungrounded`, and `uncalibrated` — the last
kept deliberately distinct, because "we have no yardstick" is a different fact
from "we measured and found nothing."

**Shadow mode is the default on purpose.** Enforcing turns a visible,
self-correcting problem (irrelevant results in context) into a silent one (an
agent proceeding without knowledge that was actually there). Run it in shadow
first, see what it *would* have dropped, then set `enforce: true`.

### Multi-Hop Reach

Graph expansion honours `--hops`, with per-hop score decay and a mandatory
beam. The beam's ordering is total and stable, so traversal is reproducible:
adding an edge elsewhere in a hub's neighbourhood cannot silently change what a
query returns.

```yaml
ranking:
  hop_decay: 0.5      # a 2-hop neighbour cannot outrank a 1-hop one
  graph_beam: 200     # required — real graphs have 800+ fan-out hubs
```

### Extraction Engines

Extraction is an **input**, never an authority. Engine output lands in the
`capture_candidates` quarantine and never writes nodes or edges directly.

```bash
# Which engines are available here
kin extract engines

# Score them against your own corpus
kin extract eval --engines keyword,llm --limit 200
```

The gate is two-part: grounding precision is a floor (don't invent), title
recall is the discriminator (actually find what a curator would record). Either
alone is gameable — an engine that only copies verbatim scores perfect
grounding while finding nothing.

An optional LLM-free deterministic engine is available behind an extra:

```bash
pip install 'kindex[talon]'   # ~2.5 GB — never a core dependency
```

It is excluded from `kindex[all]` by design, and degrades to keyword extraction
with a warning when absent. Measure it with `kin extract eval` before enabling
it; an engine that cannot beat regexes on your corpus has not earned the
install size.

## Editing & Superseding

Knowledge changes. Kindex edits are policy-aware: each node type has a mutability class that says how its content may change, so facts stay correctable while history-bearing records stay append-only.

| Class | Node types | What's allowed |
|-------|-----------|----------------|
| `editable` | concept, document, artifact, skill, person, project, question | Full in-place edit: title, content, append, tags, intent, expires |
| `additive` | decision, constraint, directive, checkpoint, watch | History matters — append and expires only; use `supersede` to replace |
| `managed` | task, session, coordination | Refused — use the dedicated `task`/`tag`/`coord` commands |

```bash
# In-place edit (editable types) — accepts node id or exact title
kin edit oauth-flow --title "OAuth2 + OIDC flow" --add-tags auth,oidc

# Additive types only grow: append a dated addendum
kin edit deploy-constraint --append "Clarified: applies to staging too"

# Give any node an expiry — expired nodes stop surfacing and get archived
kin edit conference-notes --expires 2026-09-01

# Replace with history: new node + supersedes edge, old node marked superseded
kin supersede old-decision "We now use OIDC trusted publishing" --reason "tokens deprecated"
```

Every edit logs per-field value diffs to the activity log, and `kin changelog` renders them:

```
## Edited (1 nodes)
  2026-06-11  [concept] OAuth2 + OIDC flow
      title: OAuth2 flow -> OAuth2 + OIDC flow
```

Edits re-embed the node for vector search, protect reserved operational state (locks, claims, coordination messages), and refuse to modify a node another agent has locked unless you pass `--force`. The per-type class can be overridden in config with `edit_policy: {document: additive}` if your team wants stricter history.

## Profiles

One machine, multiple sequestered graphs. Profiles map names to separate data directories so work and personal knowledge never mix — different DBs, different embeddings, different everything.

```yaml
# ~/.config/kindex/kin.yaml
profiles:
  work:
    data_dir: ~/.kindex-work
    roots: [~/Work]
  personal:
    data_dir: ~/.kindex
    roots: [~/Code, ~/Personal]
default_profile: personal
```

Resolution order (first match wins):

1. `--profile <name>` flag
2. `KIN_PROFILE` environment variable
3. `profile:` key from the project's `.kin/config` chain
4. Longest-prefix match of the current directory against profile `roots`
5. `default_profile`
6. Legacy single graph — no profiles configured means nothing changes

```bash
kin profile list                 # configured profiles + file-level stats
kin profile which                # which profile this invocation resolves to
# Two-step adoption: first register your existing graph as the default...
kin profile create personal --data-dir ~/.kindex --roots ~/Code,~/Personal --default
# ...then add the sequestered one
kin profile create work --data-dir ~/.kindex-work --roots ~/Work
kin status                       # shows: Profile: work (via roots)
```

Register the existing legacy graph (usually `~/.kindex`) as the default profile *before* creating others: once a default profile exists, sessions outside all roots route to it, and an unregistered legacy graph stops receiving cron maintenance (`kin profile create` warns when this would happen).

**Stamp guard.** Each profile's database is stamped with its profile name on first open. Opening a stamped database under a different profile raises an error instead of silently mixing graphs — a wrong `--data-dir` can't cross-contaminate.

**MCP note.** The MCP server binds its profile once at process start and keeps it for the process lifetime. To switch profiles for an agent, restart its MCP server (or run a second server with `KIN_PROFILE` set in its environment).

`kin cron` runs one maintenance pass per profile and routes session ingestion by roots — sessions whose cwd falls under a profile's roots land in that profile's graph; the default profile takes the unmatched remainder. With no `default_profile`, a final legacy-remainder pass ingests the unmatched sessions into the legacy graph and keeps its maintenance (reminders, decay, dream) running. `kin cron --profile X` pins a single pass and keeps routing active — it only ingests the sessions X owns; a bare `--data-dir` with no resolved profile runs a legacy take-everything pass on exactly that directory. Routing also applies to `kin ingest sessions|codex-sessions`, the MCP `ingest` tool, and `kin watch`. An explicit `--data-dir` that overrides a profile's data_dir never stamps an unstamped database with the active profile.

## Collab

Multiple agents working the same graph can coordinate through conversations with members, read cursors, shared resources, advisory locks, and standing inject messages.

```bash
# Join a conversation as a member — members get unread tracking
kin coord join payments-refactor

# Attach a shared resource so members see who holds what
kin coord attach payments-refactor invoice-schema

# Advisory locks: signal "I'm working on this" — edits refuse foreign locks
kin lock invoice-schema --ttl 60 --note "migrating columns"
kin unlock invoice-schema

# Targeted message — only alice sees it as unread-for-her
kin coord post payments-refactor "schema branch is yours" --to alice@mbp

# Standing inject message — pushed into members' context until cleared
kin coord inject payments-refactor set "Don't touch the invoice schema until migration lands"
kin coord inject payments-refactor clear
```

**Agent identity** resolves as `KIN_AGENT_ID` env > `agent_id` in config > `user@shorthost`. `kin whoami` shows both the user and the resolved agent id. Locks, claims, cursors, and message targeting all key off this identity.

Members see their collabs in the session-start prime block:

```
### Active collabs
- **payments-refactor** — 2 unread (focus: Extract billing service)
  COLLAB MSG: Don't touch the invoice schema until migration lands (from alice@mbp)
  Locked: invoice-schema (held by alice@mbp)
  Check the collab: coord_read payments-refactor
```

New targeted/broadcast messages and standing injects also surface mid-session through the prompt hook (with a cooldown so they don't nag). Display is configurable:

```yaml
agent_id: jeremy-laptop        # optional; default user@shorthost
collab:
  enabled: true
  display: full                # full | minimal (one line per collab) | quiet (no prime block)
  prompt_cooldown_minutes: 10  # mid-session injection cooldown
```

Locks are advisory and expire — an expired lock never blocks anyone, and the cron pass sweeps stale locks, conversations, and task claims.

## .kin/ Directory & Inheritance

Projects use `.kin/` directories that encode their communication style, engineering standards, and values. Teams inherit from orgs. Repos inherit from teams. The knowledge graph carries the voice forward.

```
~/.kindex/voices/acme.kin             # Org voice (downloadable, public)
    ^
    |  inherits
~/Code/platform/.kin/config           # Platform team context
    ^
    |  inherits
~/Code/payments-service/.kin/config   # Service-specific context
```

```yaml
# payments-service/.kin/config
name: payments-service
audience: team
domains: [payments, python]
inherits:
  - ../platform/.kin/config
work_policy:
  require_active_tag: true
  linear:
    enabled: true        # opt-in; personal repos leave this false/absent
    require_issue: true
    team: ENG
  git:
    block_commit_without_tag: true
    block_commit_without_linear: true
```

The `.kin/` directory is the standard location for all kindex project artifacts:
- `.kin/config` — project metadata (voice, domains, audience, inheritance)
- `.kin/index.json` — graph snapshot for git tracking
- `.kin/code-map.json` — repo-relative code map generated by `kin export code-map`
- `.kin/.gitignore` — ignores local-only runtime state under `.kin/local`, `.kin/cache`, `.kin/tmp`, and `.kin/private`

These files are meant to ship with the code. Do not ignore the whole `.kin/`
directory in project `.gitignore`; ignore only local/private subdirectories.
Kindex resolves project config from `--project-path`, then `KIN_PROJECT`, then
the git worktree root, then the current directory. User config still lives in
`~/.config/kindex/kin.yaml` and deep-merges below project config, so user
preferences remain local while the repo's work contract travels with the repo.
Generated `.kin/` snapshots use canonical, id-keyed ordering and omit volatile
timestamps so repeated exports of unchanged source do not churn Git diffs.
Concurrent branches merge them without manual conflicts via a structured merge
driver that `kin index` registers automatically on first run (or `kin setup-merge`
to (re)install it in a fresh clone). `.kin/index.json` is then unioned by node id
(newer `updated_at` wins) and `.kin/code-map.json` by node/edge/layer — lossless
across machines (regenerating from one machine's local DB would drop the other
branch's nodes), with output byte-identical to a fresh `kin index`. `kin merge-kin`
is the driver git invokes; repos without it registered fall back to git's default
merge. Never hand-resolve a generated `.kin` snapshot.
Tracked `.kin` artifacts must be self-contained and machine-portable: code-map
paths are repo-relative POSIX paths, and task/report metadata must not point at
`$HOME`, `/Users/...`, `/tmp/...`, or another developer-local filesystem
location. If a scanner report is needed as evidence for future work, ship a
repo-local subset or a durable shared artifact rather than a local pointer.

`kin export code-map` is different from the normal graph export: it projects
current code structure into a small dashboard- and agent-friendly graph of
files, classes, functions, layers, and code dependencies. Use it when a tool
needs the repo's code shape, not the full Kindex knowledge graph.

| Export | Contains | Use case |
| --- | --- | --- |
| `kin export` | Audience-scoped Kindex nodes, edges, provenance, and graph metadata | Backup, exchange, or graph-level tooling |
| `kin export code-map` | Current repo code structure, code dependencies, layers, and repo-relative file paths | Dashboards, code navigation, and agent tooling |

For code-map output, `src/file.py` is portable; `/Users/alice/repo/src/file.py`
and `C:\repo\src\file.py` are not.

Code-map path requirements:
- Output always uses repo-relative POSIX paths, even when the source machine is
  Windows.
- Absolute provenance is normalized only when it resolves inside the requested
  repo root.
- Outside-root, malformed, or unresolved paths are omitted with a safe warning
  rather than leaked into the artifact.
- Archived nodes are excluded by default because the tracked artifact describes
  current code; API callers can opt into `include_archived=True` for historical
  analysis.

The payments service gets Acme's voice principles, the platform's engineering standards, AND its own domain context. Local values override ancestors. Lists merge with dedup. Parent directories auto-walk when no explicit `inherits` is set.

Old-style `.kin` files (plain YAML) are auto-upgraded to `.kin/config` on first access.

See [examples/kin-voices/](examples/kin-voices/) for ready-to-use voice templates.

## Architecture

```
SQLite + FTS5          <- primary store and full-text search
  nodes: id, title, content, type, weight, audience, domains, extra
  edges: from_id, to_id, type, weight, provenance
  fts5:  content synced via triggers

Retrieval pipeline:
  FTS5 BM25 --+
  Graph BFS --+-- RRF merge -- tier formatter -- context block
  (vectors) --+                   |
      |                   full | abridged | summarized | executive | index
      |
  Embedding providers (configurable):
      voyage-context-4 (contextual chunks) | openai | gemini | local

LLM cache tiers (kin ask):
  Tier 1: codebook (stable node index)     <- cached @ 10% cost
  Tier 2: query-relevant context           <- cached per-topic @ 10% cost
  Tier 3: user question                    <- full price, tiny

Reminders:
  reminders table (SQLite)    <- separate from knowledge graph
  Time parsing:  dateparser (NL) + dateutil.rrule (recurrence) + cronsim (cron)
  Channels:      system (macOS) | slack | email | claude (hook) | terminal
  Daemon:        launchd/cron adaptive interval -> check due -> notify -> auto-snooze
  Scheduling:    adaptive tiers (>7d=daily, >1d=hourly, >1h=10min, <1h=5min, none=disabled)
  Actions:       shell commands | claude -p | codex exec | opencode run
  Stop guard:    blocks session exit when actionable reminders pending

Dream (kin dream):
  Modes:         lightweight (<5s) | full (non-LLM) | deep (claude -p clusters)
  Triggers:      CLI | cron step 11 | throttled Stop-time detach
  Dedup:         difflib.SequenceMatcher, 4-char title bucketing, 0.95 merge / 0.85 suggest
  Consolidation: suggestion auto-apply, bounded domain proposals, cluster summarisation
  Safety:        no tag-derived edges, overlap dedup, fcntl.flock, protected types

Three integration paths:
  MCP plugin --> Claude calls tools natively (search, add, learn, remind, ...)
  CLI hooks  --> SessionStart / PreCompact / Stop lifecycle events
  Adapters   --> Entry-point discovery for custom ingestion sources
  Code       --> ctags + cscope + tree-sitter structural analysis
```

### Node Types

**Knowledge**: concept, document, session, person, project, decision, question, artifact, skill

### Code Intelligence

Ingest repository structure with `kin ingest code --directory .`:

- **Module nodes** (artifact) — one per source file with structural summary: classes, public functions, signatures, imports
- **Symbol nodes** (concept) — one per class/interface/type with method signatures
- **Edges** — imports (`depends_on`), inheritance (`implements`), containment (`context_of`), call graph (`relates_to`)
- **Three extraction tiers** — ctags (100+ languages), cscope (C/C++ cross-refs), tree-sitter (AST call graphs)
- **Resilient fallback** — unsupported or untagged files still become module nodes; available tree-sitter parsers can still enrich those modules, with fallback provenance retained in metadata
- **Incremental** — file hashing skips unchanged files on re-ingest
- **Unity projects (opt-in)** — `--unity` (or `code_ingest: {unity: true}` in `.kin/config`) indexes serialized assets (`.unity`, `.prefab`, `.asset`, `.mat`, `.controller`, `.anim`), sniffs text-vs-binary serialization, and attaches each asset's `.meta` GUID so models can resolve GUID-based references; `code_ingest.include_extensions` maps further extensions (e.g. `.shader: Unity Shader`)

Code structure lives in the same graph as your decisions, watches, and constraints. Search finds both what calls a function and what broke last time someone changed it.

**Operational**: constraint (invariants), directive (soft rules), checkpoint (pre-flight), watch (attention flags)

## CLI Reference (70+ commands)

### Core
| Command | Description |
|---------|-------------|
| `kin search <query>` | Hybrid FTS5 + graph search with RRF merging (--tags, --mine, --trusted-only) |
| `kin context` | Formatted context block for AI injection (--level, --tokens, --trusted-only) |
| `kin add <text>` | Quick capture with auto-extraction and linking (--tags, --type) |
| `kin show <id>` | Full node details with edges, provenance, and state |
| `kin list` | List nodes (--type, --status, --tags, --audience, --mine, --limit) |
| `kin ask <question>` | Question classification + LLM or context answer |

### Knowledge Management
| Command | Description |
|---------|-------------|
| `kin learn` | Extract knowledge from sessions and inbox |
| `kin link <a> <b>` | Create weighted edge between nodes |
| `kin edit <id>` | Policy-aware in-place edit (--title, --content, --append, --add-tags, --expires) |
| `kin supersede <id> <text>` | Replace a node with a new one, preserving history (--reason) |
| `kin alias <id> [add\|remove\|list]` | Manage AKA/synonyms for a node |
| `kin register <id> <path>` | Associate a file path with a node |
| `kin orphans` | Semantic nodes with no semantic connections (sessions excluded) |
| `kin trail <id>` | Temporal history and provenance chain |
| `kin decay` | Apply weight decay to stale nodes/edges |
| `kin recent` | Recently active nodes |
| `kin tag [action]` | Session tags: start, update, segment, pause, end, resume, list, show |
| `kin archive [action]` | Search, restore, or run the reversible slow-graph archive |
| `kin candidate [action]` | Quarantined capture review: list, show, accept, reject, prune, erase |
| `kin verify <node>` | Assert verification and optional RFC 3339 valid interval |
| `kin invalidate <node>` | Record asserted invalidation actor, code, and exclusive end time |
| `kin stale` | Re-hash referent-bound nodes; demote stale ones from trusted recall (--rebind) |
| `kin remind [action]` | Reminders: create, list, show, snooze, done, cancel, check, exec |
| `kin mode [action]` | Conversation modes: activate, list, show, create, export, import, seed |

### Graph Analytics
| Command | Description |
|---------|-------------|
| `kin graph [mode]` | Dashboard: stats, centrality, communities, bridges, trailheads |
| `kin suggest` | Bridge opportunity suggestions (--accept, --reject) |
| `kin skills [person]` | Skill profile and expertise for a person |
| `kin embed` | Index nodes for vector similarity search |
| `kin embed plan/enqueue/reindex/drain/status` | Scoped embedding maintenance for full or gradual reindexing |

`embedding.provider: voyage` uses `voyage-context-4` by default. When the
configured model supports contextual chunk embeddings, Kindex stores stable
overlapping chunk vectors and aggregates chunk hits back to the parent node. Use
`kin embed plan --stale --kin ./path/.kin` to estimate a targeted update,
`kin embed enqueue --stale --tags mea` to trickle selected work through cron, or
`kin embed reindex --target ./repo --limit 100` for a bounded foreground pass.
Unsupported providers/models keep the single-vector strategy unless they later
gain explicit contextual support.

### Operational
| Command | Description |
|---------|-------------|
| `kin status` | Graph health + operational summary (--trigger, --owner, --mine) |
| `kin set-audience <id> <scope>` | Set privacy scope (private/team/org/public) |
| `kin set-state <id> <key> <value>` | Set mutable state on directives/watches |
| `kin export` | Audience-aware graph export with PII stripping |
| `kin export code-map` | Repo-relative code map for dashboards, code navigation, and agent tooling |
| `kin import <file>` | Import nodes/edges from JSON/JSONL (--mode merge/replace) |
| `kin sync-links` | Update node content with connection references |

### Collab & Multi-Agent
| Command | Description |
|---------|-------------|
| `kin coord [action]` | Agent coordination: start, post, read, list, end, join, attach, inject |
| `kin lock <id>` | Acquire an advisory lock on a node (--ttl, --note, --force) |
| `kin unlock <id>` | Release an advisory lock (--force for foreign locks) |
| `kin profile [action]` | Named graph profiles: list, which, create |

### Ingestion & External Sources
| Command | Description |
|---------|-------------|
| `kin ingest <source>` | Ingest from: projects, sessions, codex-sessions, files, commits, github, linear, code, all |
| `kin cron` | One-shot maintenance cycle (for crontab/launchd) |
| `kin dream` | Knowledge consolidation: dedup and reviewable link proposals (--deep, --detach) |
| `kin watch` | Watch for new sessions and ingest them (--interval) |
| `kin analytics` | Archive session analytics and activity heatmap |
| `kin index` | Write .kin/index.json for git tracking |
| `kin merge-kin` | Git merge driver: structured union merge of `.kin` artifacts (invoked by git) |

### Infrastructure
| Command | Description |
|---------|-------------|
| `kin init` | Initialize data directory |
| `kin config [show\|get\|set]` | View or edit configuration |
| `kin agent-config [show\|set]` | View or tune per-client/per-instance agent behavior overrides |
| `kin policy [show\|check]` | Show or enforce project work policy from `.kin/config` |
| `kin setup-merge` | Register the `.kin` structured merge driver in the current git repo |
| `kin setup-hooks` | Install lifecycle hooks into Claude Code |
| `kin setup-codex-hooks` | Install prompt-time attention hook into Codex |
| `kin setup-codex-mcp` | Install kindex MCP server into Codex |
| `kin setup-gemini-mcp` | Install kindex MCP server into Gemini CLI |
| `kin setup-antigravity-mcp` | Install kindex MCP server into Google Antigravity |
| `kin setup-antigravity-hooks` | Install lifecycle hooks into Google Antigravity |
| `kin setup-opencode-mcp` | Install kindex MCP server into OpenCode |
| `kin setup-cursor-mcp` | Install kindex MCP server into Cursor |
| `kin setup-cron` | Install periodic maintenance + dedicated reminder-check job (launchd/crontab) |
| `kin setup-claude-md` | Output/install recommended CLAUDE.md kindex directives |
| `kin setup-agents-md` | Output/install recommended AGENTS.md kindex directives (Codex, OpenCode) |
| `kin setup-gemini-md` | Output/install recommended GEMINI.md kindex directives |
| `kin setup-antigravity-md` | Output/install Antigravity/GEMINI.md kindex directives |
| `kin setup-cursor-rules` | Output/install recommended Cursor rule (.mdc) for kindex |
| `kin stop-guard` | Stop hook guard for actionable reminders |
| `kin doctor` | Health check with graph enforcement (--fix) |
| `kin migrate` | Import markdown topics into SQLite |
| `kin budget` | LLM spend tracking |
| `kin attention` | Toggle/check/estimate conversation-attention reminder injection |
| `kin attention-hook` | Advisory attention hook for prompt/tool events |
| `kin whoami` | Show current user identity |
| `kin changelog` | What changed (--since, --days, --actor) |
| `kin log` | Recent activity log |
| `kin git-hook [install\|uninstall]` | Manage git hooks in a repository |
| `kin prime` | Generate context for SessionStart hook (--codebook) |
| `kin compact-hook` | Pre-compact knowledge capture |

## Configuration

Config is layered like git — global defaults, then global config, then local config. Each layer deep-merges over the previous, so you only set what you want to override.

| Layer | Path | Purpose |
|-------|------|---------|
| Global | `~/.config/kindex/kin.yaml` | User-wide defaults |
| Local | `.kin/config` or `kin.yaml` at project root | Project-specific overrides shipped with code |

Use `kin config set --global llm.enabled true` for global settings, or `kin config set llm.model claude-sonnet-4-6` for project-local. Use `--project-path /path/to/repo` or `KIN_PROJECT=/path/to/repo` when running from outside the repo.

Agent-facing behavior can be tuned at three levels:

```bash
# Global/project default for every client
kin config set attention.tick_interval 3

# Client default, e.g. every Claude session
kin agent-config set attention.tick_interval 2 --client claude

# One instance/conversation
kin agent-config set hooks.prime_tokens 1200 --client claude --scope instance --instance session-a
```

`agent-config` writes only approved behavior keys such as `attention.*`,
`sim.*`, `collab.prompt_cooldown_minutes`, and `hooks.prime_tokens`; it cannot
change storage paths or arbitrary config. Agents should propose these changes
through their normal tool/command permission flow. Antigravity hooks force a user
permission prompt before `kin config set` or `kin agent-config set` runs.

```yaml
agents:
  clients:
    claude:
      attention:
        tick_interval: 2
        display: quiet
  instances:
    claude:session-a:
      client: claude
      attention:
        tick_interval: 1
      hooks:
        prime_tokens: 1200
```

```yaml
data_dir: ~/.kindex

llm:
  enabled: false
  provider: anthropic             # anthropic or openai
  model: claude-haiku-4-5-20251001
  api_key_env: ANTHROPIC_API_KEY   # comma-separated fallback allowed
  cache_control: true              # Prompt caching (90% savings on repeated prefixes)
  codebook_min_weight: 0.5         # Min node weight for codebook inclusion
  tier2_max_tokens: 4000           # Token budget for query-relevant context

embedding:
  provider: voyage                 # voyage, openai, gemini, or local
  # model: ""                      # empty = provider default (voyage-context-4 for Voyage)
  # api_key_env: ""                # empty = provider default (VOYAGE_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY)
  # dimensions: 0                  # 0 = provider default (1024 / 1536 / 3072 / 384)
  # strategy: ""                   # empty/auto; contextual only when the model supports it
  # chunk_chars: 6000              # stable local chunks for contextual embedding models
  # chunk_overlap_chars: 600
  # max_group_chunks: 20
  # reindex_max_jobs: 200          # cron drain cap for queued embedding work
  # reindex_max_queue: 100000

budget:
  daily: 0.50
  weekly: 2.00
  monthly: 5.00

capture:
  candidate_ttl_days: 7          # positive TTL for quarantined automatic captures

attention:
  enabled: false                  # default; runtime override with `kin attention on/off`
  tick_interval: 3                # run every N prompt-check ticks
  max_candidates: 6               # deterministic prefilter size before LLM judge
  max_check_cost: 0.01            # estimated per-check cap
  max_conversation_cost: 0.25     # best-effort cap when the client provides a stable session id
  cooldown_seconds: 1800          # suppress repeat injections

project_dirs:
  - ~/Code
  - ~/Personal

defaults:
  hops: 2
  min_weight: 0.1
  mode: bfs

reminders:
  enabled: true
  check_interval: 300            # 5 min base interval
  adaptive_scheduling: true      # adjust interval based on nearest reminder
  min_interval: 300              # floor for adaptive scheduling
  default_channels: [system]     # system, slack, email, claude, terminal
  snooze_duration: 900           # 15 min default snooze
  auto_snooze_timeout: 300       # auto-snooze after 5 min inaction
  max_action_overdue: 86400      # stale actions notify only; 0 disables guard
  idle_suppress_after: 600       # suppress if idle > 10 min
  stop_guard_enabled: false      # opt-in; blocking Stop hooks are noisy in Claude
  dream_on_stop_enabled: true    # launch throttled detached dream from Claude Stop hook
  dream_min_interval: 3600       # seconds between scheduled/hook dream starts
  dream_max_domain_link_suggestions: 50 # pending domain-review cap per graph
  channels:
    slack:
      enabled: false
      webhook_url: ""
    email:
      enabled: false
      smtp_host: ""
      to_addr: ""
```

Automatic snoozes retry notifications without extending an action's freshness
window. A deliberate `kin remind snooze` defers that window to the snooze expiry;
`kin remind exec` runs an action deliberately. Older snoozes without provenance
retain their previous deferral semantics, since automatic and manual snoozes
were historically stored identically. This change does not classify or repair
an existing reminder backlog.

Use `kin attention estimate --messages 1000` to estimate cost over a fixed prompt window. Conversation accounting is retained when a client provides a stable session id. Hook-driven attention does not fall back to cwd as a fake conversation id, because that would cross-pollute two chats open in the same repo.

## Development

```bash
make dev          # install with dev + LLM dependencies
make test         # run the full test suite
make check        # lint + test combined
make clean        # remove build artifacts
```

## License

MIT. Copyright (c) 2026 Wander. Kindex is a Wander project.

<!-- mcp-name: io.github.jmcentire/kindex -->

### Kinbase compatibility

Read signed Kinbase facts and unresolved questions with `kin kinbase sync --repo PATH`.
Kindex preserves provenance-clamped standing, distinguishes raw evidence from reduced
snapshots, and never writes Kinbase events. See [the reader guide](docs/kinbase.md).
