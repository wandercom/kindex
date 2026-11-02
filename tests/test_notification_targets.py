"""Output routing and session handoffs must not imply execution or resend work."""
import json
import os

import pytest

from kindex import actions, reminders
from kindex.config import Config
from kindex.notify import dispatch, notification_text
from kindex.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path / 'graph'))


@pytest.fixture
def store(config):
    value = Store(config)
    yield value
    value.close()


def test_default_is_quiet_null(config, capsys, monkeypatch):
    monkeypatch.setattr('kindex.notify.subprocess.Popen', lambda *a, **k: pytest.fail('notification subprocess'))
    assert config.reminders.default_channels == ['output']
    assert config.reminders.output_destination == '/dev/null'
    assert dispatch({'title': 'Private reminder'}, config)[0].success
    assert capsys.readouterr() == ('', '')


def test_file_append_and_explicit_destination_override(config, tmp_path):
    path = tmp_path / 'notifications.log'
    config.reminders.default_channels = ['system']
    reminder = {'id': 'r1', 'title': 'Review changes', 'body': 'Review changes',
                'extra': {'notification_destination': str(path)}}
    for _ in range(2):
        assert dispatch(reminder, config)[0].success
    assert path.read_text().count('Reminder due: Review changes') == 2
    assert 'Reminder ID: r1' in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_descriptor_is_not_closed(config, tmp_path):
    path = tmp_path / 'descriptor.log'
    with path.open('w') as handle:
        config.reminders.output_destination = f'fd:{handle.fileno()}'
        assert dispatch({'title': 'Check build'}, config)[0].success
        handle.write('still open\n')
    assert 'still open' in path.read_text()
    assert 'Check build' in path.read_text()


def test_nonblocking_pipe_descriptor(config):
    reader, writer = os.pipe()
    try:
        os.set_blocking(writer, False)
        config.reminders.output_destination = f'fd:{writer}'
        assert dispatch({'title': 'Pipe notice'}, config)[0].success
        assert b'Pipe notice' in os.read(reader, 4096)
        assert not os.get_blocking(writer)
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize('destination', ['relative.log', 'fd:no', 'https://user:secret@example.test/x', 'http://', 'MISSING_PARENT'])
def test_bad_destination_is_quiet_failure(config, capsys, destination, tmp_path):
    config.reminders.output_destination = str(tmp_path / "missing-parent" / "notice.log") if destination == "MISSING_PARENT" else destination
    results = dispatch({'title': 'Due'}, config)
    assert len(results) == 1
    assert not results[0].success
    assert results[0].channel == 'output'
    assert 'secret' not in results[0].message
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('status,success', [(200, True), (204, True), (302, False), (500, False)])
def test_http_post_status(config, monkeypatch, status, success):
    config.reminders.output_destination = 'https://example.test/notifications'
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return 0, str(status), ''
    monkeypatch.setattr(actions, '_run_process', run)
    result = dispatch({'id': 'r1', 'title': 'Review build', 'next_due': '2099-01-01'}, config)[0]
    assert result.success is success
    job = json.loads(calls[0][1]['input_text'])
    assert job['destination'] == config.reminders.output_destination
    assert job['payload']['text'].startswith('Reminder due: Review build')
    assert calls[0][1]['timeout'] == config.reminders.output_timeout
    assert config.reminders.output_destination not in calls[0][0]
    assert calls[0][0][1:4] == ['-I', '-S', '-c']


def test_http_timeout_no_fallback(config, monkeypatch, capsys):
    config.reminders.output_destination = 'https://example.test/hook?token=private'
    monkeypatch.setattr(actions, '_run_process', lambda *a, **kw: (None, '', 'private'))
    result = dispatch({'title': 'Due'}, config)[0]
    assert not result.success and 'private' not in result.message
    assert capsys.readouterr() == ('', '')


def test_wording_distinguishes_notice_from_execution():
    text = notification_text({'title': 'Inspect failures', 'body': 'Inspect failures',
                             'extra': {'wake_client': 'codex', 'wake_session_id': 'target'}})
    assert text.count('Inspect failures') == 1
    assert 'codex session target' in text
    assert 'tracked separately' in text


