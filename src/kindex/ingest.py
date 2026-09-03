"""Ingestion — scan projects, sessions, and external sources into the graph."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .store import Store


# ── Project scanning ──────────────────────────────────────────────────


def scan_projects(config: Config, store: Store, verbose: bool = False) -> int:
    """Scan configured directories for repos with CLAUDE.md files.

    Creates/updates project nodes and extracts key context from each.
    Returns count of new nodes created.
    """
    count = 0

    for project_dir in config.resolved_project_dirs:
        if not project_dir.exists():
            continue

        # Find CLAUDE.md files (direct children only — one per project)
        for claude_md in sorted(project_dir.rglob("CLAUDE.md")):
            # The project root is the parent of CLAUDE.md
            project_root = claude_md.parent
            slug = _project_slug(project_root)

            # Skip if already exists and was recently scanned
            existing = store.get_node(slug)
            if existing:
                # Update content if CLAUDE.md changed
                content = _extract_project_context(claude_md)
                if content != (existing.get("content") or ""):
                    store.update_node(slug, content=content,
                                      extra={"path": str(project_root)})
                    if verbose:
                        print(f"  Updated: {slug}")
                continue

            content = _extract_project_context(claude_md)
            title = _infer_title(project_root, claude_md)
            audience = _infer_audience(project_root)

            store.add_node(
                node_id=slug,
                title=title,
                content=content,
                node_type="project",
                domains=_infer_domains(project_root),
                audience=audience,
                prov_source=str(claude_md),
                prov_activity="project-scan",
                extra={"path": str(project_root)},
            )
            count += 1
            if verbose:
                print(f"  Created: {title} ({slug})")

            # Auto-link to existing nodes via keyword matching
            _auto_link_project(store, slug, content)

    return count


def _project_slug(project_root: Path) -> str:
    """Generate a stable slug from a project path."""
    # Use last two path components for uniqueness
    parts = project_root.parts
    if len(parts) >= 2:
        return f"proj-{parts[-2]}-{parts[-1]}".lower().replace(" ", "-")
    return f"proj-{parts[-1]}".lower().replace(" ", "-")


def _infer_title(project_root: Path, claude_md: Path) -> str:
    """Infer project title from CLAUDE.md or directory name."""
    try:
        text = claude_md.read_text(errors="replace")[:2000]
        # Look for a markdown heading
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("# ") and len(line) > 3:
                return line[2:].strip()
    except OSError:
        pass
    return project_root.name.replace("-", " ").replace("_", " ").title()


def _extract_project_context(claude_md: Path) -> str:
    """Extract the key content from a CLAUDE.md file."""
    try:
        text = claude_md.read_text(errors="replace")
        # Limit to reasonable size but keep the important stuff
        if len(text) > 4000:
            text = text[:4000] + "\n...(truncated)"
        return text.strip()
    except OSError:
        return ""


def _infer_domains(project_root: Path) -> list[str]:
    """Infer domains from project structure."""
    domains = []
    indicators = {
        "pyproject.toml": "python",
        "package.json": "javascript",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
        "Gemfile": "ruby",
    }
    for filename, domain in indicators.items():
        if (project_root / filename).exists():
            domains.append(domain)

    # Check parent dir for broader domain hints
    parent_name = project_root.parent.name.lower()
    domain_map = {
        "code": "engineering",
        "personal": "personal",
        "research": "research",
        "work": "work",
    }
    if parent_name in domain_map:
        domains.append(domain_map[parent_name])

    return domains


def _auto_link_project(store: Store, project_slug: str, content: str) -> None:
    """Link a project node to existing nodes that appear in its content."""
    content_lower = content.lower()
    existing = store.all_nodes(limit=500)

    for node in existing:
        if node["id"] == project_slug:
            continue
        title_lower = node["title"].lower()
        # Link if the project content mentions an existing node title
        if len(title_lower) > 3 and title_lower in content_lower:
            store.add_edge(
                project_slug, node["id"],
                edge_type="context_of",
                weight=0.4,
                provenance="auto-linked from CLAUDE.md content",
                bidirectional=True,
            )


# ── Session learning ──────────────────────────────────────────────────


def scan_sessions(
    config: Config,
    store: Store,
    limit: int = 10,
    verbose: bool = False,
) -> int:
    """Scan Claude Code project directories for session data.

    Reads conversation JSONL files from ~/.claude/projects/ and extracts
    knowledge into the graph.

    Returns count of new nodes created.
    """
    projects_dir = config.claude_path / "projects"
    if not projects_dir.exists():
        return 0

    count = 0
    from .extract import keyword_extract

    # Try to set up LLM summarization
    llm_summarize = None
    try:
        from .extract import llm_summarize_session
        from .budget import BudgetLedger
        ledger = BudgetLedger(config.ledger_path, config.budget)
        if ledger.can_spend():
            llm_summarize = lambda txt: llm_summarize_session(txt, config, ledger)
    except Exception:
        pass

    # Find recent JSONL conversation files
    jsonl_files = sorted(
        projects_dir.rglob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:limit]

    # Per-profile session routing: an explicit per-pass predicate (set by
    # daemon.cron_run_all / kin cron) wins; otherwise one is built from the
    # configured profiles so EVERY ingest path routes (kin ingest sessions,
    # MCP ingest, ...). None => no profiles: legacy, take everything.
    from .routing import effective_session_filter

    session_filter = effective_session_filter(config)

    for jsonl_path in jsonl_files:
        if session_filter is not None and not session_filter(jsonl_path):
            continue
        session_id = jsonl_path.stem[:12]
        session_slug = f"session-{session_id}"

        # Skip already-ingested sessions
        if store.get_node(session_slug):
            continue

        # Extract text from the session
        text = _extract_session_text(jsonl_path, max_chars=8000)
        if not text or len(text) < 50:
            continue

        # Determine which project this session belongs to
        project_context = jsonl_path.parent.name

        # Extract knowledge
        extraction = keyword_extract(text)
        concepts = extraction.get("concepts", [])

        if not concepts:
            continue

        # Generate summary: LLM if available, fallback to first 500 chars
        summary = None
        if llm_summarize is not None:
            try:
                summary = llm_summarize(text)
            except Exception:
                pass
        if not summary:
            summary = text[:500]

        # Create session node
        store.add_node(
            node_id=session_slug,
            title=f"Session: {project_context[:40]}",
            content=summary,
            node_type="session",
            prov_source=str(jsonl_path),
            prov_activity="session-scan",
            extra={"project": project_context},
        )
        count += 1

        if verbose:
            print(f"  Session: {session_slug} ({project_context})")

        # Link extracted concepts
        for concept in concepts[:5]:
            existing = store.get_node_by_title(concept["title"])
            if existing:
                store.add_edge(
                    session_slug, existing["id"],
                    edge_type="context_of",
                    weight=0.3,
                    provenance="mentioned in session",
                )

        # Link to project node if it exists
        _link_session_to_project(store, session_slug, project_context)

    return count


def scan_codex_sessions(
    config: Config,
    store: Store,
    limit: int = 10,
    verbose: bool = False,
) -> int:
    """Scan Codex session JSONL files from ~/.codex/sessions/.

    Codex stores sessions under YYYY/MM/DD directories. Each line is an event;
    user and assistant conversation messages appear as response_item payloads.
    """
    sessions_dir = config.codex_path / "sessions"
    if not sessions_dir.exists():
        return 0

    count = 0
    from .extract import keyword_extract

    # Per-profile routing by the cwd recorded in the rollout meta (Codex has
    # no Claude-style encoded dir names). None => no profiles configured.
    from .routing import effective_cwd_router

    cwd_router = effective_cwd_router(config)

    jsonl_files = sorted(
        sessions_dir.rglob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:limit]

    for jsonl_path in jsonl_files:
        meta, text = _extract_codex_session(jsonl_path, max_chars=8000)
        if not text or len(text) < 50:
            continue

        if cwd_router is not None and not cwd_router(str(meta.get("cwd") or "")):
            continue

        raw_session_id = meta.get("id") or jsonl_path.stem
        session_id = str(raw_session_id).replace("rollout-", "")[:12]
        session_slug = f"codex-session-{session_id}"

        if store.get_node(session_slug):
            continue

        extraction = keyword_extract(text)
        concepts = extraction.get("concepts", [])
        if not concepts:
            continue

        cwd = meta.get("cwd") or ""
        project_context = Path(cwd).name if cwd else jsonl_path.parent.name
        summary = text[:500]

        extra = {
            "agent": "codex",
            "project": project_context,
            "cwd": cwd,
            "model_provider": meta.get("model_provider", ""),
            "cli_version": meta.get("cli_version", ""),
            "timestamp": meta.get("timestamp", ""),
        }

        store.add_node(
            node_id=session_slug,
            title=f"Codex Session: {project_context[:40]}",
            content=summary,
            node_type="session",
            domains=["codex"],
            prov_source=str(jsonl_path),
            prov_activity="codex-session-scan",
            extra=extra,
        )
        count += 1

        if verbose:
            print(f"  Codex session: {session_slug} ({project_context})")

        for concept in concepts[:5]:
            existing = store.get_node_by_title(concept["title"])
            if existing:
                store.add_edge(
                    session_slug,
                    existing["id"],
                    edge_type="context_of",
                    weight=0.3,
                    provenance="mentioned in Codex session",
                )

        if project_context:
            _link_session_to_project(store, session_slug, project_context)

    return count


def _extract_codex_session(jsonl_path: Path, max_chars: int = 8000) -> tuple[dict, str]:
    """Extract metadata and readable conversation text from a Codex JSONL session."""
    meta: dict = {}
    texts: list[str] = []
    total_len = 0

    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                if total_len >= max_chars:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if entry.get("type") == "session_meta":
                    payload = entry.get("payload") or {}
                    if isinstance(payload, dict):
                        meta.update(payload)
                    continue

                if entry.get("type") != "response_item":
                    continue

                payload = entry.get("payload") or {}
                if not isinstance(payload, dict) or payload.get("type") != "message":
                    continue

                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    continue

                for text in _codex_message_texts(payload.get("content")):
                    stripped = text.strip()
                    if not stripped or stripped.startswith("<environment_context>"):
                        continue
                    prefix = "User" if role == "user" else "Assistant"
                    chunk = f"{prefix}: {stripped[:1000]}"
                    texts.append(chunk)
                    total_len += len(chunk)
                    if total_len >= max_chars:
                        break
    except OSError:
        return meta, ""

    return meta, "\n".join(texts)


def _codex_message_texts(content) -> list[str]:
    """Return text blocks from Codex message content."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            val = block.get(key)
            if isinstance(val, str):
                texts.append(val)
                break
    return texts


