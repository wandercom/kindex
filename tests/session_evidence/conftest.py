"""Independent contract fixtures. Authored without loading candidate implementation."""
import os
import socket
from types import SimpleNamespace

import pytest

from kindex.config import Config, bound_root
from kindex.sessions import start_tag
from kindex.store import Store

AT = '2026-10-19T12:00:00Z'


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.endswith('_API_KEY') or name.startswith('KIN_'):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('KIN_NO_SCHEDULER_WRITES', '1')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.chdir(tmp_path)

    def no_network(*args, **kwargs):
        raise AssertionError('Acceptance tests must not access a network')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    with bound_root(tmp_path):
        yield


@pytest.fixture
def env(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    cfg = Config(data_dir=str(tmp_path / 'store'))
    cfg._project_path = project
    store = Store(cfg)
    session = start_tag(store, 'retained-work', focus='ordinary focus', project_path=str(project))
    result = SimpleNamespace(store=store, cfg=cfg, project=str(project.resolve()),
                             session=session, tag='retained-work', root=tmp_path)
    yield result
    result.store.close()


@pytest.fixture
def api():
    # Deferred until execution by Validator; absent exports are not a behavioral red.
    from kindex import evidence
    return evidence


@pytest.fixture
def source(env):
    def create(text='Pinned quantum detail', *, verified=True, scoped=True, **kwargs):
        extra = dict(kwargs.pop('extra', {}))
        if scoped:
            extra['project_path'] = env.project
        node = env.store.add_node('source ' + str(env.store.conn.total_changes), content=text,
                                  prov_who=['claimed actor'], prov_activity='test-source',
                                  prov_source='fixture://source', prov_when='2026-01-01T00:00:00Z',
                                  extra=extra, queue_embedding=False, **kwargs)
        if verified:
            env.store.verify_node(node, verified_by='claimed reviewer',
                                  prov_method='fixture comparison',
                                  verified_at='2026-01-02T00:00:00Z')
        return node
    return create
