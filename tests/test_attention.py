"""Tests for conversation-attention reminder injection."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import kindex.attention as attention
from kindex.agent_adapters import (
    adapter_scoped_out,
    permission_gate_output,
    render_hook_context,
)
from kindex.cli import build_parser
from kindex.attention import (
    ATTENTION_PENDING_META,
    ATTENTION_QUEUE_META,
    _prepare_attention_job,
    drain_attention_queue,
    enqueue_attention_review,
    estimate_message_window,
    extract_conversation_text,
    is_background_action,
    pop_pending_attention_injections,
    resolve_conversation_id,
    run_attention_check,
    runtime_status,
    select_candidates,
    set_runtime_enabled,
    wait_for_pending_attention,
)
from kindex.budget import BudgetLedger
from kindex.config import AttentionConfig, BudgetConfig, Config, LLMConfig
from kindex.store import Store


class _MockMessages:
    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        payload = {
            "inject": [{
                "id": self.candidate_id,
                "message": "Before deploying, verify tests, version, artifact, and live endpoint.",
                "reason": "The conversation is about deploying.",
                "confidence": 0.91,
            }]
        }
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(payload))],
            usage=SimpleNamespace(
                input_tokens=120,
                output_tokens=35,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


class _MockClient:
    def __init__(self, candidate_id: str):
        self.messages = _MockMessages(candidate_id)


def _config(tmp_path, *, enabled=True, tick_interval=1, check_budget=0.05):
    return Config(
        data_dir=str(tmp_path),
        llm=LLMConfig(enabled=True),
        budget=BudgetConfig(daily=1.0, weekly=5.0, monthly=10.0),
        attention=AttentionConfig(
            enabled=enabled,
            tick_interval=tick_interval,
            max_check_cost=check_budget,
            max_conversation_cost=0.50,
        ),
    )


def test_select_candidates_matches_explicit_trigger(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        content="Any time you deploy, verify tests and live endpoint.",
        extra={"attention_triggers": ["deploy", "release", "go live"]},
    )

    candidates = select_candidates(store, "Let's deploy this now.", cfg)

    assert candidates
    assert candidates[0].id == f"node:{node_id}"
    assert "trigger:deploy" in candidates[0].reason
    store.close()


def test_adapter_scoped_out_filters_foreign_client():
    ag_tags = ["antigravity", "hooks"]
    # A bare coined client name (antigravity) IS a scoping signal: noise for Claude/Codex...
    assert adapter_scoped_out(ag_tags, "claude") is True
    assert adapter_scoped_out(ag_tags, "codex") is True
    assert adapter_scoped_out("antigravity, hooks", "claude") is True
    # ...but in scope for Antigravity (incl. the "ag" alias as the running client)...
    assert adapter_scoped_out(ag_tags, "antigravity") is False
    assert adapter_scoped_out(ag_tags, "ag") is False
    # ...nodes with no client tag apply everywhere...
    assert adapter_scoped_out(["deploy", "release"], "claude") is False
    assert adapter_scoped_out([], "antigravity") is False
    # ...and an unknown / plain caller can't scope, so it never filters.
    assert adapter_scoped_out(ag_tags, "plain") is False
    assert adapter_scoped_out(ag_tags, None) is False


def test_adapter_scoped_out_does_not_overfilter_topical_tags():
    # Bare AMBIGUOUS client names are NOT inferred as scope — they are routinely
    # topical (a `gemini` task about the Gemini API, a `cursor` text cursor, the
    # `ag` 2-char alias colliding with silver/agriculture/Attorney-General).
    assert adapter_scoped_out(["gemini"], "claude") is False
    assert adapter_scoped_out(["gemini", "research"], "antigravity") is False
    assert adapter_scoped_out(["cursor"], "claude") is False
    assert adapter_scoped_out(["codex"], "claude") is False
    assert adapter_scoped_out(["ag"], "claude") is False
    # Explicit markers ARE authoritative for any client (alias-resolved).
    assert adapter_scoped_out(["client:gemini"], "claude") is True
    assert adapter_scoped_out(["client:gemini"], "gemini") is False
    assert adapter_scoped_out(["agent:codex"], "claude") is True
    assert adapter_scoped_out(["client:ag"], "claude") is True  # ag -> antigravity
    assert adapter_scoped_out(["client:ag"], "antigravity") is False


def test_select_candidates_excludes_other_adapter_scoped_nodes(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    ag_id = store.add_node(
        "Antigravity PreToolUse schema",
        node_type="directive",
        content="Antigravity PreToolUse stdin is nested camelCase JSON: toolCall.name / toolCall.args.",
        domains=["antigravity", "hooks"],
        extra={"attention_triggers": ["pretooluse", "toolcall"]},
    )
    snippet = "tool_name: Bash\ntool_input: PreToolUse toolCall run command"

    # The Antigravity directive must not surface for Claude or Codex sessions.
    claude = select_candidates(store, snippet, cfg, adapter="claude")
    assert all(c.id != f"node:{ag_id}" for c in claude)
    codex = select_candidates(store, snippet, cfg, adapter="codex")
    assert all(c.id != f"node:{ag_id}" for c in codex)

    # But it is in scope when Antigravity itself is the running client.
    ag = select_candidates(store, snippet, cfg, adapter="antigravity")
    assert any(c.id == f"node:{ag_id}" for c in ag)
    store.close()


def test_select_candidates_keeps_unscoped_nodes_for_every_adapter(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        content="Any time you deploy, verify tests and live endpoint.",
        extra={"attention_triggers": ["deploy"]},
    )
    for adapter in ("claude", "codex", "antigravity", None):
        candidates = select_candidates(
            store, "Let's deploy this now.", cfg, adapter=adapter
        )
        assert any(c.id == f"node:{node_id}" for c in candidates), adapter
    store.close()


def test_select_candidates_keeps_topically_tagged_client_name(tmp_path):
    """A node tagged with an ambiguous client name for TOPICAL reasons must not be dropped."""
    cfg = _config(tmp_path)
    store = Store(cfg)
    # Real-world shape: a directive about the Gemini API, tagged `gemini` topically.
    node_id = store.add_node(
        "Gemini API safety settings",
        node_type="directive",
        content="When calling the Gemini API, always set safety settings.",
        domains=["gemini", "api"],
        extra={"attention_triggers": ["gemini"]},
    )
    # It is not client-scoped, so it must still surface in a Claude session.
    candidates = select_candidates(
        store, "calling the gemini api now", cfg, adapter="claude"
    )
    assert any(c.id == f"node:{node_id}" for c in candidates)
    store.close()


def test_select_candidates_matches_reminder_trigger(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    rid = store.add_reminder(
        "Production release note",
        "2099-01-01T10:00:00",
        body="Remember to publish the release note.",
        extra={"attention_triggers": ["release", "ship"]},
    )

    candidates = select_candidates(store, "We are going to ship this.", cfg)

    assert any(c.id == f"reminder:{rid}" for c in candidates)
    store.close()


def test_select_candidates_scopes_reminders_by_conversation_id(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    chat_a = store.add_reminder(
        "Chat A release note",
        "2099-01-01T10:00:00",
        extra={"attention_triggers": ["ship"], "conversation_id": "chat-a"},
    )
    chat_b = store.add_reminder(
        "Chat B release note",
        "2099-01-01T10:00:00",
        extra={"attention_triggers": ["ship"], "conversation_id": "chat-b"},
    )
    legacy = store.add_reminder(
        "Legacy release note",
        "2099-01-01T10:00:00",
        extra={"attention_triggers": ["ship"]},
    )
    global_id = store.add_reminder(
        "Global release note",
        "2099-01-01T10:00:00",
        extra={"attention_triggers": ["ship"], "reminder_scope": "global"},
    )

    candidates = select_candidates(
        store,
        "We are going to ship this.",
        cfg,
        conversation_id="chat-a",
    )
    ids = {c.id for c in candidates}

    assert f"reminder:{chat_a}" in ids
    assert f"reminder:{global_id}" in ids
    assert f"reminder:{chat_b}" not in ids
    assert f"reminder:{legacy}" not in ids
    store.close()


def test_resolve_conversation_id_accepts_client_aliases(monkeypatch):
    assert resolve_conversation_id(hook_payload={"chat_id": "chat-1"}) == "chat-1"
    assert resolve_conversation_id(hook_payload={"conversationId": "conv-1"}) == "conv-1"
    assert resolve_conversation_id(hook_payload={"session": {"id": "session-1"}}) == "session-1"

    monkeypatch.setenv("KIN_CHAT_ID", "env-chat")
    assert resolve_conversation_id(fallback_to_cwd=False) == "env-chat"


def test_resolve_conversation_id_can_disable_cwd_fallback(monkeypatch):
    for key in (
        "KIN_CHAT_ID",
        "KIN_CONVERSATION_ID",
        "CLAUDE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CODEX_CONVERSATION_ID",
        "OPENCODE_SESSION_ID",
        "OPENCODE_CHAT_ID",
        "CURSOR_SESSION_ID",
        "CURSOR_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    assert resolve_conversation_id(fallback_to_cwd=False) == ""


def test_candidate_trigger_derivation_uses_word_boundaries(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    store.add_node(
        "Unrelated validation task",
        node_type="task",
        content="Fix validation in contract parsing after adoption runs.",
    )

    candidates = select_candidates(store, "Let's deploy Kindex now.", cfg)

    assert not any(c.reason == "trigger:in" for c in candidates)
    store.close()


def test_candidate_trigger_derivation_does_not_use_on_project_name(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    store.add_node(
        "Watch flagged on Kindex rust support",
        node_type="watch",
        content="This should not become a Kindex trigger just because the phrase says on Kindex.",
    )

    candidates = select_candidates(store, "Let's deploy Kindex now.", cfg)

    assert not any("trigger:kindex" in c.reason for c in candidates)
    store.close()


def test_attention_noops_without_llm_configured(tmp_path):
    cfg = Config(
        data_dir=str(tmp_path),
        llm=LLMConfig(enabled=False),
        attention=AttentionConfig(enabled=True, tick_interval=1),
    )
    store = Store(cfg)
    store.add_node(
        "Deploy checklist",
        node_type="directive",
        extra={"attention_triggers": ["deploy"]},
    )
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)

    result = run_attention_check(
        store,
        cfg,
        ledger,
        "deploy this",
        "conv-1",
        force=True,
        client=_MockClient("node:any"),
    )

    assert result["status"] == "llm_not_configured"
    assert result["injections"] == []
    assert ledger.entries == []
    store.close()


def test_attention_uses_ticks_and_records_conversation_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path, tick_interval=2)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        content="Any time you deploy, verify tests, changelog, version, artifact, and live endpoint.",
        extra={"attention_triggers": ["deploy"]},
    )
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    client = _MockClient(f"node:{node_id}")

    first = run_attention_check(store, cfg, ledger, "deploy this", "conv-1", client=client)
    second = run_attention_check(store, cfg, ledger, "deploy this", "conv-1", client=client)

    assert first["status"] == "waiting_for_tick"
    assert second["status"] == "ok"
    assert second["injections"][0]["id"] == f"node:{node_id}"
    assert client.messages.calls == 1
    assert ledger.entries[0]["purpose"] == "attention"
    assert ledger.entries[0]["conversation_id"] == "conv-1"
    assert ledger.conversation_spend("conv-1", purpose="attention") > 0
    store.close()


def test_attention_cooldown_suppresses_repeat(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        extra={"attention_triggers": ["deploy"]},
    )
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)

    first_client = _MockClient(f"node:{node_id}")
    second_client = _MockClient(f"node:{node_id}")
    first = run_attention_check(
        store, cfg, ledger, "deploy this", "conv-1", force=True, client=first_client,
    )
    second = run_attention_check(
        store, cfg, ledger, "deploy this", "conv-1", force=True, client=second_client,
    )

    assert first["injections"]
    assert second["status"] == "no_candidates"
    assert second_client.messages.calls == 0
    store.close()


def test_attention_estimate_respects_check_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path, check_budget=0.000001)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        extra={"attention_triggers": ["deploy"]},
    )
    ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    client = _MockClient(f"node:{node_id}")

    result = run_attention_check(
        store,
        cfg,
        ledger,
        "deploy this",
        "conv-1",
        force=True,
        client=client,
    )

    assert result["status"] == "estimate_exceeds_check_budget"
    assert client.messages.calls == 0
    assert ledger.entries == []
    store.close()


def test_async_attention_delivers_fast_result_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        extra={"attention_triggers": ["deploy"]},
    )
    prepared = _prepare_attention_job(store, cfg, "deploy this", "conv-1", force=True)
    assert prepared["status"] == "queued"
    assert enqueue_attention_review(store, cfg, prepared["job"]) is True

    client = _MockClient(f"node:{node_id}")
    drained = drain_attention_queue(store, cfg, client=client)
    assert drained["reviewed"] == 1
    assert drained["flagged"] == 1
    assert store.get_meta(ATTENTION_QUEUE_META) == "[]"

    injections = pop_pending_attention_injections(
        store,
        cfg,
        "conv-1",
        "deploy this",
        tick=prepared["ticks"],
        job_id=prepared["job"]["job_id"],
    )
    assert len(injections) == 1
    assert injections[0].id == f"node:{node_id}"
    assert store.get_meta(ATTENTION_PENDING_META) == "[]"
    store.close()


def test_async_drain_scopes_candidates_by_adapter(tmp_path, monkeypatch):
    """End-to-end: the drain must not deliver another client's scoped node."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path)
    store = Store(cfg)
    ag_id = store.add_node(
        "Antigravity PreToolUse schema",
        node_type="directive",
        content="Antigravity PreToolUse stdin is nested camelCase JSON: toolCall.name / toolCall.args.",
        domains=["antigravity", "hooks"],
        extra={"attention_triggers": ["pretooluse", "toolcall"]},
    )
    snippet = "tool_name: Bash\ntool_input: PreToolUse toolCall run command"

    def _drain_for(adapter: str, conv: str):
        job = {
            "job_id": f"job-{adapter}",
            "conversation_id": conv,
            "snippet": snippet,
            "force": True,
            "adapter": adapter,
            "at": "2099-01-01T00:00:00",
            "attempts": 0,
        }
        assert enqueue_attention_review(store, cfg, job) is True
        drain_attention_queue(store, cfg, client=_MockClient(f"node:{ag_id}"))
        return pop_pending_attention_injections(
            store, cfg, conv, snippet, tick=1, job_id=f"job-{adapter}"
        )

    # The Antigravity-scoped directive is filtered out of candidate selection for
    # Claude, so the judge can never inject it — nothing is delivered.
    assert _drain_for("claude", "conv-claude") == []
    # Antigravity itself keeps the directive in scope and receives it.
    ag_injections = _drain_for("antigravity", "conv-ag")
    assert any(inj.id == f"node:{ag_id}" for inj in ag_injections)
    store.close()


