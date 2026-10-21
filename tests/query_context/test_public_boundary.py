"""Public adapter contracts derived from the frozen installed V4 qualification.

These are ordinary repository tests, not claims of wheel-install isolation.
"""
import asyncio
import json

import pytest

from .constants import AT
from .test_additive_contract import QUERY, EXCEPTION, call


@pytest.fixture
def server(env, monkeypatch):
    from kindex import mcp_server
    monkeypatch.setattr(mcp_server, '_config', env.cfg)
    monkeypatch.setattr(mcp_server, '_store', env.rw)
    monkeypatch.setenv('KIN_PROJECT_PATH', env.project)
    monkeypatch.setenv('KIN_CLIENT', 'codex')
    return mcp_server


def request(**overrides):
    result = dict(query=QUERY, retrieval_max_bytes=4096, top_k=6,
                  evaluation_time=AT, include_handoff=False,
                  evidence_session_id=None, handoff_selection='query_candidates',
                  evidence_ids=None, handoff_max_bytes=0, action='')
    result.update(overrides)
    return result


def tool_value(server, arguments):
    value = asyncio.run(server.mcp.call_tool('query_context', arguments))
    if isinstance(value, tuple):
        value = value[0]
    if hasattr(value, 'content'):
        value = value.content
    text = '\n'.join(part.text for part in value if hasattr(part, 'text'))
    return json.loads(text)


def test_registered_mcp_schema_is_typed_and_has_no_scope_override(server):
    tools = asyncio.run(server.mcp.list_tools())
    matched = [tool for tool in tools if tool.name == 'query_context']
    assert len(matched) == 1
    props = matched[0].inputSchema['properties']
    assert set(request()) <= props.keys()
    assert not {'client', 'client_scope', 'project', 'project_path', 'db_path',
                'data_dir', 'source_ref', 'candidates', 'ranked_candidates'} & props.keys()
    assert props['query']['type'] == 'string'
    assert props['include_handoff']['type'] == 'boolean'
    for key in ('retrieval_max_bytes', 'top_k', 'handoff_max_bytes'):
        assert props[key]['type'] == 'integer'


def test_public_mcp_baseline_handoff_and_read_only_match_library(api, env, server):
    body = QUERY + '\n\n' + 'Unrelated background.\n\n' * 650 + EXCEPTION
    nid = env.source(body, title=QUERY)
    eid = env.pin(nid, EXCEPTION)
    before = tuple(env.rw.conn.iterdump())
    baseline = tool_value(server, request())
    assert baseline == call(api, env)
    assert baseline['handoff']['status'] == 'disabled'
    enriched = tool_value(server, request(include_handoff=True,
                         evidence_session_id=env.session, handoff_max_bytes=16384))
    assert enriched['retrieval'] == baseline['retrieval']
    assert eid in [item['evidence_id'] for item in json.loads(enriched['handoff']['text'])['entries']]
    assert enriched['delivery'] == enriched['use'] == 'not_observed'
    assert tuple(env.rw.conn.iterdump()) == before


@pytest.mark.parametrize('tags', [['client:claude'], ['client:codex']])
def test_public_mcp_uses_host_client_and_keeps_denied_sources_hidden(env, server, tags):
    marker = 'INVENTED_TRANSPORT_SOURCE'
    nid = env.source(QUERY + ' ' + marker, title=QUERY, tags=tags)
    before = tuple(env.rw.conn.iterdump())
    value = tool_value(server, request())
    serialized = json.dumps(value)
    if tags == ['client:claude']:
        assert marker not in serialized and nid not in serialized
    else:
        assert marker in value['retrieval']['text']
    assert tuple(env.rw.conn.iterdump()) == before


def test_public_mcp_redaction_sensitive_query_fails_explicitly(env, server):
    from mcp.server.fastmcp.exceptions import ToolError
    invented = 'ghp_' + 'INVENTED_NOT_A_REAL_CREDENTIAL_' * 2
    env.source(QUERY + ' benign', title=QUERY)
    before = tuple(env.rw.conn.iterdump())
    try:
        value = tool_value(server, request(query=QUERY + ' ' + invented))
    except ToolError as error:
        assert str(error) and invented not in str(error)
    else:
        assert invented not in json.dumps(value)
        assert value.get('status') == 'unavailable' or value.get('error')
        assert value.get('status') != 'prepared'
        assert not value.get('retrieval', {}).get('text')
        assert not value.get('handoff', {}).get('text')
    assert tuple(env.rw.conn.iterdump()) == before


def test_cli_and_library_return_identical_baseline(api, env, monkeypatch, capsys):
    from kindex import cli
    env.source(QUERY + ' exact CLI evidence', title=QUERY)
    monkeypatch.setattr(cli, '_config', lambda args: env.cfg)
    monkeypatch.setenv('KIN_CLIENT', 'codex')
    before = tuple(env.rw.conn.iterdump())
    args = cli.build_parser().parse_args(['context-query', QUERY, '--source-bytes',
            '4096', '--top-k', '6', '--at', AT])
    args.func(args)
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out) == call(api, env)
    assert tuple(env.rw.conn.iterdump()) == before
