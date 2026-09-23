"""Tests for project-scoped .kin config and work policy."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.config import load_config, resolve_project_root


def _write_kin_config(directory: Path, content: str) -> Path:
    kin_dir = directory / ".kin"
    kin_dir.mkdir(exist_ok=True)
    config = kin_dir / "config"
    config.write_text(content)
    return config


def test_load_config_from_explicit_project_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_kin_config(
        project,
        "data_dir: /tmp/kindex-project\n"
        "work_policy:\n"
        "  require_active_tag: true\n"
        "  linear:\n"
        "    enabled: true\n"
        "    require_issue: true\n"
        "    team: ENG\n",
    )

    cfg = load_config(project_path=project)

    assert cfg.data_dir == "/tmp/kindex-project"
    assert cfg.work_policy.require_active_tag is True
    assert cfg.work_policy.linear.enabled is True
    assert cfg.work_policy.linear.require_issue is True
    assert cfg.work_policy.linear.team == "ENG"


def test_code_ingest_from_kin_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_kin_config(
        project,
        "code_ingest:\n"
        "  unity: true\n"
        "  include_extensions:\n"
        "    .shader: Unity Shader\n",
    )

    cfg = load_config(project_path=project)

    assert cfg.code_ingest.unity is True
    assert cfg.code_ingest.include_extensions == {".shader": "Unity Shader"}


def test_code_ingest_defaults_off(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_kin_config(project, "data_dir: /tmp/kindex-project\n")

    cfg = load_config(project_path=project)

    assert cfg.code_ingest.unity is False
    assert cfg.code_ingest.include_extensions == {}


def test_kin_project_config_inheritance_merges_lists_and_policy(tmp_path):
    org = tmp_path / "org"
    project = tmp_path / "project"
    org.mkdir()
    project.mkdir()
    org_config = _write_kin_config(
        org,
        "domains: [engineering]\n"
        "work_policy:\n"
        "  require_active_tag: true\n",
    )
    _write_kin_config(
        project,
        f"inherits:\n  - {org_config}\n"
        "domains: [python]\n"
        "work_policy:\n"
        "  linear:\n"
        "    enabled: true\n",
    )

    cfg = load_config(project_path=project)

    assert cfg.work_policy.require_active_tag is True
    assert cfg.work_policy.linear.enabled is True


def test_resolve_project_root_prefers_kin_project_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KIN_PROJECT", str(project))

    assert resolve_project_root() == project.resolve()


def test_resolve_project_root_prefers_kin_project_path_over_kin_project(tmp_path, monkeypatch):
    declared = tmp_path / "declared"
    legacy = tmp_path / "legacy"
    declared.mkdir()
    legacy.mkdir()
    monkeypatch.setenv("KIN_PROJECT_PATH", str(declared))
    monkeypatch.setenv("KIN_PROJECT", str(legacy))

    assert resolve_project_root() == declared.resolve()


def test_resolve_project_root_names_missing_kin_project_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_PROJECT_PATH", str(tmp_path / "missing"))

    with pytest.raises(ValueError, match="KIN_PROJECT_PATH"):
        resolve_project_root()


def test_current_user_prefers_repo_local_git_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    empty_config = tmp_path / "empty.yaml"
    empty_config.write_text("")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Repo User"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    cfg = load_config(config_path=empty_config, project_path=project)

    assert cfg.current_user == "repo-user"


def test_current_user_uses_global_git_config_when_local_missing(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    empty_config = tmp_path / "empty.yaml"
    empty_config.write_text("")
    git_config = tmp_path / "gitconfig"
    git_config.write_text("[user]\n\tname = Global User\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)

    cfg = load_config(config_path=empty_config, project_path=project)

    assert cfg.current_user == "global-user"


def test_policy_check_allows_absent_policy(tmp_path):
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    _write_kin_config(project, "name: personal-project\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kindex.cli",
            "policy",
            "check",
            "--project-path",
            str(project),
            "--data-dir",
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "Policy check passed" in result.stdout


def test_policy_check_blocks_linear_only_when_enabled(tmp_path):
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    _write_kin_config(
        project,
        "work_policy:\n"
        "  linear:\n"
        "    enabled: true\n"
        "    require_issue: true\n",
    )

    env = os.environ.copy()
    env.pop("KIN_LINEAR_ID", None)
    env.pop("LINEAR_ISSUE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kindex.cli",
            "policy",
            "check",
            "--strict",
            "--project-path",
            str(project),
            "--data-dir",
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 1
    assert "Linear issue required" in result.stderr


def test_config_set_with_project_path_writes_project_kin_config(tmp_path):
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kindex.cli",
            "config",
            "set",
            "work_policy.require_active_tag",
            "true",
            "--project-path",
            str(project),
            "--data-dir",
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert (project / ".kin" / "config").exists()
    cfg = load_config(project_path=project)
    assert cfg.work_policy.require_active_tag is True
