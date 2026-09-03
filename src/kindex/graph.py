"""Graph algorithms — BFS, PPR, centrality, search."""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .schema import SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES
from .vault import Vault


@dataclass
class TraversalResult:
    slug: str
    depth: int
    reason: str | None
    edge_weight: float
    cumulative_weight: float


@dataclass
class GraphStats:
    node_count: int = 0
    edge_count: int = 0
    density: float = 0.0
    components: int = 0
    avg_degree: float = 0.0
    max_degree_node: str = ""
    max_degree: int = 0


def build_nx(vault: Vault) -> nx.DiGraph:
    """Build a NetworkX directed graph from all vault nodes."""
    G = nx.DiGraph()

    for slug, node in vault.topics.items():
        G.add_node(slug, kind="topic", title=node.title,
                   weight=node.weight, domains=node.domains)

    for slug, node in vault.skills.items():
        G.add_node(slug, kind="skill", title=node.title,
                   level=node.level, domains=node.domains)

    for slug in list(vault.topics) + list(vault.skills):
        for edge in vault.edges_from(slug):
            if edge.target in vault.topics or edge.target in vault.skills:
                G.add_edge(slug, edge.target, weight=edge.weight, reason=edge.reason)

    return G


def weighted_bfs(vault: Vault, start: str, max_hops: int = 2,
                 min_weight: float = 0.1) -> list[TraversalResult]:
    """BFS with cumulative weight decay (product of edge weights along path)."""
    if start not in vault.topics and start not in vault.skills:
        return []

    visited = {start}
    result = [TraversalResult(start, 0, None, 1.0, 1.0)]
    frontier = [(start, 0, 1.0)]

    while frontier:
        current, depth, cum = frontier.pop(0)
        if depth >= max_hops:
            continue

        node = vault.get(current)
        if node is None:
            continue

        edges = sorted(node.connects_to, key=lambda e: e.weight, reverse=True)
        for edge in edges:
            if edge.target in visited:
                continue
            if vault.get(edge.target) is None:
                continue

            new_cum = cum * edge.weight
            if new_cum < min_weight:
                continue

            visited.add(edge.target)
            result.append(TraversalResult(
                edge.target, depth + 1, edge.reason, edge.weight, new_cum))
            frontier.append((edge.target, depth + 1, new_cum))

    return result


def text_search(vault: Vault, query: str, top_k: int = 10) -> list[tuple[str, float]]:
    """Simple keyword search across titles, bodies, tags, and domains.

    Returns (slug, score) sorted by score descending. No LLM needed.
    """
    query_terms = query.lower().split()
    scores: dict[str, float] = {}

    for slug in vault.all_slugs():
        node = vault.get(slug)
        if node is None:
            continue

        score = 0.0
        title = (node.title or "").lower()
        body = (node.body or "").lower()
        domains = " ".join(getattr(node, "domains", [])).lower()
        tags = " ".join(getattr(node, "tags", [])).lower()
        slug_text = slug.replace("-", " ").lower()

        for term in query_terms:
            if term in slug_text:
                score += 3.0  # slug match is strong signal
            if term in title:
                score += 2.0
            if term in domains or term in tags:
                score += 1.5
            if term in body:
                score += 0.5
                # Bonus for frequency
                score += min(body.count(term) * 0.1, 1.0)

        if score > 0:
            # Boost by node weight
            node_weight = getattr(node, "weight", 0.5) or 0.5
            scores[slug] = score * (0.5 + 0.5 * node_weight)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_k]


def ppr(vault: Vault, start: str, top_k: int = 10,
        alpha: float = 0.85) -> list[tuple[str, float]]:
    """Personalized PageRank seeded from a node."""
    G = build_nx(vault)
    if start not in G:
        return []

    personalization = {n: 0.0 for n in G.nodes()}
    personalization[start] = 1.0
    scores = nx.pagerank(G, alpha=alpha, personalization=personalization, weight="weight")
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


def graph_stats(vault: Vault) -> GraphStats:
    G = build_nx(vault)
    if not G:
        return GraphStats()
    degrees = dict(G.degree())
    mx = max(degrees, key=degrees.get) if degrees else ""
    return GraphStats(
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
        density=nx.density(G),
        components=nx.number_weakly_connected_components(G),
        avg_degree=sum(degrees.values()) / len(degrees) if degrees else 0,
        max_degree_node=mx,
        max_degree=degrees.get(mx, 0),
    )


def centrality(vault: Vault, method: str = "betweenness") -> list[tuple[str, float]]:
    G = build_nx(vault)
    if not G:
        return []
    fn = {
        "betweenness": lambda: nx.betweenness_centrality(G, weight="weight"),
        "degree": lambda: nx.degree_centrality(G),
        "closeness": lambda: nx.closeness_centrality(G),
    }
    scores = fn.get(method, fn["betweenness"])()
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def communities(vault: Vault) -> list[set[str]]:
    G = build_nx(vault)
    if not G:
        return []
    U = G.to_undirected()
    return list(nx.community.greedy_modularity_communities(U, weight="weight"))


