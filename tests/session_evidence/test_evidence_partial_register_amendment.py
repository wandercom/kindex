"""Amendment 02: existing partial registers fail closed without normalization."""
import copy

import pytest

from test_evidence_contract import request, retain, only_entry, register_key, db_state


def contains_exact(value, wanted):
    if value == wanted:
        return True
    if isinstance(value, dict):
        return any(contains_exact(v, wanted) for v in value.values())
    if isinstance(value, list):
        return any(contains_exact(v, wanted) for v in value)
    return False


def semantic_field(register, semantic, entry):
    # Resolve only from the persisted public capture facts, not candidate code.
    if semantic in register:
        return semantic
    if semantic == 'version':
        candidates = [key for key, value in register.items()
                      if 'version' in key.lower() and isinstance(value, int)
                      and not isinstance(value, bool)]
    else:
        wanted = entry['record' if semantic == 'records' else 'receipt']
        candidates = [key for key, value in register.items()
                      if contains_exact(value, wanted)]
    assert len(candidates) == 1, f'Cannot identify required {semantic} register field unambiguously'
    return candidates[0]


@pytest.mark.parametrize('missing_field', ['version', 'records', 'receipts'])
def test_partial_existing_register_cannot_be_reinitialized(
        api, env, source, missing_field):
    node = source('Retain this original history despite malformed register')
    original_extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    retain(api, env, node, operation='original-capture')
    entry = only_entry(api, env)
    key = register_key(env, original_extra)
    extra = copy.deepcopy(env.store.peek_node(env.session)['extra'])
    register = extra[key]
    assert isinstance(register, dict)
    target = semantic_field(register, missing_field, entry)
    # Remove just one mandatory field; owner and all unrelated data stay intact.
    del register[target]
    env.store.update_node(env.session, extra=extra)
    assert env.store.peek_node(env.session)['extra']['project_path'] == env.project
    args = request(api, env, node, operation='must-not-reinitialize')
    before = db_state(env.store)
    with pytest.raises(ValueError):
        api.retain_evidence(env.store, **args)
    assert db_state(env.store) == before
    assert env.store.peek_node(env.session)['extra'] == extra
