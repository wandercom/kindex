"""Explicit transport for selected shareable codebase evidence in Git.

This is not Guildhall's signed authority protocol. A clone's knowledge is imported
as quarantined evidence, never as policy, configuration, or trusted instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .privacy import redact

SCHEMA = "kindex-evidence/1"
MAX_BYTES = 2 * 1024 * 1024
ALLOWED_TYPES = frozenset({"concept", "decision", "question"})
_RECORD_FIELDS = frozenset({"id", "title", "content", "type", "audience", "domains",
                            "updated_at", "connections", "provenance", "verification", "referent"})


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _path(root):
    path = Path(root) / ".kin" / "knowledge.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing symlinked repo evidence")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate repo evidence field")
        result[key] = value
    return result


def _validate_record(record):
    from .schema import EDGE_TYPES
    from .store import (_clean_capture_text, _CAPTURE_TITLE_LIMIT,
                        _CAPTURE_CONTENT_LIMIT, _AUDIT_TEXT_LIMIT, Store)

    if not isinstance(record, dict) or record.keys() - _RECORD_FIELDS:
        raise ValueError("Unsupported repo evidence record fields")
    if record.get("type") not in ALLOWED_TYPES or record.get("audience") not in {"public", "team"}:
        raise ValueError("Invalid repo evidence record")
    if not isinstance(record.get("id"), str) or not record["id"].strip():
        raise ValueError("Invalid repo evidence ID")
    # Use the actual quarantine contract before writing or importing any item.
    # Oversized evidence needs explicit curation, never silent truncation.
    _clean_capture_text(record.get("title"), field="title", limit=_CAPTURE_TITLE_LIMIT)
    _clean_capture_text(record.get("content"), field="content", limit=_CAPTURE_CONTENT_LIMIT, content=True)
    if not isinstance(record.get("domains", []), list):
        raise ValueError("Invalid repo evidence domains")
    for domain in record.get("domains", []):
        _clean_capture_text(domain, field="domain", limit=_AUDIT_TEXT_LIMIT)
    if not isinstance(record.get("connections", []), list):
        raise ValueError("Invalid repo evidence connections")
    for edge in record.get("connections", []):
        if (not isinstance(edge, dict) or edge.keys() - {"target", "target_title", "type", "why"}
                or not isinstance(edge.get("target"), str) or not edge["target"].strip()
                or edge.get("type") not in EDGE_TYPES):
            raise ValueError("Invalid repo evidence connection")
        Store._canonical_connections([{"from_title": record["title"],
            "to_title": edge.get("target_title"), "type": edge["type"], "why": edge.get("why", "")}])
    for field in ("provenance", "verification", "referent"):
        if field in record and not isinstance(record[field], dict):
            raise ValueError("Invalid repo evidence metadata")
    if redact(record) != record:
        raise ValueError("Repo evidence contains recognized credentials; refuse to propagate historical bytes")


def _load(path):
    if not path.exists():
        return {"schema": SCHEMA, "records": {}}
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Repo evidence exceeds 2 MiB")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or value.keys() != {"schema", "records"}
            or value.get("schema") != SCHEMA or not isinstance(value.get("records"), dict)):
        raise ValueError("Unsupported repo evidence schema")
    for digest, record in value["records"].items():
        if hashlib.sha256(_encoded(record).encode()).hexdigest() != digest:
            raise ValueError("Repo evidence digest mismatch")
        _validate_record(record)
    records = list(value["records"].values())
    endpoints = {(record["id"], record["title"]) for record in records}
    for record in records:
        for edge in record.get("connections", []):
            if (edge["target"], edge["target_title"]) not in endpoints:
                raise ValueError("Repo evidence connection target is absent")
    return value


@contextmanager
def _artifact_lock(root):
    """Serialize file union even for callers using distinct SQLite stores.

    The lock is local-only and process-lifetime scoped; a crash releases it.
    A concurrent publisher/importer gets an explicit retry, not a lost update.
    This is not isolation against a hostile process running as the same user.
    """
    import fcntl

    path = _path(root)
    local = path.parent / "local"
    if local.is_symlink():
        raise ValueError("Refusing symlinked repo evidence lock directory")
    local.mkdir(parents=True, exist_ok=True)
    fd = os.open(local / "repo-memory.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Repo evidence is being updated; retry publication/import") from None
        yield
    finally:
        os.close(fd)


def publish(store, root, node_ids: list[str]) -> dict:
    # Serialize append/union against other publishers using the same repo-local
    # SQLite writer lock. Atomic rename alone does not prevent lost updates.
    if store.conn.in_transaction:
        raise ValueError("Repo publication requires its own transaction")
    with _artifact_lock(root):
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            result = _publish_locked(store, root, node_ids)
            store.conn.commit()
            return result
        except BaseException:
            store.conn.rollback()
            raise


def _publish_locked(store, root, node_ids: list[str]) -> dict:
    """Append explicitly selected shareable evidence, not certified assertions."""
    if not node_ids:
        raise ValueError("Publish requires explicit shareable node IDs")
    path = _path(root)
    document = _load(path)
    added = 0
    selected = {}
    for node_id in sorted(set(node_ids)):
        row = store.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        node = store._row_to_dict(row) if row is not None else None
        if not node or node.get("type") not in ALLOWED_TYPES or node.get("status") in {"archived", "superseded"}:
            raise ValueError("Publish only active concept, decision, or question nodes")
        if node.get("audience") not in {"public", "team"}:
            raise ValueError("Publish requires explicit public/team audience; private nodes never enter Git")
        selected[node_id] = node
    for node_id, node in selected.items():
        record = redact({"id": node["id"], "title": node["title"], "content": node.get("content", ""),
                         "type": node["type"], "audience": node["audience"], "domains": node.get("domains", []),
                         "updated_at": node.get("updated_at", ""),
                         "connections": sorted([
                             {"target": edge["to_id"], "target_title": selected[edge["to_id"]]["title"],
                              "type": edge["type"], "why": edge.get("provenance") or ""}
                             for edge in store.edges_from(node_id) if edge["to_id"] in selected
                         ], key=_encoded),
                         "provenance": {key: node[key] for key in
                            ("prov_who", "prov_when", "prov_activity", "prov_why", "prov_source") if node.get(key)},
                         "verification": {key: node[key] for key in
                            ("verified_at", "verified_by", "prov_method", "valid_at", "invalid_at") if node.get(key)},
                         **({"referent": node["referent"]} if node.get("referent") else {})})
        _validate_record(record)
        digest = hashlib.sha256(_encoded(record).encode()).hexdigest()
        if digest not in document["records"]:
            document["records"][digest] = record
            added += 1
    output = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(output.encode()) > MAX_BYTES:
        raise ValueError("Repo evidence exceeds 2 MiB; curate the transport explicitly")
    path.parent.mkdir(exist_ok=True)
    # The caller publishes an explicit set. Write only after every item passed.
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".knowledge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"added": added, "records": len(document["records"]), "path": str(path), "authority": "untrusted-evidence"}


def import_candidates(store, root) -> dict:
    from .store import (_clean_capture_text, _CAPTURE_TITLE_LIMIT,
                        _CAPTURE_CONTENT_LIMIT, _AUDIT_TEXT_LIMIT, Store)

    if store.conn.in_transaction:
        raise ValueError("Repo import requires its own per-candidate transactions")
    with _artifact_lock(root):
        document = _load(_path(root))
        ids, already_present = [], 0
        incoming = {}
        for record in document["records"].values():
            for edge in record.get("connections", []):
                proposal = {"from_title": record["title"], "to_title": edge["target_title"],
                            "type": edge["type"], "why": edge.get("why", "")}
                incoming.setdefault((edge["target"], edge["target_title"]), []).append(proposal)
        for digest, record in document["records"].items():
            # Duplicate each proposal onto both endpoint candidates so either
            # acceptance order can resolve the edge. The existing review gate
            # still binds the proposal and current graph; no auto-accept occurs.
            connections = incoming.get((record["id"], record["title"]), []) + [
                {"from_title": record["title"], "to_title": edge["target_title"],
                 "type": edge["type"], "why": edge.get("why", "")}
                for edge in record.get("connections", [])]
            payload = {
                "title": _clean_capture_text(record["title"], field="title", limit=_CAPTURE_TITLE_LIMIT),
                "content": _clean_capture_text(record["content"], field="content", limit=_CAPTURE_CONTENT_LIMIT, content=True),
                "node_type": record["type"],
                "domains": sorted({_clean_capture_text(domain, field="domain", limit=_AUDIT_TEXT_LIMIT)
                                   for domain in record.get("domains", [])}),
                "connections": Store._canonical_connections(connections),
            }
            payload_digest = hashlib.sha256(_encoded(payload).encode()).hexdigest()
            existing = store.conn.execute(
                "SELECT id FROM capture_candidates WHERE source_digest = ? OR payload_digest = ? "
                "ORDER BY created_at, id LIMIT 1", (digest, payload_digest),
            ).fetchone()
            if existing is not None:
                # Capture already deduplicates equivalent pending payloads.
                # Extend that here to terminal review receipts too: a second
                # source cannot endlessly restage the same accepted/rejected
                # subject. All distinct provenance stays in the Git artifact.
                ids.append(existing["id"])
                already_present += 1
                continue
            # Provenance/verification in Git is evidence only; never install
            # the clone's claimed reviewer as local verification authority.
            ids.append(store.add_capture_candidate(**payload, source_digest=digest))
    return {"candidates": ids, "authority": "quarantined", "records": len(ids),
            "new": len(ids) - already_present, "already_present": already_present}
