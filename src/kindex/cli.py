"""Kindex CLI (kin) — knowledge graph that learns from your conversations."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import yaml

from . import __version__
from .privacy import redacting_print as print


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        from .privacy import redact_text
        super().error(redact_text(message))


def _redacted_excepthook(error_type, error, traceback_object):
    """Keep useful unexpected-error traces without exposing credential values."""
    import traceback
    print("".join(traceback.format_exception(error_type, error, traceback_object)),
          file=sys.stderr, end="")


def _json_default(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def _dumps(obj, **kw):
    return json.dumps(obj, default=_json_default, **kw)


def operation_now() -> str:
    """One normalized UTC instant for a time-dependent CLI operation.

    This module seam is intentionally monkeypatchable in-process, but it is not
    exposed as a command-line option.
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _config(args):
    from .config import load_config
    try:
        cfg = load_config(
            getattr(args, "config", None),
            project_path=getattr(args, "project_path", None),
            profile=getattr(args, "profile", None),
        )
    except ValueError as e:
        # Unknown profile (or otherwise invalid config) — fail clearly
        # instead of dumping a traceback or falling through to legacy.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if getattr(args, "data_dir", None):
        if cfg.active_profile:
            # Explicit --data-dir overriding a profile-resolved data_dir:
            # never stamp an unstamped database with the active profile
            # (an existing mismatched stamp still hard-refuses in Store).
            try:
                same = (Path(args.data_dir).expanduser().resolve()
                        == Path(cfg.data_dir).expanduser().resolve())
            except (OSError, ValueError):
                same = False
            if not same:
                cfg._stamp_on_open = False
        cfg.data_dir = args.data_dir
    return cfg


def _store(args):
    from .store import Store
    return Store(_config(args))


def _hook_store(args, cfg=None):
    """Open the graph with a short SQLite busy timeout for hook hot paths."""
    from .store import Store
    return Store(cfg or _config(args), sqlite_timeout=0.25)


def _ledger(args):
    from .budget import BudgetLedger
    cfg = _config(args)
    return BudgetLedger(cfg.ledger_path, cfg.budget), cfg


# ── search ─────────────────────────────────────────────────────────────

def cmd_search(args):
    """Hybrid search: FTS5 + graph traversal, merged via RRF."""
    store = _store(args)
    query = " ".join(args.query)
    include_archived = bool(getattr(args, "include_archived", False))
    trusted_only = bool(getattr(args, "trusted_only", False))
    evaluation_time = operation_now() if trusted_only else None

    from .retrieve import hybrid_search
    fence_stats: dict = {}
    results = hybrid_search(store, query, top_k=args.top_k,
                            include_archived=include_archived,
                            fence_stats=fence_stats,
                            trusted_only=trusted_only,
                            evaluation_time=evaluation_time)

    # Post-filters apply identically to results and fenced candidates so
    # the fence note reflects the same filter set the results use.
    fenced_nodes = fence_stats.get("fenced_nodes", [])

    # --tags: filter by tag membership
    if getattr(args, "tags", None):
        filter_tags = {t.strip().lower() for t in args.tags.split(",") if t.strip()}

        def _tag_match(r):
            return bool(filter_tags & {d.lower() for d in (r.get("domains") or [])})

        results = [r for r in results if _tag_match(r)]
        fenced_nodes = [r for r in fenced_nodes if _tag_match(r)]

    # --mine: filter to nodes owned by current user
    if getattr(args, "mine", False):
        cfg = _config(args)
        me = cfg.current_user

        def _mine_match(r):
            extra = r.get("extra")
            owner = extra.get("owner") if isinstance(extra, dict) else None
            return me in (r.get("prov_who") or []) or owner == me

        results = [r for r in results if _mine_match(r)]
        fenced_nodes = [r for r in fenced_nodes if _mine_match(r)]

    # The fence note is derived in a single place both surfaces call (R3.1).
    from .retrieve import build_fence_note
    fence_note = build_fence_note(results, fenced_nodes, args.top_k,
                                  include_archived,
                                  candidate_count=fence_stats.get("candidate_count", 0))
    trust_note = ""
    if trusted_only:
        from .retrieve import build_trust_note
        trust_note = build_trust_note(fence_stats.get("trusted_omissions"))

    if not results:
        print("No results.", file=sys.stderr)
        if fence_note:
            if args.json:
                print(fence_note, file=sys.stderr)
            else:
                print(fence_note)
        if trust_note and not args.json:
            print(trust_note)
        return

    if args.json:
        out = [{
            "id": r["id"], "type": r["type"], "title": r["title"],
            "weight": r["weight"], "rrf_score": r.get("rrf_score", 0),
            "content_preview": (r.get("content") or "")[:300],
            "edges": [{"to": e["to_id"], "type": e["type"], "weight": e["weight"]}
                      for e in r.get("edges_out", [])],
        } for r in results]
        print(_dumps(out, indent=2))
        if fence_note:
            print(fence_note, file=sys.stderr)
    else:
        print(f"# Kindex:{len(results)} results for \"{query}\"\n")
        for r in results:
            title = r.get("title", r["id"])
            ntype = r.get("type", "concept")
            weight = r.get("weight", 0)
            content = (r.get("content") or "")[:200]
            edges = r.get("edges_out", [])

            print(f"## [{ntype}] {title} (w={weight:.2f})")
            if content:
                print(f"  {content}")
            if edges:
                connected = ", ".join(e.get("to_title", e["to_id"]) for e in edges[:5])
                print(f"  → {connected}")
            print()
        if fence_note:
            print(fence_note)
        if trust_note:
            print(trust_note)

    store.close()


# ── context ────────────────────────────────────────────────────────────

def cmd_context(args):
    """Output formatted context block for CLAUDE.md injection.

    Supports five context tiers: full, abridged, summarized, executive, index.
    Auto-selects based on --tokens if --level is not specified.
    """
    store = _store(args)

    from .retrieve import (
        auto_select_tier, detect_domain_from_path, format_context_block, hybrid_search,
    )

    # Auto-detect topic from $PWD if not specified
    topic = args.topic
    if not topic:
        cwd = os.getcwd()
        domains = detect_domain_from_path(store, cwd)
        if domains:
            topic = " ".join(domains)
        else:
            topic = os.path.basename(cwd)

    level = getattr(args, "level", None)
    tokens = getattr(args, "tokens", None)

    trusted_only = bool(getattr(args, "trusted_only", False))
    evaluation_time = operation_now() if trusted_only else None
    fence_stats: dict = {}
    results = hybrid_search(
        store,
        topic,
        top_k=args.depth or 10,
        trusted_only=trusted_only,
        evaluation_time=evaluation_time,
        fence_stats=fence_stats,
    )

    if args.format == "json":
        tier = level or auto_select_tier(tokens)
        print(_dumps({"query": topic, "level": tier, "results": [{
            "id": r["id"], "title": r["title"], "type": r["type"],
        } for r in results]}, indent=2))
    else:
        block = format_context_block(store, results, query=topic,
                                     level=level, max_tokens_approx=tokens,
                                     trusted_only=trusted_only,
                                     evaluation_time=evaluation_time)
        if trusted_only:
            from .retrieve import build_trust_note
            block = block.rstrip() + "\n" + build_trust_note(
                fence_stats.get("trusted_omissions")
            )
        print(block)

    store.close()


# ── state-resilience review and trust surfaces ───────────────────────

def _state_error_code(exc: ValueError) -> str:
    from .store import (
        CandidateNotFoundError,
        CandidateStateError,
        InvalidIntervalError,
        StaleReviewError,
        TitleCollisionError,
    )

    if isinstance(exc, CandidateNotFoundError):
        return "candidate_not_found"
    if isinstance(exc, CandidateStateError):
        return "candidate_state"
    if isinstance(exc, StaleReviewError):
        return "stale_review"
    if isinstance(exc, TitleCollisionError):
        return "title_collision"
    if isinstance(exc, InvalidIntervalError):
        return "invalid_interval"
    return "invalid_input"


def _print_state_error(exc: ValueError) -> None:
    print(f"Error: {_state_error_code(exc)}: {exc}", file=sys.stderr)


def _neutralize_untrusted(value: object, *, keep_layout: bool = False) -> str:
    """Make hostile candidate text inert on a human terminal."""
    text = str(value if value is not None else "")
    chars: list[str] = []
    for char in text:
        code = ord(char)
        if keep_layout and char in ("\n", "\t"):
            chars.append(char)
        elif code < 32 or 127 <= code <= 159:
            chars.append(f"\\u{code:04x}")
        else:
            chars.append(char)
    return "".join(chars)


def _candidate_show_payload(store, candidate_id: str) -> dict:
    candidate = store.get_capture_candidate(candidate_id)
    if candidate is None:
        from .store import CandidateNotFoundError

        raise CandidateNotFoundError(f"Candidate not found: {candidate_id}")
    candidate["review_token"] = store.candidate_review_token(candidate_id)
    return candidate


def _print_candidate_human(candidate: dict) -> None:
    print("=== BEGIN UNTRUSTED CAPTURE CANDIDATE ===")
    print(f"ID: {_neutralize_untrusted(candidate.get('id'))}")
    print(f"Status: {_neutralize_untrusted(candidate.get('status'))}")
    print(f"Created: {_neutralize_untrusted(candidate.get('created_at'))}")
    print(f"Expires: {_neutralize_untrusted(candidate.get('expires_at'))}")
    if candidate.get("title") is not None:
        print(f"Title: {_neutralize_untrusted(candidate.get('title'))}")
        print(f"Type: {_neutralize_untrusted(candidate.get('node_type'))}")
        print(f"Domains: {_neutralize_untrusted(_dumps(candidate.get('domains') or []))}")
        print("Content:")
        print("--- BEGIN CANDIDATE CONTENT ---")
        print(_neutralize_untrusted(candidate.get("content"), keep_layout=True))
        print("--- END CANDIDATE CONTENT ---")
        print(
            "Connections: "
            + _neutralize_untrusted(_dumps(candidate.get("connections") or []))
        )
    print(f"Source digest: {candidate.get('source_digest', '')}")
    print(f"Payload digest: {candidate.get('payload_digest', '')}")
    print(f"Conflict IDs: {_dumps(candidate.get('conflict_ids') or [])}")
    print(f"Conflict codes: {_dumps(candidate.get('conflict_codes') or [])}")
    if candidate.get("created_node_id"):
        print(f"Created node: {candidate['created_node_id']}")
    print(f"Review token: {candidate.get('review_token', '')}")
    print("=== END UNTRUSTED CAPTURE CANDIDATE ===")


def cmd_candidate(args):
    """Review quarantined automatic-capture candidates."""
    store = _store(args)
    action = args.candidate_action
    candidate_id = getattr(args, "candidate_id", None)
    operation_instant = (
        operation_now() if action in ("accept", "reject", "prune") else None
    )
    try:
        if action == "list":
            result = store.list_capture_candidates(
                status=getattr(args, "status", "") or "",
                limit=getattr(args, "limit", 20),
            )
            if args.json:
                print(_dumps(result, indent=2))
            elif not result:
                print("No capture candidates.")
            else:
                for candidate in result:
                    print(
                        f"[{candidate['status']}] {candidate['id']} "
                        f"created={candidate['created_at']} expires={candidate['expires_at']}"
                    )
            return
        if not candidate_id and action != "prune":
            raise ValueError(f"candidate ID is required for {action}")
        if action == "show":
            result = _candidate_show_payload(store, candidate_id)
        elif action == "accept":
            result = store.accept_capture_candidate(
                candidate_id,
                review_token=getattr(args, "review_token", None) or "",
                reviewed_by=getattr(args, "by", None) or "",
                prov_method=getattr(args, "method", None) or "",
                valid_at=getattr(args, "valid_at", None),
                invalid_at=getattr(args, "invalid_at", None),
                now=operation_instant,
            )
        elif action == "reject":
            result = store.reject_capture_candidate(
                candidate_id,
                reviewed_by=getattr(args, "by", None) or "",
                disposition_code=getattr(args, "code", None) or "",
                now=operation_instant,
            )
        elif action == "prune":
            count = store.prune_capture_candidates(now=operation_instant)
            result = {"pruned": count}
        elif action == "erase":
            result = {"id": candidate_id, "erased": store.erase_capture_candidate(candidate_id)}
        else:
            raise ValueError(f"Unknown candidate action: {action}")

        if args.json:
            print(_dumps(result, indent=2))
        elif action == "show":
            _print_candidate_human(result)
        elif action == "accept" and result.get("status") == "conflicted":
            print(
                f"Candidate {candidate_id} remains conflicted: "
                f"{', '.join(result.get('conflict_codes') or [])}"
            )
        elif action == "accept":
            print(f"Accepted {candidate_id} -> node {result.get('created_node_id')}")
        elif action == "reject":
            print(f"Rejected {candidate_id}")
        elif action == "prune":
            print(f"Expired {result['pruned']} capture candidate(s).")
        elif action == "erase":
            print(f"Erased {candidate_id}: {result['erased']}")
    except ValueError as exc:
        _print_state_error(exc)
    finally:
        store.close()


def _resolve_cli_node(store, identity: str) -> dict:
    node = store.get_node(identity) or store.get_node_by_title(identity)
    if node is None:
        raise ValueError(f"Node not found: {identity}")
    return node


def cmd_verify(args):
    store = _store(args)
    operation_instant = operation_now()
    try:
        node = _resolve_cli_node(store, args.node)
        result = store.verify_node(
            node["id"],
            verified_by=args.by or "",
            prov_method=args.method or "",
            verified_at=getattr(args, "verified_at", None) or operation_instant,
            valid_at=getattr(args, "valid_at", None),
            invalid_at=getattr(args, "invalid_at", None),
        )
        if args.json:
            print(_dumps(result, indent=2))
        else:
            print(
                f"Verified {result['id']} by {result['verified_by']} "
                f"via {result['prov_method']} at {result['verified_at']}"
            )
    except ValueError as exc:
        _print_state_error(exc)
    finally:
        store.close()


def cmd_invalidate(args):
    store = _store(args)
    operation_instant = operation_now()
    try:
        node = _resolve_cli_node(store, args.node)
        result = store.invalidate_node(
            node["id"],
            invalidated_by=args.by or "",
            disposition_code=args.code or "",
            invalid_at=getattr(args, "at", None) or operation_instant,
        )
        if args.json:
            print(_dumps(result, indent=2))
        else:
            print(f"Invalidated {result['id']} at {result['invalid_at']}")
    except ValueError as exc:
        _print_state_error(exc)
    finally:
        store.close()


# ── add ────────────────────────────────────────────────────────────────

def _referent_binding_from_args(args) -> dict:
    """Build add_node binding kwargs from the --referent flags ({} if unused).

    File-scope referents are hashed here (relative paths resolve against the
    cwd) unless an explicit --referent-digest is supplied; url/repo scopes
    require the explicit digest. Fail-closed: an unhashable file with no
    digest is an error, never a silent unbound add.
    """
    raw = getattr(args, "referent", None)
    asserted = getattr(args, "asserted_at", None)
    true_of = getattr(args, "true_of", None)
    if not raw:
        if asserted or true_of:
            return {"asserted_at": asserted, "true_of": true_of}
        return {}
    from .referent import ReferentError, hash_file, validate_referent

    scope = getattr(args, "referent_scope", None) or (
        "url" if "://" in raw else "file")
    digest = getattr(args, "referent_digest", None)
    if not digest:
        if scope != "file":
            print(f"Error: --referent-digest is required for scope '{scope}'",
                  file=sys.stderr)
            sys.exit(1)
        try:
            digest = hash_file(Path(raw))
        except OSError as e:
            print(f"Error: cannot hash referent file '{raw}': {e}",
                  file=sys.stderr)
            sys.exit(1)
    key = "url" if scope == "url" else "path"
    ref = {key: raw, "content_digest": digest, "digest_scope": scope}
    try:
        validate_referent(ref)
    except ReferentError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return {"referent": ref, "asserted_at": asserted, "true_of": true_of}


def cmd_add(args):
    """Quick capture with auto-extraction and linking.

    For operational types (constraint, directive, checkpoint, watch),
    creates the node directly with metadata from flags.
    For knowledge types, runs the extraction pipeline — unless a referent
    binding is supplied, which implies direct creation (extraction would
    rewrite the claim and decouple it from what it was bound to).
    """
    store = _store(args)
    ledger, cfg = _ledger(args)
    content = " ".join(args.note)
    node_type = args.type or "concept"
    tag_list = [t.strip() for t in args.tags.split(",") if t.strip()] if getattr(args, "tags", None) else []
    binding = _referent_binding_from_args(args)

    # Resolve current user for provenance
    cfg = _config(args)
    current_user = cfg.current_user

    # Operational types get direct creation with metadata
    operational = {"constraint", "directive", "checkpoint", "watch"}
    if node_type in operational:
        extra = {}
        if args.trigger:
            extra["trigger"] = args.trigger
        if args.action:
            extra["action"] = args.action
        if args.scope:
            extra["scope"] = args.scope
        if args.owner:
            extra["owner"] = args.owner
        if args.expires:
            extra["expires"] = args.expires
        if args.resets:
            extra["resets"] = args.resets
        if getattr(args, "attention_trigger", None):
            extra["attention_triggers"] = [
                t.strip() for t in args.attention_trigger.split(",") if t.strip()
            ]

        nid = store.add_node(
            title=content,
            content="",
            node_type=node_type,
            audience=args.audience or "private",
            tags=tag_list,
            prov_activity="manual-add",
            prov_source="cli",
            prov_who=[current_user],
            extra=extra,
            **binding,
        )
        label = node_type.capitalize()
        print(f"  {label}: {content} ({nid})")
        if extra:
            for k, v in extra.items():
                print(f"    {k}: {v}")
        print(f"\n1 {node_type} added.")
        store.close()
        return

    # A referent-bound claim is created directly: extraction would rewrite
    # the text and decouple the claim from the state it was bound to.
    if binding:
        nid = store.add_node(
            title=content[:60].strip() or content,
            content=content,
            node_type=node_type,
            audience=args.audience or "private",
            tags=tag_list,
            prov_activity="manual-add",
            prov_source="cli",
            prov_who=[current_user],
            **binding,
        )
        ref = binding.get("referent") or {}
        bound = ref.get("path") or ref.get("url") or "clocks only"
        print(f"  Added (bound to {bound}): {content[:60]} ({nid})")
        store.close()
        return

    # Knowledge types — run extraction pipeline
    from .extract import extract

    existing = [n["title"] for n in store.all_nodes(limit=200)]
    extraction = extract(content, existing, cfg, ledger)

    created_ids = []

    # Add extracted concepts
    for concept in extraction.get("concepts", []):
        existing_node = store.get_node_by_title(concept["title"])
        if existing_node:
            old_content = existing_node.get("content", "")
            new_content = concept.get("content", "")
            if new_content and new_content not in old_content:
                store.update_node(existing_node["id"],
                                  content=old_content + "\n\n" + new_content)
                print(f"  Updated: {concept['title']}")
            continue

        nid = store.add_node(
            title=concept["title"],
            content=concept.get("content", content),
            node_type=concept.get("type", node_type),
            domains=concept.get("domains", []),
            tags=tag_list,
            prov_activity="manual-add",
            prov_source="cli",
            prov_who=[current_user],
        )
        created_ids.append(nid)
        print(f"  Created: {concept['title']} ({nid})")

    # If no concepts extracted, create a single node from the raw text
    if not extraction.get("concepts"):
        title = content[:60].strip()
        if len(content) > 60:
            title += "..."
        nid = store.add_node(
            title=title, content=content, node_type=node_type,
            tags=tag_list,
            prov_activity="manual-add", prov_source="cli",
            prov_who=[current_user],
        )
        created_ids.append(nid)
        print(f"  Created: {title} ({nid})")

    # Add extracted decisions
    for decision in extraction.get("decisions", []):
        nid = store.add_node(
            title=decision["title"],
            content=decision.get("rationale", ""),
            node_type="decision",
            prov_activity="manual-add",
        )
        created_ids.append(nid)
        print(f"  Decision: {decision['title']} ({nid})")

    # Add extracted questions
    for question in extraction.get("questions", []):
        nid = store.add_node(
            title=question["question"],
            content=question.get("context", ""),
            node_type="question",
            status="open-question",
            prov_activity="manual-add",
        )
        created_ids.append(nid)
        print(f"  Question: {question['question']} ({nid})")

    # Add connections
    for conn in extraction.get("connections", []):
        from_node = store.get_node_by_title(conn.get("from_title", ""))
        to_node = store.get_node_by_title(conn.get("to_title", ""))
        if from_node and to_node:
            store.add_edge(from_node["id"], to_node["id"],
                           edge_type=conn.get("type", "relates_to"),
                           provenance=conn.get("why", "extracted"))
            print(f"  Linked: {conn['from_title']} → {conn['to_title']}")

    # Ensure no orphans — link created nodes to each other if multiple
    if len(created_ids) > 1:
        for i in range(len(created_ids) - 1):
            store.add_edge(created_ids[i], created_ids[i + 1],
                           provenance="co-created")

    print(f"\n{len(created_ids)} node(s) added.")
    store.close()


# ── learn ──────────────────────────────────────────────────────────────

def cmd_learn(args):
    """Extract knowledge from a Claude Code session or inbox."""
    store = _store(args)
    ledger, cfg = _ledger(args)

    if args.from_inbox:
        inbox_dir = cfg.inbox_dir
        if not inbox_dir.exists():
            print("No inbox directory.", file=sys.stderr)
            return

        from .vault import parse_frontmatter
        count = 0
        for f in sorted(inbox_dir.glob("*.md")):
            meta, body = parse_frontmatter(f)
            if meta.get("processed"):
                continue

            content = meta.get("content", body or "")
            if isinstance(content, str) and content.strip():
                from .extract import extract
                existing = [n["title"] for n in store.all_nodes(limit=200)]
                extraction = extract(content, existing, cfg, ledger)

                for concept in extraction.get("concepts", []):
                    if not store.get_node_by_title(concept["title"]):
                        store.add_node(
                            title=concept["title"],
                            content=concept.get("content", content),
                            node_type=concept.get("type", "concept"),
                            domains=concept.get("domains", []),
                            prov_source=str(f.name),
                        )
                        print(f"  Extracted: {concept['title']}")
                        count += 1

            # Mark as processed
            meta["processed"] = True
            from .vault import serialize_frontmatter
            f.write_text(serialize_frontmatter(meta, body))

        print(f"\nProcessed inbox: {count} new node(s).")
    else:
        print("Usage: conv learn --from-inbox", file=sys.stderr)
        print("Session learning will be added with archive integration.", file=sys.stderr)

    store.close()


# ── link ───────────────────────────────────────────────────────────────

def cmd_link(args):
    """Create an edge between two nodes."""
    store = _store(args)

    node_a = store.get_node(args.node_a) or store.get_node_by_title(args.node_a)
    node_b = store.get_node(args.node_b) or store.get_node_by_title(args.node_b)

    if not node_a:
        print(f"Error: '{args.node_a}' not found.", file=sys.stderr)
        sys.exit(1)
    if not node_b:
        print(f"Error: '{args.node_b}' not found.", file=sys.stderr)
        sys.exit(1)

    store.add_edge(node_a["id"], node_b["id"],
                   edge_type=args.relationship,
                   weight=args.weight,
                   provenance=args.why or "")
    print(f"Linked: {node_a['title']} —[{args.relationship}]→ {node_b['title']}")
    store.close()


# ── show ───────────────────────────────────────────────────────────────

def cmd_show(args):
    """Show full node with edges and provenance."""
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    edges_out = store.edges_from(node["id"])
    edges_in = store.edges_to(node["id"])

    if args.json:
        node["edges_out"] = edges_out
        node["edges_in"] = edges_in
        print(_dumps(node, indent=2))
    else:
        print(f"# {node['title']} [{node['type']}]")
        print(f"**ID:** {node['id']}")
        print(f"**Weight:** {node['weight']:.2f}")
        print(f"**Status:** {node['status']}")
        print(f"**Tags:** {', '.join(node.get('tags') or node.get('domains') or [])}")
        if node.get("aka"):
            print(f"**AKA:** {', '.join(node['aka'])}")
        if node.get("intent"):
            print(f"**Intent:** {node['intent']}")
        if node.get("prov_source"):
            print(f"**Source:** {node['prov_source']}")
        if node.get("prov_when"):
            print(f"**When:** {node['prov_when']}")

        # Display current_state if present (mutable directive state)
        extra = node.get("extra") or {}
        current_state = extra.get("current_state")
        if current_state:
            print(f"\n**Current State:**")
            for k, v in current_state.items():
                print(f"  {k}: {v}")
            state_updated = extra.get("state_updated_at")
            if state_updated:
                print(f"  (updated: {state_updated})")

        if node.get("content"):
            print(f"\n{node['content'][:1000]}")

        if edges_out:
            print(f"\n## Outgoing ({len(edges_out)})")
            for e in edges_out:
                print(f"  → {e.get('to_title', e['to_id']):30s} [{e['type']}] w={e['weight']:.2f}  {e.get('provenance', '')[:60]}")

        if edges_in:
            print(f"\n## Incoming ({len(edges_in)})")
            for e in edges_in:
                print(f"  ← {e.get('from_title', e['from_id']):30s} [{e['type']}] w={e['weight']:.2f}")

    store.close()


# ── list / recent / orphans ────────────────────────────────────────────

def cmd_list(args):
    store = _store(args)
    tag_list = [t.strip() for t in args.tags.split(",") if t.strip()] if getattr(args, "tags", None) else None
    nodes = store.all_nodes(
        node_type=args.type, status=args.status,
        audience=getattr(args, "audience", None),
        tags=tag_list,
        limit=args.limit or 100,
    )

    # --mine: filter to nodes owned by current user
    if getattr(args, "mine", False):
        cfg = _config(args)
        me = cfg.current_user
        nodes = [n for n in nodes if me in (n.get("prov_who") or [])
                 or (n.get("extra") or {}).get("owner") == me]

    if args.json:
        print(_dumps([{"id": n["id"], "type": n["type"], "title": n["title"],
                        "weight": n["weight"], "status": n["status"]}
                       for n in nodes], indent=2))
    else:
        for n in nodes:
            print(f"  [{n['type'][:4]:4s}] {n['title'][:50]:50s} w={n['weight']:.2f}  {n['id']}")

    store.close()


def cmd_recent(args):
    store = _store(args)
    nodes = store.recent_nodes(n=args.n)

    for n in nodes:
        when = n.get("updated_at", "")[:16]
        print(f"  {when}  [{n['type'][:4]}] {n['title'][:50]}  {n['id']}")

    store.close()


def cmd_orphans(args):
    store = _store(args)
    orphans = store.orphans()

    if orphans:
        print(f"{len(orphans)} semantic orphan(s):")
        for n in orphans:
            print(f"  {n['id']}  [{n['type']}] {n['title']}")
    else:
        print("No semantic orphans. Graph health: good.")

    store.close()


# ── status / budget ────────────────────────────────────────────────────

def cmd_status(args):
    store = _store(args)
    trigger = getattr(args, "trigger", None)
    owner = getattr(args, "owner", None)
    filter_type = getattr(args, "type", None)

    # --mine resolves to current user
    if getattr(args, "mine", False) and not owner:
        cfg = _config(args)
        owner = cfg.current_user

    # If requesting operational status (trigger or specific operational type)
    operational_types = {"constraint", "directive", "checkpoint", "watch"}
    if trigger or filter_type in operational_types:
        ops = store.operational_summary(trigger=trigger, owner=owner)

        if args.json:
            print(_dumps({
                "trigger": trigger,
                "owner": owner,
                "constraints": [{"id": n["id"], "title": n["title"],
                                 "action": (n.get("extra") or {}).get("action", "warn"),
                                 "trigger": (n.get("extra") or {}).get("trigger", "")}
                                for n in ops["constraints"]],
                "checkpoints": [{"id": n["id"], "title": n["title"],
                                 "trigger": (n.get("extra") or {}).get("trigger", "")}
                                for n in ops["checkpoints"]],
                "watches": [{"id": n["id"], "title": n["title"],
                             "owner": (n.get("extra") or {}).get("owner", ""),
                             "expires": (n.get("extra") or {}).get("expires", "")}
                            for n in ops["watches"]],
                "directives": [{"id": n["id"], "title": n["title"],
                                "scope": (n.get("extra") or {}).get("scope", "")}
                               for n in ops["directives"]],
            }, indent=2))
        else:
            if trigger:
                print(f"# Operational status for trigger: {trigger}\n")
            else:
                print("# Operational status\n")

            if ops["constraints"]:
                print(f"## Constraints ({len(ops['constraints'])})")
                for n in ops["constraints"]:
                    extra = n.get("extra") or {}
                    action = extra.get("action", "warn")
                    trig = extra.get("trigger", "")
                    print(f"  [{action:5s}] {n['title'][:60]}")
                    if trig:
                        print(f"         trigger: {trig}")
                print()

            if ops["checkpoints"]:
                print(f"## Checkpoints ({len(ops['checkpoints'])})")
                for n in ops["checkpoints"]:
                    trig = (n.get("extra") or {}).get("trigger", "")
                    print(f"  [ ] {n['title'][:60]}")
                    if trig:
                        print(f"      trigger: {trig}")
                print()

            if ops["watches"]:
                print(f"## Watches ({len(ops['watches'])})")
                for n in ops["watches"]:
                    extra = n.get("extra") or {}
                    who = extra.get("owner", "")
                    exp = extra.get("expires", "")
                    suffix = ""
                    if who:
                        suffix += f" @{who}"
                    if exp:
                        suffix += f" (expires {exp})"
                    print(f"  ! {n['title'][:55]}{suffix}")
                print()

            if ops["directives"]:
                print(f"## Directives ({len(ops['directives'])})")
                for n in ops["directives"]:
                    scope = (n.get("extra") or {}).get("scope", "")
                    print(f"  > {n['title'][:60]}")
                    if scope:
                        print(f"    scope: {scope}")
                print()

            total = sum(len(v) for v in ops.values())
            if total == 0:
                print("No active operational nodes.")

        store.close()
        return

    # Standard graph stats
    stats = store.stats()
    from .store import SCHEMA_RECOVERY_PATH_META, SCHEMA_RECOVERY_REASON_META
    recovery_path = store.get_meta(SCHEMA_RECOVERY_PATH_META)
    recovery_reason = store.get_meta(SCHEMA_RECOVERY_REASON_META)
    from .archive import ARCHIVE_DUPLICATE_COUNT_META, ARCHIVE_DUPLICATE_IDS_META
    try:
        archive_duplicate_count = int(
            store.get_meta(ARCHIVE_DUPLICATE_COUNT_META) or 0
        )
    except (TypeError, ValueError):
        archive_duplicate_count = 0
    try:
        archive_duplicate_ids = json.loads(
            store.get_meta(ARCHIVE_DUPLICATE_IDS_META) or "[]"
        )
    except (TypeError, json.JSONDecodeError):
        archive_duplicate_ids = []
    if not isinstance(archive_duplicate_ids, list):
        archive_duplicate_ids = []

    cfg = _config(args)
    from .config import read_degraded_events
    degraded = read_degraded_events(cfg, override_dir=getattr(args, "data_dir", None))
    if args.json:
        stats["profile"] = cfg.active_profile
        stats["profile_source"] = cfg.profile_source if cfg.active_profile else None
        if degraded:
            last = degraded[-1]
            stats["degraded_7d"] = len(degraded)
            stats["degraded_last"] = {"cmd": last.get("cmd"),
                                      "error_class": last.get("error_class"),
                                      "ts": last.get("ts")}
        if recovery_path:
            stats["schema_recovery"] = {
                "path": recovery_path,
                "reason": recovery_reason,
            }
        if archive_duplicate_count:
            stats["archive_duplicates"] = {
                "count": archive_duplicate_count,
                "sample_ids": archive_duplicate_ids,
            }
        print(_dumps(stats, indent=2))
    else:
        if cfg.active_profile:
            print(f"Profile: {cfg.active_profile} (via {cfg.profile_source})")
        else:
            print("Profile: (none — legacy single-graph)")
        print(f"Nodes:     {stats['semantic_nodes']} semantic")
        print(f"Edges:     {stats['edges']} semantic")
        print(f"Orphans:   {stats['orphans']}")
        print(f"Metrics:   schema {stats['metrics_schema']}")
        if recovery_path:
            display_path = "".join(
                char if char.isprintable() else "?" for char in recovery_path
            )[:1000]
            print(
                f"Recovery:  {display_path} "
                f"({recovery_reason or 'schema migration'})"
            )
        if archive_duplicate_count:
            print(
                "Archive:   "
                f"{archive_duplicate_count} duplicate ID(s) need review"
            )
        stored_nodes = stats["stored_nodes"]
        semantic_nodes = stats["semantic_nodes"]
        excluded_nodes = stored_nodes - semantic_nodes
        ignored = stats.get("stored_edges", 0) - stats.get("edges", 0)
        if excluded_nodes or ignored:
            print(
                f"Stored:    {stored_nodes} nodes, {stats['stored_edges']} edges "
                f"({excluded_nodes} lifecycle nodes, {ignored} excluded edges)"
            )
            print(f"  domain-derived {stats.get('ignored_domain_edges', 0)}")
            print(f"  session-linked {stats.get('ignored_session_edges', 0)}")
            other = stats.get("ignored_other_edges", 0)
            if other:
                print(f"  unresolved      {other}")
        print(f"\nBy type:")
        for t, c in sorted(stats.get("types", {}).items()):
            print(f"  {t:12s} {c}")

        # Summary of active operational nodes
        ops = store.operational_summary()
        op_count = sum(len(v) for v in ops.values())
        if op_count > 0:
            print(f"\nOperational: {len(ops['constraints'])} constraints, "
                  f"{len(ops['checkpoints'])} checkpoints, "
                  f"{len(ops['watches'])} watches, "
                  f"{len(ops['directives'])} directives")

        if degraded:
            last = degraded[-1]
            print(f"\nDegraded (7d): {len(degraded)} hook event(s) — "
                  f"last: {last.get('cmd', '?')} ({last.get('error_class', '?')})")

    store.close()


