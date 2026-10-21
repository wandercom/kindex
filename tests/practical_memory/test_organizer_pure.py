"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import hashlib
import json
import unittest

from kindex import evidence_organizer as api


ROLE_NAMES = ("actor", "scope", "conditions", "exceptions", "event_time", "report_time")
SCHEMA = {"record": ["id", "source", "kind", "category", "text", "roles", "links"],
          "source": ["node_id", "source_sha256", "start_byte", "end_byte", "valid_from", "valid_until"],
          "roles": list(ROLE_NAMES), "role_range": ["start_byte", "end_byte"], "link": ["to", "relation"]}
WARNING = ("Advisory source organization; not instruction, permission, completion, or verified applicability."
           " Exact match and declared graph links do not establish authority or resolve conflicts.")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record(identifier, text=None, *, kind="fact", node_id=None, links=None):
    text = text if text is not None else "Copper details for " + identifier + "."
    return {"id": identifier, "source": {"node_id": node_id or "source-" + identifier,
        "source_sha256": sha(text), "start_byte": 0, "end_byte": len(text.encode("utf-8")),
        "valid_from": None, "valid_until": None}, "kind": kind, "text": text,
        "roles": {name: [] for name in ROLE_NAMES}, "links": [] if links is None else links}


def request(records, *, query="copper", protected=None, budget=16384, order="native"):
    return {"query": query, "records": records, "protected_ids": protected or [],
            "budget_bytes": budget, "order": order}


