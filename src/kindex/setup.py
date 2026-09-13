"""System setup — install agent integrations, launchd plists, crontab entries."""

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape


def _binding_refused() -> list[str] | None:
    """Under an active config binding, refuse to touch the machine's real
    scheduler (launchd/crontab) — R1.5: no write outside the binding.
    Returns a message list if refused, None if unbound (proceed)."""
    from .config import _bound_root
    if _bound_root is not None:
        return ["Refused: config binding active — cannot modify machine scheduler"]
    return None

if TYPE_CHECKING:
    from .config import Config


def _kin_command_parts(kin_path: str) -> list[str]:
    """Split the fallback python -m invocation while preserving normal kin paths."""
    if " -m kindex.cli" in kin_path:
        return shlex.split(kin_path)
    return [kin_path]


def _kin_hook_command(kin_path: str, args: list[str]) -> str:
    """Build a hook command that loads shell exports before running kin."""
    command = " ".join(shlex.quote(part) for part in [*_kin_command_parts(kin_path), *args])
    script = f"source ~/.profile >/dev/null 2>&1 || true; exec {command}"
    return f"/bin/bash -lc {shlex.quote(script)}"


def _kin_stop_hook_command(kin_path: str, args: list[str]) -> str:
    """Build a Claude Stop hook command that avoids stop-hook recursion."""
    command = " ".join(shlex.quote(part) for part in [*_kin_command_parts(kin_path), *args])
    active_check = (
        "import json,sys; "
        "raw=sys.stdin.read(); "
        "\ntry:\n data=json.loads(raw or '{}') if raw.strip() else {}\n"
        "except Exception:\n data={}\n"
        "sys.exit(0 if data.get('stop_hook_active') else 1)"
    )
    script = (
        "payload=$(cat); "
        f"if printf '%s' \"$payload\" | python3 -c {shlex.quote(active_check)}; "
        "then exit 0; fi; "
        "source ~/.profile >/dev/null 2>&1 || true; "
        f"printf '%s' \"$payload\" | {command}"
    )
    return f"/bin/bash -lc {shlex.quote(script)}"


def _hook_needs_profile(entry: object) -> bool:
    return "source ~/.profile" not in str(entry)


def _hook_needs_stop_active_guard(entry: object) -> bool:
    return "stop_hook_active" not in str(entry)


def _hook_needs_attention_deadline(entry: object) -> bool:
    return "--deadline-ms" not in str(entry)


def _hook_needs_envelope_capture(entry: object) -> bool:
    """Old Stop entries passed `--text "Session ended"`, which preempted
    the stdin envelope so the transcript was never extracted. Re-running
    setup is the migration (issue-#15 pattern): rebuild the whole entry."""
    return "--text" in str(entry)

def install_claude_hooks(config: "Config", dry_run: bool = False, *,
                         mode: str = "legacy") -> list[str]:
    """Install one selected adapter; never load legacy and modern together."""
    from .claude_install import install
    return install(config, mode=mode, dry_run=dry_run)


def install_codex_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex prompt-time attention hook into ~/.codex/hooks.json."""
    hooks_path = config.codex_path / "hooks.json"
    actions = []
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text())
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    kin_path = _find_kin_path()

    # SessionStart hook — inject the Kindex prime block (auto-primed context +
    # the "use kindex"/.kin directive) so Codex sessions start with the same
    # context as Claude. Codex SessionStart supports additionalContext injection
    # (fires on startup/resume/clear); prime --adapter codex emits that envelope.
    session_start = hooks.setdefault("SessionStart", [])
    session_entry = {
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": _kin_hook_command(kin_path, ["prime", "--for", "hook", "--adapter", "codex"]),
            "timeout": 5000,
            "statusMessage": "Loading Kindex context",
        }]
    }
    existing_idx = next(
        (i for i, h in enumerate(session_start)
         if "kin prime" in str(h) or "kindex" in str(h).lower()),
        None,
    )
    if existing_idx is None:
        session_start.append(session_entry)
        actions.append("Added Codex SessionStart hook: kin prime --for hook")
    elif (
        _hook_needs_profile(session_start[existing_idx])
        or "--adapter" not in str(session_start[existing_idx])
    ):
        session_start[existing_idx] = session_entry
        actions.append("Updated Codex SessionStart hook")
    else:
        actions.append("Codex SessionStart hook already installed")

    prompt_submit = hooks.setdefault("UserPromptSubmit", [])
    entry = {
        "hooks": [{
            "type": "command",
            "command": _kin_hook_command(
                kin_path,
                ["attention-hook", "--adapter", "codex", "--event", "UserPromptSubmit",
                 "--deadline-ms", "3500"],
            ),
            "timeout": 5,
            "statusMessage": "Checking Kindex attention",
        }]
    }

    existing_idx = next(
        (i for i, h in enumerate(prompt_submit)
         if "prompt-check" in str(h) or "attention-hook" in str(h)),
        None,
    )
    if existing_idx is None:
        prompt_submit.append(entry)
        actions.append("Added Codex UserPromptSubmit hook: kin attention-hook")
    elif (
        _hook_needs_profile(prompt_submit[existing_idx])
        or "prompt-check" in str(prompt_submit[existing_idx])
        or "--adapter" not in str(prompt_submit[existing_idx])
        or _hook_needs_attention_deadline(prompt_submit[existing_idx])
    ):
        prompt_submit[existing_idx] = entry
        actions.append("Updated Codex UserPromptSubmit hook to source ~/.profile")
    else:
        actions.append("Codex UserPromptSubmit hook already installed")

    post_tool = hooks.setdefault("PostToolUse", [])
    post_entry = {
        "hooks": [{
            "type": "command",
            "command": _kin_hook_command(
                kin_path,
                ["attention-hook", "--adapter", "codex", "--event", "PostToolUse",
                 "--deadline-ms", "3500"],
            ),
            "timeout": 5,
            "statusMessage": "Checking Kindex attention",
        }]
    }
    existing_idx = next((i for i, h in enumerate(post_tool) if "attention-hook" in str(h)), None)
    if existing_idx is None:
        post_tool.append(post_entry)
        actions.append("Added Codex PostToolUse hook: kin attention-hook")
    elif (
        _hook_needs_profile(post_tool[existing_idx])
        or _hook_needs_attention_deadline(post_tool[existing_idx])
    ):
        post_tool[existing_idx] = post_entry
        actions.append("Updated Codex PostToolUse hook with internal deadline")
    else:
        actions.append("Codex PostToolUse attention hook already installed")

    if dry_run:
        actions.append(f"Would write {hooks_path}")
        return actions

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    actions.append(f"Wrote {hooks_path}")
    return actions


def uninstall_codex_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex prompt-time attention hook from ~/.codex/hooks.json."""
    hooks_path = config.codex_path / "hooks.json"
    if not hooks_path.exists():
        return ["No Codex hooks.json found"]

    data = json.loads(hooks_path.read_text())
    hooks = data.get("hooks", {})
    prompt_submit = hooks.get("UserPromptSubmit", [])
    post_tool = hooks.get("PostToolUse", [])
    session_start = hooks.get("SessionStart", [])
    kept = [
        h for h in prompt_submit
        if "prompt-check" not in str(h) and "attention-hook" not in str(h)
    ]
    kept_post = [h for h in post_tool if "attention-hook" not in str(h)]
    kept_session = [
        h for h in session_start
        if "kin prime" not in str(h) and "kindex" not in str(h).lower()
    ]
    if (
        len(kept) == len(prompt_submit)
        and len(kept_post) == len(post_tool)
        and len(kept_session) == len(session_start)
    ):
        return ["No Kindex Codex hooks found"]

    if dry_run:
        return [f"Would remove Codex Kindex hooks from {hooks_path}"]

    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    if kept_post:
        hooks["PostToolUse"] = kept_post
    else:
        hooks.pop("PostToolUse", None)
    if kept_session:
        hooks["SessionStart"] = kept_session
    else:
        hooks.pop("SessionStart", None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"Removed Codex Kindex hooks from {hooks_path}"]