def cmd_budget(args):
    ledger, _ = _ledger(args)
    conversation_id = getattr(args, "conversation_id", None)
    s = ledger.summary(conversation_id=conversation_id)

    if args.json:
        print(_dumps(s, indent=2))
    else:
        print("LLM Budget")
        for period in ["today", "week", "month"]:
            d = s[period]
            bar_len = 20
            pct = d["spent"] / d["limit"] if d["limit"] > 0 else 0
            filled = int(min(pct, 1.0) * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"  {period:6s} {bar} ${d['spent']:.4f} / ${d['limit']:.2f}")
        status = "OK" if s["can_spend"] else "LIMIT REACHED"
        print(f"\n  Status: {status}")
        if "conversation" in s:
            c = s["conversation"]
            print(
                f"  Conversation {c['id']}: "
                f"${c['spent']:.4f} total, ${c['spent_today']:.4f} today"
            )


# ── init / migrate / doctor ────────────────────────────────────────────

def cmd_init(args):
    cfg = _config(args)
    dp = cfg.data_path
    if (dp / "kindex.db").exists() or (dp / "conv.db").exists():
        print(f"Error: database already exists at {dp}", file=sys.stderr)
        sys.exit(1)

    synonyms_dir = dp / "synonyms"
    for d in [cfg.topics_dir, cfg.skills_dir, cfg.inbox_dir, cfg.tmp_dir, synonyms_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Create the database
    from .store import Store
    store = Store(cfg)
    _ = store.conn  # triggers schema creation
    store.close()

    print(f"Initialized Kindex at {dp}")
    print(f"  kindex.db  — SQLite knowledge graph")
    print(f"  topics/    — markdown topic files")
    print(f"  skills/    — skill/ability files")
    print(f"  inbox/     — queued discoveries")
    print(f"  synonyms/  — synonym ring files (.syn)")


def cmd_migrate(args):
    """Import existing Conv markdown topics into the SQLite store."""
    cfg = _config(args)
    from .store import Store
    from .vault import Vault

    vault = Vault(cfg).load()
    store = Store(cfg)

    count = 0
    for slug, topic in vault.topics.items():
        existing = store.get_node(slug) or store.get_node_by_title(topic.title)
        if existing:
            continue

        nid = store.add_node(
            node_id=slug,
            title=topic.title or slug,
            content=topic.body,
            node_type="concept",
            weight=topic.weight or 0.5,
            domains=topic.domains,
            status=str(topic.status) if topic.status else "active",
            extra=topic.__pydantic_extra__ or {},
            prov_source=str(topic.path or ""),
        )
        count += 1

    # Import edges with bidirectional enforcement
    for slug, topic in vault.topics.items():
        for edge in topic.connects_to:
            if store.get_node(edge.target):
                store.add_edge(slug, edge.target,
                               weight=edge.weight,
                               provenance=edge.reason,
                               bidirectional=True)

    # Import skills
    for slug, skill in vault.skills.items():
        if store.get_node(slug):
            continue
        store.add_node(
            node_id=slug,
            title=skill.title or slug,
            content=skill.body,
            node_type="skill",
            domains=skill.domains,
            prov_source=str(skill.path or ""),
        )
        count += 1

        for edge in skill.connects_to:
            if store.get_node(edge.target):
                store.add_edge(slug, edge.target,
                               weight=edge.weight,
                               provenance=edge.reason,
                               bidirectional=True)

    stats = store.stats()
    print(f"Migrated: {count} new nodes")
    print(f"Total: {stats['nodes']} nodes, {stats['edges']} edges, {stats['orphans']} orphans")
    store.close()


def cmd_extract(args):
    """Extraction engine tools: compare engines against the local corpus."""
    action = getattr(args, "extract_action", None) or "eval"
    store = _store(args)
    cfg = _config(args)

    if action == "engines":
        from .extractors import DeterministicExtractor
        from .llm import resolve_api_key
        rows = [
            ("keyword", True, "pure Python, always available — the baseline"),
            ("llm", bool(resolve_api_key(cfg)[0]),
             f"{cfg.llm.provider} {cfg.llm.model}"),
            ("deterministic", DeterministicExtractor.available(),
             "optional kindex[talon] extra (~2.5 GB)"),
        ]
        if args.json:
            print(_dumps([{"engine": n, "available": a, "detail": d}
                          for n, a, d in rows], indent=2))
        else:
            for name, available, detail in rows:
                mark = "available" if available else "not installed"
                print(f"  {name:<15} {mark:<15} {detail}")
        store.close()
        return

    # eval
    from .budget import BudgetLedger
    from .extract_eval import run_eval

    engines = tuple(
        e.strip() for e in (getattr(args, "engines", None)
                            or "keyword,deterministic").split(",") if e.strip())
    result = run_eval(store, cfg, engines=engines,
                      limit=getattr(args, "limit", 200),
                      ledger=BudgetLedger(cfg.ledger_path, cfg.budget))

    if args.json:
        print(_dumps(result, indent=2))
    elif result.get("status") != "ok":
        print(f"{result.get('status')}: {result.get('detail', '')}")
    else:
        print(f"Sample: {result['sample_size']} curated nodes "
              f"(title match >= {result['match_threshold']})\n")
        print(f"  {'engine':<16}{'grounded':>10}{'title recall':>14}"
              f"{'items/doc':>12}{'errors':>9}")
        for name, sc in result["scores"].items():
            print(f"  {name:<16}{sc['grounding_precision']:>10.1%}"
                  f"{sc['recall']:>14.1%}"
                  f"{sc['noise_items_per_doc']:>12.1f}{sc['errors']:>9}")
        print("\n  grounded = share of proposed items that really occur in the "
              "source (the precision question).\n  title recall = share of docs "
              "where the engine reproduced the curator's own title (harsh on "
              "this corpus).")
        gate = result.get("gate") or {}
        if gate:
            print()
            for name, g in gate.items():
                verdict = "PASSES" if g["beats_baseline"] else "FAILS"
                floor = "ok" if g["clears_grounding_floor"] else "BELOW"
                disc = "ok" if g["beats_title_recall"] else "no gain"
                print(f"  {name}: {verdict} the gate")
                print(f"      grounding floor: {floor} "
                      f"({g['grounding_delta']:+.1%} vs baseline)")
                print(f"      title recall:    {disc} "
                      f"({g['recall_delta']:+.1%} vs baseline)")
                print(f"      review cost:     {g['noise_delta']:+.1f} items/doc")
            if not result.get("passes_gate"):
                print("\n  No candidate engine cleared both parts of the gate. "
                      "An engine that cannot beat regexes has not earned its "
                      "dependencies.")
    store.close()


def cmd_doctor(args):
    """Comprehensive health check with graph invariants."""
    store = _store(args)
    stats = store.stats()
    issues = []
    warnings = []
    fixes_applied = 0
    do_fix = getattr(args, "fix", False)

    # ── Basic health ──
    if stats["nodes"] == 0:
        issues.append("No nodes — run `kin migrate` or `kin add` to create knowledge")

    # ── Orphan check ──
    orphans = store.orphans()
    if orphans:
        orphan_pct = len(orphans) / max(stats["nodes"], 1) * 100
        if orphan_pct > 30:
            issues.append(f"{len(orphans)} orphan nodes ({orphan_pct:.0f}%) — "
                          f"run `kin orphans` then `kin link`")
        elif orphan_pct > 10:
            warnings.append(f"{len(orphans)} orphan nodes ({orphan_pct:.0f}%)")

    # ── Weight distribution ──
    nodes = store.all_nodes(limit=10000)
    if nodes:
        weights = [n.get("weight", 0) for n in nodes]
        avg_weight = sum(weights) / len(weights)
        low_weight = sum(1 for w in weights if w < 0.1)
        if low_weight > len(weights) * 0.5:
            warnings.append(f"{low_weight}/{len(weights)} nodes have weight < 0.1 — "
                            f"run `kin decay` or boost important nodes")
        if avg_weight < 0.2:
            warnings.append(f"Average weight is {avg_weight:.2f} — graph may be over-decayed")

    # ── Stale nodes (not accessed in 90+ days) ──
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()[:10]
    stale = [n for n in nodes if (n.get("last_accessed") or "")[:10] < cutoff]
    if stale and len(stale) > len(nodes) * 0.3:
        warnings.append(f"{len(stale)} nodes not accessed in 90+ days")

    # ── Degraded hook events (last 7 days) ──
    from .config import read_degraded_events
    degraded_events = read_degraded_events(
        _config(args), override_dir=getattr(args, "data_dir", None))
    if degraded_events:
        last = degraded_events[-1]
        warnings.append(
            f"{len(degraded_events)} degraded hook event(s) in last 7 days — "
            f"last: {last.get('cmd', '?')} ({last.get('error_class', '?')}); "
            f"see degraded.jsonl")

    # ── Schema drift ──
    # A table can exist with the right name and the wrong shape while
    # schema_version reads current, because `CREATE TABLE IF NOT EXISTS` never
    # repairs an existing table. That is invisible to a table-existence check
    # and it silently killed the pheromone channel for three months, so this
    # asserts COLUMNS. --fix reopens the store, which replays migrations.
    drift = store.schema_drift()
    if drift:
        detail = "; ".join(
            f"{table} missing {', '.join(sorted(cols))}"
            for table, cols in sorted(drift.items())
        )
        issues.append(f"Schema drift: {detail} — run `kin doctor --fix`")
        if do_fix:
            store.close()
            store = _store(args)
            remaining = store.schema_drift()
            if remaining:
                issues[-1] += " (FIX FAILED — migration did not add the columns)"
            else:
                fixes_applied += 1
                issues[-1] += " (FIXED: migrations replayed)"

    # ── Silently-recovered failures ──
    # Counters bumped by recovery paths. A handled failure still emits a
    # signal; a rising count here is the leading indicator that a subsystem is
    # degraded but not complaining.
    failures = store.get_meta("pheromone.deposit_failures")
    if failures and failures != "0":
        warnings.append(
            f"{failures} pheromone deposit failure(s) recorded — the stigmergic "
            f"channel is degraded; check schema drift above")

    refusals = store.get_meta("dream.merge_refusals")
    if refusals and refusals != "0":
        warnings.append(
            f"{refusals} dream merge(s) refused by the runaway guards — usually "
            f"generated or minified content being matched against itself; "
            f"run `kin doctor --oversized` to see the targets")

    # ── Oversized nodes ──
    # A knowledge node past this size is not knowledge; it is ingested build
    # output or a runaway merge. They dominate embedding cost and poison
    # vector-space neighbourhoods.
    try:
        oversized = store.conn.execute(
            "SELECT id, substr(title,1,50) AS title, LENGTH(content) AS chars "
            "FROM nodes WHERE status='active' AND LENGTH(content) > 100000 "
            "ORDER BY LENGTH(content) DESC LIMIT 20"
        ).fetchall()
        if oversized:
            total_mb = sum(r["chars"] for r in oversized) / 1_048_576
            warnings.append(
                f"{len(oversized)} oversized node(s) holding {total_mb:.1f} MB — "
                f"largest: {oversized[0]['title']!r} at "
                f"{oversized[0]['chars'] // 1024} KB; archive with "
                f"`kin set-state <id> status archived`")
    except Exception:
        pass

    # ── FTS5 sync check ──
    try:
        fts_count = store.conn.execute(
            "SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
        node_count = stats["nodes"]
        if fts_count != node_count:
            issues.append(f"FTS5 index out of sync: {fts_count} indexed vs {node_count} nodes")
            if do_fix:
                store.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
                store.conn.commit()
                fixes_applied += 1
                issues[-1] += " (FIXED: rebuilt FTS5)"
    except Exception:
        warnings.append("Could not check FTS5 index health")

    # ── Dangling edges ──
    dangling = store.conn.execute(
        """SELECT COUNT(*) FROM edges WHERE
           from_id NOT IN (SELECT id FROM nodes) OR
           to_id NOT IN (SELECT id FROM nodes)"""
    ).fetchone()[0]
    if dangling:
        issues.append(f"{dangling} dangling edge(s) pointing to deleted nodes")
        if do_fix:
            store.conn.execute(
                """DELETE FROM edges WHERE
                   from_id NOT IN (SELECT id FROM nodes) OR
                   to_id NOT IN (SELECT id FROM nodes)""")
            store.conn.commit()
            fixes_applied += 1
            issues[-1] += " (FIXED: removed)"

    # ── Bidirectional invariant check ──
    one_way = store.conn.execute(
        """SELECT COUNT(*) FROM edges e1
           WHERE NOT EXISTS (
               SELECT 1 FROM edges e2
               WHERE e2.from_id = e1.to_id AND e2.to_id = e1.from_id
           )"""
    ).fetchone()[0]
    total_edges = stats["edges"]
    if total_edges > 0 and one_way > total_edges * 0.3:
        warnings.append(f"{one_way}/{total_edges} edges lack reverse — "
                        f"consider re-adding with bidirectional=True")

    # ── Empty content check ──
    empty = sum(1 for n in nodes if not (n.get("content") or "").strip())
    if empty > len(nodes) * 0.5 and len(nodes) > 5:
        warnings.append(f"{empty}/{len(nodes)} nodes have empty content")

    # ── Graph connectivity (bridge edges) ──
    if stats["nodes"] >= 5 and stats["edges"] >= 4:
        from .graph import store_stats as gstats
        gs = gstats(store)
        if gs["components"] > 1:
            warnings.append(f"Graph has {gs['components']} disconnected components")

    # ── Cross-domain bridge density ──
    if stats["nodes"] >= 5 and stats["edges"] >= 4:
        from .graph import build_nx_from_store
        G = build_nx_from_store(store)
        # Collect all unique domains across nodes
        domain_sets = {}
        for nid in G.nodes():
            domains = G.nodes[nid].get("domains") or []
            if isinstance(domains, str):
                domains = [domains]
            domain_sets[nid] = set(domains)
        all_domains = set()
        for ds in domain_sets.values():
            all_domains.update(ds)
        if len(all_domains) >= 2:
            total_edges_g = G.number_of_edges()
            cross_domain = 0
            for u, v in G.edges():
                u_doms = domain_sets.get(u, set())
                v_doms = domain_sets.get(v, set())
                if u_doms and v_doms and not u_doms.intersection(v_doms):
                    cross_domain += 1
            if total_edges_g > 0:
                cross_pct = cross_domain / total_edges_g
                if cross_pct < 0.10:
                    warnings.append(
                        f"Low cross-domain bridging: {cross_domain}/{total_edges_g} edges "
                        f"({cross_pct:.0%}) cross domain boundaries (< 10%)")
                    if do_fix:
                        # Suggest edges between nodes in different domains
                        import random
                        domain_nodes: dict[str, list[str]] = {}
                        for nid, doms in domain_sets.items():
                            for d in doms:
                                domain_nodes.setdefault(d, []).append(nid)
                        dom_list = list(domain_nodes.keys())
                        suggested = 0
                        for i in range(len(dom_list)):
                            for j in range(i + 1, len(dom_list)):
                                pool_a = domain_nodes[dom_list[i]]
                                pool_b = domain_nodes[dom_list[j]]
                                if pool_a and pool_b:
                                    a = random.choice(pool_a)
                                    b = random.choice(pool_b)
                                    a_title = G.nodes[a].get("title", a)
                                    b_title = G.nodes[b].get("title", b)
                                    store.add_suggestion(
                                        a_title, b_title,
                                        reason=f"Cross-domain bridge: {dom_list[i]} <-> {dom_list[j]}",
                                        source="doctor --fix",
                                    )
                                    suggested += 1
                                    if suggested >= 5:
                                        break
                            if suggested >= 5:
                                break
                        if suggested:
                            warnings[-1] += f" (suggested {suggested} bridge edges — see `kin suggest`)"
                            fixes_applied += 1

    # ── Trailhead coverage ──
    if stats["nodes"] > 10 and stats["edges"] >= 4:
        from .graph import store_trailheads
        trailheads = store_trailheads(store, top_k=10)
        # Count trailheads with meaningful scores
        significant = [t for t in trailheads if t["score"] > 0 and t["out_degree"] >= 2]
        if len(significant) < 2:
            warnings.append(
                f"Low trailhead coverage: only {len(significant)} entry point(s) detected "
                f"(< 2). Add more high-connectivity nodes to improve discoverability")

    # ── Component balance ──
    if stats["nodes"] >= 5 and stats["edges"] >= 4:
        import networkx as nx
        try:
            G_bal = build_nx_from_store(store)
        except NameError:
            from .graph import build_nx_from_store
            G_bal = build_nx_from_store(store)
        components = list(nx.weakly_connected_components(G_bal))
        if components:
            largest = max(len(c) for c in components)
            total_nodes = G_bal.number_of_nodes()
            if total_nodes > 0 and largest / total_nodes > 0.80:
                warnings.append(
                    f"Component imbalance: largest component has {largest}/{total_nodes} "
                    f"nodes ({largest/total_nodes:.0%}). Consider splitting into sub-domains")

    # ── Output ──
    if args.json:
        print(_dumps({
            "healthy": not issues,
            "issues": issues,
            "warnings": warnings,
            "stats": stats,
            "fixes_applied": fixes_applied,
        }, indent=2))
    else:
        if issues:
            print(f"{len(issues)} issue(s):")
            for i in issues:
                print(f"  ✗ {i}")
        if warnings:
            print(f"\n{len(warnings)} warning(s):")
            for w in warnings:
                print(f"  ⚠ {w}")
        if not issues and not warnings:
            print(f"Healthy: {stats['nodes']} nodes, {stats['edges']} edges, 0 issues")
        elif not issues:
            print(f"\nNo critical issues. {stats['nodes']} nodes, {stats['edges']} edges.")
        if fixes_applied:
            print(f"\n{fixes_applied} fix(es) applied.")
        if issues and not do_fix:
            print(f"\nRun `kin doctor --fix` to auto-repair fixable issues.")

    store.close()


# ── set-audience ──────────────────────────────────────────────────────

def cmd_set_audience(args):
    """Set the audience scope of a node (private/team/org/public)."""
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    store.update_node(node["id"], audience=args.audience)
    print(f"Set {node['title']} audience to: {args.audience}")
    store.close()


# ── set-state ─────────────────────────────────────────────────────────

def cmd_set_state(args):
    """Set a key-value pair in a node's current_state (mutable directive state)."""
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    # Build state dict: get existing current_state and update the key
    extra = node.get("extra") or {}
    current_state = extra.get("current_state") or {}

    # Coerce value to appropriate type
    value = args.value
    if value.lower() in ("true", "yes"):
        value = True
    elif value.lower() in ("false", "no"):
        value = False
    else:
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass  # keep as string

    current_state[args.key] = value
    store.update_directive_state(node["id"], current_state)
    print(f"Set state on {node['title']}: {args.key} = {value}")
    store.close()


# ── edit / supersede ──────────────────────────────────────────────────

_EDIT_FIELD_FLAGS = ("--title, --content, --append, --add-tags, "
                     "--remove-tags, --intent, --expires")


def cmd_edit(args):
    """Policy-aware in-place edit of a node (ID or title resolution)."""
    from .config import resolve_agent_id
    from .store import EditPolicyError, LockHeldError

    add_tags = [t.strip() for t in (getattr(args, "add_tags", None) or "").split(",")
                if t.strip()] or None
    remove_tags = [t.strip() for t in (getattr(args, "remove_tags", None) or "").split(",")
                   if t.strip()] or None
    fields = {
        "title": getattr(args, "title", None),
        "content": getattr(args, "content", None),
        "append": getattr(args, "append", None),
        "add_tags": add_tags,
        "remove_tags": remove_tags,
        "intent": getattr(args, "intent", None),
        "expires": getattr(args, "expires", None),
    }
    provided = {k: v for k, v in fields.items() if v is not None}
    if not provided:
        print(f"Error: kin edit requires at least one field ({_EDIT_FIELD_FLAGS}).",
              file=sys.stderr)
        sys.exit(2)

    cfg = _config(args)
    store = _store(args)
    ref = args.node_id
    node = store.get_node(ref) or store.get_node_by_title(ref)
    if not node:
        print(f"Error: '{ref}' not found.", file=sys.stderr)
        store.close()
        sys.exit(1)

    try:
        updated = store.edit_node(
            node["id"],
            actor=resolve_agent_id(cfg),
            force=getattr(args, "force", False),
            policy_overrides=cfg.edit_policy or None,
            **provided,
        )
    except (EditPolicyError, LockHeldError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        store.close()
        sys.exit(1)

    print(f"Edited {updated.get('title', '')} ({updated['id']}) — "
          f"fields: {', '.join(sorted(provided))}")
    store.close()


def cmd_supersede(args):
    """Replace a node with a fresh one, preserving history (ID or title)."""
    from .config import resolve_agent_id
    from .store import LockHeldError

    cfg = _config(args)
    store = _store(args)
    ref = args.node_id
    node = store.get_node(ref) or store.get_node_by_title(ref)
    if not node:
        print(f"Error: '{ref}' not found.", file=sys.stderr)
        store.close()
        sys.exit(1)

    text = " ".join(args.text)
    try:
        new = store.supersede_node(
            node["id"], text,
            actor=resolve_agent_id(cfg),
            expires=getattr(args, "expires", None),
            reason=getattr(args, "reason", None),
            policy_overrides=cfg.edit_policy or None,
        )
    except (LockHeldError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        store.close()
        sys.exit(1)

    print(f"Superseded {node['title']} ({node['id']}) -> {new['id']}")
    store.close()


# ── export ────────────────────────────────────────────────────────────

def _strip_pii(node: dict) -> dict:
    """Strip personally identifiable information from a node dict."""
    import re
    from .privacy import redact
    node = redact(node)
    node["prov_who"] = ["anonymous"]
    from urllib.parse import urlsplit
    source = node.get("prov_source", "")
    if urlsplit(source).scheme not in ("http", "https"):
        node["prov_source"] = Path(source).name
    # Strip emails from content
    content = node.get("content", "")
    content = re.sub(r'\S+@\S+\.\S+', '[email]', content)
    # Credentials use the common policy; ordinary evidence hashes remain intact.
    node["content"] = content
    # Strip actor from activity log entries stored in extra
    extra = node.get("extra")
    if isinstance(extra, dict):
        extra = dict(extra)
        if "actor" in extra:
            del extra["actor"]
        node["extra"] = extra
    return node


def cmd_export(args):
    """Export the graph, respecting audience boundaries.

    --audience team: exports team + org + public nodes (for shared drives)
    --audience org: exports org + public nodes (for org-wide sharing)
    --audience public: exports only public nodes (for open-source / LinkedIn)
    --audience private: exports everything (for personal backup)
    """
    store = _store(args)
    if getattr(args, "export_kind", "graph") == "code-map":
        from .code_map import export_understand_anything

        output_format = getattr(args, "format", "understand-anything")
        if output_format not in ("understand-anything", "json"):
            print("Error: code-map export supports --format understand-anything or json",
                  file=sys.stderr)
            sys.exit(1)
        # Actively find the repo being worked on: default the relativization root
        # to the git top-level of the cwd, so `kin export code-map` run anywhere
        # inside a repo scopes to that repo and emits paths relative to its root —
        # never machine-local absolutes. Explicit --directory always wins.
        directory = getattr(args, "directory", None)
        if not directory:
            from .setup import git_repo_root
            root = git_repo_root()
            if root is not None:
                directory = str(root)
        graph = export_understand_anything(
            store,
            directory=directory,
            project_name=getattr(args, "project_name", None),
            limit=getattr(args, "limit", 10000),
        )
        output = _dumps(graph, indent=2)
        output_path = getattr(args, "output", None)
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output + "\n")
            print(f"Wrote {path}", file=sys.stderr)
        else:
            print(output)
        store.close()
        return

    target_audience = args.audience

    if args.format == "understand-anything":
        print("Error: --format understand-anything requires `kin export code-map`",
              file=sys.stderr)
        sys.exit(1)

    audiences = {"private": (None,), "team": ("team", "org", "public"),
                 "org": ("org", "public"), "public": ("public",)}[target_audience]
    # A snapshot must not silently truncate at the query helper's display limit.
    nodes = [n for audience in audiences for n in store.all_nodes(audience=audience, limit=-1)]

    # Apply PII stripping for public/org exports
    strip_pii = target_audience in ("public", "org")

    # Strip edges that cross audience boundaries
    output = []
    node_ids = {n["id"] for n in nodes}
    from .graph_transfer import export_record
    for n in nodes:
        if strip_pii:
            n = _strip_pii(n)
        output.append(export_record(n, store.edges_from(n["id"]), node_ids, public=strip_pii))

    if args.format == "jsonl":
        for item in output:
            print(_dumps(item))
    else:
        print(_dumps(output, indent=2))

    print(f"\nExported {len(output)} nodes.", file=sys.stderr)
    store.close()


# ── ingest ────────────────────────────────────────────────────────────

def cmd_ingest(args):
    """Ingest knowledge from external sources via adapter protocol."""
    from .adapters.pipeline import IngestConfig, run_adapter, run_all
    from .adapters.registry import discover, get

    store = _store(args)
    cfg = _config(args)
    source = args.source

    config = IngestConfig(
        since=getattr(args, "since", None),
        limit=getattr(args, "limit", None),
        verbose=True,
    )

    # Collect adapter-specific kwargs
    extra = {}
    for key in ("repo", "repo_path", "team", "directory", "unity"):
        val = getattr(args, key, None)
        if val is not None:
            extra[key] = val
    # Pass config for adapters that need it (projects, sessions)
    extra["_config"] = cfg

    if source == "all":
        results = run_all(store, config, **extra)
        total_created = sum(r.created for r in results.values())
        total_updated = sum(r.updated for r in results.values())
        print(f"\n{total_created} created, {total_updated} updated across {len(results)} adapter(s).")
    else:
        adapter = get(source)
        if not adapter:
            adapters = discover()
            names = ", ".join(sorted(adapters.keys()))
            print(f"Unknown adapter: {source}. Available: {names}, all",
                  file=sys.stderr)
            sys.exit(1)
        result = run_adapter(adapter, store, config, **extra)
        if result.errors:
            for err in result.errors:
                print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        print(f"\n{adapter.meta.name}: {result}")

    store.close()


# ── stale (R0 referent staleness) ─────────────────────────────────────

def cmd_stale(args):
    """Re-hash referent-bound nodes; demote stale ones from trusted recall.

    Detection never deletes or rewrites content: a stale/missing referent
    records a demotion marker and the node becomes a re-verification
    candidate. `--rebind <id>` is the deliberate re-verification act.
    """
    from .referent import rebind, stale_sweep

    store = _store(args)
    base = getattr(args, "base_dir", None)

    if getattr(args, "rebind", None):
        try:
            node = rebind(store, args.rebind, base)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            store.close()
            sys.exit(1)
        ref = node.get("referent") or {}
        if args.json:
            print(_dumps({
                "rebound": node["id"],
                "content_digest": ref.get("content_digest"),
                "true_of": node.get("true_of"),
            }, indent=2))
        else:
            print(f"Rebound {node['id']} to "
                  f"{(ref.get('content_digest') or '')[:12]} "
                  f"(true_of {node.get('true_of')})")
        store.close()
        return

    report = stale_sweep(store, base)
    if args.json:
        print(_dumps(report, indent=2))
    else:
        print(f"Checked {report['checked']} referent-bound node(s): "
              f"{report['fresh']} fresh, {len(report['stale'])} stale, "
              f"{len(report['missing'])} missing, "
              f"{report['unhashable']} unhashable")
        for kind in ("stale", "missing"):
            for e in report[kind]:
                print(f"  [{kind}] {e['id']}  {e['title'][:60]}")
        for e in report["cleared"]:
            print(f"  [cleared] {e['id']}  {e['title'][:60]}")
        if report["stale"] or report["missing"]:
            print("\nThese nodes are demoted from trusted recall "
                  "(re-verification candidates). After confirming a claim "
                  "still holds, rebind with `kin stale --rebind <id>`.")
    store.close()


# ── git-hook ──────────────────────────────────────────────────────────

def cmd_git_hook(args):
    """Install or uninstall Kindex git hooks in a repository."""
    from .adapters.git_hooks import install_hooks, uninstall_hooks

    action = args.hook_action
    repo_path = getattr(args, "repo_path", ".") or "."

    if action == "install":
        cfg = _config(args)
        actions = install_hooks(repo_path, cfg)
        for a in actions:
            print(f"  {a}")
    elif action == "uninstall":
        actions = uninstall_hooks(repo_path)
        for a in actions:
            print(f"  {a}")
    else:
        print(f"Unknown action: {action}. Use: install, uninstall", file=sys.stderr)


# ── trail ─────────────────────────────────────────────────────────────

def cmd_trail(args):
    """Show temporal history and connections for a node."""
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    edges_out = store.edges_from(node["id"])
    edges_in = store.edges_to(node["id"])

    # Fetch activity log entries for this node
    node_activity = store.activity_since("1970-01-01")
    node_activity = [
        e for e in node_activity
        if e.get("target_id") == node["id"]
        or (e.get("target_id") or "").startswith(node["id"] + "->")
        or (e.get("target_id") or "").endswith("->" + node["id"])
    ]

    if args.json:
        print(_dumps({
            "node": {"id": node["id"], "title": node["title"], "type": node["type"]},
            "created": node.get("created_at"),
            "updated": node.get("updated_at"),
            "accessed": node.get("last_accessed"),
            "weight": node.get("weight"),
            "provenance": {
                "who": node.get("prov_who", []),
                "when": node.get("prov_when"),
                "activity": node.get("prov_activity"),
                "source": node.get("prov_source"),
                "why": node.get("prov_why"),
            },
            "outgoing": [{"to": e["to_id"], "title": e.get("to_title"),
                         "type": e["type"], "weight": e["weight"]}
                        for e in edges_out],
            "incoming": [{"from": e["from_id"], "title": e.get("from_title"),
                         "type": e["type"], "weight": e["weight"]}
                        for e in edges_in],
            "activity_log": node_activity,
        }, indent=2))
    else:
        print(f"# Trail: {node['title']}")
        print(f"  Created:  {node.get('created_at', '?')}")
        print(f"  Updated:  {node.get('updated_at', '?')}")
        print(f"  Accessed: {node.get('last_accessed', '?')}")
        print(f"  Weight:   {node.get('weight', 0):.2f}")

        if node.get("prov_source"):
            print(f"  Source:   {node['prov_source']}")
        if node.get("prov_activity"):
            print(f"  Activity: {node['prov_activity']}")

        if edges_out:
            print(f"\n  Outgoing ({len(edges_out)}):")
            for e in edges_out:
                print(f"    → {e.get('to_title', e['to_id'])} [{e['type']}] w={e['weight']:.2f}")
        if edges_in:
            print(f"\n  Incoming ({len(edges_in)}):")
            for e in edges_in:
                print(f"    ← {e.get('from_title', e['from_id'])} [{e['type']}] w={e['weight']:.2f}")

        if node_activity:
            print(f"\n  Activity Log ({len(node_activity)} entries):")
            for e in node_activity:
                ts = (e.get("timestamp") or "")[:16]
                action = e.get("action", "")
                actor = e.get("actor", "")
                details = e.get("details") or {}
                actor_str = f" @{actor}" if actor else ""
                detail_str = ""
                if isinstance(details, dict):
                    fields = details.get("fields", [])
                    if fields:
                        detail_str = f" ({', '.join(fields)})"
                print(f"    {ts}  {action}{detail_str}{actor_str}")

    store.close()


# ── decay ─────────────────────────────────────────────────────────────

def cmd_decay(args):
    """Run weight decay on nodes and edges based on last access time."""
    store = _store(args)
    count = store.apply_weight_decay(
        node_half_life_days=args.node_half_life,
        edge_half_life_days=args.edge_half_life,
    )
    cfg = _config(args)
    pruned = store.decay_pheromone(
        half_life_days=cfg.attention.pheromone_half_life_days,
    )

    if args.json:
        print(_dumps({"decayed_nodes": count, "pheromone_trails_pruned": pruned}))
    else:
        print(f"Weight decay applied: {count} node(s) adjusted; "
              f"{pruned} dead pheromone trail(s) pruned.")

    store.close()


# ── compact-hook ──────────────────────────────────────────────────────

def cmd_compact_hook(args):
    """Pre-compact hook: capture session discoveries before context compaction.

    Reads from stdin or --text, extracts knowledge, and stages review candidates.
    Designed to be called by Claude Code's PreCompact hook.
    """
    store = _store(args)
    ledger, cfg = _ledger(args)

    # A Claude Code hook pipes a JSON envelope ({session_id,
    # transcript_path, ...}) on stdin — metadata, not conversation text.
    # The envelope preempts --text: the Stop hook historically passed
    # both, and letting --text win ran extraction on the literal instead
    # of the transcript the envelope points at. --text is the effective
    # input only when stdin is not a parseable envelope.
    stdin_text = ""
    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read()

    env = {}
    try:
        from .attention import parse_hook_payload
        env = parse_hook_payload(stdin_text) if stdin_text else {}
    except Exception:
        env = {}
    tpath = env.get("transcript_path") or env.get("transcriptPath") or ""
    # The hook envelope, per spec, is a parseable JSON object carrying
    # BOTH hook_event_name and transcript_path — only that suppresses
    # --text. Hook-ish JSON without a transcript pointer (a session_id
    # ping, an envelope missing its transcript) is still metadata, never
    # extraction input: --text applies when given, otherwise there is
    # nothing to extract.
    is_envelope = bool(env.get("hook_event_name")) and bool(tpath)
    is_hook_metadata = bool(env.get("hook_event_name") or env.get("session_id") or tpath)

    if is_envelope:
        text = stdin_text
    elif args.text:
        text = args.text
    else:
        text = "" if is_hook_metadata else stdin_text
    if not text and sys.stdin.isatty():
        print("No text provided. Use --text or pipe via stdin.", file=sys.stderr)
        store.close()
        return

    # Silent, lightweight: if a hook envelope (PreCompact/Stop) gave us a
    # transcript path, queue the session for later reinforcement grading in cron.
    try:
        from .attention import resolve_conversation_id
        from .reinforce import enqueue_reinforce
        if tpath or env.get("session_id"):
            enqueue_reinforce(store, resolve_conversation_id(None, env),
                              transcript_path=tpath)
    except Exception:
        pass

    if is_envelope:
        # Extracting from the envelope itself would mint one junk node per
        # JSON field (issue #14). Substitute the real conversation text
        # from the transcript file the envelope points at.
        from .ingest import _extract_session_text
        text = _extract_session_text(Path(tpath)) if tpath else ""

    # Envelope-derived transcripts get a higher floor (real conversations
    # are long; short residue means extraction failed). Plain piped text
    # keeps the original threshold so short direct captures still work.
    min_len = 50 if is_envelope else 10
    if not text or len(text.strip()) < min_len:
        store.close()
        return

    from .extract import extract

    existing = [n["title"] for n in store.all_nodes(limit=200)]
    try:
        extraction = extract(text, existing, cfg, ledger)
    except Exception as exc:
        try:
            from .config import record_degraded
            record_degraded("compact-hook-extract", exc, config=cfg)
        except Exception:
            pass
        store.close()
        return

    import hashlib

    source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    staged_ids: list[str] = []
    proposals = extraction.get("connections", [])
    if not isinstance(proposals, list):
        proposals = []
    concepts = extraction.get("concepts", [])
    if not isinstance(concepts, list):
        concepts = []
    capture_instant = operation_now()
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        # Keyword fallback emits title-only concepts (useful for linking,
        # not worth staging) — never create content-empty candidates.
        content = concept.get("content") or ""
        title = concept.get("title") or ""
        if not isinstance(content, str) or not content.strip() or not isinstance(title, str):
            continue
        title_key = title.strip().casefold()
        related_proposals = [
            proposal for proposal in proposals
            if isinstance(proposal, dict)
            and (
                str(proposal.get("from_title", "")).strip().casefold() == title_key
                or str(proposal.get("to_title", "")).strip().casefold() == title_key
            )
        ]
        try:
            staged_ids.append(
                store.add_capture_candidate(
                    title=title,
                    content=content,
                    node_type=concept.get("type", "concept"),
                    domains=concept.get("domains", []),
                    connections=related_proposals,
                    source_digest=source_digest,
                    now=capture_instant,
                )
            )
        except Exception as exc:
            # Host compaction must continue, and failure must never fall back to
            # durable node/edge creation.
            try:
                from .config import record_degraded
                record_degraded("compact-hook-candidate", exc, config=cfg)
            except Exception:
                pass

    count = len(staged_ids)

    # Output context at executive level for re-injection after compaction
    if count > 0 or args.emit_context:
        if count > 0:
            print(f"# Kindex: staged {count} capture candidate(s) for review.")
        from .retrieve import format_context_block, hybrid_search
        topic = text[:100].split("\n")[0]
        results = hybrid_search(store, topic, top_k=5)
        block = format_context_block(store, results, query=topic, level="executive")
        print(block)

    store.close()


# ── prime ─────────────────────────────────────────────────────────────

def cmd_prime(args):
    """Generate context injection for Claude Code SessionStart hook.

    kin prime [--topic TOPIC] [--tokens N] [--for hook|stdout] [--codebook]
    """
    store = _store(args)
    cfg = _config(args)

    if getattr(args, "codebook", False):
        _prime_codebook(store, args)
        store.close()
        return

    from .agent_adapters import normalize_adapter, scope_adapter
    from .agent_settings import (
        agent_setting_value,
        apply_agent_overrides,
        resolve_agent_instance_key,
    )
    from .attention import parse_hook_payload, resolve_conversation_id
    from .hooks import prime_context

    topic = getattr(args, "topic", None)
    tokens = getattr(args, "tokens", 750) or 750
    output_for = getattr(args, "output_for", "stdout") or "stdout"
    conversation_id = getattr(args, "conversation_id", None)
    adapter = normalize_adapter(getattr(args, "adapter", "claude"))
    hook_payload = {}
    if output_for == "hook" and not conversation_id and not sys.stdin.isatty():
        hook_payload = parse_hook_payload(sys.stdin.read())
        conversation_id = resolve_conversation_id(
            None,
            hook_payload,
            fallback_to_cwd=False,
        )
    instance_key = ""
    if output_for == "hook" or getattr(args, "agent_instance", None):
        instance_key = resolve_agent_instance_key(
            adapter,
            getattr(args, "agent_instance", None),
            hook_payload,
        )
        tokens = int(agent_setting_value(
            cfg,
            client=adapter,
            instance_key=instance_key,
            key="hooks.prime_tokens",
            default=tokens,
        ) or tokens)
        cfg = apply_agent_overrides(cfg, client=adapter, instance_key=instance_key)

    block = prime_context(
        store,
        topic=topic,
        max_tokens=tokens,
        config=cfg,
        conversation_id=conversation_id,
        adapter=scope_adapter(adapter),
    )

    if output_for == "hook":
        # A new session clears any operator guidance (session-scoped) and says so once.
        notice = ""
        try:
            from .sim import clear_sim_guidance
            if clear_sim_guidance(store):
                notice = "sim guidance cleared\n"
        except Exception:
            pass
        body = notice + block
        quiet = str(getattr(cfg.attention, "display", "full")).lower() == "quiet"
        if adapter in {"codex", "antigravity"}:
            # JSON adapters ingest context through client hook envelopes, so
            # always render the adapter-specific envelope.
            rendered = _hook_context_output(
                body,
                adapter=adapter,
                event=("PreInvocation" if adapter == "antigravity" else "SessionStart"),
                suppress=quiet,
            )
            if rendered:
                print(rendered, end="")
        elif adapter == "opencode":
            # The OpenCode plugin captures stdout and pushes it onto the system
            # prompt (output.system) itself — there is no suppressOutput envelope,
            # so always emit PLAIN text, regardless of the quiet display setting.
            print(body, end="")
        elif quiet:
            # Quiet mode: feed the context to the model but ask the client not to
            # render the SessionStart block. Needs the JSON adapter for suppressOutput.
            rendered = _hook_context_output(
                body, adapter="claude", event="SessionStart", suppress=True,
            )
            if rendered:
                print(rendered, end="")
        else:
            print(body, end="")
    else:
        # Add a header for human-readable output
        print("# Kindex Prime Context")
        print(f"# Topic: {topic or '(auto-detected)'}")
        print(f"# Max tokens: {tokens}")
        print()
        print(block)

    store.close()


def cmd_agent_prime_hook(args):
    """Prime-once hook for clients that do not have a SessionStart event."""
    from .agent_adapters import normalize_adapter, scope_adapter
    from .agent_settings import (
        agent_setting_value,
        apply_agent_overrides,
        resolve_agent_instance_key,
    )
    from .attention import read_hook_payload, resolve_conversation_id
    from .hooks import prime_context

    adapter = normalize_adapter(getattr(args, "adapter", "plain"))
    client = normalize_adapter(getattr(args, "client", None) or adapter)
    payload = read_hook_payload()
    conversation_id = resolve_conversation_id(
        getattr(args, "conversation_id", None),
        payload,
        fallback_to_cwd=False,
    )
    instance_key = resolve_agent_instance_key(
        client,
        getattr(args, "agent_instance", None),
        payload,
    )

    store = _store(args)
    cfg = _config(args)
    if not conversation_id:
        store.close()
        return

    meta_key = f"agent_prime_hook.{client}.{conversation_id}"
    if store.get_meta(meta_key):
        store.close()
        return

    tokens = int(agent_setting_value(
        cfg,
        client=client,
        instance_key=instance_key,
        key="hooks.prime_tokens",
        default=getattr(args, "tokens", 750) or 750,
    ) or 750)
    cfg = apply_agent_overrides(cfg, client=client, instance_key=instance_key)
    block = prime_context(
        store,
        topic=getattr(args, "topic", None),
        max_tokens=tokens,
        config=cfg,
        conversation_id=conversation_id,
        adapter=scope_adapter(client),
    )
    store.set_meta(meta_key, datetime.datetime.now().isoformat(timespec="seconds"))
    rendered = _hook_context_output(
        block,
        adapter=adapter,
        event=getattr(args, "event", None) or "PreInvocation",
        suppress=str(getattr(cfg.attention, "display", "full")).lower() == "quiet",
    )
    if rendered:
        print(rendered, end="")
    store.close()


def cmd_agent_stop_hook(args):
    """Portable session-end hook: enqueue reinforcement and satisfy client schema."""
    from .agent_adapters import normalize_adapter
    from .attention import read_hook_payload, resolve_conversation_id

    adapter = normalize_adapter(getattr(args, "adapter", "plain"))
    payload = read_hook_payload()
    store = _store(args)
    conversation_id = resolve_conversation_id(
        getattr(args, "conversation_id", None),
        payload,
        fallback_to_cwd=False,
    )
    if conversation_id:
        try:
            from .reinforce import enqueue_reinforce
            transcript_path = (
                payload.get("transcript_path")
                or payload.get("transcriptPath")
                or payload.get("conversationPath")
                or ""
            )
            enqueue_reinforce(store, conversation_id, transcript_path=transcript_path)
        except Exception:
            pass
    store.close()
    if adapter == "antigravity":
        print(_dumps({"decision": ""}))


def _prime_codebook(store, args):
    """Regenerate the LLM prompt cache codebook."""
    from .retrieve import generate_codebook

    cfg = _config(args)
    min_weight = cfg.llm.codebook_min_weight
    text, hash_val = generate_codebook(store, min_weight=min_weight)

    old_hash = store.get_meta("codebook_hash")
    store.set_meta("codebook_text", text)
    store.set_meta("codebook_hash", hash_val)
    from datetime import datetime
    store.set_meta("codebook_generated_at", datetime.now().isoformat())

    # Track node count for staleness detection
    stats = store.stats() if hasattr(store, "stats") else {}
    node_count = stats.get("nodes", 0) if isinstance(stats, dict) else 0
    store.set_meta("codebook_node_count", str(node_count))

    entry_count = text.count("\n#")
    est_tokens = len(text) // 4

    if old_hash and old_hash == hash_val:
        print(f"Codebook unchanged (hash: {hash_val})")
    elif old_hash:
        print(f"Codebook updated: {hash_val} (was: {old_hash})")
    else:
        print(f"Codebook created: {hash_val}")
    print(f"  {entry_count} entries, ~{est_tokens} tokens")
    print(f"  Min weight: {min_weight}")


# ── suggest ───────────────────────────────────────────────────────────

def cmd_suggest(args):
    """Show and manage bridge opportunity suggestions.

    kin suggest [--accept ID] [--reject ID] [--limit N]
    """
    store = _store(args)

    accept_id = getattr(args, "accept", None)
    reject_id = getattr(args, "reject", None)
    limit = getattr(args, "limit", 20) or 20

    if accept_id is not None:
        # Accept: create the edge between the two concepts
        suggestions = store.pending_suggestions(limit=1000)
        suggestion = None
        for s in suggestions:
            if s["id"] == accept_id:
                suggestion = s
                break

        if not suggestion:
            # Also check non-pending in case of confusion
            print(f"Suggestion {accept_id} not found or already processed.", file=sys.stderr)
            store.close()
            return

        # Endpoint identity is persisted independently of its producer. Title
        # lookups refuse ambiguity instead of creating an edge to a guess.
        identity_kind = suggestion.get("identity_kind", "title")
        try:
            node_a = store.resolve_suggestion_node(
                suggestion["concept_a"], identity_kind
            )
            node_b = store.resolve_suggestion_node(
                suggestion["concept_b"], identity_kind
            )
        except ValueError as exc:
            print(f"Cannot accept: {exc}", file=sys.stderr)
            store.close()
            return

        if node_a and node_b:
            store.add_edge(
                node_a["id"], node_b["id"],
                edge_type="relates_to",
                provenance=f"suggestion: {suggestion.get('reason', '')}",
            )
            store.update_suggestion(accept_id, "accepted")
            print(f"Accepted: {suggestion['concept_a']} <-> {suggestion['concept_b']}")
            print(f"  Edge created: {node_a['title']} -> {node_b['title']}")
        else:
            missing = []
            if not node_a:
                missing.append(suggestion["concept_a"])
            if not node_b:
                missing.append(suggestion["concept_b"])
            print(f"Cannot accept: node(s) not found: {', '.join(missing)}", file=sys.stderr)
            print("Create the nodes first, then accept the suggestion.", file=sys.stderr)

        store.close()
        return

    if reject_id is not None:
        store.update_suggestion(reject_id, "rejected")
        print(f"Rejected suggestion {reject_id}.")
        store.close()
        return

    # List pending suggestions
    suggestions = store.pending_suggestions(limit=limit)

    if not suggestions:
        print("No pending suggestions.")
        store.close()
        return

    if args.json:
        print(_dumps(suggestions, indent=2))
    else:
        print(f"# Bridge Opportunities ({len(suggestions)} pending)\n")
        for s in suggestions:
            sid = s["id"]
            ca = s["concept_a"]
            cb = s["concept_b"]
            reason = s.get("reason", "")
            source = s.get("source", "")
            created = (s.get("created_at") or "")[:16]

            print(f"  [{sid}] {ca} <-> {cb}")
            if reason:
                print(f"       Why: {reason}")
            if source:
                print(f"       Source: {source}")
            if created:
                print(f"       Created: {created}")
            print()

        print(f"Accept: kin suggest --accept <ID>")
        print(f"Reject: kin suggest --reject <ID>")

    store.close()


# ── log ───────────────────────────────────────────────────────────────

def cmd_log(args):
    """Show recent activity log."""
    store = _store(args)
    entries = store.recent_activity(limit=args.n)

    if not entries:
        print("No activity logged yet.")
        store.close()
        return

    if args.json:
        print(_dumps(entries, indent=2))
    else:
        for e in entries:
            ts = (e.get("timestamp") or "")[:16]
            action = e.get("action", "")
            target = e.get("target_title") or e.get("target_id", "")
            actor = e.get("actor", "")
            actor_str = f" @{actor}" if actor else ""
            print(f"  {ts}  {action:15s} {target[:45]}{actor_str}")

    store.close()


# ── changelog ─────────────────────────────────────────────────────────

def _diff_value(value, limit: int = 60) -> str:
    """Compact a diff old/new value for one-line changelog rendering."""
    if value is None or value == "":
        return "(none)"
    s = value if isinstance(value, str) else _dumps(value)
    s = " ".join(s.split())  # collapse newlines/whitespace
    return s if len(s) <= limit else s[:limit - 1] + "…"


def cmd_changelog(args):
    """Show what changed in the graph since a date or over the last N days."""
    store = _store(args)

    # Determine the since timestamp
    if args.since:
        since_iso = args.since
    else:
        days = args.days or 7
        since_dt = datetime.datetime.now() - datetime.timedelta(days=days)
        since_iso = since_dt.isoformat(timespec="seconds")

    # Fetch activity, optionally filtered by actor
    if args.actor:
        entries = store.activity_by_actor(args.actor)
        # Further filter by timestamp
        entries = [e for e in entries if (e.get("timestamp") or "") >= since_iso]
    else:
        entries = store.activity_since(since_iso)

    if not entries:
        if args.json:
            print(_dumps({"since": since_iso, "groups": {}, "total": 0}))
        else:
            days_label = args.days or 7
            if args.since:
                print(f"# Changelog (since {args.since})\n\nNo activity found.")
            else:
                print(f"# Changelog (last {days_label} days)\n\nNo activity found.")
        store.close()
        return

    # Group entries by action type, mapping to display categories
    action_map = {
        "add_node": "Added",
        "update_node": "Updated",
        "delete_node": "Deleted",
        "add_edge": "Linked",
        "edit_node": "Edited",
        "supersede_node": "Superseded",
    }
    groups: dict[str, list[dict]] = {}
    for e in entries:
        action = e.get("action", "unknown")
        label = action_map.get(action, action)
        groups.setdefault(label, []).append(e)

    if args.json:
        print(_dumps({
            "since": since_iso,
            "actor": args.actor or None,
            "groups": {k: v for k, v in groups.items()},
            "total": len(entries),
        }, indent=2))
    else:
        days_label = args.days or 7
        if args.since:
            print(f"# Changelog (since {args.since})")
        else:
            print(f"# Changelog (last {days_label} days)")
        if args.actor:
            print(f"  Actor: {args.actor}")
        print()

        # Display order: Added, Updated, Deleted, Linked, then anything else
        display_order = ["Added", "Updated", "Deleted", "Linked"]
        all_labels = display_order + [k for k in groups if k not in display_order]

        for label in all_labels:
            if label not in groups:
                continue
            items = groups[label]
            # Determine count label
            if label in ("Linked",):
                count_label = f"{len(items)} edges"
            else:
                count_label = f"{len(items)} nodes"

            print(f"## {label} ({count_label})")
            for e in items:
                ts = (e.get("timestamp") or "")[:10]
                target_title = e.get("target_title") or e.get("target_id", "")
                details = e.get("details") or {}

                if label == "Linked":
                    # Show edge details: from -> to [type]
                    edge_type = details.get("type", "relates_to")
                    target = e.get("target_id", "")
                    print(f"  {ts}  {target} [{edge_type}]")
                elif label == "Updated":
                    fields = details.get("fields", [])
                    field_str = f" ({', '.join(fields)})" if fields else ""
                    print(f"  {ts}  Updated{field_str}: {target_title}")
                else:
                    ntype = details.get("type", "")
                    type_str = f"[{ntype}] " if ntype else ""
                    print(f"  {ts}  {type_str}{target_title}")

                # Compact per-field diff lines for edits
                diffs = details.get("diffs") if isinstance(details, dict) else None
                if isinstance(diffs, dict):
                    for field, change in diffs.items():
                        if not isinstance(change, dict):
                            continue
                        print(f"      {field}: {_diff_value(change.get('old'))} "
                              f"-> {_diff_value(change.get('new'))}")
            print()

    store.close()


# ── graph ─────────────────────────────────────────────────────────────

def cmd_graph(args):
    """Graph analytics — stats, centrality, communities, bridges, trailheads."""
    store = _store(args)

    from .graph import (
        store_bridges, store_centrality, store_communities,
        store_stats, store_trailheads,
    )

    mode = args.graph_mode or "stats"

    if mode == "stats":
        stats = store_stats(store)
        if args.json:
            print(_dumps(stats, indent=2))
        else:
            print(f"Graph Statistics")
            print(f"  Nodes:      {stats['semantic_nodes']} semantic")
            print(f"  Edges:      {stats['edges']} semantic")
            print(f"  Density:    {stats['density']}")
            print(f"  Components: {stats['components']}")
            print(f"  Avg degree: {stats['avg_degree']}")
            if stats['max_degree_node']:
                print(f"  Hub:        {stats['max_degree_node']} (degree {stats['max_degree']})")
            ignored = stats.get("stored_edges", 0) - stats.get("edges", 0)
            excluded_nodes = stats["stored_nodes"] - stats["semantic_nodes"]
            if excluded_nodes or ignored:
                print(
                    f"  Stored:     {stats['stored_nodes']} nodes, "
                    f"{stats['stored_edges']} edges ({excluded_nodes} lifecycle "
                    f"nodes, {ignored} excluded edges)"
                )
                print(
                    "    domain-derived: "
                    f"{stats.get('ignored_domain_edges', 0)}"
                )
                print(
                    "    session-linked: "
                    f"{stats.get('ignored_session_edges', 0)}"
                )
                other = stats.get("ignored_other_edges", 0)
                if other:
                    print(f"    unresolved:     {other}")

    elif mode == "centrality":
        method = args.method or "betweenness"
        results = store_centrality(store, method=method, top_k=args.top_k or 20)
        if args.json:
            print(_dumps([{"id": nid, "title": t, "score": s}
                          for nid, t, s in results], indent=2))
        else:
            print(f"Centrality ({method})")
            for nid, title, score in results:
                bar = "█" * int(score * 40)
                print(f"  {score:.4f} {bar:20s} {title[:50]}")

    elif mode == "communities":
        comms = store_communities(store)
        if args.json:
            print(_dumps(comms, indent=2))
        else:
            print(f"{len(comms)} communities detected")
            for i, comm in enumerate(comms):
                members = ", ".join(m["title"][:30] for m in comm[:5])
                extra = f" +{len(comm)-5} more" if len(comm) > 5 else ""
                print(f"  {i+1}. [{len(comm)} nodes] {members}{extra}")

    elif mode == "bridges":
        bridges = store_bridges(store, top_k=args.top_k or 10)
        if args.json:
            print(_dumps(bridges, indent=2))
        else:
            print("Bridge edges (critical connections)")
            for b in bridges:
                print(f"  {b['from_title'][:25]:25s} <-> {b['to_title'][:25]:25s}  "
                      f"btw={b['betweenness']}")

    elif mode == "trailheads":
        trails = store_trailheads(store, top_k=args.top_k or 10)
        if args.json:
            print(_dumps(trails, indent=2))
        else:
            print("Trailheads (entry points)")
            for t in trails:
                print(f"  [{t['type'][:4]}] {t['title'][:40]:40s}  "
                      f"score={t['score']}  out={t['out_degree']}  btw={t['betweenness']}")

    store.close()


# ── analytics ─────────────────────────────────────────────────────────

def cmd_analytics(args):
    """Archive analytics — session stats and activity heatmap."""
    cfg = _config(args)

    from .analytics import activity_heatmap, find_archive_db, session_stats

    show_heatmap = getattr(args, "heatmap", False)
    show_sessions = getattr(args, "sessions", False)
    days = getattr(args, "days", 90) or 90

    # Default: show sessions if neither flag is set
    if not show_heatmap and not show_sessions:
        show_sessions = True

    if show_sessions:
        stats = session_stats(cfg)
        if "error" in stats:
            db_path = find_archive_db(cfg)
            if not db_path:
                print(f"Error: {stats['error']}", file=sys.stderr)
                print(f"Searched: {cfg.claude_path / 'archive'}", file=sys.stderr)
                sys.exit(1)

        if args.json:
            print(_dumps(stats, indent=2))
        else:
            print("# Archive Session Stats\n")
            print(f"Total sessions: {stats.get('total_sessions', 0)}")

            by_month = stats.get("sessions_by_month", {})
            if by_month:
                print("\n## Sessions by Month")
                for month, count in sorted(by_month.items(), reverse=True):
                    bar = "█" * min(count, 40)
                    print(f"  {month}  {bar} {count}")

            top_projects = stats.get("top_projects", {})
            if top_projects:
                print("\n## Top Projects")
                for proj, count in top_projects.items():
                    print(f"  {proj:30s} {count}")

    if show_heatmap:
        heatmap = activity_heatmap(cfg, days=days)
        if "error" in heatmap:
            print(f"Error: {heatmap['error']}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(_dumps(heatmap, indent=2))
        else:
            print(f"\n# Activity Heatmap (last {days} days)\n")
            grid = heatmap.get("grid", {})
            # Header row: hours
            hours_header = "          " + "".join(f"{h:3d}" for h in range(24))
            print(hours_header)
            for day_name in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
                row = grid.get(day_name, {})
                cells = []
                for h in range(24):
                    v = row.get(h, 0)
                    if v == 0:
                        cells.append("  .")
                    elif v < 3:
                        cells.append("  o")
                    elif v < 6:
                        cells.append("  O")
                    else:
                        cells.append("  #")
                print(f"  {day_name:3s}   {''.join(cells)}")
            print("\n  Legend: . = 0, o = 1-2, O = 3-5, # = 6+")


# ── index ─────────────────────────────────────────────────────────────

def cmd_index(args):
    """Write .kin/index.json summarizing the graph for git tracking."""
    store = _store(args)

    from .ingest import write_kin_index
    from .setup import git_repo_root

    # Anchor `.kin/` at the GIT ROOT of the repo being worked on, not the cwd —
    # so `kin index` run from any subdirectory writes one `.kin/` at the top level
    # (and the same root drives the merge-driver registration below). An explicit
    # --output-dir is honored verbatim for advanced/monorepo use.
    if getattr(args, "output_dir", None):
        output_dir = Path(args.output_dir)
    else:
        output_dir = git_repo_root(Path.cwd()) or Path.cwd()
    try:
        path = write_kin_index(store, output_dir)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        store.close()
        sys.exit(1)
    print(f"Wrote {path}")
    print(f"  ({path.stat().st_size} bytes)")

    # Default: register the structured merge driver so this freshly written,
    # git-tracked artifact is conflict-safe from the start. Guarded + idempotent
    # (only when inside a git repo and not already registered in this clone).
    # Best-effort: a merge-driver hiccup must never fail the index write itself.
    if not getattr(args, "no_merge_driver", False):
        try:
            from .setup import (
                git_repo_root, install_merge_driver, merge_driver_registered,
            )
            root = git_repo_root(output_dir)
            if root is not None and not merge_driver_registered(root):
                for a in install_merge_driver(root):
                    print(f"  merge-driver: {a}")
                print("  (registered .kin structured merge; opt out with --no-merge-driver)")
        except Exception as e:
            print(f"  merge-driver: skipped ({e})", file=sys.stderr)

    store.close()


def cmd_merge_kin(args):
    """Git merge driver for .kin artifacts — structured union, no manual conflicts.

    Invoked by git as ``kin merge-kin %O %A %B %P``. Writes the merged result to
    the ours/%A file and exits 0 when resolved; exits 1 (declining) for an
    unrecognized file or invalid JSON, leaving the conflict for git to record
    so it can be resolved manually.
    """
    from .kin_merge import merge_kin_files

    merged = merge_kin_files(
        getattr(args, "path", "") or "",
        getattr(args, "base", "") or "",
        getattr(args, "ours", "") or "",
        getattr(args, "theirs", "") or "",
    )
    if merged is None:
        sys.exit(1)
    Path(args.ours).write_text(merged)
    print(f"merge-kin: resolved {args.path}", file=sys.stderr)


# ── sync-links ────────────────────────────────────────────────────────

def cmd_sync_links(args):
    """Update each node's content with a '## Connections' section.

    Reads all nodes, finds their outgoing edges, and appends (or replaces)
    a ``## Connections`` section in the node content listing linked nodes.
    Reports how many nodes were updated.
    """
    import re as _re

    store = _store(args)
    nodes = store.all_nodes(limit=5000)
    updated = 0

    for node in nodes:
        edges = store.edges_from(node["id"], semantic_only=True)
        if not edges:
            continue

        # Build the connections section
        lines = ["## Connections", ""]
        for edge in edges:
            label = edge.get("to_title") or edge.get("to_id", "?")
            etype = edge.get("type", "relates_to")
            lines.append(f"- **{label}** ({etype})")
        connections_block = "\n".join(lines)

        content = node.get("content") or ""

        # Strip any previous Connections section so we don't duplicate
        content = _re.sub(
            r"(?m)^## Connections\n(?:.*\n)*?(?=^## |\Z)",
            "",
            content,
        ).rstrip()

        # Append the new connections section
        if content:
            new_content = content + "\n\n" + connections_block + "\n"
        else:
            new_content = connections_block + "\n"

        store.update_node(node["id"], content=new_content)
        updated += 1

    print(f"Updated {updated} node(s) with connection references.")
    store.close()


# ── alias ─────────────────────────────────────────────────────────────

def cmd_alias(args):
    """Manage AKA/synonyms for a node.

    kin alias <node> add <alias>    — add a synonym
    kin alias <node> remove <alias> — remove a synonym
    kin alias <node> list           — show all aliases
    """
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    aka = list(node.get("aka") or [])
    action = args.alias_action

    if action == "list":
        if aka:
            print(f"Aliases for {node['title']}:")
            for a in aka:
                print(f"  - {a}")
        else:
            print(f"No aliases for {node['title']}.")
        store.close()
        return

    if action == "add":
        if not args.alias_value:
            print("Error: kin alias <node> add <alias>", file=sys.stderr)
            sys.exit(1)
        new_alias = args.alias_value
        if new_alias not in aka:
            aka.append(new_alias)
            store.update_node(node["id"], aka=aka)
            print(f"Added alias '{new_alias}' to {node['title']}")
        else:
            print(f"'{new_alias}' is already an alias for {node['title']}")

    elif action == "remove":
        if not args.alias_value:
            print("Error: kin alias <node> remove <alias>", file=sys.stderr)
            sys.exit(1)
        old_alias = args.alias_value
        if old_alias in aka:
            aka.remove(old_alias)
            store.update_node(node["id"], aka=aka)
            print(f"Removed alias '{old_alias}' from {node['title']}")
        else:
            print(f"'{old_alias}' is not an alias for {node['title']}")

    store.close()


# ── whoami ────────────────────────────────────────────────────────────

def cmd_whoami(args):
    """Show the current user identity used for --mine filtering."""
    from .config import resolve_agent_id
    cfg = _config(args)
    if getattr(args, "json", False):
        print(_dumps({"user": cfg.current_user, "agent": resolve_agent_id(cfg)}))
        return
    print(cfg.current_user)
    print(f"Agent: {resolve_agent_id(cfg)}")


# ── profile ───────────────────────────────────────────────────────────

def cmd_profile(args):
    """Manage named graph profiles (sequestered multi-profile storage).

    kin profile list              — configured profiles + file-level stats
    kin profile which             — resolved profile for this invocation
    kin profile create <name> --data-dir DIR [--roots a,b] [--default]
    """
    action = getattr(args, "profile_action", "list")

    if action == "create":
        _profile_create(args)
        return

    cfg = _config(args)

    if action == "which":
        if args.json:
            print(_dumps({
                "profile": cfg.active_profile,
                "source": cfg.profile_source if cfg.active_profile else None,
            }))
        elif cfg.active_profile:
            print(f"{cfg.active_profile} (via {cfg.profile_source})")
        else:
            print("(none — legacy single-graph)")
        return

    # list (default) — file-level stats only; no cross-profile graph reads.
    if not cfg.profiles:
        if args.json:
            print(_dumps({"profiles": {}, "default_profile": None}))
        else:
            print("No profiles configured (legacy single-graph).")
            print("Create one: kin profile create <name> --data-dir <dir> [--roots <dirs>]")
        return

    if args.json:
        print(_dumps({
            "profiles": {n: e.model_dump() for n, e in cfg.profiles.items()},
            "default_profile": cfg.default_profile,
            "active_profile": cfg.active_profile,
        }, indent=2))
        return

    for name, entry in cfg.profiles.items():
        markers = []
        if name == cfg.default_profile:
            markers.append("default")
        if name == cfg.active_profile:
            markers.append("active")
        suffix = f" ({', '.join(markers)})" if markers else ""
        print(f"{name}{suffix}")
        data_path = Path(entry.data_dir).expanduser()
        db = data_path / "kindex.db"
        if not db.exists() and (data_path / "conv.db").exists():
            db = data_path / "conv.db"
        if db.exists():
            print(f"  data_dir: {entry.data_dir} ({db.name}, {db.stat().st_size} bytes)")
        else:
            print(f"  data_dir: {entry.data_dir} (no database yet)")
        if entry.roots:
            print(f"  roots:    {', '.join(entry.roots)}")


def _profile_create(args):
    """Create a profile in the GLOBAL kin.yaml (or an explicit --config file),
    preserving existing content."""
    name = getattr(args, "name", None)
    if not name:
        print("Error: kin profile create <name> --data-dir <dir>", file=sys.stderr)
        sys.exit(2)
    profile_data_dir = getattr(args, "data_dir", None)
    if not profile_data_dir:
        print("Error: --data-dir is required for profile create", file=sys.stderr)
        sys.exit(2)

    from .config import _effective_global_paths

    # An explicit --config is the write target (mirrors `kin config set`);
    # otherwise fall through to the global kin.yaml discovery.
    explicit = getattr(args, "config", None)
    if explicit:
        path = Path(explicit).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = None
        for p in _effective_global_paths():
            from .config import _contained_resolve
            p = _contained_resolve(p)
            if p is not None and p.is_file():
                path = p
                break
        if path is None:
            path = _effective_global_paths()[0]
            from .config import _contained_resolve, _bound_root as _br
            contained = _contained_resolve(path)
            if contained is None:
                # Symlink escaped the root — don't write through it.
                if _br is not None:
                    path = _br / "kin.yaml"
                else:
                    path = Path.home() / "kin.yaml"
            else:
                path = contained
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except (FileNotFoundError, OSError):
                # Dangling symlink or broken parent — fall back to
                # a path that doesn't go through the symlink.
                path = Path.home() / "kin.yaml"

    # Round-trip the existing yaml: load, modify, dump — unknown keys survive.
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    profiles = data.get("profiles") or {}
    if name in profiles:
        print(f"Error: profile '{name}' already exists in {path}", file=sys.stderr)
        sys.exit(1)

    roots = [r.strip() for r in (getattr(args, "roots", None) or "").split(",")
             if r.strip()]
    profiles[name] = {"data_dir": profile_data_dir, "roots": roots}
    data["profiles"] = profiles
    if getattr(args, "set_default", False):
        data["default_profile"] = name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    except (FileNotFoundError, OSError) as e:
        print(f"Error: cannot write config to {path}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Created profile '{name}' in {path}")
    print(f"  data_dir: {profile_data_dir}")
    if roots:
        print(f"  roots:    {', '.join(roots)}")
    if getattr(args, "set_default", False):
        print(f"  default_profile: {name}")

    # Warn when the legacy graph would be orphaned or shadowed: an existing
    # graph at the base data_dir that no profile registers stops receiving
    # routed sessions once a default profile exists.
    default_name = data.get("default_profile")
    base_dir = Path(str(data.get("data_dir") or "~/.kindex")).expanduser()
    try:
        base_res = base_dir.resolve()
    except OSError:
        base_res = base_dir
    registered = set()
    for entry in profiles.values():
        if isinstance(entry, dict) and entry.get("data_dir"):
            try:
                registered.add(Path(str(entry["data_dir"])).expanduser().resolve())
            except OSError:
                registered.add(Path(str(entry["data_dir"])).expanduser())
    legacy_db_exists = ((base_dir / "kindex.db").exists()
                        or (base_dir / "conv.db").exists())
    if base_res not in registered:
        if default_name and legacy_db_exists:
            print(f"Warning: the existing legacy graph at {base_dir} is not "
                  f"registered as a profile. With default_profile "
                  f"'{default_name}', sessions outside all profile roots are "
                  f"routed to '{default_name}' and the legacy graph no longer "
                  f"receives cron maintenance.", file=sys.stderr)
            print(f"  Register it first, e.g.: kin profile create personal "
                  f"--data-dir {base_dir} --roots <dirs> --default",
                  file=sys.stderr)
        elif not default_name:
            print(f"Note: no default_profile set — sessions outside all "
                  f"profile roots stay in the legacy graph at {base_dir} "
                  f"(cron services it in a legacy-remainder pass). Use "
                  f"--default on one profile to change that.", file=sys.stderr)


# ── embed ─────────────────────────────────────────────────────────────

def cmd_embed(args):
    """Index or reindex nodes for vector similarity search.

    Requires: pip install kindex[vectors]
    """
    store = _store(args)
    action = getattr(args, "embed_action", None) or "index"

    def _split_csv(value):
        if not value:
            return None
        return [v.strip() for v in value.split(",") if v.strip()]

    def _filters():
        return {
            "tags": _split_csv(getattr(args, "tags", None)),
            "node_type": getattr(args, "node_type", None),
            "status": getattr(args, "status", None),
            "since": getattr(args, "since", None),
            "project_path": (getattr(args, "kin", None)
                             or getattr(args, "target", None)),
            "stale": getattr(args, "stale", False),
            "limit": getattr(args, "limit", None),
        }

    try:
        from .vectors import (
            drain_embedding_queue,
            embedding_status,
            enqueue_reindex,
            index_all_nodes,
            plan_embedding_reindex,
            reindex_now,
        )
        if action == "status":
            result = embedding_status(store)
        elif action == "calibrate":
            from .grounding import calibrate, load_calibration
            from .vectors import _resolve_embedding_config
            provider, model, _, _ = _resolve_embedding_config(store.config)
            if getattr(args, "show", False):
                record = load_calibration(store, provider, model)
                result = (record.to_dict() if record else
                          {"status": "uncalibrated",
                           "provider": provider, "model": model})
            else:
                result = calibrate(
                    store, store.config,
                    percentile=getattr(args, "percentile", None),
                ).to_dict()
        elif action == "plan":
            result = plan_embedding_reindex(store, **_filters())
        elif action == "enqueue":
            filters = _filters()
            filters["max_queue"] = getattr(args, "max_queue", None)
            result = enqueue_reindex(store, **filters)
        elif action == "drain":
            budget = getattr(args, "time_budget", None)
            if budget == 0:
                budget = float("inf")  # explicit 0 = drain the whole backlog
            result = drain_embedding_queue(
                store, store.config,
                max_jobs=getattr(args, "max_jobs", None),
                time_budget=budget,
            )
        elif action == "reindex":
            if getattr(args, "enqueue", False):
                filters = _filters()
                filters["max_queue"] = getattr(args, "max_queue", None)
                result = enqueue_reindex(store, **filters)
            else:
                result = reindex_now(
                    store, verbose=getattr(args, "verbose", False), **_filters()
                )
        else:
            count = index_all_nodes(store, verbose=getattr(args, "verbose", False))
            result = {"status": "ok", "embedded": count}
        if args.json:
            print(_dumps(result, indent=2))
        elif action == "status":
            print(f"Provider: {result['provider']} / {result['model']}")
            print(f"Strategy: {result['strategy']} "
                  f"(contextual_supported={result['contextual_supported']})")
            print(f"Dimensions: {result['dimensions']}")
            print(f"Indexed nodes: {result.get('indexed_nodes')}")
            print(f"Vector rows: {result.get('vector_rows')}")
            print(f"Queue pending: {result['queue_pending']}")
        elif action == "calibrate":
            if result.get("status") == "uncalibrated":
                print(f"No calibration record for "
                      f"{result['provider']}:{result['model']}. "
                      f"Run `kin embed calibrate` to create one.")
            else:
                print(f"Provider: {result['provider']} / {result['model']}")
                print(f"Floor: {result['floor']:.6f} "
                      f"(p{result['percentile']:g} of {result['sample_size']} "
                      f"null-query similarities)")
                print(f"Corpus at calibration: {result['corpus_node_count']} "
                      f"active nodes, {result['embedding_count']} embedded")
                print(f"Calibrated at: {result['calibrated_at']}")
                enforce = store.config.grounding.enforce
                print(f"Enforcement: {'ON' if enforce else 'SHADOW MODE '
                      '(verdict reported, no rows dropped)'}")
        elif action == "plan":
            print(f"Nodes: {result['nodes']}")
            print(f"Chunks: {result['chunks']}")
            print(f"Estimated tokens: {result['estimated_tokens']}")
            if result.get("estimated_cost_usd") is not None:
                print(f"Estimated cost: ${result['estimated_cost_usd']:.4f}")
            else:
                print("Estimated cost: unknown for this provider/model")
        elif action == "enqueue":
            print(f"Enqueued {result['enqueued']} nodes "
                  f"({result['queue_pending']} pending).")
        elif action == "drain":
            print(f"Embedded {result.get('embedded', 0)} queued nodes "
                  f"({result.get('pending', 0)} pending).")
        elif action == "reindex":
            if result.get("enqueued") is not None:
                print(f"Enqueued {result['enqueued']} nodes "
                      f"({result['queue_pending']} pending).")
            else:
                print(f"Embedded {result.get('embedded', 0)} nodes "
                      f"({result.get('failed', 0)} failed).")
        else:
            print(f"Embedded {result['embedded']} nodes for vector search.")
    except Exception as e:
        print(f"Vector indexing failed: {e}", file=sys.stderr)
        print("Install dependencies: pip install kindex[vectors]", file=sys.stderr)

    store.close()


# ── ask ───────────────────────────────────────────────────────────────


def _classify_question(question: str) -> str:
    """Classify a question by type using keyword heuristics.

    Returns one of: 'procedural', 'decision', 'factual', 'exploratory'.
    """
    q = question.lower().strip()

    # Procedural: how-to questions
    procedural_patterns = [
        "how do i ", "how to ", "how can i ", "how should i ",
        "steps to ", "way to ", "guide to ", "instructions for ",
    ]
    for pat in procedural_patterns:
        if pat in q or q.startswith(pat.strip()):
            return "procedural"

    # Decision: comparison / choice questions
    decision_patterns = [
        "should i ", "which is better", "which one", "compare ",
        "vs ", " or ", "trade-off", "tradeoff", "pros and cons",
        "advantage", "disadvantage", "prefer ",
    ]
    for pat in decision_patterns:
        if pat in q:
            return "decision"

    # Factual: definitional / lookup questions
    factual_patterns = [
        "what is ", "what are ", "what was ", "what does ",
        "who is ", "who are ", "who was ",
        "when did ", "when was ", "when is ",
        "where is ", "where did ", "where are ",
        "define ", "definition of ",
    ]
    for pat in factual_patterns:
        if pat in q or q.startswith(pat.strip()):
            return "factual"

    return "exploratory"


def cmd_ask(args):
    """Query the knowledge graph with natural language.

    Uses LLM if available, otherwise falls back to search + context formatting.
    Classifies the question type to improve search and output.
    """
    store = _store(args)
    question = " ".join(args.question)
    qtype = _classify_question(question)

    from .retrieve import format_context_block, hybrid_search

    # Adjust top_k based on question type
    top_k_map = {
        "factual": 5,
        "procedural": 8,
        "decision": 10,
        "exploratory": 12,
    }
    top_k = top_k_map.get(qtype, 10)

    results = hybrid_search(store, question, top_k=top_k)

    if not results:
        print("No relevant knowledge found.", file=sys.stderr)
        store.close()
        return

    # Try LLM-powered answer
    ledger, cfg = _ledger(args)
    answer = _ask_llm(question, results, cfg, ledger, qtype=qtype, store=store)

    if answer:
        print(answer)
    else:
        # Fallback: show classified context
        level_map = {
            "factual": "abridged",
            "procedural": "full",
            "decision": "full",
            "exploratory": "abridged",
        }
        level = level_map.get(qtype, "abridged")
        block = format_context_block(store, results, query=question, level=level)
        print(f"[{qtype} question] (No LLM available — showing search results)\n")
        print(block)

    store.close()


_STYLE_HINTS = {
    "factual": "Give a direct, concise factual answer.",
    "procedural": "Provide clear step-by-step instructions.",
    "decision": "Compare the options and give a recommendation with trade-offs.",
    "exploratory": "Provide a broad overview touching on the key aspects.",
}

_SYSTEM_PREAMBLE = (
    "You are Kindex, a knowledge graph assistant. "
    "Below is a codebook listing nodes in the user's knowledge graph. "
    "Use it as a lookup table — identify relevant entries by their # number. "
    "Detailed context for query-relevant nodes follows the codebook.\n\n"
)


def _ask_llm(question: str, results: list[dict], config, ledger,
             qtype: str = "exploratory", store=None) -> str | None:
    """Use LLM to answer a question given graph context.

    Routes to cache-optimized path (three-tier with cache_control breakpoints)
    or flat path (single user message) based on config.
    """
    if not config.llm.enabled:
        return None
    if not ledger.can_spend():
        return None

    from .llm import get_client, calculate_cost
    client = get_client(config)
    if client is None:
        return None

    use_cache = (config.llm.cache_control
                 and config.llm.provider == "anthropic"
                 and store is not None)

    if use_cache:
        return _ask_llm_cached(question, results, config, ledger, client, store, qtype)
    return _ask_llm_flat(question, results, config, ledger, client, qtype)


def _ask_llm_flat(question, results, config, ledger, client, qtype):
    """Original flat message format (no caching)."""
    from .llm import calculate_cost

    context_parts = []
    for r in results[:5]:
        title = r.get("title", r["id"])
        content = (r.get("content") or "")[:500]
        ntype = r.get("type", "concept")
        context_parts.append(f"[{ntype}] {title}: {content}")
    context = "\n\n".join(context_parts)
    style = _STYLE_HINTS.get(qtype, _STYLE_HINTS["exploratory"])

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=500,
            messages=[{"role": "user", "content": f"""Based on this knowledge graph context:

{context}

Answer this question concisely: {question}

{style}

If the context doesn't contain enough information, say so honestly."""}],
        )
        cost_info = calculate_cost(config.llm.model, response.usage)
        ledger.record(**cost_info, model=config.llm.model, purpose="ask")
        return response.content[0].text
    except Exception:
        return None


def _ask_llm_cached(question, results, config, ledger, client, store, qtype):
    """Three-tier cached message format with cache_control breakpoints."""
    from .llm import calculate_cost
    from .retrieve import (build_codebook_index, format_tier2,
                           generate_codebook, predict_tier2)

    # Tier 1: Load or auto-generate codebook
    codebook_text = store.get_meta("codebook_text")
    if not codebook_text:
        codebook_text, codebook_hash = generate_codebook(
            store, min_weight=config.llm.codebook_min_weight)
        store.set_meta("codebook_text", codebook_text)
        store.set_meta("codebook_hash", codebook_hash)
    else:
        # Staleness check
        import json
        stats = store.stats() if hasattr(store, "stats") else {}
        node_count = stats.get("nodes", 0) if isinstance(stats, dict) else 0
        old_count_raw = store.get_meta("codebook_node_count")
        old_count = int(old_count_raw) if old_count_raw else 0
        if old_count and node_count > old_count * 1.1:
            print("Hint: codebook may be stale. Run: kin prime --codebook",
                  file=sys.stderr)

    codebook_index = build_codebook_index(codebook_text)

    # Tier 2: Predict and format context
    tier2_results = predict_tier2(store, question, results)
    tier2_text = format_tier2(tier2_results, codebook_index,
                              max_tokens=config.llm.tier2_max_tokens)

    style = _STYLE_HINTS.get(qtype, _STYLE_HINTS["exploratory"])

    # Min cacheable tokens: 1024 for Haiku, 2048 for larger models
    model_lower = config.llm.model.lower()
    min_cache = 1024 if "haiku" in model_lower else 2048

    # Build system blocks with cache_control breakpoints
    tier1_content = _SYSTEM_PREAMBLE + codebook_text
    tier1_tokens_est = len(tier1_content) // 4

    system_blocks = []
    if tier1_tokens_est >= min_cache:
        # Tier 1 large enough to cache on its own
        system_blocks.append({
            "type": "text",
            "text": tier1_content,
            "cache_control": {"type": "ephemeral"},
        })
        if tier2_text.strip():
            tier2_tokens_est = len(tier2_text) // 4
            if tier2_tokens_est >= min_cache:
                system_blocks.append({
                    "type": "text",
                    "text": tier2_text,
                    "cache_control": {"type": "ephemeral"},
                })
            else:
                system_blocks.append({"type": "text", "text": tier2_text})
    else:
        # Combine tier 1 + tier 2 into single cached block
        combined = tier1_content + "\n\n" + tier2_text
        system_blocks.append({
            "type": "text",
            "text": combined,
            "cache_control": {"type": "ephemeral"},
        })

    # Tier 3: User message — just the question
    user_content = f"{question}\n\n{style}"

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=800,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
        )
        cost_info = calculate_cost(config.llm.model, response.usage)
        ledger.record(**cost_info, model=config.llm.model, purpose="ask")
        return response.content[0].text
    except Exception:
        return None


# ── register ─────────────────────────────────────────────────────────

def cmd_register(args):
    """Register a file path with a knowledge node.

    Associates filesystem paths with nodes so Claude Code can find
    the actual files that relate to a concept.
    """
    store = _store(args)
    node = store.get_node(args.node_id) or store.get_node_by_title(args.node_id)

    if not node:
        print(f"Error: '{args.node_id}' not found.", file=sys.stderr)
        sys.exit(1)

    filepath = Path(args.filepath).expanduser().resolve()
    if not filepath.exists():
        print(f"Warning: '{filepath}' does not exist.", file=sys.stderr)

    # Store file path in extra metadata
    extra = node.get("extra") or {}
    paths = extra.get("file_paths", [])
    path_str = str(filepath)
    if path_str not in paths:
        paths.append(path_str)
        extra["file_paths"] = paths
        store.update_node(node["id"], extra=extra)
        print(f"Registered: {filepath} -> {node['title']}")
    else:
        print(f"Already registered: {filepath} -> {node['title']}")

    store.close()


# ── skills ─────────────────────────────────────────────────────────────

def cmd_skills(args):
    """Show skill profile for a person."""
    store = _store(args)
    cfg = _config(args)

    person_name = args.person or cfg.current_user
    person = store.get_node_by_title(person_name) or store.get_node(person_name)

    if not person:
        # Try to find any person node that matches
        persons = store.all_nodes(node_type="person", limit=100)
        for p in persons:
            if person_name.lower() in p["title"].lower():
                person = p
                break

    if not person:
        print(f"No person node found for '{person_name}'.", file=sys.stderr)
        print("Create one with: kin add --type person <name>", file=sys.stderr)
        store.close()
        return

    # Find skill edges (demonstrates)
    edges = store.edges_from(person["id"], semantic_only=True)
    skill_edges = [e for e in edges if e.get("type") == "demonstrates"]

    # Also find context_of edges to skill nodes
    for e in edges:
        if e.get("type") != "demonstrates":
            target = store.get_node(e["to_id"])
            if target and target.get("type") == "skill":
                skill_edges.append(e)

    if args.json:
        skills = []
        for e in skill_edges:
            target = store.get_node(e["to_id"])
            if target:
                skills.append({
                    "title": target["title"],
                    "weight": target["weight"],
                    "edge_weight": e["weight"],
                    "provenance": e.get("provenance", ""),
                    "last_updated": target.get("updated_at", ""),
                })
        print(_dumps({"person": person["title"], "skills": skills}, indent=2))
    else:
        print(f"# Skills: {person['title']}\n")
        if not skill_edges:
            print("  No skills recorded yet.")
            print("  Record with: kin add --type skill '<skill name>'")
        else:
            for e in skill_edges:
                target = store.get_node(e["to_id"])
                if target:
                    when = (target.get("updated_at") or "")[:10]
                    prov = e.get("provenance", "")[:40]
                    print(f"  {target['title'][:40]:40s} w={target['weight']:.2f}  {when}  {prov}")

    store.close()


# ── import ─────────────────────────────────────────────────────────────

def cmd_import_graph(args):
    """Import nodes and edges from a JSON or JSONL file."""
    from .graph_transfer import import_records

    store = _store(args)
    dry_run = getattr(args, "dry_run", False)
    try:
        filepath = Path(args.filepath)
        text = filepath.read_text()
        if filepath.suffix == ".jsonl" or args.format == "jsonl":
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
            items = data if isinstance(data, list) else [data]
        counts = import_records(store, items, replace=getattr(args, "mode", "merge") == "replace",
                                dry_run=dry_run)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        store.close()
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Import complete: {counts['created']} created, {counts['updated']} updated, "
          f"{counts['edges']} edges, {counts['skipped']} skipped")


# ── cron ──────────────────────────────────────────────────────────────

def cmd_cron(args):
    """Run one-shot maintenance cycle (designed for crontab).

    With profiles configured, runs one pass per profile (each on its own
    data_dir), plus a legacy-remainder pass when no default_profile is set.
    An explicit --data-dir or --profile pins a single pass; session routing
    stays active whenever a profile resolves (the pinned pass only ingests
    the sessions that profile owns). A bare --data-dir with no resolved
    profile runs a legacy take-everything pass on exactly that directory.
    """
    cfg = _config(args)
    verbose = getattr(args, "verbose", False)

    from .daemon import cron_run_all

    if getattr(args, "data_dir", None) or getattr(args, "profile", None):
        # Explicit targeting: single pass on exactly this data_dir/profile.
        # Routing must survive the pin: build the session predicate from the
        # FULL profiles dict before clearing it, so the pinned pass never
        # ingests sessions owned by other profiles.
        if cfg.profiles and cfg.active_profile:
            from .routing import profile_session_filter
            cfg._session_filter = profile_session_filter(
                dict(cfg.profiles), cfg.active_profile, cfg.default_profile)
        cfg.profiles = {}
    passes = cron_run_all(cfg, verbose=verbose)

    if args.json:
        if len(passes) == 1 and passes[0]["profile"] is None:
            print(_dumps(passes[0]["results"], indent=2))  # legacy shape
        else:
            print(_dumps(passes, indent=2))
        return

    for p in passes:
        results = p["results"]
        if p["profile"]:
            print(f"Cron maintenance complete (profile: {p['profile']}):")
        else:
            print("Cron maintenance complete:")
        print(f"  Projects scanned:  {results.get('projects', 0)}")
        print(f"  .kin/config updates: {results.get('kin_updates', 0)}")
        print(f"  Sessions ingested: {results.get('sessions', 0)}")
        print(f"  Inbox processed:   {results.get('inbox', 0)}")
        print(f"  Nodes decayed:     {results.get('decayed', 0)}")
        print(f"  Link suggestions:  {results.get('link_suggestions', 0)}")
        archived = results.get("orphans_archived", 0)
        linked = results.get("orphans_linked", 0)
        if archived or linked:
            print(f"  Graph hygiene:     {archived} archived, {linked} auto-linked")
        slow = results.get("slow_graph_archived", 0)
        if slow:
            print(f"  Slow graph:        {slow} nodes moved to archive")
        w_expired = results.get("watches_expired", 0)
        w_notified = results.get("watches_notified", 0)
        if w_expired or w_notified:
            print(f"  Watches:           {w_expired} expired, {w_notified} boosted")
        candidate_pruned = results.get("capture_candidates_pruned", 0)
        if candidate_pruned:
            print(f"  Capture review:    {candidate_pruned} expired candidate(s) pruned")
        stats = results.get("stats", {})
        print(f"  Graph: {stats.get('nodes', 0)} nodes, "
              f"{stats.get('edges', 0)} edges, "
              f"{results.get('orphan_count', 0)} orphans")
        repack = results.get("repack", {})
        if repack:
            interval = repack.get("interval", "?")
            action = repack.get("action", "?")
            if action == "unchanged":
                print(f"  Cron interval: {interval}s (unchanged)")
            elif action == "updated":
                print(f"  Cron interval: {repack.get('previous', '?')}s -> {interval}s (adaptive)")
            elif action == "disabled":
                print(f"  Cron interval: disabled (no pending reminders)")


# ── dream ─────────────────────────────────────────────────────────────

def cmd_dream(args):
    """Run knowledge consolidation (dream cycle)."""
    store = _store(args)
    cfg = _config(args)
    verbose = getattr(args, "verbose", False)
    dry_run = getattr(args, "dry_run", False)
    detach = getattr(args, "detach", False)

    # Determine mode
    if getattr(args, "deep", False):
        mode = "deep"
    elif getattr(args, "lightweight", False):
        mode = "lightweight"
    else:
        mode = "full"

    if detach:
        from .dream import detach_dream
        result = detach_dream(cfg, mode=mode, force=getattr(args, "force", False))
        if not args.json:
            if result.get("detached"):
                print(f"Dream detached (pid={result['pid']}, mode={mode})")
            else:
                skipped = result.get("skipped", "not_due")
                detail = f"; next allowed {result['next_allowed']}" if result.get("next_allowed") else ""
                print(f"Dream detach skipped: {skipped}{detail}")
        else:
            print(_dumps(result))
        store.close()
        return

    from .dream import dream_cycle
    results = dream_cycle(cfg, store, mode=mode, verbose=verbose, dry_run=dry_run)

    if args.json:
        print(_dumps(results, indent=2))
    else:
        if results.get("skipped"):
            print(f"Dream skipped: {results['skipped']}")
        else:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"{prefix}Dream ({results.get('mode', mode)}) complete:")
            print(f"  Merged:              {results.get('merged', 0)}")
            print(f"  Suggested:           {results.get('suggested', 0)}")
            print(f"  Suggestions applied: {results.get('suggestions_applied', 0)}")
            proposals = results.get("domain_link_proposals", [])
            if "domain_link_proposals" in results:
                capped = " (capped)" if results.get(
                    "domain_link_proposals_capped"
                ) else ""
                print(f"  Domain proposals:    {len(proposals)}{capped}")
                if dry_run:
                    for proposal in proposals:
                        print(
                            f"    [{proposal['domain']}] "
                            f"{proposal['from_title']} <-> {proposal['to_title']}"
                        )
            created = results.get("domain_link_suggestions_created", 0)
            if created:
                print(f"  Domain suggestions:  {created} queued for review")
            if "domain_link_suggestions_pending" in results:
                print(
                    "  Domain review queue: "
                    f"{results['domain_link_suggestions_pending']}/"
                    f"{results.get('domain_link_proposal_limit', 0)}"
                )
            if "cluster_summaries" in results:
                print(f"  Cluster summaries:   {results['cluster_summaries']}")

    store.close()


