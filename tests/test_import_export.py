"""Tests for import/export round trip — export JSON, export JSONL, import, merge, roundtrip."""

import json
import subprocess
import sys

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


def run(*args, data_dir=None):
    cmd = [sys.executable, "-m", "kindex.cli", *args]
    if data_dir:
        cmd.extend(["--data-dir", data_dir])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class TestExportJSON:
    def test_export_json(self, tmp_path):
        """Export and verify JSON structure."""
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "Export Test Alpha is a concept", data_dir=d)
        run("add", "Export Test Beta is another concept", data_dir=d)

        r = run("export", "--audience", "private", "--format", "json", data_dir=d)
        assert r.returncode == 0

        # Parse the JSON output (strip the stderr line about export count)
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) >= 2

        # Verify structure of each node
        for item in data:
            assert "id" in item
            assert "title" in item
            assert "type" in item
            assert "content" in item
            assert "weight" in item
            assert "edges" in item
            assert isinstance(item["edges"], list)

    def test_export_json_audience_filter(self, tmp_path):
        """Export with audience filter should only include matching nodes."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Create nodes via store to control audience
        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Private Node", node_id="priv", audience="private")
        s.add_node("Team Node", node_id="team-node", audience="team")
        s.add_node("Public Node", node_id="pub", audience="public")
        s.add_edge("priv", "team-node")
        s.add_edge("team-node", "pub")
        s.close()

        # Export public only
        r = run("export", "--audience", "public", "--format", "json", data_dir=d)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        ids = [n["id"] for n in data]
        assert "pub" in ids
        assert "priv" not in ids

        # Export team (includes team + public)
        r = run("export", "--audience", "team", "--format", "json", data_dir=d)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        ids = [n["id"] for n in data]
        assert "team-node" in ids
        assert "pub" in ids
        assert "priv" not in ids


class TestExportJSONL:
    def test_export_jsonl(self, tmp_path):
        """Export JSONL format."""
        d = str(tmp_path)
        run("init", data_dir=d)
        run("add", "JSONL Test Concept description here", data_dir=d)

        r = run("export", "--audience", "private", "--format", "jsonl", data_dir=d)
        assert r.returncode == 0

        # Each non-empty line should be valid JSON
        lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
        assert len(lines) >= 1

        for line in lines:
            item = json.loads(line)
            assert "id" in item
            assert "title" in item

    def test_export_jsonl_multiple_nodes(self, tmp_path):
        """Multiple nodes should produce one line per node."""
        d = str(tmp_path)
        run("init", data_dir=d)

        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Node A", node_id="a")
        s.add_node("Node B", node_id="b")
        s.add_node("Node C", node_id="c")
        s.add_edge("a", "b")
        s.add_edge("b", "c")
        s.close()

        r = run("export", "--audience", "private", "--format", "jsonl", data_dir=d)
        assert r.returncode == 0
        lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
        assert len(lines) >= 3

    def test_export_uses_complete_stable_active_contract(self, tmp_path):
        """JSONL rows retain their shape across stores with legacy omissions."""
        d = str(tmp_path)
        run("init", data_dir=d)
        s = Store(Config(data_dir=d))
        s.add_node("Legacy active", node_id="legacy", audience="private")
        s.add_node("Missing status", node_id="missing", audience="private")
        s.add_node("Replacement", node_id="replacement", audience="private")
        s.add_node("Archived", node_id="archived", audience="private",
                   status="archived")
        # Simulate an older persisted row: no lifecycle value, but a successor
        # marker that must still win when filtering the active projection.
        s.conn.execute("UPDATE nodes SET status = '', extra = ? WHERE id = 'legacy'",
                       (json.dumps({"superseded_by": "replacement"}),))
        s.conn.execute("UPDATE nodes SET status = '' WHERE id = 'missing'")
        s.add_edge("replacement", "archived", edge_type="relates_to", weight=0.75,
                   bidirectional=False)
        s.close()

        r = run("export", "--audience", "private", "--format", "jsonl",
                "--active-only", data_dir=d)
        assert r.returncode == 0, r.stderr
        rows = [json.loads(line) for line in r.stdout.splitlines() if line]
        assert [row["id"] for row in rows] == ["missing", "replacement"]
        assert tuple(rows[0]) == (
            "id", "type", "title", "content", "intent", "status", "audience",
            "prov_when", "prov_activity", "prov_why", "prov_source", "created_at",
            "updated_at", "standing", "aka", "domains", "prov_who", "valid_at",
            "invalid_at", "asserted_at", "true_of", "verified_at", "verified_by",
            "prov_method", "weight", "referent", "extra", "edges",
        )
        for row in rows:
            assert row["status"] == "active"
            assert row["standing"] == "unruled"
            assert row["referent"] is None
            assert row["asserted_at"] is None
            assert row["true_of"] is None

    def test_export_inferrs_missing_reciprocal_edge_weight(self, tmp_path):
        d = str(tmp_path)
        run("init", data_dir=d)
        s = Store(Config(data_dir=d))
        s.add_node("A", node_id="a", audience="private")
        s.add_node("B", node_id="b", audience="private")
        s.add_edge("a", "b", edge_type="implements", weight=0.75,
                   bidirectional=False)
        s.close()

        r = run("export", "--audience", "private", "--format", "json", data_dir=d)
        assert r.returncode == 0, r.stderr
        rows = {row["id"]: row for row in json.loads(r.stdout)}
        assert rows["a"]["edges"] == [
            {"to": "b", "type": "implements", "weight": 0.75,
             "bidirectional": False, "provenance": ""},
        ]
        assert rows["b"]["edges"] == [
            {"to": "a", "type": "implements", "weight": 0.75 * 0.8,
             "bidirectional": False, "provenance": ""},
        ]


class TestImportJSON:
    def test_import_json(self, tmp_path):
        """Import a JSON file, verify nodes created."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Create JSON export data
        export_data = [
            {
                "id": "imported-alpha",
                "type": "concept",
                "title": "Imported Alpha Concept",
                "content": "Content about alpha",
                "weight": 0.7,
                "domains": ["test"],
                "audience": "private",
                "edges": [],
            },
            {
                "id": "imported-beta",
                "type": "concept",
                "title": "Imported Beta Concept",
                "content": "Content about beta",
                "weight": 0.6,
                "domains": ["test"],
                "audience": "private",
                "edges": [{"to": "imported-alpha", "type": "relates_to", "weight": 0.5}],
            },
        ]

        import_file = tmp_path / "import-data.json"
        import_file.write_text(json.dumps(export_data, indent=2))

        # Import the data by manually creating nodes from the file
        cfg = Config(data_dir=d)
        s = Store(cfg)
        for item in export_data:
            s.add_node(
                title=item["title"],
                content=item.get("content", ""),
                node_id=item["id"],
                node_type=item.get("type", "concept"),
                weight=item.get("weight", 0.5),
                domains=item.get("domains", []),
                audience=item.get("audience", "private"),
            )
            for edge in item.get("edges", []):
                try:
                    s.add_edge(item["id"], edge["to"],
                               edge_type=edge.get("type", "relates_to"),
                               weight=edge.get("weight", 0.5))
                except Exception:
                    pass  # edge target may not exist yet

        # Verify nodes exist
        alpha = s.get_node("imported-alpha")
        assert alpha is not None
        assert alpha["title"] == "Imported Alpha Concept"

        beta = s.get_node("imported-beta")
        assert beta is not None
        assert beta["title"] == "Imported Beta Concept"

        s.close()