def install_codex_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex as a Codex MCP server in ~/.codex/config.toml.

    This mirrors the config produced by:
        codex mcp add kindex -- kin-mcp

    Preserves existing Codex settings. Returns list of actions taken.
    """
    config_path = config.codex_path / "config.toml"
    actions = []

    existing = config_path.read_text() if config_path.exists() else ""

    if "[mcp_servers.kindex]" in existing:
        actions.append("Codex MCP server already installed")
        return actions

    block = '[mcp_servers.kindex]\ncommand = "kin-mcp"\n'

    if dry_run:
        actions.append(f"Would add Codex MCP server to {config_path}")
        actions.append("Would configure: codex mcp add kindex -- kin-mcp")
        return actions

    config_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = existing.rstrip()
    content = f"{prefix}\n\n{block}" if prefix else block
    config_path.write_text(content)
    actions.append(f"Added Codex MCP server: kindex -> kin-mcp")
    actions.append(f"Wrote {config_path}")
    return actions


def uninstall_codex_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex's Codex MCP server block from ~/.codex/config.toml."""
    config_path = config.codex_path / "config.toml"
    actions = []

    if not config_path.exists():
        return ["No Codex config.toml found"]

    text = config_path.read_text()
    lines = text.splitlines()
    out: list[str] = []
    removed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "[mcp_servers.kindex]":
            removed = True
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(line)
        i += 1

    if not removed:
        return ["No Kindex Codex MCP server found"]

    if dry_run:
        actions.append(f"Would remove Codex MCP server from {config_path}")
    else:
        config_path.write_text("\n".join(out).rstrip() + "\n")
        actions.append(f"Removed Codex MCP server from {config_path}")

    return actions


def install_gemini_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex MCP server config into ~/.gemini/settings.json."""
    settings_path = config.gemini_path / "settings.json"
    actions = []

    if settings_path.exists():
        data = json.loads(settings_path.read_text())
    else:
        data = {}

    mcp_servers = data.setdefault("mcpServers", {})
    if "kindex" in mcp_servers:
        return ["Gemini MCP server already installed"]

    mcp_servers["kindex"] = {"command": "kin-mcp", "args": []}

    if dry_run:
        actions.append(f"Would add Gemini MCP server to {settings_path}")
        actions.append('Would configure: mcpServers.kindex = {"command":"kin-mcp","args":[]}')
        return actions

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    actions.append("Added Gemini MCP server: kindex -> kin-mcp")
    actions.append(f"Wrote {settings_path}")
    return actions


def uninstall_gemini_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex MCP config from ~/.gemini/settings.json."""
    settings_path = config.gemini_path / "settings.json"

    if not settings_path.exists():
        return ["No Gemini settings.json found"]

    data = json.loads(settings_path.read_text())
    mcp_servers = data.get("mcpServers", {})

    if "kindex" not in mcp_servers:
        return ["No Kindex Gemini MCP server found"]

    if dry_run:
        return [f"Would remove Gemini MCP server from {settings_path}"]

    del mcp_servers["kindex"]
    if mcp_servers:
        data["mcpServers"] = mcp_servers
    else:
        data.pop("mcpServers", None)

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"Removed Gemini MCP server from {settings_path}"]


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.exists():
        content = path.read_text().strip()
        if not content:
            return {}
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _antigravity_mcp_paths(config: "Config") -> list[tuple[Path, str]]:
    candidates = [
        (config.antigravity_path / "mcp_config.json", "Antigravity editor/shared"),
        (config.antigravity_cli_path / "mcp_config.json", "Antigravity CLI"),
    ]
    seen: set[Path] = set()
    out: list[tuple[Path, str]] = []
    for path, label in candidates:
        if path in seen:
            continue
        seen.add(path)
        out.append((path, label))
    return out


