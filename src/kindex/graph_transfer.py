"""Graph snapshots are evidence, not locally verified assertions or runtime state."""

from __future__ import annotations

import json
import math
import hashlib
import re
from pathlib import PureWindowsPath
from urllib.parse import urlsplit

from .privacy import redact
from .schema import AUDIENCES, STANDINGS
from .store import _jdumps, _normalize_binding, _now, _uuid, _validate_expires
from .trust import parse_rfc3339, validate_interval

TEXT_FIELDS = (
    "type", "title", "content", "intent", "status", "audience", "prov_when",
    "prov_activity", "prov_why", "prov_source", "created_at", "updated_at", "standing",
)
LIST_FIELDS = ("aka", "domains", "prov_who")
CLOCK_FIELDS = ("valid_at", "invalid_at", "asserted_at", "true_of")
VERIFICATION_FIELDS = ("verified_at", "verified_by", "prov_method")
# Deliberately exclude locks, actors, tasks, reminders and executable actions.
LIFECYCLE_KEYS = (
    "expires", "expired_at", "referent_stale", "superseded_by", "supersedes",
    "supersede_reason", "imported_verification", "imported_referent", "kinbase",
)


def scrub_shared_text(value: str) -> str:
    """Remove contact and machine-path prose while retaining evidence URLs."""
    value = re.sub(r'\S+@\S+\.\S+', '[email]', value)
    return re.sub(r'''https?://[^\s"'<>]+|(?<![\w:/])(?:[A-Za-z]:[\\/]|/(?!/)|~/)[^\s"'<>]+''',
                  lambda match: match[0] if match[0].startswith(("https://", "http://")) else "[path]", value)


def scrub_kinbase_metadata(metadata: dict) -> dict:
    """Preserve evidence structure and stable grouping without shared-copy PII."""
    def scrub(value):
        if isinstance(value, str):
            return scrub_shared_text(value)
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                clean_key = scrub_shared_text(key)
                if clean_key in result:
                    raise ValueError("Sharing redaction would merge Kinbase metadata fields")
                result[clean_key] = "[owner identity redacted]" if key == "owner_identity" and item else scrub(item)
            return result
        return value
    source = redact(metadata)
    result = scrub(source)
    repo = source.get("repo", "")
    if not isinstance(repo, str):
        raise ValueError("Kinbase repo must be text")
    result["repo"] = repo if re.fullmatch(r"sha256:[0-9a-f]{64}", repo) else "sha256:" + hashlib.sha256(repo.encode()).hexdigest()
    identity = source.get("source_identity")
    if isinstance(identity, str) and scrub_shared_text(identity) != identity:
        result["source_identity"] = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    result["source_document_redacted"] = True
    result["signature_verification_scope"] = "original external source bytes; shared copy redacted"
    return result


