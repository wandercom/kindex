"""Cursor H3 appendix using Validator-supplied public native CLI metadata.

No product source read and no tests executed by the independent Tester.
"""
import datetime as dt
import hashlib
import json
import os

import pytest

from test_health import b, issues, session


SID = "d9e90d49-226b-40a3-bb8b-78f5ef990180"


def cursor_metadata(b, **changes):
    root = b.root / "isolated-cursor-config"
    b.env["CURSOR_CONFIG_DIR"] = str(root)
    workspace_id = hashlib.md5(str(b.project.resolve()).encode()).hexdigest()
    path = root / "chats" / workspace_id / SID / "meta.json"
    path.parent.mkdir(parents=True)
    created = b.now - dt.timedelta(days=4)
    updated = b.now - dt.timedelta(seconds=400)
    data = {"schemaVersion": 1, "createdAtMs": int(created.timestamp() * 1000),
            "updatedAtMs": int(updated.timestamp() * 1000), "hasConversation": True,
            "isSubagent": False, "cwd": str(b.project),
            "title": "SYNTHETIC_PRIVATE_CURSOR_TITLE_362", **changes}
    path.write_text(json.dumps(data))
    os.utime(path, (b.now.timestamp(), b.now.timestamp()))
    return path


def test_cursor_resumed_old_conversation_is_native_activity_with_honest_coverage(b):
    """H3/public Cursor fixture: recent update resumes an old CLI conversation.

    Mutation: ignore Cursor, use creation age, or invent IDE/native-use coverage.
    """
    cursor_metadata(b)
    report = b.check()
    found = [issue for issue in issues(report, "missing_hooks") if issue["scope"]["session_id"] == SID]
    assert len(found) == 1
    assert found[0]["scope"] == {"project_path": str(b.project), "agent": "cursor", "session_id": SID}
    assert session(report, SID)["activity_evidence"] == "native_observed"
    sources = report["coverage"]["cursor"]["sources"]
    assert sources["cli"] == "session_metadata"
    assert sources["ide"] == "unverified"
    assert sources["native_use"] == "unverified"
    assert "SYNTHETIC_PRIVATE_CURSOR_TITLE_362" not in json.dumps(report)
    for path in b.state.rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_PRIVATE_CURSOR_TITLE_362" not in path.read_bytes()


@pytest.mark.parametrize("case", ["no_conversation", "subagent", "stale_update"])
def test_cursor_nonactive_metadata_does_not_raise_missing_hook(b, case):
    """H3/public Cursor fixture: idle, non-conversation and subagent exemptions.

    Mutation: treat every fresh metadata file as active parent conversation.
    """
    changes = {}
    if case == "no_conversation":
        changes["hasConversation"] = False
    elif case == "subagent":
        changes["isSubagent"] = True
    else:
        changes["updatedAtMs"] = int((b.now - dt.timedelta(days=4)).timestamp() * 1000)
    cursor_metadata(b, **changes)
    report = b.check()
    assert not [issue for issue in issues(report, "missing_hooks") if issue["scope"]["session_id"] == SID]


def test_cursor_recent_metadata_without_cwd_reports_unidentified_coverage(b):
    """H3/public coverage: missing identity is unavailable, never a guessed scope.

    Mutation: infer project from process cwd/hash directory or claim healthy coverage.
    """
    path = cursor_metadata(b)
    data = json.loads(path.read_text())
    del data["cwd"]
    path.write_text(json.dumps(data))
    os.utime(path, (b.now.timestamp(), b.now.timestamp()))
    report = b.check()
    coverage = report["coverage"]["cursor"]
    assert coverage["unidentified"] >= 1
    unknown = [issue for issue in issues(report, "observation_unavailable")
               if issue["scope"]["agent"] == "cursor"]
    assert unknown
    assert all(issue["scope"]["project_path"] is None for issue in unknown)
    assert all(issue["scope"]["session_id"] is None for issue in unknown)
    assert not [issue for issue in issues(report, "missing_hooks") if issue["scope"]["session_id"] == SID]