def install_antigravity_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex MCP config into Antigravity's standalone MCP files.

    Antigravity documentation has used both ~/.gemini/config/mcp_config.json
    and ~/.gemini/antigravity-cli/mcp_config.json for global MCP server config,
    so Kindex writes both while preserving unrelated servers.
    """
    actions: list[str] = []
    server = {"command": "kin-mcp", "args": []}

    for settings_path, label in _antigravity_mcp_paths(config):
        data = _read_json_object(settings_path)
        mcp_servers = data.setdefault("mcpServers", {})
        if "kindex" in mcp_servers:
            actions.append(f"{label} MCP server already installed")
            continue
        mcp_servers["kindex"] = server
        if dry_run:
            actions.append(f"Would add Antigravity MCP server to {settings_path}")
            continue
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        actions.append(f"Added {label} MCP server: kindex -> kin-mcp")
        actions.append(f"Wrote {settings_path}")

    return actions


def uninstall_antigravity_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex MCP config from Antigravity MCP files."""
    actions: list[str] = []
    for settings_path, label in _antigravity_mcp_paths(config):
        if not settings_path.exists():
            actions.append(f"No {label} mcp_config.json found")
            continue
        data = _read_json_object(settings_path)
        mcp_servers = data.get("mcpServers", {})
        if "kindex" not in mcp_servers:
            actions.append(f"No Kindex {label} MCP server found")
            continue
        if dry_run:
            actions.append(f"Would remove Antigravity MCP server from {settings_path}")
            continue
        del mcp_servers["kindex"]
        if mcp_servers:
            data["mcpServers"] = mcp_servers
        else:
            data.pop("mcpServers", None)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        actions.append(f"Removed {label} MCP server from {settings_path}")
    return actions


def _antigravity_hook_config(kin_path: str) -> dict[str, Any]:
    """Kindex hook block for Antigravity's hooks.json schema."""
    return {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": _kin_hook_command(
                    kin_path,
                    ["agent-prime-hook", "--adapter", "antigravity", "--client", "antigravity"],
                ),
                "timeout": 5,
            },
            {
                "type": "command",
                "command": _kin_hook_command(
                    kin_path,
                    ["supervisor-hook", "--adapter", "antigravity"],
                ),
                "timeout": 5,
            },
        ],
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": _kin_hook_command(
                        kin_path,
                        [
                            "attention-hook",
                            "--adapter",
                            "antigravity",
                            "--event",
                            "PreToolUse",
                            "--deadline-ms",
                            "3500",
                        ],
                    ),
                    "timeout": 5,
                }],
            },
        ],
        "Stop": [
            {
                "type": "command",
                "command": _kin_hook_command(
                    kin_path,
                    ["agent-stop-hook", "--adapter", "antigravity"],
                ),
                "timeout": 5,
            },
        ],
    }


def install_antigravity_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex lifecycle hooks into Antigravity hooks.json."""
    hooks_path = config.antigravity_path / "hooks.json"
    data = _read_json_object(hooks_path)
    hook_config = _antigravity_hook_config(_find_kin_path())
    existing = data.get("kindex")

    if existing == hook_config:
        return ["Antigravity Kindex hooks already installed"]

    action = "Updated Antigravity Kindex hooks" if existing else "Added Antigravity Kindex hooks"
    data["kindex"] = hook_config
    actions = [action]

    if dry_run:
        actions.append(f"Would write {hooks_path}")
        return actions

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    actions.append(f"Wrote {hooks_path}")
    return actions


def uninstall_antigravity_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex's Antigravity hook block."""
    hooks_path = config.antigravity_path / "hooks.json"
    if not hooks_path.exists():
        return ["No Antigravity hooks.json found"]
    data = _read_json_object(hooks_path)
    if "kindex" not in data:
        return ["No Kindex Antigravity hooks found"]
    if dry_run:
        return [f"Would remove Antigravity Kindex hooks from {hooks_path}"]
    data.pop("kindex", None)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"Removed Antigravity Kindex hooks from {hooks_path}"]


