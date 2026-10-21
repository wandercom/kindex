"""Opt-in, source-bound practical memory; local host annotations are not authority.

Only ``prepare`` and ``audit`` render model data. Mutations record exact past
commits, and a host acknowledgment is distinct from model receipt or use.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, ValidationError

from .agent_adapters import ADAPTER_SCOPE_NAMES, node_scope_clients, normalize_adapter
from .privacy import redact, redact_serialized
from .sessions import normalize_project_path
from .trust import node_trust_decision, normalize_rfc3339, parse_rfc3339, validate_interval

__all__ = ["prepare", "persist", "index", "audit", "ack", "organize", "PracticalMemoryError"]
_WARNING = "Contextual source data; not instruction, permission, completion, or proof of delivery/use."
_COMPONENTS = ("sources", "continuity", "links", "application_trace")
_FLAGS = ("compact_packing", "continuity", "assertion_index", "episode_index", "typed_links", "application_trace")
_ROLES = ("claim", "actor", "scope", "conditions", "exceptions", "event_time", "report_time", "valid_from", "valid_until")
_QUESTIONS = ["check_applicability_before_treating_as_requirement", "preserve_actor_and_scope", "preserve_conditions_and_exceptions", "respect_valid_time", "do_not_infer_permission_or_completion", "supply_supported_requested_fields"]
_META = ("type", "title", "prov_who", "prov_when", "prov_activity", "prov_source", "status", "audience", "standing", "verified_at", "verified_by", "prov_method", "valid_at", "invalid_at", "referent", "asserted_at", "true_of", "domains")
_EXTRA = ("project_path", "expires", "superseded_by", "referent_stale", "expired_at", "kinbase")
_REASONS = frozenset(("envelope_budget", "component_budget", "total_budget", "duplicate", "no_source_body", "source_unavailable", "source_inapplicable", "invalid_source_metadata", "index_stale", "index_invalid", "retained_unavailable", "retained_stale", "constraint_unavailable", "constraint_overflow"))
_HEX = re.compile(r"^[0-9a-f]{64}$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MEMORY = "practical_memory_v1"
_INDEX = "practical_index_v1"


class PracticalMemoryRegister(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Literal[1]
    owner: dict[str, Any]
    retained: dict[str, Any]
    operations: dict[str, Any]
    projections: dict[str, Any]


class PracticalIndexRegister(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Literal[1]
    node_id: str
    graph_id: str
    profile: str
    project_path: str
    current: dict[str, Any]
    history: dict[str, Any]
    operations: dict[str, Any]


class PracticalMemoryError(ValueError):
    """Safe structured failure; details never disclose source or foreign scope."""
    def __init__(self, code: str, message: str = "Practical memory request is unavailable"):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code="invalid_request"):
    raise PracticalMemoryError(code)


def _c(value):
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    _fail()
                pending.extend(item.keys())
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
            elif type(item) is str:
                item.encode("utf-8")
            elif item is None or type(item) in (bool, int):
                pass
            elif type(item) is float and math.isfinite(item):
                pass
            else:
                _fail()
        return serialized
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _d(kind, value):
    return _sha(("kindex.practical_memory.v1/" + kind + "\n").encode() + _c(value))


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail()
        out[key] = value
    return out


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        _fail()
    return result


def _loads(raw):
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_float=_float,
                            parse_constant=lambda _: _fail())
        _c(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _obj(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail()
    return value


def _string(value, *, minimum=0, maximum=None):
    if type(value) is not str:
        _fail()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail()
    if size < minimum or (maximum is not None and size > maximum):
        _fail()
    return value


def _integer(value, low=0, high=16384):
    if type(value) is not int or not low <= value <= high:
        _fail()
    return value


def _digest(value, nullable=False):
    if nullable and value is None:
        return value
    if type(value) is not str or not _HEX.fullmatch(value):
        _fail()
    return value


def _ids(value, limit=16):
    if type(value) is not list or len(value) > limit:
        _fail()
    for item in value:
        _string(item, minimum=1, maximum=256)
    if len(set(value)) != len(value):
        _fail()
    return value


def _time(value):
    _string(value, minimum=1)
    try:
        if not value.endswith("Z") or normalize_rfc3339(value, field="evaluation_time") != value:
            _fail()
        return parse_rfc3339(value, field="evaluation_time")
    except ValueError:
        _fail()


def _request(request, keys):
    _obj(request, keys)
    _c(request)
    _scope_shape(request["scope"])
    return _time(request["evaluation_time"])


def _scope_shape(scope):
    _obj(scope, ("graph_id", "profile", "project_path", "client", "session_id"))
    try:
        if str(uuid.UUID(scope["graph_id"])) != scope["graph_id"]:
            _fail()
    except (ValueError, TypeError, AttributeError):
        _fail()
    _string(scope["profile"])
    project = _string(scope["project_path"], minimum=1)
    if not os.path.isabs(project) or normalize_project_path(project) != project:
        _fail()
    if _string(scope["client"]) not in ADAPTER_SCOPE_NAMES:
        _fail()
    _string(scope["session_id"], minimum=1, maximum=256)


def _raw_node(store, node_id):
    row = store.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if row is None:
        return None
    node = dict(row)
    try:
        for key in ("domains", "prov_who", "referent", "extra"):
            if isinstance(node.get(key), str):
                node[key] = _loads(node[key])
        if type(node.get("extra")) is not dict:
            _fail()
        _c(node)
    except PracticalMemoryError:
        _fail("unavailable")
    return node


def _binding(node, scope):
    domains = node.get("domains")
    if type(domains) is not list or any(type(t) is not str for t in domains):
        _fail("invalid_request")
    for tag in domains:
        tag = tag.strip().lower()
        if tag.startswith(("client:", "agent:")):
            marker = tag.split(":", 1)[1]
            if normalize_adapter(marker) not in ADAPTER_SCOPE_NAMES or not marker:
                _fail("invalid_request")
    extra = node.get("extra")
    if type(extra) is not dict:
        _fail("invalid_request")
    project = extra.get("project_path")
    if project is None:
        _fail("scope_mismatch")
    if type(project) is not str or not os.path.isabs(project) or normalize_project_path(project) != project:
        _fail("invalid_request")
    if project != scope["project_path"]:
        _fail("scope_mismatch")
    clients = node_scope_clients(domains)
    if clients and scope["client"] not in clients:
        _fail("scope_mismatch")


def _scope(store, scope):
    _scope_shape(scope)
    graph = store.conn.execute("SELECT value FROM meta WHERE key='graph_id'").fetchone()
    stamp = store.conn.execute("SELECT value FROM meta WHERE key='kin_profile'").fetchone()
    selected = getattr(store.config, "active_profile", None) or ""
    if graph is None or graph[0] != scope["graph_id"] or selected != scope["profile"] or (stamp[0] if stamp else "") != selected:
        _fail("scope_mismatch")
    configured = getattr(store.config, "_project_path", None)
    if configured and normalize_project_path(str(configured)) != scope["project_path"]:
        _fail("scope_mismatch")
    owner = _raw_node(store, scope["session_id"])
    if owner is None or owner.get("type") != "session":
        _fail("scope_mismatch")
    try:
        _binding(owner, scope)
    except PracticalMemoryError:
        _fail("scope_mismatch")
    return owner


def _source(node, scope):
    metadata = {key: node.get(key) for key in _META}
    metadata["extra"] = {key: node["extra"][key] for key in _EXTRA if key in node["extra"]}
    return {"node_id": node["id"], "graph_id": scope["graph_id"], "profile": scope["profile"],
            "project_path": scope["project_path"], "source_sha256": _sha(node["content"].encode("utf-8")),
            "metadata_sha256": _d("metadata", metadata), "metadata": metadata}


def _admit(store, node_id, scope, at):
    try:
        node = _raw_node(store, node_id)
    except PracticalMemoryError:
        return None, "invalid_source_metadata"
    if node is None:
        return None, "source_unavailable"
    try:
        _binding(node, scope)
        if type(node.get("content")) is not str:
            _fail()
        for key in ("valid_at", "invalid_at", "verified_at", "asserted_at", "true_of"):
            if node.get(key) is not None:
                parse_rfc3339(node[key], field=key)
        validate_interval(node.get("valid_at"), node.get("invalid_at"))
        extra = node["extra"]
        if "expires" in extra:
            value = extra["expires"]
            if type(value) is not str or date.fromisoformat(value).isoformat() != value:
                _fail()
            if date.fromisoformat(value) < at.date():
                return None, "source_inapplicable"
        for key in ("expired_at",):
            if extra.get(key) is not None:
                parse_rfc3339(extra[key], field=key)
        if "referent_stale" in extra and type(extra["referent_stale"]) is not bool:
            _fail()
        if "superseded_by" in extra and (type(extra["superseded_by"]) is not str or not extra["superseded_by"]):
            _fail()
    except PracticalMemoryError as exc:
        return None, "source_inapplicable" if exc.code == "scope_mismatch" else "invalid_source_metadata"
    except (ValueError, TypeError, OverflowError):
        return None, "invalid_source_metadata"
    if not node_trust_decision(store, node, at=at).eligible:
        return None, "source_inapplicable"
    return node, None


@contextmanager
def _snapshot(store):
    if not getattr(store, "read_only", False):
        _fail("unavailable")
    conn = store.conn
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        _fail("unavailable")
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            conn.rollback()


def _public(fn):
    @wraps(fn)
    def call(store, request):
        try:
            result = fn(store, request)
            # Native egress is authoritative. Never advertise exact hashes for
            # text that the privacy boundary would subsequently rewrite.
            text = _c(result).decode()
            if redact_serialized(text) != text:
                _fail("privacy_unavailable")
            return result
        except PracticalMemoryError:
            raise
        except (sqlite3.Error, OSError, KeyError):
            _fail("unavailable")
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            _fail()
    return call


def _group(group, body=None, *, kind=None, bounded=True):
    try:
        _obj(group, ("group_id", "episode_key", "spans", "roles"))
        _string(group["group_id"], minimum=1, maximum=256)
        _string(group["episode_key"], minimum=1 if kind == "episode" else 0, maximum=256)
        spans = group["spans"]
        if type(spans) is not list or not spans or (bounded and len(spans) > 8):
            _fail("invalid_group")
        _obj(group["roles"], _ROLES)
        seen, end, size = set(), 0, 0
        for span in spans:
            _obj(span, ("span_id", "start_byte", "end_byte", "text", "span_sha256"))
            sid = _string(span["span_id"], minimum=1, maximum=256)
            if sid in seen:
                _fail("invalid_span")
            seen.add(sid)
            start = _integer(span["start_byte"], 0, 2**63-1)
            stop = _integer(span["end_byte"], 1, 2**63-1)
            raw = _string(span["text"], minimum=1).encode()
            if start < end or stop <= start or stop-start != len(raw) or _sha(raw) != _digest(span["span_sha256"]):
                _fail("invalid_span")
            if bounded and len(raw) > 2048:
                _fail("invalid_span")
            if body is not None and body[start:stop] != raw:
                _fail("invalid_span")
            end, size = stop, size+len(raw)
        if bounded and size > 4096:
            _fail("invalid_group")
        for role, ids in group["roles"].items():
            if type(ids) is not list or len(ids) != len(set(ids)) or any(type(sid) is not str or sid not in seen for sid in ids):
                _fail("invalid_group")
            if role == "claim" and not ids:
                _fail("invalid_group")
    except PracticalMemoryError as exc:
        if exc.code in ("invalid_group", "invalid_span"):
            raise
        _fail("invalid_group")
    except (ValueError, TypeError, AttributeError):
        _fail("invalid_group")
    return group


def _whole(node):
    raw = node["content"].encode()
    return {"group_id": "whole_source", "episode_key": "", "spans": [{"span_id": "s0", "start_byte": 0, "end_byte": len(raw), "text": node["content"], "span_sha256": _sha(raw)}], "roles": {key: ["s0"] if key == "claim" else [] for key in _ROLES}}


def _entry(source, group, kind):
    return {"entry_id": _d("group", {"source": source, "group": group}), "kind": kind, "source": source, "group": group}


def _trace(node, scope):
    return {"constraint": _source(node, scope), "group": _whole(node), "questions": list(_QUESTIONS)}


def _component(entries, compact):
    if not entries:
        return None
    entries = copy.deepcopy(entries)
    table = {}
    for entry in entries:
        for key in ("source", "from", "to", "constraint"):
            if key in entry:
                src = entry[key]
                source_key = _d("source", src)
                table[source_key] = src
                if compact:
                    entry[key] = {"source_key": source_key}
    return {"representation": "shared" if compact else "inline", "source_table": [{"source_key": key, "source": table[key]} for key in sorted(table)] if compact else [], "entries": entries}


def _omissions(counts):
    return [{"reason": reason, "count": count} for reason, count in sorted(counts.items()) if count]


def _diagnostics(entries, component, counts):
    text = _c(component).decode() if component is not None else ""
    return {"entries": entries, "text": text, "utf8_bytes": len(text.encode()), "text_sha256": _sha(text.encode()), "omissions": _omissions(counts)}


def _covered(entry, packed):
    for old in packed:
        if "source" not in old or old["source"] != entry["source"]:
            continue
        spans = old["group"]["spans"]
        if all(any(a["start_byte"] <= s["start_byte"] and a["end_byte"] >= s["end_byte"] and a["text"].encode()[s["start_byte"]-a["start_byte"]:s["end_byte"]-a["start_byte"]] == s["text"].encode() for a in spans) for s in entry["group"]["spans"]):
            return True
    return False


def _source_duplicate(entry, packed):
    """Indexed annotations remain distinct unless their complete groups match.

    A full body already carries all literal source intervals. Other indexed
    groups may carry different role/episode annotations over the same bytes;
    span coverage alone cannot discard those representations.
    """
    for old in packed:
        if old["source"] != entry["source"]:
            continue
        if old["group"] == entry["group"]:
            return True
        if old["kind"] == "whole_source" and _covered(entry, [old]):
            return True
    return False


def _features(value):
    _obj(value, _FLAGS)
    if any(type(v) is not bool for v in value.values()):
        _fail()


def _prepare_request(request):
    at = _request(request, ("scope", "evaluation_time", "query", "features", "top_k", "retained_ids", "budgets", "total_max_bytes"))
    if not _string(request["query"], minimum=1, maximum=4096).strip():
        _fail()
    _features(request["features"])
    _integer(request["top_k"], 1, 10)
    _ids(request["retained_ids"])
    if request["retained_ids"] and not request["features"]["continuity"]:
        _fail()
    _obj(request["budgets"], _COMPONENTS)
    for cap in request["budgets"].values():
        _integer(cap)
    _integer(request["total_max_bytes"])
    return at


def _payload(request):
    return {"format": "kindex.practical_memory.context", "version": 1, "warning": _WARNING,
            "scope": request["scope"], "evaluation_time": request["evaluation_time"], "query": request["query"],
            "components": {name: None for name in _COMPONENTS}}


def _source_shape(source, scope):
    _obj(source, ("node_id", "graph_id", "profile", "project_path", "source_sha256", "metadata_sha256", "metadata"))
    _string(source["node_id"], minimum=1, maximum=256)
    for key in ("graph_id", "profile", "project_path"):
        if source[key] != scope[key]:
            _fail()
    _digest(source["source_sha256"])
    _digest(source["metadata_sha256"])
    _obj(source["metadata"], (*_META, "extra"))
    extra = source["metadata"]["extra"]
    if type(extra) is not dict or set(extra)-set(_EXTRA) or extra.get("project_path") != scope["project_path"]:
        _fail()
    if _d("metadata", source["metadata"]) != source["metadata_sha256"]:
        _fail()


def _entry_shape(entry, scope, bounded=False):
    _obj(entry, ("entry_id", "kind", "source", "group"))
    if entry["kind"] not in ("whole_source", "retained", "assertion", "episode"):
        _fail()
    _source_shape(entry["source"], scope)
    _group(entry["group"], kind=entry["kind"], bounded=bounded or entry["kind"] not in ("whole_source",))
    if entry["kind"] == "whole_source":
        _whole_shape(entry["group"], entry["source"])
    if entry["entry_id"] != _d("group", {"source": entry["source"], "group": entry["group"]}):
        _fail()


def _whole_shape(group, source):
    if len(group["spans"]) != 1:
        _fail("invalid_group")
    span = group["spans"][0]
    expected = _whole({"content": span["text"]})
    if group != expected or span["span_sha256"] != source["source_sha256"]:
        _fail("invalid_group")


def _receipt_shape(result, scope):
    _obj(result, ("format", "version", "receipt"))
    if result["format"] != "kindex.practical_memory.receipt" or type(result["version"]) is not int or result["version"] != 1:
        _fail()
    r = result["receipt"]
    _obj(r, ("operation", "operation_id", "scope", "request_sha256", "target_sha256", "committed_at", "persistence", "delivery", "use", "receipt_sha256"))
    if r["scope"] != scope or r["operation"] not in ("persist", "index", "ack") or r["persistence"] != "committed" or r["use"] != "not_observed" or r["delivery"] != ("host_acknowledged" if r["operation"] == "ack" else "not_observed"):
        _fail()
    _string(r["operation_id"], minimum=1, maximum=256)
    _time(r["committed_at"])
    for key in ("request_sha256", "target_sha256", "receipt_sha256"):
        _digest(r[key])
    if _d("receipt", {key: value for key, value in r.items() if key != "receipt_sha256"}) != r["receipt_sha256"]:
        _fail()


def _register(extra, scope, *, absent=True):
    if _MEMORY not in extra:
        if absent:
            return {"version": 1, "owner": copy.deepcopy(scope), "retained": {}, "operations": {}, "projections": {}}
        _fail("unavailable")
    register = extra[_MEMORY]
    try:
        PracticalMemoryRegister.model_validate(register, strict=True)
        _obj(register, ("version", "owner", "retained", "operations", "projections"))
        if type(register["version"]) is not int or register["version"] != 1 or register["owner"] != scope:
            _fail()
        for key, limit in (("retained", 16), ("operations", 32), ("projections", 32)):
            if type(register[key]) is not dict or len(register[key]) > limit:
                _fail()
        for key, record in register["retained"].items():
            _obj(record, ("entry", "captured_at"))
            _entry_shape(record["entry"], scope, bounded=True)
            if key != record["entry"]["entry_id"]:
                _fail()
            _time(record["captured_at"])
        for key, receipt in register["operations"].items():
            _receipt_shape(receipt, scope)
            r = receipt["receipt"]
            if r["operation"] not in ("persist", "ack") or key != _d("request", {"scope": scope, "operation_id": r["operation_id"]}):
                _fail()
        for key, projection in register["projections"].items():
            _digest(key)
            _obj(projection, ("payload_sha256", "persist_receipt"))
            _digest(projection["payload_sha256"])
            _receipt_shape(projection["persist_receipt"], scope)
            r = projection["persist_receipt"]["receipt"]
            if r["operation"] != "persist" or r["target_sha256"] != key or register["operations"].get(_d("request", {"scope": scope, "operation_id": r["operation_id"]})) != projection["persist_receipt"]:
                _fail()
        if any(receipt["receipt"]["target_sha256"] not in register["projections"] for receipt in register["operations"].values()):
            _fail()
    except (PracticalMemoryError, ValidationError):
        _fail("unavailable")
    return copy.deepcopy(register)


def _index_register(extra, scope, node_id):
    if _INDEX not in extra:
        return {"version": 1, "node_id": node_id, "graph_id": scope["graph_id"], "profile": scope["profile"], "project_path": scope["project_path"], "current": {}, "history": {}, "operations": {}}
    reg = extra[_INDEX]
    try:
        PracticalIndexRegister.model_validate(reg, strict=True)
    except ValidationError:
        _fail()
    _obj(reg, ("version", "node_id", "graph_id", "profile", "project_path", "current", "history", "operations"))
    if type(reg["version"]) is not int or reg["version"] != 1 or reg["node_id"] != node_id or any(reg[k] != scope[k] for k in ("graph_id", "profile", "project_path")):
        _fail()
    if type(reg["current"]) is not dict or set(reg["current"])-{"assertion", "episode"} or type(reg["history"]) is not dict or type(reg["operations"]) is not dict or len(reg["operations"]) > 32 or len(reg["history"]) != len(reg["operations"]):
        _fail()
    for key, record in reg["history"].items():
        _obj(record, ("source", "index_kind", "groups", "index_sha256", "scope", "committed_at", "receipt"))
        _scope_shape(record["scope"])
        if any(record["scope"][k] != scope[k] for k in ("graph_id", "profile", "project_path")):
            _fail()
        _source_shape(record["source"], record["scope"])
        if record["source"]["node_id"] != node_id or record["index_kind"] not in ("assertion", "episode"):
            _fail()
        _groups(record["groups"], kind=record["index_kind"])
        if record["index_sha256"] != _d("index", {k: record[k] for k in ("source", "index_kind", "groups")}):
            _fail()
        _receipt_shape(record["receipt"], record["scope"])
        receipt = record["receipt"]["receipt"]
        if receipt["operation"] != "index" or receipt["target_sha256"] != record["index_sha256"] or receipt["committed_at"] != record["committed_at"] or key != _d("request", {"scope": record["scope"], "operation_id": receipt["operation_id"]}) or reg["operations"].get(key) != record["receipt"]:
            _fail()
    if set(reg["operations"]) != set(reg["history"]):
        _fail()
    if set(reg["current"]) != {record["index_kind"] for record in reg["history"].values()}:
        _fail()
    for kind, key in reg["current"].items():
        if type(key) is not str or key not in reg["history"] or reg["history"][key]["index_kind"] != kind:
            _fail()
    return copy.deepcopy(reg)


def _groups(groups, *, kind, body=None):
    if type(groups) is not list or len(groups) > 16 or len(_c(groups)) > 32768:
        _fail("invalid_group")
    seen = set()
    for group in groups:
        _group(group, body, kind=kind)
        if group["group_id"] in seen:
            _fail("invalid_group")
        seen.add(group["group_id"])


def _selected_groups(node, scope, features, query, counts):
    enabled = [kind for kind, flag in (("assertion", "assertion_index"), ("episode", "episode_index")) if features[flag]]
    if not enabled or _INDEX not in node["extra"]:
        return []
    try:
        reg = _index_register(node["extra"], scope, node["id"])
    except PracticalMemoryError:
        counts["index_invalid"] += 1
        return []
    src = _source(node, scope)
    tokens = set(_WORD.findall(query.casefold()))
    selected = []
    for kind in enabled:
        pointer = reg["current"].get(kind)
        if pointer is None:
            continue
        record = reg["history"][pointer]
        if record["source"] != src:
            counts["index_stale"] += 1
            continue
        try:
            _groups(record["groups"], kind=kind, body=node["content"].encode())
        except PracticalMemoryError:
            counts["index_invalid"] += 1
            continue
        for group in record["groups"]:
            score = len(tokens & set(_WORD.findall(" ".join(s["text"] for s in group["spans"]).casefold())))
            if score:
                selected.append((-score, 0 if kind == "assertion" else 1, group["group_id"], _entry(src, group, kind)))
    return [item[-1] for item in sorted(selected, key=lambda item: item[:3])]


@_public
def prepare(store, request):
    """Prepare one exact bounded payload from native recall and explicit IDs."""
    at = _prepare_request(request)
    with _snapshot(store):
        owner = _scope(store, request["scope"])
        reg = _register(owner["extra"], request["scope"])
        from .retrieve import hybrid_search
        hits = hybrid_search(store, request["query"], top_k=request["top_k"], expand_graph=True, graph_hops=1, ranking="ensemble", use_vectors=False, include_expired=False, include_archived=False, trusted_only=True, evaluation_time=at, recency_time=at)
        scope, features = request["scope"], request["features"]
        counts = {name: Counter() for name in _COMPONENTS}
        candidates = {name: [] for name in _COMPONENTS}
        admitted = {}
        seen = set()
        for hit in hits:
            node_id = hit["id"]
            if node_id in seen:
                continue
            seen.add(node_id)
            node, reason = _admit(store, node_id, scope, at)
            if reason:
                counts["sources"][reason] += 1
                continue
            admitted[node_id] = node
            if not node["content"]:
                counts["sources"]["no_source_body"] += 1
                continue
            selected = _selected_groups(node, scope, features, request["query"], counts["sources"])
            candidates["sources"].extend(selected or [_entry(_source(node, scope), _whole(node), "whole_source")])
        if features["continuity"]:
            for retained_id in request["retained_ids"]:
                record = reg["retained"].get(retained_id)
                if record is None:
                    counts["continuity"]["retained_unavailable"] += 1
                    continue
                entry = record["entry"]
                node, reason = _admit(store, entry["source"]["node_id"], scope, at)
                if reason:
                    counts["continuity"]["retained_unavailable" if reason == "source_unavailable" else "retained_stale"] += 1
                    continue
                if _source(node, scope) != entry["source"]:
                    counts["continuity"]["retained_stale"] += 1
                    continue
                try:
                    _group(entry["group"], node["content"].encode())
                except PracticalMemoryError:
                    counts["continuity"]["retained_stale"] += 1
                    continue
                admitted[node["id"]] = node
                candidates["continuity"].append({**entry, "kind": "retained"})
        if features["typed_links"] and admitted:
            admitted_ids = sorted(admitted)
            placeholders = ",".join("?" for _ in admitted_ids)
            edges = store.conn.execute(
                f"SELECT from_id,to_id,type,provenance FROM edges WHERE from_id IN ({placeholders}) AND to_id IN ({placeholders}) ORDER BY from_id,type,to_id,id",
                admitted_ids + admitted_ids).fetchall()
            seen_edges = set()
            for edge in edges:
                key = (edge["from_id"], edge["type"], edge["to_id"])
                if key in seen_edges or edge["from_id"] not in admitted or edge["to_id"] not in admitted:
                    continue
                seen_edges.add(key)
                candidates["links"].append({"from": _source(admitted[edge["from_id"]], scope), "to": _source(admitted[edge["to_id"]], scope), "type": edge["type"], "provenance": edge["provenance"], "direction": "stored_from_to", "operational_effect": "none"})
        if features["application_trace"]:
            nodes = [n for _, n in sorted(admitted.items()) if n["type"] in ("constraint", "directive", "checkpoint")]
            if len(nodes) > 16:
                counts["application_trace"]["constraint_overflow"] += len(nodes)-16
            for node in nodes[:16]:
                if node["content"]:
                    candidates["application_trace"].append(_trace(node, scope))
                else:
                    counts["application_trace"]["no_source_body"] += 1
        payload = _payload(request)
        packed = {name: [] for name in _COMPONENTS}
        if len(_c(payload)) > request["total_max_bytes"]:
            counts = {name: Counter() for name in _COMPONENTS}
            counts["sources"]["envelope_budget"] += 1
            text = ""
        else:
            for name in _COMPONENTS:
                for entry in candidates[name]:
                    duplicate = (_source_duplicate(entry, packed["sources"]) if name == "sources"
                                 else (_covered(entry, packed["sources"])
                                       or _source_duplicate(entry, packed["continuity"]))
                                 if name == "continuity" else False)
                    if duplicate:
                        counts[name]["duplicate"] += 1
                        continue
                    component = _component(packed[name]+[entry], features["compact_packing"])
                    if len(_c(component)) > request["budgets"][name]:
                        counts[name]["component_budget"] += 1
                        continue
                    trial = {**payload, "components": {**payload["components"], name: component}}
                    if len(_c(trial)) > request["total_max_bytes"]:
                        counts[name]["total_budget"] += 1
                        continue
                    packed[name].append(entry)
                    payload = trial
            text = _c(payload).decode()
        components = {name: _diagnostics(packed[name], payload["components"][name] if text else None, counts[name]) for name in _COMPONENTS}
        result = {"format": "kindex.practical_memory.prepared", "version": 1, "scope": scope, "evaluation_time": request["evaluation_time"], "query": request["query"], "features": features, "budgets": request["budgets"], "total_max_bytes": request["total_max_bytes"], "status": "partial" if any(counts.values()) else "prepared", "components": components, "model_text": text, "utf8_bytes": len(text.encode()), "text_sha256": _sha(text.encode()), "persistence": "not_requested", "delivery": "not_observed", "use": "not_observed"}
        result["projection_sha256"] = _d("projection", result)
        return result


@_public
def audit(store, request):
    """Pack a current source-backed checklist; perform no semantic validation."""
    at = _request(request, ("scope", "evaluation_time", "answer", "constraint_ids", "total_max_bytes"))
    _string(request["answer"], maximum=65536)
    _ids(request["constraint_ids"])
    _integer(request["total_max_bytes"])
    with _snapshot(store):
        owner = _scope(store, request["scope"])
        _register(owner["extra"], request["scope"])
        counts = Counter()
        ids = request["constraint_ids"] or [r[0] for r in store.conn.execute("SELECT id FROM nodes WHERE type IN ('constraint','directive','checkpoint') ORDER BY id")]
        entries = []
        for node_id in ids:
            node, reason = _admit(store, node_id, request["scope"], at)
            if reason or node["type"] not in ("constraint", "directive", "checkpoint"):
                if request["constraint_ids"]:
                    counts["constraint_unavailable"] += 1
                continue
            if not node["content"]:
                counts["no_source_body"] += 1
                continue
            entries.append(_trace(node, request["scope"]))
        if len(entries) > 16:
            counts["constraint_overflow"] += len(entries)-16
            entries = entries[:16]
        payload = {"format": "kindex.practical_memory.audit", "version": 1, "warning": _WARNING, "scope": request["scope"], "evaluation_time": request["evaluation_time"], "answer": request["answer"], "entries": []}
        if len(_c(payload)) > request["total_max_bytes"]:
            text = ""
            counts["envelope_budget"] += 1
        else:
            for entry in entries:
                trial = {**payload, "entries": payload["entries"]+[entry]}
                if len(_c(trial)) <= request["total_max_bytes"]:
                    payload = trial
                else:
                    counts["total_budget"] += 1
            text = _c(payload).decode()
        return {"format": "kindex.practical_memory.audit_packet", "version": 1, "scope": request["scope"], "evaluation_time": request["evaluation_time"], "answer_sha256": _d("answer", request["answer"]), "constraints_sha256": _d("constraints", entries), "model_text": text, "utf8_bytes": len(text.encode()), "text_sha256": _sha(text.encode()), "omissions": _omissions(counts), "mode": "packet_only", "semantic_validation": "not_performed", "delivery": "not_observed", "use": "not_observed"}


def _prepared(prepared, scope, evaluation_time):
    _obj(prepared, ("format", "version", "scope", "evaluation_time", "query", "features", "budgets", "total_max_bytes", "status", "components", "model_text", "utf8_bytes", "text_sha256", "projection_sha256", "persistence", "delivery", "use"))
    if prepared["format"] != "kindex.practical_memory.prepared" or type(prepared["version"]) is not int or prepared["version"] != 1 or prepared["scope"] != scope or prepared["evaluation_time"] != evaluation_time:
        _fail()
    _prepare_request({key: prepared[key] for key in ("scope", "evaluation_time", "query", "features", "budgets", "total_max_bytes")} | {"top_k": 1, "retained_ids": []})
    if prepared["persistence"] != "not_requested" or prepared["delivery"] != "not_observed" or prepared["use"] != "not_observed":
        _fail()
    if prepared["projection_sha256"] != _d("projection", {key: value for key, value in prepared.items() if key != "projection_sha256"}):
        _fail()
    _obj(prepared["components"], _COMPONENTS)
    payload = _payload(prepared)
    entries = {}
    omitted = False
    envelope = False
    for name in _COMPONENTS:
        diag = prepared["components"][name]
        _obj(diag, ("entries", "text", "utf8_bytes", "text_sha256", "omissions"))
        if type(diag["entries"]) is not list or type(diag["omissions"]) is not list:
            _fail()
        disabled = name != "sources" and not prepared["features"][{"continuity": "continuity", "links": "typed_links", "application_trace": "application_trace"}[name]]
        if disabled and (diag["entries"] or diag["text"] or diag["omissions"] or diag["utf8_bytes"]):
            _fail()
        reasons = []
        for omission in diag["omissions"]:
            _obj(omission, ("reason", "count"))
            if omission["reason"] not in _REASONS:
                _fail()
            _integer(omission["count"], 1, 2**63-1)
            if omission["reason"] == "envelope_budget":
                if name != "sources":
                    _fail()
                envelope = True
            reasons.append(omission["reason"])
        if reasons != sorted(set(reasons)):
            _fail()
        omitted = omitted or bool(reasons)
        for entry in diag["entries"]:
            if name in ("sources", "continuity"):
                _entry_shape(entry, scope)
                if name == "continuity" and entry["kind"] != "retained":
                    _fail()
                if name == "sources" and entry["kind"] == "retained":
                    _fail()
                if entry["kind"] in ("assertion", "episode") and not prepared["features"][entry["kind"]+"_index"]:
                    _fail()
                old = entries.get(entry["entry_id"])
                if old is not None and {**old, "kind": entry["kind"]} != entry:
                    _fail()
                entries[entry["entry_id"]] = entry
            elif name == "links":
                _obj(entry, ("from", "to", "type", "provenance", "direction", "operational_effect"))
                _source_shape(entry["from"], scope)
                _source_shape(entry["to"], scope)
                _string(entry["type"], minimum=1)
                _string(entry["provenance"])
                if entry["direction"] != "stored_from_to" or entry["operational_effect"] != "none":
                    _fail()
            else:
                _obj(entry, ("constraint", "group", "questions"))
                _source_shape(entry["constraint"], scope)
                _group(entry["group"], bounded=False)
                _whole_shape(entry["group"], entry["constraint"])
                if entry["questions"] != _QUESTIONS:
                    _fail()
        component = _component(diag["entries"], prepared["features"]["compact_packing"])
        expected = _c(component).decode() if component is not None else ""
        if diag["text"] != expected or type(diag["utf8_bytes"]) is not int or diag["utf8_bytes"] != len(expected.encode()) or diag["text_sha256"] != _sha(expected.encode()):
            _fail()
        if diag["utf8_bytes"] > prepared["budgets"][name]:
            _fail()
        payload["components"][name] = component
    if envelope:
        if any(d["entries"] for d in prepared["components"].values()) or len(_c(_payload(prepared))) <= prepared["total_max_bytes"]:
            _fail()
        text = ""
    else:
        text = _c(payload).decode()
    if prepared["model_text"] != text or type(prepared["utf8_bytes"]) is not int or prepared["utf8_bytes"] != len(text.encode()) or prepared["text_sha256"] != _sha(text.encode()) or len(text.encode()) > prepared["total_max_bytes"] or prepared["status"] != ("partial" if omitted else "prepared"):
        _fail()
    if redact_serialized(_c(prepared).decode()) != _c(prepared).decode():
        _fail("privacy_unavailable")
    return entries


def _clock():
    return normalize_rfc3339(datetime.now(timezone.utc), field="committed_at")


def _receipt(operation, request, target):
    receipt = {"operation": operation, "operation_id": request["operation_id"], "scope": copy.deepcopy(request["scope"]), "request_sha256": _d("request", {"operation": operation, "request": request}), "target_sha256": target, "committed_at": _clock(), "persistence": "committed", "delivery": "host_acknowledged" if operation == "ack" else "not_observed", "use": "not_observed"}
    receipt["receipt_sha256"] = _d("receipt", receipt)
    return {"format": "kindex.practical_memory.receipt", "version": 1, "receipt": receipt}


class _Replay(Exception):
    def __init__(self, receipt):
        self.receipt = receipt


def _replay(reg, operation, request):
    key = _d("request", {"scope": request["scope"], "operation_id": request["operation_id"]})
    old = reg["operations"].get(key)
    if old is not None:
        if old["receipt"]["request_sha256"] != _d("request", {"operation": operation, "request": request}):
            _fail("operation_conflict")
        raise _Replay(copy.deepcopy(old))
    if len(reg["operations"]) >= 32:
        _fail("capacity")
    return key


def _current_entry(store, entry, scope, at):
    node, reason = _admit(store, entry["source"]["node_id"], scope, at)
    if reason:
        _fail("source_changed")
    src = _source(node, scope)
    if src["source_sha256"] != entry["source"]["source_sha256"]:
        _fail("source_changed")
    if src != entry["source"]:
        _fail("metadata_changed")
    _group(entry["group"], node["content"].encode(), kind=entry["kind"], bounded=True)


@contextmanager
def _existing_writer(store):
    """Use native atomic writes without native create/repair side effects."""
    if getattr(store, "read_only", False):
        _fail("unavailable")
    opened = getattr(store, "_conn", None) is None
    if opened:
        conn = sqlite3.connect(store.db_path.resolve().as_uri()+"?mode=rw", uri=True,
                               timeout=getattr(store, "_sqlite_timeout", 5.0))
        conn.row_factory = sqlite3.Row
        store._conn = conn
    else:
        conn = store.conn
    try:
        if conn.in_transaction:
            _fail("transaction_conflict")
        from .schema import SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION):
            _fail("unavailable")
        if opened:
            conn.execute("PRAGMA foreign_keys=ON")
        yield
    finally:
        if opened:
            store._conn = None
            conn.close()


def _atomic(store, target, scope, mutate):
    result = None

    def update(_native_parsed_extra):
        nonlocal result
        # Read raw JSON anew *inside* native BEGIN IMMEDIATE. Do not accept its
        # legacy fallback/normalization of a malformed extra column.
        owner = _scope(store, scope)
        node = _raw_node(store, target)
        if node is None:
            _fail("unavailable")
        try:
            _binding(node, scope)
        except PracticalMemoryError:
            _fail("scope_mismatch")
        extra = copy.deepcopy(node["extra"])
        # Both namespaces are strict even when the current write targets only
        # one of them; no unrelated malformed namespace is silently repaired.
        if _MEMORY in extra:
            _register(extra, scope, absent=False)
        if _INDEX in extra:
            try:
                _index_register(extra, scope, target)
            except PracticalMemoryError:
                _fail("unavailable")
        result = mutate(extra, node, owner)
        if redact(extra) != extra or redact_serialized(_c(result).decode()) != _c(result).decode():
            _fail("privacy_unavailable")
        return extra

    try:
        with _existing_writer(store):
            store.atomic_extra_update(target, update)
    except _Replay as replay:
        return replay.receipt
    return result


@_public
def persist(store, request):
    """Atomically retain complete model-visible groups and a payload receipt."""
    at = _request(request, ("scope", "evaluation_time", "operation_id", "prepared", "entry_ids"))
    _string(request["operation_id"], minimum=1, maximum=256)
    _ids(request["entry_ids"])
    entries = _prepared(request["prepared"], request["scope"], request["evaluation_time"])
    selected = []
    for entry_id in request["entry_ids"]:
        if entry_id not in entries:
            _fail()
        entry = entries[entry_id]
        _entry_shape(entry, request["scope"], bounded=True)
        selected.append(entry)

    def mutate(extra, node, owner):
        reg = _register(extra, request["scope"])
        key = _replay(reg, "persist", request)
        for entry in selected:
            _current_entry(store, entry, request["scope"], at)
        target = request["prepared"]["projection_sha256"]
        if target not in reg["projections"] and len(reg["projections"]) >= 32:
            _fail("capacity")
        receipt = _receipt("persist", request, target)
        for entry in selected:
            eid = entry["entry_id"]
            if eid not in reg["retained"]:
                if len(reg["retained"]) >= 16:
                    _fail("capacity")
                reg["retained"][eid] = {"entry": copy.deepcopy(entry), "captured_at": receipt["receipt"]["committed_at"]}
        reg["operations"][key] = receipt
        if target not in reg["projections"]:
            reg["projections"][target] = {"payload_sha256": request["prepared"]["text_sha256"], "persist_receipt": receipt}
        extra[_MEMORY] = reg
        return receipt

    return _atomic(store, request["scope"]["session_id"], request["scope"], mutate)


@_public
def index(store, request):
    """CAS a derived source-span representation, preserving immutable history."""
    at = _request(request, ("scope", "evaluation_time", "operation_id", "node_id", "index_kind", "expected_source_sha256", "expected_metadata_sha256", "expected_index_sha256", "groups"))
    _string(request["operation_id"], minimum=1, maximum=256)
    _string(request["node_id"], minimum=1, maximum=256)
    if request["index_kind"] not in ("assertion", "episode"):
        _fail()
    for field in ("expected_source_sha256", "expected_metadata_sha256"):
        _digest(request[field])
    _digest(request["expected_index_sha256"], nullable=True)
    _groups(request["groups"], kind=request["index_kind"])

    def mutate(extra, node, owner):
        try:
            reg = _index_register(extra, request["scope"], request["node_id"])
        except PracticalMemoryError:
            _fail("unavailable")
        key = _replay(reg, "index", request)
        current, reason = _admit(store, request["node_id"], request["scope"], at)
        if reason:
            _fail("source_changed")
        src = _source(current, request["scope"])
        if src["source_sha256"] != request["expected_source_sha256"]:
            _fail("source_changed")
        if src["metadata_sha256"] != request["expected_metadata_sha256"]:
            _fail("metadata_changed")
        kind = request["index_kind"]
        pointer = reg["current"].get(kind)
        current_hash = reg["history"][pointer]["index_sha256"] if pointer is not None else None
        if current_hash != request["expected_index_sha256"]:
            _fail("index_conflict")
        _groups(request["groups"], kind=kind, body=current["content"].encode())
        target = _d("index", {"source": src, "index_kind": kind, "groups": request["groups"]})
        receipt = _receipt("index", request, target)
        reg["history"][key] = {"source": src, "index_kind": kind, "groups": copy.deepcopy(request["groups"]), "index_sha256": target, "scope": copy.deepcopy(request["scope"]), "committed_at": receipt["receipt"]["committed_at"], "receipt": receipt}
        reg["current"][kind] = key
        reg["operations"][key] = receipt
        extra[_INDEX] = reg
        return receipt

    return _atomic(store, request["node_id"], request["scope"], mutate)


@_public
def ack(store, request):
    """Record an explicit host assertion about one exact persisted payload."""
    _request(request, ("scope", "evaluation_time", "operation_id", "projection_sha256", "host_receipt", "payload_sha256"))
    _string(request["operation_id"], minimum=1, maximum=256)
    _string(request["host_receipt"], minimum=1, maximum=256)
    _digest(request["projection_sha256"])
    _digest(request["payload_sha256"])

    def mutate(extra, node, owner):
        reg = _register(extra, request["scope"], absent=False)
        key = _replay(reg, "ack", request)
        projection = reg["projections"].get(request["projection_sha256"])
        if projection is None or projection["payload_sha256"] != request["payload_sha256"]:
            _fail("unavailable")
        receipt = _receipt("ack", request, request["projection_sha256"])
        reg["operations"][key] = receipt
        extra[_MEMORY] = reg
        return receipt

    return _atomic(store, request["scope"]["session_id"], request["scope"], mutate)


@_public
def organize(store, request):
    """Read once, admit natively, and atomically co-deliver correction records.

    Explicit query references establish relevance only. The pure renderer never
    receives arbitrary caller sources, and correction membership expands only
    within this one admitted native retrieval pool.
    """
    at = _request(request, ("scope", "evaluation_time", "query", "top_k", "protected_node_ids", "total_max_bytes", "order"))
    if not _string(request["query"], minimum=1, maximum=4096).strip():
        _fail()
    _integer(request["top_k"], 1, 32)
    _integer(request["total_max_bytes"])
    if type(request["order"]) is not str or request["order"] not in ("native", "relevance"):
        _fail()
    protected = request["protected_node_ids"]
    if type(protected) is not list or len(protected) > 32:
        _fail()
    for node_id in protected:
        _string(node_id, minimum=1, maximum=128)
        if re.search(r"(?<!\w)" + re.escape(node_id) + r"(?!\w)", request["query"], re.UNICODE) is None:
            _fail()
    if len(set(protected)) != len(protected):
        _fail()
    from . import evidence_organizer
    from .retrieve import hybrid_search

    with _snapshot(store):
        scope = request["scope"]
        owner = _scope(store, scope)
        _register(owner["extra"], scope)
        hits = hybrid_search(store, request["query"], top_k=request["top_k"],
                             expand_graph=True, graph_hops=1, ranking="ensemble",
                             use_vectors=False, trusted_only=True,
                             include_expired=False, include_archived=False,
                             evaluation_time=at, recency_time=at)
        counts = Counter()
        nodes = {}
        seen = set()
        for hit in hits:
            node_id = hit["id"]
            if node_id in seen:
                continue
            seen.add(node_id)
            node, reason = _admit(store, node_id, scope, at)
            if reason:
                counts[reason] += 1
                continue
            nodes[node_id] = node
        retrieval_ids = list(nodes)
        relations = evidence_organizer._RELATIONS
        edges = []
        if retrieval_ids:
            marks = ",".join("?" for _ in retrieval_ids)
            relation_marks = ",".join("?" for _ in relations)
            rows = store.conn.execute(
                f"SELECT DISTINCT from_id,to_id,type FROM edges WHERE from_id IN ({marks}) AND to_id IN ({marks}) AND type IN ({relation_marks}) ORDER BY from_id,type,to_id",
                retrieval_ids + retrieval_ids + list(relations)).fetchall()
            edges = [(row["from_id"], row["to_id"], row["type"]) for row in rows]
        # Build membership before deciding renderability. Otherwise a correction
        # that is too large (or has an unsupported ref) could leave its old record
        # behind as an apparently protected complete result.
        components = evidence_organizer._components(retrieval_ids, edges)
        outgoing = {node_id: [] for node_id in retrieval_ids}
        for source, target, relation in edges:
            outgoing[source].append({"to": target, "relation": relation})
        records = {}
        invalid = {}
        for node_id, node in nodes.items():
            body = node["content"]
            size = len(body.encode("utf-8"))
            if size == 0:
                invalid[node_id] = "no_source_body"
                continue
            if size > 65536:
                invalid[node_id] = "source_oversized"
                continue
            kind = ("principle" if node["type"] in ("constraint", "directive") else
                    "episode" if node["type"] in ("episode", "checkpoint") else "fact")
            record = {"id": node_id, "source": {"node_id": node_id,
                      "source_sha256": _sha(body.encode("utf-8")), "start_byte": 0,
                      "end_byte": size, "valid_from": node.get("valid_at"),
                      "valid_until": node.get("invalid_at")}, "kind": kind, "text": body,
                      "roles": {role: [] for role in evidence_organizer._ROLES},
                      "links": outgoing[node_id]}
            try:
                evidence_organizer._record(record)
            except evidence_organizer.OrganizerError:
                invalid[node_id] = "source_unrenderable"
            else:
                records[node_id] = record
        removed = set()
        for component in components:
            if any(node_id in invalid for node_id in component):
                for node_id in component:
                    removed.add(node_id)
                    counts[invalid.get(node_id, "component_unrenderable")] += 1
        admitted_ids = [node_id for node_id in retrieval_ids if node_id not in removed]
        admitted = set(admitted_ids)
        renderable = []
        for node_id in admitted_ids:
            record = records[node_id]
            # Only literal declared links in the retained admitted pool enter
            # the pure request. Correction links cannot cross this removal:
            # their whole connected component was removed above.
            record["links"] = [link for link in record["links"] if link["to"] in admitted]
            renderable.append(record)
        requested = set(protected)
        selected_protected = [node_id for node_id in admitted_ids if node_id in requested]
        unavailable = [node_id for node_id in protected if node_id not in admitted]
        if unavailable:
            counts["protected_unavailable"] += len(unavailable)
        try:
            result = evidence_organizer.organize({"query": request["query"],
                        "records": renderable, "protected_ids": selected_protected,
                        "budget_bytes": request["total_max_bytes"], "order": request["order"]})
        except evidence_organizer.OrganizerError:
            _fail("unavailable")
        return {"format": "kindex.practical_memory.organized", "version": 1,
                "scope": copy.deepcopy(scope), "evaluation_time": request["evaluation_time"],
                "query": request["query"], "retrieval_calls": 1,
                "retrieval_ids": retrieval_ids, "admitted_ids": admitted_ids,
                "omissions": _omissions(counts), "unavailable_protected_ids": unavailable,
                "source_references": [_source(nodes[node_id], scope) for node_id in admitted_ids],
                "organizer": result, "protected_complete": result["protected_complete"] and not unavailable,
                "persistence": "not_requested", "delivery": "not_observed", "use": "not_observed"}
