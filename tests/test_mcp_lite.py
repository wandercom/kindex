"""Independent acceptance contract for a repo-bound Kindex Lite MCP server.

Exercise FastMCP's public call_tool/list_tools boundary, not implementation
helpers. All graphs, config files and HOME paths are disposable fixtures.
"""

import asyncio
import importlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp.server.fastmcp.exceptions import ToolError

from kindex.config import Config
from kindex.store import Store


def _server(repo, **capabilities):
    return importlib.import_module("kindex.mcp_lite").create_server(repo, **capabilities)


def _call(server, tool_name, **arguments):
    try:
        result = asyncio.run(server.call_tool(tool_name, arguments))
    except ToolError as exc:
        return f"MCP tool error: {exc}"
    if isinstance(result, tuple):
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    return "\n".join(block.text for block in result if hasattr(block, "text"))


def _ref(output):
    match = re.search(r"(?:Created (?:node|task): |id=)([\w:-]+)", output)
    assert match, output
    return match.group(1)


def _refused(output):
    assert any(word in output.lower() for word in (
        "error", "refus", "denied", "not allowed", "not found", "invalid",
        "outside", "unsupported", "unavailable", "disabled", "unknown tool",
        "stale graph reference",
    )), output


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import kindex.config as config_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("KIN_AGENT_ID", "lite-acceptance")
    for key in ("KIN_PROFILE", "KIN_PROJECT", "KIN_PROJECT_PATH", "KIN_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    for root in (repo, sibling):
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True,
                       capture_output=True)
        (root / ".kin").mkdir()
    local_dir = repo / ".kin/local/kindex"
    home_dir = home / ".kindex"
    sibling_dir = sibling / ".kin/local/kindex"
    stores = [Store(Config(data_dir=str(path)))
              for path in (local_dir, home_dir, sibling_dir)]
    local, outer, other = stores
    local.add_node("Local boundaryneedle", content="local payload boundaryneedle",
                   node_id="111111111111")
    outer.add_node("Home boundaryneedle", content="HOME_PRIVATE_SENTINEL boundaryneedle",
                   node_id="222222222222")
    other.add_node("Sibling boundaryneedle", content="SIBLING_PRIVATE_SENTINEL boundaryneedle",
                   node_id="333333333333")
    user_config = home / "kin.yaml"
    user_config.write_text(f"data_dir: {home_dir}\nprofiles:\n  private:\n    data_dir: {home_dir}\n")
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [user_config])
    monkeypatch.chdir(repo)
    yield dict(repo=repo, sibling=sibling, home=home, local=local,
               outer=outer, other=other, local_dir=local_dir, home_dir=home_dir,
               user_config=user_config)
    for store in stores:
        store.close()


def test_discovery_exposes_local_workflow_but_no_execution_surface(isolated):
    server = _server(isolated["repo"])
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert {
        "search", "add", "show", "context", "list_nodes", "status",
        "tag_start", "tag_update", "tag_resume", "task_add", "task_list", "task_done",
        "coord_start", "coord_post", "coord_read", "kinbase_sync", "kinbase_explain",
    } <= tools.keys()
    assert {"ingest", "remind_exec"}.isdisjoint(tools)
    assert {"kinbase_submit", "kinbase_status"}.isdisjoint(tools)
    # New full-server tools must receive a deliberate capability decision.
    allowed = {
        "scope_info", "search", "add", "show", "context", "list_nodes", "status",
        "edit", "link", "changelog", "learn", "supersede", "verify", "invalidate",
        "tag_start", "tag_update", "tag_resume",
        "task_add", "task_list", "task_done", "task_get", "task_update", "task_cancel",
        "task_claim", "task_release", "lock_acquire", "lock_release",
        "coord_start", "coord_join", "coord_post", "coord_read", "coord_attach",
        "coord_inject", "coord_list", "coord_end",
        "kinbase_sync", "kinbase_status", "kinbase_explain", "kinbase_submit",
    }
    assert tools.keys() <= allowed
    if "remind_create" in tools:
        assert {"action", "wake", "instructions", "cwd"}.isdisjoint(
            tools["remind_create"].inputSchema.get("properties", {}))
    _refused(_call(server, "ingest", source=str(isolated["home"])))
    _refused(_call(server, "remind_exec", id="anything"))


