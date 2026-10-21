"""CLI/MCP host parity, supplied-context rejection and read-only behavior."""
import asyncio
import copy
import json
import os
from contextlib import ExitStack
from unittest import mock

from . import reference_support as s


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


class HostSurfaces(s.Fixture):
    def host_context(self):
        stack = ExitStack()
        environment = {"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef", "KIN_PROJECT": self.project}
        stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
        from kindex import config
        selected = self.config.model_copy(deep=True)
        selected._project_path = s.Path(self.project)
        stack.enter_context(mock.patch.object(config, "load_config", return_value=selected))
        stack.enter_context(mock.patch.object(config, "resolve_project_root", return_value=s.Path(self.project)))
        return stack, selected

    def test_mcp_registered_tools_match_config_only_host_reads(self):
        from kindex import practical_memory_host as host
        from kindex import mcp_server as server
        context, selected = self.host_context()
        before = self.database()
        with context, mock.patch.object(server, "_config", selected), mock.patch.object(server, "_store", None), \
                mock.patch.object(server, "_get_store", side_effect=AssertionError("Read initialized native Store")):
            tools = asyncio.run(server.mcp.list_tools())
            self.assertTrue({"practical_memory", "correction_references"} <= {tool.name for tool in tools})
            request = self.expand_request()
            expected = host.run_correction_references(selected, "expand", request, project_path=self.project)
            result = decode(server.correction_references("expand", s.J(request)))
            self.assertEqual(result, expected)
            self.assertEqual(result["schema"], "evidence-result/1")
            for encoding in ("full", "lossless_common"):
                explicit = decode(server.correction_references("expand", s.J(request), metadata_encoding=encoding, incoming_later=False))
                direct = host.run_correction_references(selected, "expand", request, project_path=self.project, metadata_encoding=encoding, incoming_later=False)
                self.assertEqual(explicit, direct)
            organized_request = {"scope": {"graph_id": self.graph, "profile": "", "project_path": self.project,
                "client": "codex", "session_id": self.session}, "evaluation_time": "2026-10-20T00:00:00.125000Z", "query": "quartz",
                "top_k": 10, "protected_node_ids": [], "total_max_bytes": 16384, "order": "native"}
            organized = decode(server.practical_memory("organize", s.J(organized_request)))
            expected = host.run_practical_memory(selected, "organize", organized_request, project_path=self.project)
            self.assertEqual(organized, expected)
        self.assertEqual(self.database(), before, "MCP read wrote telemetry, graph, registry or profile state")

    def test_mcp_rejects_ambiguous_json_and_request_supplied_authority_without_writes(self):
        from kindex import mcp_server as server
        context, selected = self.host_context()
        before = self.database()
        forbidden = "foreign-private-value-do-not-return"
        extra_context = dict(self.expand_request(), context={"actor_id": forbidden, "partitions": []})
        bodies = [s.J(extra_context), '{"schema":"expand-request/1","schema":"duplicate"}',
                  '{"schema":NaN}', '{"schema":Infinity}', '[]']
        with context, mock.patch.object(server, "_config", selected), mock.patch.object(server, "_store", None), \
                mock.patch.object(server, "_get_store", side_effect=AssertionError("Malformed read initialized Store")):
            for raw in bodies:
                with self.subTest(request=raw):
                    result = decode(server.correction_references("expand", raw))
                    self.assertIsInstance(result, dict)
                    self.assertIn("error", result)
                    self.assertNotIn(forbidden, s.J(result))
            result = decode(server.correction_references("expand", s.J(self.expand_request()), metadata_encoding="unknown"))
            self.assertIn("error", result)
        self.assertEqual(self.database(), before)

    def test_mcp_missing_store_read_does_not_initialize_database(self):
        from kindex import mcp_server as server
        context, selected = self.host_context()
        selected.data_dir = str(self.root / "never-initialized")
        with context, mock.patch.object(server, "_config", selected), mock.patch.object(server, "_store", None), \
                mock.patch.object(server, "_get_store", side_effect=AssertionError("Read initialized Store")):
            result = decode(server.correction_references("reference", s.J(self.resolve_request())))
            self.assertIn("error", result)
        self.assertFalse(s.Path(selected.data_dir).exists())


    def test_shared_host_cannot_take_session_or_client_authority_from_request(self):
        from kindex import practical_memory_host as host
        from kindex import mcp_server as server
        from kindex.practical_memory import PracticalMemoryError
        alternate = self.store.add_node("Other active session", node_id="other-active-session", node_type="session",
            extra={"project_path": self.project, "session_status": "active"}, queue_embedding=False)
        request = {"scope": {"graph_id": self.graph, "profile": "", "project_path": self.project,
            "client": "codex", "session_id": self.session}, "evaluation_time": "2026-10-20T00:00:00.125000Z", "query": "quartz",
            "top_k": 10, "protected_node_ids": [], "total_max_bytes": 16384, "order": "native"}
        selected = self.config.model_copy(deep=True)
        selected._project_path = s.Path(self.project)
        correct_env = {"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": "0192f123-4567-7000-8123-456789abcdef"}
        before = self.database()
        with mock.patch.dict(os.environ, correct_env, clear=True):
            positive = host.run_practical_memory(selected, "organize", request, project_path=self.project)
            self.assertNotIn("error", positive)
        mismatches = []
        for key, value in (("session_id", alternate), ("client", "claude")):
            forged = copy.deepcopy(request)
            forged["scope"][key] = value
            mismatches.append((correct_env, forged))
        mismatches.extend([
            ({}, request),
            ({"CODEX_THREAD_ID": self.session}, request),
            ({"CLAUDE_SESSION_ID": self.session}, request),
            ({"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": "provider-thread", "KIN_CLIENT": "claude"}, request),
            ({"KIN_SESSION_ID": self.session}, request),
            ({"KIN_SESSION_ID": self.session, "CODEX_THREAD_ID": "codex-host", "CLAUDE_SESSION_ID": "claude-host"}, request),
        ])
        for environment, candidate in mismatches:
            with self.subTest(environment=environment, scope=candidate["scope"]), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(server, "_config", selected), \
                    mock.patch.object(server, "_store", None), \
                    mock.patch.object(server, "_get_store", side_effect=AssertionError("Rejected host request initialized Store")):
                with self.assertRaises(PracticalMemoryError):
                    host.run_practical_memory(selected, "organize", candidate, project_path=self.project)
                result = decode(server.practical_memory("organize", s.J(candidate)))
                self.assertIn("error", result)
                self.assertNotIn(self.store.peek_node(self.old)["content"], s.J(result))
        self.assertEqual(self.database(), before)


    def test_fastmcp_dispatch_rejects_coerced_options_and_unknown_arguments(self):
        from mcp.server.fastmcp.exceptions import ToolError
        from kindex import mcp_server as server
        context, selected = self.host_context()
        before = self.database()
        valid = {"operation": "expand", "request_json": s.J(self.expand_request(mode="direct"))}
        invalid = [dict(valid, incoming_later=value) for value in ("false", "true", "0", 0, 1)]
        invalid.extend([
            dict(valid, unknown_argument="must-not-be-silently-dropped"),
            dict(valid, context={"actor_id": "forged-authority"}),
        ])
        with context, mock.patch.object(server, "_config", selected), mock.patch.object(server, "_store", None), \
                mock.patch.object(server, "_get_store", side_effect=AssertionError("Transport validation initialized Store")):
            # First prove this name is registered and ordinary transport works.
            positive = asyncio.run(server.mcp.call_tool("correction_references", dict(valid, incoming_later=False)))
            self.assertFalse(getattr(positive, "isError", False))
            self.assertIn("evidence-result/1", str(positive))
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    try:
                        result = asyncio.run(server.mcp.call_tool("correction_references", arguments))
                    except ToolError:
                        pass
                    else:
                        is_error = result.get("isError", False) if isinstance(result, dict) else getattr(result, "isError", False)
                        self.assertTrue(is_error, "FastMCP accepted malformed or unknown top-level arguments")
                    self.assertEqual(self.database(), before)
        self.assertEqual(self.database(), before)
