"""Offline regression tests for preserving incompatible embedding indexes.

All stores and homes are temporary. HTTP is denied except for explicit fake
provider responses; no provider credentials or live scheduler state are used.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from kindex.config import Config
from kindex.store import Store
from kindex import vectors


class EmbeddingIndexSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kindex-safety-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # No inherited credentials, project selectors, or user's config.
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.http = patch("urllib.request.urlopen", side_effect=AssertionError("Live provider access forbidden"))
        self.http.start()
        self.addCleanup(self.http.stop)
        self.stderr = io.StringIO()
        self.capture_stderr = contextlib.redirect_stderr(self.stderr)
        self.capture_stderr.__enter__()
        self.addCleanup(self.capture_stderr.__exit__, None, None, None)


    def store(self, embedding=None, name="graph"):
        store = Store(Config(data_dir=str(self.root / name), embedding=embedding or {}), manage_project_storage=False)
        self.addCleanup(store.close)
        return store


    def seed(self, store):
        node = store.add_node("Existing node", content="Stable content", queue_embedding=False)
        with patch("kindex.vectors._embed_text_or_raise", return_value=[0.1] * vectors._get_embedding_dim(store.config)):
            self.assertTrue(vectors.upsert_embedding(store, node, vectors._embedding_text_for_node(store.get_node(node))))
        return node


    def vector_state(self, store):
        return {table: [tuple(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("node_vectors", "node_vector_meta", "vec_meta")}


    def test_missing_embedding_key_preserves_vectors_and_queue(self):
        store = self.store({"provider": "openai", "dimensions": 2})
        self.seed(store)
        pending = store.add_node("Pending node", content="Not yet embedded")
        before = self.vector_state(store)
        queued = store.get_meta(vectors.EMBED_QUEUE_META)
        result = vectors.drain_embedding_queue(store, max_jobs=1)
        self.assertFalse(result["drain_complete"])
        self.assertEqual(result["embedded"], 0)
        self.assertEqual(self.vector_state(store), before)
        self.assertEqual(store.get_meta(vectors.EMBED_QUEUE_META), queued)
        self.assertIn(pending, json.loads(queued))
        self.assertIn("not set", self.stderr.getvalue())


    def test_provider_authentication_failure_preserves_vectors_and_queue(self):
        store = self.store({"provider": "openai", "dimensions": 2})
        self.seed(store)
        store.add_node("Pending node", content="Not yet embedded")
        before = self.vector_state(store)
        queued = store.get_meta(vectors.EMBED_QUEUE_META)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-invalid"}):
            with patch("urllib.request.urlopen", side_effect=HTTPError("https://api.openai.com/v1/embeddings", 401, "Unauthorized", {}, io.BytesIO(b'{}'))):
                result = vectors.drain_embedding_queue(store, max_jobs=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.vector_state(store), before)
        self.assertEqual(store.get_meta(vectors.EMBED_QUEUE_META), queued)


    def test_ordinary_paths_never_replace_an_existing_fingerprint(self):
        for explicit in (False, True):
            for operation in ("ensure", "search", "upsert", "reindex", "enqueue_drain"):
                with self.subTest(explicit=explicit, operation=operation):
                    store = self.store({"provider": "openai", "dimensions": 2}, name=f"graph-{explicit}-{operation}")
                    old = self.seed(store)
                    pending = store.add_node("Pending node", content="Other content")
                    before = self.vector_state(store)
                    # Same dimensions are intentional: a provider change is still incompatible.
                    store.config.embedding = Config(embedding={"model": "voyage-3.5", "dimensions": 2, "strategy": "single",
                        **({"provider": "voyage"} if explicit else {})}).embedding
                    with patch("kindex.vectors._embed_text_or_raise", return_value=[0.2, 0.3]) as provider_call:
                        if operation == "ensure":
                            self.assertFalse(vectors.ensure_vec_table(store))
                        elif operation == "search":
                            with self.assertRaises(vectors.EmbeddingFingerprintMismatch):
                                vectors.vector_search(store, "query")
                        elif operation == "upsert":
                            self.assertFalse(vectors.upsert_embedding(store, pending, "Other content"))
                        elif operation == "reindex":
                            self.assertEqual(vectors.reindex_now(store)["status"], "failed")
                        else:
                            vectors.enqueue_reindex(store)
                            self.assertFalse(vectors.drain_embedding_queue(store, max_jobs=1)["drain_complete"])
                    provider_call.assert_not_called()
                    self.assertEqual(self.vector_state(store), before)
                    self.assertIn(pending, json.loads(store.get_meta(vectors.EMBED_QUEUE_META)))
                    self.assertIsNotNone(store.get_node(old))


    def test_missing_legacy_fingerprint_does_not_authorize_reset(self):
        store = self.store({"provider": "openai", "dimensions": 2})
        self.seed(store)
        store.conn.execute("DROP TABLE vec_meta")
        store.conn.commit()
        before = [tuple(row) for row in store.conn.execute("SELECT * FROM node_vectors")]
        self.assertFalse(vectors.ensure_vec_table(store))
        self.assertIsNone(store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_meta'").fetchone())
        result = vectors.reindex_now(store)
        self.assertEqual(result["status"], "failed")
        self.assertEqual([tuple(row) for row in store.conn.execute("SELECT * FROM node_vectors")], before)


    def test_stale_metadata_without_vector_table_does_not_block_index_creation(self):
        store = self.store({"provider": "openai", "dimensions": 2})
        store.conn.execute("CREATE TABLE vec_meta (key TEXT PRIMARY KEY, value TEXT)")
        store.conn.execute("INSERT INTO vec_meta VALUES ('embedding_dim', '999')")
        store.conn.execute("INSERT INTO vec_meta VALUES ('embedding_fingerprint', 'stale')")
        store.conn.commit()
        self.assertTrue(vectors.ensure_vec_table(store))
        metadata = dict(store.conn.execute("SELECT key, value FROM vec_meta"))
        self.assertEqual(metadata["embedding_dim"], "2")
        self.assertEqual(metadata["embedding_fingerprint"], vectors.embedding_fingerprint(store.config))


    def test_read_only_mismatch_drops_only_secondary_vector_channel(self):
        from kindex.retrieve import hybrid_search

        store = self.store({"provider": "openai", "dimensions": 2})
        node = self.seed(store)
        store.conn.commit()
        readonly_config = store.config.model_copy(deep=True)
        readonly_config.embedding.model = "different-model"
        readonly = Store(readonly_config, read_only=True, migrate=False,
                         manage_project_storage=False)
        self.addCleanup(readonly.close)
        grounding = {}
        results = hybrid_search(readonly, "Existing node", top_k=5,
                                grounding=grounding)
        self.assertIn(node, {row["id"] for row in results})
        self.assertIn("read-only graph", grounding["embedding_warning"])
        self.assertIn("stored:", grounding["embedding_warning"])
        self.assertIn("current:", grounding["embedding_warning"])
        self.assertIn("different-model", grounding["embedding_warning"])


    def test_mcp_secondary_mismatch_warning_survives_all_evidence_paths(self):
        from kindex import mcp_server as server

        primary = self.store({"provider": "openai", "dimensions": 2}, name="primary")
        primary.add_node("Primaryneedle", content="Project evidence", queue_embedding=False)
        secondary = self.store({"provider": "openai", "dimensions": 2}, name="secondary")
        self.seed(secondary)
        before = self.vector_state(secondary)
        readonly_config = secondary.config.model_copy(deep=True)
        readonly_config.embedding.model = "different-model"
        readonly = Store(readonly_config, read_only=True, migrate=False,
                         manage_project_storage=False)
        self.addCleanup(readonly.close)

        @contextlib.contextmanager
        def selected(*args, **kwargs):
            yield primary, readonly

        with patch.object(server, "_selected_read_stores", selected), \
                patch.object(server, "_get_config", return_value=primary.config), \
                patch.object(server, "_graph_aware_session", return_value=False):
            for query in ("Existing node", "Primaryneedle", "Nohitsneedle"):
                for surface in ("context", "evidence", "ask"):
                    with self.subTest(query=query, surface=surface):
                        if surface == "ask":
                            output = server.ask(query)
                        else:
                            output = server.context(topic=query, level=(
                                "evidence" if surface == "evidence" else "abridged"))
                        self.assertIn("Global graph:", output)
                        self.assertIn("read-only graph", output)
                        self.assertIn("stored:", output)
                        self.assertIn("current:", output)
                        self.assertIn("different-model", output)
                        if query == "Nohitsneedle":
                            self.assertIn("No relevant knowledge found", output)
                        else:
                            self.assertIn(query, output)
            # Evidence truncation must never erase the degradation warning.
            for output in (server.ask("Existing node", max_tokens=1),
                           server.context(topic="Existing node", level="evidence", max_tokens=1)):
                self.assertIn("read-only graph", output)
                self.assertIn("different-model", output)
            with patch("kindex.answer.answer_client", return_value=object()), \
                    patch("kindex.answer.plan_question", return_value=("factual", [], False)), \
                    patch("kindex.answer.draft_answer", return_value="Drafted answer") as draft:
                output = server.ask("Existing node", answer=True)
                self.assertIn("read-only graph", draft.call_args.args[2])
                self.assertIn("read-only graph", output)
                self.assertIn("Drafted answer", output)
        self.assertEqual(self.vector_state(secondary), before)


    def test_nonsemantic_task_nearby_does_not_use_vectors(self):
        from kindex.cli import _cmd_task
        from kindex import retrieve

        store = self.store()
        with patch.object(retrieve, "detect_domain_from_path", return_value=[]), \
                patch.object(retrieve, "hybrid_search", return_value=[]) as search, \
                patch("kindex.tasks.nearby_tasks", return_value=[]):
            _cmd_task(SimpleNamespace(task_action="nearby", json=True), store)
        search.assert_called_once()
        self.assertFalse(search.call_args.kwargs["use_vectors"])


    def test_optional_compact_hook_context_survives_fingerprint_mismatch(self):
        from kindex.cli import _emit_compact_context
        store = self.store()
        output = io.StringIO()
        with patch("kindex.retrieve.hybrid_search",
                   side_effect=vectors.EmbeddingFingerprintMismatch("mismatch")), \
                contextlib.redirect_stderr(output):
            _emit_compact_context(store, SimpleNamespace(emit_context=True), "topic")
        self.assertIn("Optional context unavailable: mismatch", output.getvalue())


    def test_mcp_semantic_tools_return_actionable_error_while_inspection_works(self):
        from kindex import mcp_server as server
        store = self.store({"provider": "openai", "dimensions": 2})
        node = self.seed(store)
        store.config.embedding.model = "different-model"
        before = self.vector_state(store)
        @contextlib.contextmanager
        def selected(*args, **kwargs):
            yield store, None
        with patch.object(server, "_selected_read_stores", selected), \
                patch.object(server, "_get_store", return_value=(store, store.config)), \
                patch.object(server, "_get_config", return_value=store.config), \
                patch("kindex.supervisor_health.record_mcp"), \
                patch("kindex.vectors._embed_text_or_raise") as call:
            from mcp import types
            handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
            for name, arguments in (("search", {"query": "Existing"}),
                                    ("context", {"topic": "Existing"}),
                                    ("ask", {"question": "Existing"})):
                response = asyncio.run(handler(types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments))))
                result = response.root
                self.assertTrue(result.isError)
                output = "\n".join(item.text for item in result.content if item.type == "text")
                self.assertIn("fingerprint mismatch", output.lower())
                self.assertIn("matching", output)
                self.assertIn("stored:", output)
                self.assertIn("current:", output)
                self.assertNotIn("fake-", output)
            # Non-semantic graph inspection and diagnostic counters remain usable.
            self.assertIsNotNone(store.get_node(node))
            self.assertEqual(vectors.embedding_status(store)["vector_rows"], 1)
            call.assert_not_called()
        self.assertEqual(self.vector_state(store), before)


    def test_scheduled_maintenance_fails_without_prompt_or_provider_call(self):
        from kindex import daemon
        store = self.store({"provider": "openai", "dimensions": 2})
        self.seed(store)
        store.config.embedding.model = "different-model"
        store.config._project_path = self.root
        before = self.vector_state(store)
        # Exercise the scheduler's existing project-maintenance function only on
        # this fixture; never run a live cron job or unrelated maintenance.
        with patch("kindex.daemon._project_housekeeping", return_value={}), \
                patch("kindex.project_store.project_is_established", return_value=True), \
                patch("kindex.reinforce.drain_reinforce_queue", return_value={"status": "empty"}), \
                patch("kindex.attention.drain_attention_queue", return_value={"status": "empty"}), \
                patch("kindex.daemon.set_run_marker"), \
                patch("builtins.input", side_effect=AssertionError("Scheduled jobs must not prompt")), \
                patch("kindex.vectors._embed_text_or_raise") as call:
            result = daemon._project_maintenance(store.config, store)
            call.assert_not_called()
        self.assertEqual(result["embedding_status"], "failed")
        self.assertEqual(result["embedding"]["errors"][0]["kind"], "fingerprint_mismatch")
        self.assertEqual(self.vector_state(store), before)


    def test_no_unimplemented_rebuild_command_is_advertised(self):
        result = subprocess.run([sys.executable, "-m", "kindex.cli", "embed", "reindex", "--help"],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--rebuild", result.stdout)


    def test_cli_embedding_exception_exits_nonzero(self):
        from kindex import cli
        store = self.store({"provider": "openai", "dimensions": 2})
        args = SimpleNamespace(embed_action="reindex", enqueue=False, json=True)
        with patch("kindex.cli._store", return_value=store), \
                patch("kindex.vectors.reindex_now", side_effect=OSError("synthetic commit failure")):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_embed(args)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Vector indexing failed", self.stderr.getvalue())
