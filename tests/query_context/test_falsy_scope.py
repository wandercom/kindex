"""Malformed persisted client scope must not acquire unrestricted-list semantics.

Independent contract supplement. Candidate source/results were not read by author.
"""
import json

import pytest

AT = '2030-01-02T12:00:00Z'
QUERY = 'auroracable'
SOURCE_MARKER = 'INVENTED_DENIED_SOURCE_LITERAL'
PIN_MARKER = 'INVENTED_OWNER_PIN_LITERAL leave the violet latch open.'
CUE_MARKER = 'INVENTED_PRIVATE_PIN_CUE'
TITLE_MARKER = 'INVENTED_PRIVATE_SOURCE_TITLE'
MALFORMED = ['null', 'false', '0', '{}', '""']


def prepare(api, env, *, handoff=False, source_bytes=4096):
    arguments = dict(query=QUERY, project_path=env.project,
        client_scope=api.TrustedClientScope(mode='client', client='codex'),
        retrieval_max_bytes=source_bytes, top_k=6, evaluation_time=AT)
    if handoff:
        arguments.update(include_handoff=True, evidence_session_id=env.session,
                         handoff_max_bytes=16384)
    return api.prepare_query_context(env.ro(), **arguments)


def pin_ids(component):
    if not component['text']:
        return []
    return [entry['evidence_id'] for entry in json.loads(component['text'])['entries']]


def deny_leaves(result, *forbidden):
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for value in forbidden:
        assert value not in serialized, 'Denied scope exposed an invented identifier or literal'


def persist_domains(env, node_id, raw):
    # Direct synthetic historical mutation; SQL NULL is not used. These are five
    # valid JSON encodings with invalid non-list decoded domain values.
    env.rw.conn.execute('UPDATE nodes SET domains=? WHERE id=?', (raw, node_id))
    env.rw.conn.commit()
    persisted = env.rw.conn.execute('SELECT domains FROM nodes WHERE id=?', (node_id,)).fetchone()[0]
    assert persisted == raw
    assert not isinstance(json.loads(persisted), list) or raw == '[]'


def historical_fixture(env):
    # A safe first query window and an independently captured low-overlap tail.
    # The exact owner-pin literal cannot fit into the baseline's selected windows.
    body = (QUERY + ' ' + SOURCE_MARKER + '\n\n'
            + 'Fictional background without matching terms.\n\n' * 650
            + PIN_MARKER)
    nid = env.source(body, title=TITLE_MARKER)
    eid = env.pin(nid, PIN_MARKER, cue=CUE_MARKER)
    return nid, eid


def test_real_empty_lists_remain_unrestricted_with_source_and_owner_positive(api, env):
    nid, eid = historical_fixture(env)
    persist_domains(env, nid, '[]')
    persist_domains(env, env.session, '[]')
    before = tuple(env.rw.conn.iterdump())
    baseline = prepare(api, env)
    result = prepare(api, env, handoff=True)
    assert SOURCE_MARKER in baseline['retrieval']['text']
    assert PIN_MARKER not in baseline['retrieval']['text']
    assert eid in pin_ids(result['handoff'])
    assert result['retrieval'] == baseline['retrieval']
    assert tuple(env.rw.conn.iterdump()) == before


@pytest.mark.parametrize('raw', MALFORMED, ids=['null', 'false', 'zero', 'object', 'string'])
def test_falsy_nonlist_source_scope_denies_default_retrieval_and_handoff(api, env, raw):
    nid, eid = historical_fixture(env)
    persist_domains(env, nid, raw)
    before = tuple(env.rw.conn.iterdump())
    result = prepare(api, env, handoff=True)
    assert result['retrieval']['text'] == ''
    assert result['retrieval']['included'] == []
    assert result['handoff']['text'] == ''
    assert result['handoff']['included'] == []
    # IDs were not explicitly supplied; a default scan may count denials, not
    # disclose denied source/pin identities or their hidden historical fields.
    deny_leaves(result, nid, eid, SOURCE_MARKER, PIN_MARKER, CUE_MARKER, TITLE_MARKER)
    assert tuple(env.rw.conn.iterdump()) == before


@pytest.mark.parametrize('raw', MALFORMED, ids=['null', 'false', 'zero', 'object', 'string'])
def test_falsy_nonlist_owner_scope_denies_pins_and_preserves_baseline(api, env, raw):
    nid, eid = historical_fixture(env)
    persist_domains(env, env.session, raw)
    before = tuple(env.rw.conn.iterdump())
    baseline = prepare(api, env)
    assert SOURCE_MARKER in baseline['retrieval']['text']
    assert PIN_MARKER not in baseline['retrieval']['text']
    result = prepare(api, env, handoff=True)
    assert result['retrieval'] == baseline['retrieval']
    assert result['status'] == 'partial'
    assert result['handoff']['status'] == 'unavailable'
    assert result['handoff']['text'] == ''
    assert result['handoff']['included'] == []
    # Baseline source identity is independently authorized. Requested session ID
    # may appear in top-level scope; undisclosed owner pins/cues must not appear.
    deny_leaves(result, eid, PIN_MARKER, CUE_MARKER)
    assert tuple(env.rw.conn.iterdump()) == before