@pytest.mark.parametrize('client,flag', [('claude', '--resume'), ('cursor', '--resume'),
                                       ('antigravity', '--conversation'), ('muse', '--session-id'),
                                       ('opencode', '--session'), ('codex', 'resume')])
def test_explicit_target_and_cwd_survive(client, flag, config, store, tmp_path, monkeypatch):
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if '--help' in cmd:
            return 0, flag, ''
        return 0, 'completed turn', ''
    monkeypatch.setattr(actions, '_run_process', run)
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    rid = reminders.create_reminder(store, 'Review tests', 'in 1 hour', wake_client=client,
                                    wake_session_id='target-123', wake_cwd=str(tmp_path),
                                    notification_destination='/dev/null')
    result = actions.execute_action(store, store.get_reminder(rid), config)
    assert result['status'] == 'completed'
    cmd, kwargs = calls[-1]
    assert cmd[cmd.index(flag) + 1] == 'target-123'
    assert str(tmp_path) in cmd or kwargs.get('cwd') == str(tmp_path.resolve())
    assert '--last' not in cmd and '--continue' not in cmd


@pytest.mark.parametrize('client', ['claude', 'cursor', 'antigravity', 'muse'])
def test_unresolved_last_is_refused(store, client):
    with pytest.raises(ValueError, match='Cannot resolve'):
        reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client=client, wake_session_id='last')


@pytest.mark.parametrize('client', ['cursor', 'antigravity', 'muse'])
def test_unsupported_cli_fails_before_prompt(store, config, client, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    monkeypatch.setattr(actions, '_run_process', lambda cmd, **kw: (calls.append(cmd) or (0, 'old help', '')))
    rid = reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client=client, wake_session_id='id')
    result = actions.execute_action(store, store.get_reminder(rid), config)
    assert result['status'] == 'exhausted'
    assert len(calls) == 1 and '--help' in calls[0]


@pytest.mark.parametrize('queue_code,state', [(0, 'queued'), (None, 'submission_unknown'), (1, 'submission_unknown')])
@pytest.mark.parametrize('recurring', [False, True])
def test_queue_handoff_is_not_completed_or_resent(store, config, monkeypatch, queue_code, state, recurring):
    calls = []
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == 'exec':
            return 1, '', 'thread/resume failed: thread exact-id already has an active writer'
        assert cmd[:4] == ['codex', 'queue', '--thread', 'exact-id']
        return queue_code, 'accepted', ''
    monkeypatch.setattr(actions, '_run_process', run)
    rid = store.add_reminder('Wake', '2000-01-01', reminder_type='recurring' if recurring else 'once',
                             schedule='FREQ=HOURLY' if recurring else '', extra={
                                 'wake_client': 'codex', 'wake_session_id': 'exact-id',
                                 'action_mode': 'codex', 'action_status': 'pending'})
    row = store.get_reminder(rid)
    # Direct execution avoids the deliberate stale-wake guard in the sweep.
    assert actions.execute_action(store, row, config)['status'] == state
    assert store.get_reminder(rid)['extra']['action_status'] == state
    assert store.get_reminder(rid)['status'] != 'completed'
    assert actions.execute_action(store, row, config, manual=True)['status'] == 'skipped'
    assert reminders._fire_one(store, config, store.get_reminder(rid)) is recurring
    if recurring:
        fresh = store.get_reminder(rid)
        assert fresh['extra']['action_status'] == 'pending'
        assert fresh['extra']['last_action_delivery']['status'] == state
        assert fresh['extra']['last_action_delivery']['occurrence'] == '2000-01-01'
        assert fresh['next_due'] != '2000-01-01'
    assert len(calls) == 2


def test_output_failure_does_not_prevent_wake_and_is_recorded(store, config, monkeypatch, tmp_path):
    monkeypatch.setattr(actions, '_run_codex', lambda *a, **kw: {'ok': True, 'output': 'turn complete'})
    rid = reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client='codex',
                                    wake_session_id='target', notification_destination=str(tmp_path / 'missing-parent' / 'notice'))
    assert reminders._fire_one(store, config, store.get_reminder(rid))
    row = store.get_reminder(rid)
    assert row['extra']['action_status'] == 'completed'
    assert not row['extra']['notification_results'][0]['success']


