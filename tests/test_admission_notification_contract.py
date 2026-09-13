"""Independent bounded admission and notification contracts; Validator executes."""
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
MARKER = 'ADMISSION_CONTRACT_RESULT='

WORKER = r'''
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

request = json.loads(sys.argv[1])
root = Path(request['root'])
operation = request['op']

if operation == 'notification':
    from kindex.supervisor_notifications import ensure_schema, inbox_snapshot
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    try:
        ensure_schema(connection)
        payload = {
            'code': 'missing_hooks',
            'scope': {'project_path': '/synthetic/project', 'session_id': 'synthetic', 'agent': 'claude'},
            'reason': 'Native session activity was observed but a corresponding recent Kindex hook invocation was not.',
            'evidence': {},
            'diagnostic': 'python3 -m kindex.supervisor_health status --json',
        }
        connection.execute(
            'INSERT INTO health_alerts (id,issue_id,created_at,updated_at,acknowledged_at,resolved_at,payload) VALUES (?,?,?,?,?,?,?)',
            ('synthetic-alert', 'synthetic-issue', 1789214400.0, 1789214400.0, None, None, json.dumps(payload)),
        )
        connection.commit()
        valid = inbox_snapshot(connection)
        invalid_results = {}
        for label, invalid in (
            ('extra_field', dict(payload, raw_prompt='SYNTHETIC_PRIVATE_CANARY')),
            ('invalid_code', dict(payload, code='arbitrary_unregistered_code')),
        ):
            connection.execute('UPDATE health_alerts SET payload=? WHERE id=?', (json.dumps(invalid), 'synthetic-alert'))
            connection.commit()
            try:
                snapshot = inbox_snapshot(connection)
            except ValueError:
                invalid_results[label] = {'rejected': True}
            else:
                invalid_results[label] = {'rejected': False, 'snapshot': snapshot}
        result = {'valid': valid, 'invalid': invalid_results}
    finally:
        connection.close()
else:
    # The Validator explicitly permits this independently authored legacy test
    # module as a public fixture/signing-helper dependency.
    fixture_path = Path(request['repo']) / 'tests' / 'test_kinbase_acceptance.py'
    spec = importlib.util.spec_from_file_location('legacy_independent_kinbase_fixture', fixture_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    source = root / 'source-repository'
    (source / '.kin' / 'events').mkdir(parents=True)
    (source / '.kin' / 'kinbase.toml').write_text('repository_id = "test-repository"\n')
    store = helper.Store(helper.Config(data_dir=str(root / 'destination')))
    try:
        with patch('kindex.vectors.is_available', return_value=False):
            if operation == 'unknown_identity':
                unknown = helper.unknown('stable-explicit-unknown')
                helper.write_doc(source, helper.sign(unknown))
                source_before = helper.source_snapshot(source)
                raw_report = helper.sync(store, source, mode='raw')
                raw = [{'id': row['id'], 'status': row['status']} for row in helper.rows(store)]
                reduced_unknown = {key: unknown[key] for key in (
                    'logical_key', 'question', 'owner_role', 'owner_identity', 'status', 'scope',
                    'decision_blocked', 'closure_evidence', 'response_due_at', 'expiry_policy',
                )}
                reduced_unknown.update(unknown_id=unknown['fact_id'], kind='explicit')
                payload = {
                    unknown['logical_key']: {
                        'logical_key': unknown['logical_key'], 'current': None,
                        'unknowns': [reduced_unknown], 'trusted': False,
                    },
                }
                binary = helper.executable(root, payload)
                helper.sync(store, source, mode='reduced', binary=binary)
                reduced = [{'id': row['id'], 'status': row['status']} for row in helper.rows(store)]
                result = {'raw_imported': raw_report['imported'], 'raw': raw, 'reduced': reduced,
                          'source_unchanged': helper.source_snapshot(source) == source_before}
            elif operation == 'timestamp':
                document_type = request['document_type']
                create = helper.fact if document_type == 'fact' else helper.unknown
                valid = create('valid-' + document_type)
                invalid = create('invalid-' + document_type, asserted_at='2026-W01-1T00:00:00Z')
                helper.write_doc(source, helper.sign(valid))
                helper.write_doc(source, helper.sign(invalid))
                source_before = helper.source_snapshot(source)
                report = helper.sync(store, source, mode='raw')
                result = {'imported': report['imported'], 'quarantined': report['quarantined'],
                          'node_count': len(helper.rows(store)),
                          'source_unchanged': helper.source_snapshot(source) == source_before}
            else:
                raise ValueError('Unknown fixture operation')
    finally:
        store.close()
print('ADMISSION_CONTRACT_RESULT=' + json.dumps(result))
'''


class AdmissionNotificationContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        (self.root / 'tmp').mkdir(mode=0o700)
        self.env = {
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_DATA_HOME': str(self.home / '.local' / 'share'),
            'XDG_CACHE_HOME': str(self.home / '.cache'),
            'XDG_STATE_HOME': str(self.home / '.local' / 'state'),
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

    def child(self, operation, **kwargs):
        work = self.root / (operation + '-' + kwargs.get('document_type', 'case'))
        work.mkdir(mode=0o700)
        request = {'op': operation, 'root': str(work), 'repo': str(REPO), **kwargs}
        response = subprocess.run(
            [PYTHON, '-c', WORKER, json.dumps(request)], cwd=work,
            env=self.env, text=True, capture_output=True, timeout=45, check=False,
        )
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        output = [line[len(MARKER):] for line in response.stdout.splitlines() if line.startswith(MARKER)]
        self.assertEqual(len(output), 1, response.stdout + response.stderr)
        return json.loads(output[0])

    def test_01_explicit_unknown_keeps_identity_across_raw_to_reduced_refresh(self):
        result = self.child('unknown_identity')
        self.assertEqual(result['raw_imported'], 1, result)
        self.assertEqual(len(result['raw']), 1, result)
        original = result['raw'][0]
        self.assertEqual(original['status'], 'active', result)
        active = [row for row in result['reduced'] if row['status'] == 'active']
        self.assertEqual([row['id'] for row in active], [original['id']],
                         'An explicit unknown with the same source fact identity must retain its active node ID.')
        self.assertTrue(result['source_unchanged'], result)

    def test_02_signed_non_rfc3339_week_dates_are_quarantined_in_direct_sync(self):
        for document_type in ('fact', 'unknown'):
            with self.subTest(document_type=document_type):
                result = self.child('timestamp', document_type=document_type)
                self.assertEqual(result['imported'], 1, result)
                self.assertEqual(result['quarantined'], 1, result)
                self.assertEqual(result['node_count'], 1, result)
                self.assertTrue(result['source_unchanged'], result)

    def test_03_database_notification_payloads_are_validated_before_display(self):
        result = self.child('notification')
        self.assertEqual(len(result['valid']['alerts']), 1, result['valid'])
        self.assertEqual(result['valid']['alerts'][0]['id'], 'synthetic-alert')
        for label, outcome in result['invalid'].items():
            with self.subTest(invalid_payload=label):
                self.assertTrue(outcome['rejected'],
                                'Stored notification payload must raise ValueError for invalid fields or codes: ' + repr(outcome))
                self.assertNotIn('SYNTHETIC_PRIVATE_CANARY', json.dumps(outcome))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    arguments, unittest_arguments = parser.parse_known_args()
    REPO = arguments.repo.resolve()
    PYTHON = str(Path(arguments.python).resolve())
    unittest.main(argv=[sys.argv[0], *unittest_arguments])
