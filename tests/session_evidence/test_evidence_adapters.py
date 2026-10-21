"""Independent CLI/MCP public round trips; no subprocess/network/model dependency."""
import inspect as python_inspect
import json

import pytest

from test_evidence_contract import leaves, request, snapshot, inspect, project, sha, db_state


def json_result(value):
    if isinstance(value, str):
        return json.loads(value)
    assert isinstance(value, dict), 'Evidence adapters must return JSON object results'
    return value


@pytest.fixture
def cli_call(env, monkeypatch, capsys):
    from kindex import cli
    monkeypatch.chdir(env.project)
    monkeypatch.setattr(cli, '_config', lambda args: env.cfg)
    def call(*args, error=False):
        parsed = cli.build_parser().parse_args(list(args))
        try:
            parsed.func(parsed)
        except SystemExit as exc:
            if not error:
                raise
            assert exc.code not in (0, None)
        except ValueError:
            if not error:
                raise
        captured = capsys.readouterr()
        if error:
            # Either explicit exception or error payload is accepted, never stored success.
            assert '"stored"' not in captured.out
            return captured
        assert not captured.err, captured.err
        return json.loads(captured.out)
    return call


def test_cli_snapshot_retain_inspect_project_roundtrip(api, env, source, cli_call):
    text = 'CLI retained é\r\ncontrol\ttext'
    node = source(text)
    direct = snapshot(api, env, node)
    cli_snap = cli_call('evidence', 'snapshot', '--node', node)
    assert cli_snap == direct
    flags = ['--session-id', env.session, '--node', node,
             '--expected-source-sha256', direct['source_sha256'],
             '--expected-metadata-sha256', direct['metadata_sha256'],
             '--start-byte', '0', '--end-byte', str(direct['source_byte_length']),
             '--operation-id', 'cli-operation', '--cue', 'CLI cue']
    stored = cli_call('evidence', 'retain', *flags)
    assert stored['receipt']['status'] == 'stored'
    historical = cli_call('evidence', 'inspect', '--session-id', env.session)
    assert len(historical['entries']) == 1
    assert historical['entries'][0]['receipt'] == stored['receipt']
    assert text in leaves(historical['entries'][0]['record'])
    assert cli_call('evidence', 'retain', *flags)['receipt'] == stored['receipt']
    prepared = cli_call('evidence', 'project', '--session-id', env.session)
    assert prepared['included_ids'] == [historical['entries'][0]['evidence_id']]
    assert text in leaves(json.loads(prepared['text']))
    assert prepared['receipt']['status'] == 'prepared'
    assert prepared['receipt']['text_sha256'] == sha(prepared['text'])
    assert prepared['receipt']['delivery'] == 'not_observed'
    assert prepared['receipt']['use'] == 'not_observed'


def test_cli_retain_stale_digest_no_success_or_partial_write(api, env, source, cli_call):
    node = source('original data')
    snap = snapshot(api, env, node)
    env.store.update_node(node, content='modified data')
    before = db_state(env.store)
    cli_call('evidence', 'retain', '--session-id', env.session, '--node', node,
             '--expected-source-sha256', snap['source_sha256'],
             '--expected-metadata-sha256', snap['metadata_sha256'],
             '--start-byte', '0', '--end-byte', '4', '--operation-id', 'cli-race', error=True)
    assert db_state(env.store) == before
    assert inspect(api, env) == []


@pytest.fixture
def mcp_server(env, monkeypatch):
    # MCP is a requested acceptance surface. Missing dependency is an environment
    # blocker, not a silent skipped success.
    import kindex.mcp_server as server
    monkeypatch.setattr(server, '_store', env.store)
    monkeypatch.setattr(server, '_config', env.cfg)
    monkeypatch.chdir(env.project)
    monkeypatch.setenv('KIN_PROJECT_PATH', env.project)
    return server


def node_argument(fn, node):
    # Contract names the tools but not this argument spelling; both are native conventions.
    parameters = python_inspect.signature(fn).parameters
    name = 'node_id' if 'node_id' in parameters else 'node'
    assert name in parameters, 'Snapshot must expose an explicit source node selector'
    return {name: node}


def retain_arguments(fn, node, args):
    parameters = python_inspect.signature(fn).parameters
    result = {k: v for k, v in args.items() if k != 'project_path'}
    if 'source_ref' not in parameters:
        result.pop('source_ref')
        result.update(node_argument(fn, node))
    else:
        annotation = parameters['source_ref'].annotation
        if annotation is str or annotation == 'str':
            result['source_ref'] = json.dumps(result['source_ref'])
    return result


def test_mcp_snapshot_retain_inspect_project_roundtrip(api, env, source, mcp_server):
    text = 'MCP retained proof fragment'
    node = source(text)
    server = mcp_server
    snap = json_result(server.evidence_snapshot(**node_argument(server.evidence_snapshot, node)))
    assert snap == snapshot(api, env, node)
    args = request(api, env, node, operation='mcp-operation')
    kwargs = retain_arguments(server.evidence_retain, node, args)
    saved = json_result(server.evidence_retain(**kwargs))
    assert saved['receipt']['status'] == 'stored'
    viewed = json_result(server.evidence_inspect(session_id=env.session))
    assert viewed['session_id'] == env.session
    assert viewed['entries'][0]['receipt'] == saved['receipt']
    assert text in leaves(viewed['entries'][0]['record'])
    before = db_state(env.store)
    assert json_result(server.evidence_retain(**kwargs))['receipt'] == saved['receipt']
    assert db_state(env.store) == before
    prepared = json_result(server.evidence_project(session_id=env.session))
    assert prepared['included_ids'] == [viewed['entries'][0]['evidence_id']]
    assert text in leaves(json.loads(prepared['text']))
    assert prepared['receipt']['status'] == 'prepared'
    assert prepared['receipt']['delivery'] == 'not_observed'
    assert prepared['receipt']['use'] == 'not_observed'


def test_mcp_scope_error_is_explicit_without_history(api, env, source, mcp_server):
    api.retain_evidence(env.store, **request(api, env, source('PRIVATE EVIDENCE CANARY')))
    before = db_state(env.store)
    for fn in (mcp_server.evidence_inspect, mcp_server.evidence_project):
        try:
            result = fn(session_id='not-a-session')
        except ValueError:
            continue
        rendered = json.dumps(result) if isinstance(result, dict) else result
        assert 'PRIVATE EVIDENCE CANARY' not in rendered
        assert 'error' in rendered.lower()
    assert db_state(env.store) == before


def test_mcp_resume_disabled_and_explicit_opt_in(api, env, source, mcp_server):
    node = source('MCP explicit evidence canary')
    baseline = mcp_server.tag_resume(name=env.tag)
    api.retain_evidence(env.store, **request(api, env, node))
    disabled = mcp_server.tag_resume(name=env.tag, include_evidence=False)
    assert disabled == baseline
    enabled = mcp_server.tag_resume(name=env.tag, include_evidence=True,
                                    evidence_session_id=env.session, evidence_max_bytes=65536)
    assert enabled.startswith(baseline)
    assert 'MCP explicit evidence canary' in enabled[len(baseline):]