# ── archive (slow graph) ──────────────────────────────────────────────

def cmd_archive(args):
    """Manage the slow graph archive."""
    from .archive import (
        archive_cycle,
        find_archive_duplicates,
        list_archives,
        restore_node,
        search_archives,
    )

    store = _store(args)
    cfg = _config(args)
    action = getattr(args, "archive_action", "list")

    if action == "list":
        archives = list_archives(cfg)
        if not archives:
            print("No archives yet. Stale nodes move here during cron cycles.")
            store.close()
            return
        total_nodes = 0
        total_size = 0.0
        for a in archives:
            total_nodes += a.get("nodes", 0)
            total_size += a.get("size_mb", 0)
            created = a.get("created_at", "?")
            print(f"  {a['name']}: {a.get('nodes', 0)} nodes, "
                  f"{a.get('edges', 0)} edges, {a['size_mb']}MB "
                  f"(created {created})")
        print(f"\nTotal: {len(archives)} archives, {total_nodes} nodes, "
              f"{total_size:.1f}MB")
        duplicates = find_archive_duplicates(cfg, store)
        if duplicates["count"]:
            sample = ", ".join(
                item["id"] for item in duplicates["samples"][:5]
            )
            print(
                "Warning: "
                f"{duplicates['count']} ID(s) exist in both fast and slow "
                f"graphs ({sample}); both copies were preserved for review."
            )

    elif action == "search":
        query = getattr(args, "query", "")
        if not query:
            print("Usage: kin archive search <query>")
            store.close()
            return
        results = search_archives(cfg, query)
        if not results:
            print(f"No archived nodes matching '{query}'")
        else:
            for r in results:
                print(f"  [{r['type']}] {r['title']} "
                      f"(id={r['id']}, archived={r['archived_at']}, "
                      f"from={r['archive_file']})")

    elif action == "restore":
        node_id = getattr(args, "node_id", "") or getattr(args, "query", "")
        if not node_id:
            print("Usage: kin archive restore <node-id>")
            store.close()
            return
        ok = restore_node(cfg, store, node_id, verbose=True)
        if ok:
            print(f"Restored {node_id} to fast graph.")
        else:
            print(f"Node {node_id} not found in any archive.")

    elif action == "run":
        count = archive_cycle(cfg, store, verbose=True)
        print(f"Archived {count} nodes to slow graph.")

    store.close()


