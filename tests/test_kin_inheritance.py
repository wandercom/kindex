"""Tests for .kin/config inheritance and resolution chain."""

from pathlib import Path

import pytest

from kindex.ingest import load_project_context, merge_kin_chain, resolve_kin_chain


def _write_kin_config(directory: Path, content: str) -> Path:
    """Helper: create .kin/config inside a directory."""
    kin_dir = directory / ".kin"
    kin_dir.mkdir(exist_ok=True)
    config_path = kin_dir / "config"
    config_path.write_text(content)
    return config_path


@pytest.fixture
def kin_tree(tmp_path):
    """Create a hierarchy of .kin/config files for testing inheritance."""
    # Root org
    org_dir = tmp_path / "org"
    org_dir.mkdir()
    _write_kin_config(org_dir,
        "name: acme-corp\n"
        "audience: team\n"
        "domains: [engineering]\n"
        "privacy: team\n"
    )

    # Team (inherits from org)
    team_dir = tmp_path / "team"
    team_dir.mkdir()
    _write_kin_config(team_dir,
        "name: platform-team\n"
        "audience: team\n"
        "domains: [platform, infrastructure]\n"
        f"inherits:\n  - {org_dir / '.kin' / 'config'}\n"
    )

    # Project (inherits from team)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_kin_config(project_dir,
        "name: payments-service\n"
        "audience: team\n"
        "domains: [integrations, python]\n"
        f"inherits:\n  - {team_dir / '.kin' / 'config'}\n"
        "shared_with:\n  - team: engineering\n"
    )

    # Personal (no inheritance)
    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    _write_kin_config(personal_dir,
        "name: personal-notes\n"
        "audience: private\n"
        "domains: [personal, health]\n"
    )

    return {
        "org": org_dir,
        "team": team_dir,
        "project": project_dir,
        "personal": personal_dir,
    }


class TestResolveKinChain:
    def test_single_file(self, kin_tree):
        chain = resolve_kin_chain(kin_tree["personal"] / ".kin" / "config")
        assert len(chain) == 1
        assert chain[0]["name"] == "personal-notes"

    def test_two_level_inheritance(self, kin_tree):
        chain = resolve_kin_chain(kin_tree["team"] / ".kin" / "config")
        assert len(chain) == 2
        assert chain[0]["name"] == "platform-team"  # local first
        assert chain[1]["name"] == "acme-corp"       # ancestor second

    def test_three_level_inheritance(self, kin_tree):
        chain = resolve_kin_chain(kin_tree["project"] / ".kin" / "config")
        assert len(chain) == 3
        assert chain[0]["name"] == "payments-service"
        assert chain[1]["name"] == "platform-team"
        assert chain[2]["name"] == "acme-corp"

    def test_nonexistent_parent(self, tmp_path):
        _write_kin_config(tmp_path,
            "name: orphan\ninherits:\n  - /nonexistent/.kin/config\n"
        )
        chain = resolve_kin_chain(tmp_path / ".kin" / "config")
        assert len(chain) == 1  # just the local file

    def test_circular_reference(self, tmp_path):
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        _write_kin_config(a_dir, f"name: a\ninherits:\n  - {b_dir / '.kin' / 'config'}\n")
        _write_kin_config(b_dir, f"name: b\ninherits:\n  - {a_dir / '.kin' / 'config'}\n")
        chain = resolve_kin_chain(a_dir / ".kin" / "config")
        assert len(chain) == 2  # stops at visited

    def test_max_depth(self, tmp_path):
        # Create a deep chain
        dirs = []
        for i in range(10):
            d = tmp_path / f"level{i}"
            d.mkdir()
            dirs.append(d)

        for i, d in enumerate(dirs):
            if i < len(dirs) - 1:
                _write_kin_config(d,
                    f"name: level{i}\ninherits:\n  - {dirs[i+1] / '.kin' / 'config'}\n"
                )
            else:
                _write_kin_config(d, f"name: level{i}\n")

        chain = resolve_kin_chain(dirs[0] / ".kin" / "config", max_depth=3)
        assert len(chain) <= 3

    def test_old_kin_file_is_read_in_place(self, tmp_path):
        """Old-style .kin file is read without being rewritten: a load is a read."""
        (tmp_path / ".kin").write_text("name: legacy\ndomains: [old]\n")
        chain = resolve_kin_chain(tmp_path / ".kin")
        assert len(chain) == 1
        assert chain[0]["name"] == "legacy"
        assert (tmp_path / ".kin").is_file()

    def test_opening_a_repo_local_store_upgrades_a_legacy_kin_file(self, tmp_path):
        """A store cannot be created beneath a .kin file; opening one upgrades it."""
        from kindex.config import Config
        from kindex.store import Store

        (tmp_path / ".kin").write_text("name: legacy\n")
        store = Store(Config(data_dir=str(tmp_path / ".kin" / "local" / "kindex")))
        try:
            store.conn
        finally:
            store.close()
        assert (tmp_path / ".kin" / "config").read_text() == "name: legacy\n"
        assert (tmp_path / ".kin" / "local" / "kindex").is_dir()
        assert "local/" in (tmp_path / ".kin" / ".gitignore").read_text()

    def test_explicit_upgrade_keeps_the_content(self, tmp_path, monkeypatch):
        """The upgrade moves the file aside before anything else, so a failed
        write leaves the content on disk."""
        from pathlib import Path

        from kindex.config import _maybe_upgrade_kin_file

        (tmp_path / ".kin").write_text("name: legacy\n")
        real_write = Path.write_bytes

        def failing_write(self, data):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_bytes", failing_write)
        assert _maybe_upgrade_kin_file(tmp_path / ".kin") is None
        aside = list(tmp_path.glob(".kin.upgrade-*"))
        assert len(aside) == 1 and aside[0].read_text() == "name: legacy\n"
        monkeypatch.setattr(Path, "write_bytes", real_write)

        (tmp_path / "other").mkdir()
        (tmp_path / "other" / ".kin").write_text("name: other\n")
        upgraded = _maybe_upgrade_kin_file(tmp_path / "other" / ".kin")
        assert upgraded == tmp_path / "other" / ".kin" / "config"
        assert upgraded.read_text() == "name: other\n"
        assert not list((tmp_path / "other").glob(".kin.upgrade-*"))

    def test_old_style_inherits_ref(self, tmp_path):
        """Old-style inherits ref (pointing to .kin dir) still resolves."""
        parent = tmp_path / "parent"
        child = tmp_path / "child"
        parent.mkdir()
        child.mkdir()
        _write_kin_config(parent, "name: parent\ndomains: [base]\n")
        # Child uses old-style ref pointing to .kin (a directory now)
        _write_kin_config(child,
            f"name: child\ndomains: [specific]\ninherits:\n  - {parent / '.kin'}\n"
        )
        chain = resolve_kin_chain(child / ".kin" / "config")
        assert len(chain) == 2
        assert chain[0]["name"] == "child"
        assert chain[1]["name"] == "parent"


