"""Profile session routing — which profile owns a recorded agent session.

These helpers decide, for a session transcript on disk, which configured
profile's graph it belongs to. They are deliberately dependency-free
(stdlib only) so that both daemon.py and ingest.py can import them without
circular imports. daemon.py re-exports them for backward compatibility.

Routing rules (the profile feature's core sequestration guarantee):
- A cwd recorded inside the session JSONL is authoritative: it is resolved
  (symlinks followed) and matched longest-prefix against each profile's
  resolved roots. If it matches no root, the session belongs to the default
  profile — the lossy encoded-dir fallback is never consulted.
- Only when no cwd could be extracted do we fall back to the Claude-encoded
  project directory name. Because that encoding replaces every
  non-alphanumeric character with '-', a prefix match alone cannot
  distinguish a true subdirectory (~/src/acme/tools) from a punctuated
  sibling (~/src/acme-tools); prefix matches are therefore verified
  against the real filesystem before a profile may claim the session.
"""

from __future__ import annotations

import json as _json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Config


def _encode_claude_project_dir(path: str) -> str:
    """Encode a cwd the way Claude Code names its per-project session dirs.

    Claude Code replaces every non-alphanumeric character of the absolute
    path with '-': /Users/me/Code/my.repo -> -Users-me-Code-my-repo.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _session_cwd(jsonl_path: Path, max_lines: int = 10) -> str:
    """Best-effort cwd extraction from a session JSONL's leading entries."""
    try:
        with open(jsonl_path, "r", errors="replace", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    entry = _json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(entry, dict) and entry.get("cwd"):
                    return str(entry["cwd"])
    except OSError:
        pass
    return ""


def _resolved(p: Path) -> Path:
    """resolve() with a safe fallback for unresolvable paths."""
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p


def cwd_profile_owner(cwd: str, profiles: dict) -> str | None:
    """Which profile owns a raw cwd string, by longest-prefix root match.

    Both the cwd and each root are expanded and resolved (symlinks
    followed), mirroring config._resolve_profile's live-resolution
    semantics so cron routing and interactive resolution agree.
    """
    if not cwd:
        return None
    try:
        cp = _resolved(Path(cwd).expanduser())
    except (ValueError, OSError):
        return None
    best: tuple[int, str] | None = None
    for pname, entry in profiles.items():
        for root in getattr(entry, "roots", None) or []:
            try:
                rp = _resolved(Path(root).expanduser())
            except (ValueError, OSError):
                continue
            if cp == rp or rp in cp.parents:
                plen = len(str(rp))
                if best is None or plen > best[0]:
                    best = (plen, pname)
    return best[1] if best else None


def _enc_suffix_is_subdir(root: Path, suffix: str, _depth: int = 0) -> bool:
    """True if an encoded-name suffix maps to a real directory chain under root.

    Disambiguates a genuine subdirectory (root/tools encodes to
    enc(root)+'-tools') from a punctuated sibling (root '~/src/acme' vs
    directory '~/src/acme-tools' — identical encodings). Conservative:
    if the chain cannot be verified on the filesystem, it does not match,
    so an ambiguous name can never pull a session across profile bounds.
    """
    if _depth > 8 or not suffix.startswith("-"):
        return False
    try:
        children = [c for c in root.iterdir() if c.is_dir()]
    except OSError:
        return False
    for child in children:
        enc_child = "-" + _encode_claude_project_dir(child.name)
        if suffix == enc_child:
            return True
        if (suffix.startswith(enc_child + "-")
                and _enc_suffix_is_subdir(child, suffix[len(enc_child):],
                                          _depth + 1)):
            return True
    return False


def _session_profile_owner(jsonl_path: Path, profiles: dict) -> str | None:
    """Which profile owns a session, by longest-prefix root match (or None).

    The cwd recorded inside the JSONL is authoritative when present; the
    Claude-encoded project directory name is only consulted when no cwd
    could be extracted, and prefix matches are filesystem-verified.
    """
    cwd = _session_cwd(jsonl_path)
    if cwd:
        return cwd_profile_owner(cwd, profiles)

    enc_name = jsonl_path.parent.name
    best: tuple[int, str] | None = None
    for pname, entry in profiles.items():
        for root in getattr(entry, "roots", None) or []:
            rp = Path(root).expanduser()
            rp_res = _resolved(rp)
            matched = False
            # Claude encodes its physical cwd, so the resolved encoding is
            # primary; the unresolved one covers logical-cwd recorders.
            for cand in {str(rp_res), str(rp)}:
                enc = _encode_claude_project_dir(cand)
                if enc_name == enc:
                    matched = True
                    break
                if (enc_name.startswith(enc + "-")
                        and _enc_suffix_is_subdir(rp_res,
                                                  enc_name[len(enc):])):
                    matched = True
                    break
            if matched:
                plen = len(str(rp_res))
                if best is None or plen > best[0]:
                    best = (plen, pname)
    return best[1] if best else None


def profile_session_filter(
    profiles: dict, name: str | None, default_name: str | None
) -> "Callable[[Path], bool]":
    """Predicate deciding whether a session JSONL belongs to profile `name`.

    A session is owned by the profile whose roots contain its cwd (longest
    prefix wins). Sessions matching no profile go to the default profile.
    With name=None (legacy-remainder pass) and no default, the predicate
    accepts exactly the unmatched remainder.
    """
    def _filter(jsonl_path: Path) -> bool:
        owner = _session_profile_owner(jsonl_path, profiles)
        if owner is None:
            return name == default_name
        return owner == name

    return _filter


def effective_session_filter(config: "Config") -> "Callable[[Path], bool] | None":
    """The session routing predicate every Claude-session ingest path must use.

    An explicit per-pass filter (set by daemon.cron_run_all or cmd_cron)
    wins. Otherwise, whenever profiles are configured the filter is built
    from the full profiles dict and the resolved active profile, so that
    `kin ingest sessions`, the MCP ingest tool, and `kin watch` route
    sessions exactly like the multi-pass cron does. Only a config with no
    profiles at all returns None (legacy: take everything).
    """
    explicit = getattr(config, "_session_filter", None)
    if explicit is not None:
        return explicit
    profiles = dict(getattr(config, "profiles", {}) or {})
    if not profiles:
        return None
    active = getattr(config, "active_profile", None)
    default = getattr(config, "default_profile", None)
    return profile_session_filter(profiles, active, default)


def effective_cwd_router(config: "Config") -> "Callable[[str], bool] | None":
    """Ownership predicate over a raw cwd, for non-Claude session layouts.

    Codex rollouts record their cwd in the session meta rather than in a
    Claude-style encoded directory, so routing matches that cwd against
    profile roots directly. None => no profiles configured, take everything.
    """
    profiles = dict(getattr(config, "profiles", {}) or {})
    if not profiles:
        return None
    active = getattr(config, "active_profile", None)
    default = getattr(config, "default_profile", None)

    def _accept(cwd: str) -> bool:
        owner = cwd_profile_owner(cwd, profiles)
        if owner is None:
            return active == default
        return owner == active

    return _accept
