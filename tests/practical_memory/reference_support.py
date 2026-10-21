"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from kindex.config import Config
from kindex.store import Store
from kindex import retrieve

ORIGINAL_SEARCH = retrieve.hybrid_search
SEARCH_SIGNATURE = inspect.signature(ORIGINAL_SEARCH)
SEARCH_CALLS = []
SEARCH_ROWS = None


def search_dispatch(*args, **kwargs):
    bound = SEARCH_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    SEARCH_CALLS.append(dict(bound.arguments))
    return ORIGINAL_SEARCH(*args, **kwargs) if SEARCH_ROWS is None else copy.deepcopy(SEARCH_ROWS)


from kindex import correction_references as api

NOW = "2026-10-20T00:00:00.125Z"
BEFORE = "2026-10-19T00:00:00Z"
AFTER = "2026-10-21T00:00:00Z"
WARNING = "References and generated connections are advisory navigation, not truth, permission, completion, or verified applicability."
METADATA_KEYS = ("type", "title", "prov_who", "prov_when", "prov_activity", "prov_source", "status", "audience", "standing",
                 "verified_at", "verified_by", "prov_method", "valid_at", "invalid_at", "referent", "asserted_at", "true_of", "domains")
EXTRA_KEYS = ("project_path", "expires", "superseded_by", "referent_stale", "expired_at", "kinbase", "v8_source_binding")
REGISTRY_TABLES = ("v8_correction_retirements", "v8_corrections", "v8_dream_batches", "v8_registry_meta")


