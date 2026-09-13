"""Independent PR30 persisted-wire regressions; Validator executes.

Wire shapes were supplied by the Validator as a v1 compatibility contract.
Provider I/O is replaced at the agreed _run_provider boundary; no implementation
source was read to construct malformed states or expected admission outcomes.
"""
from datetime import date
import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from kindex.config import Config
from kindex import subscription_review as native


CONVERSATION = "persisted-wire-conversation"


@pytest.fixture
def wire(tmp_path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": True, "backend": "codex", "max_conversation_reviews": 5,
        "max_daily_reviews": 10,
    })
    digest = hashlib.sha256(CONVERSATION.encode()).hexdigest()
    root = cfg.data_path / "subscription-review"
    workspace = root / (digest + "-codex")
    workspace.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    expected_id = str(uuid4())
    ledger = {"version": 1, "conversations": {digest: 1}, "days": {date.today().isoformat(): 1}}
    receipt = {"backend": "codex", "conversation": digest, "session_id": expected_id,
               "status": "ok", "usage": {}}
    checkpoint = {"version": 1, "backend": "codex", "conversation": digest,
                  "workspace": str(workspace.resolve()), "session_id": expected_id}
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw:
                        "/fake-native/" + name if name in {"tmux", "codex", "claude", "agy"}
                        else real_which(name, *a, **kw))
    provider = Mock(side_effect=lambda cfg, sid, prompt, work: {
        "status": "ok", "session_id": sid or expected_id,
        "response": json.dumps({"rating": 0.8, "note": "PERSISTENCE_CONTRACT_NOTE"}), "usage": {}})
    monkeypatch.setattr(native, "_run_provider", provider)
    return {"cfg": cfg, "digest": digest, "root": root, "workspace": workspace,
            "ledger": ledger, "receipt": receipt, "checkpoint": checkpoint,
            "id": expected_id, "provider": provider}


def write(path, payload):
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def rejected_before_dispatch(wire):
    result = native.run_review(wire["cfg"], CONVERSATION, "Review with persisted local state")
    assert result.get("status") != "ok", "Malformed persisted state must visibly fail closed"
    assert wire["provider"].call_count == 0, "Malformed state must be rejected before native dispatch"


@pytest.mark.parametrize("location,bad", [
    ("root", []), ("root", None), ("version", True),
    ("conversations", []), ("days", []),
    ("conversation_count", True), ("conversation_count", -1), ("conversation_count", 1.5),
    ("day_count", True), ("day_count", -1), ("day_count", "1"),
])
def test_allowance_wire_rejects_malformed_shapes_and_noninteger_counts_before_dispatch(wire, location, bad):
    ledger = wire["ledger"]
    if location == "root":
        ledger = bad
    elif location == "conversation_count":
        ledger["conversations"][wire["digest"]] = bad
    elif location == "day_count":
        ledger["days"][date.today().isoformat()] = bad
    else:
        ledger[location] = bad
    write(wire["root"] / "allowances.json", ledger)
    rejected_before_dispatch(wire)


@pytest.mark.parametrize("field,bad", [
    ("root", []), ("root", "invalid-receipt"), ("session_id", True),
    ("session_id", "not-a-native-uuid"), ("status", []), ("usage", []),
    ("backend", "claude"), ("conversation", "wrong-conversation"),
])
def test_session_receipt_wire_rejects_malformed_or_mismatched_state_before_dispatch(wire, field, bad):
    write(wire["root"] / "allowances.json", wire["ledger"])
    receipt = wire["receipt"]
    if field == "root":
        receipt = bad
    else:
        receipt[field] = bad
    write(wire["workspace"] / "session.json", receipt)
    rejected_before_dispatch(wire)


@pytest.mark.parametrize("field,bad", [
    ("root", []), ("version", True), ("session_id", False),
    ("session_id", "not-a-native-uuid"), ("workspace", []),
    ("workspace", "relative-workspace"), ("backend", "claude"),
    ("conversation", "wrong-conversation"),
])
def test_native_checkpoint_wire_rejects_malformed_or_mismatched_state_before_dispatch(wire, field, bad):
    write(wire["root"] / "allowances.json", wire["ledger"])
    receipt = {key: value for key, value in wire["receipt"].items() if key != "usage"}
    receipt.update(session_id=None, status="completion_unknown")
    write(wire["workspace"] / "session.json", receipt)
    checkpoint = wire["checkpoint"]
    if field == "root":
        checkpoint = bad
    else:
        checkpoint[field] = bad
    write(wire["workspace"] / "observed-native.json", checkpoint)
    rejected_before_dispatch(wire)


@pytest.mark.parametrize("completion_unknown", [False, True])
def test_supported_receipt_shapes_remain_admissible_and_resume_exact_native_identity(wire, completion_unknown):
    write(wire["root"] / "allowances.json", wire["ledger"])
    receipt = wire["receipt"]
    if completion_unknown:
        receipt = {key: value for key, value in receipt.items() if key != "usage"}
        receipt.update(session_id=None, status="completion_unknown")
        write(wire["workspace"] / "observed-native.json", wire["checkpoint"])
    write(wire["workspace"] / "session.json", receipt)
    result = native.run_review(wire["cfg"], CONVERSATION, "New admitted review")
    assert result.get("status") == "ok", result
    assert result.get("session_id") == wire["id"]
    assert wire["provider"].call_count == 1
    assert wire["provider"].call_args.args[1] == wire["id"]


@pytest.mark.parametrize("session_id", [None, "1b25a3e2-60f4-42fd-8e31-fbb1a33b4b12"])
def test_antigravity_prompt_is_private_stdin_stream_message_and_never_process_argument(tmp_path, session_id):
    """PR30: documented stream-json user input is exact and absent from argv."""
    prompt = "PRIVATE_PROMPT_CANARY: 'quoted' \"double\" $(touch NEVER_EXECUTE)\nsecond line; & | \\ end"
    workspace = tmp_path / "review-workspace"
    workspace.mkdir()
    argv, stdin = native._arguments("antigravity", "agy", session_id, "", "medium",
                                    prompt, workspace, 30)
    assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
    assert all("PRIVATE_PROMPT_CANARY" not in arg for arg in argv), "Private review content cannot appear in process listings"
    assert "-p" not in argv and "--prompt" not in argv
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert isinstance(stdin, str) and stdin.endswith("\n")
    messages = [json.loads(line) for line in stdin.splitlines() if line]
    assert messages == [{"event": "user", "message": {"role": "user", "content": prompt}}]