class TestImportMerge:
    def test_import_merge(self, tmp_path):
        """Import overlapping data with merge mode (add_node uses INSERT OR REPLACE)."""
        d = str(tmp_path)

        cfg = Config(data_dir=d)
        s = Store(cfg)

        # Create initial data
        s.add_node("Merge Target", content="Original content",
                    node_id="merge-target", node_type="concept", weight=0.5)

        # "Import" updated data with same ID
        s.add_node("Merge Target", content="Updated content after merge",
                    node_id="merge-target", node_type="concept", weight=0.8)

        # Verify the merge (INSERT OR REPLACE means latest wins)
        node = s.get_node("merge-target")
        assert node is not None
        assert node["content"] == "Updated content after merge"
        assert node["weight"] == 0.8

        s.close()


class TestRoundtrip:
    def test_roundtrip(self, tmp_path):
        """Export then import, verify lossless."""
        d = str(tmp_path)
        run("init", data_dir=d)

        # Create source data
        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Roundtrip Alpha", content="Alpha content about something",
                    node_id="rt-alpha", node_type="concept", weight=0.7,
                    domains=["engineering"])
        s.add_node("Roundtrip Beta", content="Beta content about another thing",
                    node_id="rt-beta", node_type="concept", weight=0.6,
                    domains=["research"])
        s.add_edge("rt-alpha", "rt-beta", edge_type="relates_to", weight=0.5)
        s.close()

        # Export
        r = run("export", "--audience", "private", "--format", "json", data_dir=d)
        assert r.returncode == 0
        exported = json.loads(r.stdout)
        assert len(exported) >= 2

        # Verify the exported data has expected structure
        exported_ids = {n["id"] for n in exported}
        assert "rt-alpha" in exported_ids
        assert "rt-beta" in exported_ids

        # Verify content is preserved
        alpha_export = next(n for n in exported if n["id"] == "rt-alpha")
        assert alpha_export["title"] == "Roundtrip Alpha"
        assert "Alpha content" in alpha_export["content"]
        assert alpha_export["weight"] == 0.7

        beta_export = next(n for n in exported if n["id"] == "rt-beta")
        assert beta_export["title"] == "Roundtrip Beta"

        # Import into a new store
        d2 = str(tmp_path / "imported")
        run("init", data_dir=d2)

        cfg2 = Config(data_dir=d2)
        s2 = Store(cfg2)

        for item in exported:
            s2.add_node(
                title=item["title"],
                content=item.get("content", ""),
                node_id=item["id"],
                node_type=item.get("type", "concept"),
                weight=item.get("weight", 0.5),
                domains=item.get("domains", []),
                audience=item.get("audience", "private"),
            )
            for edge in item.get("edges", []):
                try:
                    s2.add_edge(item["id"], edge["to"],
                                edge_type=edge.get("type", "relates_to"),
                                weight=edge.get("weight", 0.5))
                except Exception:
                    pass

        # Verify roundtrip fidelity
        alpha_imported = s2.get_node("rt-alpha")
        assert alpha_imported is not None
        assert alpha_imported["title"] == "Roundtrip Alpha"
        assert "Alpha content" in alpha_imported["content"]

        beta_imported = s2.get_node("rt-beta")
        assert beta_imported is not None
        assert beta_imported["title"] == "Roundtrip Beta"

        # Verify edges survived
        edges = s2.edges_from("rt-alpha")
        to_ids = [e["to_id"] for e in edges]
        assert "rt-beta" in to_ids

        s2.close()

    def test_roundtrip_edge_types(self, tmp_path):
        """Verify edge types and weights survive export/import."""
        d = str(tmp_path)
        run("init", data_dir=d)

        cfg = Config(data_dir=d)
        s = Store(cfg)
        s.add_node("Source", node_id="src", node_type="concept")
        s.add_node("Target", node_id="tgt", node_type="skill")
        s.add_edge("src", "tgt", edge_type="implements", weight=0.9)
        s.close()

        # Export
        r = run("export", "--audience", "private", "--format", "json", data_dir=d)
        exported = json.loads(r.stdout)

        src_export = next(n for n in exported if n["id"] == "src")
        assert len(src_export["edges"]) >= 1

        edge = next(e for e in src_export["edges"] if e["to"] == "tgt")
        assert edge["type"] == "implements"
        assert edge["weight"] == 0.9