class TestMergeKinChain:
    def test_local_overrides_ancestor(self):
        chain = [
            {"name": "local", "audience": "private"},
            {"name": "parent", "audience": "team"},
        ]
        merged = merge_kin_chain(chain)
        assert merged["name"] == "local"       # local wins
        assert merged["audience"] == "private"  # local wins

    def test_lists_concatenated(self):
        chain = [
            {"domains": ["python", "api"]},
            {"domains": ["engineering", "python"]},  # python deduped
        ]
        merged = merge_kin_chain(chain)
        assert "python" in merged["domains"]
        assert "api" in merged["domains"]
        assert "engineering" in merged["domains"]
        # No duplicates
        assert len([d for d in merged["domains"] if d == "python"]) == 1

    def test_ancestor_provides_defaults(self):
        chain = [
            {"name": "local"},
            {"name": "parent", "privacy": "team", "extra_field": "inherited"},
        ]
        merged = merge_kin_chain(chain)
        assert merged["name"] == "local"
        assert merged["privacy"] == "team"        # inherited
        assert merged["extra_field"] == "inherited"  # inherited

    def test_chain_tracking(self, kin_tree):
        chain = resolve_kin_chain(kin_tree["project"] / ".kin" / "config")
        merged = merge_kin_chain(chain)
        assert "_chain" in merged
        assert len(merged["_chain"]) == 3

    def test_empty_chain(self):
        merged = merge_kin_chain([])
        assert merged == {}


class TestLoadProjectContext:
    def test_full_resolution(self, kin_tree):
        ctx = load_project_context(kin_tree["project"] / ".kin" / "config")
        assert ctx["name"] == "payments-service"
        # Domains merged from all three levels
        assert "integrations" in ctx["domains"]
        assert "python" in ctx["domains"]
        assert "platform" in ctx["domains"]
        assert "engineering" in ctx["domains"]

    def test_private_stays_private(self, kin_tree):
        ctx = load_project_context(kin_tree["personal"] / ".kin" / "config")
        assert ctx["audience"] == "private"
        assert "personal" in ctx["domains"]

    def test_nonexistent_file(self, tmp_path):
        ctx = load_project_context(tmp_path / "nonexistent" / ".kin" / "config")
        assert ctx == {}


class TestRelativeInherits:
    def test_relative_path(self, tmp_path):
        parent = tmp_path / "parent"
        child = tmp_path / "parent" / "child"
        parent.mkdir()
        child.mkdir()
        _write_kin_config(parent, "name: parent\ndomains: [base]\n")
        _write_kin_config(child,
            "name: child\ndomains: [specific]\ninherits:\n  - ../../.kin/config\n"
        )

        chain = resolve_kin_chain(child / ".kin" / "config")
        assert len(chain) == 2
        merged = merge_kin_chain(chain)
        assert "base" in merged["domains"]
        assert "specific" in merged["domains"]