def _extract_session_text(jsonl_path: Path, max_chars: int = 8000) -> str:
    """Extract human-readable text from a Claude Code JSONL session file."""
    texts = []
    total_len = 0

    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                if total_len >= max_chars:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue

                # Extract text from assistant messages. Modern Claude Code
                # transcripts nest the message: {"type": "assistant",
                # "message": {"role": ..., "content": ...}}; older ones put
                # role/content at the top level.
                role = entry.get("role", "")
                content = entry.get("content", "")
                if not role and isinstance(entry.get("message"), dict):
                    role = entry["message"].get("role", "")
                    content = entry["message"].get("content", "")
                if role != "assistant":
                    continue
                if isinstance(content, str):
                    texts.append(content[:1000])
                    total_len += len(content[:1000])
                elif isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict) and block.get("type") == "text"
                                and isinstance(block.get("text"), str)):
                            text = block["text"][:1000]
                            texts.append(text)
                            total_len += len(text)
    except OSError:
        return ""

    return "\n".join(texts)


# ── Parent directory .kin walk ────────────────────────────────────────


def find_parent_kin(start_path: Path | None = None, max_depth: int = 10) -> list[Path]:
    """Walk up from start_path (or cwd) to find .kin/config files.

    Returns list of config file paths from most specific (deepest) to root.
    Auto-upgrades old .kin files to .kin/config on discovery.
    Stops at filesystem root or after max_depth levels.
    """
    from .config import _maybe_upgrade_kin_file

    if start_path is None:
        start_path = Path.cwd()
    start_path = Path(start_path).resolve()

    found = []
    current = start_path
    for _ in range(max_depth):
        kin_entry = current / ".kin"
        if kin_entry.is_dir():
            config_file = kin_entry / "config"
            if config_file.is_file():
                found.append(config_file)
        elif kin_entry.is_file():
            upgraded = _maybe_upgrade_kin_file(kin_entry)
            if upgraded and upgraded.is_file():
                found.append(upgraded)
        parent = current.parent
        if parent == current:  # filesystem root
            break
        current = parent

    return found


