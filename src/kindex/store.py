"""SQLite store — primary persistence layer for Kindex."""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .privacy import POLICY_VERSION, protect_logger, redact, redact_serialized, redact_text
from .privacy import redacting_print as print


def _json_default(obj):
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return redact_text(str(obj))
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def _jdumps(obj):
    return json.dumps(redact(obj), default=_json_default)


def _guard_action_credentials(extra):
    """Do not persist a broken executable command by replacing its secret."""
    command = extra.get("action_command") if isinstance(extra, dict) else None
    if isinstance(command, str) and redact_text(command) != command:
        raise ValueError("Reminder commands must reference credentials through environment variables")

from .config import Config
from .schema import (
    ALL_NODE_TYPES,
    CREATE_TABLES,
    LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,
    NODE_ID_SUGGESTION_SOURCES,
    EDGE_TYPES,
    SCHEMA_VERSION,
    SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
    SEMANTIC_METRICS_SCHEMA_VERSION,
    SESSION_PAUSE_REASON_DUPLICATE_MIGRATION,
    SUGGESTION_IDENTITY_KINDS,
    edit_class_for,
)

logger = protect_logger(logging.getLogger(__name__))

SCHEMA_RECOVERY_PATH_META = "schema_recovery_snapshot_path"
SCHEMA_RECOVERY_REASON_META = "schema_recovery_snapshot_reason"

_SEMANTIC_NODE_PLACEHOLDERS = ",".join(
    "?" for _ in SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES
)
_NON_DOMAIN_EDGE_SQL = "COALESCE(e.provenance, '') != ?"
_SEMANTIC_EDGE_SQL = (
    f"{_NON_DOMAIN_EDGE_SQL} "
    f"AND source.type NOT IN ({_SEMANTIC_NODE_PLACEHOLDERS}) "
    f"AND target.type NOT IN ({_SEMANTIC_NODE_PLACEHOLDERS})"
)
_SEMANTIC_EDGE_PARAMS = (
    LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,
    *SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
    *SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
)


def _now() -> str:
    return datetime.now(tz=None).isoformat(timespec="seconds")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class EditPolicyError(ValueError):
    """An edit was refused by the node-type edit policy."""


class LockHeldError(RuntimeError):
    """The node is locked by another agent and force was not given."""


class ProfileMismatchError(RuntimeError):
    """The database is stamped for a different profile than the active one."""


class UnsupportedSchemaVersionError(RuntimeError):
    """The database schema is newer than this Kindex build understands."""


class SchemaMigrationError(RuntimeError):
    """A schema migration could not proceed safely or complete."""


class CandidateNotFoundError(ValueError):
    """A capture candidate does not exist."""


class CandidateStateError(ValueError):
    """A capture candidate is expired or already terminal."""


class StaleReviewError(ValueError):
    """The supplied candidate review token no longer matches durable state."""


class TitleCollisionError(ValueError):
    """Automated promotion would collide with an existing durable title."""


class InvalidIntervalError(ValueError):
    """A valid-time or evaluation-time value violates the UTC contract."""


# Extra-JSON keys owned by dedicated subsystems (tasks, sessions,
# coordination, locks). edit_node must never alter these.
RESERVED_EXTRA_KEYS = frozenset({
    "claim", "lock", "coord_status", "session_status", "task_status",
    "current_state", "messages", "members", "resources", "inject_messages",
    # Recorded referent-staleness demotion (R0). Written by the stale sweep /
    # bind_referent only; a generic edit must not fabricate or clear it.
    "referent_stale",
})

