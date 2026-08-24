"""Referent binding and staleness (R0 — PRD lineage-grounding, lead item).

A node may bind the external thing its claim describes: a ``referent``
(``{path|url, content_digest, digest_scope}``) plus two clocks —
``asserted_at`` (when the claim was made) and ``true_of`` (when the referent
was observed in the state the digest describes). Staleness then becomes a
computable divergence: re-hash the referent; a mismatch means the claim was
true of something that has since moved.

Invariants upheld here:
- Detection never deletes or rewrites node content. A stale finding is a
  recorded demotion marker (``extra["referent_stale"]``, a reserved key) plus
  a re-verification candidate in the sweep report — nothing else.
- The recorded digest is primary; a commit hash is a hint (``repo`` scope is
  recorded but never auto-checked in v1 — review outcome point 1).
- Only ``file``-scope referents with a resolvable path are re-hashable;
  ``url``/``repo`` scopes are recorded, surfaced, and skipped by the sweep.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

DIGEST_SCOPES = ("file", "url", "repo")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")

# Marker reasons recorded in extra["referent_stale"].
REASON_MISMATCH = "digest-mismatch"
REASON_MISSING = "referent-missing"


class ReferentError(ValueError):
    """A referent binding is malformed."""


def validate_referent(referent: dict) -> dict:
    """Validate and normalize a referent dict; return a clean copy.

    Exactly one of ``path`` | ``url``; ``digest_scope`` in DIGEST_SCOPES
    (default: ``url`` for URLs, ``file`` for paths); ``content_digest`` is
    lowercase sha256 hex for file/url scope, 7-64 hex for repo scope (a
    commit hint). Fail-closed: anything else raises ``ReferentError``.
    """
    if not isinstance(referent, dict):
        raise ReferentError("referent must be a dict")
    path = referent.get("path")
    url = referent.get("url")
    if bool(path) == bool(url):
        raise ReferentError("referent requires exactly one of 'path' or 'url'")
    scope = referent.get("digest_scope") or ("url" if url else "file")
    if scope not in DIGEST_SCOPES:
        raise ReferentError(
            f"digest_scope must be one of {DIGEST_SCOPES}, got {scope!r}"
        )
    digest = str(referent.get("content_digest") or "").lower()
    pattern = _COMMIT_RE if scope == "repo" else _SHA256_RE
    if not pattern.match(digest):
        kind = "7-64 hex (commit)" if scope == "repo" else "64-hex sha256"
        raise ReferentError(
            f"content_digest for scope '{scope}' must be {kind}"
        )
    clean: dict = {"content_digest": digest, "digest_scope": scope}
    if path:
        clean["path"] = str(path)
    else:
        clean["url"] = str(url)
    return clean


def hash_file(path: Path) -> str:
    """sha256 hex of a file's bytes (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ReferentCheck:
    """Outcome of re-hashing one node's referent."""

    status: str  # fresh | stale | missing | unhashable
    expected_digest: str | None = None
    actual_digest: str | None = None


def _resolve_path(raw: str, base_dir: Path | None) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (base_dir or Path.cwd()) / p


def check_node_referent(node: dict, base_dir: Path | None = None) -> ReferentCheck:
    """Re-hash one node's referent against its recorded digest.

    Only file-scope path referents are re-hashable; everything else is
    ``unhashable`` (recorded, surfaced, never auto-judged in v1).
    """
    referent = node.get("referent")
    if isinstance(referent, str):
        try:
            referent = json.loads(referent)
        except (json.JSONDecodeError, TypeError):
            referent = None
    if not isinstance(referent, dict):
        return ReferentCheck("unhashable")
    expected = referent.get("content_digest")
    if referent.get("digest_scope") != "file" or not referent.get("path"):
        return ReferentCheck("unhashable", expected_digest=expected)
    target = _resolve_path(referent["path"], base_dir)
    if not target.is_file():
        return ReferentCheck("missing", expected_digest=expected)
    try:
        actual = hash_file(target)
    except OSError:
        return ReferentCheck("missing", expected_digest=expected)
    if actual == expected:
        return ReferentCheck("fresh", expected_digest=expected, actual_digest=actual)
    return ReferentCheck("stale", expected_digest=expected, actual_digest=actual)


def _record_stale(store: "Store", node_id: str, check: ReferentCheck) -> None:
    from .store import _utc_now

    reason = REASON_MISSING if check.status == "missing" else REASON_MISMATCH
    marker = {
        "reason": reason,
        "expected_digest": check.expected_digest,
        "actual_digest": check.actual_digest,
        "detected_at": _utc_now(),
    }

    def mutate(extra: dict) -> None:
        extra["referent_stale"] = marker

    store.atomic_extra_update(node_id, mutate)
    store._log("referent_stale", node_id, "", "", details=marker)


def _clear_stale(store: "Store", node_id: str) -> bool:
    cleared = False

    def mutate(extra: dict) -> None:
        nonlocal cleared
        cleared = extra.pop("referent_stale", None) is not None

    store.atomic_extra_update(node_id, mutate)
    if cleared:
        store._log("referent_fresh", node_id, "", "",
                   details={"cleared": "referent_stale"})
    return cleared


def stale_sweep(store: "Store", base_dir: Path | str | None = None) -> dict:
    """Re-hash every active node's referent; record/clear demotion markers.

    A ``stale`` or ``missing`` referent records ``extra["referent_stale"]``
    (demoting the node from trusted recall — trust.py reads the marker); a
    ``fresh`` re-hash clears any existing marker. Content is never touched.
    The stale/missing lists are the re-verification candidates.
    """
    base = Path(base_dir) if base_dir is not None else None
    report: dict = {
        "checked": 0, "fresh": 0, "unhashable": 0,
        "stale": [], "missing": [], "cleared": [],
    }
    rows = store.conn.execute(
        "SELECT * FROM nodes WHERE referent IS NOT NULL AND status = 'active' "
        "ORDER BY id"
    ).fetchall()
    for row in rows:
        node = store._row_to_dict(row)
        check = check_node_referent(node, base)
        report["checked"] += 1
        entry = {
            "id": node["id"],
            "title": node.get("title", ""),
            "expected_digest": check.expected_digest,
            "actual_digest": check.actual_digest,
        }
        if check.status == "fresh":
            report["fresh"] += 1
            if (node.get("extra") or {}).get("referent_stale"):
                _clear_stale(store, node["id"])
                report["cleared"].append(entry)
        elif check.status == "unhashable":
            report["unhashable"] += 1
        else:
            _record_stale(store, node["id"], check)
            report[check.status].append(entry)
    return report


def rebind(store: "Store", node_id: str, base_dir: Path | str | None = None) -> dict:
    """Re-hash a node's current file referent and rebind to the new state.

    The deliberate re-verification act: ``true_of`` moves to now,
    ``asserted_at`` stays (the claim is not re-dated), the stale marker
    clears. Raises on missing node/referent or unreadable file.
    """
    node = store.get_node(node_id)
    if node is None:
        raise ValueError(f"Node not found: {node_id}")
    referent = node.get("referent")
    if not isinstance(referent, dict):
        raise ReferentError(f"Node {node_id} has no referent to rebind")
    if referent.get("digest_scope") != "file" or not referent.get("path"):
        raise ReferentError(
            "only file-scope path referents can be re-hashed; "
            "use Store.bind_referent with an explicit digest instead"
        )
    base = Path(base_dir) if base_dir is not None else None
    target = _resolve_path(referent["path"], base)
    new_digest = hash_file(target)  # OSError propagates: no digest, no rebind
    updated = dict(referent, content_digest=new_digest)
    return store.bind_referent(node_id, updated)
