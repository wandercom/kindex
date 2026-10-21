"""These synthetic contracts never need network or scheduler access."""
import socket

import pytest


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch, tmp_path):
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "VOYAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def reject_network(*args, **kwargs):
        raise AssertionError("Practical memory contract attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
