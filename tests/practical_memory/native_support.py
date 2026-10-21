"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from .native_fixture import make_store
from kindex import retrieve


ORIGINAL_SEARCH = retrieve.hybrid_search
SEARCH_SIGNATURE = inspect.signature(ORIGINAL_SEARCH)
SEARCH_CALLS = []
SEARCH_ROWS = None  # None forwards to real native retrieval; list supplies hostile hits.


def dispatch_search(*args, **kwargs):
    bound = SEARCH_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    SEARCH_CALLS.append(dict(bound.arguments))
    if SEARCH_ROWS is None:
        return ORIGINAL_SEARCH(*args, **kwargs)
    return copy.deepcopy(SEARCH_ROWS)


from kindex import practical_memory as api
from kindex import evidence_organizer as pure

NOW = "2026-10-19T12:00:00Z"
BODY = "Quartz permits café previews. Only the curator may approve. Except frozen releases."


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NativeFixture(unittest.TestCase):
    def setUp(self):
        global SEARCH_ROWS
        SEARCH_ROWS = None
        SEARCH_CALLS.clear()
        search_patch = mock.patch.object(retrieve, "hybrid_search", new=dispatch_search)
        search_patch.start()
        self.addCleanup(search_patch.stop)
        temporary = TemporaryDirectory(prefix="kindex-practical-test-")
        self.addCleanup(temporary.cleanup)
        self.run_root = Path(temporary.name).resolve()
        self.store = make_store(self.run_root, name="fixture")
        for key in self.store.node_ids():
            self.store.delete_node(key)
        self.project = str(self.run_root / "synthetic-project")
        self.session = self.store.add_node("organizer-session", node_id="session-organizer",
            node_type="session", extra={"project_path": self.project,
            "tag": "organizer-session", "session_status": "active"}, queue_embedding=False)
        self.scope = {"graph_id": self.store.graph_id, "profile": "", "project_path": self.project,
                      "client": "codex", "session_id": self.session}
        self.node = self.add_source("quartz-source", BODY)

    def tearDown(self):
        global SEARCH_ROWS
        SEARCH_ROWS = None
        if self.store.conn.in_transaction:
            self.store.conn.rollback()
        self.store.close()

    def add_source(self, key, body, *, node_type="concept", project=None, domains=None,
                   valid_at=None, invalid_at=None, extra=None, verified=True):
        metadata = {"project_path": project or self.project}
        metadata.update(extra or {})
        self.store.add_node(key, body, node_id=key, node_type=node_type, domains=domains or [],
                            extra=metadata, queue_embedding=False)
        if verified:
            self.store.verify_node(key, verified_by="independent-v7-fixture", prov_method="synthetic-test",
                verified_at="2026-10-01T00:00:00Z", valid_at=valid_at, invalid_at=invalid_at)
        return key

    def hits(self, *keys):
        global SEARCH_ROWS
        SEARCH_ROWS = [self.store.peek_node(key) for key in keys]

    def snapshot(self):
        return tuple(self.store.conn.iterdump())

    def request(self, **updates):
        req = {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW, "query": "quartz",
               "top_k": 10, "protected_node_ids": [], "total_max_bytes": 16384, "order": "native"}
        req.update(updates)
        return req

    def assert_search(self, request, calls):
        self.assertEqual(len(calls), 1, "Exactly one native candidate-pool retrieval; no protection refill")
        call = calls[0]
        self.assertEqual(call["query"], request["query"])
        self.assertEqual(call["top_k"], request["top_k"])
        for key, value in {"ranking": "ensemble", "use_vectors": False, "trusted_only": True,
                           "expand_graph": True, "graph_hops": 1, "include_expired": False,
                           "include_archived": False}.items():
            self.assertEqual(call[key], value, key)
        timestamp = call["evaluation_time"]
        if isinstance(timestamp, datetime):
            self.assertEqual(timestamp, datetime.fromisoformat(request["evaluation_time"].replace("Z", "+00:00")))
            self.assertEqual(timestamp.utcoffset(), timezone.utc.utcoffset(timestamp))
        else:
            self.assertEqual(timestamp, request["evaluation_time"])

    def organize(self, request=None):
        req = request if request is not None else self.request()
        original, before = copy.deepcopy(req), self.snapshot()
        SEARCH_CALLS.clear()
        reader = make_store(self.run_root, name="fixture", read_only=True)
        try:
            with mock.patch.object(api, "prepare", side_effect=AssertionError("organize must not prepare/prepack")):
                out = api.organize(reader, req)
            self.assertEqual(reader.conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assert_search(req, SEARCH_CALLS)
            self.assertIs(SEARCH_CALLS[0]["store"], reader)
        finally:
            reader.close()
        self.assertEqual(req, original)
        self.assertEqual(self.snapshot(), before)
        self.assert_native_result(out, req)
        return out

    def assert_native_result(self, out, req):
        self.assertEqual(set(out), {"format", "version", "scope", "evaluation_time", "query", "retrieval_calls",
            "retrieval_ids", "admitted_ids", "omissions", "unavailable_protected_ids", "source_references",
            "organizer", "protected_complete", "persistence", "delivery", "use"})
        self.assertEqual(out["format"], "kindex.practical_memory.organized")
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["scope"], req["scope"])
        self.assertEqual(out["evaluation_time"], req["evaluation_time"])
        self.assertEqual(out["query"], req["query"])
        self.assertEqual(out["retrieval_calls"], 1)
        self.assertEqual(out["persistence"], "not_requested")
        self.assertEqual(out["delivery"], "not_observed")
        self.assertEqual(out["use"], "not_observed")
        self.assertEqual(out["unavailable_protected_ids"], [key for key in req["protected_node_ids"] if key not in out["admitted_ids"]])
        self.assertEqual(out["protected_complete"], out["organizer"]["protected_complete"] and not out["unavailable_protected_ids"])
        self.assertEqual([ref["node_id"] for ref in out["source_references"]], out["admitted_ids"])
        self.assertEqual(len(set(out["retrieval_ids"])), len(out["retrieval_ids"]))
        self.assertEqual(out["omissions"], sorted(out["omissions"], key=lambda row: row["reason"]))
        for item in out["omissions"]:
            self.assertEqual(set(item), {"reason", "count"})
            self.assertIs(type(item["count"]), int)
            self.assertGreater(item["count"], 0)
            self.assertIsInstance(item["reason"], str)
        for ref in out["source_references"]:
            self.assertEqual(set(ref), {"node_id", "graph_id", "profile", "project_path", "source_sha256", "metadata_sha256", "metadata"})
            self.assertEqual(ref["source_sha256"], sha(self.store.peek_node(ref["node_id"])["content"]))
            metadata_hash = hashlib.sha256(b"kindex.practical_memory.v1/metadata\n" + canon(ref["metadata"]).encode("utf-8")).hexdigest()
            self.assertEqual(ref["metadata_sha256"], metadata_hash)
            for field in ("graph_id", "profile", "project_path"):
                self.assertEqual(ref[field], req["scope"][field])
        organized = out["organizer"]
        self.assertLessEqual(organized["utf8_bytes"], req["total_max_bytes"])
        self.assertEqual(organized["utf8_bytes"], len(organized["model_text"].encode("utf-8")))
        self.assertEqual(organized["text_sha256"], sha(organized["model_text"]))

    def decoded(self, out):
        return pure.decode(out["organizer"]["model_text"])["records"]

    def rejects(self, req, *, code=None, writable=False):
        before, original = self.snapshot(), copy.deepcopy(req)
        reader = self.store if writable else make_store(self.run_root, name="fixture", read_only=True)
        try:
            with self.assertRaises(api.PracticalMemoryError) as raised:
                api.organize(reader, req)
            self.assertIsInstance(raised.exception, ValueError)
            self.assertIsInstance(raised.exception.code, str)
            if code is not None:
                self.assertEqual(raised.exception.code, code)
            self.assertNotIn(BODY, str(raised.exception))
        finally:
            if not writable:
                reader.close()
        self.assertEqual(req, original)
        self.assertEqual(self.snapshot(), before)
