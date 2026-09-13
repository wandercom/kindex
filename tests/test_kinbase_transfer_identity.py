"""Independent shared-transfer identity and recursive local-path privacy guards.

Oracle: parent-ratified org export/import contract and public Store/attach_unknowns.
No product implementation source inspected and no tests executed by the Tester.
"""
import json
import os
import subprocess
import sys

import pytest


ALPHA = "f0000000-0000-4000-8000-000000000001"
BETA = "f0000000-0000-4000-8000-000000000002"
QUESTION = "f0000000-0000-4000-8000-000000000003"
EVIDENCE_URL = "https://example.test/evidence/synthetic-alpha"
UNC_PATH = r"\\server\private-unc-owner\repo"
DRIVE_PATH = r"C:private-drive-owner\file"


SEED = r'''
import json, sys
from kindex.config import Config
from kindex.store import Store
from kindex.kinbase import attach_unknowns
p = json.load(sys.stdin)
store = Store(Config(data_dir=p['data_dir']))
try:
    for item in p['nodes']:
        store.add_node(item['title'], content=item['content'], node_type=item['type'],
                       node_id=item['id'], audience='org', extra={'kinbase':item['kinbase']})
    output = {}
    for identity in p['facts']:
        node = store.get_node(identity)
        if node is None:
            raise AssertionError('Public Store did not preserve explicit fixture node ID')
        attach_unknowns(store, node)
        output[identity] = [item['id'] for item in node.get('kinbase_unknowns', [])]
    print(json.dumps(output))
finally:
    store.close()
'''


ASSOCIATIONS = r'''
import json, sys
from kindex.config import Config
from kindex.store import Store
from kindex.kinbase import attach_unknowns
p = json.load(sys.stdin)
store = Store(Config(data_dir=p['data_dir']))
try:
    output = {}
    for identity in p['facts']:
        node = store.get_node(identity)
        if node is None:
            raise AssertionError('Roundtrip did not preserve the explicit node ID')
        attach_unknowns(store, node)
        output[identity] = [item['id'] for item in node.get('kinbase_unknowns', [])]
    print(json.dumps(output))
finally:
    store.close()
'''


def run_python(fixture, script, payload):
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(payload),
                            text=True, capture_output=True, cwd=fixture["root"], env=fixture["env"])
    assert result.returncode == 0, result.stderr  # Ratified public fixture/API reachability.
    return json.loads(result.stdout)


def cli(fixture, *args):
    result = subprocess.run([sys.executable, "-m", "kindex.cli", *args],
                            text=True, capture_output=True, cwd=fixture["root"], env=fixture["env"])
    assert result.returncode == 0, result.stderr  # Ratified public export/import CLI.
    return result.stdout


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_strings(child)


@pytest.fixture
def transfer(tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir()
    names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CURSOR_CONFIG_DIR=str(home / ".cursor"),
               KIN_HEALTH_DIR=str(tmp_path / "isolated-health"))
    fixture = {"root": tmp_path, "env": env, "source": tmp_path / "source-data", "target": tmp_path / "target-data"}

    def metadata(key, identity):
        return {"repo": "synthetic-acceptance-repository", "logical_key": key,
                "source_identity": identity, "mode": "raw", "provenance": "external"}

    alpha = metadata("/private/alpha", "synthetic:fact:alpha")
    alpha["evidence"] = {"nested": [{"unc": UNC_PATH, "drive_relative": DRIVE_PATH,
                                      "url": EVIDENCE_URL}]}
    nodes = [
        {"id": ALPHA, "title": "Synthetic alpha fact", "content": "Synthetic alpha observation.",
         "type": "concept", "kinbase": alpha},
        {"id": BETA, "title": "Synthetic beta fact", "content": "Synthetic beta observation.",
         "type": "concept", "kinbase": metadata("/private/beta", "synthetic:fact:beta")},
        {"id": QUESTION, "title": "Synthetic alpha unknown", "content": "Which synthetic alpha check remains unresolved?",
         "type": "question", "kinbase": metadata("/private/alpha", "synthetic:question:alpha")},
    ]
    source_links = run_python(fixture, SEED, {"data_dir": str(fixture["source"]), "nodes": nodes,
                                            "facts": [ALPHA, BETA]})
    # Ratified precondition: the synthetic fixture associates only alpha before transfer.
    assert source_links[ALPHA] == [QUESTION]
    assert source_links[BETA] == []
    return fixture


def export_org(transfer):
    raw = cli(transfer, "export", "--audience", "org", "--format", "json",
              "--data-dir", str(transfer["source"]), "--config", "/dev/null")
    rows = json.loads(raw)
    assert isinstance(rows, list)  # Ratified JSON export format.
    by_id = {row["id"]: row for row in rows}
    assert {ALPHA, BETA, QUESTION} <= set(by_id)
    return raw, by_id


def test_org_roundtrip_preserves_distinct_logical_identity_and_question_association(transfer):
    """Ratified transfer identity: sanitize paths without collapsing semantic keys.

    Mutation: replace all path-shaped logical keys with one generic marker, or
    independently rekey fact/question occurrences of the same identity.
    """
    raw, rows = export_org(transfer)
    alpha = rows[ALPHA]["extra"]["kinbase"]
    beta = rows[BETA]["extra"]["kinbase"]
    question = rows[QUESTION]["extra"]["kinbase"]
    assert alpha["logical_key"] != beta["logical_key"]
    assert alpha["logical_key"] == question["logical_key"]
    assert alpha["source_identity"] != beta["source_identity"]
    assert "/private/alpha" not in set(all_strings([alpha, beta, question]))
    assert "/private/beta" not in set(all_strings([alpha, beta, question]))
    exported = transfer["root"] / "shared-org-export.json"
    exported.write_text(raw)
    cli(transfer, "import", str(exported), "--data-dir", str(transfer["target"]), "--config", "/dev/null")
    restored = run_python(transfer, ASSOCIATIONS, {"data_dir": str(transfer["target"]), "facts": [ALPHA, BETA]})
    assert restored[ALPHA] == [QUESTION]
    assert restored[BETA] == []


def test_org_export_redacts_recursive_windows_paths_but_preserves_https_evidence(transfer):
    """Ratified recursive privacy: UNC and drive-relative local paths are private.

    Mutation: recognize only Unix/drive-absolute paths, or remove HTTPS evidence.
    Decoded string traversal prevents JSON backslash escaping from faking redaction.
    """
    _, rows = export_org(transfer)
    exported_strings = list(all_strings(rows[ALPHA]["extra"]["kinbase"]))
    assert not any("private-unc-owner" in value for value in exported_strings)
    assert not any("private-drive-owner" in value for value in exported_strings)
    assert EVIDENCE_URL in exported_strings
