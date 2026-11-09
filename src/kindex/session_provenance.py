"""Recoverable session-discovery deferrals; never rewrite source transcripts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .project_store import is_transient_workspace, session_cwd_status
from .routing import _encode_claude_project_dir, _session_cwd

_PREFIX = "ingest.session_provenance."


class _VerifiedSessionEvidence(BaseModel):
    cwd: str
    encoded_directory: str | None = None
    evidence: str
    transcript_sha256: str | None = None


class _SessionProvenanceRecord(BaseModel):
    source: str
    cwd: str
    reason: Literal["canonical_workspace", "unknown_workspace", "ambiguous_workspace",
                    "transient_workspace", "workspace_not_established",
                    "source_scope_excluded", "discovery_scope_excluded"]
    disposition: Literal["eligible", "excluded", "deferred"]
    encoded_directory: str | None = None
    evidence: str | None = None
    transcript_sha256: str | None = None
    last_verified: _VerifiedSessionEvidence | None = None


def _transcript_digest(path: Path) -> str | None:
    """Identity of observed bytes; a pathname is not historical evidence."""
    try:
        with path.open("rb") as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
            return digest.hexdigest()
    except OSError:
        return None


def _encoded_candidates(config, encoded: str, *, historical_path: str | None = None
                        ) -> tuple[list[Path] | None, bool]:
    """Resolve only filesystem-verified paths in configured discovery scopes.

    Return candidates and whether a matching discovery scope is excluded.
    Candidates are None when bounded traversal cannot establish a complete answer.
    Keep lexical aliases: their ancestry is part of the indexing policy.
    """
    roots = list(config.project_dirs)
    for profile in config.profiles.values():
        roots.extend(profile.roots)
    roots.extend(path for path in (getattr(config, "_project_path", None),
                                  getattr(config, "_project_input_path", None)) if path)
    pending = []
    excluded_scope = False
    from .config import active_root, _resolve_path
    for root in roots:
        path = _resolve_path(root) if active_root() else Path(root).expanduser().absolute()
        # A marked alias out to a canonical directory is not discovery
        # authority for that target. Check its supplied ancestry first.
        if is_transient_workspace(path):
            resolved = path.resolve()
            for spelling in (path, resolved):
                prefix = _encode_claude_project_dir(str(spelling))
                if encoded == prefix or encoded.startswith(prefix + "-"):
                    excluded_scope = True
            # Saved evidence can use another logical alias (e.g. /var vs
            # /private/var). Scope identity must survive that spelling change.
            if historical_path:
                historical = Path(historical_path).resolve()
                if historical == resolved or resolved in historical.parents:
                    excluded_scope = True
            continue
        pending.extend((path, path.resolve()))
    seen, found = set(), {}
    inspected = 0
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        prefix = _encode_claude_project_dir(str(path))
        if encoded != prefix and not encoded.startswith(prefix + "-"):
            continue
        if not path.is_dir():
            continue
        if encoded == prefix:
            # Distinct lexical names can encode identically; do not collapse
            # aliases before applying their potentially different policies.
            found[str(path)] = path
            continue
        try:
            for child in path.iterdir():
                inspected += 1
                if inspected > 10000 or len(seen) > 256:
                    return None, excluded_scope
                child_prefix = _encode_claude_project_dir(str(child))
                if encoded == child_prefix or encoded.startswith(child_prefix + "-"):
                    pending.append(child)
        except OSError:
            return None, excluded_scope
    return list(found.values()), excluded_scope


def session_deferrals(store) -> list[dict]:
    """Local attribution records, exposed by kin status --json for recovery."""
    records = []
    for row in store.conn.execute("SELECT value FROM meta WHERE key LIKE ?", (_PREFIX + "%",)):
        record = _SessionProvenanceRecord.model_validate_json(row[0]).model_dump(exclude_none=True)
        if record.get("disposition") != "eligible":
            records.append(record)
    return records


def session_indexing_allowed(config, store, transcript: Path, *, cwd: str | None = None,
                             source_root: str | Path | None = None) -> bool:
    """Recheck unknown origin every scan; remember observed transient history.

    An explicit cwd never falls back to an encoded directory, even if it is
    missing or excluded. Known worktree transcripts remain excluded after the
    workspace disappears; accepted artifacts belong in the canonical project.
    """
    source = str(transcript.absolute())
    key = _PREFIX + hashlib.sha256(source.encode()).hexdigest()
    saved = store.get_meta(key)
    prior = (_SessionProvenanceRecord.model_validate_json(saved).model_dump(exclude_none=True)
             if saved else {})
    if prior.get("reason") == "transient_workspace":
        return False
    # Current disposition and last verified history are separate facts. A
    # temporary unknown version must not destroy evidence for earlier bytes.
    verified = prior.get("last_verified") or (
        {field: prior.get(field) for field in
         ("cwd", "encoded_directory", "evidence", "transcript_sha256")}
        if prior.get("disposition") == "eligible" else None)
    # The transcript's own location is a source boundary independently of
    # the cwd it claims. Do not even read an excluded source to infer cwd.
    source_excluded = is_transient_workspace(transcript)
    source_scope_excluded = False
    if source_root is not None:
        from .config import active_root, _resolve_path
        raw_root = _resolve_path(source_root) if active_root() else Path(source_root)
        source_scope_excluded = is_transient_workspace(raw_root)
    discovery_scope_excluded = False
    if source_excluded or source_scope_excluded:
        cwd, candidates = cwd or "", []
    else:
        cwd = _session_cwd(transcript) if cwd is None else cwd
        if cwd:
            candidates = [Path(cwd)]
        else:
            historical = (verified.get("evidence") if verified
                          and not verified.get("cwd")
                          and verified.get("encoded_directory") == transcript.parent.name else None)
            candidates, discovery_scope_excluded = _encoded_candidates(
                config, transcript.parent.name, historical_path=historical)
    reason = ("transient_workspace" if source_excluded else
              "source_scope_excluded" if source_scope_excluded else
              "discovery_scope_excluded" if discovery_scope_excluded else "unknown_workspace")
    evidence = None
    digest = None
    if discovery_scope_excluded:
        pass  # current scope denial overrides any older canonical evidence
    elif candidates and len(candidates) == 1:
        path = candidates[0]
        status = session_cwd_status(str(path))
        if status == "transient":
            reason = "transient_workspace"
        elif status == "established":
            reason, evidence = "canonical_workspace", str(path)
            digest = _transcript_digest(transcript)
        elif status == "wip":
            reason = "workspace_not_established"
    elif candidates is None or (candidates and len(candidates) > 1):
        reason = "ambiguous_workspace"
    # Retain positive attribution for this transcript if its canonical checkout
    # later disappears. Never reuse it around current denial or ambiguity.
    encoded = transcript.parent.name if not cwd else None
    if (reason == "unknown_workspace" and verified
            and verified.get("cwd") == cwd and verified.get("encoded_directory") == encoded
            and verified.get("transcript_sha256")
            and verified["transcript_sha256"] == _transcript_digest(transcript)
            and session_cwd_status(verified["evidence"]) in ("unknown", "established")):
        reason, evidence = "canonical_workspace", verified["evidence"]
        digest = verified["transcript_sha256"]
    record = {"source": source, "cwd": cwd, "reason": reason,
              "encoded_directory": encoded,
              "disposition": ("eligible" if evidence else
                              "excluded" if reason == "transient_workspace" else "deferred")}
    if evidence:
        record["evidence"] = evidence
        record["transcript_sha256"] = digest
        if digest:
            verified = {"cwd": cwd, "encoded_directory": encoded,
                        "evidence": evidence, "transcript_sha256": digest}
    if verified:
        record["last_verified"] = verified
    validated = _SessionProvenanceRecord.model_validate(record)
    if validated.model_dump(exclude_none=True) != prior:
        store.set_meta(key, validated.model_dump_json(exclude_none=True))
    return bool(evidence)
