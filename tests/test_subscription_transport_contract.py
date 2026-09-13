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
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from kindex.config import Config
from kindex import subscription_review as native


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="Local transport qualification requires tmux")


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


def test_r2_native_session_id_emitted_before_timeout_is_used_for_next_fresh_review(tmp_path, monkeypatch):
    cfg, receipts, expected_id, _ = install_fake(tmp_path, monkeypatch, first_timeout=True)
    try:
        first = native.run_review(cfg, "first-turn-timeout", "First review which deliberately times out")
        assert first.get("status") != "ok", first
        assert len(rows(receipts / "calls.jsonl")) == 1
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
