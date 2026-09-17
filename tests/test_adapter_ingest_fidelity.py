"""Adapters record what the source says, and only that.

- claude-web never wrote an edge (add_edge got an unknown keyword), and
  re-ingesting a grown conversation reset what the user had set on its node.
- The code adapter never retired a deleted or renamed file or class, and
  exported the phantoms into the committed index.
- `kin watch` read only the old top-level transcript shape and ingested
  nothing; the session scans treated subagent transcripts as sessions.
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest

from kindex.adapters import code
from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    graph = Store(Config(data_dir=str(tmp_path / "kindex")))
    yield graph
    graph.close()


def conversation(uuid: str, messages: int, name: str = "Design chat") -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "created_at": "2026-01-01T00:00:00Z",
        "_project_name": "Kindex",
        "chat_messages": [
            {"sender": "human" if n % 2 == 0 else "assistant",
             "content": [{"type": "text", "text": f"Knowledge Graph design note {n}"}]}
            for n in range(messages)
        ],
    }


def test_claude_web_links_and_keeps_user_edits(store, tmp_path):
    from kindex.adapters.claude_web import ClaudeWebAdapter

    store.add_node("Knowledge Graph", "the graph", node_id="kg")
    store.add_node("Kindex", "the project", node_id="kindex-project")
    exports = tmp_path / "web"
    exports.mkdir()
    (exports / "abc-123.json").write_text(json.dumps(conversation("abc-123", 2)))
    adapter = ClaudeWebAdapter()
    result = adapter.ingest(store, directory=str(exports))
    assert result.created == 1
    targets = {edge["to_id"] for edge in store.edges_from("claude-web-abc-123")}
    assert "kindex-project" in targets, targets

    store.update_node("claude-web-abc-123", audience="team", aka=["design-chat"],
                      weight=0.9, intent="keep this", title="Renamed by me")
    (exports / "abc-123.json").write_text(json.dumps(conversation("abc-123", 4)))
    assert adapter.ingest(store, directory=str(exports)).updated == 1
    node = store.get_node("claude-web-abc-123")
    assert (node["audience"], node["aka"], node["weight"], node["intent"]) == (
        "team", ["design-chat"], 0.9, "keep this")
    assert node["title"] == "Renamed by me"
    assert node["extra"]["message_count"] == 4


def test_claude_web_follows_a_rename_it_made(store, tmp_path):
    from kindex.adapters.claude_web import ClaudeWebAdapter

    exports = tmp_path / "web"
    exports.mkdir()
    (exports / "abc-123.json").write_text(json.dumps(conversation("abc-123", 2)))
    adapter = ClaudeWebAdapter()
    adapter.ingest(store, directory=str(exports))
    (exports / "abc-123.json").write_text(
        json.dumps(conversation("abc-123", 4, name="Design chat, continued")))
    adapter.ingest(store, directory=str(exports))
    assert store.get_node("claude-web-abc-123")["title"] == "Design chat, continued"


def fake_ctags(files, root):
    tags = []
    for path in files:
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            match = re.match(r"class (\w+)", line)
            if match:
                tags.append({"path": str(path), "name": match.group(1), "kind": "class",
                             "line": line_no, "language": "Python"})
    return tags


@pytest.fixture
def local_code(monkeypatch):
    monkeypatch.setattr(code, "_run_ctags", fake_ctags)
    monkeypatch.setattr(code, "_check_cscope", lambda: False)
    monkeypatch.setattr(code, "_check_treesitter", lambda _lang: None)
    monkeypatch.setattr(code, "_detect_repo", lambda _path: None)


def live_code_titles(store):
    return sorted(node["title"] for node in store.all_nodes(limit=500)
                  if node["id"].startswith("code-") and node.get("status") == "active")


def test_code_ingest_retires_what_is_gone_and_restores_what_returns(store, tmp_path, local_code):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "alpha.py").write_text("class Alpha:\n    pass\nclass Gamma:\n    pass\n")
    (repo / "pkg" / "beta.py").write_text("class Beta:\n    pass\n")
    code.ingest_code(store, repo)
    assert live_code_titles(store) == ["Alpha", "Beta", "Gamma", "pkg/alpha.py", "pkg/beta.py"]

    beta = (repo / "pkg" / "beta.py").read_text()
    (repo / "pkg" / "beta.py").unlink()
    (repo / "pkg" / "alpha.py").write_text("class Alpha:\n    pass\n")
    result = code.ingest_code(store, repo)
    assert live_code_titles(store) == ["Alpha", "pkg/alpha.py"]
    assert any("retired 3" in warning for warning in result.warnings)

    (repo / "pkg" / "beta.py").write_text(beta)
    code.ingest_code(store, repo)
    assert live_code_titles(store) == ["Alpha", "Beta", "pkg/alpha.py", "pkg/beta.py"]


def test_code_ingest_leaves_hand_archived_and_other_scopes_alone(store, tmp_path, local_code):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "alpha.py").write_text("class Alpha:\n    pass\n")
    (repo / "pkg" / "beta.py").write_text("class Beta:\n    pass\n")
    code.ingest_code(store, repo)
    alpha = next(n for n in store.all_nodes(limit=50) if n["title"] == "Alpha")
    store.update_node(alpha["id"], status="archived")
    code.ingest_code(store, repo)
    assert store.get_node(alpha["id"])["status"] == "archived"

    other = tmp_path / "other"
    other.mkdir()
    (other / "solo.py").write_text("class Solo:\n    pass\n")
    code.ingest_code(store, other)
    assert "Beta" in live_code_titles(store)


def test_a_truncated_code_ingest_retires_nothing(store, tmp_path, local_code):
    repo = tmp_path / "repo"
    repo.mkdir()
    for n in range(4):
        (repo / f"m{n}.py").write_text(f"class C{n}:\n    pass\n")
    code.ingest_code(store, repo)
    for n in range(4):
        (repo / f"m{n}.py").write_text(f"class C{n}:\n    x = {n}\n")
    code.ingest_code(store, repo, limit=1)
    assert len(live_code_titles(store)) == 8


def assistant_line(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


def test_watch_ingests_current_transcripts_and_skips_subagents(tmp_path):
    from kindex.daemon import find_new_sessions, incremental_ingest

    claude = tmp_path / "claude"
    project = claude / "projects" / "-code-myproj"
    subagents = project / "11111111-2222" / "subagents"
    subagents.mkdir(parents=True)
    body = ("Refactored the Payment Reconciliation pipeline and the Ledger Service "
            "so the Settlement Report matches.")
    (project / "abcdef123456-session.jsonl").write_text(
        json.dumps(["not", "a", "dict"]) + "\n" + assistant_line(body) + "\n")
    (subagents / "agent-1.jsonl").write_text(assistant_line(body) + "\n")
    old = time.time() - 3600
    os.utime(subagents / "agent-1.jsonl", (old + 7200, old + 7200))

    cfg = Config(data_dir=str(tmp_path / "kindex"), claude_dir=str(claude))
    found = find_new_sessions(cfg, "2000-01-01T00:00:00")
    assert [path.name for path in found] == ["abcdef123456-session.jsonl"]
    graph = Store(cfg)
    try:
        assert incremental_ingest(cfg, graph, "2000-01-01T00:00:00") == 1
        node = graph.get_node("session-abcdef123456")
        assert node["title"] == "Session: -code-myproj"
    finally:
        graph.close()


def test_the_session_scan_skips_subagents(tmp_path):
    from kindex.ingest import session_transcripts

    projects = tmp_path / "projects"
    (projects / "p" / "s" / "subagents" / "workflows" / "wf_1").mkdir(parents=True)
    (projects / "p" / "session.jsonl").write_text("{}\n")
    (projects / "p" / "s" / "subagents" / "workflows" / "wf_1" / "agent.jsonl").write_text("{}\n")
    assert [path.name for path, _ in session_transcripts(projects)] == ["session.jsonl"]