_EXPIRES_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DIFF_TRUNCATE = 500
_CAPTURE_CONTENT_LIMIT = 4000
_CAPTURE_TITLE_LIMIT = 500
_CAPTURE_WHY_LIMIT = 2000
_AUDIT_TEXT_LIMIT = 128
_LIVE_CANDIDATE_STATUSES = ("pending", "conflicted")
_ALL_CANDIDATE_STATUSES = _LIVE_CANDIDATE_STATUSES + (
    "accepted", "rejected", "expired",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Lock-error remedy for the supersede path: neither `kin supersede` nor the
# MCP supersede tool exposes a force flag (per contract), so the message must
# name the remedy that actually exists on those surfaces.
_SUPERSEDE_LOCK_REMEDY = (
    "release the lock first (kin unlock --force / lock_release force=True); "
    "supersede does not take a force flag"
)


def _validate_expires(expires: str) -> None:
    """Accept YYYY-MM-DD only (zero-padded, real calendar date)."""
    if not isinstance(expires, str) or not _EXPIRES_RE.match(expires):
        raise ValueError(f"expires must be YYYY-MM-DD, got {expires!r}")
    try:
        datetime.strptime(expires, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"expires must be a real YYYY-MM-DD date, got {expires!r}"
        ) from None


def _normalize_binding(
    referent: dict | None,
    asserted_at: str | None,
    true_of: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Validate/normalize an R0 referent binding for storage.

    Returns ``(referent_json, asserted_at, true_of)``. ``asserted_at``
    defaults to now when a referent is supplied; ``true_of`` defaults to
    ``asserted_at`` (claim time and observation time coincide unless the
    caller says otherwise).
    """
    from .referent import validate_referent
    from .trust import normalize_rfc3339

    referent_json = None
    if referent is not None:
        clean_referent = validate_referent(referent)
        if redact(clean_referent) != clean_referent:
            # A replacement URL/path would name a different external object.
            # Refuse the binding without echoing its credential-bearing value.
            raise ValueError("Referent contains credentials; remove them before binding")
        referent_json = _jdumps(clean_referent)
    asserted_norm = (
        normalize_rfc3339(asserted_at, field="asserted_at")
        if asserted_at is not None else None
    )
    true_norm = (
        normalize_rfc3339(true_of, field="true_of")
        if true_of is not None else None
    )
    if referent_json is not None and asserted_norm is None:
        asserted_norm = normalize_rfc3339(_utc_now(), field="asserted_at")
    if true_norm is None:
        true_norm = asserted_norm
    return referent_json, asserted_norm, true_norm


def _trunc(value: Any, limit: int = _DIFF_TRUNCATE) -> str | None:
    """Stringify a diff value and truncate it for activity-log storage."""
    if value is None:
        return None
    s = redact_text(value) if isinstance(value, str) else _jdumps(value)
    return s if len(s) <= limit else s[:limit]


def _canonical_dumps(value: Any) -> str:
    """Canonical UTF-8 JSON text used for capture and review digests."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _clean_audit_text(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > _AUDIT_TEXT_LIMIT:
        raise ValueError(f"{field} must be at most {_AUDIT_TEXT_LIMIT} characters")
    if _ANY_CONTROL_RE.search(cleaned):
        raise ValueError(f"{field} must not contain control characters")
    return redact_text(cleaned)


def _clean_capture_text(
    value: str,
    *,
    field: str,
    limit: int,
    content: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    controls = _TERMINAL_CONTROL_RE if content else _ANY_CONTROL_RE
    if controls.search(cleaned):
        raise ValueError(f"{field} must not contain terminal control characters")
    return redact_text(cleaned)


def active_lock(node: dict) -> dict | None:
    """Return extra['lock'] if present and unexpired, else None.

    Expiry is lazy: an expired lock is treated as absent (callers may
    overwrite it); the daemon sweep clears them from storage eventually.
    """
    extra = node.get("extra") or {}
    lock = extra.get("lock") if isinstance(extra, dict) else None
    if not isinstance(lock, dict):
        return None
    expires_at = lock.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now():
                return None
        except (ValueError, TypeError):
            pass  # unparseable expiry — keep the lock active rather than dropping it
    return lock


def node_expired(node: dict, today: str | None = None) -> bool:
    """True if extra['expires'] (YYYY-MM-DD) is strictly in the past.

    Matches active_watches() semantics: a node expiring today is still live.
    Generic — usable by hooks/attention/daemon for any node type.
    """
    extra = node.get("extra") or {}
    expires = extra.get("expires") if isinstance(extra, dict) else None
    if not expires or not isinstance(expires, str):
        return False
    today = today or _now()[:10]
    return expires < today


def node_retired(node: dict) -> bool:
    """True for any non-active node (archived, superseded, completed, …).

    Context-rendering pulls filter to ACTIVE status via this helper; the
    search fence in fts/hybrid keeps its explicit archived/superseded
    pair because search legitimately ranks e.g. completed tasks.
    """
    return (node.get("status") or "active") != "active"


class Store:
    """SQLite-backed knowledge graph with FTS5 full-text search.

    This is the primary query engine. Markdown files remain as
    human-readable canonical source; the store indexes them.
    """

    def __init__(
        self,
        config: Config,
        *,
        sqlite_timeout: float = 5.0,
        migration_step_hook: Callable[[int, str], None] | None = None,
    ):
        self.config = config
        # Support both kindex.db (new) and conv.db (legacy)
        new_db = config.data_path / "kindex.db"
        old_db = config.data_path / "conv.db"
        self.db_path = old_db if old_db.exists() and not new_db.exists() else new_db
        self._conn: sqlite3.Connection | None = None
        self._sqlite_timeout = max(0.0, float(sqlite_timeout))
        self._migration_step_hook = migration_step_hook
        # Profile stamp guard: configs that carry an active_profile (added by
        # the profiles feature) bind this database to that profile name.
        self._expected_profile: str | None = getattr(config, "active_profile", None)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.config.data_path.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                timeout=self._sqlite_timeout,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={int(self._sqlite_timeout * 1000)}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            try:
                self._init_schema()
                self._check_profile_stamp()
            except BaseException:
                conn, self._conn = self._conn, None
                if conn is not None:
                    conn.close()
                raise
        return self._conn

    def _check_profile_stamp(self) -> None:
        """Enforce the per-database profile stamp (meta key 'kin_profile').

        No active profile -> no stamping, no check (legacy single-graph).
        Active profile + unstamped db -> stamp it, unless the config marks
        this open as a --data-dir override (_stamp_on_open False): an
        explicit override must never bind a foreign database to the active
        profile. An existing mismatched stamp still hard-refuses.
        Active profile != stamp -> close the connection and raise.
        """
        expected = self._expected_profile
        if not expected:
            return
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'kin_profile'"
        ).fetchone()
        if row is None:
            if not getattr(self.config, "_stamp_on_open", True):
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('kin_profile', ?)",
                (expected,),
            )
            self._conn.commit()
            return
        stamped = row["value"]
        if stamped != expected:
            conn, self._conn = self._conn, None
            conn.close()
            raise ProfileMismatchError(
                f"Database {self.db_path} is stamped for profile '{stamped}' "
                f"but the active profile is '{expected}'"
            )

    def _init_schema(self) -> None:
        # Check if this is an existing database that needs migration
        # before applying the full schema (which includes triggers
        # referencing columns that may not exist yet).
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        )
        has_meta = cur.fetchone() is not None

        if has_meta:
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is not None:
                current = self._parse_schema_version(row["value"])
                if current > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Database schema version {current} is newer than "
                        f"this Kindex build supports ({SCHEMA_VERSION}). "
                        "Upgrade Kindex or restore a compatible database backup."
                    )
                if current < SCHEMA_VERSION:
                    with self._schema_migration_lock():
                        self._migrate_versioned_schema_after_lock()
                # An already-current store performs no DDL on reopen. An
                # upgraded store was fully verified inside its transaction.
                return
        elif self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone() is not None:
            # A daemon and foreground command can discover the same ancient
            # pre-versioning store concurrently. Lock and recheck before even
            # creating meta, just as the versioned path does.
            with self._schema_migration_lock():
                locked_has_meta = self._conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'meta'"
                ).fetchone()
                if locked_has_meta is not None:
                    self._migrate_versioned_schema_after_lock()
                    return

                snapshot, reason = self._snapshot_schema_migration(1)
                self._conn.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"
                )
                self._conn.execute(
                    "INSERT INTO meta (key, value) "
                    "VALUES ('schema_version', '1')"
                )
                self._conn.commit()
                self._record_schema_recovery_metadata(snapshot, reason)
                try:
                    self._migrate_schema(1)
                except Exception as exc:
                    raise SchemaMigrationError(
                        f"Schema migration {reason} failed ({exc}); "
                        f"recovery snapshot: {snapshot}"
                    ) from exc
                self._record_schema_migration_snapshot(snapshot, reason)
            return

        # Now safe to apply full schema (IF NOT EXISTS is idempotent
        # once columns are up to date).
        self._conn.executescript(CREATE_TABLES)

        # Ensure schema version is set for fresh databases.
        cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        if cur.fetchone() is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _parse_schema_version(self, value: object) -> int:
        """Parse a schema stamp with operator-facing recovery guidance."""
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise SchemaMigrationError(
                f"Database {self.db_path} has an invalid schema_version "
                f"value ({value!r}); restore a validated database snapshot"
            ) from exc

    def _migrate_versioned_schema_after_lock(self) -> None:
        """Recheck and, if still needed, migrate while exclusion is held."""
        # The optimistic read before the lock must never be reused here.
        # SELECT does not leave an implicit transaction in sqlite3's default
        # mode, and rollback makes that freshness precondition explicit.
        self._conn.rollback()
        locked_row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if locked_row is None:
            raise SchemaMigrationError(
                "Schema version disappeared while waiting for the migration "
                "lock; database was not migrated"
            )
        locked_current = self._parse_schema_version(locked_row["value"])
        if locked_current > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Database schema version {locked_current} is newer than this "
                f"Kindex build supports ({SCHEMA_VERSION}). Upgrade Kindex or "
                "restore a compatible database backup."
            )
        if locked_current == SCHEMA_VERSION:
            return

        snapshot, reason = self._snapshot_schema_migration(locked_current)
        self._record_schema_recovery_metadata(snapshot, reason)
        try:
            self._migrate_schema(locked_current)
        except Exception as exc:
            raise SchemaMigrationError(
                f"Schema migration {reason} failed ({exc}); recovery "
                f"snapshot: {snapshot}"
            ) from exc
        self._record_schema_migration_snapshot(snapshot, reason)

    @contextmanager
    def _schema_migration_lock(self):
        """Serialize snapshot-plus-migration with SQLite's own lock protocol.

        A dedicated rollback-journal database gives every supported platform
        the same crash-released mutex and the same local-filesystem assumptions
        as the primary Kindex database.  The caller must re-read
        ``schema_version`` after acquisition; that re-check closes the window
        between its optimistic first read and ownership of the migration.

        The lock database deliberately persists.  Unlinking a lock file while
        waiters hold its old inode would let a newcomer lock a different inode
        and enter concurrently.
        """
        lock_path = self.db_path.with_name(
            f".{self.db_path.name}.schema-migration-lock.sqlite3"
        )
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_conn: sqlite3.Connection | None = None
        try:
            fd = os.open(lock_path, flags, 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                else:  # pragma: no cover - Windows
                    os.chmod(lock_path, 0o600)
            finally:
                os.close(fd)
            lock_conn = sqlite3.connect(
                str(lock_path),
                timeout=self._sqlite_timeout,
                isolation_level=None,
            )
            lock_conn.execute(
                f"PRAGMA busy_timeout={int(self._sqlite_timeout * 1000)}"
            )
            lock_conn.execute("PRAGMA journal_mode=DELETE")
            lock_conn.execute(
                "CREATE TABLE IF NOT EXISTS migration_lock "
                "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1))"
            )
            lock_conn.execute("BEGIN EXCLUSIVE")
        except (OSError, sqlite3.Error) as exc:
            if lock_conn is not None:
                lock_conn.close()
            raise SchemaMigrationError(
                "Could not acquire the schema migration lock; stop older "
                f"Kindex processes and retry ({exc})"
            ) from exc
        try:
            yield
        finally:
            try:
                lock_conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            lock_conn.close()

    def _snapshot_schema_migration(
        self,
        current_version: int,
    ) -> tuple[Path, str]:
        """Create the recoverable pre-state before any schema mutation."""
        from .snapshots import snapshot_schema_migration

        try:
            return snapshot_schema_migration(
                self.db_path,
                self._conn,
                current_version,
                SCHEMA_VERSION,
            )
        except Exception as exc:
            raise SchemaMigrationError(
                f"Schema migration v{current_version} to v{SCHEMA_VERSION} "
                f"refused because its recovery snapshot failed ({exc}); "
                "the database was not migrated"
            ) from exc

    def _record_schema_recovery_metadata(
        self,
        path: Path,
        reason: str,
    ) -> None:
        """Persist the apology path before applying any schema mutation."""
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (
                    (SCHEMA_RECOVERY_PATH_META, str(path)),
                    (SCHEMA_RECOVERY_REASON_META, reason),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise SchemaMigrationError(
                f"Schema migration {reason} refused because its recovery "
                f"path could not be recorded ({exc}); recovery snapshot: {path}"
            ) from exc

    def _record_schema_migration_snapshot(
        self,
        path: Path,
        reason: str,
    ) -> None:
        """Record the migration recovery point after current tables exist."""
        from .snapshots import RESTORE_HINT

        # Some migration unit fixtures (and potentially hand-built legacy
        # stores) carry a version stamp without the earlier activity table.
        # The on-disk recovery point is still valid and discoverable in its
        # dedicated directory; normal released schemas always have this table.
        has_activity_log = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'activity_log'"
        ).fetchone()
        if has_activity_log is None:
            return

        try:
            self._log_in_transaction(
                self._conn,
                "db_snapshot",
                details={
                    "path": str(path),
                    "reason": reason,
                    "restore": RESTORE_HINT,
                },
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.warning(
                "schema migration completed but changelog recording failed "
                "(%s); recovery snapshot remains recorded in meta at %s",
                exc,
                path,
            )

    def _migrate_schema(self, current_version: int) -> None:
        """Apply incremental schema migrations. Uses self._conn directly
        to avoid triggering the conn property (which calls _init_schema)."""
        c = self._conn
        if current_version < 2:
            # v2: add audience column
            try:
                c.execute("ALTER TABLE nodes ADD COLUMN audience TEXT NOT NULL DEFAULT 'private'")
                c.execute("CREATE INDEX IF NOT EXISTS idx_nodes_audience ON nodes(audience)")
                c.commit()
            except Exception:
                pass  # column already exists

        if current_version < 3:
            # v3: add activity_log table
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS activity_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                        action TEXT NOT NULL,
                        target_id TEXT NOT NULL DEFAULT '',
                        target_title TEXT NOT NULL DEFAULT '',
                        actor TEXT NOT NULL DEFAULT '',
                        details TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_activity_action ON activity_log(action);
                """)
                c.commit()
            except Exception:
                pass

        if current_version < 4:
            # v4: add suggestions table
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        concept_a TEXT NOT NULL,
                        concept_b TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
                    CREATE INDEX IF NOT EXISTS idx_suggestions_status_created
                        ON suggestions(status, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_suggestions_status_pair
                        ON suggestions(status, concept_a, concept_b);
                """)
                c.commit()
            except Exception:
                pass

        if current_version < 5:
            # v5: add reminders table
            try:
                c.executescript("""
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
                """)
                c.commit()
            except Exception:
                pass

        if current_version < 6:
            # v6: index suggestions for scheduled dream workloads
            try:
                c.executescript("""
                    CREATE INDEX IF NOT EXISTS idx_suggestions_status_created
                        ON suggestions(status, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_suggestions_status_pair
                        ON suggestions(status, concept_a, concept_b);
                """)
                c.commit()
            except Exception:
                pass

        if current_version < 7:
            # v7: stigmergic injection pheromone (retrieval channel, not topology)
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS injection_pheromone (
                        node_id TEXT NOT NULL REFERENCES nodes(id),
                        context TEXT NOT NULL DEFAULT '',
                        strength REAL NOT NULL DEFAULT 0.0,
                        deposits INTEGER NOT NULL DEFAULT 0,
                        reinforcements INTEGER NOT NULL DEFAULT 0,
                        missed INTEGER NOT NULL DEFAULT 0,
                        last_deposit TEXT NOT NULL DEFAULT (datetime('now')),
                        last_decay TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (node_id, context)
                    );
                    CREATE INDEX IF NOT EXISTS idx_pheromone_node
                        ON injection_pheromone(node_id);
                    CREATE INDEX IF NOT EXISTS idx_pheromone_strength
                        ON injection_pheromone(strength DESC);
                """)
                c.commit()
            except Exception:
                pass

        if current_version < 8:
            self._migrate_v8()

        if current_version < 9:
            self._migrate_v9()

        if current_version < 10:
            self._migrate_v10()

        if current_version < 11:
            self._migrate_v11()

        if current_version < 12:
            self._migrate_v12()

    def _migrate_v8(self) -> None:
        """Atomically upgrade a version-7 store to the state-resilience schema.

        Every v8 mutation, metadata verification, and version-stamp write shares
        one ``BEGIN IMMEDIATE`` transaction. ``BaseException`` is intentional:
        injected failures and cancellation must roll back just as reliably as a
        normal SQLite error.
        """
        c = self._conn
        step_index = 0

        def execute(label: str, sql: str, params: tuple = ()) -> sqlite3.Cursor:
            nonlocal step_index
            if self._migration_step_hook is not None:
                self._migration_step_hook(step_index, label)
            step_index += 1
            return c.execute(sql, params)

        c.execute("BEGIN IMMEDIATE")
        try:
            execute("nodes.add_verified_at", "ALTER TABLE nodes ADD COLUMN verified_at TEXT")
            execute("nodes.add_verified_by", "ALTER TABLE nodes ADD COLUMN verified_by TEXT")
            execute("nodes.add_prov_method", "ALTER TABLE nodes ADD COLUMN prov_method TEXT")
            execute("nodes.add_valid_at", "ALTER TABLE nodes ADD COLUMN valid_at TEXT")
            execute("nodes.add_invalid_at", "ALTER TABLE nodes ADD COLUMN invalid_at TEXT")
            execute(
                "edges.add_updated_at",
                "ALTER TABLE edges ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
            )
            execute(
                "edges.backfill_updated_at",
                "UPDATE edges SET updated_at = created_at WHERE updated_at = ''",
            )
            execute(
                "suggestions.add_kind",
                "ALTER TABLE suggestions ADD COLUMN kind TEXT NOT NULL DEFAULT 'bridge'",
            )
            execute(
                "suggestions.backfill_kind",
                "UPDATE suggestions SET kind = 'bridge' WHERE kind IS NULL OR kind = ''",
            )
            execute(
                "candidates.create_table",
                """CREATE TABLE capture_candidates (
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
                )""",
            )
            execute(
                "candidates.index_status_created",
                "CREATE INDEX idx_capture_candidates_status_created "
                "ON capture_candidates(status, created_at DESC)",
            )
            execute(
                "candidates.index_status_expires",
                "CREATE INDEX idx_capture_candidates_status_expires "
                "ON capture_candidates(status, expires_at)",
            )
            execute(
                "candidates.index_live_payload",
                "CREATE UNIQUE INDEX idx_capture_candidates_live_payload "
                "ON capture_candidates(payload_digest) "
                "WHERE status IN ('pending', 'conflicted')",
            )

            node_cols = {
                row["name"] for row in execute(
                    "verify.nodes_columns", "PRAGMA table_info(nodes)"
                ).fetchall()
            }
            if not {"verified_at", "verified_by", "prov_method", "valid_at", "invalid_at"} <= node_cols:
                raise RuntimeError("v8 migration verification failed: node trust columns")
            edge_cols = {
                row["name"] for row in execute(
                    "verify.edges_columns", "PRAGMA table_info(edges)"
                ).fetchall()
            }
            if "updated_at" not in edge_cols:
                raise RuntimeError("v8 migration verification failed: edge clock")
            suggestion_cols = {
                row["name"] for row in execute(
                    "verify.suggestions_columns", "PRAGMA table_info(suggestions)"
                ).fetchall()
            }
            if "kind" not in suggestion_cols:
                raise RuntimeError("v8 migration verification failed: suggestion kind")
            candidate_table = execute(
                "verify.candidates_table",
                "SELECT name FROM sqlite_master WHERE type='table' AND name='capture_candidates'",
            ).fetchone()
            if candidate_table is None:
                raise RuntimeError("v8 migration verification failed: candidate table")
            index_rows = execute(
                "verify.candidate_indexes",
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='capture_candidates'",
            ).fetchall()
            indexes = {row["name"] for row in index_rows}
            required_indexes = {
                "idx_capture_candidates_status_created",
                "idx_capture_candidates_status_expires",
                "idx_capture_candidates_live_payload",
            }
            if not required_indexes <= indexes:
                raise RuntimeError("v8 migration verification failed: candidate indexes")

            execute(
                "meta.stamp_version_8",
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                # Literal "8", not str(SCHEMA_VERSION): this migration brings a
                # store TO version 8; stamping the code's current version here
                # would mark later migrations (v9+) as applied before they run.
                # Byte-identical to what every already-migrated DB received
                # (SCHEMA_VERSION was 8 when this block last changed).
                ("8",),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def _migrate_v9(self) -> None:
        """Referent binding + two clocks (PRD lineage-grounding R0).

        Adds nullable ``referent`` (JSON), ``asserted_at``, ``true_of`` to
        nodes. Atomic: mutations, verification, and the version stamp share
        one BEGIN IMMEDIATE transaction (v8 pattern); ``BaseException`` so
        cancellation rolls back as reliably as a SQLite error.
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN referent TEXT")
            c.execute("ALTER TABLE nodes ADD COLUMN asserted_at TEXT")
            c.execute("ALTER TABLE nodes ADD COLUMN true_of TEXT")
            node_cols = {
                row["name"]
                for row in c.execute("PRAGMA table_info(nodes)").fetchall()
            }
            if not {"referent", "asserted_at", "true_of"} <= node_cols:
                raise RuntimeError(
                    "v9 migration verification failed: referent columns"
                )
            c.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                ("9",),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def _migrate_v10(self) -> None:
        """Repair ``injection_pheromone.missed`` on stores that ran v7 early.

        The ``missed`` column (counterfactual deposits) was added to the v7
        block's ``CREATE TABLE IF NOT EXISTS`` after v7 had already shipped.
        For a store that ran v7 before that edit, the CREATE is a no-op and the
        column never arrives — while ``schema_version`` still reads current, so
        nothing flags it. ``deposit_pheromone`` then raises ``no such column:
        missed`` on every call, the attention hook swallows it, and the whole
        stigmergic channel is silently dead. Adding a column is the only way to
        reach those stores.

        Idempotent: stores created after that edit already have the column, so
        a duplicate-column error is the success case, not a failure. Atomic and
        verified against ``PRAGMA table_info`` (v8/v9 pattern); ``BaseException``
        so cancellation rolls back as reliably as a SQLite error.
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            has_table = c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='injection_pheromone'"
            ).fetchone() is not None
            if has_table:
                cols = {
                    row["name"]
                    for row in c.execute(
                        "PRAGMA table_info(injection_pheromone)").fetchall()
                }
                if "missed" not in cols:
                    c.execute(
                        "ALTER TABLE injection_pheromone "
                        "ADD COLUMN missed INTEGER NOT NULL DEFAULT 0"
                    )
                cols = {
                    row["name"]
                    for row in c.execute(
                        "PRAGMA table_info(injection_pheromone)").fetchall()
                }
                if "missed" not in cols:
                    raise RuntimeError(
                        "v10 migration verification failed: "
                        "injection_pheromone.missed"
                    )
            c.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                ("10",),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def _migrate_v11(self) -> None:
        """Learned pair co-activation channel (W4).

        Creates ``node_coactivation`` — a third retrieval channel, separate
        from both ``edges.weight`` (asserted topology) and node-level
        ``injection_pheromone``. Atomic and verified against
        ``sqlite_master``/``PRAGMA table_info`` (v8-v10 pattern), and the
        column check is what v10 exists to teach: a table present with the
        wrong shape is invisible to an existence check.
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executescript("""
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
                CREATE INDEX IF NOT EXISTS idx_coactivation_a
                    ON node_coactivation(node_a);
                CREATE INDEX IF NOT EXISTS idx_coactivation_b
                    ON node_coactivation(node_b);
                CREATE INDEX IF NOT EXISTS idx_coactivation_strength
                    ON node_coactivation(strength DESC);
            """)
            cols = {
                row["name"]
                for row in c.execute(
                    "PRAGMA table_info(node_coactivation)").fetchall()
            }
            required = {"node_a", "node_b", "context", "strength", "events",
                        "last_event", "last_decay"}
            if not required <= cols:
                raise RuntimeError(
                    "v11 migration verification failed: node_coactivation "
                    f"missing {sorted(required - cols)}")
            c.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                ("11",),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def _migrate_v12(self) -> None:
        """Make active session-tag identity deterministic and race-safe.

        Existing duplicate active rows are preserved as paused history. The
        most recently updated row remains active, then a partial unique index
        makes another duplicate for the same normalized tag and project
        unrepresentable.
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            from .sessions import normalize_project_path

            node_columns = {
                row["name"]
                for row in c.execute("PRAGMA table_info(nodes)").fetchall()
            }
            updated_expr = "updated_at" if "updated_at" in node_columns else "''"
            created_expr = "created_at" if "created_at" in node_columns else "''"
            rows = c.execute(
                f"""SELECT id, extra,
                           {updated_expr} AS updated_at,
                           {created_expr} AS created_at
                     FROM nodes
                     WHERE type = 'session'
                       AND json_valid(extra)
                       AND json_extract(extra, '$.tag') IS NOT NULL
                     ORDER BY updated_at DESC, created_at DESC, id DESC"""
            ).fetchall()
            active_keys: set[tuple[str, str]] = set()
            paused_at = _now()
            for row in rows:
                extra = json.loads(row["extra"])
                original_project = str(extra.get("project_path") or "")
                canonical_project = normalize_project_path(original_project)
                changed = canonical_project != original_project
                extra["project_path"] = canonical_project
                if extra.get("session_status") != "active":
                    if changed:
                        c.execute(
                            "UPDATE nodes SET extra = ? WHERE id = ?",
                            (_jdumps(extra), row["id"]),
                        )
                    continue
                key = (
                    str(extra.get("tag") or ""),
                    canonical_project,
                )
                if key not in active_keys:
                    active_keys.add(key)
                else:
                    extra["session_status"] = "paused"
                    extra["paused_at"] = extra.get("paused_at") or paused_at
                    extra["paused_reason"] = (
                        SESSION_PAUSE_REASON_DUPLICATE_MIGRATION
                    )
                    changed = True
                if changed:
                    c.execute(
                        "UPDATE nodes SET extra = ? WHERE id = ?",
                        (_jdumps(extra), row["id"]),
                    )

            c.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_session_active_tag_project
                       ON nodes (
                           json_extract(extra, '$.tag'),
                           COALESCE(json_extract(extra, '$.project_path'), '')
                       )
                     WHERE type = 'session'
                       AND json_valid(extra)
                       AND json_extract(extra, '$.session_status') = 'active'"""
            )
            has_suggestions = c.execute(
                """SELECT 1 FROM sqlite_master
                     WHERE type = 'table' AND name = 'suggestions'"""
            ).fetchone()
            if has_suggestions is not None:
                suggestion_columns = {
                    row["name"]
                    for row in c.execute(
                        "PRAGMA table_info(suggestions)"
                    ).fetchall()
                }
                if "identity_kind" not in suggestion_columns:
                    c.execute(
                        "ALTER TABLE suggestions ADD COLUMN identity_kind "
                        "TEXT NOT NULL DEFAULT 'title' "
                        "CHECK (identity_kind IN ('title', 'node_id'))"
                    )
                id_source_placeholders = ",".join(
                    "?" for _ in NODE_ID_SUGGESTION_SOURCES
                )
                c.execute(
                    "UPDATE suggestions SET identity_kind = 'node_id' "
                    f"WHERE source IN ({id_source_placeholders})",
                    tuple(sorted(NODE_ID_SUGGESTION_SOURCES)),
                )
                c.execute(
                    """CREATE INDEX IF NOT EXISTS idx_suggestions_pair
                           ON suggestions(concept_a, concept_b)"""
                )
            duplicate = c.execute(
                """SELECT 1 FROM nodes
                     WHERE type = 'session'
                       AND json_valid(extra)
                       AND json_extract(extra, '$.session_status') = 'active'
                     GROUP BY json_extract(extra, '$.tag'),
                              COALESCE(json_extract(extra, '$.project_path'), '')
                    HAVING COUNT(*) > 1
                     LIMIT 1"""
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError(
                    "v12 migration verification failed: duplicate active session tags"
                )
            index = c.execute(
                """SELECT 1 FROM sqlite_master
                     WHERE type = 'index'
                       AND name = 'idx_session_active_tag_project'"""
            ).fetchone()
            if index is None:
                raise RuntimeError(
                    "v12 migration verification failed: active session index"
                )
            if has_suggestions is not None:
                suggestion_columns = {
                    row["name"]
                    for row in c.execute(
                        "PRAGMA table_info(suggestions)"
                    ).fetchall()
                }
                if "identity_kind" not in suggestion_columns:
                    raise RuntimeError(
                        "v12 migration verification failed: suggestion "
                        "identity kind"
                    )
                suggestion_index = c.execute(
                    """SELECT 1 FROM sqlite_master
                         WHERE type = 'index'
                           AND name = 'idx_suggestions_pair'"""
                ).fetchone()
                if suggestion_index is None:
                    raise RuntimeError(
                        "v12 migration verification failed: suggestion pair index"
                    )
            c.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                ("12",),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    # Columns each table must carry for the code that queries it to work.
    # Checked by `kin doctor`, which is the only place that catches the failure
    # class v10 repairs: a table that exists with the right name and the wrong
    # shape, on a store whose schema_version reads current. Table-existence
    # checks are blind to it — every migration verifier here asserts columns.
    REQUIRED_COLUMNS: dict[str, set[str]] = {
        "injection_pheromone": {
            "node_id", "context", "strength", "deposits",
            "reinforcements", "missed", "last_deposit", "last_decay",
        },
        "nodes": {
            "id", "title", "content", "type", "weight", "status",
            "audience", "referent", "asserted_at", "true_of",
        },
        "edges": {"from_id", "to_id", "type", "weight"},
        "capture_candidates": {"payload_digest", "status"},
        "suggestions": {"concept_a", "concept_b", "identity_kind"},
        "node_coactivation": {
            "node_a", "node_b", "context", "strength",
            "events", "last_event", "last_decay",
        },
    }

    def schema_drift(self) -> dict[str, set[str]]:
        """Report columns the code requires that this store is missing.

        Returns ``{table: {missing columns}}`` — empty when the store's shape
        matches what the code queries. A table absent altogether is not drift
        (a migration will create it); a table present with missing columns is,
        because ``CREATE TABLE IF NOT EXISTS`` will never repair it.
        """
        drift: dict[str, set[str]] = {}
        for table, required in self.REQUIRED_COLUMNS.items():
            try:
                exists = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                cols = {
                    row["name"]
                    for row in self.conn.execute(
                        f"PRAGMA table_info({table})").fetchall()
                }
                missing = required - cols
                if missing:
                    drift[table] = missing
            except Exception:
                continue
        return drift

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Activity logging ─────────────────────────────────────────────

    def _log(self, action: str, target_id: str = "", target_title: str = "",
             actor: str = "", details: dict | None = None) -> None:
        """Record an action in the activity log."""
        try:
            self._log_in_transaction(
                self.conn, action, target_id, target_title, actor, details
            )
            self.conn.commit()
        except Exception:
            pass  # don't let logging break operations

    @staticmethod
    def _log_in_transaction(
        conn: sqlite3.Connection,
        action: str,
        target_id: str = "",
        target_title: str = "",
        actor: str = "",
        details: dict | None = None,
    ) -> None:
        """Write an activity row without committing or swallowing failures."""
        conn.execute(
            """INSERT INTO activity_log
               (timestamp, action, target_id, target_title, actor, details)
               VALUES (datetime('now'), ?, ?, ?, ?, ?)""",
            (
                redact_text(action),
                redact_text(target_id),
                redact_text(target_title),
                redact_text(actor),
                _jdumps(details or {}),
            ),
        )

    def recent_activity(self, limit: int = 50) -> list[dict]:
        """Get recent activity log entries."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("details"), str):
                    try:
                        d["details"] = json.loads(d["details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception:
            return []

    # ── Temporal queries ───────────────────────────────────────────────

    def activity_since(self, since_iso: str, action: str | None = None) -> list[dict]:
        """Get activity log entries since a timestamp, optionally filtered by action type."""
        try:
            q = "SELECT * FROM activity_log WHERE timestamp >= ? "
            params: list = [since_iso]
            if action:
                q += "AND action = ? "
                params.append(action)
            q += "ORDER BY timestamp DESC"
            rows = self.conn.execute(q, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("details"), str):
                    try:
                        d["details"] = json.loads(d["details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception:
            return []

    def nodes_changed_since(self, since_iso: str) -> list[dict]:
        """Get nodes that were updated since a timestamp."""
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE updated_at >= ? ORDER BY updated_at DESC",
            (since_iso,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def activity_by_actor(self, actor: str, limit: int = 50) -> list[dict]:
        """Get activity by a specific actor."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM activity_log WHERE actor = ? ORDER BY timestamp DESC LIMIT ?",
                (actor, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("details"), str):
                    try:
                        d["details"] = json.loads(d["details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception:
            return []

    # ── Suggestions ───────────────────────────────────────────────────

    def add_suggestion(
        self,
        concept_a: str,
        concept_b: str,
        reason: str = "",
        source: str = "",
        *,
        identity_kind: str = "title",
    ) -> int:
        """Add a bridge opportunity suggestion. Returns the suggestion ID."""
        if identity_kind not in SUGGESTION_IDENTITY_KINDS:
            raise ValueError(
                "Suggestion identity_kind must be one of: "
                + ", ".join(SUGGESTION_IDENTITY_KINDS)
            )
        if identity_kind == "node_id":
            missing = [
                value
                for value in (concept_a, concept_b)
                if self.get_node(value) is None
            ]
            if missing:
                raise ValueError(
                    "Suggestion node_id endpoint(s) do not exist: "
                    + ", ".join(missing)
                )
        concept_a, concept_b, reason, source = (
            redact_text(value) for value in (concept_a, concept_b, reason, source)
        )
        cur = self.conn.execute(
            """INSERT INTO suggestions
               (concept_a, concept_b, reason, source, identity_kind, kind)
               VALUES (?, ?, ?, ?, ?, 'bridge')""",
            (concept_a, concept_b, reason, source, identity_kind),
        )
        self.conn.commit()
        self._log("add_suggestion", f"{concept_a}->{concept_b}", "",
                  details={"reason": reason, "source": source})
        return cur.lastrowid

    def pending_suggestions(self, limit: int = 20) -> list[dict]:
        """Get pending suggestions (bridge opportunities)."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM suggestions WHERE status = 'pending' AND kind = 'bridge' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def resolve_suggestion_node(
        self,
        value: str,
        identity_kind: str,
    ) -> dict | None:
        """Resolve one endpoint from the identity contract stored on its row."""
        if identity_kind == "node_id":
            return self.get_node(value)
        if identity_kind != "title":
            raise ValueError(
                f"Unsupported suggestion identity kind: {identity_kind!r}"
            )

        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE lower(title) = lower(?) "
            "ORDER BY id LIMIT 2",
            (value,),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(
                f"Suggestion endpoint title is ambiguous: {value!r}"
            )
        if rows:
            return self._row_to_dict(rows[0])

        alias_rows = self.conn.execute(
            """SELECT DISTINCT n.*
                 FROM nodes n,
                      json_each(
                          CASE WHEN json_valid(n.aka) THEN n.aka ELSE '[]' END
                      ) alias
                WHERE lower(CAST(alias.value AS TEXT)) = lower(?)
                ORDER BY n.id
                LIMIT 2""",
            (value,),
        ).fetchall()
        if len(alias_rows) > 1:
            raise ValueError(
                f"Suggestion endpoint alias is ambiguous: {value!r}"
            )
        return self._row_to_dict(alias_rows[0]) if alias_rows else None

    def suggestion_exists(
        self,
        concept_a: str,
        concept_b: str,
        *,
        status: str | None = "pending",
    ) -> bool:
        """Return true if a suggestion exists for this pair in either order."""
        status_clause = "status = ? AND " if status is not None else ""
        params: tuple[Any, ...]
        if status is None:
            params = (concept_a, concept_b, concept_b, concept_a)
        else:
            params = (status, concept_a, concept_b, status, concept_b, concept_a)
        row = self.conn.execute(
            f"""
            SELECT 1 FROM suggestions
             WHERE {status_clause}kind = 'bridge'
               AND concept_a = ? AND concept_b = ?
            UNION ALL
            SELECT 1 FROM suggestions
             WHERE {status_clause}kind = 'bridge'
               AND concept_a = ? AND concept_b = ?
            LIMIT 1
            """,
            params,
        ).fetchone()
        return row is not None

    def update_suggestion(self, suggestion_id: int, status: str) -> None:
        """Update suggestion status (accepted/rejected)."""
        self.conn.execute(
            "UPDATE suggestions SET status = ? WHERE id = ?",
            (status, suggestion_id),
        )
        self.conn.commit()
        self._log("update_suggestion", str(suggestion_id), "",
                  details={"status": status})

    # ── Node operations ────────────────────────────────────────────────

    def add_node(
        self,
        title: str,
        content: str = "",
        *,
        node_id: str | None = None,
        node_type: str = "concept",
        aka: list[str] | None = None,
        intent: str = "",
        domains: list[str] | None = None,
        tags: list[str] | None = None,
        status: str = "active",
        audience: str = "private",
        weight: float = 0.5,
        prov_who: list[str] | None = None,
        prov_when: str | None = None,
        prov_activity: str = "",
        prov_why: str = "",
        prov_source: str = "",
        extra: dict | None = None,
        referent: dict | None = None,
        asserted_at: str | None = None,
        true_of: str | None = None,
    ) -> str:
        """Insert a node. Returns its ID.

        ``referent``/``asserted_at``/``true_of`` bind the claim to the external
        thing it describes (R0): the referent shape is validated, the two
        clocks are normalized RFC 3339 UTC, ``asserted_at`` defaults to now
        when a binding is supplied, and ``true_of`` defaults to ``asserted_at``.
        The store performs no file IO — digests are computed by callers.
        """
        # Merge user-supplied tags into domains (supplement, never replace)
        if tags:
            domains = list(set((domains or []) + tags))
        title, content, intent, prov_activity, prov_why, prov_source = (
            redact_text(value) for value in
            (title, content, intent, prov_activity, prov_why, prov_source)
        )
        prov_who = redact(prov_who)
        referent_json, asserted_norm, true_of_norm = _normalize_binding(
            referent, asserted_at, true_of
        )
        nid = node_id or _uuid()
        if redact_text(nid) != nid:
            raise ValueError("Node IDs must not contain recognizable credentials")
        now = _now()
        when = prov_when or now
        self.conn.execute(
            """INSERT INTO nodes
               (id, type, title, content, aka, intent,
                prov_who, prov_when, prov_activity, prov_why, prov_source,
                weight, domains, status, audience,
                created_at, updated_at, last_accessed, extra,
                referent, asserted_at, true_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   type = excluded.type,
                   title = excluded.title,
                   content = excluded.content,
                   aka = excluded.aka,
                   intent = excluded.intent,
                   prov_who = excluded.prov_who,
                   prov_when = excluded.prov_when,
                   prov_activity = excluded.prov_activity,
                   prov_why = excluded.prov_why,
                   prov_source = excluded.prov_source,
                   weight = excluded.weight,
                   domains = excluded.domains,
                   status = excluded.status,
                   audience = excluded.audience,
                   created_at = excluded.created_at,
                   updated_at = excluded.updated_at,
                   last_accessed = excluded.last_accessed,
                   extra = excluded.extra,
                   referent = excluded.referent,
                   asserted_at = excluded.asserted_at,
                   true_of = excluded.true_of""",
            (nid, node_type, title, content,
             _jdumps(aka or []), intent,
             _jdumps(prov_who or []), when, prov_activity, prov_why, prov_source,
             weight, _jdumps(domains or []), status, audience,
             now, now, now, _jdumps(extra or {}),
             referent_json, asserted_norm, true_of_norm),
        )
        self.conn.commit()
        actor = (prov_who or [""])[0] if prov_who else ""
        self._log("add_node", nid, title, actor,
                  {"type": node_type, "activity": prov_activity})

        # Auto-create person nodes from prov_who entries
        if prov_who and node_type != "person" and prov_activity not in ("auto-created", ""):
            for person_name in prov_who:
                if not person_name:
                    continue
                existing_person = self.get_node_by_title(person_name)
                if existing_person is None:
                    existing_person = self.get_node(person_name)
                if existing_person is None:
                    self.add_node(
                        person_name,
                        node_type="person",
                        prov_activity="auto-created",
                        prov_why=f"Referenced in prov_who of '{title}'",
                    )
                    existing_person = self.get_node_by_title(person_name)
                if existing_person:
                    self.add_edge(nid, existing_person["id"],
                                  edge_type="context_of",
                                  weight=0.4,
                                  provenance="auto-linked from prov_who",
                                  bidirectional=False)

        # Queue for vector embedding — deferred to the daemon so a slow
        # embedding provider never stalls the add hot path (best-effort).
        try:
            from .vectors import enqueue_embedding
            enqueue_embedding(self, nid)
        except Exception:
            pass  # vectors not installed — node still created

        return nid

    def get_node(self, node_id: str) -> dict | None:
        """Fetch a node by ID, updating last_accessed."""
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE nodes SET last_accessed = ? WHERE id = ?", (_now(), node_id))
        self.conn.commit()
        return self._row_to_dict(row)

    def get_node_domains(self, node_id: str) -> list[str]:
        """Read a node's domains/tags without touching last_accessed (non-mutating).

        Used on hot paths (e.g. prime) that only need a node's client scope and
        must not bump the freshness-decay signal or commit per lookup.
        """
        if not node_id:
            return []
        row = self.conn.execute(
            "SELECT domains FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row or not row[0]:
            return []
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []

    def get_node_by_title(self, title: str) -> dict | None:
        """Match by title or AKA (case-insensitive)."""
        # Exact title match
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE lower(title) = lower(?)", (title,)).fetchone()
        if row:
            return self._row_to_dict(row)
        # AKA match: search JSON array for alias
        lower = title.lower()
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE aka != '[]' AND aka != ''").fetchall()
        for r in rows:
            d = self._row_to_dict(r)
            if any(a.lower() == lower for a in (d.get("aka") or [])):
                return d
        return None

    def update_node(self, node_id: str, _log_activity: bool = True,
                    **fields) -> None:
        """Update specific fields on a node.

        ``_log_activity`` is private: internal callers that write their own
        richer activity entry (edit_node) pass False to avoid double-logging
        the same change. External semantics are unchanged.
        """
        allowed = {"title", "content", "aka", "intent", "weight", "domains",
                   "tags", "status", "audience", "prov_who", "prov_activity",
                   "prov_why", "prov_source", "extra"}
        # Handle tags -> domains alias (tags supplement, never replace)
        if "tags" in fields:
            tag_vals = fields.pop("tags")
            if isinstance(tag_vals, list):
                existing = fields.get("domains") or []
                fields["domains"] = list(set(existing + tag_vals))
        updates = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            v = redact(v)
            if isinstance(v, (list, dict)):
                v = _jdumps(v)
            updates[k] = v

        if not updates:
            return

        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [node_id]
        self.conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?", vals)
        self.conn.commit()
        if _log_activity:
            self._log("update_node", node_id, "",
                      details={"fields": list(fields.keys())})

    def delete_node(self, node_id: str) -> None:
        # Capture title before deletion for logging
        row = self.conn.execute("SELECT title FROM nodes WHERE id = ?", (node_id,)).fetchone()
        title = row["title"] if row else node_id
        self.conn.execute("DELETE FROM edges WHERE from_id = ? OR to_id = ?",
                          (node_id, node_id))
        self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        self.conn.commit()
        # Drop the vector embedding too (best-effort — table may not exist)
        try:
            from .vectors import delete_embedding
            delete_embedding(self, node_id)
        except Exception:
            pass
        self._log("delete_node", node_id, title)

    def all_nodes(self, node_type: str | None = None,
                  status: str | None = None,
                  audience: str | None = None,
                  tags: list[str] | None = None,
                  exclude_types: tuple[str, ...] = (),
                  limit: int = 500) -> list[dict]:
        """List nodes with optional type/status/audience/tags filters."""
        q = "SELECT * FROM nodes WHERE 1=1"
        params: list = []
        if node_type:
            q += " AND type = ?"
            params.append(node_type)
        if status:
            q += " AND status = ?"
            params.append(status)
        if audience:
            q += " AND audience = ?"
            params.append(audience)
        if tags:
            for tag in tags:
                q += " AND domains LIKE ?"
                params.append(f'%"{tag}"%')
        if exclude_types:
            placeholders = ",".join("?" for _ in exclude_types)
            q += f" AND type NOT IN ({placeholders})"
            params.extend(exclude_types)
        q += " ORDER BY weight DESC, updated_at DESC, id ASC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def recent_nodes(self, n: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM nodes ORDER BY updated_at DESC LIMIT ?", (n,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def nodes_with_expiry(self, status: str = "active",
                          limit: int = 1000) -> list[dict]:
        """Nodes carrying extra['expires'] (cheap LIKE prefilter).

        The `"expires"` pattern (quote-delimited) deliberately does not match
        `"expires_at"` lock timestamps. Callers confirm with node_expired().
        """
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE status = ? AND extra LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (status, '%"expires"%', limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Edit / supersede / atomic extra ─────────────────────────────────

    def _check_lock(self, node: dict, actor: str | None, force: bool,
                    *, remedy: str = "pass force to override") -> None:
        """Raise LockHeldError on a foreign unexpired lock unless force.

        `remedy` is the actionable hint appended to the error — callers
        whose surface has no force flag (e.g. supersede) pass one that
        names a remedy that actually exists on that surface.
        """
        if force:
            return
        lock = active_lock(node)
        if lock is None:
            return
        holder = lock.get("agent") or ""
        if actor is not None and holder == actor:
            return
        until = f" until {lock['expires_at']}" if lock.get("expires_at") else ""
        raise LockHeldError(
            f"Node {node.get('id')} is locked by '{holder or 'unknown'}'{until} "
            f"— {remedy}"
        )

    @staticmethod
    def _check_mutable_status(node: dict, force: bool) -> None:
        """Refuse edit/supersede on dead nodes.

        - superseded: always refused (force does NOT bypass) — the error
          names the successor so the caller can retarget it.
        - archived: refused unless force.
        """
        status = node.get("status") or "active"
        if status == "superseded":
            successor = (node.get("extra") or {}).get("superseded_by") or "unknown"
            raise EditPolicyError(
                f"Node {node.get('id')} was superseded by {successor} "
                f"— operate on that node instead"
            )
        if status == "archived" and not force:
            raise EditPolicyError(
                f"Node {node.get('id')} is archived — pass force to modify it"
            )

    def edit_node(
        self,
        node_id: str,
        actor: str | None = None,
        force: bool = False,
        policy_overrides: dict[str, str] | None = None,
        *,
        title: str | None = None,
        content: str | None = None,
        append: str | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        intent: str | None = None,
        aka: list[str] | None = None,
        expires: str | None = None,
    ) -> dict:
        """Policy-aware node edit. Routes through update_node (UPDATE only).

        - editable types: all fields allowed
        - additive types: only append + expires (replacement -> supersede_node)
        - managed types: always refused (task/session/coordination tooling owns them)
        Refuses a foreign unexpired lock unless force. Logs per-field old/new
        diffs to the activity log. Reserved extra keys are never altered.
        Returns the updated node dict.
        """
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"No node with id '{node_id}'")

        provided = {
            name: val for name, val in (
                ("title", title), ("content", content), ("append", append),
                ("add_tags", add_tags), ("remove_tags", remove_tags),
                ("intent", intent), ("aka", aka), ("expires", expires),
            ) if val is not None
        }
        if not provided:
            raise ValueError("edit_node requires at least one field to change")

        node_type = node.get("type", "concept")
        cls = edit_class_for(node_type, policy_overrides)
        if cls == "managed":
            raise EditPolicyError(
                f"Node type '{node_type}' is managed — edit is not allowed; "
                f"use the dedicated task/session/coordination tools"
            )
        if cls == "additive":
            disallowed = sorted(set(provided) - {"append", "expires"})
            if disallowed:
                raise EditPolicyError(
                    f"Node type '{node_type}' is additive — only append and "
                    f"expires are allowed (got: {', '.join(disallowed)}); "
                    f"use supersede to replace it"
                )

        self._check_mutable_status(node, force)
        self._check_lock(node, actor, force)
        if expires is not None:
            _validate_expires(expires)

        updates: dict[str, Any] = {}
        diffs: dict[str, dict] = {}

        old_title = node.get("title") or ""
        new_title = old_title
        if title is not None and title != old_title:
            updates["title"] = title
            diffs["title"] = {"old": _trunc(old_title), "new": _trunc(title)}
            new_title = title

        old_content = node.get("content") or ""
        new_content = content if content is not None else old_content
        if append:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            header = f"[addendum {stamp} {actor}]" if actor else f"[addendum {stamp}]"
            prefix = f"{new_content}\n\n" if new_content else ""
            new_content = f"{prefix}{header}\n{append}"
        if new_content != old_content:
            updates["content"] = new_content
            diffs["content"] = {"old": _trunc(old_content), "new": _trunc(new_content)}

        if intent is not None and intent != (node.get("intent") or ""):
            updates["intent"] = intent
            diffs["intent"] = {"old": _trunc(node.get("intent") or ""),
                               "new": _trunc(intent)}

        if aka is not None:
            old_aka = list(node.get("aka") or [])
            if list(aka) != old_aka:
                updates["aka"] = list(aka)
                diffs["aka"] = {"old": _trunc(old_aka), "new": _trunc(list(aka))}

        if "title" in updates and old_title:
            # A rename keeps the old title reachable: preserve it as an alias
            # so title-keyed dedup (session capture, inbox ingest, dream_deep
            # idempotency) still matches via get_node_by_title's AKA path.
            base_aka = list(updates.get("aka", node.get("aka") or []))
            lowered = {a.lower() for a in base_aka}
            if (old_title.lower() != new_title.lower()
                    and old_title.lower() not in lowered):
                preserved = base_aka + [old_title]
                updates["aka"] = preserved
                diffs["aka"] = {"old": _trunc(list(node.get("aka") or [])),
                                "new": _trunc(preserved)}

        if add_tags or remove_tags:
            old_domains = list(node.get("domains") or [])
            removed = set(remove_tags or [])
            new_domains = [d for d in old_domains if d not in removed]
            for t in add_tags or []:
                if t not in new_domains:
                    new_domains.append(t)
            if new_domains != old_domains:
                updates["domains"] = new_domains
                diffs["tags"] = {"old": _trunc(old_domains), "new": _trunc(new_domains)}

        exp_diff: dict[str, Any] = {}
        if expires is not None:
            # The expires merge must not write the extra column from the
            # pre-check snapshot: a lock (or any extra key) committed by a
            # concurrent agent between get_node above and the write would be
            # silently erased. Route it through atomic_extra_update and
            # re-check the lock against the fresh in-transaction state —
            # LockHeldError raised here propagates and rolls back.
            def _set_expires(fresh: dict) -> None:
                self._check_lock({"id": node_id, "extra": fresh}, actor, force)
                old_expires = fresh.get("expires")
                if expires != old_expires:
                    fresh["expires"] = expires
                    exp_diff["old"] = old_expires
                    exp_diff["new"] = expires

            self.atomic_extra_update(node_id, _set_expires)
            if exp_diff:
                diffs["expires"] = {"old": exp_diff["old"],
                                    "new": exp_diff["new"]}

        if updates:
            # _log_activity=False: edit_node writes its own (richer) entry
            # below — without it every edit appears twice in the changelog.
            self.update_node(node_id, _log_activity=False, **updates)
        if updates or exp_diff:
            self._log("edit_node", node_id, updates.get("title", old_title),
                      actor or "", {"diffs": diffs, "type": node_type})
            if "title" in updates or "content" in updates:
                # Queue re-embedding — deferred to the daemon so a slow
                # embedding provider never stalls the edit hot path
                # (best-effort, like add_node).
                try:
                    from .vectors import enqueue_embedding
                    enqueue_embedding(self, node_id)
                except Exception:
                    pass  # vectors not installed — edit persisted

        return self.get_node(node_id)

    def supersede_node(
        self,
        node_id: str,
        new_text: str,
        actor: str | None = None,
        expires: str | None = None,
        reason: str | None = None,
        force: bool = False,
        policy_overrides: dict[str, str] | None = None,
    ) -> dict:
        """Replace a node with a fresh one, preserving history.

        Single transaction: insert the new node (same type/tags/audience/
        intent, fresh id), edge new-[supersedes]->old, mark the old node
        status='superseded' with extra['superseded_by'], and migrate its
        injection_pheromone trails to the new node. Lock check as edit_node.
        Managed types (task/session/coordination) are refused like edit_node
        — their tooling owns their state. Superseded nodes cannot be
        superseded again (the error names the successor); archived nodes
        require force. Returns the new node dict.
        """
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"No node with id '{node_id}'")
        node_type = node.get("type", "concept")
        cls = edit_class_for(node_type, policy_overrides)
        if cls == "managed":
            raise EditPolicyError(
                f"Node type '{node_type}' is managed — supersede is not "
                f"allowed; use the dedicated task/session/coordination tools"
            )
        self._check_mutable_status(node, force)
        self._check_lock(node, actor, force, remedy=_SUPERSEDE_LOCK_REMEDY)
        text = redact_text((new_text or "").strip())
        actor = redact_text(actor) if actor is not None else None
        reason = redact_text(reason) if reason is not None else None
        if not text:
            raise ValueError("supersede_node requires non-empty new_text")
        if expires is not None:
            _validate_expires(expires)

        new_id = _uuid()
        now = _now()
        title = text[:60].strip()  # same title convention as MCP add
        new_extra: dict[str, Any] = {"supersedes": node_id}
        if expires:
            new_extra["expires"] = expires
        if reason:
            new_extra["supersede_reason"] = reason

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-verify status inside the transaction: two concurrent
            # supersedes serialize on BEGIN IMMEDIATE, so the second one
            # must see the first's status flip and abort instead of
            # creating a competing successor.
            row = conn.execute(
                "SELECT status, extra FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No node with id '{node_id}'")
            try:
                cur_extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                cur_extra = {}
            if not isinstance(cur_extra, dict):
                cur_extra = {}
            cur_status = row["status"] or "active"
            if cur_status == "superseded":
                successor = cur_extra.get("superseded_by") or "unknown"
                raise EditPolicyError(
                    f"Node {node_id} was superseded by {successor} "
                    f"— operate on that node instead"
                )
            if cur_status == "archived" and not force:
                raise EditPolicyError(
                    f"Node {node_id} is archived — pass force to modify it"
                )
            # Re-check the lock against the fresh in-transaction extra: a
            # lock acquired between the pre-flight snapshot and BEGIN
            # IMMEDIATE must still block the supersede (LockHeldError
            # propagates to the rollback handler below).
            self._check_lock({"id": node_id, "extra": cur_extra}, actor, force,
                             remedy=_SUPERSEDE_LOCK_REMEDY)
            old_extra = dict(cur_extra)
            old_extra["superseded_by"] = new_id
            conn.execute(
                """INSERT INTO nodes
                   (id, type, title, content, aka, intent,
                    prov_who, prov_when, prov_activity, prov_why, prov_source,
                    weight, domains, status, audience,
                    created_at, updated_at, last_accessed, extra)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id, node.get("type", "concept"), title, text,
                 _jdumps([]), redact_text(node.get("intent") or ""),
                 _jdumps([actor] if actor else []), now, "supersede",
                 reason or redact_text(f"Supersedes '{node.get('title', '')}'"), node_id,
                 node.get("weight", 0.5), _jdumps(node.get("domains") or []),
                 "active", node.get("audience", "private"),
                 now, now, now, _jdumps(new_extra)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO edges (from_id, to_id, type, weight, provenance) "
                "VALUES (?, ?, 'supersedes', 0.9, ?)",
                (new_id, node_id,
                 f"superseded by {actor}" if actor else "superseded"),
            )
            conn.execute(
                "UPDATE nodes SET status = 'superseded', extra = ?, updated_at = ? "
                "WHERE id = ?",
                (_jdumps(old_extra), now, node_id),
            )
            conn.execute(
                "UPDATE OR REPLACE injection_pheromone SET node_id = ? "
                "WHERE node_id = ?",
                (new_id, node_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        self._log("supersede_node", new_id, title, actor or "",
                  {"superseded": node_id, "reason": reason or ""})

        # Drop the old node's embedding so vector search stops surfacing the
        # superseded text (best-effort — table may not exist).
        try:
            from .vectors import delete_embedding
            delete_embedding(self, node_id)
        except Exception:
            pass

        # Queue the replacement for embedding — deferred to the daemon
        # (best-effort, like add_node).
        try:
            from .vectors import enqueue_embedding
            enqueue_embedding(self, new_id)
        except Exception:
            pass  # vectors not installed — node still created

        return self.get_node(new_id)

    def atomic_extra_update(
        self, node_id: str, mutator: Callable[[dict], dict | None]
    ) -> dict:
        """Atomically read-modify-write a node's extra JSON.

        BEGIN IMMEDIATE serializes concurrent writers (no lost updates across
        Store handles). The mutator may return a replacement dict or mutate
        its argument in place and return None. Returns the final extra dict.
        Raises KeyError on a missing node.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT extra FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No node with id '{node_id}'")
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            replacement = mutator(extra)
            final = redact(extra if replacement is None else replacement)
            conn.execute(
                "UPDATE nodes SET extra = ?, updated_at = ? WHERE id = ?",
                (_jdumps(final), _now(), node_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return final

    def atomic_archive_expired(self, node_id: str, expired_at: str) -> bool:
        """Archive a node iff its fresh extra['expires'] is still past.

        BEGIN IMMEDIATE re-reads the row, so an expiry extended (or removed)
        after the caller's snapshot wins: the node is left untouched and
        False is returned. Only active nodes are archived; the
        extra['expired_at'] stamp and the status flip land in the same
        UPDATE. Returns True when the node was archived.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, extra FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            fresh_extra: dict | None = None
            if row is not None and (row["status"] or "active") == "active":
                try:
                    parsed = json.loads(row["extra"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
                if isinstance(parsed, dict) and node_expired({"extra": parsed}):
                    fresh_extra = parsed
            if fresh_extra is None:
                conn.rollback()
                return False
            fresh_extra["expired_at"] = expired_at
            conn.execute(
                "UPDATE nodes SET status = 'archived', extra = ?, "
                "updated_at = ? WHERE id = ?",
                (_jdumps(fresh_extra), _now(), node_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return True

    # ── Verification and valid-time operations ─────────────────────────

    def verify_node(
        self,
        node_id: str,
        *,
        verified_by: str,
        prov_method: str,
        verified_at: str | None = None,
        valid_at: str | None = None,
        invalid_at: str | None = None,
    ) -> dict:
        """Record an asserted verification without rewriting provenance time."""
        from .trust import normalize_rfc3339, validate_interval

        reviewer = _clean_audit_text(verified_by, field="verified_by")
        method = _clean_audit_text(prov_method, field="prov_method")
        verified = normalize_rfc3339(
            verified_at or _utc_now(), field="verified_at"
        )
        valid, invalid = validate_interval(valid_at, invalid_at)

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, title FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Node not found: {node_id}")
            updated = _utc_now()
            conn.execute(
                """UPDATE nodes
                      SET verified_at = ?, verified_by = ?, prov_method = ?,
                          valid_at = ?, invalid_at = ?, updated_at = ?
                    WHERE id = ?""",
                (verified, reviewer, method, valid, invalid, updated, node_id),
            )
            self._log_in_transaction(
                conn,
                "verify_node",
                node_id,
                row["title"],
                reviewer,
                {
                    "method": method,
                    "verified_at": verified,
                    "valid_at": valid,
                    "invalid_at": invalid,
                },
            )
            result_row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self._row_to_dict(result_row)

    def bind_referent(
        self,
        node_id: str,
        referent: dict,
        *,
        asserted_at: str | None = None,
        true_of: str | None = None,
    ) -> dict:
        """(Re)bind a node's referent and clocks; clear any stale marker.

        A deliberate, logged act (R0): rebinding asserts the claim is true of
        the referent's current recorded state. ``asserted_at`` keeps the
        node's existing claim time by default (a rebind re-observes the
        referent, it does not re-date the claim); ``true_of`` defaults to now
        (the new observation instant). Node content is never touched.
        """
        from .referent import validate_referent
        from .trust import normalize_rfc3339

        validated = validate_referent(referent)
        if redact(validated) != validated:
            raise ValueError("Referent contains credentials; remove them before binding")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Node not found: {node_id}")
            now = _utc_now()
            asserted = normalize_rfc3339(
                asserted_at or row["asserted_at"] or now, field="asserted_at"
            )
            observed = normalize_rfc3339(true_of or now, field="true_of")
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            stale_cleared = extra.pop("referent_stale", None) is not None
            conn.execute(
                """UPDATE nodes
                      SET referent = ?, asserted_at = ?, true_of = ?,
                          extra = ?, updated_at = ?
                    WHERE id = ?""",
                (_jdumps(validated), asserted, observed,
                 _jdumps(extra), _now(), node_id),
            )
            self._log_in_transaction(
                conn, "bind_referent", node_id, row["title"], "",
                {
                    "digest": validated.get("content_digest"),
                    "scope": validated.get("digest_scope"),
                    "true_of": observed,
                    "stale_cleared": stale_cleared,
                },
            )
            result_row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self._row_to_dict(result_row)

    def invalidate_node(
        self,
        node_id: str,
        *,
        invalidated_by: str,
        disposition_code: str,
        invalid_at: str | None = None,
    ) -> dict:
        """Record an exclusive valid-time end without deleting the node."""
        from .trust import normalize_rfc3339, validate_interval

        actor = _clean_audit_text(invalidated_by, field="invalidated_by")
        code = _clean_audit_text(disposition_code, field="disposition_code")
        invalid = normalize_rfc3339(
            invalid_at or _utc_now(), field="invalid_at"
        )

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, title, valid_at FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Node not found: {node_id}")
            _, invalid = validate_interval(row["valid_at"], invalid)
            updated = _utc_now()
            conn.execute(
                "UPDATE nodes SET invalid_at = ?, updated_at = ? WHERE id = ?",
                (invalid, updated, node_id),
            )
            self._log_in_transaction(
                conn,
                "invalidate_node",
                node_id,
                row["title"],
                actor,
                {"disposition_code": code, "invalid_at": invalid},
            )
            result_row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self._row_to_dict(result_row)

    # ── Automatic-capture candidate state ──────────────────────────────

    @staticmethod
    def _candidate_to_dict(row: sqlite3.Row | dict) -> dict:
        candidate = dict(row)
        for key in ("domains", "connections", "conflict_ids", "conflict_codes"):
            value = candidate.get(key)
            if isinstance(value, str):
                try:
                    candidate[key] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    candidate[key] = []
        return candidate

    @staticmethod
    def _canonical_connections(connections: list[dict] | None) -> list[dict]:
        if connections is not None and not isinstance(connections, list):
            raise ValueError("connections must be a list")
        canonical: list[dict] = []
        for index, proposal in enumerate(connections or []):
            if not isinstance(proposal, dict):
                raise ValueError(f"connections[{index}] must be an object")
            from_title = _clean_capture_text(
                proposal.get("from_title", ""),
                field=f"connections[{index}].from_title",
                limit=_CAPTURE_TITLE_LIMIT,
            )
            to_title = _clean_capture_text(
                proposal.get("to_title", ""),
                field=f"connections[{index}].to_title",
                limit=_CAPTURE_TITLE_LIMIT,
            )
            edge_type = _clean_capture_text(
                proposal.get("type", "relates_to"),
                field=f"connections[{index}].type",
                limit=64,
            )
            if edge_type not in EDGE_TYPES:
                raise ValueError(f"connections[{index}].type is not an allowed edge type")
            why_raw = proposal.get("why", "")
            if not isinstance(why_raw, str):
                raise ValueError(f"connections[{index}].why must be text")
            why = why_raw.strip()
            if len(why) > _CAPTURE_WHY_LIMIT:
                raise ValueError(
                    f"connections[{index}].why must be at most {_CAPTURE_WHY_LIMIT} characters"
                )
            if _ANY_CONTROL_RE.search(why):
                raise ValueError(
                    f"connections[{index}].why must not contain control characters"
                )
            canonical.append(
                {
                    "from_title": from_title,
                    "to_title": to_title,
                    "type": edge_type,
                    "why": redact_text(why),
                }
            )
        # Connection ordering is not semantic. Sort and deduplicate so replayed
        # extraction produces the same review subject and payload digest.
        return [
            dict(items)
            for items in sorted(
                {tuple(sorted(item.items())) for item in canonical},
                key=lambda item: tuple(value for _, value in item),
            )
        ]

    def add_capture_candidate(
        self,
        *,
        title: str,
        content: str,
        node_type: str = "concept",
        domains: list[str] | None = None,
        connections: list[dict] | None = None,
        source_digest: str,
        now: str | None = None,
        ttl_days: int | None = None,
    ) -> str:
        """Atomically stage one automatic extraction for human/agent review."""
        from .trust import normalize_rfc3339, parse_rfc3339

        clean_title = _clean_capture_text(
            title, field="title", limit=_CAPTURE_TITLE_LIMIT
        )
        clean_content = _clean_capture_text(
            content,
            field="content",
            limit=_CAPTURE_CONTENT_LIMIT,
            content=True,
        )
        if node_type not in ALL_NODE_TYPES:
            raise ValueError(f"node_type is not allowed: {node_type}")
        if domains is not None and not isinstance(domains, list):
            raise ValueError("domains must be a list")
        clean_domains: list[str] = []
        for index, domain in enumerate(domains or []):
            clean_domains.append(
                _clean_capture_text(
                    domain,
                    field=f"domains[{index}]",
                    limit=_AUDIT_TEXT_LIMIT,
                )
            )
        clean_domains = sorted(set(clean_domains))
        clean_connections = self._canonical_connections(connections)
        if not isinstance(source_digest, str):
            raise ValueError("source_digest must be a SHA-256 hex digest")
        clean_source_digest = source_digest.strip().lower()
        if not _SHA256_RE.fullmatch(clean_source_digest):
            raise ValueError("source_digest must be a SHA-256 hex digest")

        days = ttl_days
        if days is None:
            days = getattr(getattr(self.config, "capture", None), "candidate_ttl_days", 7)
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("ttl_days must be a positive integer")
        created = normalize_rfc3339(now or _utc_now(), field="now")
        expires_dt = parse_rfc3339(created, field="now") + timedelta(days=days)
        expires = normalize_rfc3339(expires_dt, field="expires_at")
        payload = {
            "title": clean_title,
            "content": clean_content,
            "node_type": node_type,
            "domains": clean_domains,
            "connections": clean_connections,
        }
        payload_digest = hashlib.sha256(
            _canonical_dumps(payload).encode("utf-8")
        ).hexdigest()
        candidate_id = _uuid()

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                """SELECT id FROM capture_candidates
                    WHERE payload_digest = ? AND status IN ('pending', 'conflicted')""",
                (payload_digest,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return existing["id"]
            conn.execute(
                """INSERT INTO capture_candidates
                   (id, title, content, node_type, domains, connections,
                    source_digest, payload_digest, status, created_at,
                    updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    candidate_id,
                    clean_title,
                    clean_content,
                    node_type,
                    _canonical_dumps(clean_domains),
                    _canonical_dumps(clean_connections),
                    clean_source_digest,
                    payload_digest,
                    created,
                    created,
                    expires,
                ),
            )
            self._log_in_transaction(
                conn,
                "stage_capture_candidate",
                candidate_id,
                "",
                "",
                {
                    "source_digest": clean_source_digest,
                    "payload_digest": payload_digest,
                    "status": "pending",
                },
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return candidate_id

    def list_capture_candidates(
        self,
        *,
        status: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """List candidate receipts without exposing untrusted payload fields."""
        if status and status not in _ALL_CANDIDATE_STATUSES:
            raise ValueError(f"Unknown candidate status: {status}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        fields = (
            "id, source_digest, payload_digest, status, created_at, updated_at, "
            "expires_at, reviewed_at, reviewed_by, review_method, disposition_code, "
            "conflict_ids, conflict_codes, created_node_id"
        )
        if status:
            rows = self.conn.execute(
                f"SELECT {fields} FROM capture_candidates WHERE status = ? "
                "ORDER BY created_at DESC, id LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT {fields} FROM capture_candidates "
                "ORDER BY created_at DESC, id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._candidate_to_dict(row) for row in rows]

    def get_capture_candidate(self, candidate_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return self._candidate_to_dict(row) if row is not None else None

    @staticmethod
    def _node_review_state(row: sqlite3.Row | dict) -> dict:
        node = dict(row)
        return {
            key: node.get(key)
            for key in (
                "id",
                "status",
                "updated_at",
                "verified_at",
                "verified_by",
                "prov_method",
                "valid_at",
                "invalid_at",
            )
        }

    @staticmethod
    def _rows_same_title(
        conn: sqlite3.Connection,
        title: str,
    ) -> list[sqlite3.Row]:
        target = title.casefold()
        return [
            row
            for row in conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
            if (row["title"] or "").casefold() == target
        ]

    @staticmethod
    def _rows_matching_title(
        conn: sqlite3.Connection,
        title: str,
    ) -> list[sqlite3.Row]:
        """Resolve exact title or AKA matches deterministically without writes."""
        lower = title.casefold()
        matches: list[sqlite3.Row] = []
        rows = conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
        for row in rows:
            if (row["title"] or "").casefold() == lower:
                matches.append(row)
                continue
            try:
                aliases = json.loads(row["aka"] or "[]")
            except (json.JSONDecodeError, TypeError):
                aliases = []
            if any(isinstance(alias, str) and alias.casefold() == lower for alias in aliases):
                matches.append(row)
        return matches

    def _candidate_review_token_locked(
        self,
        conn: sqlite3.Connection,
        candidate_row: sqlite3.Row | dict,
    ) -> str:
        candidate = self._candidate_to_dict(candidate_row)
        same_title = self._rows_same_title(conn, candidate.get("title") or "")
        referenced: dict[str, sqlite3.Row] = {}
        for proposal in candidate.get("connections") or []:
            for key in ("from_title", "to_title"):
                title = proposal.get(key)
                if not title:
                    continue
                for row in self._rows_matching_title(conn, title):
                    referenced[row["id"]] = row

        relevant: dict[str, sqlite3.Row] = {row["id"]: row for row in same_title}
        relevant.update(referenced)
        relevant_ids = set(relevant)
        contradiction_rows: dict[int, dict] = {}
        for node_id in sorted(relevant_ids):
            for edge in conn.execute(
                """SELECT id, from_id, to_id, type, created_at, updated_at
                     FROM edges WHERE type = 'contradicts' AND from_id = ?""",
                (node_id,),
            ).fetchall():
                if edge["to_id"] in relevant_ids:
                    contradiction_rows[edge["id"]] = dict(edge)

        subject = {
            "candidate": {
                key: candidate.get(key)
                for key in ("id", "payload_digest", "status", "updated_at", "expires_at")
            },
            "same_title_nodes": [
                self._node_review_state(row)
                for row in sorted(same_title, key=lambda item: item["id"])
            ],
            "referenced_nodes": [
                self._node_review_state(referenced[node_id])
                for node_id in sorted(referenced)
            ],
            "contradictions": [
                contradiction_rows[edge_id] for edge_id in sorted(contradiction_rows)
            ],
        }
        return hashlib.sha256(_canonical_dumps(subject).encode("utf-8")).hexdigest()

    def candidate_review_token(self, candidate_id: str) -> str:
        row = self.conn.execute(
            "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise CandidateNotFoundError(f"Candidate not found: {candidate_id}")
        return self._candidate_review_token_locked(self.conn, row)

    @staticmethod
    def _resolve_candidate_endpoint(
        conn: sqlite3.Connection,
        title: str,
        *,
        candidate_title: str,
        created_node_id: str,
    ) -> str | None:
        if title.casefold() == candidate_title.casefold():
            return created_node_id
        matches = Store._rows_matching_title(conn, title)
        # An ambiguous title is not safely resolvable. Reviewers can
        # disambiguate a future candidate instead of Kindex guessing.
        return matches[0]["id"] if len(matches) == 1 else None

    def accept_capture_candidate(
        self,
        candidate_id: str,
        *,
        review_token: str,
        reviewed_by: str,
        prov_method: str,
        valid_at: str | None = None,
        invalid_at: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Atomically promote one fresh, conflict-free candidate."""
        from .trust import (
            _base_trust_decision,
            normalize_rfc3339,
            parse_rfc3339,
            validate_interval,
        )

        reviewer = _clean_audit_text(reviewed_by, field="reviewed_by")
        method = _clean_audit_text(prov_method, field="prov_method")
        reviewed = normalize_rfc3339(now or _utc_now(), field="now")
        evaluation_time = parse_rfc3339(reviewed, field="now")
        valid, invalid = validate_interval(valid_at, invalid_at)
        if not isinstance(review_token, str) or not review_token.strip():
            raise StaleReviewError("A current review token is required")

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise CandidateNotFoundError(f"Candidate not found: {candidate_id}")
            candidate = self._candidate_to_dict(row)
            payload = {key: candidate.get(key) for key in
                       ("title", "content", "node_type", "domains", "connections")}
            if redact(payload) != payload:
                # Historical candidates retain their exact review-bound bytes.
                # Do not silently rewrite one under an already-issued token.
                raise CandidateStateError(
                    "Candidate contains credentials; reject and restage sanitized content"
                )
            status = candidate.get("status")
            if status not in _LIVE_CANDIDATE_STATUSES:
                raise CandidateStateError(f"Candidate is already terminal: {status}")
            if parse_rfc3339(candidate["expires_at"], field="expires_at") <= evaluation_time:
                raise CandidateStateError("Candidate is expired")

            current_token = self._candidate_review_token_locked(conn, row)
            if not hmac.compare_digest(review_token.strip(), current_token):
                raise StaleReviewError("Candidate or relevant graph state changed")

            same_title = self._rows_same_title(conn, candidate["title"])
            if same_title:
                raise TitleCollisionError(
                    "A durable node already uses this title; disambiguate the candidate title"
                )

            # Explicit contradiction proposals are blocked only by endpoints
            # that independently pass status, verification, and valid time.
            conflict_ids: set[str] = set()
            for proposal in candidate.get("connections") or []:
                if proposal.get("type") != "contradicts":
                    continue
                from_title = proposal.get("from_title", "")
                to_title = proposal.get("to_title", "")
                candidate_lower = candidate["title"].casefold()
                if from_title.casefold() == candidate_lower:
                    counterpart = to_title
                elif to_title.casefold() == candidate_lower:
                    counterpart = from_title
                else:
                    continue
                for counterpart_row in self._rows_matching_title(conn, counterpart):
                    counterpart_node = self._row_to_dict(counterpart_row)
                    if _base_trust_decision(
                        counterpart_node, at=evaluation_time
                    ).eligible:
                        conflict_ids.add(counterpart_node["id"])

            if conflict_ids:
                ids = sorted(conflict_ids)
                codes = ["explicit_current_contradiction"]
                changed = conn.execute(
                    """UPDATE capture_candidates
                          SET status = 'conflicted', updated_at = ?, reviewed_at = ?,
                              reviewed_by = ?, review_method = ?,
                              disposition_code = 'explicit_contradiction',
                              conflict_ids = ?, conflict_codes = ?
                        WHERE id = ? AND status IN ('pending', 'conflicted')
                          AND expires_at > ?""",
                    (
                        reviewed,
                        reviewed,
                        reviewer,
                        method,
                        _canonical_dumps(ids),
                        _canonical_dumps(codes),
                        candidate_id,
                        reviewed,
                    ),
                )
                if changed.rowcount != 1:
                    raise CandidateStateError("Candidate changed during conflict review")
                self._log_in_transaction(
                    conn,
                    "conflict_capture_candidate",
                    candidate_id,
                    "",
                    reviewer,
                    {"conflict_ids": ids, "conflict_codes": codes},
                )
                result_row = conn.execute(
                    "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
                ).fetchone()
                conn.commit()
                return self._candidate_to_dict(result_row)

            node_id = _uuid()
            conn.execute(
                """INSERT INTO nodes
                   (id, type, title, content, aka, intent,
                    prov_who, prov_when, prov_activity, prov_why, prov_source,
                    verified_at, verified_by, prov_method, valid_at, invalid_at,
                    weight, domains, status, audience,
                    created_at, updated_at, last_accessed, extra)
                   VALUES (?, ?, ?, ?, '[]', '', ?, ?, 'capture-review', ?, ?,
                           ?, ?, ?, ?, ?, 0.5, ?, 'active', 'private', ?, ?, ?, ?)""",
                (
                    node_id,
                    candidate["node_type"],
                    candidate["title"],
                    candidate["content"],
                    _canonical_dumps([reviewer]),
                    candidate["created_at"],
                    f"Accepted automatic capture candidate {candidate_id}",
                    candidate["source_digest"],
                    reviewed,
                    reviewer,
                    method,
                    valid,
                    invalid,
                    _canonical_dumps(candidate.get("domains") or []),
                    reviewed,
                    reviewed,
                    reviewed,
                    _canonical_dumps(
                        {
                            "capture_candidate_id": candidate_id,
                            "payload_digest": candidate["payload_digest"],
                            "redaction_policy": POLICY_VERSION,
                        }
                    ),
                ),
            )

            inserted_edges: list[tuple[str, str, str]] = []
            for proposal in candidate.get("connections") or []:
                from_id = self._resolve_candidate_endpoint(
                    conn,
                    proposal["from_title"],
                    candidate_title=candidate["title"],
                    created_node_id=node_id,
                )
                to_id = self._resolve_candidate_endpoint(
                    conn,
                    proposal["to_title"],
                    candidate_title=candidate["title"],
                    created_node_id=node_id,
                )
                if from_id is None or to_id is None:
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO edges
                       (from_id, to_id, type, weight, provenance, updated_at)
                       VALUES (?, ?, ?, 0.5, ?, ?)""",
                    (
                        from_id,
                        to_id,
                        proposal["type"],
                        proposal.get("why", ""),
                        reviewed,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted_edges.append((from_id, to_id, proposal["type"]))

            terminal = conn.execute(
                """UPDATE capture_candidates
                      SET title = NULL, content = NULL, node_type = NULL,
                          domains = NULL, connections = NULL,
                          status = 'accepted', updated_at = ?, reviewed_at = ?,
                          reviewed_by = ?, review_method = ?,
                          disposition_code = 'accepted', conflict_ids = '[]',
                          conflict_codes = '[]', created_node_id = ?
                    WHERE id = ? AND status IN ('pending', 'conflicted')
                      AND expires_at > ?""",
                (
                    reviewed,
                    reviewed,
                    reviewer,
                    method,
                    node_id,
                    candidate_id,
                    reviewed,
                ),
            )
            if terminal.rowcount != 1:
                raise CandidateStateError("Candidate changed before promotion committed")

            self._log_in_transaction(
                conn,
                "add_node",
                node_id,
                candidate["title"],
                reviewer,
                {"type": candidate["node_type"], "activity": "capture-review"},
            )
            for from_id, to_id, edge_type in inserted_edges:
                self._log_in_transaction(
                    conn,
                    "add_edge",
                    f"{from_id}->{to_id}",
                    "",
                    reviewer,
                    {"type": edge_type, "source_candidate_id": candidate_id},
                )
            self._log_in_transaction(
                conn,
                "accept_capture_candidate",
                candidate_id,
                "",
                reviewer,
                {
                    "disposition_code": "accepted",
                    "created_node_id": node_id,
                    "payload_digest": candidate["payload_digest"],
                },
            )
            result_row = conn.execute(
                "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        try:
            from .vectors import enqueue_embedding

            enqueue_embedding(self, node_id)
        except Exception:
            pass
        return self._candidate_to_dict(result_row)

    def reject_capture_candidate(
        self,
        candidate_id: str,
        *,
        reviewed_by: str,
        disposition_code: str,
        now: str | None = None,
    ) -> dict:
        """Atomically reject and minimize a live candidate."""
        from .trust import normalize_rfc3339, parse_rfc3339

        reviewer = _clean_audit_text(reviewed_by, field="reviewed_by")
        code = _clean_audit_text(disposition_code, field="disposition_code")
        reviewed = normalize_rfc3339(now or _utc_now(), field="now")
        evaluation_time = parse_rfc3339(reviewed, field="now")

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, expires_at FROM capture_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise CandidateNotFoundError(f"Candidate not found: {candidate_id}")
            if row["status"] not in _LIVE_CANDIDATE_STATUSES:
                raise CandidateStateError(
                    f"Candidate is already terminal: {row['status']}"
                )
            if parse_rfc3339(row["expires_at"], field="expires_at") <= evaluation_time:
                raise CandidateStateError("Candidate is expired")
            changed = conn.execute(
                """UPDATE capture_candidates
                      SET title = NULL, content = NULL, node_type = NULL,
                          domains = NULL, connections = NULL,
                          status = 'rejected', updated_at = ?, reviewed_at = ?,
                          reviewed_by = ?, review_method = NULL,
                          disposition_code = ?, created_node_id = NULL
                    WHERE id = ? AND status IN ('pending', 'conflicted')
                      AND expires_at > ?""",
                (reviewed, reviewed, reviewer, code, candidate_id, reviewed),
            )
            if changed.rowcount != 1:
                raise CandidateStateError("Candidate changed before rejection committed")
            self._log_in_transaction(
                conn,
                "reject_capture_candidate",
                candidate_id,
                "",
                reviewer,
                {"disposition_code": code},
            )
            result_row = conn.execute(
                "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self._candidate_to_dict(result_row)

    def prune_capture_candidates(self, *, now: str | None = None) -> int:
        """Expire all live candidates whose exclusive expiry has been reached."""
        from .trust import normalize_rfc3339, parse_rfc3339

        pruned_at = normalize_rfc3339(now or _utc_now(), field="now")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            evaluation_time = parse_rfc3339(pruned_at, field="now")
            rows = conn.execute(
                """SELECT id, expires_at FROM capture_candidates
                    WHERE status IN ('pending', 'conflicted')
                    ORDER BY id"""
            ).fetchall()
            due = [
                row for row in rows
                if parse_rfc3339(row["expires_at"], field="expires_at")
                <= evaluation_time
            ]
            for row in due:
                changed = conn.execute(
                    """UPDATE capture_candidates
                          SET title = NULL, content = NULL, node_type = NULL,
                              domains = NULL, connections = NULL,
                              status = 'expired', updated_at = ?, reviewed_at = ?,
                              disposition_code = 'expired', created_node_id = NULL
                        WHERE id = ? AND status IN ('pending', 'conflicted')
                          AND expires_at = ?""",
                    (pruned_at, pruned_at, row["id"], row["expires_at"]),
                )
                if changed.rowcount != 1:
                    raise CandidateStateError("Candidate set changed during prune")
                self._log_in_transaction(
                    conn,
                    "expire_capture_candidate",
                    row["id"],
                    "",
                    "",
                    {"disposition_code": "expired"},
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return len(due)

    def erase_capture_candidate(self, candidate_id: str) -> bool:
        """Delete a candidate or minimized receipt by exact ID."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM capture_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            conn.execute("DELETE FROM capture_candidates WHERE id = ?", (candidate_id,))
            self._log_in_transaction(
                conn,
                "erase_capture_candidate",
                candidate_id,
                "",
                "",
                {"status": row["status"]},
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return True

    # ── Edge operations ────────────────────────────────────────────────

    def add_edge(self, from_id: str, to_id: str, edge_type: str = "relates_to",
                 weight: float = 0.5, provenance: str = "",
                 bidirectional: bool = True) -> None:
        """Add an edge. Bidirectional by default (enforces graph invariant)."""
        provenance = redact_text(provenance)
        now = _utc_now()
        self.conn.execute(
            """INSERT OR REPLACE INTO edges
               (from_id, to_id, type, weight, provenance, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (from_id, to_id, edge_type, weight, provenance, now),
        )
        if bidirectional:
            self.conn.execute(
                """INSERT OR IGNORE INTO edges
                   (from_id, to_id, type, weight, provenance, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (to_id, from_id, edge_type, weight * 0.8, provenance, now),
            )
        self.conn.commit()
        self._log("add_edge", f"{from_id}->{to_id}", "",
                  details={"type": edge_type, "weight": weight})

    def expand_multihop(
        self,
        seeds: dict[str, float],
        *,
        max_hops: int = 2,
        hop_decay: float = 0.5,
        beam: int = 200,
    ) -> list[tuple[str, float]]:
        """Walk `max_hops` out from weighted seeds, returning (node_id, score).

        Replaces a hand-rolled single-hop loop. Depth was the real limit, not
        speed: measured on a 113,355-edge graph, a 3-hop recursive CTE runs in
        the same wall-clock as 1 hop (both inside sqlite3 process-startup
        noise), so reach was being left on the table for no gain.

        Two properties matter more than the depth:

        * **The beam is mandatory.** Max observed out-fanout is 849, so an
          uncapped 3-hop walk from a hub explodes.
        * **The beam ordering is total and stable** — score desc, then node id
          asc. A beam filled by whatever SQLite happened to return first would
          make traversal nondeterministic: adding one edge anywhere in a hub's
          neighbourhood would silently change what a 2-hop query returns, with
          no changelog entry and no way to explain it. For a graph whose value
          is auditable provenance, a nondeterministic walk is a worse defect
          than a slow one.

        Seeds are excluded from the result — the caller already has them. Each
        node keeps its BEST score across all paths that reach it, and score
        attenuates by ``hop_decay`` per hop so a 2-hop neighbour cannot
        outrank a 1-hop one on edge weight alone.
        """
        if not seeds or max_hops < 1:
            return []

        best: dict[str, float] = {}
        frontier: dict[str, float] = dict(seeds)
        seen: set[str] = set(seeds)

        for _ in range(max_hops):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            try:
                rows = self.conn.execute(
                    f"""SELECT e.from_id, e.to_id, e.weight FROM edges e
                          JOIN nodes source ON source.id = e.from_id
                          JOIN nodes target ON target.id = e.to_id
                         WHERE e.from_id IN ({placeholders})
                           AND {_SEMANTIC_EDGE_SQL}""",
                    (*frontier, *_SEMANTIC_EDGE_PARAMS),
                ).fetchall()
            except Exception:
                break

            next_scores: dict[str, float] = {}
            for row in rows:
                try:
                    target = row["to_id"]
                    if target in seen:
                        continue
                    score = frontier[row["from_id"]] * (row["weight"] or 0.0) * hop_decay
                except (KeyError, TypeError):
                    continue
                if score > next_scores.get(target, 0.0):
                    next_scores[target] = score

            # Total, stable ordering — see the docstring. Ties break on id so
            # the beam contents cannot depend on row order.
            ranked = sorted(next_scores.items(), key=lambda kv: (-kv[1], kv[0]))
            frontier = dict(ranked[:beam])
            seen.update(frontier)
            for nid, score in frontier.items():
                if score > best.get(nid, 0.0):
                    best[nid] = score

        return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))

    def edges_from(self, node_id: str, *, semantic_only: bool = False) -> list[dict]:
        q = """SELECT e.*, target.title as to_title FROM edges e
                 JOIN nodes source ON source.id = e.from_id
                 JOIN nodes target ON target.id = e.to_id
                WHERE e.from_id = ?"""
        params: list[Any] = [node_id]
        if semantic_only:
            q += f" AND {_SEMANTIC_EDGE_SQL}"
            params.extend(_SEMANTIC_EDGE_PARAMS)
        q += " ORDER BY e.weight DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def edges_to(self, node_id: str, *, semantic_only: bool = False) -> list[dict]:
        q = """SELECT e.*, source.title as from_title FROM edges e
                 JOIN nodes source ON source.id = e.from_id
                 JOIN nodes target ON target.id = e.to_id
                WHERE e.to_id = ?"""
        params: list[Any] = [node_id]
        if semantic_only:
            q += f" AND {_SEMANTIC_EDGE_SQL}"
            params.extend(_SEMANTIC_EDGE_PARAMS)
        q += " ORDER BY e.weight DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def orphans(self) -> list[dict]:
        """Semantic nodes with no semantic edges."""
        rows = self.conn.execute(
            f"""SELECT n.* FROM nodes n
                  WHERE n.type NOT IN ({_SEMANTIC_NODE_PLACEHOLDERS})
                    AND NOT EXISTS (
                        SELECT 1 FROM edges e
                        JOIN nodes source ON source.id = e.from_id
                        JOIN nodes target ON target.id = e.to_id
                        WHERE e.from_id = n.id
                          AND {_SEMANTIC_EDGE_SQL}
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM edges e
                        JOIN nodes source ON source.id = e.from_id
                        JOIN nodes target ON target.id = e.to_id
                        WHERE e.to_id = n.id
                          AND {_SEMANTIC_EDGE_SQL}
                    )""",
            (
                *SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
                *_SEMANTIC_EDGE_PARAMS,
                *_SEMANTIC_EDGE_PARAMS,
            ),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def graph_edge_counts(self) -> dict[str, int]:
        """Count stored and semantic edges without conflating derived topology."""
        stored = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        domain = self.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE provenance = ?",
            (LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,),
        ).fetchone()[0]
        semantic = self.conn.execute(
            f"""SELECT COUNT(*) FROM edges e
                  JOIN nodes source ON source.id = e.from_id
                  JOIN nodes target ON target.id = e.to_id
                 WHERE {_SEMANTIC_EDGE_SQL}""",
            _SEMANTIC_EDGE_PARAMS,
        ).fetchone()[0]
        session = self.conn.execute(
            f"""SELECT COUNT(*) FROM edges e
                  JOIN nodes source ON source.id = e.from_id
                  JOIN nodes target ON target.id = e.to_id
                 WHERE {_NON_DOMAIN_EDGE_SQL}
                   AND (
                       source.type IN ({_SEMANTIC_NODE_PLACEHOLDERS})
                       OR target.type IN ({_SEMANTIC_NODE_PLACEHOLDERS})
                   )""",
            (
                LEGACY_DREAM_DOMAIN_EDGE_PROVENANCE,
                *SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
                *SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
            ),
        ).fetchone()[0]
        other = stored - semantic - domain - session
        return {
            "stored": stored,
            "semantic": semantic,
            "domain": domain,
            "session": session,
            "other": other,
        }

    # ── FTS5 search ────────────────────────────────────────────────────

    def fts_search(self, query: str, limit: int = 20,
                   include_archived: bool = False) -> list[dict]:
        """Full-text search using FTS5 BM25 ranking.

        Archived and superseded nodes are fenced from default results;
        include_archived=True restores both archived and superseded
        candidates (the pre-fence behavior) for callers that legitimately
        need retired content (R3.1: the fence note names both, the flag
        delivers both).
        """
        import re
        # Strip punctuation and FTS5 special chars, keep only words
        tokens = re.findall(r'\w+', query.lower())
        if not tokens:
            return []
        if include_archived:
            fts_fence = "1=1"
            like_fence = "1=1"
        else:
            fts_fence = "n.status NOT IN ('archived', 'superseded')"
            like_fence = "status NOT IN ('archived', 'superseded')"
        # Build FTS5 query: quoted phrase OR individual tokens
        phrase = " ".join(tokens)
        safe_phrase = phrase.replace('"', '""')
        token_expr = " OR ".join(tokens)
        fts_query = f'"{safe_phrase}" OR {token_expr}'
        try:
            rows = self.conn.execute(
                f"""SELECT n.*, rank FROM nodes_fts
                   JOIN nodes n ON n.id = nodes_fts.id
                   WHERE nodes_fts MATCH ? AND {fts_fence}
                   ORDER BY rank LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Fallback: simple LIKE search if FTS query syntax fails.
            # This is a degraded path — log it so a malformed fence or
            # broken FTS index announces itself rather than silently
            # returning plausible wrong results (R5.1/I4).
            import sys
            print(f"Warning: FTS search degraded to LIKE fallback for "
                  f"query {fts_query!r} (fence: {fts_fence})",
                  file=sys.stderr)
            rows = self.conn.execute(
                f"""SELECT *, 0 as rank FROM nodes
                   WHERE (title LIKE ? OR content LIKE ?)
                     AND {like_fence}
                   ORDER BY weight DESC LIMIT ?""",
                (f"%{phrase}%", f"%{phrase}%", limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Weight decay ───────────────────────────────────────────────────

    # The fold is unconditional (no interval gate) so weight at time T
    # matches the closed form w0 * 0.5^((T - max(A,S))/H) regardless of
    # run schedule (R2.1). The write decision is the only threshold: a
    # sub-day slice that doesn't change the 4-dp weight is a no-op write,
    # but the interval is never discarded (R2.4) — per-row accounting via
    # meta keys (decay.row.<id>) tracks the unrounded weight and the time
    # of the last ACTUAL decay. When a write is suppressed (4-dp delta too
    # small), the row's stamp and unrounded weight are preserved so the next
    # fold computes from the true weight over the accumulated interval,
    # avoiding cumulative 4-dp rounding error. When the write succeeds,
    # the meta key is cleaned up (the global stamp and stored weight suffice
    # until the next suppression).

    def apply_weight_decay(self, node_half_life_days: int = 90,
                           edge_half_life_days: int = 30) -> int:
        """Decay weights over the interval since the previous decay run.

        Schedule-independent: the fold is unconditional and the global stamp
        always advances, so weight at time T equals ``w0 * 0.5^((T -
        max(last_accessed, S)) / H)`` for every run schedule including
        sub-day intervals (R2.1). The write threshold (4-dp delta) is the
        only suppression, but per-row accounting (meta key ``decay.row.<id>``)
        stores the unrounded weight and last-decay timestamp so a suppressed
        write does not discard the interval or accumulate rounding error —
        the next fold computes from the true weight over the full accumulated
        interval (R2.4 anti-starvation). Running twice in immediate succession
        is a no-op the second time because the interval is ~zero (R2.2).
        The first run after upgrade stamps the checkpoint and decays nothing
        — cold start never retro-punishes (R2.3).
        Returns count of affected nodes.
        """
        import json as _json

        now = datetime.now()

        # Checkpoint read, row updates, and stamp are ONE serialized unit:
        # BEGIN IMMEDIATE takes the write lock up front, so two concurrent
        # runs cannot both read the same checkpoint and double-apply an
        # interval (the loser blocks, then sees the fresh stamp and folds
        # ~zero).
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            began = True
        except sqlite3.OperationalError:
            # Already inside a caller-managed transaction — its
            # serialization applies.
            began = False

        try:
            prev_raw = self.get_meta("decay.last_run")
            prev = None
            if prev_raw:
                try:
                    prev = datetime.fromisoformat(prev_raw)
                except (ValueError, TypeError):
                    prev = None
            if prev is None:
                # Cold start (or corrupt stamp): establish accounting, decay nothing.
                self.set_meta("decay.last_run", now.isoformat())
                return 0

            # Monotonic guard (R2.3): a backwards clock reading must not
            # erase the checkpoint or retro-punish. If now < prev, treat
            # the run as a no-op — leave the stamp and per-row accounting
            # untouched.
            if now < prev:
                if began and self.conn.in_transaction:
                    self.conn.rollback()
                return 0

            # Node decay over (max(last_accessed, row_prev), now] — unconditional
            # fold, per-row accounting for the write threshold.
            rows = self.conn.execute(
                "SELECT id, weight, last_accessed FROM nodes").fetchall()
            count = 0
            for row in rows:
                try:
                    last = datetime.fromisoformat(row["last_accessed"])
                except (ValueError, TypeError):
                    continue
                # Per-row state: {ts, w_true, w_stored} where ts is the
                # last decay time, w_true is the unrounded weight at that
                # time, and w_stored is the 4-dp weight we wrote to the row.
                # Falls back to the global stamp and stored weight when the
                # row has never been suppressed (no meta key).
                # On the next fold, if the row's current weight differs
                # from w_stored, an external write (e.g. reinforcement)
                # changed the weight between folds — discard the snapshot
                # and use the row's current stored weight as w0 (R2.1).
                row_meta_raw = self.get_meta(f"_wtr.node.{row['id']}")
                row_prev = prev
                true_weight = float(row["weight"])
                if row_meta_raw:
                    try:
                        row_meta = _json.loads(row_meta_raw)
                        row_prev = datetime.fromisoformat(row_meta["ts"])
                        w_stored = float(row_meta["w_stored"])
                        if w_stored == float(row["weight"]):
                            # Row untouched since last fold — use the
                            # unrounded snapshot to avoid cumulative
                            # 4-dp rounding error (R2.1).
                            true_weight = float(row_meta["w_true"])
                        # else: external write happened — true_weight
                        # stays as the row's current stored weight.
                    except (ValueError, TypeError, KeyError):
                        row_prev = prev
                        true_weight = float(row["weight"])
                start = max(last, row_prev)
                days_since = (now - start).total_seconds() / 86400.0
                if days_since <= 0:
                    continue
                decay = 0.5 ** (days_since / node_half_life_days)
                new_true = true_weight * decay
                new_weight = max(0.01, round(new_true, 4))
                if new_weight != round(float(row["weight"]), 4):
                    self.conn.execute(
                        "UPDATE nodes SET weight = ? WHERE id = ?",
                        (new_weight, row["id"]),
                    )
                    count += 1
                    stored_weight = new_weight
                else:
                    stored_weight = float(row["weight"])
                # Always write the per-row meta — the true weight must be
                # preserved for the next fold's rounding-error avoidance.
                # The cost is O(changed + suppressed) rows, not O(graph):
                # rows with days_since <= 0 (just accessed, no interval)
                # are skipped by the continue above and never reach here.
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (f"_wtr.node.{row['id']}",
                     _json.dumps({
                         "ts": now.isoformat(),
                         "w_true": new_true,
                         "w_stored": stored_weight,
                     })),
                )

            # Edge decay over (max(created_at, prev), now] — unconditional.
            # Edges use the global stamp and per-row accounting for the
            # same rounding-error reason as nodes.
            edge_rows = self.conn.execute(
                "SELECT id, weight, created_at FROM edges").fetchall()
            for row in edge_rows:
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except (ValueError, TypeError):
                    continue
                row_meta_raw = self.get_meta(f"_wtr.edge.{row['id']}")
                row_prev = prev
                true_weight = float(row["weight"])
                if row_meta_raw:
                    try:
                        row_meta = _json.loads(row_meta_raw)
                        row_prev = datetime.fromisoformat(row_meta["ts"])
                        w_stored = float(row_meta["w_stored"])
                        if w_stored == float(row["weight"]):
                            true_weight = float(row_meta["w_true"])
                    except (ValueError, TypeError, KeyError):
                        row_prev = prev
                        true_weight = float(row["weight"])
                start = max(created, row_prev)
                days_since = (now - start).total_seconds() / 86400.0
                if days_since <= 0:
                    continue
                decay = 0.5 ** (days_since / edge_half_life_days)
                new_true = true_weight * decay
                new_weight = max(0.01, round(new_true, 4))
                if new_weight != round(float(row["weight"]), 4):
                    self.conn.execute(
                        "UPDATE edges SET weight = ? WHERE id = ?",
                        (new_weight, row["id"]),
                    )
                    stored_weight = new_weight
                else:
                    stored_weight = float(row["weight"])
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (f"_wtr.edge.{row['id']}",
                     _json.dumps({
                         "ts": now.isoformat(),
                         "w_true": new_true,
                         "w_stored": stored_weight,
                     })),
                )

            # Stamp always advances — even if no write crossed the 4-dp
            # threshold, the interval is accounted for (R2.1, R2.4).
            # Stamp + decay commit together (set_meta commits): a crash
            # mid-run rolls the whole slice back, never double-applies.
            self.set_meta("decay.last_run", now.isoformat())
            return count
        except BaseException:
            if began and self.conn.in_transaction:
                self.conn.rollback()
            raise

    # ── Stigmergic injection pheromone ──────────────────────────────────

    @staticmethod
    def _decayed_strength(strength: float, last_decay: str, half_life_days: float,
                          now: datetime | None = None) -> float:
        """Effective pheromone after exponential decay since last_decay."""
        if strength <= 0 or half_life_days <= 0:
            return max(0.0, strength)
        now = now or datetime.now()
        try:
            last = datetime.fromisoformat(last_decay)
        except (ValueError, TypeError):
            return strength
        days = (now - last).total_seconds() / 86400.0
        if days <= 0:
            return strength
        return strength * (0.5 ** (days / half_life_days))

    # ── Learned pair co-activation ─────────────────────────────────────

    def deposit_coactivation(self, node_a: str, node_b: str, context: str = "",
                             eta: float = 0.15, half_life_days: float = 14.0) -> float:
        """Strengthen a pair that proved useful TOGETHER. Returns new strength.

        Bounded Hebbian update, ``w <- w + eta * (1 - w)``: this is the one
        piece of Hillock's math worth taking. It approaches 1.0 asymptotically
        and can never run away, unlike the unbounded additive deposit used for
        node-level pheromone — a pair confirmed a hundred times should saturate,
        not dominate.

        Prior decay is folded in before the update so the accumulator stays
        current. Callers must gate on CONFIRMED USE: co-retrieval is not
        usefulness, and depositing on it would teach the graph the retriever's
        own biases and then present the result as evidence.
        """
        a = self._live_node_id(node_a)
        b = self._live_node_id(node_b)
        if a == b:
            return 0.0
        if a > b:
            a, b = b, a  # canonical order — one row per pair
        now = _now()
        row = self.conn.execute(
            "SELECT strength, events, last_decay FROM node_coactivation "
            "WHERE node_a = ? AND node_b = ? AND context = ?",
            (a, b, context),
        ).fetchone()

        current = 0.0
        events = 0
        if row is not None:
            current = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days)
            events = row["events"]

        new = current + eta * (1.0 - current)
        new = max(0.0, min(1.0, new))
        self.conn.execute(
            """INSERT INTO node_coactivation
                   (node_a, node_b, context, strength, events, last_event, last_decay)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(node_a, node_b, context) DO UPDATE SET
                   strength = excluded.strength,
                   events = excluded.events,
                   last_event = excluded.last_event,
                   last_decay = excluded.last_decay""",
            (a, b, context, round(new, 6), events + 1, now, now),
        )
        self.conn.commit()
        return new

    def coactivation_scores(self, node_ids: set[str], context: str = "",
                            half_life_days: float = 14.0,
                            min_events: int = 3) -> list[tuple[str, float]]:
        """Decayed co-activation score per node, summed over its partners.

        Read-only: decay is applied lazily here so a read is always current
        without needing a write. ``min_events`` suppresses pairs seen too few
        times to be signal — a single co-occurrence is an anecdote.
        """
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        params = list(node_ids) + list(node_ids) + [context, min_events]
        try:
            rows = self.conn.execute(
                f"""SELECT node_a, node_b, strength, last_decay
                    FROM node_coactivation
                    WHERE (node_a IN ({placeholders}) OR node_b IN ({placeholders}))
                      AND context = ? AND events >= ?""",
                params,
            ).fetchall()
        except Exception:
            return []

        totals: dict[str, float] = {}
        for row in rows:
            decayed = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days)
            if decayed <= 0:
                continue
            # Credit only the endpoints the caller asked about; a pair whose
            # other end is outside the candidate set still counts for the end
            # that is inside it.
            for nid in (row["node_a"], row["node_b"]):
                if nid in node_ids:
                    totals[nid] = totals.get(nid, 0.0) + decayed
        return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))

    def decay_coactivation(self, half_life_days: float = 14.0,
                           floor: float = 0.02) -> int:
        """Write back decayed co-activation; prune pairs below `floor`.

        Mirrors ``decay_pheromone``. Returns rows pruned.
        """
        now = datetime.now()
        try:
            rows = self.conn.execute(
                "SELECT node_a, node_b, context, strength, last_decay "
                "FROM node_coactivation").fetchall()
        except Exception:
            return 0
        pruned = 0
        for row in rows:
            strength = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days, now)
            key = (row["node_a"], row["node_b"], row["context"])
            if strength < floor:
                self.conn.execute(
                    "DELETE FROM node_coactivation "
                    "WHERE node_a = ? AND node_b = ? AND context = ?", key)
                pruned += 1
            elif abs(strength - row["strength"]) > 0.001:
                self.conn.execute(
                    "UPDATE node_coactivation SET strength = ?, last_decay = ? "
                    "WHERE node_a = ? AND node_b = ? AND context = ?",
                    (round(strength, 6), now.isoformat(timespec="seconds"), *key))
        self.conn.commit()
        return pruned

    def coactivation_stats(self, half_life_days: float = 14.0,
                           warm_floor: float = 0.3) -> dict:
        """Decayed signal summary used to decide if the channel is mature.

        Mirrors ``pheromone_stats``: the channel stays inert in ranking until
        it has enough warm signal to be worth trusting.
        """
        try:
            rows = self.conn.execute(
                "SELECT strength, last_decay FROM node_coactivation").fetchall()
        except Exception:
            return {"pairs": 0, "warm_pairs": 0, "signal": 0.0}
        now = datetime.now()
        warm = 0
        signal = 0.0
        for row in rows:
            decayed = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days, now)
            signal += decayed
            if decayed >= warm_floor:
                warm += 1
        return {"pairs": len(rows), "warm_pairs": warm,
                "signal": round(signal, 4)}

    def _live_node_id(self, node_id: str, max_hops: int = 10) -> str:
        """Follow extra['superseded_by'] to the live successor (bounded, cycle-safe).

        Returns the input id unchanged when the node is live, missing, or the
        chain is malformed.
        """
        nid = node_id
        seen = {nid}
        for _ in range(max_hops):
            row = self.conn.execute(
                "SELECT extra FROM nodes WHERE id = ?", (nid,)).fetchone()
            if row is None:
                return nid
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                return nid
            succ = extra.get("superseded_by") if isinstance(extra, dict) else None
            if not succ or not isinstance(succ, str) or succ in seen:
                return nid
            seen.add(succ)
            nid = succ
        return nid

    def deposit_pheromone(self, node_id: str, context: str = "",
                          amount: float = 1.0, half_life_days: float = 14.0,
                          reinforce: bool = False, missed: bool = False) -> float:
        """Lay (or reinforce) pheromone on a node for a context.

        Folds prior decay into the stored strength before adding `amount`, so the
        accumulator stays current. Counter bumped depends on the kind of deposit:
        - default: `deposits` (a node was injected)
        - reinforce=True: `reinforcements` (a confirmed-useful injection)
        - missed=True: `missed` (counterfactual — would have helped but wasn't injected)
        Deposits on a superseded node are redirected to its live successor
        (supersede_node migrates existing trails exactly once; late deposits —
        e.g. deferred session-end reinforcement — must follow the same chain
        or the signal strands on the dead node and boosts it in ranking).
        Returns the new stored strength.
        """
        node_id = self._live_node_id(node_id)
        now = _now()
        d_inc = 0 if (reinforce or missed) else 1
        r_inc = 1 if reinforce else 0
        m_inc = 1 if missed else 0
        row = self.conn.execute(
            "SELECT strength, deposits, reinforcements, missed, last_decay "
            "FROM injection_pheromone WHERE node_id = ? AND context = ?",
            (node_id, context),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO injection_pheromone "
                "(node_id, context, strength, deposits, reinforcements, missed, last_deposit, last_decay) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (node_id, context, round(amount, 4), d_inc, r_inc, m_inc, now, now),
            )
            self.conn.commit()
            return round(amount, 4)

        decayed = self._decayed_strength(
            row["strength"], row["last_decay"], half_life_days)
        new_strength = round(decayed + amount, 4)
        self.conn.execute(
            "UPDATE injection_pheromone SET strength = ?, deposits = ?, "
            "reinforcements = ?, missed = ?, last_deposit = ?, last_decay = ? "
            "WHERE node_id = ? AND context = ?",
            (new_strength,
             row["deposits"] + d_inc,
             row["reinforcements"] + r_inc,
             row["missed"] + m_inc,
             now, now, node_id, context),
        )
        self.conn.commit()
        return new_strength

    def pheromone_scores(self, node_ids: set[str], context: str = "",
                         half_life_days: float = 14.0,
                         min_deposits: int = 5) -> list[tuple[str, float]]:
        """Decayed pheromone strength per node for retrieval ranking (read-only).

        Uses the context-conditioned trail when it has enough deposits to be
        statistically real; otherwise falls back to the coarse global trail.
        """
        if not node_ids:
            return []
        now = datetime.now()
        ids = list(node_ids)
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT node_id, context, strength, deposits, last_decay "
            f"FROM injection_pheromone "
            f"WHERE node_id IN ({placeholders}) AND context IN ('', ?)",
            (*ids, context),
        ).fetchall()

        conditioned: dict[str, sqlite3.Row] = {}
        glob: dict[str, sqlite3.Row] = {}
        for row in rows:
            (conditioned if row["context"] == context and context else glob)[row["node_id"]] = row

        results: list[tuple[str, float]] = []
        for nid in ids:
            cond = conditioned.get(nid)
            chosen = cond if (cond and cond["deposits"] >= min_deposits) else glob.get(nid)
            if chosen is None:
                continue
            strength = self._decayed_strength(
                chosen["strength"], chosen["last_decay"], half_life_days, now)
            if strength > 0.0:
                results.append((nid, strength))
        return results

    def decay_pheromone(self, half_life_days: float = 14.0,
                        floor: float = 0.02) -> int:
        """Write back decayed pheromone strength; prune trails below `floor`.

        Lazy decay already keeps reads correct; this periodic pass keeps the
        stored values honest and evaporates dead trails. Returns rows pruned.
        """
        now = datetime.now()
        rows = self.conn.execute(
            "SELECT node_id, context, strength, last_decay FROM injection_pheromone"
        ).fetchall()
        pruned = 0
        for row in rows:
            strength = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days, now)
            if strength < floor:
                self.conn.execute(
                    "DELETE FROM injection_pheromone WHERE node_id = ? AND context = ?",
                    (row["node_id"], row["context"]),
                )
                pruned += 1
            elif abs(strength - row["strength"]) > 0.001:
                self.conn.execute(
                    "UPDATE injection_pheromone SET strength = ?, last_decay = ? "
                    "WHERE node_id = ? AND context = ?",
                    (round(strength, 4), now.isoformat(timespec="seconds"),
                     row["node_id"], row["context"]),
                )
        self.conn.commit()
        return pruned

    def pheromone_stats(self, half_life_days: float = 14.0,
                        warm_floor: float = 0.5) -> dict:
        """Decayed signal summary used to decide if pheromone is mature enough
        to trust in ranking.

        Counts only GRADED signal (reinforcements or counterfactual deposits) —
        bare injection deposits are popularity, not usefulness, and are excluded.
        Uses decayed strength so the measure falls when trails cool.
        """
        now = datetime.now()
        rows = self.conn.execute(
            "SELECT node_id, context, strength, reinforcements, missed, last_decay "
            "FROM injection_pheromone WHERE reinforcements > 0 OR missed > 0"
        ).fetchall()
        warm_nodes: set[str] = set()
        warm_signal = 0.0
        signal_events = 0
        for row in rows:
            signal_events += (row["reinforcements"] or 0) + (row["missed"] or 0)
            strength = self._decayed_strength(
                row["strength"], row["last_decay"], half_life_days, now)
            if strength >= warm_floor:
                warm_nodes.add(row["node_id"])
                warm_signal += strength
        total_rows = self.conn.execute(
            "SELECT COUNT(*) FROM injection_pheromone").fetchone()[0]
        return {
            "warm_graded_nodes": len(warm_nodes),
            "warm_signal": round(warm_signal, 3),
            "signal_events": signal_events,
            "total_trails": total_rows,
        }

    # ── Operational node queries ────────────────────────────────────────

    def nodes_by_trigger(self, trigger: str, node_type: str | None = None) -> list[dict]:
        """Find operational nodes (constraints, checkpoints) matching a trigger.

        Trigger is stored in extra JSON: {"trigger": "pre-deploy"}.
        """
        q = "SELECT * FROM nodes WHERE extra LIKE ?"
        params: list = [f'%"trigger"%{trigger}%']
        if node_type:
            q += " AND type = ?"
            params.append(node_type)
        q += " AND status = 'active' ORDER BY weight DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def nodes_by_owner(self, owner: str, node_type: str | None = None) -> list[dict]:
        """Find nodes owned by a specific person (watches, directives)."""
        q = "SELECT * FROM nodes WHERE extra LIKE ?"
        params: list = [f'%"owner"%"{owner}"%']
        if node_type:
            q += " AND type = ?"
            params.append(node_type)
        q += " AND status = 'active' ORDER BY weight DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_tags(
        self,
        status: str | None = None,
        project_path: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Query session tags, applying exact filters before ordering and limit."""
        q = """SELECT * FROM nodes
                WHERE type = 'session'
                  AND json_valid(extra)
                  AND json_extract(extra, '$.session_status') IS NOT NULL"""
        params: list[Any] = []
        if status:
            q += " AND json_extract(extra, '$.session_status') = ?"
            params.append(status)
        if project_path is not None:
            q += " AND COALESCE(json_extract(extra, '$.project_path'), '') = ?"
            params.append(project_path)
        q += " ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_tag_by_name(
        self,
        tag_name: str,
        *,
        project_path: str | None = None,
    ) -> dict | None:
        """Find the current session tag with an exact, deterministic lookup."""
        q = """SELECT * FROM nodes
                WHERE type = 'session'
                  AND json_valid(extra)
                  AND json_extract(extra, '$.tag') = ?"""
        params: list[Any] = [tag_name]
        if project_path is not None:
            q += " AND COALESCE(json_extract(extra, '$.project_path'), '') = ?"
            params.append(project_path)
        q += """ ORDER BY
                    CASE json_extract(extra, '$.session_status')
                        WHEN 'active' THEN 0
                        WHEN 'paused' THEN 1
                        WHEN 'completed' THEN 2
                        ELSE 3
                    END,
                    updated_at DESC, created_at DESC, id DESC
                  LIMIT 1"""
        row = self.conn.execute(q, params).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def active_watches(self) -> list[dict]:
        """Get all active watches that haven't expired."""
        now = _now()[:10]  # YYYY-MM-DD
        rows = self.conn.execute(
            """SELECT * FROM nodes WHERE type = 'watch' AND status = 'active'
               ORDER BY weight DESC"""
        ).fetchall()
        result = []
        for r in rows:
            d = self._row_to_dict(r)
            extra = d.get("extra")
            expires = extra.get("expires", "") if isinstance(extra, dict) else ""
            # Include if no (valid) expiry or not yet expired — a watch
            # with malformed extra still surfaces rather than crashing
            # every operational pull.
            if not isinstance(expires, str) or not expires or expires >= now:
                result.append(d)
        return result

    def active_constraints(self, trigger: str | None = None) -> list[dict]:
        """Get active constraints, optionally filtered by trigger."""
        if trigger:
            return self.nodes_by_trigger(trigger, node_type="constraint")
        return self.all_nodes(node_type="constraint", status="active")

    def active_checkpoints(self, trigger: str | None = None) -> list[dict]:
        """Get active checkpoints, optionally filtered by trigger."""
        if trigger:
            return self.nodes_by_trigger(trigger, node_type="checkpoint")
        return self.all_nodes(node_type="checkpoint", status="active")

    def operational_summary(self, trigger: str | None = None,
                            owner: str | None = None) -> dict:
        """Summary of all active operational nodes."""
        constraints = self.active_constraints(trigger)
        checkpoints = self.active_checkpoints(trigger)
        watches = self.active_watches()
        directives = self.all_nodes(node_type="directive", status="active")

        if owner:
            watches = [w for w in watches if (w.get("extra") or {}).get("owner") == owner]
            directives = [d for d in directives if (d.get("extra") or {}).get("owner") == owner]

        return {
            "constraints": constraints,
            "checkpoints": checkpoints,
            "watches": watches,
            "directives": directives,
        }

    # ── Meta key-value ────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        """Read a value from the meta table. Returns None if not found."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return row["value"]

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to the meta table (upsert)."""
        if redact_text(key) != key:
            raise ValueError("Metadata keys must not contain recognizable credentials")
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, redact_serialized(value)),
        )
        self.conn.commit()

    def bump_meta_counter(self, key: str, amount: int = 1) -> int:
        """Increment a durable counter in the meta table; return the new value.

        Used by recovery paths that must leave a trace: a handled failure still
        emits a signal, so a rising count is visible to `kin doctor` instead of
        vanishing into a caught exception. A non-integer stored value is
        treated as zero rather than raising — a counter must never become the
        thing that breaks the path it is instrumenting.
        """
        raw = self.get_meta(key)
        try:
            current = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            current = 0
        new = current + amount
        self.set_meta(key, str(new))
        return new

    # ── Skill tracking ─────────────────────────────────────────────────

    def record_skill_evidence(self, person_id: str, skill_title: str,
                              evidence: str, source: str = "") -> None:
        """Record evidence of a person demonstrating a skill.

        - Find or create the person node
        - Find or create the skill node
        - Add/update a 'demonstrates' edge from person to skill
        - Store evidence in edge provenance
        - Boost the skill node weight by 0.05 per evidence (cap at 1.0)
        """
        # Find or create person node
        person_node = self.get_node(person_id)
        if person_node is None:
            person_node = self.get_node_by_title(person_id)
        if person_node is None:
            person_id = self.add_node(
                person_id,
                node_type="person",
                prov_activity="skill-tracking",
                prov_why="Auto-created for skill evidence",
            )
        else:
            person_id = person_node["id"]

        # Find or create skill node
        skill_node = self.get_node_by_title(skill_title)
        if skill_node is None:
            skill_id = self.add_node(
                skill_title,
                node_type="skill",
                weight=0.5,
                prov_activity="skill-tracking",
                prov_why="Auto-created for skill evidence",
            )
        else:
            skill_id = skill_node["id"]

        # Build evidence record
        now = _now()

        # Check for existing demonstrates edge
        existing = self.conn.execute(
            """SELECT id, provenance FROM edges
               WHERE from_id = ? AND to_id = ? AND type = 'demonstrates'""",
            (person_id, skill_id),
        ).fetchone()

        if existing:
            # Append evidence to existing provenance
            try:
                prev = json.loads(existing["provenance"])
                if isinstance(prev, list):
                    prev.append({"evidence": evidence, "source": source, "recorded_at": now})
                else:
                    prev = [prev, {"evidence": evidence, "source": source, "recorded_at": now}]
            except (json.JSONDecodeError, TypeError):
                prev = [{"evidence": evidence, "source": source, "recorded_at": now}]
            self.conn.execute(
                "UPDATE edges SET provenance = ? WHERE id = ?",
                (_jdumps(prev), existing["id"]),
            )
            self.conn.commit()
        else:
            # Create new demonstrates edge (unidirectional — person -> skill)
            prov_list = [{"evidence": evidence, "source": source, "recorded_at": now}]
            self.conn.execute(
                """INSERT OR REPLACE INTO edges (from_id, to_id, type, weight, provenance)
                   VALUES (?, ?, 'demonstrates', 0.5, ?)""",
                (person_id, skill_id, _jdumps(prov_list)),
            )
            self.conn.commit()

        # Boost skill weight by 0.05, capped at 1.0
        skill_node = self.get_node(skill_id)
        if skill_node:
            new_weight = min(1.0, skill_node["weight"] + 0.05)
            self.update_node(skill_id, weight=new_weight)

        self._log("record_skill_evidence", skill_id, skill_title, person_id,
                  {"evidence": evidence, "source": source})

    # ── Directive mutable state ─────────────────────────────────────────

    def update_directive_state(self, node_id: str, state: dict) -> None:
        """Update the current_state of a directive/operational node.

        Routed through atomic_extra_update so a concurrent extra writer
        (a lock, an expires edit, another set-state) is never clobbered
        by a stale-snapshot write.
        """
        node = self.get_node(node_id)
        if not node:
            return

        def _mutate(extra: dict) -> None:
            extra["current_state"] = state
            extra["state_updated_at"] = _now()

        self.atomic_extra_update(node_id, _mutate)
        self._log("update_state", node_id, node.get("title", ""),
                  details={"state": state})

    # ── Reminders ───────────────────────────────────────────────────────

    def add_reminder(
        self,
        title: str,
        next_due: str,
        *,
        reminder_id: str | None = None,
        body: str = "",
        priority: str = "normal",
        reminder_type: str = "once",
        schedule: str = "",
        channels: list[str] | None = None,
        related_node_id: str | None = None,
        tags: str = "",
        extra: dict | None = None,
    ) -> str:
        """Insert a reminder. Returns its ID."""
        _guard_action_credentials(extra)
        rid = reminder_id or _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO reminders
               (id, title, body, priority, status, reminder_type, schedule,
                next_due, channels, related_node_id, tags, extra,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, redact_text(title), redact_text(body), priority, reminder_type, schedule,
             next_due, _jdumps(channels or []), related_node_id or "",
             tags, _jdumps(extra or {}), now, now),
        )
        self.conn.commit()
        self._log("add_reminder", rid, title,
                  details={"priority": priority, "next_due": next_due,
                           "type": reminder_type})
        return rid

    def get_reminder(self, reminder_id: str) -> dict | None:
        """Fetch a reminder by ID."""
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return None
        return self._reminder_to_dict(row)

    def update_reminder(self, reminder_id: str, **fields) -> None:
        """Update specific fields on a reminder."""
        if "extra" in fields:
            _guard_action_credentials(fields["extra"])
        allowed = {
            "title", "body", "priority", "status", "reminder_type",
            "schedule", "next_due", "last_fired", "snooze_until",
            "snooze_count", "channels", "related_node_id", "tags", "extra",
        }
        updates = []
        values = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            v = redact(v)
            if k in ("channels", "extra"):
                v = _jdumps(v)
            updates.append(f"{k} = ?")
            values.append(v)
        if not updates:
            return
        updates.append("updated_at = ?")
        values.append(_now())
        values.append(reminder_id)
        self.conn.execute(
            f"UPDATE reminders SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        self.conn.commit()

    def delete_reminder(self, reminder_id: str) -> None:
        """Delete a reminder."""
        self.conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        self.conn.commit()
        self._log("delete_reminder", reminder_id)

    def list_reminders(
        self,
        status: str | None = None,
        priority: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List reminders with optional filters."""
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if priority:
            clauses.append("priority = ?")
            params.append(priority)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT * FROM reminders{where}
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                        WHEN 'normal' THEN 2 WHEN 'low' THEN 3
                    END,
                    next_due ASC
                LIMIT ?""",
            params,
        ).fetchall()
        return [self._reminder_to_dict(r) for r in rows]

    def due_reminders(self, as_of: str | None = None) -> list[dict]:
        """Get all reminders that are due now (active past due or snoozed past snooze_until)."""
        now = as_of or _now()
        rows = self.conn.execute(
            """SELECT * FROM reminders
               WHERE (status = 'active' AND next_due <= ?)
                  OR (status = 'snoozed' AND snooze_until <= ?)
               ORDER BY
                   CASE priority
                       WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                       WHEN 'normal' THEN 2 WHEN 'low' THEN 3
                   END,
                   next_due ASC""",
            (now, now),
        ).fetchall()
        return [self._reminder_to_dict(r) for r in rows]

    def snooze_reminder(
        self, reminder_id: str, snooze_until: str, increment_count: bool = True,
        *, automatic: bool = False,
    ) -> bool:
        """Snooze notifications; only a deliberate snooze defers action freshness.

        Old records have no separate action deadline. Preserve their existing
        snooze on the first automatic retry rather than guessing its origin.
        Returns whether a row was snoozed. Automatic retries only claim fired
        rows; completion/cancellation and manual snoozes win if committed first.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = self.get_reminder(reminder_id)
            if not r or (automatic and r["status"] != "fired"):
                conn.rollback()
                return False
            fields: dict[str, Any] = {
                "status": "snoozed",
                "snooze_until": snooze_until,
            }
            extra = dict(r.get("extra") or {})
            if automatic:
                extra.setdefault("action_snooze_until", r.get("snooze_until"))
            else:
                extra["action_snooze_until"] = snooze_until
            fields["extra"] = extra
            if increment_count:
                fields["snooze_count"] = r.get("snooze_count", 0) + 1
            self.update_reminder(reminder_id, **fields)
        except BaseException:
            conn.rollback()
            raise
        self._log("snooze_reminder", reminder_id, "",
                  details={"snooze_until": snooze_until, "automatic": automatic})
        return True

    def update_reminder_action(
        self, reminder_id: str, *, status: str, result: str, executed_at: str,
    ) -> None:
        """Update action fields without losing a concurrent snooze's deadline."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = self.get_reminder(reminder_id)
            if not r:
                conn.rollback()
                return
            extra = dict(r.get("extra") or {})
            extra.update(action_status=status, action_result=result,
                         action_executed_at=executed_at)
            self.update_reminder(reminder_id, extra=extra)
        except BaseException:
            conn.rollback()
            raise

    def complete_reminder(self, reminder_id: str) -> None:
        """Mark a reminder as completed."""
        self.update_reminder(reminder_id, status="completed")
        self._log("complete_reminder", reminder_id)

    def _reminder_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a reminder row to dict with JSON parsing."""
        d = dict(row)
        for key in ("channels", "extra"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def nearest_pending_reminder(self) -> str | None:
        """Return the ISO timestamp of the nearest pending reminder, or None."""
        row = self.conn.execute(
            """SELECT MIN(CASE
                 WHEN status = 'active' THEN next_due
                 WHEN status = 'snoozed' THEN snooze_until
               END) AS nearest
               FROM reminders
               WHERE status IN ('active', 'snoozed')"""
        ).fetchone()
        if row is None or row["nearest"] is None:
            return None
        return row["nearest"]

    # ── Stats ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        stored_node_count = self.conn.execute(
            "SELECT COUNT(*) FROM nodes"
        ).fetchone()[0]
        placeholders = ",".join(
            "?" for _ in SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES
        )
        semantic_node_count = self.conn.execute(
            f"SELECT COUNT(*) FROM nodes WHERE type NOT IN ({placeholders})",
            SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
        ).fetchone()[0]
        edge_counts = self.graph_edge_counts()
        orphan_count = len(self.orphans())
        type_counts = {}
        for row in self.conn.execute("SELECT type, COUNT(*) as c FROM nodes GROUP BY type"):
            type_counts[row["type"]] = row["c"]
        return {
            # ``nodes`` remains as a compatibility alias, but it now has the
            # same semantic meaning as graph.store_stats().
            "nodes": semantic_node_count,
            "semantic_nodes": semantic_node_count,
            "stored_nodes": stored_node_count,
            "edges": edge_counts["semantic"],
            "stored_edges": edge_counts["stored"],
            "ignored_domain_edges": edge_counts["domain"],
            "ignored_session_edges": edge_counts["session"],
            "ignored_other_edges": edge_counts["other"],
            "metrics_schema": SEMANTIC_METRICS_SCHEMA_VERSION,
            "orphans": orphan_count,
            "types": type_counts,
        }

    # ── Helpers ────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("aka", "domains", "prov_who", "extra", "referent"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        d["tags"] = d.get("domains") or []
        return d

    def node_ids(self) -> list[str]:
        """All node IDs."""
        return [r[0] for r in self.conn.execute("SELECT id FROM nodes").fetchall()]
