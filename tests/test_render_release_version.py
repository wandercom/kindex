"""Tests for rendering the package version in release builds."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-release-version.py"
SPEC = importlib.util.spec_from_file_location("render_release_version", SCRIPT)
assert SPEC and SPEC.loader
release_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_version)


def test_release_tag_renders_package_version(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "kindex"\nversion = "0.38.0"\n')

    assert release_version.render_release_version("v0.1.2", pyproject) == "0.1.2"
    assert 'version = "0.1.2"' in pyproject.read_text()


@pytest.mark.parametrize("tag", ["0.1.2", "v0.1", "v1.2.3rc1", "v1.2.3-rc1"])
def test_release_tag_must_use_three_part_numeric_v_format(tmp_path, tag):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.38.0"\n')

    with pytest.raises(ValueError, match="vX.Y.Z"):
        release_version.render_release_version(tag, pyproject)
