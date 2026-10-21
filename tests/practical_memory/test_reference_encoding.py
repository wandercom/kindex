"""Synthetic contracts ported from the frozen practical-memory campaign."""
import copy
import json
from pathlib import Path

from . import reference_support as s
from . import privacy_probe


def merge_objects(common, delta):
    """Independent format oracle: only objects merge; all else replaces."""
    out = copy.deepcopy(common)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_objects(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def reconstruct(text):
    if not text:
        return None
    value = json.loads(text)
    if "metadata_encoding" in value:
        assert value.pop("metadata_encoding") == "recursive-common-with-source-delta/1"
        common = value.pop("metadata_common")
        assert isinstance(common, dict)
        for row in value["sources"]:
            row["metadata"] = merge_objects(common, row.pop("metadata_delta"))
    return value


def common_object(objects):
    result = {}
    for key in sorted(set.intersection(*(set(value) for value in objects))):
        values = [value[key] for value in objects]
        if all(isinstance(value, dict) for value in values):
            result[key] = common_object(values)
        elif all(s.J(value) == s.J(values[0]) for value in values[1:]):
            result[key] = copy.deepcopy(values[0])
    return result


def residual(original, common):
    result = {}
    for key, value in original.items():
        if key not in common:
            result[key] = copy.deepcopy(value)
        elif s.J(value) == s.J(common[key]):
            continue
        elif isinstance(value, dict) and isinstance(common[key], dict):
            remaining = residual(value, common[key])
            if remaining:
                result[key] = remaining
        else:
            result[key] = copy.deepcopy(value)
    return result


def factored(full):
    compact = copy.deepcopy(full)
    compact["metadata_encoding"] = "recursive-common-with-source-delta/1"
    common = common_object([row["metadata"] for row in full["sources"]])
    compact["metadata_common"] = common
    for row in compact["sources"]:
        row["metadata_delta"] = residual(row.pop("metadata"), common)
    return compact


class NoStorage:
    def __getattribute__(self, name):
        raise AssertionError("Invalid options touched storage: " + name)


class V9Contract(s.Fixture):
    def snapshot(self):
        main = next(row[2] for row in self.store.conn.execute("PRAGMA database_list") if row[1] == "main")
        return self.database(), Path(main).read_bytes()

    def at(self, key, value):
        # Deliberate synthetic provenance input, not production mutation. The
        # native update_node API does not expose prov_when; change only this column.
        self.store.conn.execute("UPDATE nodes SET prov_when=? WHERE id=?", (value, key))
        self.store.conn.commit()
        return key

    def note(self, key, *, when="2026-10-19T12:00:00Z", **kwargs):
        return self.at(self.add(key, "Invented later note " + key + ": café 雪 🙂.", **kwargs), when)

    def edge(self, source, target, relation="relates_to"):
        self.store.add_edge(source, target, edge_type=relation, bidirectional=False)

    def rich(self, keys, variants=None):
        for index, key in enumerate(keys):
            extra = copy.deepcopy(self.store.peek_node(key)["extra"])
            extra["v8_source_binding"] = {
                "retained_extension": {"unicode": "雪🙂é" * 150, "nested": {"a": [1, True, None, {}], "b": {}}},
                "variant": copy.deepcopy((variants or [{}, {}])[index]),
                "ordinary_nonbank_field": ["preserve", {"no_special_omission": True}],
            }
            self.store.update_node(key, extra=extra)

    def call(self, name, request, *, context=None, **options):
        before, original = self.snapshot(), copy.deepcopy(request)
        s.SEARCH_CALLS.clear()
        reader = self.reader()
        try:
            out = getattr(s.api, name)(reader, request, context=context or self.context, **options)
            self.assertEqual(reader.conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertFalse(reader.conn.in_transaction)
        finally:
            reader.close()
        self.assertEqual(self.snapshot(), before, "Reads must preserve logical rows and main-file bytes")
        self.assertEqual(request, original)
        self.assertEqual(s.SEARCH_CALLS, [], "Exact input refs must not search/refill")
        self.assertEqual(out["context_sha256"], s.framed("context", (context or self.context).to_dict()))
        self.assertEqual(out["evaluation_time"], request["evaluation_time"])
        incoming = options.get("incoming_later", False)
        encoding = options.get("metadata_encoding", "full")
        self.assertEqual(set(out["counts"]), {"visited_nodes", "inspected_corrections", "inspected_contradictions", "optional_candidates"})
        for count in out["counts"].values():
            self.assertIs(type(count), int)
            self.assertGreaterEqual(count, 0)
        if name == "resolve_references":
            keys = {"schema", "evaluation_time", "context_sha256", "registry", "entries", "assertions", "priority_refs",
                    "optional", "omissions", "counts", "model_text", "utf8_bytes", "text_sha256"}
            self.assertEqual(set(out), keys | ({"options"} if incoming else set()))
            self.assertEqual(out["schema"], "reference-result/2" if incoming else "reference-result/1")
            if incoming:
                self.assertEqual(out["options"], {"incoming_later": True})
            else:
                self.assertNotIn("options", out)
            self.assert_payload(out["model_text"], out["utf8_bytes"], out["text_sha256"], request["reference_budget_bytes"])
            for key in ("entries", "assertions", "priority_refs", "optional", "omissions"):
                self.assertEqual(json.loads(out["model_text"])[key], out[key])
        else:
            changed = incoming or encoding != "full"
            keys = {"schema", "evaluation_time", "context_sha256", "registry", "base_text", "optional_text",
                    "base_utf8_bytes", "optional_utf8_bytes", "base_sha256", "optional_sha256", "included_refs", "omissions",
                    "counts", "assertion_receipts", "receipt_utf8_bytes"}
            self.assertEqual(set(out), keys | ({"options"} if changed else set()))
            self.assertEqual(out["schema"], "evidence-result/2" if changed else "evidence-result/1")
            if changed:
                self.assertEqual(out["options"], {"metadata_encoding": encoding, "incoming_later": incoming})
            else:
                self.assertNotIn("options", out)
            for part in ("base", "optional"):
                text = out[part + "_text"]
                self.assert_payload(text, out[part + "_utf8_bytes"], out[part + "_sha256"], request[part + "_budget_bytes"])
                full = reconstruct(text)
                self.assertEqual(s.J(s.api.decode_evidence_text(text)), s.J(full))
                if full is None:
                    continue
                self.assertEqual(set(full), {"warning", "sources", "references", "assertions", "connections"})
                self.assertEqual(full["warning"], s.WARNING)
                expected = s.J(full)
                if encoding == "lossless_common" and full["sources"]:
                    alternative = s.J(factored(full))
                    if len(alternative.encode("utf-8")) < len(expected.encode("utf-8")):
                        expected = alternative
                self.assertEqual(text, expected, "Deterministic whole-envelope choice, full on tie")
                if "metadata_encoding" in json.loads(text):
                    self.assertLess(len(text.encode("utf-8")), len(s.J(full).encode("utf-8")), "Compact ties must select full")
                    self.assertTrue(full["sources"])
                for row in full["sources"]:
                    self.assertEqual(set(row), {"reference", "metadata", "body", "applicability"})
                    node = self.store.peek_node(row["reference"]["node_id"])
                    self.assertEqual(s.J(row["metadata"]), s.J(s.metadata(node)))
                    self.assertEqual(row["body"], node["content"])
                    self.assertEqual(s.H(row["body"]), row["reference"]["body_sha256"])
                    self.assertEqual(s.framed("metadata", row["metadata"]), row["reference"]["metadata_sha256"])
        return out

    def expanded(self, request=None, **options):
        return self.call("expand_references", request or self.expand_request(), **options)

    def incoming(self, **updates):
        return self.expand_request(mode="direct", optional_budget_bytes=24000, **updates)

    def connections(self, result):
        payload = reconstruct(result["optional_text"])
        return [] if payload is None else payload["connections"]

    def test_01_explicit_defaults_preserve_complete_legacy_values(self):
        self.edge(self.old, self.new)
        for mode in ("none", "direct", "dream"):
            with self.subTest(mode=mode):
                request = self.expand_request(mode=mode, optional_budget_bytes=12000)
                implicit = self.expand(request)
                explicit = self.expanded(request, metadata_encoding="full", incoming_later=False)
                self.assertEqual(s.J(explicit), s.J(implicit))
                request = self.resolve_request(mode=mode)
                self.assertEqual(s.J(self.resolve(request)), s.J(self.call("resolve_references", request, incoming_later=False)))

    def test_02_invalid_options_and_mode_combinations_precede_storage(self):
        for value in (None, "FULL", "lossless", True, 1, [], {}):
            with self.subTest(encoding=value), self.assertRaises(s.api.ReferenceError):
                s.api.expand_references(NoStorage(), self.expand_request(), context=self.context, metadata_encoding=value)
        for name, make in (("expand_references", self.expand_request), ("resolve_references", self.resolve_request)):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(api=name, incoming=value), self.assertRaises(s.api.ReferenceError):
                    getattr(s.api, name)(NoStorage(), make(mode="direct"), context=self.context, incoming_later=value)
            for mode in ("none", "dream"):
                with self.subTest(api=name, mode=mode), self.assertRaises(s.api.ReferenceError):
                    getattr(s.api, name)(NoStorage(), make(mode=mode), context=self.context, incoming_later=True)
        with self.assertRaises(s.api.ReferenceError):
            s.api.expand_references(NoStorage(), self.expand_request(mode="dream"), context=self.context, metadata_encoding="lossless_common")

    def test_03_lossless_full_extra_unicode_and_typed_metadata(self):
        variants = [
            {"typed": True, "number": 1.0, "text": "é", "null_or_missing": None, "shape": {}, "nested_empty": {}, "list": [True, 1, None, {"雪": []}]},
            {"typed": 1, "number": 1, "text": "e\u0301", "shape": [], "nested_empty": {"x": {}}, "list": [1, True, None, {"雪": {}}]},
        ]
        self.rich([self.old, self.new], variants)
        req = self.expand_request([self.ref(self.old), self.ref(self.new)])
        full = self.expanded(req)
        compact = self.expanded(req, metadata_encoding="lossless_common")
        self.assertIn("metadata_encoding", json.loads(compact["base_text"]))
        self.assertEqual(s.J(reconstruct(compact["base_text"])), s.J(reconstruct(full["base_text"])))
        self.assertLess(compact["base_utf8_bytes"], full["base_utf8_bytes"])
        self.assertGreater(compact["base_utf8_bytes"], len(compact["base_text"]))

    def test_04_nonbeneficial_or_source_free_payloads_stay_full(self):
        self.store.update_node(self.new, status="archived")
        for req in (self.expand_request(), self.expand_request([self.ref(self.new)])):
            with self.subTest(refs=req["entry_refs"]):
                out = self.expanded(req, metadata_encoding="lossless_common")
                if out["base_text"]:
                    self.assertNotIn("metadata_encoding", json.loads(out["base_text"]))
        self.assertIsNone(s.api.decode_evidence_text(""))

    def test_05_decoder_reconstructs_atomic_types_without_storage(self):
        variants = [{"a": None, "empty": {}, "kind": True}, {"empty": [], "kind": 1}]
        self.rich([self.old, self.new], variants)
        full = reconstruct(self.expanded(self.expand_request([self.ref(self.old), self.ref(self.new)]))["base_text"])
        compact = copy.deepcopy(full)
        common = {"extra": {"v8_source_binding": {"ordinary_nonbank_field": ["preserve", {"no_special_omission": True}]}}}
        compact["metadata_encoding"] = "recursive-common-with-source-delta/1"
        compact["metadata_common"] = common
        for row in compact["sources"]:
            row["metadata_delta"] = row.pop("metadata")
            del row["metadata_delta"]["extra"]["v8_source_binding"]["ordinary_nonbank_field"]
        before = self.snapshot()
        with s.mock.patch.object(s.Store, "peek_node", side_effect=AssertionError("Decoder consulted store")):
            self.assertEqual(s.J(s.api.decode_evidence_text(s.J(compact))), s.J(full))
            self.assertEqual(s.J(s.api.decode_evidence_text(s.J(full))), s.J(full))
        self.assertEqual(self.snapshot(), before)

    def test_06_decoder_rejects_noncanonical_duplicate_and_nonfinite_json(self):
        text = self.expanded()["base_text"]
        bad = [" " + text, text + "\n", json.dumps(json.loads(text), ensure_ascii=False, indent=2),
               '{"warning":"x",' + text[1:], '{"n":NaN}', '{"n":Infinity}', '[]', 'null', '1', '"text"', '{']
        for value in bad:
            with self.subTest(value=value[:55]), self.assertRaises((ValueError, TypeError, s.api.ReferenceError)):
                s.api.decode_evidence_text(value)

    def test_07_decoder_rejects_ambiguous_shapes_and_bound_hash_changes(self):
        good = json.loads(self.expanded()["base_text"])
        cases = []
        def altered(action):
            value = copy.deepcopy(good)
            action(value)
            cases.append(value)
        altered(lambda v: v.update(unrecognized=True))
        altered(lambda v: v.pop("connections"))
        altered(lambda v: v.update(sources={}))
        altered(lambda v: v["sources"][0].update(metadata_delta={}))
        altered(lambda v: v["sources"][0].update(body=v["sources"][0]["body"] + "!"))
        altered(lambda v: v["sources"][0]["metadata"].update(title="corrupt"))
        altered(lambda v: v["sources"][0]["reference"].update(body_sha256="0" * 64))
        altered(lambda v: v["sources"][0]["reference"].pop("partition_id"))
        altered(lambda v: v.update(metadata_encoding="unknown", metadata_common={}))
        altered(lambda v: v.update(metadata_encoding="recursive-common-with-source-delta/1", metadata_common=[]))
        compact = factored(good)
        corrupt_common = copy.deepcopy(compact)
        corrupt_common["metadata_common"]["title"] = "wrong reconstructed title"
        # Remove a delta override so the common mutation changes the hash-bound value.
        corrupt_common["sources"][0]["metadata_delta"].pop("title", None)
        cases.append(corrupt_common)
        bad_delta = copy.deepcopy(compact)
        bad_delta["sources"][0]["metadata_delta"] = []
        cases.append(bad_delta)
        for index, value in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((ValueError, TypeError, s.api.ReferenceError)):
                s.api.decode_evidence_text(s.J(value))
        # Cross-location identity coherence: each reference is structurally
        # valid on its own, but a single node cannot identify two source versions.
        coherent = copy.deepcopy(good)
        coherent["references"].append({"reference": self.ref(self.new), "applicability": "current"})
        def incoming_connection(from_ref):
            to_ref = self.ref(self.new)
            return {"id": s.framed("incoming", {"from_ref": from_ref, "to_ref": to_ref, "relation": "relates_to"}),
                    "route": "incoming", "relation": "relates_to", "motif": None,
                    "supports": [{"reference": from_ref, "spans": []}, {"reference": to_ref, "spans": []}],
                    "differences": [], "transfer_checks": [], "status": "declared_link"}
        coherent["connections"] = [incoming_connection(self.ref(self.old))]
        self.assertEqual(s.J(s.api.decode_evidence_text(s.J(coherent))), s.J(coherent))
        collisions = []
        for field, replacement in (("body_sha256", "0" * 64), ("metadata_sha256", "1" * 64), ("graph_id", "another-graph")):
            wrong = dict(self.ref(self.old), **{field: replacement})
            descriptor_case = copy.deepcopy(coherent)
            descriptor_case["references"].append({"reference": wrong, "applicability": "current"})
            collisions.append(descriptor_case)
            connection_case = copy.deepcopy(coherent)
            connection_case["connections"] = [incoming_connection(wrong)]
            collisions.append(connection_case)
        duplicate = copy.deepcopy(coherent)
        row = copy.deepcopy(duplicate["sources"][0])
        row["body"] += " Different version."
        row["reference"]["body_sha256"] = s.H(row["body"])
        duplicate["sources"].append(row)
        collisions.append(duplicate)
        self.record()
        asserted = reconstruct(self.expanded()["base_text"])
        asserted["assertions"][0]["from_ref"]["metadata_sha256"] = "2" * 64
        collisions.append(asserted)
        for index, value in enumerate(collisions):
            for encoding in ("full", "factored"):
                packet = value if encoding == "full" else factored(value)
                with self.subTest(collision=index, encoding=encoding), self.assertRaises((ValueError, TypeError, s.api.ReferenceError)):
                    s.api.decode_evidence_text(s.J(packet))

    def test_08_base_group_uses_actual_encoding_budget_and_never_splits(self):
        self.rich([self.old, self.new])
        self.record(self.assertion(effect="qualify"))
        request = self.expand_request()
        compact = self.expanded(request, metadata_encoding="lossless_common")
        full = self.expanded(request)
        self.assertEqual(set(self.source_ids(compact)), {self.old, self.new})
        self.assertLess(compact["base_utf8_bytes"], full["base_utf8_bytes"])
        size = compact["base_utf8_bytes"]
        exact = self.expanded(dict(request, base_budget_bytes=size), metadata_encoding="lossless_common")
        self.assertEqual(exact["base_text"], compact["base_text"])
        too_small = self.expanded(dict(request, base_budget_bytes=size - 1), metadata_encoding="lossless_common")
        self.assertEqual(self.source_ids(too_small), [])
        self.assertEqual(self.source_ids(self.expanded(dict(request, base_budget_bytes=size))), [])

    def test_09_correction_first_large_history_has_no_old_rule_fallback(self):
        self.store.update_node(self.old, content="Obsolete règle 雪. " * 3000)
        self.record()
        current = self.expanded(metadata_encoding="lossless_common")
        self.assertEqual(self.source_ids(current), [self.new])
        size = current["base_utf8_bytes"]
        history = self.expanded(self.expand_request(include_history=True, base_budget_bytes=size), metadata_encoding="lossless_common")
        self.assertEqual(self.source_ids(history), [self.new])
        omitted = self.expanded(self.expand_request(base_budget_bytes=size - 1), metadata_encoding="lossless_common")
        self.assertEqual(self.source_ids(omitted), [])

    def test_10_incoming_is_actual_directed_reference_and_not_authority(self):
        self.at(self.old, s.BEFORE)
        later = self.note("later")
        self.edge(later, self.old, "context_of")
        base = self.expanded(self.incoming())
        self.assertEqual(self.source_ids(base, "optional"), [])
        out = self.expanded(self.incoming(), incoming_later=True)
        self.assertEqual(self.source_ids(out), [self.old])
        self.assertEqual(self.source_ids(out, "optional"), [later])
        expected_id = s.framed("incoming", {"from_ref": self.ref(later), "to_ref": self.ref(self.old), "relation": "context_of"})
        self.assertEqual(self.connections(out), [{"id": expected_id, "route": "incoming", "relation": "context_of", "motif": None,
            "supports": [{"reference": self.ref(later), "spans": []}, {"reference": self.ref(self.old), "spans": []}],
            "differences": [], "transfer_checks": [], "status": "declared_link"}])
        result = self.call("resolve_references", self.resolve_request(mode="direct"), incoming_later=True)
        self.assertEqual(len(result["optional"]), 1)
        descriptor = result["optional"][0]
        self.assertEqual(set(descriptor), {"route", "id", "reference_ids", "status"})
        self.assertEqual((descriptor["route"], descriptor["id"], descriptor["status"]), ("incoming", expected_id, "declared_link"))
        self.assertEqual(set(descriptor["reference_ids"]), {later, self.old})
        self.assertEqual(result["assertions"], [])

    def test_11_chronology_strictly_later_and_not_after_fixed_instant(self):
        self.at(self.old, s.BEFORE)
        for key, timestamp in (("unknown", ""), ("malformed", "yesterday"), ("equal", s.BEFORE),
                               ("earlier", "2026-10-18T23:59:59Z"), ("future", s.AFTER),
                               ("beyond_fraction", "2026-10-20T00:00:00.125001Z"),
                               ("at_cutoff", s.NOW), ("offset_ok", "2026-10-19T13:00:00+01:00")):
            self.edge(self.note(key, when=timestamp), self.old)
        out = self.expanded(self.incoming(), incoming_later=True)
        self.assertEqual(self.source_ids(out, "optional"), ["at_cutoff", "offset_ok"])
        for invalid_seed in ("", "unparseable", s.AFTER):
            with self.subTest(seed=invalid_seed):
                self.at(self.old, invalid_seed)
                self.assertEqual(self.source_ids(self.expanded(self.incoming(), incoming_later=True), "optional"), [])

    def test_12_wrong_edge_types_unlinked_and_historical_notes_not_candidates(self):
        self.at(self.old, s.BEFORE)
        for relation in ("depends_on", "contradicts", "corrects"):
            self.edge(self.note("wrong-" + relation), self.old, relation)
        self.note("unlinked")
        historical = self.note("archived-later", status="archived")
        self.edge(historical, self.old)
        baseline = self.expanded(self.incoming())
        out = self.expanded(self.incoming(), incoming_later=True)
        self.assertEqual([row for row in self.connections(out) if row["route"] == "incoming"], [])
        # Existing mandatory contradiction closure/direct behavior is preserved;
        # this test excludes these edge types from the new incoming route only.
        unchanged = copy.deepcopy(out)
        unchanged.pop("options")
        unchanged["schema"] = baseline["schema"]
        self.assertEqual(s.J(unchanged), s.J(baseline))

    def test_13_outgoing_priority_incoming_sort_dedup_and_one_common_cap(self):
        self.at(self.old, s.BEFORE)
        for key in ("out", "aa-relates", "zz-context", "bb-relates"):
            self.note(key)
        self.edge(self.old, "out")
        self.edge("out", self.old)
        self.edge("aa-relates", self.old)
        self.edge("zz-context", self.old, "context_of")
        self.edge("bb-relates", self.old)
        self.edge("bb-relates", self.old, "context_of")
        for limit, expected in ((0, []), (1, ["out"]), (2, ["out", "bb-relates"]),
                                (3, ["out", "bb-relates", "zz-context"]),
                                (8, ["out", "bb-relates", "zz-context", "aa-relates"])):
            with self.subTest(limit=limit):
                out = self.expanded(self.incoming(optional_limit=limit), incoming_later=True)
                self.assertEqual(self.source_ids(out, "optional"), expected)
                self.assertLessEqual(len(self.connections(out)), limit)

    def test_14_hidden_incoming_endpoints_do_not_change_bytes_or_counts(self):
        self.at(self.old, s.BEFORE)
        self.edge(self.note("visible"), self.old)
        request = self.incoming()
        before = self.expanded(request, metadata_encoding="lossless_common", incoming_later=True)
        for index in range(12):
            key = self.note("hidden-%02d" % index, project=str(self.root / "foreign-repo"))
            self.edge(key, self.old, "context_of")
        client_hidden = self.note("hidden-client", domains=["client:claude"])
        self.edge(client_hidden, self.old)
        after = self.expanded(request, metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(s.J(after), s.J(before))

    def test_15_incoming_target_correction_closure_no_old_fallback(self):
        self.at(self.old, s.BEFORE)
        target = self.note("incoming-old")
        successor = self.note("incoming-current")
        self.edge(target, self.old)
        self.record(self.assertion(successor, target))
        request = self.incoming()
        out = self.expanded(request, incoming_later=True)
        self.assertEqual(self.source_ids(out, "optional"), [successor])
        self.assertNotIn(target, self.source_ids(out, "optional"))
        size = out["optional_utf8_bytes"]
        under = self.expanded(dict(request, optional_budget_bytes=size - 1), incoming_later=True)
        self.assertEqual(self.source_ids(under, "optional"), [])

    def test_16_optional_atomic_group_uses_compact_actual_trial_size(self):
        self.at(self.old, s.BEFORE)
        first, second = self.note("incoming-basis"), self.note("incoming-qualifier")
        self.rich([first, second])
        self.edge(first, self.old)
        self.record(self.assertion(second, first, effect="qualify"))
        request = self.incoming()
        compact = self.expanded(request, metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(set(self.source_ids(compact, "optional")), {first, second})
        full = self.expanded(request, incoming_later=True)
        size = compact["optional_utf8_bytes"]
        self.assertLess(size, full["optional_utf8_bytes"])
        exact = self.expanded(dict(request, optional_budget_bytes=size), metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(exact["optional_text"], compact["optional_text"])
        under = self.expanded(dict(request, optional_budget_bytes=size - 1), metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(self.source_ids(under, "optional"), [])
        self.assertEqual(self.source_ids(self.expanded(dict(request, optional_budget_bytes=size), incoming_later=True), "optional"), [])

    def test_17_shared_incoming_stays_evidence_only_without_active_correction(self):
        self.at(self.old, s.BEFORE)
        shared = self.note("shared-later", audience="team", prov_source="kinbase:invented-origin")
        extra = {"kinbase": {"repo": "invented-origin", "scope": "invented-scope", "store_kind": "codebase",
                 "source_identity": "invented-document", "source_document_redacted": False,
                 "signature_verified": False, "governance_verified": False, "mode": "synthetic_fixture",
                 "status": "current", "trust": "synthetic_evidence"},
                 "v8_source_binding": {"receipt_kind": "synthetic_fixture", "source_bank_sha256": s.H("independent invented v9 fixture")}}
        self.store.update_node(shared, extra=extra)
        partitions = copy.deepcopy(self.shape["partitions"]) + [{"partition_id": "shared", "kind": "kinbase", "project_path": None,
            "kinbase": {"repo": "invented-origin", "scope": "invented-scope", "store_kind": "codebase", "receipt_kind": "synthetic_fixture"}}]
        shape = s.context_shape(self.graph, self.project, self.session, partitions=partitions)
        context = s.api.ReadContext(**shape)
        self.edge(shared, self.old)
        out = self.expanded(self.incoming(), context=context, incoming_later=True)
        self.assertEqual(self.source_ids(out, "optional"), [shared])
        self.assertEqual(reconstruct(out["optional_text"])["sources"][0]["applicability"], "evidence_only")
        self.assertEqual(self.source_ids(out), [self.old])
        self.assertEqual(self.source_ids(self.expanded(self.incoming(), incoming_later=True), "optional"), [])
        assertion = self.assertion(shared, self.old)
        assertion["from_ref"] = self.ref(shared, "shared")
        self.reject(lambda: s.api.record_correction(self.store, assertion, context=self.write_context(shape=shape)))

    def test_18_incoming_conflict_group_remains_atomic_quoted_evidence(self):
        self.at(self.old, s.BEFORE)
        first, second = self.note("incoming-conflict-a"), self.note("incoming-conflict-b")
        self.edge(first, self.old)
        self.edge(first, second, "contradicts")
        request = self.incoming()
        out = self.expanded(request, metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(set(self.source_ids(out, "optional")), {first, second})
        for row in reconstruct(out["optional_text"])["sources"]:
            self.assertEqual(row["applicability"], "conflicting_evidence")
        under = self.expanded(dict(request, optional_budget_bytes=out["optional_utf8_bytes"] - 1), metadata_encoding="lossless_common", incoming_later=True)
        self.assertEqual(self.source_ids(under, "optional"), [])

    def test_19_historical_seed_keeps_historical_status_and_seed_order(self):
        self.at(self.old, s.BEFORE)
        self.store.update_node(self.old, status="archived")
        self.at(self.new, s.BEFORE)
        first, second, duplicate = self.note("zz-first-seed"), self.note("aa-second-seed"), self.note("duplicate-seeds")
        self.edge(first, self.old)
        self.edge(second, self.new, "context_of")
        self.edge(duplicate, self.old)
        self.edge(duplicate, self.new, "context_of")
        request = self.incoming(entry_refs=[self.ref(self.old), self.ref(self.new)])
        # Incoming order inherits public base priority, not request entry order.
        # Resolve without either v9 option, then independently order our known
        # native edges and deduplicate the newly navigated source IDs.
        baseline = self.resolve(self.resolve_request([self.ref(self.old), self.ref(self.new)]))
        seed_priority = [ref["node_id"] for ref in baseline["priority_refs"]]
        self.assertEqual(set(seed_priority), {self.old, self.new})
        edges = [(first, self.old, "relates_to"), (second, self.new, "context_of"),
                 (duplicate, self.old, "relates_to"), (duplicate, self.new, "context_of")]
        ordered = sorted(edges, key=lambda edge: (seed_priority.index(edge[1]), edge[2], edge[0]))
        expected = list(dict.fromkeys(edge[0] for edge in ordered))
        out = self.expanded(request, incoming_later=True)
        self.assertEqual(self.source_ids(out), [self.new])
        self.assertEqual(self.source_ids(out, "optional"), expected)
        reference = next(row for row in reconstruct(out["base_text"])["references"] if row["reference"]["node_id"] == self.old)
        self.assertEqual(reference["applicability"], "historical")
        resolved = self.call("resolve_references", self.resolve_request([self.ref(self.old), self.ref(self.new)], mode="direct"), incoming_later=True)
        self.assertEqual(s.J(resolved["entries"]), s.J(baseline["entries"]))
        historical_entry = next(row for row in resolved["entries"] if row["reference"]["node_id"] == self.old)
        self.assertNotEqual(historical_entry["status"], "current")

    def test_20_both_complete_privacy_representations_must_pass(self):
        self.rich([self.old, self.new])
        request = self.expand_request([self.ref(self.old), self.ref(self.new)])
        for surface in ("compact", "restored"):
            with self.subTest(surface=surface):
                before = self.snapshot()
                reader = self.reader()
                try:
                    with privacy_probe.reject_surface(surface):
                        with self.assertRaises(s.api.ReferenceError) as raised:
                            s.api.expand_references(reader, request, context=self.context, metadata_encoding="lossless_common")
                        self.assertEqual(raised.exception.code, "privacy_unavailable")
                        self.assertGreater(privacy_probe.HITS, 0, "Required complete privacy surface was never checked")
                finally:
                    reader.close()
                self.assertEqual(self.snapshot(), before)