# ── .kin directory support ────────────────────────────────────────────


def scan_kin_files(config: Config, store: Store, verbose: bool = False) -> int:
    """Scan project directories for .kin/config files (per-repo metadata).

    A .kin/config file in a repo root provides Kindex-specific metadata:
      audience: team
      domains: [engineering, python]
      connects_to: [other-project-slug]
      description: What this project is about.

    Auto-upgrades old .kin files to .kin/config on discovery.
    Returns count of updated nodes.
    """
    import yaml
    from .config import _maybe_upgrade_kin_file

    count = 0
    all_pending: list[tuple[str, str]] = []  # (source_slug, target_name)
    project_graphs: dict[str, str] = {}  # project_root -> resolved data_dir
    for project_dir in config.resolved_project_dirs:
        if not project_dir.exists():
            continue

        for kin_entry in sorted(project_dir.rglob(".kin")):
            # Resolve to the config file inside the .kin directory
            if kin_entry.is_dir():
                config_file = kin_entry / "config"
                if not config_file.is_file():
                    continue
                project_root = kin_entry.parent
            elif kin_entry.is_file():
                upgraded = _maybe_upgrade_kin_file(kin_entry)
                if not upgraded or not upgraded.is_file():
                    continue
                config_file = upgraded
                project_root = kin_entry.parent
            else:
                continue

            slug = _project_slug(project_root)

            try:
                data = yaml.safe_load(config_file.read_text()) or {}
            except Exception:
                continue

            # Register project-local graphs (data_dir in .kin/config) so the
            # reminder sweep (`remind_check_all`) can service them — these
            # graphs belong to no profile, so without this registry nothing
            # scheduled would ever fire their reminders. data_dir must come
            # from the resolved inheritance chain, matching what load_config
            # gives interactive sessions: a repo that merely `inherits:` a
            # template declaring data_dir still gets its own live graph.
            raw_dir = data.get("data_dir")
            if not raw_dir:
                try:
                    from .config import _load_kin_config_with_inheritance
                    raw_dir = _load_kin_config_with_inheritance(
                        config_file).get("data_dir")
                except Exception:
                    raw_dir = None
            if raw_dir:
                resolved_dir = Path(str(raw_dir)).expanduser()
                if not resolved_dir.is_absolute():
                    resolved_dir = project_root / resolved_dir
                project_graphs[str(project_root)] = str(resolved_dir.resolve())

            existing = store.get_node(slug)
            if existing:
                updates = {}
                if "audience" in data:
                    updates["audience"] = data["audience"]
                if "domains" in data:
                    updates["domains"] = data["domains"]
                if "description" in data:
                    updates["content"] = data["description"]
                if updates:
                    store.update_node(slug, **updates)
                    count += 1
                    if verbose:
                        print(f"  Updated from .kin/config: {slug}")

                # Handle connects_to — link to existing nodes, track pending for later
                for target in data.get("connects_to", []):
                    target_node = store.get_node(target) or store.get_node_by_title(target)
                    if target_node:
                        store.add_edge(slug, target_node["id"],
                                       edge_type="relates_to",
                                       weight=0.6,
                                       provenance=".kin/config")
                    else:
                        all_pending.append((slug, target))
                        if verbose:
                            print(f"  Warning: connects_to target '{target}' not found "
                                  f"(from {config_file}), will retry after ingestion")
            else:
                title = data.get("title", project_root.name.replace("-", " ").title())
                audience = data.get("audience", _infer_audience(project_root))
                store.add_node(
                    node_id=slug,
                    title=title,
                    content=data.get("description", ""),
                    node_type="project",
                    domains=data.get("domains", _infer_domains(project_root)),
                    audience=audience,
                    prov_source=str(config_file),
                    prov_activity="kin-file-scan",
                    extra={"path": str(project_root)},
                )
                count += 1
                if verbose:
                    print(f"  Created from .kin/config: {slug}")

                # Handle connects_to for newly created nodes too
                for target in data.get("connects_to", []):
                    target_node = store.get_node(target) or store.get_node_by_title(target)
                    if target_node:
                        store.add_edge(slug, target_node["id"],
                                       edge_type="relates_to",
                                       weight=0.6,
                                       provenance=".kin/config")
                    else:
                        all_pending.append((slug, target))
                        if verbose:
                            print(f"  Warning: connects_to target '{target}' not found "
                                  f"(from {config_file}), will retry after ingestion")

    # Resolution pass: retry pending connects_to now that all projects are ingested
    if all_pending:
        resolved = 0
        for source_slug, target in all_pending:
            target_node = store.get_node(target) or store.get_node_by_title(target)
            if target_node:
                store.add_edge(source_slug, target_node["id"],
                               edge_type="relates_to",
                               weight=0.6,
                               provenance=".kin/config (deferred)")
                resolved += 1
            elif verbose:
                print(f"  Unresolved: connects_to '{target}' from {source_slug} "
                      f"— target not in graph")
        if verbose and resolved:
            print(f"  Resolved {resolved}/{len(all_pending)} deferred connections")

    # Persist the project-graph registry (merge over previous scans; drop
    # entries whose data_dir no longer exists so deleted projects age out).
    try:
        previous = json.loads(store.get_meta("project_graph_dirs") or "{}")
        if not isinstance(previous, dict):
            previous = {}
    except Exception:
        previous = {}
    merged_graphs = {**previous, **project_graphs}
    merged_graphs = {root: d for root, d in merged_graphs.items() if Path(d).exists()}
    store.set_meta("project_graph_dirs", json.dumps(merged_graphs))

    return count


