#!/usr/bin/env python3
"""Contract-only external regression tests. Execution belongs to the Validator."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SCRATCH = Path(__file__).resolve().parent
MARKER = 'SCOPE_CONTRACT_RESULT='

WORKER = r'''
import json
from pathlib import Path
import sys
from kindex.config import Config, load_config
from kindex.store import Store
request = json.loads(sys.argv[1])
try:
    if request['op'] == 'seed':
        store = Store(Config(data_dir=str(request['directory'])))
        try:
            for node_id in request['ids']:
                store.add_node(title='Synthetic scope fixture ' + node_id, node_id=node_id)
            result = {'ids': sorted(store.node_ids())}
        finally:
            store.close()
    elif request['op'] == 'load':
        kwargs = request.get('kwargs', {})
        if 'project_path' in kwargs:
            kwargs['project_path'] = Path(kwargs['project_path'])
        config = load_config(**kwargs)
        result = {'directory': str(Path(config.data_path).resolve())}
    else:
        raise ValueError('Unknown fixture operation')
except Exception as exc:
    result = {'error_type': type(exc).__name__, 'error': str(exc)}
print('SCOPE_CONTRACT_RESULT=' + json.dumps(result, sort_keys=True))
'''


class ScopeContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.home = self.root / 'home'
        self.project = self.root / 'project'
        self.home_store = self.home / '.kindex'
        self.project_store = self.project / '.kin' / 'local' / 'kindex'
        self.profile_store = self.root / 'personal-profile'
        self.xdg = self.home / '.config'
        for directory in (self.home, self.project / '.kin', self.xdg / 'kindex', self.root / 'tmp'):
            directory.mkdir(parents=True, mode=0o700)
        # The subprocess environment is deliberately closed; no credentials or
        # pre-existing Kindex configuration variables can reach product code.
        self.env = {
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.xdg),
            'XDG_DATA_HOME': str(self.home / '.local' / 'share'),
            'XDG_CACHE_HOME': str(self.home / '.cache'),
            'TMPDIR': str(self.root / 'tmp'),
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'PYTHONPATH': os.pathsep.join((str(REPO / 'src'), str(REPO))),
            'PYTHONNOUSERSITE': '1',
            'PYTHONDONTWRITEBYTECODE': '1',
        }
        init = self.run_process(['git', 'init', '--quiet', str(self.project)])
        self.assertEqual(init.returncode, 0, init.stderr)
        (self.project / '.kin' / 'config').write_text('name: scope-fixture\n', encoding='utf-8')
        self.seed(self.home_store, ['scope-home-node'])

    def run_process(self, args):
        return subprocess.run(args, cwd=self.project, env=self.env, text=True,
                              capture_output=True, timeout=30, check=False)

    def api(self, request):
        response = self.run_process([PYTHON, '-c', WORKER, json.dumps(request)])
        self.assertEqual(response.returncode, 0,
                         'Fixture/product child crashed:\n' + response.stdout + response.stderr)
        result_lines = [line[len(MARKER):] for line in response.stdout.splitlines()
                        if line.startswith(MARKER)]
        self.assertEqual(len(result_lines), 1, response.stdout + response.stderr)
        return json.loads(result_lines[0])

    def seed(self, directory, ids):
        result = self.api({'op': 'seed', 'directory': str(directory), 'ids': ids})
        self.assertNotIn('error', result, result)
        self.assertEqual(result['ids'], sorted(ids))
        self.assertTrue((directory / 'kindex.db').is_file())

    def create_project_store(self):
        self.seed(self.project_store, ['scope-project-node-a', 'scope-project-node-b'])

    def snapshot(self, directory):
        database = directory / 'kindex.db'
        self.assertTrue(database.is_file(), database)
        # Read-only SQLite snapshot covers all durable records without assuming
        # internal table or status-output schemas. No Store is opened to inspect.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('PRAGMA query_only = ON')
            dump = '\n'.join(connection.iterdump())
        return hashlib.sha256(dump.encode('utf-8')).hexdigest()

    def snapshots(self):
        return {str(path): self.snapshot(path) for path in (self.home_store, self.project_store)}

    def assert_selected(self, result, directory):
        self.assertNotIn('error', result, result)
        self.assertEqual(result['directory'], str(directory.resolve()))

    def test_01_hook_store_creation_exposes_implicit_ambiguity(self):
        self.assertFalse((self.project_store / 'kindex.db').exists())
        original_home = self.snapshot(self.home_store)
        self.assert_selected(self.api({'op': 'load'}), self.home_store)
        self.assertEqual(self.snapshot(self.home_store), original_home)
        self.assertFalse((self.project_store / 'kindex.db').exists())
        self.create_project_store()
        before = self.snapshots()
        ambiguous = self.api({'op': 'load'})
        # Preserve evidence even when selection behavior is wrong on baseline.
        self.assertEqual(self.snapshots(), before)
        self.assertIn('error', ambiguous,
                      'Implicit selection silently chose a scope after hook store creation: ' + repr(ambiguous))
        diagnostic = ambiguous['error']
        self.assertRegex(diagnostic.lower(), r'ambig|multiple.*(?:store|scope)|both.*(?:store|scope)|choose.*(?:store|scope)')
        self.assertIn(str(self.home_store), diagnostic)
        self.assertIn(str(self.project_store), diagnostic)
        self.assertIn('--project-path', diagnostic)
        self.assertIn('--data-dir', diagnostic)

    def test_02_explicit_project_and_named_profile_are_retained(self):
        self.create_project_store()
        self.seed(self.profile_store, ['scope-profile-a', 'scope-profile-b', 'scope-profile-c'])
        # JSON scalar syntax is valid YAML and safely quotes the absolute path.
        profile_yaml = 'profiles:\n  personal:\n    data_dir: ' + json.dumps(str(self.profile_store)) + '\n    roots: []\n'
        (self.xdg / 'kindex' / 'kin.yaml').write_text(profile_yaml, encoding='utf-8')
        before = self.snapshots()
        before_profile = self.snapshot(self.profile_store)
        project = self.api({'op': 'load', 'kwargs': {'project_path': str(self.project)}})
        profile = self.api({'op': 'load', 'kwargs': {'profile': 'personal'}})
        self.assertEqual(self.snapshots(), before)
        self.assertEqual(self.snapshot(self.profile_store), before_profile)
        self.assert_selected(project, self.project_store)
        self.assert_selected(profile, self.profile_store)

    def test_03_explicit_cli_scope_bypasses_implicit_guard(self):
        self.create_project_store()
        before = self.snapshots()
        commands = [
            (['--data-dir', str(self.home_store)], 1),
            (['--data-dir', str(self.home_store), '--config', '/dev/null'], 1),
            (['--project-path', str(self.project)], 2),
        ]
        for flags, expected_count in commands:
            with self.subTest(flags=flags):
                response = self.run_process([PYTHON, '-m', 'kindex.cli', 'status', '--json', *flags])
                self.assertEqual(self.snapshots(), before)
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                status = json.loads(response.stdout)
                self.assertEqual(status['stored_nodes'], expected_count, status)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    arguments, unittest_arguments = parser.parse_known_args()
    REPO = arguments.repo.resolve()
    PYTHON = str(Path(arguments.python).resolve())
    unittest.main(argv=[sys.argv[0], *unittest_arguments])