# ── watch ─────────────────────────────────────────────────────────────

def cmd_watch(args):
    """Watch for new sessions and ingest them (long-running)."""
    import time

    store = _store(args)
    cfg = _config(args)
    interval = getattr(args, "interval", 60) or 60
    verbose = getattr(args, "verbose", False)

    from .daemon import find_new_sessions, incremental_ingest, set_run_marker

    # Start from now (or last run marker)
    from .daemon import last_run_marker as _last_run
    since = _last_run(cfg)
    if not since:
        import datetime as _dt
        since = _dt.datetime.now(tz=None).isoformat(timespec="seconds")

    print(f"Watching for new sessions (every {interval}s). Ctrl+C to stop.")
    print(f"  Since: {since}")

    try:
        while True:
            new_files = find_new_sessions(cfg, since)
            if new_files:
                count = incremental_ingest(cfg, store, since, verbose=verbose)
                if count > 0:
                    print(f"  [{_now_short()}] Ingested {count} new session(s)")
                    set_run_marker(store)

                # Update the since marker to now
                import datetime as _dt
                since = _dt.datetime.now(tz=None).isoformat(timespec="seconds")
            elif verbose:
                print(f"  [{_now_short()}] No new sessions")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nWatch stopped.")
    finally:
        store.close()


def _now_short() -> str:
    """Short timestamp for watch output."""
    import datetime as _dt
    return _dt.datetime.now(tz=None).strftime("%H:%M:%S")