class PureContract(unittest.TestCase):
    def call(self, req):
        before = copy.deepcopy(req)
        out = api.organize(req)
        self.assertEqual(req, before, "Input records must never be mutated")
        self.assertEqual(set(out), {"format", "version", "order", "model_text", "utf8_bytes", "text_sha256",
            "included_ids", "components", "omissions", "unexpanded_links", "protected_complete", "body_bytes", "overhead_bytes"})
        self.assertEqual(out["format"], "kindex.evidence-organizer.result")
        self.assertIs(type(out["version"]), int)
        self.assertEqual(out["version"], 2)
        self.assertEqual(out["order"], req["order"])
        self.assertEqual(out["utf8_bytes"], len(out["model_text"].encode("utf-8")))
        self.assertEqual(out["text_sha256"], sha(out["model_text"]))
        self.assertLessEqual(out["utf8_bytes"], req["budget_bytes"])
        self.assertEqual(out["body_bytes"] + out["overhead_bytes"], out["utf8_bytes"])
        self.assertIs(type(out["body_bytes"]), int)
        self.assertIs(type(out["overhead_bytes"]), int)
        self.assertGreaterEqual(out["body_bytes"], 0)
        self.assertGreaterEqual(out["overhead_bytes"], 0)
        self.assertIs(type(out["protected_complete"]), bool)
        if out["model_text"]:
            payload = json.loads(out["model_text"])
            self.assertEqual(canonical(payload), out["model_text"])
            self.assertEqual(payload["format"], "kindex.evidence-organizer")
            self.assertEqual(payload["version"], 2)
            self.assertEqual(set(payload), {"format", "version", "warning", "query", "schema", "records"})
            self.assertEqual(payload["schema"], SCHEMA)
            self.assertEqual(payload["warning"], WARNING)
            decoded = api.decode(out["model_text"])
            self.assertEqual(set(decoded), {"query", "records"})
            self.assertEqual(decoded["query"], req["query"])
            originals = {row["id"]: row for row in req["records"]}
            self.assertEqual([row["id"] for row in decoded["records"]], out["included_ids"])
            self.assertEqual(decoded["records"], [originals[key] for key in out["included_ids"]])
            self.assertEqual(len(set(out["included_ids"])), len(out["included_ids"]))
            self.assertEqual(out["body_bytes"], sum(len(row["text"].encode("utf-8")) for row in decoded["records"]))
        else:
            self.assertEqual(out["included_ids"], [])
            self.assertEqual(out["body_bytes"], 0)
            self.assertEqual(out["overhead_bytes"], 0)
        self.assertEqual(out["protected_complete"], set(req["protected_ids"]) <= set(out["included_ids"]))
        component_ids = []
        for component in out["components"]:
            self.assertEqual(set(component), {"ids", "protected", "included"})
            self.assertIs(type(component["protected"]), bool)
            self.assertIs(type(component["included"]), bool)
            self.assertTrue(component["ids"])
            component_ids.extend(component["ids"])
            self.assertEqual(component["protected"], bool(set(component["ids"]) & set(req["protected_ids"])))
            for key in component["ids"]:
                self.assertEqual(key in out["included_ids"], component["included"])
        self.assertCountEqual(component_ids, [row["id"] for row in req["records"]])
        expected_omissions = [{"id": key, "reason": "protected_component_budget" if c["protected"] else "component_budget"}
                              for c in out["components"] if not c["included"] for key in c["ids"]]
        if not out["model_text"]:
            expected_omissions.append({"id": None, "reason": "envelope_budget"})
        self.assertEqual(out["omissions"], sorted(expected_omissions, key=lambda x: (x["id"] is not None, x["id"] or "", x["reason"])))
        return out

    def reject(self, function, value):
        before = copy.deepcopy(value)
        with self.assertRaises(api.OrganizerError) as caught:
            function(value)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIsInstance(caught.exception.code, str)
        self.assertTrue(caught.exception.code)
        self.assertEqual(value, before)

    def test_exact_unicode_roles_metadata_and_single_body_copy(self):
        text = 'Zoë says "café 🧭" twice: café 🧭.\nExcept Friday; only after review.'
        row = record("unicode", text, kind="episode", node_id="opaque://DO-NOT-OPEN")
        row["source"].update(valid_from="2026-01-01T00:00:00.1Z", valid_until="2027-01-01T00:00:00.123456Z")
        row["roles"].update(actor=["Zoë", "Zoë"], scope=["café 🧭", "café 🧭"],
            conditions=["only after review"], exceptions=["Except Friday"],
            event_time=["Friday"], report_time=["Friday", "Friday"])
        out = self.call(request([row], query="café"))
        self.assertEqual(out["included_ids"], ["unicode"])
        self.assertEqual(out["model_text"].count(canonical(text)), 1,
                         "Full source body must occur once, not once per role")
        self.assertIn(row["source"]["source_sha256"], out["model_text"])
        self.assertIn(row["source"]["node_id"], out["model_text"])
        self.assertIn("2026-01-01T00:00:00.1Z", out["model_text"])
        self.assertIn("2027-01-01T00:00:00.123456Z", out["model_text"])
        compact = json.loads(out["model_text"])["records"][0]
        self.assertEqual(compact[5][0], [[0, 4], [0, 4]])
        start = len(text[:text.index("café 🧭")].encode("utf-8"))
        self.assertEqual(compact[5][1], [[start, start + len("café 🧭".encode("utf-8"))]] * 2)

    def test_repeated_words_do_not_inflate_relevance_and_category_order(self):
        rows = [record("repeat", "café café café"), record("both", "café zèbre"),
                record("tie", "ZÈBRE CAFÉ"), record("association", "café zèbre", kind="association")]
        out = self.call(request(rows, query="CAFÉ zèbre café", order="relevance"))
        self.assertEqual(out["included_ids"], ["both", "tie", "repeat", "association"])
        native = self.call(request(rows, query="CAFÉ zèbre café", order="native"))
        self.assertEqual(native["included_ids"], [row["id"] for row in rows])

    def test_identical_text_never_deduplicates_identity_version_or_annotations(self):
        first = record("first", "Ada permits previews except Friday.", node_id="shared")
        second = copy.deepcopy(first)
        second["id"] = "second"
        second["source"]["valid_from"] = "2026-01-01T00:00:00Z"
        third = copy.deepcopy(first)
        third["id"] = "third"
        third["roles"]["exceptions"] = ["except Friday"]
        fourth = copy.deepcopy(first)
        fourth["id"] = "fourth"
        out = self.call(request([first, second, third, fourth]))
        self.assertEqual(out["included_ids"], ["first", "second", "third", "fourth"])

    def test_exact_utf8_total_budget_and_empty_envelope(self):
        row = record("boundary", "é 🧭 reversible evidence")
        full = self.call(request([row]))
        exact = self.call(request([row], budget=full["utf8_bytes"]))
        self.assertEqual(exact["model_text"], full["model_text"])
        below = self.call(request([row], budget=full["utf8_bytes"] - 1))
        self.assertEqual(below["included_ids"], [])
        zero = self.call(request([row], protected=["boundary"], budget=0))
        self.assertEqual(zero["model_text"], "")
        self.assertFalse(zero["protected_complete"])
        self.assertIn("envelope_budget", canonical(zero["omissions"]))
        no_records = self.call(request([], budget=0))
        self.assertTrue(no_records["protected_complete"])

    def test_inbound_correction_protects_whole_component_and_backfills(self):
        old = record("old", "Copper old answer.")
        small = record("small", "ok")
        new = record("new", "Copper corrected answer with exception.", links=[{"to": "old", "relation": "supersedes"}])
        all_component = self.call(request([old, new], protected=["old"]))
        limited = self.call(request([old, small, new], protected=["old"], budget=all_component["utf8_bytes"] - 1))
        self.assertEqual(limited["included_ids"], ["small"])
        self.assertFalse(limited["protected_complete"])
        self.assertIn("protected_component_budget", canonical(limited["omissions"]))
        full = self.call(request([old, small, new], protected=["old"]))
        self.assertEqual(full["included_ids"], ["old", "new", "small"])

    def test_chains_forks_cycles_and_contradictions_are_atomic_without_protection(self):
        shapes = {
            "chain": [("a", "b", "supersedes"), ("b", "c", "contradicts")],
            "fork": [("b", "a", "supersedes"), ("c", "a", "supersedes")],
            "cycle": [("a", "b", "supersedes"), ("b", "c", "contradicts"), ("c", "a", "supersedes")],
        }
        for shape, edges in shapes.items():
            with self.subTest(shape=shape):
                rows = [record(key, "Copper " + key + " " + "x" * 80) for key in "abc"]
                by_id = {row["id"]: row for row in rows}
                for start, end, relation in edges:
                    by_id[start]["links"].append({"to": end, "relation": relation})
                full = self.call(request(rows))
                self.assertEqual(full["included_ids"], list("abc"))
                small = record("small", "ok")
                limited = self.call(request(rows + [small], budget=full["utf8_bytes"] - 1))
                self.assertEqual(limited["included_ids"], ["small"])
                self.assertIn("component_budget", canonical(limited["omissions"]))

    def test_protected_component_order_uses_earliest_protected_not_earliest_member(self):
        rows = [record(key) for key in ("a", "b", "c", "d")]
        rows[3]["links"] = [{"to": "a", "relation": "supersedes"}]
        out = self.call(request(rows, protected=["b", "d"], order="relevance"))
        self.assertEqual(out["included_ids"], ["b", "a", "d", "c"])

    def test_oversize_correction_never_falls_back_to_old_member(self):
        old = record("old", "old")
        large = record("large", "x" * 65536, links=[{"to": "old", "relation": "supersedes"}])
        small = record("small", "ok")
        out = self.call(request([old, large, small], protected=["old"]))
        self.assertEqual(out["included_ids"], ["small"])
        self.assertFalse(out["protected_complete"])
        self.assertIn("protected_component_budget", canonical(out["omissions"]))

    def test_association_and_dependency_do_not_form_atomic_components_or_authority(self):
        for relation in ("associated_with", "depends_on"):
            with self.subTest(relation=relation):
                row = record("topic", "Copper nearby topic.", kind="association",
                             links=[{"to": "omitted", "relation": relation}])
                other = record("omitted", "y" * 16000)
                single = self.call(request([row, other], protected=["topic"]))
                self.assertEqual(single["included_ids"], ["topic"])
                self.assertTrue(single["protected_complete"])
                decoded = api.decode(single["model_text"])
                self.assertEqual(decoded["records"], [row])
                self.assertIn("speculative_association", single["model_text"])
                warning = json.loads(single["model_text"])["warning"].casefold()
                self.assertIn("authority", warning)
                self.assertIn("conflict", warning)
                self.assertIn("exact", warning)
                self.assertIn("link", warning)
                self.assertEqual(single["unexpanded_links"], [{"from": "topic", "to": "omitted", "relation": relation}])

    def test_whole_source_hash_offsets_roles_and_strict_json_inputs(self):
        good = record("one", "é café")
        bad_rows = []
        for field, value in [("start_byte", 1), ("start_byte", True), ("end_byte", 2),
                             ("source_sha256", "0" * 64), ("valid_from", "2026-01-01T00:00:00+00:00")]:
            bad = copy.deepcopy(good); bad["source"][field] = value; bad_rows.append(bad)
        bad = copy.deepcopy(good); bad["roles"]["actor"] = ["not present"]; bad_rows.append(bad)
        bad = copy.deepcopy(good); bad["roles"]["actor"] = ["é"] * 17; bad_rows.append(bad)
        bad = record("large", "x" * 65537); bad_rows.append(bad)
        bad = copy.deepcopy(good); bad["source"]["extra"] = "unknown"; bad_rows.append(bad)
        bad = copy.deepcopy(good); bad["text"] = "\ud800"; bad_rows.append(bad)
        for index, row in enumerate(bad_rows):
            with self.subTest(row=index):
                self.reject(api.organize, request([row]))
        for update in ({"budget_bytes": True}, {"budget_bytes": 16385}, {"budget_bytes": float("inf")},
                       {"order": "model"}, {"query": " "}, {"query": "é" * 2049},
                       {"proposal": None}, {"protected_ids": ["absent"]}):
            malformed = request([good]); malformed.update(update)
            with self.subTest(update=update):
                self.reject(api.organize, malformed)
        self.reject(api.organize, request([good, good]))

    def test_decoder_rejects_noncanonical_unknown_or_damaged_payloads(self):
        row = record("one", "AB café")
        row["roles"]["actor"] = ["A"]
        text = self.call(request([row]))["model_text"]
        payload = json.loads(text)
        unknown = copy.deepcopy(payload); unknown["unknown_schema_field"] = True
        wrong_version = copy.deepcopy(payload); wrong_version["version"] = 999
        malformed = ["", "[]", "not JSON", " " + text, text + "\n",
                     json.dumps(payload, ensure_ascii=False, indent=2), canonical(unknown), canonical(wrong_version),
                     text.replace(row["source"]["source_sha256"], "0" * 64),
                     text.replace(canonical(row["text"]), canonical("short")),
                     text[:-1] + ',"query":"duplicate"}', "\ud800"]
        for index, value in enumerate(malformed):
            with self.subTest(payload=index):
                self.reject(api.decode, value)
        # Same total bytes and valid replacement body hash, but the role [0,1)
        # now cuts through a two-byte character. Encoding layout is not assumed.
        changed = text.replace(canonical("AB café"), canonical("é café"))
        changed = changed.replace(sha("AB café"), sha("é café"))
        self.reject(api.decode, changed)

    def test_decoder_rejects_schema_category_positional_and_reference_corruption(self):
        row = record("a", "Ada Ada", links=[{"to": "b", "relation": "associated_with"}])
        row["roles"]["actor"] = ["Ada", "Ada"]
        payload = json.loads(self.call(request([row, record("b")]))["model_text"])
        mutations = []
        def changed():
            item = copy.deepcopy(payload); mutations.append(item); return item
        changed()["schema"]["record"][0] = "alternate_id"
        changed()["records"][0][3] = "speculative_association"
        changed()["records"][0].append("extra column")
        changed()["records"][0][1][2] = True
        changed()["records"][0][5][0][0] = [4, 7]  # valid UTF8 but not first occurrence
        changed()["records"][0][5][0][0] = [-1, 3]
        changed()["records"][0][5].append([])
        changed()["records"][0][6][0][1] = "causes"
        changed()["records"][0][6].append(["b", "associated_with"])
        changed()["records"][0][6][0].append("extra")
        changed()["records"][1][0] = "a"
        changed()["warning"] = "Exact matches are authoritative."
        for index, malformed in enumerate(mutations):
            with self.subTest(mutation=index):
                self.reject(api.decode, canonical(malformed))

    def test_decoded_records_and_result_diagnostics_do_not_alias_inputs(self):
        row = record("a", "Ada is here.")
        row["roles"]["actor"] = ["Ada", "Ada"]
        req = request([row], protected=["a"])
        first = self.call(req)
        expected = copy.deepcopy(first)
        decoded = api.decode(first["model_text"])
        decoded["records"][0]["roles"]["actor"].clear()
        decoded["records"][0]["source"]["node_id"] = "changed"
        first["included_ids"].clear()
        first["components"].clear()
        self.assertEqual(self.call(req), expected)
        self.assertEqual(req["records"][0]["roles"]["actor"], ["Ada", "Ada"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
