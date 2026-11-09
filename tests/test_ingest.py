"""Tests for ingestion — project scanning, .conv files, session learning."""

import json
import subprocess
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store


def _canonical_checkout(path):
    """Positive ingestion fixtures represent real primary checkouts.

    Canonical/WIP contract: unclassified ordinary folders are no longer
    implicitly indexable. Keep the original ingestion outcomes and supply
    their canonical Git identity explicitly, without committing any content.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def store_with_projects(tmp_path):
    """Create a store and fake project structure for testing."""
    data_dir = tmp_path / "data"
    projects_dir = tmp_path / "projects"

    # Create fake projects with CLAUDE.md
    proj_a = projects_dir / "project-alpha"
    proj_a.mkdir(parents=True)
    (proj_a / "CLAUDE.md").write_text(
        "# Project Alpha\n\nThis project uses stigmergy for coordination.\n"
        "It implements the Ambient Structure Discovery pattern.\n"
    )
    (proj_a / "pyproject.toml").write_text("[project]\nname = 'alpha'\n")

    proj_b = projects_dir / "project-beta"
    proj_b.mkdir(parents=True)
    (proj_b / "CLAUDE.md").write_text(
        "# Project Beta\n\nA database tool for graph analytics.\n"
    )
    (proj_b / "package.json").write_text("{}")

    # A project with no CLAUDE.md — should be skipped
    proj_c = projects_dir / "project-gamma"
    proj_c.mkdir(parents=True)
    (proj_c / "README.md").write_text("# Gamma\n")
    for project in (proj_a, proj_b, proj_c):
        _canonical_checkout(project)

    cfg = Config(data_dir=str(data_dir), project_dirs=[str(projects_dir)])
    s = Store(cfg)
    yield s, cfg, projects_dir
    s.close()


class TestScanProjects:
    def test_finds_claude_md_projects(self, store_with_projects):
        s, cfg, _ = store_with_projects
        from kindex.ingest import scan_projects
        count = scan_projects(cfg, s)
        assert count == 2  # alpha and beta

    def test_creates_project_nodes(self, store_with_projects):
        s, cfg, _ = store_with_projects
        from kindex.ingest import scan_projects
        scan_projects(cfg, s)
        nodes = s.all_nodes(node_type="project")
        assert len(nodes) == 2
        titles = [n["title"] for n in nodes]
        assert "Project Alpha" in titles
        assert "Project Beta" in titles

    def test_infers_domains(self, store_with_projects):
        s, cfg, _ = store_with_projects
        from kindex.ingest import scan_projects
        scan_projects(cfg, s)
        nodes = {n["title"]: n for n in s.all_nodes(node_type="project")}
        assert "python" in nodes["Project Alpha"]["domains"]
        assert "javascript" in nodes["Project Beta"]["domains"]

    def test_idempotent(self, store_with_projects):
        s, cfg, _ = store_with_projects
        from kindex.ingest import scan_projects
        count1 = scan_projects(cfg, s)
        count2 = scan_projects(cfg, s)
        assert count1 == 2
        assert count2 == 0  # already exists

    def test_updates_on_content_change(self, store_with_projects):
        s, cfg, projects_dir = store_with_projects
        from kindex.ingest import scan_projects
        scan_projects(cfg, s)

        # Change CLAUDE.md content
        alpha_md = projects_dir / "project-alpha" / "CLAUDE.md"
        alpha_md.write_text("# Project Alpha\n\nUpdated content here.\n")

        count = scan_projects(cfg, s)
        assert count == 0  # not a new node, but content was updated

    def test_auto_links_to_existing_nodes(self, store_with_projects):
        s, cfg, _ = store_with_projects
        # Pre-add a concept node
        s.add_node("Stigmergy", content="Coordination through traces", node_id="stig")
        from kindex.ingest import scan_projects
        scan_projects(cfg, s)

        # Project Alpha mentions stigmergy — should be linked
        alpha_slug = "proj-projects-project-alpha"
        edges = s.edges_from(alpha_slug)
        linked_ids = [e["to_id"] for e in edges]
        assert "stig" in linked_ids


class TestKinFiles:
    def test_reads_kin_file(self, store_with_projects):
        s, cfg, projects_dir = store_with_projects
        from kindex.ingest import scan_kin_files, scan_projects

        # First create the project nodes
        scan_projects(cfg, s)

        # Add a .kin/config
        kin_dir = projects_dir / "project-alpha" / ".kin"
        kin_dir.mkdir(exist_ok=True)
        (kin_dir / "config").write_text("audience: team\ndomains: [engineering, ml]\n")

        count = scan_kin_files(cfg, s)
        assert count >= 1

        # Check audience was updated
        slug = "proj-projects-project-alpha"
        node = s.get_node(slug)
        assert node["audience"] == "team"

    def test_creates_from_conv_if_no_claude_md(self, tmp_path):
        data_dir = tmp_path / "data"
        projects_dir = tmp_path / "projects"

        proj = projects_dir / "solo-project"
        proj.mkdir(parents=True)
        _canonical_checkout(proj)
        kin_dir = proj / ".kin"
        kin_dir.mkdir(exist_ok=True)
        (kin_dir / "config").write_text(
            "title: Solo Project\naudience: private\n"
            "domains: [research]\ndescription: A research project.\n"
        )

        cfg = Config(data_dir=str(data_dir), project_dirs=[str(projects_dir)])
        s = Store(cfg)

        from kindex.ingest import scan_kin_files
        count = scan_kin_files(cfg, s)
        assert count == 1

        nodes = s.all_nodes(node_type="project")
        assert len(nodes) == 1
        assert nodes[0]["title"] == "Solo Project"
        assert nodes[0]["audience"] == "private"
        s.close()


class TestScanSessions:
    def test_scans_jsonl_sessions(self, tmp_path):
        data_dir = tmp_path / "data"
        project = _canonical_checkout(tmp_path / "canonical-project")
        claude_dir = tmp_path / "claude"
        projects_dir = claude_dir / "projects" / "-Users-test-Code-MyProject"
        projects_dir.mkdir(parents=True)

        # Create a fake session JSONL
        session_file = projects_dir / "abc123def456.jsonl"
        lines = [
            json.dumps({"cwd": str(project), "role": "user", "content": "Tell me about stigmergy"}),
            json.dumps({"role": "assistant", "content": "Stigmergy is coordination through environmental traces. Ambient Structure Discovery uses it."}),
            json.dumps({"role": "user", "content": "How does it relate to emergence?"}),
            json.dumps({"role": "assistant", "content": "Emergence Architecture builds on stigmergic principles to create self-organizing systems."}),
        ]
        session_file.write_text("\n".join(lines))

        cfg = Config(data_dir=str(data_dir), claude_dir=str(claude_dir))
        s = Store(cfg)

        from kindex.ingest import scan_sessions
        count = scan_sessions(cfg, s, limit=5)
        assert count >= 1

        sessions = s.all_nodes(node_type="session")
        assert len(sessions) >= 1
        s.close()

    def test_idempotent_sessions(self, tmp_path):
        data_dir = tmp_path / "data"
        project = _canonical_checkout(tmp_path / "canonical-project")
        claude_dir = tmp_path / "claude"
        projects_dir = claude_dir / "projects" / "-Users-test-Code-Proj"
        projects_dir.mkdir(parents=True)

        session_file = projects_dir / "session12345.jsonl"
        session_file.write_text(
            json.dumps({"cwd": str(project), "role": "assistant", "content": [{"type": "text", "text": "The Emergence Architecture pattern uses stigmergic coordination."}]})
        )

        cfg = Config(data_dir=str(data_dir), claude_dir=str(claude_dir))
        s = Store(cfg)

        from kindex.ingest import scan_sessions
        count1 = scan_sessions(cfg, s, limit=5)
        count2 = scan_sessions(cfg, s, limit=5)
        assert count2 == 0  # already ingested
        s.close()

    def test_scans_codex_jsonl_sessions(self, tmp_path):
        data_dir = tmp_path / "data"
        project = _canonical_checkout(tmp_path / "canonical-project")
        codex_dir = tmp_path / "codex"
        sessions_dir = codex_dir / "sessions" / "2026" / "05" / "03"
        sessions_dir.mkdir(parents=True)

        session_file = sessions_dir / "rollout-2026-05-03T11-20-01-abcdef123456.jsonl"
        lines = [
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "abcdef123456",
                    "cwd": str(project),
                    "cli_version": "0.128.0",
                    "model_provider": "openai",
                },
            }),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Tell me about kindex memory"}],
                },
            }),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "Kindex memory stores Codex sessions as knowledge graph context. Ambient Structure Discovery uses session traces.",
                    }],
                },
            }),
        ]
        session_file.write_text("\n".join(lines))

        cfg = Config(data_dir=str(data_dir), codex_dir=str(codex_dir))
        s = Store(cfg)

        from kindex.ingest import scan_codex_sessions
        count = scan_codex_sessions(cfg, s, limit=5)

        assert count >= 1
        sessions = s.all_nodes(node_type="session")
        assert len(sessions) >= 1
        assert sessions[0]["id"].startswith("codex-session-")
        assert sessions[0]["extra"]["agent"] == "codex"
        s.close()

    def test_codex_sessions_idempotent(self, tmp_path):
        data_dir = tmp_path / "data"
        project = _canonical_checkout(tmp_path / "canonical-project")
        codex_dir = tmp_path / "codex"
        sessions_dir = codex_dir / "sessions" / "2026" / "05" / "03"
        sessions_dir.mkdir(parents=True)

        session_file = sessions_dir / "rollout-abcdef123456.jsonl"
        session_file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": "abcdef123456", "cwd": str(project)}}),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "Kindex memory stores Codex sessions as knowledge graph context. Ambient Structure Discovery uses session traces.",
                    }],
                },
            }),
        ]))

        cfg = Config(data_dir=str(data_dir), codex_dir=str(codex_dir))
        s = Store(cfg)

        from kindex.ingest import scan_codex_sessions
        count1 = scan_codex_sessions(cfg, s, limit=5)
        count2 = scan_codex_sessions(cfg, s, limit=5)

        assert count1 >= 1
        assert count2 == 0
        s.close()


class TestAudienceInference:
    def test_personal_is_private(self):
        from kindex.ingest import _infer_audience
        assert _infer_audience(Path("/Users/me/Personal/journal")) == "private"

    def test_code_is_team(self):
        from kindex.ingest import _infer_audience
        assert _infer_audience(Path("/Users/me/Code/webapp")) == "team"

    def test_work_is_team(self):
        from kindex.ingest import _infer_audience
        assert _infer_audience(Path("/Users/me/Work/project")) == "team"

    def test_default_is_private(self):
        from kindex.ingest import _infer_audience
        assert _infer_audience(Path("/tmp/random")) == "private"