# ── .kin inheritance resolution ───────────────────────────────────────


def resolve_kin_chain(kin_path: Path, max_depth: int = 5, auto_walk: bool = False) -> list[dict]:
    """Resolve the .kin inheritance chain for a given .kin/config file.

    Accepts either .kin/config (new) or .kin (old, auto-upgraded).
    If auto_walk=True and no explicit inherits, also walk up the directory tree
    to discover parent .kin directories automatically.

    Returns a list of parsed config dicts from most specific (local) to
    most general (root ancestor). Each inheritor's values override ancestors.

    The .kin/config file format:
        name: my-project
        audience: team
        inherits:
          - ../platform/.kin/config   # parent repo context
          - ~/.kindex/.kin/config     # user's personal kindex (private)
        shared_with:
          - team: engineering
        privacy: team
    """
    from .config import resolve_kin_config

    kin_path = resolve_kin_config(kin_path)

    chain: list[dict] = []
    visited: set[str] = set()
    _resolve_kin_recursive(kin_path, chain, visited, max_depth)

    # If auto_walk is enabled and the root config has no explicit inherits,
    # walk up the directory tree for additional .kin directories
    if auto_walk and chain:
        root_data = chain[0]
        if not root_data.get("inherits"):
            # Start walk from parent of the .kin directory
            walk_start = kin_path.parent.parent.parent if kin_path.name == "config" else kin_path.parent.parent
            parent_kins = find_parent_kin(walk_start, max_depth=max_depth)
            for parent_kin in parent_kins:
                resolved = parent_kin.resolve()
                if str(resolved) not in visited:
                    _resolve_kin_recursive(parent_kin, chain, visited, max_depth - len(chain))

    return chain