def test_positive_local_read_write_and_exclusion(isolated):
    server = _server(isolated["repo"])
    created = _call(server, "add", text="Local accepted capture boundaryneedle")
    node_ref = _ref(created)
    assert isolated["local"].get_node_by_title("Local accepted capture boundaryneedle")
    assert "Local accepted capture" in _call(server, "show", node_id=node_ref)
    assert "Local boundaryneedle" in _call(server, "show", node_id="111111111111")
    for name, args in (
        ("search", {"query": "boundaryneedle"}),
        ("context", {"topic": "boundaryneedle"}),
        ("list_nodes", {}),
        ("status", {}),
    ):
        output = _call(server, name, **args)
        assert "HOME_PRIVATE_SENTINEL" not in output
        assert "SIBLING_PRIVATE_SENTINEL" not in output
        assert "Home boundaryneedle" not in output
        assert "Sibling boundaryneedle" not in output
        assert "Global graph" not in output
        if name != "status":
            assert "Local" in output, output
    assert "Nodes: 2" in _call(server, "status")
    for foreign_id in ("222222222222", "333333333333"):
        _refused(_call(server, "show", node_id=foreign_id))
    assert not (isolated["home"] / ".config/kindex").exists()


def test_existing_legacy_local_layout_is_used(isolated, tmp_path):
    repo = tmp_path / "legacy"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    store = Store(Config(data_dir=str(repo / ".kin/local")))
    try:
        store.add_node("Legacy local sentinel", content="legacyboundaryneedle")
        server = _server(repo)
        assert "Legacy local sentinel" in _call(server, "search", query="legacyboundaryneedle")
        _ref(_call(server, "add", text="Legacy local write"))
        assert store.get_node_by_title("Legacy local write")
        assert not (repo / ".kin/local/kindex/kindex.db").exists()
    finally:
        store.close()


@pytest.mark.parametrize("name,args", [
    ("search", {"query": "boundaryneedle"}), ("context", {"topic": "boundaryneedle"}),
    ("status", {}), ("list_nodes", {}), ("task_list", {}),
    ("add", {"text": "Must never be captured"}),
    ("task_add", {"text": "Must never be captured"}),
])
def test_global_scope_is_refused_for_reads_and_writes(isolated, name, args):
    output = _call(_server(isolated["repo"]), name, graph="global", **args)
    _refused(output)
    assert "HOME_PRIVATE_SENTINEL" not in output
    for key in ("local", "outer", "other"):
        assert isolated[key].get_node_by_title("Must never be captured") is None


@pytest.mark.parametrize("foreign_ref", [
    "global:deadbeef:222222222222", "project:deadbeef:111111111111",
    "global:deadbeef:111111111111",
])
def test_foreign_and_stale_qualified_references_do_not_route(isolated, foreign_ref):
    server = _server(isolated["repo"])
    _refused(_call(server, "show", node_id=foreign_ref))
    _refused(_call(server, "add", text="Forbidden derived capture", source_refs=foreign_ref))
    _refused(_call(server, "task_add", text="Forbidden linked task", link_to=foreign_ref))
    for key in ("local", "outer", "other"):
        assert isolated[key].get_node_by_title("Forbidden derived capture") is None
        assert isolated[key].get_node_by_title("Forbidden linked task") is None


