"""The store a call opens is the one it means, and opening it stays cheap.

- A present project store wins over configured and default personal stores.
- An absolute or trailing-slash spelling of ~/.kindex made --project-path
  and KIN_PROJECT no-ops.
- `kin config set` wrote into the first ancestor .kin/config it found,
  reconfiguring every sibling repository.
- A fresh store was created one statement at a time; one interrupted before
  its version stamp was migrated from v1 on every open and never opened.
- The full-text trigger re-indexed a node's whole text on every UPDATE, and
  every get_node wrote last_accessed.
"""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from kindex.config import Config, load_config
from kindex.schema import CREATE_TABLES, SCHEMA_VERSION
from kindex.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    import kindex.config as kconfig
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(kconfig, "_GLOBAL_PATHS", [home / ".config" / "kindex" / "kin.yaml"])
    for name in ("KIN_PROJECT", "KIN_PROJECT_PATH", "KIN_PROFILE", "KIN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "code" / "repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".kin").mkdir()
    (root / ".kin" / "config").write_text("name: repo\n")
    monkeypatch.chdir(root)
    return root


def seed(directory, *ids):
    store = Store(Config(data_dir=str(directory)))
    store.conn  # opening creates the database
    for node_id in ids:
        store.add_node(f"fixture {node_id}", node_id=node_id)
    store.close()


def test_unconfigured_resolution_uses_a_present_project_store(home, project):
    seed(home / ".kindex", "home-note")
    project_store = project / ".kin" / "local" / "kindex"
    seed(project_store)  # schema only, as any read leaves it
    assert load_config().data_path.resolve() == project_store.resolve()

    seed(project_store, "project-note")
    assert load_config().data_path.resolve() == project_store.resolve()


def test_unconfigured_resolution_uses_a_present_project_store_without_home_data(home, project):
    project_store = project / ".kin" / "local" / "kindex"
    seed(project_store)
    assert load_config().data_path.resolve() == project_store.resolve()


def test_explicit_project_path_wins_over_a_configured_data_dir(home, project):
    configured = home / "configured"
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text(f"data_dir: {configured}\n")

    assert load_config(project_path=project).data_path.resolve() == (
        project / ".kin" / "local" / "kindex"
    ).resolve()


def test_present_project_store_wins_over_a_configured_data_dir(home, project):
    configured = home / "configured"
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text(f"data_dir: {configured}\n")
    project_store = project / ".kin" / "local" / "kindex"
    seed(project_store)

    assert load_config().data_path.resolve() == project_store.resolve()


def test_configured_data_dir_is_used_without_a_project_store(home, project):
    configured = home / "configured"
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text(f"data_dir: {configured}\n")

    assert load_config().data_path.resolve() == configured.resolve()


def test_home_default_is_used_without_a_project_store_or_configuration(home, project):
    assert load_config().data_path.resolve() == (home / ".kindex").resolve()


@pytest.mark.parametrize("spelling", ["{home}/.kindex", "{home}/.kindex/", "~/.kindex/"])
def test_any_spelling_of_the_home_default_still_honours_the_project(home, project, spelling):
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text(f"data_dir: {spelling.format(home=home)}\n")
    selected = load_config(project_path=project).data_path.resolve()
    assert selected == (project / ".kin" / "local" / "kindex").resolve()


def test_config_set_writes_only_the_projects_own_file(home, tmp_path, project):
    from kindex.cli import _config_write
    shared = tmp_path / "code" / ".kin"
    shared.mkdir()
    (shared / "config").write_text("name: org-template\n")
    (project / ".kin" / "config").unlink()
    _config_write("llm.model", "fixture-model", None, global_=False, project_path=str(project))
    assert (shared / "config").read_text() == "name: org-template\n"
    assert "fixture-model" in (project / ".kin" / "config").read_text()


def test_a_fresh_store_is_created_whole_or_not_at_all(tmp_path, monkeypatch):
    import kindex.store as kstore
    monkeypatch.setattr(kstore, "CREATE_TABLES", CREATE_TABLES + "\nCREATE TABLE broken (;\n")
    store = Store(Config(data_dir=str(tmp_path / "data")))
    with pytest.raises(sqlite3.Error):
        store.conn
    with sqlite3.connect(tmp_path / "data" / "kindex.db") as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)


def interrupted_create(db_path, *, before_meta: bool):
    """What an earlier build left: CREATE_TABLES applied statement by
    statement, stopped before `meta` or before the version stamp."""
    script = CREATE_TABLES
    if before_meta:
        script = script[:script.index("CREATE TABLE IF NOT EXISTS meta")]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(script)
        conn.execute(
            "INSERT INTO nodes (id, title, content, type, status, weight, created_at, updated_at) "
            "VALUES ('kept', 'kept note', 'body', 'concept', 'active', 0.5, '2026-01-01', '2026-01-01')")


@pytest.mark.parametrize("before_meta", [True, False])
def test_an_interrupted_create_is_finished_on_open(tmp_path, before_meta):
    data = tmp_path / "data"
    interrupted_create(data / "kindex.db", before_meta=before_meta)
    store = Store(Config(data_dir=str(data)))
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert store.get_node("kept")["title"] == "kept note"
    store.close()
    assert not list((data).glob("**/*schema-v1-*"))


