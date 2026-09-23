"""External contracts for routed, stdin-only automatic capture staging."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from kindex.config import Config
from kindex.store import Store


REPO = Path(__file__).resolve().parents[1]
SOURCE_DIGEST = hashlib.sha256(b"S2S capture source").hexdigest()


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "kindex.cli", "candidate", "create", *args]


def _payload(**overrides) -> bytes:
    value = {
        "title": "Routed semantic capture",
        "content": "This automatic extraction remains quarantined for review.",
        "source_digest": SOURCE_DIGEST,
        "domains": ["s2s"],
    }
    value.update(overrides)
    return json.dumps(value).encode("utf-8")


def _run(command: list[str], payload: bytes, *, cwd: Path, env: dict | None = None):
    return subprocess.run(
        command, input=payload, cwd=cwd, env=env, capture_output=True,
        check=False, timeout=30,
    )


def test_candidate_create_reads_only_bounded_json_from_stdin_and_returns_redacted_receipt(tmp_path):
    data_dir = tmp_path / "selected-store"
    result = _run(_command("--data-dir", str(data_dir), "--json"), _payload(), cwd=tmp_path)

    assert result.returncode == 0, result.stderr.decode()
    receipt = json.loads(result.stdout)
    assert set(receipt) == {"id", "status", "created_at", "expires_at", "payload_digest"}
    assert receipt["status"] == "pending"
    assert b"automatic extraction remains" not in result.stdout

    store = Store(Config(data_dir=str(data_dir)))
    try:
        candidate = store.get_capture_candidate(receipt["id"])
        assert candidate is not None
        assert candidate["content"] == "This automatic extraction remains quarantined for review."
    finally:
        store.close()


def test_candidate_create_rejects_unsupported_or_oversize_stdin_without_writing(tmp_path):
    data_dir = tmp_path / "selected-store"
    for payload in (
        _payload(unexpected="must not become an argv-like extension"),
        b"{" + b" " * (32 * 1024) + b"}",
    ):
        result = _run(_command("--data-dir", str(data_dir)), payload, cwd=tmp_path)
        assert result.returncode == 2

    store = Store(Config(data_dir=str(data_dir)))
    try:
        assert store.list_capture_candidates(limit=10) == []
    finally:
        store.close()


def test_candidate_create_rejects_hostile_metadata_and_deep_json_without_traceback(tmp_path):
    data_dir = tmp_path / "selected-store"
    hostile_type = "concept\x1b]52;c;forged\x07\n"
    deeply_nested = (
        b'{"title":"x","content":"x","source_digest":"' + SOURCE_DIGEST.encode()
        + b'","domains":' + b"[" * 1_000 + b"]" * 1_000 + b"}"
    )
    for payload in (_payload(node_type=hostile_type), deeply_nested,
                    _payload(ttl_days=999_999_999)):
        result = _run(_command("--data-dir", str(data_dir)), payload, cwd=tmp_path)
        assert result.returncode == 2
        assert b"\x1b" not in result.stderr
        assert b"Traceback" not in result.stderr

    store = Store(Config(data_dir=str(data_dir)))
    try:
        assert store.list_capture_candidates(limit=10) == []
    finally:
        store.close()


def test_candidate_create_routes_to_the_present_project_store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home_store = home / ".kindex"
    project_store = project / ".kin" / "local" / "kindex"
    (project / ".kin").mkdir(parents=True)
    (project / ".kin" / "config").write_text("name: capture-route\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    for index, directory in enumerate((home_store, project_store)):
        store = Store(Config(data_dir=str(directory)))
        store.add_node(f"route fixture {index}")
        store.close()

    env = {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "PATH": os.environ["PATH"],
        "PYTHONPATH": str(REPO / "src"), "PYTHONNOUSERSITE": "1",
    }
    implicit = _run(_command("--json"), _payload(), cwd=project, env=env)
    assert implicit.returncode == 0, implicit.stderr.decode()
    implicit_receipt = json.loads(implicit.stdout)

    routed = _run(
        _command("--project-path", str(project), "--json"), _payload(), cwd=tmp_path, env=env,
    )
    assert routed.returncode == 0, routed.stderr.decode()
    routed_receipt = json.loads(routed.stdout)
    project_db = Store(Config(data_dir=str(project_store)))
    home_db = Store(Config(data_dir=str(home_store)))
    try:
        assert project_db.get_capture_candidate(implicit_receipt["id"]) is not None
        assert home_db.get_capture_candidate(implicit_receipt["id"]) is None
        assert project_db.get_capture_candidate(routed_receipt["id"]) is not None
        assert home_db.get_capture_candidate(routed_receipt["id"]) is None
    finally:
        project_db.close()
        home_db.close()
