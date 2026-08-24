"""Structured 3-way merge for git-tracked ``.kin`` artifacts.

``.kin/index.json`` and ``.kin/code-map.json`` are generated, id-keyed JSON
snapshots. Git's line-based merge conflicts on them needlessly — they are sorted
node lists, so the correct merge is a structured union keyed by node id, not a
textual 3-way diff. This module powers the ``kin merge-kin`` git merge driver.

Why a union rather than "regenerate from the graph": ``index.json`` projects the
local SQLite DB, which is NOT in git. Regenerating from one machine's DB would
silently drop the *other* branch's concept/decision nodes (that DB never ingested
them). A union of the two committed files is lossless across machines. The result
is byte-identical to what ``kin index`` would emit for the merged node set, so a
later regeneration produces no spurious diff.

``code-map.json`` projects the code (which IS in the merge tree), so its content
collections are unioned here too; ``kin code-map`` is the canonical refresh for
the commit-tied ``project`` metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

# Schema version of the ``.kin/index.json`` artifact this driver understands.
# v1: original header {domains, node_count, nodes, repo, version}.
# v2: unknown TOP-LEVEL fields are preserved verbatim by the merge driver and
#     nodes may carry additional fields (e.g. referent binding); consumers must
#     ignore fields they do not understand. ``ingest.write_kin_index`` emits
#     this version, keeping writer and merger single-sourced.
KIN_INDEX_SCHEMA_VERSION = 2

# Top-level index fields this driver recomputes or assigns itself; everything
# else on either side passes through the 3-way field merge.
_INDEX_OWNED_FIELDS = frozenset({"domains", "node_count", "nodes", "repo", "version"})

# Legacy volatile fields old writers emitted; deliberately never resurrected
# by the merge (they churn git history — see write_kin_index's NB comment).
_INDEX_DROPPED_FIELDS = frozenset({"source_updated_at"})

# Top-level code-map fields the code-map merger owns (recomputed/assigned).
_CODE_MAP_OWNED_FIELDS = frozenset(
    {"version", "project", "nodes", "edges", "layers", "tour"}
)


class UnsupportedKinSchemaError(ValueError):
    """A ``.kin`` side declares a schema version newer than this driver.

    Raised so the git driver can DECLINE the merge (normal conflict fallback)
    instead of silently rewriting — and thereby corrupting — a newer-schema
    file it cannot faithfully merge. Fail-closed by construction.
    """


def load_json(path: str | Path) -> dict | None:
    """Load a ``.kin`` side for merging.

    Returns ``None`` for an absent/empty side (git passes an empty file when a
    file exists on only one branch). Raises ``ValueError`` on non-empty invalid
    JSON so the driver can decline and let git fall back to a normal conflict.
    """
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text()
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    return data if isinstance(data, dict) else None


def dumps_kin(obj: dict) -> str:
    """Serialize ``index.json`` exactly as ``write_kin_index`` does (sort_keys),
    so a later ``kin index`` regeneration yields no diff."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def dumps_code_map(obj: dict) -> str:
    """Serialize ``code-map.json`` exactly as ``kin export code-map`` does — no
    ``sort_keys`` (insertion-order keys), matching the canonical exporter so a
    later regeneration yields no diff. The merge reuses the original node/layer
    dicts, so their key order is already the exporter's."""
    return json.dumps(obj, indent=2) + "\n"


def _three_way_union(
    base: list[dict] | None,
    ours: list[dict] | None,
    theirs: list[dict] | None,
    key: Callable[[dict], Any],
    pick: Callable[[dict, dict], dict] | None = None,
) -> dict[Any, dict]:
    """3-way set merge of id-keyed item lists.

    Union of ``ours`` and ``theirs``; on a key present in both, ``pick`` chooses
    (default: keep ``ours``). ``base`` is used to honor deletions: an item present
    on exactly one side and unchanged there from base was deleted on the other
    side, so it is dropped. Returns a key -> item dict.
    """
    om = {key(x): x for x in (base or [])}
    am = {key(x): x for x in (ours or [])}
    bm = {key(x): x for x in (theirs or [])}
    out: dict[Any, dict] = {}
    for k in set(am) | set(bm):
        xa, xb = am.get(k), bm.get(k)
        if xa is not None and xb is not None:
            out[k] = pick(xa, xb) if pick else xa
        elif xa is not None:
            if k in om and xa == om[k]:
                continue  # unchanged on ours, deleted on theirs
            out[k] = xa
        else:
            if k in om and xb == om[k]:
                continue  # unchanged on theirs, deleted on ours
            out[k] = xb
    return out


