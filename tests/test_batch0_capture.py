"""Historical batch0 Stop-hook suite under state-resilience supersession.

Tester-lane authored, blind to the implementation; the oracle is the signed
batch0 artifacts for envelope/setup behavior only. Automatic promotion is now
superseded by state-resilience product P2.1 (product ece927181a13), architecture
A6 (c96a1839b7b7), and strategy T1/T5 (d41c761e2c15): extractable content is a
pending candidate and creates zero durable nodes/edges. The older citation
shorthand remains historical provenance for assertions not superseded here:

  spec@1f0cdd71  = product-specification.md
      sha256 1f0cdd7134ad671d3795252cbaedde858ae82dd0ef4d5a65d7d09a048bca617b
  arch@59540239  = architecture.md
      sha256 595402395f445c619122ede5435a303b31e8ed9df5d4c1f96e2db73a6ec4f1e6
  strat@e58068c2 = testing-strategy.md
      sha256 e58068c20913db342c6e24af5896f5f24d429ecc699103a8a787fda0372eece5

Markers per strat@e58068c2 T0.2 (registered in the new rootdir conftest.py):
``red_now`` asserts the FIXED behavior and is expected to FAIL on unfixed base
SHA 8c5cc925648c; unmarked tests are green-now guards that must pass
identically before and after the fix. Determinism per T0.4: no network, no
provider keys, tmp dirs only.

Subprocess and fixture conventions are replicated from the read-only
tests/test_compact_hook.py (fixture library; never edited).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store

_SESSION_ID = "b0b0c0de-1111-2222-3333-444455556666"

# Distinctive transcript material. The read-only suite proves the no-key
# keyword extractor deterministically derives a "cache invalidation" subject from
# this sentence (tests/test_compact_hook.py::
# test_envelope_extracts_from_transcript_text), so its appearance is a sound
# transcript-was-consumed probe.
TRANSCRIPT_SENTENCE = (
    "We learned that the cache invalidation bug was caused by a stale "
    "TTL configuration in the deploy pipeline."
)
# Long, extractable --text material (same provenance:
# tests/test_compact_hook.py::test_plain_text_still_extracts derives a
# "retry logic" review subject from it).
TEXT_SENTENCE = (
    "During this refactor we learned that the retry logic never honored "
    "the backoff ceiling configured for the API client."
)


def _env(tmp_path):
    """Hermetic subprocess env (strat@e58068c2 T0.4): HOME inside tmp, no
    provider keys, no ambient KIN_* overrides."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("KIN_"):
            env.pop(k)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "VOYAGE_API_KEY"):
        env.pop(k, None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


def _run_compact_hook(tmp_path, stdin_text, *extra_args):
    cmd = [sys.executable, "-m", "kindex.cli", "compact-hook", *extra_args,
           "--data-dir", str(tmp_path / "data")]
    return subprocess.run(cmd, input=stdin_text, capture_output=True,
                          text=True, timeout=120, env=_env(tmp_path),
                          cwd=str(tmp_path))


def _write_transcript(tmp_path, texts, name="transcript.jsonl"):
    """Modern Claude Code transcript shape (nested ``message`` objects), per
    strat@e58068c2 'Declared test-visible surfaces' and the read-only fixture
    builder in tests/test_compact_hook.py."""
    transcript = tmp_path / name
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]},
        })
        for text in texts
    ]
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


def _envelope(tmp_path, transcript_path):
    """Hook envelope carrying exactly the keys declared in strat@e58068c2
    'Declared test-visible surfaces'."""
    return json.dumps({
        "hook_event_name": "Stop",
        "transcript_path": str(transcript_path),
        "session_id": _SESSION_ID,
        "cwd": str(tmp_path),
    })


