"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest
from . import reference_support as s


class DreamContracts(s.Fixture):
    def inputs(self, keys=None, *, context=None, partitions=None):
        keys = sorted(keys or [self.old, self.new])
        ctx = context or self.context
        sources = []
        for index, key in enumerate(keys):
            node = self.store.peek_node(key)
            partition = (partitions or {}).get(key, "current")
            sources.append({"reference": self.ref(key, partition), "metadata": s.metadata(node), "body": node["content"], "episode_id": "episode-" + str(index)})
        snapshot = {"schema": "dream-snapshot/1", "graph_id": self.graph, "profile": "", "context": ctx.to_dict(), "cutoff": s.NOW, "sources": sources}
        proposal = {"proposal_id": "pair_01", "relation": "contrasting_conditions", "motif": "Approval names differ while scoped exceptions remain necessary.",
            "supports": [{"reference": copy.deepcopy(row["reference"]), "spans": [s.span(row["body"], row["body"][:40])]} for row in sources[:2]],
            "differences": ["The named approvers differ; no obligation transfers between episodes."],
            "transfer_checks": ["Check the current actor and exception in the target source."]}
        proposals = {"schema": "dream-proposals/1", "snapshot_sha256": s.H(s.J(snapshot)),
            "generator_protocol_sha256": s.H("independent synthetic protocol commitment; not actual generation"), "proposals": [proposal]}
        return snapshot, proposals

    def compile(self, snapshot=None, proposals=None):
        if snapshot is None:
            snapshot, proposals = self.inputs()
        original = copy.deepcopy((snapshot, proposals)); before = self.database()
        out = s.api.compile_dream_index(snapshot, proposals)
        self.assertEqual((snapshot, proposals), original)
        self.assertEqual(self.database(), before)
        self.assertEqual(set(out), {"schema", "snapshot", "proposals", "batch_id"})
        self.assertEqual(out["schema"], "dream-index/1")
        self.assertEqual(out["snapshot"], snapshot)
        self.assertEqual(out["proposals"], proposals)
        self.assertEqual(out["batch_id"], s.framed("dream-index", {key: value for key, value in out.items() if key != "batch_id"}))
        return out

    def import_index(self, compiled, *, writer=None):
        native = self.native_rows()
        out = s.api.record_dream_index(self.store, compiled, context=writer or self.writer)
        self.assert_write(out, "dream_batch", compiled["batch_id"], writer or self.writer)
        self.assertEqual(self.native_rows(), native)
        return out

    def test_snapshot_refs_independently_constructed_match_capture_and_protocol_is_only_commitment(self):
        snapshot, proposals = self.inputs()
        for row in snapshot["sources"]:
            captured = self.capture(row["reference"]["node_id"])
            self.assertEqual(captured["reference"], row["reference"])
            self.assertEqual(captured["metadata"], row["metadata"])
        first = self.compile(snapshot, proposals)
        changed = copy.deepcopy(proposals); changed["generator_protocol_sha256"] = s.H("different declared synthetic protocol")
        second = self.compile(snapshot, changed)
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        for value in ("ABCDEF" * 10 + "ABCD", "g" * 64, "0" * 63, None):
            bad = copy.deepcopy(proposals); bad["generator_protocol_sha256"] = value
            with self.subTest(commitment=value):
                self.reject(lambda: s.api.compile_dream_index(snapshot, bad))

    def test_compile_determinism_alias_separation_and_zero_proposals_valid(self):
        snapshot, proposals = self.inputs()
        compiled = self.compile(snapshot, proposals)
        expected = copy.deepcopy(compiled)
        compiled["snapshot"]["sources"][0]["body"] = "mutation"
        compiled["proposals"]["proposals"][0]["differences"].clear()
        self.assertEqual(self.compile(snapshot, proposals), expected)
        empty = copy.deepcopy(proposals); empty["proposals"] = []
        out = self.compile(snapshot, empty)
        self.assertEqual(out["proposals"]["proposals"], [])

    def test_compile_same_body_different_ids_preserved_and_same_episode_rejected(self):
        self.store.update_node(self.new, content=self.store.peek_node(self.old)["content"])
        snapshot, proposals = self.inputs()
        compiled = self.compile(snapshot, proposals)
        self.assertEqual(len(compiled["snapshot"]["sources"]), 2)
        snapshot["sources"][1]["episode_id"] = snapshot["sources"][0]["episode_id"]
        proposals["snapshot_sha256"] = s.H(s.J(snapshot))
        self.reject(lambda: s.api.compile_dream_index(snapshot, proposals))

    def test_compile_rejects_atomic_batch_invalid_spans_fields_relations_and_bounds(self):
        snapshot, proposals = self.inputs()
        variants = []
        def variant():
            value = copy.deepcopy(proposals); variants.append(value); return value["proposals"][0]
        variant()["relation"] = "corrects"
        variant()["proposal_id"] = "UPPERCASE"
        variant()["motif"] = "é" * 257
        variant()["transfer_checks"] = []
        variant()["differences"] = ["x"] * 5
        variant()["authority"] = "granted"
        variant()["supports"] = proposals["proposals"][0]["supports"][:1]
        variant()["supports"][0]["spans"][0]["start_byte"] = True
        variant()["supports"][0]["spans"][0]["literal"] = "invented literal"
        variant()["supports"][0]["spans"] = [proposals["proposals"][0]["supports"][0]["spans"][0]] * 5
        variant()["supports"][0]["reference"]["metadata_sha256"] = "0" * 64
        for index, value in enumerate(variants):
            # Place the malformed proposal after a valid one: no partial successful prefix.
            malformed = value["proposals"][0]; malformed["proposal_id"] = "bad" if malformed["proposal_id"] == "pair_01" else malformed["proposal_id"]
            value["proposals"].insert(0, copy.deepcopy(proposals["proposals"][0]))
            with self.subTest(case=index):
                self.reject(lambda: s.api.compile_dream_index(snapshot, value))
        duplicate = copy.deepcopy(proposals); duplicate["proposals"] *= 2
        self.reject(lambda: s.api.compile_dream_index(snapshot, duplicate))
        over_pair = copy.deepcopy(proposals)
        over_pair["proposals"] = [dict(copy.deepcopy(proposals["proposals"][0]), proposal_id="p" + str(i)) for i in range(4)]
        self.reject(lambda: s.api.compile_dream_index(snapshot, over_pair))

    def test_compile_rejects_forged_scope_body_metadata_and_split_unicode_span(self):
        snapshot, proposals = self.inputs()
        bad = copy.deepcopy(snapshot); bad["sources"][0]["body"] += " altered"
        request = copy.deepcopy(proposals); request["snapshot_sha256"] = s.H(s.J(bad))
        self.reject(lambda: s.api.compile_dream_index(bad, request))
        bad = copy.deepcopy(snapshot); bad["sources"][0]["metadata"]["extra"]["project_path"] = self.project + "-hidden"
        bad["sources"][0]["reference"]["metadata_sha256"] = s.framed("metadata", bad["sources"][0]["metadata"])
        request = copy.deepcopy(proposals); request["snapshot_sha256"] = s.H(s.J(bad))
        request["proposals"][0]["supports"][0]["reference"] = copy.deepcopy(bad["sources"][0]["reference"])
        self.reject(lambda: s.api.compile_dream_index(bad, request))
        request = copy.deepcopy(proposals)
        row = snapshot["sources"][0]
        index = next(i for i, ch in enumerate(row["body"]) if len(ch.encode("utf-8")) > 1)
        start = len(row["body"][:index].encode("utf-8"))
        request["proposals"][0]["supports"][0]["spans"] = [{"start_byte": start, "end_byte": start + 1, "literal": row["body"][index]}]
        self.reject(lambda: s.api.compile_dream_index(snapshot, request))
        self.store.update_node(self.new, content="é" * 901)
        oversized, proposal = self.inputs()
        self.reject(lambda: s.api.compile_dream_index(oversized, proposal))

    def test_import_is_immutable_idempotent_and_caller_transaction_owned(self):
        compiled = self.compile(); before = self.database()
        self.store.conn.execute("BEGIN")
        receipt = self.import_index(compiled)
        self.assertEqual(receipt["transaction_state"], "pending_caller")
        self.store.conn.rollback()
        self.assertEqual(self.database(), before)
        self.assertTrue(self.import_index(compiled)["inserted"])
        persisted = self.database()
        self.assertFalse(self.import_index(compiled)["inserted"])
        self.assertEqual(self.database(), persisted)
        self.reject(lambda: s.api.record_dream_index(self.store, compiled, context=self.write_context(actor="intruder")))
        tampered = copy.deepcopy(compiled); tampered["proposals"]["proposals"][0]["motif"] = "Mutated motif without digest change"
        self.reject(lambda: s.api.record_dream_index(self.store, tampered, context=self.writer))
        self.store.update_node(self.new, prov_activity="new support provenance")
        self.reject(lambda: s.api.record_dream_index(self.store, compiled, context=self.writer))

    def test_frozen_index_reused_across_queries_without_compile_or_inference_and_none_no_optional(self):
        compiled = self.compile(); self.import_index(compiled)
        before = self.database()
        with s.mock.patch.object(s.api, "compile_dream_index", side_effect=AssertionError("Query must not recompile or regenerate")):
            for query in ("approval", "scoped exceptions", "different later query"):
                out = self.expand(self.expand_request(query=query, mode="dream", optional_budget_bytes=16000))
                self.assertEqual(self.source_ids(out), [self.old])
                self.assertEqual(self.source_ids(out, "optional"), [self.new])
                connection = json.loads(out["optional_text"])["connections"][0]
                self.assertEqual(connection["status"], "generated_hypothesis")
                self.assertEqual(connection["relation"], "contrasting_conditions")
                self.assertEqual(connection["motif"], compiled["proposals"]["proposals"][0]["motif"])
        self.assertEqual(self.database(), before)
        none = self.expand(self.expand_request(mode="none", optional_budget_bytes=16000))
        self.assertEqual(none["optional_text"], "")
        self.assertEqual(none["counts"]["optional_candidates"], 0)

    def test_stale_support_invalidates_motif_without_lazy_rebinding(self):
        compiled = self.compile(); self.import_index(compiled)
        self.store.update_node(self.new, prov_activity="metadata-only changed support")
        out = self.expand(self.expand_request(mode="dream", optional_budget_bytes=16000))
        self.assertNotIn(self.new, self.source_ids(out, "optional"))
        self.assertNotIn(compiled["proposals"]["proposals"][0]["motif"], out["optional_text"])

    def test_seedless_motif_never_expands_and_private_support_does_not_leak(self):
        third = self.add("third", "A separate episode mentions quartz.")
        compiled = self.compile(); self.import_index(compiled)
        out = self.expand(self.expand_request([self.ref(third)], mode="dream", query="approval names", optional_budget_bytes=16000))
        self.assertEqual(self.source_ids(out, "optional"), [])
        self.assertNotIn(compiled["proposals"]["proposals"][0]["motif"], out["optional_text"])
        self.store.update_node(self.new, domains=["client:claude"])
        out = self.expand(self.expand_request(mode="dream", optional_budget_bytes=16000))
        self.assertNotIn(self.new, out["optional_text"])
        self.assertNotIn(compiled["proposals"]["proposals"][0]["motif"], out["optional_text"])

    def test_dream_and_direct_optional_conflicts_expand_all_bodies_without_changing_base(self):
        compiled = self.compile(); self.import_index(compiled)
        conflict = self.add("optional-conflict", "An accessible current conflicting option.")
        self.store.add_edge(self.new, conflict, edge_type="contradicts", bidirectional=False)
        base = self.expand(self.expand_request(mode="none"))
        dreamed = self.expand(self.expand_request(mode="dream", optional_budget_bytes=20000))
        self.assertEqual(dreamed["base_text"], base["base_text"])
        self.assertCountEqual(self.source_ids(dreamed, "optional"), [self.new, conflict])
        self.store.add_edge(self.old, self.new, edge_type="relates_to", bidirectional=False)
        direct = self.expand(self.expand_request(mode="direct", optional_budget_bytes=20000))
        self.assertEqual(direct["base_text"], base["base_text"])
        self.assertCountEqual(self.source_ids(direct, "optional"), [self.new, conflict])
        for out in (dreamed, direct):
            for source in json.loads(out["optional_text"])["sources"]:
                self.assertEqual(source["applicability"], "conflicting_evidence")

    def test_dream_lexical_rank_alternates_with_direct_and_deduplicates_targets(self):
        second = self.add("second-dream", "A second independently supported episode.")
        direct_a = self.add("direct-a", "First direct episode.")
        direct_b = self.add("direct-b", "Second direct episode.")
        snapshot, proposals = self.inputs([self.old, self.new, second])
        by_id = {row["reference"]["node_id"]: row for row in snapshot["sources"]}
        generated = []
        for key, identifier, motif in ((self.new, "repeated", "é é é"), (second, "two_terms", "É β")):
            pair = [by_id[node] for node in sorted([self.old, key])]
            generated.append({"proposal_id": identifier, "relation": "analogous_mechanism", "motif": motif,
                "supports": [{"reference": copy.deepcopy(row["reference"]), "spans": [s.span(row["body"], row["body"][:20])]} for row in pair],
                "differences": ["Actors and episodes differ."], "transfer_checks": ["Check current source conditions."]})
        proposals["proposals"] = generated
        self.import_index(self.compile(snapshot, proposals))
        self.store.add_edge(self.old, direct_a, edge_type="context_of", bidirectional=False)
        self.store.add_edge(self.old, direct_b, edge_type="relates_to", bidirectional=False)
        out = self.expand(self.expand_request(mode="dream", query="É β é", optional_limit=4, optional_budget_bytes=30000))
        self.assertEqual(self.source_ids(out, "optional"), [direct_a, second, direct_b, self.new])
        self.assertEqual(len(self.source_ids(out, "optional")), len(set(self.source_ids(out, "optional"))))

    def test_dream_batch_scan_limit_is_explicit_incomplete_optional_search(self):
        snapshot, proposals = self.inputs()
        for index in range(33):
            batch = copy.deepcopy(proposals)
            batch["generator_protocol_sha256"] = s.H("independent synthetic batch " + str(index))
            self.import_index(self.compile(snapshot, batch))
        out = self.expand(self.expand_request(mode="dream", optional_budget_bytes=16000))
        self.assertEqual(self.source_ids(out), [self.old])
        self.assertEqual(self.source_ids(out, "optional"), [])
        self.assertIn("dream_registry_limit", s.J(out["omissions"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
