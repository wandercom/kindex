
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'concept',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    aka TEXT NOT NULL DEFAULT '',           -- JSON array of synonyms
    intent TEXT NOT NULL DEFAULT '',        -- "I was trying to..."
    -- provenance
    prov_who TEXT NOT NULL DEFAULT '',      -- JSON array of person IDs
    prov_when TEXT NOT NULL DEFAULT '',     -- ISO datetime
    prov_activity TEXT NOT NULL DEFAULT '', -- meeting / debug-session / etc.
    prov_why TEXT NOT NULL DEFAULT '',      -- what question prompted capture
    prov_source TEXT NOT NULL DEFAULT '',   -- url / file path / session id
    -- explicit trust assertions (NULL means legacy/unverified)
    verified_at TEXT,
    verified_by TEXT,
    prov_method TEXT,
    valid_at TEXT,
    invalid_at TEXT,
    -- referent binding + two clocks (R0): what external thing the claim
    -- describes (JSON {path|url, content_digest, digest_scope}), when the
    -- claim was asserted, and when the referent was observed in the state
    -- the digest describes. NULL = unbound claim (legacy behavior).
    referent TEXT,
    asserted_at TEXT,
    true_of TEXT,
    -- scoring
    weight REAL NOT NULL DEFAULT 0.5,
    domains TEXT NOT NULL DEFAULT '',       -- JSON array
    status TEXT NOT NULL DEFAULT 'active',  -- active / archived / deprecated / open-question
    audience TEXT NOT NULL DEFAULT 'private',  -- private / team / public
    -- timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_accessed TEXT NOT NULL DEFAULT (datetime('now')),
    -- extra fields as JSON (preserves domain-specific data)
    extra TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id TEXT NOT NULL REFERENCES nodes(id),
    to_id TEXT NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL DEFAULT 'relates_to',
    weight REAL NOT NULL DEFAULT 0.5,
    provenance TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(from_id, to_id, type)
);

-- FTS5 full-text search over node content
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    id UNINDEXED,
    title,
    content,
    aka,
    intent,
    domains,
    content=nodes,
    content_rowid=rowid
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, id, title, content, aka, intent, domains)
    VALUES (new.rowid, new.id, new.title, new.content, new.aka, new.intent, new.domains);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, title, content, aka, intent, domains)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.aka, old.intent, old.domains);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, title, content, aka, intent, domains)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.aka, old.intent, old.domains);
    INSERT INTO nodes_fts(rowid, id, title, content, aka, intent, domains)
    VALUES (new.rowid, new.id, new.title, new.content, new.aka, new.intent, new.domains);
END;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_updated ON nodes(updated_at);
CREATE INDEX IF NOT EXISTS idx_nodes_weight ON nodes(weight DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_audience ON nodes(audience);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_active_tag_project
    ON nodes (
        json_extract(extra, '$.tag'),
        COALESCE(json_extract(extra, '$.project_path'), '')
    )
    WHERE type = 'session'
      AND json_valid(extra)
      AND json_extract(extra, '$.session_status') = 'active';

-- Activity log for audit trail
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,             -- add_node, update_node, delete_node, add_edge, etc.
    target_id TEXT NOT NULL DEFAULT '',  -- node or edge ID
    target_title TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',    -- who performed the action
    details TEXT NOT NULL DEFAULT ''   -- JSON with additional context
);

CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_activity_action ON activity_log(action);

-- Suggestions table for bridge opportunities
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_a TEXT NOT NULL,
    concept_b TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    identity_kind TEXT NOT NULL DEFAULT 'title'
        CHECK (identity_kind IN ('title', 'node_id')),
    kind TEXT NOT NULL DEFAULT 'bridge',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_status_created
    ON suggestions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_suggestions_status_pair
    ON suggestions(status, concept_a, concept_b);
CREATE INDEX IF NOT EXISTS idx_suggestions_pair
    ON suggestions(concept_a, concept_b);

