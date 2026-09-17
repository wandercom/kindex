"""Independent local transport acceptance, A3/A4: real tmux, synthetic Codex.

These tests perform no inference and skip if tmux is absent. Fake provider script
records argument lists, environment NAMES, and the synthetic prompt separately.
Validator owns execution; no implementation source was read to author tests.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from unittest.mock import Mock

import pytest

from kindex.config import Config
from kindex import subscription_review as native


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="Local transport qualification requires tmux")


@pytest.fixture(autouse=True)
def isolated_health_registry(tmp_path, monkeypatch):
    root = tmp_path / "health"
    monkeypatch.setenv("KIN_HEALTH_DIR", str(root))
    return root


FAKE_CODEX = r'''
import json, os, pathlib, subprocess, sys, time
ARGS = sys.argv[1:]
if ARGS[:2] == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)
root = pathlib.Path(__ROOT__)
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": ARGS, "env_names": sorted(os.environ)}) + "\n")
with (root / "prompts.jsonl").open("a") as log:
    log.write(json.dumps(sys.stdin.read()) + "\n")
if __FIRST_TIMEOUT__ and len((root / "calls.jsonl").read_text().splitlines()) == 1:
    (root / "provider.pid").write_text(str(os.getpid()))
    print(json.dumps({"type": "thread.started", "thread_id": __SESSION__}), flush=True)
    time.sleep(120)
if __BLOCK__:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", __MARKER__])
    (root / "child.pid").write_text(str(child.pid))
    (root / "provider.pid").write_text(str(os.getpid()))
    time.sleep(120)
else:
    sid = __SESSION__
    print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"id": "reply-1", "type": "agent_message",
        "text": json.dumps({"rating": 0.91, "note": "TRANSPORT_CONTRACT_NOTE"})}}), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 13, "cached_input_tokens": 0, "output_tokens": 7}}), flush=True)
'''


def install_fake(tmp_path, monkeypatch, *, block=False, first_timeout=False):
    binary_dir = tmp_path / "fake-bin"
    binary_dir.mkdir()
    receipts = tmp_path / "fake-receipts"
    receipts.mkdir()
    session_id = str(uuid4())
    marker = "kindex-transport-child-" + str(uuid4())
    source = FAKE_CODEX.replace("__ROOT__", repr(str(receipts)))
    source = source.replace("__BLOCK__", repr(block)).replace("__SESSION__", repr(session_id))
    source = source.replace("__FIRST_TIMEOUT__", repr(first_timeout))
    source = source.replace("__MARKER__", repr(marker))
    executable = binary_dir / "codex"
    executable.write_text("#!" + sys.executable + "\n" + source)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": True, "backend": "codex", "agent_timeout": 1 if block or first_timeout else 10,
        "max_conversation_reviews": 5, "max_daily_reviews": 10,
    })
    return cfg, receipts, session_id, marker


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_a4_real_tmux_preserves_literal_prompt_filters_environment_and_resumes_exact_id(tmp_path, monkeypatch):
    cfg, receipts, expected_id, _ = install_fake(tmp_path, monkeypatch)
    canary = tmp_path / "shell-injection-canary"
    hostile = "Review literally: $(touch " + shlex.quote(str(canary)) + "); touch " + shlex.quote(str(canary)) + "; # unchanged"
    sentinels = {"KINDEX_TEST_AMBIENT_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BASH_ENV"}
    for name in sentinels:
        monkeypatch.setenv(name, "synthetic-credential-must-not-reach-provider")
    first = native.run_review(cfg, "literal-and-resume", hostile)
    assert first.get("status") == "ok", first
    assert first.get("session_id") == expected_id
    assert not canary.exists(), "A4: prompt shell syntax must remain inert text"
    calls = rows(receipts / "calls.jsonl")
    prompts = rows(receipts / "prompts.jsonl")
    assert len(calls) == len(prompts) == 1
    assert prompts[0] == hostile or hostile in calls[0]["argv"], "A4: native provider receives the exact literal prompt"
    assert not sentinels.intersection(calls[0]["env_names"])
    assert "KINDEX_REVIEW_WORKER" in calls[0]["env_names"]
    assert "KIN_HEALTH_DIR" in calls[0]["env_names"], "The worker must inherit the explicit isolated health registry"

    second = native.run_review(Config(**cfg.model_dump()), "literal-and-resume", "Second synthetic review")
    assert second.get("status") == "ok" and second.get("session_id") == expected_id
    calls = rows(receipts / "calls.jsonl")
    assert len(calls) == 2
    resumed = calls[1]["argv"]
    assert "resume" in resumed and expected_id in resumed[resumed.index("resume") + 1:]
    assert not {"--last", "--continue"}.intersection(resumed)
    assert not sentinels.intersection(calls[1]["env_names"])
    assert not canary.exists()


def process_running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True)
    state = result.stdout.strip()
    return bool(state and not state.startswith("Z"))


def test_a3_a4_timeout_retains_reservation_and_terminates_provider_children(tmp_path, monkeypatch):
    cfg, receipts, _, marker = install_fake(tmp_path, monkeypatch, block=True)
    try:
        start = time.monotonic()
        result = native.run_review(cfg, "timeout-conversation", "Synthetic timeout review")
        elapsed = time.monotonic() - start
        assert result.get("status") != "ok", result
        assert elapsed < 12, "A4: one-second review timeout must bound dispatch and cleanup"
        assert (receipts / "child.pid").exists(), "Fixture must actually launch a child before timing out"
        child = int((receipts / "child.pid").read_text())
        provider = int((receipts / "provider.pid").read_text())
        deadline = time.monotonic() + 3
        while (process_running(child) or process_running(provider)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_running(child), "A4: timeout must terminate a provider's child, not just the waiting caller"
        assert not process_running(provider)
        allowance = native.allowance_status(Config(**cfg.model_dump()), "timeout-conversation")
        assert allowance["conversation"]["used"] == allowance["day"]["used"] == 1
        assert len(rows(receipts / "calls.jsonl")) == 1, "A3: unknown completion cannot automatically replay"
    finally:
        # Failing implementations must not leave this test's synthetic sleepers.
        for filename in ("child.pid", "provider.pid"):
            path = receipts / filename
            if not path.exists():
                continue
            pid = int(path.read_text())
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True).stdout
            if marker in command or str(tmp_path / "fake-bin" / "codex") in command:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_r2_native_session_id_emitted_before_timeout_is_used_for_next_fresh_review(tmp_path, monkeypatch, isolated_health_registry):
    cfg, receipts, expected_id, _ = install_fake(tmp_path, monkeypatch, first_timeout=True)
    try:
        first = native.run_review(cfg, "first-turn-timeout", "First review which deliberately times out")
        assert first.get("status") != "ok", first
        assert len(rows(receipts / "calls.jsonl")) == 1
        # The configured registry must retain the known native identity after
        # interruption, before the next admission produces a successful result.
        registry = isolated_health_registry / "health.sqlite3"
        assert registry.is_file(), "Interrupted reviews must retain identity in the configured health registry"
        with sqlite3.connect(registry.as_uri() + "?mode=ro", uri=True) as conn:
            assert any(expected_id in statement for statement in conn.iterdump()), \
                "The configured registry must contain the known native identity after interruption"
        # Construct a fresh Config so receipt persistence, not caller-local state,
        # owns the native identity after unsuccessful first-turn completion.
        second = native.run_review(Config(**cfg.model_dump()), "first-turn-timeout", "New review after timeout")
        assert second.get("status") == "ok", second
        assert second.get("session_id") == expected_id
        calls = rows(receipts / "calls.jsonl")
        assert len(calls) == 2, "R2: the old review is not automatically replayed"
        resumed = calls[1]["argv"]
        assert "resume" in resumed and expected_id in resumed[resumed.index("resume") + 1:], \
            "R2: an emitted native ID survives failure of the first completed receipt"
        assert not {"--last", "--continue"}.intersection(resumed)
        allowance = native.allowance_status(cfg, "first-turn-timeout")
        assert allowance["conversation"]["used"] == allowance["day"]["used"] == 2
    finally:
        pid_file = receipts / "provider.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True).stdout
            if str(tmp_path / "fake-bin" / "codex") in command:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("error", [sqlite3.OperationalError("synthetic health database failure"),
                                  OSError("synthetic health storage failure")])
def test_health_registration_failure_preserves_valid_review_and_native_checkpoint(tmp_path, monkeypatch, error):
    """PR30: optional health storage cannot invalidate completed native work."""
    from kindex import supervisor_health
    cfg, receipts, expected_id, _ = install_fake(tmp_path, monkeypatch)
    registration = Mock(side_effect=error)
    monkeypatch.setattr(supervisor_health, "register_reviewer_session", registration)
    first = native.run_review(cfg, "health-unavailable", "Review while health storage is unavailable")
    assert registration.call_count >= 1, "Fixture must actually encounter unavailable health registration"
    assert first.get("status") == "ok", first
    assert first.get("session_id") == expected_id
    second = native.run_review(Config(**cfg.model_dump()), "health-unavailable", "Fresh admission after health failure")
    assert second.get("status") == "ok", second
    assert second.get("session_id") == expected_id
    calls = rows(receipts / "calls.jsonl")
    assert len(calls) == 2
    resumed = calls[1]["argv"]
    assert "resume" in resumed and expected_id in resumed[resumed.index("resume") + 1:]
    allowance = native.allowance_status(cfg, "health-unavailable")
    assert allowance["conversation"]["used"] == allowance["day"]["used"] == 2


@pytest.mark.parametrize("field,bad", [("timeout", True), ("timeout", -1),
                                      ("session_id", True), ("prompt", []),
                                      ("workspace", []), ("model", {}), ("effort", [])])
def test_malformed_worker_job_is_rejected_before_native_process_dispatch(tmp_path, monkeypatch, field, bad):
    """PR30: mutate the persisted v1 job at the tmux launch I/O boundary."""
    actual_tmux = shutil.which("tmux")
    cfg, receipts, _, _ = install_fake(tmp_path, monkeypatch)
    mutation_receipt = receipts / "mutated-jobs.json"
    shim = r'''
import json, pathlib, subprocess, sys
if "new-session" in sys.argv[1:]:
    changed = []
    for path in pathlib.Path(__ROOT__).rglob("job.json"):
        if not path.parent.name.startswith("attempt-"):
            continue
        payload = json.loads(path.read_text())
        payload[__FIELD__] = __BAD__
        path.write_text(json.dumps(payload))
        changed.append(str(path))
    pathlib.Path(__RECEIPT__).write_text(json.dumps(changed))
sys.exit(subprocess.run([__TMUX__, *sys.argv[1:]]).returncode)
'''
    substitutions = {"__ROOT__": str(cfg.data_path / "subscription-review"),
                     "__FIELD__": field, "__BAD__": bad,
                     "__RECEIPT__": str(mutation_receipt), "__TMUX__": actual_tmux}
    for key, value in substitutions.items():
        shim = shim.replace(key, repr(value))
    executable = tmp_path / "fake-bin" / "tmux"
    executable.write_text("#!" + sys.executable + "\n" + shim)
    executable.chmod(0o755)
    result = native.run_review(cfg, "malformed-worker-job", "Synthetic worker job validation")
    assert mutation_receipt.exists() and json.loads(mutation_receipt.read_text()), \
        "Fixture must mutate the actual saved worker job before tmux starts it"
    assert result.get("status") != "ok", "Malformed worker payload must visibly fail closed"
    assert not (receipts / "calls.jsonl").exists(), "Malformed worker jobs cannot dispatch a native client"


def test_claude_streamed_init_identity_survives_first_turn_timeout(tmp_path, monkeypatch):
    """PR30: system/init is a durable native checkpoint before final result."""
    binary_dir = tmp_path / "fake-claude-bin"
    receipts = tmp_path / "fake-claude-receipts"
    binary_dir.mkdir()
    receipts.mkdir()
    expected_id = str(uuid4())
    source = r'''
import json, os, pathlib, sys, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
root = pathlib.Path(__ROOT__)
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env_names": sorted(os.environ)}) + "\n")
with (root / "prompts.jsonl").open("a") as log:
    log.write(json.dumps(sys.stdin.read()) + "\n")
sid = __SESSION__
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
if len((root / "calls.jsonl").read_text().splitlines()) == 1:
    (root / "provider.pid").write_text(str(os.getpid()))
    time.sleep(120)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
    "session_id": sid, "result": json.dumps({"rating": 0.91, "note": "CLAUDE_STREAM_CONTRACT_NOTE"}),
    "usage": {"input_tokens": 13, "output_tokens": 7}}), flush=True)
'''
    source = source.replace("__ROOT__", repr(str(receipts))).replace("__SESSION__", repr(expected_id))
    executable = binary_dir / "claude"
    executable.write_text("#!" + sys.executable + "\n" + source)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    # The first turn must time out after init (the fixture then sleeps two
    # minutes); a loaded runner can take a second just to start Python, so
    # neither turn is held to one second.
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": True, "backend": "claude", "agent_timeout": 5,
        "max_conversation_reviews": 5, "max_daily_reviews": 10,
    })
    try:
        first = native.run_review(cfg, "claude-interrupted-init", "First synthetic Claude review")
        assert first.get("status") != "ok", first
        assert (receipts / "provider.pid").exists(), "Fixture must emit init before timing out"
        resumed_config = cfg.model_dump()
        resumed_config["sim"]["agent_timeout"] = 60
        second = native.run_review(Config(**resumed_config), "claude-interrupted-init", "Fresh review after init timeout")
        assert second.get("status") == "ok", second
        assert second.get("session_id") == expected_id
        calls = rows(receipts / "calls.jsonl")
        assert len(calls) == 2, "The interrupted review must never automatically replay"
        resumed = calls[1]["argv"]
        assert "--resume" in resumed and resumed[resumed.index("--resume") + 1] == expected_id
        assert not {"--continue", "--last"}.intersection(resumed)
        for call in calls:
            args = call["argv"]
            assert "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
        assert native.allowance_status(cfg, "claude-interrupted-init")["conversation"]["used"] == 2
    finally:
        pid_file = receipts / "provider.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True).stdout
            if str(executable) in command:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_claude_missing_native_conversation_preserves_checkpoint_without_new_identity_fallback(tmp_path, monkeypatch):
    """Native init can precede provider persistence; failed resumes stay exact."""
    binary_dir = tmp_path / "fake-claude-bin"
    receipts = tmp_path / "fake-claude-receipts"
    binary_dir.mkdir()
    receipts.mkdir()
    expected_id = str(uuid4())
    source = r'''
import json, os, pathlib, sys, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
root = pathlib.Path(__ROOT__)
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env_names": sorted(os.environ)}) + "\n")
sys.stdin.read()
if len((root / "calls.jsonl").read_text().splitlines()) == 1:
    (root / "provider.pid").write_text(str(os.getpid()))
    print(json.dumps({"type": "system", "subtype": "init", "session_id": __SESSION__}), flush=True)
    time.sleep(120)
print("No conversation found with session ID: " + __SESSION__, file=sys.stderr, flush=True)
sys.exit(1)
'''
    source = source.replace("__ROOT__", repr(str(receipts))).replace("__SESSION__", repr(expected_id))
    executable = binary_dir / "claude"
    executable.write_text("#!" + sys.executable + "\n" + source)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": True, "backend": "claude", "agent_timeout": 1,
        "max_conversation_reviews": 5, "max_daily_reviews": 10,
    })
    try:
        first = native.run_review(cfg, "claude-init-before-flush", "Initial review interrupted before native persistence")
        assert first.get("status") != "ok", first
        assert (receipts / "provider.pid").exists(), "Fixture must emit init and enter the timeout path"
        assert len(rows(receipts / "calls.jsonl")) == 1
        for admission in (2, 3):
            result = native.run_review(Config(**cfg.model_dump()), "claude-init-before-flush",
                                       f"Fresh admission {admission} with explicit known identity")
            assert result.get("status") != "ok", result
            calls = rows(receipts / "calls.jsonl")
            assert len(calls) == admission, "Each admission permits one attempt, with no retry or new-session fallback"
            resumed = calls[-1]["argv"]
            assert "--resume" in resumed and resumed[resumed.index("--resume") + 1] == expected_id
            assert not {"--continue", "--last"}.intersection(resumed)
        allowance = native.allowance_status(cfg, "claude-init-before-flush")
        assert allowance["conversation"]["used"] == allowance["day"]["used"] == 3
    finally:
        pid_file = receipts / "provider.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True).stdout
            if str(executable) in command:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