def test_tasks_sessions_and_coordination_remain_usable(isolated):
    server = _server(isolated["repo"])
    assert "Started" in _call(server, "tag_start", name="local-work", focus="verify scope")
    assert "Updated" in _call(server, "tag_update", name="local-work", focus="test lifecycle")
    assert "local-work" in _call(server, "tag_resume", name="local-work")
    task = _ref(_call(server, "task_add", text="Local acceptance task"))
    assert "Local acceptance task" in _call(server, "task_list")
    assert "Completed" in _call(server, "task_done", id=task)
    assert isolated["local"].get_node_by_title("Local acceptance task")["extra"]["task_status"] == "done"
    assert "Started" in _call(server, "coord_start", name="local-coordination", agent="tester")
    assert "Posted" in _call(server, "coord_post", conversation="local-coordination",
                             agent="tester", message="LOCAL_COORDINATION_SENTINEL")
    assert "LOCAL_COORDINATION_SENTINEL" in _call(
        server, "coord_read", conversation="local-coordination", since_id=0)
    assert "Completed" in _call(server, "tag_update", name="local-work", action="end",
                                summary="Verified local lifecycle")
    for key in ("outer", "other"):
        assert isolated[key].get_node_by_title("Local acceptance task") is None


def test_ambient_profile_project_and_data_directory_cannot_change_binding(isolated, monkeypatch):
    monkeypatch.setenv("KIN_PROFILE", "private")
    monkeypatch.setenv("KIN_PROJECT", str(isolated["sibling"]))
    monkeypatch.setenv("KIN_PROJECT_PATH", str(isolated["sibling"]))
    monkeypatch.setenv("KIN_DATA_DIR", str(isolated["home_dir"]))
    monkeypatch.chdir(isolated["sibling"])
    server = _server(isolated["repo"])
    output = _call(server, "search", query="boundaryneedle")
    assert "Local boundaryneedle" in output
    assert "Home boundaryneedle" not in output
    assert "Sibling boundaryneedle" not in output
    _ref(_call(server, "add", text="Ambient selection ignored"))
    assert isolated["local"].get_node_by_title("Ambient selection ignored")
    assert isolated["outer"].get_node_by_title("Ambient selection ignored") is None
    assert isolated["other"].get_node_by_title("Ambient selection ignored") is None


@pytest.mark.parametrize("setting", ["data_dir", "profile", "extends"])
def test_repo_config_cannot_redirect_outside_canonical_store(isolated, setting):
    values = {"data_dir": str(isolated["home_dir"]), "profile": "private",
              "extends": str(isolated["user_config"])}
    (isolated["repo"] / ".kin/config").write_text(f"{setting}: {values[setting]}\n")
    try:
        server = _server(isolated["repo"])
    except (ValueError, OSError, RuntimeError):
        return  # Rejecting an unsafe config at startup is also fail-closed.
    output = _call(server, "search", query="boundaryneedle")
    assert "Home boundaryneedle" not in output
    assert "Sibling boundaryneedle" not in output
    created = _call(server, "add", text="Config cannot redirect this capture")
    if "Created node:" in created:
        assert isolated["local"].get_node_by_title("Config cannot redirect this capture")
    else:
        _refused(created)
    assert isolated["outer"].get_node_by_title("Config cannot redirect this capture") is None


@pytest.mark.parametrize("redirect", [".kin", ".kin/local", ".kin/local/kindex",
                                       ".kin/local/kindex/kindex.db"])
def test_symlinked_store_escape_is_rejected(isolated, tmp_path, redirect):
    repo = tmp_path / "redirected"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    link = repo / redirect
    link.parent.mkdir(parents=True, exist_ok=True)
    target = isolated["outer"].db_path if redirect.endswith(".db") else isolated["home_dir"]
    link.symlink_to(target, target_is_directory=not redirect.endswith(".db"))
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _server(repo)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_repo_refuses_instead_of_ambient_fallback(isolated, tmp_path, kind):
    invalid = tmp_path / ("invalid-" + kind)
    if kind == "file":
        invalid.write_text("not a repository")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _server(invalid)


