"""Kindex-owned text boundaries: deterministic, local credential minimization.

This is not an entropy detector, a secret vault, or a historical cleanup tool.
It recognizes credential formats and explicit credential-bearing fields. Ordinary
hashes, IDs, email addresses and IP addresses are preserved. Never use this on
operational authentication headers; use it on stored data and model/log output.
"""

from __future__ import annotations

import json
import builtins
import logging
import re
import traceback
from typing import Any

POLICY_VERSION = "credentials-v1"
REDACTED = "[REDACTED]"

_SENSITIVE_FIELDS = frozenset({
    "authorization", "proxyauthorization", "apikey", "apitoken",
    "accesstoken", "refreshtoken", "idtoken", "password", "passwd",
    "secret", "clientsecret", "secretkey", "secretaccesskey", "privatekey",
    "signingkey", "cookie", "setcookie", "credentials", "token", "authtoken",
})
_FIELD_NAME = re.compile(r"[^a-z0-9]")
_PLACEHOLDER = re.compile(r"^(?:\[REDACTED\]|\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})$")
_KEYS = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-(?:ant-(?:api\d+-)?|proj-|svcacct-)?[A-Za-z0-9_-]{20,}"
    r"|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|(?:rk|sk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r")(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?"
    r"(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|\Z)"
)
_URL_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")
_URL_CREDENTIAL = re.compile(
    r"(?P<prefix>[?&](?:key|token|auth|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|password|secret)=)(?P<value>[^\s&#\"'<>]+)", re.I,
)
_AUTH = re.compile(
    r"(?P<prefix>\b(?:Authorization|Proxy-Authorization)[\"']?\s*[:=]\s*"
    r"[\"']?(?:Bearer|Basic|Token)\s+)(?P<value>[A-Za-z0-9._~+/=-]+)", re.I,
)
_HEADER = re.compile(r"^(?P<prefix>\s*(?:Authorization|Proxy-Authorization|Cookie|Set-Cookie):\s*)[^\r\n]+", re.I | re.M)
_KEY_DECLARATION = re.compile(
    r"(?P<prefix>\bAPI[ _-]?(?:key|token)\s+is\s+)"
    r"(?P<value>[^\s,;]+)", re.I,
)
# Explicit assignments in prose, shell, JSON, YAML and URL query strings.
# Environment key prefixes are recognized; *_env names are deliberately not.
_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9_]*[_-])?"
    r"(?:api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret[_-]?access[_-]?key|secret[_-]?key|"
    r"password|passwd|private[_-]?key|authorization|proxy[_-]?authorization|"
    r"cookie|set[_-]?cookie|secret)\b[\"']?\s*[:=]\s*)"
    r'''(?P<value>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|[^\s,;&\}\"']+)''', re.I,
)


def _field_sensitive(key: str) -> bool:
    normalized = _FIELD_NAME.sub("", key.lower())
    return normalized in _SENSITIVE_FIELDS or any(
        normalized.endswith(suffix)
        for suffix in ("apikey", "apitoken", "clientsecret", "secretaccesskey")
    )


def _hidden(value: str) -> str:
    if not value or _PLACEHOLDER.fullmatch(value):
        return value
    return REDACTED


def redact_text(text: str) -> str:
    """Redact recognizable credentials, preserving nonsecret text and shape."""
    if not isinstance(text, str) or not text:
        return text
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _HEADER.sub(lambda m: m["prefix"] + REDACTED, text)
    text = _KEY_DECLARATION.sub(lambda m: m["prefix"] + _hidden(m["value"]), text)
    text = _URL_USERINFO.sub(lambda m: m["scheme"] + REDACTED + "@", text)
    text = _URL_CREDENTIAL.sub(lambda m: m["prefix"] + _hidden(m["value"]), text)
    text = _KEYS.sub(REDACTED, text)
    text = _AUTH.sub(lambda m: m["prefix"] + _hidden(m["value"]), text)

    def assignment(match: re.Match) -> str:
        value = match["value"]
        quote = value[0] if value[:1] in ("'", '"') else ""
        inside = value[1:-1] if quote else value
        return match["prefix"] + quote + _hidden(inside) + quote

    return _ASSIGNMENT.sub(assignment, text)


def redact(value: Any) -> Any:
    """Return a sanitized copy of JSON-compatible data, retaining scalar types.

    Sensitive dictionary fields are interpreted before text matching, so short
    passwords and multiline credentials need no recognizable token prefix.
    Structured IDs/digests receive no entropy/length heuristic.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            clean_key = redact_text(key) if isinstance(key, str) else key
            if clean_key in cleaned:
                # Do not silently merge fields if two credential-bearing keys
                # collapse to the marker. Reject before any persistence occurs.
                raise ValueError("Credential redaction would merge object fields")
            cleaned[clean_key] = (
                _redact_field(item) if isinstance(key, str) and _field_sensitive(key)
                else redact(item)
            )
        if "content_digest" in value and "digest_scope" in value:
            for location in ("path", "url"):
                if location in value and cleaned.get(location) != value[location]:
                    # Preserve evidence of the original referent without
                    # claiming that a substituted location has that digest.
                    cleaned.pop(location, None)
                    cleaned[location + "_redacted"] = True
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def _redact_field(value: Any) -> Any:
    if isinstance(value, str):
        return _hidden(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            clean_key = redact_text(key) if isinstance(key, str) else key
            if clean_key in cleaned:
                raise ValueError("Credential redaction would merge object fields")
            cleaned[clean_key] = _redact_field(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_field(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_field(item) for item in value)
    return None if value is None else REDACTED


def redact_serialized(text: str) -> str:
    """Sanitize a stored JSON string structurally; retain unchanged formatting."""
    try:
        original = json.loads(text)
    except (ValueError, TypeError):
        return redact_text(text)
    cleaned = redact(original)
    if cleaned == original:
        return text
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    return leading + json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + trailing


def safe_error(error: BaseException, limit: int = 200) -> str:
    """Sanitize before truncation; a failed formatter cannot leak raw errors."""
    try:
        return redact_serialized(str(error))[:limit]
    except Exception:
        return "Error details unavailable"


def redacting_print(*values, sep=" ", end="\n", file=None, flush=False) -> None:
    """A complete Kindex print event; joins arguments before credential checks.

    This does not claim to intercept arbitrary writes or fragmented byte streams.
    Already-safe JSON retains its original formatting and serialized shape.
    """
    separator = " " if sep is None else sep
    ending = "\n" if end is None else end
    rendered = separator.join(str(value) for value in values) + ending
    builtins.print(redact_serialized(rendered), end="", file=file, flush=flush)


class RedactingFilter(logging.Filter):
    """Protect Kindex logger messages, interpolation arguments and tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_serialized(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = redact_text(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
            # Structured extras can be rendered by custom formatters too.
            for key, value in tuple(record.__dict__.items()):
                if key not in {"msg", "args", "exc_info", "exc_text", "stack_info"}:
                    record.__dict__[key] = _redact_field(value) if _field_sensitive(key) else redact(value)
        except Exception:
            record.msg, record.args = "Kindex log details unavailable", ()
            record.exc_info, record.exc_text = None, None
            record.stack_info = None
            # A malformed extra must not let a formatter emit its raw value.
            for key in record.__dict__:
                if key not in logging.makeLogRecord({}).__dict__:
                    record.__dict__[key] = REDACTED
        return True


def protect_logger(logger: logging.Logger) -> logging.Logger:
    if not any(isinstance(item, RedactingFilter) for item in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
