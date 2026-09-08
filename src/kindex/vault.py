"""Vault — filesystem-backed knowledge graph store."""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from .config import Config, load_config
from .models import Edge, SkillNode, TopicNode


def parse_frontmatter(filepath: Path) -> tuple[dict, str]:
    """Extract YAML frontmatter from a markdown file.

    Returns (meta_dict, body_text). Handles no-frontmatter, CRLF, bad YAML.
    """
    content = filepath.read_text(encoding="utf-8").replace("\r\n", "\n")

    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        meta = yaml.safe_load(parts[1])
        return meta or {}, parts[2].strip()
    except yaml.YAMLError:
        return {}, content


def serialize_frontmatter(meta: dict, body: str) -> str:
    """Serialize back to markdown with YAML frontmatter."""
    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True,
                         sort_keys=False, width=120)
    return f"---\n{yaml_str}---\n{body}\n"


class Vault:
    """A Conv knowledge graph backed by the filesystem.

    Data lives at config.data_path (default ~/.conv/).
    """

    def __init__(self, config: Config | None = None, config_path: str | None = None):
        self.config = config or load_config(config_path)
        self.topics: dict[str, TopicNode] = {}
        self.skills: dict[str, SkillNode] = {}
        self.forward: dict[str, list[Edge]] = defaultdict(list)
        self.reverse: dict[str, list[tuple[str, Edge]]] = defaultdict(list)

    @property
    def data_path(self) -> Path:
        return self.config.data_path

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        for d in [self.config.topics_dir, self.config.skills_dir,
                  self.config.inbox_dir, self.config.tmp_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def load(self) -> Vault:
        """Load all topics and skills from disk, build indexes."""
        self.topics.clear()
        self.skills.clear()
        self.forward.clear()
        self.reverse.clear()

        self._load_topics()
        self._load_skills()
        self._build_indexes()
        return self

    def _load_topics(self) -> None:
        td = self.config.topics_dir
        if not td.exists():
            return
        for f in sorted(td.glob("*.md")):
            slug = f.stem
            meta, body = parse_frontmatter(f)
            node = TopicNode(slug=slug, path=f, body=body, **meta)
            node._has_frontmatter = bool(meta)
            if not node.topic:
                node.topic = slug
            self.topics[slug] = node

    def _load_skills(self) -> None:
        sd = self.config.skills_dir
        if not sd.exists():
            return
        for f in sorted(sd.glob("*.md")):
            slug = f.stem
            meta, body = parse_frontmatter(f)
            node = SkillNode(slug=slug, path=f, body=body, **meta)
            if not node.skill:
                node.skill = slug
            self.skills[slug] = node

    def _build_indexes(self) -> None:
        for slug, node in self.topics.items():
            for edge in node.connects_to:
                self.forward[slug].append(edge)
                self.reverse[edge.target].append((slug, edge))
        for slug, node in self.skills.items():
            for edge in node.connects_to:
                self.forward[slug].append(edge)
                self.reverse[edge.target].append((slug, edge))

    def get(self, slug: str) -> TopicNode | SkillNode | None:
        return self.topics.get(slug) or self.skills.get(slug)

    def all_slugs(self) -> list[str]:
        return sorted(set(list(self.topics.keys()) + list(self.skills.keys())))

    def edges_from(self, slug: str) -> list[Edge]:
        return self.forward.get(slug, [])

    def edges_to(self, slug: str) -> list[tuple[str, Edge]]:
        return self.reverse.get(slug, [])

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write to tmp, then os.replace() for crash safety."""
        from .privacy import redact_text
        content = redact_text(content)
        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.config.tmp_dir, suffix=".md")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def save_topic(self, node: TopicNode) -> None:
        if node.path is None:
            node.path = self.config.topics_dir / f"{node.slug}.md"
        content = serialize_frontmatter(node.frontmatter_dict(), node.body)
        self._atomic_write(node.path, content)

    def save_skill(self, node: SkillNode) -> None:
        if node.path is None:
            node.path = self.config.skills_dir / f"{node.slug}.md"
        content = serialize_frontmatter(node.frontmatter_dict(), node.body)
        self._atomic_write(node.path, content)

    def add_edge(self, source: str, target: str, weight: float, reason: str) -> None:
        node = self.get(source)
        if node is None:
            raise KeyError(f"Node '{source}' not found")

        existing = node.edge_to(target) if hasattr(node, "edge_to") else None
        if existing:
            existing.weight = weight
            existing.reason = reason
        else:
            edge = Edge(target=target, weight=weight, reason=reason)
            node.connects_to.append(edge)
            self.forward[source].append(edge)
            self.reverse[target].append((source, edge))

        if isinstance(node, TopicNode):
            self.save_topic(node)
        else:
            self.save_skill(node)
