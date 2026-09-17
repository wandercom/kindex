"""A release tag must name the committed project version."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

import kindex


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-release-version.py"
SPEC = importlib.util.spec_from_file_location("check_release_version", SCRIPT)
assert SPEC and SPEC.loader
release_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_version)


def test_a_tag_naming_the_committed_version_passes_unchanged(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    text = '[project]\nname = "kindex"\nversion = "0.1.2"\n'
    pyproject.write_text(text)

    assert release_version.check_release_version("v0.1.2", pyproject) == "0.1.2"
    assert pyproject.read_text() == text


def test_a_tag_ahead_of_the_committed_version_is_refused(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "kindex"\nversion = "0.38.0"\n')

    with pytest.raises(ValueError, match="does not name the committed project version 0.38.0"):
        release_version.check_release_version("v0.38.1", pyproject)
    assert 'version = "0.38.0"' in pyproject.read_text()


@pytest.mark.parametrize("tag", ["0.1.2", "v0.1", "v1.2.3rc1", "v1.2.3-rc1"])
def test_release_tag_must_use_three_part_numeric_v_format(tmp_path, tag):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.38.0"\n')

    with pytest.raises(ValueError, match="vX.Y.Z"):
        release_version.check_release_version(tag, pyproject)


def test_the_tag_being_built_is_this_version():
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF_TYPE") != "tag" or not tag.startswith("v"):
        pytest.skip("not a tag build")
    assert tag == f"v{kindex.__version__}"