def export_record(node: dict, edges: list[dict], visible_ids: set[str], *, public: bool) -> dict:
    record = {key: node[key] for key in (*TEXT_FIELDS, *LIST_FIELDS, *CLOCK_FIELDS,
                                       *VERIFICATION_FIELDS, "id", "weight", "referent") if key in node}
    for key in LIST_FIELDS:
        record[key] = node.get(key) or []  # Older SQLite defaults used empty text.
    record["extra"] = {k: v for k, v in (node.get("extra") or {}).items() if k in LIFECYCLE_KEYS}
    # These IDs are relationships too: never expose a hidden endpoint.
    for key in ("supersedes", "superseded_by"):
        if record["extra"].get(key) not in visible_ids:
            record["extra"].pop(key, None)
    record["edges"] = [
        {"to": e["to_id"], "type": e["type"], "weight": e["weight"],
         "bidirectional": False, "provenance": "" if public else e.get("provenance", "")}
        for e in edges if e["to_id"] in visible_ids
    ]
    if public:
        for key in ("title", "content"):
            if isinstance(record.get(key), str):
                record[key] = scrub_shared_text(record[key])
        for key in ("aka", "domains"):
            record[key] = [scrub_shared_text(value) for value in record[key]]
        if isinstance(record["extra"].get("kinbase"), dict):
            record["extra"]["kinbase"] = scrub_kinbase_metadata(record["extra"]["kinbase"])
            record["prov_source"] = "kinbase:" + record["extra"]["kinbase"]["repo"]
        record["prov_who"] = ["anonymous"]
        record["verified_by"] = "anonymous" if record.get("verified_by") else None
        # Preserve canonical evidence URLs, not machine-local paths or actor prose.
        source = record.get("prov_source", "")
        if urlsplit(source).scheme not in ("http", "https"):
            record["prov_source"] = PureWindowsPath(source).name
        for key in ("intent", "prov_why", "prov_activity"):
            record.pop(key, None)
        record["extra"].pop("supersede_reason", None)
        verification = record["extra"].get("imported_verification")
        if isinstance(verification, dict):
            record["extra"]["imported_verification"] = {
                "verified_at": verification.get("verified_at"), "verified_by": "anonymous",
            }
        # Methods and stale diagnostics may contain private actor/path prose.
        record.pop("prov_method", None)
        if record["extra"].get("referent_stale"):
            record["extra"]["referent_stale"] = {"reason": "upstream-stale"}
        for container, key in ((record, "referent"), (record["extra"], "imported_referent")):
            binding = container.get(key)
            if isinstance(binding, dict):
                binding = {k: v for k, v in binding.items() if k in
                           ("path", "url", "content_digest", "digest_scope", "path_redacted", "url_redacted")}
                container[key] = binding
            if isinstance(binding, dict) and PureWindowsPath(binding.get("path", "")).anchor:
                binding = dict(binding)
                binding.pop("path")
                binding["path_redacted"] = True
                container[key] = binding
    return redact(record)