def _resolve_kin_recursive(
    kin_path: Path,
    chain: list[dict],
    visited: set,
    remaining: int,
) -> None:
    """Recursively resolve .kin inheritance chain."""
    import yaml
    from .config import resolve_kin_config

    resolved = resolve_kin_config(kin_path)
    if str(resolved) in visited or remaining <= 0:
        return
    visited.add(str(resolved))

    if not resolved.is_file():
        return

    try:
        data = yaml.safe_load(resolved.read_text()) or {}
    except Exception:
        return

    data["_source"] = str(resolved)
    chain.append(data)

    # Resolve inherited configs (supports both old and new ref styles)
    for parent_ref in data.get("inherits", []):
        parent_path = (resolved.parent / parent_ref).resolve()
        _resolve_kin_recursive(parent_path, chain, visited, remaining - 1)


def merge_kin_chain(chain: list[dict]) -> dict:
    """Merge an inheritance chain into a single resolved config.

    Later entries (ancestors) provide defaults; earlier entries (local) override.
    Lists are concatenated. Dicts are merged (local wins). Scalars are overridden.
    """
    if not chain:
        return {}

    result: dict = {}

    # Process from ancestor (end) to local (start) — local overrides
    for layer in reversed(chain):
        for key, value in layer.items():
            if key.startswith("_"):
                continue
            if key == "inherits":
                continue
            if key in result:
                existing = result[key]
                if isinstance(existing, list) and isinstance(value, list):
                    # Concatenate lists, dedup
                    seen = set()
                    merged = []
                    for item in value + existing:
                        s = str(item)
                        if s not in seen:
                            seen.add(s)
                            merged.append(item)
                    result[key] = merged
                elif isinstance(existing, dict) and isinstance(value, dict):
                    existing.update(value)
                else:
                    result[key] = value
            else:
                result[key] = value

    result["_chain"] = [d.get("_source", "?") for d in chain]
    return result


