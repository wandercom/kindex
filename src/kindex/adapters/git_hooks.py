"""Git hooks adapter — capture commits and surface constraints on push."""

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .base import AdapterMeta, AdapterOption, IngestResult
from ..privacy import redacting_print as print

if TYPE_CHECKING:
    from ..store import Store
    from ..config import Config


def install_hooks(repo_path: str | Path, config: "Config") -> list[str]:
    """Install Kindex git hooks in a repository.

    Installs:
    - pre-commit: enforces opt-in project work policy from .kin/config
    - post-commit: records the commit as a knowledge node
    - pre-push: enforces opt-in project policy and surfaces constraints

    Returns list of actions taken.
    """
    repo_path = Path(repo_path)
    hooks_dir = repo_path / ".git" / "hooks"
    if not hooks_dir.exists():
        return ["Error: not a git repository"]

    actions = []
    kin_path = _find_kin()

    # pre-commit hook
    pre_commit = hooks_dir / "pre-commit"
    pre_commit_content = f'''#!/bin/sh
# Kindex: enforce opt-in project policy before commit
{kin_path} policy check --event pre-commit --project-path "$(git rev-parse --show-toplevel)" 2>&1
'''

    if pre_commit.exists():
        existing = pre_commit.read_text()
        if "kin policy check" in existing:
            actions.append("pre-commit hook already has Kindex policy integration")
        else:
            with open(pre_commit, "a") as f:
                f.write("\n# --- Kindex integration ---\n")
                f.write(pre_commit_content.split("\n", 1)[1])
            pre_commit.chmod(0o755)
            actions.append("Appended Kindex to existing pre-commit hook")
    else:
        pre_commit.write_text(pre_commit_content)
        pre_commit.chmod(0o755)
        actions.append("Created pre-commit hook")

    # post-commit hook
    post_commit = hooks_dir / "post-commit"
    post_commit_content = f'''#!/bin/sh
# Kindex: record commit as knowledge
COMMIT_MSG=$(git log -1 --pretty=%B)
COMMIT_SHA=$(git log -1 --pretty=%H)
AUTHOR=$(git log -1 --pretty=%an)
{kin_path} add "$COMMIT_MSG" --type session 2>/dev/null || true
'''

    if post_commit.exists():
        existing = post_commit.read_text()
        if "kindex" in existing.lower() or "kin add" in existing:
            actions.append("post-commit hook already has Kindex integration")
        else:
            # Append to existing hook
            with open(post_commit, "a") as f:
                f.write("\n# --- Kindex integration ---\n")
                f.write(post_commit_content.split("\n", 1)[1])  # Skip shebang
            post_commit.chmod(0o755)
            actions.append("Appended Kindex to existing post-commit hook")
    else:
        post_commit.write_text(post_commit_content)
        post_commit.chmod(0o755)
        actions.append("Created post-commit hook")

    # pre-push hook
    pre_push = hooks_dir / "pre-push"
    pre_push_content = f'''#!/bin/sh
# Kindex: enforce opt-in project policy and surface constraints before push
{kin_path} policy check --event pre-push --project-path "$(git rev-parse --show-toplevel)" 2>&1
{kin_path} status --trigger pre-push 2>/dev/null || true
'''

    if pre_push.exists():
        existing = pre_push.read_text()
        if "kin policy check" in existing:
            actions.append("pre-push hook already has Kindex integration")
        else:
            with open(pre_push, "a") as f:
                f.write("\n# --- Kindex integration ---\n")
                f.write(pre_push_content.split("\n", 1)[1])
            pre_push.chmod(0o755)
            actions.append("Appended Kindex to existing pre-push hook")
    else:
        pre_push.write_text(pre_push_content)
        pre_push.chmod(0o755)
        actions.append("Created pre-push hook")

    return actions


def uninstall_hooks(repo_path: str | Path) -> list[str]:
    """Remove Kindex git hooks from a repository."""
    # Just removes the Kindex sections, not the whole hook file
    repo_path = Path(repo_path)
    hooks_dir = repo_path / ".git" / "hooks"
    actions = []

    for hook_name in ["pre-commit", "post-commit", "pre-push"]:
        hook_path = hooks_dir / hook_name
        if hook_path.exists():
            content = hook_path.read_text()
            if "Kindex" in content:
                # Remove Kindex section
                lines = content.split("\n")
                filtered = []
                skip = False
                for line in lines:
                    if "--- Kindex integration ---" in line:
                        skip = True
                        continue
                    if skip and line.strip() == "":
                        skip = False
                        continue
                    if not skip:
                        filtered.append(line)

                new_content = "\n".join(filtered)
                if new_content.strip() == "#!/bin/sh":
                    hook_path.unlink()
                    actions.append(f"Removed {hook_name} hook (was only Kindex)")
                else:
                    hook_path.write_text(new_content)
                    actions.append(f"Removed Kindex section from {hook_name}")

    if not actions:
        actions.append("No Kindex hooks found")

    return actions


def ingest_recent_commits(store: "Store", repo_path: str | Path = ".",
                          limit: int = 20, verbose: bool = False) -> int:
    """Ingest recent git commits from a local repository."""
    repo_path = Path(repo_path).resolve()

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", f"--max-count={limit}",
             "--pretty=format:%H|%s|%an|%aI"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0

    count = 0
    repo_name = repo_path.name

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue

        sha, subject, author, date = parts
        node_id = f"commit-{sha[:8]}"

        if store.get_node(node_id):
            continue

        store.add_node(
            node_id=node_id,
            title=subject[:80],
            content="",
            node_type="session",
            domains=[repo_name.lower()],
            prov_who=[author.lower().replace(" ", "-")],
            prov_when=date,
            prov_source=f"{repo_path}@{sha[:8]}",
            prov_activity="git-commit-ingest",
            extra={"sha": sha, "repo": str(repo_path)},
        )
        count += 1
        if verbose:
            print(f"  Commit: {subject[:60]}")

    return count


def _find_kin() -> str:
    """Find the kin executable."""
    result = subprocess.run(["which", "kin"], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        return result.stdout.strip()
    import sys
    return f"{sys.executable} -m kindex.cli"


# ── Adapter protocol wrapper ────────────────────────────────────────


class CommitsAdapter:
    meta = AdapterMeta(
        name="commits",
        description="Ingest recent commits from a local git repository",
        options=[
            AdapterOption("repo_path", "Local repository path", default="."),
        ],
    )

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        repo_path = kwargs.get("repo_path", ".")
        created = ingest_recent_commits(
            store, repo_path=repo_path, limit=limit, verbose=verbose
        )
        return IngestResult(created=created)


adapter = CommitsAdapter()
