"""Regression tests for the session-routing sequestration fixes.

Each test encodes a concrete leak found by the adversarial review:
- punctuated-sibling encoded-dir fallback (acme vs acme-tools) — idx 0
- cwd authority over the lossy encoded-name match — idx 0
- symlinked/alternate-path roots resolve like live config — idx 18
- kin cron --profile/--data-dir pinning keeps routing — idx 1/14
- legacy-remainder cron pass when no default_profile — idx 5/30
- scan_sessions / incremental_ingest / scan_codex_sessions route on
  their own (not only multi-pass cron) — idx 13
- explicit --data-dir override never stamps an unstamped DB — idx 15
- profile create warns when the legacy graph is orphaned — idx 17
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kindex.config import Config, ProfileEntry
from kindex.routing import (
    _encode_claude_project_dir,
    _session_profile_owner,
    cwd_profile_owner,
    profile_session_filter,
)
from kindex.store import Store


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


def _enc(path: Path) -> str:
    return _encode_claude_project_dir(str(path))


# ── idx 0: punctuated siblings and cwd authority ─────────────────────


class TestPunctuatedSiblings:
    def _profiles(self, tmp_path):
        return {"work": ProfileEntry(data_dir=str(tmp_path / "work-data"),
                                     roots=[str(tmp_path / "Code" / "acme")])}

    def test_cwd_sibling_not_claimed(self, tmp_path):
        """The original repro: work root ~/src/acme, personal project at
        ~/src/acme-tools. The JSONL cwd proves no match — the encoded-dir
        fallback must not override it."""
        profiles = self._profiles(tmp_path)
        sibling = tmp_path / "Code" / "acme-tools"
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(sibling), "leakysess001",
                          cwd=str(sibling))
        assert _session_profile_owner(p, profiles) is None
        assert profile_session_filter(profiles, "work", None)(p) is False
        # The default profile takes it instead.
        assert profile_session_filter(profiles, "personal", "personal")(p) is True

    def test_cwd_match_outranks_encoded_match(self, tmp_path):
        """Repro 2: a genuine cwd match against a short root must beat the
        lossy encoded-name match against a longer root."""
        profiles = {
            "personal": ProfileEntry(data_dir="p", roots=[str(tmp_path)]),
            "work": ProfileEntry(data_dir="w",
                                 roots=[str(tmp_path / "Code" / "acme")]),
        }
        sibling = tmp_path / "Code" / "acme-tools"
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(sibling), "leakysess002",
                          cwd=str(sibling))
        assert _session_profile_owner(p, profiles) == "personal"

    def test_no_cwd_sibling_not_claimed(self, tmp_path):
        """Even without a cwd, a punctuated sibling must not be claimed:
        the encoded suffix is verified against the real filesystem."""
        profiles = self._profiles(tmp_path)
        (tmp_path / "Code" / "acme").mkdir(parents=True)        # root exists
        sibling = tmp_path / "Code" / "acme-tools"
        sibling.mkdir(parents=True)                              # sibling too
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(sibling), "leakysess003")  # no cwd
        assert _session_profile_owner(p, profiles) is None

    def test_no_cwd_genuine_subdir_claimed(self, tmp_path):
        profiles = self._profiles(tmp_path)
        subdir = tmp_path / "Code" / "acme" / "tools"
        subdir.mkdir(parents=True)
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(subdir), "subdirsess01")  # no cwd
        assert _session_profile_owner(p, profiles) == "work"

    def test_no_cwd_exact_root_claimed(self, tmp_path):
        """Exact encoded match needs no filesystem confirmation."""
        profiles = self._profiles(tmp_path)
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(tmp_path / "Code" / "acme"),
                          "exactsess001")
        assert _session_profile_owner(p, profiles) == "work"

    def test_cwd_outside_all_roots_is_unowned(self, tmp_path):
        """An extracted cwd is authoritative: no match means default."""
        profiles = self._profiles(tmp_path)
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(tmp_path / "Code" / "acme" / "x"),
                          "elsewheresss1", cwd=str(tmp_path / "elsewhere"))
        assert _session_profile_owner(p, profiles) is None


# ── idx 18: symlinked roots ───────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
class TestSymlinkedRoots:
    @pytest.fixture
    def layout(self, tmp_path):
        physical = tmp_path / "vol" / "Work"
        (physical / "repo").mkdir(parents=True)
        link = tmp_path / "home" / "Work"
        link.parent.mkdir(parents=True)
        link.symlink_to(physical, target_is_directory=True)
        profiles = {"work": ProfileEntry(data_dir=str(tmp_path / "wd"),
                                         roots=[str(link)])}
        return physical, link, profiles

    def test_physical_cwd_matches_symlinked_root(self, tmp_path, layout):
        physical, _link, profiles = layout
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(physical / "repo"), "symsess00001",
                          cwd=str(physical / "repo"))
        assert _session_profile_owner(p, profiles) == "work"

    def test_physical_encoded_name_matches_symlinked_root(self, tmp_path,
                                                          layout):
        physical, _link, profiles = layout
        projects = tmp_path / "claude" / "projects"
        p = _make_session(projects, _enc(physical / "repo"), "symsess00002")
        assert _session_profile_owner(p, profiles) == "work"

    def test_cwd_profile_owner_resolves_both_sides(self, layout):
        physical, _link, profiles = layout
        assert cwd_profile_owner(str(physical / "repo"), profiles) == "work"


# ── idx 13: every ingest path routes, not just multi-pass cron ───────


def _routed_config(tmp_path, active, default="personal", data_dir=None):
    return Config(
        data_dir=str(data_dir or tmp_path / f"{active or 'legacy'}-data"),
        claude_dir=str(tmp_path / "claude"),
        codex_dir=str(tmp_path / "codex"),
        project_dirs=[str(tmp_path / "no-projects")],
        profiles={
            "work": ProfileEntry(data_dir=str(tmp_path / "work-data"),
                                 roots=[str(tmp_path / "work")]),
            "personal": ProfileEntry(data_dir=str(tmp_path / "personal-data"),
                                     roots=[str(tmp_path / "personal")]),
        },
        default_profile=default,
        active_profile=active,
    )


def _seed_claude_sessions(tmp_path):
    _canonical_session_projects(tmp_path)
    projects = tmp_path / "claude" / "projects"
    _make_session(projects, "dir-a", "workaaaa1111",
                  cwd=str(tmp_path / "work" / "repo"))
    _make_session(projects, "dir-b", "persbbbb2222",
                  cwd=str(tmp_path / "personal" / "notes"))
    _make_session(projects, "dir-c", "straycccc333",
                  cwd=str(tmp_path / "elsewhere"))


def _canonical_session_projects(tmp_path):
    """Routing fixtures supply eligible projects without changing ownership."""
    for relative in ("work/repo", "personal/notes", "elsewhere"):
        project = tmp_path / relative
        project.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)


class TestScanSessionsSelfRouting:
    def _ids(self, store):
        return sorted(n["id"] for n in
                      store.all_nodes(node_type="session", limit=50))

    def test_active_nondefault_skips_foreign_sessions(self, tmp_path):
        """`kin ingest sessions` / MCP ingest under the work profile must
        not pull personal or stray sessions into the work store."""
        from kindex.ingest import scan_sessions

        _seed_claude_sessions(tmp_path)
        cfg = _routed_config(tmp_path, "work",
                             data_dir=tmp_path / "work-data")
        store = Store(cfg)
        scan_sessions(cfg, store, limit=50)
        assert self._ids(store) == ["session-workaaaa1111"]
        store.close()

    def test_default_profile_takes_unmatched(self, tmp_path):
        from kindex.ingest import scan_sessions

        _seed_claude_sessions(tmp_path)
        cfg = _routed_config(tmp_path, "personal",
                             data_dir=tmp_path / "personal-data")
        store = Store(cfg)
        scan_sessions(cfg, store, limit=50)
        assert self._ids(store) == ["session-persbbbb2222",
                                    "session-straycccc333"]
        store.close()

    def test_legacy_passthrough_takes_only_remainder(self, tmp_path):
        """Profiles configured, no default, cwd matched nothing: the legacy
        graph ingests exactly the unmatched sessions."""
        from kindex.ingest import scan_sessions

        _seed_claude_sessions(tmp_path)
        cfg = _routed_config(tmp_path, None, default=None,
                             data_dir=tmp_path / "legacy-data")
        store = Store(cfg)
        scan_sessions(cfg, store, limit=50)
        assert self._ids(store) == ["session-straycccc333"]
        store.close()

    def test_explicit_filter_still_wins(self, tmp_path):
        from kindex.ingest import scan_sessions

        _seed_claude_sessions(tmp_path)
        cfg = _routed_config(tmp_path, "work",
                             data_dir=tmp_path / "work-data")
        cfg._session_filter = lambda p: False
        store = Store(cfg)
        scan_sessions(cfg, store, limit=50)
        assert self._ids(store) == []
        store.close()

    def test_no_profiles_takes_everything(self, tmp_path):
        """Legacy regression guard: no profiles => unchanged behavior."""
        from kindex.ingest import scan_sessions

        _seed_claude_sessions(tmp_path)
        cfg = Config(data_dir=str(tmp_path / "plain-data"),
                     claude_dir=str(tmp_path / "claude"),
                     project_dirs=[str(tmp_path / "no-projects")])
        store = Store(cfg)
        scan_sessions(cfg, store, limit=50)
        assert len(self._ids(store)) == 3
        store.close()


class TestIncrementalIngestRouting:
    def test_watch_path_routes_sessions(self, tmp_path):
        from kindex.daemon import incremental_ingest

        _seed_claude_sessions(tmp_path)
        cfg = _routed_config(tmp_path, "work",
                             data_dir=tmp_path / "work-data")
        store = Store(cfg)
        count = incremental_ingest(cfg, store, "2000-01-01T00:00:00")
        ids = sorted(n["id"] for n in
                     store.all_nodes(node_type="session", limit=50))
        assert count == 1
        assert ids == ["session-workaaaa1111"]
        store.close()


def _make_codex_session(sessions_dir: Path, sid: str, cwd: str) -> Path:
    d = sessions_dir / "2026" / "06" / "11"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-06-11-{sid}.jsonl"
    lines = [
        {"type": "session_meta",
         "payload": {"id": f"rollout-{sid}", "cwd": cwd,
                     "model_provider": "openai"}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text",
                                  "text": SESSION_TEXT}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


class TestCodexRouting:
    def _seed(self, tmp_path):
        _canonical_session_projects(tmp_path)
        sessions = tmp_path / "codex" / "sessions"
        _make_codex_session(sessions, "workcdx00001",
                            str(tmp_path / "work" / "repo"))
        _make_codex_session(sessions, "perscdx00002",
                            str(tmp_path / "personal" / "notes"))
        _make_codex_session(sessions, "straycdx0003",
                            str(tmp_path / "elsewhere"))

    def _ids(self, store):
        return sorted(n["id"] for n in
                      store.all_nodes(node_type="session", limit=50))

    def test_codex_sessions_route_by_meta_cwd(self, tmp_path):
        from kindex.ingest import scan_codex_sessions

        self._seed(tmp_path)
        cfg = _routed_config(tmp_path, "work",
                             data_dir=tmp_path / "work-data")
        store = Store(cfg)
        scan_codex_sessions(cfg, store, limit=50)
        assert self._ids(store) == ["codex-session-workcdx00001"]
        store.close()

    def test_codex_default_takes_unmatched(self, tmp_path):
        from kindex.ingest import scan_codex_sessions

        self._seed(tmp_path)
        cfg = _routed_config(tmp_path, "personal",
                             data_dir=tmp_path / "personal-data")
        store = Store(cfg)
        scan_codex_sessions(cfg, store, limit=50)
        assert self._ids(store) == ["codex-session-perscdx00002",
                                    "codex-session-straycdx0003"]
        store.close()

    def test_codex_no_profiles_takes_everything(self, tmp_path):
        from kindex.ingest import scan_codex_sessions

        self._seed(tmp_path)
        cfg = Config(data_dir=str(tmp_path / "plain-data"),
                     codex_dir=str(tmp_path / "codex"),
                     project_dirs=[str(tmp_path / "no-projects")])
        store = Store(cfg)
        scan_codex_sessions(cfg, store, limit=50)
        assert len(self._ids(store)) == 3
        store.close()


# ── idx 5/30: legacy-remainder pass when no default_profile ──────────


class TestLegacyRemainderPass:
    def test_remainder_pass_services_legacy_graph(self, tmp_path):
        from kindex.daemon import cron_run_all

        _seed_claude_sessions(tmp_path)
        base = _routed_config(tmp_path, None, default=None,
                              data_dir=tmp_path / "legacy-data")
        passes = cron_run_all(base)
        assert [p["profile"] for p in passes] == ["work", "personal", None]
        assert passes[-1]["source"] == "legacy-remainder"

        work = Store(Config(data_dir=str(tmp_path / "work-data")))
        assert [n["id"] for n in work.all_nodes(node_type="session", limit=50)] \
            == ["session-workaaaa1111"]
        work.close()

        legacy = Store(Config(data_dir=str(tmp_path / "legacy-data")))
        ids = [n["id"] for n in legacy.all_nodes(node_type="session", limit=50)]
        assert ids == ["session-straycccc333"]
        # Maintenance ran on the legacy graph (reminders, decay, etc.)...
        assert legacy.get_meta("last_cron_run") is not None
        # ...and the legacy DB stays unstamped (no active profile).
        assert legacy.get_meta("kin_profile") is None
        legacy.close()

    def test_remainder_uses_preactivation_data_dir(self, tmp_path):
        """Even when this cron invocation itself resolved to a profile
        (data_dir swapped), the remainder pass finds the legacy graph."""
        from kindex.config import _activate_profile
        from kindex.daemon import cron_run_all

        _seed_claude_sessions(tmp_path)
        base = _routed_config(tmp_path, None, default=None,
                              data_dir=tmp_path / "legacy-data")
        _activate_profile(base, "work", "roots")
        assert base.data_dir == str(tmp_path / "work-data")

        passes = cron_run_all(base)
        assert passes[-1]["source"] == "legacy-remainder"
        legacy = Store(Config(data_dir=str(tmp_path / "legacy-data")))
        ids = [n["id"] for n in legacy.all_nodes(node_type="session", limit=50)]
        assert ids == ["session-straycccc333"]
        legacy.close()

    def test_no_remainder_pass_when_default_set(self, tmp_path):
        from kindex.daemon import cron_run_all

        _seed_claude_sessions(tmp_path)
        base = _routed_config(tmp_path, None, default="personal",
                              data_dir=tmp_path / "legacy-data")
        passes = cron_run_all(base)
        assert [p["profile"] for p in passes] == ["work", "personal"]
        assert not (tmp_path / "legacy-data").exists()


# ── subprocess fixtures (pinned cron, stamp override, create warning) ─


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
        capture_output=True, text=True, timeout=120, env=env, cwd=str(cwd),
    )


@pytest.fixture
def cli_home(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "kindex").mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    return home, project


def _write_global_profiles(home: Path, tmp_path: Path,
                           default: str | None = "personal") -> Path:
    gpath = home / ".config" / "kindex" / "kin.yaml"
    data = {
        "llm": {"enabled": False},
        "profiles": {
            "work": {"data_dir": str(tmp_path / "work-data"),
                     "roots": [str(tmp_path / "work")]},
            "personal": {"data_dir": str(tmp_path / "personal-data"),
                         "roots": [str(tmp_path / "personal")]},
        },
    }
    if default:
        data["default_profile"] = default
    gpath.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return gpath


# ── idx 1/14: pinned cron keeps routing ───────────────────────────────


class TestCronPinnedPass:
    def test_cron_profile_does_not_ingest_foreign_sessions(self, cli_home,
                                                           tmp_path):
        home, project = cli_home
        _write_global_profiles(home, tmp_path)
        _canonical_session_projects(tmp_path)
        projects = home / ".claude" / "projects"
        _make_session(projects, "dir-w", "workaaaa1111",
                      cwd=str(tmp_path / "work" / "repo"))
        _make_session(projects, "dir-p", "persbbbb2222",
                      cwd=str(tmp_path / "personal" / "notes"))

        r = _run_cli(["cron", "--profile", "work"],
                     _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr

        work = Store(Config(data_dir=str(tmp_path / "work-data")))
        ids = [n["id"] for n in work.all_nodes(node_type="session", limit=50)]
        assert ids == ["session-workaaaa1111"]
        assert work.get_meta("kin_profile") == "work"
        work.close()
        # Single pinned pass: the personal store is never opened.
        assert not (tmp_path / "personal-data").exists()

    def test_cron_bare_data_dir_without_profile_is_legacy(self, cli_home,
                                                          tmp_path):
        """Documented behavior: a bare --data-dir with no resolved profile
        runs a legacy take-everything pass, and never stamps."""
        home, project = cli_home
        _write_global_profiles(home, tmp_path, default=None)
        _canonical_session_projects(tmp_path)
        projects = home / ".claude" / "projects"
        _make_session(projects, "dir-w", "workaaaa1111",
                      cwd=str(tmp_path / "work" / "repo"))
        _make_session(projects, "dir-p", "persbbbb2222",
                      cwd=str(tmp_path / "personal" / "notes"))

        target = tmp_path / "explicit-data"
        r = _run_cli(["cron", "--data-dir", str(target)],
                     _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr

        s = Store(Config(data_dir=str(target)))
        ids = sorted(n["id"] for n in
                     s.all_nodes(node_type="session", limit=50))
        assert ids == ["session-persbbbb2222", "session-workaaaa1111"]
        assert s.get_meta("kin_profile") is None
        s.close()


# ── idx 15: --data-dir override must not stamp an unstamped DB ───────


class TestNoStampOnDataDirOverride:
    def test_override_does_not_stamp_or_brick_other_profile(self, cli_home,
                                                            tmp_path):
        home, project = cli_home
        _write_global_profiles(home, tmp_path, default=None)
        env = _cli_env(home, project)

        # The repro: before personal ever runs, a work-profile command
        # explicitly targets personal's data_dir.
        r = _run_cli(["add", "stray note", "--profile", "work",
                      "--data-dir", str(tmp_path / "personal-data")],
                     env, project)
        assert r.returncode == 0, r.stderr

        s = Store(Config(data_dir=str(tmp_path / "personal-data")))
        assert s.get_meta("kin_profile") is None     # NOT stamped 'work'
        s.close()

        # The personal profile is not bricked.
        r = _run_cli(["status", "--profile", "personal"], env, project)
        assert r.returncode == 0, r.stderr
        assert "stamped for profile" not in r.stderr

    def test_existing_mismatched_stamp_still_refuses(self, cli_home,
                                                     tmp_path):
        home, project = cli_home
        _write_global_profiles(home, tmp_path, default=None)
        env = _cli_env(home, project)

        r = _run_cli(["add", "stamp me", "--profile", "work"], env, project)
        assert r.returncode == 0, r.stderr

        r = _run_cli(["status", "--profile", "personal",
                      "--data-dir", str(tmp_path / "work-data")], env, project)
        assert r.returncode == 2
        assert "stamped for profile 'work'" in r.stderr

    def test_matching_data_dir_still_stamps(self, cli_home, tmp_path):
        """--data-dir equal to the profile's own dir keeps normal stamping."""
        home, project = cli_home
        _write_global_profiles(home, tmp_path, default=None)
        env = _cli_env(home, project)

        r = _run_cli(["add", "note", "--profile", "work",
                      "--data-dir", str(tmp_path / "work-data")], env, project)
        assert r.returncode == 0, r.stderr
        s = Store(Config(data_dir=str(tmp_path / "work-data")))
        assert s.get_meta("kin_profile") == "work"
        s.close()