def install_opencode_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex MCP server config into ~/.config/opencode/opencode.json."""
    settings_path = config.opencode_path / "opencode.json"
    actions = []

    if settings_path.exists():
        data = json.loads(settings_path.read_text())
    else:
        data = {"$schema": "https://opencode.ai/config.json"}

    mcp = data.setdefault("mcp", {})
    if "kindex" in mcp:
        return ["OpenCode MCP server already installed"]

    mcp["kindex"] = {
        "type": "local",
        "command": ["kin-mcp"],
        "enabled": True,
    }

    if dry_run:
        actions.append(f"Would add OpenCode MCP server to {settings_path}")
        actions.append('Would configure: mcp.kindex = {"type":"local","command":["kin-mcp"],"enabled":true}')
        return actions

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    actions.append("Added OpenCode MCP server: kindex -> kin-mcp")
    actions.append(f"Wrote {settings_path}")
    return actions


def uninstall_opencode_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex MCP config from ~/.config/opencode/opencode.json."""
    settings_path = config.opencode_path / "opencode.json"

    if not settings_path.exists():
        return ["No OpenCode opencode.json found"]

    data = json.loads(settings_path.read_text())
    mcp = data.get("mcp", {})

    if "kindex" not in mcp:
        return ["No Kindex OpenCode MCP server found"]

    if dry_run:
        return [f"Would remove OpenCode MCP server from {settings_path}"]

    del mcp["kindex"]
    if mcp:
        data["mcp"] = mcp
    else:
        data.pop("mcp", None)

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"Removed OpenCode MCP server from {settings_path}"]


# The Kindex OpenCode plugin. OpenCode has no settings-file hook LIST like Claude
# (settings.json) or Codex (hooks.json); instead it auto-loads plugins from the
# `plugin/` directory and calls their lifecycle hooks. This plugin gives OpenCode
# the parity Claude/Codex have: it runs `kin prime` IN THE WORKING DIRECTORY at
# session start (so it finds and leverages the repo's own `.kin/`) and injects the
# result into the system prompt via `experimental.chat.system.transform`, and feeds
# Kindex context into compaction via `experimental.session.compacting`. Hooks run in
# the agent's process at the project cwd — which MCP cannot do — so this is what lets
# an OpenCode session actually leverage the repo `.kin/`.
_OPENCODE_PLUGIN_JS = r'''// Kindex OpenCode plugin — installed by `kin setup-opencode-hooks`. Do not edit;
// re-run the installer to update. Primes each session with repo-scoped Kindex
// context by running `kin prime` in the working directory (so it leverages the
// repo's .kin/), mirroring the Claude/Codex SessionStart hooks.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
export const KindexPlugin = async ({ directory, $, client }) => {
  const primeContexts = new Map();
  const turnChecks = new Map();
  const latestUserIds = new Map();
  const compactionTransforms = new Set();
  const windows = new Map();
  const goals = new Map();
  const supervisor = (sid) => new Promise((resolve) => {
    const child = spawn("bash", ["-lc", "source ~/.profile >/dev/null 2>&1 || true; exec kin supervisor-hook --adapter opencode --json"],
      {cwd: directory, stdio: ["pipe", "pipe", "pipe"]});
    let out = "";
    const timer = setTimeout(() => {child.kill(); resolve("Kindex supervisor unavailable: hook timeout; no fresh lookback completed.");}, 20000);
    child.stdout.on("data", chunk => {out += chunk; if (out.length > 1048576) child.kill();});
    child.stderr.resume();
    child.on("error", () => {clearTimeout(timer); resolve("Kindex supervisor unavailable; no fresh lookback completed.");});
    child.on("close", () => {
      clearTimeout(timer);
      try {const result = JSON.parse(out); resolve(result.context || "");}
      catch {resolve("Kindex supervisor failed; no fresh lookback completed.");}
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({session_id: sid, cwd: directory,
      prompt: windows.get(sid) || "", initial_goal: goals.get(sid) || ""}));
  });
  const runKin = async (args) => {
    try {
      const res = await $`bash -lc ${"source ~/.profile >/dev/null 2>&1 || true; exec kin " + args}`
        .cwd(directory)
        .quiet()
        .nothrow();
      const out = res && res.stdout ? res.stdout.toString().trim() : "";
      return res && res.exitCode === 0 ? out : "";
    } catch (_e) {
      return "";
    }
  };
  return {
    "chat.message": async (input, output) => {
      if (!input.sessionID) return;
      if (!output.parts.some(p => p.type === "text" && !p.synthetic && !p.ignored)) return;
      turnChecks.delete(input.sessionID);
      latestUserIds.set(input.sessionID, output.message.id);
      const text = output.parts.filter(p => p.type === "text" && !p.synthetic && !p.ignored).map(p => p.text).join("\n");
      if (!goals.has(input.sessionID)) goals.set(input.sessionID, text.slice(0, 2000));
      windows.set(input.sessionID, ((windows.get(input.sessionID) || "") + "\nUSER: " + text).slice(-12000));
    },
    "tool.execute.after": async (input, output) => {
      if (!input.sessionID) return;
      turnChecks.delete(input.sessionID);
      windows.set(input.sessionID, ((windows.get(input.sessionID) || "") +
        "\nTOOL " + input.tool + ": " + (output.output || "")).slice(-12000));
    },
    "experimental.chat.messages.transform": async (_input, output) => {
      const bySession = new Map();
      const compacting = new Set(output.messages.map(item => item.info.sessionID)
        .filter(sid => compactionTransforms.has(sid)));
      for (const sid of compacting) compactionTransforms.delete(sid);
      for (const item of output.messages) {
        const sid = item.info.sessionID;
        if (!sid || compacting.has(sid)) continue;
        item.parts = item.parts.filter(p => !p.metadata?.kindex_supervisor);
        const text = item.parts.filter(p => !p.synthetic && !p.ignored).map(p => p.type === "text" ? p.text :
          p.type === "tool" ? JSON.stringify({tool: p.tool, state: p.state}) : "").join("\n");
        if (item.info.role === "user" && !goals.has(sid)) goals.set(sid, text.slice(0, 2000));
        bySession.set(sid, ((bySession.get(sid) || "") + "\n" + item.info.role + ": " + text).slice(-12000));
      }
      for (const [sid, text] of bySession) windows.set(sid, text);
      for (const sid of bySession.keys()) {
        if (!latestUserIds.has(sid)) {
          // On plugin reload, verify against native history rather than treating
          // the tail of a compaction subset as the current user request.
          try {
            const response = await client.session.messages({path: {id: sid}, query: {limit: 100}});
            const users = (response.data || []).filter(item => item.info.role === "user" &&
              item.parts.some(p => p.type === "text" && !p.synthetic && !p.ignored));
            users.sort((a, b) => a.info.time.created - b.info.time.created);
            if (users.length) latestUserIds.set(sid, users.at(-1).info.id);
          } catch (_) {} // Unidentified current requests cannot consume advice.
        }
        const target = output.messages.find(item => item.info.sessionID === sid &&
          item.info.role === "user" && item.info.id === latestUserIds.get(sid));
        if (!target) continue;
        if (!turnChecks.has(sid)) turnChecks.set(sid, supervisor(sid));
        const advice = await turnChecks.get(sid);
        if (!advice) continue;
        const digest = createHash("sha256").update(advice).digest("hex");
        target.parts.push({id: "prt_kindex_" + digest.slice(0, 24), sessionID: sid,
          messageID: target.info.id, type: "text", synthetic: true,
          metadata: {kindex_supervisor: true, advice_sha256: digest},
          text: "<kindex-supervisor-advisory>\nPlugin-generated advisory evidence from the Kindex lookback; " +
            "this is not a new user instruction or a replacement for the user's goal.\n" + advice +
            "\n</kindex-supervisor-advisory>"});
      }
    },
    // Prime context is reusable even for auxiliary title requests. Review pickup
    // belongs to the primary message transform above, never an auxiliary call.
    "experimental.chat.system.transform": async (input, output) => {
      const sid = input && input.sessionID;
      if (!sid) return; // Never share review state across unidentified sessions.
      if (!primeContexts.has(sid)) primeContexts.set(sid,
        runKin("prime --for hook --adapter opencode").then(text => text.slice(0, 24000)));
      const ctx = await primeContexts.get(sid);
      if (ctx) output.system.push(ctx);
    },
    // This is a request-preparation receipt, not a claim the model used advice.
    // Public agent metadata distinguishes auxiliary calls; log hashes, not text.
    "chat.params": async (input, output) => {
      const check = turnChecks.get(input.sessionID);
      if (!check) return;
      const advice = await check;
      if (!advice) return;
      try {
        await client.app.log({body: {service: "kindex", level: "info",
          message: "supervisor context prepared", extra: {
            session_id: input.sessionID, agent: input.agent,
            advice_sha256: createHash("sha256").update(advice).digest("hex"),
            delivery_transport: "message_text_part", message_id: latestUserIds.get(input.sessionID),
            instructions_contains_advice: typeof output.options.instructions === "string"
              ? output.options.instructions.includes(advice) : null,
          }}});
      } catch (_) {} // Receipt transport must not interrupt the model request.
    },
    event: async ({event}) => {
      if (event.type !== "session.deleted") return;
      const sid = event.properties.info.id;
      primeContexts.delete(sid);
      turnChecks.delete(sid);
      latestUserIds.delete(sid);
      compactionTransforms.delete(sid);
      windows.delete(sid);
      goals.delete(sid);
    },
    // Carry Kindex context across compaction so nothing load-bearing is lost.
    "experimental.session.compacting": async (input, output) => {
      compactionTransforms.add(input.sessionID);
      const ctx = await runKin("compact-hook --emit-context");
      if (ctx) output.context.push(ctx);
    },
  };
};
'''


