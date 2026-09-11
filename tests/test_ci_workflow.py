"""The complete test suite gates pull requests and release publication."""

from pathlib import Path

import yaml


def test_pr_and_release_share_the_full_test_job():
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    ci = yaml.load(
        (workflows / "ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    release = yaml.load(
        (workflows / "workflow.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert ci["name"] == "CI"
    assert ci["on"] == {
        "pull_request": "", "push": {"branches": ["main"]},
        "workflow_dispatch": "", "workflow_call": "",
    }
    assert release["name"] == "Publish to PyPI"
    assert release["on"] == {"push": {"tags": ["v*"]}}

    assert set(ci["jobs"]) == {"test"}
    test = ci["jobs"]["test"]
    assert "if" not in test
    assert "continue-on-error" not in test
    assert test["runs-on"] == "ubuntu-latest"
    assert [step["run"] for step in test["steps"] if "run" in step] == [
        'pip install -e ".[dev,mcp]"', "pytest",
    ]
    assert all("continue-on-error" not in step and "if" not in step
               for step in test["steps"])

    jobs = release["jobs"]
    assert jobs["ci"] == {"uses": "./.github/workflows/ci.yml"}
    assert jobs["build"]["needs"] == "ci"
    assert "if" not in jobs["build"]
    assert jobs["publish"]["needs"] == "build"
    assert "if" not in jobs["publish"]
