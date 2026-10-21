"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest
from . import reference_support as s


class ScopeContracts(s.Fixture):
    def test_raw_falsy_or_malformed_domains_fail_closed_but_empty_list_is_valid(self):
        self.assertEqual(self.capture(self.old)["applicability"], "current")
        for raw in ("", "null", "false", "0", "{}", '"codex"', '["client:unknown-tool"]', '[1]'):
            with self.subTest(raw=raw):
                self.store.conn.execute("UPDATE nodes SET domains=? WHERE id=?", (raw, self.old)); self.store.conn.commit()
                reader = self.reader()
                try:
                    self.reject(lambda: s.api.capture_reference(reader, self.old, context=self.context, evaluation_time=s.NOW))
                finally:
                    reader.close()
        self.store.conn.execute("UPDATE nodes SET domains='[]' WHERE id=?", (self.old,)); self.store.conn.commit()
        self.assertEqual(self.capture(self.old)["applicability"], "current")

    def test_client_markers_native_semantics_and_unscoped_only(self):
        self.store.update_node(self.old, domains=["claude"])
        self.assertEqual(self.capture(self.old)["applicability"], "current")
        self.store.update_node(self.old, domains=["client:claude"])
        reader = self.reader()
        try:
            self.reject(lambda: s.api.capture_reference(reader, self.old, context=self.context, evaluation_time=s.NOW))
        finally:
            reader.close()
        claude = s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session, client="claude"))
        self.assertEqual(self.capture(self.old, context=claude)["applicability"], "current")
        unscoped = s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session, client="unscoped_only"))
        reader = self.reader()
        try:
            self.reject(lambda: s.api.capture_reference(reader, self.old, context=unscoped, evaluation_time=s.NOW))
        finally:
            reader.close()
        self.store.update_node(self.old, domains=[])
        self.assertEqual(self.capture(self.old, context=unscoped)["applicability"], "current")

    def test_raw_extra_missing_invalid_and_foreign_entry_diagnostics_do_not_leak(self):
        original = self.ref(self.old)
        for raw in ("", "null", "false", "0", "[]", "{}", '{"project_path":"relative"}'):
            with self.subTest(raw=raw):
                self.store.conn.execute("UPDATE nodes SET extra=? WHERE id=?", (raw, self.old)); self.store.conn.commit()
                out = self.resolve(self.resolve_request([original]))
                self.assertEqual(out["entries"], [])
                self.assertTrue(out["omissions"])
                self.assertNotIn(original["node_id"], s.J(out))
                self.assertNotIn(original["body_sha256"], s.J(out))
        self.store.update_node(self.old, extra={"project_path": self.project})
        hidden = self.add("foreign-entry-opaque", "UNAVAILABLE PRIVATE BODY", project=self.project + "-outside")
        out = self.resolve(self.resolve_request([self.ref(hidden)]))
        self.assertNotIn(hidden, s.J(out)); self.assertNotIn("UNAVAILABLE PRIVATE BODY", s.J(out))

    def test_graph_profile_owner_and_config_project_mismatch_never_stamp_or_guess(self):
        for key, value in (("graph_id", "00000000-0000-0000-0000-000000000001"), ("profile", "other-profile"),
                           ("session_id", "missing-owner"), ("current_project", self.project + "-outside")):
            shape = s.context_shape(value if key == "graph_id" else self.graph,
                value if key == "current_project" else self.project, value if key == "session_id" else self.session,
                profile=value if key == "profile" else "")
            with self.subTest(field=key):
                self.reject_read("resolve_references", self.resolve_request(), context=s.api.ReadContext(**shape))
        self.store.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('kin_profile','foreign-profile')")
        self.store.conn.commit()
        self.reject_read("resolve_references", self.resolve_request())
        self.store.conn.execute("DELETE FROM meta WHERE key='kin_profile'"); self.store.conn.commit()
        self.config._project_path = self.root / "different-selected-project"
        self.reject_read("resolve_references", self.resolve_request())
        self.config._project_path = None
        self.store.conn.execute("DELETE FROM meta WHERE key='graph_id'"); self.store.conn.commit()
        self.reject_read("resolve_references", self.resolve_request())
        self.assertIsNone(self.store.conn.execute("SELECT value FROM meta WHERE key='graph_id'").fetchone())

    def test_personal_and_repo_partitions_are_explicit_host_grants(self):
        personal_project = str(self.root / "personal-space")
        private = self.add("private-note", "Private personal evidence.", project=personal_project)
        partitions = copy.deepcopy(self.shape["partitions"]) + [{"partition_id": "personal", "kind": "personal", "project_path": personal_project, "kinbase": None}]
        broad = s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session, partitions=partitions))
        result = self.capture(private, context=broad)
        self.assertEqual(result["reference"], self.ref(private, "personal"))
        narrower = self.resolve(self.resolve_request([self.ref(private, "personal")]))
        self.assertEqual(narrower["entries"], [])
        self.assertNotIn(private, s.J(narrower))
        duplicate = copy.deepcopy(partitions); duplicate.append({"partition_id": "alias", "kind": "repo", "project_path": personal_project, "kinbase": None})
        self.reject(lambda: s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session, partitions=duplicate)))

    def imported(self, *, synthetic=False, key="shared-evidence"):
        kind = "synthetic_fixture" if synthetic else "native_import"
        partition = {"partition_id": "shared", "kind": "kinbase", "project_path": None,
            "kinbase": {"repo": "fixture-local-origin", "scope": "fixture-scope", "store_kind": "codebase", "receipt_kind": kind}}
        kinbase = {"repo": "fixture-local-origin", "scope": "fixture-scope", "store_kind": "codebase",
            "source_identity": "original-document-identity", "source_document_redacted": False,
            "signature_verified": not synthetic, "governance_verified": not synthetic,
            "mode": "synthetic_fixture" if synthetic else "reduced", "status": "current",
            "trust": "synthetic_evidence" if synthetic else "trusted"}
        extra = {"kinbase": kinbase}
        if synthetic:
            extra["v8_source_binding"] = {"receipt_kind": "synthetic_fixture", "source_bank_sha256": s.H("independent-invented-fixture"),
                "owner": "imaginary-owner", "episode": "invented-episode"}
        else:
            kinbase["reduction"] = {"state": "current", "projection_state": "visible"}
        self.add(key, "Shared imported evidence, not operational permission.", audience="team", prov_source="kinbase:fixture-local-origin")
        self.store.update_node(key, extra=extra)
        context = s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session,
            partitions=copy.deepcopy(self.shape["partitions"]) + [partition]))
        return key, context

    def test_qualified_native_import_is_evidence_only_and_not_new_operational_authority(self):
        key, context = self.imported()
        result = self.capture(key, context=context)
        self.assertEqual(result["applicability"], "evidence_only")
        self.assertEqual(result["reference"], self.ref(key, "shared"))
        self.assertEqual(result["metadata"]["extra"]["kinbase"], self.store.peek_node(key)["extra"]["kinbase"])
        out = self.expand(self.expand_request([self.ref(key, "shared")]), context=context)
        self.assertEqual(self.source_ids(out), [key])
        self.assertEqual(json.loads(out["base_text"])["sources"][0]["applicability"], "evidence_only")
        assertion = self.assertion(source=key); assertion["from_ref"] = self.ref(key, "shared")
        self.reject(lambda: s.api.record_correction(self.store, assertion, context=self.write_context(shape=context.to_dict())))
        partitions = copy.deepcopy(context.to_dict()["partitions"])
        duplicate = copy.deepcopy(partitions[-1]); duplicate["partition_id"] = "origin-alias"
        duplicate["kinbase"]["receipt_kind"] = "synthetic_fixture"
        partitions.append(duplicate)
        self.reject(lambda: s.api.ReadContext(**s.context_shape(self.graph, self.project, self.session, partitions=partitions)))

    def test_synthetic_import_preserves_receipt_without_fabricated_reducer_or_signature(self):
        key, context = self.imported(synthetic=True)
        captured = self.capture(key, context=context)
        self.assertEqual(captured["applicability"], "evidence_only")
        self.assertFalse(captured["metadata"]["extra"]["kinbase"]["signature_verified"])
        self.assertFalse(captured["metadata"]["extra"]["kinbase"]["governance_verified"])
        self.assertNotIn("reduction", captured["metadata"]["extra"]["kinbase"])
        original = self.store.peek_node(key)["extra"]
        changed = copy.deepcopy(original); changed["kinbase"]["reduction"] = {"state": "current", "projection_state": "visible"}
        self.store.update_node(key, extra=changed)
        # Invalid evidence-tier qualifications may become historical, never current/evidence-only.
        reader = self.reader()
        try:
            try:
                out = s.api.capture_reference(reader, key, context=context, evaluation_time=s.NOW)
            except s.api.ReferenceError:
                pass
            else:
                self.assertEqual(out["applicability"], "historical")
        finally:
            reader.close()

    def test_import_origin_or_audience_mismatch_does_not_fall_back_to_project_grant(self):
        key, context = self.imported()
        for field, value in (("repo", "ungranted-origin"), ("signature_verified", False), ("source_document_redacted", True)):
            extra = copy.deepcopy(self.store.peek_node(key)["extra"])
            extra["kinbase"][field] = value
            extra["project_path"] = self.project
            self.store.update_node(key, extra=extra)
            reader = self.reader()
            try:
                self.reject(lambda: s.api.capture_reference(reader, key, context=context, evaluation_time=s.NOW))
            finally:
                reader.close()
            # Rebuild the known explicit fixture for the next independent variant.
            self.store.delete_node(key); key, context = self.imported()
        self.store.update_node(key, audience="private")
        reader = self.reader()
        try:
            self.reject(lambda: s.api.capture_reference(reader, key, context=context, evaluation_time=s.NOW))
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
