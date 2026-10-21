"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
import unittest
from . import reference_support as s


class WriteContracts(s.Fixture):
    def test_context_deep_copy_policy_hash_and_immutable_nested_values(self):
        shape = copy.deepcopy(self.shape)
        ctx = s.api.ReadContext(**shape)
        shape["partitions"][0]["partition_id"] = "mutated"
        shape["correction_policy"]["allowed_actor_ids"].append("intruder")
        self.assertEqual(ctx.to_dict(), self.shape)
        detached = ctx.to_dict(); detached["partitions"].clear()
        self.assertEqual(ctx.to_dict(), self.shape)
        with self.assertRaises((TypeError, AttributeError)):
            ctx.partitions[0]["partition_id"] = "mutated"
        bad = copy.deepcopy(self.shape); bad["correction_policy"]["policy_sha256"] = "0" * 64
        self.reject(lambda: s.api.ReadContext(**bad))
        bad = copy.deepcopy(self.shape); bad["partitions"].append(copy.deepcopy(bad["partitions"][0]))
        self.reject(lambda: s.api.ReadContext(**bad))
        self.reject(lambda: self.write_context(explicit=False))

    def test_canonical_fact_derived_incoming_view_and_replay_original_writer(self):
        assertion = self.assertion()
        receipt = self.record(assertion)
        self.assertTrue(receipt["inserted"])
        self.assertEqual(receipt["transaction_state"], "committed")
        rows = self.store.conn.execute("SELECT * FROM v8_corrections").fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(row["assertion_id"], s.framed("correction", assertion))
        self.assertEqual((row["from_id"], row["to_id"]), (self.new, self.old))
        self.assertEqual(json.loads(row["payload_json"]), assertion)
        writer = json.loads(row["writer_json"])
        self.assertEqual(writer, {"actor_id": "fixture-actor", "recorded_at": s.NOW,
                                 "context_sha256": s.framed("context", self.writer.to_dict())})
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM edges").fetchone()[0], 0)
        result = self.resolve()
        self.assertEqual([a["assertion_id"] for a in result["assertions"]], [receipt["id"]])
        self.assertEqual(result["assertions"][0]["from_ref"], assertion["from_ref"])
        self.assertEqual(result["assertions"][0]["to_ref"], assertion["to_ref"])
        self.assertEqual(result["priority_refs"][0], assertion["from_ref"])
        repeated = self.record(assertion, writer=self.write_context(at=s.AFTER))
        self.assertFalse(repeated["inserted"])
        self.assertEqual(self.store.conn.execute("SELECT writer_json FROM v8_corrections").fetchone()[0], row["writer_json"])
        distinct = copy.deepcopy(assertion); distinct["claim_key"] = "another-explicit-claim"
        self.assertNotEqual(self.record(distinct)["id"], receipt["id"])

    def test_record_caller_transaction_pending_and_rollback_includes_registry_ddl(self):
        assertion = self.assertion()
        before = self.database()
        self.store.conn.execute("BEGIN")
        self.store.conn.execute("INSERT INTO meta(key,value) VALUES ('v8_test_marker','caller')")
        out = self.record(assertion)
        self.assertEqual(out["transaction_state"], "pending_caller")
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        self.assertEqual(self.database(), before)
        self.assertIsNone(self.store.conn.execute("SELECT name FROM sqlite_master WHERE name='v8_corrections'").fetchone())

    def test_rejected_record_preserves_caller_work_and_never_creates_registry(self):
        invalid = self.assertion(); invalid["from_spans"][0]["literal"] = "not the source"
        self.store.conn.execute("BEGIN")
        self.store.conn.execute("INSERT INTO meta(key,value) VALUES ('v8_test_marker','caller')")
        self.reject(lambda: s.api.record_correction(self.store, invalid, context=self.writer))
        self.assertTrue(self.store.conn.in_transaction)
        self.assertEqual(self.store.conn.execute("SELECT value FROM meta WHERE key='v8_test_marker'").fetchone()[0], "caller")
        self.assertIsNone(self.store.conn.execute("SELECT name FROM sqlite_master WHERE name='v8_corrections'").fetchone())
        self.store.conn.commit()

    def test_writer_rechecks_authorization_and_versions_before_idempotent_success(self):
        assertion = self.assertion(); self.record(assertion)
        self.reject(lambda: s.api.record_correction(self.store, assertion, context=self.write_context(actor="intruder")))
        revoked_shape = s.context_shape(self.graph, self.project, self.session, actors=["replacement-actor"])
        self.reject(lambda: s.api.record_correction(self.store, assertion,
                    context=self.write_context(shape=revoked_shape, actor="fixture-actor")))
        self.store.update_node(self.new, prov_activity="changed provenance, same body")
        self.reject(lambda: s.api.record_correction(self.store, assertion, context=self.writer))

    def test_retirement_append_only_replay_unauthorized_stale_and_inaccessible(self):
        assertion = self.assertion(); correction = self.record(assertion)
        out = s.api.retire_correction(self.store, correction["id"], context=self.writer, reason="explicit withdrawal", recorded_at=s.NOW)
        self.assert_write(out, "retirement", correction["id"], self.writer)
        self.assertTrue(out["inserted"])
        original = self.database()
        repeated = s.api.retire_correction(self.store, correction["id"], context=self.writer, reason="explicit withdrawal", recorded_at=s.NOW)
        self.assertFalse(repeated["inserted"])
        self.assertEqual(self.database(), original)
        self.reject(lambda: s.api.retire_correction(self.store, correction["id"], context=self.write_context(actor="intruder"), reason="explicit withdrawal", recorded_at=s.NOW))
        self.reject(lambda: s.api.retire_correction(self.store, correction["id"], context=self.writer, reason="explicit withdrawal", recorded_at=s.AFTER))
        self.store.update_node(self.new, domains=["client:claude"])
        self.reject(lambda: s.api.retire_correction(self.store, correction["id"], context=self.writer, reason="explicit withdrawal", recorded_at=s.NOW))
        self.store.update_node(self.new, domains=[])
        self.store.update_node(self.new, content="Changed source version")
        self.reject(lambda: s.api.retire_correction(self.store, correction["id"], context=self.writer, reason="explicit withdrawal", recorded_at=s.NOW))

    def test_retirement_missing_registry_is_failure_without_ddl(self):
        self.reject(lambda: s.api.retire_correction(self.store, "0" * 64, context=self.writer, reason="missing", recorded_at=s.NOW))
        self.assertIsNone(self.store.conn.execute("SELECT name FROM sqlite_master WHERE name='v8_registry_meta'").fetchone())

    def test_authorized_retirement_allows_unchanged_historical_endpoint_and_preserves_caller_transaction(self):
        self.store.verify_node(self.new, verified_by="independent-fixture", prov_method="invented-input",
            verified_at="2026-10-01T00:00:00Z", invalid_at=s.NOW)
        receipt = self.record(writer=self.write_context(at=s.BEFORE))
        self.assertEqual(self.capture(self.new)["applicability"], "historical")
        before = self.database()
        self.store.conn.execute("BEGIN")
        self.store.conn.execute("INSERT INTO meta(key,value) VALUES ('v8_test_marker','caller')")
        out = s.api.retire_correction(self.store, receipt["id"], context=self.writer, reason="historical retirement", recorded_at=s.NOW)
        self.assertEqual(out["transaction_state"], "pending_caller")
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        self.assertEqual(self.database(), before)

    def test_new_authorized_session_context_keeps_assertion_but_actor_revocation_does_not(self):
        extra_project = str(self.root / "second-project-v8")
        partitions = copy.deepcopy(self.shape["partitions"]) + [{"partition_id": "extra", "kind": "personal", "project_path": extra_project, "kinbase": None}]
        broad = s.context_shape(self.graph, self.project, self.session, partitions=partitions)
        self.record(writer=self.write_context(shape=broad))
        self.store.add_node("Second session", node_id="next-session", node_type="session", domains=[], extra={"project_path": self.project}, queue_embedding=False)
        narrow = s.context_shape(self.graph, self.project, "next-session")
        out = self.resolve(context=s.api.ReadContext(**narrow))
        self.assertIn("resolved_chain", self.statuses(out))
        revoked = s.context_shape(self.graph, self.project, "next-session", actors=["different-actor"])
        out = self.resolve(context=s.api.ReadContext(**revoked))
        self.assertIn("unresolved", self.statuses(out))
        self.assertNotIn("resolved_chain", self.statuses(out))

    def test_strict_assertion_spans_types_and_scope_no_source_authorization(self):
        base = self.assertion()
        variants = []
        def change():
            value = copy.deepcopy(base); variants.append(value); return value
        change()["effect"] = "permission"
        change()["condition"] = "query_says_yes"
        change()["subject_key"] = ""
        change()["actor_id"] = "fixture-actor"
        change()["from_spans"][0]["start_byte"] = True
        change()["from_spans"] = []
        change()["effective_from"] = s.AFTER; variants[-1]["effective_until"] = s.BEFORE
        change()["from_ref"]["metadata_sha256"] = "0" * 64
        change()["from_ref"] = copy.deepcopy(base["to_ref"])
        for index, value in enumerate(variants):
            with self.subTest(case=index):
                self.reject(lambda: s.api.record_correction(self.store, value, context=self.writer))
        project = str(self.root / "other-project-v8")
        remote = self.add("cross-partition", "Explicit correction text cannot grant scope.", project=project)
        partitions = copy.deepcopy(self.shape["partitions"]) + [{"partition_id": "other", "kind": "repo", "project_path": project, "kinbase": None}]
        broad = s.context_shape(self.graph, self.project, self.session, partitions=partitions)
        assertion = self.assertion(source=remote); assertion["from_ref"] = self.ref(remote, "other")
        self.reject(lambda: s.api.record_correction(self.store, assertion, context=self.write_context(shape=broad)))

    def test_registry_version_rejection_does_not_repair_or_write(self):
        self.record()
        self.store.conn.execute("UPDATE v8_registry_meta SET value='999' WHERE key='schema_version'")
        self.store.conn.commit()
        self.reject(lambda: s.api.record_correction(self.store, self.assertion(), context=self.writer))
        self.reject_read("resolve_references", self.resolve_request())


if __name__ == "__main__":
    unittest.main(verbosity=2)
