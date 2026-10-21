"""Amendment 03: an owner-only persisted register is corrupt, never fresh."""
import copy

import pytest

from test_evidence_contract import request, retain, only_entry, register_key, db_state
from test_evidence_partial_register_amendment import semantic_field


def test_owner_only_existing_register_cannot_be_reinitialized(api, env, source):
    node = source('Owner-only corruption must not erase the prior capture boundary')
    original_extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    retain(api, env, node, operation='original-owner-only-capture')
    entry = only_entry(api, env)
    key = register_key(env, original_extra)
    extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    register = extra[key]
    assert isinstance(register, dict)
    targets = [semantic_field(register, semantic, entry)
               for semantic in ('version', 'records', 'receipts')]
    assert len(set(targets)) == 3, 'Version and both collection fields must be distinct'
    expected_remainder = {name: value for name, value in register.items()
                          if name not in targets}
    assert expected_remainder, 'Keep the correct owner binding in the persisted register'
    for target in targets:
        del register[target]
    assert register == expected_remainder
    env.store.update_node(env.session, extra=extra)
    assert env.store.peek_node(env.session)['extra']['project_path'] == env.project
    args = request(api, env, node, operation='must-not-reinitialize-owner-only')
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before
    assert env.store.peek_node(env.session)['extra'] == extra