def test_crashed_wake_is_not_automatically_reclaimed(store, config, monkeypatch):
    monkeypatch.setattr(actions, '_run_process', lambda *a, **kw: pytest.fail('duplicate wake'))
    rid = store.add_reminder('Wake', '2000-01-01', extra={
        'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex',
        'action_status': 'running', 'action_executed_at': '2000-01-01T00:00:00'})
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'skipped'
    assert store.get_reminder(rid)['extra']['action_status'] == 'submission_unknown'


def test_cli_and_mcp_expose_same_destination_and_agents(monkeypatch, store, config, tmp_path):
    from kindex.cli import build_parser
    from kindex import mcp_server
    args = build_parser().parse_args(['remind', 'create', 'Review', '--at', 'in 1 hour',
                                      '--wake', 'cursor', '--session', 'target',
                                      '--destination', str(tmp_path / 'notice.log')])
    assert args.wake_client == 'cursor' and args.destination.endswith('notice.log')
    monkeypatch.setattr(mcp_server, '_get_store', lambda: (store, config))
    result = mcp_server.remind_create('Review', 'in 1 hour', scope='global',
                                     wake='cursor', wake_session='target',
                                     wake_cwd=str(tmp_path), destination=str(tmp_path / 'notice.log'))
    assert 'Error' not in result
    rows = store.list_reminders()
    assert rows[0]['extra']['notification_destination'] == str(tmp_path / 'notice.log')
    assert rows[0]['extra']['wake_session_id'] == 'target'


def test_output_failure_does_not_try_next_channel(config, monkeypatch, tmp_path):
    config.reminders.default_channels = ['output', 'terminal']
    config.reminders.output_destination = str(tmp_path / 'missing-parent' / 'output')
    monkeypatch.setattr('kindex.notify.TerminalChannel.send', lambda *a: pytest.fail('noisy fallback'))
    assert not dispatch({'title': 'Due'}, config)[0].success


def test_crashed_recurring_wake_advances_without_replaying_uncertain_occurrence(store, config, monkeypatch):
    monkeypatch.setattr(actions, '_run_process', lambda *a, **kw: pytest.fail('duplicate wake'))
    config.reminders.max_action_overdue = 0
    rid = store.add_reminder('Wake', '2000-01-01', reminder_type='recurring', schedule='FREQ=HOURLY', extra={
        'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex',
        'action_status': 'running', 'action_executed_at': '2000-01-01T00:00:00'})
    reminders._fire_one(store, config, store.get_reminder(rid))
    row = store.get_reminder(rid)
    assert row['extra']['action_status'] == 'pending'
    assert row['extra']['last_action_delivery']['status'] == 'submission_unknown'
    assert row['next_due'] != '2000-01-01'
    assert store.due_reminders() == []


def test_missing_agent_fails_without_new_session(store, config, monkeypatch):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: None)
    monkeypatch.setattr(actions, '_run_process', lambda *a, **kw: pytest.fail('missing CLI launched'))
    rid = reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client='claude', wake_session_id='target')
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'exhausted'


def test_unrelated_codex_conflict_never_queues_elsewhere(store, config, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    monkeypatch.setattr(actions, '_run_process', lambda cmd, **kw: (calls.append(cmd) or (
        1, '', 'thread/resume failed: thread unrelated already has an active writer')))
    rid = reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client='codex', wake_session_id='target')
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'failed'
    assert len(calls) == 1 and calls[0][1] == 'exec'


