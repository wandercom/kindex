"""Known-secret masking: replace secret values from user-named env files with
their key names.

The user names one or more shell env files (for example ``~/.profile``) in
their own config::

    secret_mask:
      files: [~/.profile]
      keys: [OPENAI, "AWS_*_KEY"]   # optional; DEFAULT_KEY_PATTERNS when omitted
      min_length: 8

Every ``NAME=VALUE`` line whose name is selected yields a known secret. Any
occurrence of VALUE in text Kindex stores or shows becomes ``$NAME``, which is
safe to keep. ``privacy.redact_text`` applies the active mask first, so every
write and output path that already redacts credentials masks known secrets
too. This module never logs, raises or returns a secret value.

Only the user's config may set ``secret_mask``; a repository's ``.kin/config``
cannot enable, weaken or disable it (``config._PROJECT_LAYER_UNTRUSTED_KEYS``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from . import platform_io

#: Name fragments selected when ``keys`` is omitted (case-insensitive substrings).
DEFAULT_KEY_PATTERNS: tuple[str, ...] = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASS", "PW", "CREDENTIAL",
    "AUTH", "PRIVATE", "SID", "DSN", "WEBHOOK", "COOKIE", "CLIENT_ID",
    "ACCOUNT_ID", "API",
)
DEFAULT_MIN_LENGTH = 8

logger = logging.getLogger(__name__)

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_COMMENT = re.compile(r"\s#")


class SecretMaskConfig(BaseModel):
    """User-level ``secret_mask`` settings. Absent means masking is off."""

    files: list[str] = Field(default_factory=list)
    keys: list[str] | None = None
    min_length: int = Field(default=DEFAULT_MIN_LENGTH, ge=1)


def _selector(pattern: str) -> re.Pattern[str]:
    # Case-insensitive substring; `*` matches any run of characters. A full
    # name is a selector that also matches itself.
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(body, re.IGNORECASE)


def key_selected(name: str, keys: Sequence[str] | None = None) -> bool:
    """Whether ``name`` is a secret name under ``keys`` (default patterns if None)."""
    patterns = DEFAULT_KEY_PATTERNS if keys is None else keys
    return any(_selector(p).search(name) for p in patterns if p and p.strip("*"))


def _value(raw: str) -> str | None:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        rest = raw[end + 1:] if end > 0 else None
        if rest is None or (rest.strip() and not rest.lstrip().startswith("#")):
            return None  # unterminated or concatenated: not a plain literal
        return raw[1:end]
    comment = _COMMENT.search(raw)
    return (raw[:comment.start()] if comment else raw).rstrip()


def parse_env_text(text: str, keys: Sequence[str] | None = None,
                   min_length: int = DEFAULT_MIN_LENGTH) -> list[tuple[str, str]]:
    """``(name, value)`` pairs in file order for selected, literal values.

    Lines are ``[export ]NAME=VALUE``. Matching surrounding quotes are
    stripped; an unquoted value loses a trailing `` #comment``. Values with
    ``$`` or a backtick (expansions Kindex cannot resolve) and values shorter
    than ``min_length`` are skipped.
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match or not key_selected(match[1], keys):
            continue
        value = _value(match[2])
        if value is None or "$" in value or "`" in value or len(value) < min_length:
            continue
        pairs.append((match[1], value))
    return pairs