WIDE_TRIGGER = """
DROP TRIGGER nodes_au;
CREATE TRIGGER nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, title, content, aka, intent, domains)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.aka, old.intent, old.domains);
    INSERT INTO nodes_fts(rowid, id, title, content, aka, intent, domains)
    VALUES (new.rowid, new.id, new.title, new.content, new.aka, new.intent, new.domains);
END;
"""


def trigger_sql(store):
    return store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'nodes_au'"
    ).fetchone()[0]


def test_an_existing_store_gets_the_narrow_trigger_and_text_stays_searchable(tmp_path):
    data = tmp_path / "data"
    store = Store(Config(data_dir=str(data)))
    store.add_node("legacy heading", "body text", node_id="n")
    store.conn.executescript(WIDE_TRIGGER)
    store.close()

    store = Store(Config(data_dir=str(data)))
    assert "UPDATE OF" in trigger_sql(store).upper()
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.update_node("n", title="renamed heading")
    assert [row["id"] for row in store.fts_search("renamed")] == ["n"]
    assert store.fts_search("legacy") == []
    changes = store.conn.total_changes
    store.conn.execute("UPDATE nodes SET weight = 0.2 WHERE id = 'n'")
    assert store.conn.total_changes - changes == 1, "a weight write re-indexed the text"
    store.close()


def test_reads_write_last_accessed_at_most_once_an_interval(tmp_path):
    store = Store(Config(data_dir=str(tmp_path / "data")))
    store.add_node("note", node_id="n")
    store.conn.execute("UPDATE nodes SET last_accessed = '2020-01-01T00:00:00' WHERE id = 'n'")
    store.conn.commit()
    store.get_node("n")
    first = store.conn.execute("SELECT last_accessed FROM nodes WHERE id = 'n'").fetchone()[0]
    assert first > "2020-01-01T00:00:00"
    changes = store.conn.total_changes
    store.get_node("n")
    assert store.conn.total_changes == changes
    store.close()


def test_config_set_follows_a_symlinked_project_file(home, project):
    from kindex.cli import _config_write
    (project / ".kin" / "config").unlink()
    (project / "kindex.yaml").write_text("data_dir: kdata\n")
    (project / "kin.yaml").symlink_to("kindex.yaml")
    _config_write("llm.model", "fixture-model", None, global_=False, project_path=str(project))
    assert not (project / ".kin" / "config").exists()
    assert "fixture-model" in (project / "kindex.yaml").read_text()


def corrupt(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "kindex.db").write_bytes(b"not a database" * 100)


def test_present_project_store_wins_without_inspecting_its_contents(home, project, tmp_path):
    corrupt(project / ".kin" / "local" / "kindex")
    config_dir = home / ".config" / "kindex"
    config_dir.mkdir(parents=True)
    (config_dir / "kin.yaml").write_text(f"data_dir: {tmp_path / 'custom'}\n")
    assert load_config().data_path.resolve() == (project / ".kin" / "local" / "kindex").resolve()

    (config_dir / "kin.yaml").unlink()
    (project / ".kin" / "local" / "kindex" / "kindex.db").unlink()
    corrupt(home / ".kindex")
    selected = load_config(project_path=project).data_path.resolve()
    assert selected == (project / ".kin" / "local" / "kindex").resolve()


def test_a_half_created_store_with_rows_is_searchable(tmp_path):
    data = tmp_path / "data"
    script = CREATE_TABLES[:CREATE_TABLES.index("-- FTS5 full-text search")]
    data.mkdir()
    with sqlite3.connect(data / "kindex.db") as conn:
        conn.executescript(script)
        conn.execute(
            "INSERT INTO nodes (id, title, content, type, status, weight, created_at, updated_at) "
            "VALUES ('kept', 'kept note', 'body', 'concept', 'active', 0.5, '2026-01-01', '2026-01-01')")
    store = Store(Config(data_dir=str(data)))
    assert [row["id"] for row in store.fts_search("kept")] == ["kept"]
    store.update_node("kept", title="renamed note")
    assert [row["id"] for row in store.fts_search("renamed")] == ["kept"]
    store.close()


def test_a_half_created_store_gets_the_narrow_trigger(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    wide = CREATE_TABLES.replace(
        "AFTER UPDATE OF title, content, aka, intent, domains ON nodes", "AFTER UPDATE ON nodes")
    with sqlite3.connect(data / "kindex.db") as conn:
        conn.executescript(wide[:wide.index("CREATE TABLE IF NOT EXISTS meta")])
    store = Store(Config(data_dir=str(data)))
    assert "UPDATE OF" in trigger_sql(store).upper()
    store.close()


def test_concurrent_readers_record_one_access(tmp_path, monkeypatch):
    import kindex.store as kstore
    data = tmp_path / "data"
    store = Store(Config(data_dir=str(data)))
    store.add_node("note", node_id="n")
    store.conn.execute("UPDATE nodes SET last_accessed = '2020-01-01T00:00:00' WHERE id = 'n'")
    store.conn.commit()
    other = sqlite3.connect(data / "kindex.db")
    real = kstore._minutes_ago

    def another_reader_writes_first(minutes):
        other.execute("UPDATE nodes SET last_accessed = '2999-01-01T00:00:00' WHERE id = 'n'")
        other.commit()
        return real(minutes)

    monkeypatch.setattr(kstore, "_minutes_ago", another_reader_writes_first)
    store.get_node("n")
    assert store.conn.execute(
        "SELECT last_accessed FROM nodes WHERE id = 'n'").fetchone()[0] == "2999-01-01T00:00:00"
    other.close()
    store.close()