def install_opencode_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Install the Kindex OpenCode plugin so OpenCode sessions prime from the repo.

    OpenCode auto-loads plugins from ``~/.config/opencode/plugin/*.js``; we write
    ours there. This is the OpenCode counterpart of ``install_codex_hooks`` /
    ``install_claude_hooks`` — a hook that runs ``kin prime`` in the working
    directory so the session leverages the repo's own ``.kin/`` (MCP cannot,
    because the MCP server's cwd is not the project's).
    """
    plugin_path = config.opencode_path / "plugin" / "kindex.js"
    if plugin_path.exists() and plugin_path.read_text() == _OPENCODE_PLUGIN_JS:
        return ["OpenCode plugin already installed"]
    verb = "Updated" if plugin_path.exists() else "Added"
    if dry_run:
        return [f"Would write Kindex OpenCode plugin to {plugin_path}"]
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(_OPENCODE_PLUGIN_JS)
    return [f"{verb} Kindex OpenCode plugin (kin prime on session start)",
            f"Wrote {plugin_path}"]


def uninstall_opencode_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove the Kindex OpenCode plugin."""
    plugin_path = config.opencode_path / "plugin" / "kindex.js"
    if not plugin_path.exists():
        return ["No Kindex OpenCode plugin found"]
    if dry_run:
        return [f"Would remove {plugin_path}"]
    plugin_path.unlink()
    return [f"Removed {plugin_path}"]


_CURSOR_SUPERVISOR_EVENTS = ("sessionStart", "beforeSubmitPrompt", "postToolUse", "afterMCPExecution", "afterAgentResponse", "stop")


def _is_cursor_supervisor_hook(entry: Any) -> bool:
    """Recognize only the exact Kindex command, including earlier install paths."""
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    try:
        parts = shlex.split(entry["command"])
        if len(parts) == 3 and Path(parts[0]).name == "bash" and parts[1] == "-lc":
            prefix = "source ~/.profile >/dev/null 2>&1 || true; exec "
            if not parts[2].startswith(prefix):
                return False
            parts = shlex.split(parts[2][len(prefix):])
        if parts[-3:] != ["supervisor-hook", "--adapter", "cursor"]:
            return False
        return (len(parts) == 4 and Path(parts[0]).name == "kin") or (
            len(parts) == 6 and parts[1:3] == ["-m", "kindex.cli"])
    except ValueError:
        return False


def _configure_cursor_hooks(config: "Config", *, uninstall: bool, dry_run: bool) -> list[str]:
    path = config.cursor_path / "hooks.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Cursor hooks configuration must be an object; it was not changed")
    original = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Cursor hooks must be an object; existing configuration was not changed")
    if data.get("version", 1) != 1:
        raise ValueError("Unsupported Cursor hooks version; existing configuration was not changed")
    command = _kin_hook_command(_find_kin_path(), ["supervisor-hook", "--adapter", "cursor"])
    for event in _CURSOR_SUPERVISOR_EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Cursor {event} hooks must be an array; configuration was not changed")
        entries = [entry for entry in entries if not _is_cursor_supervisor_hook(entry)]
        if not uninstall:
            entry = {"command": command, "timeout": 20}
            if event == "stop":
                entry["loop_limit"] = 1
            entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not uninstall or "hooks" in data:
        data["hooks"] = hooks
    if not uninstall:
        data.setdefault("version", 1)
    if json.dumps(data, sort_keys=True) == original:
        return ["Cursor Kindex hooks already removed" if uninstall else "Cursor Kindex hooks already installed"]
    verb = "Remove" if uninstall else "Install"
    if dry_run:
        return [f"Would {verb.lower()} Cursor Kindex supervisor hooks in {path}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"{'Removed' if uninstall else 'Installed'} Cursor Kindex supervisor hooks in {path}"]


def install_cursor_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Merge native Cursor hooks without replacing other users' hook entries."""
    return _configure_cursor_hooks(config, uninstall=False, dry_run=dry_run)


def uninstall_cursor_hooks(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove only Kindex's native Cursor supervisor commands."""
    return _configure_cursor_hooks(config, uninstall=True, dry_run=dry_run)


def install_cursor_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Install Kindex MCP server config into ~/.cursor/mcp.json."""
    settings_path = config.cursor_path / "mcp.json"
    actions = []

    if settings_path.exists():
        data = json.loads(settings_path.read_text())
    else:
        data = {}

    mcp_servers = data.setdefault("mcpServers", {})
    if "kindex" in mcp_servers:
        return ["Cursor MCP server already installed"]

    mcp_servers["kindex"] = {
        "type": "stdio",
        "command": "kin-mcp",
    }

    if dry_run:
        actions.append(f"Would add Cursor MCP server to {settings_path}")
        actions.append('Would configure: mcpServers.kindex = {"type":"stdio","command":"kin-mcp"}')
        return actions

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    actions.append("Added Cursor MCP server: kindex -> kin-mcp")
    actions.append(f"Wrote {settings_path}")
    return actions


def uninstall_cursor_mcp(config: "Config", dry_run: bool = False) -> list[str]:
    """Remove Kindex MCP config from ~/.cursor/mcp.json."""
    settings_path = config.cursor_path / "mcp.json"

    if not settings_path.exists():
        return ["No Cursor mcp.json found"]

    data = json.loads(settings_path.read_text())
    mcp_servers = data.get("mcpServers", {})

    if "kindex" not in mcp_servers:
        return ["No Kindex Cursor MCP server found"]

    if dry_run:
        return [f"Would remove Cursor MCP server from {settings_path}"]

    del mcp_servers["kindex"]
    if mcp_servers:
        data["mcpServers"] = mcp_servers
    else:
        data.pop("mcpServers", None)

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"Removed Cursor MCP server from {settings_path}"]


def install_launchd(config: "Config", dry_run: bool = False) -> list[str]:
    """Install macOS launchd plist for kin cron.

    Creates ~/Library/LaunchAgents/com.kindex.cron.plist
    Uses config.reminders.check_interval for the initial interval.
    """
    refused = _binding_refused()
    if refused:
        return refused
    actions = []
    kin_path = _find_kin_path()
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / "com.kindex.cron.plist"
    log_dir = config.scheduler_log_path
    interval = config.reminders.check_interval

    plist_content = _launchd_plist(
        label="com.kindex.cron",
        program_args=[*_kin_command_parts(kin_path), "cron"],
        interval=interval,
        stdout_path=f"{log_dir}/cron.log",
        stderr_path=f"{log_dir}/cron-error.log",
    )

    if not dry_run:
        launch_agents.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content)
        _launchctl_reload(plist_path)
        actions.append(f"Installed launchd plist: {plist_path}")
        actions.append("Loaded with launchctl")
    else:
        actions.append(f"Would install: {plist_path}")

    return actions


def _launchctl_reload(plist_path: Path) -> None:
    """Unload (ignoring 'not loaded' errors) then load a plist.

    A bare ``load`` on an already-loaded label is a no-op error and launchd
    keeps the OLD job definition — an upgraded argv or interval would silently
    never take effect until logout.
    """
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True, timeout=5)
    subprocess.run(["launchctl", "load", str(plist_path)],
                   capture_output=True, timeout=5)


def _launchd_plist(*, label: str, program_args: list[str], interval: int,
                   stdout_path: str, stderr_path: str) -> str:
    """Render a launchd plist for a periodic kin job.

    ``program_args`` is emitted one <string> per argv element so the
    ``python -m kindex.cli`` fallback path stays a valid command instead of
    collapsing into a single unrunnable string.
    """
    arg_lines = "\n".join(
        f"        <string>{escape(part)}</string>" for part in program_args
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
{arg_lines}
    </array>
    <key>StartInterval</key>
    <integer>{int(interval)}</integer>
    <key>StandardOutPath</key>
    <string>{escape(stdout_path)}</string>
    <key>StandardErrorPath</key>
    <string>{escape(stderr_path)}</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def reload_launchd() -> bool:
    """Unload and reload the cron plist. Returns True if successful."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kindex.cron.plist"
    if not plist_path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True, timeout=5)
    result = subprocess.run(["launchctl", "load", str(plist_path)],
                            capture_output=True, timeout=5)
    return result.returncode == 0


def uninstall_launchd(dry_run: bool = False) -> list[str]:
    """Remove the launchd plist."""
    refused = _binding_refused()
    if refused:
        return refused
    actions = []
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kindex.cron.plist"

    if plist_path.exists():
        if not dry_run:
            subprocess.run(["launchctl", "unload", str(plist_path)],
                          capture_output=True, timeout=5)
            plist_path.unlink()
            actions.append("Unloaded and removed launchd plist")
        else:
            actions.append(f"Would remove: {plist_path}")
    else:
        actions.append("No launchd plist found")

    return actions


def is_kindex_cron_line(line: str) -> bool:
    """Shape-match every historical kindex crontab job line.

    Shared by install (migration), uninstall, and the adaptive repack so
    the matchers never diverge. Matches command shapes only — never
    comments or user lines that merely mention a kindex path (e.g. a
    backup job touching ~/.kindex must not be classified as ours).
    Covers the binary form ("kin cron"), the `python -m kindex.cli`
    fallback ("kindex.cli cron"), and the reminder job.
    """
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False
    return ("kin cron" in stripped or "kindex.cli cron" in stripped
            or "kindex cron" in stripped
            or "remind check --all-profiles" in stripped)


def install_crontab(config: "Config", dry_run: bool = False) -> list[str]:
    """Install crontab entries for kin maintenance (for Linux/non-macOS).

    Two entries: the full maintenance cycle, plus a dedicated frequent
    ``kin remind check --all-profiles`` so reminder delivery never waits
    behind a slow or stalled maintenance run.

    Re-running migrates stale entries in place (e.g. a log path frozen
    from a previously active profile) instead of reporting them as
    already installed. A line counts as current when its command and log
    redirect match; the schedule field is ignored so an adaptively
    repacked interval (scheduling._apply_crontab) survives re-runs.
    """
    refused = _binding_refused()
    if refused:
        return refused
    actions = []
    kin_path = _find_kin_path()
    log_dir = config.scheduler_log_path

    # Maintenance runs at :02/:32 so it is never phase-locked with the
    # reminder checker's :00/:05/... schedule.
    wanted = [
        (f"{kin_path} cron >> {log_dir}/cron.log 2>&1",
         f"2-59/30 * * * * {kin_path} cron >> {log_dir}/cron.log 2>&1"),
        (f"remind check --all-profiles >> {log_dir}/reminders.log 2>&1",
         f"*/5 * * * * {kin_path} remind check --all-profiles "
         f">> {log_dir}/reminders.log 2>&1"),
    ]

    # Check existing crontab
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""
    lines = existing.splitlines()

    kept = [l for l in lines if not is_kindex_cron_line(l)]
    pool = [l for l in lines if is_kindex_cron_line(l)]
    had_ours = bool(pool)

    final = []
    changed = False
    for fingerprint, default_line in wanted:
        match = next((l for l in pool if fingerprint in l), None)
        if match is not None:
            # Current command + log target: keep as-is (preserves an
            # adaptively repacked schedule).
            pool.remove(match)
            final.append(match)
        else:
            final.append(default_line)
            changed = True
    if pool:
        # Leftover kindex job lines are stale (old log path) or dupes —
        # dropping them is the migration.
        changed = True

    if not changed:
        actions.append("Crontab entries already exist")
        return actions

    verb, dry_verb = ("Replaced", "replace") if had_ours else ("Added", "add")
    if not dry_run:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            actions.append(f"Failed to create log dir {log_dir}: {e}")
            return actions
        new_crontab = "\n".join([*kept, *final]) + "\n"
        proc = subprocess.run(["crontab", "-"], input=new_crontab,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            for line in final:
                actions.append(f"{verb} crontab: {line}")
        else:
            actions.append(f"Failed to add crontab: {proc.stderr}")
    else:
        for line in final:
            actions.append(f"Would {dry_verb} crontab: {line}")

    return actions


def install_reminder_daemon(config: "Config", dry_run: bool = False) -> list[str]:
    """Install macOS launchd plist for reminder checks.

    Creates ~/Library/LaunchAgents/com.kindex.reminders.plist. Separate from
    the main cron plist so reminder delivery never waits behind heavy
    maintenance (ingest/LLM/embedding): even if a ``kin cron`` run stalls or
    overruns its interval, this job keeps firing due reminders directly.
    ``--all-profiles`` sweeps every configured profile graph, not just the
    default one. Interval is the reminder check_interval capped at 5 minutes.
    """
    refused = _binding_refused()
    if refused:
        return refused
    actions = []
    kin_path = _find_kin_path()
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / "com.kindex.reminders.plist"
    log_dir = config.scheduler_log_path
    interval = min(300, max(60, int(config.reminders.check_interval or 300)))

    plist_content = _launchd_plist(
        label="com.kindex.reminders",
        program_args=[*_kin_command_parts(kin_path), "remind", "check",
                      "--all-profiles"],
        interval=interval,
        stdout_path=f"{log_dir}/reminders.log",
        stderr_path=f"{log_dir}/reminders-error.log",
    )

    if not dry_run:
        launch_agents.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content)
        _launchctl_reload(plist_path)
        actions.append(f"Installed reminder daemon: {plist_path}")
        actions.append(f"Check interval: {interval}s")
    else:
        actions.append(f"Would install: {plist_path}")

    return actions