def test_async_attention_drops_stale_deferred_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = _config(tmp_path)
    store = Store(cfg)
    node_id = store.add_node(
        "Deploy checklist",
        node_type="directive",
        extra={"attention_triggers": ["deploy"]},
    )
    prepared = _prepare_attention_job(store, cfg, "deploy this", "conv-1", force=True)
    assert enqueue_attention_review(store, cfg, prepared["job"]) is True
    drain_attention_queue(store, cfg, client=_MockClient(f"node:{node_id}"))
    pending = json.loads(store.get_meta(ATTENTION_PENDING_META) or "[]")

    injections = pop_pending_attention_injections(
        store,
        cfg,
        "conv-1",
        "unrelated frontend colors",
        tick=int(pending[0]["tick"]) + 4,
    )
    assert injections == []
    assert store.get_meta(ATTENTION_PENDING_META) == "[]"
    store.close()


def test_wait_for_pending_attention_returns_empty_at_deadline(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    store = Store(cfg)
    clock = [100.0]
    monkeypatch.setattr(
        attention,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    start = clock[0]
    injections = wait_for_pending_attention(
        store,
        cfg,
        "conv-1",
        "deploy this",
        tick=1,
        job_id="missing",
        deadline=start + 0.05,
    )
    assert injections == []
    assert clock[0] - start == 0.05
    store.close()


def test_runtime_toggle_overrides_config_default(tmp_path):
    cfg = Config(data_dir=str(tmp_path), attention=AttentionConfig(enabled=False))
    store = Store(cfg)

    assert runtime_status(store, cfg)["enabled"] is False
    set_runtime_enabled(store, True)
    assert runtime_status(store, cfg)["enabled"] is True
    set_runtime_enabled(store, False, conversation_id="conv-1")
    status = runtime_status(store, cfg, "conv-1")
    assert status["enabled"] is False
    assert status["conversation_override"] is False
    store.close()


def test_estimate_message_window_projects_by_tick_interval(tmp_path):
    cfg = _config(tmp_path, tick_interval=4)

    estimate = estimate_message_window(cfg, messages=10)

    assert estimate["messages"] == 10
    assert estimate["tick_interval"] == 4
    assert estimate["estimated_llm_checks"] == 3
    assert estimate["window_estimate"] > 0


def test_estimate_message_window_includes_observed_projection(tmp_path):
    cfg = _config(tmp_path, tick_interval=5)

    estimate = estimate_message_window(
        cfg,
        messages=10,
        observed_entries=[
            {"purpose": "attention", "amount": 0.002},
            {"purpose": "search", "amount": 0.050},
            {"purpose": "attention", "amount": 0.004},
        ],
    )

    assert estimate["observed"]["checks"] == 2
    assert estimate["observed"]["window_projection"] == 0.006


def test_attention_parser_accepts_estimate_action():
    parser = build_parser()

    args = parser.parse_args(["attention", "estimate", "--messages", "1000"])

    assert args.attention_action == "estimate"
    assert args.messages == 1000


def test_extract_conversation_text_falls_back_to_tool_payload():
    text = extract_conversation_text(
        hook_payload={
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m test"},
        },
    )

    assert "tool_name: Bash" in text
    assert "git commit" in text


def test_is_background_action_skips_noise_and_fires_on_actions():
    cfg = Config()

    def bg(tool_name, command=None, **tool_input):
        payload = {"tool_name": tool_name}
        if command is not None:
            payload["tool_input"] = {"command": command}
        elif tool_input:
            payload["tool_input"] = tool_input
        return is_background_action(payload, cfg)

    # Kindex's own tool calls and pure inspection are background noise.
    assert bg("mcp__kindex__add", text="x") is True
    assert bg("Read", file_path="/x") is True
    assert bg("Grep", pattern="x") is True
    assert bg("Bash", "grep -rn export src") is True
    assert bg("Bash", "ls -la | sort") is True
    assert bg("Bash", "git status") is True
    assert bg("Bash", "kin search foo") is True
    assert bg("Bash", "sudo cat /var/log/y") is True

    # Real actions fire — including API I/O and arbitrary commands.
    assert bg("Bash", "curl -X POST https://api.example.com -d @body") is False
    assert bg("Bash", "grep x f && curl https://y") is False
    assert bg("Bash", "git push origin main") is False
    assert bg("Bash", "kin index") is False
    assert bg("Bash", "echo hi > out.txt") is False
    assert bg("Bash", "pytest -q") is False
    assert bg("Edit", file_path="/x") is False
    assert bg("WebFetch", url="http://x") is False

    # Non-tool events (real user prompts) always run.
    assert is_background_action({"prompt": "push to github"}, cfg) is False


def test_antigravity_tool_call_payload_extracts_text_and_background_status():
    cfg = Config()
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "git push origin main", "Cwd": "/repo"},
        },
    }

    text = extract_conversation_text(hook_payload=payload)

    assert "tool_name: run_command" in text
    assert "git push origin main" in text
    assert is_background_action(payload, cfg) is False
    assert is_background_action({
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "rg antigravity src"},
        },
    }, cfg) is True
    assert is_background_action({
        "toolCall": {
            "name": "view_file",
            "args": {"Path": "src/kindex/cli.py"},
        },
    }, cfg) is True