def build_table(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(value, name)`` replacements, longest value first.

    The first name in file order owns a value shared by several names. A
    value's JSON-escaped spelling (as stored inside JSON columns) maps to the
    same name, so serialized occurrences are masked too.
    """
    owner: dict[str, str] = {}
    pairs = list(pairs)
    for name, value in pairs:
        owner.setdefault(value, name)
    for name, value in pairs:
        for ascii_only in (False, True):
            escaped = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            if escaped != value:
                owner.setdefault(escaped, owner[value])
    # sorted() is stable: equal lengths keep file order.
    return sorted(owner.items(), key=lambda item: -len(item[0]))


def mask_text(text: str, table: Sequence[tuple[str, str]]) -> str:
    """Replace every occurrence of each known value with ``$NAME`` (pure)."""
    if not isinstance(text, str) or not text:
        return text
    for value, name in table:
        if value in text:
            text = text.replace(value, "$" + name)
    return text


def count_matches(text: str, table: Sequence[tuple[str, str]]) -> Counter[str]:
    """Occurrences per key name that ``mask_text`` would replace."""
    counts: Counter[str] = Counter()
    if not isinstance(text, str) or not text:
        return counts
    for value, name in table:
        found = text.count(value)
        if found:
            counts[name] += found
            text = text.replace(value, "$" + name)
    return counts


class SecretMask:
    """Known secrets from env files, re-read when a file's mtime or size changes."""

    def __init__(self, files: Sequence[str | Path], keys: Sequence[str] | None = None,
                 min_length: int = DEFAULT_MIN_LENGTH):
        self.files = [Path(f) for f in files]
        self.keys = None if keys is None else list(keys)
        self.min_length = min_length
        self._stamps: dict[Path, tuple[int, int] | None] = {}
        self._pairs: dict[Path, list[tuple[str, str]]] = {}
        self._table: list[tuple[str, str]] = []
        self._warned: set[Path] = set()
        self._lock = threading.RLock()

    def _stamp(self, path: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def table(self) -> list[tuple[str, str]]:
        """The current replacement table, refreshed from changed files."""
        stamps = {path: self._stamp(path) for path in self.files}
        if all(path in self._stamps and self._stamps[path] == stamps[path]
               for path in self.files):
            return self._table
        unreadable: list[tuple[Path, str]] = []
        with self._lock:
            for path in self.files:
                if path in self._stamps and self._stamps[path] == stamps[path]:
                    continue
                self._stamps[path] = stamps[path]
                try:
                    text = path.read_text(errors="replace", encoding="utf-8")
                except OSError as error:
                    self._pairs[path] = []
                    if path not in self._warned:
                        self._warned.add(path)
                        unreadable.append((path, type(error).__name__))
                    continue
                self._warned.discard(path)
                self._pairs[path] = parse_env_text(text, self.keys, self.min_length)
            self._table = build_table(
                pair for path in self.files for pair in self._pairs.get(path, []))
            table = self._table
        # Warn outside the lock: a redacting log filter calls back into mask().
        for path, reason in unreadable:
            logger.warning("secret_mask: cannot read %s (%s); masking nothing from it",
                           path, reason)
        return table

    def names(self) -> list[str]:
        """Distinct key names with at least one masked value, in file order."""
        self.table()
        return list(dict.fromkeys(
            name for path in self.files for name, _ in self._pairs.get(path, [])))

    def mask(self, text: str) -> str:
        return mask_text(text, self.table())

    def counts(self, text: str) -> Counter[str]:
        return count_matches(text, self.table())


_active: SecretMask | None = None


def configure(settings: SecretMaskConfig | Mapping | None,
              resolve=lambda p: platform_io.expanduser(Path(p))) -> SecretMask | None:
    """Install the process-wide mask from the user's settings (None turns it off).

    ``resolve`` maps a configured file path to a filesystem path; config passes
    its bound-root-aware resolver.
    """
    global _active
    if settings is None:
        _active = None
        return None
    if not isinstance(settings, SecretMaskConfig):
        settings = SecretMaskConfig(**settings)
    files = [resolve(f) for f in settings.files]
    current = _active
    if (current is not None and current.files == files and current.keys == settings.keys
            and current.min_length == settings.min_length):
        return current  # keep the parsed cache across repeated config loads
    _active = SecretMask(files, settings.keys, settings.min_length)
    return _active


def active() -> SecretMask | None:
    """The installed mask, or None when masking is off."""
    return _active


def mask(text: str) -> str:
    """Mask ``text`` with the installed mask; unchanged when masking is off."""
    current = _active
    if current is None or not isinstance(text, str) or not text:
        return text
    return current.mask(text)