def _index_version(doc: dict | None) -> int:
    """Validated integer schema version of one index side (absent side -> 1).

    A non-integer version or one newer than this driver raises
    ``UnsupportedKinSchemaError``: both mean "written by something this driver
    does not understand", and guessing would risk exactly the silent field
    corruption the version marker exists to prevent.
    """
    if doc is None:
        return 1
    version = doc.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise UnsupportedKinSchemaError(
            f"unrecognized .kin index schema version: {version!r}"
        )
    if version > KIN_INDEX_SCHEMA_VERSION:
        raise UnsupportedKinSchemaError(
            f".kin index schema version {version} is newer than this driver "
            f"(understands <= {KIN_INDEX_SCHEMA_VERSION})"
        )
    return version


def _merge_passthrough_fields(
    out: dict,
    base: dict | None,
    ours: dict | None,
    theirs: dict | None,
    *,
    owned: frozenset[str],
    dropped: frozenset[str] = frozenset(),
) -> None:
    """3-way merge unknown top-level fields into ``out`` (in place).

    Same semantics as the node union, applied per field: a field present on
    both sides keeps ours unless only theirs changed it vs base; a field on
    one side survives unless it is unchanged from base (deleted on the other
    side). Fields in ``owned`` are the merger's own output; fields in
    ``dropped`` are legacy volatile fields that must never resurrect.
    """
    o, a, b = base or {}, ours or {}, theirs or {}
    for key in sorted((set(a) | set(b)) - owned - dropped):
        in_ours, in_theirs = key in a, key in b
        if in_ours and in_theirs:
            if a[key] == b[key]:
                out[key] = a[key]
            elif key in o and a[key] == o[key]:
                out[key] = b[key]  # only theirs changed it
            else:
                out[key] = a[key]  # ours changed (or no base): ours wins
        elif in_ours:
            if key in o and a[key] == o[key]:
                continue  # unchanged on ours, deleted on theirs
            out[key] = a[key]
        else:
            if key in o and b[key] == o[key]:
                continue  # unchanged on theirs, deleted on ours
            out[key] = b[key]


def _newer(a: dict, b: dict) -> dict:
    """Pick the node with the later ``updated_at`` (ISO strings sort lexically).

    On an exact timestamp tie, ``a`` (ours) wins — deterministic per merge but
    direction-dependent. Acceptable: the snapshot is advisory and a later
    ``kin index`` from the authoritative DB overwrites it regardless.
    """
    return b if str(b.get("updated_at", "")) > str(a.get("updated_at", "")) else a


def merge_index(
    base: dict | None, ours: dict | None, theirs: dict | None
) -> dict:
    """Union ``.kin/index.json`` node sets; recompute the derived header.

    Unknown top-level fields pass through the 3-way field merge so a newer
    writer's fields survive this driver; a side declaring a schema version
    newer than this driver raises ``UnsupportedKinSchemaError`` instead of
    being silently rewritten.
    """
    versions = [_index_version(side) for side in (base, ours, theirs)]
    head = ours or theirs or {}
    merged = _three_way_union(
        (base or {}).get("nodes"),
        (ours or {}).get("nodes"),
        (theirs or {}).get("nodes"),
        key=lambda n: n["id"],
        pick=_newer,
    )
    nodes = [merged[k] for k in sorted(merged)]
    out = {
        "domains": sorted({d for n in nodes for d in (n.get("domains") or [])}),
        "node_count": len(nodes),
        "nodes": nodes,
        "repo": head.get("repo"),
        # The union carries the newer side's fields, so it is the newer schema.
        "version": max(versions[1:]) if (ours or theirs) else 1,
    }
    _merge_passthrough_fields(
        out, base, ours, theirs,
        owned=_INDEX_OWNED_FIELDS, dropped=_INDEX_DROPPED_FIELDS,
    )
    return out


