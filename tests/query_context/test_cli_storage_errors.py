"""Evidence CLI storage failures are explicit, sanitized, and machine readable.

The injected failures represent selected-store operations; no live database or
private filesystem location is read. Validator runs behavioral red before repair.
"""
import json
import sqlite3

import pytest


@pytest.mark.parametrize('operation,public_function,error_type,arguments', [
    ('snapshot', 'source_snapshot', sqlite3.OperationalError,
     ['--node', 'invented-node']),
    ('inspect', 'inspect_evidence', sqlite3.DatabaseError,
     ['--session-id', 'invented-session']),
    ('project', 'project_evidence', sqlite3.IntegrityError,
     ['--session-id', 'invented-session']),
    ('retain', 'retain_evidence', sqlite3.OperationalError,
     ['--session-id', 'invented-session', '--node', 'invented-node',
      '--expected-source-sha256', 'a' * 64, '--expected-metadata-sha256', 'b' * 64,
      '--start-byte', '0', '--end-byte', '1', '--operation-id', 'invented-op']),
])
def test_evidence_storage_failure_is_safe_json_exit_two(
        env, monkeypatch, capsys, operation, public_function, error_type, arguments):
    from kindex import cli, evidence

    marker = 'INVENTED_PRIVATE_STORAGE_DETAIL_9f0d'
    error_text = (
        'database is locked: /invented/private/' + marker + '/graph.db; '
        'UPDATE nodes SET content=secret WHERE id=hidden'
    )
    entered = []

    def failed_operation(*args, **kwargs):
        entered.append(public_function)
        raise error_type(error_text)

    # The command selects an isolated fixture graph. Failure is injected only at
    # its public operation boundary, so no implementation helper or SQL layout
    # is prescribed by the oracle.
    monkeypatch.setattr(cli, '_config', lambda args: env.cfg)
    monkeypatch.setenv('KIN_PROJECT_PATH', env.project)
    monkeypatch.setattr(evidence, public_function, failed_operation)
    if operation == 'retain':
        # Retain's CLI derives a native locator via the public snapshot API first.
        # A plain synthetic locator is enough: the retain operation is where the
        # simulated SQLite failure must occur.
        monkeypatch.setattr(evidence, 'source_snapshot', lambda *args, **kwargs: {
            'source_ref': {'graph_id': 'invented-graph', 'node_id': 'invented-node',
                           'db_path': '/invented/fixture/graph.db', 'profile': '',
                           'project_path': env.project},
        })
    parsed = cli.build_parser().parse_args(['evidence', operation, *arguments, '--json'])
    with pytest.raises(SystemExit) as failure:
        parsed.func(parsed)
    assert failure.value.code == 2
    assert entered == [public_function], 'The SQLite failure must reach the selected public operation'
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert '/invented/private/' not in captured.out + captured.err
    assert 'UPDATE nodes' not in captured.out + captured.err
    assert 'Traceback' not in captured.out + captured.err
    channels = [channel for channel in (captured.out, captured.err) if channel]
    assert len(channels) == 1, 'Emit one JSON error on exactly one output channel'
    payload = json.loads(channels[0])
    assert isinstance(payload, dict)
    assert isinstance(payload.get('error'), str) and payload['error'].strip()
    assert payload.get('status') not in {'prepared', 'stored'}
