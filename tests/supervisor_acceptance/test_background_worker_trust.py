"""Independent regression: reviewed workspaces cannot replace background Kindex.

Oracle: parent-ratified worker trust contract and public spawn_background_drain API.
No implementation source read or tests executed by the independent Tester.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


LAUNCH = r'''
import json, os, sys
from pathlib import Path
import kindex
from kindex.config import Config
from kindex.sim import spawn_background_drain

p = json.load(sys.stdin)
workspace = Path(p['workspace']).resolve()
trusted_package = Path(kindex.__file__).resolve()
if trusted_package.is_relative_to(workspace):
    raise AssertionError('Fixture parent imported workspace package before launch')
cfg = Config(data_dir=p['data_dir'])
cfg._project_path = str(workspace)
cfg.sim.enabled = True
cfg.sim.drain_on_tick = True
cfg.llm.enabled = False
os.chdir(workspace)
launched = spawn_background_drain(cfg)
print(json.dumps({'launched': launched, 'trusted_package': str(trusted_package)}))
'''


def schema_is_ready(database):
    # Generic SQLite catalog, opened read-only: this probe cannot create the Store.
    if not database.is_file():
        return False
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        tables = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        return tables > 0 and connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def test_background_drain_uses_trusted_package_despite_workspace_shadow(tmp_path):
    """Worker trust regression: workspace kindex package must never execute.

    Mutation: launch `python -m kindex...` with ordinary workspace-first import
    resolution. The marker proves behavioral red; real isolated schema proves the
    positive worker effect, without a parent-created Store or a provider call.
    """
    workspace = tmp_path / "reviewed-workspace"
    planted_package = workspace / "kindex"
    planted_package.mkdir(parents=True)
    marker = tmp_path / "WORKSPACE_PACKAGE_EXECUTED"
    malicious = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('synthetic workspace import executed')\n"
    (planted_package / "__init__.py").write_text(malicious)
    (planted_package / "supervisor.py").write_text(malicious)
    neutral = tmp_path / "trusted-parent-entry"
    neutral.mkdir()
    home = tmp_path / "isolated-home"
    home.mkdir()
    data_dir = tmp_path / "intended-worker-state"
    database = data_dir / "kindex.db"
    names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"),
               CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"),
               CURSOR_CONFIG_DIR=str(home / ".cursor"),
               KIN_HEALTH_DIR=str(tmp_path / "isolated-health"))
    assert not data_dir.exists()
    assert not marker.exists()
    parent = subprocess.run([sys.executable, "-c", LAUNCH],
                            input=json.dumps({"workspace": str(workspace), "data_dir": str(data_dir)}),
                            text=True, capture_output=True, cwd=neutral, env=env, timeout=15)
    assert parent.returncode == 0, parent.stderr
    report = json.loads(parent.stdout)
    assert report["launched"] is True
    assert not Path(report["trusted_package"]).is_relative_to(workspace)

    # Observe the detached worker's concrete outcome; the deadline is a watchdog,
    # not a performance requirement or a fixed sleep used as success evidence.
    deadline = time.monotonic() + 10
    ready = False
    while time.monotonic() < deadline:
        if marker.exists():
            break
        if schema_is_ready(database):
            ready = True
            break
        time.sleep(0.02)
    assert not marker.exists(), "Detached worker executed the reviewed workspace's planted kindex package"
    assert ready, "Trusted worker did not create the intended isolated SQLite schema"
