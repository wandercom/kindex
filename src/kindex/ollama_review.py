"""Fail-closed, loopback-only Ollama reviews for the Sim supervisor."""
from __future__ import annotations

import errno
import json
import select
import socket
import time
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024
_REMOTE_METADATA = {"remote_model", "remote_host"}


class _TagsResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    models: list[dict[str, Any]]


class _TagDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    format: str


class _LocalTag(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    name: str
    model: str
    size: int
    digest: str
    details: _TagDetails


class _OllamaError(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _endpoint(value: Any) -> tuple[str, int, str] | None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "http" or parts.username is not None or parts.password is not None or
            parts.query or parts.fragment or parts.path not in ("", "/")):
        return None
    host = parts.hostname
    if not host:
        return None
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        address = ip_address(host)
    except ValueError:
        return None
    if not address.is_loopback or (address.version == 6 and address.ipv4_mapped is not None):
        return None
    target_port = 11434 if port is None else port
    if not 1 <= target_port <= 65535:
        return None
    host_header = "[{}]".format(host) if address.version == 6 else host
    return host, target_port, host_header


def _wait(sock: socket.socket, *, readable: bool, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _OllamaError("ollama_timeout")
    ready = select.select([sock] if readable else [], [] if readable else [sock], [sock], remaining)
    if ready[2]:
        raise _OllamaError("ollama_http_failed")
    if not ready[0] and not ready[1]:
        raise _OllamaError("ollama_timeout")


def _connect(endpoint: tuple[str, int, str], deadline: float) -> socket.socket:
    host, port, _ = endpoint
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        result = sock.connect_ex((host, port))
        if result not in (0, errno.EISCONN):
            if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                raise _OllamaError("ollama_unavailable")
            _wait(sock, readable=False, deadline=deadline)
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
                raise _OllamaError("ollama_unavailable")
        return sock
    except Exception:
        sock.close()
        raise


def _send(sock: socket.socket, data: bytes, deadline: float) -> None:
    view = memoryview(data)
    while view:
        _wait(sock, readable=False, deadline=deadline)
        try:
            sent = sock.send(view)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise _OllamaError("ollama_http_failed") from exc
        if sent <= 0:
            raise _OllamaError("ollama_http_failed")
        view = view[sent:]


def _recv(sock: socket.socket, buffer: bytearray, deadline: float) -> bool:
    _wait(sock, readable=True, deadline=deadline)
    try:
        chunk = sock.recv(65536)
    except BlockingIOError:
        return True
    except OSError as exc:
        raise _OllamaError("ollama_http_failed") from exc
    if not chunk:
        return False
    buffer.extend(chunk)
    return True


def _headers(raw: bytes) -> tuple[int, dict[str, str]]:
    try:
        rows = raw.decode("iso-8859-1").split("\r\n")
        version, status, _ = rows[0].split(" ", 2)
        code = int(status)
    except (UnicodeDecodeError, IndexError, ValueError):
        raise _OllamaError("ollama_response_invalid") from None
    if not version.startswith("HTTP/"):
        raise _OllamaError("ollama_response_invalid")
    headers: dict[str, str] = {}
    for row in rows[1:]:
        if not row or ":" not in row:
            raise _OllamaError("ollama_response_invalid")
        key, value = row.split(":", 1)
        key = key.strip().lower()
        if not key or key in headers:
            raise _OllamaError("ollama_response_invalid")
        headers[key] = value.strip()
    if 300 <= code < 400 or "location" in headers:
        raise _OllamaError("ollama_redirect_rejected")
    if code != 200:
        raise _OllamaError("ollama_http_failed")
    return code, headers


def _read_exact(sock: socket.socket, buffer: bytearray, size: int, deadline: float) -> bytes:
    while len(buffer) < size:
        if not _recv(sock, buffer, deadline):
            raise _OllamaError("ollama_response_invalid")
    value = bytes(buffer[:size])
    del buffer[:size]
    return value


def _read_chunked(sock: socket.socket, buffer: bytearray, deadline: float) -> bytes:
    body = bytearray()
    while True:
        while b"\r\n" not in buffer:
            if len(buffer) > _MAX_HEADER_BYTES or not _recv(sock, buffer, deadline):
                raise _OllamaError("ollama_response_invalid")
        line, _, remainder = buffer.partition(b"\r\n")
        buffer[:] = remainder
        try:
            size = int(line.split(b";", 1)[0], 16)
        except ValueError:
            raise _OllamaError("ollama_response_invalid") from None
        if size < 0 or len(body) + size > _MAX_RESPONSE_BYTES:
            raise _OllamaError("ollama_response_oversized")
        if size == 0:
            while b"\r\n\r\n" not in buffer:
                if buffer == b"\r\n":
                    return bytes(body)
                if len(buffer) > _MAX_HEADER_BYTES or not _recv(sock, buffer, deadline):
                    raise _OllamaError("ollama_response_invalid")
            return bytes(body)
        body.extend(_read_exact(sock, buffer, size, deadline))
        if _read_exact(sock, buffer, 2, deadline) != b"\r\n":
            raise _OllamaError("ollama_response_invalid")


def _read_body(sock: socket.socket, initial: bytearray, headers: dict[str, str], deadline: float) -> bytes:
    transfer = headers.get("transfer-encoding", "").lower()
    if transfer:
        if transfer != "chunked":
            raise _OllamaError("ollama_response_invalid")
        return _read_chunked(sock, initial, deadline)
    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError:
            raise _OllamaError("ollama_response_invalid") from None
        if size < 0:
            raise _OllamaError("ollama_response_invalid")
        if size > _MAX_RESPONSE_BYTES:
            raise _OllamaError("ollama_response_oversized")
        return _read_exact(sock, initial, size, deadline)
    body = initial
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _OllamaError("ollama_response_oversized")
    while _recv(sock, body, deadline):
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _OllamaError("ollama_response_oversized")
    return bytes(body)


def _request(endpoint: tuple[str, int, str], method: str, path: str, payload: dict[str, Any] | None,
             deadline: float) -> dict[str, Any]:
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {endpoint[2]}:{endpoint[1]}", "Accept: application/json",
             "Connection: close"]
    if payload is not None:
        lines.extend(("Content-Type: application/json", f"Content-Length: {len(body)}"))
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
    sock = _connect(endpoint, deadline)
    try:
        _send(sock, request, deadline)
        received = bytearray()
        while b"\r\n\r\n" not in received:
            if len(received) > _MAX_HEADER_BYTES:
                raise _OllamaError("ollama_response_oversized")
            if not _recv(sock, received, deadline):
                raise _OllamaError("ollama_response_invalid")
        raw_headers, _, remainder = received.partition(b"\r\n\r\n")
        _, headers = _headers(raw_headers)
        response = _read_body(sock, bytearray(remainder), headers, deadline)
    finally:
        sock.close()
    try:
        data = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _OllamaError("ollama_response_invalid") from None
    if not isinstance(data, dict):
        raise _OllamaError("ollama_response_invalid")
    return data


def _has_remote_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _REMOTE_METADATA or _has_remote_metadata(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_has_remote_metadata(item) for item in value)
    return False


def _matches(configured: str, candidate: str) -> bool:
    return candidate == configured or (":" not in configured and candidate == configured + ":latest")


def _verified_model(endpoint: tuple[str, int, str], model: str, deadline: float) -> str:
    data = _request(endpoint, "GET", "/api/tags", None, deadline)
    try:
        tags = _TagsResponse.model_validate(data)
    except ValidationError:
        raise _OllamaError("ollama_response_invalid") from None
    candidates = []
    for raw in tags.models:
        name = raw.get("name")
        remote_model = raw.get("model")
        if isinstance(name, str) and isinstance(remote_model, str) and _matches(model, name) and _matches(model, remote_model):
            candidates.append(raw)
    if len(candidates) != 1:
        raise _OllamaError("ollama_model_unavailable")
    raw = candidates[0]
    if model.lower().endswith(":cloud") or _has_remote_metadata(raw):
        raise _OllamaError("ollama_model_unavailable")
    try:
        tag = _LocalTag.model_validate(raw)
    except ValidationError:
        raise _OllamaError("ollama_model_unavailable") from None
    if (tag.name.lower().endswith(":cloud") or tag.model.lower().endswith(":cloud") or
            tag.details.format.lower() != "gguf" or tag.size <= 0 or not tag.digest.strip()):
        raise _OllamaError("ollama_model_unavailable")
    return tag.name


def preflight(config, conversation_id: str) -> tuple[str, str] | None:
    """Reject local configuration and exhausted shared allowances before queueing."""
    if _endpoint(config.sim.ollama_url) is None:
        return "unavailable", "ollama_endpoint_invalid"
    if not isinstance(config.sim.ollama_model, str) or not config.sim.ollama_model.strip():
        return "unavailable", "ollama_model_unavailable"
    if config.sim.ollama_model.lower().endswith(":cloud"):
        return "unavailable", "ollama_model_unavailable"
    if type(config.sim.max_output_tokens) is not int or config.sim.max_output_tokens <= 0:
        return "unavailable", "ollama_output_limit_invalid"
    from .subscription_review import allowance_status
    try:
        status = allowance_status(config, conversation_id)
    except TimeoutError:
        return "unavailable", "ollama_timeout"
    except (OSError, ValueError, TypeError):
        return "unavailable", "review_accounting_unavailable"
    if any(status[name]["remaining"] <= 0 for name in ("conversation", "day")):
        return "budget_exhausted", "review_budget_exhausted"
    return None


def run_review(config, conversation_id: str, prompt: str) -> dict[str, Any]:
    """Review redacted text through verified local Ollama weights exactly once."""
    endpoint = _endpoint(config.sim.ollama_url)
    model = config.sim.ollama_model
    if endpoint is None:
        return {"status": "ollama_endpoint_invalid"}
    if not isinstance(model, str) or not model.strip():
        return {"status": "ollama_model_unavailable"}
    if model.lower().endswith(":cloud"):
        return {"status": "ollama_model_unavailable"}
    if type(config.sim.max_output_tokens) is not int or config.sim.max_output_tokens <= 0:
        return {"status": "ollama_output_limit_invalid"}
    deadline = time.monotonic() + config.sim.agent_timeout
    try:
        from .subscription_review import reserve_attempt
        reservation = reserve_attempt(config, conversation_id, deadline=deadline)
        if reservation.get("status") != "ok":
            if reservation.get("status") == "review_lock_timeout":
                return {"status": "ollama_timeout"}
            return reservation
        verified_model = _verified_model(endpoint, model, deadline)
        response = _request(endpoint, "POST", "/api/chat", {
            "model": verified_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"num_predict": config.sim.max_output_tokens},
        }, deadline)
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if ("error" in response or response.get("done") is not True or
                not isinstance(content, str)):
            return {"status": "ollama_response_invalid"}
        from .sim import _parse_sim, _result_from_parsed
        parsed = _parse_sim(content)
        if not isinstance(parsed.get("note"), str) or _result_from_parsed(parsed) is None:
            return {"status": "ollama_response_invalid"}
        usage = {key: response[key] for key in ("prompt_eval_count", "eval_count")
                 if isinstance(response.get(key), int) and not isinstance(response.get(key), bool) and response[key] >= 0}
        return {"status": "ok", "response": content, "usage": usage, "via": "ollama"}
    except _OllamaError as exc:
        return {"status": exc.reason}
    except (OSError, TypeError, ValueError):
        return {"status": "ollama_review_failed"}
