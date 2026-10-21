"""Opt-in version-bound references, declared corrections and advisory dream routes.

Host contexts are local trust conventions, not authentication. Reads never
initialize storage; writers never change native sources or ordinary retrieval.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
import re
import sqlite3
from types import MappingProxyType
from types import SimpleNamespace
import uuid
from pydantic import BaseModel, ConfigDict

from .agent_adapters import ADAPTER_SCOPE_NAMES, node_scope_clients, normalize_adapter
from .privacy import redact_serialized
from .sessions import normalize_project_path
from .trust import _base_trust_decision, parse_rfc3339, validate_interval

__all__ = ["ReadContext", "WriteContext", "ReferenceError", "capture_reference",
           "record_correction", "retire_correction", "resolve_references",
           "expand_references", "compile_dream_index", "record_dream_index",
           "decode_evidence_text"]
_WARNING = "References and generated connections are advisory navigation, not truth, permission, completion, or verified applicability."
_META = ("type", "title", "prov_who", "prov_when", "prov_activity", "prov_source", "status", "audience", "standing", "verified_at", "verified_by", "prov_method", "valid_at", "invalid_at", "referent", "asserted_at", "true_of", "domains")
_EXTRA = ("project_path", "expires", "superseded_by", "referent_stale", "expired_at", "kinbase", "v8_source_binding")
_CONTEXT = ("graph_id", "profile", "client", "session_id", "current_project", "correction_policy", "partitions")
_REF = ("node_id", "graph_id", "profile", "partition_id", "body_sha256", "metadata_sha256")
_TABLES = {"v8_registry_meta", "v8_corrections", "v8_correction_retirements", "v8_dream_batches"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DREAM_RELATIONS = ("analogous_mechanism", "contrasting_conditions", "shared_constraint_pattern")
_INCOMING_ROW_LIMIT = 1024


class ReferenceError(ValueError):
    def __init__(self, code="invalid_request"):
        self.code = code
        super().__init__("Correction reference operation is unavailable")


def _fail(code="invalid_request"):
    raise ReferenceError(code)


def _j(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is dict:
                if any(type(key) is not str for key in item): _fail()
                pending.extend(item.keys()); pending.extend(item.values())
            elif type(item) is list: pending.extend(item)
            elif type(item) is str: item.encode("utf-8")
            elif item is None or type(item) in (int, bool): pass
            elif type(item) is float and math.isfinite(item): pass
            else: _fail()
        return raw
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        _fail()


def _h(raw): return hashlib.sha256(raw).hexdigest()
def _frame(name, value): return _h(b"kindex-v8/"+name.encode("ascii")+b"\0"+_j(value))


def _loads(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: _fail()
            out[key] = value
        return out
    def constant(_): _fail()
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        _j(value)
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError): _fail()


def _obj(value, keys):
    if type(value) is not dict or set(value) != set(keys): _fail()


def _str(value, maximum=256, minimum=1):
    if type(value) is not str: _fail()
    try: size = len(value.encode("utf-8"))
    except UnicodeError: _fail()
    if size < minimum or size > maximum: _fail()
    return value


def _int(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper: _fail()
    return value


def _hash(value):
    if type(value) is not str or not _HEX.fullmatch(value): _fail()
    return value


def _time(value):
    _str(value, 64)
    try: return parse_rfc3339(value, field="timestamp")
    except ValueError: _fail()


def _interval(start, end):
    try: validate_interval(start, end)
    except (ValueError, TypeError): _fail()


def _path(value):
    _str(value, 8192)
    if not os.path.isabs(value) or normalize_project_path(value) != value: _fail()
    return value


def _unique_strings(value, maximum, width=128, *, empty=False):
    if type(value) is not list or len(value) > maximum or (not empty and not value): _fail()
    for item in value: _str(item, width)
    if len(set(value)) != len(value): _fail()


def _freeze(value):
    if type(value) is dict: return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if type(value) is list: return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType): return {k: _thaw(v) for k, v in value.items()}
    if type(value) is tuple: return [_thaw(v) for v in value]
    return copy.deepcopy(value)


def _context_shape(value, write=False):
    _obj(value, (*_CONTEXT, "actor_id", "recorded_at", "explicit_action") if write else _CONTEXT)
    _j(value)
    try:
        if str(uuid.UUID(value["graph_id"])) != value["graph_id"]: _fail()
    except (ValueError, TypeError, AttributeError): _fail()
    _str(value["profile"], 256, 0); _str(value["session_id"]); _path(value["current_project"])
    if type(value["client"]) is not str or value["client"] not in ADAPTER_SCOPE_NAMES | {"unscoped_only"}: _fail()
    partitions = value["partitions"]
    if type(partitions) is not list or len(partitions) > 8: _fail()
    ids, projects, origins = set(), set(), set()
    for part in partitions:
        _obj(part, ("partition_id", "kind", "project_path", "kinbase"))
        pid = _str(part["partition_id"], 128)
        if pid in ids: _fail()
        ids.add(pid)
        if part["kind"] in ("personal", "repo"):
            project = _path(part["project_path"])
            if project in projects or part["kinbase"] is not None: _fail()
            projects.add(project)
        elif part["kind"] == "kinbase":
            if part["project_path"] is not None: _fail()
            kb = part["kinbase"]
            _obj(kb, ("repo", "scope", "store_kind", "receipt_kind"))
            _str(kb["repo"], 8192); _str(kb["scope"], 512)
            if kb["store_kind"] not in ("personal", "company", "codebase") or kb["receipt_kind"] not in ("native_import", "synthetic_fixture"): _fail()
            origin = (kb["repo"], kb["scope"], kb["store_kind"])
            if origin in origins: _fail()
            origins.add(origin)
        else: _fail()
    policy = value["correction_policy"]
    _obj(policy, ("policy_id", "policy_sha256", "allowed_actor_ids"))
    _str(policy["policy_id"], 128); _hash(policy["policy_sha256"])
    _unique_strings(policy["allowed_actor_ids"], 32, 256, empty=True)
    document = {key: value[key] for key in ("graph_id", "profile", "session_id", "current_project", "partitions")}
    document.update(policy_id=policy["policy_id"], allowed_actor_ids=policy["allowed_actor_ids"])
    if _h(_j(document)) != policy["policy_sha256"]: _fail("invalid_context")
    if write:
        _str(value["actor_id"]); _time(value["recorded_at"])
        if value["explicit_action"] is not True: _fail("invalid_context")


class ReadContext(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    graph_id: str
    profile: str
    client: str
    session_id: str
    current_project: str
    correction_policy: object
    partitions: object

    def __init__(self, **data):
        raw = copy.deepcopy(data)
        _context_shape(raw, write="actor_id" in type(self).model_fields)
        super().__init__(**raw)
        for key, value in raw.items(): object.__setattr__(self, key, _freeze(value))

    def to_dict(self): return {key: _thaw(getattr(self, key)) for key in _CONTEXT}


class WriteContext(ReadContext):
    actor_id: str
    recorded_at: str
    explicit_action: bool

    def to_dict(self):
        return {key: _thaw(getattr(self, key)) for key in (*_CONTEXT, "actor_id", "recorded_at", "explicit_action")}


def _ctx(context, write=False):
    if not isinstance(context, WriteContext if write else ReadContext): _fail("invalid_context")
    value = context.to_dict()
    _context_shape(value, isinstance(context, WriteContext))
    return value


def _client(node, context):
    tags = node.get("domains")
    if type(tags) is not list or any(type(tag) is not str for tag in tags): return False
    for raw in tags:
        tag = raw.strip().lower()
        if tag.startswith(("client:", "agent:")):
            if normalize_adapter(tag.split(":", 1)[1]) not in ADAPTER_SCOPE_NAMES: return False
    clients = node_scope_clients(tags)
    return not clients if context["client"] == "unscoped_only" else not clients or context["client"] in clients


def _raw(store, node_id):
    row = store.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if row is None: return None
    node = dict(row)
    try:
        for key in ("domains", "prov_who", "referent", "extra"):
            if type(node.get(key)) is str: node[key] = _loads(node[key])
        if type(node.get("extra")) is not dict or type(node.get("content")) is not str: return None
        _str(node["id"]); _j(node)
    except ReferenceError: return None
    return node


def _metadata(node):
    result = {key: node.get(key) for key in _META}
    result["extra"] = {key: node["extra"][key] for key in _EXTRA if key in node["extra"]}
    return result


def _valid_metadata(node):
    try:
        _interval(node.get("valid_at"), node.get("invalid_at"))
        for key in ("verified_at", "asserted_at", "true_of"):
            if node.get(key) is not None: _time(node[key])
        extra = node["extra"]
        if "expires" in extra:
            if type(extra["expires"]) is not str or date.fromisoformat(extra["expires"]).isoformat() != extra["expires"]: return False
        if "referent_stale" in extra and type(extra["referent_stale"]) is not bool: return False
        if "v8_source_binding" in extra and type(extra["v8_source_binding"]) is not dict: return False
        return True
    except (ValueError, TypeError): return False


def _partition(node, context):
    if not _client(node, context) or not _valid_metadata(node): return None
    extra = node["extra"]
    matches = []
    if "kinbase" in extra:
        kb = extra["kinbase"]
        if type(kb) is not dict: return None
        for part in context["partitions"]:
            if part["kind"] != "kinbase": continue
            origin = part["kinbase"]
            if any(kb.get(key) != origin[key] for key in ("repo", "scope", "store_kind")): continue
            if node.get("prov_source") != "kinbase:"+origin["repo"] or node.get("audience") != {"personal": "private", "company": "org", "codebase": "team"}[origin["store_kind"]]: continue
            if type(kb.get("source_identity")) is not str or not kb["source_identity"] or kb.get("source_document_redacted") is not False: continue
            if origin["receipt_kind"] == "native_import":
                if kb.get("signature_verified") is not True: continue
            else:
                binding = extra.get("v8_source_binding")
                if kb.get("signature_verified") is not False or type(binding) is not dict or binding.get("receipt_kind") != "synthetic_fixture": continue
                try: _hash(binding.get("source_bank_sha256"))
                except ReferenceError: continue
            matches.append(part)
    else:
        project = extra.get("project_path")
        try: _path(project)
        except ReferenceError: return None
        matches = [p for p in context["partitions"] if p["kind"] in ("personal", "repo") and p["project_path"] == project]
    return matches[0] if len(matches) == 1 else None


def _base_tier(node, part, at):
    extra = node["extra"]
    if extra.get("expires") and date.fromisoformat(extra["expires"]) < at.date(): return "historical"
    if part["kind"] != "kinbase":
        return "current" if _base_trust_decision(node, at=at).eligible else "historical"
    if node.get("status") != "active" or extra.get("referent_stale"): return "historical"
    if node.get("valid_at") is not None and at < _time(node["valid_at"]): return "historical"
    if node.get("invalid_at") is not None and at >= _time(node["invalid_at"]): return "historical"
    kb = extra["kinbase"]
    if kb.get("status") != "current": return "historical"
    if part["kinbase"]["receipt_kind"] == "synthetic_fixture":
        valid = kb.get("mode") == "synthetic_fixture" and kb.get("governance_verified") is False and kb.get("signature_verified") is False and kb.get("trust") == "synthetic_evidence" and "reduction" not in kb
    else:
        reduction = kb.get("reduction")
        valid = kb.get("mode") == "reduced" and kb.get("governance_verified") is True and kb.get("trust") == "trusted" and type(reduction) is dict and reduction.get("state") == "current" and "projection_state" in reduction and reduction["projection_state"] != "withheld"
    return "evidence_only" if valid else "historical"


def _privacy(value):
    raw = _j(value).decode("utf-8")
    if redact_serialized(raw) != raw: _fail("privacy_unavailable")


def _info(node, context, at, *, privacy=True):
    if node is None: return None
    part = _partition(node, context)
    if part is None: return None
    metadata = _metadata(node)
    if privacy: _privacy({"metadata": metadata, "body": node["content"]})
    reference = {"node_id": node["id"], "graph_id": context["graph_id"], "profile": context["profile"], "partition_id": part["partition_id"], "body_sha256": _h(node["content"].encode()), "metadata_sha256": _frame("metadata", metadata)}
    return {"node": node, "metadata": metadata, "reference": reference, "partition": part, "tier": _base_tier(node, part, at)}


def _ref_shape(ref):
    _obj(ref, _REF); _str(ref["node_id"]); _str(ref["partition_id"], 128); _str(ref["profile"], 256, 0)
    try:
        if str(uuid.UUID(ref["graph_id"])) != ref["graph_id"]: _fail()
    except (ValueError, TypeError, AttributeError): _fail()
    _hash(ref["body_sha256"]); _hash(ref["metadata_sha256"])


def _owner(store, context, write=False):
    conn = store.conn
    graph = conn.execute("SELECT value FROM meta WHERE key='graph_id'").fetchone()
    stamp = conn.execute("SELECT value FROM meta WHERE key='kin_profile'").fetchone()
    selected = getattr(store.config, "active_profile", None) or ""
    if graph is None or graph[0] != context["graph_id"] or selected != context["profile"] or (stamp[0] if stamp else "") != selected: _fail("unavailable")
    project = getattr(store.config, "_project_path", None)
    if project and normalize_project_path(str(project)) != context["current_project"]: _fail("unavailable")
    owner = _raw(store, context["session_id"])
    if owner is None or owner.get("type") != "session" or owner["extra"].get("project_path") != context["current_project"] or not _client(owner, context): _fail("unavailable")
    if write and context["actor_id"] not in context["correction_policy"]["allowed_actor_ids"]: _fail("actor_not_allowed")


def _existing(store, context, *, write=False):
    """Qualify an unopened explicitly selected store without native write-open."""
    if getattr(store, "_conn", None) is not None: return
    path = store.db_path
    if not path.is_file(): _fail("unavailable")
    conn = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        from .schema import SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION): _fail("unavailable")
        _owner(SimpleNamespace(conn=conn, config=store.config), context, write=write)
    finally:
        conn.close()


@contextmanager
def _read(store, context):
    if not getattr(store, "read_only", False) or getattr(store, "_migrate", True) is not False or getattr(store, "_manage_project_storage", True) is not False: _fail("unavailable")
    _existing(store, context)
    conn = store.conn
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1: _fail("unavailable")
    owned = not conn.in_transaction
    if owned: conn.execute("BEGIN")
    try:
        from .schema import SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION): _fail("unavailable")
        _owner(store, context)
        yield
    finally:
        if owned: conn.rollback()


@contextmanager
def _write(store, context):
    if getattr(store, "read_only", False): _fail("unavailable")
    try:
        _existing(store, context, write=True)
    except (sqlite3.Error, OSError):
        _fail("unavailable")
    conn = getattr(store, "_conn", None)
    opened = conn is None
    # Native writable Store.conn initializes/repairs schema before returning.
    # This opt-in registry writer instead opens only an existing selected file;
    # all owner checks and registry changes occur in the owned TX/savepoint.
    if opened:
        # SQLite itself must enforce existing-file opening. An existence check
        # alone races deletion and an ordinary connection would recreate the DB.
        try:
            if not store.db_path.is_file(): _fail("unavailable")
            conn = sqlite3.connect(store.db_path.resolve().as_uri()+"?mode=rw", uri=True,
                                   timeout=getattr(store, "_sqlite_timeout", 5.0))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
        except (sqlite3.Error, OSError):
            if conn is not None: conn.close()
            _fail("unavailable")
        selected = SimpleNamespace(conn=conn, config=store.config)
    else: selected = store
    owned = not conn.in_transaction
    savepoint = "v8_"+uuid.uuid4().hex
    begun = False
    try:
        conn.execute("BEGIN IMMEDIATE" if owned else "SAVEPOINT "+savepoint)
        begun = True
        from .schema import SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION): _fail("unavailable")
        _owner(selected, context, write=True)
        yield selected, "committed" if owned else "pending_caller"
        if owned: conn.commit()
        else: conn.execute("RELEASE SAVEPOINT "+savepoint)
    except BaseException as exc:
        if begun:
            if owned: conn.rollback()
            else:
                conn.execute("ROLLBACK TO SAVEPOINT "+savepoint); conn.execute("RELEASE SAVEPOINT "+savepoint)
        if isinstance(exc, sqlite3.Error): _fail("unavailable")
        raise
    finally:
        if opened: conn.close()


def _registry(conn, create=False):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'v8_%'")}
    if not tables:
        if not create: return False
        for statement in (
            "CREATE TABLE v8_registry_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            "CREATE TABLE v8_corrections(assertion_id TEXT PRIMARY KEY,from_id TEXT NOT NULL,to_id TEXT NOT NULL,payload_json TEXT NOT NULL,writer_json TEXT NOT NULL)",
            "CREATE INDEX v8_corrections_to ON v8_corrections(to_id,assertion_id)",
            "CREATE INDEX v8_corrections_from ON v8_corrections(from_id,assertion_id)",
            "CREATE TABLE v8_correction_retirements(assertion_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL)",
            "CREATE TABLE v8_dream_batches(batch_id TEXT PRIMARY KEY,compiled_json TEXT NOT NULL,writer_json TEXT NOT NULL)"):
            conn.execute(statement)
        conn.execute("INSERT INTO v8_registry_meta VALUES('schema_version','1')")
        return True
    if tables != _TABLES: _fail("invalid_registry")
    version = conn.execute("SELECT value FROM v8_registry_meta WHERE key='schema_version'").fetchone()
    if version is None or version[0] != "1": _fail("registry_version")
    return True


def _bound(store, ref, context, at):
    _ref_shape(ref)
    if ref["graph_id"] != context["graph_id"] or ref["profile"] != context["profile"]: _fail("unavailable")
    info = _info(_raw(store, ref["node_id"]), context, at)
    if info is None: _fail("unavailable")
    if info["reference"] != ref: _fail("stale_reference")
    return info


def _spans(spans, body=None):
    if type(spans) is not list or not 1 <= len(spans) <= 4: _fail("invalid_span")
    for span in spans:
        _obj(span, ("start_byte", "end_byte", "literal"))
        start = _int(span["start_byte"], 0, 2**63-1); end = _int(span["end_byte"], 1, 2**63-1)
        raw = _str(span["literal"], 2048).encode()
        if end <= start or end-start != len(raw): _fail("invalid_span")
        if body is not None and (end > len(body) or body[start:end] != raw): _fail("invalid_span")


def _assertion(assertion):
    _obj(assertion, ("schema", "from_ref", "to_ref", "subject_key", "claim_key", "effect", "effective_from", "effective_until", "condition", "from_spans", "to_spans"))
    _j(assertion)
    if assertion["schema"] != "correction-assertion/1": _fail()
    _ref_shape(assertion["from_ref"]); _ref_shape(assertion["to_ref"])
    if assertion["from_ref"]["node_id"] == assertion["to_ref"]["node_id"] or assertion["from_ref"]["partition_id"] != assertion["to_ref"]["partition_id"]: _fail()
    _str(assertion["subject_key"], 128); _str(assertion["claim_key"], 128)
    if assertion["effect"] not in ("replace", "qualify", "retract") or assertion["condition"] not in ("unconditional", "unresolved"): _fail()
    _interval(assertion["effective_from"], assertion["effective_until"])
    _spans(assertion["from_spans"]); _spans(assertion["to_spans"])


def _writer(context):
    return {"actor_id": context["actor_id"], "recorded_at": context["recorded_at"], "context_sha256": _frame("context", context)}


def _writer_shape(writer):
    _obj(writer, ("actor_id", "recorded_at", "context_sha256"))
    _str(writer["actor_id"]); _time(writer["recorded_at"]); _hash(writer["context_sha256"])


def _write_receipt(kind, identity, payload, inserted, state, context):
    result = {"schema": "registry-write/1", "kind": kind, "id": identity,
              "payload_sha256": _h(_j(payload)), "inserted": inserted,
              "transaction_state": state, "context_sha256": _frame("context", context)}
    _privacy(result)
    return result


def record_correction(store, assertion, *, context):
    ctx = _ctx(context, True); _assertion(assertion)
    at = _time(ctx["recorded_at"])
    with _write(store, ctx) as (store, state):
        source = _bound(store, assertion["from_ref"], ctx, at)
        target = _bound(store, assertion["to_ref"], ctx, at)
        if source["partition"]["kind"] not in ("personal", "repo") or target["partition"]["kind"] not in ("personal", "repo") or source["tier"] != "current": _fail("unavailable")
        _spans(assertion["from_spans"], source["node"]["content"].encode())
        _spans(assertion["to_spans"], target["node"]["content"].encode())
        _privacy(assertion)
        identity = _frame("correction", assertion)
        present = _registry(store.conn)
        row = store.conn.execute("SELECT payload_json,writer_json FROM v8_corrections WHERE assertion_id=?", (identity,)).fetchone() if present else None
        if row is not None:
            if row["payload_json"] != _j(assertion).decode(): _fail("invalid_registry")
            _writer_shape(_loads(row["writer_json"]))
        else:
            _registry(store.conn, True)
            store.conn.execute("INSERT INTO v8_corrections VALUES(?,?,?,?,?)", (identity, assertion["from_ref"]["node_id"], assertion["to_ref"]["node_id"], _j(assertion).decode(), _j(_writer(ctx)).decode()))
        return _write_receipt("correction", identity, assertion, row is None, state, ctx)


def retire_correction(store, assertion_id, *, context, reason, recorded_at):
    ctx = _ctx(context, True); _hash(assertion_id); _str(reason, 512); _time(recorded_at)
    if recorded_at != ctx["recorded_at"]: _fail()
    at = _time(recorded_at)
    with _write(store, ctx) as (store, state):
        if not _registry(store.conn): _fail("unavailable")
        row = store.conn.execute("SELECT * FROM v8_corrections WHERE assertion_id=?", (assertion_id,)).fetchone()
        if row is None: _fail("unavailable")
        assertion = _loads(row["payload_json"]); _assertion(assertion)
        if _frame("correction", assertion) != assertion_id or row["from_id"] != assertion["from_ref"]["node_id"] or row["to_id"] != assertion["to_ref"]["node_id"]: _fail("invalid_registry")
        _writer_shape(_loads(row["writer_json"]))
        _bound(store, assertion["from_ref"], ctx, at); _bound(store, assertion["to_ref"], ctx, at)
        old = store.conn.execute("SELECT receipt_json FROM v8_correction_retirements WHERE assertion_id=?", (assertion_id,)).fetchone()
        payload = {"assertion_id": assertion_id, "reason": reason, "writer": _writer(ctx)}
        if old is not None:
            payload = _loads(old[0]); _obj(payload, ("assertion_id", "reason", "writer")); _writer_shape(payload["writer"])
            if payload["assertion_id"] != assertion_id or payload["reason"] != reason: _fail("operation_conflict")
        else:
            _privacy(payload)
            store.conn.execute("INSERT INTO v8_correction_retirements VALUES(?,?)", (assertion_id, _j(payload).decode()))
        return _write_receipt("retirement", assertion_id, payload, old is None, state, ctx)


def _snapshot_shape(snapshot, *, admission=True):
    _obj(snapshot, ("schema", "graph_id", "profile", "context", "cutoff", "sources")); _j(snapshot)
    if snapshot["schema"] != "dream-snapshot/1": _fail("invalid_snapshot")
    ctx = snapshot["context"]; _context_shape(ctx)
    if snapshot["graph_id"] != ctx["graph_id"] or snapshot["profile"] != ctx["profile"]: _fail("invalid_snapshot")
    at = _time(snapshot["cutoff"])
    sources = snapshot["sources"]
    if type(sources) is not list or not 2 <= len(sources) <= 54 or len(_j(snapshot)) > 131072: _fail("invalid_snapshot")
    ids, records = [], {}
    for source in sources:
        _obj(source, ("reference", "metadata", "body", "episode_id")); _ref_shape(source["reference"])
        _str(source["episode_id"], 256); _str(source["body"], 1800, 0)
        metadata = source["metadata"]; _obj(metadata, (*_META, "extra"))
        if type(metadata["extra"]) is not dict or set(metadata["extra"])-set(_EXTRA): _fail("invalid_snapshot")
        ref = source["reference"]
        node = {**metadata, "id": ref["node_id"], "content": source["body"]}
        if admission:
            info = _info(node, ctx, at, privacy=False)
            if info is None or info["reference"] != ref or info["tier"] == "historical": _fail("invalid_snapshot")
        else:
            # Stored-index recall checks immutable structure/integrity only;
            # selected supports are admitted against the live read snapshot.
            if ref["graph_id"] != ctx["graph_id"] or ref["profile"] != ctx["profile"] or ref["body_sha256"] != _h(source["body"].encode()) or ref["metadata_sha256"] != _frame("metadata", metadata): _fail("invalid_snapshot")
        if ref["node_id"] in records: _fail("invalid_snapshot")
        ids.append(ref["node_id"]); records[ref["node_id"]] = source
    if ids != sorted(ids): _fail("invalid_snapshot")
    return ctx, records


def _proposal_shape(snapshot, proposals, records):
    """Validate bounded stored proposal structure without constructing an index."""
    _obj(proposals, ("schema", "snapshot_sha256", "generator_protocol_sha256", "proposals")); _j(proposals)
    if proposals["schema"] != "dream-proposals/1" or _hash(proposals["snapshot_sha256"]) != _h(_j(snapshot)): _fail("invalid_proposals")
    _hash(proposals["generator_protocol_sha256"])
    values = proposals["proposals"]
    if type(values) is not list or len(values) > 24: _fail("invalid_proposals")
    ids, pairs = set(), {}
    for proposal in values:
        _obj(proposal, ("proposal_id", "relation", "motif", "supports", "differences", "transfer_checks"))
        pid = _str(proposal["proposal_id"], 64)
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", pid) or pid in ids or proposal["relation"] not in _DREAM_RELATIONS: _fail("invalid_proposals")
        ids.add(pid); _str(proposal["motif"], 512)
        for key in ("differences", "transfer_checks"):
            if type(proposal[key]) is not list or not 1 <= len(proposal[key]) <= 4: _fail("invalid_proposals")
            for value in proposal[key]: _str(value, 512)
        supports = proposal["supports"]
        if type(supports) is not list or len(supports) != 2: _fail("invalid_proposals")
        support_ids = []
        for support in supports:
            _obj(support, ("reference", "spans")); _ref_shape(support["reference"])
            node_id = support["reference"]["node_id"]
            source = records.get(node_id)
            if source is None or source["reference"] != support["reference"]: _fail("invalid_proposals")
            _spans(support["spans"], source["body"].encode())
            support_ids.append(node_id)
        if support_ids != sorted(set(support_ids)) or records[support_ids[0]]["episode_id"] == records[support_ids[1]]["episode_id"]: _fail("invalid_proposals")
        pair = tuple(support_ids); pairs[pair] = pairs.get(pair, 0)+1
        if pairs[pair] > 3: _fail("invalid_proposals")


def compile_dream_index(snapshot, proposals):
    """Pure structural compiler; preserves protocol commitment, not its preimage."""
    _, records = _snapshot_shape(snapshot)
    _proposal_shape(snapshot, proposals, records)
    compiled = {"schema": "dream-index/1", "snapshot": copy.deepcopy(snapshot), "proposals": copy.deepcopy(proposals)}
    compiled["batch_id"] = _frame("dream-index", compiled)
    return compiled


def _compiled(compiled):
    _obj(compiled, ("schema", "snapshot", "proposals", "batch_id")); _hash(compiled["batch_id"])
    expected = compile_dream_index(compiled["snapshot"], compiled["proposals"])
    if _j(expected) != _j(compiled): _fail("invalid_proposals")


def _stored_batch(batch):
    """Verify a frozen batch; query paths never invoke the index compiler."""
    _obj(batch, ("schema", "snapshot", "proposals", "batch_id")); _hash(batch["batch_id"])
    if batch["schema"] != "dream-index/1": _fail("invalid_proposals")
    original = {key: batch[key] for key in ("schema", "snapshot", "proposals")}
    if _frame("dream-index", original) != batch["batch_id"]: _fail("invalid_proposals")
    _, records = _snapshot_shape(batch["snapshot"], admission=False)
    _proposal_shape(batch["snapshot"], batch["proposals"], records)


def record_dream_index(store, compiled, *, context):
    ctx = _ctx(context, True); _compiled(compiled)
    at = _time(ctx["recorded_at"])
    with _write(store, ctx) as (store, state):
        _privacy(compiled)
        for source in compiled["snapshot"]["sources"]:
            info = _bound(store, source["reference"], ctx, at)
            if info["tier"] == "historical": _fail("unavailable")
        present = _registry(store.conn)
        row = store.conn.execute("SELECT compiled_json,writer_json FROM v8_dream_batches WHERE batch_id=?", (compiled["batch_id"],)).fetchone() if present else None
        if row is not None:
            if row["compiled_json"] != _j(compiled).decode(): _fail("invalid_registry")
            _writer_shape(_loads(row["writer_json"]))
        else:
            _registry(store.conn, True)
            store.conn.execute("INSERT INTO v8_dream_batches VALUES(?,?,?)", (compiled["batch_id"], _j(compiled).decode(), _j(_writer(ctx)).decode()))
        return _write_receipt("dream_batch", compiled["batch_id"], compiled, row is None, state, ctx)


def _descriptor(identity, assertion, status):
    return {"assertion_id": identity, **{k: copy.deepcopy(assertion[k]) for k in ("from_ref", "to_ref", "subject_key", "claim_key", "effect", "condition", "effective_from", "effective_until")}, "status": status}


class _Resolver:
    def __init__(self, store, context, evaluation_time):
        self.store, self.context, self.evaluation_time = store, context, evaluation_time
        self.at = _time(evaluation_time)
        self.registry = _registry(store.conn)
        self.infos, self.visited, self.corrections, self.pairs = {}, set(), set(), set()
        self.screened = set()
        self.optional_count = 0

    def info(self, node_id, *, current=False, screen=True):
        if node_id not in self.infos:
            self.infos[node_id] = _info(_raw(self.store, node_id), self.context, self.at, privacy=False)
        info = self.infos[node_id]
        if info is None or (current and info["tier"] == "historical"): return None
        if screen and node_id not in self.screened:
            _privacy({"metadata": info["metadata"], "body": info["node"]["content"]})
            self.screened.add(node_id)
        return info

    def probe(self, node_id, *, current=False):
        try: return self.info(node_id, current=current)
        except ReferenceError: return None

    def visit(self, info):
        node_id = info["reference"]["node_id"]
        if node_id not in self.visited and len(self.visited) >= 32: return False
        self.visited.add(node_id)
        return True

    def conflicts(self, origin):
        if origin["tier"] == "historical": return [origin["reference"]["node_id"]], False, False
        start = origin["reference"]["node_id"]
        queue, found, conflict, incomplete = [start], {start}, False, False
        while queue:
            node_id = queue.pop(0)
            current = self.info(node_id)
            if not self.visit(current): incomplete = True; break
            rows = self.store.conn.execute("SELECT CASE WHEN from_id=? THEN to_id ELSE from_id END AS other_id FROM edges WHERE type='contradicts' AND (from_id=? OR to_id=?) ORDER BY other_id", (node_id, node_id, node_id)).fetchall()
            for row in rows:
                other = row[0]
                if other == node_id: continue
                info = self.probe(other, current=True)
                # Noncurrent and inaccessible neighbors affect no visible count,
                # order, cap, identity or status. Check before all public bounds.
                if info is None or info["tier"] == "historical": continue
                pair = tuple(sorted((node_id, other)))
                if pair not in self.pairs and len(self.pairs) >= 64:
                    incomplete = True; continue
                if not self.visit(info): incomplete = True; continue
                self.pairs.add(pair); conflict = True
                if other not in found:
                    found.add(other); queue.append(other); queue.sort()
        return sorted(found), conflict, incomplete

    def counts(self):
        return {"visited_nodes": len(self.visited), "inspected_corrections": len(self.corrections), "inspected_contradictions": len(self.pairs), "optional_candidates": self.optional_count}

    def group(self, origin, entry_index=None):
        root = origin["reference"]["node_id"]
        info_ids, depth, pending = [root], {root: 0}, [root]
        declarations, descriptors, receipts = {}, {}, {}
        scanned, stale_targets = set(), set()
        incomplete = not self.visit(origin)
        conflict_ids, conflict_seeds, outgoing_seeds = set(), [], []

        def unique(values): return list(dict.fromkeys(values))

        def scan_corrections():
            nonlocal incomplete
            while pending and not incomplete:
                target_id = pending.pop(0)
                if target_id in scanned: continue
                scanned.add(target_id)
                if not self.registry: continue
                if target_id == root:
                    # Derived corrects and corrected_by expose the same stored
                    # assertion. Outgoing discovery never writes an inverse row.
                    rows = self.store.conn.execute("SELECT * FROM v8_corrections WHERE to_id=? OR from_id=? ORDER BY assertion_id", (target_id, target_id)).fetchall()
                else:
                    rows = self.store.conn.execute("SELECT * FROM v8_corrections WHERE to_id=? ORDER BY assertion_id", (target_id,)).fetchall()
                for row in rows:
                    source = self.info(row["from_id"], screen=False)
                    target = self.info(row["to_id"], screen=False)
                    if source is None or target is None: continue
                    identity = row["assertion_id"]
                    # Current native endpoint access permits only a generic
                    # unresolved signal if the stored payload is corrupt. It
                    # never authorizes rendering any historical payload.
                    authorized = True
                    try:
                        assertion = _loads(row["payload_json"])
                        if type(assertion) is not dict: _fail()
                        _ref_shape(assertion.get("from_ref")); _ref_shape(assertion.get("to_ref"))
                        # Current access to reused node IDs does not authorize a
                        # historical payload. Scope routing precedes all visible
                        # counts; exact versions precede descriptors and receipts.
                        if any(any(ref[key] != info["reference"][key] for key in ("graph_id", "profile", "partition_id"))
                               for ref, info in ((assertion["from_ref"], source), (assertion["to_ref"], target))): continue
                        if assertion["from_ref"]["node_id"] != row["from_id"] or assertion["to_ref"]["node_id"] != row["to_id"]: _fail()
                        authorized = True
                        _assertion(assertion)
                        writer = _loads(row["writer_json"]); _writer_shape(writer)
                        if _frame("correction", assertion) != identity: _fail()
                        if identity not in self.corrections and len(self.corrections) >= 64:
                            incomplete = True; break
                        self.corrections.add(identity)
                        if assertion["from_ref"] != source["reference"] or assertion["to_ref"] != target["reference"]:
                            # Hashes carry no historical scope preimage. Preserve
                            # only a generic no-revival signal for this current
                            # target, never old refs, claims, spans or writer data.
                            stale_targets.add(row["to_id"])
                            continue
                        source, target = self.probe(row["from_id"]), self.probe(row["to_id"])
                        if source is None or target is None:
                            stale_targets.add(row["to_id"]); continue
                        if not self.visit(source) or not self.visit(target): incomplete = True; break
                        _spans(assertion["from_spans"], source["node"]["content"].encode())
                        _spans(assertion["to_spans"], target["node"]["content"].encode())
                        valid_actor = writer["actor_id"] in self.context["correction_policy"]["allowed_actor_ids"]
                        retirement = self.store.conn.execute("SELECT receipt_json FROM v8_correction_retirements WHERE assertion_id=?", (identity,)).fetchone()
                        retired = False
                        if retirement:
                            value = _loads(retirement[0]); _obj(value, ("assertion_id", "reason", "writer")); _writer_shape(value["writer"]); _str(value["reason"], 512)
                            if value["assertion_id"] != identity: _fail()
                            retired = True
                        if not valid_actor or source["partition"]["kind"] not in ("personal", "repo") or target["partition"]["kind"] not in ("personal", "repo"): status = "unresolved"
                        elif retired: status = "retired"
                        elif assertion["effective_from"] is not None and self.at < _time(assertion["effective_from"]): status = "future"
                        elif assertion["effective_until"] is not None and self.at >= _time(assertion["effective_until"]): status = "expired"
                        elif assertion["condition"] != "unconditional": status = "unresolved"
                        else: status = "active"
                        declarations[identity] = {"assertion": assertion, "status": status, "from": row["from_id"], "to": row["to_id"],
                                                  "conditional": valid_actor and not retired and status == "unresolved" and assertion["condition"] == "unresolved"}
                        descriptors[identity] = _descriptor(identity, assertion, status)
                        receipts[identity] = {"assertion_id": identity, "assertion": assertion, "writer": writer}
                    except (ReferenceError, TypeError, ValueError):
                        if authorized:
                            if identity not in self.corrections and len(self.corrections) >= 64:
                                incomplete = True; break
                            self.corrections.add(identity)
                            stale_targets.add(row["to_id"])
                        continue
                    actual_target = row["to_id"]
                    if actual_target not in depth:
                        depth[actual_target] = 0
                        info_ids.append(actual_target); pending.append(actual_target)
                    if row["from_id"] == root and (status == "active" or declarations[identity]["conditional"]):
                        if actual_target not in outgoing_seeds: outgoing_seeds.append(actual_target)
                    node_id = row["from_id"]
                    if node_id not in depth:
                        if depth[actual_target] >= 8:
                            incomplete = True; break
                        depth[node_id] = depth[actual_target]+1
                        info_ids.append(node_id); pending.append(node_id)

        def select_bodies():
            path_states, conditional, examined_lanes = {}, set(), set()

            def select(node_id, visiting, lane=None):
                if node_id in visiting:
                    cycle = [nid for nid in sorted(visiting) if self.info(nid)["tier"] != "historical"]
                    return cycle, [], True
                visiting = visiting | {node_id}
                info = self.info(node_id)
                by_claim = {}
                for identity, declaration in declarations.items():
                    if declaration["to"] != node_id: continue
                    a = declaration["assertion"]
                    key = (a["to_ref"]["partition_id"], a["subject_key"], a["claim_key"])
                    if lane is None or key == lane:
                        by_claim.setdefault(key, []).append((identity, declaration))
                if not by_claim:
                    if node_id in stale_targets: return [], [], True
                    return ([node_id] if info["tier"] != "historical" else []), ([node_id] if info["tier"] == "current" else []), info["tier"] == "historical"
                bodies, finals, unresolved = [], [], node_id in stale_targets
                for key, incoming in sorted(by_claim.items()):
                    examined_lanes.add((node_id, key))
                    active = [(identity, d) for identity, d in incoming if d["status"] == "active"]
                    invalid = [(identity, d) for identity, d in incoming if d["status"] not in ("active", "future")]
                    selected, end, bad = [], [], False
                    # Resolve effective branches before unknown conditions. A
                    # conditional qualification applies to the corrected basis,
                    # not an obsolete original when a replacement also exists.
                    replacements = [(identity, d) for identity, d in active if d["assertion"]["effect"] != "qualify"]
                    qualifications = [(identity, d) for identity, d in active if d["assertion"]["effect"] == "qualify"]
                    for _, d in sorted(active):
                        child, terminal, child_bad = select(d["from"], visiting, key)
                        selected.extend(child); end.extend(terminal); bad = bad or child_bad
                    if qualifications and not replacements:
                        if node_id not in stale_targets and info["tier"] != "historical": selected.append(node_id)
                        else: bad = True
                    if not active and not invalid:
                        if node_id not in stale_targets and info["tier"] != "historical": selected.append(node_id)
                        else: bad = True
                    for _, d in invalid:
                        if d["conditional"]:
                            child, _, _ = select(d["from"], visiting, key)
                            selected.extend(child)
                            if not active and node_id not in stale_targets and info["tier"] != "historical": selected.append(node_id)
                            conditional.update(selected)
                        bad = True
                    if len(active) > 1: bad = True
                    if invalid or node_id in stale_targets: bad = True
                    if all(d["status"] == "future" for _, d in incoming):
                        state = {"current": "uncorrected_current", "evidence_only": "evidence_only", "historical": "historical_only"}[info["tier"]]
                    else: state = "unresolved" if bad else "resolved_chain"
                    old = path_states.get(key)
                    identities = {identity for identity, _ in incoming}
                    if old is None: path_states[key] = [state, identities]
                    else:
                        old[1].update(identities)
                        if state == "unresolved": old[0] = state
                    bodies.extend(selected)
                    if not bad: finals.extend(end)
                    unresolved = unresolved or bad
                return unique(bodies), unique(finals) if not unresolved else [], unresolved

            required, terminals, unresolved = [], [], False
            for seed in unique([root]+outgoing_seeds+conflict_seeds):
                bodies, final, bad = select(seed, set())
                required.extend(bodies); terminals.extend(final); unresolved = unresolved or bad
            # A source retained for one claim cannot be replaced wholesale by a
            # correction to another claim. Inspect those other lanes separately
            # and union their evidence without removing the first lane's basis.
            while True:
                extra = []
                for node_id in unique(required+conflict_seeds):
                    for declaration in declarations.values():
                        if declaration["to"] != node_id: continue
                        a = declaration["assertion"]
                        key = (a["to_ref"]["partition_id"], a["subject_key"], a["claim_key"])
                        if (node_id, key) not in examined_lanes: extra.append((node_id, key))
                if not extra: break
                for node_id, key in sorted(set(extra)):
                    bodies, final, bad = select(node_id, set(), key)
                    required.extend(bodies); terminals.extend(final); unresolved = unresolved or bad
            paths = [{"subject_key": key[1], "claim_key": key[2], "status": state,
                      "assertion_ids": sorted(identities)} for key, (state, identities) in sorted(path_states.items())]
            return unique(required), unique(terminals), unresolved, paths, conditional

        # Corrections and native contradictions form one bounded fixed point.
        # Only selected effective/conditional bodies activate contradiction
        # traversal: a future or retired correcting source is not such a seed.
        conflict_scanned = set()
        while not incomplete:
            scan_corrections()
            required, terminals, unresolved, paths, conditional = select_bodies()
            seeds = [nid for nid in unique(required+conflict_seeds) if nid not in conflict_scanned]
            if not seeds or incomplete: break
            for node_id in seeds:
                conflict_scanned.add(node_id)
                related, conflict, limited = self.conflicts(self.info(node_id))
                incomplete = incomplete or limited
                if not conflict: continue
                conflict_ids.update(related)
                for other in related:
                    if other not in conflict_seeds: conflict_seeds.append(other)
                    if other not in depth:
                        depth[other] = 0; info_ids.append(other)
                    if other not in scanned and other not in pending: pending.append(other)
        required, terminals, unresolved, paths, conditional = select_bodies()
        if conflict_ids:
            required.extend(sorted(conflict_ids)); terminals = []
            for path in paths: path["status"] = "conflicting_evidence"
        if incomplete:
            status = "incomplete"; required, terminals = [], []
            for path in paths: path["status"] = "incomplete"
        elif conflict_ids: status = "conflicting_evidence"
        elif unresolved: status = "unresolved"
        else: status = origin["tier"]
        terminals = sorted(set(terminals), key=lambda nid: (-depth.get(nid, 0), nid))
        required = unique(terminals+required)
        priority = unique(terminals+required+info_ids)
        return {"entry": {"reference": origin["reference"], "status": status, "claim_paths": paths},
                "infos": info_ids, "required": required, "priority": priority,
                "terminals": terminals, "depth": depth, "assertions": [descriptors[k] for k in sorted(descriptors)],
                "receipts": [receipts[k] for k in sorted(receipts)], "conditional": conditional,
                "conflict_ids": conflict_ids, "input_index": entry_index}


def _read_request(request, expand=False):
    fields = ("schema", "evaluation_time", "query", "entry_refs", "mode", "optional_limit")
    _obj(request, (*fields, "base_budget_bytes", "optional_budget_bytes", "include_history") if expand else (*fields, "reference_budget_bytes")); _j(request)
    if request["schema"] != ("expand-request/1" if expand else "reference-request/1"): _fail()
    _time(request["evaluation_time"]); _str(request["query"], 2048, 0)
    refs = request["entry_refs"]
    if type(refs) is not list or len(refs) > 8: _fail()
    ids = []
    for ref in refs:
        _ref_shape(ref); ids.append(ref["node_id"])
    if len(ids) != len(set(ids)) or (not refs and not request["query"].strip()): _fail()
    if request["mode"] not in ("none", "direct", "dream"): _fail()
    _int(request["optional_limit"], 0, 8)
    if expand:
        _int(request["base_budget_bytes"], 1, 65536); _int(request["optional_budget_bytes"], 0, 32768)
        if request["base_budget_bytes"]+request["optional_budget_bytes"] > 65536 or type(request["include_history"]) is not bool: _fail()
    else: _int(request["reference_budget_bytes"], 512, 16384)


def _entries(resolver, request):
    entries, omissions = [], []
    if request["entry_refs"]:
        for index, ref in enumerate(request["entry_refs"]):
            info = resolver.info(ref["node_id"])
            if info is None or ref["graph_id"] != resolver.context["graph_id"] or ref["profile"] != resolver.context["profile"]:
                omissions.append({"input_index": index, "reason": "unavailable"}); continue
            if info["reference"] != ref:
                omissions.append({"input_index": index, "reason": "stale_reference"}); continue
            entries.append((index, info))
    else:
        from .retrieve import hybrid_search
        hits = hybrid_search(resolver.store, request["query"], top_k=8, ranking="rrf", use_vectors=False,
                             expand_graph=False, include_expired=True, include_archived=True,
                             trusted_only=False, evaluation_time=request["evaluation_time"], recency_time=resolver.at)
        seen = set()
        for hit in hits:
            node_id = hit["id"]
            if node_id in seen: continue
            seen.add(node_id)
            info = resolver.info(node_id)
            if info is not None: entries.append((len(entries), info))
    return [resolver.group(info, index) for index, info in entries], omissions


def _tokens(text):
    words, current = set(), []
    for char in text.casefold():
        if char.isalnum(): current.append(char)
        elif current: words.add("".join(current)); current = []
    if current: words.add("".join(current))
    return words


def _priority(groups):
    terminals = {}
    for position, group in enumerate(groups):
        for node_id in group["terminals"]:
            key = (-group["depth"].get(node_id, 0), position, node_id)
            if node_id not in terminals or key < terminals[node_id]: terminals[node_id] = key
    ids = sorted(terminals, key=lambda nid: terminals[nid])
    for group in groups:
        for node_id in group["priority"]:
            if node_id not in ids: ids.append(node_id)
    return ids


def _optional(resolver, groups, request, omissions, *, incoming_later=False):
    if request["mode"] == "none" or not request["optional_limit"]: return []
    base_ids = _priority(groups)
    direct, dreams = [], []
    base_set = set(base_ids)
    # Scope/current filtering precedes the visible candidate cap. No query-time
    # native search, reverse inference, learned signal or provider work occurs.
    seen_targets = set()
    for node_id in base_ids:
        rows = resolver.store.conn.execute("SELECT DISTINCT to_id,type FROM edges WHERE from_id=? AND type IN ('relates_to','context_of','depends_on','contradicts') ORDER BY type,to_id", (node_id,)).fetchall()
        for row in rows:
            target = row["to_id"]
            if target in base_set or target in seen_targets: continue
            info = resolver.probe(target, current=True)
            if info is None or info["tier"] == "historical": continue
            seen_targets.add(target)
            source_ref = resolver.info(node_id)["reference"]
            connection = {"id": _frame("direct", {"from_ref": source_ref, "to_ref": info["reference"], "relation": row["type"]}), "route": "direct", "relation": row["type"], "motif": None,
                          "supports": [{"reference": source_ref, "spans": []}, {"reference": info["reference"], "spans": []}], "differences": [], "transfer_checks": [], "status": "declared_link"}
            direct.append({"target": target, "info": info, "connection": connection, "supports": [node_id, target]})
    if incoming_later:
        # One actual incoming hop, appended after every existing outgoing
        # choice. Scope, current eligibility and chronology precede dedup/caps.
        remaining_rows = _INCOMING_ROW_LIMIT
        for node_id in base_ids:
            # Existing entry policy permits an exactly bound historical seed;
            # only the newly navigated later source must be nonhistorical.
            seed = resolver.probe(node_id)
            if seed is None: continue
            try: earlier = _time(seed["node"].get("prov_when"))
            except ReferenceError: continue
            rows = resolver.store.conn.execute("SELECT DISTINCT from_id,type FROM edges WHERE to_id=? AND type IN ('relates_to','context_of') ORDER BY type,from_id LIMIT ?", (node_id, remaining_rows + 1)).fetchall()
            # Refuse the opt-in rather than silently ranking a partial tail.
            # No endpoint identity or hidden-row count is returned on overflow.
            if len(rows) > remaining_rows: _fail("unavailable")
            remaining_rows -= len(rows)
            for row in rows:
                target = row["from_id"]
                if target in base_set or target in seen_targets: continue
                info = resolver.probe(target, current=True)
                if info is None: continue
                try: later = _time(info["node"].get("prov_when"))
                except ReferenceError: continue
                if not earlier < later <= resolver.at: continue
                seen_targets.add(target)
                binding = {"from_ref": info["reference"], "to_ref": seed["reference"], "relation": row["type"]}
                connection = {"id": _frame("incoming", binding), "route": "incoming", "relation": row["type"], "motif": None,
                              "supports": [{"reference": info["reference"], "spans": []}, {"reference": seed["reference"], "spans": []}],
                              "differences": [], "transfer_checks": [], "status": "declared_link"}
                direct.append({"target": target, "info": info, "connection": connection, "supports": [target, node_id]})
    if request["mode"] == "dream" and resolver.registry:
        rows = resolver.store.conn.execute("SELECT batch_id,compiled_json FROM v8_dream_batches ORDER BY batch_id LIMIT 33").fetchall()
        limited = len(rows) > 32
        proposals = []
        if not limited:
            for row in rows:
                try:
                    batch = _loads(row["compiled_json"]); _stored_batch(batch)
                    if batch["batch_id"] != row["batch_id"]: _fail()
                except ReferenceError:
                    omissions.append({"input_index": None, "reason": "dream_unavailable"}); continue
                for proposal in sorted(batch["proposals"]["proposals"], key=lambda p: p["proposal_id"]):
                    proposals.append((row["batch_id"], proposal))
                if len(proposals) > 256: limited = True; break
        if limited:
            omissions.append({"input_index": None, "reason": "dream_registry_limit"})
        else:
            query_words = _tokens(request["query"])
            for batch_id, proposal in proposals:
                ids = [s["reference"]["node_id"] for s in proposal["supports"]]
                if not base_set.intersection(ids): continue
                infos = [resolver.probe(nid, current=True) for nid in ids]
                if any(info is None or info["tier"] == "historical" or info["reference"] != support["reference"] for info, support in zip(infos, proposal["supports"])): continue
                targets = [nid for nid in ids if nid not in base_set]
                if not targets: continue
                target = targets[0]
                connection = {"id": batch_id+":"+proposal["proposal_id"], "route": "dream", "relation": proposal["relation"], "motif": proposal["motif"], "supports": copy.deepcopy(proposal["supports"]), "differences": list(proposal["differences"]), "transfer_checks": list(proposal["transfer_checks"]), "status": "generated_hypothesis"}
                dreams.append({"target": target, "info": resolver.info(target), "connection": connection, "supports": ids,
                               "sort": (-len(query_words & _tokens(proposal["motif"])), proposal["proposal_id"], batch_id)})
            dreams.sort(key=lambda candidate: candidate["sort"])
    candidates, seen = [], set()
    if request["mode"] == "direct": stream = direct
    else:
        stream = []
        for index in range(max(len(direct), len(dreams))):
            if index < len(direct): stream.append(direct[index])
            if index < len(dreams): stream.append(dreams[index])
    for candidate in stream:
        if candidate["target"] in seen: continue
        seen.add(candidate["target"]); candidates.append(candidate)
        if len(candidates) >= request["optional_limit"]: break
    resolver.optional_count = len(candidates)
    for candidate in candidates:
        candidate["groups"] = [resolver.group(resolver.info(node_id)) for node_id in candidate["supports"]]
    return candidates


def capture_reference(store, node_id, *, context, evaluation_time):
    ctx = _ctx(context); _str(node_id); _time(evaluation_time)
    with _read(store, ctx):
        resolver = _Resolver(store, ctx, evaluation_time)
        info = resolver.info(node_id)
        if info is None: _fail("unavailable")
        _, conflict, incomplete = resolver.conflicts(info)
        applicability = "incomplete" if incomplete else "conflicting_evidence" if conflict else info["tier"]
        result = {"schema": "reference-capture/1", "reference": info["reference"], "metadata": info["metadata"],
                  "applicability": applicability, "evaluation_time": evaluation_time, "context_sha256": _frame("context", ctx)}
        _privacy(result)
        return result


def _reference_packet(resolver, groups, optional, omissions):
    entries = [g["entry"] for g in groups]
    assertions, seen = [], set()
    for group in groups:
        for assertion in group["assertions"]:
            if assertion["assertion_id"] not in seen:
                seen.add(assertion["assertion_id"]); assertions.append(assertion)
    refs = [resolver.info(nid)["reference"] for nid in _priority(groups)]
    return {"warning": _WARNING, "entries": entries, "assertions": assertions, "priority_refs": refs,
            "optional": optional, "omissions": omissions}


def _read_options(request, incoming_later, metadata_encoding="full"):
    if type(incoming_later) is not bool or type(metadata_encoding) is not str or metadata_encoding not in ("full", "lossless_common"): _fail()
    mode = request.get("mode") if type(request) is dict else None
    if incoming_later and mode != "direct": _fail()
    if metadata_encoding != "full" and mode not in ("none", "direct"): _fail()


def resolve_references(store, request, *, context, incoming_later=False):
    _read_options(request, incoming_later)
    ctx = _ctx(context); _read_request(request)
    with _read(store, ctx):
        resolver = _Resolver(store, ctx, request["evaluation_time"])
        groups, omissions = _entries(resolver, request)
        optional = _optional(resolver, groups, request, omissions, incoming_later=incoming_later)
        packed = []
        cap = request["reference_budget_bytes"]
        if len(_j(_reference_packet(resolver, [], [], omissions))) > cap: _fail("budget_too_small")
        for group in groups:
            trial = _reference_packet(resolver, packed+[group], [], omissions)
            if len(_j(trial)) <= cap: packed.append(group)
            else: omissions.append({"input_index": group["input_index"], "reason": "reference_budget"})
        descriptors = []
        for candidate in optional:
            # Optional routes require their own full scoped closure descriptors;
            # never advertise only a selected target while hiding its correction.
            ids = list(dict.fromkeys(nid for g in candidate["groups"] for nid in g["priority"]))
            descriptor = {"route": candidate["connection"]["route"], "id": candidate["connection"]["id"],
                          "reference_ids": ids, "status": "navigation_hypothesis" if candidate["connection"]["route"] == "dream" else "declared_link"}
            trial = _reference_packet(resolver, packed, descriptors+[descriptor], omissions)
            if len(_j(trial)) <= cap: descriptors.append(descriptor)
            else: omissions.append({"input_index": None, "reason": "reference_budget"})
        packet = _reference_packet(resolver, packed, descriptors, omissions)
        # Omission serialization is charged as well. Retain early groups first.
        while len(_j(packet)) > cap and (descriptors or packed):
            if descriptors: descriptors.pop()
            else:
                old = packed.pop(); omission = {"input_index": old["input_index"], "reason": "reference_budget"}
                if omission not in omissions: omissions.append(omission)
            packet = _reference_packet(resolver, packed, descriptors, omissions)
        if len(_j(packet)) > cap: _fail("budget_too_small")
        text = _j(packet).decode()
        result = {"schema": "reference-result/1", "evaluation_time": request["evaluation_time"], "context_sha256": _frame("context", ctx),
                  "registry": "present" if resolver.registry else "absent", **{k: packet[k] for k in ("entries", "assertions", "priority_refs", "optional", "omissions")},
                  "counts": resolver.counts(), "model_text": text, "utf8_bytes": len(text.encode()), "text_sha256": _h(text.encode())}
        if incoming_later:
            result.update(schema="reference-result/2", options={"incoming_later": True})
        _privacy(result)
        return result


def _evidence_empty():
    return {"warning": _WARNING, "sources": [], "references": [], "assertions": [], "connections": []}


def _metadata_common(values):
    common = {}
    for key in sorted(set.intersection(*(set(value) for value in values))):
        children = [value[key] for value in values]
        if all(type(child) is dict for child in children):
            # Even an empty common object preserves an object-valued key that
            # is present in every source; absence is a different JSON value.
            common[key] = _metadata_common(children)
        elif all(_j(child) == _j(children[0]) for child in children[1:]):
            common[key] = copy.deepcopy(children[0])
    return common


def _metadata_delta(value, common):
    delta = {}
    for key, child in value.items():
        if key not in common: delta[key] = copy.deepcopy(child)
        elif _j(child) == _j(common[key]): continue
        elif type(child) is dict and type(common[key]) is dict:
            nested = _metadata_delta(child, common[key])
            if nested: delta[key] = nested
        else: delta[key] = copy.deepcopy(child)
    return delta


def _metadata_merge(common, delta):
    value = copy.deepcopy(common)
    for key, child in delta.items():
        if key in value and type(value[key]) is dict and type(child) is dict:
            value[key] = _metadata_merge(value[key], child)
        else: value[key] = copy.deepcopy(child)
    return value


def _evidence_bytes(payload, metadata_encoding):
    full = _j(payload)
    if metadata_encoding == "full" or not payload["sources"]: return full
    try:
        common = _metadata_common([source["metadata"] for source in payload["sources"]])
        factored = {**payload, "metadata_encoding": "recursive-common-with-source-delta/1", "metadata_common": common,
                    "sources": [{key: copy.deepcopy(value) for key, value in source.items() if key != "metadata"}
                                | {"metadata_delta": _metadata_delta(source["metadata"], common)} for source in payload["sources"]]}
        encoded = _j(factored)
        return encoded if len(encoded) < len(full) else full
    except RecursionError: _fail()


def _evidence_shape(payload):
    _obj(payload, ("warning", "sources", "references", "assertions", "connections"))
    if payload["warning"] != _WARNING: _fail()
    for key in ("sources", "references", "assertions", "connections"):
        if type(payload[key]) is not list: _fail()
    statuses = {"current", "evidence_only", "historical", "unresolved", "incomplete", "conflicting_evidence"}
    ids, bodies, bindings = set(), {}, {}

    def bind_reference(reference):
        _ref_shape(reference)
        node_id, canonical = reference["node_id"], _j(reference)
        if node_id in bindings and bindings[node_id] != canonical: _fail()
        bindings[node_id] = canonical

    for source in payload["sources"]:
        _obj(source, ("reference", "metadata", "applicability", "body")); bind_reference(source["reference"])
        if type(source["applicability"]) is not str or source["applicability"] not in statuses or type(source["body"]) is not str: _fail()
        metadata, ref = source["metadata"], source["reference"]
        _obj(metadata, (*_META, "extra"))
        if type(metadata["extra"]) is not dict or set(metadata["extra"])-set(_EXTRA): _fail()
        if type(metadata["domains"]) is not list or any(type(tag) is not str for tag in metadata["domains"]): _fail()
        raw = source["body"].encode("utf-8")
        if _h(raw) != ref["body_sha256"] or _frame("metadata", metadata) != ref["metadata_sha256"] or ref["node_id"] in ids: _fail()
        ids.add(ref["node_id"]); bodies[ref["node_id"]] = (ref, raw)
    for row in payload["references"]:
        _obj(row, ("reference", "applicability")); bind_reference(row["reference"])
        if type(row["applicability"]) is not str or row["applicability"] not in statuses or row["reference"]["node_id"] in ids: _fail()
        ids.add(row["reference"]["node_id"])
    assertion_ids = set()
    for row in payload["assertions"]:
        _obj(row, ("assertion_id", "from_ref", "to_ref", "subject_key", "claim_key", "effect", "condition", "effective_from", "effective_until", "status"))
        _hash(row["assertion_id"]); bind_reference(row["from_ref"]); bind_reference(row["to_ref"])
        if row["assertion_id"] in assertion_ids or row["from_ref"]["node_id"] == row["to_ref"]["node_id"] or row["from_ref"]["partition_id"] != row["to_ref"]["partition_id"]: _fail()
        assertion_ids.add(row["assertion_id"])
        _str(row["subject_key"], 128); _str(row["claim_key"], 128); _interval(row["effective_from"], row["effective_until"])
        if row["effect"] not in ("replace", "qualify", "retract") or row["condition"] not in ("unconditional", "unresolved") or row["status"] not in ("active", "future", "expired", "retired", "unresolved"): _fail()
    connection_ids = set()
    for row in payload["connections"]:
        _obj(row, ("id", "route", "relation", "motif", "supports", "differences", "transfer_checks", "status"))
        _str(row["id"], 129)
        if row["id"] in connection_ids or type(row["supports"]) is not list or len(row["supports"]) != 2: _fail()
        connection_ids.add(row["id"])
        for support in row["supports"]:
            _obj(support, ("reference", "spans")); bind_reference(support["reference"])
        first, second = [support["reference"] for support in row["supports"]]
        if first["node_id"] == second["node_id"]: _fail()
        if row["route"] in ("direct", "incoming"):
            relations = ("relates_to", "context_of") if row["route"] == "incoming" else ("relates_to", "context_of", "depends_on", "contradicts")
            if row["relation"] not in relations or row["motif"] is not None or row["differences"] != [] or row["transfer_checks"] != [] or row["status"] != "declared_link": _fail()
            if any(support["spans"] != [] for support in row["supports"]): _fail()
            if row["id"] != _frame(row["route"], {"from_ref": first, "to_ref": second, "relation": row["relation"]}): _fail()
        elif row["route"] == "dream":
            if not re.fullmatch(r"[0-9a-f]{64}:[a-z0-9_-]{1,64}", row["id"]) or row["relation"] not in _DREAM_RELATIONS or row["status"] != "generated_hypothesis": _fail()
            _str(row["motif"], 512)
            for key in ("differences", "transfer_checks"):
                if type(row[key]) is not list or not 1 <= len(row[key]) <= 4: _fail()
                for value in row[key]: _str(value, 512)
            for support in row["supports"]:
                known = bodies.get(support["reference"]["node_id"])
                raw = known[1] if known and _j(known[0]) == _j(support["reference"]) else None
                _spans(support["spans"], raw)
        else: _fail()


def decode_evidence_text(text):
    """Pure canonical-format/hash decoder; no current access or truth claim."""
    try:
        if type(text) is not str: _fail()
        if text == "": return None
        payload = _loads(text)
        if _j(payload).decode("utf-8") != text: _fail()
        if type(payload) is not dict: _fail()
        if "metadata_encoding" in payload or "metadata_common" in payload:
            _obj(payload, ("warning", "sources", "references", "assertions", "connections", "metadata_encoding", "metadata_common"))
            if payload["metadata_encoding"] != "recursive-common-with-source-delta/1" or type(payload["metadata_common"]) is not dict or type(payload["sources"]) is not list or not payload["sources"]: _fail()
            sources = []
            for source in payload["sources"]:
                _obj(source, ("reference", "metadata_delta", "applicability", "body"))
                if type(source["metadata_delta"]) is not dict: _fail()
                sources.append({"reference": source["reference"], "metadata": _metadata_merge(payload["metadata_common"], source["metadata_delta"]),
                                "applicability": source["applicability"], "body": source["body"]})
            payload = {key: payload[key] for key in ("warning", "references", "assertions", "connections")} | {"sources": sources}
        _evidence_shape(payload)
        return payload
    except (ReferenceError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail("invalid_evidence_text")


def _merge_evidence(resolver, payload, groups, *, history=False, already=(), connection=None):
    trial = copy.deepcopy(payload)
    ids = {row["reference"]["node_id"] for row in trial["sources"]} | set(already)
    required = _priority(groups) if history else list(dict.fromkeys(nid for group in groups for nid in group["required"]))
    conditional = set().union(*(group["conditional"] for group in groups)) if groups else set()
    conflicting = set().union(*(group["conflict_ids"] for group in groups)) if groups else set()
    for node_id in required:
        if node_id in ids: continue
        info = resolver.info(node_id)
        if not history and info["tier"] == "historical": continue
        applicability = "conflicting_evidence" if node_id in conflicting else "unresolved" if node_id in conditional else info["tier"]
        trial["sources"].append({"reference": info["reference"], "metadata": info["metadata"], "applicability": applicability, "body": info["node"]["content"]})
        ids.add(node_id)
    refs = {row["reference"]["node_id"] for row in trial["references"]}
    for group in groups:
        for node_id in group["infos"]:
            if node_id in ids or node_id in refs: continue
            info = resolver.info(node_id)
            applicability = "conflicting_evidence" if node_id in conflicting else "unresolved" if node_id in conditional else info["tier"]
            trial["references"].append({"reference": info["reference"], "applicability": applicability}); refs.add(node_id)
        old = {a["assertion_id"] for a in trial["assertions"]}
        for assertion in group["assertions"]:
            if assertion["assertion_id"] not in old:
                trial["assertions"].append(assertion); old.add(assertion["assertion_id"])
    trial["references"] = [row for row in trial["references"] if row["reference"]["node_id"] not in ids]
    if connection is not None: trial["connections"].append(connection)
    return trial


def expand_references(store, request, *, context, metadata_encoding="full", incoming_later=False):
    _read_options(request, incoming_later, metadata_encoding)
    ctx = _ctx(context); _read_request(request, True)
    with _read(store, ctx):
        resolver = _Resolver(store, ctx, request["evaluation_time"])
        groups, omissions = _entries(resolver, request)
        base, optional_payload = _evidence_empty(), _evidence_empty()
        base_cap, optional_cap = request["base_budget_bytes"], request["optional_budget_bytes"]
        successful = []
        ordered = sorted(enumerate(groups), key=lambda pair: (
            min((-pair[1]["depth"].get(nid, 0) for nid in pair[1]["terminals"]), default=1), pair[0]))
        for _, group in ordered:
            trial = _merge_evidence(resolver, base, [group])
            if len(_evidence_bytes(trial, metadata_encoding)) <= base_cap:
                base = trial; successful.append(group)
            else:
                omissions.append({"input_index": group["input_index"], "reason": "base_budget"})
                # Preserve a bounded descriptor without backfilling the old body.
                bounded = {**group, "required": []}
                trial = _merge_evidence(resolver, base, [bounded])
                if len(_evidence_bytes(trial, metadata_encoding)) <= base_cap: base = trial
        if request["include_history"]:
            for group in successful:
                for node_id in group["infos"]:
                    if any(row["reference"]["node_id"] == node_id for row in base["sources"]): continue
                    single = {**group, "priority": [node_id], "required": [node_id], "infos": [] , "assertions": []}
                    trial = _merge_evidence(resolver, base, [single], history=True)
                    if len(_evidence_bytes(trial, metadata_encoding)) <= base_cap: base = trial
                    else: omissions.append({"input_index": group["input_index"], "reason": "history_budget"})
            # A no-correction historical entry is considered only in this
            # optional history pass, never instead of a required correction.
        candidates = _optional(resolver, groups, request, omissions, incoming_later=incoming_later)
        base_ids = {row["reference"]["node_id"] for row in base["sources"]}
        for candidate in candidates:
            if any(group["entry"]["status"] == "incomplete" for group in candidate["groups"]):
                omissions.append({"input_index": None, "reason": "optional_unresolved"}); continue
            # A motif is accompanied by all support closures, with all required
            # current evidence atomically fitting this separate optional cap.
            needed = set(nid for group in candidate["groups"] for nid in group["required"])
            def unavailable_support(position, group):
                if candidate["connection"]["route"] == "incoming" and position == 1:
                    seed = resolver.info(candidate["supports"][1])
                    # A historically accessible seed may remain reference-only;
                    # its body is not a required current incoming support. Any
                    # current successors it does require stay in the group.
                    if seed["tier"] == "historical": return False
                return not group["required"] and group["entry"]["status"] in ("unresolved", "historical", "incomplete")
            if any(unavailable_support(position, group) for position, group in enumerate(candidate["groups"])):
                omissions.append({"input_index": None, "reason": "optional_unresolved"}); continue
            trial = _merge_evidence(resolver, optional_payload, candidate["groups"], already=base_ids, connection=candidate["connection"])
            if needed-base_ids and len(_evidence_bytes(trial, metadata_encoding)) > optional_cap:
                omissions.append({"input_index": None, "reason": "optional_budget"}); continue
            if len(_evidence_bytes(trial, metadata_encoding)) <= optional_cap: optional_payload = trial
            else: omissions.append({"input_index": None, "reason": "optional_budget"})
        base_raw = _evidence_bytes(base, metadata_encoding)
        base_text = base_raw.decode() if len(base_raw) <= base_cap and (base["sources"] or base["references"] or base["assertions"]) else ""
        optional_text = _evidence_bytes(optional_payload, metadata_encoding).decode() if optional_payload["connections"] else ""
        included = [row["reference"] for payload in (base, optional_payload) for row in payload["sources"]]
        receipts, seen = [], set()
        for group in groups+[g for c in candidates for g in c["groups"]]:
            for receipt in group["receipts"]:
                if receipt["assertion_id"] not in seen:
                    seen.add(receipt["assertion_id"]); receipts.append(receipt)
        result = {"schema": "evidence-result/1", "evaluation_time": request["evaluation_time"], "context_sha256": _frame("context", ctx),
                  "registry": "present" if resolver.registry else "absent", "base_text": base_text, "optional_text": optional_text,
                  "base_utf8_bytes": len(base_text.encode()), "optional_utf8_bytes": len(optional_text.encode()),
                  "base_sha256": _h(base_text.encode()), "optional_sha256": _h(optional_text.encode()),
                  "included_refs": included, "omissions": omissions, "counts": resolver.counts(),
                  "assertion_receipts": receipts, "receipt_utf8_bytes": len(_j(receipts))}
        opted_in = metadata_encoding != "full" or incoming_later
        if opted_in:
            result.update(schema="evidence-result/2", options={"metadata_encoding": metadata_encoding, "incoming_later": incoming_later})
        _privacy(result)
        if opted_in:
            # Check both actual delivery and the complete reconstructed format.
            # Shared metadata cannot conceal a native redaction match spanning
            # fields. The reconstructed copy is never delivered off-budget.
            reconstructed = dict(result)
            for prefix, text in (("base", base_text), ("optional", optional_text)):
                full = decode_evidence_text(text)
                if full is not None: _privacy(full)
                raw = _j(full) if full is not None else b""
                reconstructed[prefix+"_text"] = raw.decode("utf-8")
            _privacy(reconstructed)
        return result
