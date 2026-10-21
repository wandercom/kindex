"""Synthetic contracts ported from the frozen practical-memory campaign."""
import contextlib
import importlib.metadata as importlib_metadata
import io
import json
import os
import sys
import unittest
from unittest import mock

from . import native_support as n


class CLIContract(n.NativeFixture):
    @mock.patch.object(importlib_metadata, "entry_points", return_value=importlib_metadata.EntryPoints())
    def test_explicit_nested_owner_and_read_only_organize_path(self, _no_plugins):
        from kindex import cli, config, logstamp

        parent = self.run_root / "synthetic-project"
        nested = parent / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        config_path = self.run_root / "v7-explicit-config.json"
        request_path = self.run_root / "v7-organize-request.json"
        fixture_config = self.store.config.model_copy(deep=True)
        config_path.write_text(fixture_config.model_dump_json(), encoding="utf-8")
        session = self.store.add_node("nested-session", node_id="nested-session", node_type="session",
            extra={"project_path": str(nested), "tag": "nested-session", "session_status": "active"}, queue_embedding=False)
        source = self.add_source("quartz-nested", "Quartz nested evidence; except frozen releases.", project=str(nested))

        def load_explicit(config_path=None, project_path=None, profile=None, data_dir=None):
            self.assertEqual(str(config_path), str(self.run_root / "v7-explicit-config.json"))
            self.assertEqual(str(project_path), str(nested))
            self.assertIsNone(profile)
            self.assertIsNone(data_dir)
            result = fixture_config.model_copy(deep=True)
            # Public config discovery may widen to the enclosing Git root. The
            # action must retain the explicit selector. No actual Git probing.
            result._project_path = config.resolve_project_root(project_path)
            self.assertEqual(result._project_path, parent)
            return result

        def invoke(req):
            request_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
            argv = ["kin", "practical-memory", "organize", "--request-json", str(request_path),
                    "--json", "--config", str(config_path), "--project-path", str(nested)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(config, "load_config", side_effect=load_explicit), \
                 mock.patch.object(config, "resolve_project_root", return_value=parent), \
                 mock.patch.object(logstamp, "install_from_env", return_value=None), \
                 mock.patch.dict(os.environ, {"KIN_SESSION_ID": session, "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef"}, clear=True), \
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
        # Parent ownership is not authorization for the explicitly selected child.
        code, stdout, stderr = invoke(self.request())
        self.assertEqual(code, 2)
        self.assertIsInstance(json.loads(stdout if stdout.strip() else stderr), dict)
        self.assertNotIn(n.BODY, stdout + stderr)
        self.assertNotIn(self.node, stdout + stderr)
        self.assertEqual(self.snapshot(), before)

        req = self.request()
        req["scope"].update(project_path=str(nested), session_id=session)
        n.SEARCH_CALLS.clear()
        code, stdout, stderr = invoke(req)
        self.assertEqual(code, 0, stderr)
        out = json.loads(stdout)
        self.assert_native_result(out, req)
        self.assert_search(req, n.SEARCH_CALLS)
        self.assertEqual(out["admitted_ids"], [source])
        self.assertEqual([row["source"]["node_id"] for row in self.decoded(out)], [source])
        self.assertEqual(self.snapshot(), before, "CLI organize must not write access, audit, delivery or application state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
