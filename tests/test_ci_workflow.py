"""The complete test suite gates pull requests and release publication."""

from pathlib import Path

import yaml


def test_pr_and_release_share_the_full_test_job():
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/workflow.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    triggers = workflow["on"]
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["tags"] == ["v*"]
    assert not triggers["pull_request"]  # No path or activity filters exclude tests.
    assert "paths" not in triggers["push"]
    assert "paths-ignore" not in triggers["push"]

    jobs = workflow["jobs"]
    test = jobs["test"]
    assert "if" not in test
    assert "continue-on-error" not in test
    assert test["runs-on"] == "ubuntu-latest"
    assert [step["run"] for step in test["steps"] if "run" in step] == [
        'pip install -e ".[dev,mcp]"', "pytest",
    ]
    assert all("continue-on-error" not in step and "if" not in step
               for step in test["steps"])
    assert jobs["build"]["needs"] == "test"
    assert jobs["build"]["if"] == (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    )
    assert jobs["publish"]["needs"] == "build"
    assert "if" not in jobs["publish"]
