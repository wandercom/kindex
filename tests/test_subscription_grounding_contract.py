"""Independent R1 regression: subscription grounding retains local retrieval.

Only HEAD baseline public interfaces were read. Validator executes this test.
"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from kindex.config import Config
from kindex.sim import build_sim_grounding
from kindex.store import Store
from kindex import vectors


def test_r1_subscription_grounding_keeps_local_fts_without_vector_provider_calls(tmp_path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"), sim={"enabled": True, "backend": "codex"})
    assert cfg.sim.grounding_chars == 1500
    vector_search = Mock(return_value=[])
    monkeypatch.setattr(vectors, "is_available", lambda: True)
    monkeypatch.setattr(vectors, "vector_search", vector_search)
    # Keep an optional query translator from invoking any external service while
    # exercising the existing API backend vector-retrieval compatibility path.
    monkeypatch.setitem(sys.modules, "transmogrifier.core", SimpleNamespace(
        Transmogrifier=lambda: SimpleNamespace(translate=lambda query: SimpleNamespace(skipped=True))))
    store = Store(cfg)
    try:
        title = "Subscription grounding retains local concepts"
        store.add_node(title, content="Local FTS evidence survives subscription review routing.",
                       node_type="concept", node_id="subscription-fts-grounding")
        grounded = build_sim_grounding(store, title, cfg)
        assert title in grounded, "R1: suppressing remote retrieval cannot remove useful local knowledge"
        assert vector_search.call_count == 0, "R1: subscription grounding must not invoke a vector provider"
        cfg.sim.backend = "api"
        api_grounded = build_sim_grounding(store, title, cfg)
        assert title in api_grounded
        assert vector_search.call_count >= 1, "R1: the API backend retains existing vector retrieval"
    finally:
        store.close()