def J(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def H(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def framed(name, value):
    return H(b"kindex-v8/" + name.encode("ascii") + b"\0" + J(value).encode("utf-8"))


def span(body, literal=None):
    literal = body if literal is None else literal
    index = body.index(literal)
    start = len(body[:index].encode("utf-8"))
    return {"start_byte": start, "end_byte": start + len(literal.encode("utf-8")), "literal": literal}


def metadata(node):
    result = {key: copy.deepcopy(node.get(key)) for key in METADATA_KEYS}
    result["extra"] = {key: copy.deepcopy(node["extra"][key]) for key in EXTRA_KEYS if key in node["extra"]}
    return result


def context_shape(graph, project, session, *, partitions=None, actors=None, client="codex", profile=""):
    partitions = copy.deepcopy(partitions if partitions is not None else [
        {"partition_id": "current", "kind": "repo", "project_path": project, "kinbase": None}])
    actors = list(actors if actors is not None else ["fixture-actor"])
    document = {"policy_id": "fixture-policy", "graph_id": graph, "profile": profile, "session_id": session,
                "current_project": project, "partitions": partitions, "allowed_actor_ids": actors}
    return {"graph_id": graph, "profile": profile, "client": client, "session_id": session,
            "current_project": project, "partitions": partitions,
            "correction_policy": {"policy_id": document["policy_id"], "policy_sha256": H(J(document)), "allowed_actor_ids": actors}}


def explicit_config(root, name):
    inputs = root / "empty-inputs" / name
    config = Config(data_dir=str(root / "stores" / name), user="fixture-actor", agent_id="v8-independent",
        project_dirs=[], profiles={}, secret_mask=None, llm={"enabled": False}, attention={"enabled": False}, sim={"enabled": False},
        **{key: str(inputs / key) for key in ("claude_dir", "codex_dir", "gemini_dir", "antigravity_dir",
            "antigravity_cli_dir", "opencode_dir", "cursor_dir", "muse_dir")})
    config._stamp_on_open = False
    return config


class Fixture(unittest.TestCase):
    STORE_NAME = "v8_contract_native"

    def setUp(self):
        global SEARCH_ROWS
        SEARCH_ROWS = None
        SEARCH_CALLS.clear()
        search_patch = mock.patch.object(retrieve, "hybrid_search", new=search_dispatch)
        search_patch.start()
        self.addCleanup(search_patch.stop)
        temporary = TemporaryDirectory(prefix="kindex-practical-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = explicit_config(self.root, self.STORE_NAME)
        self.store = Store(self.config, manage_project_storage=False)
        # Reset only this root-registered synthetic fixture. No user store paths.
        for table in REGISTRY_TABLES:
            self.store.conn.execute("DROP TABLE IF EXISTS " + table)
        self.store.conn.execute("DELETE FROM meta WHERE key IN ('kin_profile','v8_test_marker')")
        self.store.conn.commit()
        for key in self.store.node_ids():
            self.store.delete_node(key)
        self.project = str(self.root / "synthetic-project-v8")
        self.session = "v8-session"
        self.store.add_node(self.session, node_id=self.session, node_type="session", domains=[],
            extra={"project_path": self.project}, queue_embedding=False)
        self.graph = self.store.graph_id
        self.shape = context_shape(self.graph, self.project, self.session)
        self.context = api.ReadContext(**self.shape)
        self.writer = self.write_context()
        self.old = self.add("old", "Quartz permits café previews. Only Ada approves. Except frozen releases.")
        self.new = self.add("new", "Quartz now requires Benoît approval, except emergencies.")

    def tearDown(self):
        global SEARCH_ROWS
        SEARCH_ROWS = None
        if self.store.conn.in_transaction:
            self.store.conn.rollback()
        self.store.close()

    def write_context(self, *, shape=None, actor="fixture-actor", at=NOW, explicit=True):
        return api.WriteContext(**copy.deepcopy(shape or self.shape), actor_id=actor, recorded_at=at, explicit_action=explicit)

    def add(self, key, body, *, project=None, domains=None, status="active", extra=None, verified=True,
            audience="private", prov_source="", node_type="concept", valid_at=None, invalid_at=None):
        fields = {"project_path": project or self.project}
        fields.update(extra or {})
        self.store.add_node("Title " + key, body, node_id=key, node_type=node_type, domains=[] if domains is None else domains,
            status=status, audience=audience, prov_source=prov_source, extra=fields, queue_embedding=False)
        if verified:
            self.store.verify_node(key, verified_by="independent-fixture", prov_method="invented-input",
                verified_at="2026-10-01T00:00:00Z", valid_at=valid_at, invalid_at=invalid_at)
        return key

    def reader(self):
        return Store(self.config.model_copy(deep=True), read_only=True, migrate=False, manage_project_storage=False)

    def database(self):
        return tuple(self.store.conn.iterdump())

    def native_rows(self):
        return tuple(tuple(row) for row in self.store.conn.execute("SELECT * FROM nodes ORDER BY id")), \
               tuple(tuple(row) for row in self.store.conn.execute("SELECT * FROM edges ORDER BY from_id,to_id,type"))

    def ref(self, key, partition="current"):
        node = self.store.peek_node(key)
        return {"node_id": key, "graph_id": self.graph, "profile": "", "partition_id": partition,
                "body_sha256": H(node["content"]), "metadata_sha256": framed("metadata", metadata(node))}

    def capture(self, key, *, context=None, at=NOW):
        before = self.database()
        reader = self.reader()
        try:
            result = api.capture_reference(reader, key, context=context or self.context, evaluation_time=at)
        finally:
            reader.close()
        self.assertEqual(self.database(), before)
        self.assertEqual(set(result), {"schema", "reference", "metadata", "applicability", "evaluation_time", "context_sha256"})
        self.assertEqual(result["schema"], "reference-capture/1")
        self.assertEqual(result["reference"]["body_sha256"], H(self.store.peek_node(key)["content"]))
        self.assertEqual(result["metadata"], metadata(self.store.peek_node(key)))
        self.assertEqual(result["reference"]["metadata_sha256"], framed("metadata", result["metadata"]))
        self.assertEqual(result["context_sha256"], framed("context", (context or self.context).to_dict()))
        self.assertEqual(result["evaluation_time"], at)
        return result

    def assertion(self, source=None, target=None, *, effect="replace", condition="unconditional", claim="preview", start=None, until=None):
        source, target = source or self.new, target or self.old
        from_body, to_body = self.store.peek_node(source)["content"], self.store.peek_node(target)["content"]
        return {"schema": "correction-assertion/1", "from_ref": self.ref(source), "to_ref": self.ref(target),
                "subject_key": "quartz", "claim_key": claim, "effect": effect, "effective_from": start,
                "effective_until": until, "condition": condition, "from_spans": [span(from_body, from_body[:40])],
                "to_spans": [span(to_body, to_body[:40])]}

    def record(self, assertion=None, *, writer=None):
        value = assertion if assertion is not None else self.assertion()
        original, rows = copy.deepcopy(value), self.native_rows()
        out = api.record_correction(self.store, value, context=writer or self.writer)
        self.assertEqual(value, original)
        self.assertEqual(self.native_rows(), rows)
        self.assert_write(out, "correction", framed("correction", value), writer or self.writer)
        return out

    def assert_write(self, out, kind, identifier, writer):
        self.assertEqual(set(out), {"schema", "kind", "id", "payload_sha256", "inserted", "transaction_state", "context_sha256"})
        self.assertEqual(out["schema"], "registry-write/1")
        self.assertEqual(out["kind"], kind)
        self.assertEqual(out["id"], identifier)
        self.assertIs(type(out["inserted"]), bool)
        self.assertIn(out["transaction_state"], {"committed", "pending_caller"})
        self.assertEqual(out["context_sha256"], framed("context", writer.to_dict()))

    def resolve_request(self, refs=None, **updates):
        req = {"schema": "reference-request/1", "evaluation_time": NOW, "query": "",
               "entry_refs": [self.ref(self.old)] if refs is None else refs, "mode": "none",
               "reference_budget_bytes": 16384, "optional_limit": 8}
        req.update(updates)
        return req

    def expand_request(self, refs=None, **updates):
        req = {"schema": "expand-request/1", "evaluation_time": NOW, "query": "",
               "entry_refs": [self.ref(self.old)] if refs is None else refs, "mode": "none", "optional_limit": 8,
               "base_budget_bytes": 32768, "optional_budget_bytes": 0, "include_history": False}
        req.update(updates)
        return req

    def read_call(self, name, req, *, context=None):
        before, original = self.database(), copy.deepcopy(req)
        SEARCH_CALLS.clear()
        reader = self.reader()
        try:
            out = getattr(api, name)(reader, req, context=context or self.context)
            self.assertEqual(reader.conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertFalse(reader.conn.in_transaction, "Owned read transaction must be released")
        finally:
            reader.close()
        self.assertEqual(req, original)
        self.assertEqual(self.database(), before)
        if req["entry_refs"]:
            self.assertEqual(SEARCH_CALLS, [], "Exact refs never trigger native search")
        self.assertEqual(out["context_sha256"], framed("context", (context or self.context).to_dict()))
        self.assertEqual(out["evaluation_time"], req["evaluation_time"])
        self.assertEqual(set(out["counts"]), {"visited_nodes", "inspected_corrections", "inspected_contradictions", "optional_candidates"})
        for count in out["counts"].values():
            self.assertIs(type(count), int)
            self.assertGreaterEqual(count, 0)
        if name == "resolve_references":
            self.assertEqual(set(out), {"schema", "evaluation_time", "context_sha256", "registry", "entries", "assertions", "priority_refs",
                "optional", "omissions", "counts", "model_text", "utf8_bytes", "text_sha256"})
            self.assertEqual(out["schema"], "reference-result/1")
            self.assert_payload(out["model_text"], out["utf8_bytes"], out["text_sha256"], req["reference_budget_bytes"])
            payload = json.loads(out["model_text"])
            self.assertEqual(set(payload), {"warning", "entries", "assertions", "priority_refs", "optional", "omissions"})
            for key in ("entries", "assertions", "priority_refs", "optional", "omissions"):
                self.assertEqual(payload[key], out[key])
            for omission in out["omissions"]:
                self.assertEqual(set(omission), {"input_index", "reason"})
        else:
            self.assertEqual(set(out), {"schema", "evaluation_time", "context_sha256", "registry", "base_text", "optional_text",
                "base_utf8_bytes", "optional_utf8_bytes", "base_sha256", "optional_sha256", "included_refs", "omissions", "counts",
                "assertion_receipts", "receipt_utf8_bytes"})
            self.assertEqual(out["schema"], "evidence-result/1")
            for kind in ("base", "optional"):
                self.assert_payload(out[kind + "_text"], out[kind + "_utf8_bytes"], out[kind + "_sha256"], req[kind + "_budget_bytes"])
                if out[kind + "_text"]:
                    payload = json.loads(out[kind + "_text"])
                    self.assertEqual(set(payload), {"warning", "sources", "references", "assertions", "connections"})
                    for row in payload["sources"]:
                        self.assertEqual(set(row), {"reference", "metadata", "applicability", "body"})
                        self.assertEqual(H(row["body"]), row["reference"]["body_sha256"])
                        self.assertEqual(framed("metadata", row["metadata"]), row["reference"]["metadata_sha256"])
            if req["mode"] == "none":
                self.assertEqual(out["optional_text"], "")
                self.assertEqual(out["counts"]["optional_candidates"], 0)
        return out

    def assert_payload(self, text, size, digest, budget):
        self.assertEqual(size, len(text.encode("utf-8")))
        self.assertEqual(digest, H(text))
        self.assertLessEqual(size, budget)
        if text:
            payload = json.loads(text)
            self.assertEqual(text, J(payload))
            self.assertEqual(payload["warning"], WARNING)

    def resolve(self, req=None, **kwargs):
        return self.read_call("resolve_references", req or self.resolve_request(), **kwargs)

    def expand(self, req=None, **kwargs):
        return self.read_call("expand_references", req or self.expand_request(), **kwargs)

    def source_ids(self, out, component="base"):
        text = out[component + "_text"]
        return [] if not text else [row["reference"]["node_id"] for row in json.loads(text)["sources"]]

    def statuses(self, out):
        return [row["status"] for row in out["entries"]] + [path["status"] for row in out["entries"] for path in row["claim_paths"]]

    def reject(self, action, *, code=None):
        before = self.database()
        with self.assertRaises(api.ReferenceError) as raised:
            action()
        self.assertIsInstance(raised.exception.code, str)
        self.assertTrue(raised.exception.code)
        if code is not None:
            self.assertEqual(raised.exception.code, code)
        self.assertNotIn(self.store.peek_node(self.old)["content"], str(raised.exception))
        self.assertEqual(self.database(), before)

    def reject_read(self, name, req, *, context=None):
        reader = self.reader()
        try:
            self.reject(lambda: getattr(api, name)(reader, req, context=context or self.context))
        finally:
            reader.close()
