"""Read-only Kinbase evidence import; signing and governance stay in Kinbase.

Raw verifies immutable local events. Reduced asks Kinbase to explain each local
logical key, retaining the reduction receipt. Neither grants Kindex verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .privacy import redact
from .schema import ALL_NODE_TYPES, STANDINGS

_CEILINGS = {"human": "authoritative", "transcript": "prevalent",
             "human_review": "prevalent", "human-review": "prevalent",
             "ai_generated": "present", "agent": "present", "bot": "present",
             "unknown": "present"}
_EVENT_PATH = re.compile(r"[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}\.json\Z")
_MAX_EVENT_BYTES = 64 * 1024


def clamp_standing(standing: str, provenance: str) -> str:
    if standing not in STANDINGS:
        raise ValueError("Unsupported Kinbase standing")
    ceiling = _CEILINGS.get(provenance, "present")
    return STANDINGS[min(STANDINGS.index(standing), STANDINGS.index(ceiling))]


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _loads(raw):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def _dependencies():
    try:
        import rfc8785
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("Kinbase sync requires pip install 'kindex[kinbase]'") from exc
    return rfc8785.dumps, Ed25519PublicKey


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Kinbase validity timestamps must be RFC 3339 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Kinbase validity timestamps require a timezone")
    return parsed


def _validate(doc, *, unknown=False):
    if not isinstance(doc, dict):
        raise ValueError("Kinbase document must be an object")
    for field in ("logical_key", "question" if unknown else "statement"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            raise ValueError(f"Kinbase document requires {field}")
    if any(char in doc["logical_key"] for char in "*%?\x00"):
        raise ValueError("Kinbase logical key must be exact")
    for field in ("standing", "claimed_standing"):
        if field in doc and doc[field] not in STANDINGS:
            raise ValueError("Unsupported Kinbase standing")
    if not isinstance(doc.get("provenance", "unknown"), str):
        raise ValueError("Kinbase provenance must be a string")
    for field in ("owner_role", "owner_identity", "scope", "status", "disposition", "atom_kind"):
        if field in doc and not isinstance(doc[field], str):
            raise ValueError(f"Kinbase {field} must be a string")
    for field in ("effective_from", "effective_until", "asserted_at"):
        if doc.get(field) is not None:
            _timestamp(doc[field])
    if doc.get("effective_from") and doc.get("effective_until"):
        if _timestamp(doc["effective_until"]) <= _timestamp(doc["effective_from"]):
            raise ValueError("Kinbase validity interval is empty")
    for field in ("evidence_refs", "governs_paths"):
        if field in doc and (not isinstance(doc[field], list) or not all(isinstance(item, str) for item in doc[field])):
            raise ValueError(f"Kinbase {field} must be a list of strings")
    for field in ("anchors", "evidence_refs", "governs_paths"):
        if field in doc and not isinstance(doc[field], list):
            raise ValueError(f"Kinbase {field} must be a list")


def _read_events(root):
    canonical, public_key = _dependencies()
    events = root / ".kin" / "events"
    if (root / ".kin").is_symlink() or events.is_symlink():
        raise ValueError("Refusing symlinked Kinbase event directory")
    if not events.is_dir():
        raise ValueError("Kinbase event directory is missing or unreadable")
    documents, quarantine = [], []

    def walk_error(error):
        raise OSError("Kinbase event inventory is unreadable") from error

    for directory, dirs, files in os.walk(events, followlinks=False, onerror=walk_error):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                quarantine.append({"path": str(path.relative_to(events)), "reason": "symlink directory"})
                dirs.remove(name)
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(events).as_posix()
            try:
                if path.is_symlink():
                    raise ValueError("symlink event")
                if not _EVENT_PATH.fullmatch(relative):
                    raise ValueError("invalid content-addressed event path")
                with path.open("rb") as stream:
                    raw = stream.read(_MAX_EVENT_BYTES + 1)
                if len(raw) > _MAX_EVENT_BYTES:
                    raise ValueError("event exceeds 64 KiB")
                doc = _loads(raw)
                if not isinstance(doc, dict):
                    raise ValueError("event must be an object")
                digest = hashlib.sha256(canonical(doc)).hexdigest()
                if digest != relative.replace("/", "")[:-5]:
                    raise ValueError("content address mismatch")
                schema = doc.get("schema")
                if schema not in ("kinbase-event/1", "kinbase-unknown/1"):
                    raise ValueError("unsupported event schema")
                _validate(doc, unknown=schema == "kinbase-unknown/1")
                unsigned = {key: value for key, value in doc.items() if key != "signature"}
                message_type = b"unknown-event" if schema == "kinbase-unknown/1" else b"fact-event"
                signed_digest = hashlib.sha256(b"kinbase-sig/1\x00" + message_type + b"\x00" + canonical(unsigned)).digest()
                signer = bytes.fromhex(doc["signer"])
                try:
                    signature = bytes.fromhex(doc["signature"])
                except ValueError:
                    signature = base64.b64decode(doc["signature"], validate=True)
                public_key.from_public_bytes(signer).verify(signature, signed_digest)
                documents.append((digest, doc))
            except OSError:
                # Permission and transient IO failures are not authoritative deletions.
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                quarantine.append({"path": relative, "reason": reason or "invalid signature"})
    return sorted(documents), quarantine


def _identity(repo, identity):
    return "kinbase-" + hashlib.sha256((repo + "\x00" + identity).encode()).hexdigest()


def _reduced(root, binary, documents):
    by_event = {}
    for digest, doc in documents:
        event_id = doc.get("event_id")
        if event_id:
            by_event.setdefault(event_id, []).append((digest, doc))
    rows = []
    for key in sorted({doc["logical_key"] for _, doc in documents}):
        try:
            result = subprocess.run(
                [binary, "explain", key, "--repo", str(root), "--decision",
                 "Kindex read-only synchronization", "--json"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Kinbase explain failed; sync not applied") from exc
        if result.returncode:
            raise RuntimeError(f"Kinbase explain exited {result.returncode}; sync not applied")
        try:
            envelope = _loads(result.stdout)
            if (not isinstance(envelope, dict) or envelope.get("logical_key") != key
                    or "current" not in envelope or not isinstance(envelope.get("unknowns"), list)):
                raise ValueError("unsupported exact-key explain response")
            if not isinstance(envelope.get("as_of"), str):
                raise ValueError("explain response requires snapshot as_of")
            _timestamp(envelope["as_of"])
            if not isinstance(envelope.get("reducer_version"), str):
                raise ValueError("explain response requires reducer_version")
            receipt = {k: v for k, v in envelope.items() if k not in ("current", "unknowns")}
            current = envelope["current"]
            if current is not None:
                if not isinstance(current, dict) or current.get("logical_key") != key:
                    raise ValueError("explain current fact does not match requested key")
                matches = by_event.get(current.get("event_id"), [])
                source = matches[0] if len(matches) == 1 else None
                doc = {**(source[1] if source else {}), **current}
                _validate(doc)
                identity = source[0] if source else "reduced-fact:" + str(current.get("event_id") or current.get("fact_id") or key)
                rows.append((identity, doc, receipt))
            for unknown in envelope["unknowns"]:
                if not isinstance(unknown, dict) or unknown.get("logical_key") != key:
                    raise ValueError("explain unknown does not match requested key")
                _validate(unknown, unknown=True)
                identity = unknown.get("unknown_id")
                if not isinstance(identity, str) or not identity:
                    raise ValueError("explain unknown requires unknown_id")
                rows.append(("reduced-unknown:" + identity,
                             {**unknown, "schema": "kinbase-unknown/1"}, receipt))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Invalid Kinbase explain response: {exc}; sync not applied") from exc
    return rows


def _node(repo, identity, doc, mode, receipt):
    unknown = doc.get("schema") == "kinbase-unknown/1"
    provenance = doc.get("provenance", "unknown")
    standing = clamp_standing(doc.get("standing", "unruled"), provenance)
    body = doc["question"] if unknown else doc["statement"]
    metadata = {**doc, "repo": repo, "mode": mode, "source_identity": identity,
                "claimed_standing": doc.get("claimed_standing", doc.get("standing", "unruled")),
                "standing": standing, "provenance": provenance,
                "signature_verified": True, "signature_verification_scope": "original source bytes",
                "governance_verified": mode == "reduced"}
    if receipt is not None:
        metadata["reduction"] = receipt
        metadata["signature_verification_scope"] = "Kinbase reducer admission"
    metadata["source_document_redacted"] = redact(doc) != doc
    disposition = doc.get("disposition", "accepted")
    retired = disposition in ("rejected", "superseded", "proposed")
    if unknown:
        retired = doc.get("status") in ("closed", "abandoned", "superseded")
    elif mode == "reduced":
        retired = (retired or doc.get("status") != "current" or receipt.get("state") != "current"
                   or doc.get("trust") != "trusted" or receipt.get("projection_state") == "withheld")
    node_type = "question" if unknown else doc.get("atom_kind", "concept")
    if node_type not in ALL_NODE_TYPES:
        node_type = "concept"
    # Importing external operational rules never installs them as Kindex policy.
    # These retain atom_kind and are surfaced as labelled evidence by retrieval.
    if node_type in ("constraint", "directive", "checkpoint", "watch"):
        node_type = "concept"
    audience = {"personal": "private", "company": "org", "codebase": "team"}.get(doc.get("store_kind"), "private")
    return redact({"id": _identity(repo, identity), "type": node_type,
                   "title": body[:160], "content": body, "aka": [doc["logical_key"]],
                   "prov_when": doc.get("asserted_at", ""),
                   "domains": [doc["scope"]] if doc.get("scope") else [],
                   "status": "archived" if retired else "active", "audience": audience,
                   "standing": standing, "prov_who": [provenance],
                   "prov_source": "kinbase:" + repo, "extra": {"kinbase": metadata}})


def sync_kinbase(store, repo: str | Path, *, mode="auto", binary="kinbase") -> dict:
    """Refresh source-scoped evidence atomically; never write signed source files.

    Reduced coverage is all keys in the verified local event inventory, not the
    complete Company corpus and not a token-budgeted project selection.
    """
    if mode not in ("auto", "raw", "reduced"):
        raise ValueError("Kinbase mode must be auto, raw, or reduced")
    root = Path(repo).expanduser().resolve(strict=True)
    executable = shutil.which(str(binary))
    if mode == "auto":
        mode = "reduced" if executable else "raw"
    if mode == "reduced" and not executable:
        raise RuntimeError("Kinbase binary unavailable; use --mode raw or install Kinbase")
    documents, quarantine = _read_events(root)
    inputs = (_reduced(root, executable, documents) if mode == "reduced"
              else [(digest, doc, None) for digest, doc in documents])
    rows = [_node(str(root), identity, doc, mode, receipt) for identity, doc, receipt in inputs]
    desired = {row["id"]: row for row in rows}
    if len(desired) != len(rows):
        raise ValueError("Kinbase reduction returned duplicate identities")
    conn = store.conn
    if conn.in_transaction:
        raise ValueError("Kinbase sync requires its own transaction")
    imported = unchanged = deactivated = 0
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = {row["id"]: store._row_to_dict(row) for row in conn.execute(
            "SELECT * FROM nodes WHERE json_extract(extra, '$.kinbase.repo') = ?", (str(root),))}
        for node_id, old in previous.items():
            if node_id not in desired and old["status"] != "archived":
                metadata = old["extra"]["kinbase"]
                metadata["inactive_reason"] = "absent-or-quarantined" if mode == "raw" else "not-in-reduced-view"
                conn.execute("UPDATE nodes SET status='archived', verified_at=NULL, verified_by=NULL, "
                             "prov_method=NULL, updated_at=?, extra=? WHERE id=?",
                             (now, json.dumps(old["extra"]), node_id))
                deactivated += 1
        for node_id, node in desired.items():
            old = previous.get(node_id)
            if old and all(old.get(k) == v for k, v in node.items()):
                unchanged += 1
                continue
            values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                      for k, v in node.items()}
            values.update(updated_at=now, created_at=old["created_at"] if old else now,
                          last_accessed=old["last_accessed"] if old else now,
                          verified_at=None, verified_by=None, prov_method=None)
            columns = list(values)
            assignments = ",".join(f"{column}=excluded.{column}" for column in columns if column != "id")
            conn.execute(f"INSERT INTO nodes ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                         f"ON CONFLICT(id) DO UPDATE SET {assignments}", tuple(values.values()))
            imported += 1
        # Rebuild only source-owned edges; Kindex assertions remain untouched.
        edge_source = "kinbase:" + str(root)
        conn.execute("DELETE FROM edges WHERE provenance = ?", (edge_source,))
        facts, unknowns, evidence = {}, [], {}
        for node in rows:
            metadata = node["extra"]["kinbase"]
            evidence.setdefault(metadata.get("event_id", ""), []).append(node["id"])
            if node["status"] != "active":
                continue
            if node["type"] == "question":
                unknowns.append(node)
            else:
                facts.setdefault(metadata["logical_key"], []).append(node["id"])
        for node in unknowns:
            for target in facts.get(node["extra"]["kinbase"]["logical_key"], []):
                conn.execute("INSERT OR IGNORE INTO edges (from_id,to_id,type,weight,provenance) VALUES (?,?, 'contradicts',1,?)",
                             (node["id"], target, edge_source))
        for node in rows:
            for reference in node["extra"]["kinbase"].get("evidence_refs", []):
                for target in evidence.get(reference, []):
                    if target != node["id"]:
                        conn.execute("INSERT OR IGNORE INTO edges (from_id,to_id,type,weight,provenance) VALUES (?,?,'derived_from',0.5,?)",
                                     (node["id"], target, edge_source))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"mode": mode, "repo": str(root), "coverage": "local-event-keys",
            "imported": imported, "quarantined": len(quarantine), "unchanged": unchanged,
            "deactivated": deactivated, "quarantine": redact(quarantine)}


def attach_unknowns(store, node):
    """Attach contested questions outside top_k so selecting a fact keeps its caveat."""
    metadata = (node.get("extra") or {}).get("kinbase")
    if not isinstance(metadata, dict):
        return
    node["kinbase_unknowns"] = []
    rows = store.conn.execute(
        "SELECT * FROM nodes WHERE type='question' AND status='active' "
        "AND json_extract(extra,'$.kinbase.repo')=? "
        "AND json_extract(extra,'$.kinbase.logical_key')=? AND id != ? ORDER BY id",
        (metadata["repo"], metadata["logical_key"], node["id"]))
    for row in rows:
        question = store._row_to_dict(row)
        from .store import node_expired
        if node_expired(question):
            continue
        data = question["extra"]["kinbase"]
        node["kinbase_unknowns"].append({"id": question["id"], "question": question["content"],
            "owner_role": data.get("owner_role", ""), "owner_identity": data.get("owner_identity", ""),
            "status": data.get("status", "open")})


def evidence_note(node):
    metadata = (node.get("extra") or {}).get("kinbase")
    if not isinstance(metadata, dict):
        return ""
    mode = metadata["mode"]
    label = "raw signed evidence; governance not evaluated" if mode == "raw" else "reduced snapshot"
    note = f"Kinbase {label}; standing={node.get('standing', 'unruled')}"
    if mode == "reduced":
        receipt = metadata.get("reduction", {})
        note += f"; as_of={receipt.get('as_of', 'unknown')}"
        note += f"; projection={receipt.get('projection_state', 'unknown')}"
        note += f"; source_trusted={receipt.get('trusted', False)}"
    questions = list(node.get("kinbase_unknowns", []))
    if node.get("type") == "question":
        questions.insert(0, {**metadata, "question": node["content"]})
    for question in questions:
        owner = " / ".join(filter(None, (question.get("owner_role"), question.get("owner_identity")))) or "unresolved"
        note += f"\nUnknown [{question.get('status', 'open')}]: {question['question']} (owner: {owner})"
    return note