def uninstall_reminder_daemon(dry_run: bool = False) -> list[str]:
    """Remove the dedicated reminder-check launchd plist."""
    refused = _binding_refused()
    if refused:
        return refused
    actions = []
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kindex.reminders.plist"

    if plist_path.exists():
        if not dry_run:
            subprocess.run(["launchctl", "unload", str(plist_path)],
                           capture_output=True, timeout=5)
            plist_path.unlink()
            actions.append("Unloaded and removed reminder daemon plist")
        else:
            actions.append(f"Would remove: {plist_path}")
    else:
        actions.append("No reminder daemon plist found")

    return actions


def _find_kin_path() -> str:
    """Find the kin executable path."""
    import shutil
    executable = shutil.which("kin")
    if executable:
        return executable
    # Fallback to python -m
    import sys
    return f"{sys.executable} -m kindex.cli"


# ── .kin structured merge driver (project-level) ─────────────────────────

_KIN_MERGE_ATTRS = (
    ".kin/index.json merge=kindex",
    ".kin/code-map.json merge=kindex",
)
_KIN_MERGE_ATTR_HEADER = "# Kindex generated artifacts: structured union merge"


def git_repo_root(start: "str | Path | None" = None) -> "Path | None":
    """Repo top-level for ``start`` (cwd by default), or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start or Path.cwd()),
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def merge_driver_registered(root: "Path") -> bool:
    """True when this clone is FULLY set up for the kindex merge driver.

    Requires BOTH the local .git/config driver definition (per-clone, not shared)
    AND the .gitattributes entries that point the artifacts at it. Requiring both
    means a half-applied install (config written but the .gitattributes write
    failed) reports False and self-heals on the next ``kin index``; and a fresh
    clone whose committed .gitattributes references the driver still registers the
    missing local config.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "merge.kindex.driver"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if not (r.returncode == 0 and r.stdout.strip()):
        return False
    attrs_path = root / ".gitattributes"
    if not attrs_path.exists():
        return False
    have = {line.strip() for line in attrs_path.read_text().splitlines()}
    return all(attr in have for attr in _KIN_MERGE_ATTRS)


