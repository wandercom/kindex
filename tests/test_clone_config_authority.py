"""Content a git clone delivers is evidence, never authority.

A repository's tracked .kin/config, and any store whose files the repository
tracks, arrive with every clone. An unconfigured CLI must retain the user's
home graph and never open a clone-provided local store. Explicit routing and
the cron reminder sweep likewise must not let a cloned repository make kindex
run its own shell commands (a shipped reminder with a shell action, fired by
cron with no user action; a `sim.command` run by `kin sim check`) or become
the user's store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store

SRC = str(Path(__file__).resolve().parents[1] / "src")
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "kindex").mkdir(parents=True)
    code = tmp_path / "code"
    repo = code / "cloned-repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""), SRC) if p),
        "PYTHONDONTWRITEBYTECODE": "1",
        **GIT_ENV,
    }
    return {"tmp": tmp_path, "home": home, "code": code, "repo": repo, "env": env}


def run_py(world, code: str, *args: str):
    return subprocess.run([sys.executable, "-c", code, *args], cwd=world["repo"], env=world["env"],
                          text=True, capture_output=True, timeout=30)


def run_kin(world, *args: str):
    return subprocess.run([sys.executable, "-m", "kindex.cli", *args], cwd=world["repo"],
                          env=world["env"], text=True, capture_output=True, timeout=60)


def commit_all(repo: Path) -> None:
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "shipped")


def seed_store(path: Path) -> None:
    store = Store(Config(data_dir=str(path)))
    store.add_node(title="Shipped node", node_id="shipped-node")
    store.close()


def test_tracked_repo_local_store_is_not_used_by_unconfigured_status(world):
    home_store = world["home"] / ".kindex"
    clone_store = world["repo"] / ".kin" / "local" / "kindex"
    seed_store(home_store)
    seed_store(clone_store)
    commit_all(world["repo"])
    before = {path.relative_to(clone_store): path.read_bytes()
              for path in clone_store.rglob("*") if path.is_file()}
    result = run_kin(world, "status", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["stored_nodes"] == 1
    after = {path.relative_to(clone_store): path.read_bytes()
             for path in clone_store.rglob("*") if path.is_file()}
    assert after == before


def test_tracked_store_named_by_repo_config_is_refused(world):
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text("data_dir: shipped-store\n")
    seed_store(world["repo"] / "shipped-store")
    commit_all(world["repo"])
    result = run_py(world, "from kindex.config import load_config; print(load_config().data_path)")
    assert result.returncode != 0
    assert "Refusing tracked Kindex storage" in result.stderr


REFUSAL = """
import sys
from pathlib import Path
from kindex.project_store import tracked_store_refusal
print(tracked_store_refusal(Path(sys.argv[1])))
"""


def refusal_for(world, directory: Path, **env):
    result = subprocess.run([sys.executable, "-c", REFUSAL, str(directory)], cwd=world["tmp"],
                            env={**world["env"], **env}, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_pathspec_magic_directory_name_cannot_hide_a_tracked_store(world):
    """`:(exclude)*` read as a pathspec lists nothing, so the store looked untracked."""
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text('data_dir: ":(exclude)*"\n')
    seed_store(world["repo"] / ":(exclude)*")
    commit_all(world["repo"])
    result = run_py(world, "from kindex.config import load_config; print(load_config().data_path)")
    assert result.returncode != 0, result.stdout
    assert "Refusing tracked Kindex storage" in result.stderr


def test_a_tracked_symlink_to_a_tracked_store_is_refused(world):
    seed_store(world["repo"] / "shipped-store")
    (world["repo"] / "innocent").symlink_to("shipped-store")
    commit_all(world["repo"])
    assert refusal_for(world, world["repo"] / "innocent").startswith("Refusing tracked")


def test_an_inherited_git_dir_does_not_redirect_the_check(world):
    """A git hook environment sets GIT_DIR; the question is about the store's own worktree."""
    seed_store(world["repo"] / "shipped-store")
    commit_all(world["repo"])
    elsewhere = world["tmp"] / "elsewhere"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q")
    assert refusal_for(world, world["repo"] / "shipped-store",
                       GIT_DIR=str(elsewhere / ".git")).startswith("Refusing tracked")