def load_project_context(kin_path: Path) -> dict:
    """Load the full resolved context for a project from its .kin/config.

    This is the main entry point for Claude Code integration.
    When Claude opens a repo, it calls this to get the merged context
    from the full inheritance chain. Accepts .kin/config or legacy .kin paths.
    """
    chain = resolve_kin_chain(kin_path)
    return merge_kin_chain(chain)


def _infer_audience(project_root: Path) -> str:
    """Infer audience from directory heuristic."""
    path_str = str(project_root).lower()
    if "/personal/" in path_str or "/gemweaver/" in path_str:
        return "private"
    if "/work/" in path_str:
        return "team"
    if "/code/" in path_str:
        return "team"  # code repos default to team-visible
    return "private"


# ── Synonym ring files ───────────────────────────────────────────────


def load_synonym_rings(config: "Config", store: "Store", verbose: bool = False) -> int:
    """Load synonym ring files from the data directory.

    Synonym ring format (.syn files in data_dir/synonyms/):
        ring: database-terms
        synonyms:
          - database
          - db
          - datastore
          - data store
          - persistence layer

    Applies synonyms as AKA entries on matching nodes.
    Returns count of nodes updated.
    """
    import yaml

    syn_dir = config.data_path / "synonyms"
    if not syn_dir.exists():
        return 0

    count = 0
    for syn_file in sorted(syn_dir.glob("*.syn")):
        try:
            data = yaml.safe_load(syn_file.read_text()) or {}
        except Exception:
            if verbose:
                print(f"  Warning: could not parse {syn_file.name}")
            continue

        ring_name = data.get("ring", syn_file.stem)
        synonyms = data.get("synonyms", [])
        if not synonyms or not isinstance(synonyms, list):
            continue

        # For each synonym, find nodes whose title matches and add
        # all other synonyms as AKA entries
        for synonym in synonyms:
            node = store.get_node_by_title(synonym)
            if not node:
                continue

            existing_aka = list(node.get("aka") or [])
            new_aka = list(existing_aka)
            added = False

            for other in synonyms:
                if other == synonym:
                    continue
                if other.lower() == node.get("title", "").lower():
                    continue
                if other not in new_aka:
                    new_aka.append(other)
                    added = True

            if added:
                store.update_node(node["id"], aka=new_aka)
                count += 1
                if verbose:
                    added_count = len(new_aka) - len(existing_aka)
                    print(f"  {node['title']}: +{added_count} synonyms from ring '{ring_name}'")

    return count


def _link_session_to_project(store: Store, session_slug: str, project_context: str) -> None:
    """Try to link a session to its corresponding project node."""
    # project_context is like "-Users-jmcentire-Code-Conv"
    # Try to match to a project node
    parts = project_context.strip("-").split("-")
    # Try from the end, building longer matches
    for i in range(len(parts), max(0, len(parts) - 3), -1):
        candidate = "-".join(parts[-2:]).lower() if len(parts) >= 2 else parts[-1].lower()
        slug = f"proj-{candidate}"
        if store.get_node(slug):
            store.add_edge(
                session_slug, slug,
                edge_type="spawned_from",
                weight=0.5,
                provenance="session in project dir",
            )
            return


