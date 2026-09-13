"""Independent public-contract queue regressions; Validator owns execution."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SCRATCH = Path(__file__).resolve().parent
MARKER = 'QUEUE_CONTRACT_RESULT='

WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from kindex.config import Config
from kindex.store import Store
from kindex.sim import _SimResult, enqueue_sim_review, set_sim_enabled, drain_sim_queue
from kindex.supervisor import worker_main, config_snapshot, read_state

request = json.loads(sys.argv[1])
root = Path(request['root'])
cfg = Config(data_dir=str(root / 'store'))
cfg.sim.enabled = True
cfg.sim.tick_interval = 1
cfg.sim.triage_banter = False
cfg.sim.drain_on_tick = True


def fake_call(cfg, ledger, window, conv, **kwargs):
    event = json.dumps({'conv': conv, 'mode': request.get('provider', 'quiet')}) + '\n'
    fd = os.open(root / 'provider-calls.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, event.encode('utf-8'))
    finally:
        os.close(fd)
    if request.get('provider') == 'crash':
        os._exit(77)
    if request.get('provider') == 'block' and conv == 'first-subject':
        (root / 'first-provider-entered').touch()
        deadline = time.monotonic() + 25
        while not (root / 'release-first-provider').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Fixture first-provider release deadline exceeded')
            time.sleep(0.02)
    if request.get('provider') == 'budget':
        return None, {'status': 'over_global_budget'}
    return _SimResult(rating=0.1, note='', basis='', dimension='', stakes='low',
                           escalate=False, escalate_reason=''), {'status': 'ok'}


with patch('kindex.sim.call_sim', side_effect=fake_call), patch('kindex.sim.build_sim_grounding', return_value=''):
    if request['op'] == 'worker':
        (root / ('worker-entered-' + request.get('provider', 'quiet'))).touch()
        worker_main()
        result = {'worker_returned': True}
    else:
        store = Store(cfg)
        try:
            operation = request['op']
            if operation == 'setup':
                set_sim_enabled(store, True)
                result = {'config_snapshot': config_snapshot(cfg)}
            elif operation == 'enqueue':
                conv = request['conv']
                accepted = enqueue_sim_review(
                    store, cfg, conv, 'Synthetic work request for ' + conv,
                    tick=1, intent='Evaluate the synthetic work request',
                    scope={'project_path': str(root / 'project'), 'session_id': conv, 'agent': 'claude'},
                )
                result = {'accepted': accepted}
            elif operation == 'drain':
                drain_sim_queue(store, cfg, max_jobs=5)
                result = {'drained': True}
            elif operation == 'inspect':
                result = {
                    'queue': store.get_meta('sim.queue'),
                    'states': {conv: read_state(store, conv) for conv in request['convs']},
                }
            else:
                raise ValueError('Unknown fixture operation')
        finally:
            store.close()
print('QUEUE_CONTRACT_RESULT=' + json.dumps(result))
'''


class SimQueueContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        for name in ('home', 'tmp', 'project'):
            (self.root / name).mkdir(mode=0o700)
        home = self.root / 'home'
        self.env = {
            'HOME': str(home),
            'XDG_CONFIG_HOME': str(home / '.config'),
            'XDG_DATA_HOME': str(home / '.local' / 'share'),
            'XDG_CACHE_HOME': str(home / '.cache'),
            'CLAUDE_CONFIG_DIR': str(home / '.claude'),
            'CODEX_HOME': str(home / '.codex'),
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
        self.children = []
        self.addCleanup(self.stop_children)
        self.snapshot = self.child('setup')['config_snapshot']

    def stop_children(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def request(self, operation, **kwargs):
        return {'op': operation, 'root': str(self.root), **kwargs}

    def spawn(self, operation, **kwargs):
        process = subprocess.Popen(
            [PYTHON, '-c', WORKER, json.dumps(self.request(operation, **kwargs))],
            cwd=self.root, env=self.env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.children.append(process)
        return process

    def decode(self, process, output, errors, expected_code=0):
        self.assertEqual(process.returncode, expected_code, output + errors)
        if expected_code != 0:
            return None
        lines = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
        self.assertEqual(len(lines), 1, output + errors)
        return json.loads(lines[0])

    def child(self, operation, expected_code=0, **kwargs):
        process = self.spawn(operation, **kwargs)
        output, errors = process.communicate(timeout=35)
        return self.decode(process, output, errors, expected_code)

    def enqueue(self, conv):
        return self.child('enqueue', conv=conv)

    def inspect(self, *convs):
        return self.child('inspect', convs=list(convs))

    def calls(self):
        path = self.root / 'provider-calls.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def start_worker(self, provider):
        process = self.spawn('worker', provider=provider)
        process.stdin.write(json.dumps(self.snapshot))
        process.stdin.close()
        process.stdin = None
        return process

    def wait_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.025)
        return bool(predicate())

    def test_01_provider_start_crash_retains_explicit_uncertainty_without_retry(self):
        conv = 'crash-subject'
        self.assertTrue(self.enqueue(conv)['accepted'])
        self.child('drain', provider='crash', expected_code=77)
        before_recovery = self.inspect(conv)
        for _ in range(3):
            self.child('drain', provider='quiet')
        recovered = self.inspect(conv)
        state = recovered['states'][conv]
        self.assertEqual(state['state'], 'failed', {'before': before_recovery, 'after': recovered})
        self.assertRegex(state['reason'].lower(), r'interrupt|uncertain')
        self.assertEqual([call['conv'] for call in self.calls()], [conv],
                         'Provider-started work must not be automatically charged again after a crash.')

    def test_02_second_background_worker_finishes_job_enqueued_during_first(self):
        self.assertTrue(self.enqueue('first-subject')['accepted'])
        first = self.start_worker('block')
        self.assertTrue(self.wait_until(lambda: (self.root / 'first-provider-entered').exists()),
                        'First background worker never reached the fake provider.')
        self.assertIsNone(first.poll(), 'First worker must remain active at the handoff boundary.')
        self.assertTrue(self.enqueue('second-subject')['accepted'])
        second = self.start_worker('quiet')
        # Leave the first worker blocked briefly so the second worker encounters
        # the active-worker boundary before the release. No later wake is sent.
        self.assertTrue(self.wait_until(lambda: (self.root / 'worker-entered-quiet').exists()),
                        'Second background worker never reached worker_main.')
        time.sleep(0.3)
        (self.root / 'release-first-provider').touch()
        self.assertTrue(self.wait_until(lambda: any(call['conv'] == 'second-subject' for call in self.calls()), 15),
                        'Second queued subject was stranded after the active worker completed.')
        for process in (first, second):
            output, errors = process.communicate(timeout=15)
            self.decode(process, output, errors)
        self.assertEqual(sorted(call['conv'] for call in self.calls()), ['first-subject', 'second-subject'])
        states = self.inspect('first-subject', 'second-subject')['states']
        for conv in ('first-subject', 'second-subject'):
            self.assertEqual(states[conv]['state'], 'reviewed_quiet', states[conv])

    def test_03_unchanged_budget_exhaustion_and_success_do_not_retry_endlessly(self):
        for provider, conv in (('budget', 'budget-subject'), ('quiet', 'quiet-subject')):
            with self.subTest(provider=provider):
                self.assertTrue(self.enqueue(conv)['accepted'])
                self.child('drain', provider=provider)
                for _ in range(3):
                    self.enqueue(conv)
                    self.child('drain', provider=provider)
                self.assertEqual(sum(call['conv'] == conv for call in self.calls()), 1,
                                 'Unchanged completed or budget-exhausted work must not repeatedly invoke the provider.')
                state = self.inspect(conv)['states'][conv]
                self.assertNotIn(state['state'], ('queued', 'running'), state)
                if provider == 'quiet':
                    self.assertEqual(state['state'], 'reviewed_quiet', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    arguments, unittest_arguments = parser.parse_known_args()
    REPO = arguments.repo.resolve()
    PYTHON = str(Path(arguments.python).resolve())
    unittest.main(argv=[sys.argv[0], *unittest_arguments])