def test_a_store_git_cannot_vouch_for_is_refused(world):
    """Inside a worktree, a git failure refuses the store instead of accepting it."""
    seed_store(world["repo"] / "some-store")
    (world["repo"] / ".git").rename(world["tmp"] / "moved-git")
    (world["repo"] / ".git").write_text("gitdir: /nonexistent/kindex-fixture\n")
    assert refusal_for(world, world["repo"] / "some-store").startswith(
        "Refusing Kindex storage at")


def test_a_store_outside_any_worktree_is_accepted(world):
    seed_store(world["tmp"] / "plain-store")
    assert refusal_for(world, world["tmp"] / "plain-store") == "None"


def test_unconfigured_status_does_not_recommend_deleting_a_clone_store(world):
    seed_store(world["home"] / ".kindex")
    seed_store(world["repo"] / ".kin" / "local" / "kindex")
    commit_all(world["repo"])
    result = run_kin(world, "status", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["stored_nodes"] == 1
    assert "rm -r --cached -- .kin/local" not in result.stderr


def test_untracked_repo_relative_store_still_works(world):
    """A repository may name its own local store (e.g. .kindex-data)."""
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text("data_dir: .kindex-data\n")
    commit_all(world["repo"])
    seed_store(world["repo"] / ".kindex-data")
    result = run_py(world, "from kindex.config import load_config; print(load_config().data_path)")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == (world["repo"] / ".kindex-data").resolve()


def _shipped_reminder_world(world, *, tracked: bool) -> Path:
    marker = world["tmp"] / "CRON_REMINDER_RAN"
    (world["repo"] / ".kin").mkdir(exist_ok=True)
    (world["repo"] / ".kin" / "config").write_text("data_dir: shipped-store\n")
    store = Store(Config(data_dir=str(world["repo"] / "shipped-store")))
    store.add_reminder("shipped", "2020-01-01T00:00:00", channels=["terminal"],
                       extra={"action_command": f"touch {marker}", "action_mode": "shell",
                              "action_snooze_until": "2099-01-01T00:00:00"})
    store.close()
    if tracked:
        commit_all(world["repo"])
    else:
        git(world["repo"], "add", ".kin/config")
        git(world["repo"], "commit", "-q", "-m", "config only")
    return marker


SWEEP = """
import json, sys
from pathlib import Path
from kindex.config import Config
from kindex.store import Store
from kindex.ingest import scan_kin_files
from kindex.daemon import remind_check_all
home, code = Path(sys.argv[1]), Path(sys.argv[2])
# Firing is suppressed while the machine is idle; the probe must not depend
# on whether someone is at the keyboard.
base = Config(data_dir=str(home / ".kindex"), project_dirs=[str(code)],
              reminders={"idle_suppress_after": 10**9})
store = Store(base)
scan_kin_files(base, store)
registry = json.loads(store.get_meta("project_graph_dirs") or "{}")
store.close()
print(json.dumps({"registry": registry, "sweep": remind_check_all(base)}))
"""


def test_cron_never_fires_a_reminder_shipped_in_a_clone(world):
    marker = _shipped_reminder_world(world, tracked=True)
    result = run_py(world, SWEEP, str(world["home"]), str(world["code"]))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert not marker.exists(), "a shell action shipped in a clone ran"
    assert str(world["repo"]) not in report["registry"]


def test_cron_still_fires_the_users_own_repo_local_reminders(world):
    """Positive control: the same untracked store is the user's own and runs."""
    marker = _shipped_reminder_world(world, tracked=False)
    result = run_py(world, SWEEP, str(world["home"]), str(world["code"]))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert marker.exists(), result.stdout
    assert str(world["repo"]) in report["registry"]
    assert any(entry.get("profile") == str(world["repo"]) and entry.get("fired", 0) >= 1
               for entry in report["sweep"]), report["sweep"]


def test_the_next_scan_drops_a_registered_graph_that_became_tracked(world):
    _shipped_reminder_world(world, tracked=False)
    first = run_py(world, SWEEP, str(world["home"]), str(world["code"]))
    assert str(world["repo"]) in json.loads(first.stdout.strip().splitlines()[-1])["registry"]
    commit_all(world["repo"])
    second = run_py(world, SWEEP, str(world["home"]), str(world["code"]))
    assert second.returncode == 0, second.stderr
    assert str(world["repo"]) not in json.loads(second.stdout.strip().splitlines()[-1])["registry"]


def test_sweep_skips_a_registered_graph_that_became_tracked(world):
    marker = _shipped_reminder_world(world, tracked=True)
    code = """
import json, sys
from pathlib import Path
from kindex.config import Config
from kindex.store import Store
from kindex.daemon import remind_check_all
home, repo = Path(sys.argv[1]), Path(sys.argv[2])
base = Config(data_dir=str(home / ".kindex"), reminders={"idle_suppress_after": 10**9})
store = Store(base)
store.set_meta("project_graph_dirs", json.dumps({str(repo): str(repo / "shipped-store")}))
store.close()
print(json.dumps(remind_check_all(base)))
"""
    result = run_py(world, code, str(world["home"]), str(world["repo"]))
    assert result.returncode == 0, result.stderr
    sweep = json.loads(result.stdout.strip().splitlines()[-1])
    assert not marker.exists()
    assert any("Refusing tracked" in str(entry.get("error", "")) for entry in sweep)


def test_repo_config_cannot_supply_a_sim_command(world):
    marker = world["tmp"] / "SIM_COMMAND_RAN"
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text(
        f"sim:\n  enabled: true\n  command: touch {marker}\n")
    commit_all(world["repo"])
    run_kin(world, "sim", "check", "--text", "hello")
    assert not marker.exists(), "a repository's sim.command ran"
    probe = run_py(world, "from kindex.config import load_config; c = load_config(); "
                          "print(repr(c.sim.command), c._ignored_project_keys)")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "'' ['sim']"


def test_repo_config_cannot_set_spend_identity_or_ingest_paths(world):
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text(
        "llm:\n  model: attacker-model\nbudget:\n  daily: 1000\nuser: someone-else\n"
        "project_dirs: [/]\nclaude_dir: /tmp/elsewhere\nname: legit-name\n")
    commit_all(world["repo"])
    probe = run_py(world, "from kindex.config import load_config; c = load_config(); "
                          "print(c.llm.model != 'attacker-model', c.user != 'someone-else', "
                          "c.project_dirs != ['/'], c.claude_dir != '/tmp/elsewhere', "
                          "sorted(c._ignored_project_keys))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == (
        "True True True True ['budget', 'claude_dir', 'llm', 'project_dirs', 'user']")


def test_doctor_reports_ignored_repo_keys(world):
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text("sim:\n  enabled: true\n")
    commit_all(world["repo"])
    result = run_kin(world, "doctor")
    assert "sets sim" in result.stdout, result.stdout + result.stderr


def test_a_new_repo_local_store_is_git_ignored(world):
    Store(Config(data_dir=str(world["repo"] / ".kin" / "local" / "kindex"))).close()
    store = Store(Config(data_dir=str(world["repo"] / ".kin" / "local" / "kindex")))
    store.add_node(title="local", node_id="local-node")
    store.close()
    assert "local/" in (world["repo"] / ".kin" / ".gitignore").read_text().splitlines()
    status = subprocess.run(["git", "-C", str(world["repo"]), "status", "--porcelain"],
                            capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert ".kin/local" not in status.stdout


def test_a_symlinked_kin_directory_is_never_written_through(world):
    outside = world["tmp"] / "outside"
    outside.mkdir()
    (world["repo"] / ".kin").symlink_to(outside)
    from kindex.project_store import ensure_local_ignored
    ensure_local_ignored(world["repo"] / ".kin" / "local" / "kindex")
    assert not (outside / ".gitignore").exists()


def test_the_ignore_entry_is_appended_to_existing_bytes(world):
    kin = world["repo"] / ".kin"
    kin.mkdir()
    (kin / ".gitignore").write_bytes(b"caf\xe9-notes")
    from kindex.project_store import ensure_local_ignored
    ensure_local_ignored(kin / "local")
    ensure_local_ignored(kin / "local" / "kindex")
    assert (kin / ".gitignore").read_bytes() == b"caf\xe9-notes\nlocal/\n"


def test_repo_config_cannot_route_reminders_or_turn_on_attention(world):
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text(
        "reminders:\n  remind_kindex_usage: false\n  action_enabled: true\n"
        "  channels:\n    slack:\n      enabled: true\n      webhook_url: https://hooks.example.invalid/x\n"
        "attention:\n  enabled: true\n")
    commit_all(world["repo"])
    probe = run_py(world, "from kindex.config import load_config; c = load_config(); "
                          "print(c.reminders.remind_kindex_usage, c.reminders.channels.slack.webhook_url == '', "
                          "c.attention.enabled, c._ignored_project_keys)")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == (
        "False True False ['attention', 'reminders.action_enabled', 'reminders.channels']")


def test_an_explicit_store_choice_replaces_a_tracked_project_store(world):
    """Only the store actually selected is asked about."""
    (world["repo"] / ".kin").mkdir()
    (world["repo"] / ".kin" / "config").write_text("data_dir: shipped-store\n")
    seed_store(world["repo"] / "shipped-store")
    commit_all(world["repo"])
    mine = world["tmp"] / "my-store"
    chosen = run_py(world, "import sys; from kindex.config import load_config; "
                           "print(load_config(data_dir=sys.argv[1]).data_path)", str(mine))
    assert chosen.returncode == 0, chosen.stderr
    assert chosen.stdout.strip() == str(mine)
    (world["home"] / ".config" / "kindex" / "kin.yaml").write_text(
        f"profiles:\n  work:\n    data_dir: {world['tmp'] / 'work-store'}\n")
    profiled = run_py(world, "from kindex.config import load_config; "
                             "print(load_config(profile='work').data_path)")
    assert profiled.returncode == 0, profiled.stderr
    assert profiled.stdout.strip() == str(world["tmp"] / "work-store")


def test_the_suggested_remedy_is_safe_to_paste(world):
    hostile = world["repo"] / ";touch OWNED;#"
    seed_store(hostile)
    commit_all(world["repo"])
    refusal = refusal_for(world, hostile)
    command = refusal.split("\n  ", 1)[1]
    subprocess.run(["bash", "-c", command], cwd=world["tmp"], check=True,
                   capture_output=True, env={**os.environ, **GIT_ENV})
    assert not (world["tmp"] / "OWNED").exists() and not (world["repo"] / "OWNED").exists()
    assert refusal_for(world, hostile) == "None", "the store is untracked and kept"
    assert (hostile / "kindex.db").exists()


def test_opening_a_store_never_writes_through_a_symlinked_kin(world):
    # The target is itself a .kin directory, so the resolved path looks
    # canonical; only the path as named shows the link.
    outside = world["tmp"] / "elsewhere" / ".kin"
    outside.mkdir(parents=True)
    (world["repo"] / ".kin").symlink_to(outside)
    store = Store(Config(data_dir=str(world["repo"] / ".kin" / "local" / "kindex")))
    store.add_node(title="local", node_id="local-node")
    store.close()
    assert not (outside / ".gitignore").exists()
