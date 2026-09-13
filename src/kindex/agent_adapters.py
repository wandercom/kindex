"""Agent/client adapter helpers for hook protocols and tool payloads."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any


ADAPTER_ALIASES = {
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "opencode": "opencode",
    "open-code": "opencode",
    "cursor": "cursor",
    "antigravity": "antigravity",
    "ag": "antigravity",
    "plain": "plain",
    "claude-plain": "plain",
}


def hook_project_path(payload: dict, explicit: str | None = None) -> str:
    """Resolve native workspace metadata, independent of a hook's process cwd.

    Antigravity runs global hook commands in its configuration directory and
    sends the real workspace in workspacePaths. Multiple distinct worktrees
    require an explicit project selection instead of arbitrarily taking one.
    """
    from pathlib import Path
    from .config import resolve_project_root
    selected = explicit or payload.get("cwd") or payload.get("project_path")
    if selected:
        if not isinstance(selected, str) or not Path(selected).is_absolute():
            raise ValueError("Hook project path must be absolute")
        return str(resolve_project_root(selected))
    paths = payload.get("workspacePaths") or payload.get("workspace_paths") or payload.get("workspace_roots")
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
        raise ValueError("Hook requires an explicit workspace path")
    roots = {str(resolve_project_root(path)) for path in paths}
    if len(roots) != 1:
        raise ValueError("Hook has multiple workspaces; select one explicit project_path")
    return roots.pop()


def normalize_adapter(adapter: str | None) -> str:
    """Return Kindex's canonical client/adapter name."""
    value = (adapter or "plain").strip().lower()
    return ADAPTER_ALIASES.get(value, value)


# Canonical client names Kindex can scope a node to.
ADAPTER_SCOPE_NAMES = frozenset(
    {"claude", "codex", "gemini", "opencode", "cursor", "antigravity"}
)

# A BARE tag (no `client:`/`agent:` prefix) is treated as a client-scoping signal
# only for coined client names that have no everyday or vendor meaning. Names that
# double as topical subjects — `claude`, `codex`, `gemini`, `cursor`, and the
# 2-char `ag` alias — are NOT inferred from a bare tag, because nodes are routinely
# tagged with them for SUBJECT reasons (e.g. a task tagged `gemini` about the Gemini
# API, which a Claude session may well need). Scope a node to one of those clients
# with an explicit `client:<name>` / `agent:<name>` tag instead.
_BARE_SCOPE_NAMES = frozenset({"antigravity", "opencode"})
_SCOPE_TAG_PREFIXES = ("client:", "agent:")


def _iter_tag_strings(tags: Any) -> list[str]:
    """Normalize a tags value (list/tuple/set or comma/space string) to a list."""
    if isinstance(tags, str):
        return [t for t in re.split(r"[\s,]+", tags) if t]
    if isinstance(tags, (list, tuple, set)):
        return [str(t) for t in tags]
    return []


def node_scope_clients(tags: Any) -> set[str]:
    """Canonical client names a node is scoped to via its tags.

    Recognizes explicit `client:<name>` / `agent:<name>` markers (authoritative for
    any known client, alias-resolved) and bare tags for the coined client names in
    `_BARE_SCOPE_NAMES`. Returns an empty set when the node names no client — i.e.
    it is not client-scoped and applies everywhere.
    """
    clients: set[str] = set()
    for raw in _iter_tag_strings(tags):
        tag = raw.strip().lower()
        if not tag:
            continue
        marker = next(
            (tag[len(p):] for p in _SCOPE_TAG_PREFIXES if tag.startswith(p)),
            None,
        )
        if marker is not None:
            norm = normalize_adapter(marker)
            if norm in ADAPTER_SCOPE_NAMES:
                clients.add(norm)
        elif tag in _BARE_SCOPE_NAMES:
            clients.add(normalize_adapter(tag))
    return clients


def adapter_scoped_out(tags: Any, adapter: str | None) -> bool:
    """True when a node's tags scope it to a different agent client than ``adapter``.

    Some graph nodes are advisory only for one client — e.g. a directive tagged
    ``antigravity`` documents Antigravity's nested ``toolCall`` hook protocol.
    Surfacing it to Claude or Codex (which use the flat ``tool_name``/``tool_input``
    schema) is noise and actively misleading. A node is scoped out when it names one
    or more known clients (see `node_scope_clients`) and the running client is not
    among them. Nodes that name no client apply everywhere and are never scoped out.

    ``tags`` accepts a list/tuple/set of tag strings or a comma/space-separated
    string. ``adapter`` is any adapter name/alias; an unknown or ``plain`` caller
    cannot make a scoping decision and never scopes anything out.
    """
    canonical = normalize_adapter(adapter)
    if canonical not in ADAPTER_SCOPE_NAMES:
        return False
    clients = node_scope_clients(tags)
    if not clients:
        return False
    return canonical not in clients