# ── idx 17: profile create warns about an orphaned legacy graph ──────


class TestProfileCreateWarning:
    def test_default_create_warns_when_legacy_graph_orphaned(self, cli_home,
                                                             tmp_path):
        home, project = cli_home
        # Existing legacy graph at ~/.kindex
        legacy = Store(Config(data_dir=str(home / ".kindex")))
        legacy.add_node("legacy knowledge")
        legacy.close()

        r = _run_cli(["profile", "create", "work",
                      "--data-dir", str(tmp_path / "work-data"),
                      "--roots", str(tmp_path / "work"), "--default"],
                     _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr
        assert "Warning" in r.stderr
        assert "legacy graph" in r.stderr

    def test_create_without_default_prints_remainder_note(self, cli_home,
                                                          tmp_path):
        home, project = cli_home
        r = _run_cli(["profile", "create", "work",
                      "--data-dir", str(tmp_path / "work-data"),
                      "--roots", str(tmp_path / "work")],
                     _cli_env(home, project), project)
        assert r.returncode == 0, r.stderr
        assert "no default_profile" in r.stderr
        assert "legacy-remainder" in r.stderr

    def test_two_step_adoption_is_quiet(self, cli_home, tmp_path):
        """The README-recommended flow (register legacy as default first)
        produces no orphaned-legacy warning."""
        home, project = cli_home
        legacy = Store(Config(data_dir=str(home / ".kindex")))
        legacy.add_node("legacy knowledge")
        legacy.close()
        env = _cli_env(home, project)

        r1 = _run_cli(["profile", "create", "personal",
                       "--data-dir", str(home / ".kindex"),
                       "--roots", str(tmp_path / "personal"), "--default"],
                      env, project)
        assert r1.returncode == 0, r1.stderr
        assert "Warning" not in r1.stderr

        r2 = _run_cli(["profile", "create", "work",
                       "--data-dir", str(tmp_path / "work-data"),
                       "--roots", str(tmp_path / "work")], env, project)
        assert r2.returncode == 0, r2.stderr
        assert "Warning" not in r2.stderr
