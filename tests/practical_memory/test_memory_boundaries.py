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

from . import test_memory_contract as core


class PracticalMemoryBoundaries(core.PracticalMemoryContract):
    def test_40_index_missing_current_pointer_cannot_recreate_with_null_cas(self):
        request, _ = self.index_request()
        core.api.index(self.store, request)
        raw = self.store.conn.execute("SELECT extra FROM nodes WHERE id = ?", (self.node,)).fetchone()[0]
        corrupted = json.loads(raw)
        # Authorized synthetic persisted-state adapter. Delete only the current
        # assertion pointer, preserving its existing operation/history records.
        self.assertIn("assertion", corrupted["practical_index_v1"]["current"])
        del corrupted["practical_index_v1"]["current"]["assertion"]
        self.store.conn.execute("UPDATE nodes SET extra = ? WHERE id = ?",
            (json.dumps(corrupted, ensure_ascii=False, separators=(",", ":")), self.node))
        self.store.conn.commit()
        before = self.snapshot()
        attempted_recreation = copy.deepcopy(request)
        attempted_recreation["operation_id"] = "index-recreate-after-corruption"
        self.assertIsNone(attempted_recreation["expected_index_sha256"])
        with self.assertRaises(core.api.PracticalMemoryError) as raised:
            core.api.index(self.store, attempted_recreation)
        self.assertIn(raised.exception.code, {"invalid_request", "scope_mismatch", "source_changed",
            "metadata_changed", "invalid_span", "invalid_group", "index_conflict", "operation_conflict",
            "capacity", "transaction_conflict", "unavailable", "privacy_unavailable"})
        self.assertNotIn(core.BODY, str(raised.exception))
        self.assertEqual(self.snapshot(), before, "Malformed pointer/history state must not be repaired or overwritten")
        after = json.loads(self.store.conn.execute("SELECT extra FROM nodes WHERE id = ?", (self.node,)).fetchone()[0])
        self.assertEqual(after, corrupted)

    @mock.patch.object(importlib_metadata, "entry_points", return_value=importlib_metadata.EntryPoints())
    def test_41_cli_explicit_nested_project_never_widens_to_git_root(self, _empty_entry_points):
        # The public distribution-discovery seam is replaced before native CLI
        # import and remains replaced through parser construction/invocation.
        # This fixture declares no third-party plugins; it does not mock the
        # practical-memory command, scope checks, native Store or serialization.
        from kindex import cli, config, logstamp

        parent = self.run_root / "synthetic-project"
        nested = parent / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        explicit_config_path = self.run_root / "boundary-explicit-config.json"
        request_path = self.run_root / "boundary-request.json"
        fixture_config = self.store.config.model_copy(deep=True)
        # The file is explicit synthetic input. The mocked public loader below
        # prevents ambient layering, process-wide secret lookup and Git probing.
        explicit_config_path.write_text(fixture_config.model_dump_json(), encoding="utf-8")
        nested_session = self.store.add_node("nested-session", node_id="session-nested",
            node_type="session", extra={"project_path": str(nested), "tag": "nested-session",
            "session_status": "active"}, queue_embedding=False)
        nested_source = self.add_source("quartz-nested", "Quartz nested project evidence.", project=str(nested))

        def explicit_fixture_loader(config_path=None, project_path=None, profile=None, data_dir=None):
            self.assertEqual(str(config_path), str(explicit_config_path))
            self.assertEqual(str(project_path), str(nested))
            self.assertIsNone(profile)
            self.assertIsNone(data_dir)
            result = fixture_config.model_copy(deep=True)
            # Emulate native config discovery resolving an explicitly supplied
            # nested path to its enclosing Git root, without probing Git at all.
            result._project_path = config.resolve_project_root(project_path)
            self.assertEqual(result._project_path, parent)
            return result

        def invoke(request):
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            argv = ["kin", "practical-memory", "prepare", "--request-json", str(request_path),
                    "--json", "--config", str(explicit_config_path), "--project-path", str(nested)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(config, "load_config", side_effect=explicit_fixture_loader), \
                 mock.patch.object(config, "resolve_project_root", return_value=parent), \
                 mock.patch.object(logstamp, "install_from_env", return_value=None), \
                 mock.patch.dict(os.environ, {"KIN_SESSION_ID": nested_session, "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef"}, clear=True), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sys, "excepthook", sys.excepthook), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    returned = cli.main()
                except SystemExit as exc:
                    code = exc.code
                else:
                    code = 0 if returned is None else returned
            return code, stdout.getvalue(), stderr.getvalue()

        before = self.snapshot()
        # Existing owner/source at the parent must not become the receiver merely
        # because configuration discovery reports the enclosing Git root.
        code, stdout, stderr = invoke(self.prepare_request())
        self.assertEqual(code, 2)
        error = json.loads(stdout if stdout.strip() else stderr)
        self.assertIsInstance(error, dict)
        self.assertNotIn(core.BODY, stdout + stderr)
        self.assertNotIn(self.node, stdout + stderr)
        self.assertEqual(self.snapshot(), before)

        # A positive exact-nested case prevents satisfying the negative test by
        # rejecting every invocation with a nested explicit selector.
        nested_request = self.prepare_request()
        nested_request["scope"]["project_path"] = str(nested)
        nested_request["scope"]["session_id"] = nested_session
        code, stdout, stderr = invoke(nested_request)
        self.assertEqual(code, 0, stderr)
        prepared = json.loads(stdout)
        self.assert_prepared(prepared, nested_request)
        self.assertEqual([entry["source"]["node_id"] for entry in prepared["components"]["sources"]["entries"]],
                         [nested_source])
        self.assertEqual(self.snapshot(), before)


def load_tests(loader, tests, pattern):
    names = [name for name in loader.getTestCaseNames(PracticalMemoryBoundaries)
             if int(name.split("_")[1]) >= 40]
    return unittest.TestSuite(PracticalMemoryBoundaries(name) for name in names)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Avoid rerunning base contracts through unittest subclass discovery.
for _name in core.PracticalMemoryContract.__dict__:
    if _name.startswith("test_") and _name not in PracticalMemoryBoundaries.__dict__:
        setattr(PracticalMemoryBoundaries, _name, None)
