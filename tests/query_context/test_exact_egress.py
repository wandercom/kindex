"""Independent clarification tests, never executed by the test author.

Synthetic history injection follows the frozen v2 test_mcp_privacy convention.
No current-admission sanitizer is bypassed in product code.
"""
import hashlib
import json

import pytest

AT = '2030-01-02T12:00:00Z'
QUERY = 'auroracable'
INVENTED = 'ghp_' + 'INVENTED_NOT_A_REAL_CREDENTIAL_' * 2
BENIGN = 'auroracable keep the violet archive cover closed.'


def request(api, env, query=QUERY):
    return api.prepare_query_context(env.ro(), query=query, project_path=env.project,
        client_scope=api.TrustedClientScope(mode='client', client='codex'),
        retrieval_max_bytes=16384, top_k=6, evaluation_time=AT)


def assert_unavailable(value):
    assert isinstance(value, dict) and value.get('status') == 'unavailable'
    for key in ('retrieval', 'handoff'):
        component = value.get(key, {})
        assert not component.get('text') and not component.get('included')
    assert INVENTED not in json.dumps(value, ensure_ascii=False)


def test_benign_exact_result_remains_prepared(api, env):
    env.source(BENIGN, title=QUERY)
    before = tuple(env.rw.conn.iterdump())
    result = request(api, env)
    assert result['status'] == 'prepared'
    component = result['retrieval']
    entries = json.loads(component['text'])['entries']
    assert any(entry['text'] == BENIGN for entry in entries)
    assert component['utf8_bytes'] == len(component['text'].encode())
    assert component['text_sha256'] == hashlib.sha256(component['text'].encode()).hexdigest()
    assert result['delivery'] == result['use'] == 'not_observed'
    assert tuple(env.rw.conn.iterdump()) == before


@pytest.mark.parametrize('location', ['query', 'source', 'metadata'])
def test_redaction_sensitive_preparation_refuses_without_rewriting_history(api, env, location):
    nid = env.source(BENIGN, title=QUERY)
    query = QUERY
    if location == 'query':
        query += ' ' + INVENTED
    elif location == 'source':
        literal = QUERY + ' ' + INVENTED
        env.rw.conn.execute('UPDATE nodes SET content=? WHERE id=?', (literal, nid))
        env.rw.conn.commit()
        assert env.rw.conn.execute('SELECT content FROM nodes WHERE id=?', (nid,)).fetchone()[0] == literal
    else:
        # Existing claimed provenance, not a real credential or user history.
        env.rw.conn.execute('UPDATE nodes SET prov_activity=? WHERE id=?', (INVENTED, nid))
        env.rw.conn.commit()
        assert env.rw.conn.execute('SELECT prov_activity FROM nodes WHERE id=?', (nid,)).fetchone()[0] == INVENTED
    before = tuple(env.rw.conn.iterdump())
    try:
        result = request(api, env, query)
    except ValueError as error:
        assert str(error), 'Explicit refusal requires a diagnostic'
        assert INVENTED not in str(error)
    else:
        # Contract permits a structured whole-operation unavailable response.
        # Silent redaction, omitted-source success, and partial preparation do not pass.
        assert_unavailable(result)
    assert tuple(env.rw.conn.iterdump()) == before
