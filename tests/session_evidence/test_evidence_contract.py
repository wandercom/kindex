"""Behavioral acceptance for contract daf4bcad9cfc8c06481abead9eb102ed6d435f0e803f75e499233050ebab62f4.

No candidate code or judging results were read while authoring. Inner record key
spellings are intentionally not invented: assertions recurse over captured facts.
Only dedicated public response keys are bound by the contract.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from kindex.config import Config
from kindex.sessions import start_tag, complete_tag, update_tag, add_segment, format_resume_context
from kindex.store import Store

AT = '2026-10-19T12:00:00Z'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def leaves(value):
    if isinstance(value, dict):
        return [leaf for child in value.values() for leaf in leaves(child)]
    if isinstance(value, list):
        return [leaf for child in value for leaf in leaves(child)]
    return [value]


def snapshot(api, env, node):
    return api.source_snapshot(env.store, node, project_path=env.project)


def request(api, env, node, operation='capture-1', **changes):
    snap = snapshot(api, env, node)
    result = dict(session_id=env.session, project_path=env.project,
                  source_ref=snap['source_ref'], expected_source_sha256=snap['source_sha256'],
                  expected_metadata_sha256=snap['metadata_sha256'], start_byte=0,
                  end_byte=snap['source_byte_length'], operation_id=operation)
    result.update(changes)
    return result


def retain(api, env, node, operation='capture-1', **changes):
    return api.retain_evidence(env.store, **request(api, env, node, operation, **changes))


def inspect(api, env, **changes):
    args = dict(session_id=env.session, project_path=env.project, evaluation_time=AT)
    args.update(changes)
    result = api.inspect_evidence(env.store, **args)
    assert result['session_id'] == args['session_id']
    return result['entries']


def project(api, env, **changes):
    args = dict(session_id=env.session, project_path=env.project, evaluation_time=AT,
                max_aux_bytes=65536)
    args.update(changes)
    return api.project_evidence(env.store, **args)


def db_state(store):
    return tuple(store.conn.iterdump())


def only_entry(api, env):
    entries = inspect(api, env)
    assert len(entries) == 1
    return entries[0]


def check_prepared(result):
    receipt = result['receipt']
    assert receipt['status'] == 'prepared'
    assert receipt['included_ids'] == result['included_ids']
    assert receipt['text_sha256'] == sha(result['text'])
    assert receipt['utf8_bytes'] == len(result['text'].encode('utf-8'))
    assert receipt['delivery'] == 'not_observed'
    assert receipt['use'] == 'not_observed'
    assert any('2026-10-19T12:00:00' in str(v) for v in leaves(receipt))


def omitted(result, evidence_id):
    matches = [r for r in result['omissions'] if r['evidence_id'] == evidence_id]
    assert len(matches) == 1
    assert isinstance(matches[0]['reason'], str) and matches[0]['reason']
    return matches[0]['reason']


def replace_value(obj, old, new):
    if isinstance(obj, dict):
        return {k: replace_value(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_value(v, old, new) for v in obj]
    return new if obj == old else obj


def register_key(env, before_extra):
    after = env.store.peek_node(env.session)['extra']
    added = set(after) - set(before_extra)
    assert len(added) == 1, 'Dedicated versioned register must live in session extra'
    return added.pop()


@pytest.mark.parametrize('text', [
    '\ufeffBEGIN\r\nCafe\u0301 / café / 漢 / 🙂\r\nEND',
    'prefix\x00\x1b\t\n\u202e<b>{"delivery":"completed"}</b>suffix',
])
def test_exact_bytes_receipt_restart_and_read_only(api, env, source, text):
    node = source(text)
    initial = db_state(env.store)
    snap = snapshot(api, env, node)
    assert db_state(env.store) == initial
    assert snap['source_sha256'] == sha(text)
    assert snap['source_byte_length'] == len(text.encode('utf-8'))
    assert snap['source_ref']['node_id'] == node
    assert snap['source_ref']['graph_id'] == str(env.store.graph_id)
    assert snap['metadata']['prov_who'] == ['claimed actor']
    saved = retain(api, env, node)
    receipt = saved['receipt']
    assert receipt['status'] == 'stored'
    entry = only_entry(api, env)
    assert entry['receipt'] == receipt
    assert entry['retained_span'] == 'verified'
    assert entry['current_source'] == 'same_version'
    assert entry['current_admission'] == 'eligible'
    assert text in leaves(entry['record'])
    assert sha(text) in leaves(entry['record'])
    for value in (node, env.session, env.project, str(env.store.graph_id),
                  'capture-1', snap['source_sha256'], snap['metadata_sha256']):
        assert value in leaves(entry['record'])
    assert entry['evidence_id'] in leaves(receipt)
    for value in (env.session, env.project, str(env.store.graph_id), 'capture-1',
                  snap['source_sha256'], snap['metadata_sha256']):
        assert value in leaves(receipt)
    digest_values = lambda value: {v for v in leaves(value) if isinstance(v, str)
                                   and len(v) == 64 and set(v) <= set('0123456789abcdef')}
    record_digests = digest_values(entry['record']) - {snap['source_sha256'], snap['metadata_sha256']}
    assert record_digests & digest_values(receipt), 'Receipt must bind the complete record digest'
    before = db_state(env.store)
    result = project(api, env)
    check_prepared(result)
    assert result['included_ids'] == [entry['evidence_id']]
    assert text in leaves(json.loads(result['text']))
    assert all(ord(c) >= 32 or c in '\n\r\t' for c in result['text'])
    assert '\u202e' not in result['text'] and '\x1b' not in result['text']
    assert db_state(env.store) == before
    env.store.close()
    env.store = Store(env.cfg)
    assert only_entry(api, env) == entry
    assert project(api, env) == result


def test_span_byte_offsets_and_exact_boundary(api, env, source):
    node = source('AAé🙂ZZ')
    retain(api, env, node, start_byte=2, end_byte=8)
    entry = only_entry(api, env)
    assert 'é🙂' in leaves(entry['record'])
    assert sha('é🙂') in leaves(entry['record'])
    assert 'é🙂' in leaves(json.loads(project(api, env)['text']))


@pytest.mark.parametrize('start,end', [(True, 2), (0, True), (-1, 2), (2, 2), (3, 2),
                                      (0, 100), (3, 8), (2, 5), ('0', 2), (0, 2.0)])
def test_invalid_spans_are_atomic(api, env, source, start, end):
    node = source('AAé🙂ZZ')
    args = request(api, env, node, start_byte=start, end_byte=end)
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before
    assert inspect(api, env) == []


def test_span_and_cue_byte_ceiling(api, env, source):
    node = source('x' * 2049)
    with pytest.raises(ValueError):
        retain(api, env, node)
    with pytest.raises(ValueError):
        retain(api, env, node, end_byte=2048, cue='é' * 129)
    retain(api, env, node, end_byte=2048, cue='é' * 128)
    assert 'x' * 2048 in leaves(only_entry(api, env)['record'])


@pytest.mark.parametrize('field,value', [
    ('kind', 'instruction'), ('kind', 'evidence_permission'), ('action', 'deploy'),
    ('kind', True), ('cue', '\ud800'), ('operation_id', ''),
])
def test_invalid_capture_requests(api, env, source, field, value):
    args = request(api, env, source())
    args[field] = value
    before = db_state(env.store)
    with pytest.raises((ValueError, UnicodeError)):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before


@pytest.mark.parametrize('action', ['', 'Deploy', 'a b', '-deploy', 'a' * 65, True])
def test_prerequisite_action_strictness(api, env, source, action):
    args = request(api, env, source(), kind='prerequisite_context', action=action)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert inspect(api, env) == []


@pytest.mark.parametrize('mutation', ['body', 'actor'])
def test_stale_snapshot_rejected_inside_transaction(api, env, source, monkeypatch, mutation):
    node = source('ABCD')
    args = request(api, env, node)
    real = env.store.atomic_extra_update
    observed = []

    def racing_update(session_id, callback):
        # Simulate a write after any caller preflight but before BEGIN IMMEDIATE.
        env.store.update_node(node, **({'content': 'WXYZ'} if mutation == 'body'
                                      else {'prov_who': ['another claimed actor']}))
        def checked(extra):
            assert env.store.conn.in_transaction
            observed.append(True)
            return callback(extra)
        return real(session_id, checked)

    monkeypatch.setattr(env.store, 'atomic_extra_update', racing_update)
    before_extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert observed, 'Validation must occur in the atomic callback'
    assert env.store.peek_node(env.session)['extra'] == before_extra


@pytest.mark.parametrize('field', ['expected_source_sha256', 'expected_metadata_sha256'])
def test_required_digests_reject_mismatch(api, env, source, field):
    args = request(api, env, source())
    args[field] = '0' * 64
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before


@pytest.mark.parametrize('change,expected', [('body', 'changed_version'), ('actor', 'metadata_changed'),
                                            ('delete', 'missing'), ('scope', 'scope_mismatch')])
def test_historical_retention_is_not_current_admission(api, env, source, change, expected):
    text = 'OLD SOURCE 1234'
    node = source(text)
    retain(api, env, node)
    original = only_entry(api, env)
    if change == 'body':
        env.store.update_node(node, content='NEW SOURCE 5678')
    elif change == 'actor':
        env.store.update_node(node, prov_who=['replacement claimant'])
    elif change == 'delete':
        env.store.delete_node(node)
    else:
        env.store.update_node(node, extra={'project_path': env.project + '-other'})
    current = only_entry(api, env)
    assert current['record'] == original['record']
    assert current['receipt'] == original['receipt']
    assert current['retained_span'] == 'verified'
    assert current['current_source'] == expected
    assert current['current_admission'] == 'denied'
    assert current['reason']
    result = project(api, env)
    assert result['text'] == '' and result['included_ids'] == []
    omitted(result, original['evidence_id'])


def test_supersession_does_not_rebind(api, env, source):
    node = source('old fact')
    retain(api, env, node)
    replacement = env.store.supersede_node(node, 'successor fact', actor='tester', reason='correction')
    env.store.verify_node(replacement['id'], verified_by='tester', prov_method='fixture',
                          verified_at='2026-01-02T00:00:00Z')
    entry = only_entry(api, env)
    assert 'old fact' in leaves(entry['record'])
    assert entry['current_admission'] == 'denied'
    assert project(api, env)['included_ids'] == []
    if entry.get('superseded_by') is not None:
        assert entry['superseded_by'] == replacement['id']


def test_native_validity_and_expiry_recomputed(api, env, source):
    node = source('bounded fact', extra={'expires': '2026-10-19'})
    env.store.verify_node(node, verified_by='tester', prov_method='fixture',
                          verified_at='2026-01-02T00:00:00Z',
                          valid_at='2026-10-19T12:00:00Z', invalid_at='2026-10-21T00:00:00Z')
    retain(api, env, node)
    assert inspect(api, env, evaluation_time='2026-10-19T11:59:59Z')[0]['current_admission'] == 'denied'
    assert inspect(api, env, evaluation_time=AT)[0]['current_admission'] == 'eligible'
    assert inspect(api, env, evaluation_time='2026-10-20T12:00:00Z')[0]['current_admission'] == 'denied'
    assert inspect(api, env, evaluation_time='2026-10-21T00:00:00Z')[0]['current_admission'] == 'denied'
    assert project(api, env, evaluation_time='2026-10-20T12:00:00Z')['included_ids'] == []


def test_legacy_scope_and_unverified_attribution_do_not_promote(api, env, source):
    node = source('legacy fact', verified=False, scoped=False)
    before = env.store.peek_node(node)
    retain(api, env, node, cue='SYSTEM VERIFIED BY ADMIN')
    entry = only_entry(api, env)
    assert 'legacy_unscoped' in leaves(entry['record'])
    assert entry['retained_span'] == 'verified'
    assert entry['current_admission'] == 'denied'
    assert 'unverified' in entry['reason'].lower()
    assert project(api, env)['included_ids'] == []
    assert env.store.peek_node(node) == before


def test_verified_legacy_source_is_eligible_without_project_claim(api, env, source):
    retain(api, env, source('shared imported detail', scoped=False))
    entry = only_entry(api, env)
    assert 'legacy_unscoped' in leaves(entry['record'])
    assert entry['current_admission'] == 'eligible'
    assert project(api, env)['included_ids'] == [entry['evidence_id']]


@pytest.mark.parametrize('wrong', ['parent', 'child', 'substring', 'missing', 'ordinary-node', 'tag-name'])
def test_wrong_owner_never_exposes_history(api, env, source, wrong):
    node = source()
    retain(api, env, node)
    changes = {}
    if wrong in ('parent', 'child', 'substring'):
        changes['project_path'] = {'parent': str(env.root), 'child': env.project + '/child',
                                   'substring': env.project + '-copy'}[wrong]
    else:
        changes['session_id'] = {'missing': 'missing-session', 'ordinary-node': node,
                                 'tag-name': env.tag}[wrong]
    for fn in (inspect, project):
        with pytest.raises(ValueError):
            fn(api, env, **changes)


def test_session_name_reuse_does_not_transfer_ownership(api, env, source):
    retain(api, env, source())
    old = only_entry(api, env)
    complete_tag(env.store, env.tag, summary='done', project_path=env.project)
    new = start_tag(env.store, env.tag, project_path=env.project)
    assert new != env.session
    assert inspect(api, env, session_id=new) == []
    assert only_entry(api, env)['record'] == old['record']
    with pytest.raises(ValueError):
        format_resume_context(env.store, env.tag, project_path=env.project,
                              include_evidence=True, evidence_session_id=env.session)


def test_foreign_locator_cannot_open_other_database(api, env, source, monkeypatch):
    node = source()
    args = request(api, env, node)
    other_path = str(env.root / 'do-not-create.sqlite3')
    real_connect = sqlite3.connect
    opened = []
    def guarded_connect(path, *a, **kw):
        opened.append(str(path))
        assert other_path not in str(path), 'A source locator must never select a database'
        return real_connect(path, *a, **kw)
    monkeypatch.setattr(sqlite3, 'connect', guarded_connect)
    for key, value in [('db_path', other_path), ('graph_id', '11111111-1111-4111-8111-111111111111')]:
        bad = copy.deepcopy(args)
        bad['source_ref'][key] = value
        with pytest.raises(ValueError):
            api.retain_evidence(env.store, **bad)
    assert not (env.root / 'do-not-create.sqlite3').exists()
    assert inspect(api, env) == []


def test_foreign_scoped_source_rejected(api, env, source):
    node = source(scoped=False, extra={'project_path': env.project + '/nested'})
    with pytest.raises(ValueError):
        retain(api, env, node)
    assert inspect(api, env) == []


def test_replay_original_receipt_after_restart_and_source_change(api, env, source):
    node = source('original')
    args = request(api, env, node)
    first = api.retain_evidence(env.store, **args)
    env.store.update_node(node, content='changed!')
    env.store.close()
    env.store = Store(env.cfg)
    before = db_state(env.store)
    replay = api.retain_evidence(env.store, **args)
    assert replay['receipt'] == first['receipt']
    assert db_state(env.store) == before
    assert len(inspect(api, env)) == 1
    for change in ({'cue': 'conflicting cue'}, {'end_byte': 1}, {'expected_source_sha256': '1' * 64}):
        with pytest.raises(ValueError):
            api.retain_evidence(env.store, **dict(args, **change))
        assert db_state(env.store) == before


def test_capacity_is_bounded_without_eviction_even_when_stale(api, env, source):
    node = source('original')
    receipts = [retain(api, env, node, operation=f'op-{n}')['receipt'] for n in range(12)]
    original = inspect(api, env)
    assert len({e['evidence_id'] for e in original}) == 12
    env.store.update_node(node, content='modified')
    before = db_state(env.store)
    with pytest.raises(ValueError):
        retain(api, env, node, operation='overflow')
    assert db_state(env.store) == before
    assert [e['receipt'] for e in inspect(api, env)] == receipts
    assert [e['record'] for e in inspect(api, env)] == [e['record'] for e in original]


def test_mutation_failure_rolls_back_and_yields_no_receipt(api, env, source, monkeypatch):
    node = source()
    args = request(api, env, node)
    before = db_state(env.store)
    real = env.store.atomic_extra_update
    def fail_inside(session, callback):
        def failing(extra):
            callback(extra)
            raise sqlite3.OperationalError('injected write failure')
        return real(session, failing)
    with monkeypatch.context() as patch:
        patch.setattr(env.store, 'atomic_extra_update', fail_inside)
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before
    assert inspect(api, env) == []
    assert api.retain_evidence(env.store, **args)['receipt']['status'] == 'stored'


def test_sqlite_write_abort_leaves_no_partial_register(api, env, source):
    args = request(api, env, source())
    env.store.conn.execute("CREATE TEMP TRIGGER reject_capture BEFORE UPDATE OF extra ON nodes BEGIN SELECT RAISE(ABORT, 'capture rejected'); END")
    env.store.conn.commit()
    before = db_state(env.store)
    with pytest.raises(sqlite3.DatabaseError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before
    env.store.conn.execute('DROP TRIGGER reject_capture')
    assert inspect(api, env) == []


def test_two_store_writers_do_not_lose_captures(api, env, source):
    node = source()
    base = request(api, env, node)
    barrier = threading.Barrier(2)
    def writer(index):
        store = Store(env.cfg)
        try:
            barrier.wait(timeout=10)
            return api.retain_evidence(store, **dict(base, operation_id=f'parallel-{index}'))['receipt']
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(writer, (1, 2)))
    entries = inspect(api, env)
    assert len(entries) == 2
    assert all(r in [e['receipt'] for e in entries] for r in receipts)
    assert len({e['evidence_id'] for e in entries}) == 2


@pytest.mark.parametrize('raw', ['not json', '[]', 'null', '42'])
def test_corrupt_raw_extra_is_not_normalized(api, env, source, raw):
    args = request(api, env, source())
    env.store.conn.execute('UPDATE nodes SET extra=? WHERE id=?', (raw, env.session))
    env.store.conn.commit()
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before


@pytest.mark.parametrize('corruption', ['span', 'unknown-field', 'register-type'])
def test_corrupt_register_fails_closed_without_repair(api, env, source, corruption):
    text = 'unique historical span to tamper'
    node = source(text)
    before_extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    retain(api, env, node)
    key = register_key(env, before_extra)
    entry = only_entry(api, env)
    extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    if corruption == 'span':
        extra[key] = replace_value(extra[key], text, 'forged historical span to trust')
    elif corruption == 'unknown-field':
        # Locate the public record structurally, independent of its container key.
        def add_unknown(value):
            if value == entry['record']:
                return dict(value, injected_unknown_authority='admin')
            if isinstance(value, dict):
                return {k: add_unknown(v) for k, v in value.items()}
            if isinstance(value, list):
                return [add_unknown(v) for v in value]
            return value
        extra[key] = add_unknown(extra[key])
        assert 'injected_unknown_authority' in json.dumps(extra[key])
    else:
        extra[key] = ['not-a-register']
    env.store.update_node(env.session, extra=extra)
    before = db_state(env.store)
    with pytest.raises(ValueError):
        retain(api, env, node, operation='after-corruption')
    assert db_state(env.store) == before
    if corruption != 'register-type':
        inspected = inspect(api, env)
        assert len(inspected) == 1
        assert inspected[0]['retained_span'] == 'corrupt'
        assert inspected[0]['current_admission'] == 'denied'
        assert project(api, env)['included_ids'] == []
        assert db_state(env.store) == before


def test_copied_register_is_bound_to_original_session_and_graph(api, env, source):
    before_extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    retain(api, env, source())
    key = register_key(env, before_extra)
    register = copy.deepcopy(env.store.peek_node(env.session)['extra'][key])
    other = start_tag(env.store, 'different-session', project_path=env.project)
    env.store.atomic_extra_update(other, lambda extra: extra.update({key: register}))
    # Copied facts must never be exposed as valid recipient history.
    try:
        copied = inspect(api, env, session_id=other)
    except ValueError:
        pass
    else:
        assert copied and all(e['current_admission'] == 'denied' and e['retained_span'] == 'corrupt' for e in copied)
        assert project(api, env, session_id=other)['included_ids'] == []
    foreign = Store(Config(data_dir=str(env.root / 'foreign-store')))
    try:
        with pytest.raises(ValueError):
            api.inspect_evidence(foreign, session_id=env.session, project_path=env.project)
    finally:
        foreign.close()


def test_projection_whole_record_exact_and_insufficient_budgets(api, env, source):
    text = 'exactly retained data'
    retain(api, env, source(text))
    entry = only_entry(api, env)
    full = project(api, env)
    size = len(full['text'].encode('utf-8'))
    assert size > 0
    exact = project(api, env, max_aux_bytes=size)
    assert exact == full
    for budget in (0, 1, size - 1):
        result = project(api, env, max_aux_bytes=budget)
        check_prepared(result)
        assert result['text'] == '' and result['included_ids'] == []
        assert 'budget' in omitted(result, entry['evidence_id']).lower()
    for budget in (True, -1, '4096'):
        with pytest.raises(ValueError):
            project(api, env, max_aux_bytes=budget)


def test_oversized_record_does_not_block_later_fitting_record(api, env, source):
    retain(api, env, source('L' * 2048), operation='large')
    retain(api, env, source('small useful detail'), operation='small')
    entries = inspect(api, env)
    full = project(api, env)
    assert full['included_ids'] == [e['evidence_id'] for e in entries]
    # Scan bounded budgets without guessing envelope schema/overhead.
    fitting = None
    for budget in range(0, len(full['text'].encode('utf-8')) + 1, 128):
        result = project(api, env, max_aux_bytes=budget)
        assert len(result['text'].encode('utf-8')) <= budget
        if result['included_ids'] == [entries[1]['evidence_id']]:
            fitting = result
            break
    assert fitting is not None, 'Later small record must be admitted when first does not fit'
    assert 'small useful detail' in leaves(json.loads(fitting['text']))
    assert 'budget' in omitted(fitting, entries[0]['evidence_id']).lower()


def test_prerequisite_order_action_omission_and_context_warning(api, env, source):
    retain(api, env, source('ordinary evidence'), operation='ordinary')
    retain(api, env, source('Check the rollback plan'), operation='prereq',
           kind='prerequisite_context', action='deploy-prod')
    entries = inspect(api, env)
    mismatch = project(api, env, action='deploy')
    assert mismatch['included_ids'] == [entries[0]['evidence_id']]
    assert 'action' in omitted(mismatch, entries[1]['evidence_id']).lower()
    match = project(api, env, action='deploy-prod')
    assert match['included_ids'] == [entries[1]['evidence_id'], entries[0]['evidence_id']]
    assert match == project(api, env, action='deploy-prod')
    payload = json.loads(match['text'])
    assert 'Check the rollback plan' in leaves(payload)
    warning = ' '.join(v for v in leaves(payload) if isinstance(v, str)).lower()
    for word in ('context', 'data', 'instruction', 'permission'):
        assert word in warning
    assert 'never' in warning or 'not' in warning
    check_prepared(match)


def test_session_native_updates_and_disabled_resume_preserve_evidence(api, env, source):
    baseline = format_resume_context(env.store, env.tag, project_path=env.project, evaluation_time=AT)
    retain(api, env, source())
    entry = only_entry(api, env)
    assert format_resume_context(env.store, env.tag, project_path=env.project, evaluation_time=AT) == baseline
    assert format_resume_context(env.store, env.tag, project_path=env.project, evaluation_time=AT,
                                  include_evidence=False) == baseline
    enabled = format_resume_context(env.store, env.tag, project_path=env.project, evaluation_time=AT,
                                     include_evidence=True, evidence_session_id=env.session,
                                     evidence_max_bytes=65536)
    assert enabled.startswith(baseline)
    assert project(api, env)['text'] in enabled[len(baseline):]
    with pytest.raises(ValueError):
        format_resume_context(env.store, env.tag, project_path=env.project, include_evidence=True)
    zero = format_resume_context(env.store, env.tag, project_path=env.project, evaluation_time=AT,
                                  include_evidence=True, evidence_session_id=env.session, evidence_max_bytes=0)
    assert zero == baseline
    update_tag(env.store, env.tag, focus='new focus', remaining=['follow up'], project_path=env.project)
    add_segment(env.store, env.tag, new_focus='next segment', summary='prior summary', project_path=env.project)
    env.store.close()
    env.store = Store(env.cfg)
    assert only_entry(api, env)['record'] == entry['record']
    assert only_entry(api, env)['receipt'] == entry['receipt']


def test_capture_does_not_promote_sources_or_change_ordinary_retrieval(api, env, source):
    from kindex.retrieve import hybrid_search
    node = source('narwhal search fact', verified=False)
    query = dict(use_vectors=False, expand_graph=False, ranking='rrf', evaluation_time=AT)
    before = hybrid_search(env.store, 'narwhal', **query)
    assert node in [r['id'] for r in before]
    retain(api, env, node)
    after = hybrid_search(env.store, 'narwhal', **query)
    assert [(r['id'], r['confidence'], r['rrf_score']) for r in after] == [
        (r['id'], r['confidence'], r['rrf_score']) for r in before
    ]
    assert env.store.peek_node(node)['verified_at'] is None
    assert project(api, env)['included_ids'] == []


def test_deferred_sqlite_commit_failure_rolls_back_receipt(api, env, source):
    args = request(api, env, source())
    conn = env.store.conn
    conn.executescript('''
        CREATE TABLE acceptance_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE acceptance_child(parent_id INTEGER REFERENCES acceptance_parent(id)
                                      DEFERRABLE INITIALLY DEFERRED);
        CREATE TRIGGER acceptance_commit_failure AFTER UPDATE OF extra ON nodes
        BEGIN INSERT INTO acceptance_child(parent_id) VALUES (987654321); END;
    ''')
    before = db_state(env.store)
    with pytest.raises(sqlite3.IntegrityError):
        api.retain_evidence(env.store, **args)
    assert not conn.in_transaction
    assert db_state(env.store) == before
    conn.execute('DROP TRIGGER acceptance_commit_failure')
    assert inspect(api, env) == []
    assert api.retain_evidence(env.store, **args)['receipt']['status'] == 'stored'


def test_read_operations_do_not_refresh_stale_access_timestamp(api, env, source):
    node = source()
    retain(api, env, node)
    env.store.conn.execute("UPDATE nodes SET last_accessed='2001-01-01T00:00:00' WHERE id IN (?,?)",
                           (node, env.session))
    env.store.conn.commit()
    before = db_state(env.store)
    snapshot(api, env, node)
    inspect(api, env)
    project(api, env)
    assert db_state(env.store) == before


def test_access_timestamp_is_not_metadata_version(api, env, source):
    node = source()
    snap = snapshot(api, env, node)
    retain(api, env, node)
    env.store.conn.execute("UPDATE nodes SET last_accessed='2040-01-01T00:00:00' WHERE id=?", (node,))
    env.store.conn.commit()
    again = snapshot(api, env, node)
    assert again['metadata_sha256'] == snap['metadata_sha256']
    assert only_entry(api, env)['current_admission'] == 'eligible'


@pytest.mark.parametrize('field,value', [
    ('prov_activity', 'changed activity'), ('prov_source', 'fixture://changed'),
    ('prov_who', ['changed actor']), ('status', 'archived'),
    ('extra', {'expires': '2026-10-18'}),
])
def test_metadata_version_binds_supported_mutable_fields(api, env, source, field, value):
    node = source(scoped=False)
    initial = snapshot(api, env, node)
    retain(api, env, node)
    env.store.update_node(node, **{field: value})
    after = snapshot(api, env, node)
    assert after['source_sha256'] == initial['source_sha256']
    assert after['metadata_sha256'] != initial['metadata_sha256']
    entry = only_entry(api, env)
    assert entry['current_source'] == 'metadata_changed'
    assert entry['current_admission'] == 'denied'


def test_verification_actor_edit_invalidates_metadata(api, env, source):
    node = source()
    initial = snapshot(api, env, node)
    retain(api, env, node)
    env.store.verify_node(node, verified_by='new reviewer', prov_method='new method',
                          verified_at='2026-01-03T00:00:00Z')
    assert snapshot(api, env, node)['metadata_sha256'] != initial['metadata_sha256']
    entry = only_entry(api, env)
    assert entry['current_source'] == 'metadata_changed'
    assert entry['current_admission'] == 'denied'


def test_redaction_changing_literal_cue_is_rejected(api, env, source):
    # A synthetic recognisable credential is data used to exercise the native policy.
    from kindex.privacy import redact_text
    cue = 'password=synthetic_fixture_only'
    assert redact_text(cue) != cue
    args = request(api, env, source(), cue=cue)
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before


def test_register_does_not_store_admission_verdict(api, env, source):
    extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    retain(api, env, source())
    key = register_key(env, extra)
    def all_keys(value):
        if isinstance(value, dict):
            return set(value).union(*(all_keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(all_keys(v) for v in value))
        return set()
    keys = all_keys(env.store.peek_node(env.session)['extra'][key])
    assert not {'current_admission', 'current_source', 'eligible', 'admitted'} & keys


def test_native_explicit_contradiction_denies_current_projection(api, env, source):
    node = source('first claim')
    other = source('contradicting claim')
    retain(api, env, node)
    env.store.add_edge(node, other, edge_type='contradicts', provenance='explicit')
    entry = only_entry(api, env)
    assert entry['retained_span'] == 'verified'
    assert entry['current_admission'] == 'denied'
    assert 'contradiction' in entry['reason']
    assert project(api, env)['included_ids'] == []


def test_concurrent_identical_operation_has_one_record_and_receipt(api, env, source):
    args = request(api, env, source(), operation='same-operation')
    barrier = threading.Barrier(2)
    def writer(_):
        store = Store(env.cfg)
        try:
            barrier.wait(timeout=10)
            return api.retain_evidence(store, **args)['receipt']
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(writer, (1, 2)))
    assert receipts[0] == receipts[1]
    assert len(inspect(api, env)) == 1
    assert only_entry(api, env)['receipt'] == receipts[0]


def test_concurrent_capacity_boundary_admits_exactly_one_writer(api, env, source):
    node = source()
    for index in range(11):
        retain(api, env, node, operation=f'existing-{index}')
    args = request(api, env, node)
    barrier = threading.Barrier(2)
    def writer(index):
        store = Store(env.cfg)
        try:
            barrier.wait(timeout=10)
            try:
                return api.retain_evidence(store, **dict(args, operation_id=f'last-{index}'))['receipt']
            except ValueError:
                return None
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(writer, (1, 2)))
    assert sum(r is not None for r in receipts) == 1
    assert len(inspect(api, env)) == 12
    assert next(r for r in receipts if r is not None) in [e['receipt'] for e in inspect(api, env)]
