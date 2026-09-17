"""Tests for sequestered multi-profile storage (config/profiles stage).

Covers: profile resolution order (flag > env > kin > roots > default >
legacy), hard sequestration between profile stores, the db profile stamp,
the legacy no-profiles passthrough (the dominant regression case), the
profile CLI subcommands, cron_run_all session routing, write_kin_index
audience filtering, and resolve_agent_id precedence.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kindex.config import (
    CollabConfig,
    Config,
    ProfileEntry,
    load_config,
    resolve_agent_id,
)
from kindex.store import ProfileMismatchError, Store


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env_clean(monkeypatch):
    """Strip profile/agent env vars so tests control them explicitly."""
    monkeypatch.delenv("KIN_PROFILE", raising=False)
    monkeypatch.delenv("KIN_AGENT_ID", raising=False)
    monkeypatch.delenv("KIN_PROJECT", raising=False)


@pytest.fixture
def global_yaml(tmp_path, monkeypatch, env_clean):
    """Redirect the global config layer to a temp file (initially absent)."""
    gpath = tmp_path / "home" / ".config" / "kindex" / "kin.yaml"
    gpath.parent.mkdir(parents=True)
    monkeypatch.setattr("kindex.config._GLOBAL_PATHS", [gpath])
    return gpath


@pytest.fixture
def project(tmp_path):
    """A bare project dir with no .kin config (neutral local layer)."""
    p = tmp_path / "neutral-project"
    p.mkdir()
    return p


def _write_profiles(gpath: Path, tmp_path: Path, default: str | None = "personal",
                    extra: dict | None = None) -> dict:
    data = {
        "profiles": {
            "work": {
                "data_dir": str(tmp_path / "work-data"),
                "roots": [str(tmp_path / "work")],
            },
            "personal": {
                "data_dir": str(tmp_path / "personal-data"),
                "roots": [str(tmp_path / "personal")],
            },
        },
    }
    if default:
        data["default_profile"] = default
    if extra:
        data.update(extra)
    gpath.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return data


# ── resolution order ─────────────────────────────────────────────────


class TestProfileResolution:
    def test_legacy_no_profiles_passthrough(self, global_yaml, project, monkeypatch):
        """THE regression case: no profiles, no flag, no env => byte-identical
        legacy behavior. active_profile None, data_dir untouched."""
        monkeypatch.chdir(project)
        cfg = load_config(project_path=project)
        assert cfg.active_profile is None
        assert cfg.profile_source == "legacy"
        assert cfg.data_dir == "~/.kindex"
        # Full-model equality with code defaults: nothing else drifted either.
        assert cfg.model_dump() == Config().model_dump()

    def test_legacy_passthrough_keeps_configured_data_dir(
            self, global_yaml, project, monkeypatch):
        monkeypatch.chdir(project)
        global_yaml.write_text(yaml.dump({"data_dir": "/tmp/custom-graph"}))
        cfg = load_config(project_path=project)
        assert cfg.data_dir == "/tmp/custom-graph"
        assert cfg.active_profile is None
        assert cfg.profile_source == "legacy"

    def test_flag_beats_env(self, global_yaml, project, tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        monkeypatch.setenv("KIN_PROFILE", "personal")
        monkeypatch.chdir(project)
        cfg = load_config(project_path=project, profile="work")
        assert cfg.active_profile == "work"
        assert cfg.profile_source == "flag"
        assert cfg.data_dir == str(tmp_path / "work-data")

    def test_env_beats_kin_key(self, global_yaml, project, tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        kin_dir = project / ".kin"
        kin_dir.mkdir()
        (kin_dir / "config").write_text("profile: personal\n")
        monkeypatch.setenv("KIN_PROFILE", "work")
        monkeypatch.chdir(project)
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "work"
        assert cfg.profile_source == "env"

    def test_kin_key_beats_roots(self, global_yaml, project, tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        kin_dir = project / ".kin"
        kin_dir.mkdir()
        (kin_dir / "config").write_text("profile: work\n")
        # cwd inside personal's root would match roots — kin key must win
        personal_repo = tmp_path / "personal" / "repo"
        personal_repo.mkdir(parents=True)
        monkeypatch.chdir(personal_repo)
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "work"
        assert cfg.profile_source == "kin"

    def test_roots_match_beats_default(self, global_yaml, project, tmp_path,
                                       monkeypatch):
        _write_profiles(global_yaml, tmp_path, default="personal")
        work_repo = tmp_path / "work" / "repo"
        work_repo.mkdir(parents=True)
        monkeypatch.chdir(work_repo)
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "work"
        assert cfg.profile_source == "roots"
        assert cfg.data_dir == str(tmp_path / "work-data")

    def test_roots_longest_prefix_wins(self, global_yaml, project, tmp_path,
                                       monkeypatch):
        # personal claims the whole tmp tree; work claims the deeper subtree
        data = {
            "profiles": {
                "personal": {"data_dir": str(tmp_path / "p-data"),
                             "roots": [str(tmp_path)]},
                "work": {"data_dir": str(tmp_path / "w-data"),
                         "roots": [str(tmp_path / "work")]},
            },
        }
        global_yaml.write_text(yaml.dump(data))
        deep = tmp_path / "work" / "deep" / "repo"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "work"
        assert cfg.profile_source == "roots"

    def test_default_profile_when_nothing_matches(self, global_yaml, project,
                                                  tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path, default="personal")
        monkeypatch.chdir(project)  # outside all roots
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "personal"
        assert cfg.profile_source == "default"
        assert cfg.data_dir == str(tmp_path / "personal-data")

    def test_no_match_no_default_falls_to_legacy(self, global_yaml, project,
                                                 tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path, default=None)
        monkeypatch.chdir(project)
        cfg = load_config(project_path=project)
        assert cfg.active_profile is None
        assert cfg.profile_source == "legacy"
        assert cfg.data_dir == "~/.kindex"

    def test_unknown_profile_flag_raises(self, global_yaml, project, tmp_path,
                                         monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        monkeypatch.chdir(project)
        with pytest.raises(ValueError) as exc:
            load_config(project_path=project, profile="nope")
        assert "nope" in str(exc.value)
        assert "personal" in str(exc.value)
        assert "work" in str(exc.value)

    def test_unknown_profile_env_raises(self, global_yaml, project, tmp_path,
                                        monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        monkeypatch.setenv("KIN_PROFILE", "ghost")
        monkeypatch.chdir(project)
        with pytest.raises(ValueError, match="ghost"):
            load_config(project_path=project)

    def test_explicit_profile_with_no_profiles_configured_raises(
            self, global_yaml, project, monkeypatch):
        """KIN_PROFILE/--profile must error clearly, never fall through."""
        monkeypatch.chdir(project)
        with pytest.raises(ValueError, match="none"):
            load_config(project_path=project, profile="anything")
        monkeypatch.setenv("KIN_PROFILE", "anything")
        with pytest.raises(ValueError):
            load_config(project_path=project)

    def test_unknown_default_profile_raises(self, global_yaml, project,
                                            tmp_path, monkeypatch):
        _write_profiles(global_yaml, tmp_path, default="ghost")
        monkeypatch.chdir(project)
        with pytest.raises(ValueError, match="ghost"):
            load_config(project_path=project)

    def test_expanduser_on_profile_data_dir(self, global_yaml, project,
                                            tmp_path, monkeypatch):
        data = {"profiles": {"work": {"data_dir": "~/work-graph", "roots": []}},
                "default_profile": "work"}
        global_yaml.write_text(yaml.dump(data))
        monkeypatch.chdir(project)
        cfg = load_config(project_path=project)
        assert cfg.active_profile == "work"
        assert "~" not in cfg.data_dir
        assert cfg.data_dir == str(Path("~/work-graph").expanduser())


# ── agent identity ────────────────────────────────────────────────────


class TestResolveAgentId:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("KIN_AGENT_ID", "agent-env")
        cfg = Config(agent_id="agent-config")
        assert resolve_agent_id(cfg) == "agent-env"

    def test_config_beats_fallback(self, monkeypatch):
        monkeypatch.delenv("KIN_AGENT_ID", raising=False)
        cfg = Config(agent_id="agent-config")
        assert resolve_agent_id(cfg) == "agent-config"

    def test_fallback_user_at_host(self, monkeypatch):
        monkeypatch.delenv("KIN_AGENT_ID", raising=False)
        cfg = Config(user="testuser")
        agent = resolve_agent_id(cfg)
        assert agent.startswith("testuser@")
        assert "." not in agent.split("@", 1)[1]  # short hostname


# ── collab config defaults ────────────────────────────────────────────


class TestNewConfigFields:
    def test_collab_defaults(self):
        cfg = Config()
        assert cfg.collab.enabled is True
        assert cfg.collab.display == "full"
        assert cfg.collab.prompt_cooldown_minutes == 10
        assert isinstance(cfg.collab, CollabConfig)

    def test_edit_policy_default_empty(self):
        assert Config().edit_policy == {}

    def test_readonly_kin_subcommands_gains_entries(self):
        subs = Config().attention.readonly_kin_subcommands
        for entry in ("coord read", "coord list", "profile list",
                      "profile which", "whoami"):
            assert entry in subs

    def test_two_word_kin_subcommand_matching(self):
        from kindex.attention import _bash_segment_is_readonly
        cfg = Config()
        assert _bash_segment_is_readonly("kin coord read standup", cfg) is True
        assert _bash_segment_is_readonly("kin coord post standup hi", cfg) is False
        assert _bash_segment_is_readonly("kin profile list", cfg) is True
        assert _bash_segment_is_readonly("kin profile create x", cfg) is False
        assert _bash_segment_is_readonly("kin whoami", cfg) is True


# ── sequestration + stamp ─────────────────────────────────────────────


class TestSequestration:
    def test_two_profiles_zero_leakage(self, global_yaml, project, tmp_path,
                                       monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        monkeypatch.chdir(project)

        cfg_work = load_config(project_path=project, profile="work")
        store_work = Store(cfg_work)
        store_work.add_node("Work secret roadmap")
        store_work.close()

        cfg_personal = load_config(project_path=project, profile="personal")
        store_personal = Store(cfg_personal)
        assert store_personal.all_nodes(limit=100) == []
        store_personal.add_node("Personal journal idea")
        store_personal.close()

        store_work = Store(load_config(project_path=project, profile="work"))
        titles = [n["title"] for n in store_work.all_nodes(limit=100)]
        assert titles == ["Work secret roadmap"]
        store_work.close()

    def test_profile_stamp_and_mismatch(self, global_yaml, project, tmp_path,
                                        monkeypatch):
        _write_profiles(global_yaml, tmp_path)
        monkeypatch.chdir(project)

        cfg_work = load_config(project_path=project, profile="work")
        store = Store(cfg_work)
        assert store.get_meta("kin_profile") == "work"
        store.close()

        # Same db dir, different active profile -> hard refuse, no override.
        cfg_clash = load_config(project_path=project, profile="personal")
        cfg_clash.data_dir = cfg_work.data_dir
        store2 = Store(cfg_clash)
        with pytest.raises(ProfileMismatchError, match="work"):
            _ = store2.conn

    def test_legacy_config_does_not_stamp(self, tmp_path):
        cfg = Config(data_dir=str(tmp_path / "plain"))
        store = Store(cfg)
        store.add_node("anything")
        assert store.get_meta("kin_profile") is None
        store.close()


# ── CLI subcommands (subprocess) ──────────────────────────────────────


def _cli_env(home: Path, project: Path, **extra) -> dict:
    env = dict(os.environ)
    env.pop("KIN_PROFILE", None)
    env.pop("KIN_AGENT_ID", None)
    env["HOME"] = str(home)
    env["KIN_PROJECT"] = str(project)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_cli(args, env, cwd):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd),
    )


@pytest.fixture
def cli_home(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "kindex").mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    return home, project


class TestProfileCLI:
    def test_create_roundtrips_existing_yaml(self, cli_home, tmp_path):
        home, project = cli_home
        gpath = home / ".config" / "kindex" / "kin.yaml"
        gpath.write_text(yaml.dump({
            "llm": {"enabled": False},
            "custom_unknown_key": "keepme",
        }))
        env = _cli_env(home, project)

        r = _run_cli(["profile", "create", "work",
                      "--data-dir", str(tmp_path / "work-data"),
                      "--roots", f"{tmp_path / 'work'},{tmp_path / 'work2'}",
                      "--default"], env, project)
        assert r.returncode == 0, r.stderr
        assert "Created profile 'work'" in r.stdout

        data = yaml.safe_load(gpath.read_text())
        assert data["custom_unknown_key"] == "keepme"          # not dropped
        assert data["llm"] == {"enabled": False}               # not dropped
        assert data["profiles"]["work"]["data_dir"] == str(tmp_path / "work-data")
        assert data["profiles"]["work"]["roots"] == [
            str(tmp_path / "work"), str(tmp_path / "work2")]
        assert data["default_profile"] == "work"

    def test_create_refuses_duplicate(self, cli_home, tmp_path):
        home, project = cli_home
        env = _cli_env(home, project)
        r1 = _run_cli(["profile", "create", "work",
                       "--data-dir", str(tmp_path / "wd")], env, project)
        assert r1.returncode == 0, r1.stderr
        r2 = _run_cli(["profile", "create", "work",
                       "--data-dir", str(tmp_path / "other")], env, project)
        assert r2.returncode != 0
        assert "already exists" in r2.stderr

    def test_stamp_mismatch_exits_cleanly(self, cli_home, tmp_path):
        home, project = cli_home
        gpath = home / ".config" / "kindex" / "kin.yaml"
        gpath.write_text(yaml.dump({
            "profiles": {
                "work": {"data_dir": str(tmp_path / "work-data")},
                "personal": {"data_dir": str(tmp_path / "personal-data")},
            },
        }))
        env = _cli_env(home, project)

        r = _run_cli(["add", "stamp me", "--profile", "work"], env, project)
        assert r.returncode == 0, r.stderr

        # Point the personal profile at work's stamped db: clean refusal,
        # not a traceback.
        r = _run_cli(["status", "--profile", "personal",
                      "--data-dir", str(tmp_path / "work-data")], env, project)
        assert r.returncode == 2
        assert "Error:" in r.stderr
        assert "stamped for profile 'work'" in r.stderr
        assert "Traceback" not in r.stderr

    def test_create_requires_data_dir(self, cli_home):
        home, project = cli_home
        r = _run_cli(["profile", "create", "work"], _cli_env(home, project), project)
        assert r.returncode == 2
        assert "--data-dir" in r.stderr

    def test_list_and_which(self, cli_home, tmp_path):
        home, project = cli_home
        env = _cli_env(home, project)
        _run_cli(["profile", "create", "work",
                  "--data-dir", str(tmp_path / "work-data"),
                  "--roots", str(tmp_path / "work"), "--default"], env, project)

        r = _run_cli(["profile", "list"], env, project)
        assert r.returncode == 0, r.stderr
        assert "work" in r.stdout
        assert "default" in r.stdout
        assert "work-data" in r.stdout

        r = _run_cli(["profile", "which"], env, project)
        assert r.returncode == 0, r.stderr
        assert "work (via default)" in r.stdout

        # roots tier: run from inside the work root
        work_repo = tmp_path / "work" / "repo"
        work_repo.mkdir(parents=True)
        r = _run_cli(["profile", "which"], env, work_repo)
        assert "work (via roots)" in r.stdout

    def test_which_legacy_when_no_profiles(self, cli_home):
        home, project = cli_home
        r = _run_cli(["profile", "which"], _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr
        assert "legacy single-graph" in r.stdout

    def test_unknown_profile_flag_errors(self, cli_home, tmp_path):
        home, project = cli_home
        env = _cli_env(home, project)
        r = _run_cli(["status", "--profile", "nope"], env, project)
        assert r.returncode != 0
        assert "Unknown kindex profile 'nope'" in r.stderr

    def test_unknown_profile_env_errors(self, cli_home):
        home, project = cli_home
        env = _cli_env(home, project, KIN_PROFILE="ghost")
        r = _run_cli(["status"], env, project)
        assert r.returncode != 0
        assert "Unknown kindex profile 'ghost'" in r.stderr

    def test_status_shows_profile_line(self, cli_home, tmp_path):
        home, project = cli_home
        env = _cli_env(home, project)
        _run_cli(["profile", "create", "work",
                  "--data-dir", str(tmp_path / "work-data"), "--default"],
                 env, project)
        r = _run_cli(["status"], env, project)
        assert r.returncode == 0, r.stderr
        assert "Profile: work (via default)" in r.stdout

    def test_status_shows_legacy_line(self, cli_home):
        home, project = cli_home
        r = _run_cli(["status"], _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr
        assert "legacy single-graph" in r.stdout

    def test_whoami_prints_agent_line(self, cli_home):
        home, project = cli_home
        env = _cli_env(home, project, KIN_AGENT_ID="custom-agent-7")
        r = _run_cli(["whoami"], env, project)
        assert r.returncode == 0, r.stderr
        assert "Agent: custom-agent-7" in r.stdout


# ── cron_run_all routing ──────────────────────────────────────────────


SESSION_TEXT = ("Stigmergy is coordination through environmental traces. "
                "Ambient Structure Discovery uses these stigmergic principles.")


def _make_session(projects_dir: Path, dirname: str, stem: str,
                  cwd: str | None = None) -> Path:
    d = projects_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    entry = {"role": "assistant", "content": SESSION_TEXT}
    if cwd:
        entry["cwd"] = cwd
    path = d / f"{stem}.jsonl"
    path.write_text(json.dumps(entry) + "\n")
    return path


def _encode(path: Path) -> str:
    from kindex.daemon import _encode_claude_project_dir
    return _encode_claude_project_dir(str(path))


class TestCronRunAll:
    def _base_config(self, tmp_path) -> Config:
        return Config(
            data_dir=str(tmp_path / "legacy-data"),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "no-projects")],
            profiles={
                "work": ProfileEntry(
                    data_dir=str(tmp_path / "work-data"),
                    roots=[str(tmp_path / "work")]),
                "personal": ProfileEntry(
                    data_dir=str(tmp_path / "personal-data"),
                    roots=[str(tmp_path / "personal")]),
            },
            default_profile="personal",
        )

    def test_no_profiles_single_legacy_pass(self, tmp_path):
        from kindex.daemon import cron_run_all

        cfg = Config(
            data_dir=str(tmp_path / "data"),
            claude_dir=str(tmp_path / "claude"),
            project_dirs=[str(tmp_path / "no-projects")],
        )
        passes = cron_run_all(cfg)
        assert len(passes) == 1
        assert passes[0]["profile"] is None
        assert passes[0]["source"] == "legacy"
        assert "sessions" in passes[0]["results"]
        assert (tmp_path / "data" / "kindex.db").exists()

    def test_per_profile_session_routing(self, tmp_path):
        from kindex.daemon import cron_run_all

        base = self._base_config(tmp_path)
        projects_dir = tmp_path / "claude" / "projects"
        # Session cwds encoded in the Claude project-dir names. The encoded
        # fallback (no cwd in the JSONL) verifies subdirectories against the
        # filesystem, so the real project dirs must exist.
        (tmp_path / "work" / "repo").mkdir(parents=True)
        (tmp_path / "personal" / "notes").mkdir(parents=True)
        _make_session(projects_dir, _encode(tmp_path / "work" / "repo"),
                      "workaaaa1111")
        _make_session(projects_dir, _encode(tmp_path / "personal" / "notes"),
                      "persbbbb2222")
        # Stray session under neither root -> default profile (personal)
        _make_session(projects_dir, _encode(tmp_path / "elsewhere" / "misc"),
                      "straycccc333")

        passes = cron_run_all(base)
        assert [p["profile"] for p in passes] == ["work", "personal"]

        work_store = Store(Config(data_dir=str(tmp_path / "work-data")))
        work_sessions = work_store.all_nodes(node_type="session", limit=50)
        assert [n["id"] for n in work_sessions] == ["session-workaaaa1111"]
        # Stamped for the work profile
        assert work_store.get_meta("kin_profile") == "work"
        work_store.close()

        personal_store = Store(Config(data_dir=str(tmp_path / "personal-data")))
        ids = sorted(n["id"] for n in
                     personal_store.all_nodes(node_type="session", limit=50))
        assert ids == ["session-persbbbb2222", "session-straycccc333"]
        personal_store.close()

        # The legacy data_dir is never touched when profiles exist
        assert not (tmp_path / "legacy-data").exists()

    def test_routing_prefers_cwd_recorded_in_jsonl(self, tmp_path):
        from kindex.daemon import cron_run_all

        base = self._base_config(tmp_path)
        projects_dir = tmp_path / "claude" / "projects"
        # Directory name is meaningless, but the JSONL records a work cwd.
        _make_session(projects_dir, "opaque-dir-name", "cwdrouted1234",
                      cwd=str(tmp_path / "work" / "deep" / "repo"))

        cron_run_all(base)

        work_store = Store(Config(data_dir=str(tmp_path / "work-data")))
        ids = [n["id"] for n in
               work_store.all_nodes(node_type="session", limit=50)]
        assert ids == ["session-cwdrouted123"]
        work_store.close()

        personal_store = Store(Config(data_dir=str(tmp_path / "personal-data")))
        assert personal_store.all_nodes(node_type="session", limit=50) == []
        personal_store.close()

    def test_session_filter_longest_prefix(self, tmp_path):
        from kindex.daemon import profile_session_filter

        profiles = {
            "broad": ProfileEntry(data_dir="x", roots=[str(tmp_path)]),
            "narrow": ProfileEntry(data_dir="y",
                                   roots=[str(tmp_path / "work")]),
        }
        projects_dir = tmp_path / "claude" / "projects"
        deep = _make_session(projects_dir, _encode(tmp_path / "work" / "r"),
                             "deepsess0001",
                             cwd=str(tmp_path / "work" / "r"))
        assert profile_session_filter(profiles, "narrow", None)(deep) is True
        assert profile_session_filter(profiles, "broad", None)(deep) is False


# ── write_kin_index audience scoping ──────────────────────────────────


class TestWriteKinIndexAudience:
    @pytest.fixture
    def store(self, tmp_path):
        s = Store(Config(data_dir=str(tmp_path / "data")))
        s.add_node("Private node", audience="private")
        s.add_node("Team node", audience="team")
        s.add_node("Public node", audience="public")
        yield s
        s.close()

    def test_non_repo_fallback_excludes_private(self, store, tmp_path):
        from kindex.ingest import write_kin_index

        out = tmp_path / "not-a-repo"
        out.mkdir()
        path = write_kin_index(store, out)
        data = json.loads(path.read_text())
        titles = {n["title"] for n in data["nodes"]}
        assert titles == {"Team node", "Public node"}
        assert data["repo"] is None

    def test_private_kin_audience_includes_everything(self, store, tmp_path):
        from kindex.ingest import write_kin_index

        out = tmp_path / "private-repo"
        (out / ".kin").mkdir(parents=True)
        (out / ".kin" / "config").write_text("audience: private\n")
        path = write_kin_index(store, out)
        data = json.loads(path.read_text())
        titles = {n["title"] for n in data["nodes"]}
        assert "Private node" in titles
        assert len(titles) == 3

    def test_repo_scoped_selection_preferred(self, store, tmp_path):
        from kindex.ingest import write_kin_index

        out = tmp_path / "gitrepo"
        out.mkdir()
        subprocess.run(["git", "init", "-q", str(out)], check=True,
                       capture_output=True)
        slug = out.name.lower()
        # Canonical adapter id shape: code-mod-{slug}-{12 hex} — the index
        # post-filters to exactly this shape (slug-collision hardening).
        mod_id = f"code-mod-{slug}-{'a' * 12}"
        store.add_node("Repo module", node_id=mod_id, audience="public")
        # A same-prefix id from another repo must NOT leak in
        store.add_node("Other repo module",
                       node_id=f"code-mod-{slug}-extra-{'b' * 12}",
                       audience="private")
        path = write_kin_index(store, out)
        data = json.loads(path.read_text())
        # Repo scoping wins: only the repo's code nodes, no global head
        assert data["repo"] == slug
        assert [n["id"] for n in data["nodes"]] == [mod_id]


# ── profile create honors --config as the write target (idx 23) ───────


class TestProfileCreateExplicitConfig:
    def test_create_writes_to_explicit_config_not_global(self, cli_home, tmp_path):
        """Repro: pre-fix, `profile create ... --config <file>` silently
        ignored the flag and mutated the user's real global kin.yaml."""
        home, project = cli_home
        gpath = home / ".config" / "kindex" / "kin.yaml"
        gpath.write_text(yaml.dump({"custom_unknown_key": "keepme"}))
        target = tmp_path / "sandbox" / "test-kin.yaml"
        env = _cli_env(home, project)

        r = _run_cli(["profile", "create", "scratch",
                      "--data-dir", str(tmp_path / "scratch-data"),
                      "--config", str(target),
                      "--default"], env, project)
        assert r.returncode == 0, r.stderr
        assert str(target) in r.stdout

        # The explicit target received the profile (parents auto-created)...
        data = yaml.safe_load(target.read_text())
        assert data["profiles"]["scratch"]["data_dir"] == str(tmp_path / "scratch-data")
        assert data["default_profile"] == "scratch"

        # ...and the real global config was not touched.
        gdata = yaml.safe_load(gpath.read_text())
        assert gdata == {"custom_unknown_key": "keepme"}

    def test_create_without_config_still_writes_global(self, cli_home, tmp_path):
        home, project = cli_home
        gpath = home / ".config" / "kindex" / "kin.yaml"
        env = _cli_env(home, project)

        r = _run_cli(["profile", "create", "work",
                      "--data-dir", str(tmp_path / "wd")], env, project)
        assert r.returncode == 0, r.stderr
        data = yaml.safe_load(gpath.read_text())
        assert "work" in data["profiles"]


class TestExplicitPathsAndRelativeProfiles:
    def test_a_missing_config_file_is_refused(self, env_clean, tmp_path, project):
        with pytest.raises(ValueError, match="config file not found"):
            load_config(config_path=tmp_path / "absent.yaml", project_path=project)

    def test_a_missing_project_path_is_refused(self, global_yaml, tmp_path, monkeypatch):
        with pytest.raises(ValueError, match="--project-path does not exist"):
            load_config(project_path=tmp_path / "gone")
        monkeypatch.setenv("KIN_PROJECT", str(tmp_path / "also-gone"))
        with pytest.raises(ValueError, match="KIN_PROJECT does not exist"):
            load_config()

    def test_a_relative_profile_dir_is_anchored_to_its_config(
            self, global_yaml, project, tmp_path, monkeypatch):
        global_yaml.write_text(yaml.dump({
            "profiles": {"work": {"data_dir": "graphs/work", "roots": []}},
            "default_profile": "work",
        }))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cfg = load_config(project_path=project)
        assert cfg.data_dir == str(global_yaml.parent / "graphs" / "work")
