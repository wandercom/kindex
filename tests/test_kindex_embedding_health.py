"""Offline regression tests for KinDoctor embedding coverage and freshness.

Every graph/home is temporary; embeddings use fake vectors, never credentials
or provider HTTP. Runnable with unittest as well as pytest.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kindex import cli, vectors
from kindex.config import Config
from kindex.embedding_health import embedding_doctor_findings, embedding_health_lines
from kindex.store import Store


class EmbeddingHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kindex-health-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        environment = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = patch("urllib.request.urlopen", side_effect=AssertionError("Provider HTTP forbidden"))
        self.http_call = self.http.start()
        self.addCleanup(self.http.stop)
        self.store = Store(Config(data_dir=str(self.root / "graph"),
                                  embedding={"provider": "openai", "dimensions": 2}),
                           manage_project_storage=False)
        self.addCleanup(self.store.close)

    def add(self, title="Synthetic node", content="Synthetic content"):
        return self.store.add_node(title, content=content, queue_embedding=False)

    def embed(self, node_id, timestamp=None, chunks=1):
        node = self.store.get_node(node_id)
        text = vectors._embedding_text_for_node(node)
        records = [{"embedding": [0.1, 0.2], "index": index, "text_hash": vectors._hash_text(text),
                    "token_estimate": 2, "text": "synthetic"} for index in range(chunks)]
        with patch("kindex.vectors._embed_document_chunks", return_value=records):
            self.assertTrue(vectors.upsert_embedding(self.store, node_id, text))
        if timestamp is not None:
            self.store.conn.execute("UPDATE node_vector_meta SET updated_at=? WHERE node_id=?", (timestamp, node_id))
            self.store.conn.commit()

    def status(self):
        return vectors.embedding_status(self.store)

    def doctor(self, json_output=True, fix=False):
        args = SimpleNamespace(data_dir=str(self.root / "graph"), json=json_output,
                               fix=fix, fix_task_owners=False)
        output = io.StringIO()
        with patch("kindex.cli._store", return_value=self.store), \
                patch("kindex.cli._config", return_value=self.store.config), \
                patch.object(self.store, "close"), contextlib.redirect_stdout(output):
            cli.cmd_doctor(args)
        return json.loads(output.getvalue()) if json_output else output.getvalue()

    def test_empty_graph_is_explicitly_not_a_false_100_percent(self):
        result = self.status()
        self.assertEqual(result["coverage_state"], "empty")
        self.assertEqual(result["eligible_nodes"], 0)
        self.assertEqual(result["fresh_nodes"], 0)
        self.assertIsNone(result["coverage_percent"])
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["retained_embedding_timestamp_state"], "none")
        self.assertIn("0/0 current (n/a", embedding_health_lines(result)[0])

    def test_old_fully_current_embeddings_are_healthy(self):
        self.embed(self.add(), "2001-01-01T00:00:00Z")
        result = self.status()
        self.assertEqual(result["fresh_nodes"], 1)
        self.assertEqual(result["coverage_percent"], 100)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["latest_retained_embedding_at"], "2001-01-01T00:00:00+00:00")
        self.assertEqual(embedding_doctor_findings(result), ([], []))

    def test_recent_embedding_does_not_hide_massive_missing_coverage(self):
        self.embed(self.add("Recent"), "2026-01-01T12:00:00Z")
        for i in range(19):
            self.add(f"Missing {i}")
        result = self.status()
        self.assertEqual((result["fresh_nodes"], result["eligible_nodes"], result["missing_nodes"]), (1, 20, 19))
        self.assertEqual(result["coverage_percent"], 5)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["queue_pending"], 0)
        self.assertIn("1/20 current (5.00%)", embedding_doctor_findings(result)[0][0])
        doctor = self.doctor()
        self.assertFalse(doctor["healthy"])
        self.assertEqual(doctor["embeddings"]["coverage_percent"], 5)

    def test_metadata_only_edit_does_not_stale_embedding_but_content_edit_does(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.conn.execute("UPDATE nodes SET updated_at='2099-01-01', weight=0.9, domains='[\"new-tag\"]' WHERE id=?", (node_id,))
        self.assertTrue(self.status()["coverage_complete"])
        self.store.conn.execute("UPDATE nodes SET content='Changed semantic content' WHERE id=?", (node_id,))
        result = self.status()
        self.assertEqual((result["fresh_nodes"], result["stale_or_incomplete_nodes"], result["missing_nodes"]), (0, 1, 0))
        self.assertFalse(result["coverage_complete"])

    def test_title_edit_is_semantically_stale(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.conn.execute("UPDATE nodes SET title='Changed title' WHERE id=?", (node_id,))
        self.assertEqual(self.status()["stale_or_incomplete_nodes"], 1)

    def test_redacted_only_content_change_matches_indexing_semantics(self):
        node_id = self.add()
        with patch("kindex.vectors.redact_text", return_value="same redacted text"):
            self.embed(node_id)
            self.store.conn.execute("UPDATE nodes SET content='Different private source' WHERE id=?", (node_id,))
            self.assertTrue(self.status()["coverage_complete"])

    def test_archived_superseded_and_empty_text_are_excluded(self):
        self.embed(self.add("Eligible"))
        for state in ("archived", "superseded"):
            node_id = self.add(state)
            self.store.conn.execute("UPDATE nodes SET status=? WHERE id=?", (state, node_id))
        empty_id = self.add("Temporary title")
        self.store.conn.execute("UPDATE nodes SET title='', content='' WHERE id=?", (empty_id,))
        result = self.status()
        self.assertEqual(result["eligible_nodes"], 1)
        self.assertTrue(result["coverage_complete"])

    def test_wrong_node_fingerprint_is_stale(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.conn.execute("UPDATE node_vector_meta SET fingerprint='wrong'")
        result = self.status()
        self.assertEqual(result["stale_or_incomplete_nodes"], 1)
        self.assertFalse(result["fingerprint_mismatch"])
        self.assertFalse(result["coverage_complete"])

    def test_index_fingerprint_mismatch_is_actionable_and_never_resets(self):
        self.embed(self.add())
        self.store.config.embedding.model = "different-model"
        before = self.store.conn.total_changes
        result = self.status()
        self.assertTrue(result["fingerprint_mismatch"])
        self.assertNotEqual(result["fingerprint"], result["stored_fingerprint"])
        self.assertFalse(result["coverage_complete"])
        issue = embedding_doctor_findings(result)[0][0]
        self.assertIn("Restore the matching", issue)
        self.assertIn("separately reviewed procedure", issue)
        self.assertEqual(self.store.conn.total_changes, before)
        output = self.doctor()
        self.assertFalse(output["healthy"])
        self.assertTrue(any("fingerprint mismatch" in item for item in output["issues"]))

    def test_dimension_mismatch_and_missing_identity_are_explicit(self):
        self.embed(self.add())
        for sql in ("UPDATE vec_meta SET value='999' WHERE key='embedding_dim'",
                    "DELETE FROM vec_meta WHERE key='embedding_fingerprint'"):
            self.store.conn.execute(sql)
            self.assertTrue(self.status()["fingerprint_mismatch"])
            self.assertFalse(self.status()["coverage_complete"])

    def test_missing_chunk_and_missing_vector_row_are_incomplete(self):
        node_id = self.add()
        self.embed(node_id, chunks=2)
        self.assertTrue(self.status()["coverage_complete"])
        self.store.conn.execute("DELETE FROM node_vectors WHERE node_id=?", (node_id + "#0001",))
        self.assertEqual(self.status()["stale_or_incomplete_nodes"], 1)
        self.store.conn.execute("DELETE FROM node_vector_meta WHERE vector_id=?", (node_id + "#0001",))
        result = self.status()
        self.assertEqual(result["stale_or_incomplete_nodes"], 1)
        self.assertEqual(result["missing_nodes"], 0)

    def test_noncontiguous_chunk_indices_are_incomplete(self):
        node_id = self.add()
        self.embed(node_id, chunks=2)
        self.store.conn.execute("UPDATE node_vector_meta SET chunk_index=3 WHERE chunk_index=1")
        self.assertEqual(self.status()["stale_or_incomplete_nodes"], 1)

    def test_quarantine_is_issue_even_with_empty_queue(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.set_meta(vectors.EMBED_QUARANTINE_META, json.dumps({"version": 1, "items": {node_id: {"kind": "input_too_large", "message": "Synthetic input too large"}}}))
        result = self.status()
        self.assertEqual((result["queue_pending"], result["quarantined"]), (0, 1))
        self.assertEqual(result["coverage_percent"], 100)
        self.assertFalse(result["coverage_complete"])
        self.assertIn("quarantined", embedding_doctor_findings(result)[0][0])
        self.assertFalse(self.doctor()["healthy"])

    def test_pending_work_is_visible_independently_of_freshness(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.set_meta(vectors.EMBED_QUEUE_META, json.dumps([node_id]))
        result = self.status()
        self.assertEqual(result["fresh_nodes"], 1)
        self.assertFalse(result["coverage_complete"])
        self.assertIn("pending", embedding_doctor_findings(result)[1][0])

    def test_never_created_index_reports_zero_of_eligible_coverage(self):
        self.add()
        result = self.status()
        self.assertEqual(result["eligible_nodes"], 1)
        self.assertEqual(result["missing_nodes"], 1)
        self.assertEqual(result["coverage_state"], "incomplete")
        self.assertEqual(result["coverage_percent"], 0)
        self.assertEqual(result["fresh_nodes"], 0)
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["coverage_error"])
        self.assertFalse(embedding_doctor_findings(result)[0])
        self.assertIn("0/1 current (0.00%)", embedding_doctor_findings(result)[1][0])
        self.assertIn("No vector index is initialized", embedding_doctor_findings(result)[1][0])

    def test_missing_vector_table_is_not_a_successful_retained_update(self):
        self.embed(self.add())
        self.store.conn.execute("DROP TABLE node_vectors")
        result = self.status()
        self.assertEqual(result["unassessed_nodes"], 1)
        self.assertEqual(result["coverage_state"], "unavailable")
        self.assertIsNone(result["latest_retained_embedding_at"])
        self.assertFalse(result["coverage_complete"])

    def test_unavailable_backend_reports_unassessed_counts(self):
        self.embed(self.add())
        self.add("Missing")
        self.store.conn.commit()
        path = self.store.conn.execute("PRAGMA database_list").fetchone()[2]
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        readonly = SimpleNamespace(conn=connection, config=self.store.config,
                                   _row_to_dict=self.store._row_to_dict,
                                   get_meta=self.store.get_meta)
        with patch("sqlite_vec.load", side_effect=sqlite3.OperationalError("Synthetic unavailable backend")):
            result = vectors.embedding_status(readonly)
        self.assertEqual(result["coverage_state"], "unavailable")
        self.assertEqual((result["eligible_nodes"], result["missing_nodes"], result["unassessed_nodes"]), (2, 1, 1))
        self.assertIsNone(result["fresh_nodes"])
        self.assertIsNone(result["coverage_percent"])
        self.assertFalse(result["coverage_complete"])

    def test_unreadable_metadata_is_unavailable_not_missing(self):
        self.embed(self.add())
        self.store.conn.execute("ALTER TABLE node_vector_meta RENAME COLUMN text_hash TO broken_hash")
        result = self.status()
        self.assertEqual(result["coverage_state"], "unavailable")
        self.assertIsNone(result["missing_nodes"])
        self.assertEqual(result["unassessed_nodes"], 1)
        self.assertFalse(result["coverage_complete"])

    def test_latest_retained_timestamp_excludes_deleted_vector_rows_and_normalizes_utc(self):
        first = self.add("First")
        second = self.add("Second")
        self.embed(first, "2001-01-01T03:00:00+03:00")
        self.embed(second, "2099-01-01T00:00:00Z")
        self.store.conn.execute("DELETE FROM node_vectors WHERE node_id=?", (second,))
        result = self.status()
        self.assertEqual(result["latest_retained_embedding_at"], "2001-01-01T00:00:00+00:00")
        self.assertEqual(result["retained_embedding_timestamp_state"], "known")

    def test_vector_without_metadata_has_unknown_retained_timestamp(self):
        self.embed(self.add())
        self.store.conn.execute("DELETE FROM node_vector_meta")
        result = self.status()
        self.assertEqual(result["missing_nodes"], 1)
        self.assertEqual(result["coverage_percent"], 0)
        self.assertEqual(result["retained_embedding_timestamp_state"], "unknown")
        self.assertIsNone(result["latest_retained_embedding_at"])

    def test_invalid_timestamp_is_unknown_but_does_not_make_content_stale(self):
        self.embed(self.add(), "not-a-timestamp")
        result = self.status()
        self.assertTrue(result["coverage_complete"])
        self.assertIsNone(result["latest_retained_embedding_at"])
        self.assertEqual(result["retained_embedding_timestamp_state"], "unknown")
        self.assertEqual(embedding_doctor_findings(result)[0], [])
        self.assertTrue(embedding_doctor_findings(result)[1])

    def test_cheap_status_never_scans_nodes_or_loads_vector_backend(self):
        self.embed(self.add())
        with patch("kindex.vectors.select_reindex_nodes", side_effect=AssertionError("No graph scan")), \
                patch("kindex.embedding_health.embedding_coverage", side_effect=AssertionError("No coverage")), \
                patch("sqlite_vec.load", side_effect=AssertionError("No backend load")):
            result = vectors.embedding_status(self.store, coverage=False)
        self.assertIsNone(result["coverage_complete"])
        self.assertNotIn("eligible_nodes", result)
        self.assertEqual(result["indexed_nodes"], 1)

    def test_scan_loads_extension_once_not_per_node(self):
        import sqlite_vec
        for i in range(5):
            self.embed(self.add(f"Indexed {i}"))
        with patch("sqlite_vec.load", wraps=sqlite_vec.load) as load, \
                patch("kindex.vectors._node_embedding_fresh", side_effect=AssertionError("No per-node query")):
            self.assertTrue(self.status()["coverage_complete"])
        load.assert_not_called()  # Already loaded; do not reload in a caller's transaction.

    def test_diagnostics_and_doctor_fix_never_index_or_change_embedding_state(self):
        self.embed(self.add(), "2001-01-01 00:00:00")
        self.add("Missing")
        before = self.store.conn.total_changes
        snapshot = lambda: {table: [tuple(row) for row in self.store.conn.execute("SELECT * FROM " + table)]
                            for table in ("nodes", "edges", "meta", "vec_meta", "node_vectors", "node_vector_meta")}
        before_state = snapshot()
        with contextlib.ExitStack() as stack:
            for name in ("ensure_vec_table", "_embed_text_or_raise", "_embed_document_chunks", "enqueue_reindex", "drain_embedding_queue", "reindex_now"):
                stack.enter_context(patch("kindex.vectors." + name, side_effect=AssertionError("Diagnosis must not mutate/call provider")))
            result = self.status()
            self.assertEqual(self.store.conn.total_changes, before)
            output = self.doctor(fix=True)
        self.assertEqual(result["coverage_percent"], 50)
        self.assertEqual(output["embeddings"]["coverage_percent"], 50)
        self.assertEqual(output["fixes_applied"], 0)
        # Existing doctor invokes FTS5's integrity-check pseudo-INSERT. It is
        # counted as a change by SQLite but changes no graph or embedding state.
        self.assertEqual(snapshot(), before_state)
        self.http_call.assert_not_called()

    def test_status_operates_on_read_only_connection_without_persistent_changes(self):
        self.embed(self.add())
        self.store.conn.commit()
        db = self.root / "graph" / "kindex.db"
        # Find the actual synthetic store filename, not any user's database.
        if not db.exists():
            db = self.root / "graph" / "conv.db"
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        readonly = SimpleNamespace(conn=connection, config=self.store.config,
                                   _row_to_dict=self.store._row_to_dict,
                                   get_meta=lambda key: (connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone() or [None])[0])
        result = vectors.embedding_status(readonly)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(connection.total_changes, 0)

    def test_doctor_text_includes_counts_work_identity_and_timestamp_scope(self):
        self.embed(self.add(), "2001-01-01T00:00:00Z")
        output = self.doctor(json_output=False)
        self.assertIn("Embedding coverage: 1/1 current (100.00%)", output)
        self.assertIn("0 queued, 0 quarantined", output)
        self.assertIn("fingerprint (current)", output)
        self.assertIn("fingerprint (stored)", output)
        self.assertIn("Latest retained embedding update: 2001", output)
        self.assertIn("not attempt history", output)
        self.assertNotIn("No embeddings in 24", output)

    def test_embed_status_text_includes_coverage_and_mismatch_details(self):
        node_id = self.add()
        self.embed(node_id)
        self.store.config.embedding.model = "different-model"
        args = SimpleNamespace(embed_action="status", data_dir=str(self.root / "graph"), json=False)
        output = io.StringIO()
        with patch("kindex.cli._store", return_value=self.store), \
                patch.object(self.store, "close"), contextlib.redirect_stdout(output):
            cli.cmd_embed(args)
        text = output.getvalue()
        self.assertIn("Embedding coverage:", text)
        self.assertIn("Embedding fingerprint (stored):", text)
        self.assertIn("Embedding fingerprint (current):", text)
        self.assertIn("different-model", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