# ── Person expertise auto-detection ───────────────────────────────────


def detect_expertise(store: "Store", person_node_id: str) -> dict[str, int]:
    """Detect expertise domains for a person by analysing their graph connections.

    Walks edges from the person node, inspects the ``domains`` field on
    each connected node, and tallies how often each domain appears.  The
    person's node is then updated with the top domains.

    Parameters
    ----------
    store : Store
        An open Kindex store.
    person_node_id : str
        The node ID of the person to analyse.

    Returns
    -------
    dict[str, int]
        Mapping of domain name to frequency count.
    """
    from collections import Counter

    person = store.get_node(person_node_id)
    if person is None:
        return {}

    domain_counts: Counter[str] = Counter()

    # 1. Tally domains from directly connected nodes
    edges = store.edges_from(person_node_id, semantic_only=True)
    for edge in edges:
        target_id = edge.get("to_id")
        if not target_id:
            continue
        target = store.get_node(target_id)
        if target is None:
            continue
        for domain in (target.get("domains") or []):
            domain_counts[domain] += 1

    # 2. Also inspect activity log entries for this person
    activities = store.activity_by_actor(person_node_id, limit=100)
    for act in activities:
        node_id = act.get("node_id") or ""
        if not node_id:
            continue
        node = store.get_node(node_id)
        if node is None:
            continue
        for domain in (node.get("domains") or []):
            domain_counts[domain] += 1

    # 3. Update the person node with the top domains (up to 10)
    if domain_counts:
        top_domains = [d for d, _ in domain_counts.most_common(10)]
        store.update_node(person_node_id, domains=top_domains)

    return dict(domain_counts)


# ── Git-tracked index ────────────────────────────────────────────────


def _node_time(node: dict) -> str:
    return str(
        node.get("updated_at")
        or node.get("prov_when")
        or node.get("created_at")
        or ""
    )


def _kin_index_node(node: dict) -> dict:
    # Weight is included but rounded to 2 decimal places so decay-only
    # movement (typically <0.001 per fold) does not churn git history.
    # A genuine weight change (reinforcement +0.05, access boost, manual
    # edit) crosses a 2-dp boundary and is visible. This preserves the
    # information 0.30.1 carried while keeping the file stable against
    # the unconditional decay fold (R2.1).
    out = {
        "domains": sorted(node.get("domains") or []),
        "id": node["id"],
        "title": node["title"],
        "type": node["type"],
        "updated_at": _node_time(node),
        "weight": round(node["weight"], 2),
    }
    # R0 referent binding + two clocks travel with the projection (schema v2
    # unknown-field passthrough keeps them safe through older merge drivers,
    # which pass node dicts through whole). An absolute local path is
    # REDACTED (digest and scope kept, path_redacted flag set): tracked
    # .kin files must never carry machine-local absolute paths.
    referent = node.get("referent")
    if isinstance(referent, dict):
        path = referent.get("path")
        if path and Path(path).is_absolute():
            referent = {
                k: v for k, v in referent.items() if k != "path"
            }
            referent["path_redacted"] = True
        out["referent"] = referent
    for clock in ("asserted_at", "true_of"):
        if node.get(clock):
            out[clock] = node[clock]
    return out


def _git_ancestor_exists(path: Path) -> bool:
    """True if path or any ancestor contains a .git entry (dir or file)."""
    try:
        p = path.resolve()
    except OSError:
        p = path
    for candidate in (p, *p.parents):
        try:
            if (candidate / ".git").exists():
                return True
        except OSError:
            continue
    return False


