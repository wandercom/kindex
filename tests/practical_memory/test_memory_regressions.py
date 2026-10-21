"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest

from . import test_memory_contract as core
from .contract_assertions import QUESTIONS, annotated_group, canonical, digest, whole_group


class PracticalMemoryRegressions(core.PracticalMemoryContract):
    def assert_typed_rejection(self, function, request):
        before = self.snapshot()
        with self.assertRaises(core.api.PracticalMemoryError) as raised:
            function(self.store, request)
        self.assertIn(raised.exception.code, {"invalid_request", "scope_mismatch", "source_changed",
            "metadata_changed", "invalid_span", "invalid_group", "index_conflict", "operation_conflict",
            "capacity", "transaction_conflict", "unavailable", "privacy_unavailable"})
        self.assertNotIn(core.BODY, str(raised.exception))
        self.assertEqual(self.snapshot(), before)

    def audit_request(self, ids, time=core.NOW):
        return {"scope": copy.deepcopy(self.scope), "evaluation_time": time,
                "answer": "Synthetic audit answer.", "constraint_ids": ids, "total_max_bytes": 16384}

    def test_20_new_persist_rejects_source_and_metadata_drift(self):
        original = self.persist_request()
        self.store.update_node(self.node, content=core.BODY + " Drifted.")
        self.assert_error("source_changed", core.api.persist, self.store, original)
        self.store.update_node(self.node, content=core.BODY)
        request = self.persist_request()
        self.store.update_node(self.node, title="Changed source heading")
        self.assert_error("metadata_changed", core.api.persist, self.store, request)

    def test_21_selected_sources_commit_all_or_none(self):
        other = self.add_source("quartz-second", "Quartz requires complete atomic capture.")
        prepared = self.prepare()
        entries = prepared["components"]["sources"]["entries"]
        self.assertEqual({entry["source"]["node_id"] for entry in entries}, {self.node, other})
        request = self.persist_request(prepared, entry_ids=[entry["entry_id"] for entry in entries])
        self.store.update_node(other, content="Quartz changed after preparation.")
        self.assert_error("source_changed", core.api.persist, self.store, request)
        self.assertNotIn("practical_memory_v1", self.store.peek_node(self.scope["session_id"])["extra"])

    def test_22_index_history_replay_and_stale_cas(self):
        first, source = self.index_request()
        first_receipt = core.api.index(self.store, first)
        before = self.snapshot()
        self.assertEqual(core.api.index(self.store, first), first_receipt)
        self.assertEqual(self.snapshot(), before)
        next_group = annotated_group(core.BODY, [("claim", "Quartz permits café previews.")], group_id="revision-two")
        second = copy.deepcopy(first)
        second.update(operation_id="index-second", expected_index_sha256=first_receipt["receipt"]["target_sha256"], groups=[next_group])
        second_receipt = core.api.index(self.store, second)
        self.assert_receipt(second_receipt, "index", second,
            digest("index", {"source": source, "index_kind": "assertion", "groups": [next_group]}))
        stale = copy.deepcopy(first)
        stale.update(operation_id="index-third-stale", expected_index_sha256=first_receipt["receipt"]["target_sha256"])
        self.assert_error("index_conflict", core.api.index, self.store, stale)
        before = self.snapshot()
        self.assertEqual(core.api.index(self.store, first), first_receipt)
        self.assertEqual(self.snapshot(), before)
        history = self.store.peek_node(self.node)["extra"]["practical_index_v1"]
        encoded = canonical(history).decode()
        self.assertIn(first_receipt["receipt"]["receipt_sha256"], encoded)
        self.assertIn(second_receipt["receipt"]["receipt_sha256"], encoded)
        self.assertIn("Except frozen releases.", encoded)
        self.assertIn("revision-two", encoded)

    def test_23_episode_flag_is_independent_and_requires_episode_key(self):
        request, _ = self.index_request(kind="episode")
        bad = copy.deepcopy(request)
        bad["groups"][0]["episode_key"] = ""
        self.assert_error("invalid_group", core.api.index, self.store, bad)
        core.api.index(self.store, request)
        prep = self.prepare_request()
        prep["features"]["assertion_index"] = True
        self.assertEqual(self.one_entry(self.prepare(prep))["kind"], "whole_source")
        prep["features"]["episode_index"] = True
        entry = self.one_entry(self.prepare(prep))
        self.assertEqual(entry["kind"], "episode")
        self.assertEqual(entry["group"], request["groups"][0])

    def test_24_stale_index_falls_back_to_current_whole_source(self):
        request, _ = self.index_request()
        core.api.index(self.store, request)
        changed = core.BODY + " Updated qualification."
        self.store.update_node(self.node, content=changed)
        prep = self.prepare_request()
        prep["features"]["assertion_index"] = True
        result = self.prepare(prep)
        entry = self.one_entry(result)
        self.assertEqual(entry["kind"], "whole_source")
        self.assertEqual(entry["group"], whole_group(changed))
        self.assertIn({"reason": "index_stale", "count": 1}, result["components"]["sources"]["omissions"])

    def test_25_index_group_never_drops_exception_to_fit(self):
        request, _ = self.index_request()
        core.api.index(self.store, request)
        prep = self.prepare_request()
        prep["features"]["assertion_index"] = True
        full = self.prepare(prep)
        self.assertEqual(self.one_entry(full)["group"], request["groups"][0])
        prep["budgets"]["sources"] = full["components"]["sources"]["utf8_bytes"] - 1
        result = self.prepare(prep)
        self.assertEqual(result["components"]["sources"]["entries"], [])
        self.assertNotIn("Quartz permits", result["model_text"])
        self.assertIn({"reason": "component_budget", "count": 1}, result["components"]["sources"]["omissions"])

    def test_26_validity_is_inclusive_start_exclusive_end(self):
        node = self.add_source("time-constraint", "Valid precisely during the stated interval.", node_type="constraint",
            valid_at=core.NOW, invalid_at="2026-10-19T12:00:01Z")
        for time, expected in [("2026-10-19T11:59:59Z", False), (core.NOW, True),
                               ("2026-10-19T12:00:01Z", False)]:
            with self.subTest(time=time):
                result = self.audit(self.audit_request([node], time))
                entries = json.loads(result["model_text"])["entries"]
                self.assertEqual(bool(entries), expected)
                if expected:
                    self.assertEqual(entries[0]["constraint"]["node_id"], node)
                else:
                    self.assertEqual(result["omissions"], [{"reason": "constraint_unavailable", "count": 1}])

    def test_27_expiry_today_and_malformed_domains_fail_closed(self):
        live = self.add_source("today-constraint", "Allowed on expiry date.", node_type="constraint", extra={"expires": "2026-10-19"})
        expired = self.add_source("past-constraint", "Expired before evaluation date.", node_type="constraint", extra={"expires": "2026-10-18"})
        bad = self.add_source("bad-domains", "Malformed marker must not widen scope.", node_type="constraint")
        # Corrupt only this synthetic fixture; native decoded tags fallback must not hide malformed original domains.
        self.store.conn.execute("UPDATE nodes SET domains = ? WHERE id = ?", ('"codex"', bad))
        self.store.conn.commit()
        result = self.audit(self.audit_request([live, expired, bad]))
        entries = json.loads(result["model_text"])["entries"]
        self.assertEqual([entry["constraint"]["node_id"] for entry in entries], [live])
        self.assertEqual(result["omissions"], [{"reason": "constraint_unavailable", "count": 2}])

    def test_28_client_markers_do_not_cross_scope(self):
        foreign = self.add_source("client-private", "Quartz foreign client details.", node_type="constraint", domains=["client:claude"])
        general = self.add_source("client-general", "Quartz general advisory.", node_type="constraint")
        result = self.audit(self.audit_request([foreign, general]))
        entries = json.loads(result["model_text"])["entries"]
        self.assertEqual([entry["constraint"]["node_id"] for entry in entries], [general])
        self.assertNotIn(foreign, canonical(result).decode())

    def test_29_read_transactions_are_preserved(self):
        reader = core.make_store(self.run_root, name="fixture", read_only=True)
        try:
            reader.conn.execute("BEGIN")
            before = self.snapshot()
            result = core.api.prepare(reader, self.prepare_request())
            self.assert_prepared(result, self.prepare_request())
            self.one_entry(result)
            self.assertTrue(reader.conn.in_transaction)
            core.api.audit(reader, self.audit_request([]))
            self.assertTrue(reader.conn.in_transaction)
            self.assertEqual(self.snapshot(), before)
            reader.conn.rollback()
        finally:
            reader.close()

    def test_30_index_and_ack_reject_caller_transactions(self):
        index_request, _ = self.index_request()
        persist_request = self.persist_request()
        core.api.persist(self.store, persist_request)
        prepared = persist_request["prepared"]
        ack_request = {"scope": copy.deepcopy(self.scope), "evaluation_time": core.NOW,
            "operation_id": "ack-transaction", "projection_sha256": prepared["projection_sha256"],
            "payload_sha256": prepared["text_sha256"], "host_receipt": "opaque"}
        for function, request in [(core.api.index, index_request), (core.api.ack, ack_request)]:
            with self.subTest(operation=function.__name__):
                self.store.conn.execute("BEGIN")
                self.assert_error("transaction_conflict", function, self.store, request)
                self.assertTrue(self.store.conn.in_transaction)
                self.store.conn.rollback()

    def test_31_malformed_raw_extra_and_namespaces_are_not_repaired(self):
        request = self.persist_request()
        owner = self.scope["session_id"]
        old_extra = self.store.conn.execute("SELECT extra FROM nodes WHERE id = ?", (owner,)).fetchone()[0]
        for malformed in ("not-json", "[]", '{"project_path":' + json.dumps(self.project) + ',"practical_memory_v1":[]}'):
            with self.subTest(raw=malformed):
                self.store.conn.execute("UPDATE nodes SET extra = ? WHERE id = ?", (malformed, owner))
                self.store.conn.commit()
                self.assert_typed_rejection(core.api.persist, request)
        self.store.conn.execute("UPDATE nodes SET extra = ? WHERE id = ?", (old_extra, owner))
        self.store.conn.commit()
        index_request, _ = self.index_request()
        self.store.conn.execute("UPDATE nodes SET extra = ? WHERE id = ?",
            (json.dumps({"project_path": self.project, "practical_index_v1": []}), self.node))
        self.store.conn.commit()
        self.assert_typed_rejection(core.api.index, index_request)

    def test_32_typed_links_preserve_stored_direction_without_inference(self):
        other = self.add_source("quartz-target", "Quartz supplies the target context.")
        self.store.add_edge(self.node, other, edge_type="depends_on", provenance="synthetic directed assertion", bidirectional=False)
        prep = self.prepare_request()
        prep["features"]["typed_links"] = True
        result = self.prepare(prep)
        links = result["components"]["links"]["entries"]
        self.assertEqual(len(links), 1)
        self.assertEqual(set(links[0]), {"from", "to", "type", "provenance", "direction", "operational_effect"})
        self.assertEqual((links[0]["from"]["node_id"], links[0]["type"], links[0]["to"]["node_id"]),
                         (self.node, "depends_on", other))
        self.assertEqual(links[0]["direction"], "stored_from_to")
        self.assertEqual(links[0]["operational_effect"], "none")
        self.assertEqual(links[0]["provenance"], "synthetic directed assertion")
        prep["features"]["compact_packing"] = True
        compact = self.prepare(prep)
        self.assertEqual(compact["components"]["links"]["entries"], links)

    def test_33_trace_uses_retrieved_constraints_and_whole_source_groups(self):
        selected = self.add_source("quartz-rule", "Quartz reviews require curator approval, except previews.", node_type="directive")
        unrelated = self.add_source("unrelated-rule", "Zebras eat grass.", node_type="constraint")
        prep = self.prepare_request()
        prep["features"]["application_trace"] = True
        result = self.prepare(prep)
        entries = result["components"]["application_trace"]["entries"]
        self.assertEqual([entry["constraint"]["node_id"] for entry in entries], [selected])
        self.assertEqual(entries[0]["questions"], QUESTIONS)
        self.assertEqual(entries[0]["group"], whole_group("Quartz reviews require curator approval, except previews."))
        self.assertNotIn(unrelated, canonical(result).decode())

    def test_34_late_ack_remains_bound_to_old_projection(self):
        first_request = self.persist_request()
        first_receipt = core.api.persist(self.store, first_request)
        self.store.update_node(self.node, content=core.BODY + " Revision two.")
        second_request = self.persist_request(operation_id="persist-second")
        second_receipt = core.api.persist(self.store, second_request)
        old = first_request["prepared"]
        request = {"scope": copy.deepcopy(self.scope), "evaluation_time": core.NOW,
            "operation_id": "late-ack", "projection_sha256": old["projection_sha256"],
            "payload_sha256": old["text_sha256"], "host_receipt": "late-host-observation"}
        bad = copy.deepcopy(request)
        bad["payload_sha256"] = second_request["prepared"]["text_sha256"]
        self.assert_typed_rejection(core.api.ack, bad)
        acknowledged = core.api.ack(self.store, request)
        self.assert_receipt(acknowledged, "ack", request, old["projection_sha256"])
        self.assertEqual(core.api.persist(self.store, first_request), first_receipt)
        self.assertEqual(core.api.persist(self.store, second_request), second_receipt)
        self.assertNotEqual(acknowledged["receipt"]["target_sha256"], second_receipt["receipt"]["target_sha256"])

    def test_35_bounded_operations_reject_without_eviction(self):
        prepared = self.prepare()
        first_request = self.persist_request(prepared, operation_id="operation-00", entry_ids=[])
        first_receipt = None
        for number in range(32):
            request = self.persist_request(prepared, operation_id="operation-%02d" % number, entry_ids=[])
            receipt = core.api.persist(self.store, request)
            if number == 0:
                first_receipt = receipt
        extra = self.persist_request(prepared, operation_id="operation-overflow", entry_ids=[])
        self.assert_error("capacity", core.api.persist, self.store, extra)
        before = self.snapshot()
        self.assertEqual(core.api.persist(self.store, first_request), first_receipt)
        self.assertEqual(self.snapshot(), before)

    def test_36_continuity_duplicates_and_unknown_ids_are_explicit(self):
        request = self.persist_request()
        core.api.persist(self.store, request)
        prep = self.prepare_request(retained_ids=request["entry_ids"])
        prep["features"]["continuity"] = True
        duplicate = self.prepare(prep)
        self.one_entry(duplicate)
        self.assertEqual(duplicate["components"]["continuity"]["entries"], [])
        self.assertEqual(duplicate["components"]["continuity"]["omissions"], [{"reason": "duplicate", "count": 1}])
        prep["retained_ids"] = ["unavailable-private-id"]
        unknown = self.prepare(prep)
        self.assertNotIn("unavailable-private-id", canonical(unknown).decode())
        self.assertEqual(unknown["components"]["continuity"]["omissions"], [{"reason": "retained_unavailable", "count": 1}])

    def test_37_bare_claude_is_topical_and_remains_available_to_codex(self):
        topical = self.add_source("claude-topic", "Quartz discussion of Claude is general evidence.",
                                  node_type="constraint", domains=["claude"])
        result = self.audit(self.audit_request([topical]))
        entries = json.loads(result["model_text"])["entries"]
        self.assertEqual([entry["constraint"]["node_id"] for entry in entries], [topical])
        self.assertEqual(entries[0]["group"], whole_group("Quartz discussion of Claude is general evidence."))
        self.assertEqual(result["omissions"], [])

    def test_38_distinct_index_annotations_survive_identical_span_bytes(self):
        first = annotated_group(core.BODY, [
            ("claim", "Quartz permits café previews."),
            ("actor", "Only the curator may approve."),
            ("exceptions", "Except frozen releases.")], group_id="annotation-a")
        second = copy.deepcopy(first)
        second["group_id"] = "annotation-b"
        second["roles"]["conditions"] = second["roles"]["actor"]
        second["roles"]["actor"] = []
        self.assertEqual(first["spans"], second["spans"])
        self.assertNotEqual(first["roles"], second["roles"])
        request, source = self.index_request(group=first)
        request["groups"] = [first, second]
        receipt = core.api.index(self.store, request)
        self.assert_receipt(receipt, "index", request,
            digest("index", {"source": source, "index_kind": "assertion", "groups": [first, second]}))
        prep = self.prepare_request()
        prep["features"]["assertion_index"] = True
        prepared = self.prepare(prep)
        entries = prepared["components"]["sources"]["entries"]
        self.assertEqual(len(entries), 2, "Span overlap must not erase distinct complete group annotations")
        self.assertEqual([entry["group"] for entry in entries], [first, second])
        self.assertEqual([entry["kind"] for entry in entries], ["assertion", "assertion"])
        self.assertEqual(len({entry["entry_id"] for entry in entries}), 2)
        self.assertNotIn("duplicate", [item["reason"] for item in prepared["components"]["sources"]["omissions"]])
        persisted = self.persist_request(prepared, entry_ids=[entry["entry_id"] for entry in entries])
        core.api.persist(self.store, persisted)
        continued = self.prepare_request(query="unmatchedzzzzz", retained_ids=persisted["entry_ids"])
        continued["features"]["continuity"] = True
        retained = self.prepare(continued)["components"]["continuity"]["entries"]
        self.assertEqual([entry["group"] for entry in retained], [first, second])
        self.assertEqual([entry["kind"] for entry in retained], ["retained", "retained"])


def load_tests(loader, tests, pattern):
    names = [name for name in loader.getTestCaseNames(PracticalMemoryRegressions)
             if int(name.split("_")[1]) >= 20]
    return unittest.TestSuite(PracticalMemoryRegressions(name) for name in names)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Avoid rerunning base contracts through unittest subclass discovery.
for _name in core.PracticalMemoryContract.__dict__:
    if _name.startswith("test_") and _name not in PracticalMemoryRegressions.__dict__:
        setattr(PracticalMemoryRegressions, _name, None)
