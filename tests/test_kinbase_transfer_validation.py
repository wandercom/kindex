"""Independent atomic rejection of malformed retained Kinbase recall metadata.

Oracle: parent-ratified metadata validation contract, not governance authority.
Only public Store and CLI export/import interfaces are exercised.
"""
import json
import os
import subprocess
import sys

import pytest


EXISTING = "f1000000-0000-4000-8000-000000000001"
VALID_FIRST = "f1000000-0000-4000-8000-000000000002"
MALFORMED_SECOND = "f1000000-0000-4000-8000-000000000003"


SEED = r'''
import json, sys
from kindex.config import Config
from kindex.store import Store
p = json.load(sys.stdin)
for item in p['stores']:
    store = Store(Config(data_dir=item['data_dir']))
    try:
        store.add_node(item['title'], content='Synthetic transfer-validation record.',
                       node_type='concept', node_id=item['id'], audience='org', extra=item['extra'])
    finally:
        store.close()
'''


def cli(fixture, *args):
    return subprocess.run([sys.executable, "-m", "kindex.cli", *args],
                          text=True, capture_output=True, cwd=fixture["root"], env=fixture["env"])


def export_rows(fixture, directory):
    result = cli(fixture, "export", "--audience", "org", "--format", "json",
                 "--data-dir", str(directory), "--config", "/dev/null")
    assert result.returncode == 0, result.stderr  # Public format/fixture reachability.
    rows = json.loads(result.stdout)
    assert isinstance(rows, list)
    return rows


@pytest.fixture
def transfer_validation(tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir()
    names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CURSOR_CONFIG_DIR=str(home / ".cursor"),
               KIN_HEALTH_DIR=str(tmp_path / "isolated-health"))
    fixture = {"root": tmp_path, "env": env, "source": tmp_path / "source", "destination": tmp_path / "destination"}
    metadata = {"repo": "synthetic-validation-repository", "logical_key": "synthetic-valid-key",
                "mode": "raw", "provenance": "external", "source_identity": "synthetic-validation-source"}
    payload = {"stores": [
        {"data_dir": str(fixture["source"]), "id": VALID_FIRST, "title": "Valid new record first",
         "extra": {"kinbase": metadata}},
        {"data_dir": str(fixture["destination"]), "id": EXISTING, "title": "Existing destination record",
         "extra": {}},
    ]}
    seeded = subprocess.run([sys.executable, "-c", SEED], input=json.dumps(payload),
                            text=True, capture_output=True, cwd=tmp_path, env=env)
    assert seeded.returncode == 0, seeded.stderr
    source_rows = export_rows(fixture, fixture["source"])
    assert len(source_rows) == 1
    fixture["valid_row"] = source_rows[0]
    assert fixture["valid_row"]["id"] == VALID_FIRST
    valid_metadata = fixture["valid_row"]["extra"]["kinbase"]
    assert valid_metadata["mode"] in ("raw", "reduced")
    assert isinstance(valid_metadata["repo"], str) and valid_metadata["repo"]
    assert isinstance(valid_metadata["logical_key"], str) and valid_metadata["logical_key"]
    return fixture


@pytest.mark.parametrize("field,value,omit", [
    pytest.param("repo", None, True, id="missing-repo"),
    pytest.param("repo", "", False, id="empty-repo"),
    pytest.param("repo", 7, False, id="nonstring-repo"),
    pytest.param("logical_key", None, True, id="missing-logical-key"),
    pytest.param("logical_key", "", False, id="empty-logical-key"),
    pytest.param("logical_key", [], False, id="nonstring-logical-key"),
    pytest.param("mode", None, True, id="missing-mode"),
    pytest.param("mode", "unknown", False, id="invalid-mode"),
    pytest.param("source_identity", "", False, id="empty-source-identity"),
    pytest.param("source_identity", {}, False, id="nonstring-source-identity"),
    pytest.param("reduction", "not-an-object", False, id="nonobject-reduction"),
    pytest.param("owner_role", [], False, id="nonstring-owner-role"),
    pytest.param("owner_identity", 7, False, id="nonstring-owner-identity"),
    pytest.param("status", {}, False, id="nonstring-status"),
])
def test_malformed_kinbase_metadata_rejects_the_whole_import_atomically(transfer_validation, field, value, omit):
    """Ratified validation: malformed retained metadata must not reach recall.

    Mutation: accept the malformed field, skip the bad row, or commit valid earlier
    rows before discovering the later invalid record. This is null/type safety,
    not proof of external-source governance or ownership authority.
    """
    fixture = transfer_validation
    before = {row["id"]: row for row in export_rows(fixture, fixture["destination"])}
    assert set(before) == {EXISTING}
    valid = fixture["valid_row"]
    bad = json.loads(json.dumps(valid))
    bad["id"] = MALFORMED_SECOND
    bad["title"] = "Malformed retained metadata second"
    if omit:
        del bad["extra"]["kinbase"][field]
    else:
        bad["extra"]["kinbase"][field] = value
    incoming = fixture["root"] / "valid-first-malformed-second.json"
    incoming.write_text(json.dumps([valid, bad]))
    imported = cli(fixture, "import", str(incoming), "--data-dir", str(fixture["destination"]), "--config", "/dev/null")
    assert imported.returncode != 0, "Importer accepted malformed extra.kinbase field: " + field
    after = {row["id"]: row for row in export_rows(fixture, fixture["destination"])}
    assert after == before
    assert VALID_FIRST not in after
    assert MALFORMED_SECOND not in after
