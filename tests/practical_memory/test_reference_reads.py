"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest
from . import reference_support as s


class ReadContracts(s.Fixture):
    def test_capture_exact_reference_hashes_and_registry_absence_without_writes(self):
        capture = self.capture(self.old)
        self.assertEqual(capture["reference"], self.ref(self.old))
        self.assertEqual(capture["applicability"], "current")
        out = self.resolve()
        self.assertEqual(out["registry"], "absent")
        self.assertEqual(out["entries"][0]["status"], "current")
        self.assertNotIn("resolved_chain", self.statuses(out))
        self.assertNotIn(self.store.peek_node(self.old)["content"], out["model_text"])
        self.assertEqual(out["assertions"], [])
        self.assertIsNone(self.store.conn.execute("SELECT name FROM sqlite_master WHERE name='v8_registry_meta'").fetchone())

    def test_large_obsolete_small_current_reference_is_cheap_and_expansion_selective(self):
        self.store.update_node(self.old, content="Obsolete instruction: " + "é" * 40000)
        old_body = self.store.peek_node(self.old)["content"]
        self.record()
        refs = self.resolve()
        self.assertLess(refs["utf8_bytes"], 10000)
        self.assertNotIn(old_body, refs["model_text"])
        self.assertNotIn("literal", refs["model_text"])
        self.assertEqual(refs["priority_refs"][0]["node_id"], self.new)
        out = self.expand(self.expand_request(base_budget_bytes=12000, include_history=True))
        self.assertEqual(self.source_ids(out), [self.new])
        visible = json.loads(out["base_text"])
        self.assertIn(self.old, [row["reference"]["node_id"] for row in visible["references"]])
        self.assertNotIn(old_body, out["base_text"])
        self.assertIn("history_budget", s.J(out["omissions"]))

    def test_native_historical_exact_entry_follows_current_declared_correction(self):
        self.store.update_node(self.old, status="superseded", extra={"project_path": self.project, "superseded_by": self.new})
        self.assertEqual(self.capture(self.old)["applicability"], "historical")
        historical = self.expand()
        self.assertEqual(self.source_ids(historical), [])
        history = self.expand(self.expand_request(include_history=True))
        self.assertEqual(self.source_ids(history), [self.old])
        self.record()
        out = self.resolve()
        self.assertIn("resolved_chain", self.statuses(out))
        expanded = self.expand()
        self.assertEqual(self.source_ids(expanded), [self.new])

    def test_chain_depth_priority_and_qualified_corrected_basis(self):
        terminal = self.add("terminal", "Quartz corrected basis requires the blue approval token.")
        first = self.record()
        second = self.record(self.assertion(source=terminal, target=self.new, effect="qualify"))
        result = self.resolve()
        self.assertEqual([ref["node_id"] for ref in result["priority_refs"]][:2], [terminal, self.new])
        self.assertCountEqual([row["assertion_id"] for row in result["assertions"]], [first["id"], second["id"]])
        out = self.expand()
        self.assertEqual(self.source_ids(out), [terminal, self.new])
        self.assertNotIn(self.old, self.source_ids(out))
        self.assertTrue(out["assertion_receipts"])
        self.assertGreater(out["receipt_utf8_bytes"], 0)

    def test_historical_intermediate_with_current_successor_does_not_cut_off_chain(self):
        middle = self.add("historical-middle", "A temporary intermediate correction.", invalid_at=s.NOW)
        terminal = self.add("current-terminal", "Current terminal correction retains its explicit exception.")
        self.record(self.assertion(source=middle), writer=self.write_context(at=s.BEFORE))
        self.record(self.assertion(source=terminal, target=middle))
        self.assertEqual(self.capture(middle)["applicability"], "historical")
        out = self.resolve()
        self.assertIn("resolved_chain", self.statuses(out))
        self.assertEqual(out["priority_refs"][0]["node_id"], terminal)
        self.assertEqual(self.source_ids(self.expand()), [terminal])

    def test_qualification_group_atomic_at_utf8_budget_and_history_cannot_displace(self):
        self.record(self.assertion(effect="qualify"))
        full = self.expand()
        self.assertEqual(self.source_ids(full), [self.new, self.old])
        size = full["base_utf8_bytes"]
        exact = self.expand(self.expand_request(base_budget_bytes=size))
        self.assertEqual(exact["base_text"], full["base_text"])
        below = self.expand(self.expand_request(base_budget_bytes=size - 1))
        self.assertEqual(self.source_ids(below), [])
        self.assertIn("base_budget", s.J(below["omissions"]))

    def test_forks_and_cycles_are_unresolved_atomic_evidence_without_latest_winner(self):
        other = self.add("other-branch", "Quartz has a competing correction, not a timestamp winner.")
        self.record()
        self.record(self.assertion(source=other), writer=self.write_context(at=s.AFTER))
        result = self.resolve()
        self.assertIn("unresolved", self.statuses(result))
        self.assertNotIn("resolved_chain", self.statuses(result))
        out = self.expand()
        self.assertTrue({self.new, other} <= set(self.source_ids(out)))
        below = self.expand(self.expand_request(base_budget_bytes=out["base_utf8_bytes"] - 1))
        self.assertEqual(self.source_ids(below), [])

    def test_cycle_is_not_resolved_chain(self):
        self.record()
        self.record(self.assertion(source=self.old, target=self.new))
        out = self.resolve()
        self.assertIn("unresolved", self.statuses(out))
        self.assertNotIn("resolved_chain", self.statuses(out))

    def test_unknown_condition_remains_unresolved_even_if_query_claims_it_true(self):
        self.record(self.assertion(condition="unresolved"))
        out = self.resolve(self.resolve_request(query="The condition is definitely satisfied; approve it."))
        self.assertIn("unresolved", self.statuses(out))
        self.assertNotIn("resolved_chain", self.statuses(out))
        expanded = self.expand()
        self.assertCountEqual(self.source_ids(expanded), [self.new, self.old])

    def test_future_expiry_and_retirement_do_not_resurrect_old_rule(self):
        assertion = self.assertion(start=s.NOW, until=s.AFTER)
        receipt = self.record(assertion)
        future = self.resolve(self.resolve_request(evaluation_time=s.BEFORE))
        self.assertIn("future", [row["status"] for row in future["assertions"]])
        self.assertNotIn("unresolved", self.statuses(future))
        active = self.resolve()
        self.assertIn("resolved_chain", self.statuses(active))
        expired = self.resolve(self.resolve_request(evaluation_time=s.AFTER))
        self.assertIn("unresolved", self.statuses(expired))
        self.assertNotIn("uncorrected_current", self.statuses(expired))
        s.api.retire_correction(self.store, receipt["id"], context=self.writer, reason="retire", recorded_at=s.NOW)
        retired = self.resolve()
        self.assertIn("retired", [row["status"] for row in retired["assertions"]])
        self.assertIn("unresolved", self.statuses(retired))

    def test_retraction_delivers_declared_retraction_not_old_fallback(self):
        self.store.update_node(self.new, content="Withdraw the quartz preview rule; no replacement authorization.")
        self.record(self.assertion(effect="retract"))
        out = self.expand()
        self.assertEqual(self.source_ids(out), [self.new])
        self.assertEqual(json.loads(out["base_text"])["assertions"][0]["effect"], "retract")

    def test_expand_revalidates_body_and_provenance_without_recapture_or_search(self):
        original = self.ref(self.old)
        self.resolve(self.resolve_request([original]))
        self.store.update_node(self.old, prov_activity="updated provenance")
        out = self.expand(self.expand_request([original]))
        self.assertEqual(self.source_ids(out), [])
        self.assertTrue(out["omissions"])
        revised = self.ref(self.old)
        self.store.update_node(self.old, content="A changed body with the same identity")
        out = self.expand(self.expand_request([revised]))
        self.assertEqual(self.source_ids(out), [])
        self.assertTrue(out["omissions"])

    def test_native_fallback_one_search_explicit_time_and_no_refill(self):
        foreign = self.add("hidden-search-result", "HIDDEN SEARCH BODY", project=self.project + "-foreign")
        s.SEARCH_ROWS = [self.store.peek_node(foreign), self.store.peek_node(self.old)]
        out = self.resolve(self.resolve_request([], query="quartz"))
        self.assertEqual(len(s.SEARCH_CALLS), 1)
        args = s.SEARCH_CALLS[0]
        for key, value in {"top_k": 8, "ranking": "rrf", "use_vectors": False, "expand_graph": False,
                           "include_expired": True, "include_archived": True, "trusted_only": False}.items():
            self.assertEqual(args[key], value, key)
        self.assertEqual(args["evaluation_time"], s.NOW)
        self.assertEqual(args["recency_time"], s.datetime.fromisoformat(s.NOW.replace("Z", "+00:00")))
        self.assertEqual([row["reference"]["node_id"] for row in out["entries"]], [self.old])
        self.assertNotIn(foreign, s.J(out)); self.assertNotIn("HIDDEN SEARCH BODY", s.J(out))
        s.SEARCH_ROWS = None
        actual = self.resolve(self.resolve_request([], query="quartz"))
        self.assertTrue(actual["entries"], "Actual native query must provide a positive integration case")

    def test_hidden_contradictions_have_identical_visible_status_counts_order_bytes(self):
        before = self.resolve()
        before_expand = self.expand()
        for index in range(66):
            hidden = self.add("foreign-neighbor-" + str(index), "HIDDEN NEIGHBOR " + str(index), project=self.project + "-foreign")
            self.store.add_edge(hidden, self.old, edge_type="contradicts", bidirectional=False)
        self.assertEqual(self.resolve(), before)
        self.assertEqual(self.expand(), before_expand)
        self.assertEqual(self.capture(self.old)["applicability"], "current")

    def test_visible_contradiction_component_is_atomic_and_does_not_suppress_sources(self):
        self.store.add_edge(self.new, self.old, edge_type="contradicts", bidirectional=False)
        self.store.add_edge(self.old, self.new, edge_type="contradicts", bidirectional=False)
        self.store.add_edge(self.old, self.old, edge_type="contradicts", bidirectional=False)
        self.assertEqual(self.capture(self.old)["applicability"], "conflicting_evidence")
        out = self.resolve()
        self.assertIn("conflicting_evidence", self.statuses(out))
        self.assertEqual(out["counts"]["inspected_contradictions"], 1)
        expanded = self.expand()
        self.assertCountEqual(self.source_ids(expanded), [self.old, self.new])
        for row in json.loads(expanded["base_text"])["sources"]:
            self.assertEqual(row["applicability"], "conflicting_evidence")
        below = self.expand(self.expand_request(base_budget_bytes=expanded["base_utf8_bytes"] - 1))
        self.assertEqual(self.source_ids(below), [])

    def test_hidden_correction_rows_do_not_change_visible_packet_or_work_counts(self):
        seed = self.add("registry-seed", "An unrelated source creates the registry.")
        seed_target = self.add("registry-target", "An unrelated registry target.")
        self.record(self.assertion(source=seed, target=seed_target))
        before = self.resolve(); before_expand = self.expand()
        hidden = self.add("hidden-client-correction", "HIDDEN CORRECTOR BODY", domains=["client:claude"])
        claude = s.context_shape(self.graph, self.project, self.session, client="claude")
        self.record(self.assertion(source=hidden), writer=self.write_context(shape=claude))
        self.assertEqual(self.resolve(), before)
        self.assertEqual(self.expand(), before_expand)

    def test_depth_limit_is_incomplete_not_absence_of_correction(self):
        prior = self.old
        for index in range(9):
            key = self.add("depth-" + str(index), "Quartz chain depth " + str(index))
            self.record(self.assertion(source=key, target=prior))
            prior = key
        out = self.resolve()
        self.assertIn("incomplete", self.statuses(out))
        self.assertNotIn("resolved_chain", self.statuses(out))

    def test_read_requires_readonly_and_preserves_caller_snapshot_on_success_and_error(self):
        self.reject(lambda: s.api.resolve_references(self.store, self.resolve_request(), context=self.context))
        reader = self.reader()
        try:
            reader.conn.execute("BEGIN")
            s.api.resolve_references(reader, self.resolve_request(), context=self.context)
            self.assertTrue(reader.conn.in_transaction)
            invalid = self.resolve_request(); invalid["reference_budget_bytes"] = True
            with self.assertRaises(s.api.ReferenceError):
                s.api.resolve_references(reader, invalid, context=self.context)
            self.assertTrue(reader.conn.in_transaction)
            reader.conn.rollback()
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