def _nodes(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        return {n["title"]: n for n in store.all_nodes(limit=500)}
    finally:
        store.close()


def _candidates(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        return [
            store.get_capture_candidate(row["id"])
            for row in store.list_capture_candidates(limit=500)
        ]
    finally:
        store.close()


# -- R1.1: the envelope preempts --text -----------------------------------


@pytest.mark.red_now
def test_r1_1_envelope_preempts_text_flag(tmp_path):
    """Historical R1.1 plus state-resilience P2.1/T5.2 (red_now).

    With a parseable envelope on stdin AND ``--text "Session ended"`` on
    argv, extraction MUST consume the transcript, not the 13-char literal.
    Observable: zero durable nodes and a transcript-derived pending candidate.

    Red if: the Stop hook path again lets --text preempt the stdin envelope
    (no cache-invalidation candidate), or direct node promotion returns.
    """
    transcript = _write_transcript(tmp_path, [TRANSCRIPT_SENTENCE])
    r = _run_compact_hook(tmp_path, _envelope(tmp_path, transcript),
                          "--text", "Session ended")
    assert r.returncode == 0, r.stderr
    assert _nodes(tmp_path) == {}
    candidates = _candidates(tmp_path)
    assert any("cache invalidation" in row["title"] for row in candidates), (
        "transcript was not consumed; envelope did not preempt --text")
    assert not any("session ended" in row["title"].lower()
                   for row in candidates)
    assert all(row["status"] == "pending" for row in candidates)


@pytest.mark.parametrize("stdin_text", [
    "",                           # empty stdin
    "   \n\t  ",                  # whitespace-only stdin
    "{this is not json at all",   # non-JSON garbage
    "null",                       # parseable JSON, not an object
    "[1, 2, 3]",                  # parseable JSON, not an object
])
@pytest.mark.red_now
def test_r1_1_non_envelope_stdin_falls_back_to_text(tmp_path, stdin_text):
    """Historical R1.1 plus state-resilience P2.1/T5.2 (red_now).

    When stdin is not a parseable envelope, --text remains the effective
    input: exit 0, zero durable nodes, and a retry-logic pending candidate.

    Red if: the fix over-rotates and ignores --text entirely (requiring an
    envelope), the parser crashes on garbage, or staging reverts to promotion.
    """
    r = _run_compact_hook(tmp_path, stdin_text, "--text", TEXT_SENTENCE)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert _nodes(tmp_path) == {}
    assert any("retry logic" in row["title"] for row in _candidates(tmp_path)), (
        "non-envelope stdin must fall back to the --text path")


# -- R1.3: missing transcript means no extraction, not fallback -----------


@pytest.mark.red_now
def test_r1_3_envelope_with_missing_transcript_mints_nothing(tmp_path):
    """Historical R1.3 plus state-resilience P2.1/T5.2 (red_now).

    Envelope whose transcript_path does not exist: no candidates/nodes, exit
    0. --text here is long and extractable on purpose: once the
    envelope is recognized, the missing transcript must NOT fall back to
    --text extraction.

    Red if: --text again preempts the envelope (a retry-logic subject appears),
    direct promotion occurs, or a missing transcript crashes the hook
    (nonzero exit).
    """
    missing = tmp_path / "gone.jsonl"  # never created
    r = _run_compact_hook(tmp_path, _envelope(tmp_path, missing),
                          "--text", TEXT_SENTENCE)
    assert r.returncode == 0, r.stderr
    assert _nodes(tmp_path) == {}, (
        "missing transcript must mean no extraction at all")
    assert _candidates(tmp_path) == []


# -- R1.4/P2.1: no content-empty candidates or durable nodes --------------


@pytest.mark.red_now
def test_r1_4_no_empty_content_nodes_on_envelope_path(tmp_path):
    """Historical R1.4 plus state-resilience P2.1/T5.1 (red_now).

    The envelope+transcript path creates zero nodes and only complete pending
    candidates with non-empty content.

    Red if: direct promotion returns or a title-only/partial candidate is staged.
    """
    transcript = _write_transcript(tmp_path, [TRANSCRIPT_SENTENCE])
    r = _run_compact_hook(tmp_path, _envelope(tmp_path, transcript))
    assert r.returncode == 0, r.stderr
    assert _nodes(tmp_path) == {}
    candidates = _candidates(tmp_path)
    assert candidates, "sanity: the envelope path should stage the transcript"
    for candidate in candidates:
        assert candidate["status"] == "pending"
        assert candidate.get("content", "").strip(), (
            f"content-empty candidate staged: {candidate.get('title')!r}")


# -- R1.5: transcript tolerance under concurrent writes -------------------


@pytest.mark.red_now
def test_r1_5_truncated_and_garbage_lines_tolerated(tmp_path):
    """Historical R1.5 plus state-resilience P2.1/T5.5 (red_now).

    A transcript with one non-JSON garbage line and a final line truncated
    mid-JSON (the mid-write shape when the Stop hook fires) must yield
    extraction from the parseable remainder: exit 0, no crash, zero nodes, and
    the good line's material staged as a pending candidate.

    Red if: malformed input aborts extraction, no candidate survives, or direct
    node promotion returns.
    """
    good_line = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text",
                                 "text": TRANSCRIPT_SENTENCE}]},
    })
    full_second = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "Second insight."}]},
    })
    truncated = full_second[: len(full_second) * 6 // 10]  # cut mid-JSON
    transcript = tmp_path / "mid-write.jsonl"
    transcript.write_text(
        good_line + "\n"
        + "this is {{{ not json at all\n"
        + truncated)  # no trailing newline: file ends mid-record
    r = _run_compact_hook(tmp_path, _envelope(tmp_path, transcript))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert _nodes(tmp_path) == {}
    assert any("cache invalidation" in row["title"]
               for row in _candidates(tmp_path)), (
        "extraction must succeed on the parseable remainder")


# -- R1.2: kin setup writes/migrates the Stop hook entry ------------------

# Verbatim capture of the Stop hook entry that kindex 0.29.0
# install_claude_hooks writes (generated from the released 0.29.0 wheel in
# the sanctioned authoring-baseline venv, strat@e58068c2 T0.5). This is the
# PRECONDITION state for the re-run-is-the-migration test -- historical
# machine state, not an oracle; the expected POST state comes from
# spec@1f0cdd71 R1.2. One entry, three sibling commands: compact-hook with
# the defective --text, `attention reinforce --enqueue`, and `dream`; each
# wrapped in the stop_hook_active guard prelude (the "stop-guard" sibling).
_OLD_STOP_ENTRY_JSON = r'''[{"hooks": [{"command": "/bin/bash -lc 'payload=$(cat); if printf '\"'\"'%s'\"'\"' \"$payload\" | python3 -c '\"'\"'import json,sys; raw=sys.stdin.read(); \ntry:\n data=json.loads(raw or '\"'\"'\"'\"'\"'\"'\"'\"'{}'\"'\"'\"'\"'\"'\"'\"'\"') if raw.strip() else {}\nexcept Exception:\n data={}\nsys.exit(0 if data.get('\"'\"'\"'\"'\"'\"'\"'\"'stop_hook_active'\"'\"'\"'\"'\"'\"'\"'\"') else 1)'\"'\"'; then exit 0; fi; source ~/.profile >/dev/null 2>&1 || true; printf '\"'\"'%s'\"'\"' \"$payload\" | /opt/homebrew/bin/kin compact-hook --text '\"'\"'Session ended'\"'\"''", "timeout": 5000, "type": "command"}, {"command": "/bin/bash -lc 'payload=$(cat); if printf '\"'\"'%s'\"'\"' \"$payload\" | python3 -c '\"'\"'import json,sys; raw=sys.stdin.read(); \ntry:\n data=json.loads(raw or '\"'\"'\"'\"'\"'\"'\"'\"'{}'\"'\"'\"'\"'\"'\"'\"'\"') if raw.strip() else {}\nexcept Exception:\n data={}\nsys.exit(0 if data.get('\"'\"'\"'\"'\"'\"'\"'\"'stop_hook_active'\"'\"'\"'\"'\"'\"'\"'\"') else 1)'\"'\"'; then exit 0; fi; source ~/.profile >/dev/null 2>&1 || true; printf '\"'\"'%s'\"'\"' \"$payload\" | /opt/homebrew/bin/kin attention reinforce --enqueue'", "timeout": 3000, "type": "command"}, {"command": "/bin/bash -lc 'payload=$(cat); if printf '\"'\"'%s'\"'\"' \"$payload\" | python3 -c '\"'\"'import json,sys; raw=sys.stdin.read(); \ntry:\n data=json.loads(raw or '\"'\"'\"'\"'\"'\"'\"'\"'{}'\"'\"'\"'\"'\"'\"'\"'\"') if raw.strip() else {}\nexcept Exception:\n data={}\nsys.exit(0 if data.get('\"'\"'\"'\"'\"'\"'\"'\"'stop_hook_active'\"'\"'\"'\"'\"'\"'\"'\"') else 1)'\"'\"'; then exit 0; fi; source ~/.profile >/dev/null 2>&1 || true; printf '\"'\"'%s'\"'\"' \"$payload\" | /opt/homebrew/bin/kin dream --detach --lightweight'", "timeout": 3000, "type": "command"}], "matcher": ""}]'''


def _claude_cfg(tmp_path, seed_settings=None):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps(
        seed_settings if seed_settings is not None else {}))
    cfg = Config(data_dir=str(tmp_path / "data"), claude_dir=str(claude_dir))
    return cfg, settings


