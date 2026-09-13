"""Deterministic valid-time and trust admission for Kindex recall.

Ordinary search is intentionally a recall surface and does not use this module
unless the caller opts in. Resume context always does. Verification identity is
asserted local audit text; none of these predicates authenticate a caller.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .store import Store


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:(?!60(?:[.,]|Z|[+-]))\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)

TRUST_REASONS = (
    "trusted",
    "inactive",
    "unverified",
    "not_yet_valid",
    "invalidated",
    "stale_referent",
    "mutual_contradiction",
)


@dataclass(frozen=True)
class TrustDecision:
    eligible: bool
    reason: str
    conflict_ids: tuple[str, ...] = ()


def parse_rfc3339(value: str, *, field: str) -> datetime:
    """Parse one timezone-aware RFC 3339 instant.

    Kindex accepts ``Z`` or an explicit numeric offset, rejects leap-second
    notation and naive timestamps, and returns an aware UTC datetime.
    """
    from .store import InvalidIntervalError

    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise InvalidIntervalError(
            f"{field} must be timezone-aware RFC 3339 (Z or numeric offset)"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidIntervalError(f"{field} is not a valid RFC 3339 instant") from exc
    if parsed.tzinfo is None or offset is None:
        raise InvalidIntervalError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def normalize_rfc3339(value: str | datetime, *, field: str) -> str:
    """Normalize an aware instant to canonical UTC RFC 3339."""
    from .store import InvalidIntervalError

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidIntervalError(f"{field} must include a timezone offset")
        parsed = value.astimezone(timezone.utc)
    else:
        parsed = parse_rfc3339(value, field=field)
    if parsed.microsecond:
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_interval(
    valid_at: str | None,
    invalid_at: str | None,
) -> tuple[str | None, str | None]:
    """Normalize and validate a half-open ``[valid_at, invalid_at)`` interval."""
    from .store import InvalidIntervalError

    normalized_valid = (
        normalize_rfc3339(valid_at, field="valid_at") if valid_at is not None else None
    )
    normalized_invalid = (
        normalize_rfc3339(invalid_at, field="invalid_at")
        if invalid_at is not None
        else None
    )
    if normalized_valid is not None and normalized_invalid is not None:
        if parse_rfc3339(normalized_invalid, field="invalid_at") <= parse_rfc3339(
            normalized_valid, field="valid_at"
        ):
            raise InvalidIntervalError("invalid_at must be later than valid_at")
    return normalized_valid, normalized_invalid


def _operation_time(at: datetime | str | None) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    if isinstance(at, datetime):
        # Normalize through the public function so naive values fail identically.
        value = normalize_rfc3339(at, field="evaluation_time")
        return parse_rfc3339(value, field="evaluation_time")
    return parse_rfc3339(at, field="evaluation_time")


def _base_trust_decision(node: dict, *, at: datetime) -> TrustDecision:
    """Trust decision excluding graph contradictions.

    This is intentionally a two-stage predicate: a contradiction endpoint must
    independently pass this stage before it can suppress another node.
    """
    if (node.get("status") or "active") != "active":
        return TrustDecision(False, "inactive")

    external_extra = node.get("extra")
    imported = external_extra.get("kinbase") if isinstance(external_extra, dict) else None
    if isinstance(imported, dict):
        # A source signature authenticates bytes, not the signer authority.
        # Reduced snapshots also need a freshness/admission policy before
        # they can enter Kindex trusted recall; manual verification cannot
        # silently promote this external evidence cache.
        return TrustDecision(False, "unverified")

    verified_at = node.get("verified_at")
    verified_by = node.get("verified_by")
    method = node.get("prov_method")
    if not all(isinstance(v, str) and v.strip() for v in (verified_at, verified_by, method)):
        return TrustDecision(False, "unverified")
    try:
        parse_rfc3339(verified_at, field="verified_at")
    except ValueError:
        return TrustDecision(False, "unverified")

    valid_at = node.get("valid_at")
    if valid_at:
        try:
            if at < parse_rfc3339(valid_at, field="valid_at"):
                return TrustDecision(False, "not_yet_valid")
        except ValueError:
            return TrustDecision(False, "not_yet_valid")

    invalid_at = node.get("invalid_at")
    if invalid_at:
        try:
            if parse_rfc3339(invalid_at, field="invalid_at") <= at:
                return TrustDecision(False, "invalidated")
        except ValueError:
            # Corrupt end-time metadata fails closed.
            return TrustDecision(False, "invalidated")

    # R0 demotion: a recorded stale-referent marker (written by the referent
    # sweep when re-hashing found the referent moved or missing) removes the
    # node from trusted recall until it is re-verified/rebound. This also
    # strips its power to contradict — only admitted nodes may suppress.
    extra = node.get("extra")
    if isinstance(extra, str):
        try:
            import json
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    if isinstance(extra, dict) and extra.get("referent_stale"):
        return TrustDecision(False, "stale_referent")

    return TrustDecision(True, "trusted")


def node_trust_decision(
    store: Store,
    node: dict,
    *,
    at: datetime | str | None = None,
) -> TrustDecision:
    """Return the admission result for one durable node."""
    evaluation_time = _operation_time(at)
    base = _base_trust_decision(node, at=evaluation_time)
    if not base.eligible:
        return base

    node_id = node.get("id")
    if not node_id:
        return TrustDecision(False, "unverified")
    rows = store.conn.execute(
        """SELECT CASE WHEN from_id = ? THEN to_id ELSE from_id END AS other_id
             FROM edges
            WHERE type = 'contradicts' AND (from_id = ? OR to_id = ?)
            ORDER BY other_id""",
        (node_id, node_id, node_id),
    ).fetchall()
    conflict_ids: list[str] = []
    for row in rows:
        other_id = row["other_id"]
        if not other_id or other_id == node_id or other_id in conflict_ids:
            continue
        other_row = store.conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (other_id,)
        ).fetchone()
        if other_row is None:
            continue
        other = store._row_to_dict(other_row)
        if _base_trust_decision(other, at=evaluation_time).eligible:
            conflict_ids.append(other_id)
    if conflict_ids:
        return TrustDecision(False, "mutual_contradiction", tuple(sorted(conflict_ids)))
    return base


def filter_trusted_nodes(
    store: Store,
    nodes: Iterable[dict],
    *,
    at: datetime | str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Filter nodes with one operation-scoped evaluation instant.

    Survivor order is preserved. The returned reason map contains every denial
    category, including zeroes, so callers can disclose omissions without
    inventing or renaming machine reasons.
    """
    evaluation_time = _operation_time(at)
    omissions: Counter[str] = Counter()
    trusted: list[dict] = []
    for node in nodes:
        decision = node_trust_decision(store, node, at=evaluation_time)
        if decision.eligible:
            trusted.append(node)
        else:
            omissions[decision.reason] += 1
    counts = {reason: omissions.get(reason, 0) for reason in TRUST_REASONS if reason != "trusted"}
    return trusted, counts
