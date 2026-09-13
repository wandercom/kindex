"""AC2 appendix: durable legacy stores and conflicting store preservation.

Oracle: Validator-supplied AC2 requirement and public Store/Config/resolver API.
No product implementation inspected or tests executed by this Tester.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SEED = """
import hashlib, json, sys
from pathlib import Path
from kindex.config import Config
from kindex.store import Store
p = json.load(sys.stdin)
store = Store(Config(data_dir=p['data_dir']))
try:
    if p['kind'] == 'candidate':
        identifier = store.add_capture_candidate(
            title='Independent durable candidate',
            content='Synthetic durable candidate for store-preservation acceptance.',
            source_digest=hashlib.sha256(b'independent-store-preservation').hexdigest(),
            node_type='concept',
        )
        if not identifier:
            raise AssertionError('Public candidate API did not return its promised ID')
    elif p['kind'] == 'reminder':
        identifier = store.add_reminder(
            title='Independent durable reminder',
            next_due='2030-01-01T12:00:00+00:00',
            body='Synthetic durable reminder for store-preservation acceptance.',
        )
        if not identifier:
            raise AssertionError('Public reminder API did not return its promised ID')
finally:
    store.close()
"""


RESOLVE = """
import json, sys
from pathlib import Path
from kindex.project_store import project_data_path
root = Path(json.load(sys.stdin)['root'])
try:
    selected = project_data_path(root)
except ValueError as error:
    print(json.dumps({'conflict': True, 'error': str(error)}))
else:
    print(json.dumps({'conflict': False, 'data_dir': str(selected)}))
"""


@pytest.fixture
def stores(tmp_path):
    project = tmp_path / "project"
    legacy = project / ".kin/local"
    nested = legacy / "kindex"
    legacy.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    inherited_names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in inherited_names if name in os.environ}
    env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"),
               XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"),
               KIN_HEALTH_DIR=str(tmp_path / "health"))
    return {"project": project, "legacy": legacy, "nested": nested, "env": env}


def invoke(stores, script, payload):
    result = subprocess.run([sys.executable, "-c", script],
                            input=json.dumps(payload), text=True, capture_output=True,
                            cwd=stores["project"], env=stores["env"])
    assert result.returncode == 0, result.stderr  # Shared public API must execute successfully.
    return result.stdout


def seed(stores, location, kind):
    invoke(stores, SEED, {"data_dir": str(stores[location]), "kind": kind})


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("legacy_kind", ["candidate", "reminder"])
def test_durable_only_legacy_store_is_selected_over_fresh_nested(stores, legacy_kind):
    """AC2: candidates-only/reminders-only are durable, despite no nodes.

    Mutation: determine populatedness from nodes alone or prefer fresh nested.
    """
    seed(stores, "legacy", legacy_kind)
    seed(stores, "nested", "empty")
    legacy_db = stores["legacy"] / "kindex.db"
    before = digest(legacy_db)
    result = json.loads(invoke(stores, RESOLVE, {"root": str(stores["project"])}))
    assert result["conflict"] is False
    assert Path(result["data_dir"]).resolve() == stores["legacy"].resolve()
    assert digest(legacy_db) == before


@pytest.mark.parametrize("legacy_kind", ["candidate", "reminder"])
def test_two_durable_stores_raise_conflict_without_mutation(stores, legacy_kind):
    """AC2: both durable stores require visible conflict and preserve originals.

    Mutation: choose either populated store, delete one, or modify it in conflict.
    """
    seed(stores, "legacy", legacy_kind)
    seed(stores, "nested", "candidate")
    databases = [stores[location] / "kindex.db" for location in ("legacy", "nested")]
    before = {path: digest(path) for path in databases}
    result = json.loads(invoke(stores, RESOLVE, {"root": str(stores["project"])}))
    assert result["conflict"] is True
    assert result["error"]
    for path in databases:
        assert path.is_file()
        assert digest(path) == before[path]