def install_merge_driver(root: "Path", dry_run: bool = False) -> list[str]:
    """Register the ``kin merge-kin`` git merge driver + ``.gitattributes``.

    The driver definition lives in the repo's local ``.git/config`` (not shared),
    while ``.gitattributes`` (committed) points the ``.kin`` artifacts at it. Repos
    without the driver registered fall back to git's default merge gracefully.
    """
    actions: list[str] = []
    driver_cmd = f"{_find_kin_path()} merge-kin %O %A %B %P"
    for key, value in (
        ("merge.kindex.name", "Kindex .kin artifact structured merge"),
        ("merge.kindex.driver", driver_cmd),
    ):
        if dry_run:
            actions.append(f"[dry-run] git config {key} = {value}")
            continue
        subprocess.run(["git", "-C", str(root), "config", key, value],
                       capture_output=True, text=True, timeout=5)
        actions.append(f"git config {key} = {value}")

    attrs_path = root / ".gitattributes"
    existing = attrs_path.read_text().splitlines() if attrs_path.exists() else []
    have = {line.strip() for line in existing}
    missing = [a for a in _KIN_MERGE_ATTRS if a not in have]
    if not missing:
        actions.append(f".gitattributes already current ({attrs_path})")
        return actions
    if dry_run:
        actions.append(f"[dry-run] add to .gitattributes: {missing}")
        return actions
    lines = list(existing)
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(_KIN_MERGE_ATTR_HEADER)
    lines.extend(missing)
    attrs_path.write_text("\n".join(lines) + "\n")
    actions.append(f"updated {attrs_path} (+{len(missing)} entries)")
    return actions


def uninstall_merge_driver(root: "Path", dry_run: bool = False) -> list[str]:
    """Remove the merge driver config and ``.gitattributes`` entries."""
    actions: list[str] = []
    if dry_run:
        actions.append("[dry-run] git config --remove-section merge.kindex")
    else:
        r = subprocess.run(
            ["git", "-C", str(root), "config", "--remove-section", "merge.kindex"],
            capture_output=True, text=True, timeout=5,
        )
        actions.append(
            "removed git config section merge.kindex" if r.returncode == 0
            else "git config section merge.kindex not present"
        )
    attrs_path = root / ".gitattributes"
    if attrs_path.exists():
        drop = set(_KIN_MERGE_ATTRS) | {_KIN_MERGE_ATTR_HEADER}
        kept = [ln for ln in attrs_path.read_text().splitlines() if ln.strip() not in drop]
        if dry_run:
            actions.append(f"[dry-run] strip kindex entries from {attrs_path}")
        elif any(ln.strip() for ln in kept):
            attrs_path.write_text("\n".join(kept).rstrip() + "\n")
            actions.append(f"stripped kindex entries from {attrs_path}")
        else:
            attrs_path.write_text("")
            actions.append(f"cleared {attrs_path}")
    return actions
