"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import unittest
from . import native_support as n


class NativeContract(n.NativeFixture):
    def test_real_native_retrieval_whole_source_read_only_and_full_budget(self):
        out = self.organize()
        self.assertIn(self.node, out["retrieval_ids"])
        records = self.decoded(out)
        self.assertEqual([row["source"]["node_id"] for row in records], [self.node])
        self.assertEqual(records[0]["text"], n.BODY)
        self.assertEqual(records[0]["source"]["source_sha256"], n.sha(n.BODY))
        exact = self.organize(self.request(total_max_bytes=out["organizer"]["utf8_bytes"]))
        self.assertEqual(exact["organizer"]["model_text"], out["organizer"]["model_text"])
        below = self.organize(self.request(total_max_bytes=out["organizer"]["utf8_bytes"] - 1))
        self.assertEqual(below["organizer"]["included_ids"], [])
        self.assertEqual(below["admitted_ids"], [self.node], "Packing omission is distinct from admission")

    def test_rechecks_hostile_hits_scope_client_time_trust_and_deduplicates(self):
        foreign = self.add_source("foreign-secret-id", "FOREIGN BODY DO NOT DISCLOSE", project=self.project + "-other")
        foreign_client = self.add_source("foreign-client-id", "FOREIGN CLIENT BODY", domains=["client:claude"])
        future = self.add_source("future-secret-id", "FUTURE BODY", valid_at="2026-10-20T00:00:00Z")
        ended = self.add_source("ended-secret-id", "ENDED BODY", invalid_at=n.NOW)
        unverified = self.add_source("unverified-secret-id", "UNVERIFIED BODY", verified=False)
        archived = self.add_source("archived-secret-id", "ARCHIVED BODY")
        self.store.update_node(archived, status="archived")
        stale = self.add_source("stale-secret-id", "STALE BODY", extra={"referent_stale": True})
        expired = self.add_source("expired-secret-id", "EXPIRED BODY", extra={"expires": "2026-10-18T00:00:00Z"})
        allowed = self.add_source("bare-claude-topic", "Quartz topic permits previews.", domains=["claude"], valid_at=n.NOW)
        rejected = [foreign, foreign_client, future, ended, unverified, archived, stale, expired]
        self.hits(*rejected, self.node, self.node, allowed)
        out = self.organize()
        self.assertEqual(out["retrieval_ids"], [self.node, allowed])
        self.assertEqual(out["admitted_ids"], [self.node, allowed])
        text = n.canon(out)
        for key in rejected:
            self.assertNotIn(key, text)
            self.assertNotIn(self.store.peek_node(key)["content"], text)
        self.assertTrue(out["omissions"])

    def test_protection_never_fetches_or_refills_missing_pool_member(self):
        missing = self.add_source("protected-existing", "Quartz must not be fetched outside original hits.")
        self.hits(self.node)
        req = self.request(query="quartz protected-existing absent-id", protected_node_ids=[missing, "absent-id"], top_k=1)
        out = self.organize(req)
        self.assertEqual(out["retrieval_ids"], [self.node])
        self.assertEqual(out["admitted_ids"], [self.node])
        self.assertEqual(out["unavailable_protected_ids"], [missing, "absent-id"])
        self.assertFalse(out["protected_complete"])
        self.assertNotIn("must not be fetched", n.canon(out))

    def test_exact_query_id_protection_escapes_regex_and_unicode_boundaries(self):
        key = self.add_source("coil.v7", "Quartz special identifier.")
        self.hits(key)
        for query in ("coilXv7", "xcoil.v7", "coil.v7z", "écoil.v7", "coil.v7é"):
            with self.subTest(query=query):
                self.rejects(self.request(query=query, protected_node_ids=[key]), code="invalid_request")
        out = self.organize(self.request(query="[coil.v7]", protected_node_ids=[key]))
        self.assertTrue(out["protected_complete"])
        self.assertEqual(out["organizer"]["included_ids"], [key])

    def test_type_mapping_ignores_fixture_roles_and_stored_edge_direction_aliases(self):
        keys = []
        expected = []
        for index, (native_type, kind) in enumerate((("constraint", "principle"), ("directive", "principle"),
                ("episode", "episode"), ("checkpoint", "episode"), ("concept", "fact"))):
            key = self.add_source("kind-" + str(index), "Quartz actor Ada.", node_type=native_type,
                extra={"organizer_kind": "association", "roles": {"actor": ["Ada"]}})
            keys.append(key); expected.append(kind)
        self.store.add_edge(keys[0], keys[1], edge_type="associated_with", bidirectional=False)
        self.store.add_edge(keys[0], keys[2], edge_type="depends_on", bidirectional=False)
        self.store.add_edge(keys[0], keys[3], edge_type="relates_to", bidirectional=False)
        self.hits(*keys)
        out = self.organize()
        rows = self.decoded(out)
        self.assertEqual([row["kind"] for row in rows], expected)
        for row in rows:
            self.assertEqual(row["roles"], {name: [] for name in ("actor", "scope", "conditions", "exceptions", "event_time", "report_time")})
        self.assertEqual(rows[0]["links"], [{"to": keys[1], "relation": "associated_with"}, {"to": keys[2], "relation": "depends_on"}])
        self.assertTrue(all(row["links"] == [] for row in rows[1:]))

    def test_inbound_correction_whole_component_before_oversized_body_omission(self):
        large = self.add_source("correction-large", "é" * 32769)
        small = self.add_source("small-independent", "Quartz small.")
        self.store.add_edge(large, self.node, edge_type="supersedes", bidirectional=False)
        self.hits(self.node, large, small)
        out = self.organize(self.request(query="quartz quartz-source", protected_node_ids=[self.node]))
        self.assertEqual(out["retrieval_ids"], [self.node, large, small])
        self.assertEqual(out["admitted_ids"], [small])
        self.assertEqual(out["organizer"]["included_ids"], [small])
        self.assertEqual(out["unavailable_protected_ids"], [self.node])
        self.assertFalse(out["protected_complete"])
        self.assertNotIn(n.BODY, n.canon(out))
        self.assertTrue(out["omissions"])

    def test_empty_correction_and_multihop_component_omit_all_before_formatting(self):
        middle = self.add_source("correction-middle", "Quartz middle correction.")
        empty = self.add_source("correction-empty", "")
        self.store.add_edge(middle, self.node, edge_type="supersedes", bidirectional=False)
        self.store.add_edge(empty, middle, edge_type="supersedes", bidirectional=False)
        self.hits(self.node, middle, empty)
        out = self.organize()
        self.assertEqual(out["retrieval_ids"], [self.node, middle, empty])
        self.assertEqual(out["admitted_ids"], [])
        self.assertEqual(out["organizer"]["included_ids"], [])
        self.assertTrue(out["omissions"])

    def test_admitted_inbound_correction_is_preserved_and_not_partial_under_budget(self):
        new = self.add_source("new-correction", "Quartz correction requires Friday approval.")
        self.store.add_edge(new, self.node, edge_type="supersedes", bidirectional=False)
        self.hits(self.node, new)
        req = self.request(query="quartz quartz-source", protected_node_ids=[self.node])
        full = self.organize(req)
        self.assertEqual(full["admitted_ids"], [self.node, new])
        rows = self.decoded(full)
        self.assertEqual(rows[0]["links"], [])
        self.assertEqual(rows[1]["links"], [{"to": self.node, "relation": "supersedes"}])
        req["total_max_bytes"] = full["organizer"]["utf8_bytes"] - 1
        limited = self.organize(req)
        self.assertEqual(limited["organizer"]["included_ids"], [])
        self.assertFalse(limited["protected_complete"])

    def test_lifecycle_and_native_contradiction_trust_are_not_relaxed(self):
        newer = self.add_source("lifecycle-new", "Quartz new lifecycle source.")
        self.store.update_node(self.node, status="superseded", extra={"project_path": self.project, "superseded_by": newer})
        a = self.add_source("conflict-a", "Quartz option A.")
        b = self.add_source("conflict-b", "Quartz option B.")
        self.store.add_edge(a, b, edge_type="contradicts", bidirectional=False)
        self.hits(self.node, newer, a, b)
        out = self.organize(self.request(query="quartz quartz-source", protected_node_ids=[self.node]))
        self.assertEqual(out["retrieval_ids"], [newer])
        self.assertEqual(out["unavailable_protected_ids"], [self.node])
        self.assertFalse(out["protected_complete"])
        self.assertNotIn("Quartz option", n.canon(out))
        self.assertNotIn(n.BODY, n.canon(out))

    def test_writable_store_and_invalid_native_requests_reject_without_mutation(self):
        self.rejects(self.request(), writable=True)
        for update in ({"top_k": True}, {"top_k": 0}, {"top_k": 33}, {"total_max_bytes": True},
                       {"total_max_bytes": 16385}, {"query": " "}, {"order": "model"},
                       {"protected_node_ids": [self.node, self.node], "query": self.node},
                       {"unknown": None}):
            with self.subTest(update=update):
                self.rejects(self.request(**update), code="invalid_request")
        for field, value in (("graph_id", "foreign-graph"), ("project_path", self.project + "-foreign"),
                             ("client", "unknown-client"), ("session_id", "missing-session")):
            req = self.request(); req["scope"][field] = value
            with self.subTest(scope=field):
                self.rejects(req)

    def test_excess_links_never_silently_remove_corrections(self):
        targets = [self.add_source("target-" + str(index), "Quartz correction " + str(index)) for index in range(17)]
        for target in targets:
            self.store.add_edge(self.node, target, edge_type="supersedes", bidirectional=False)
        self.hits(self.node, *targets)
        before = self.snapshot()
        try:
            out = self.organize(self.request(top_k=32))
        except n.api.PracticalMemoryError as exc:
            self.assertIn(exc.code, {"unavailable", "capacity"})
            self.assertEqual(self.snapshot(), before)
        else:
            self.assertEqual(out["admitted_ids"], [], "Unrenderable 17-link member must not strand any correction peer")
            self.assertEqual(out["organizer"]["included_ids"], [])
            self.assertTrue(out["omissions"])

    def test_oversized_identifier_never_strands_the_old_component_member(self):
        oversized = self.add_source("z" * 129, "Quartz corrective evidence.")
        self.store.add_edge(oversized, self.node, edge_type="supersedes", bidirectional=False)
        self.hits(self.node, oversized)
        before = self.snapshot()
        try:
            out = self.organize()
        except n.api.PracticalMemoryError as exc:
            self.assertIn(exc.code, {"unavailable", "capacity"})
            self.assertEqual(self.snapshot(), before)
        else:
            self.assertEqual(out["admitted_ids"], [])
            self.assertEqual(out["organizer"]["included_ids"], [])
            self.assertTrue(out["omissions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
