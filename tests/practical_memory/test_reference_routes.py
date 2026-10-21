"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest
from . import reference_support as s


class LimitRouteContracts(s.Fixture):
    def test_strict_request_fields_bool_limits_invalid_time_and_missing_entry(self):
        for updates in ({"query": "é" * 1025}, {"reference_budget_bytes": True}, {"reference_budget_bytes": 511},
            {"reference_budget_bytes": 16385}, {"optional_limit": 9}, {"optional_limit": True}, {"mode": "generate"},
            {"evaluation_time": "2026-10-20T00:00:00"}, {"actor_id": "fixture-actor"}, {"context": self.shape}):
            with self.subTest(updates=updates):
                self.reject_read("resolve_references", self.resolve_request(**updates))
        self.reject_read("resolve_references", self.resolve_request([], query=""))
        duplicate = self.resolve_request([self.ref(self.old), self.ref(self.old)])
        self.reject_read("resolve_references", duplicate)
        for updates in ({"base_budget_bytes": True}, {"base_budget_bytes": 0}, {"base_budget_bytes": 65537},
            {"base_budget_bytes": 40000, "optional_budget_bytes": 30000}, {"optional_budget_bytes": 32769},
            {"include_history": 1}, {"optional_limit": -1}):
            with self.subTest(updates=updates):
                self.reject_read("expand_references", self.expand_request(**updates))
        req = self.expand_request(); del req["include_history"]
        self.reject_read("expand_references", req)

    def test_reference_budget_accounts_for_entire_canonical_envelope(self):
        self.record()
        full = self.resolve()
        exact = self.resolve(self.resolve_request(reference_budget_bytes=full["utf8_bytes"]))
        self.assertEqual(exact["model_text"], full["model_text"])
        below = self.resolve(self.resolve_request(reference_budget_bytes=full["utf8_bytes"] - 1))
        self.assertEqual(below["entries"], [])
        self.assertEqual(below["assertions"], [])
        self.assertEqual(below["priority_refs"], [])
        self.assertIn("reference_budget", s.J(below["omissions"]))

    def test_direct_outgoing_order_scope_filter_before_limit_and_no_reverse_inference(self):
        reverse = self.add("reverse-only", "A link points toward the entry, not outward.")
        self.store.add_edge(reverse, self.old, edge_type="relates_to", bidirectional=False)
        for i in range(9):
            key = self.add("aaa-hidden-" + str(i), "HIDDEN OPTIONAL", project=self.project + "-foreign")
            self.store.add_edge(self.old, key, edge_type="context_of", bidirectional=False)
        z = self.add("z-context", "A context navigation target.")
        a = self.add("a-dependency", "A declared dependency target, not authority.")
        r = self.add("r-related", "A declared related target.")
        for key, relation in ((r, "relates_to"), (a, "depends_on"), (z, "context_of")):
            self.store.add_edge(self.old, key, edge_type=relation, bidirectional=False)
        none = self.expand(self.expand_request(optional_budget_bytes=16000))
        out = self.expand(self.expand_request(mode="direct", optional_limit=2, optional_budget_bytes=16000))
        self.assertEqual(self.source_ids(out, "optional"), [z, a])
        self.assertEqual(out["base_text"], none["base_text"])
        self.assertNotIn(reverse, out["optional_text"])
        self.assertNotIn("aaa-hidden", s.J(out))
        self.assertNotIn("HIDDEN OPTIONAL", s.J(out))
        for connection in json.loads(out["optional_text"])["connections"]:
            self.assertEqual(connection["status"], "declared_link")
            self.assertIsNone(connection["motif"])
            self.assertEqual(connection["differences"], [])
            self.assertEqual(connection["transfer_checks"], [])

    def test_correction_to_optional_source_closes_before_body_and_never_steals_base(self):
        terminal = self.add("optional-terminal", "Current optional correction evidence.")
        self.record(self.assertion(source=terminal, target=self.new))
        self.store.add_edge(self.old, self.new, edge_type="relates_to", bidirectional=False)
        base = self.expand()
        out = self.expand(self.expand_request(mode="direct", optional_budget_bytes=16000))
        self.assertEqual(out["base_text"], base["base_text"])
        self.assertEqual(self.source_ids(out, "optional"), [terminal])
        limited = self.expand(self.expand_request(mode="direct", optional_budget_bytes=1))
        self.assertEqual(limited["base_text"], base["base_text"])
        self.assertEqual(self.source_ids(limited, "optional"), [])
        self.assertNotIn(self.new, self.source_ids(limited, "optional"))

    def test_accessible_contradiction_node_limit_is_incomplete(self):
        for i in range(33):
            key = self.add("visible-conflict-" + str(i), "Accessible contradictory evidence " + str(i))
            self.store.add_edge(self.old, key, edge_type="contradicts", bidirectional=False)
        captured = self.capture(self.old)
        self.assertEqual(captured["applicability"], "incomplete")
        out = self.resolve()
        self.assertIn("incomplete", self.statuses(out))
        self.assertNotIn("resolved_chain", self.statuses(out))

    def test_native_default_search_is_unchanged_by_opt_in_registry_activity(self):
        before = s.ORIGINAL_SEARCH(self.store, "quartz", top_k=8, ranking="rrf", use_vectors=False,
            expand_graph=False, trusted_only=True, evaluation_time=s.NOW,
            recency_time=s.datetime.fromisoformat(s.NOW.replace("Z", "+00:00")))
        baseline = [(row["id"], row["content"]) for row in before]
        self.record()
        self.resolve(); self.expand()
        after = s.ORIGINAL_SEARCH(self.store, "quartz", top_k=8, ranking="rrf", use_vectors=False,
            expand_graph=False, trusted_only=True, evaluation_time=s.NOW,
            recency_time=s.datetime.fromisoformat(s.NOW.replace("Z", "+00:00")))
        self.assertEqual([(row["id"], row["content"]) for row in after], baseline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
