"""Independent contract tests; the Validator owns execution and verdicts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SCRATCH = Path(__file__).resolve().parent
MARKER = 'PUBLIC_CONTRACT_RESULT='

HISTORICAL_WORKER = r'''
import json
import sys
from unittest.mock import patch
from kindex.config import Config
from kindex.store import Store
from kindex.retrieve import hybrid_search
store = Store(Config(data_dir=str(sys.argv[1])))
try:
    node = store.add_node(
        title='Chronofixture authoritative standing',
        content='Chronofixture authorized evidence for historical recall.',
        standing='authoritative',
        extra={'expires': '2026-08-01'},
    )
    store.verify_node(
        node, verified_by='synthetic', prov_method='test',
        verified_at='2026-06-01T00:00:00Z', valid_at='2026-06-01T00:00:00Z',
    )
    with patch('kindex.vectors.is_available', return_value=False):
        before = hybrid_search(
            store, 'Chronofixture', top_k=5, trusted_only=True,
            evaluation_time='2026-07-01T00:00:00Z', expand_graph=False,
        )
        after = hybrid_search(
            store, 'Chronofixture', top_k=5, trusted_only=True,
            evaluation_time='2026-08-02T00:00:00Z', expand_graph=False,
        )
    print('PUBLIC_CONTRACT_RESULT=' + json.dumps({
        'node_id': node,
        'before_ids': [item['id'] for item in before],
        'after_ids': [item['id'] for item in after],
    }))
finally:
    store.close()
'''

NATIVE_WORKER = r'''
import json
import sys
from kindex.supervisor_health_activity import observe_activity
print('PUBLIC_CONTRACT_RESULT=' + json.dumps(observe_activity(float(sys.argv[1]))))
'''


class HistoricalAndNativeContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        (self.root / 'tmp').mkdir(mode=0o700)
        # A closed environment prevents access to real homes, credentials,
        # native-agent state, and user Kindex environment configuration.
        self.env = {
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_DATA_HOME': str(self.home / '.local' / 'share'),
            'XDG_CACHE_HOME': str(self.home / '.cache'),
            'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
            'CODEX_HOME': str(self.home / '.codex'),
            'KIN_HEALTH_DIR': str(self.root / 'health'),
            'TMPDIR': str(self.root / 'tmp'),
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'PYTHONPATH': os.pathsep.join((str(REPO / 'src'), str(REPO))),
            'PYTHONNOUSERSITE': '1',
            'PYTHONDONTWRITEBYTECODE': '1',
            'HF_HUB_OFFLINE': '1',
            'TRANSFORMERS_OFFLINE': '1',
        }

    def child(self, program, argument):
        response = subprocess.run(
            [PYTHON, '-c', program, str(argument)],
            cwd=self.root, env=self.env, text=True, capture_output=True,
            timeout=45, check=False,
        )
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        output = [line[len(MARKER):] for line in response.stdout.splitlines()
                  if line.startswith(MARKER)]
        self.assertEqual(len(output), 1, response.stdout + response.stderr)
        return json.loads(output[0])

    def test_historical_trusted_recall_uses_requested_evaluation_time(self):
        result = self.child(HISTORICAL_WORKER, self.root / 'store')
        self.assertIn(result['node_id'], result['before_ids'],
                      'Standing valid on July 1 must remain eligible for historical recall after wall-clock expiry.')
        self.assertNotIn(result['node_id'], result['after_ids'],
                         'The same standing must be ineligible when evaluated after August 1 expiry.')

    def test_bounded_native_scan_reports_resumed_session_or_incompleteness(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        projects = self.home / '.claude' / 'projects'
        resumed = projects / 'old-parent-resumed'
        resumed.mkdir(parents=True, mode=0o700)
        session = resumed / 'synthetic-resumed.jsonl'
        session.write_text(json.dumps({
            'type': 'user',
            'sessionId': 'synthetic-resumed',
            'cwd': str(self.root / 'synthetic-project'),
            'timestamp': '2026-09-12T12:00:00Z',
            'message': {'content': 'synthetic work'},
        }) + '\n', encoding='utf-8')
        os.utime(session, (now, now))
        old = now - 30 * 24 * 60 * 60
        os.utime(resumed, (old, old))
        # These newer parent directories contain no sessions. The only active
        # file lives below a parent whose mtime predates every scan competitor.
        for number in range(513):
            empty_project = projects / ('newer-empty-%04d' % number)
            empty_project.mkdir(mode=0o700)
            os.utime(empty_project, (now - number, now - number))
        result = self.child(NATIVE_WORKER, now)
        claude = result['claude']
        self.assertTrue(claude['source_present'], claude)
        observed = claude['state'] == 'observed' and claude['sessions'] >= 1
        incomplete = claude['state'] == 'unavailable' or claude['errors'] > 0
        self.assertTrue(observed or incomplete,
                        'A bounded scan must find the resumed session or explicitly signal incomplete observation: ' + repr(claude))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    arguments, unittest_arguments = parser.parse_known_args()
    REPO = arguments.repo.resolve()
    PYTHON = str(Path(arguments.python).resolve())
    unittest.main(argv=[sys.argv[0], *unittest_arguments])