def test_existing_empty_directory_initializes_a_fresh_local_scope(isolated, tmp_path):
    repo = tmp_path / "fresh-scope"
    repo.mkdir()
    server = _server(repo)
    ref = _ref(_call(server, "add", text="Fresh local graph capture"))
    assert "Fresh local graph capture" in _call(server, "show", node_id=ref)
    databases = list((repo / ".kin/local").rglob("kindex.db"))
    assert len(databases) == 1
    assert isolated["outer"].get_node_by_title("Fresh local graph capture") is None
    assert not (isolated["home"] / ".config/kindex").exists()


def test_url_shaped_file_referent_cannot_bypass_file_scope_guard(isolated, tmp_path, monkeypatch):
    import kindex.referent as referent

    (tmp_path / "outside-secret").write_text("OUTSIDE_FILE_SECRET")
    hashed = []

    def record_hash(path, *args, **kwargs):
        hashed.append(path)
        return "ab" * 32

    monkeypatch.setattr(referent, "hash_file", record_hash)
    output = _call(_server(isolated["repo"]), "add", text="Forbidden file binding",
                   referent="http://../../outside-secret", referent_scope="file")
    _refused(output)
    assert hashed == [], "Outside file reached hash_file before scope refusal"
    assert isolated["local"].get_node_by_title("Forbidden file binding") is None


@pytest.mark.parametrize("replacement", ["root_symlink", "root_directory", "local_symlink", "db_symlink"])
def test_scope_replacement_during_server_lifetime_fails_closed(isolated, tmp_path, replacement):
    repo = isolated["repo"]
    server = _server(repo)
    assert "Local boundaryneedle" in _call(server, "search", query="boundaryneedle")
    if replacement.startswith("root_"):
        repo.rename(tmp_path / "original-repo")
        if replacement == "root_symlink":
            repo.symlink_to(isolated["sibling"], target_is_directory=True)
        else:
            repo.mkdir()
            (repo / ".kin").mkdir()
    elif replacement == "local_symlink":
        local = repo / ".kin/local"
        local.rename(repo / ".kin/original-local")
        local.symlink_to(isolated["home_dir"], target_is_directory=True)
    else:
        database = isolated["local"].db_path
        database.rename(database.with_name("original-kindex.db"))
        database.symlink_to(isolated["outer"].db_path)
    _refused(_call(server, "search", query="boundaryneedle"))
    _refused(_call(server, "add", text="Replacement must not receive capture"))
    for key in ("local", "outer", "other"):
        assert isolated[key].get_node_by_title("Replacement must not receive capture") is None


@pytest.mark.parametrize("database_name", ["kindex.db", "conv.db"])
def test_hardlinked_database_escape_is_rejected(isolated, tmp_path, database_name):
    repo = tmp_path / ("hardlink-" + database_name)
    directory = repo / ".kin/local/kindex"
    directory.mkdir(parents=True)
    (directory / database_name).hardlink_to(isolated["outer"].db_path)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _server(repo)


