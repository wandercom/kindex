"""MCP discovery across an implicit project graph and the home graph."""

import pytest
import sqlite3

pytest.importorskip("mcp")

from kindex.config import Config, ProfileEntry
from kindex.store import Store
from kindex.tasks import create_task


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    import kindex.mcp_server as server

    project = tmp_path / "s2s-framework"
    project.mkdir()
    home_dir = tmp_path / "home-graph"
    local_dir = project / ".kin" / "local" / "kindex"
    cfg = Config(data_dir=str(local_dir))
    cfg._project_path = project
    cfg._global_data_dir = str(home_dir)
    local = Store(cfg)
    home = Store(Config(data_dir=str(home_dir)))
    monkeypatch.setattr(server, "_store", local)
    monkeypatch.setattr(server, "_config", cfg)
    yield server, local, home, project
    local.close()
    home.close()


def test_search_merges_home_hit_with_nonempty_noisy_project(graphs):
    server, local, home, _ = graphs
    local.add_node(title="S2S notes", content="S2S generic unrelated notes")
    home.add_node(title="S2S backlog", content="S2S production backlog next task")

    output = server.search("S2S backlog", top_k=2)

    assert "S2S backlog" in output
    assert "graph=global" in output
    assert "S2S notes" in output
    assert "graph=project" in output
    assert "S2S backlog" in server.search("S2S backlog", top_k=1)


def test_equal_cross_graph_hits_prefer_global(graphs):
    server, local, home, _ = graphs
    local.add_node("Shared topic", content="same phrase")
    home.add_node("Shared topic", content="same phrase")

    output = server.search("Shared topic", top_k=1)

    assert "graph=global" in output
    assert "id=global:" in output


def test_home_search_is_read_only_and_does_not_create_missing_graph(graphs):
    server, _, home, _ = graphs
    node_id = home.add_node(title="Home only", content="distinct home content")
    before = home.conn.execute(
        "SELECT last_accessed FROM nodes WHERE id=?", (node_id,)).fetchone()[0]
    home.conn.execute("UPDATE nodes SET last_accessed='2000-01-01' WHERE id=?", (node_id,))
    home.conn.commit()
    assert "Home only" in server.search("distinct home content")
    after = home.conn.execute(
        "SELECT last_accessed FROM nodes WHERE id=?", (node_id,)).fetchone()[0]
    assert after == "2000-01-01"
    reader = server._global_read_store(server._store, server._config)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute("UPDATE nodes SET title='wrong' WHERE id=?", (node_id,))
    finally:
        reader.close()
    home.conn.execute("UPDATE nodes SET last_accessed=? WHERE id=?", (before, node_id))
    home.conn.commit()


def test_task_list_discovers_relevant_home_tasks_without_routing_mutations(graphs):
    server, _, home, project = graphs
    task_id = create_task(home, "S2S backlog item", project_path=str(project))
    create_task(home, "Different project task", project_path=str(project.parent / "other"))

    output = server.task_list()

    assert "S2S backlog item" in output
    assert "graph:global" in output
    assert "Different project task" not in output
    assert f"global:{task_id}" in output
    assert "Completed:" in server.task_done(f"global:{task_id}")
    assert home.get_node(task_id)["extra"]["task_status"] == "done"


def test_qualified_id_routes_edit_and_rejects_cross_graph_link(graphs):
    server, local, home, _ = graphs
    local_id = local.add_node(title="Local evidence", content="project observation")
    home_id = home.add_node(title="Global evidence", content="outer observation")

    assert "Edited Global evidence" in server.edit(f"global:{home_id}", append="verified")
    assert f"global:{home_id}" in server.show(f"global:{home_id}")
    assert "verified" in home.get_node(home_id)["content"]
    assert "verified" not in local.get_node(local_id)["content"]
    assert "cross-graph links" in server.link(local_id, f"global:{home_id}")
    assert not local.edges_from(local_id)