def test_http_error_and_failed_write_remain_quiet(config, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(actions, '_run_process', lambda *a, **kw: (0, '503', 'private'))
    config.reminders.output_destination = 'https://example.test/private'
    result = dispatch({'title': 'Due'}, config)[0]
    assert not result.success and '503' in result.message and 'private' not in result.message
    config.reminders.output_destination = str(tmp_path / 'broken.log')
    monkeypatch.setattr('kindex.notify.os.write', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    assert not dispatch({'title': 'Due'}, config)[0].success
    assert capsys.readouterr() == ('', '')


def test_character_device_and_blocking_pipe(config):
    # An inherited character-device descriptor uses the write path, not the
    # destination-name shortcut for /dev/null.
    device = os.open(os.devnull, os.O_WRONLY)
    reader, writer = os.pipe()
    try:
        os.set_blocking(device, False)
        config.reminders.output_destination = f'fd:{device}'
        assert dispatch({'title': 'Device notice'}, config)[0].success
        config.reminders.output_destination = f'fd:{writer}'
        assert not dispatch({'title': 'Due'}, config)[0].success
    finally:
        os.close(device)
        os.close(reader)
        os.close(writer)


def test_codex_queue_preserves_directory_model_and_prompt(store, config, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1] == 'exec':
            return 1, '', 'thread-store conflict: thread target already has an active writer'
        return 0, 'accepted', ''
    monkeypatch.setattr(actions, '_run_process', run)
    rid = reminders.create_reminder(store, 'Wake', 'in 1 hour', wake_client='codex',
                                    wake_session_id='target', wake_cwd=str(tmp_path), wake_model='model-1')
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'queued'
    queue = calls[1][0]
    assert queue[queue.index('--cd') + 1] == str(tmp_path.resolve())
    assert queue[queue.index('--model') + 1] == 'model-1'
    assert queue[queue.index('--message') + 1] == calls[0][1]['input_text']


def test_codex_queue_shares_the_action_timeout(store, config, monkeypatch):
    times = iter([0, 299])
    monkeypatch.setattr(actions.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    budgets = []
    def run(cmd, **kwargs):
        budgets.append(kwargs['timeout'])
        if cmd[1] == 'exec':
            return 1, '', 'thread-store conflict: thread target already has an active writer'
        return 0, '', ''
    monkeypatch.setattr(actions, '_run_process', run)
    reminder = {'id': 'r1', 'title': 'Wake', 'extra': {'wake_session_id': 'target'}}
    result = actions._run_codex(reminder, actions.get_action_fields(reminder), store, timeout=300)
    assert result['delivery'] == 'queued'
    assert budgets == [300, 1]


def test_antigravity_prompt_precedes_flags(store, monkeypatch, tmp_path):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    calls = []
    monkeypatch.setattr(actions, '_build_agent_prompt', lambda *a: 'Exact wake prompt')
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return (0, '--conversation', '') if '--help' in cmd else (0, 'done', '')
    monkeypatch.setattr(actions, '_run_process', run)
    actions._run_targeted_agent('antigravity', {}, {'wake_session_id': 'target',
        'wake_model': 'model', 'wake_cwd': str(tmp_path)}, store)
    assert calls[-1][0] == ['agy', '-p', 'Exact wake prompt', '--conversation', 'target', '--model', 'model']
    assert calls[-1][1]['cwd'] == str(tmp_path)


def test_persisted_destination_is_exact_or_refused(store, tmp_path):
    with pytest.raises(ValueError, match='redacted in storage'):
        reminders.create_reminder(store, 'Review', 'in 1 hour',
            notification_destination='https://example.test/hook?token=secret-value')
    target = 'https://example.test/hook?source=kindex'
    rid = reminders.create_reminder(store, 'Review', 'in 1 hour', notification_destination=target)
    assert store.get_reminder(rid)['extra']['notification_destination'] == target


def test_blocking_character_descriptor_is_refused(config):
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        config.reminders.output_destination = f'fd:{fd}'
        assert not dispatch({'title': 'Due'}, config)[0].success
        assert os.get_blocking(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize('state', ['queued', 'submission_unknown'])
def test_later_recurrence_runs_once_after_unconfirmed_delivery(store, config, monkeypatch, state):
    import datetime
    now = datetime.datetime.now()
    monkeypatch.setattr(reminders, '_now_dt', lambda: now)
    monkeypatch.setattr('kindex.notify.is_user_idle', lambda config: False)
    config.reminders.max_action_overdue = 0
    calls = []
    def run(*a, **kw):
        calls.append(1)
        return {'ok': state == 'queued', 'delivery': state, 'output': 'Unconfirmed handoff'}
    monkeypatch.setattr(actions, '_run_codex', run)
    rid = store.add_reminder('Hourly wake', (now - datetime.timedelta(minutes=1)).isoformat(), reminder_type='recurring',
        schedule='FREQ=HOURLY', extra={'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex'})
    reminders._fire_one(store, config, store.get_reminder(rid))
    row = store.get_reminder(rid)
    assert row['extra']['last_action_delivery']['status'] == state
    assert len(calls) == 1
    # The due query, used by every scheduler, cannot select that occurrence again.
    assert store.due_reminders() == []
    # At the next scheduled occurrence, the distinct request can run once.
    next_due = row['next_due']
    now = datetime.datetime.fromisoformat(next_due)
    reminders._fire_one(store, config, row)
    assert len(calls) == 2
    assert store.get_reminder(rid)['extra']['last_action_delivery']['occurrence'] == next_due


@pytest.mark.parametrize('state', ['pending', 'queued', 'submission_unknown'])
def test_stale_snapshot_cannot_claim_or_advance_next_occurrence(store, config, monkeypatch, state):
    import datetime
    now = datetime.datetime.now()
    monkeypatch.setattr(reminders, '_now_dt', lambda: now)
    monkeypatch.setattr(actions, '_run_codex', lambda *a, **kw: pytest.fail('stale wake'))
    monkeypatch.setattr('kindex.notify.dispatch', lambda *a, **kw: pytest.fail('stale notification'))
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring', schedule='FREQ=HOURLY', extra={
            'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex', 'action_status': state})
    stale = store.get_reminder(rid)
    reminders.advance_recurring(store, rid)
    before = store.get_reminder(rid)
    assert actions.execute_action(store, stale, config)['status'] == 'skipped'
    assert not reminders._fire_one(store, config, stale)
    assert reminders.advance_recurring(store, rid, expected_occurrence=stale['next_due']) is None
    assert store.get_reminder(rid) == before


def test_late_wake_receipt_does_not_complete_new_occurrence(store, config, monkeypatch):
    import datetime
    now = datetime.datetime.now()
    monkeypatch.setattr(reminders, '_now_dt', lambda: now)
    def run(*a, **kw):
        reminders.advance_recurring(store, rid)  # concurrent deliberate acknowledgment
        return {'ok': True, 'delivery': 'queued', 'output': 'Accepted for old occurrence'}
    monkeypatch.setattr(actions, '_run_codex', run)
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring', schedule='FREQ=HOURLY', extra={
            'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex'})
    old = store.get_reminder(rid)
    reminders._fire_one(store, config, old)
    current = store.get_reminder(rid)
    assert current['next_due'] != old['next_due']
    assert current['status'] == 'active'
    assert current['extra']['action_status'] == 'pending'
    assert current['extra']['last_action_delivery']['occurrence'] == old['next_due']
    assert current['extra']['last_action_delivery']['status'] == 'queued'