def test_ambient_provider_credentials_do_not_enable_network_calls(isolated, monkeypatch):
    import ipaddress
    import socket
    import urllib.request

    import httpx
    import kindex.vectors as vectors

    calls = []

    def forbidden(*args, **kwargs):
        calls.append("external provider attempt")
        raise AssertionError("Lite graph operation attempted an external provider")

    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VOYAGE_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(key, "fixture-provider-credential")
    real_connect = socket.socket.connect

    def loopback_or_forbidden(sock, address, *args, **kwargs):
        # Windows' asyncio builds its self-pipe with a loopback connect; that is
        # no provider. Every other address is.
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and ipaddress.ip_address(host).is_loopback:
            return real_connect(sock, address, *args, **kwargs)
        return forbidden(sock, address, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", loopback_or_forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(vectors, "_embed_voyage", forbidden)
    server = _server(isolated["repo"])
    _ref(_call(server, "add", text="Provider-free local capture boundaryneedle"))
    assert "Local boundaryneedle" in _call(server, "search", query="boundaryneedle")
    assert "Local" in _call(server, "context", topic="boundaryneedle")
    _call(server, "learn", text="The local boundaryneedle design confines this graph to its repository.")
    _call(server, "tag_start", name="provider-free")
    _call(server, "tag_update", name="provider-free", action="end", summary="local work complete")
    assert calls == []


def test_two_live_servers_do_not_share_binding(isolated):
    first = _server(isolated["repo"])
    second = _server(isolated["sibling"])
    first_ref = _ref(_call(first, "add", text="First server isolated write"))
    second_ref = _ref(_call(second, "add", text="Second server isolated write"))
    assert "First server isolated write" in _call(first, "show", node_id=first_ref)
    assert "Second server isolated write" in _call(second, "show", node_id=second_ref)
    _refused(_call(second, "show", node_id=first_ref))
    _refused(_call(first, "show", node_id=second_ref))
    _ref(_call(first, "add", text="First server subsequent write"))
    assert isolated["local"].get_node_by_title("First server subsequent write")
    assert isolated["other"].get_node_by_title("First server subsequent write") is None
    assert isolated["local"].get_node_by_title("Second server isolated write") is None


@pytest.mark.parametrize("name", ["kinbase_sync", "kinbase_status", "kinbase_explain"])
def test_kinbase_target_is_bound_before_bridge_invocation(isolated, monkeypatch, name):
    import kindex.kinbase as bridge

    calls = []

    def record(*args, **kwargs):
        repo = args[1] if name == "kinbase_sync" else args[0]
        calls.append(Path(repo).resolve())
        if name == "kinbase_sync":
            assert args[0].db_path.resolve().is_relative_to(isolated["repo"].resolve())
        return {"ok": True, "bound_marker": "LOCAL_KINBASE_REPLY"}

    functions = {"kinbase_sync": "sync_kinbase", "kinbase_status": "read_status",
                 "kinbase_explain": "read_explain"}
    monkeypatch.setattr(bridge, functions[name], record)
    server = _server(isolated["repo"], allow_kinbase_submit=name == "kinbase_status")
    arguments = {"logical_key": "symbol:scope", "decision": "inspect"} if name == "kinbase_explain" else {}
    assert "LOCAL_KINBASE_REPLY" in _call(server, name, repo=str(isolated["repo"]), **arguments)
    assert calls == [isolated["repo"].resolve()]
    for foreign in (isolated["sibling"], isolated["home"], isolated["repo"] / ".." / "sibling"):
        _refused(_call(server, name, repo=str(foreign), **arguments))
    assert calls == [isolated["repo"].resolve()]


def test_full_mcp_keeps_its_separate_surface(isolated, monkeypatch):
    import kindex.mcp_server as full

    cfg = Config(data_dir=str(isolated["local_dir"]))
    cfg._project_path = isolated["repo"]
    cfg._global_data_dir = str(isolated["home_dir"])
    monkeypatch.setattr(full, "_store", isolated["local"])
    monkeypatch.setattr(full, "_config", cfg)
    before = {tool.name for tool in asyncio.run(full.mcp.list_tools())}
    _server(isolated["repo"])
    after = {tool.name for tool in asyncio.run(full.mcp.list_tools())}
    assert after == before
    assert {"ingest", "remind_exec"} <= after
    assert "Home boundaryneedle" in _call(full.mcp, "search", query="boundaryneedle", graph="global")


def test_unknown_full_server_tool_is_not_automatically_exposed(isolated):
    import kindex.mcp_server as full

    invocations = []

    def future_unreviewed_tool() -> str:
        invocations.append(True)
        return "UNREVIEWED_CAPABILITY"

    full.mcp.add_tool(future_unreviewed_tool)
    try:
        server = _server(isolated["repo"])
        assert "future_unreviewed_tool" not in {
            tool.name for tool in asyncio.run(server.list_tools())}
        _refused(_call(server, "future_unreviewed_tool"))
        assert invocations == []
    finally:
        full.mcp.remove_tool("future_unreviewed_tool")


@pytest.mark.parametrize("capabilities,expected", [
    ({}, {"kinbase_sync", "kinbase_explain"}),
    ({"allow_kinbase_submit": True},
     {"kinbase_sync", "kinbase_explain", "kinbase_submit", "kinbase_status"}),
    ({"no_kinbase": True}, set()),
])
def test_kinbase_capability_modes_are_fixed_at_server_creation(isolated, capabilities, expected):
    server = _server(isolated["repo"], **capabilities)
    advertised = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {name for name in advertised if name.startswith("kinbase_")} == expected
    assert {"search", "add", "show", "context", "task_add"} <= advertised


@pytest.mark.parametrize("capabilities", [{}, {"no_kinbase": True}])
def test_omitted_kinbase_write_tools_cannot_be_invoked_or_enabled_by_arguments(
        isolated, monkeypatch, capabilities):
    import kindex.kinbase as bridge

    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Caller enabled a withheld Kinbase write capability")

    monkeypatch.setattr(bridge, "submit_observation", forbidden)
    monkeypatch.setattr(bridge, "read_status", forbidden)
    server = _server(isolated["repo"], **capabilities)
    for injected in ({}, {"allow_kinbase_submit": True}, {"no_kinbase": False}):
        _refused(_call(server, "kinbase_submit", repo=str(isolated["repo"]),
                       text="Caller cannot grant itself write permission", **injected))
        _refused(_call(server, "kinbase_status", repo=str(isolated["repo"]), **injected))
    assert attempted == []
    for tool in asyncio.run(server.list_tools()):
        assert {"allow_kinbase_submit", "no_kinbase"}.isdisjoint(
            tool.inputSchema.get("properties", {}))


def test_no_kinbase_mode_refuses_even_read_tools(isolated, monkeypatch):
    import kindex.kinbase as bridge

    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("No-Kinbase scope reached a Kinbase backend")

    monkeypatch.setattr(bridge, "sync_kinbase", forbidden)
    monkeypatch.setattr(bridge, "read_explain", forbidden)
    server = _server(isolated["repo"], no_kinbase=True)
    _refused(_call(server, "kinbase_sync", repo=str(isolated["repo"])))
    _refused(_call(server, "kinbase_explain", repo=str(isolated["repo"]),
                   logical_key="scope", decision="verify omitted capability"))
    assert attempted == []


def test_conflicting_kinbase_capabilities_refuse_at_construction(isolated):
    with pytest.raises((ValueError, RuntimeError)):
        _server(isolated["repo"], allow_kinbase_submit=True, no_kinbase=True)


def test_console_entrypoint_requires_repo_and_describes_stdio(isolated):
    project = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    target = metadata["project"]["scripts"]["kindex-lite"]
    module, function = target.split(":")
    command = [sys.executable, "-c", (
        "import importlib, sys; "
        f"sys.exit(getattr(importlib.import_module({module!r}), {function!r})())"
    )]
    help_result = subprocess.run(command + ["--help"], capture_output=True, text=True,
                                 cwd=project, timeout=15)
    assert help_result.returncode == 0, help_result.stderr
    assert "--repo" in help_result.stdout
    assert "--allow-kinbase-submit" in help_result.stdout
    assert "--no-kinbase" in help_result.stdout
    missing = subprocess.run(command, capture_output=True, text=True, cwd=project, timeout=15)
    assert missing.returncode != 0
    assert "repo" in (missing.stderr + missing.stdout).lower()
    conflicting = subprocess.run(
        command + ["--repo", str(isolated["repo"]), "--allow-kinbase-submit", "--no-kinbase"],
        capture_output=True, text=True, cwd=project, timeout=15)
    assert conflicting.returncode != 0
    assert any(word in (conflicting.stdout + conflicting.stderr).lower()
               for word in ("not allowed", "exclusive", "conflict", "cannot"))
