"""Acceptance oracles for CLI/MCP state-resilience surface parity."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kindex.config import Config
from kindex.store import Store


NOW = "2026-08-18T12:00:00Z"
SOURCE = hashlib.sha256(b"surface transcript").hexdigest()


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=str(tmp_path)))
    yield value
    value.close()


def _env(tmp_path):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("KIN_") or key.endswith("_API_KEY"):
            env.pop(key, None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


def _cli(tmp_path, *args, data_dir):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args,
         "--data-dir", str(data_dir)],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        env=_env(tmp_path),
    )


def _clocked_cli(
        tmp_path, monkeypatch, capsys, *args, data_dir, operation_time=NOW):
    """Invoke the real parser/dispatcher with Amendment-1 operation time."""
    import kindex.cli as cli_mod

    monkeypatch.setattr(cli_mod, "operation_now", lambda: operation_time)
    monkeypatch.setattr(
        sys, "argv", ["kin", *args, "--data-dir", str(data_dir)]
    )
    try:
        result = cli_mod.main()
        returncode = 0 if result is None else int(result)
    except SystemExit as exc:
        returncode = int(exc.code or 0)
    captured = capsys.readouterr()
    return SimpleNamespace(
        returncode=returncode, stdout=captured.out, stderr=captured.err
    )


def _structured(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise AssertionError(f"expected structured mapping/list, got {type(value)!r}")


def _count_value(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    decoded = _structured(value)
    if isinstance(decoded, int) and not isinstance(decoded, bool):
        return decoded
    return decoded.get("pruned", decoded.get("count"))


def _bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    decoded = _structured(value)
    if isinstance(decoded, bool):
        return decoded
    return decoded.get("erased", decoded.get("deleted"))


def _add(store: Store, title: str, *, ttl_days: int = 7) -> str:
    return store.add_capture_candidate(
        title=title,
        content=f"review payload for {title}",
        node_type="concept",
        domains=["surface"],
        connections=[],
        source_digest=SOURCE,
        now=NOW,
        ttl_days=ttl_days,
    )


def _machine_error(text: str) -> str:
    match = re.search(r"Error:\s*([a-z0-9_]+):", text)
    assert match, text
    return match.group(1)


@pytest.mark.red_now
def test_p3_1_cli_candidate_lifecycle_is_complete_and_structured(
        tmp_path, monkeypatch, capsys):
    """P3.1/P3.7/P3.9/A9 Amendment 2: CLI list/show/accept/reject/prune/erase reach one temp
    store; missing commands, prose in --json, nonterminal transitions, or raw
    terminal payload/public prune clock override are forbidden, while typed
    receipts, seam-bound pruning, and exact erasure are demanded. Removing any
    candidate subcommand or restoring --now is the smallest mutation red.
    """
    data = tmp_path / "cli-candidates"
    store = Store(Config(data_dir=str(data)))
    accepted_id = _add(store, "CLI accept subject")
    rejected_id = _add(store, "CLI reject subject")
    expired_id = _add(store, "CLI expiry subject", ttl_days=1)
    store.close()

    listed = _cli(
        tmp_path, "candidate", "list", "--status", "pending", "--limit", "20",
        "--json", data_dir=data,
    )
    assert listed.returncode == 0, listed.stderr
    listed_rows = json.loads(listed.stdout)
    assert {accepted_id, rejected_id, expired_id} <= {
        row["id"] for row in listed_rows
    }
    assert all(
        not ({"title", "content", "node_type", "domains", "connections"}
             & set(row))
        for row in listed_rows
    )

    shown = _cli(
        tmp_path, "candidate", "show", accepted_id, "--json", data_dir=data
    )
    assert shown.returncode == 0, shown.stderr
    subject = json.loads(shown.stdout)
    assert subject["title"] == "CLI accept subject"
    assert re.fullmatch(r"[0-9a-f]{64}", subject["review_token"])

    accepted = _clocked_cli(
        tmp_path, monkeypatch, capsys, "candidate", "accept", accepted_id,
        "--review-token", subject["review_token"],
        "--by", "cli reviewer", "--method", "cli fixture",
        "--valid-at", NOW, "--json", data_dir=data,
    )
    assert accepted.returncode == 0, accepted.stderr
    accepted_receipt = json.loads(accepted.stdout)
    assert accepted_receipt["status"] == "accepted"
    assert accepted_receipt["created_node_id"]
    for field in ("title", "content", "node_type", "domains", "connections"):
        assert accepted_receipt[field] is None

    rejected = _clocked_cli(
        tmp_path, monkeypatch, capsys, "candidate", "reject", rejected_id,
        "--by", "cli reviewer", "--code", "not_supported", "--json",
        data_dir=data,
    )
    assert rejected.returncode == 0, rejected.stderr
    assert json.loads(rejected.stdout)["status"] == "rejected"

    pruned = _clocked_cli(
        tmp_path, monkeypatch, capsys, "candidate", "prune", "--json",
        data_dir=data, operation_time="2026-08-19T12:00:00Z",
    )
    assert pruned.returncode == 0, pruned.stderr
    prune_result = json.loads(pruned.stdout)
    assert _count_value(prune_result) == 1
    prune_help = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "candidate", "prune", "--help"],
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )
    assert "--now" not in (prune_help.stdout + prune_help.stderr)

    erased = _cli(
        tmp_path, "candidate", "erase", rejected_id, "--json", data_dir=data
    )
    assert erased.returncode == 0, erased.stderr
    erase_result = json.loads(erased.stdout)
    assert _bool_value(erase_result) is True
    check = Store(Config(data_dir=str(data)))
    try:
        assert check.get_capture_candidate(rejected_id) is None
        assert check.get_capture_candidate(expired_id)["status"] == "expired"
    finally:
        check.close()


@pytest.mark.red_now
def test_p4_1_p4_2_cli_verify_and_invalidate_expose_typed_fields(
        tmp_path, monkeypatch, capsys):
    """P4.1/P4.2: CLI verify/invalidate reach a legacy node; deletion,
    provenance rewrite, prose-only JSON, or missing trust fields is forbidden,
    while normalized asserted audit fields and exclusive invalid_at are demanded.
    Routing either command through generic edit is the smallest reversion red.
    """
    data = tmp_path / "cli-verify"
    store = Store(Config(data_dir=str(data)))
    node_id = store.add_node(
        "CLI correctable", content="keep", node_id="cli-correctable",
        prov_when="2024-01-01T00:00:00Z",
    )
    store.close()
    verified = _clocked_cli(
        tmp_path, monkeypatch, capsys, "verify", node_id, "--by", "cli auditor",
        "--method", "manual source", "--verified-at",
        "2026-08-18T07:00:00-05:00", "--valid-at", NOW, "--json",
        data_dir=data,
    )
    assert verified.returncode == 0, verified.stderr
    verified_node = json.loads(verified.stdout)
    assert verified_node["verified_at"] == NOW
    assert verified_node["verified_by"] == "cli auditor"
    assert verified_node["prov_method"] == "manual source"
    assert verified_node["prov_when"] == "2024-01-01T00:00:00Z"

    invalidated = _clocked_cli(
        tmp_path, monkeypatch, capsys, "invalidate", node_id, "--by", "cli auditor",
        "--code", "superseded", "--at", "2026-08-19T12:00:00Z", "--json",
        data_dir=data,
    )
    assert invalidated.returncode == 0, invalidated.stderr
    invalidated_node = json.loads(invalidated.stdout)
    assert invalidated_node["invalid_at"] == "2026-08-19T12:00:00Z"
    assert invalidated_node["content"] == "keep"
    assert invalidated_node["status"] == "active"


@pytest.mark.red_now
def test_p3_2_cli_and_mcp_report_same_machine_error_for_stale_token(
        tmp_path, monkeypatch, capsys):
    """P3.2/A9: the same temp Store receives bogus-token accept through CLI
    and MCP; traceback/prose-only errors or graph mutation are forbidden, while
    matching Error:<machine_code>: prefixes are demanded. Catching the typed
    error in only one adapter is the smallest mutation that turns red.
    """
    pytest.importorskip("mcp", reason="MCP extra required for parity contract")
    import kindex.mcp_server as mcp_mod

    data = tmp_path / "surface-errors"
    store = Store(Config(data_dir=str(data)))
    cli_id = _add(store, "CLI stale surface")
    mcp_id = _add(store, "MCP stale surface")
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: NOW)

    cli = _clocked_cli(
        tmp_path, monkeypatch, capsys, "candidate", "accept", cli_id,
        "--review-token", "0" * 64, "--by", "reviewer",
        "--method", "surface", data_dir=data,
    )
    cli_code = _machine_error(cli.stdout + cli.stderr)
    mcp_error = mcp_mod.candidate_accept(
        mcp_id, review_token="0" * 64, reviewed_by="reviewer",
        prov_method="surface",
    )
    mcp_code = _machine_error(str(mcp_error))
    assert mcp_code == cli_code
    assert store.get_node_by_title("CLI stale surface") is None
    assert store.get_node_by_title("MCP stale surface") is None


@pytest.mark.red_now
def test_p3_3_review_token_help_makes_no_auth_claim(tmp_path):
    """P3.3/I3: candidate accept help reaches the public token description;
    authentication/authorization/identity-proof claims are forbidden, while
    the review-token flag without those claims is demanded. Labeling the digest
    an auth token is the smallest documentation mutation that turns red.
    """
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "candidate", "accept", "--help"],
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )
    text = (result.stdout + result.stderr).lower()
    assert "review-token" in text
    assert "authentication" not in text
    assert "authorization" not in text
    assert "proof of" not in text or "identity" not in text


@pytest.mark.red_now
def test_p3_1_p4_1_mcp_lifecycle_returns_same_machine_fields(store, monkeypatch):
    """P3.1/P4.1/T9: every new MCP callable is invoked against one patched
    temp Store; absent tools, unstructured review state, or missing CLI-equivalent
    fields are forbidden, while list/show/terminal/node dictionaries are
    demanded. Returning human-only prose from one tool is the smallest mutation
    that turns red.
    """
    pytest.importorskip("mcp", reason="MCP extra required for MCP contract")
    import kindex.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: NOW)
    staged = _structured(mcp_mod.candidate_create(
        title="MCP staged subject", content="quarantined MCP payload",
        source_digest=SOURCE,
        domains=["surface"],
    ))
    assert set(staged) == {"id", "status", "created_at", "expires_at", "payload_digest"}
    assert staged["status"] == "pending"
    assert store.get_capture_candidate(staged["id"])["content"] == "quarantined MCP payload"
    invalid_type = mcp_mod.candidate_create(
        title="MCP hostile subject", content="quarantined MCP payload",
        source_digest=SOURCE, node_type="concept\x1b]52;c;forged\x07",
    )
    assert _machine_error(str(invalid_type)) == "invalid_input"
    assert "\x1b" not in str(invalid_type)
    too_large_ttl = mcp_mod.candidate_create(
        title="MCP ttl subject", content="quarantined MCP payload",
        source_digest=SOURCE, ttl_days=999_999_999,
    )
    assert _machine_error(str(too_large_ttl)) == "invalid_input"
    accepted_id = _add(store, "MCP accept subject")
    rejected_id = _add(store, "MCP reject subject")
    expired_id = _add(store, "MCP expiry subject", ttl_days=1)

    listed = _structured(mcp_mod.candidate_list(status="pending", limit=20))
    assert {accepted_id, rejected_id, expired_id} <= {row["id"] for row in listed}
    shown = _structured(mcp_mod.candidate_show(accepted_id))
    assert shown["title"] == "MCP accept subject"
    assert shown["review_token"]
    accepted = _structured(mcp_mod.candidate_accept(
        accepted_id, review_token=shown["review_token"],
        reviewed_by="mcp reviewer", prov_method="mcp fixture",
        valid_at=NOW,
    ))
    assert accepted["status"] == "accepted"
    assert accepted["created_node_id"]
    rejected = _structured(mcp_mod.candidate_reject(
        rejected_id, reviewed_by="mcp reviewer",
        disposition_code="not_supported",
    ))
    assert rejected["status"] == "rejected"
    monkeypatch.setattr(
        mcp_mod, "operation_now", lambda: "2026-08-19T12:00:00Z"
    )
    pruned = mcp_mod.candidate_prune()
    assert _count_value(pruned) == 1
    erased = mcp_mod.candidate_erase(rejected_id)
    assert _bool_value(erased) is True

    node_id = store.add_node(
        "MCP correctable", content="preserve", node_id="mcp-correctable",
        prov_when="2024-01-01T00:00:00Z",
    )
    verified = _structured(mcp_mod.verify(
        node_id, verified_by="mcp auditor", prov_method="manual source",
        verified_at=NOW, valid_at=NOW,
    ))
    assert verified["verified_by"] == "mcp auditor"
    assert verified["prov_method"] == "manual source"
    invalidated = _structured(mcp_mod.invalidate(
        node_id, invalidated_by="mcp auditor", disposition_code="superseded",
        invalid_at="2026-08-19T12:00:00Z",
    ))
    assert invalidated["invalid_at"] == "2026-08-19T12:00:00Z"
    assert invalidated["content"] == "preserve"


@pytest.mark.red_now
def test_t9_2_cli_mcp_operation_clock_and_machine_fields_agree(
        tmp_path, monkeypatch, capsys):
    """P3.7/P4.1/P4.2/T9.2/A9 Amendment 1: paired CLI/MCP accept,
    verify, and invalidate calls reach the same temp store through fixed
    operation_now seams; ambient/different timestamps or missing machine fields
    are forbidden, while identical normalized times/status fields are demanded.
    Bypassing either adapter seam is the smallest mutation that turns red.
    """
    pytest.importorskip("mcp", reason="MCP extra required for parity contract")
    import kindex.mcp_server as mcp_mod

    data = tmp_path / "clock-parity"
    store = Store(Config(data_dir=str(data)))
    cli_candidate = _add(store, "Clocked CLI candidate")
    mcp_candidate = _add(store, "Clocked MCP candidate")
    cli_token = store.candidate_review_token(cli_candidate)
    mcp_token = store.candidate_review_token(mcp_candidate)
    cli_node = store.add_node(
        "Clocked CLI node", node_id="clock-cli-node",
        prov_when="2024-01-01T00:00:00Z",
    )
    mcp_node = store.add_node(
        "Clocked MCP node", node_id="clock-mcp-node",
        prov_when="2024-01-01T00:00:00Z",
    )
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: NOW)

    cli_accept = _clocked_cli(
        tmp_path, monkeypatch, capsys, "candidate", "accept", cli_candidate,
        "--review-token", cli_token, "--by", "parity reviewer",
        "--method", "parity method", "--json", data_dir=data,
    )
    assert cli_accept.returncode == 0, cli_accept.stderr
    cli_receipt = json.loads(cli_accept.stdout)
    mcp_receipt = _structured(mcp_mod.candidate_accept(
        mcp_candidate, review_token=mcp_token,
        reviewed_by="parity reviewer", prov_method="parity method",
    ))
    for field in ("status", "reviewed_at", "reviewed_by", "review_method"):
        assert cli_receipt[field] == mcp_receipt[field]
    assert cli_receipt["status"] == "accepted"
    assert cli_receipt["reviewed_at"] == NOW
    assert cli_receipt["created_node_id"]
    assert mcp_receipt["created_node_id"]

    cli_verify = _clocked_cli(
        tmp_path, monkeypatch, capsys, "verify", cli_node,
        "--by", "parity reviewer", "--method", "parity method", "--json",
        data_dir=data,
    )
    assert cli_verify.returncode == 0, cli_verify.stderr
    cli_verified = json.loads(cli_verify.stdout)
    mcp_verified = _structured(mcp_mod.verify(
        mcp_node, verified_by="parity reviewer", prov_method="parity method"
    ))
    for field in ("verified_at", "verified_by", "prov_method"):
        assert cli_verified[field] == mcp_verified[field]
    assert cli_verified["verified_at"] == NOW

    cli_invalidate = _clocked_cli(
        tmp_path, monkeypatch, capsys, "invalidate", cli_node,
        "--by", "parity reviewer", "--code", "obsolete", "--json",
        data_dir=data,
    )
    assert cli_invalidate.returncode == 0, cli_invalidate.stderr
    cli_invalidated = json.loads(cli_invalidate.stdout)
    mcp_invalidated = _structured(mcp_mod.invalidate(
        mcp_node, invalidated_by="parity reviewer",
        disposition_code="obsolete",
    ))
    assert cli_invalidated["invalid_at"] == NOW
    assert mcp_invalidated["invalid_at"] == NOW
    assert cli_invalidated["status"] == mcp_invalidated["status"] == "active"
    store.close()


@pytest.mark.red_now
def test_p3_10_hostile_candidate_is_json_encoded_and_human_neutralized(
        tmp_path, monkeypatch):
    """P3.10: a raw-SQL corruption fixture makes ANSI, carriage return, HTML,
    and newline payload reachable at CLI/MCP display; raw ESC/CR in serialized
    or human output is forbidden, while exact JSON data and visible human
    candidate delimiting are demanded. Printing payload fields directly is the
    smallest mutation that turns red.
    """
    data = tmp_path / "hostile-display"
    store = Store(Config(data_dir=str(data)))
    candidate_id = _add(store, "safe display seed")
    hostile_title = "\x1b[31m<title>hostile</title>\rtitle"
    hostile_content = "line one\n\x1b[2J<div>line two</div>\rreturn"
    store.conn.execute(
        "UPDATE capture_candidates SET title=?, content=? WHERE id=?",
        (hostile_title, hostile_content, candidate_id),
    )
    store.conn.commit()

    cli_json = _cli(
        tmp_path, "candidate", "show", candidate_id, "--json", data_dir=data
    )
    assert cli_json.returncode == 0, cli_json.stderr
    assert "\x1b" not in cli_json.stdout and "\r" not in cli_json.stdout
    decoded = json.loads(cli_json.stdout)
    assert decoded["title"] == hostile_title
    assert decoded["content"] == hostile_content

    human = _cli(
        tmp_path, "candidate", "show", candidate_id, data_dir=data
    )
    assert human.returncode == 0, human.stderr
    assert "\x1b" not in human.stdout and "\r" not in human.stdout
    assert candidate_id in human.stdout
    assert "candidate" in human.stdout.lower() or "payload" in human.stdout.lower()
    assert "line one" in human.stdout and "line two" in human.stdout

    pytest.importorskip("mcp", reason="MCP extra required for MCP display")
    import kindex.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: NOW)
    mcp_value = mcp_mod.candidate_show(candidate_id)
    if isinstance(mcp_value, str):
        assert "\x1b" not in mcp_value and "\r" not in mcp_value
    mcp_decoded = _structured(mcp_value)
    assert mcp_decoded["title"] == hostile_title
    assert mcp_decoded["content"] == hostile_content
    store.close()


@pytest.mark.red_now
def test_r1_1_r1_5_mcp_tag_resume_is_bounded_and_trusted_by_default(
        store, monkeypatch):
    """R1.1/R1.5/T9.4: MCP tag_resume reaches an oversized tag with verified
    and legacy links; byte overflow or legacy leakage is forbidden, while the
    requested hard bound and trusted default are demanded. Passing
    trusted_only=False or ignoring tokens is the smallest mutation red.
    """
    pytest.importorskip("mcp", reason="MCP extra required for tag_resume")
    import kindex.mcp_server as mcp_mod
    from kindex.sessions import link_node_to_tag, start_tag

    start_tag(
        store, "mcp-bounded", description="description " * 100,
        focus="bounded current focus", remaining=["remaining work"],
    )
    trusted = store.add_node(
        "Trusted MCP context", content="admitted", node_id="mcp-context-trusted"
    )
    store.verify_node(
        trusted, verified_by="tester", prov_method="fixture", verified_at=NOW
    )
    legacy = store.add_node(
        "Legacy MCP context", content="must be denied", node_id="mcp-context-legacy"
    )
    link_node_to_tag(store, "mcp-bounded", trusted)
    link_node_to_tag(store, "mcp-bounded", legacy)
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", store.config)
    monkeypatch.setattr(mcp_mod, "operation_now", lambda: NOW)
    result = mcp_mod.tag_resume("mcp-bounded", tokens=256)
    assert isinstance(result, str)
    assert len(result.encode("utf-8")) <= 256
    assert "Legacy MCP context" not in result
