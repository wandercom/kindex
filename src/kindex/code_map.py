"""Code-map import/export projections.

Kindex keeps its own graph schema internally. This module provides a small
interop layer for dashboard-oriented code-map JSON, including the shape used by
Understand-Anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .adapters.base import IngestResult

if TYPE_CHECKING:
    from .store import Store


UA_VERSION = "1.0.0"
_LINE_SUFFIX_RE = re.compile(r"^(?P<path>.*):(?P<line>\d+)$")
logger = logging.getLogger(__name__)


def _sha(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def _git_commit(directory: Path | None) -> str:
    if not directory:
        return ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _git_commit_time(directory: Path | None) -> str:
    if not directory:
        return ""
    try:
        r = subprocess.run(
            ["git", "show", "-s", "--format=%cI", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _node_time(node: dict) -> str:
    return str(
        node.get("updated_at")
        or node.get("prov_when")
        or node.get("created_at")
        or ""
    )


def _latest_node_time(nodes: list[dict]) -> str:
    return max((_node_time(n) for n in nodes), default="")


def _node_is_code(node: dict) -> bool:
    domains = set(node.get("domains") or [])
    extra = node.get("extra") or {}
    return (
        "code" in domains
        or bool(extra.get("relative_path"))
        or node.get("prov_activity") == "code-ingest"
    )


def _node_matches_root(node: dict, root: Path | None) -> bool:
    """Return whether a code node belongs to the requested repo root."""
    if root is None:
        return True

    extra = node.get("extra") or {}
    repo_root = extra.get("repo_root")
    if repo_root:
        try:
            if Path(repo_root).expanduser().resolve() == root:
                return True
        except OSError:
            pass

    for candidate in (
        extra.get("relative_path"),
        node.get("prov_source"),
    ):
        if not candidate:
            continue
        path, _ = _split_location(str(candidate))
        if _is_absolute_path(path) and _relative_to_root(path, root) is not None:
            return True

    # A node already in portable form — a repo-relative ``relative_path`` with no
    # ``repo_root`` and no absolute provenance — carries no evidence of belonging to
    # a DIFFERENT repo. Include it in the active export rather than dropping it: it
    # is already the exact relative shape the tracked artifact requires, and an
    # explicit root filter should not silently discard portable, unclaimed nodes.
    if not repo_root and not _is_absolute_path(_split_location(str(node.get("prov_source") or ""))[0]):
        rel = extra.get("relative_path")
        if rel and not _is_absolute_path(_split_location(str(rel))[0]):
            return True

    return False


def _split_location(value: str) -> tuple[str, str]:
    match = _LINE_SUFFIX_RE.match(value)
    if not match:
        return value, ""
    return match.group("path"), f":{match.group('line')}"


def _is_windows_absolute(value: str) -> bool:
    return PureWindowsPath(value).is_absolute()


def _is_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() or _is_windows_absolute(value)


def _relative_to_root(value: str, root: Path) -> str | None:
    if _is_windows_absolute(value) or _is_windows_absolute(str(root)):
        try:
            return PureWindowsPath(value).relative_to(
                PureWindowsPath(str(root)),
            ).as_posix()
        except ValueError:
            return None

    try:
        return Path(value).expanduser().resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _normalize_relative_path(value: str) -> str | None:
    if not value or not value.strip():
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or PureWindowsPath(normalized).drive
        or ".." in path.parts
    ):
        return None
    result = path.as_posix()
    return None if result == "." else result


def _node_location(node: dict) -> tuple[str, str]:
    extra = node.get("extra") or {}
    relative_path = extra.get("relative_path")
    if relative_path:
        return str(relative_path), ""
    return _split_location(str(node.get("prov_source") or ""))


def _effective_root(node: dict, root: Path | None) -> Path | None:
    if root is not None:
        return root
    extra = node.get("extra") or {}
    effective_root = root
    if extra.get("repo_root"):
        try:
            effective_root = Path(str(extra["repo_root"])).expanduser().resolve()
        except OSError:
            effective_root = None
    return effective_root


def _portable_path(node: dict, root: Path | None) -> str | None:
    """Return a repo-relative POSIX location suitable for tracked output."""
    path, suffix = _node_location(node)
    effective_root = _effective_root(node, root)

    if _is_absolute_path(path):
        if effective_root is None:
            return None
        relative = _relative_to_root(path, effective_root)
    else:
        relative = _normalize_relative_path(path)

    if not relative:
        return None
    return f"{relative}{suffix}"


def _unportable_path_reason(node: dict, root: Path | None) -> str:
    path, _ = _node_location(node)
    if not path or not path.strip():
        return "missing_path"
    if _is_absolute_path(path):
        if _effective_root(node, root) is None:
            return "missing_repo_root"
        return "outside_repo_root"
    return "invalid_relative_path"


def _warn_unportable_node(node: dict, root: Path | None) -> None:
    reason = _unportable_path_reason(node, root)
    logger.warning(
        "Skipping code-map node with non-portable path: %s",
        reason,
        extra={
            "node_id": node.get("id", ""),
            "reason": reason,
        },
    )


def _node_status(node: dict) -> str:
    return str(node.get("status") or "active")


def _node_is_exportable(node: dict, include_archived: bool) -> bool:
    status = _node_status(node)
    return status == "active" or (include_archived and status == "archived")


def _ua_node_type(node: dict) -> str:
    if node.get("type") == "artifact":
        return "file"
    extra = node.get("extra") or {}
    kind = str(extra.get("kind", "")).lower()
    if any(k in kind for k in ("class", "struct", "interface", "trait", "enum")):
        return "class"
    if "function" in kind or "method" in kind:
        return "function"
    return "concept"


def _canonical_code_node_key(
    node: dict,
    root: Path | None = None,
) -> tuple[str, str, str]:
    rel_path = _portable_path(node, root) or ""
    return (str(rel_path), _ua_node_type(node), str(node.get("id", "")))


def _summary(node: dict) -> str:
    content = (node.get("content") or "").strip()
    if not content:
        return ""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return " ".join(lines[:3])[:600]


def _complexity(node: dict) -> int:
    extra = node.get("extra") or {}
    if "complexity" in extra:
        try:
            return int(extra["complexity"])
        except (TypeError, ValueError):
            return 1
    return max(
        1,
        int(extra.get("class_count") or 0)
        + int(extra.get("function_count") or 0)
        + len(extra.get("public_methods") or []),
    )


def _layer_for_path(path: str, node_type: str) -> str:
    lowered = path.lower()
    if node_type == "class":
        return "domain"
    if any(part in lowered for part in ("api", "route", "router", "endpoint", "controller")):
        return "api"
    if any(part in lowered for part in ("service", "workflow", "usecase", "use_case")):
        return "service"
    if any(part in lowered for part in ("model", "schema", "store", "repo", "repository", "db", "database", "migration")):
        return "data"
    if any(part in lowered for part in ("component", "view", "page", "ui", "frontend")):
        return "ui"
    if any(part in lowered for part in ("test", "spec")):
        return "test"
    return "core"


_EDGE_MAP = {
    "depends_on": "depends_on",
    "implements": "depends_on",
    "context_of": "contains",
    "relates_to": "related",
    "blocks": "related",
    "answers": "related",
    "contradicts": "related",
    "spawned_from": "related",
    "supersedes": "related",
    "exemplifies": "related",
}


def export_understand_anything(
    store: "Store",
    *,
    directory: str | Path | None = None,
    project_name: str | None = None,
    limit: int = 10000,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Project Kindex code nodes into an Understand-Anything-compatible graph.

    Tracked code-map output is intentionally portable: file paths are always
    repo-relative POSIX paths and never raw machine-local provenance paths.
    Active nodes are exported by default; callers may opt into archived nodes
    for historical analysis.

    The relativization root is the repository top-level passed as ``directory``.
    The CLI (`kin export code-map`) defaults it to the git root of the cwd so a
    tracked .kin/code-map.json is always relative to that repo's root; when no
    root is available, only nodes carrying a repo-relative ``extra.relative_path``
    are emitted and absolute provenance is dropped rather than leaked.
    """
    root = Path(directory).resolve() if directory else None
    all_nodes = store.all_nodes(limit=limit)
    code_nodes = []
    for node in all_nodes:
        if not _node_is_code(node) or not _node_is_exportable(
            node,
            include_archived,
        ):
            continue
        if not _node_matches_root(node, root):
            continue
        if not _portable_path(node, root):
            _warn_unportable_node(node, root)
            continue
        code_nodes.append(node)
    code_nodes.sort(key=lambda node: _canonical_code_node_key(node, root))
    code_ids = {n["id"] for n in code_nodes}

    if project_name is None:
        project_name = root.name if root else "kindex-code-map"

    ua_nodes = []
    layer_members: dict[str, list[str]] = {}
    languages: set[str] = set()

    for node in code_nodes:
        extra = node.get("extra") or {}
        rel_path = _portable_path(node, root) or ""
        language = extra.get("language") or next(
            (d for d in node.get("domains", []) if d != "code"),
            "",
        )
        if language:
            languages.add(str(language))
        ua_type = _ua_node_type(node)
        layer = _layer_for_path(str(rel_path), ua_type)
        layer_members.setdefault(layer, []).append(node["id"])
        # Unity assets reference each other by GUID, so exposing the GUID
        # alongside filePath gives consumers the GUID->path mapping. Kept
        # per-node (not a top-level map) because the kin merge driver only
        # unions known top-level keys.
        language_notes = {
            "language": language,
            "kindexType": node.get("type", ""),
            "toolTier": extra.get("tool_tier", ""),
        }
        if extra.get("unity_guid"):
            language_notes["unityGuid"] = extra["unity_guid"]
        ua_nodes.append({
            "id": node["id"],
            "type": ua_type,
            "name": node.get("title", ""),
            "filePath": rel_path,
            "summary": _summary(node),
            "tags": sorted(set(node.get("domains") or [])),
            "complexity": _complexity(node),
            "languageNotes": language_notes,
        })

    ua_edges = []
    seen_edges: set[tuple[str, str, str]] = set()
    for node in code_nodes:
        for edge in store.edges_from(node["id"]):
            target = edge.get("to_id")
            if target not in code_ids:
                continue
            edge_type = _EDGE_MAP.get(edge.get("type", ""), "related")
            key = (node["id"], target, edge_type)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            ua_edges.append({
                "source": node["id"],
                "target": target,
                "type": edge_type,
                "direction": "outbound",
                "weight": round(edge.get("weight", 0.5), 2),
            })
    ua_edges.sort(key=lambda e: (e["source"], e["target"], e["type"]))

    layers = [
        {
            "id": layer_id,
            "name": layer_id.replace("_", " ").title(),
            "description": f"Kindex-inferred {layer_id} layer.",
            "nodeIds": sorted(node_ids),
        }
        for layer_id, node_ids in sorted(layer_members.items())
    ]

    tour = [
        {
            "order": i + 1,
            "title": layer["name"],
            "description": f"Review {len(layer['nodeIds'])} node(s) in the {layer['name']} layer.",
            "nodeIds": layer["nodeIds"][:25],
        }
        for i, layer in enumerate(layers)
    ]

    return {
        "version": UA_VERSION,
        "project": {
            "name": project_name,
            "description": f"Code map exported from Kindex for {project_name}.",
            "languages": sorted(languages),
            "frameworks": [],
            "analyzedAt": _git_commit_time(root) or _latest_node_time(code_nodes),
            "gitCommitHash": _git_commit(root),
        },
        "nodes": ua_nodes,
        "edges": ua_edges,
        "layers": layers,
        "tour": tour,
    }


def _ua_kindex_id(source_id: str) -> str:
    return f"ua-{_sha(source_id)}"


def _resolve_ua_graph(path_or_directory: str | Path) -> Path:
    path = Path(path_or_directory).expanduser().resolve()
    if path.is_dir():
        path = path / ".understand-anything" / "knowledge-graph.json"
    return path


def ingest_understand_anything(
    store: "Store",
    path_or_directory: str | Path,
    *,
    limit: int = 10000,
    verbose: bool = False,
) -> IngestResult:
    """Ingest an Understand-Anything knowledge-graph.json into Kindex."""
    graph_path = _resolve_ua_graph(path_or_directory)
    if not graph_path.exists():
        return IngestResult(errors=[f"Understand-Anything graph not found: {graph_path}"])

    try:
        graph = json.loads(graph_path.read_text())
    except json.JSONDecodeError as e:
        return IngestResult(errors=[f"Invalid JSON in {graph_path}: {e}"])

    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    project = graph.get("project") or {}

    created = updated = skipped = edges_created = 0
    id_map: dict[str, str] = {}

    for item in nodes[:limit]:
        source_id = str(item.get("id") or "")
        if not source_id:
            skipped += 1
            continue
        node_id = _ua_kindex_id(source_id)
        id_map[source_id] = node_id

        title = item.get("name") or item.get("filePath") or source_id
        content = item.get("summary") or ""
        tags = ["code", "understand-anything"] + list(item.get("tags") or [])
        extra = {
            "source": "understand-anything",
            "source_id": source_id,
            "source_type": item.get("type", ""),
            "relative_path": item.get("filePath", ""),
            "complexity": item.get("complexity", 1),
            "project": project.get("name", ""),
            "language_notes": item.get("languageNotes", {}),
        }
        node_type = "artifact" if item.get("type") in ("file", "module") else "concept"

        existing = store.get_node(node_id)
        if existing:
            store.update_node(
                node_id,
                title=title,
                content=content,
                domains=sorted(set(tags)),
                extra=extra,
                prov_source=str(graph_path),
            )
            updated += 1
        else:
            store.add_node(
                title=title,
                content=content,
                node_id=node_id,
                node_type=node_type,
                domains=sorted(set(tags)),
                prov_activity="understand-anything-import",
                prov_source=str(graph_path),
                extra=extra,
            )
            created += 1

    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        from_id = id_map.get(source)
        to_id = id_map.get(target)
        if not from_id or not to_id:
            continue
        edge_type = edge.get("type") or "related"
        if edge_type in ("imports", "depends_on", "calls"):
            kindex_type = "depends_on" if edge_type != "calls" else "relates_to"
        elif edge_type == "contains":
            kindex_type = "context_of"
        else:
            kindex_type = "relates_to"
        store.add_edge(
            from_id,
            to_id,
            edge_type=kindex_type,
            weight=float(edge.get("weight", 0.5) or 0.5),
            provenance=f"understand-anything import: {edge_type}",
            bidirectional=False,
        )
        edges_created += 1

    if verbose:
        print(f"  Imported {created} created, {updated} updated, {edges_created} edges from {graph_path}")

    return IngestResult(created=created, updated=updated, skipped=skipped)