def test_running_occurrence_is_not_advanced_by_other_sweep(store, config):
    import datetime
    now = datetime.datetime.now()
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring', schedule='FREQ=HOURLY', extra={
            'wake_client': 'codex', 'action_mode': 'codex', 'action_status': 'running',
            'action_executed_at': now.isoformat()})
    before = store.get_reminder(rid)
    assert not reminders._fire_one(store, config, before)
    assert store.get_reminder(rid)['next_due'] == before['next_due']


def test_late_result_cannot_overwrite_newer_claim_of_same_occurrence(store):
    rid = store.add_reminder('Wake', '2099-01-01', extra={
        'action_status': 'running', 'action_claim_id': 'new-claim', 'action_attempt_occurrence': '2099-01-01'})
    store.update_reminder_action(rid, status='completed', result='Old claim ended', executed_at='2098-01-01',
        expected_occurrence='2099-01-01', expected_claim='old-claim')
    extra = store.get_reminder(rid)['extra']
    assert extra['action_status'] == 'running' and extra['action_claim_id'] == 'new-claim'
    assert extra['last_action_delivery']['claim_id'] == 'old-claim'


def test_concurrent_snooze_blocks_automatic_wake_but_allows_manual(store, config, monkeypatch):
    import datetime
    now = datetime.datetime.now()
    calls = []
    monkeypatch.setattr(actions, '_run_codex', lambda *a, **kw: calls.append(1) or {'ok': True, 'output': 'Done'})
    monkeypatch.setattr('kindex.notify.dispatch', lambda *a, **kw: pytest.fail('snoozed notice'))
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring', schedule='FREQ=HOURLY', extra={
            'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex'})
    stale = store.get_reminder(rid)
    store.snooze_reminder(rid, (now + datetime.timedelta(hours=1)).isoformat())
    assert actions.execute_action(store, stale, config)['reason'] == 'snoozed'
    assert not reminders._fire_one(store, config, stale)
    assert not calls
    assert store.get_reminder(rid)['next_due'] == stale['next_due']
    assert actions.execute_action(store, stale, config, manual=True)['status'] == 'completed'
    assert calls == [1]


