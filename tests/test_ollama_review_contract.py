"""Independent acceptance contract for the fully-offline Ollama supervisor.

The Validator owns execution.  These tests use only public Kindex interfaces and
a loopback HTTP fixture; they never invoke Ollama, an installed agent, or a cloud
model.  The oracle is the ratified O1--O5 contract dated 2026-09-13.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from kindex.agent_settings import apply_agent_overrides, agent_settings_summary
from kindex.budget import BudgetLedger
from kindex.config import Config, SimConfig
from kindex import ollama_review, sim, subscription_review, supervisor, vectors
from kindex.store import Store


WORK = "Review this bounded change without disclosing OFFLINE_PROMPT_CANARY."
NOTE = "Preserve the durable attempt before changing the queue schema."
GOOD = json.dumps({"rating": 0.91, "note": NOTE, "basis": "local evidence"})
MODEL = "local-review"
LATEST = MODEL + ":latest"


def _local_model(name=LATEST, **changes):
    value = {
        "name": name,
        "model": name,
        "size": 4_096,
        "digest": "a" * 64,
        "details": {"format": "gguf"},
    }
    value.update(changes)
    return value


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, scenario):
        super().__init__(("127.0.0.1", 0), _OllamaHandler)
        self.scenario = scenario
        self.requests = []
        self.base_url = "http://127.0.0.1:" + str(self.server_address[1])


class _OllamaHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    def log_message(self, *_args):
        return

    def _record(self, body=b""):
        self.server.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body.decode("utf-8", errors="replace"),
        })

    def _send(self, body, *, status=200, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_chunked(self, body):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        for index in range(0, len(body), 37):
            chunk = body[index:index + 37]
            self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _send_unknown_length(self, body):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self._record()
        scenario = self.server.scenario
        if scenario.get("redirect_tags") and self.path == "/api/tags":
            self.send_response(302)
            self.send_header("Location", self.server.base_url + "/redirected-tags")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = scenario.get("tags", {"models": [_local_model()]})
        transfer = scenario.get("tags_transfer")
        if transfer == "chunked":
            self._send_chunked(payload)
        elif transfer == "oversized_chunked":
            self._send_chunked(b"x" * (1024 * 1024 + 1))
        elif transfer == "oversized_unknown":
            self._send_unknown_length(b"x" * (1024 * 1024 + 1))
        else:
            self._send(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        mode = self.server.scenario.get("chat_mode", "success")
        if mode == "http_error":
            self._send({"error": "synthetic local failure " + WORK + " RAW_CREDENTIAL_CANARY"}, status=503)
        elif mode == "malformed":
            self._send(b'{"done":')
        elif mode == "nonterminal":
            self._send({"done": False, "message": {"role": "assistant", "content": GOOD}})
        elif mode == "oversized":
            self._send(b"x" * (1024 * 1024 + 1), content_type="application/octet-stream")
        elif mode == "oversized_unknown":
            self._send_unknown_length(b"x" * (1024 * 1024 + 1))
        elif mode == "trickle":
            self._send_trickled_response()
        else:
            payload = {
                "model": LATEST,
                "done": True,
                "message": {"role": "assistant", "content": self.server.scenario.get(
                    "advisory_content", GOOD)},
                "prompt_eval_count": 17,
                "eval_count": 9,
            }
            if self.server.scenario.get("terminal_error") is not None:
                payload["error"] = self.server.scenario["terminal_error"]
            if self.server.scenario.get("tool_calls"):
                payload["message"]["tool_calls"] = self.server.scenario["tool_calls"]
            if mode == "chunked":
                self._send_chunked(payload)
            else:
                self._send(payload)

    def _send_trickled_response(self):
        """Keep every socket interval below one second but exceed total timeout."""
        body = json.dumps({
            "model": LATEST,
            "done": True,
            "message": {"role": "assistant", "content": GOOD},
            "prompt_eval_count": 17,
            "eval_count": 9,
        }).encode()
        parts = [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: application/json\r\n",
            ("Content-Length: %d\r\n" % len(body)).encode(),
            b"Connection: close\r\n",
            b"\r\n",
        ]
        width = max(1, len(body) // 8)
        parts.extend(body[index:index + width] for index in range(0, len(body), width))
        try:
            for part in parts:
                self.connection.sendall(part)
                time.sleep(0.18)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True


@pytest.fixture
def loopback_servers():
    running = []

    def start(**scenario):
        server = _LoopbackServer(scenario)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in reversed(running):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    for key in list(os.environ):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home" / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "home" / ".cache"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.setattr("kindex.llm.get_client", Mock(return_value=None))
    monkeypatch.setattr(supervisor, "record_health", lambda *a, **kw: None)
    monkeypatch.setattr(sim, "spawn_background_drain", lambda *a, **kw: False)


def _config(root, url, **changes):
    controls = {
        "enabled": True,
        "backend": "ollama",
        "ollama_url": url,
        "ollama_model": MODEL,
        "tick_interval": 1,
        "triage_banter": False,
        "drain_on_tick": False,
        "grounding_chars": 0,
        "agent_timeout": 1,
        "max_output_tokens": 73,
        "max_conversation_reviews": 5,
        "max_daily_reviews": 10,
    }
    controls.update(changes)
    return Config(data_dir=str(root), sim=controls)


def _requests(server, method=None):
    if method is None:
        return list(server.requests)
    return [request for request in server.requests if request["method"] == method]


def _assert_failed(result):
    assert isinstance(result, dict)
    assert result.get("status") != "ok", result
    rendered = json.dumps(result, default=str)
    assert WORK not in rendered
    assert "OFFLINE_PROMPT_CANARY" not in rendered
    assert "RAW_CREDENTIAL_CANARY" not in rendered


def _allowance(cfg, conversation):
    value = subscription_review.allowance_status(cfg, conversation)
    assert {"conversation", "day"} <= value.keys()
    return value


def test_o1_config_defaults_status_overrides_and_snapshots_expose_ollama(tmp_path):
    defaults = SimConfig().model_dump()
    assert defaults["backend"] == "api"
    assert defaults["ollama_url"] == "http://127.0.0.1:11434"
    assert defaults["ollama_model"] == ""
    for backend in ("api", "antigravity", "codex", "claude", "ollama"):
        assert SimConfig(backend=backend).backend == backend

    cfg = Config(data_dir=str(tmp_path / "data"), agents={
        "clients": {"codex": {"sim": {"enabled": False}}},
        "instances": {"codex:alpha": {"client": "codex", "sim": {
            "enabled": True, "backend": "ollama", "ollama_url": "http://[::1]:11434",
            "ollama_model": "review-a", "agent_timeout": 7,
            "max_output_tokens": 321, "max_conversation_reviews": 4,
            "max_daily_reviews": 8,
        }}},
    })
    admitted = apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
    assert admitted.sim.ollama_url == "http://[::1]:11434"
    assert admitted.sim.ollama_model == "review-a"
    assert apply_agent_overrides(cfg, client="codex", instance_key="codex:beta").sim.enabled is False
    report = agent_settings_summary(cfg, client="codex", instance_key="codex:alpha")
    assert {"sim.ollama_url", "sim.ollama_model"} <= set(report["allowed_keys"])
    assert report["effective"]["sim"]["ollama_model"] == "review-a"
    snapshot = supervisor.config_snapshot(admitted)
    assert snapshot["sim"]["ollama_url"] == "http://[::1]:11434"
    assert snapshot["sim"]["ollama_model"] == "review-a"
    store = Store(admitted)
    try:
        status = sim.sim_status(store, admitted)
        rendered = json.dumps(status).lower()
        assert "ollama" in rendered and "review-a" in rendered
    finally:
        store.close()


@pytest.mark.parametrize("url_suffix", [
    "/prefix", "?query=1", "#fragment", "/api", "/../api",
])
def test_o2_rejects_path_query_and_fragment_before_any_http(tmp_path, loopback_servers, url_suffix):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url + url_suffix)
    _assert_failed(ollama_review.run_review(cfg, "bad-endpoint", WORK))
    assert server.requests == []


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:11434",
    "http://user:password@127.0.0.1:11434",
    "http://192.0.2.1:11434",
    "http://2130706433:11434",
    "http://[::ffff:127.0.0.1]:11434",
])
def test_o2_rejects_nonliteral_nonhttp_or_credentialed_endpoints_without_dispatch(tmp_path, url):
    started = time.monotonic()
    try:
        cfg = _config(tmp_path / "data", url)
    except (TypeError, ValueError):
        return
    state = ollama_review.preflight(cfg, "bad-endpoint")
    assert state is not None
    _assert_failed(ollama_review.run_review(cfg, "bad-endpoint", WORK))
    assert time.monotonic() - started < 0.75, "Invalid endpoints must be denied before network I/O"


def test_o2_redirect_is_not_followed_and_prompt_is_never_disclosed(tmp_path, loopback_servers):
    server = loopback_servers(redirect_tags=True)
    result = ollama_review.run_review(_config(tmp_path / "data", server.base_url), "redirect", WORK)
    _assert_failed(result)
    assert [(item["method"], item["path"]) for item in server.requests] == [("GET", "/api/tags")]
    assert WORK not in json.dumps(server.requests)


def test_o2_empty_model_is_unavailable_without_http(tmp_path, loopback_servers):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url, ollama_model="")
    assert ollama_review.preflight(cfg, "model-required") is not None
    _assert_failed(ollama_review.run_review(cfg, "model-required", WORK))
    assert server.requests == []


@pytest.mark.parametrize("max_output_tokens", [-1, 0])
def test_o4_ollama_requires_a_positive_generation_bound_before_http(
        tmp_path, loopback_servers, max_output_tokens):
    server = loopback_servers()
    cfg = _config(
        tmp_path / "data", server.base_url, max_output_tokens=max_output_tokens)
    assert ollama_review.preflight(cfg, "bounded-output") is not None
    _assert_failed(ollama_review.run_review(cfg, "bounded-output", WORK))
    assert server.requests == []


def test_o2_localhost_is_canonicalized_to_loopback_and_latest_suffix_may_be_omitted(
        tmp_path, loopback_servers):
    server = loopback_servers()
    localhost_url = server.base_url.replace("127.0.0.1", "localhost")
    result = ollama_review.run_review(
        _config(tmp_path / "data", localhost_url, ollama_model=MODEL),
        "localhost", WORK)
    assert result.get("status") == "ok", result
    assert [request["path"] for request in server.requests] == ["/api/tags", "/api/chat"]


@pytest.mark.parametrize("models", [
    [_local_model("local-review:cloud")],
    [_local_model(remote_model="vendor/cloud-model")],
    [_local_model(remote_host="models.example.invalid")],
    [_local_model(details={})],
    [_local_model(size=0)],
    [_local_model(digest="")],
    [{"name": LATEST, "model": LATEST, "size": 4096, "digest": "a" * 64}],
    [_local_model("different:latest")],
])
def test_o2_cloud_remote_missing_or_wrong_model_metadata_never_reaches_chat(
        tmp_path, loopback_servers, models):
    server = loopback_servers(tags={"models": models})
    result = ollama_review.run_review(_config(tmp_path / "data", server.base_url), "metadata", WORK)
    _assert_failed(result)
    assert [item["path"] for item in server.requests] == ["/api/tags"]
    assert WORK not in json.dumps(server.requests)


@pytest.mark.parametrize("tags", [None, [], {}, {"models": None}, {"models": "wrong"}])
def test_o2_absent_or_malformed_tags_fail_closed(tmp_path, loopback_servers, tags):
    scenario = {"tags": tags} if tags is not None else {"tags": {}}
    server = loopback_servers(**scenario)
    result = ollama_review.run_review(_config(tmp_path / "data", server.base_url), "bad-tags", WORK)
    _assert_failed(result)
    assert _requests(server, "POST") == []


def test_o4_success_uses_verified_local_model_private_json_request_and_raw_counts(
        tmp_path, monkeypatch, loopback_servers):
    proxy = loopback_servers(tags={"models": []})
    server = loopback_servers()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, proxy.base_url)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("OPENAI_API_KEY", "AMBIENT_SECRET_CANARY")
    monkeypatch.setenv("OLLAMA_API_KEY", "AMBIENT_SECRET_CANARY")
    cfg = _config(tmp_path / "data", server.base_url)
    result = ollama_review.run_review(cfg, "success", WORK)
    assert result == {
        "status": "ok", "response": GOOD,
        "usage": {"prompt_eval_count": 17, "eval_count": 9}, "via": "ollama",
    }
    assert proxy.requests == [], "Loopback Ollama must bypass inherited HTTP proxies"
    assert [item["path"] for item in server.requests] == ["/api/tags", "/api/chat"]
    chat = _requests(server, "POST")[0]
    payload = json.loads(chat["body"])
    assert payload["model"] in {MODEL, LATEST}
    assert payload["messages"][-1]["content"] == WORK
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 73
    assert not {"authorization", "proxy-authorization", "cookie"}.intersection(chat["headers"])
    assert "AMBIENT_SECRET_CANARY" not in json.dumps(server.requests)


def test_o4_standard_chunked_tags_and_chat_are_supported(tmp_path, loopback_servers):
    server = loopback_servers(tags_transfer="chunked", chat_mode="chunked")
    result = ollama_review.run_review(_config(tmp_path / "data", server.base_url), "chunked", WORK)
    assert result.get("status") == "ok", result
    assert result["response"] == GOOD
    assert result["usage"]["prompt_eval_count"] == 17
    assert result["usage"]["eval_count"] == 9
    assert [request["path"] for request in server.requests] == ["/api/tags", "/api/chat"]


@pytest.mark.parametrize("location,scenario", [
    ("tags", {"tags_transfer": "oversized_chunked"}),
    ("tags", {"tags_transfer": "oversized_unknown"}),
    ("chat", {"chat_mode": "oversized_unknown"}),
])
def test_o4_chunked_and_unknown_length_responses_remain_bounded(
        tmp_path, loopback_servers, location, scenario):
    server = loopback_servers(**scenario)
    cfg = _config(tmp_path / "data", server.base_url)
    result = ollama_review.run_review(cfg, "bounded-" + location, WORK)
    _assert_failed(result)
    assert len(_requests(server, "POST")) == (1 if location == "chat" else 0)


def test_o4_server_tool_calls_are_inert_data(tmp_path, loopback_servers):
    canary = tmp_path / "server-directed-action"
    server = loopback_servers(tool_calls=[{
        "function": {"name": "write_file", "arguments": {"path": str(canary), "text": "bad"}},
    }])
    result = ollama_review.run_review(_config(tmp_path / "data", server.base_url), "inert-tools", WORK)
    assert result.get("status") == "ok", result
    assert result["response"] == GOOD
    assert "tool_calls" not in json.dumps(result)
    assert not canary.exists()


@pytest.mark.parametrize("scenario", [
    {"advisory_content": "not-json"},
    {"advisory_content": "{}"},
    {"advisory_content": json.dumps({"rating": 1.01, "note": "outside schema"})},
    {"advisory_content": GOOD,
     "terminal_error": "terminal provider error RAW_CREDENTIAL_CANARY " + WORK},
], ids=["non-json", "empty-object", "rating-out-of-range", "done-with-error"])
def test_o4_terminal_response_requires_valid_advisory_and_no_error_envelope(
        tmp_path, loopback_servers, scenario):
    server = loopback_servers(**scenario)
    cfg = _config(tmp_path / "data", server.base_url)
    result = ollama_review.run_review(cfg, "malformed-terminal", WORK)
    _assert_failed(result)
    assert [request["path"] for request in server.requests] == ["/api/tags", "/api/chat"]
    assert _allowance(cfg, "malformed-terminal")["conversation"]["used"] == 1


@pytest.mark.parametrize("mode", ["http_error", "malformed", "nonterminal", "oversized", "trickle"])
def test_o3_o4_failures_retain_attempt_never_fallback_and_total_timeout_is_bounded(
        tmp_path, loopback_servers, mode):
    server = loopback_servers(chat_mode=mode)
    cfg = _config(tmp_path / "data", server.base_url, max_conversation_reviews=1)
    cloud = SimpleNamespace(messages=SimpleNamespace(
        create=Mock(side_effect=AssertionError("cloud fallback attempted"))))
    dollar_ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
    started = time.monotonic()
    result, accounting = sim.call_sim(cfg, dollar_ledger, WORK, "failure", client=cloud)
    elapsed = time.monotonic() - started
    assert result is None
    assert accounting.get("status") != "ok", accounting
    assert cloud.messages.create.call_count == 0
    assert _allowance(cfg, "failure")["conversation"]["used"] == 1
    before = len(server.requests)
    _assert_failed(ollama_review.run_review(cfg, "failure", WORK))
    assert len(server.requests) == before, "Exhaustion after a retained failure must suppress HTTP"
    if mode == "trickle":
        assert elapsed < 1.75, "agent_timeout is a total wall-time bound, not a per-read timeout"


def test_o3_legacy_shared_allowance_counts_block_ollama_before_http(tmp_path, loopback_servers):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url, max_conversation_reviews=1)
    conversation = "legacy-shared"
    digest = hashlib.sha256(conversation.encode()).hexdigest()
    root = cfg.data_path / "subscription-review"
    root.mkdir(parents=True, mode=0o700)
    (root / "allowances.json").write_text(json.dumps({
        "version": 1,
        "conversations": {digest: 1},
        "days": {date.today().isoformat(): 1},
    }))
    assert ollama_review.preflight(cfg, conversation) is not None
    _assert_failed(ollama_review.run_review(cfg, conversation, WORK))
    assert server.requests == []
    assert _allowance(cfg, conversation)["conversation"]["used"] == 1


@pytest.mark.parametrize("api_state", ["exhausted", "corrupt"])
def test_o3_api_dollar_ledger_state_is_ignored_and_unchanged(tmp_path, loopback_servers, api_state):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url, max_review_cost=0, max_conversation_cost=0)
    if api_state == "exhausted":
        BudgetLedger(cfg.ledger_path, cfg.budget).record(
            1000, purpose=sim.SIM_PURPOSE, conversation_id="offline")
    else:
        cfg.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.ledger_path.write_bytes(b"not: [valid: budget: yaml")
    before = cfg.ledger_path.read_bytes()
    result = ollama_review.run_review(cfg, "offline", WORK)
    assert result.get("status") == "ok", result
    assert cfg.ledger_path.read_bytes() == before
    assert len(_requests(server, "POST")) == 1


def test_o3_cli_drain_works_with_corrupt_api_ledger(tmp_path, loopback_servers):
    server = loopback_servers()
    data = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    cfg = _config(data, server.base_url)
    store = Store(cfg)
    try:
        assert sim.enqueue_sim_review(store, cfg, "cli-offline", WORK, tick=1, intent=WORK)
    finally:
        store.close()
    cfg.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.ledger_path.write_bytes(b"not: [valid: budget: yaml")
    before = cfg.ledger_path.read_bytes()
    config_path = tmp_path / "kin.yaml"
    config_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    home = tmp_path / "cli-home"
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "KIN_HEALTH_DIR": str(tmp_path / "cli-health"),
        "PYTHONPATH": os.environ.get("PYTHONPATH", os.pathsep.join((
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[1])))),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    completed = subprocess.run([
        sys.executable, "-m", "kindex.cli", "sim", "drain",
        "--config", str(config_path), "--data-dir", str(data),
        "--project-path", str(project), "--json",
    ], cwd=project, env=env, text=True, capture_output=True, timeout=8)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert cfg.ledger_path.read_bytes() == before
    assert len(_requests(server, "POST")) == 1
    assert WORK not in completed.stdout + completed.stderr


def test_o3_cli_check_works_with_corrupt_api_ledger(tmp_path, loopback_servers):
    server = loopback_servers()
    data = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    cfg = _config(data, server.base_url)
    cfg.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.ledger_path.write_bytes(b"not: [valid: budget: yaml")
    before = cfg.ledger_path.read_bytes()
    config_path = tmp_path / "kin.yaml"
    config_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    home = tmp_path / "cli-home"
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "KIN_HEALTH_DIR": str(tmp_path / "cli-health"),
        "KIN_AGENT_SESSION_ID": "ollama-cli-check",
        "PYTHONPATH": os.environ.get("PYTHONPATH", os.pathsep.join((
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[1])))),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    completed = subprocess.run([
        sys.executable, "-m", "kindex.cli", "sim", "check", "--text", WORK,
        "--config", str(config_path), "--data-dir", str(data),
        "--project-path", str(project), "--json",
    ], cwd=project, env=env, text=True, capture_output=True, timeout=8)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert cfg.ledger_path.read_bytes() == before
    assert len(_requests(server, "POST")) == 1
    assert NOTE in completed.stdout
    assert WORK not in completed.stdout + completed.stderr


def test_o5_ollama_grounding_uses_local_fts_without_embeddings(tmp_path, monkeypatch, loopback_servers):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url, grounding_chars=1500)
    vector_search = Mock(side_effect=AssertionError("offline grounding invoked embeddings"))
    monkeypatch.setattr(vectors, "is_available", lambda: True)
    monkeypatch.setattr(vectors, "vector_search", vector_search)
    store = Store(cfg)
    try:
        title = "Offline Ollama grounding retains local FTS evidence"
        store.add_node(title, content="No embedding service is needed for this evidence.",
                       node_type="concept", node_id="ollama-local-fts")
        grounded = sim.build_sim_grounding(store, title, cfg)
        assert title in grounded
        assert vector_search.call_count == 0
    finally:
        store.close()


def test_o5_disabled_mode_admits_nothing(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": False, "backend": "api", "tick_interval": 1, "triage_banter": False,
    })
    store = Store(cfg)
    try:
        assert sim.enqueue_sim_review(store, cfg, "off", WORK, tick=1, intent=WORK) is False
        assert store.get_meta(sim.SIM_QUEUE_META) in (None, "", "[]")
    finally:
        store.close()


def test_o5_per_conversation_override_and_admitted_queue_snapshot_are_pinned(
        tmp_path, loopback_servers):
    old = loopback_servers(tags={"models": [_local_model("old-model:latest")]})
    new = loopback_servers(tags={"models": [_local_model("new-model:latest")]})
    cfg = Config(data_dir=str(tmp_path / "data"), sim={
        "enabled": False, "backend": "api", "tick_interval": 1, "triage_banter": False,
        "drain_on_tick": False, "grounding_chars": 0,
    }, agents={"instances": {"codex:alpha": {"client": "codex", "sim": {
        "enabled": True, "backend": "ollama", "ollama_url": old.base_url, "ollama_model": "old-model",
        "max_conversation_reviews": 3, "max_daily_reviews": 7,
    }}}})
    admitted = apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
    assert apply_agent_overrides(cfg, client="codex", instance_key="codex:beta").sim.enabled is False
    store = Store(admitted)
    try:
        assert sim.enqueue_sim_review(store, admitted, "alpha", WORK, tick=1, intent=WORK)
        cfg.agents.instances["codex:alpha"].sim.update({
            "ollama_url": new.base_url, "ollama_model": "new-model",
            "max_conversation_reviews": 1, "max_daily_reviews": 1,
        })
        next_work = apply_agent_overrides(cfg, client="codex", instance_key="codex:alpha")
        assert admitted.sim.ollama_url == old.base_url
        assert admitted.sim.ollama_model == "old-model"
        assert next_work.sim.ollama_url == new.base_url
        drained = sim.drain_sim_queue(store, next_work)
        assert drained["reviewed"] == 1, drained
        assert len(_requests(old, "POST")) == 1
        assert new.requests == []
    finally:
        store.close()


def test_o5_successful_queue_drain_is_delivered_by_subsequent_hook_without_paid_escalation(
        tmp_path, monkeypatch, loopback_servers):
    server = loopback_servers()
    cfg = _config(tmp_path / "data", server.base_url, threshold=0.7)
    scope = {"session_id": "delivery", "agent": "codex",
             "project_path": str(tmp_path.resolve())}
    conversation = supervisor.session_key(scope)
    escalation = Mock(side_effect=AssertionError("offline review launched paid Advocate"))
    monkeypatch.setattr(sim, "maybe_escalate_to_advocate", escalation)
    store = Store(cfg)
    try:
        assert sim.enqueue_sim_review(
            store, cfg, conversation, WORK, tick=1, intent=WORK, scope=scope)
        drained = sim.drain_sim_queue(store, cfg)
        assert drained["reviewed"] == 1 and drained["flagged"] == 1, drained
        delivered = supervisor.supervisor_tick(
            store, cfg, scope, text=WORK, event_id="next-hook", goal=WORK)
        assert NOTE in delivered["context"]
        assert escalation.call_count == 0
    finally:
        store.close()
