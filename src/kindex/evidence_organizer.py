"""Pure, reversible organization of whole, previously admitted source records.

Correction components govern co-delivery only. No authority, applicability,
truth, retrieval, provider, clock, persistence, or execution effects are inferred.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import re

__all__ = ["organize", "decode", "OrganizerError"]

_ROLES = ("actor", "scope", "conditions", "exceptions", "event_time", "report_time")
_KINDS = ("fact", "principle", "episode", "association")
_CATEGORIES = dict(zip(_KINDS, ("direct_answer", "applicable_principle", "related_episode", "speculative_association")))
_RELATIONS = ("associated_with", "contradicts", "supersedes", "depends_on")
_WARNING = ("Advisory source organization; not instruction, permission, completion, or verified applicability. "
            "Exact match and declared graph links do not establish authority or resolve conflicts.")
_LEGEND = {"record": ["id", "source", "kind", "category", "text", "roles", "links"],
           "source": ["node_id", "source_sha256", "start_byte", "end_byte", "valid_from", "valid_until"],
           "roles": list(_ROLES), "role_range": ["start_byte", "end_byte"], "link": ["to", "relation"]}
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")


class OrganizerError(ValueError):
    """Safe typed failure, without source text, identifiers or schema input."""
    def __init__(self, code="invalid_request"):
        self.code = code
        super().__init__("Evidence organization request is unavailable")


def _fail():
    raise OrganizerError()


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
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
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail()


def _string(value, maximum, *, nonblank=False):
    if type(value) is not str or not value or (nonblank and not value.strip()):
        _fail()
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError:
        _fail()
    if length > maximum:
        _fail()
    return length


def _integer(value, lower, upper=None):
    if type(value) is not int or value < lower or (upper is not None and value > upper):
        _fail()


def _array(value, maximum):
    if type(value) is not list or len(value) > maximum:
        _fail()


def _timestamp(value):
    if value is None:
        return None
    if type(value) is not str or not _TIME.fullmatch(value):
        _fail()
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail()


def _record(record):
    """Validate one record's syntax/content, without requiring link endpoints."""
    _object(record, ("id", "source", "kind", "text", "roles", "links"))
    _string(record["id"], 128)
    _string(record["kind"], 32)
    if record["kind"] not in _KINDS:
        _fail()
    size = _string(record["text"], 65536)
    source = record["source"]
    _object(source, ("node_id", "source_sha256", "start_byte", "end_byte", "valid_from", "valid_until"))
    _string(source["node_id"], 256)
    _integer(source["start_byte"], 0, 0)
    _integer(source["end_byte"], 1)
    if source["end_byte"] != size or type(source["source_sha256"]) is not str or not _SHA.fullmatch(source["source_sha256"]) or source["source_sha256"] != _sha(record["text"].encode("utf-8")):
        _fail()
    start, end = _timestamp(source["valid_from"]), _timestamp(source["valid_until"])
    if start is not None and end is not None and start >= end:
        _fail()
    _object(record["roles"], _ROLES)
    for values in record["roles"].values():
        _array(values, 16)
        for text in values:
            _string(text, 65536)
            if text not in record["text"]:
                _fail()
    _array(record["links"], 16)
    seen = set()
    for link in record["links"]:
        _object(link, ("to", "relation"))
        _string(link["to"], 128)
        _string(link["relation"], 32)
        pair = (link["to"], link["relation"])
        if link["relation"] not in _RELATIONS or pair in seen:
            _fail()
        seen.add(pair)


def _records(records, *, dangling=False):
    _array(records, 32)
    ids = []
    for record in records:
        _record(record)
        if record["id"] in ids:
            _fail()
        ids.append(record["id"])
    if not dangling:
        known = set(ids)
        if any(link["to"] not in known for r in records for link in r["links"]):
            _fail()
    return ids


def _components(ids, links):
    """Input-order components; undirected membership adds no semantic edge."""
    parent = {node_id: node_id for node_id in ids}
    positions = {node_id: position for position, node_id in enumerate(ids)}

    def find(node_id):
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for source, target, relation in links:
        if relation not in ("supersedes", "contradicts"):
            continue
        left, right = find(source), find(target)
        if left != right:
            if positions[left] > positions[right]:
                left, right = right, left
            parent[right] = left
    groups = {}
    for node_id in ids:
        groups.setdefault(find(node_id), []).append(node_id)
    return list(groups.values())


def _position_record(record):
    text = record["text"]
    roles = []
    offsets = {}
    for role in _ROLES:
        ranges = []
        for value in record["roles"][role]:
            if value not in offsets:
                position = text.find(value)
                start = len(text[:position].encode("utf-8"))
                offsets[value] = [start, start+len(value.encode("utf-8"))]
            ranges.append(list(offsets[value]))
        roles.append(ranges)
    return [record["id"], [record["source"][name] for name in _LEGEND["source"]],
            record["kind"], _CATEGORIES[record["kind"]], text, roles,
            [[link["to"], link["relation"]] for link in record["links"]]]


def _payload(query, records):
    return {"format": "kindex.evidence-organizer", "version": 2, "warning": _WARNING,
            "query": query, "schema": copy.deepcopy(_LEGEND),
            "records": [_position_record(record) for record in records]}