# ── tasks ─────────────────────────────────────────────────────────────


def cmd_task(args):
    """Task CLI with meaningful errors and guaranteed store close."""
    store = _store(args)
    try:
        _cmd_task(args, store)
    except ValueError as exc:
        from .privacy import safe_error
        print(f"Error: {safe_error(exc)}", file=sys.stderr)
        raise SystemExit(2)
    finally:
        store.close()


def _cmd_task(args, store):
    """Graph-connected task management."""
    action = getattr(args, "task_action", "list")

    if action == "add":
        from .tasks import create_task
        title = " ".join(getattr(args, "title_words", []) or [])
        if not title:
            raise ValueError("Usage: kin task add <title> [--priority N] [--due ...] [--link ...]")
        link_to = None
        if getattr(args, "link_to", None):
            link_to = [s.strip() for s in args.link_to.split(",") if s.strip()]

        task_id = create_task(
            store, title,
            priority=getattr(args, "priority", 3) or 3,
            due=getattr(args, "due", None),
            scope=getattr(args, "scope", "contextual") or "contextual",
            effort=getattr(args, "effort", None),
            link_to=link_to,
            project_path=getattr(args, "project_path", None) or os.getcwd(),
            session_id=getattr(args, "session_id", None),
            content=getattr(args, "content", "") or "",
        )
        if getattr(args, "json", False):
            print(_dumps(store.get_node(task_id)))
        else:
            print(f"Created task: {task_id}")

    elif action == "list":
        from .tasks import list_tasks, format_task_list
        status_filter = getattr(args, "status", None) or "open"
        tasks = list_tasks(
            store,
            status=status_filter,
            scope=getattr(args, "scope", None),
            domain=getattr(args, "domain", None),
            project_path=getattr(args, "project_path", None),
            max_priority=getattr(args, "priority", None),
            limit=getattr(args, "limit", 20),
        )
        if getattr(args, "json", False):
            print(_dumps(tasks))
        elif not tasks:
            print("No tasks found.")
        else:
            print(format_task_list(tasks))

    elif action == "show":
        from .tasks import format_task
        task_id = getattr(args, "task_id", None)
        if not task_id:
            raise ValueError("Usage: kin task show --task-id <id>")
        node = store.get_node(task_id)
        if node and node.get("type") == "task":
            if getattr(args, "json", False):
                print(_dumps(node))
            else:
                print(format_task(node))
        else:
            raise ValueError(f"Task not found: {task_id}")

    elif action == "claim":
        from .config import resolve_agent_id
        from .tasks import claim_task
        task_id = getattr(args, "task_id", None)
        agent = getattr(args, "agent", None) or resolve_agent_id(_config(args))
        if not task_id:
            raise ValueError("Usage: kin task claim --task-id <id> [--agent <name>]")
        try:
            result = claim_task(
                store,
                task_id,
                agent,
                ttl_minutes=getattr(args, "ttl", 120) or 120,
                note=getattr(args, "note", "") or "",
                force=getattr(args, "force", False),
            )
            if result:
                claim = (result.get("extra") or {}).get("claim") or {}
                print(f"Claimed: {result['title']} by {claim.get('agent')}")
            else:
                raise ValueError(f"Task not found: {task_id}")
        except ValueError:
            raise

    elif action == "release":
        from .config import resolve_agent_id
        from .tasks import release_task_claim
        task_id = getattr(args, "task_id", None)
        if not task_id:
            raise ValueError("Usage: kin task release --task-id <id> [--agent <name>]")
        try:
            result = release_task_claim(
                store,
                task_id,
                agent=getattr(args, "agent", "") or resolve_agent_id(_config(args)),
                force=getattr(args, "force", False),
            )
            if result:
                print(f"Released claim: {result['title']}")
            else:
                raise ValueError(f"Task not found: {task_id}")
        except ValueError:
            raise

    elif action == "cleanup":
        from .tasks import cleanup_expired_claims
        count = cleanup_expired_claims(store)
        print(f"Cleaned expired claims: {count}")

    elif action == "done":
        from .tasks import complete_task
        task_id = getattr(args, "task_id", None)
        if not task_id:
            raise ValueError("Usage: kin task done --task-id <id>")
        result = complete_task(store, task_id)
        if result:
            print(f"Completed: {result['title']}")
        else:
            raise ValueError(f"Task not found: {task_id}")

    elif action == "cancel":
        from .tasks import cancel_task
        task_id = getattr(args, "task_id", None)
        if not task_id:
            raise ValueError("Usage: kin task cancel --task-id <id>")
        result = cancel_task(store, task_id)
        if result:
            print(f"Cancelled: {result['title']}")
        else:
            raise ValueError(f"Task not found: {task_id}")

    elif action == "update":
        from .tasks import update_task
        task_id = getattr(args, "task_id", None)
        if not task_id:
            raise ValueError("Usage: kin task update --task-id <id> [--priority N] [--due ...]")
        fields = {}
        if getattr(args, "priority", None):
            fields["priority"] = args.priority
        if getattr(args, "due", None) is not None:
            fields["due"] = args.due
        if getattr(args, "effort", None):
            fields["effort"] = args.effort
        if getattr(args, "scope", None):
            fields["scope"] = args.scope
        if getattr(args, "status", None):
            fields["task_status"] = args.status
        for key in ("content", "expected_version"):
            if getattr(args, key, None) is not None:
                fields[key] = getattr(args, key)
        if getattr(args, "title_words", None):
            fields["title"] = " ".join(args.title_words)
        result = update_task(store, task_id, **fields)
        if result:
            print(f"Updated: {result['title']}")
        else:
            raise ValueError(f"Task not found: {task_id}")

    elif action == "nearby":
        from .tasks import nearby_tasks, format_task_list
        from .retrieve import detect_domain_from_path, hybrid_search
        cwd = os.getcwd()
        domains = detect_domain_from_path(store, cwd)
        topic = " ".join(domains) if domains else os.path.basename(cwd)

        results = hybrid_search(store, topic, top_k=5)
        seed_ids = [r["id"] for r in results]

        tasks = nearby_tasks(store, seed_ids, max_hops=2)
        if getattr(args, "json", False):
            print(_dumps(tasks))
        elif not tasks:
            print("No nearby tasks for this context.")
        else:
            print(f"Tasks near: {topic}")
            print(format_task_list(tasks))

    store.close()


# ── coordination ──────────────────────────────────────────────────────