def _detect_repo_for_index(output_dir: Path) -> str | None:
    """Detect git repo slug from output_dir for repo-scoped indexing.

    Returns None only when output_dir is genuinely outside any git repo.
    When git itself fails (binary missing, timeout, dubious-ownership
    refusal — routine in CI/containers) but a .git entry exists in
    output_dir or an ancestor, raises RuntimeError: a failed detection
    inside a real repo must never downgrade to the global graph head.
    """
    import subprocess
    failure: str
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=str(output_dir),
        )
        if r.returncode == 0:
            root = Path(r.stdout.strip())
            return root.name.lower().replace(" ", "-")
        failure = (r.stderr or "").strip() or f"git exited {r.returncode}"
    except Exception as e:
        failure = str(e) or type(e).__name__
    if _git_ancestor_exists(output_dir):
        raise RuntimeError(
            f"git repo detection failed inside a git repo ({failure}); "
            f"refusing to write a global-graph index into {output_dir}"
        )
    return None


def _kin_declared_audience(output_dir: Path) -> str:
    """Audience declared by the project's own .kin config ('' if none)."""
    import yaml

    config_file = output_dir / ".kin" / "config"
    try:
        if config_file.is_file():
            data = yaml.safe_load(config_file.read_text()) or {}
            return str(data.get("audience", "") or "")
    except (OSError, yaml.YAMLError):
        pass
    return ""


def write_kin_index(store: "Store", output_dir: Path) -> Path:
    """Write .kin/index.json summarizing the graph for this project.

    The .kin/ directory is the standard location for all kindex project
    artifacts.  This file is meant to be git-tracked, giving other tools
    a snapshot of what Kindex knows about this project.  When run inside
    a git repo, the index is scoped to code nodes belonging to that repo only
    (repo-scoped selection is always preferred over the global fallback).
    The non-repo fallback is a global head — since the file is meant to be
    committed and shared, it only includes public/team-audience nodes unless
    the repo's own .kin config declares ``audience: private``.

    Raises RuntimeError (from _detect_repo_for_index) when git detection
    fails inside what is visibly a git repo — the global fallback head must
    never be committed into a real repo by accident.
    """
    repo_slug = _detect_repo_for_index(output_dir)

    if repo_slug:
        # Query code nodes belonging to this repo by ID prefix
        mod_prefix = f"code-mod-{repo_slug}-"
        sym_prefix = f"code-sym-{repo_slug}-"
        rows = store.conn.execute(
            "SELECT * FROM nodes WHERE id LIKE ? OR id LIKE ? "
            "ORDER BY id ASC",
            (f"{mod_prefix}%", f"{sym_prefix}%"),
        ).fetchall()
        # Post-filter to the exact id shape the code adapter emits
        # (code-{mod|sym}-{slug}-{12 hex chars}). The LIKE prefix has no
        # terminator and slugs contain hyphens, so 'code-mod-api-%' would
        # otherwise also match another repo's 'code-mod-api-server-…' ids
        # — leaking that repo's (private-by-default) module/symbol names
        # into this repo's git-tracked index.
        id_shape = re.compile(
            rf"^code-(mod|sym)-{re.escape(repo_slug)}-[0-9a-f]{{12}}$"
        )
        nodes = [n for n in (store._row_to_dict(r) for r in rows)
                 if id_shape.match(n["id"])]
    elif _kin_declared_audience(output_dir) == "private":
        # Explicitly private index: the repo owner opted in to a full snapshot.
        rows = store.conn.execute(
            "SELECT * FROM nodes ORDER BY id ASC LIMIT ?",
            (500,),
        ).fetchall()
        nodes = [store._row_to_dict(r) for r in rows]
    else:
        rows = store.conn.execute(
            "SELECT * FROM nodes WHERE audience IN ('public', 'team') "
            "ORDER BY id ASC LIMIT ?",
            (500,),
        ).fetchall()
        nodes = [store._row_to_dict(r) for r in rows]

    # NB: no volatile timestamp here (e.g. source_updated_at). It would change on
    # every regeneration, churning git history and conflicting on every concurrent
    # merge; the commit time already records snapshot freshness. The file is a pure
    # function of the node set so a structured union merge stays lossless.
    from .kin_merge import KIN_INDEX_SCHEMA_VERSION

    index = {
        "domains": sorted(set(d for n in nodes for d in (n.get("domains") or []))),
        "node_count": len(nodes),
        "nodes": [_kin_index_node(n) for n in nodes],
        "repo": repo_slug,
        "version": KIN_INDEX_SCHEMA_VERSION,
    }

    output_path = output_dir / ".kin" / "index.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    return output_path
