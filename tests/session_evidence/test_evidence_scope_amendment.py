"""Amendment 02: preserve native adapter subdirectory session ownership."""
import inspect as python_inspect
import json
from pathlib import Path

import pytest

from test_evidence_adapters import (
    cli_call, mcp_server, json_result, node_argument, retain_arguments,
)
from test_evidence_contract import leaves, request, snapshot, inspect, db_state


def native_cli(capsys, *args):
    from kindex import cli
    parsed = cli.build_parser().parse_args(list(args))
    parsed.func(parsed)
    captured = capsys.readouterr()
    assert not captured.err, captured.err
    assert not captured.out.startswith('Error:'), captured.out
    return captured.out


def assert_subdirectory_history(api, env, ancestor, stored):
    entry = inspect(api, env)[0]
    assert entry['receipt'] == stored['receipt']
    assert entry['current_admission'] == 'eligible'
    assert env.project in leaves(entry['record'])
    assert env.project in leaves(stored['receipt'])
    assert env.store.peek_node(env.session)['extra']['project_path'] == env.project
    before = db_state(env.store)
    with pytest.raises(ValueError):
        inspect(api, env, project_path=ancestor)
    assert db_state(env.store) == before


def test_cli_subdirectory_native_session_and_evidence_share_owner(
        api, env, source, cli_call, monkeypatch, capsys):
    ancestor = env.project
    child = Path(ancestor) / 'component' / 'src'
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    # Keep selected configuration rooted at the ancestor, exactly as repo selection does.
    assert str(env.cfg._project_path) == ancestor
    native_cli(capsys, 'tag', 'start', 'nested-cli', '--focus', 'nested native focus')
    owner = str(child.resolve())
    session = env.store.get_session_tag_by_name('nested-cli', project_path=owner)
    assert session is not None
    assert session['extra']['project_path'] == owner
    env.project, env.session, env.tag = owner, session['id'], 'nested-cli'
    node = source('CLI subdirectory evidence fragment')
    direct = snapshot(api, env, node)
    assert cli_call('evidence', 'snapshot', '--node', node) == direct
    baseline = native_cli(capsys, 'tag', 'resume', env.tag)
    stored = cli_call('evidence', 'retain', '--session-id', env.session, '--node', node,
                      '--expected-source-sha256', direct['source_sha256'],
                      '--expected-metadata-sha256', direct['metadata_sha256'],
                      '--start-byte', '0', '--end-byte', str(direct['source_byte_length']),
                      '--operation-id', 'nested-cli-capture')
    assert stored['receipt']['status'] == 'stored'
    viewed = cli_call('evidence', 'inspect', '--session-id', env.session)
    assert viewed['session_id'] == env.session
    assert len(viewed['entries']) == 1
    assert viewed['entries'][0]['receipt'] == stored['receipt']
    prepared = cli_call('evidence', 'project', '--session-id', env.session)
    assert prepared['included_ids'] == [viewed['entries'][0]['evidence_id']]
    assert 'CLI subdirectory evidence fragment' in leaves(json.loads(prepared['text']))
    assert native_cli(capsys, 'tag', 'resume', env.tag) == baseline
    enabled = native_cli(capsys, 'tag', 'resume', env.tag, '--include-evidence',
                         '--evidence-session-id', env.session, '--evidence-max-bytes', '65536')
    assert enabled.startswith(baseline.rstrip('\n'))
    assert 'CLI subdirectory evidence fragment' in enabled[len(baseline.rstrip('\n')):]
    assert_subdirectory_history(api, env, ancestor, stored)


@pytest.mark.parametrize('project_selector', ['cwd', 'explicit-environment'])
def test_mcp_subdirectory_native_session_and_evidence_share_owner(
        api, env, source, mcp_server, monkeypatch, project_selector):
    ancestor = env.project
    child = Path(ancestor) / 'component' / 'src'
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    monkeypatch.delenv('KIN_PROJECT', raising=False)
    monkeypatch.delenv('KIN_PROJECT_PATH', raising=False)
    if project_selector == 'explicit-environment':
        monkeypatch.setenv('KIN_PROJECT_PATH', str(child))
    assert str(env.cfg._project_path) == ancestor
    server = mcp_server
    started = server.tag_start(name='nested-mcp', focus='nested native focus')
    assert 'Started session tag:' in started
    owner = str(child.resolve())
    session = env.store.get_session_tag_by_name('nested-mcp', project_path=owner)
    assert session is not None
    assert session['extra']['project_path'] == owner
    env.project, env.session, env.tag = owner, session['id'], 'nested-mcp'
    node = source('MCP subdirectory evidence fragment')
    direct = snapshot(api, env, node)
    assert json_result(server.evidence_snapshot(**node_argument(server.evidence_snapshot, node))) == direct
    baseline = server.tag_resume(name=env.tag)
    assert 'nested native focus' in baseline
    args = request(api, env, node, operation='nested-mcp-capture')
    stored = json_result(server.evidence_retain(**retain_arguments(server.evidence_retain, node, args)))
    assert stored['receipt']['status'] == 'stored'
    viewed = json_result(server.evidence_inspect(session_id=env.session))
    assert viewed['session_id'] == env.session
    assert len(viewed['entries']) == 1
    assert viewed['entries'][0]['receipt'] == stored['receipt']
    prepared = json_result(server.evidence_project(session_id=env.session))
    assert prepared['included_ids'] == [viewed['entries'][0]['evidence_id']]
    assert 'MCP subdirectory evidence fragment' in leaves(json.loads(prepared['text']))
    assert server.tag_resume(name=env.tag, include_evidence=False) == baseline
    enabled = server.tag_resume(name=env.tag, include_evidence=True,
                                evidence_session_id=env.session, evidence_max_bytes=65536)
    assert enabled.startswith(baseline)
    assert 'MCP subdirectory evidence fragment' in enabled[len(baseline):]
    assert_subdirectory_history(api, env, ancestor, stored)


def test_resume_public_doc_preserves_utf8_budget_and_custom_counter_contract():
    from kindex.sessions import format_resume_context
    doc = (python_inspect.getdoc(format_resume_context) or '').lower()
    # Existing baseline public documentation promises exact UTF-8 bytes by
    # default and permits callers to supply their provider's exact counter.
    assert 'max_tokens' in doc
    assert 'utf-8' in doc and 'byte' in doc and 'default' in doc
    assert 'counter' in doc and 'provider' in doc