def cmd_coord(args):
    """Short-lived coordination conversations for agents."""
    from .config import resolve_agent_id
    cfg = _config(args)
    store = _store(args)
    action = getattr(args, "coord_action", "list")
    agent = getattr(args, "agent", "") or resolve_agent_id(cfg)

    if action == "start":
        from .coordination import create_conversation
        name = getattr(args, "name", None)
        if not name:
            print("Usage: kin coord start <name>", file=sys.stderr)
            store.close()
            return
        try:
            conv_id = create_conversation(
                store,
                name,
                task_id=getattr(args, "task_id", None),
                ttl_minutes=getattr(args, "ttl", 240) or 240,
                project_path=os.getcwd(),
                created_by=agent,
            )
            print(f"Started coordination conversation: {conv_id}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "post":
        from .coordination import post_message
        ref = getattr(args, "name", None)
        body = " ".join(getattr(args, "message_words", []) or [])
        if not ref or not body:
            print("Usage: kin coord post <name-or-id> <message> [--agent <name>] [--to <agent>]",
                  file=sys.stderr)
            store.close()
            return
        try:
            msg = post_message(store, ref, agent, body,
                               to=getattr(args, "to", None))
            print(f"Posted message #{msg['id']}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "read":
        from .coordination import format_messages, read_messages
        ref = getattr(args, "name", None)
        if not ref:
            print("Usage: kin coord read <name-or-id>", file=sys.stderr)
            store.close()
            return
        try:
            payload = read_messages(
                store,
                ref,
                since_id=getattr(args, "since_id", 0) or 0,
                limit=getattr(args, "limit", 50) or 50,
                agent=agent,
            )
            print(_dumps(payload) if getattr(args, "json", False) else format_messages(payload))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "join":
        from .coordination import join_conversation
        ref = getattr(args, "name", None)
        if not ref:
            print("Usage: kin coord join <name-or-id> [--agent <name>]", file=sys.stderr)
            store.close()
            return
        try:
            member = join_conversation(store, ref, agent)
            print(f"Joined {ref} as {member['agent']}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "attach":
        from .coordination import attach_resource
        ref = getattr(args, "name", None)
        words = getattr(args, "message_words", []) or []
        if not ref or not words:
            print("Usage: kin coord attach <name-or-id> <node-id-or-title>", file=sys.stderr)
            store.close()
            return
        target = " ".join(words)
        node = store.get_node(target) or store.get_node_by_title(target)
        try:
            resources = attach_resource(store, ref, node["id"] if node else target)
            print(f"Attached. Resources: {', '.join(resources)}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "inject":
        from .coordination import (
            clear_inject_messages,
            list_inject_messages,
            set_inject_message,
        )
        ref = getattr(args, "name", None)
        words = getattr(args, "message_words", []) or []
        sub = words[0] if words else "list"
        if not ref or sub not in ("set", "clear", "list"):
            print("Usage: kin coord inject <name-or-id> set <text> [--to <agent>] | "
                  "clear [--id N] | list", file=sys.stderr)
            store.close()
            return
        try:
            if sub == "set":
                text = " ".join(words[1:])
                entry = set_inject_message(store, ref, text, agent,
                                           to=getattr(args, "to", None))
                print(f"Set inject message #{entry['id']}"
                      + (f" -> {entry['to']}" if entry.get("to") else ""))
            elif sub == "clear":
                count = clear_inject_messages(
                    store, ref, message_id=getattr(args, "id", None))
                print(f"Cleared {count} inject message(s)")
            else:
                msgs = list_inject_messages(store, ref)
                if getattr(args, "json", False):
                    print(_dumps(msgs))
                elif not msgs:
                    print("No inject messages.")
                else:
                    for m in msgs:
                        target = f" -> {m['to']}" if m.get("to") else ""
                        print(f"  #{m.get('id')} {m.get('created_at')} "
                              f"{m.get('set_by')}{target}: {m.get('text')}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "list":
        from .coordination import format_conversations, list_conversations
        conversations = list_conversations(
            store,
            status=getattr(args, "status", None) or "active",
            project_path=os.getcwd() if getattr(args, "project", False) else None,
            task_id=getattr(args, "task_id", None),
        )
        print(_dumps(conversations) if getattr(args, "json", False)
              else format_conversations(conversations))

    elif action == "end":
        from .coordination import end_conversation
        ref = getattr(args, "name", None)
        if not ref:
            print("Usage: kin coord end <name-or-id>", file=sys.stderr)
            store.close()
            return
        result = end_conversation(store, ref, summary=getattr(args, "summary", "") or "")
        if result:
            print(f"Ended coordination conversation: {ref}")
        else:
            print(f"Conversation not found: {ref}", file=sys.stderr)

    elif action == "cleanup":
        from .coordination import cleanup_expired_conversations
        count = cleanup_expired_conversations(store)
        print(f"Cleaned expired conversations: {count}")

    store.close()


# ── locks ────────────────────────────────────────────────────────────


def cmd_lock(args):
    """Acquire an advisory lock on a node for the current agent."""
    from .config import resolve_agent_id
    from .locks import lock_node
    from .store import LockHeldError
    cfg = _config(args)
    store = _store(args)
    agent = getattr(args, "agent", "") or resolve_agent_id(cfg)
    ref = args.node_id
    node = store.get_node(ref) or store.get_node_by_title(ref)
    if not node:
        print(f"Error: '{ref}' not found.", file=sys.stderr)
        store.close()
        sys.exit(1)
    try:
        lock = lock_node(
            store, node["id"], agent,
            ttl_minutes=getattr(args, "ttl", 60) or 60,
            note=getattr(args, "note", "") or "",
            force=getattr(args, "force", False),
        )
        print(f"Locked {node['title']} ({node['id']}) for {lock['agent']} "
              f"until {lock['expires_at']}")
    except (LockHeldError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        store.close()
        sys.exit(1)
    store.close()


def cmd_unlock(args):
    """Release an advisory lock on a node."""
    from .config import resolve_agent_id
    from .locks import unlock_node
    from .store import LockHeldError
    cfg = _config(args)
    store = _store(args)
    agent = getattr(args, "agent", "") or resolve_agent_id(cfg)
    ref = args.node_id
    node = store.get_node(ref) or store.get_node_by_title(ref)
    if not node:
        print(f"Error: '{ref}' not found.", file=sys.stderr)
        store.close()
        sys.exit(1)
    try:
        cleared = unlock_node(store, node["id"], agent,
                              force=getattr(args, "force", False))
        if cleared:
            print(f"Unlocked {node['title']} ({node['id']})")
        else:
            print(f"No lock on {node['title']} ({node['id']})")
    except LockHeldError as e:
        print(f"Error: {e}", file=sys.stderr)
        store.close()
        sys.exit(1)
    store.close()


# ── modes ────────────────────────────────────────────────────────────


def cmd_mode(args):
    """Conversation mode management — activate, list, show, create, export, import, seed."""
    store = _store(args)
    action = getattr(args, "mode_action", "list")

    if action == "activate":
        from .modes import activate_mode
        name = getattr(args, "mode_name", None)
        if not name:
            print("Usage: kin mode activate <name>", file=sys.stderr)
            store.close()
            return
        ctx = getattr(args, "context", None)
        result = activate_mode(store, name, session_context=ctx)
        print(result)

    elif action == "list":
        from .modes import list_modes, format_mode_list, DEFAULT_MODES
        modes = list_modes(store)
        print(format_mode_list(modes, defaults=DEFAULT_MODES))

    elif action == "show":
        from .modes import get_mode, format_mode_detail, DEFAULT_MODES
        name = getattr(args, "mode_name", None)
        if not name:
            print("Usage: kin mode show <name>", file=sys.stderr)
            store.close()
            return
        mode = get_mode(store, name)
        default = DEFAULT_MODES.get(name) if not mode else None
        print(format_mode_detail(name, mode=mode, default=default))

    elif action == "create":
        from .modes import create_mode
        name = getattr(args, "mode_name", None)
        primer = getattr(args, "primer", None)
        boundary = getattr(args, "boundary", None)
        permissions = getattr(args, "permissions", None)
        if not all([name, primer, boundary, permissions]):
            print("Usage: kin mode create <name> --primer '...' --boundary '...' --permissions '...'",
                  file=sys.stderr)
            store.close()
            return
        desc = getattr(args, "description", "") or ""
        mode_id = create_mode(store, name, primer=primer, boundary=boundary,
                             permissions=permissions, description=desc)
        print(f"Created mode: {name} ({mode_id})")

    elif action == "export":
        from .modes import export_mode
        name = getattr(args, "mode_name", None)
        if not name:
            print("Usage: kin mode export <name>", file=sys.stderr)
            store.close()
            return
        artifact = export_mode(store, name)
        if artifact:
            print(json.dumps(artifact, indent=2))
        else:
            print(f"Mode not found: {name}", file=sys.stderr)

    elif action == "import":
        from .modes import import_mode
        fpath = getattr(args, "file", None)
        if not fpath:
            print("Usage: kin mode import <file.json>", file=sys.stderr)
            store.close()
            return
        try:
            with open(fpath) as f:
                artifact = json.load(f)
            mode_id = import_mode(store, artifact)
            print(f"Imported mode: {artifact.get('name', '?')} ({mode_id})")
        except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "seed":
        from .modes import seed_defaults
        created = seed_defaults(store)
        if created:
            print(f"Seeded {len(created)} modes: {', '.join(created)}")
        else:
            print("All default modes already exist.")

    store.close()


# ── session tags ──────────────────────────────────────────────────────


def cmd_remind(args):
    """Reminder management — create, list, snooze, done, cancel, check."""
    store = _store(args)
    cfg = _config(args)
    action = getattr(args, "remind_action", None)

    if action == "create" or action is None:
        from .reminders import create_reminder
        title = " ".join(getattr(args, "title_words", []) or [])
        time_spec = getattr(args, "at", None)
        if not title or not time_spec:
            print("Usage: kin remind create <title> --at <time>", file=sys.stderr)
            store.close()
            return
        try:
            rid = create_reminder(
                store, title, time_spec,
                priority=getattr(args, "priority", None) or "normal",
                channels=([c.strip() for c in args.channel.split(",") if c.strip()]
                          if getattr(args, "channel", None) else None),
                tags=getattr(args, "tag_str", "") or "",
                action_command=getattr(args, "action_command", "") or "",
                action_instructions=getattr(args, "action_instructions", "") or "",
                action_mode=getattr(args, "action_mode", "auto") or "auto",
                wake_client=getattr(args, "wake_client", "") or "",
                wake_session_id=getattr(args, "wake_session_id", "") or "",
                wake_cwd=getattr(args, "wake_cwd", "") or "",
                wake_model=getattr(args, "wake_model", "") or "",
                wake_agent=getattr(args, "wake_agent", "") or "",
                attention_triggers=([
                    t.strip() for t in getattr(args, "attention_trigger", "").split(",")
                    if t.strip()
                ] if getattr(args, "attention_trigger", None) else None),
                conversation_id=getattr(args, "conversation_id", "") or "",
                scope=getattr(args, "reminder_scope", "") or "",
            )
            r = store.get_reminder(rid)
            if getattr(args, "json", False):
                print(_dumps(r))
            else:
                print(f"Created reminder: {rid} (due: {r['next_due']})")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "list":
        from .reminders import format_reminder_list
        status_filter = getattr(args, "status", None)
        if status_filter == "all":
            status_filter = None
        reminders = store.list_reminders(
            status=status_filter,
            priority=getattr(args, "priority", None),
        )
        if getattr(args, "json", False):
            print(_dumps(reminders))
        elif not reminders:
            print("No reminders.")
        else:
            print(format_reminder_list(reminders))

    elif action == "show":
        from .reminders import format_reminder
        rid = getattr(args, "reminder_id", None)
        if not rid:
            print("Usage: kin remind show --reminder-id <id>", file=sys.stderr)
            store.close()
            return
        r = store.get_reminder(rid)
        if r:
            if getattr(args, "json", False):
                print(_dumps(r))
            else:
                print(format_reminder(r))
        else:
            print(f"Reminder not found: {rid}", file=sys.stderr)

    elif action == "snooze":
        from .reminders import snooze_reminder, parse_duration
        rid = getattr(args, "reminder_id", None)
        if not rid:
            print("Usage: kin remind snooze --reminder-id <id>", file=sys.stderr)
            store.close()
            return
        duration = getattr(args, "duration", None)
        duration_secs = parse_duration(duration) if duration else None
        try:
            new_time = snooze_reminder(store, rid, duration_secs, cfg)
            print(f"Snoozed until: {new_time}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "done":
        from .reminders import complete_reminder
        rid = getattr(args, "reminder_id", None)
        if not rid:
            print("Usage: kin remind done --reminder-id <id>", file=sys.stderr)
            store.close()
            return
        try:
            complete_reminder(store, rid)
            print(f"Completed: {rid}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "cancel":
        from .reminders import cancel_reminder
        rid = getattr(args, "reminder_id", None)
        if not rid:
            print("Usage: kin remind cancel --reminder-id <id>", file=sys.stderr)
            store.close()
            return
        try:
            cancel_reminder(store, rid)
            print(f"Cancelled: {rid}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "exec":
        from .actions import execute_action, has_action
        rid = getattr(args, "reminder_id", None)
        if not rid:
            print("Usage: kin remind exec --reminder-id <id>", file=sys.stderr)
            store.close()
            return
        r = store.get_reminder(rid)
        if not r:
            print(f"Reminder not found: {rid}", file=sys.stderr)
            store.close()
            return
        if not has_action(r):
            print(f"Reminder {rid} has no action defined.", file=sys.stderr)
            store.close()
            return
        result = execute_action(store, r, cfg, manual=True)
        if getattr(args, "json", False):
            print(_dumps(result))
        else:
            print(f"Action {result['status']}: {result.get('output', '')[:200]}")

    elif action == "check":
        if getattr(args, "all_profiles", False):
            # Sweep every configured profile graph (plus the legacy remainder)
            # — the shape the installed reminder scheduler runs, so no graph's
            # reminders depend on which profile happens to resolve here.
            from .daemon import remind_check_all
            sweeps = remind_check_all(cfg)
            if getattr(args, "json", False):
                print(_dumps(sweeps))
            else:
                for s in sweeps:
                    label = s["profile"] or "default"
                    print(f"Checked [{label}]: {s['fired']} fired, "
                          f"{s['auto_snoozed']} auto-snoozed")
        else:
            from .reminders import auto_snooze_stale, check_and_fire
            fired = check_and_fire(store, cfg)
            snoozed = auto_snooze_stale(store, cfg)
            if getattr(args, "json", False):
                print(_dumps({"fired": len(fired), "auto_snoozed": snoozed}))
            else:
                print(f"Checked: {len(fired)} fired, {snoozed} auto-snoozed")

    store.close()


def cmd_stop_guard(args):
    """Stop hook guard: block session exit if actionable reminders are pending.

    Outputs JSON with ``decision: "block"`` if there are pending actionable
    reminders due within the stop_guard_window.  Otherwise outputs nothing.
    """
    import json as _json

    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                payload = _json.loads(raw)
            except _json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("stop_hook_active"):
                return

    store = _store(args)
    cfg = _config(args)

    if (
        not cfg.reminders.enabled
        or not cfg.reminders.action_enabled
        or not cfg.reminders.stop_guard_enabled
    ):
        store.close()
        return

    from .actions import get_action_fields, has_action

    window_seconds = cfg.reminders.stop_guard_window
    cutoff = (
        datetime.datetime.now() + datetime.timedelta(seconds=window_seconds)
    ).isoformat(timespec="seconds")

    # Active reminders due within the window
    all_active = store.list_reminders(status="active")
    pending = []
    for r in all_active:
        if not has_action(r):
            continue
        fields = get_action_fields(r)
        if fields["action_status"] != "pending":
            continue
        if r["next_due"] <= cutoff:
            pending.append(r)

    # Also check already-due reminders still pending
    due_now = store.due_reminders()
    due_ids = {r["id"] for r in pending}
    for r in due_now:
        if r["id"] in due_ids:
            continue
        if not has_action(r):
            continue
        fields = get_action_fields(r)
        if fields["action_status"] == "pending":
            pending.append(r)

    store.close()

    if pending:
        titles = [r["title"] for r in pending[:5]]
        msg = (
            f"BLOCKED: {len(pending)} actionable reminder(s) pending. "
            f"Handle before exiting: {', '.join(titles)}. "
            f"Use `kin remind exec <id>` to run or `kin remind done <id>` to dismiss."
        )
        result = {"decision": "block", "message": msg}
        print(_json.dumps(result))


def _hook_context_output(context: str, *, adapter: str, event: str,
                         suppress: bool = False) -> str:
    """Render advisory context in the hook protocol for a client.

    suppress=True sets `suppressOutput` so the context still feeds the model via
    `additionalContext` but the client is asked NOT to render the block to the
    user (the "feed me, don't show you" quiet mode). Only meaningful for the JSON
    adapters; plain adapters echo the text and cannot hide it.
    """
    from .agent_adapters import render_hook_context

    return render_hook_context(
        context,
        adapter=adapter,
        event=event,
        suppress=suppress,
    )


def _collab_unread_messages(store, collab: dict, agent: str) -> list[dict]:
    """New messages in a collab for an agent: id > their read cursor,
    targeted to them or broadcast. Does NOT advance the cursor (only an
    explicit coord_read marks messages as read)."""
    node = store.get_node(collab.get("node_id", ""))
    if not node:
        return []
    extra = node.get("extra") or {}
    cursor = 0
    for member in extra.get("members") or []:
        if isinstance(member, dict) and member.get("agent") == agent:
            cursor = int(member.get("last_read_id", 0) or 0)
            break
    out = []
    for m in extra.get("messages") or []:
        if not isinstance(m, dict) or int(m.get("id", 0)) <= cursor:
            continue
        to = (m.get("to") or "").strip()
        if to and to != agent:
            continue
        out.append(m)
    return out


def _collab_prompt_lines(store, cfg, conversation_id: str) -> list[str]:
    """Collab updates for the UserPromptSubmit hook.

    New targeted/broadcast messages since the agent's read cursor plus standing
    inject messages, rate-limited per conversation via the store meta key
    'collab.prompt_last_injected.<conversation_id>' and
    config.collab.prompt_cooldown_minutes. Bodies are truncated to ~200 chars;
    read cursors are NOT advanced (only coord_read does that).
    """
    from .config import resolve_agent_id
    from .coordination import active_collabs_for_agent

    if not cfg.collab.enabled:
        return []

    agent = resolve_agent_id(cfg)
    collabs = [
        c for c in active_collabs_for_agent(store, agent)
        if c.get("unread_count") or c.get("inject_messages")
    ]
    if not collabs:
        return []

    # Cooldown: at most one collab block per conversation per window.
    key = f"collab.prompt_last_injected.{conversation_id or ''}"
    cooldown_min = max(0, int(cfg.collab.prompt_cooldown_minutes or 0))
    now = datetime.datetime.now()
    last = store.get_meta(key)
    if last and cooldown_min:
        try:
            elapsed = (now - datetime.datetime.fromisoformat(last)).total_seconds()
            if elapsed < cooldown_min * 60:
                return []
        except ValueError:
            pass

    lines = ["COLLAB UPDATES"]
    for c in collabs[:3]:
        name = c.get("name", "")
        unread = int(c.get("unread_count", 0) or 0)
        if unread:
            lines.append(f"  [{name}] {unread} new message(s):")
            for m in _collab_unread_messages(store, c, agent)[-3:]:
                body = " ".join(str(m.get("body", "")).split())[:200]
                author = m.get("author", "")
                target = " (to you)" if (m.get("to") or "").strip() == agent else ""
                lines.append(f"    - {author}{target}: {body}")
        for m in (c.get("inject_messages") or [])[:3]:
            text = " ".join(str(m.get("text", "")).split())[:200]
            set_by = (m.get("set_by") or "").strip()
            who = f" (from {set_by})" if set_by else ""
            lines.append(f"  [{name}] COLLAB MSG: {text}{who}")
        lines.append(f"  Check the collab: coord_read {name}")
    if len(collabs) > 3:
        lines.append(f"  +{len(collabs) - 3} more collabs")

    store.set_meta(key, now.isoformat(timespec="seconds"))
    return lines


def cmd_prompt_check(args):
    """UserPromptSubmit hook: inject due reminders into conversation context.

    Outputs plain text to stdout that Claude Code adds as visible context.
    Designed to be fast (<2s). Outputs nothing if no reminders are due.
    """
    store = _store(args)
    cfg = _config(args)

    from .agent_adapters import normalize_adapter, scope_adapter
    from .agent_settings import apply_agent_overrides, resolve_agent_instance_key

    adapter = normalize_adapter(getattr(args, "adapter", "plain"))
    hook_event = "UserPromptSubmit"
    hook_payload = {}
    conversation_id = ""
    strict_scope = bool(getattr(args, "conversation_id", None))

    # Conversation-attention checks are separate from time-due reminders.
    attention_lines: list[str] = []
    try:
        from .attention import (
            _load_state,
            _record_attention_delivery,
            extract_conversation_text,
            format_attention_injections,
            pop_pending_attention_injections,
            read_hook_payload,
            resolve_conversation_id,
            run_attention_check,
        )
        from .budget import BudgetLedger

        hook_payload = read_hook_payload()
        strict_scope = strict_scope or bool(hook_payload)
        hook_event = str(
            hook_payload.get("hook_event_name")
            or hook_payload.get("hookEventName")
            or hook_event
        )
        agent_instance = resolve_agent_instance_key(
            adapter,
            getattr(args, "agent_instance", None),
            hook_payload,
        )
        cfg = apply_agent_overrides(
            cfg,
            client=adapter,
            instance_key=agent_instance,
        )
        conversation_text = extract_conversation_text(
            getattr(args, "text", None),
            hook_payload,
        )
        conversation_id = resolve_conversation_id(
            getattr(args, "conversation_id", None),
            hook_payload,
            fallback_to_cwd=False,
        )
        if conversation_text and conversation_id:
            pending = pop_pending_attention_injections(
                store,
                cfg,
                conversation_id,
                conversation_text,
                tick=int(_load_state(store, conversation_id).get("ticks", 0)),
            )
            if pending:
                _record_attention_delivery(store, cfg, conversation_id, pending)
                attention_lines.extend(format_attention_injections(
                    {"injections": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "message": item.message,
                            "reason": item.reason,
                            "confidence": item.confidence,
                        }
                        for item in pending
                    ]},
                    display=cfg.attention.display,
                ))
            ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
            attention_result = run_attention_check(
                store,
                cfg,
                ledger,
                conversation_text,
                conversation_id,
                force=getattr(args, "force_attention", False),
                adapter=scope_adapter(adapter),
            )
            attention_lines = format_attention_injections(
                attention_result, display=cfg.attention.display
            )
    except Exception:
        attention_lines = []

    # Operator guidance is session-scoped: a new session (SessionStart) clears it
    # and tells you once, so stale steering never silently outlives a restart.
    sim_lines: list[str] = []
    try:
        if str(hook_event).lower() in ("sessionstart", "session_start"):
            from .sim import clear_sim_guidance
            if clear_sim_guidance(store):
                sim_lines.append("sim guidance cleared")
    except Exception:
        pass

    # Sim supervisory check-in (opt-in): enqueue a window snapshot for async
    # review and surface any pending injection a prior drain already graded.
    # Both halves are cheap (SQLite-only); the Sim/LLM spend happens in the daemon.
    try:
        from .sim import sim_effective_enabled
        if conversation_id and sim_effective_enabled(store, cfg):
            from .attention import _load_state as _att_state
            from .sim import (
                enqueue_sim_review,
                format_sim_injection,
                pop_pending_sim_injection,
            )

            tick = int(_att_state(store, conversation_id).get("ticks", 0))
            window = ""
            tpath = hook_payload.get("transcript_path") or hook_payload.get("transcriptPath")
            if tpath:
                from .reinforce import _bounded_trace
                window = _bounded_trace(str(tpath), cfg.sim.window_chars)
            if not window:
                window = conversation_text
            queued = enqueue_sim_review(store, cfg, conversation_id, window, tick=tick)
            sim_injection = pop_pending_sim_injection(
                store, cfg, conversation_id, window, tick=tick
            )
            sim_lines.extend(format_sim_injection(sim_injection, display=cfg.sim.display))
            # No daemon? Drain off-path in the background so the review lands for
            # a later tick. Only bother when we actually queued something new.
            if queued and cfg.sim.drain_on_tick:
                from .sim import spawn_background_drain
                spawn_background_drain(cfg)
    except Exception:
        pass

    # Collab updates: new targeted/broadcast messages since the agent's read
    # cursor + standing inject messages, with a per-conversation cooldown.
    collab_lines: list[str] = []
    try:
        collab_lines = _collab_prompt_lines(store, cfg, conversation_id)
    except Exception:
        collab_lines = []

    due = []
    if cfg.reminders.enabled:
        try:
            from .reminders import scoped_due_reminders
            due = scoped_due_reminders(
                store,
                conversation_id,
                include_global=True,
                include_legacy=not strict_scope,
            )
        except Exception:
            due = []

    # Also check tasks that are urgent/overdue
    task_lines = []
    try:
        from .scoping import item_matches_conversation
        from .tasks import list_tasks
        urgent_tasks = list_tasks(store, status="open", limit=5)
        for t in urgent_tasks:
            if not item_matches_conversation(
                t,
                conversation_id,
                include_global=True,
                include_legacy=not strict_scope,
            ):
                continue
            extra = t.get("extra") or {}
            p = extra.get("priority", 3)
            due_date = extra.get("due", "")
            if p <= 2 or (due_date and due_date[:10] <= datetime.date.today().isoformat()):
                p_label = {1: "URGENT", 2: "HIGH"}.get(p, "")
                task_lines.append(f"  - [{p_label}] {t['title']} (id: {t['id']})")
    except Exception:
        pass

    if (not due and not attention_lines and not task_lines and not sim_lines
            and not collab_lines):
        store.close()
        return

    # ANSI codes for visual distinction
    BOLD = "\033[1m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BEL = "\a"

    lines = []
    lines.append(f"{BEL}<system-reminder>")
    if due:
        lines.append(f"{BOLD}{RED}{'=' * 50}")
        lines.append(f"  KINDEX REMINDERS DUE ({len(due)})")
        lines.append(f"{'=' * 50}{RESET}")
        for r in due[:5]:
            priority = r.get("priority", "normal")
            p_color = RED if priority in ("urgent", "high") else YELLOW
            p_marker = f" {p_color}[{priority.upper()}]{RESET}" if priority != "normal" else ""
            extra = r.get("extra") or {}
            lines.append(f"  {BOLD}{CYAN}-{RESET}{p_marker} {r['title']} (due: {r['next_due'][:16]}, id: {r['id']})")
            if extra.get("action_instructions"):
                lines.append(f"    Instructions: {extra['action_instructions'][:100]}")
            if extra.get("action_command"):
                lines.append(f"    Action: `{extra['action_command']}`")
        lines.append("")
        lines.append(f"{BOLD}Act on these NOW:{RESET}")
        lines.append(f"  - `kin remind done <id>` to complete")
        lines.append(f"  - `kin remind snooze <id>` to defer")
        lines.append(f"  - `kin remind exec <id>` to run action")

    if attention_lines:
        if due:
            lines.append("")
        lines.extend(attention_lines)

    if sim_lines:
        if due or attention_lines:
            lines.append("")
        lines.extend(sim_lines)

    if collab_lines:
        if due or attention_lines or sim_lines:
            lines.append("")
        lines.extend(collab_lines)

    if task_lines:
        lines.append("")
        lines.append(f"{BOLD}{RED}URGENT TASKS:{RESET}")
        lines.extend(task_lines)

    lines.append("</system-reminder>")

    # Quiet mode: still feed the context to the model, but ask the client not to
    # render the block to the user — they see the system working in the agent's
    # behavior, not as background status hum. (Empirically: depends on the client
    # honoring suppressOutput while keeping additionalContext.)
    suppress = str(getattr(cfg.attention, "display", "full")).lower() == "quiet"

    rendered = _hook_context_output(
        "\n".join(lines),
        # Preserve the caller's protocol. Forcing Claude here broke
        # Antigravity quiet-mode hooks by emitting a Claude envelope.
        adapter=adapter,
        event=hook_event,
        suppress=suppress,
    )
    if rendered:
        print(rendered)
    store.close()


def cmd_attention_hook(args):
    """Advisory attention hook for tool/action boundaries."""
    import time

    from .agent_adapters import (
        antigravity_allow,
        normalize_adapter,
        permission_gate_output,
        scope_adapter,
    )
    from .agent_settings import apply_agent_overrides, resolve_agent_instance_key
    from .attention import (
        extract_conversation_text,
        format_attention_injections,
        is_background_action,
        pop_pending_attention_injections,
        prepare_async_attention_review,
        read_hook_payload,
        _record_attention_delivery,
        _load_state,
        resolve_conversation_id,
        wait_for_pending_attention,
    )

    payload = read_hook_payload()
    event = str(
        getattr(args, "event", None)
        or payload.get("hook_event_name")
        or payload.get("hookEventName")
        or "PreToolUse"
    )
    adapter = normalize_adapter(getattr(args, "adapter", "claude"))
    gate = permission_gate_output(adapter=adapter, event=event, payload=payload)
    if gate:
        print(gate)
        return

    def allow_if_needed() -> None:
        if adapter == "antigravity" and event == "PreToolUse":
            print(antigravity_allow())

    text = extract_conversation_text(getattr(args, "text", None), payload)
    if not text:
        allow_if_needed()
        return

    deadline_ms = max(0, int(getattr(args, "deadline_ms", 3500) or 3500))
    deadline = time.monotonic() + (deadline_ms / 1000.0)
    store = None
    try:
        cfg = _config(args)
        store = _hook_store(args, cfg)
        agent_instance = resolve_agent_instance_key(
            adapter,
            getattr(args, "agent_instance", None),
            payload,
        )
        cfg = apply_agent_overrides(cfg, client=adapter, instance_key=agent_instance)

        # Ignore Kindex's own noise and local/background tool calls — attention
        # should only weigh in on outward-facing or irreversible actions.
        if not getattr(args, "force", False) and is_background_action(payload, cfg):
            allow_if_needed()
            return
        conversation_id = resolve_conversation_id(
            getattr(args, "conversation_id", None),
            payload,
            fallback_to_cwd=False,
        )
        if not conversation_id:
            allow_if_needed()
            return

        delivered = pop_pending_attention_injections(
            store,
            cfg,
            conversation_id,
            text,
            tick=int(_load_state(store, conversation_id).get("ticks", 0)),
        )
        if delivered:
            _record_attention_delivery(store, cfg, conversation_id, delivered)

        prepared = prepare_async_attention_review(
            store,
            cfg,
            text,
            conversation_id,
            force=getattr(args, "force", False),
            adapter=scope_adapter(adapter),
        )
        job = prepared.get("job") or {}
        if job and time.monotonic() < deadline:
            immediate = wait_for_pending_attention(
                store,
                cfg,
                conversation_id,
                text,
                tick=int(prepared.get("ticks", 0) or 0),
                job_id=str(job.get("job_id") or ""),
                deadline=deadline,
            )
            if immediate:
                _record_attention_delivery(store, cfg, conversation_id, immediate)
                delivered.extend(immediate)

        deduped: list = []
        seen = set()
        for injection in delivered:
            key = (injection.id, injection.message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(injection)
        if not deduped:
            allow_if_needed()
            return

        result = {"injections": [
            {
                "id": item.id,
                "title": item.title,
                "message": item.message,
                "reason": item.reason,
                "confidence": item.confidence,
            }
            for item in deduped
        ]}
        lines = format_attention_injections(result, display=cfg.attention.display)
        if not lines:
            allow_if_needed()
            return
        rendered = _hook_context_output(
            "\n".join(lines),
            adapter=adapter,
            event=event,
            suppress=str(getattr(cfg.attention, "display", "full")).lower() == "quiet",
        )
        if rendered:
            print(rendered)
    except Exception:
        allow_if_needed()
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def cmd_sim(args):
    """Runtime controls for the Sim supervisory check-in."""
    store = _store(args)
    cfg = _config(args)
    action = getattr(args, "sim_action", "status")

    from .sim import (
        call_sim,
        clear_sim_guidance,
        clear_sim_override,
        drain_sim_queue,
        get_sim_guidance,
        set_sim_enabled,
        set_sim_guidance,
        sim_status,
    )

    if action == "guidance":
        if getattr(args, "clear", False):
            clear_sim_guidance(store)
            print("Sim guidance cleared.")
            store.close()
            return
        text = (getattr(args, "text", None) or "").strip()
        if text:
            set_sim_guidance(store, text)
            print(f"Sim guidance set (clears on restart):\n  {text}")
        else:
            current = get_sim_guidance(store)
            print(f"Sim guidance: {current}" if current else "Sim guidance: (none)")
        store.close()
        return

    if action in ("on", "enable"):
        set_sim_enabled(store, True)
        print("Sim supervisory check-in enabled (runtime override).")
        store.close()
        return

    if action in ("off", "disable"):
        set_sim_enabled(store, False)
        print("Sim supervisory check-in disabled (runtime override). Use `kin sim inherit` to follow config again.")
        store.close()
        return

    if action == "inherit":
        clear_sim_override(store)
        print("Sim runtime override cleared; config.sim.enabled now governs.")
        store.close()
        return

    if action == "drain":
        result = drain_sim_queue(store, cfg)
        print(_dumps(result, indent=2))
        store.close()
        return

    if action == "check":
        # Manual one-shot review of a window from --text or stdin (ignores the
        # threshold — shows the raw rating/note so you can calibrate).
        text = getattr(args, "text", None) or ""
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        if not text.strip():
            print("Error: sim check needs --text or a window on stdin", file=sys.stderr)
            store.close()
            sys.exit(1)
        from .budget import BudgetLedger
        from .sim import _capture_intent, build_sim_grounding, get_sim_guidance
        ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
        grounding = build_sim_grounding(store, text, cfg)
        result, acct = call_sim(
            cfg, ledger, text, "sim-check", client=None,
            guidance=get_sim_guidance(store),
            grounding=grounding,
            intent=_capture_intent(store),
        )
        payload = {"status": acct.get("status")}
        if result:
            payload.update({"rating": result.rating, "note": result.note,
                            "basis": result.basis,
                            "dimension": result.dimension, "stakes": result.stakes,
                            "escalate": result.escalate,
                            "escalate_reason": result.escalate_reason,
                            "would_inject": result.rating >= cfg.sim.threshold})
        print(_dumps(payload, indent=2))
        store.close()
        return

    # status (default)
    print(_dumps(sim_status(store, cfg), indent=2))
    store.close()


def cmd_attention(args):
    """Runtime controls for conversation-attention checks."""
    store = _store(args)
    cfg = _config(args)
    action = getattr(args, "attention_action", "status")
    conversation_id = getattr(args, "conversation_id", None)

    from .attention import (
        clear_runtime_enabled,
        drain_attention_queue,
        extract_conversation_text,
        estimate_message_window,
        format_attention_injections,
        parse_hook_payload,
        resolve_conversation_id,
        run_attention_check,
        runtime_status,
        set_runtime_enabled,
    )
    from .budget import BudgetLedger

    if action == "on":
        set_runtime_enabled(store, True, conversation_id=conversation_id)
        scope = f"conversation {conversation_id}" if conversation_id else "global runtime"
        print(f"Attention enabled ({scope}).")
        store.close()
        return

    if action == "off":
        set_runtime_enabled(store, False, conversation_id=conversation_id)
        scope = f"conversation {conversation_id}" if conversation_id else "global runtime"
        print(f"Attention disabled ({scope}).")
        store.close()
        return

    if action == "inherit":
        clear_runtime_enabled(store, conversation_id=conversation_id)
        scope = f"conversation {conversation_id}" if conversation_id else "global runtime"
        print(f"Attention override cleared ({scope}).")
        store.close()
        return

    if action == "drain":
        result = drain_attention_queue(store, cfg)
        print(_dumps(result, indent=2))
        store.close()
        return

    if action == "check":
        raw = ""
        if not getattr(args, "text", None) and not sys.stdin.isatty():
            raw = sys.stdin.read()
        payload = parse_hook_payload(raw)
        conversation_id = resolve_conversation_id(conversation_id, payload)
        text = extract_conversation_text(getattr(args, "text", None), payload)
        if not text:
            print("Error: attention check needs --text or hook JSON on stdin", file=sys.stderr)
            store.close()
            sys.exit(1)
        ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
        result = run_attention_check(
            store,
            cfg,
            ledger,
            text,
            conversation_id,
            force=getattr(args, "force", False),
        )
        if getattr(args, "json", False):
            print(_dumps(result, indent=2))
        else:
            lines = format_attention_injections(result)
            if lines:
                print("\n".join(lines))
            else:
                print(f"No attention injection ({result.get('status')}).")
        store.close()
        return

    if action == "reinforce":
        # Session-end grading: read the trace (transcript/summary) from --text or stdin.
        raw = ""
        if not getattr(args, "text", None) and not sys.stdin.isatty():
            raw = sys.stdin.read()
        payload = parse_hook_payload(raw)
        conversation_id = resolve_conversation_id(conversation_id, payload)

        # --enqueue: super-lightweight hook path — record for later cron grading
        # and return SILENTLY (no LLM, no output). Used by Stop / PreCompact.
        if getattr(args, "enqueue", False):
            from .reinforce import enqueue_reinforce
            transcript_path = (payload.get("transcript_path")
                               or payload.get("transcriptPath") or "")
            enqueue_reinforce(store, conversation_id, transcript_path=transcript_path)
            store.close()
            return

        trace = extract_conversation_text(getattr(args, "text", None), payload) or raw
        from .reinforce import reinforce_session
        ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
        result = reinforce_session(store, cfg, conversation_id, trace, ledger=ledger)
        if getattr(args, "json", False):
            print(_dumps(result, indent=2))
        else:
            outs = result.get("outcomes", [])
            obs = sum(1 for o in outs if o.get("injected"))
            cf = sum(1 for o in outs if not o.get("injected"))
            print(f"Reinforcement ({result.get('status')}): "
                  f"{obs} confirmed-useful, {cf} counterfactual, "
                  f"{len(result.get('gaps', []))} knowledge gap(s).")
            for o in outs:
                kind = "used" if o.get("injected") else "missed"
                print(f"  [{kind}/{o.get('category')}] +{o.get('amount')} {o.get('title','')[:60]}")
        store.close()
        return

    if action == "budget":
        ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
        resolved = conversation_id or ""
        summary = ledger.summary(conversation_id=resolved or None)
        if getattr(args, "json", False):
            print(_dumps(summary, indent=2))
        else:
            print(yaml.dump(summary, default_flow_style=False, sort_keys=False).strip())
        store.close()
        return

    if action == "estimate":
        ledger = BudgetLedger(cfg.ledger_path, cfg.budget)
        messages = getattr(args, "messages", None) or 100
        estimate = estimate_message_window(
            cfg,
            messages=messages,
            observed_entries=ledger.entries,
        )
        if getattr(args, "json", False):
            print(_dumps(estimate, indent=2))
        else:
            print(yaml.dump(estimate, default_flow_style=False, sort_keys=False).strip())
        store.close()
        return

    status = runtime_status(store, cfg, conversation_id)
    if getattr(args, "json", False):
        print(_dumps(status, indent=2))
    else:
        print(yaml.dump(status, default_flow_style=False, sort_keys=False).strip())
    store.close()


def cmd_tag(args):
    """Session tag management — named work context handles."""
    store = _store(args)
    action = getattr(args, "tag_action", None)
    tag_name = getattr(args, "tag_name", None)

    if action == "start":
        from .sessions import start_tag

        if not tag_name:
            print("Usage: kin tag start <name>", file=sys.stderr)
            store.close()
            return
        remaining = []
        raw = getattr(args, "remaining", None)
        if raw:
            remaining = [r.strip() for r in raw.split(",") if r.strip()]
        try:
            nid = start_tag(
                store,
                tag_name,
                description=getattr(args, "description", "") or "",
                focus=getattr(args, "focus", "") or "",
                remaining=remaining,
                project_path=os.getcwd(),
            )
            print(f"Started session tag: {tag_name} ({nid})")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "update":
        from .sessions import get_active_tag, update_tag

        if not tag_name:
            active = get_active_tag(store, project_path=os.getcwd())
            if active:
                tag_name = (active.get("extra") or {}).get("tag", active["title"])
            else:
                print("No active session tag. Use: kin tag start <name>", file=sys.stderr)
                store.close()
                return
        remaining = None
        raw = getattr(args, "remaining", None)
        if raw:
            remaining = [r.strip() for r in raw.split(",") if r.strip()]
        append = None
        raw_add = getattr(args, "add_remaining", None)
        if raw_add:
            append = [r.strip() for r in raw_add.split(",") if r.strip()]
        remove = None
        raw_done = getattr(args, "done", None)
        if raw_done:
            remove = [r.strip() for r in raw_done.split(",") if r.strip()]
        try:
            update_tag(
                store,
                tag_name,
                focus=getattr(args, "focus", None),
                description=getattr(args, "description", None),
                remaining=remaining,
                append_remaining=append,
                remove_remaining=remove,
                project_path=os.getcwd(),
            )
            print(f"Updated: {tag_name}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "segment":
        from .sessions import add_segment, get_active_tag

        if not tag_name:
            active = get_active_tag(store, project_path=os.getcwd())
            if active:
                tag_name = (active.get("extra") or {}).get("tag", active["title"])
        if not tag_name:
            print("No active session tag.", file=sys.stderr)
            store.close()
            return
        focus = getattr(args, "focus", None) or "New segment"
        summary = getattr(args, "summary", None) or ""
        try:
            add_segment(
                store,
                tag_name,
                new_focus=focus,
                summary=summary,
                project_path=os.getcwd(),
            )
            print(f"New segment on {tag_name}: {focus}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "pause":
        from .sessions import get_active_tag, pause_tag

        if not tag_name:
            active = get_active_tag(store, project_path=os.getcwd())
            if active:
                tag_name = (active.get("extra") or {}).get("tag", active["title"])
        if not tag_name:
            print("No active session tag.", file=sys.stderr)
            store.close()
            return
        summary = getattr(args, "summary", None) or ""
        try:
            pause_tag(
                store, tag_name, summary=summary, project_path=os.getcwd()
            )
            print(f"Paused: {tag_name}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "end":
        from .sessions import complete_tag, get_active_tag

        if not tag_name:
            active = get_active_tag(store, project_path=os.getcwd())
            if active:
                tag_name = (active.get("extra") or {}).get("tag", active["title"])
        if not tag_name:
            print("No active session tag.", file=sys.stderr)
            store.close()
            return
        summary = getattr(args, "summary", None) or ""
        try:
            complete_tag(
                store, tag_name, summary=summary, project_path=os.getcwd()
            )
            print(f"Completed: {tag_name}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "resume":
        from .sessions import format_resume_context, resume_tag

        if not tag_name:
            print("Usage: kin tag resume <name>", file=sys.stderr)
            store.close()
            return
        tokens = getattr(args, "tokens", 1500)
        if tokens is None:
            tokens = 1500
        try:
            resume_tag(store, tag_name, project_path=os.getcwd())
            block = format_resume_context(
                store,
                tag_name,
                max_tokens=tokens,
                evaluation_time=operation_now(),
                project_path=os.getcwd(),
            )
            print(block)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif action == "list":
        from .sessions import list_tags

        status = getattr(args, "status", None)
        project = os.getcwd() if getattr(args, "project", False) else None
        tags = list_tags(store, status=status, project_path=project)
        if not tags:
            print("No session tags found.")
        else:
            for t in tags:
                extra = t.get("extra") or {}
                tname = extra.get("tag", t["title"])
                tstatus = extra.get("session_status", "?")
                tfocus = extra.get("current_focus", "")[:50]
                seg_count = len(extra.get("segments", []))
                updated = (t.get("updated_at") or "")[:16]
                reason = extra.get("paused_reason")
                reason_text = f" reason={reason}" if reason else ""
                print(
                    f"  [{tstatus:9s}] {tname:25s} {tfocus:50s} "
                    f"({seg_count} seg) {updated}{reason_text}"
                )

    elif action == "show":
        from .sessions import get_tag

        if not tag_name:
            print("Usage: kin tag show <name>", file=sys.stderr)
            store.close()
            return
        tag = get_tag(store, tag_name, project_path=os.getcwd())
        if not tag:
            print(f"Tag not found: {tag_name}", file=sys.stderr)
            store.close()
            return
        extra = tag.get("extra") or {}
        print(f"Tag: {extra.get('tag', tag['title'])}")
        print(f"Status: {extra.get('session_status', '?')}")
        print(f"Project: {extra.get('project_path', '')}")
        print(f"Focus: {extra.get('current_focus', '')}")
        print(f"Started: {extra.get('started_at', '')}")
        if extra.get("paused_at"):
            print(f"Paused: {extra['paused_at']}")
        if extra.get("paused_reason"):
            print(f"Pause reason: {extra['paused_reason']}")
        if extra.get("completed_at"):
            print(f"Completed: {extra['completed_at']}")
        if tag.get("content"):
            print(f"Description: {tag['content']}")
        remaining = extra.get("remaining", [])
        if remaining:
            print(f"Remaining ({len(remaining)}):")
            for item in remaining:
                print(f"  - {item}")
        segments = extra.get("segments", [])
        if segments:
            print(f"Segments ({len(segments)}):")
            for seg in segments:
                state = "active" if not seg.get("ended_at") else "done"
                print(f"  [{state}] {seg.get('focus', '')}")
                if seg.get("summary"):
                    print(f"         {seg['summary'][:100]}")
                if seg.get("decisions"):
                    print(f"         Decisions: {', '.join(seg['decisions'][:3])}")
        linked = extra.get("linked_nodes", [])
        if linked:
            print(f"Linked nodes ({len(linked)}):")
            for nid in linked[:10]:
                node = store.get_node(nid)
                if node:
                    print(f"  - {node['title']} ({node['type']})")

    store.close()


# ── setup ─────────────────────────────────────────────────────────────

def cmd_setup_hooks(args):
    """Select/install one Claude adapter, preserving unrelated hook handlers."""
    from .claude_install import install
    for action in install(_config(args), mode=getattr(args, "mode", "legacy"),
                          dry_run=getattr(args, "dry_run", False),
                          uninstall=getattr(args, "uninstall", False),
                          retire_commands=getattr(args, "retire_command", None)):
        print(action)


def cmd_hook_rpc(args):
    """Versioned structured adapter RPC; never emit prose on stdout."""
    from .integrations import dispatch
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Kindex hook request exceeds 1 MiB")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Kindex hook request must be an object")
        result = dispatch(request)
    except Exception as error:
        from .privacy import safe_error
        result = {"ok": False, "error": {"code": "invalid_request", "message": safe_error(error)}}
    print(json.dumps(result))


def cmd_integration_doctor(args):
    """Report actual storage, ownership, and host qualification, without enabling."""
    from .integrations import describe, project_scope
    from .privacy import safe_error
    try:
        scope = project_scope({"project_path": str(Path(getattr(args, "project_path", None) or os.getcwd()).resolve()),
                               "session_id": "doctor", "agent": "claude"})
        result = describe(scope)
        from .claude_install import QUALIFIED_CLAUDE_VERSION
        result["qualified_claude_version"] = QUALIFIED_CLAUDE_VERSION
        cfg = _config(args)
        record = cfg.claude_path / "kindex-adapter.json"
        result["adapter"] = json.loads(record.read_text()) if record.exists() else {"mode": "unmanaged"}
    except Exception as error:
        result = {"ok": False, "error": safe_error(error)}
    print(json.dumps(result, indent=2))


def cmd_integration_reconcile(args):
    """Bounded delivery recovery; never repeat the committed task effect."""
    from .integrations import reconcile_outcomes
    from .privacy import safe_error
    try:
        result = reconcile_outcomes({
            "project_path": str(Path(args.project_path or os.getcwd()).resolve()),
            "session_id": "integration-reconcile", "agent": "human",
        }, max_attempts=args.max_attempts)
    except Exception as error:
        result = {"ok": False, "error": safe_error(error)}
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


def cmd_repo_memory(args):
    """Transport selected shareable evidence with code, not Personal data."""
    from .integrations import open_project_store, project_scope
    from .repo_memory import publish, import_candidates
    scope = project_scope({"project_path": str(Path(args.project_path or os.getcwd()).resolve()),
                           "session_id": "repo-memory-cli", "agent": "human"})
    store = open_project_store(scope)
    try:
        if args.repo_memory_action == "publish":
            result = publish(store, scope["project_path"], args.node_ids)
        else:
            result = import_candidates(store, scope["project_path"])
        print(json.dumps(result, indent=2))
    finally:
        store.close()


def cmd_setup_codex_hooks(args):
    """Install/uninstall Kindex prompt-time hooks in Codex."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_codex_hooks
        actions = uninstall_codex_hooks(cfg, dry_run=dry_run)
    else:
        from .setup import install_codex_hooks
        actions = install_codex_hooks(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_codex_mcp(args):
    """Install/uninstall Kindex as a Codex MCP server."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_codex_mcp
        actions = uninstall_codex_mcp(cfg, dry_run=dry_run)
    else:
        from .setup import install_codex_mcp
        actions = install_codex_mcp(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_cron(args):
    """Install/uninstall periodic cron job for kin maintenance."""
    import platform
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)
    method = getattr(args, "method", None)

    # Auto-detect method
    if method is None:
        method = "launchd" if platform.system() == "Darwin" else "crontab"

    if getattr(args, "uninstall", False):
        if method == "launchd":
            from .setup import uninstall_launchd, uninstall_reminder_daemon
            actions = uninstall_launchd(dry_run=dry_run)
            actions += uninstall_reminder_daemon(dry_run=dry_run)
        else:
            # Remove crontab entries (maintenance + reminder check)
            import subprocess
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.splitlines()
                # Match only our installed shapes — a bare "remind check"
                # marker could delete an unrelated user line.
                from .setup import is_kindex_cron_line
                filtered = [l for l in lines if not is_kindex_cron_line(l)]
                if len(filtered) < len(lines):
                    if not dry_run:
                        new_crontab = "\n".join(filtered) + "\n"
                        subprocess.run(["crontab", "-"], input=new_crontab,
                                       capture_output=True, text=True)
                    actions = ["Removed crontab entries"]
                else:
                    actions = ["No crontab entry found"]
            else:
                actions = ["No crontab found"]
    else:
        # Install the maintenance job AND a dedicated reminder-check job:
        # reminder delivery must never wait behind a slow maintenance run.
        if method == "launchd":
            from .setup import install_launchd, install_reminder_daemon
            actions = install_launchd(cfg, dry_run=dry_run)
            actions += install_reminder_daemon(cfg, dry_run=dry_run)
        else:
            from .setup import install_crontab
            actions = install_crontab(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_claude_md(args):
    """Output recommended CLAUDE.md block for kindex integration.

    kin setup claude-md           — print to stdout
    kin setup claude-md --install — append to ~/.claude/CLAUDE.md if not present
    """
    block = _kindex_claude_md_block()

    if getattr(args, "install", False):
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
        if claude_md.exists():
            existing = claude_md.read_text()
            if "Kindex (REQUIRED" in existing or "kindex MCP tools" in existing:
                print("Kindex directives already present in CLAUDE.md")
                return
            with open(claude_md, "a") as f:
                f.write("\n" + block)
            print(f"Appended kindex directives to {claude_md}")
        else:
            claude_md.parent.mkdir(parents=True, exist_ok=True)
            claude_md.write_text(block)
            print(f"Created {claude_md} with kindex directives")
    else:
        print(block)


def cmd_setup_agents_md(args):
    """Output recommended AGENTS.md block for Codex/kindex integration.

    kin setup-agents-md           — print to stdout
    kin setup-agents-md --install — append to ./AGENTS.md if not present
    kin setup-agents-md --install --global — append to ~/.codex/AGENTS.md
    """
    block = _kindex_agents_md_block()

    if getattr(args, "install", False):
        cfg = _config(args)
        agents_md = cfg.codex_path / "AGENTS.md" if getattr(args, "global_install", False) else Path.cwd() / "AGENTS.md"
        if agents_md.exists():
            existing = agents_md.read_text()
            if "Kindex (REQUIRED" in existing or "kindex MCP tools" in existing:
                print(f"Kindex directives already present in {agents_md}")
                return
            with open(agents_md, "a") as f:
                f.write("\n" + block)
            print(f"Appended kindex directives to {agents_md}")
        else:
            agents_md.parent.mkdir(parents=True, exist_ok=True)
            agents_md.write_text(block)
            print(f"Created {agents_md} with kindex directives")
    else:
        print(block)


def cmd_setup_gemini_mcp(args):
    """Install/uninstall Kindex as a Gemini CLI MCP server."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_gemini_mcp
        actions = uninstall_gemini_mcp(cfg, dry_run=dry_run)
    else:
        from .setup import install_gemini_mcp
        actions = install_gemini_mcp(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_gemini_md(args):
    """Output recommended GEMINI.md block for Gemini CLI/kindex integration.

    kin setup-gemini-md           — print to stdout
    kin setup-gemini-md --install — append to ~/.gemini/GEMINI.md if not present
    """
    block = _kindex_agents_md_block()

    if getattr(args, "install", False):
        cfg = _config(args)
        gemini_md = cfg.gemini_path / "GEMINI.md"
        if gemini_md.exists():
            existing = gemini_md.read_text()
            if "Kindex (REQUIRED" in existing or "kindex MCP tools" in existing:
                print(f"Kindex directives already present in {gemini_md}")
                return
            with open(gemini_md, "a") as f:
                f.write("\n" + block)
            print(f"Appended kindex directives to {gemini_md}")
        else:
            gemini_md.parent.mkdir(parents=True, exist_ok=True)
            gemini_md.write_text(block)
            print(f"Created {gemini_md} with kindex directives")
    else:
        print(block)


def cmd_setup_antigravity_mcp(args):
    """Install/uninstall Kindex as an Antigravity MCP server."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_antigravity_mcp
        actions = uninstall_antigravity_mcp(cfg, dry_run=dry_run)
    else:
        from .setup import install_antigravity_mcp
        actions = install_antigravity_mcp(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_antigravity_hooks(args):
    """Install/uninstall Kindex lifecycle hooks in Antigravity."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_antigravity_hooks
        actions = uninstall_antigravity_hooks(cfg, dry_run=dry_run)
    else:
        from .setup import install_antigravity_hooks
        actions = install_antigravity_hooks(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_antigravity_md(args):
    """Output recommended Antigravity/GEMINI.md Kindex directives."""
    cmd_setup_gemini_md(args)


def cmd_setup_opencode_mcp(args):
    """Install/uninstall Kindex as an OpenCode MCP server."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_opencode_mcp
        actions = uninstall_opencode_mcp(cfg, dry_run=dry_run)
    else:
        from .setup import install_opencode_mcp
        actions = install_opencode_mcp(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_opencode_hooks(args):
    """Install/uninstall the Kindex OpenCode plugin (session-start priming)."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_opencode_hooks
        actions = uninstall_opencode_hooks(cfg, dry_run=dry_run)
    else:
        from .setup import install_opencode_hooks
        actions = install_opencode_hooks(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_cursor_mcp(args):
    """Install/uninstall Kindex as a Cursor MCP server."""
    cfg = _config(args)
    dry_run = getattr(args, "dry_run", False)

    if getattr(args, "uninstall", False):
        from .setup import uninstall_cursor_mcp
        actions = uninstall_cursor_mcp(cfg, dry_run=dry_run)
    else:
        from .setup import install_cursor_mcp
        actions = install_cursor_mcp(cfg, dry_run=dry_run)

    for a in actions:
        print(f"  {a}")


def cmd_setup_cursor_rules(args):
    """Output recommended Cursor rule for kindex integration.

    kin setup-cursor-rules           — print to stdout
    kin setup-cursor-rules --install — write ~/.cursor/rules/kindex.mdc if not present
    """
    block = _kindex_cursor_rule_block()

    if getattr(args, "install", False):
        cfg = _config(args)
        rule_path = cfg.cursor_path / "rules" / "kindex.mdc"
        if rule_path.exists():
            print(f"Kindex Cursor rule already present at {rule_path}")
            return
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(block)
        print(f"Created {rule_path} with kindex directives")
    else:
        print(block)


def cmd_setup_merge(args):
    """Install/uninstall the .kin structured merge driver in the current git repo."""
    from .setup import git_repo_root, install_merge_driver, uninstall_merge_driver

    root = git_repo_root(getattr(args, "project_path", None) or os.getcwd())
    if root is None:
        print("Error: setup-merge must run inside a git repository", file=sys.stderr)
        sys.exit(1)
    dry_run = getattr(args, "dry_run", False)
    uninstall = getattr(args, "uninstall", False)
    actions = (uninstall_merge_driver if uninstall else install_merge_driver)(
        root, dry_run=dry_run
    )
    for a in actions:
        print(f"  {a}")
    if not uninstall and not dry_run:
        print(
            "\n.kin/index.json and .kin/code-map.json now merge via `kin merge-kin`.\n"
            "Commit the updated .gitattributes so collaborators inherit it; each\n"
            "clone runs `kin setup-merge` once to register the local driver."
        )


def _kindex_claude_md_block() -> str:
    """Generate the recommended CLAUDE.md block for kindex integration."""
    return """\
## Kindex (REQUIRED -- follow these in every session)

Kindex is a persistent knowledge graph. MCP tools (`search`, `add`, `context`, \
`show`, `link`, `list_nodes`, `status`, `ask`, `suggest`, `learn`, `graph_stats`, \
`changelog`, `ingest`, `tag_start`, `tag_update`, `tag_resume`, `remind_create`, \
`remind_exec`) are always available. Use them.

### Session lifecycle (do this every session)
1. **Start**: call `tag_start` with a name and focus for the current task, OR \
`tag_resume` if continuing previous work
2. **Policy**: if the repo has `.kin/config`, treat it as tracked project context. \
Run `kin policy check --event agent-start` when shell access is available.
3. **During**: follow the capture rules below -- this is the whole point of kindex
4. **Segment**: when switching topics, call `tag_update` with `action=segment`, \
summarizing what was done
5. **End**: call `tag_update` with `action=end` and a summary before the session closes

### Project `.kin/` contract
- `.kin/config` and `.kin/index.json` are repo-shipped project artifacts, not \
private cache.
- `.kin/index.json` and `.kin/code-map.json` are generated, id-keyed snapshots: \
never hand-resolve git conflicts in them. `kin index` auto-registers a structured \
merge driver (`kin merge-kin`) on first run that unions them losslessly; run \
`kin setup-merge` to (re)install it in a fresh clone.
- Local-only state belongs in `~/.kindex` or ignored `.kin/local`, `.kin/cache`, \
`.kin/tmp`, `.kin/private`.
- Linear enforcement is opt-in. Only enforce Linear when local `.kin/config` \
sets `work_policy.linear.enabled: true`.

### What to capture (use MCP `add` tool or `learn` for bulk text)
- **Discoveries**: new patterns, surprising findings, "aha" moments -- `add` as concept
- **Decisions**: architectural choices, trade-offs made, why X over Y -- `add` as decision
- **Key files**: when you discover what a file does or why it exists -- `add` as concept \
with the file path
- **Notable outputs**: test results, build errors, performance numbers, API responses \
worth remembering
- **New topics/keywords**: domain terms, project jargon, recurring themes -- `add` as concept
- **Questions**: open problems, things to investigate later -- `add` as question
- **Connections**: when two concepts relate -- `link` them with a reason

### What NOT to capture
- Trivial file reads, routine git operations, boilerplate
- Anything already in the graph -- always `search` before adding

### When to search
- **Before starting work**: `search` or `context` to see what is already known
- **Before adding**: `search` to avoid duplicates
- **When stuck**: `ask` the graph -- it may already have the answer

### Bulk capture
- After reading a long file, article, or output: use `learn` to extract and index \
multiple concepts at once
- After a complex multi-step task: use `learn` with a summary of what happened and why

### Reminders with actions
- Use `remind_create` with `action`, `instructions`, or `wake` for deferred tasks
- The daemon will execute shell commands or launch headless Claude/Codex/OpenCode
  when they come due
"""


def _kindex_agents_md_block() -> str:
    """Generate the recommended AGENTS.md block for Codex/kindex integration."""
    return """\
## Kindex (REQUIRED -- follow these in every session)

Kindex is a persistent knowledge graph. MCP tools (`search`, `add`, `context`, \
`show`, `link`, `list_nodes`, `status`, `ask`, `suggest`, `learn`, `graph_stats`, \
`changelog`, `ingest`, `tag_start`, `tag_update`, `tag_resume`, `task_add`, \
`task_done`, `task_list`, `remind_create`, `remind_exec`) are available through \
the `kindex` MCP server. Use them proactively.

### Startup environment
- At the start of each session, source `~/.profile` into the shell environment before running project commands when feasible.

### Session lifecycle
1. **Start**: call `tag_start` with a name and focus for the current task, OR \
`tag_resume` if continuing previous work.
2. **Orient**: call `search` or `context` before significant work to see what is \
already known.
3. **Policy**: if the repo has `.kin/config`, treat it as tracked project context. \
Run `kin policy check --event agent-start` when shell access is available.
4. **During**: capture important discoveries, decisions, tasks, and connections as \
they happen.
5. **Segment**: when switching topics, call `tag_update` with `action=segment` and \
a concise summary.
6. **End**: call `tag_update` with `action=end` and a summary before the session closes.

### Project `.kin/` contract
- `.kin/config` and `.kin/index.json` are repo-shipped project artifacts, not private cache.
- `.kin/index.json` and `.kin/code-map.json` are generated, id-keyed snapshots: never hand-resolve git conflicts in them. `kin index` auto-registers a structured merge driver (`kin merge-kin`) that unions them losslessly; run `kin setup-merge` to (re)install it in a fresh clone.
- Local-only state belongs in `~/.kindex` or ignored `.kin/local`, `.kin/cache`, `.kin/tmp`, `.kin/private`.
- Linear enforcement is opt-in. Only enforce Linear when local `.kin/config` sets `work_policy.linear.enabled: true`.
- If no work policy is present, continue normally and still use kindex for search/capture.

### What to capture
- **Discoveries**: new patterns, surprising findings, "aha" moments -- `add` as concept
- **Decisions**: architectural choices, trade-offs made, why X over Y -- `add` as decision
- **Key files**: what a file does or why it exists -- `add` as concept with the file path
- **Notable outputs**: test results, build errors, performance numbers, API responses worth remembering
- **Tasks**: actionable work items -- use `task_add` and link to related concepts when possible
- **Questions**: open problems and things to investigate later -- `add` as question
- **Connections**: when two concepts relate -- `link` them with a reason

### What not to capture
- Trivial file reads, routine git operations, boilerplate
- Anything already in the graph -- always `search` before adding

### Bulk capture
- After reading a long file, article, or output: use `learn` to extract and index multiple concepts
- After a complex multi-step task: use `learn` with a summary of what happened and why

### Working rule
Do not wait for the user to mention kindex. Treat it as your durable memory layer.
"""


def _kindex_cursor_rule_block() -> str:
    """Generate a Cursor rule (.mdc) for kindex integration. Always-applied scope."""
    body = _kindex_agents_md_block()
    return (
        "---\n"
        "description: Use kindex MCP tools as durable memory across sessions\n"
        "alwaysApply: true\n"
        "---\n\n"
        + body
    )


# ── config ────────────────────────────────────────────────────────────

def cmd_config(args):
    """Read or write config values.

    kin config show          — print full config
    kin config get <key>     — read a value (dot-separated: llm.enabled)
    kin config set <key> <value> — write a value to config file
    """
    action = args.config_action

    if action == "show":
        cfg = _config(args)
        print(yaml.dump(cfg.model_dump(), default_flow_style=False, sort_keys=False).strip())
        return

    if action == "get":
        if not args.key:
            print("Error: kin config get <key>", file=sys.stderr)
            sys.exit(1)
        cfg = _config(args)
        val = _dotget(cfg.model_dump(), args.key)
        if val is None:
            print(f"No value for '{args.key}'", file=sys.stderr)
            sys.exit(1)
        if isinstance(val, dict):
            print(yaml.dump(val, default_flow_style=False).strip())
        elif isinstance(val, list):
            for item in val:
                print(f"  - {item}")
        else:
            print(val)
        return

    if action == "set":
        if not args.key or args.value is None:
            print("Error: kin config set <key> <value>", file=sys.stderr)
            sys.exit(1)
        is_global = getattr(args, "global_", False)
        _config_write(args.key, args.value, getattr(args, "config", None),
                      global_=is_global,
                      project_path=getattr(args, "project_path", None))
        scope = "global" if is_global else "local"
        print(f"Set {args.key} = {args.value} ({scope})")
        return

    # Default: show
    cfg = _config(args)
    print(yaml.dump(cfg.model_dump(), default_flow_style=False, sort_keys=False).strip())


def cmd_agent_config(args):
    """Show or set per-client/per-instance agent behavior overrides."""
    from .agent_settings import (
        agent_config_write_key,
        agent_settings_summary,
        normalize_agent_client,
        resolve_agent_instance_key,
        validate_agent_setting_key,
    )

    action = getattr(args, "agent_config_action", "show")
    client = normalize_agent_client(getattr(args, "client", None))
    if not client or client == "plain":
        print("Error: agent-config requires --client <claude|codex|antigravity|...>",
              file=sys.stderr)
        sys.exit(1)
    instance_key = resolve_agent_instance_key(
        client,
        getattr(args, "instance", None),
        {},
    )

    if action == "show":
        cfg = _config(args)
        summary = agent_settings_summary(
            cfg,
            client=client,
            instance_key=instance_key,
        )
        if getattr(args, "json", False):
            print(_dumps(summary, indent=2))
        else:
            print(yaml.dump(summary, default_flow_style=False, sort_keys=False).strip())
        return

    if action == "set":
        key = getattr(args, "key", None)
        value = getattr(args, "value", None)
        if not key or value is None:
            print("Error: kin agent-config set <key> <value> --client <client>",
                  file=sys.stderr)
            sys.exit(1)
        try:
            setting_key = validate_agent_setting_key(key)
            scope = getattr(args, "scope", "client")
            write_key = agent_config_write_key(
                scope=scope,
                client=client,
                instance_key=instance_key,
                setting_key=setting_key,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        config_path = getattr(args, "config", None)
        is_global = getattr(args, "global_", False)
        project_path = getattr(args, "project_path", None)
        if getattr(args, "scope", "client") == "instance":
            _config_write(
                f"agents.instances.{instance_key}.client",
                client,
                config_path,
                global_=is_global,
                project_path=project_path,
            )
        _config_write(
            write_key,
            value,
            config_path,
            global_=is_global,
            project_path=project_path,
        )
        scope_label = getattr(args, "scope", "client")
        target = client if scope_label == "client" else instance_key
        file_scope = "global" if is_global else "local"
        print(f"Set {setting_key} = {value} ({scope_label}: {target}, {file_scope})")
        return

    print(f"Error: unknown agent-config action '{action}'", file=sys.stderr)
    sys.exit(1)


# ── policy ────────────────────────────────────────────────────────────

def cmd_policy(args):
    """Evaluate project work policy from tracked .kin config."""
    action = getattr(args, "policy_action", "show")
    cfg = _config(args)
    policy = cfg.work_policy

    if action == "show":
        print(yaml.dump(policy.model_dump(), default_flow_style=False, sort_keys=False).strip())
        return

    if action == "check":
        event = getattr(args, "event", None) or "manual"
        strict = getattr(args, "strict", False)
        failures: list[str] = []
        warnings: list[str] = []

        require_tag = policy.require_active_tag
        if event == "pre-commit" and policy.git.block_commit_without_tag:
            require_tag = True
        if event == "pre-push" and policy.git.block_push_without_tag:
            require_tag = True

        if require_tag:
            from .sessions import get_active_tag
            from .config import resolve_project_root
            store = _store(args)
            project_root = str(resolve_project_root(getattr(args, "project_path", None)))
            active = get_active_tag(store, project_path=project_root)
            store.close()
            if not active:
                failures.append(f"no active kindex session tag for project {project_root}")

        linear_required = policy.linear.enabled and policy.linear.require_issue
        if event == "pre-commit" and policy.git.block_commit_without_linear:
            linear_required = True
        if event == "pre-push" and policy.git.block_push_without_linear:
            linear_required = True

        if linear_required:
            identifier = os.environ.get("KIN_LINEAR_ID") or os.environ.get("LINEAR_ISSUE")
            if not identifier:
                failures.append("Linear issue required by project policy; set KIN_LINEAR_ID or LINEAR_ISSUE")
            if policy.linear.enabled and not os.environ.get("LINEAR_API_KEY"):
                warnings.append("LINEAR_API_KEY is not set; issue state cannot be verified")

        if not failures and not warnings:
            print(f"Policy check passed ({event}).")
            return

        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        for failure in failures:
            print(f"Policy violation: {failure}", file=sys.stderr)

        if failures and (strict or event in {"pre-commit", "pre-push"}):
            sys.exit(1)
        return


def _dotget(d: dict, key: str):
    """Get a value from a nested dict via dot-separated key."""
    parts = key.split(".")
    current = d
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _dotset(d: dict, key: str, value) -> None:
    """Set a value in a nested dict via dot-separated key."""
    parts = key.split(".")
    current = d
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _coerce_value(value: str):
    """Coerce a string value to the appropriate Python type."""
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    # List syntax: [a, b, c]
    if value.startswith("[") and value.endswith("]"):
        items = [s.strip().strip("'\"") for s in value[1:-1].split(",")]
        return [i for i in items if i]
    return value


def _config_write(key: str, value: str, config_path: str | None = None,
                   global_: bool = False,
                   project_path: str | None = None) -> None:
    """Write a config value to the appropriate config file.

    Resolution (like git config):
    - --config <path>:  explicit file
    - --global:         user-level (~/.config/kindex/kin.yaml)
    - default:          project .kin/config, discovered from --project-path,
                        KIN_PROJECT, git root, then cwd
    """
    import yaml

    if config_path:
        from .config import _resolve_path
        path = _resolve_path(config_path)
    elif global_:
        from .config import _effective_global_paths, _contained_resolve
        path = None
        for p in _effective_global_paths():
            p = _contained_resolve(p)
            if p is not None and p.exists():
                path = p
                break
        if path is None:
            path = _effective_global_paths()[0]
            contained = _contained_resolve(path)
            if contained is None:
                # Symlink escaped the root — don't write through it.
                # Fall back to a path inside the root that doesn't go
                # through the symlink.
                from .config import _bound_root as _br
                if _br is not None:
                    path = _br / "kin.yaml"
                else:
                    path = Path.home() / "kin.yaml"
            else:
                path = contained
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except (FileNotFoundError, OSError):
                # Dangling symlink or broken parent — fall back to
                # a path that doesn't go through the symlink.
                path = Path.home() / "kin.yaml"
    else:
        from .config import _maybe_upgrade_kin_file, _project_config_paths, resolve_project_root
        # Auto-upgrade old .kin file before searching local paths
        root = resolve_project_root(project_path)
        _maybe_upgrade_kin_file((root / ".kin").expanduser().resolve())
        path = None
        for p in _project_config_paths(root):
            if p.exists():
                path = p
                break
        if path is None:
            path = root / ".kin" / "config"
            path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing or start fresh
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    else:
        data = {}

    _dotset(data, key, _coerce_value(value))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    except (FileNotFoundError, OSError) as e:
        print(f"Error: cannot write config to {path}: {e}", file=sys.stderr)
        sys.exit(1)


# ── parser ─────────────────────────────────────────────────────────────

def _common(p):
    p.add_argument("--config", help="Explicit config file (bypasses layering)")
    p.add_argument("--data-dir", help="Override data directory")
    p.add_argument("--profile", help="Use a named kindex profile (overrides auto-resolution)")
    p.add_argument("--project-path", help="Project root/path for .kin config lookup")
    p.add_argument("--json", action="store_true", help="JSON output")


def build_parser() -> argparse.ArgumentParser:
    p = _ArgumentParser(prog="kin",
                                description="Knowledge graph that learns from your conversations")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command")

    # search
    s = sub.add_parser("search", help="Hybrid search (FTS + graph)")
    s.add_argument("query", nargs="+")
    s.add_argument("--top-k", type=int, default=10)
    s.add_argument("--tags", help="Filter by tags (comma-separated)")
    s.add_argument("--mine", action="store_true", help="Only my nodes")
    s.add_argument("--include-archived", action="store_true",
                   help="Include archived nodes (fenced from default search)")
    s.add_argument(
        "--trusted-only",
        action="store_true",
        help=(
            "Admission-control results to current explicitly verified knowledge; "
            "default search remains legacy-compatible recall"
        ),
    )
    _common(s)
    s.set_defaults(func=cmd_search)

    # context
    s = sub.add_parser("context", help="Context block for CLAUDE.md injection")
    s.add_argument("--topic", help="Topic (auto-detects from $PWD if omitted)")
    s.add_argument("--depth", type=int, default=10)
    s.add_argument("--level", choices=["full", "abridged", "summarized", "executive", "index"],
                   help="Context tier (auto-selects if omitted)")
    s.add_argument("--tokens", type=int, help="Available token budget (auto-selects tier)")
    s.add_argument("--format", choices=["claude", "raw", "json"], default="claude")
    s.add_argument(
        "--trusted-only",
        action="store_true",
        help=(
            "Admission-control context to current explicitly verified knowledge; "
            "default context remains recall"
        ),
    )
    _common(s)
    s.set_defaults(func=cmd_context)

    # quarantined automatic-capture review
    s = sub.add_parser(
        "candidate",
        help="List, inspect, review, prune, or erase quarantined captures",
    )
    s.add_argument(
        "candidate_action",
        choices=["list", "show", "accept", "reject", "prune", "erase"],
    )
    s.add_argument("candidate_id", nargs="?")
    s.add_argument(
        "--status",
        choices=["pending", "conflicted", "accepted", "rejected", "expired"],
        help="Candidate status filter (list)",
    )
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--review-token", help="Freshness token returned by candidate show")
    s.add_argument("--by", help="Asserted reviewer identifier")
    s.add_argument("--method", help="Asserted verification method")
    s.add_argument("--code", help="Bounded machine disposition code")
    s.add_argument("--valid-at", help="Validity start (timezone-aware RFC 3339)")
    s.add_argument("--invalid-at", help="Exclusive validity end (timezone-aware RFC 3339)")
    _common(s)
    s.set_defaults(func=cmd_candidate)

    s = sub.add_parser("verify", help="Assert verification and optional valid time for a node")
    s.add_argument("node", help="Node ID or exact title")
    s.add_argument("--by", required=True, help="Asserted reviewer identifier")
    s.add_argument("--method", required=True, help="Asserted verification method")
    s.add_argument("--verified-at", help="Verification time (timezone-aware RFC 3339)")
    s.add_argument("--valid-at", help="Validity start (timezone-aware RFC 3339)")
    s.add_argument("--invalid-at", help="Exclusive validity end (timezone-aware RFC 3339)")
    _common(s)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("invalidate", help="Set a node's exclusive valid-time end")
    s.add_argument("node", help="Node ID or exact title")
    s.add_argument("--by", required=True, help="Asserted actor identifier")
    s.add_argument("--code", required=True, help="Bounded machine disposition code")
    s.add_argument("--at", help="Invalidation time (timezone-aware RFC 3339)")
    _common(s)
    s.set_defaults(func=cmd_invalidate)

    # add
    s = sub.add_parser("add", help="Quick capture with auto-linking")
    s.add_argument("note", nargs="+")
    s.add_argument("--type", choices=["concept", "document", "decision",
                                       "question", "skill", "artifact", "person",
                                       "constraint", "directive", "checkpoint", "watch"])
    # Operational node metadata
    s.add_argument("--trigger", help="Trigger event (pre-commit, pre-deploy, etc.)")
    s.add_argument("--action", choices=["verify", "warn", "block"], help="Constraint action")
    s.add_argument("--scope", help="Directive scope (e.g. customer-communications)")
    s.add_argument("--owner", help="Person responsible (for watches/directives)")
    s.add_argument("--expires", help="Expiry date YYYY-MM-DD (for watches)")
    s.add_argument("--resets", help="Reset schedule (e.g. monday, monthly)")
    s.add_argument("--attention-trigger",
                   help="Comma-separated conversation trigger terms for attention injection")
    s.add_argument("--audience", choices=["private", "team", "org", "public"],
                   help="Audience scope")
    s.add_argument("--tags", help="Comma-separated tags for contextual surfacing")
    # R0 referent binding: bind the claim to the thing it describes.
    # Any binding flag implies direct node creation (no extraction rewrite —
    # a bound claim must stay the exact claim that was bound).
    s.add_argument("--referent",
                   help="Path or URL the claim describes (binds a content "
                        "digest; file paths are hashed now)")
    s.add_argument("--referent-digest",
                   help="Explicit content digest (required for url/repo "
                        "scope; sha256 hex, or 7-64 hex commit for repo)")
    s.add_argument("--referent-scope", choices=["file", "url", "repo"],
                   help="What the digest covers (default: url for URLs, "
                        "file otherwise)")
    s.add_argument("--asserted-at",
                   help="RFC3339 claim time (default: now when binding)")
    s.add_argument("--true-of",
                   help="RFC3339 instant the referent was observed in the "
                        "digested state (default: asserted-at)")
    _common(s)
    s.set_defaults(func=cmd_add)

    # stale — R0 referent staleness sweep
    s = sub.add_parser("stale",
                       help="Re-hash referent-bound nodes; demote stale ones")
    s.add_argument("--base-dir",
                   help="Resolve relative referent paths against this dir "
                        "(default: cwd)")
    s.add_argument("--rebind",
                   metavar="NODE_ID",
                   help="Re-hash one node's referent and rebind to the "
                        "current state (clears its stale marker)")
    _common(s)
    s.set_defaults(func=cmd_stale)

    # learn
    s = sub.add_parser("learn", help="Extract knowledge from sessions/inbox")
    s.add_argument("--from-inbox", action="store_true", help="Process inbox items")
    s.add_argument("session_id", nargs="?", help="Session ID to learn from")
    _common(s)
    s.set_defaults(func=cmd_learn)

    # link
    s = sub.add_parser("link", help="Create edge between nodes")
    s.add_argument("node_a")
    s.add_argument("node_b")
    s.add_argument("relationship", nargs="?", default="relates_to")
    s.add_argument("--why", help="Reason for link")
    s.add_argument("--weight", type=float, default=0.5)
    _common(s)
    s.set_defaults(func=cmd_link)

    # show
    s = sub.add_parser("show", help="Show node details")
    s.add_argument("node_id")
    _common(s)
    s.set_defaults(func=cmd_show)

    # list
    s = sub.add_parser("list", help="List nodes")
    s.add_argument("--type")
    s.add_argument("--status")
    s.add_argument("--tags", help="Filter by tags (comma-separated)")
    s.add_argument("--audience", choices=["private", "team", "org", "public"],
                   help="Filter by audience scope")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--mine", action="store_true", help="Only my nodes")
    _common(s)
    s.set_defaults(func=cmd_list)

    # recent
    s = sub.add_parser("recent", help="Recently active nodes")
    s.add_argument("--n", type=int, default=20)
    _common(s)
    s.set_defaults(func=cmd_recent)

    # orphans
    s = sub.add_parser("orphans", help="Semantic nodes with no semantic edges")
    _common(s)
    s.set_defaults(func=cmd_orphans)

    # status
    s = sub.add_parser("status", help="Graph health & stats")
    s.add_argument("--type", help="Filter by node type (constraint, watch, etc.)")
    s.add_argument("--trigger", help="Filter operational nodes by trigger event")
    s.add_argument("--owner", help="Filter by owner")
    s.add_argument("--mine", action="store_true", help="Filter by current user")
    _common(s)
    s.set_defaults(func=cmd_status)

    # budget
    s = sub.add_parser("budget", help="LLM budget usage")
    s.add_argument("--conversation-id", help="Show spend for one conversation")
    _common(s)
    s.set_defaults(func=cmd_budget)

    # init
    s = sub.add_parser("init", help="Initialize Kindex data directory")
    _common(s)
    s.set_defaults(func=cmd_init)

    # migrate
    s = sub.add_parser("migrate", help="Import markdown topics into SQLite")
    _common(s)
    s.set_defaults(func=cmd_migrate)

    # doctor
    s = sub.add_parser("doctor", help="Health check")
    s.add_argument("--fix", action="store_true")
    _common(s)
    s.set_defaults(func=cmd_doctor)

    # set-audience
    s = sub.add_parser("set-audience", help="Set node audience (private/team/org/public)")
    s.add_argument("node_id")
    s.add_argument("audience", choices=["private", "team", "org", "public"])
    _common(s)
    s.set_defaults(func=cmd_set_audience)

    # set-state
    s = sub.add_parser("set-state", help="Set mutable state on a directive/operational node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("key", help="State key to set")
    s.add_argument("value", help="Value to set")
    _common(s)
    s.set_defaults(func=cmd_set_state)

    # edit
    s = sub.add_parser("edit", help="Policy-aware in-place edit of a node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("--title", help="Replace the title")
    s.add_argument("--content", help="Replace the content")
    s.add_argument("--append", help="Append a dated addendum to the content")
    s.add_argument("--add-tags", dest="add_tags", help="Comma-separated tags to add")
    s.add_argument("--remove-tags", dest="remove_tags", help="Comma-separated tags to remove")
    s.add_argument("--intent", help="Replace the intent")
    s.add_argument("--expires", help="Set expiry date (YYYY-MM-DD)")
    s.add_argument("--force", action="store_true", help="Override a foreign lock")
    _common(s)
    s.set_defaults(func=cmd_edit)

    # supersede
    s = sub.add_parser("supersede", help="Replace a node with a new one, preserving history")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("text", nargs="+", help="Replacement text")
    s.add_argument("--expires", help="Expiry date for the new node (YYYY-MM-DD)")
    s.add_argument("--reason", help="Why the node is being replaced")
    _common(s)
    s.set_defaults(func=cmd_supersede)

    # export
    s = sub.add_parser("export", help="Export graph (audience-aware)")
    s.add_argument("export_kind", nargs="?", choices=["graph", "code-map"], default="graph",
                   help="Export graph (default) or a UA-compatible code map")
    s.add_argument("--audience", choices=["private", "team", "org", "public"], default="team")
    s.add_argument("--format", choices=["json", "jsonl", "understand-anything"], default="json")
    s.add_argument("--directory", help="Repository root for code-map metadata")
    s.add_argument("--project-name", help="Project name for code-map export")
    s.add_argument("--output", help="Write export to this file instead of stdout")
    s.add_argument("--limit", type=int, default=10000,
                   help="Maximum nodes to scan for export (default 10000)")
    _common(s)
    s.set_defaults(func=cmd_export)

    # ingest
    s = sub.add_parser("ingest", help="Ingest from external sources")
    # Dynamic adapter discovery for choices
    try:
        from .adapters.registry import discover as _discover_adapters
        _adapter_names = sorted(_discover_adapters().keys())
    except Exception:
        _adapter_names = ["projects", "sessions", "files", "commits", "github", "linear"]
    s.add_argument("source", choices=_adapter_names + ["all"],
                   help="Adapter name or 'all' for all available sources")
    s.add_argument("--limit", type=int, default=None,
                   help="Max items to ingest (0 = unlimited; default depends on "
                        "adapter — code is unlimited, network/LLM adapters cap at 50)")
    s.add_argument("--repo", type=str, default=None, help="GitHub owner/repo (e.g. jmcentire/kindex)")
    s.add_argument("--repo-path", type=str, default=None,
                   help="Local repository path (for commits source)")
    s.add_argument("--since", type=str, default=None, help="ISO date to filter items created after")
    s.add_argument("--team", type=str, default=None, help="Linear team key (for linear source)")
    s.add_argument("--directory", type=str, default=None, help="Directory to ingest (for files source)")
    # default=None so an absent flag defers to the project's .kin/config
    # code_ingest.unity setting
    s.add_argument("--unity", action="store_true", default=None,
                   help="Include Unity asset files (.unity/.prefab/.asset) and "
                        ".meta GUIDs (code source; also via .kin/config code_ingest.unity)")
    _common(s)
    s.set_defaults(func=cmd_ingest)

    # git-hook
    s = sub.add_parser("git-hook", help="Install/uninstall Kindex git hooks in a repository")
    s.add_argument("hook_action", choices=["install", "uninstall"],
                   help="Action: install or uninstall git hooks")
    s.add_argument("--repo-path", type=str, default=".",
                   help="Path to git repository (default: current directory)")
    _common(s)
    s.set_defaults(func=cmd_git_hook)

    # trail
    s = sub.add_parser("trail", help="Temporal history of a node")
    s.add_argument("node_id")
    _common(s)
    s.set_defaults(func=cmd_trail)

    # decay
    s = sub.add_parser("decay", help="Run weight decay on nodes/edges")
    s.add_argument("--node-half-life", type=int, default=90, help="Node half-life in days")
    s.add_argument("--edge-half-life", type=int, default=30, help="Edge half-life in days")
    _common(s)
    s.set_defaults(func=cmd_decay)

    # compact-hook
    s = sub.add_parser("compact-hook", help="Pre-compact hook for context capture")
    s.add_argument("--text", help="Text to extract from")
    s.add_argument("--emit-context", action="store_true",
                   help="Always emit executive context summary")
    _common(s)
    s.set_defaults(func=cmd_compact_hook)

    # prime
    s = sub.add_parser("prime", help="Generate context for SessionStart hook")
    s.add_argument("--topic", help="Topic to prime (auto-detects from $PWD if omitted)")
    s.add_argument("--tokens", type=int, default=750, help="Max token budget (default 750)")
    s.add_argument("--for", dest="output_for", choices=["hook", "stdout"], default="stdout",
                   help="Output mode: hook (raw block) or stdout (with header)")
    s.add_argument("--codebook", action="store_true",
                   help="Regenerate the LLM prompt cache codebook")
    s.add_argument("--conversation-id", help="Conversation/session id for scoped reminders")
    s.add_argument("--adapter", default="claude",
                   choices=["plain", "claude", "codex", "antigravity", "opencode"],
                   help="Hook output adapter for client hook protocols")
    s.add_argument("--agent-instance", help="Agent instance/conversation override key")
    _common(s)
    s.set_defaults(func=cmd_prime)

    # suggest
    s = sub.add_parser("suggest", help="Review bridge opportunity suggestions")
    s.add_argument("--accept", type=int, metavar="ID", help="Accept suggestion by ID")
    s.add_argument("--reject", type=int, metavar="ID", help="Reject suggestion by ID")
    s.add_argument("--limit", type=int, default=20, help="Max suggestions to show")
    _common(s)
    s.set_defaults(func=cmd_suggest)

    # log
    s = sub.add_parser("log", help="Show recent activity")
    s.add_argument("--n", type=int, default=50, help="Number of entries")
    _common(s)
    s.set_defaults(func=cmd_log)

    # changelog
    s = sub.add_parser("changelog", help="Show what changed in the graph")
    s.add_argument("--since", help="ISO date/timestamp (e.g. 2026-02-20)")
    s.add_argument("--days", type=int, help="Look back N days (default 7)")
    s.add_argument("--actor", help="Filter by actor")
    _common(s)
    s.set_defaults(func=cmd_changelog)

    # graph
    s = sub.add_parser("graph", help="Graph analytics dashboard")
    s.add_argument("graph_mode", nargs="?", default="stats",
                   choices=["stats", "centrality", "communities", "bridges", "trailheads"])
    s.add_argument("--method", choices=["betweenness", "degree", "closeness"],
                   help="Centrality method")
    s.add_argument("--top-k", type=int, default=20, help="Number of results")
    _common(s)
    s.set_defaults(func=cmd_graph)

    # alias
    s = sub.add_parser("alias", help="Manage AKA/synonyms for a node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("alias_action", choices=["add", "remove", "list"])
    s.add_argument("alias_value", nargs="?", help="Alias to add/remove")
    _common(s)
    s.set_defaults(func=cmd_alias)

    # whoami
    s = sub.add_parser("whoami", help="Show current user and agent identity")
    _common(s)
    s.set_defaults(func=cmd_whoami)

    # profile
    s = sub.add_parser("profile", help="Named graph profiles (list, which, create)")
    s.add_argument("profile_action", nargs="?", default="list",
                   choices=["list", "which", "create"])
    s.add_argument("name", nargs="?", help="Profile name (for create)")
    s.add_argument("--roots", help="Comma-separated roots routed to this profile (for create)")
    s.add_argument("--default", dest="set_default", action="store_true",
                   help="Set as default_profile (for create)")
    _common(s)
    s.set_defaults(func=cmd_profile)

    # embed
    s = sub.add_parser("embed", help="Index and maintain vector search")
    s.add_argument("--verbose", "-v", action="store_true")
    embed_sub = s.add_subparsers(dest="embed_action")

    def _embed_filters(parser):
        parser.add_argument("--tags", help="Comma-separated tags/domains to target")
        parser.add_argument("--node-type", help="Target one node type")
        parser.add_argument("--status", help="Target one node status")
        parser.add_argument("--since", help="Only nodes updated since this ISO timestamp")
        parser.add_argument("--target", help="Target nodes under a project path")
        parser.add_argument("--kin", help="Target a project .kin directory or .kin/config")
        parser.add_argument("--stale", action="store_true",
                            help="Only nodes missing current embedding metadata")
        parser.add_argument("--limit", type=int, help="Maximum nodes to target")

    for name, help_text in (
        ("plan", "Estimate selected reindex work"),
        ("enqueue", "Queue selected nodes for gradual embedding maintenance"),
        ("reindex", "Run selected reindex work now, or enqueue with --enqueue"),
    ):
        es = embed_sub.add_parser(name, help=help_text)
        _embed_filters(es)
        if name in {"enqueue", "reindex"}:
            es.add_argument("--max-queue", type=int, help="Maximum retained queue size")
        if name == "reindex":
            es.add_argument("--enqueue", action="store_true",
                            help="Queue work instead of running synchronously")
            es.add_argument("--verbose", "-v", action="store_true")
        _common(es)
        es.set_defaults(func=cmd_embed)

    es = embed_sub.add_parser("drain", help="Drain queued embedding work")
    es.add_argument("--max-jobs", type=int, help="Maximum queued nodes to embed")
    es.add_argument("--time-budget", type=float, dest="time_budget",
                    help="Wall-clock seconds cap (0 = unlimited; "
                         "default: embedding.drain_time_budget)")
    _common(es)
    es.set_defaults(func=cmd_embed)

    es = embed_sub.add_parser("status", help="Show embedding index status")
    _common(es)
    es.set_defaults(func=cmd_embed)

    es = embed_sub.add_parser(
        "calibrate",
        help="Measure the null-query similarity floor for the active model")
    es.add_argument("--percentile", type=float,
                    help="Percentile of the null-query distribution to use as "
                         "the floor (default: grounding.floor_percentile)")
    es.add_argument("--show", action="store_true",
                    help="Show the current calibration record without recalibrating")
    _common(es)
    es.set_defaults(func=cmd_embed)

    _common(s)
    s.set_defaults(func=cmd_embed)

    # extract
    s = sub.add_parser("extract", help="Extraction engine tools")
    extract_sub = s.add_subparsers(dest="extract_action")

    es = extract_sub.add_parser(
        "eval", help="Score extraction engines against the local corpus")
    es.add_argument("--engines", default="keyword,deterministic",
                    help="Comma-separated engines to score "
                         "(keyword, llm, deterministic)")
    es.add_argument("--limit", type=int, default=200,
                    help="Sample size of curated nodes")
    _common(es)
    es.set_defaults(func=cmd_extract)

    es = extract_sub.add_parser("engines", help="List extraction engines")
    _common(es)
    es.set_defaults(func=cmd_extract)

    _common(s)
    s.set_defaults(func=cmd_extract)

    # ask
    s = sub.add_parser("ask", help="Query the knowledge graph")
    s.add_argument("question", nargs="+")
    _common(s)
    s.set_defaults(func=cmd_ask)

    # register
    s = sub.add_parser("register", help="Associate a file path with a node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("filepath", help="File path to register")
    _common(s)
    s.set_defaults(func=cmd_register)

    # setup-hooks
    s = sub.add_parser("setup-hooks", help="Install Kindex hooks into Claude Code")
    s.add_argument("--mode", choices=["legacy", "modern"], default="legacy",
                   help="Select one adapter; modern is qualified early-access function hooks")
    s.add_argument("--retire-command", action="append", help="Explicitly retire this exact inspected legacy wrapper command")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed hooks")
    _common(s)
    s.set_defaults(func=cmd_setup_hooks)

    s = sub.add_parser("hook-rpc", help="Internal versioned JSON adapter boundary")
    s.set_defaults(func=cmd_hook_rpc)

    s = sub.add_parser("integration-doctor", help="Inspect adapter ownership and qualification")
    _common(s)
    s.set_defaults(func=cmd_integration_doctor)

    reconcile_parser = sub.add_parser("integration-reconcile", help="Retry pending audit outcomes across this worktree's sessions; never repeat task effects")
    reconcile_parser.add_argument("--project-path", help="Explicit Git worktree (defaults to cwd)")
    reconcile_parser.add_argument("--max-attempts", type=int, default=16, help="At most 1-16 deliveries within a 20 second budget")
    reconcile_parser.set_defaults(func=cmd_integration_reconcile)

    s = sub.add_parser("repo-memory", help="Publish/import selected shareable evidence in .kin/knowledge.json")
    rs = s.add_subparsers(dest="repo_memory_action", required=True)
    for action in ("publish", "import"):
        repo_parser = rs.add_parser(action)
        repo_parser.add_argument("--project-path", help="Explicit Git worktree")
        if action == "publish":
            repo_parser.add_argument("node_ids", nargs="+", help="Explicit public/team node IDs selected for sharing")
        repo_parser.set_defaults(func=cmd_repo_memory)

    # setup-codex-hooks
    s = sub.add_parser("setup-codex-hooks", help="Install Kindex prompt hooks into Codex")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed hooks")
    _common(s)
    s.set_defaults(func=cmd_setup_codex_hooks)

    # setup-codex-mcp
    s = sub.add_parser("setup-codex-mcp", help="Install Kindex MCP server into Codex")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed MCP server")
    _common(s)
    s.set_defaults(func=cmd_setup_codex_mcp)

    # setup-cron
    s = sub.add_parser("setup-cron", help="Install periodic cron job for kin maintenance")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove cron entry")
    s.add_argument("--method", choices=["launchd", "crontab"],
                   help="Scheduling method (auto-detects: launchd on macOS, crontab on Linux)")
    _common(s)
    s.set_defaults(func=cmd_setup_cron)

    # setup-claude-md
    s = sub.add_parser("setup-claude-md",
                       help="Output recommended CLAUDE.md kindex directives")
    s.add_argument("--install", action="store_true",
                   help="Append to ~/.claude/CLAUDE.md (if not already present)")
    _common(s)
    s.set_defaults(func=cmd_setup_claude_md)

    # setup-agents-md
    s = sub.add_parser("setup-agents-md",
                       help="Output recommended AGENTS.md kindex directives")
    s.add_argument("--install", action="store_true",
                   help="Append to ./AGENTS.md (if not already present)")
    s.add_argument("--global", dest="global_install", action="store_true",
                   help="With --install, append to ~/.codex/AGENTS.md instead")
    _common(s)
    s.set_defaults(func=cmd_setup_agents_md)

    # setup-gemini-mcp
    s = sub.add_parser("setup-gemini-mcp", help="Install Kindex MCP server into Gemini CLI")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed MCP server")
    _common(s)
    s.set_defaults(func=cmd_setup_gemini_mcp)

    # setup-gemini-md
    s = sub.add_parser("setup-gemini-md",
                       help="Output recommended GEMINI.md kindex directives")
    s.add_argument("--install", action="store_true",
                   help="Append to ~/.gemini/GEMINI.md (if not already present)")
    _common(s)
    s.set_defaults(func=cmd_setup_gemini_md)

    # setup-antigravity-mcp
    s = sub.add_parser("setup-antigravity-mcp",
                       help="Install Kindex MCP server into Google Antigravity")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed MCP server")
    _common(s)
    s.set_defaults(func=cmd_setup_antigravity_mcp)

    # setup-antigravity-hooks
    s = sub.add_parser("setup-antigravity-hooks",
                       help="Install Kindex lifecycle hooks into Google Antigravity")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed hooks")
    _common(s)
    s.set_defaults(func=cmd_setup_antigravity_hooks)

    # setup-antigravity-md
    s = sub.add_parser("setup-antigravity-md",
                       help="Output recommended Antigravity/GEMINI.md kindex directives")
    s.add_argument("--install", action="store_true",
                   help="Append to ~/.gemini/GEMINI.md (if not already present)")
    _common(s)
    s.set_defaults(func=cmd_setup_antigravity_md)

    # setup-opencode-mcp
    s = sub.add_parser("setup-opencode-mcp", help="Install Kindex MCP server into OpenCode")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed MCP server")
    _common(s)
    s.set_defaults(func=cmd_setup_opencode_mcp)

    # setup-opencode-hooks
    s = sub.add_parser("setup-opencode-hooks",
                       help="Install the Kindex OpenCode plugin (session-start priming)")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove the installed plugin")
    _common(s)
    s.set_defaults(func=cmd_setup_opencode_hooks)

    # setup-cursor-mcp
    s = sub.add_parser("setup-cursor-mcp", help="Install Kindex MCP server into Cursor")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove installed MCP server")
    _common(s)
    s.set_defaults(func=cmd_setup_cursor_mcp)

    # setup-cursor-rules
    s = sub.add_parser("setup-cursor-rules",
                       help="Output recommended Cursor rule (.mdc) for kindex")
    s.add_argument("--install", action="store_true",
                   help="Write ~/.cursor/rules/kindex.mdc (if not already present)")
    _common(s)
    s.set_defaults(func=cmd_setup_cursor_rules)

    # config
    s = sub.add_parser("config", help="View or edit configuration")
    s.add_argument("config_action", nargs="?", default="show",
                   choices=["show", "get", "set"],
                   help="Action: show, get <key>, set <key> <value>")
    s.add_argument("key", nargs="?", help="Config key (dot-separated: llm.enabled)")
    s.add_argument("value", nargs="?", help="Value to set")
    s.add_argument("--global", dest="global_", action="store_true",
                   help="Write to global config (~/.config/kindex/kin.yaml)")
    _common(s)
    s.set_defaults(func=cmd_config)

    # agent-config
    s = sub.add_parser("agent-config",
                       help="Show/set per-agent Kindex behavior overrides")
    s.add_argument("agent_config_action", nargs="?", default="show",
                   choices=["show", "set"],
                   help="Action: show or set")
    s.add_argument("key", nargs="?", help="Setting key for set")
    s.add_argument("value", nargs="?", help="Setting value for set")
    s.add_argument("--client", help="Client family: claude, codex, antigravity, etc.")
    s.add_argument("--instance", help="Instance/conversation key for instance scope")
    s.add_argument("--scope", choices=["client", "instance"], default="client",
                   help="Write scope for set (default: client)")
    s.add_argument("--global", dest="global_", action="store_true",
                   help="Write to global config (~/.config/kindex/kin.yaml)")
    _common(s)
    s.set_defaults(func=cmd_agent_config)

    # attention
    s = sub.add_parser("attention", help="Conversation-attention runtime controls")
    s.add_argument("attention_action", nargs="?", default="status",
                   choices=["status", "on", "off", "inherit", "check", "drain", "budget", "estimate", "reinforce"],
                   help="Action: status, on, off, inherit, check, drain, budget, estimate, reinforce")
    s.add_argument("--conversation-id", help="Conversation/session id for per-conversation state")
    s.add_argument("--text", help="Conversation snippet for manual check")
    s.add_argument("--force", action="store_true", help="Run check regardless of tick interval")
    s.add_argument("--enqueue", action="store_true",
                   help="Silently queue this session for later reinforcement (Stop/PreCompact hook)")
    s.add_argument("--messages", type=int, default=100,
                   help="Message-window size for attention estimate")
    _common(s)
    s.set_defaults(func=cmd_attention)

    # sim — optional Jeremy-simulacrum supervisory check-in
    s = sub.add_parser("sim", help="Sim supervisory check-in runtime controls")
    s.add_argument("sim_action", nargs="?", default="status",
                   choices=["status", "on", "off", "enable", "disable", "inherit",
                            "check", "drain", "guidance"],
                   help="Action: status, on/off (kill switch), inherit, check, drain, guidance")
    s.add_argument("--text", help="Window for `check`, or guidance text for `guidance`")
    s.add_argument("--clear", action="store_true", help="With `guidance`: clear it")
    _common(s)
    s.set_defaults(func=cmd_sim)

    # policy
    s = sub.add_parser("policy", help="Show/check project work policy from .kin config")
    s.add_argument("policy_action", nargs="?", choices=["show", "check"], default="show",
                   help="Action: show or check")
    s.add_argument("--event", choices=["manual", "agent-start", "pre-commit", "pre-push", "pre-deploy"],
                   default="manual", help="Event being checked")
    s.add_argument("--strict", action="store_true", help="Exit non-zero on policy violations")
    _common(s)
    s.set_defaults(func=cmd_policy)

    # skills
    s = sub.add_parser("skills", help="Show skill profile for a person")
    s.add_argument("person", nargs="?", help="Person name/ID (default: current user)")
    _common(s)
    s.set_defaults(func=cmd_skills)

    # import (named import-graph to avoid Python keyword)
    s = sub.add_parser("import", help="Import nodes/edges from JSON/JSONL")
    s.add_argument("filepath", help="Path to JSON or JSONL file")
    s.add_argument("--mode", choices=["merge", "replace"], default="merge",
                   help="Merge (default) or replace existing nodes")
    s.add_argument("--format", choices=["json", "jsonl"],
                   help="Force format (auto-detects from extension)")
    s.add_argument("--dry-run", action="store_true",
                   help="Show what would be imported without making changes")
    _common(s)
    s.set_defaults(func=cmd_import_graph)

    # analytics
    s = sub.add_parser("analytics", help="Archive session analytics and activity heatmap")
    s.add_argument("--sessions", action="store_true", help="Show session stats")
    s.add_argument("--heatmap", action="store_true", help="Show activity heatmap")
    s.add_argument("--days", type=int, default=90, help="Lookback days for heatmap (default 90)")
    _common(s)
    s.set_defaults(func=cmd_analytics)

    # index
    s = sub.add_parser("index", help="Write .kin/index.json for git tracking")
    s.add_argument("--output-dir", type=str, help="Output directory (default: current dir)")
    s.add_argument("--no-merge-driver", action="store_true",
                   help="Skip auto-registering the .kin structured merge driver")
    _common(s)
    s.set_defaults(func=cmd_index)

    # merge-kin (git merge driver for .kin artifacts)
    s = sub.add_parser(
        "merge-kin",
        help="Git merge driver for .kin artifacts (structured union; called by git)",
    )
    s.add_argument("base", help="Ancestor/base version (git %%O)")
    s.add_argument("ours", help="Current/ours version + output target (git %%A)")
    s.add_argument("theirs", help="Other/theirs version (git %%B)")
    s.add_argument("path", help="In-repo pathname (git %%P)")
    s.set_defaults(func=cmd_merge_kin)

    # setup-merge (install the merge driver into the current repo)
    s = sub.add_parser(
        "setup-merge",
        help="Install the .kin structured merge driver into the current git repo",
    )
    s.add_argument("--project-path", help="Repo path (default: current directory)")
    s.add_argument("--dry-run", action="store_true", help="Show what would be done")
    s.add_argument("--uninstall", action="store_true", help="Remove the merge driver")
    s.set_defaults(func=cmd_setup_merge)

    # sync-links
    s = sub.add_parser("sync-links", help="Update node content with connection references")
    _common(s)
    s.set_defaults(func=cmd_sync_links)

    # cron
    s = sub.add_parser("cron", help="One-shot maintenance cycle (for crontab)")
    s.add_argument("--verbose", "-v", action="store_true", help="Detailed logging")
    _common(s)
    s.set_defaults(func=cmd_cron)

    # dream
    s = sub.add_parser("dream", help="Knowledge consolidation (dream cycle)")
    s.add_argument("--verbose", "-v", action="store_true", help="Detailed logging")
    s.add_argument("--dry-run", action="store_true", help="Report without making changes")
    s.add_argument("--lightweight", action="store_true",
                   help="Fast path: dedup + suggestion auto-apply only")
    s.add_argument("--deep", action="store_true",
                   help="Include LLM-powered cluster consolidation")
    s.add_argument("--detach", action="store_true",
                   help="Fork detached subprocess and return immediately")
    s.add_argument("--force", action="store_true",
                   help="With --detach, bypass the scheduled dream throttle")
    _common(s)
    s.set_defaults(func=cmd_dream)

    # archive (slow graph)
    s = sub.add_parser("archive", help="Manage slow graph archives")
    s.add_argument("archive_action", nargs="?", default="list",
                   choices=["list", "search", "restore", "run"],
                   help="Action (default: list)")
    s.add_argument("query", nargs="?", help="Search query or node ID (for search/restore)")
    s.add_argument("--node-id", help="Node ID to restore")
    _common(s)
    s.set_defaults(func=cmd_archive)

    # watch
    s = sub.add_parser("watch", help="Watch for new sessions and ingest them")
    s.add_argument("--interval", type=int, default=60,
                   help="Check interval in seconds (default: 60)")
    s.add_argument("--verbose", "-v", action="store_true", help="Detailed logging")
    _common(s)
    s.set_defaults(func=cmd_watch)

    # tag (session tags)
    s = sub.add_parser("tag", help="Session tag management (start, update, resume, etc.)")
    s.add_argument("tag_action",
                   choices=["start", "update", "segment", "pause", "end",
                            "resume", "list", "show"],
                   help="Tag action")
    s.add_argument("tag_name", nargs="?", help="Tag name (auto-detects active for update/pause/end)")
    s.add_argument("--focus", help="Current focus / new segment focus")
    s.add_argument("--description", help="Session description")
    s.add_argument("--summary", help="Summary (for segment/pause/end)")
    s.add_argument("--remaining", help="Comma-separated remaining items")
    s.add_argument("--add-remaining", help="Add items to remaining list (comma-separated)")
    s.add_argument("--done", help="Mark items as done / remove from remaining (comma-separated)")
    s.add_argument("--status", help="Filter by status (for list: active/paused/completed)")
    s.add_argument("--project", action="store_true", help="Filter by current project (for list)")
    s.add_argument(
        "--tokens",
        type=int,
        default=1500,
        help=(
            "Resume budget in exact UTF-8 bytes (legacy flag name; "
            "library callers may supply an exact provider counter)"
        ),
    )
    _common(s)
    s.set_defaults(func=cmd_tag)

    # task
    s = sub.add_parser("task", help="Graph-connected task management")
    s.add_argument("task_action", nargs="?", default="list",
                   choices=["add", "list", "show", "claim", "release", "cleanup",
                            "done", "cancel", "update", "nearby"])
    s.add_argument("title_words", nargs="*", help="Task title (for add)")
    s.add_argument("--priority", type=int, choices=[1, 2, 3, 4, 5], default=None,
                   help="Priority: 1=urgent 2=high 3=normal 4=low 5=someday")
    s.add_argument("--due", help="Due date: 'tomorrow', '2026-03-15', 'in 3 days'")
    s.add_argument("--scope", choices=["global", "contextual"], default=None)
    s.add_argument("--link", dest="link_to", help="Link to nodes (ID or title, comma-separated)")
    s.add_argument("--task-id", help="Task ID (for show/done/cancel/update)")
    s.add_argument("--status", help="Filter: open, in_progress, done, all")
    s.add_argument("--effort", choices=["small", "medium", "large"])
    s.add_argument("--domain", help="Filter by domain")
    s.add_argument("--limit", type=int, default=20, help="Maximum matching tasks")
    s.add_argument("--session-id", help="Host session ID for contextual reminders")
    s.add_argument("--content", help="Task description (add/update)")
    s.add_argument("--expected-version", type=int, help="Refuse update if the task version changed")
    s.add_argument("--agent", help="Agent name for claim/release")
    s.add_argument("--ttl", type=int, default=120, help="Claim TTL in minutes")
    s.add_argument("--note", help="Claim note")
    s.add_argument("--force", action="store_true", help="Force claim/release takeover")
    _common(s)
    s.set_defaults(func=cmd_task)

    # coord
    s = sub.add_parser("coord", help="Short-lived agent coordination conversations")
    s.add_argument("coord_action", nargs="?", default="list",
                   choices=["start", "post", "read", "list", "end", "cleanup",
                            "join", "attach", "inject"])
    s.add_argument("name", nargs="?", help="Conversation name or id")
    s.add_argument("message_words", nargs="*",
                   help="Message body (post), node id (attach), or "
                        "set/clear/list + text (inject)")
    s.add_argument("--agent", help="Agent name (default: resolved agent id)")
    s.add_argument("--task-id", help="Related task id")
    s.add_argument("--ttl", type=int, default=240, help="Conversation TTL in minutes")
    s.add_argument("--since-id", type=int, default=0, help="Read messages after id")
    s.add_argument("--limit", type=int, default=50, help="Read/list limit")
    s.add_argument("--status", default="active", help="Filter: active, ended, all")
    s.add_argument("--project", action="store_true", help="Filter by current project")
    s.add_argument("--summary", help="End summary retained after clearing messages")
    s.add_argument("--to", help="Target agent (post / inject set)")
    s.add_argument("--id", type=int, help="Inject message id (inject clear)")
    _common(s)
    s.set_defaults(func=cmd_coord)

    # lock / unlock
    s = sub.add_parser("lock", help="Acquire an advisory lock on a node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("--ttl", type=int, default=60, help="Lock TTL in minutes")
    s.add_argument("--note", default="", help="Why the node is locked")
    s.add_argument("--agent", help="Agent name (default: resolved agent id)")
    s.add_argument("--force", action="store_true", help="Take over a foreign lock")
    _common(s)
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser("unlock", help="Release an advisory lock on a node")
    s.add_argument("node_id", help="Node ID or title")
    s.add_argument("--agent", help="Agent name (default: resolved agent id)")
    s.add_argument("--force", action="store_true", help="Clear a foreign lock")
    _common(s)
    s.set_defaults(func=cmd_unlock)

    # remind
    s = sub.add_parser("remind", help="Reminder management (create, list, snooze, done, cancel, check)")
    s.add_argument("remind_action", nargs="?", default="create",
                   choices=["create", "list", "show", "snooze", "done", "cancel", "check", "exec"])
    s.add_argument("title_words", nargs="*", help="Reminder title (for create)")
    s.add_argument("--at", help="Time spec: 'in 30 minutes', 'every weekday at 9am', etc.")
    s.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    s.add_argument("--channel", help="Notification channels (comma-separated)")
    s.add_argument("--tag", dest="tag_str", help="Tags (comma-separated)")
    s.add_argument("--status", help="Filter (for list): active, snoozed, fired, all")
    s.add_argument("--reminder-id", help="Reminder ID (for show/snooze/done/cancel)")
    s.add_argument("--duration", help="Snooze duration: 15m, 1h, 2h30m")
    s.add_argument("--action", dest="action_command", help="Shell command to execute when due")
    s.add_argument("--instructions", dest="action_instructions",
                   help="NL instructions for Claude (triggers claude -p mode)")
    s.add_argument("--action-mode", dest="action_mode",
                   choices=["shell", "claude", "codex", "opencode", "auto"], default="auto",
                   help="Execution mode (default: auto)")
    s.add_argument("--wake", dest="wake_client", choices=["codex", "opencode"],
                   help="Wake a headless agent when due")
    s.add_argument("--wake-session", "--session", dest="wake_session_id",
                   help="Host session id to resume for --wake; use 'last' for latest")
    s.add_argument("--wake-cwd", "--cwd", dest="wake_cwd",
                   help="Working directory for the wake run")
    s.add_argument("--wake-model", dest="wake_model",
                   help="Model override for the wake run")
    s.add_argument("--wake-agent", dest="wake_agent",
                   help="OpenCode agent override for the wake run")
    s.add_argument("--attention-trigger",
                   help="Comma-separated conversation trigger terms for attention injection")
    s.add_argument("--conversation-id", help="Scope reminder to this conversation/session id")
    s.add_argument("--scope", dest="reminder_scope", choices=["chat", "global"],
                   help="Visibility scope for hook injection")
    s.add_argument("--all-profiles", dest="all_profiles", action="store_true",
                   help="Check reminders across all configured profiles (for check)")
    _common(s)
    s.set_defaults(func=cmd_remind)

    # mode
    s = sub.add_parser("mode", help="Conversation mode management")
    s.add_argument("mode_action", nargs="?", default="list",
                   choices=["activate", "list", "show", "create", "export", "import", "seed"])
    s.add_argument("mode_name", nargs="?", help="Mode name")
    s.add_argument("--primer", help="Primer text (for create)")
    s.add_argument("--boundary", help="Boundary text (for create)")
    s.add_argument("--permissions", help="Permissions text (for create)")
    s.add_argument("--description", help="Mode description (for create)")
    s.add_argument("--context", help="Session context to resume from (for activate)")
    s.add_argument("--file", help="JSON file path (for import)")
    _common(s)
    s.set_defaults(func=cmd_mode)

    # agent-prime-hook (portable one-shot prime hook)
    s = sub.add_parser("agent-prime-hook", help="Portable one-shot agent prime hook")
    s.add_argument("--adapter", default="plain",
                   choices=["plain", "claude", "codex", "antigravity", "opencode"])
    s.add_argument("--client", help="Client family for config overrides")
    s.add_argument("--event", default="PreInvocation", help="Hook event name")
    s.add_argument("--tokens", type=int, default=750)
    s.add_argument("--topic")
    s.add_argument("--conversation-id")
    s.add_argument("--agent-instance")
    _common(s)
    s.set_defaults(func=cmd_agent_prime_hook)

    # agent-stop-hook (portable session-end hook)
    s = sub.add_parser("agent-stop-hook", help="Portable session-end hook")
    s.add_argument("--adapter", default="plain",
                   choices=["plain", "claude", "codex", "antigravity", "opencode"])
    s.add_argument("--conversation-id")
    _common(s)
    s.set_defaults(func=cmd_agent_stop_hook)

    # stop-guard (Claude Code Stop hook)
    s = sub.add_parser("stop-guard", help="Stop hook guard for actionable reminders")
    _common(s)
    s.set_defaults(func=cmd_stop_guard)

    # prompt-check (Claude Code UserPromptSubmit hook)
    s = sub.add_parser("prompt-check", help="Check for due reminders on prompt submit")
    s.add_argument("--text", help="Conversation snippet (normally read from hook stdin)")
    s.add_argument("--conversation-id", help="Conversation/session id for attention budgets")
    s.add_argument("--force-attention", action="store_true",
                   help="Run attention regardless of tick interval")
    s.add_argument("--adapter", default="plain",
                   choices=["plain", "claude", "codex", "antigravity", "opencode"],
                   help="Render hook output for a client protocol")
    s.add_argument("--agent-instance", help="Agent instance/conversation override key")
    _common(s)
    s.set_defaults(func=cmd_prompt_check)

    # attention-hook (advisory tool/action hook)
    s = sub.add_parser("attention-hook", help="Advisory attention hook for tool/action events")
    s.add_argument("--adapter", default="claude",
                   choices=["plain", "claude", "codex", "antigravity", "opencode"],
                   help="Render hook output for a client protocol")
    s.add_argument("--event", help="Hook event name (default from stdin, then PreToolUse)")
    s.add_argument("--text", help="Conversation/action snippet (normally read from hook stdin)")
    s.add_argument("--conversation-id", help="Conversation/session id for attention budgets")
    s.add_argument("--agent-instance", help="Agent instance/conversation override key")
    s.add_argument("--force", action="store_true", help="Run attention regardless of tick interval")
    s.add_argument("--deadline-ms", type=int, default=3500,
                   help="Internal hook deadline; return empty if no result arrives in time")
    _common(s)
    s.set_defaults(func=cmd_attention_hook)

    return p


# Hook-surface commands: exactly the set invoked from installed hook
# entries (setup.install_claude_hooks and the agent adapters) and from
# schedulers (launchd/crontab: `kin cron`, `kin remind check`). Memory
# failure on these must degrade the turn, never crash it. `attention`,
# `dream`, and `remind` are shared with humans, so those gate on the
# invocation shape the installed entries use.
_HOOK_SURFACE_COMMANDS = {
    "prime", "compact-hook", "prompt-check", "stop-guard",
    "attention-hook", "agent-prime-hook", "agent-stop-hook", "cron",
}


def _is_hook_surface(args) -> bool:
    cmd = getattr(args, "command", None)
    if cmd in _HOOK_SURFACE_COMMANDS:
        return True
    if cmd == "attention":
        # Stop-hook sibling: `kin attention reinforce --enqueue`
        return (getattr(args, "attention_action", "") == "reinforce"
                and bool(getattr(args, "enqueue", False)))
    if cmd == "dream":
        # Stop-hook sibling: `kin dream --detach --lightweight`
        return bool(getattr(args, "detach", False))
    if cmd == "remind":
        # Scheduler entry: `kin remind check --all-profiles`
        return getattr(args, "remind_action", "") == "check"
    return False


def _degrade_hook_failure(args, exc: BaseException) -> None:
    """Record a degraded-ledger event and emit the per-hook degraded
    shape: prime-type hooks print one context line, guard-type hooks fail
    open with empty output, capture/maintenance hooks stay silent."""
    from .config import load_config, record_degraded

    cfg = None
    try:
        cfg = load_config(
            getattr(args, "config", None),
            project_path=getattr(args, "project_path", None),
            profile=getattr(args, "profile", None),
        )
    except Exception:
        cfg = None
    record_degraded(getattr(args, "command", None) or "unknown", exc,
                    config=cfg, override_dir=getattr(args, "data_dir", None))
    if getattr(args, "command", None) in ("prime", "agent-prime-hook"):
        print(f"# kindex degraded: {type(exc).__name__} — "
              "session starting without memory context")


def main():
    sys.excepthook = _redacted_excepthook
    from .store import (
        ProfileMismatchError,
        SchemaMigrationError,
        UnsupportedSchemaVersionError,
    )

    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(f"kin {__version__} (Kindex)")
        return

    if not args.command:
        parser.print_help()
        return

    if hasattr(args, "func"):
        if _is_hook_surface(args):
            try:
                args.func(args)
            except Exception as e:
                # Hooks fail open: any failure (corrupt DB, locked file,
                # schema mismatch, profile mismatch) degrades with exit 0.
                # Deliberate exits (SystemExit) pass through.
                _degrade_hook_failure(args, e)
        else:
            try:
                args.func(args)
            except (
                ProfileMismatchError,
                SchemaMigrationError,
                UnsupportedSchemaVersionError,
            ) as e:
                # Storage safety refusals are expected operator actions, not
                # programmer failures: print the remedy without a traceback.
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(2)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
