"""Synthetic contracts ported from the frozen practical-memory campaign."""
import contextlib
import copy
import importlib.metadata as importlib_metadata
import io
import json
import os
import sys
import unittest
from unittest import mock
from . import reference_support as s


class CLIContracts(s.Fixture):
    STORE_NAME = "v8_contract_cli"

    @classmethod
    def setUpClass(cls):
        cls.plugin_patch = mock.patch.object(importlib_metadata, "entry_points", return_value=importlib_metadata.EntryPoints())
        cls.plugin_patch.start()
        from kindex import cli, config, logstamp
        cls.cli, cls.native_config, cls.logstamp = cli, config, logstamp

    @classmethod
    def tearDownClass(cls):
        cls.plugin_patch.stop()

    def invoke(self, leaf, request, *, env=None, project=None, json_flag=True, raw=None, extra_args=()):
        request_path = self.root / "v8-cli-request.json"
        config_path = self.root / "v8-cli-explicit-config.json"
        request_path.write_text(s.J(request) if raw is None else raw, encoding="utf-8")
        config_path.write_text(self.config.model_dump_json(), encoding="utf-8")
        selected_project = project or self.project
        fixture_config = self.config.model_copy(deep=True)
        def loader(config_path_arg=None, project_path=None, profile=None, data_dir=None):
            self.assertEqual(str(config_path_arg), str(config_path))
            self.assertEqual(str(project_path), selected_project)
            self.assertIsNone(profile); self.assertIsNone(data_dir)
            result = fixture_config.model_copy(deep=True)
            # Emulate config discovery widening to the fixture parent; the
            # explicit CLI project must remain the receiving owner boundary.
            result._project_path = self.native_config.resolve_project_root(project_path)
            return result
        environment = {"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef", "KIN_PROJECT": self.project}
        if env is not None:
            environment = env
        argv = ["kin", "practical-memory", *leaf, "--request-json", str(request_path),
                "--config", str(config_path), "--project-path", selected_project]
        argv.extend(extra_args)
        if json_flag:
            argv.append("--json")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(self.native_config, "load_config", side_effect=loader), \
             mock.patch.object(self.native_config, "resolve_project_root", return_value=s.Path(self.project)), \
             mock.patch.object(self.logstamp, "install_from_env", return_value=None), \
             mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "excepthook", sys.excepthook), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                returned = self.cli.main()
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0 if returned is None else returned
        output = stdout.getvalue()
        self.assertTrue(output.strip(), stderr.getvalue())
        parsed = json.loads(output)
        self.assertEqual(output.rstrip("\n"), s.J(parsed), "CLI emits canonical JSON")
        return code, parsed, output + stderr.getvalue()

    def test_reference_expand_and_explicit_correction_leaves_real_store(self):
        before = self.database()
        code, ref, _ = self.invoke(["reference"], self.resolve_request())
        self.assertEqual(code, 0)
        self.assertEqual(ref["schema"], "reference-result/1")
        self.assertEqual(ref["entries"][0]["reference"], self.ref(self.old))
        self.assertEqual(self.database(), before)
        code, plain, _ = self.invoke(["reference"], self.resolve_request(), json_flag=False)
        self.assertEqual(code, 0); self.assertEqual(plain, ref)
        assertion = self.assertion()
        native = self.native_rows()
        code, receipt, _ = self.invoke(["correction", "add"], assertion)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["id"], s.framed("correction", assertion))
        self.assertTrue(receipt["inserted"])
        self.assertEqual(self.native_rows(), native)
        writer_json = self.store.conn.execute("SELECT writer_json FROM v8_corrections").fetchone()[0]
        self.assertEqual(json.loads(writer_json)["actor_id"], "fixture-actor")
        before = self.database()
        code, expanded, _ = self.invoke(["expand"], self.expand_request())
        self.assertEqual(code, 0)
        self.assertEqual(self.source_ids(expanded), [self.new])
        self.assertEqual(self.database(), before)
        code, retirement, _ = self.invoke(["correction", "retire"], {"assertion_id": receipt["id"], "reason": "Explicit fixture withdrawal"})
        self.assertEqual(code, 0)
        self.assertEqual(retirement["kind"], "retirement")

    def test_missing_ambiguous_or_forged_host_context_fails_without_writes(self):
        for env in ({}, {"KIN_SESSION_ID": "missing-owner", "CODEX_THREAD_ID": self.session},
                    {"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": self.session, "CLAUDE_SESSION_ID": self.session}):
            before = self.database()
            with self.subTest(env=env):
                code, out, text = self.invoke(["reference"], self.resolve_request(), env=env)
                self.assertNotEqual(code, 0)
                self.assertNotIn(self.store.peek_node(self.old)["content"], text)
                self.assertEqual(self.database(), before)
        before = self.database()
        bad = self.resolve_request(); bad["context"] = self.shape
        code, out, _ = self.invoke(["reference"], bad)
        self.assertNotEqual(code, 0); self.assertEqual(self.database(), before)
        raw = s.J(self.resolve_request())[:-1] + ',"mode":"dream"}'
        code, out, _ = self.invoke(["reference"], self.resolve_request(), raw=raw)
        self.assertNotEqual(code, 0); self.assertEqual(self.database(), before)

    def test_dream_index_import_leaf_accepts_only_compiled_source_bound_payload(self):
        sources = [{"reference": self.ref(key), "metadata": s.metadata(self.store.peek_node(key)),
                    "body": self.store.peek_node(key)["content"], "episode_id": "episode-" + key}
                   for key in sorted([self.old, self.new])]
        snapshot = {"schema": "dream-snapshot/1", "graph_id": self.graph, "profile": "", "context": self.context.to_dict(),
                    "cutoff": s.NOW, "sources": sources}
        proposals = {"schema": "dream-proposals/1", "snapshot_sha256": s.H(s.J(snapshot)),
                     "generator_protocol_sha256": s.H("independent CLI fixture protocol"), "proposals": []}
        compiled = s.api.compile_dream_index(snapshot, proposals)
        native = self.native_rows()
        code, out, _ = self.invoke(["dream-index", "import"], compiled)
        self.assertEqual(code, 0)
        self.assertEqual(out["kind"], "dream_batch")
        self.assertEqual(out["id"], compiled["batch_id"])
        self.assertEqual(self.native_rows(), native)
        before = self.database()
        forged = copy.deepcopy(compiled); forged["actor_id"] = "requested-writer"
        code, out, _ = self.invoke(["dream-index", "import"], forged)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.database(), before)

    def test_explicit_nested_owner_does_not_widen_to_enclosing_project(self):
        nested = self.root / "synthetic-project-v8" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        before = self.database()
        code, out, _ = self.invoke(["reference"], self.resolve_request(), project=str(nested))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.database(), before)
        self.store.add_node("Nested owner", node_id="nested-owner", node_type="session", domains=[], extra={"project_path": str(nested)}, queue_embedding=False)
        key = self.add("nested-source", "Nested exact-source evidence.", project=str(nested))
        before = self.database()
        code, out, _ = self.invoke(["reference"], self.resolve_request([self.ref(key)]), project=str(nested),
            env={"KIN_SESSION_ID": "nested-owner", "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef"})
        self.assertEqual(code, 0)
        self.assertEqual(out["entries"][0]["reference"], self.ref(key))
        self.assertEqual(self.database(), before)


    def test_explicit_cli_encoding_and_incoming_options_preserve_default_behavior(self):
        self.store.conn.execute("UPDATE nodes SET prov_when=? WHERE id=?", ("2026-10-01T00:00:00Z", self.old))
        self.store.conn.execute("UPDATE nodes SET prov_when=? WHERE id=?", ("2026-10-19T00:00:00Z", self.new))
        self.store.conn.commit()
        self.store.add_edge(self.new, self.old, edge_type="relates_to", bidirectional=False)
        request = self.expand_request(mode="direct", optional_budget_bytes=32768)
        before = self.database()
        code, legacy, _ = self.invoke(["expand"], request)
        self.assertEqual(code, 0)
        self.assertEqual(legacy["schema"], "evidence-result/1")
        self.assertEqual(legacy["optional_text"], "")
        code, opted, _ = self.invoke(["expand"], request,
            extra_args=("--metadata-encoding", "lossless_common", "--incoming-later"))
        self.assertEqual(code, 0)
        self.assertEqual(opted["schema"], "evidence-result/2")
        self.assertEqual(opted["options"], {"metadata_encoding": "lossless_common", "incoming_later": True})
        decoded = s.api.decode_evidence_text(opted["optional_text"])
        self.assertIn(self.new, [row["reference"]["node_id"] for row in decoded["sources"]])
        code, references, _ = self.invoke(["reference"], self.resolve_request(mode="direct"), extra_args=("--incoming-later",))
        self.assertEqual(code, 0)
        self.assertEqual(references["schema"], "reference-result/2")
        self.assertEqual(references["options"], {"incoming_later": True})
        self.assertEqual(self.database(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
