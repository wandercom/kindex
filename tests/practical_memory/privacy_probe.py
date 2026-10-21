"""Synthetic contracts ported from the frozen practical-memory campaign."""
from contextlib import contextmanager
import copy
import json
from unittest import mock

from kindex import privacy


ORIGINAL_REDACT = privacy.redact
ORIGINAL_SERIALIZED = privacy.redact_serialized
SURFACE = None
HITS = 0


def marked(value):
    global HITS
    if SURFACE is None or not isinstance(value, dict) or "base_text" not in value or "optional_text" not in value:
        return value
    texts = [value[key] for key in ("base_text", "optional_text") if value[key]]
    payloads = []
    for text in texts:
        try:
            payloads.append(json.loads(text))
        except (ValueError, TypeError):
            return value
    if not any(isinstance(item, dict) and item.get("sources") for item in payloads):
        return value
    compact = any("metadata_encoding" in item for item in payloads)
    if (SURFACE == "compact" and compact) or (SURFACE == "restored" and not compact):
        HITS += 1
        changed = copy.deepcopy(value)
        changed["schema"] = "synthetic-privacy-rejection"
        return changed
    return value


def redact(value):
    return marked(ORIGINAL_REDACT(value))


def redact_serialized(text):
    clean = ORIGINAL_SERIALIZED(text)
    try:
        value = json.loads(clean)
    except (ValueError, TypeError):
        return clean
    changed = marked(value)
    if changed is value:
        return clean
    return json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)




@contextmanager
def reject_surface(surface):
    global SURFACE, HITS
    if surface not in {"compact", "restored"} or SURFACE is not None:
        raise ValueError("Invalid public privacy fixture activation")
    SURFACE, HITS = surface, 0
    from kindex import correction_references
    try:
        with mock.patch.object(privacy, "redact", redact), mock.patch.object(privacy, "redact_serialized", redact_serialized), mock.patch.object(correction_references, "redact_serialized", redact_serialized):
            yield
    finally:
        SURFACE = None
