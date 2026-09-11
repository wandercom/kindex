"""Release surfaces that must move together."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import kindex
from kindex.archive import DEFAULT_ARCHIVE_MIN_AGE_DAYS
from kindex.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


def test_source_distribution_explicitly_excludes_private_runtime_state():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "/.kin/local" in excluded
    assert "/.kin/local/**" in excluded


def test_version_is_consistent_across_release_surfaces():
    version = kindex.__version__
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    registry = json.loads((ROOT / "server.json").read_text())
    card = json.loads(
        (ROOT / "docs/.well-known/mcp/server-card.json").read_text()
    )
    readme = (ROOT / "README.md").read_text()
    docs = (ROOT / "docs/index.html").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert project["version"] == version
    assert registry["version"] == version
    assert registry["packages"][0]["version"] == version
    assert card["serverInfo"]["version"] == version
    for manifest in (
        ".claude-plugin/plugin.json",
        "src/kindex/claude_modern/.claude-plugin/plugin.json",
    ):
        assert json.loads((ROOT / manifest).read_text())["version"] == version
    assert f"version-{version}-purple" in readme
    assert f"v{version}" in docs
    assert re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE)


def test_public_command_counts_match_registered_surfaces():
    docs = (ROOT / "docs/index.html").read_text()
    mcp_source = (ROOT / "src/kindex/mcp_server.py").read_text()
    mcp_tree = ast.parse(mcp_source)
    tool_count = sum(
        1
        for node in ast.walk(mcp_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "_tool"
            for decorator in node.decorator_list
        )
    )
    choices = next(
        action.choices
        for action in build_parser()._actions
        if getattr(action, "choices", None)
    )

    assert f"{tool_count} MCP Tools" in docs
    assert tool_count == 65
    assert len(choices) >= 80
    assert "80+ CLI Commands" in docs


def test_documented_archive_age_matches_the_runtime_default():
    age = f"{DEFAULT_ARCHIVE_MIN_AGE_DAYS} days"

    assert age in (ROOT / "README.md").read_text()
    assert age in (ROOT / "CHANGELOG.md").read_text()
    assert age in (ROOT / "docs/human-guide.md").read_text()
    assert age in (ROOT / "docs/llms-full.txt").read_text()


def test_documented_migration_snapshots_are_outside_merge_rotation():
    for relative_path in (
        "README.md",
        "CHANGELOG.md",
        "docs/human-guide.md",
        "docs/llms-full.txt",
    ):
        text = (ROOT / relative_path).read_text()
        assert "migrations/" in text
        assert "ten-file" in text