# ── Store-based graph algorithms ─────────────────────────────────────

_NX_BUILD_LIMIT = 50000


def build_nx_from_store(store, limit: int = _NX_BUILD_LIMIT) -> nx.DiGraph:
    """Build a NetworkX directed graph from the Store.

    ``limit`` caps how many nodes are loaded into memory for graph analysis.
    Use ``store_stats`` for accurate counts — it reads totals from SQL.
    """
    G = nx.DiGraph()

    for node in store.all_nodes(
        exclude_types=SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
        limit=limit,
    ):
        G.add_node(node["id"], title=node["title"], type=node["type"],
                   weight=node.get("weight", 0.5),
                   domains=node.get("domains", []))

    for nid in list(G.nodes()):
        for edge in store.edges_from(nid, semantic_only=True):
            if edge["to_id"] in G:
                G.add_edge(nid, edge["to_id"],
                           weight=edge["weight"],
                           type=edge["type"],
                           provenance=edge.get("provenance", ""))

    return G


def store_stats(store) -> dict:
    """Compute graph statistics from Store.

    Node/edge counts come from SQL (accurate regardless of build limit).
    Density, components, and degree stats come from the networkx graph
    (may be approximate if the graph exceeds the build limit).
    """
    placeholders = ",".join("?" for _ in SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES)
    semantic_nodes = store.conn.execute(
        f"SELECT COUNT(*) FROM nodes WHERE type NOT IN ({placeholders})",
        SEMANTIC_GRAPH_EXCLUDED_NODE_TYPES,
    ).fetchone()[0]
    stored_nodes = store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_counts = store.graph_edge_counts()

    G = build_nx_from_store(store)
    truncated = G.number_of_nodes() < semantic_nodes

    if not G:
        return {"nodes": semantic_nodes, "semantic_nodes": semantic_nodes,
                "edges": edge_counts["semantic"],
                "stored_nodes": stored_nodes,
                "stored_edges": edge_counts["stored"],
                "ignored_domain_edges": edge_counts["domain"],
                "ignored_session_edges": edge_counts["session"],
                "ignored_other_edges": edge_counts["other"],
                "density": 0,
                "components": 0, "avg_degree": 0, "max_degree_node": "",
                "max_degree": 0, "truncated": truncated}

    degrees = dict(G.degree())
    mx = max(degrees, key=degrees.get) if degrees else ""
    return {
        "nodes": semantic_nodes,
        "semantic_nodes": semantic_nodes,
        "edges": edge_counts["semantic"],
        "stored_nodes": stored_nodes,
        "stored_edges": edge_counts["stored"],
        "ignored_domain_edges": edge_counts["domain"],
        "ignored_session_edges": edge_counts["session"],
        "ignored_other_edges": edge_counts["other"],
        "density": round(nx.density(G), 4),
        "components": nx.number_weakly_connected_components(G),
        "avg_degree": round(sum(degrees.values()) / len(degrees), 2) if degrees else 0,
        "max_degree_node": mx,
        "max_degree": degrees.get(mx, 0),
        "truncated": truncated,
    }


def store_centrality(store, method: str = "betweenness",
                     top_k: int = 20) -> list[tuple[str, str, float]]:
    """Compute centrality from Store. Returns [(id, title, score)]."""
    G = build_nx_from_store(store)
    if not G:
        return []

    fn = {
        "betweenness": lambda: nx.betweenness_centrality(G, weight="weight"),
        "degree": lambda: nx.degree_centrality(G),
        "closeness": lambda: nx.closeness_centrality(G),
    }
    scores = fn.get(method, fn["betweenness"])()
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    result = []
    for nid, score in ranked:
        title = G.nodes[nid].get("title", nid)
        result.append((nid, title, round(score, 4)))
    return result


def store_communities(store) -> list[list[dict]]:
    """Detect communities from Store. Returns list of communities (each a list of {id, title})."""
    G = build_nx_from_store(store)
    if not G or G.number_of_nodes() < 2:
        return []

    U = G.to_undirected()
    comms = nx.community.greedy_modularity_communities(U, weight="weight")
    result = []
    for comm in comms:
        members = []
        for nid in comm:
            title = G.nodes[nid].get("title", nid)
            members.append({"id": nid, "title": title})
        result.append(sorted(members, key=lambda m: m["title"]))
    return result


