"""Independent compact-projection fixtures; no candidate imported while authoring."""
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from kindex.config import Config, bound_root
from kindex.sessions import start_tag
from kindex.store import Store


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.endswith('_API_KEY') or name.startswith('KIN_'):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('KIN_NO_SCHEDULER_WRITES', '1')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.chdir(tmp_path)
    def no_network(*args, **kwargs):
        raise AssertionError('Compactness acceptance must not use network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    with bound_root(tmp_path):
        yield


def realistic_path(root, suffix):
    path = root / 'engineering-workspaces' / 'media-processing-services' / suffix
    while len(str(path).encode()) < 170:
        path = path / 'configuration'
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = realistic_path(tmp_path, 'asset-conversion-policy')
    project.mkdir(parents=True)
    data = realistic_path(tmp_path, 'persistent-shared-graph')
    cfg = Config(data_dir=str(data))
    cfg._project_path = project
    store = Store(cfg)
    session = start_tag(store, 'conversion-session', focus='Review image conversion requirements',
                        project_path=str(project))
    value = SimpleNamespace(store=store, cfg=cfg, project=str(project.resolve()),
                            session=session, tag='conversion-session', root=tmp_path)
    monkeypatch.chdir(project)
    yield value
    value.store.close()


@pytest.fixture
def api():
    from kindex import evidence
    return evidence