@pytest.mark.red_now
def test_r1_2_fresh_install_stop_entry_has_no_text_and_is_idempotent(tmp_path):
    """spec@1f0cdd71 R1.2; strat@e58068c2 oracle row R1.2 (red_now).

    A fresh hook install writes the Stop entry with compact-hook and WITHOUT
    --text, keeps the sibling commands (dream, reinforce enqueue), and a
    second run is a no-op.

    Red if: setup again emits `compact-hook --text 'Session ended'` (the S1
    defect at its source), or the rewrite drops a sibling, or re-running
    setup duplicates/alters entries.
    """
    from kindex.setup import install_claude_hooks

    cfg, settings = _claude_cfg(tmp_path)
    install_claude_hooks(cfg)
    data1 = json.loads(settings.read_text())
    stop1 = json.dumps(data1["hooks"]["Stop"])
    assert "compact-hook" in stop1
    assert "--text" not in stop1
    assert "Session ended" not in stop1
    assert "dream" in stop1, "sibling command dropped: dream"
    assert "reinforce" in stop1, "sibling command dropped: reinforce enqueue"

    install_claude_hooks(cfg)
    data2 = json.loads(settings.read_text())
    assert data2 == data1, "re-running setup must be idempotent"


@pytest.mark.red_now
def test_r1_2_rerun_migrates_old_broken_entry_preserving_siblings(tmp_path, monkeypatch):
    """spec@1f0cdd71 R1.2 (re-run-is-the-migration, issue-#15 pattern);
    strat@e58068c2 oracle row R1.2 (red_now).

    On a machine whose settings.json still holds the 0.29.0 Stop entry
    (compact-hook --text 'Session ended' + siblings in the SAME entry),
    re-running setup MUST replace the whole entry: --text gone, and the
    sibling commands (stop-guard prelude, dream, reinforce enqueue)
    preserved in the rebuilt entry.

    Red if: the existing-entry matcher reports 'already installed' and
    leaves --text in place, or the whole-entry rewrite drops any sibling
    command.
    """
    from kindex.setup import install_claude_hooks

    # The released fixture names this binary. Exact handler ownership must
    # be tested against that installation, not the test runner's venv path.
    monkeypatch.setattr("kindex.setup._find_kin_path", lambda: "/opt/homebrew/bin/kin")
    old_stop = json.loads(_OLD_STOP_ENTRY_JSON)
    cfg, settings = _claude_cfg(
        tmp_path, seed_settings={"hooks": {"Stop": old_stop}})
    install_claude_hooks(cfg)

    data = json.loads(settings.read_text())
    stop = json.dumps(data["hooks"]["Stop"])
    compact_cmds = [h.get("command", "")
                    for entry in data["hooks"]["Stop"]
                    for h in entry.get("hooks", [])
                    if "compact-hook" in h.get("command", "")]
    assert compact_cmds, "migration dropped the compact-hook sibling"
    assert all("--text" not in c for c in compact_cmds), (
        "old --text entry survived the re-run migration")
    assert "--text" not in stop
    assert "Session ended" not in stop
    assert "dream" in stop, "sibling command dropped: dream"
    assert "reinforce" in stop, "sibling command dropped: reinforce enqueue"
    assert "stop_hook_active" in stop, (
        "stop-guard prelude dropped from the rebuilt entry")