def store_bridges(store, top_k: int = 10) -> list[dict]:
    """Find bridge edges — edges whose removal would disconnect components.

    Returns edges sorted by importance (betweenness centrality).
    """
    G = build_nx_from_store(store)
    if not G or G.number_of_edges() < 2:
        return []

    U = G.to_undirected()
    edge_btw = nx.edge_betweenness_centrality(U, weight="weight")
    ranked = sorted(edge_btw.items(), key=lambda x: x[1], reverse=True)[:top_k]

    result = []
    for (u, v), score in ranked:
        u_title = G.nodes[u].get("title", u) if u in G else u
        v_title = G.nodes[v].get("title", v) if v in G else v
        result.append({
            "from_id": u, "from_title": u_title,
            "to_id": v, "to_title": v_title,
            "betweenness": round(score, 4),
        })
    return result


def store_trailheads(store, top_k: int = 10) -> list[dict]:
    """Identify trailhead nodes — high-centrality entry points into the graph.

    Trailheads are nodes with high betweenness centrality and multiple
    outgoing edges, making them good starting points for exploration.
    """
    G = build_nx_from_store(store)
    if not G:
        return []

    betweenness = nx.betweenness_centrality(G, weight="weight")
    out_degrees = dict(G.out_degree())

    # Score = betweenness * (1 + log(out_degree))
    import math
    scores = {}
    for nid in G.nodes():
        out = out_degrees.get(nid, 0)
        btw = betweenness.get(nid, 0)
        if out > 0:
            scores[nid] = btw * (1 + math.log(1 + out))

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    result = []
    for nid, score in ranked:
        data = G.nodes[nid]
        result.append({
            "id": nid,
            "title": data.get("title", nid),
            "type": data.get("type", "concept"),
            "score": round(score, 4),
            "out_degree": out_degrees.get(nid, 0),
            "betweenness": round(betweenness.get(nid, 0), 4),
        })
    return result


def suggest_cross_component_links(
    store,
    max_suggestions: int = 10,
) -> list[dict]:
    """Find candidate links between disconnected graph components.

    For each pair of components, picks the highest-weight node from each
    and uses FTS title/domain overlap to find plausible connections.
    Returns suggestions sorted by similarity score.
    """
    G = build_nx_from_store(store)
    if not G or G.number_of_nodes() < 2:
        return []

    components = list(nx.weakly_connected_components(G))
    if len(components) < 2:
        return []

    # Get representative nodes per component (top 3 by weight)
    comp_reps: list[list[dict]] = []
    for comp in components:
        if len(comp) < 1:
            continue
        members = []
        for nid in comp:
            data = G.nodes[nid]
            members.append({
                "id": nid,
                "title": data.get("title", ""),
                "type": data.get("type", "concept"),
                "weight": data.get("weight", 0),
                "domains": data.get("domains", ""),
            })
        members.sort(key=lambda m: m["weight"], reverse=True)
        comp_reps.append(members[:3])

    # Compare representatives across components using keyword overlap
    suggestions = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, reps_a in enumerate(comp_reps):
        for j, reps_b in enumerate(comp_reps):
            if j <= i:
                continue
            for a in reps_a:
                for b in reps_b:
                    pair = tuple(sorted([a["id"], b["id"]]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    score = _title_similarity(a["title"], b["title"])

                    # Domain overlap bonus
                    domains_a = _parse_domains(a["domains"])
                    domains_b = _parse_domains(b["domains"])
                    if domains_a and domains_b:
                        overlap = domains_a & domains_b
                        if overlap:
                            score += 0.3 * len(overlap)

                    if score > 0.1:
                        suggestions.append({
                            "concept_a": a["title"],
                            "concept_b": b["title"],
                            "id_a": a["id"],
                            "id_b": b["id"],
                            "score": round(score, 3),
                            "reason": f"Cross-component link (similarity: {score:.2f})",
                        })

            if len(suggestions) >= max_suggestions * 3:
                break
        if len(suggestions) >= max_suggestions * 3:
            break

    suggestions.sort(key=lambda s: s["score"], reverse=True)
    return suggestions[:max_suggestions]


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two titles."""
    if not a or not b:
        return 0.0
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    stop = {"the", "a", "an", "is", "in", "of", "to", "and", "for", "with", "on", "at", "by"}
    words_a -= stop
    words_b -= stop
    if not words_a or not words_b:
        return 0.0
    overlap = words_a & words_b
    if not overlap:
        return 0.0
    return len(overlap) / min(len(words_a), len(words_b))


def _parse_domains(domains) -> set[str]:
    """Parse domains from various formats (JSON string, list, etc.)."""
    if isinstance(domains, list):
        return {d.lower() for d in domains}
    if isinstance(domains, str) and domains:
        import json
        try:
            parsed = json.loads(domains)
            if isinstance(parsed, list):
                return {d.lower() for d in parsed}
        except (json.JSONDecodeError, TypeError):
            return {d.strip().lower() for d in domains.split(",") if d.strip()}
    return set()