def test_duplicate_id_needs_qualified_reference(graphs):
    server, local, home, _ = graphs
    shared = "abcdef123456"
    local.add_node(title="Local version", node_id=shared)
    home.add_node(title="Global version", node_id=shared)

    assert "exists in both graphs" in server.edit(shared, append="unsafe")
    assert "Edited Global version" in server.edit(f"global:{shared}", append="safe")
    assert "safe" not in local.get_node(shared)["content"]


def test_derived_add_can_explicitly_target_global_graph(graphs):
    server, local, home, _ = graphs
    global_id = home.add_node("Global source")
    local_id = local.add_node("Project source")
    output = server.add("Outer graph derived observation",
                        source_refs=f"project:{local_id},global:{global_id}")
    ref = output.split("Created node: ", 1)[1].split(" ", 1)[0]

    assert ref.startswith("global:")
    assert home.get_node(ref.removeprefix("global:"))
    assert local.get_node(ref.removeprefix("global:")) is None
    denied = server.add("Wrong graph observation", graph="project",
                        source_refs=f"global:{global_id}")
    assert "Global source requires" in denied
    assert local.get_node_by_title("Wrong graph observation") is None


def test_derived_task_and_qualified_update_stay_global(graphs):
    server, local, home, project = graphs
    source = home.add_node("Global backlog source")
    output = server.task_add("Follow global backlog", project_path=str(project),
                             source_refs=f"global:{source}")
    task_ref = output.split("Created task: ", 1)[1].split(" ", 1)[0]

    assert task_ref.startswith("global:")
    updated = server.task_update(task_ref, priority=1)
    assert updated["ok"]
    assert updated["task"]["id"] == task_ref
    assert home.get_node(task_ref.removeprefix("global:"))["extra"]["priority"] == 1
    assert local.get_node(task_ref.removeprefix("global:")) is None
    local_source = local.add_node("Local source")
    denied = server.task_add("Impossible cross-store task", link_to=f"project:{local_source}",
                             source_refs=f"global:{source}")
    assert "cross-graph links" in denied
    assert home.get_node_by_title("Impossible cross-store task") is None


def test_global_watch_resolves_in_its_source_graph(graphs):
    server, local, home, _ = graphs
    watch_id = home.add_node("Global watch", node_type="watch")

    assert "Resolved watch" in server.watch_resolve(f"global:{watch_id}")
    assert home.get_node(watch_id)["status"] == "archived"
    assert local.get_node(watch_id) is None


def test_explicit_profile_keeps_search_isolated(graphs):
    server, local, home, _ = graphs
    local.config.active_profile = "work"
    home.add_node(title="Home only", content="distinct home content")

    assert "Home only" not in server.search("distinct home content")


def test_configured_global_profile_target_keeps_its_stamp(graphs):
    server, local, home, _ = graphs
    home.add_node("Profiled global result", content="profile target")
    home.conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('kin_profile', 'outer')")
    home.conn.commit()
    local.config.profiles["outer"] = ProfileEntry(data_dir=str(home.config.data_path))

    assert "graph=global" in server.search("profile target")


def test_no_home_database_is_not_created(graphs):
    server, local, home, _ = graphs
    home.close()
    home.db_path.unlink(missing_ok=True)
    assert "No results" in server.search("missing topic")
    assert not home.db_path.exists()


def test_load_config_uses_nondefault_global_data_dir(tmp_path, monkeypatch):
    from kindex.config import load_config

    project = tmp_path / "project"
    project.mkdir()
    local_dir = project / ".kin" / "local" / "kindex"
    global_dir = tmp_path / "configured-outer-graph"
    global_config = tmp_path / "kin.yaml"
    global_config.write_text(f"data_dir: {global_dir}\n")
    monkeypatch.setattr("kindex.config._GLOBAL_PATHS", [global_config])
    monkeypatch.setattr("kindex.config._git_root", lambda _: project)
    monkeypatch.delenv("KIN_PROFILE", raising=False)
    local = Store(Config(data_dir=str(local_dir)))
    local.add_node("Project seed")
    local.close()
    cfg = load_config(project_path=project)

    assert cfg.data_path == local_dir
    assert cfg._global_data_dir == str(global_dir)