def test_antigravity_pretool_config_write_forces_permission_prompt():
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "kin agent-config set attention.tick_interval 1 --client claude",
            },
        },
    }

    output = permission_gate_output(
        adapter="antigravity",
        event="PreToolUse",
        payload=payload,
    )

    data = json.loads(output)
    assert data["decision"] == "force_ask"
    assert "changes Kindex behavior" in data["reason"]


def test_antigravity_hook_context_uses_inject_steps_and_pretool_allow():
    pre_invocation = json.loads(render_hook_context(
        "hello",
        adapter="antigravity",
        event="PreInvocation",
    ))
    pre_tool = json.loads(render_hook_context(
        "check this",
        adapter="antigravity",
        event="PreToolUse",
    ))

    assert pre_invocation == {"injectSteps": [{"ephemeralMessage": "hello"}]}
    assert pre_tool == {"decision": "allow", "reason": "check this"}


def test_codex_quiet_output_does_not_emit_unsupported_suppress_output():
    payload = json.loads(render_hook_context(
        "hello",
        adapter="codex",
        event="SessionStart",
        suppress=True,
    ))

    assert payload["hookSpecificOutput"]["additionalContext"] == "hello"
    assert "suppressOutput" not in payload


def test_claude_quiet_output_keeps_suppress_output():
    payload = json.loads(render_hook_context(
        "hello",
        adapter="claude",
        event="SessionStart",
        suppress=True,
    ))

    assert payload["suppressOutput"] is True


def test_prompt_check_parser_accepts_adapter():
    parser = build_parser()

    args = parser.parse_args(["prompt-check", "--adapter", "antigravity"])

    assert args.adapter == "antigravity"


def test_attention_hook_parser_accepts_client_adapter():
    parser = build_parser()

    args = parser.parse_args([
        "attention-hook",
        "--adapter",
        "antigravity",
        "--event",
        "PreToolUse",
    ])

    assert args.adapter == "antigravity"
    assert args.event == "PreToolUse"