def scope_adapter(adapter: str | None) -> str:
    """Resolve the client name to use for attention/context scoping.

    A ``plain`` or unknown hook caller is the default (unlabeled) Claude install,
    so it scopes as Claude: it should not see Antigravity/OpenCode-scoped nodes,
    and it should see Claude-scoped ones. Explicit clients pass through unchanged.
    Rendering still uses the caller's original adapter; only scoping is resolved here.
    """
    canonical = normalize_adapter(adapter)
    return canonical if canonical in ADAPTER_SCOPE_NAMES else "claude"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def render_hook_context(
    context: str,
    *,
    adapter: str,
    event: str,
    suppress: bool = False,
) -> str:
    """Render context for a client's hook protocol.

    Core Kindex produces the same context block regardless of client. This
    adapter boundary only translates that block into the client's hook envelope.
    """
    if not context.strip():
        return ""

    canonical = normalize_adapter(adapter)
    hook_event = event or "UserPromptSubmit"

    if canonical == "plain":
        return context

    if canonical == "cursor":
        if hook_event in {"sessionStart", "postToolUse"}:
            return _json({"additional_context": context})
        if hook_event == "stop":
            return _json({"followup_message": context})
        # Cursor's prompt/response hooks have no context injection field.
        return _json({"continue": True} if hook_event == "beforeSubmitPrompt" else {})

    if canonical == "antigravity":
        if hook_event == "PreToolUse":
            return _json({"decision": "allow", "reason": context})
        if hook_event == "Stop":
            return _json({"decision": "", "reason": context})
        payload: dict[str, Any] = {
            "injectSteps": [{"ephemeralMessage": context}],
        }
        if hook_event == "PostInvocation":
            payload["terminationBehavior"] = ""
        return _json(payload)

    hook_output = {
        "hookEventName": hook_event,
        "additionalContext": context,
    }
    # Context is advisory. It must not approve a tool or override an
    # enforcement hook (including signet-eval) later in the chain.
    payload = {"hookSpecificOutput": hook_output}
    # Codex parses suppressOutput but does not implement it yet. Emitting it
    # causes a failed hook run without hiding the context.
    if suppress and canonical == "claude":
        payload["suppressOutput"] = True
    return _json(payload)


def antigravity_allow() -> str:
    """PreToolUse allow response for Antigravity when Kindex has no advisory."""
    return _json({"decision": "allow"})


def extract_tool_call(payload: dict[str, Any] | None) -> tuple[str, Any]:
    """Extract a normalized tool name/input pair from known hook payloads."""
    data = payload or {}
    tool_name = data.get("tool_name") or data.get("toolName")
    tool_input = (
        data.get("tool_input")
        or data.get("toolInput")
        or data.get("parameters")
    )

    tool_call = data.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = tool_name or tool_call.get("name") or tool_call.get("toolName")
        tool_input = (
            tool_input
            or tool_call.get("args")
            or tool_call.get("arguments")
            or tool_call.get("parameters")
        )

    return str(tool_name or ""), tool_input


def extract_shell_command(payload: dict[str, Any] | None) -> str:
    """Return the shell command from Claude/Codex/Antigravity tool payloads."""
    tool_name, tool_input = extract_tool_call(payload)
    if tool_name not in {"Bash", "run_command"}:
        return ""
    if isinstance(tool_input, dict):
        for key in ("command", "CommandLine", "cmd", "script"):
            value = tool_input.get(key)
            if value:
                return str(value)
    return str(tool_input or "")


_CONFIG_WRITE_RE = re.compile(
    r"(?:^|[;&|]\s*)"
    r"(?:"
    r"(?:[A-Za-z0-9_./-]*kin)|"
    r"(?:python3?|[A-Za-z0-9_./-]*python3?)\s+-m\s+kindex\.cli"
    r")\s+"
    r"(?:agent-config\s+set|config\s+set)\b"
)


def is_kindex_config_write(payload: dict[str, Any] | None) -> bool:
    """True when a tool call is trying to mutate Kindex configuration."""
    command = extract_shell_command(payload)
    if not command.strip():
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    for idx, token in enumerate(tokens):
        base = token.rsplit("/", 1)[-1]
        if base == "kin" and tokens[idx + 1:idx + 3] == ["agent-config", "set"]:
            return True
        if base == "kin" and tokens[idx + 1:idx + 3] == ["config", "set"]:
            return True
        if (
            base in {"python", "python3"}
            and tokens[idx + 1:idx + 4] == ["-m", "kindex.cli", "agent-config"]
            and len(tokens) > idx + 4
            and tokens[idx + 4] == "set"
        ):
            return True
    return bool(_CONFIG_WRITE_RE.search(command))


def permission_gate_output(
    *,
    adapter: str,
    event: str,
    payload: dict[str, Any] | None,
) -> str:
    """Return a client permission-gate response, if Kindex should force one."""
    if normalize_adapter(adapter) != "antigravity" or event != "PreToolUse":
        return ""
    if not is_kindex_config_write(payload):
        return ""
    return _json({
        "decision": "force_ask",
        "reason": (
            "This changes Kindex behavior. Approve only if you want this agent "
            "to tune Kindex settings for the requested scope."
        ),
    })
