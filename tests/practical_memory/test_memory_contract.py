"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory

from .native_fixture import make_store
from kindex import practical_memory as api
from .contract_assertions import (COMPONENTS, FLAGS, QUESTIONS, ContractAssertions,
                                 annotated_group, canonical, digest, sha, whole_group)


NOW = "2026-10-19T12:00:00Z"
BODY = "Quartz permits café previews. Only the curator may approve. Except frozen releases."


class PracticalMemoryContract(ContractAssertions, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="kindex-memory-test-")
        self.addCleanup(temporary.cleanup)
        self.run_root = Path(temporary.name).resolve()
        self.store = make_store(self.run_root, name="fixture")
        for node_id in self.store.node_ids():
            self.store.delete_node(node_id)
        self.project = str(self.run_root / "synthetic-project")
        session = self.store.add_node("contract-session", node_id="session-contract",
            node_type="session", extra={"project_path": self.project,
            "tag": "contract-session", "session_status": "active"}, queue_embedding=False)
        self.scope = {"graph_id": self.store.graph_id, "profile": "",
                      "project_path": self.project, "client": "codex", "session_id": session}
        self.node = self.add_source("quartz-source", BODY)

    def tearDown(self):
        if self.store.conn.in_transaction:
            self.store.conn.rollback()
        self.store.close()

    def add_source(self, node_id, body, *, node_type="concept", project=None, extra=None,
                   valid_at=None, invalid_at=None, domains=None):
        fields = {"project_path": project or self.project, "unrelated": {"keep": [1, "é"]}}
        fields.update(extra or {})
        self.store.add_node(node_id, body, node_id=node_id, node_type=node_type,
            extra=fields, domains=domains or [], queue_embedding=False)
        self.store.verify_node(node_id, verified_by="contract-fixture",
            prov_method="synthetic-test", verified_at="2026-10-01T00:00:00Z",
            valid_at=valid_at, invalid_at=invalid_at)
        return node_id

    def snapshot(self):
        return tuple(self.store.conn.iterdump())

    def prepare_request(self, **updates):
        request = {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW,
            "query": "quartz", "features": {key: False for key in FLAGS},
            "top_k": 10, "retained_ids": [], "budgets": {key: 16384 for key in COMPONENTS},
            "total_max_bytes": 16384}
        request.update(updates)
        return request

    def prepare(self, request=None):
        request = request or self.prepare_request()
        reader = make_store(self.run_root, name="fixture", read_only=True)
        try:
            result = api.prepare(reader, request)
        finally:
            reader.close()
        self.assert_prepared(result, request)
        return result

    def audit(self, request):
        reader = make_store(self.run_root, name="fixture", read_only=True)
        try:
            return api.audit(reader, request)
        finally:
            reader.close()

    def one_entry(self, result):
        entries = [entry for entry in result["components"]["sources"]["entries"]
                   if entry["source"]["node_id"] == self.node]
        self.assertEqual(len(entries), 1, "Known trusted exact-token source must be represented")
        return entries[0]

    def persist_request(self, prepared=None, operation_id="persist-first", entry_ids=None):
        prepared = prepared or self.prepare()
        if entry_ids is None:
            entry_ids = [self.one_entry(prepared)["entry_id"]]
        return {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW,
            "operation_id": operation_id, "prepared": prepared, "entry_ids": entry_ids}

    def index_request(self, *, operation_id="index-first", group=None, kind="assertion"):
        entry = self.one_entry(self.prepare())
        source = entry["source"]
        if group is None:
            group = annotated_group(BODY, [("claim", "Quartz permits café previews."),
                ("actor", "Only the curator may approve."),
                ("exceptions", "Except frozen releases.")], episode_key="episode-one" if kind == "episode" else "")
        return {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW,
            "operation_id": operation_id, "node_id": self.node, "index_kind": kind,
            "expected_source_sha256": source["source_sha256"],
            "expected_metadata_sha256": source["metadata_sha256"],
            "expected_index_sha256": None, "groups": [group]}, source

    def assert_error(self, code, function, *args):
        before = self.snapshot()
        with self.assertRaises(api.PracticalMemoryError) as raised:
            function(*args)
        self.assertIsInstance(raised.exception, ValueError)
        self.assertEqual(raised.exception.code, code)
        self.assertIsInstance(raised.exception.message, str)
        self.assertNotIn(BODY, str(raised.exception))
        self.assertEqual(self.snapshot(), before, "Rejected request must leave state unchanged")

    def test_01_plain_sources_are_exact_and_prepare_is_read_only(self):
        before = self.snapshot()
        result = self.prepare()
        entry = self.one_entry(result)
        self.assertEqual(entry["kind"], "whole_source")
        self.assertEqual(entry["group"], whole_group(BODY))
        self.assertEqual(entry["source"]["source_sha256"], sha(BODY))
        for name in ("continuity", "links", "application_trace"):
            self.assertEqual(result["components"][name]["entries"], [])
            self.assertEqual(result["components"][name]["omissions"], [])
        self.assertEqual(self.snapshot(), before)

    def test_02_exact_component_and_total_budget_boundaries(self):
        full = self.prepare()
        self.one_entry(full)
        request = self.prepare_request(total_max_bytes=full["utf8_bytes"])
        self.one_entry(self.prepare(request))
        request["total_max_bytes"] -= 1
        result = self.prepare(request)
        self.assertEqual(result["components"]["sources"]["entries"], [])
        self.assertIn({"reason": "total_budget", "count": 1}, result["components"]["sources"]["omissions"])
        request = self.prepare_request()
        request["budgets"]["sources"] = full["components"]["sources"]["utf8_bytes"] - 1
        result = self.prepare(request)
        self.assertEqual(result["components"]["sources"]["entries"], [])
        self.assertIn({"reason": "component_budget", "count": 1}, result["components"]["sources"]["omissions"])

    def test_03_zero_total_reports_envelope_without_hidden_entries(self):
        result = self.prepare(self.prepare_request(total_max_bytes=0))
        self.assertEqual(result["model_text"], "")
        self.assertIn({"reason": "envelope_budget", "count": 1}, result["components"]["sources"]["omissions"])

    def test_04_compact_tables_preserve_exact_inline_entries(self):
        plain = self.prepare()
        request = self.prepare_request()
        request["features"]["compact_packing"] = True
        compact = self.prepare(request)
        self.assertEqual(compact["components"]["sources"]["entries"], plain["components"]["sources"]["entries"])

    def test_05_strict_request_types_and_unknown_fields(self):
        cases = [dict(top_k=True), dict(total_max_bytes="1000"), dict(top_k=0),
                 dict(query=" "), dict(evaluation_time="2026-10-19T12:00:00+00:00"),
                 dict(unrecognized=True), dict(retained_ids=["missing"])]
        for updates in cases:
            with self.subTest(updates=updates):
                request = self.prepare_request(**updates)
                reader = make_store(self.run_root, name="fixture", read_only=True)
                try:
                    self.assert_error("invalid_request", api.prepare, reader, request)
                finally:
                    reader.close()

    def test_06_wrong_owner_scope_rejects_without_disclosure(self):
        request = self.prepare_request()
        request["scope"]["project_path"] = str(self.run_root / "another-project")
        reader = make_store(self.run_root, name="fixture", read_only=True)
        try:
            self.assert_error("scope_mismatch", api.prepare, reader, request)
        finally:
            reader.close()

    def test_07_foreign_project_source_is_never_visible(self):
        foreign = self.add_source("foreign-private-id", "Quartz foreign-only-secret-phrase.",
                                  project=str(self.run_root / "foreign-project"))
        result = self.prepare()
        self.one_entry(result)
        rendered = canonical(result).decode()
        self.assertNotIn(foreign, rendered)
        self.assertNotIn("foreign-only-secret-phrase", rendered)

    def test_08_index_replaces_body_and_preserves_all_qualifiers(self):
        request, source = self.index_request()
        before_source = self.store.peek_node(self.node)
        target = digest("index", {"source": source, "index_kind": "assertion", "groups": request["groups"]})
        result = api.index(self.store, request)
        self.assert_receipt(result, "index", request, target)
        after_source = self.store.peek_node(self.node)
        self.assertEqual(before_source["content"], after_source["content"])
        self.assertEqual(before_source["extra"]["unrelated"], after_source["extra"]["unrelated"])
        plain = self.prepare()
        self.assertEqual(self.one_entry(plain)["group"], whole_group(BODY))
        prep_request = self.prepare_request()
        prep_request["features"]["assertion_index"] = True
        indexed = self.prepare(prep_request)
        entry = self.one_entry(indexed)
        self.assertEqual(entry["kind"], "assertion")
        self.assertEqual(entry["group"], request["groups"][0])
        self.assertEqual(entry["source"], source, "Derived namespace writes must not invalidate metadata")

    def test_09_index_bad_utf8_slice_and_cas_are_atomic(self):
        request, _ = self.index_request()
        bad = copy.deepcopy(request)
        bad["groups"][0]["spans"][0]["end_byte"] -= 1
        self.assert_error("invalid_span", api.index, self.store, bad)
        api.index(self.store, request)
        conflict = copy.deepcopy(request)
        conflict["operation_id"] = "different-index-op"
        self.assert_error("index_conflict", api.index, self.store, conflict)

    def test_10_persist_replay_is_byte_identical_and_request_bound(self):
        request = self.persist_request()
        receipt = api.persist(self.store, request)
        self.assert_receipt(receipt, "persist", request, request["prepared"]["projection_sha256"])
        before = self.snapshot()
        self.assertEqual(api.persist(self.store, copy.deepcopy(request)), receipt)
        self.assertEqual(self.snapshot(), before)
        conflict = copy.deepcopy(request)
        conflict["entry_ids"] = []
        self.assert_error("operation_conflict", api.persist, self.store, conflict)

    def test_11_persist_tampered_and_unrepresented_entries_reject(self):
        request = self.persist_request()
        bad = copy.deepcopy(request)
        bad["prepared"]["model_text"] += " "
        self.assert_error("invalid_request", api.persist, self.store, bad)
        bad = copy.deepcopy(request)
        bad["entry_ids"] = ["f" * 64]
        self.assert_error("invalid_request", api.persist, self.store, bad)

    def test_12_continuity_requires_explicit_saved_hit_and_rechecks_version(self):
        persist_request = self.persist_request()
        api.persist(self.store, persist_request)
        retained_id = persist_request["entry_ids"][0]
        request = self.prepare_request(query="unmatchedzzzzz")
        request["features"]["continuity"] = True
        empty = self.prepare(request)
        self.assertEqual(empty["components"]["continuity"]["entries"], [])
        request["retained_ids"] = [retained_id]
        result = self.prepare(request)
        entries = result["components"]["continuity"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "retained")
        self.assertEqual(entries[0]["group"], whole_group(BODY))
        self.store.update_node(self.node, content=BODY + " Changed.")
        stale = self.prepare(request)
        self.assertEqual(stale["components"]["continuity"]["entries"], [])
        self.assertIn({"reason": "retained_stale", "count": 1}, stale["components"]["continuity"]["omissions"])
        before = self.snapshot()
        historical = api.persist(self.store, persist_request)
        self.assertEqual(historical["receipt"]["target_sha256"], persist_request["prepared"]["projection_sha256"])
        self.assertEqual(before, self.snapshot(), "Historical replay may not revalidate/repair old source")

    def test_13_ack_binds_payload_and_preserves_persistence_receipt(self):
        persist_request = self.persist_request()
        persisted = api.persist(self.store, persist_request)
        prepared = persist_request["prepared"]
        request = {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW,
            "operation_id": "ack-one", "projection_sha256": prepared["projection_sha256"],
            "host_receipt": "opaque-host-observation", "payload_sha256": prepared["text_sha256"]}
        acknowledged = api.ack(self.store, request)
        self.assert_receipt(acknowledged, "ack", request, prepared["projection_sha256"])
        self.assertEqual(api.persist(self.store, persist_request), persisted)
        before = self.snapshot()
        self.assertEqual(api.ack(self.store, request), acknowledged)
        self.assertEqual(self.snapshot(), before)

    def test_14_mutations_preserve_preexisting_caller_transaction(self):
        request = self.persist_request()
        self.store.conn.execute("BEGIN")
        self.assert_error("transaction_conflict", api.persist, self.store, request)
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()

    def test_15_audit_binds_exact_answer_and_never_claims_semantic_check(self):
        constraint = self.add_source("constraint-one", "Quartz needs approval except sandbox previews.", node_type="constraint")
        request = {"scope": copy.deepcopy(self.scope), "evaluation_time": NOW,
            "answer": "A café preview can proceed.", "constraint_ids": [constraint], "total_max_bytes": 16384}
        before = self.snapshot()
        result = self.audit(request)
        self.assertEqual(set(result), {"format", "version", "scope", "evaluation_time", "answer_sha256",
            "constraints_sha256", "model_text", "utf8_bytes", "text_sha256", "omissions", "mode",
            "semantic_validation", "delivery", "use"})
        self.assertEqual(result["mode"], "packet_only")
        self.assertEqual(result["semantic_validation"], "not_performed")
        self.assertEqual(result["delivery"], "not_observed")
        self.assertEqual(result["use"], "not_observed")
        self.assertEqual(result["answer_sha256"], digest("answer", request["answer"]))
        self.assertEqual(result["text_sha256"], sha(result["model_text"]))
        self.assertEqual(result["utf8_bytes"], len(result["model_text"].encode()))
        payload = json.loads(result["model_text"])
        self.assertEqual(payload["answer"], request["answer"])
        self.assertEqual(payload["entries"][0]["questions"], QUESTIONS)
        self.assertEqual(payload["entries"][0]["constraint"]["node_id"], constraint)
        self.assertEqual(result["constraints_sha256"], digest("constraints", payload["entries"]))
        self.assertEqual(self.snapshot(), before)
        request["total_max_bytes"] = 0
        omitted = self.audit(request)
        self.assertEqual(omitted["model_text"], "")
        self.assertEqual(omitted["constraints_sha256"], result["constraints_sha256"])
        self.assertIn({"reason": "envelope_budget", "count": 1}, omitted["omissions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
