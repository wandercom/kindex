"""Durable locators may not assert a foreign project or profile for local data."""
import copy
from uuid import uuid4

import pytest


@pytest.mark.parametrize('field,value', [('project_path', '/invented/foreign-project'),
                                        ('profile', 'invented-foreign-profile')])
def test_retention_cannot_persist_false_project_or_profile(env, field, value):
    from kindex import evidence
    body = 'Literal evidence from the selected graph.'
    nid = env.source(body)
    snapshot = evidence.source_snapshot(env.rw, nid, project_path=env.project)
    reference = copy.deepcopy(snapshot['source_ref'])
    assert field in reference, 'The public locator must declare the tested binding'
    reference[field] = value
    before = tuple(env.rw.conn.iterdump())
    try:
        saved = evidence.retain_evidence(env.rw, session_id=env.session,
            project_path=env.project, source_ref=reference,
            expected_source_sha256=snapshot['source_sha256'],
            expected_metadata_sha256=snapshot['metadata_sha256'], start_byte=0,
            end_byte=len(body.encode()), operation_id=str(uuid4()), cue='locator proof')
    except ValueError:
        assert tuple(env.rw.conn.iterdump()) == before
    else:
        # Canonicalization is also valid, provided the durable record tells the truth.
        assert saved['record']['source_ref'][field] == snapshot['source_ref'][field]
        inspected = evidence.inspect_evidence(env.rw, session_id=env.session,
                                             project_path=env.project)
        assert value not in str(inspected)