def test_snooze_during_notification_does_not_advance_occurrence(store, config, monkeypatch):
    import datetime
    now = datetime.datetime.now()
    monkeypatch.setattr(actions, '_run_codex', lambda *a, **kw: pytest.fail('snoozed wake'))
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring', schedule='FREQ=HOURLY', extra={
            'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex'})
    before = store.get_reminder(rid)
    def notify(*args, **kwargs):
        store.snooze_reminder(rid, (now + datetime.timedelta(hours=1)).isoformat())
        return []
    monkeypatch.setattr('kindex.notify.dispatch', notify)
    assert not reminders._fire_one(store, config, before)
    after = store.get_reminder(rid)
    assert after['next_due'] == before['next_due']
    assert after['status'] == 'snoozed'


@pytest.mark.parametrize('channel', ['system', 'claude', 'terminal'])
def test_explicit_legacy_channel_survives_new_default(tmp_path, monkeypatch, channel):
    from kindex.notify import NotifyResult, get_channel
    cfg = Config.model_validate({'data_dir': str(tmp_path), 'reminders': {'default_channels': [channel]}})
    calls = []
    selected = get_channel(channel)
    monkeypatch.setattr(selected, 'is_available', lambda config: True)
    monkeypatch.setattr(selected, 'send', lambda reminder, config: calls.append(channel) or NotifyResult(True, channel))
    monkeypatch.setattr('kindex.notify.OutputChannel.send', lambda *args: pytest.fail('overrode explicit legacy channel'))
    assert dispatch({'title': 'Synthetic notice'}, cfg)[0].channel == channel
    assert calls == [channel]


@pytest.mark.parametrize('replacement_state', ['running', 'failed'])
@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('delivery', ['completed', 'queued', 'submission_unknown', 'failed'])
def test_superseded_result_cannot_settle_replacement_claim(store, config, monkeypatch, recurring, delivery, replacement_state):
    import datetime
    now = datetime.datetime.now()
    rid = store.add_reminder('Wake', (now - datetime.timedelta(minutes=1)).isoformat(),
        reminder_type='recurring' if recurring else 'once', schedule='FREQ=HOURLY' if recurring else '',
        extra={'wake_client': 'codex', 'wake_session_id': 'target', 'action_mode': 'codex'})
    before = store.get_reminder(rid)
    def run(*args, **kwargs):
        extra = store.get_reminder(rid)['extra']
        extra['action_claim_id'] = 'replacement'
        extra['action_status'] = replacement_state
        store.update_reminder(rid, extra=extra)
        result = {'ok': delivery in ('completed', 'queued'), 'output': 'Old claim result'}
        if delivery in ('queued', 'submission_unknown'):
            result['delivery'] = delivery
        return result
    monkeypatch.setattr(actions, '_run_codex', run)
    assert not reminders._fire_one(store, config, before)
    after = store.get_reminder(rid)
    assert after['status'] == 'active'
    assert after['next_due'] == before['next_due']
    assert after['extra']['action_status'] == replacement_state
    assert after['extra']['action_claim_id'] == 'replacement'
    assert after['extra']['last_action_delivery']['status'] == delivery


def test_null_destination_does_not_require_posix_absolute_paths(config, monkeypatch):
    from pathlib import Path
    from kindex.notify import validate_destination
    monkeypatch.setattr(Path, 'is_absolute', lambda self: False)
    monkeypatch.setattr(os, 'devnull', 'nul')
    for destination in ['/dev/null', 'nul']:
        assert validate_destination(destination) == destination
        config.reminders.output_destination = destination
        assert dispatch({'title': 'Quiet'}, config)[0].success


def test_regular_file_without_posix_nonblocking_flag(config, monkeypatch, tmp_path):
    path = tmp_path / 'notice.log'
    config.reminders.output_destination = str(path)
    monkeypatch.delattr(os, 'O_NONBLOCK', raising=False)
    assert dispatch({'title': 'Portable file'}, config)[0].success
    assert 'Portable file' in path.read_text()


