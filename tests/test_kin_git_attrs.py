"""Git transformations must not contradict the UTF-8 .kin text contract."""

import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.ingest import write_kin_index
from kindex.setup import (
    audit_kin_text_attributes,
    install_kin_text_attributes,
    kin_text_attribute_findings,
)
from kindex.store import Store


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / ".kin").mkdir()
    return root


def test_text_policy_overrides_global_encoding_but_not_local_override(tmp_path):
    root = _repo(tmp_path)
    global_attrs = tmp_path / "global-attributes"
    global_attrs.write_text(".kin/* working-tree-encoding=CP1252 filter=alter\n", encoding="utf-8")
    _git(root, "config", "core.attributesFile", str(global_attrs))

    assert install_kin_text_attributes(root)
    assert install_kin_text_attributes(root) == []
    path = root / ".kin" / "config"
    assert kin_text_attribute_findings(root, path) == ([], [])
    assert kin_text_attribute_findings(root, root / ".kin" / "knowledge.jsonl") == ([], [])
    path.write_text("name: Café\n", encoding="utf-8")
    _git(root, "add", ".kin/config")
    blob = subprocess.check_output(["git", "-C", str(root), "show", ":.kin/config"])
    assert blob == path.read_bytes()
    # Local info/attributes outranks the tracked policy; runtime checks catch it.
    (root / ".git" / "info" / "attributes").write_text(
        ".kin/config working-tree-encoding=CP1252\n", encoding="utf-8"
    )
    errors, _ = kin_text_attribute_findings(root, path)
    assert "working-tree-encoding=CP1252" in errors[0]


def test_doctor_reports_checkout_transformation_before_loading_config(tmp_path):
    root = _repo(tmp_path)
    config = root / ".kin" / "config"
    config.write_text("name: Café\n", encoding="utf-8")
    _git(root, "add", ".kin/config")
    _git(root, "commit", "-qm", "UTF-8 config")
    install_kin_text_attributes(root)
    (root / ".git" / "info" / "attributes").write_text(
        ".kin/config working-tree-encoding=CP1252\n", encoding="utf-8"
    )
    config.unlink()
    _git(root, "restore", ".kin/config")
    assert config.read_bytes() == b"name: Caf\xe9\n"

    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "doctor", "--json",
         "--project-path", str(root), "--data-dir", str(tmp_path / "data")],
        capture_output=True, text=True, timeout=30, cwd=root,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert any("working-tree-encoding=CP1252" in item for item in report["issues"])
    assert any("not UTF-8" in item for item in report["issues"])
    assert "Traceback" not in result.stderr
    assert config.read_bytes() == b"name: Caf\xe9\n"


def test_index_writer_refuses_local_filter_and_preserves_existing_file(tmp_path):
    root = _repo(tmp_path)
    index = root / ".kin" / "index.json"
    index.write_bytes(b'{"nodes": []}\n')
    (root / ".git" / "info" / "attributes").write_text(
        ".kin/index.json filter=alter\n", encoding="utf-8"
    )
    store = Store(Config(data_dir=str(tmp_path / "data")))
    try:
        with pytest.raises(ValueError, match="filter=alter"):
            write_kin_index(store, root)
    finally:
        store.close()
    assert index.read_bytes() == b'{"nodes": []}\n'
    assert (root / ".gitattributes").exists()


def test_code_map_export_refuses_local_filter_without_replacing_file(tmp_path):
    root = _repo(tmp_path)
    output = root / ".kin" / "code-map.json"
    output.write_bytes(b'{"original": true}\n')
    (root / ".git" / "info" / "attributes").write_text(
        ".kin/code-map.json filter=alter\n", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "export", "code-map",
         "--directory", str(root), "--output", str(output),
         "--data-dir", str(tmp_path / "data")],
        capture_output=True, text=True, timeout=30, cwd=root,
    )
    assert result.returncode == 2
    assert "filter=alter" in result.stderr
    assert "Traceback" not in result.stderr
    assert output.read_bytes() == b'{"original": true}\n'


def test_repo_evidence_publisher_refuses_local_encoding_override(tmp_path):
    from kindex.repo_memory import publish

    root = _repo(tmp_path)
    (root / ".git" / "info" / "attributes").write_text(
        ".kin/knowledge.jsonl working-tree-encoding=CP1252\n", encoding="utf-8"
    )
    store = Store(Config(data_dir=str(tmp_path / "data")))
    try:
        node_id = store.add_node("Café evidence", content="Résumé", audience="team")
        with pytest.raises(ValueError, match="working-tree-encoding=CP1252"):
            publish(store, root, [node_id])
        assert not (root / ".kin" / "knowledge.jsonl").exists()
        (root / ".git" / "info" / "attributes").write_text("", encoding="utf-8")
        publish(store, root, [node_id])
    finally:
        store.close()
    assert "Résumé".encode("utf-8") in (root / ".kin" / "knowledge.jsonl").read_bytes()


def test_doctor_warns_on_missing_policy_without_changing_files(tmp_path):
    root = _repo(tmp_path)
    config = root / ".kin" / "config"
    config.write_text("name: example\n", encoding="utf-8")
    _git(root, "add", ".kin/config")
    before = config.read_bytes()
    errors, warnings = audit_kin_text_attributes(root)
    assert errors == []
    assert any("LF text attributes are not pinned" in warning for warning in warnings)
    result = subprocess.run(
        [sys.executable, "-m", "kindex.cli", "doctor", "--json",
         "--project-path", str(root), "--data-dir", str(tmp_path / "data")],
        capture_output=True, text=True, timeout=30, cwd=root,
    )
    assert result.returncode == 0, result.stderr
    assert any("LF text attributes are not pinned" in warning
               for warning in json.loads(result.stdout)["warnings"])
    assert not (root / ".gitattributes").exists()
    assert config.read_bytes() == before