-- Automatic extraction is staged here for explicit review. Candidate rows are
-- deliberately separate from nodes/edges/FTS so no query can accidentally
-- promote or recall unreviewed material.
CREATE TABLE IF NOT EXISTS capture_candidates (
    id TEXT PRIMARY KEY,
    title TEXT,
    content TEXT,
    node_type TEXT,
    domains TEXT,
    connections TEXT,
    source_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_method TEXT,
    disposition_code TEXT,
    conflict_ids TEXT NOT NULL DEFAULT '[]',
    conflict_codes TEXT NOT NULL DEFAULT '[]',
    created_node_id TEXT,
    CHECK (status IN ('pending','conflicted','accepted','rejected','expired'))
);

CREATE INDEX IF NOT EXISTS idx_capture_candidates_status_created
    ON capture_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_status_expires
    ON capture_candidates(status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_capture_candidates_live_payload
    ON capture_candidates(payload_digest)
    WHERE status IN ('pending', 'conflicted');

-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Reminders
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'active',
    reminder_type TEXT DEFAULT 'once',
    schedule TEXT DEFAULT '',
    next_due TEXT NOT NULL,
    last_fired TEXT,
    snooze_until TEXT,
    snooze_count INTEGER DEFAULT 0,
    channels TEXT DEFAULT '[]',
    related_node_id TEXT,
    tags TEXT DEFAULT '',
    extra TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_next_due ON reminders(next_due);
CREATE INDEX IF NOT EXISTS idx_reminders_priority ON reminders(priority);

-- Stigmergic injection pheromone: a retrieval-ranking channel SEPARATE from
-- edge.weight/node.weight (which drive graph topology). Tracks which nodes have
-- proven useful WHEN INJECTED, learned across sessions. Deposited on injection,
-- reinforced when the agent actually used the injection, decayed over time.
-- context='' is the coarse global trail (warm-up); context=<project> are
-- conditioned trails that self-resolve when the work regime changes.
CREATE TABLE IF NOT EXISTS injection_pheromone (
    node_id TEXT NOT NULL REFERENCES nodes(id),
    context TEXT NOT NULL DEFAULT '',
    strength REAL NOT NULL DEFAULT 0.0,
    deposits INTEGER NOT NULL DEFAULT 0,
    reinforcements INTEGER NOT NULL DEFAULT 0,
    missed INTEGER NOT NULL DEFAULT 0,   -- counterfactual deposits: would-have-helped but wasn't injected
    last_deposit TEXT NOT NULL DEFAULT (datetime('now')),
    last_decay TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (node_id, context)
);

CREATE INDEX IF NOT EXISTS idx_pheromone_node ON injection_pheromone(node_id);
CREATE INDEX IF NOT EXISTS idx_pheromone_strength ON injection_pheromone(strength DESC);

-- Learned PAIR co-activation: nodes that proved useful together in the same
-- session. A THIRD channel, separate from both edge.weight and node-level
-- pheromone. It must never be folded into edges.weight: that column is
-- topology ASSERTED by a human or an agent, and merging a learned correction
-- into it destroys the told/inferred distinction — after which the graph can
-- no longer tell you what it was told from what it inferred.
--
-- Deposits are gated on CONFIRMED USE, not co-retrieval. Co-occurrence in a
-- result set is not evidence of usefulness; strengthening on it would teach
-- the graph the retriever's own biases and then call the result evidence.
-- node_a < node_b always, so a pair has exactly one row.
CREATE TABLE IF NOT EXISTS node_coactivation (
    node_a TEXT NOT NULL REFERENCES nodes(id),
    node_b TEXT NOT NULL REFERENCES nodes(id),
    context TEXT NOT NULL DEFAULT '',
    strength REAL NOT NULL DEFAULT 0.0,
    events INTEGER NOT NULL DEFAULT 0,
    last_event TEXT NOT NULL DEFAULT (datetime('now')),
    last_decay TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (node_a, node_b, context)
);

CREATE INDEX IF NOT EXISTS idx_coactivation_a ON node_coactivation(node_a);
CREATE INDEX IF NOT EXISTS idx_coactivation_b ON node_coactivation(node_b);
CREATE INDEX IF NOT EXISTS idx_coactivation_strength
    ON node_coactivation(strength DESC);
