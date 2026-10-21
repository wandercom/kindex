"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest

from . import reference_support as s


class SupplementalRegressions(s.Fixture):
    def compiled_fixture(self, shape, sources):
        snapshot = {"schema": "dream-snapshot/1", "graph_id": shape["graph_id"], "profile": shape["profile"],
                    "context": copy.deepcopy(shape), "cutoff": s.NOW, "sources": sorted(sources, key=lambda row: row["reference"]["node_id"])}
        proposals = {"schema": "dream-proposals/1", "snapshot_sha256": s.H(s.J(snapshot)),
                     "generator_protocol_sha256": s.H("independent v2 unopened-write fixture protocol"), "proposals": []}
        return s.api.compile_dream_index(snapshot, proposals)

    def rejected_unopened_writers(self, config, assertion, compiled, writer):
        for kind in ("correction", "retirement", "dream_batch"):
            with self.subTest(write_api=kind):
                selected = s.Store(config.model_copy(deep=True), migrate=False, manage_project_storage=False)
                try:
                    if kind == "correction":
                        self.reject(lambda: s.api.record_correction(selected, assertion, context=writer))
                    elif kind == "retirement":
                        self.reject(lambda: s.api.retire_correction(selected, "0" * 64, context=writer,
                            reason="Rejected unopened fixture action", recorded_at=s.NOW))
                    else:
                        self.reject(lambda: s.api.record_dream_index(selected, compiled, context=writer))
                finally:
                    selected.close()
                yield kind

    def test_01_future_correction_conflict_does_not_replace_current_entry_group(self):
        conflict = self.add("future-conflict-peer", "A conflicting statement about the future correction.")
        correction = self.record(self.assertion(start=s.AFTER))
        self.store.add_edge(self.new, conflict, edge_type="contradicts", bidirectional=False)
        resolved = self.resolve()
        self.assertEqual(resolved["entries"][0]["status"], "current")
        self.assertNotIn("unresolved", self.statuses(resolved))
        self.assertNotIn("conflicting_evidence", self.statuses(resolved))
        descriptor = next(row for row in resolved["assertions"] if row["assertion_id"] == correction["id"])
        self.assertEqual(descriptor["status"], "future")
        expanded = self.expand()
        self.assertEqual(self.source_ids(expanded), [self.old])
        self.assertNotIn(self.store.peek_node(self.new)["content"], expanded["base_text"])
        self.assertNotIn(self.store.peek_node(conflict)["content"], expanded["base_text"])

    def test_02_unknown_qualification_accompanies_already_corrected_basis(self):
        qualifier = self.add("conditional-qualifier", "If the unverified condition holds, qualify the current preview rule.")
        self.record(self.assertion(effect="replace"))
        self.record(self.assertion(source=qualifier, effect="qualify", condition="unresolved"))
        resolved = self.resolve()
        self.assertIn("unresolved", self.statuses(resolved))
        self.assertNotIn("resolved_chain", self.statuses(resolved))
        expanded = self.expand()
        self.assertTrue({self.new, qualifier} <= set(self.source_ids(expanded)),
                        "Unknown qualification requires its source plus the corrected basis, not only the obsolete target")
        rows = {row["reference"]["node_id"]: row for row in json.loads(expanded["base_text"])["sources"]}
        self.assertEqual(rows[self.new]["body"], self.store.peek_node(self.new)["content"])
        self.assertEqual(rows[qualifier]["body"], self.store.peek_node(qualifier)["content"])

    def test_03_contradiction_peer_correction_is_mandatory_evidence(self):
        terminal = self.add("conflict-peer-correction", "The peer statement is corrected by this current source.")
        correction = self.record(self.assertion(source=terminal, target=self.new))
        self.store.add_edge(self.old, self.new, edge_type="contradicts", bidirectional=False)
        expanded = self.expand()
        self.assertTrue({self.old, terminal} <= set(self.source_ids(expanded)),
                        "Correction closure must cover accessible contradiction peers, not only the original entry")
        descriptors = json.loads(expanded["base_text"])["assertions"]
        self.assertIn(correction["id"], [row["assertion_id"] for row in descriptors])
        self.assertIn(self.store.peek_node(terminal)["content"], expanded["base_text"])

    def test_04_correction_of_different_claim_does_not_replace_whole_basis_document(self):
        self.store.update_node(self.old, content="For the explicit x claim, x=1.")
        self.store.update_node(self.new, content="For the explicit x claim, x=2. Separately, y=4.")
        terminal = self.add("different-claim-correction", "For the distinct y claim, y=5.")
        x_assertion = self.assertion(claim="x")
        x_assertion["from_spans"] = [s.span(self.store.peek_node(self.new)["content"], "x=2")]
        x_assertion["to_spans"] = [s.span(self.store.peek_node(self.old)["content"], "x=1")]
        self.record(x_assertion)
        y_assertion = self.assertion(source=terminal, target=self.new, claim="y")
        y_assertion["from_spans"] = [s.span(self.store.peek_node(terminal)["content"], "y=5")]
        y_assertion["to_spans"] = [s.span(self.store.peek_node(self.new)["content"], "y=4")]
        self.record(y_assertion)
        expanded = self.expand(self.expand_request(query="What is the current x claim?"))
        self.assertIn(self.new, self.source_ids(expanded),
                      "The y correction cannot stand in for the document carrying x=2")
        self.assertNotEqual(self.source_ids(expanded), [terminal])
        row = next(row for row in json.loads(expanded["base_text"])["sources"] if row["reference"]["node_id"] == self.new)
        self.assertEqual(row["body"], self.store.peek_node(self.new)["content"])
        self.assertIn("x=2", row["body"])

    def test_05_moved_node_ids_do_not_launder_old_private_assertion_payload(self):
        private_old = "PRIVATE-P-TARGET literal is confidential to the original partition."
        private_new = "PRIVATE-P-CORRECTOR literal is confidential to the original partition."
        self.store.update_node(self.old, content=private_old)
        self.store.update_node(self.new, content=private_new)
        assertion = self.assertion(claim="private-claim-key-do-not-disclose")
        assertion["subject_key"] = "private-subject-key-do-not-disclose"
        receipt = self.record(assertion)
        protected_values = [private_old, private_new, assertion["claim_key"], assertion["subject_key"], receipt["id"],
            assertion["from_ref"]["body_sha256"], assertion["to_ref"]["body_sha256"],
            assertion["from_ref"]["metadata_sha256"], assertion["to_ref"]["metadata_sha256"]]
        public_project = str(self.root / "public-project-q")
        self.store.update_node(self.old, content="Public Q source now occupying the old native ID.", extra={"project_path": public_project})
        self.store.update_node(self.new, content="Public Q source now occupying the correcting native ID.", extra={"project_path": public_project})
        self.store.add_node("Public owner", node_id="q-owner", node_type="session", domains=[],
                            extra={"project_path": public_project}, queue_embedding=False)
        partitions = [{"partition_id": "q", "kind": "repo", "project_path": public_project, "kinbase": None}]
        context = s.api.ReadContext(**s.context_shape(self.graph, public_project, "q-owner", partitions=partitions))
        refs = [self.ref(self.old, "q")]
        resolved = self.resolve(self.resolve_request(refs), context=context)
        expanded = self.expand(self.expand_request(refs), context=context)
        self.assertEqual(resolved["entries"][0]["status"], "current")
        self.assertEqual(resolved["assertions"], [])
        self.assertEqual(resolved["counts"]["inspected_corrections"], 0)
        self.assertEqual(self.source_ids(expanded), [self.old])
        for value in protected_values:
            with self.subTest(private_value=value):
                self.assertNotIn(value, s.J(resolved))
                self.assertNotIn(value, s.J(expanded))

    def test_06_accessible_corrupt_canonical_row_is_unresolved_not_uncorrected(self):
        assertion = self.assertion()
        receipt = self.record(assertion)
        wrong_ref = copy.deepcopy(assertion)
        wrong_ref["from_ref"]["body_sha256"] = "CORRUPT-HASH-MUST-NOT-EGRESS"
        wrong_shape = copy.deepcopy(assertion)
        wrong_shape["to_ref"] = {"untrusted_injected_field": "CORRUPT-REF-MUST-NOT-EGRESS"}
        variants = [("{BROKEN-PAYLOAD-MUST-NOT-EGRESS", "BROKEN-PAYLOAD-MUST-NOT-EGRESS"),
                    (s.J(wrong_ref), "CORRUPT-HASH-MUST-NOT-EGRESS"),
                    (s.J(wrong_shape), "CORRUPT-REF-MUST-NOT-EGRESS")]
        for raw, marker in variants:
            with self.subTest(corruption=marker):
                # Contract-declared persisted representation, deliberately damaged
                # only inside this synthetic fixture; no private helper is called.
                self.store.conn.execute("UPDATE v8_corrections SET payload_json=? WHERE assertion_id=?", (raw, receipt["id"]))
                self.store.conn.commit()
                resolved = self.resolve()
                self.assertIn("unresolved", self.statuses(resolved))
                self.assertNotIn("uncorrected_current", self.statuses(resolved))
                self.assertNotIn("resolved_chain", self.statuses(resolved))
                expanded = self.expand()
                self.assertNotIn(marker, s.J(resolved))
                self.assertNotIn(marker, s.J(expanded))
        self.store.conn.execute("UPDATE v8_corrections SET payload_json=? WHERE assertion_id=?", (s.J(assertion), receipt["id"]))
        self.store.conn.commit()

    def test_07_exact_correcting_source_exposes_same_outgoing_assertion_identity(self):
        assertion = self.assertion()
        receipt = self.record(assertion)
        old_view = self.resolve()
        new_view = self.resolve(self.resolve_request([self.ref(self.new)]))
        for view in (old_view, new_view):
            rows = [row for row in view["assertions"] if row["assertion_id"] == receipt["id"]]
            self.assertEqual(len(rows), 1, "Both reference directions expose one canonical assertion identity")
            self.assertEqual(rows[0]["from_ref"], assertion["from_ref"])
            self.assertEqual(rows[0]["to_ref"], assertion["to_ref"])
        rows = self.store.conn.execute("SELECT assertion_id,from_id,to_id FROM v8_corrections").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(receipt["id"], self.new, self.old)])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM edges").fetchone()[0], 0)

    def test_08_rejected_unopened_missing_store_does_not_create_directory_or_database(self):
        config = s.explicit_config(self.root, "v8_regression_missing")
        directory = config.data_path
        self.assertFalse(directory.exists(), "Root must supply a fresh run_root for this missing-store fixture")
        sources = [{"reference": self.ref(key), "metadata": s.metadata(self.store.peek_node(key)),
                    "body": self.store.peek_node(key)["content"], "episode_id": "episode-" + key} for key in (self.old, self.new)]
        compiled = self.compiled_fixture(self.shape, sources)
        for kind in self.rejected_unopened_writers(config, self.assertion(), compiled, self.writer):
            self.assertFalse(directory.exists(), "Rejected unopened " + kind + " must not create a selected database or parent directory")

    def test_09_rejected_unopened_wrong_owner_current_schema_store_is_byte_unchanged(self):
        config = s.explicit_config(self.root, "v8_regression_wrong_owner")
        self.assertFalse(config.data_path.exists(), "Root must supply a fresh run_root for the separate ownership fixture")
        setup = s.Store(config, manage_project_storage=False)
        try:
            setup.add_node("Wrong owner", node_id="selected-owner", node_type="session", domains=[],
                extra={"project_path": str(self.root / "wrong-owner-project")}, queue_embedding=False)
            for key in (self.old, self.new):
                setup.add_node("Selected " + key, self.store.peek_node(key)["content"], node_id=key, domains=[],
                    extra={"project_path": self.project}, queue_embedding=False)
                setup.verify_node(key, verified_by="independent-fixture", prov_method="invented-input", verified_at="2026-10-01T00:00:00Z")
            graph = setup.graph_id
            assertion = self.assertion()
            sources = []
            for field, key in (("from_ref", self.new), ("to_ref", self.old)):
                node = setup.peek_node(key)
                assertion[field] = {"node_id": key, "graph_id": graph, "profile": "", "partition_id": "current",
                    "body_sha256": s.H(node["content"]), "metadata_sha256": s.framed("metadata", s.metadata(node))}
                sources.append({"reference": copy.deepcopy(assertion[field]), "metadata": s.metadata(node),
                                "body": node["content"], "episode_id": "episode-" + key})
            logical_before = tuple(setup.conn.iterdump())
            db_path = setup.db_path
        finally:
            setup.close()
        bytes_before = db_path.read_bytes()
        shape = s.context_shape(graph, self.project, "selected-owner")
        writer = s.api.WriteContext(**shape,
            actor_id="fixture-actor", recorded_at=s.NOW, explicit_action=True)
        compiled = self.compiled_fixture(shape, sources)
        for kind in self.rejected_unopened_writers(config, assertion, compiled, writer):
            self.assertEqual(db_path.read_bytes(), bytes_before, "Rejected unopened " + kind + " must not initialize, migrate or stamp the existing selected DB")
        reader = s.Store(config.model_copy(deep=True), read_only=True, migrate=False, manage_project_storage=False)
        try:
            self.assertEqual(tuple(reader.conn.iterdump()), logical_before)
            self.assertIsNone(reader.conn.execute("SELECT name FROM sqlite_master WHERE name='v8_registry_meta'").fetchone())
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
