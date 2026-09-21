"""Hooks spend once, finish inside the host's budget, and answer in the
host's protocol even when they cannot run.

- The Stop hook read the transcript from the top on every turn, paying for
  the same extraction each time and never reaching later turns.
- prompt-check judged attention inline with an unbounded client; the host's
  two-second limit killed it mid-request.
- Antigravity hooks that could not resolve a multi-root workspace printed
  nothing (no PreToolUse decision) or plain text on a JSON surface, and the
  permission gate never ran.
- kin-mcp answered an ambiguous scope with a bare class name, and
  task_execute needed the legacy store only to name the agent.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store

SRC = str(Path(__file__).resolve().parents[1] / "src")


def isolate_global_config(monkeypatch, home):
    import kindex.config as config
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "_GLOBAL_PATHS", [home / ".config" / "kindex" / "kin.yaml"])


def assistant_line(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def turn(n: int) -> str:
    return f"Turn {n}: the scheduler keeps its retry budget at three attempts before paging."


def run_compact_hook(monkeypatch, tmp_path, transcript: Path, calls: list[str]):
    import kindex.extract as extract_module
    from kindex.cli import build_parser, cmd_compact_hook

    def fake_extract(text, existing, config, ledger, timeout=None):
        calls.append((text, timeout))
        return {"concepts": [], "connections": []}

    monkeypatch.setattr(extract_module, "extract", fake_extract)
    envelope = json.dumps({"hook_event_name": "Stop", "session_id": "s1",
                           "transcript_path": str(transcript), "stop_hook_active": False})
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope))
    args = build_parser().parse_args(["compact-hook", "--data-dir", str(tmp_path / "data")])
    cmd_compact_hook(args)


def test_each_turn_is_extracted_once(monkeypatch, tmp_path):
    isolate_global_config(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(assistant_line(turn(n)) for n in range(3)) + "\n")
    calls: list = []
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    assert len(calls) == 1, "the same turns were extracted twice"
    assert all(turn(n) in calls[0][0] for n in range(3))
    with transcript.open("a") as handle:
        handle.write(assistant_line(turn(3)) + "\n")
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    assert len(calls) == 2
    assert turn(3) in calls[1][0] and turn(0) not in calls[1][0]
    assert calls[1][1] is not None and calls[1][1] < 10, "the request is bounded by the hook"


def test_a_line_still_being_written_is_read_next_turn(monkeypatch, tmp_path):
    isolate_global_config(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    whole = assistant_line(turn(1))
    transcript.write_text(assistant_line(turn(0)) + "\n" + whole[:40])
    calls: list = []
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    assert len(calls) == 1 and turn(1) not in calls[0][0]
    transcript.write_text(assistant_line(turn(0)) + "\n" + whole + "\n")
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    assert len(calls) == 2 and turn(1) in calls[1][0] and turn(0) not in calls[1][0]


def test_a_finished_transcript_keeps_its_unterminated_last_line(tmp_path):
    from kindex.ingest import _extract_session_text
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(assistant_line(turn(0)) + "\n" + assistant_line(turn(1)))
    assert turn(1) in _extract_session_text(transcript)


def test_prompt_check_waits_only_within_its_budget(monkeypatch, tmp_path):
    isolate_global_config(monkeypatch, tmp_path)
    import kindex.attention as attention
    from kindex.cli import build_parser, cmd_prompt_check

    seen: dict = {}

    def inline(*a, **k):
        seen["inline"] = True
        return {"injections": []}

    def prepare(store, cfg, text, conversation_id, **kwargs):
        return {"status": "queued", "job": {"job_id": "job-1"}, "ticks": 0}

    def wait(store, cfg, conversation_id, snippet, *, tick, job_id, deadline):
        seen["budget"] = deadline - time.monotonic()
        return []

    monkeypatch.setattr(attention, "run_attention_check", inline)
    monkeypatch.setattr(attention, "prepare_async_attention_review", prepare)
    monkeypatch.setattr(attention, "wait_for_pending_attention", wait)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
               "prompt": "please deploy the release now"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    args = build_parser().parse_args(["prompt-check", "--data-dir", str(tmp_path / "data")])
    cmd_prompt_check(args)
    assert "inline" not in seen, "attention was judged inside the hook"
    assert 0 < seen["budget"] <= 1.0


def test_hook_llm_client_has_a_deadline_and_no_retries(monkeypatch):
    pytest.importorskip("anthropic")
    from kindex.llm import get_client
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    cfg = Config(llm={"enabled": True, "provider": "anthropic"})
    hooked = get_client(cfg, timeout=6.0)
    assert (hooked.timeout, hooked.max_retries) == (6.0, 0)
    command = get_client(cfg)
    assert command.timeout <= 60.0


# ── Antigravity: a scope that cannot resolve still gets a protocol answer ──

@pytest.fixture
def multi_root(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "kindex").mkdir(parents=True)
    roots = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        roots.append(str(root))
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_STATE_HOME": str(home / ".local" / "state"),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""), SRC) if p),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return {"tmp": tmp_path, "roots": roots, "env": env}


def hook(world, *args, payload):
    return subprocess.run([sys.executable, "-m", "kindex.cli", *args], cwd=world["tmp"],
                          env=world["env"], input=json.dumps(payload), text=True,
                          capture_output=True, timeout=60)


def test_pre_tool_use_answers_with_a_decision(multi_root):
    result = hook(multi_root, "attention-hook", "--adapter", "antigravity", "--event", "PreToolUse",
                  payload={"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}},
                           "workspacePaths": multi_root["roots"]})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "allow"


def test_the_permission_gate_runs_before_the_scope_is_resolved(multi_root):
    result = hook(multi_root, "attention-hook", "--adapter", "antigravity", "--event", "PreToolUse",
                  payload={"toolCall": {"name": "run_command",
                                        "args": {"CommandLine": "kin config set sim.enabled false"}},
                           "workspacePaths": multi_root["roots"]})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "force_ask", result.stdout


def test_prime_and_stop_answer_in_json(multi_root):
    prime = hook(multi_root, "agent-prime-hook", "--adapter", "antigravity", "--client", "antigravity",
                 payload={"workspacePaths": multi_root["roots"], "conversationId": "c1"})
    assert "injectSteps" in json.loads(prime.stdout), prime.stdout
    stop = hook(multi_root, "agent-stop-hook", "--adapter", "antigravity",
                payload={"workspacePaths": multi_root["roots"], "conversationId": "c1"})
    assert json.loads(stop.stdout)["decision"] == "", stop.stdout


# ── kin-mcp ─────────────────────────────────────────────────────────────

@pytest.fixture
def ambiguous_mcp(tmp_path, monkeypatch):
    import kindex.mcp_server as mcp_server
    home = tmp_path / "home"
    project = tmp_path / "acme-client-portal"
    (home / ".config" / "kindex").mkdir(parents=True)
    (project / ".kin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    (project / ".kin" / "config").write_text("name: mcp-fixture\n")
    for data_dir, node_id in ((home / ".kindex", "home-node"),
                              (project / ".kin" / "local" / "kindex", "project-node")):
        store = Store(Config(data_dir=str(data_dir)))
        store.add_node(title=f"Synthetic {node_id}", node_id=node_id)
        store.close()
    isolate_global_config(monkeypatch, home)
    monkeypatch.delenv("KIN_PROJECT", raising=False)
    monkeypatch.delenv("KIN_AGENT_ID", raising=False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_config", None)
    return {"project": project, "mcp": mcp_server}


def test_unconfigured_mcp_status_uses_the_legacy_home_graph(ambiguous_mcp):
    result = ambiguous_mcp["mcp"].status()
    assert not result.startswith("Error: memory unavailable"), result
    assert "Nodes:" in result
    nodes = ambiguous_mcp["mcp"].list_nodes()
    assert "Synthetic home-node" in nodes
    assert "Synthetic project-node" not in nodes


def test_task_execute_needs_no_legacy_store(ambiguous_mcp, monkeypatch):
    import kindex.integrations as integrations
    monkeypatch.setattr(integrations, "_signet", lambda command, payload: None)
    result = ambiguous_mcp["mcp"].task_execute(
        "list", {}, project_path=str(ambiguous_mcp["project"]), session_id="s1", agent="")
    assert result.get("ok") is True, result


@pytest.mark.parametrize("inferred_agent", ["Jane Doe@host", "agent[blue]@host", "μser@host"])
def test_task_execute_uses_project_default_for_invalid_inferred_agent(
        ambiguous_mcp, monkeypatch, inferred_agent):
    import kindex.integrations as integrations

    observed = {}
    monkeypatch.setattr(ambiguous_mcp["mcp"], "_default_agent", lambda agent: inferred_agent)
    monkeypatch.setattr(
        integrations, "execute_task",
        lambda store, operation, arguments, scope, **kwargs: observed.update(scope) or {"ok": True},
    )

    result = ambiguous_mcp["mcp"].task_execute(
        "list", {}, project_path=str(ambiguous_mcp["project"]), session_id="s1", agent="")

    assert result == {"ok": True}
    assert observed["agent"] == "claude"


def test_task_execute_refuses_invalid_explicit_agent(ambiguous_mcp):
    result = ambiguous_mcp["mcp"].task_execute(
        "list", {}, project_path=str(ambiguous_mcp["project"]), session_id="s1", agent="Jane Doe")

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_scope"


def test_a_finished_transcript_is_read_past_the_live_byte_bound(tmp_path):
    from kindex.ingest import _extract_session_text
    transcript = tmp_path / "t.jsonl"
    tool_output = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 60000}]}})
    with transcript.open("w") as handle:
        for _ in range(80):  # about 4.8 MiB of tool output before any assistant text
            handle.write(tool_output + "\n")
        handle.write(assistant_line(turn(9)) + "\n")
    assert transcript.stat().st_size > 4 * 1024 * 1024
    assert turn(9) in _extract_session_text(transcript)


def test_requested_context_survives_a_transcript_the_stop_hook_consumed(
        monkeypatch, tmp_path, capsys):
    isolate_global_config(monkeypatch, tmp_path)
    import kindex.retrieve as retrieve
    from kindex.cli import build_parser, cmd_compact_hook

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(assistant_line(turn(n)) for n in range(3)) + "\n")
    calls: list = []
    run_compact_hook(monkeypatch, tmp_path, transcript, calls)
    assert len(calls) == 1
    capsys.readouterr()

    topics: list[str] = []
    monkeypatch.setattr(retrieve, "hybrid_search",
                        lambda store, topic, top_k=5: topics.append(topic) or [])
    monkeypatch.setattr(retrieve, "format_context_block",
                        lambda store, results, query, level: "CONTEXT BLOCK")
    envelope = json.dumps({"hook_event_name": "PreCompact", "session_id": "s1",
                           "transcript_path": str(transcript)})
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope))
    args = build_parser().parse_args(
        ["compact-hook", "--emit-context", "--data-dir", str(tmp_path / "data")])
    cmd_compact_hook(args)
    assert len(calls) == 1, "the consumed turns were extracted again"
    assert "CONTEXT BLOCK" in capsys.readouterr().out
    assert topics and "Turn 0" in topics[0]


def test_the_drain_judges_with_the_hooks_client_overrides(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import kindex.attention as attention
    from kindex.agent_settings import apply_agent_overrides
    from kindex.config import AttentionConfig, BudgetConfig, LLMConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(attention, "spawn_background_attention_drain", lambda config: True)
    base = Config(
        data_dir=str(tmp_path),
        llm=LLMConfig(enabled=True),
        budget=BudgetConfig(daily=1.0, weekly=5.0, monthly=10.0),
        attention=AttentionConfig(enabled=False, max_check_cost=0.05,
                                  max_conversation_cost=0.5),
        agents={"instances": {"claude:s1": {"client": "claude",
                                            "attention": {"enabled": True}}}},
    )
    store = Store(base)
    node_id = store.add_node("Deploy checklist", node_type="directive",
                             extra={"attention_triggers": ["deploy"]})
    hook_config = apply_agent_overrides(base, client="claude", instance_key="claude:s1")
    prepared = attention.prepare_async_attention_review(
        store, hook_config, "deploy this", "conv-1", force=True,
        adapter="claude", client="claude", agent_instance="claude:s1")
    assert prepared["status"] == "queued"

    calls: list = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            payload = {"inject": [{"id": f"node:{node_id}", "message": "Verify the deploy.",
                                   "reason": "deploying", "confidence": 0.9}]}
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(payload))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                      cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0))

    drained = attention.drain_attention_queue(
        store, base, client=SimpleNamespace(messages=Messages()))
    assert calls, "the drain dropped a job its instance enabled"
    assert drained["flagged"] == 1
    store.close()


def test_requested_context_finds_the_topic_past_a_long_stretch_of_tool_output(
        monkeypatch, tmp_path, capsys):
    isolate_global_config(monkeypatch, tmp_path)
    import kindex.retrieve as retrieve
    from kindex.cli import build_parser, cmd_compact_hook

    transcript = tmp_path / "t.jsonl"
    tool_output = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 60000}]}})
    with transcript.open("w") as handle:
        for _ in range(80):  # past both the live window and one scan window
            handle.write(tool_output + "\n")
        for n in range(3):
            handle.write(assistant_line(turn(n)) + "\n")
    topics: list[str] = []
    monkeypatch.setattr(retrieve, "hybrid_search",
                        lambda store, topic, top_k=5: topics.append(topic) or [])
    monkeypatch.setattr(retrieve, "format_context_block",
                        lambda store, results, query, level: "CONTEXT BLOCK")
    calls: list = []
    import kindex.extract as extract_module
    monkeypatch.setattr(extract_module, "extract",
                        lambda text, existing, config, ledger, timeout=None:
                        calls.append(text) or {"concepts": [], "connections": []})

    def run(event, *flags):
        envelope = json.dumps({"hook_event_name": event, "session_id": "s1",
                               "transcript_path": str(transcript)})
        monkeypatch.setattr(sys, "stdin", io.StringIO(envelope))
        cmd_compact_hook(build_parser().parse_args(
            ["compact-hook", *flags, "--data-dir", str(tmp_path / "data")]))

    run("PreCompact", "--emit-context")
    assert "CONTEXT BLOCK" in capsys.readouterr().out
    assert topics[-1].startswith("Turn 0")
    # Stop consumes the turns; a later PreCompact reuses the kept topic.
    run("Stop")
    run("Stop")
    assert calls and turn(2) in calls[-1]
    run("PreCompact", "--emit-context")
    assert "CONTEXT BLOCK" in capsys.readouterr().out
    assert topics[-1].startswith("Turn 0")
