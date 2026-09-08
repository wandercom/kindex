"""Linear adapter — ingest issues and projects from Linear."""

import json
import os
import subprocess
from typing import TYPE_CHECKING

from .base import AdapterMeta, AdapterOption, IngestResult
from ..privacy import redacting_print as print

if TYPE_CHECKING:
    from ..store import Store


def is_linear_available() -> bool:
    """Check if Linear API key is available."""
    return bool(os.environ.get("LINEAR_API_KEY"))


def _linear_query(query: str, variables: dict | None = None) -> dict | None:
    """Execute a Linear GraphQL query."""
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        return None

    payload = json.dumps({"query": query, "variables": variables or {}})

    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "https://api.linear.app/graphql",
             "-H", "Content-Type: application/json",
             "-H", f"Authorization: {api_key}",
             "-d", payload],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def ingest_issues(store: "Store", team: str | None = None,
                  limit: int = 50, verbose: bool = False) -> int:
    """Ingest Linear issues as knowledge nodes."""
    # Linear's GraphQL API caps page size at 250 and errors on larger
    # `first` values (an "unlimited" limit would otherwise break the call).
    limit = min(limit, 250)

    query = """
    query($first: Int, $filter: IssueFilter) {
        issues(first: $first, filter: $filter, orderBy: updatedAt) {
            nodes {
                id
                identifier
                title
                description
                state { name }
                labels { nodes { name } }
                assignee { name }
                createdAt
                updatedAt
                url
                project { name }
            }
        }
    }
    """

    variables = {"first": limit}
    if team:
        variables["filter"] = {"team": {"key": {"eq": team}}}

    data = _linear_query(query, variables)
    if not data or "data" not in data:
        return 0

    issues = data["data"].get("issues", {}).get("nodes", [])
    count = 0

    for issue in issues:
        node_id = f"linear-{issue['identifier']}"

        if store.get_node(node_id):
            continue

        title = f"{issue['identifier']}: {issue['title']}"
        body = (issue.get("description") or "")[:2000]
        labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
        assignee = (issue.get("assignee") or {}).get("name", "")
        state = (issue.get("state") or {}).get("name", "")
        project = (issue.get("project") or {}).get("name", "")

        status = "active"
        if state.lower() in ("done", "closed", "cancelled", "canceled"):
            status = "archived"

        domains = ["linear"] + labels
        if project:
            domains.append(project.lower().replace(" ", "-"))

        store.add_node(
            node_id=node_id,
            title=title,
            content=body,
            node_type="document",
            domains=domains,
            status=status,
            prov_who=[assignee.lower().replace(" ", "-")] if assignee else [],
            prov_source=issue.get("url", ""),
            prov_activity="linear-ingest",
            extra={"identifier": issue["identifier"], "state": state,
                   "project": project, "labels": labels},
        )
        count += 1
        if verbose:
            print(f"  Linear: {title}")

    return count


# ── Adapter protocol wrapper ────────────────────────────────────────


class LinearAdapter:
    meta = AdapterMeta(
        name="linear",
        description="Ingest issues from Linear",
        requires_auth=True,
        auth_hint="Set LINEAR_API_KEY env var",
        options=[
            AdapterOption("team", "Linear team key to filter by"),
        ],
    )

    def is_available(self) -> bool:
        return is_linear_available()

    def ingest(self, store, *, limit=50, since=None, verbose=False, **kwargs):
        team = kwargs.get("team")
        created = ingest_issues(store, team=team, limit=limit, verbose=verbose)
        return IngestResult(created=created)


adapter = LinearAdapter()
