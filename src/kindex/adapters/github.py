"""GitHub adapter — ingest issues, PRs, and commits via gh CLI."""

import json
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING

from .base import AdapterMeta, AdapterOption, IngestResult
from ..privacy import redacting_print as print

if TYPE_CHECKING:
    from ..store import Store


def is_gh_available() -> bool:
    """Check if gh CLI is installed and authenticated."""
    try:
        result = subprocess.run(["gh", "auth", "status"],
                               capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ingest_issues(store: "Store", repo: str, since: str | None = None,
                  limit: int = 50, verbose: bool = False) -> int:
    """Ingest GitHub issues as knowledge nodes.

    Args:
        store: Kindex store
        repo: owner/repo format (e.g. "jmcentire/kindex")
        since: ISO date to filter issues created after
        limit: max issues to fetch

    Creates:
        - One node per issue (type=document, domains=["github"])
        - Links to project node if exists
        - Labels become domains
    """
    cmd = ["gh", "issue", "list", "--repo", repo, "--json",
           "number,title,body,state,labels,author,createdAt,url",
           "--limit", str(limit)]
    if since:
        # gh doesn't have --since for issues, we filter in code
        pass

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 0

    issues = json.loads(result.stdout)
    count = 0

    for issue in issues:
        # Filter by date if since provided
        if since and issue.get("createdAt", "") < since:
            continue

        node_id = f"gh-issue-{repo.replace('/', '-')}-{issue['number']}"

        # Skip if already exists
        if store.get_node(node_id):
            continue

        title = f"#{issue['number']}: {issue['title']}"
        body = (issue.get("body") or "")[:2000]
        labels = [l.get("name", "") for l in issue.get("labels", [])]
        author = issue.get("author", {}).get("login", "")

        domains = ["github"] + labels

        store.add_node(
            node_id=node_id,
            title=title,
            content=body,
            node_type="document",
            domains=domains,
            status="active" if issue.get("state") == "OPEN" else "archived",
            prov_who=[author] if author else [],
            prov_source=issue.get("url", ""),
            prov_activity="github-ingest",
            extra={"repo": repo, "issue_number": issue["number"],
                   "state": issue.get("state", ""), "labels": labels},
        )
        count += 1
        if verbose:
            print(f"  Issue: {title}")

    # Link to project node
    _link_to_project(store, repo,
                     [f"gh-issue-{repo.replace('/', '-')}-{i['number']}" for i in issues])

    return count


def ingest_prs(store: "Store", repo: str, since: str | None = None,
               limit: int = 30, verbose: bool = False) -> int:
    """Ingest GitHub PRs as knowledge nodes."""
    cmd = ["gh", "pr", "list", "--repo", repo, "--json",
           "number,title,body,state,labels,author,createdAt,url,mergedAt",
           "--limit", str(limit), "--state", "all"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 0

    prs = json.loads(result.stdout)
    count = 0

    for pr in prs:
        if since and pr.get("createdAt", "") < since:
            continue

        node_id = f"gh-pr-{repo.replace('/', '-')}-{pr['number']}"
        if store.get_node(node_id):
            continue

        title = f"PR #{pr['number']}: {pr['title']}"
        body = (pr.get("body") or "")[:2000]
        labels = [l.get("name", "") for l in pr.get("labels", [])]
        author = pr.get("author", {}).get("login", "")

        status = "active"
        if pr.get("state") == "MERGED" or pr.get("mergedAt"):
            status = "archived"
        elif pr.get("state") == "CLOSED":
            status = "archived"

        store.add_node(
            node_id=node_id,
            title=title,
            content=body,
            node_type="document",
            domains=["github"] + labels,
            status=status,
            prov_who=[author] if author else [],
            prov_source=pr.get("url", ""),
            prov_activity="github-ingest",
            extra={"repo": repo, "pr_number": pr["number"],
                   "state": pr.get("state", ""), "merged": bool(pr.get("mergedAt"))},
        )
        count += 1
        if verbose:
            print(f"  PR: {title}")

    _link_to_project(store, repo,
                     [f"gh-pr-{repo.replace('/', '-')}-{p['number']}" for p in prs])

    return count


def ingest_commits(store: "Store", repo: str, since: str | None = None,
                   limit: int = 50, verbose: bool = False) -> int:
    """Ingest recent commits as session-like nodes."""
    cmd = ["gh", "api", f"repos/{repo}/commits",
           "--jq", ".[].sha, .[].commit.message, .[].commit.author.name, .[].commit.author.date",
           "-q", f".[:{limit}]"]

    # Use the proper API approach. GitHub caps per_page at 100; larger
    # values (e.g. an "unlimited" limit) must not be passed through.
    cmd = ["gh", "api", f"repos/{repo}/commits?per_page={min(limit, 100)}"]
    if since:
        cmd[-1] += f"&since={since}"

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 0

    commits = json.loads(result.stdout)
    count = 0

    for commit in commits:
        sha = commit.get("sha", "")[:8]
        node_id = f"gh-commit-{repo.replace('/', '-')}-{sha}"

        if store.get_node(node_id):
            continue

        msg = commit.get("commit", {}).get("message", "")
        author = commit.get("commit", {}).get("author", {}).get("name", "")
        date = commit.get("commit", {}).get("author", {}).get("date", "")

        # Only create nodes for meaningful commits
        if len(msg) < 10:
            continue

        title = msg.split("\n")[0][:80]
        body = msg if "\n" in msg else ""

        store.add_node(
            node_id=node_id,
            title=title,
            content=body,
            node_type="session",
            domains=["github"],
            prov_who=[author.lower().replace(" ", "-")] if author else [],
            prov_when=date,
            prov_source=f"https://github.com/{repo}/commit/{commit.get('sha', '')}",
            prov_activity="github-commit-ingest",
            extra={"repo": repo, "sha": commit.get("sha", "")},
        )
        count += 1
        if verbose:
            print(f"  Commit: {title}")

    return count


def _link_to_project(store: "Store", repo: str, node_ids: list[str]) -> None:
    """Link GitHub artifact nodes to their project node if it exists."""
    # Try to find project node by repo name
    repo_name = repo.split("/")[-1]
    project = store.get_node_by_title(repo_name)
    if not project:
        # Try slug format
        from ..ingest import _project_slug
        # Search all project nodes
        projects = store.all_nodes(node_type="project", limit=100)
        for p in projects:
            extra = p.get("extra") or {}
            path = extra.get("path", "")
            if repo_name.lower() in path.lower():
                project = p
                break

    if project:
        for nid in node_ids:
            if store.get_node(nid):
                try:
                    store.add_edge(nid, project["id"],
                                  edge_type="spawned_from",
                                  weight=0.3,
                                  provenance="github-ingest",
                                  bidirectional=False)
                except Exception:
                    pass


# ── Adapter protocol wrapper ────────────────────────────────────────


class GitHubAdapter:
    meta = AdapterMeta(
        name="github",
        description="Ingest issues, PRs, and commits from GitHub",
        requires_auth=True,
        auth_hint="Install and authenticate gh CLI: brew install gh && gh auth login",
        options=[
            AdapterOption("repo", "GitHub owner/repo (e.g. jmcentire/kindex)", required=True),
        ],
    )

    def is_available(self) -> bool:
        return is_gh_available()

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        repo = kwargs.get("repo")
        if not repo:
            return IngestResult(errors=["--repo required for github adapter"])
        created = 0
        created += ingest_issues(store, repo, since=since, limit=limit, verbose=verbose)
        created += ingest_prs(store, repo, since=since, limit=limit, verbose=verbose)
        created += ingest_commits(store, repo, since=since, limit=limit, verbose=verbose)
        return IngestResult(created=created)


adapter = GitHubAdapter()