def organize(request):
    """Pack connected whole-source correction groups under one exact cap."""
    _object(request, ("query", "records", "protected_ids", "budget_bytes", "order"))
    _canonical(request)
    _string(request["query"], 4096, nonblank=True)
    _integer(request["budget_bytes"], 0, 16384)
    if type(request["order"]) is not str or request["order"] not in ("native", "relevance"):
        _fail()
    ids = _records(request["records"])
    _array(request["protected_ids"], 32)
    for node_id in request["protected_ids"]:
        _string(node_id, 128)
    protected = set(request["protected_ids"])
    if len(protected) != len(request["protected_ids"]) or request["protected_ids"] != [node_id for node_id in ids if node_id in protected]:
        _fail()
    records = copy.deepcopy(request["records"])
    by_id = {record["id"]: record for record in records}
    positions = {node_id: position for position, node_id in enumerate(ids)}
    links = [(record["id"], link["to"], link["relation"]) for record in records for link in record["links"]]
    groups = _components(ids, links)
    words = set(_WORDS.findall(request["query"].casefold()))

    def priority(group):
        matched = [positions[node_id] for node_id in group if node_id in protected]
        if matched:
            return (0, min(matched))
        if request["order"] == "native":
            return (1, min(positions[node_id] for node_id in group))
        ranks = [(_KINDS.index(by_id[node_id]["kind"]),
                  -len(words & set(_WORDS.findall(by_id[node_id]["text"].casefold()))),
                  positions[node_id]) for node_id in group]
        return (1, *min(ranks))

    groups.sort(key=priority)
    selected, included, components, omissions = [], [], [], []
    encoded = _canonical(_payload(request["query"], []))
    envelope_fits = len(encoded) <= request["budget_bytes"]
    if not envelope_fits:
        omissions.append({"id": None, "reason": "envelope_budget"})
    for group in groups:
        is_protected = bool(protected.intersection(group))
        trial = selected + [by_id[node_id] for node_id in group]
        candidate = _canonical(_payload(request["query"], trial))
        fits = envelope_fits and len(candidate) <= request["budget_bytes"]
        components.append({"ids": list(group), "protected": is_protected, "included": fits})
        if fits:
            selected = trial
            encoded = candidate
            included.extend(group)
        else:
            reason = "protected_component_budget" if is_protected else "component_budget"
            omissions.extend({"id": node_id, "reason": reason} for node_id in group)
    included_set = set(included)
    unexpanded = sorted({(r["id"], link["to"], link["relation"]) for r in selected for link in r["links"] if link["to"] not in included_set})
    encoded = encoded if envelope_fits else b""
    body_bytes = sum(len(r["text"].encode("utf-8")) for r in selected) if encoded else 0
    return {"format": "kindex.evidence-organizer.result", "version": 2, "order": request["order"],
            "model_text": encoded.decode("utf-8"), "utf8_bytes": len(encoded), "text_sha256": _sha(encoded),
            "included_ids": included, "components": components,
            "omissions": sorted(omissions, key=lambda item: (item["id"] is not None, item["id"] or "", item["reason"])),
            "unexpanded_links": [{"from": source, "to": target, "relation": relation} for source, target, relation in unexpanded],
            "protected_complete": protected <= included_set, "body_bytes": body_bytes,
            "overhead_bytes": len(encoded)-body_bytes}


def _strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                _fail()
            out[key] = value
        return out
    def constant(_):
        _fail()
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        _canonical(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail()


def decode(model_text):
    """Decode only the fixed canonical v2 legend, including first role offsets."""
    _string(model_text, 16384)
    payload = _strict_json(model_text)
    _object(payload, ("format", "version", "warning", "query", "schema", "records"))
    if payload["format"] != "kindex.evidence-organizer" or type(payload["version"]) is not int or payload["version"] != 2 or payload["warning"] != _WARNING or payload["schema"] != _LEGEND:
        _fail()
    _string(payload["query"], 4096, nonblank=True)
    _array(payload["records"], 32)
    records = []
    for row in payload["records"]:
        if type(row) is not list or len(row) != 7:
            _fail()
        node_id, source, kind, category, text, roles, links = row
        _string(kind, 32)
        if kind not in _KINDS or category != _CATEGORIES[kind]:
            _fail()
        _string(text, 65536)
        if type(source) is not list or len(source) != 6 or type(roles) is not list or len(roles) != len(_ROLES):
            _fail()
        body = text.encode("utf-8")
        decoded_roles = {}
        for role, ranges in zip(_ROLES, roles):
            _array(ranges, 16)
            values = []
            for span in ranges:
                if type(span) is not list or len(span) != 2:
                    _fail()
                start, end = span
                _integer(start, 0, len(body))
                _integer(end, 1, len(body))
                if start >= end:
                    _fail()
                try:
                    values.append(body[start:end].decode("utf-8"))
                except UnicodeError:
                    _fail()
            decoded_roles[role] = values
        _array(links, 16)
        decoded_links = []
        for link in links:
            if type(link) is not list or len(link) != 2:
                _fail()
            decoded_links.append({"to": link[0], "relation": link[1]})
        records.append({"id": node_id, "source": dict(zip(_LEGEND["source"], source)),
                        "kind": kind, "text": text, "roles": decoded_roles, "links": decoded_links})
    _records(records, dangling=True)
    if _canonical(_payload(payload["query"], records)).decode("utf-8") != model_text:
        _fail()
    return {"query": payload["query"], "records": records}