def test_failed_system_command_receipt_preserved_with_legacy_fallback(store, config, monkeypatch):
    import subprocess
    from kindex.notify import NotifyResult, get_channel
    config.reminders.default_channels = ['system']
    monkeypatch.setattr(get_channel('system'), 'is_available', lambda *args: True)
    monkeypatch.setattr('shutil.which', lambda name: None)
    monkeypatch.setattr('kindex.notify.os.path.isfile', lambda path: False)
    monkeypatch.setattr('kindex.notify.subprocess.run',
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b'', b'Failed'))
    fallback_calls = []
    monkeypatch.setattr(get_channel('terminal'), 'send',
        lambda *args: fallback_calls.append(1) or NotifyResult(True, 'terminal', 'Synthetic fallback'))
    rid = store.add_reminder('Synthetic reminder', '2000-01-01')
    assert reminders._fire_one(store, config, store.get_reminder(rid))
    results = store.get_reminder(rid)['extra']['notification_results']
    assert [(result['channel'], result['success']) for result in results] == [('system', False), ('terminal', True)]
    assert fallback_calls == [1]


def test_cursor_falls_back_to_the_agent_executable(config, store, monkeypatch):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name if name == 'agent' else None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return (0, '--resume', '') if '--help' in cmd else (0, 'done', '')

    monkeypatch.setattr(actions, '_run_process', fake_run)
    rid = store.add_reminder('Continue', '2099-03-01T10:00:00', extra={
        'wake_client': 'cursor', 'wake_session_id': 'chat-1', 'action_mode': 'cursor',
        'action_instructions': 'Continue.', 'action_status': 'pending'})
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'completed'
    assert calls[-1][0] == 'agent'


@pytest.mark.parametrize('breaks', ['spawn', 'prompt'])
def test_wake_that_never_launched_can_retry(config, store, monkeypatch, breaks):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    if breaks == 'spawn':
        def no_spawn(*a, **k):
            raise FileNotFoundError(2, 'No such directory')
        monkeypatch.setattr(actions.subprocess, 'Popen', no_spawn)
    else:
        def no_prompt(*a, **k):
            raise RuntimeError('context unavailable')
        monkeypatch.setattr(actions, '_compose_agent_prompt', no_prompt)
    rid = store.add_reminder('Continue', '2099-03-01T10:00:00', extra={
        'wake_client': 'codex', 'wake_session_id': 'thread-1', 'action_mode': 'codex',
        'action_instructions': 'Continue.', 'action_status': 'pending'})
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'failed'


def test_failure_after_launch_is_not_resent(config, store, monkeypatch):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)

    def lost(*a, **k):
        raise RuntimeError('connection to child lost')

    monkeypatch.setattr(actions, '_run_process', lost)
    rid = store.add_reminder('Continue', '2099-03-01T10:00:00', extra={
        'wake_client': 'codex', 'wake_session_id': 'thread-1', 'action_mode': 'codex',
        'action_instructions': 'Continue.', 'action_status': 'pending'})
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == 'submission_unknown'


@pytest.mark.parametrize('wake,expected', [(True, 'submission_unknown'), (False, 'failed')])
def test_claude_timeout_is_held_only_for_a_wake(config, store, monkeypatch, wake, expected):
    monkeypatch.setattr(actions, '_resolve_cli', lambda name: name)
    monkeypatch.setattr(actions, '_run_process', lambda *a, **k: (None, '', ''))
    extra = {'action_mode': 'claude', 'action_instructions': 'Continue.', 'action_status': 'pending'}
    if wake:
        extra.update(wake_client='claude', wake_session_id='session-1')
    rid = store.add_reminder('Continue', '2099-03-01T10:00:00', extra=extra)
    assert actions.execute_action(store, store.get_reminder(rid), config)['status'] == expected


def test_output_destination_is_validated_when_configured():
    from kindex.config import ReminderConfig

    assert ReminderConfig(output_destination='/tmp/notices.log').output_destination == '/tmp/notices.log'
    with pytest.raises(ValueError, match='absolute path'):
        ReminderConfig(output_destination='notices.log')
