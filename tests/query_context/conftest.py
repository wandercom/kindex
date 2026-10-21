"""Only invented local fixtures; candidate imports are deferred to Validator runs."""
import os
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest

AT = '2030-01-02T12:00:00Z'

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from kindex.config import bound_root
    for name in tuple(os.environ):
        if name.endswith('_API_KEY') or name.startswith(('KIN_', 'KINDEX_')):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('KIN_NO_SCHEDULER_WRITES', '1')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.chdir(tmp_path)
    def deny(*args, **kwargs):
        raise AssertionError('Independent finite fixtures forbid network calls')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    with bound_root(tmp_path):
        yield

@pytest.fixture
def env(tmp_path, monkeypatch):
    from kindex.config import Config
    from kindex.sessions import start_tag
    from kindex.store import Store
    from kindex import evidence
    project = tmp_path / 'invented-aurora-workspace'
    project.mkdir()
    cfg = Config(data_dir=str(tmp_path / 'graph'), user='invented-local-reviewer',
                 embedding={'provider': 'none'}, llm={'enabled': False},
                 **{name: str(tmp_path / 'absent-history') for name in (
                    'claude_dir', 'codex_dir', 'gemini_dir', 'antigravity_dir',
                    'antigravity_cli_dir', 'opencode_dir', 'cursor_dir', 'muse_dir')})
    cfg._project_path = project
    rw = Store(cfg, manage_project_storage=False)
    session = start_tag(rw, 'invented-continuity', focus='local fixture', project_path=str(project))
    opened = []
    e = SimpleNamespace(rw=rw, cfg=cfg, project=str(project.resolve()), session=session,
                        source_data={}, records={}, root=tmp_path)
    def source(body, *, title='Aurora fixture', tags=(), project_scope=True, verified=True, **kwargs):
        extra = dict(kwargs.pop('extra', {}))
        if project_scope:
            extra.setdefault('project_path', e.project)
        nid = rw.add_node(title + ' ' + uuid4().hex[:8], content=body, node_type='document',
                          tags=list(tags), prov_who=['claimed invented archivist'],
                          prov_activity='fixture transcription', prov_source='fixture://aurora',
                          prov_when='2025-01-01T00:00:00Z', extra=extra,
                          queue_embedding=False, **kwargs)
        if verified:
            rw.verify_node(nid, verified_by='invented reviewer', prov_method='literal comparison',
                           verified_at='2025-01-02T00:00:00Z', valid_at='2025-01-01T00:00:00Z')
        e.source_data[nid] = body
        return nid
    def pin(nid, literal, *, start=None, kind='evidence', action='', cue='invented cue'):
        body = e.source_data[nid].encode()
        start = body.index(literal.encode()) if start is None else start
        snap = evidence.source_snapshot(rw, nid, project_path=e.project)
        saved = evidence.retain_evidence(rw, session_id=session, project_path=e.project,
                 source_ref=snap['source_ref'], expected_source_sha256=snap['source_sha256'],
                 expected_metadata_sha256=snap['metadata_sha256'], start_byte=start,
                 end_byte=start+len(literal.encode()), operation_id=str(uuid4()),
                 cue=cue, kind=kind, action=action)
        record = saved['record']
        e.records[record['evidence_id']] = record
        return record['evidence_id']
    def ro(config=None):
        value = Store(config or cfg, read_only=True, migrate=False, manage_project_storage=False)
        opened.append(value)
        return value
    e.source, e.pin, e.ro = source, pin, ro
    monkeypatch.chdir(project)
    yield e
    for value in opened:
        value.close()
    rw.close()

@pytest.fixture
def api():
    # Module absence is a setup blocker, never claimed as behavioral red.
    from kindex import query_context
    return query_context
