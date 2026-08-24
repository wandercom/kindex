"""Shared test fixtures."""

from pathlib import Path

import pytest

from kindex.config import Config
from kindex.vault import Vault
from kindex.vectors import PROVIDER_DEFAULTS as _EMBED_PROVIDER_DEFAULTS

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_DATA = FIXTURES / "sample-vault"

# Provider API keys that, if inherited from the developer's real environment,
# would make extraction/summarization OR EMBEDDING hit live APIs — turning
# deterministic tests non-deterministic (and spending money). The embedding-
# provider keys are derived from vectors.PROVIDER_DEFAULTS so a new or default
# provider (e.g. Voyage, the default embedder, whose VOYAGE_API_KEY was
# previously missed) is covered automatically instead of silently drifting out.
_PROVIDER_KEY_ENVS = tuple(sorted({
    "ANTHROPIC_API_KEY",  # LLM: extraction / summarization / attention / sim
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    *(d["api_key_env"] for d in _EMBED_PROVIDER_DEFAULTS.values() if d.get("api_key_env")),
}))


@pytest.fixture(autouse=True)
def hermetic_state_dir(tmp_path, monkeypatch):
    """Point XDG_STATE_HOME at a per-test temp dir.

    Pre-merge DB snapshots (kindex.snapshots) default to
    ``$XDG_STATE_HOME/kindex/snapshots``; without this, any test exercising
    dream auto-merges or graph_merge would write the developer's real
    ``~/.local/state``.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def hermetic_provider_env(monkeypatch):
    """Keep the test suite hermetic.

    Strips ambient LLM provider keys so code paths that consult the environment
    (e.g. ``extract()`` -> ``llm_extract`` -> ``_get_client``) fall back to the
    deterministic keyword extractor instead of calling a live API. Tests that
    exercise the LLM path set their own (fake) key and mock the client, which
    runs after this fixture and therefore overrides it.
    """
    for var in _PROVIDER_KEY_ENVS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sample_config():
    """Config pointing at the sample fixture data."""
    return Config(data_dir=str(SAMPLE_DATA))


@pytest.fixture
def sample_vault(sample_config):
    """Loaded vault from fixture data."""
    return Vault(sample_config).load()


@pytest.fixture
def tmp_vault(tmp_path):
    """Empty vault in a temp directory for write tests."""
    cfg = Config(data_dir=str(tmp_path))
    v = Vault(cfg)
    v.ensure_dirs()
    return v.load()