def _fields(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each graph record must be an object")
    if "id" in item and (not isinstance(item["id"], str) or not item["id"].strip()):
        raise ValueError("Graph IDs must be nonempty strings")
    if "id" in item and redact(item["id"]) != item["id"]:
        raise ValueError("Graph ID contains credentials")
    fields = {k: item[k] for k in (*TEXT_FIELDS, *LIST_FIELDS, *CLOCK_FIELDS, "weight", "referent") if k in item}
    for key in TEXT_FIELDS:
        if key in fields and not isinstance(fields[key], str):
            raise ValueError(f"{key} must be text")
    for key in LIST_FIELDS:
        if key in fields and (not isinstance(fields[key], list) or
                              not all(isinstance(v, str) for v in fields[key])):
            raise ValueError(f"{key} must be a list of strings")
    if "audience" in fields and fields["audience"] not in AUDIENCES:
        raise ValueError("Unknown audience")
    if "standing" in fields and fields["standing"] not in STANDINGS:
        raise ValueError("Unknown standing")
    if "status" in fields and not fields["status"].strip():
        raise ValueError("Status must not be empty")
    if "weight" in fields:
        _weight(fields["weight"])
    extra = item.get("extra", {})
    if not isinstance(extra, dict):
        raise ValueError("extra must be an object")
    extra = {k: v for k, v in extra.items() if k in LIFECYCLE_KEYS}
    if "kinbase" in extra:
        metadata = extra["kinbase"]
        if not isinstance(metadata, dict):
            raise ValueError("Kinbase metadata must be an object")
        metadata = dict(metadata)
        for key in ("repo", "source_identity", "logical_key", "mode", "provenance"):
            if key in metadata and not isinstance(metadata[key], str):
                raise ValueError(f"Kinbase {key} must be text")
        for key in ("standing", "claimed_standing"):
            if key in metadata and metadata[key] not in STANDINGS:
                raise ValueError("Unknown Kinbase standing")
        for key in ("effective_from", "effective_until", "asserted_at"):
            if metadata.get(key) is not None:
                parse_rfc3339(metadata[key], field=key)
        validate_interval(metadata.get("effective_from"), metadata.get("effective_until"))
        from .kinbase import clamp_standing
        provenance = metadata.get("provenance", "unknown")
        if "standing" in metadata:
            metadata["standing"] = clamp_standing(metadata["standing"], provenance)
        if "standing" in fields:
            fields["standing"] = clamp_standing(fields["standing"], provenance)
        elif "standing" in metadata:
            fields["standing"] = metadata["standing"]
        extra["kinbase"] = metadata
        # External rule evidence never installs operational policy on import.
        if fields.get("type") in {"constraint", "directive", "checkpoint", "watch"}:
            fields["type"] = "concept"
    if "expires" in extra:
        _validate_expires(extra["expires"])
    for key in ("expired_at", "supersedes", "superseded_by", "supersede_reason"):
        if key in extra and not isinstance(extra[key], str):
            raise ValueError(f"{key} must be text")
    for key in ("referent_stale", "imported_verification", "imported_referent"):
        if key in extra and not isinstance(extra[key], dict):
            raise ValueError(f"{key} must be an object")
    if "imported_verification" in extra:
        verification = extra["imported_verification"]
        if any(v is not None and not isinstance(v, str) for k, v in verification.items() if k in VERIFICATION_FIELDS):
            raise ValueError("Claimed verification must be text")
        extra["imported_verification"] = {k: v for k, v in verification.items() if k in VERIFICATION_FIELDS}
    claimed = {k: item[k] for k in VERIFICATION_FIELDS if item.get(k) is not None}
    if claimed:
        if not all(isinstance(v, str) for v in claimed.values()):
            raise ValueError("Claimed verification must be text")
        extra["imported_verification"] = claimed
    # A deliberately redacted location is evidence, never a fabricated binding.
    binding = fields.get("referent")
    if isinstance(binding, dict) and (binding.get("path_redacted") or binding.get("url_redacted")):
        extra["imported_referent"] = binding
        fields["referent"] = None
    if any(k in fields for k in ("referent", "asserted_at", "true_of")):
        binding, asserted, observed = _normalize_binding(
            fields.get("referent"), fields.get("asserted_at"), fields.get("true_of"))
        if "referent" in fields:
            fields["referent"] = json.loads(binding) if binding else None
        # Never invent freshness clocks for legacy evidence lacking them.
        if fields.get("asserted_at") is not None:
            fields["asserted_at"] = asserted
        if fields.get("true_of") is not None:
            fields["true_of"] = observed
    for key in ("valid_at", "invalid_at"):
        if key in fields:
            fields[key] = validate_interval(fields[key], None)[0]
    if extra:
        fields["extra"] = extra
    return redact(fields)


def _weight(value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Weight must be a finite number")


def _resolve(store, reference: str, *, by_title: bool = False) -> dict | None:
    column = "title COLLATE NOCASE" if by_title else "id"
    rows = store.conn.execute(f"SELECT * FROM nodes WHERE {column} = ?", (reference,)).fetchall()
    if len(rows) > 1:
        raise ValueError("Ambiguous legacy title; use an explicit ID")
    return store._row_to_dict(rows[0]) if rows else None


def _protect(existing: dict, fields: dict) -> None:
    if existing["status"] != "active" and fields.get("status") == "active":
        raise ValueError("Import cannot reactivate inactive knowledge")
    if AUDIENCES.index(fields.get("audience", existing["audience"])) > AUDIENCES.index(existing["audience"]):
        raise ValueError("Import cannot widen an existing audience")
    for key in ("invalid_at", "valid_at"):
        old, new = existing.get(key), fields.get(key, existing.get(key))
        if old:
            if not new:
                raise ValueError("Import cannot relax an existing valid-time boundary")
            before, after = parse_rfc3339(old, field=key), parse_rfc3339(new, field=key)
            relaxes = after > before if key == "invalid_at" else after < before
            if relaxes:
                raise ValueError("Import cannot relax an existing valid-time boundary")
    extra = existing.get("extra") or {}
    old_source = extra.get("kinbase")
    new_source = fields.get("extra", {}).get("kinbase")
    if isinstance(old_source, dict) and isinstance(new_source, dict):
        for key in ("repo", "source_identity", "logical_key", "provenance"):
            if key in old_source and new_source.get(key) != old_source[key]:
                raise ValueError("Import cannot replace an existing Kinbase source identity")
        for key in ("effective_from", "effective_until"):
            old, new = old_source.get(key), new_source.get(key)
            if old:
                if not new:
                    raise ValueError("Import cannot remove an existing Kinbase effective-time boundary")
                before, after = parse_rfc3339(old, field=key), parse_rfc3339(new, field=key)
                if (after > before if key == "effective_until" else after < before):
                    raise ValueError("Import cannot relax an existing Kinbase effective-time boundary")
    if isinstance(old_source, dict):
        if "standing" in fields:
            from .kinbase import clamp_standing
            fields["standing"] = clamp_standing(fields["standing"], old_source.get("provenance", "unknown"))
        if fields.get("type") in {"constraint", "directive", "checkpoint", "watch"}:
            fields["type"] = "concept"
    for key, value in fields.get("extra", {}).items():
        if extra.get(key) and key in ("referent_stale", "expired_at", "superseded_by") and value != extra[key]:
            raise ValueError("Import cannot remove or replace an existing lifecycle demotion")
        if key == "expires" and extra.get(key) and value > extra[key]:
            raise ValueError("Import cannot extend an existing expiry")


def import_records(store, items: list[dict], *, replace: bool = False, dry_run: bool = False) -> dict:
    """Atomic two-pass import. Missing fields are never destructive updates."""
    if not isinstance(items, list):
        raise ValueError("Graph must be an array of records")
    if store.conn.in_transaction:
        raise ValueError("Graph import requires its own transaction")
    counts = dict(created=0, updated=0, edges=0, skipped=0)
    bindings, changed = [], []
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            fields = _fields(item)
            node_id = item.get("id")
            title = fields.get("title", "")
            if not node_id and not title.strip():
                raise ValueError("Graph record requires an ID or title")
            source = fields.get("extra", {}).get("kinbase")
            if isinstance(source, dict) and not node_id:
                raise ValueError("Kinbase evidence requires an explicit source node ID")
            existing = _resolve(store, node_id) if node_id else _resolve(store, title, by_title=True)
            node_id = existing["id"] if existing else node_id or _uuid()
            bindings.append((node_id, item.get("edges", [])))
            if existing:
                _protect(existing, fields)
                incoming_extra = fields.pop("extra", {})
                old_extra = existing.get("extra") or {}
                if not replace and any(k in old_extra and old_extra[k] != v for k, v in incoming_extra.items()):
                    raise ValueError(f"Import conflict for {node_id}: lifecycle metadata")
                if incoming_extra:
                    fields["extra"] = {**old_extra, **incoming_extra}
                fields = {k: v for k, v in fields.items() if v != existing.get(k)}
                if not replace and any(existing.get(k) not in (None, "", [], {}) for k in fields if k != "extra"):
                    raise ValueError(f"Import conflict for {node_id}; inspect evidence before using --mode replace")
                if not fields:
                    counts["skipped"] += 1
                    continue
                store._check_lock(existing, actor=None, force=False, remedy="release the lock before importing")
                # Even replacement cannot inherit the old claim's local verification.
                fields.update({k: None for k in VERIFICATION_FIELDS})
                fields.setdefault("updated_at", _now())
                counts["updated"] += 1
            else:
                fields.setdefault("title", "")
                fields.setdefault("prov_activity", "import")
                for key in LIST_FIELDS:
                    fields.setdefault(key, [])
                counts["created"] += 1
            validate_interval(fields.get("valid_at", (existing or {}).get("valid_at")),
                              fields.get("invalid_at", (existing or {}).get("invalid_at")))
            values = {k: _jdumps(v) if k in (*LIST_FIELDS, "extra", "referent") and v is not None else v
                      for k, v in fields.items()}
            if existing:
                store.conn.execute(f"UPDATE nodes SET {', '.join(k + ' = ?' for k in values)} WHERE id = ?",
                                   (*values.values(), node_id))
            else:
                store.conn.execute(f"INSERT INTO nodes (id, {', '.join(values)}) VALUES (?, {', '.join('?' for _ in values)})",
                                   (node_id, *values.values()))
            store._log_in_transaction(store.conn, "import_node", node_id, fields.get("title", ""))
            changed.append(node_id)

        counts["edges"] = _import_edges(store, bindings, replace=replace)
        if counts["edges"]:
            store._log_in_transaction(store.conn, "import_edges", details={"count": counts["edges"]})
        if not dry_run:
            from .vectors import enqueue_embedding
            for node_id in changed:
                enqueue_embedding(store, node_id, commit=False)
        if dry_run:
            store.conn.rollback()
        else:
            store.conn.commit()
    except BaseException:
        store.conn.rollback()
        raise
    return counts


def _import_edges(store, bindings, *, replace: bool) -> int:
    """Import declared arcs before legacy implicit reverse arcs, in the caller's transaction."""
    declared, reverse = {}, set()
    for from_id, edges in bindings:
        if not isinstance(edges, list):
            raise ValueError("edges must be a list")
        for edge in edges:
            if not isinstance(edge, dict) or not isinstance(edge.get("to"), str) or not edge["to"]:
                raise ValueError("Edge requires a target ID or unambiguous legacy title")
            target = _resolve(store, edge["to"]) or _resolve(store, edge["to"], by_title=True)
            if not target:
                raise ValueError("Unresolved edge target; import all nodes before importing edges")
            _weight(edge.get("weight", 0.5))
            kind, provenance = edge.get("type", "relates_to"), edge.get("provenance", "import")
            if not isinstance(kind, str) or not kind or not isinstance(provenance, str):
                raise ValueError("Edge type and provenance must be text")
            bidirectional = edge.get("bidirectional", True)
            if not isinstance(bidirectional, bool):
                raise ValueError("bidirectional must be boolean")
            key = (from_id, target["id"], redact(kind))
            fields = redact({k: edge[k] for k in ("weight", "provenance") if k in edge})
            previous = declared.setdefault(key, {})
            if any(k in previous and previous[k] != v for k, v in fields.items()):
                raise ValueError("Import conflict: duplicate edge declarations disagree")
            previous.update(fields)
            if bidirectional:
                reverse.add(key)

    count = 0
    for key, fields in declared.items():
        existing = store.conn.execute(
            "SELECT weight, provenance FROM edges WHERE from_id = ? AND to_id = ? AND type = ?", key,
        ).fetchone()
        values = {"weight": 0.5, "provenance": "import", **(dict(existing) if existing else {}), **fields}
        declared[key] = values
        if existing and dict(existing) == values:
            continue
        if existing and not replace:
            raise ValueError(f"Import conflict for edge {key}; inspect evidence before using --mode replace")
        store.conn.execute(
            "INSERT INTO edges (from_id, to_id, type, weight, provenance, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_id, to_id, type) DO UPDATE SET "
            "weight = excluded.weight, provenance = excluded.provenance, updated_at = excluded.updated_at",
            (*key, values["weight"], values["provenance"], _now()),
        )
        count += 1

    # An implied reverse is a legacy convenience, not a replacement for an
    # explicitly recorded arc. Deferring it also makes old two-way exports order-independent.
    for key in sorted(reverse):
        source, target, kind = key
        values = declared[key]
        count += store.conn.execute(
            "INSERT INTO edges (from_id, to_id, type, weight, provenance, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_id, to_id, type) DO NOTHING",
            (target, source, kind, values["weight"] * 0.8, values["provenance"], _now()),
        ).rowcount
    return count