def merge_code_map(
    base: dict | None, ours: dict | None, theirs: dict | None
) -> dict:
    """Union ``.kin/code-map.json`` content collections.

    Nodes/edges are unioned (lossless across branches); layers union their
    members; ``tour`` is recomputed from the merged layers. ``project`` keeps
    ours' commit-tied metadata (``kin code-map`` refreshes it) but unions the
    detected ``languages``.
    """
    o, a, b = base or {}, ours or {}, theirs or {}

    node_map = _three_way_union(
        o.get("nodes"), a.get("nodes"), b.get("nodes"), key=lambda n: n["id"]
    )
    # Match the exporter's _canonical_code_node_key order: (filePath, type, id).
    nodes = sorted(
        node_map.values(),
        key=lambda n: (n.get("filePath", ""), n.get("type", ""), n.get("id", "")),
    )

    edge_map = _three_way_union(
        o.get("edges"), a.get("edges"), b.get("edges"),
        key=lambda e: (e.get("source"), e.get("target"), e.get("type")),
    )
    edges = sorted(
        edge_map.values(),
        key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("type", "")),
    )

    # Layers: union by id, union member node ids.
    layers_by_id: dict[Any, dict] = {}
    for layer in (a.get("layers") or []) + (b.get("layers") or []):
        lid = layer.get("id")
        existing = layers_by_id.get(lid)
        if existing is None:
            layers_by_id[lid] = {**layer, "nodeIds": list(layer.get("nodeIds") or [])}
        else:
            existing["nodeIds"] = sorted(
                set(existing["nodeIds"]) | set(layer.get("nodeIds") or [])
            )
    present_ids = {n["id"] for n in nodes}
    layers = []
    for lid in sorted(layers_by_id, key=lambda k: str(k)):
        layer = layers_by_id[lid]
        members = sorted(nid for nid in layer.get("nodeIds") or [] if nid in present_ids)
        layers.append({**layer, "nodeIds": members})

    tour = [
        {
            "order": i + 1,
            "title": layer.get("name"),
            "description": f"Review {len(layer['nodeIds'])} node(s) in the {layer.get('name')} layer.",
            "nodeIds": layer["nodeIds"][:25],
        }
        for i, layer in enumerate(layers)
    ]

    project = dict(a.get("project") or b.get("project") or {})
    langs = set(project.get("languages") or [])
    langs |= set((b.get("project") or {}).get("languages") or [])
    project["languages"] = sorted(langs)

    out = {
        # code-map version is the exporter-owned UA semver string (kin export
        # code-map refreshes it); no numeric future-guard applies here.
        "version": a.get("version") or b.get("version"),
        "project": project,
        "nodes": nodes,
        "edges": edges,
        "layers": layers,
        "tour": tour,
    }
    _merge_passthrough_fields(out, base, ours, theirs, owned=_CODE_MAP_OWNED_FIELDS)
    return out


# Dispatch by the in-repo filename git passes as %P. Each artifact has its own
# serializer matching its canonical writer so a post-merge regeneration is a no-op.
_MERGERS: dict[str, Callable[[dict | None, dict | None, dict | None], dict]] = {
    "index.json": merge_index,
    "code-map.json": merge_code_map,
}
_SERIALIZERS: dict[str, Callable[[dict], str]] = {
    "index.json": dumps_kin,
    "code-map.json": dumps_code_map,
}


def merge_for(
    name: str, base: dict | None, ours: dict | None, theirs: dict | None
) -> dict | None:
    """Merge by ``.kin`` filename; ``None`` if the filename is not recognized."""
    merger = _MERGERS.get(Path(name).name)
    if merger is None:
        return None
    return merger(base, ours, theirs)


def merge_kin_files(
    repo_path: str, base_file: str, ours_file: str, theirs_file: str
) -> str | None:
    """Driver entrypoint. Returns merged text to write to the ours (%A) file, or
    ``None`` to decline (unknown file / invalid JSON / newer schema version)
    so git keeps the conflict."""
    if Path(repo_path).name not in _MERGERS:
        return None
    try:
        base = load_json(base_file)
        ours = load_json(ours_file)
        theirs = load_json(theirs_file)
        merged = merge_for(repo_path, base, ours, theirs)
    except ValueError:
        # Invalid JSON, or a schema version newer than this driver
        # (UnsupportedKinSchemaError): never rewrite what we cannot
        # faithfully merge — decline and let git keep the conflict.
        return None
    if merged is None:
        return None
    return _SERIALIZERS[Path(repo_path).name](merged)
