import json
import subprocess
import sys

import pytest

from kindex import mcp_server
from kindex.config import Config
from kindex.store import Store


def test_historical_node_mcp_egress_redacted_without_rewriting_history(tmp_path, monkeypatch):
    store = Store(Config(data_dir=str(tmp_path)))
    node = store.add_node("Historical canary")
    secret = "sk-ant-api03-" + "abcd1234" * 8
    # Existing history deliberately bypasses current admission.
    store.conn.execute("UPDATE nodes SET content=? WHERE id=?", (secret, node))
    store.conn.commit()
    monkeypatch.setattr(mcp_server, "_get_store", lambda: (store, store.config))
    try:
        for call in (lambda: mcp_server.show(node), lambda: mcp_server.resource_node(node),
                     lambda: mcp_server.prime("Historical canary")):
            output = call()
            assert secret not in output
            assert "[REDACTED]" in output
        assert store.conn.execute("SELECT content FROM nodes WHERE id=?", (node,)).fetchone()[0] == secret
    finally:
        store.close()


def test_structured_output_retains_review_digests_and_json_shape():
    digest = "a" * 64
    @mcp_server._safe_output
    def response():
        return json.dumps({"review_token": digest, "content": "password=short-canary"})
    output = json.loads(response())
    assert output["review_token"] == digest
    assert "short-canary" not in output["content"]


def test_exception_projection_never_emits_raw_credential():
    @mcp_server._safe_output
    def broken():
        raise ValueError("password=error-canary")
    with pytest.raises(RuntimeError) as caught:
        broken()
    assert "error-canary" not in str(caught.value)


def test_cli_unknown_argument_does_not_echo_recognized_secret():
    secret = "sk-ant-api03-" + "abcd1234" * 8
    result = subprocess.run([sys.executable, "-m", "kindex.cli", "hook-rpc", "--" + secret],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr
