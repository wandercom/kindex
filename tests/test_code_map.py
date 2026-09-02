"""Tests for code-map interop import/export."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.code_map import (
    _relative_to_root,
    export_understand_anything,
    ingest_understand_anything,
)
from kindex.config import Config
from kindex.store import Store


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", str(data_dir)])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_export_understand_anything_projection(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        store.add_node(
            "src/app.py",
            content="Module src/app.py\nDefines AppService.",
            node_id="code-mod-demo-app",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={
                "relative_path": "src/app.py",
                "repo_root": str(tmp_path),
                "language": "Python",
                "class_count": 1,
                "function_count": 3,
                "tool_tier": "C",
            },
        )
        store.add_node(
            "AppService",
            content="class AppService\nPublic methods: run",
            node_id="code-sym-demo-appservice",
            node_type="concept",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={
                "relative_path": "src/app.py",
                "repo_root": str(tmp_path),
                "language": "Python",
                "kind": "class",
                "public_methods": ["run"],
            },
        )
        store.add_edge(
            "code-sym-demo-appservice",
            "code-mod-demo-app",
            edge_type="context_of",
            weight=0.6,
            provenance="test",
            bidirectional=False,
        )

        graph = export_understand_anything(
            store,
            directory=tmp_path,
            project_name="demo",
        )
    finally:
        store.close()

    assert graph["version"] == "1.0.0"
    assert graph["project"]["name"] == "demo"
    assert graph["project"]["languages"] == ["Python"]
    assert {n["id"] for n in graph["nodes"]} == {
        "code-mod-demo-app",
        "code-sym-demo-appservice",
    }
    service = next(n for n in graph["nodes"] if n["name"] == "AppService")
    assert service["type"] == "class"
    assert service["filePath"] == "src/app.py"
    assert graph["edges"][0]["type"] == "contains"
    assert graph["layers"]
    assert graph["tour"]


def test_export_includes_unity_guid_in_language_notes(tmp_path):
    guid = "0123456789abcdef0123456789abcdef"
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        store.add_node(
            "Assets/Player.prefab",
            content="Module Assets/Player.prefab",
            node_id="code-mod-demo-prefab",
            node_type="artifact",
            domains=["code", "unity prefab"],
            prov_activity="code-ingest",
            extra={
                "relative_path": "Assets/Player.prefab",
                "repo_root": str(tmp_path),
                "language": "Unity Prefab",
                "unity_guid": guid,
            },
        )
        store.add_node(
            "src/app.py",
            content="Module src/app.py",
            node_id="code-mod-demo-app",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={
                "relative_path": "src/app.py",
                "repo_root": str(tmp_path),
                "language": "Python",
            },
        )

        graph = export_understand_anything(
            store,
            directory=tmp_path,
            project_name="demo",
        )
    finally:
        store.close()

    by_name = {n["name"]: n for n in graph["nodes"]}
    assert by_name["Assets/Player.prefab"]["languageNotes"]["unityGuid"] == guid
    # Non-Unity nodes must not churn existing code-maps with a new key
    assert "unityGuid" not in by_name["src/app.py"]["languageNotes"]


def test_export_understand_anything_filters_by_repo_root(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    try:
        store.add_node(
            "src/a.py",
            node_id="code-mod-a",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": "src/a.py", "repo_root": str(repo_a)},
        )
        store.add_node(
            "src/b.py",
            node_id="code-mod-b",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": "src/b.py", "repo_root": str(repo_b)},
        )

        graph = export_understand_anything(store, directory=repo_a)
    finally:
        store.close()

    assert [n["id"] for n in graph["nodes"]] == ["code-mod-a"]


def test_export_understand_anything_normalizes_absolute_provenance(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    source = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-app",
            node_type="concept",
            domains=["code", "python"],
            prov_source=f"{source}:22",
            prov_activity="code-ingest",
            extra={"repo_root": str(repo), "kind": "class"},
        )

        graph = export_understand_anything(store, directory=repo)
    finally:
        store.close()

    assert [n["filePath"] for n in graph["nodes"]] == ["src/app.py:22"]


def test_export_recovers_in_repo_absolute_and_never_emits_absolute(tmp_path):
    """The Wander bug: nodes with an absolute prov_source INSIDE the repo were
    either leaked as absolute (old serializer) or dropped (no root). Given the
    git root, they must be RECOVERED as repo-relative — and no emitted filePath
    may ever be absolute."""
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    try:
        # in-repo absolute provenance (the 294-node Wander pattern) -> recovered
        store.add_node(
            "InRepo", node_id="code-sym-demo-in", node_type="concept",
            domains=["code"], prov_source=f"{repo}/src/a.py:1",
            extra={"repo_root": str(repo), "kind": "class"},
        )
        # absolute provenance OUTSIDE the repo -> dropped, never leaked
        store.add_node(
            "OutRepo", node_id="code-sym-demo-out", node_type="concept",
            domains=["code"], prov_source=f"{tmp_path}/other/b.py:2",
            extra={"repo_root": str(repo), "kind": "class"},
        )
        # already-relative, no repo_root -> a portable node, kept under the filter
        store.add_node(
            "Rel", node_id="code-mod-demo-rel", node_type="artifact",
            domains=["code"], extra={"relative_path": "src/c.py"},
        )
        graph = export_understand_anything(store, directory=repo)
    finally:
        store.close()

    paths = [n["filePath"] for n in graph["nodes"]]
    assert all(not p.startswith("/") for p in paths), paths       # THE INVARIANT
    assert "src/a.py:1" in paths      # in-repo absolute recovered as repo-relative
    assert "src/c.py" in paths        # portable relative node kept under root filter
    assert not any("other" in p for p in paths)   # out-of-repo absolute dropped


def test_export_keeps_portable_relative_node_under_root_filter(tmp_path):
    """A node already in portable form (relative_path, no repo_root) must not be
    dropped just because a root filter is active — it carries no evidence of
    belonging to another repo and is already the exact shape we require."""
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    try:
        store.add_node(
            "App", node_id="code-mod-demo-app", node_type="artifact",
            domains=["code"], extra={"relative_path": "src/app.py"},  # no repo_root
        )
        graph = export_understand_anything(store, directory=repo)
    finally:
        store.close()
    assert [n["filePath"] for n in graph["nodes"]] == ["src/app.py"]


def test_export_understand_anything_omits_unresolved_absolute_provenance(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    source = tmp_path / "outside" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-outside",
            node_type="concept",
            domains=["code", "python"],
            prov_source=str(source),
            prov_activity="code-ingest",
            extra={"kind": "class"},
        )

        graph = export_understand_anything(store)
    finally:
        store.close()

    assert graph["nodes"] == []


def test_export_understand_anything_warns_when_omitting_unportable_path(
    tmp_path,
    caplog,
):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    source = tmp_path / "outside" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-unportable",
            node_type="concept",
            domains=["code", "python"],
            prov_source=str(source),
            prov_activity="code-ingest",
            extra={"repo_root": str(repo), "kind": "class"},
        )

        with caplog.at_level(logging.WARNING, logger="kindex.code_map"):
            graph = export_understand_anything(store, directory=repo)
    finally:
        store.close()

    assert graph["nodes"] == []
    assert "Skipping code-map node with non-portable path" in caplog.text
    assert "outside_repo_root" in caplog.text
    assert str(source) not in caplog.text


def test_export_understand_anything_rejects_symlink_outside_repo(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    source = tmp_path / "outside" / "app.py"
    link = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    link.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-symlink",
            node_type="concept",
            domains=["code", "python"],
            prov_source=str(link),
            prov_activity="code-ingest",
            extra={"repo_root": str(repo), "kind": "class"},
        )

        graph = export_understand_anything(store, directory=repo)
    finally:
        store.close()

    assert graph["nodes"] == []


def test_export_understand_anything_normalizes_windows_relative_separators(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-windows",
            node_type="concept",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": r"src\app.py", "kind": "class"},
        )

        graph = export_understand_anything(store)
    finally:
        store.close()

    assert [n["filePath"] for n in graph["nodes"]] == ["src/app.py"]


def test_relative_to_root_handles_windows_paths_without_host_os_dependency():
    assert _relative_to_root(
        r"C:\repo\src\app.py",
        Path(r"C:\repo"),
    ) == "src/app.py"


def test_relative_to_root_handles_unc_paths_without_host_os_dependency():
    assert _relative_to_root(
        r"\\server\share\repo\src\app.py",
        Path(r"\\server\share\repo"),
    ) == "src/app.py"


def test_export_understand_anything_excludes_archived_nodes_by_default(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    try:
        store.add_node(
            "ActiveService",
            node_id="code-sym-demo-active",
            node_type="concept",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": "src/active.py", "repo_root": str(repo)},
        )
        store.add_node(
            "ArchivedService",
            node_id="code-sym-demo-archived",
            node_type="concept",
            domains=["code", "python"],
            status="archived",
            prov_activity="code-ingest",
            extra={"relative_path": "src/archived.py", "repo_root": str(repo)},
        )

        graph = export_understand_anything(store, directory=repo)
        with_archived = export_understand_anything(
            store,
            directory=repo,
            include_archived=True,
        )
    finally:
        store.close()

    assert [n["id"] for n in graph["nodes"]] == ["code-sym-demo-active"]
    assert [n["id"] for n in with_archived["nodes"]] == [
        "code-sym-demo-active",
        "code-sym-demo-archived",
    ]


def test_export_understand_anything_does_not_mutate_source_provenance(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    repo = tmp_path / "repo"
    source = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    try:
        store.add_node(
            "AppService",
            node_id="code-sym-demo-immutable",
            node_type="concept",
            domains=["code", "python"],
            prov_source=str(source),
            prov_activity="code-ingest",
            extra={"repo_root": str(repo), "kind": "class"},
        )
        before = store.get_node("code-sym-demo-immutable")

        graph = export_understand_anything(store, directory=repo)
        after = store.get_node("code-sym-demo-immutable")
    finally:
        store.close()

    assert graph["nodes"][0]["filePath"] == "src/app.py"
    assert after["prov_source"] == before["prov_source"]
    assert after["extra"] == before["extra"]


def test_export_understand_anything_uses_canonical_ordering_and_source_time(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        store.add_node(
            "src/z.py",
            node_id="code-mod-demo-z",
            node_type="artifact",
            domains=["python", "code"],
            weight=1.0,
            prov_activity="code-ingest",
            extra={"relative_path": "src/z.py", "repo_root": str(tmp_path)},
        )
        store.add_node(
            "src/a.py",
            node_id="code-mod-demo-a",
            node_type="artifact",
            domains=["code", "python"],
            weight=0.1,
            prov_activity="code-ingest",
            extra={"relative_path": "src/a.py", "repo_root": str(tmp_path)},
        )
        store.add_edge(
            "code-mod-demo-z",
            "code-mod-demo-a",
            edge_type="depends_on",
            weight=0.4,
            bidirectional=False,
        )
        store.add_edge(
            "code-mod-demo-a",
            "code-mod-demo-z",
            edge_type="relates_to",
            weight=0.2,
            bidirectional=False,
        )

        graph = export_understand_anything(store, directory=tmp_path)
        graph_again = export_understand_anything(store, directory=tmp_path)
        node_times = [
            store.get_node("code-mod-demo-a")["updated_at"],
            store.get_node("code-mod-demo-z")["updated_at"],
        ]
    finally:
        store.close()

    assert graph == graph_again
    assert [node["id"] for node in graph["nodes"]] == [
        "code-mod-demo-a",
        "code-mod-demo-z",
    ]
    assert graph["edges"] == sorted(
        graph["edges"],
        key=lambda edge: (edge["source"], edge["target"], edge["type"]),
    )
    assert graph["project"]["analyzedAt"] == max(node_times)


def test_ingest_understand_anything_graph(tmp_path):
    graph_dir = tmp_path / "repo" / ".understand-anything"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "knowledge-graph.json"
    graph_path.write_text(json.dumps({
        "project": {"name": "demo", "languages": ["Python"]},
        "nodes": [
            {
                "id": "file:src/app.py",
                "type": "file",
                "name": "src/app.py",
                "filePath": "src/app.py",
                "summary": "Application module.",
                "tags": ["python"],
                "complexity": 2,
            },
            {
                "id": "class:src/app.py:AppService",
                "type": "class",
                "name": "AppService",
                "filePath": "src/app.py",
                "summary": "Runs the app.",
                "tags": ["service"],
                "complexity": 3,
            },
        ],
        "edges": [
            {
                "source": "class:src/app.py:AppService",
                "target": "file:src/app.py",
                "type": "contains",
                "weight": 0.8,
            },
        ],
        "layers": [],
        "tour": [],
    }))

    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    try:
        result = ingest_understand_anything(store, tmp_path / "repo")
        assert result.created == 2
        nodes = store.all_nodes(tags=["understand-anything"], limit=10)
        assert len(nodes) == 2
        app_service = next(n for n in nodes if n["title"] == "AppService")
        assert app_service["extra"]["source"] == "understand-anything"
        assert app_service["extra"]["relative_path"] == "src/app.py"
        edges = store.edges_from(app_service["id"])
        assert len(edges) == 1
        assert edges[0]["type"] == "context_of"
    finally:
        store.close()


def test_cli_export_code_map(tmp_path):
    data_dir = tmp_path / "data"
    cfg = Config(data_dir=str(data_dir))
    store = Store(cfg)
    try:
        store.add_node(
            "src/app.py",
            node_id="code-mod-cli-app",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": "src/app.py", "language": "Python"},
        )
    finally:
        store.close()

    r = run(
        "export",
        "code-map",
        "--format",
        "understand-anything",
        "--project-name",
        "cli-demo",
        data_dir=data_dir,
    )
    assert r.returncode == 0, r.stderr
    graph = json.loads(r.stdout)
    assert graph["project"]["name"] == "cli-demo"
    assert graph["nodes"][0]["id"] == "code-mod-cli-app"


def test_adapter_is_registered():
    from kindex.adapters.registry import discover

    assert "understand-anything" in discover()
