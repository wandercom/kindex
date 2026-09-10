"""Selectable Claude adapters with handler-level, recorded ownership.

Modern assets never import or invoke the shell-hook adapter. The default remains
legacy during the early-access transition; switching is explicit and reversible.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import os
import tempfile


def _atomic_json(path: Path, value: dict) -> None:
    import os
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".kindex-install-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

QUALIFIED_CLAUDE_VERSION = "2.1.263"
PLUGIN_NAME = "kindex-modern"


def legacy_manifest(config, kin_path: str) -> dict:
    """One command manifest for installs and generated plugin distribution.

    Claude timeouts are seconds, unlike Codex's millisecond hook timeouts.
    """
    from .setup import _kin_hook_command, _kin_stop_hook_command
    def command(args, seconds, stop=False):
        build = _kin_stop_hook_command if stop else _kin_hook_command
        return {"type": "command", "command": build(kin_path, args), "timeout": seconds}
    stop = [command(["compact-hook"], 10, True),
            command(["attention", "reinforce", "--enqueue"], 3, True)]
    if config.reminders.stop_guard_enabled:
        stop.insert(0, command(["stop-guard"], 5, True))
    if config.reminders.dream_on_stop_enabled:
        stop.append(command(["dream", "--detach", "--lightweight"], 3, True))
    items = {
        "SessionStart": [command(["prime", "--for", "hook"], 5)],
        "PreCompact": [command(["compact-hook", "--emit-context"], 10)],
        "UserPromptSubmit": [command(["prompt-check"], 2)],
        "PreToolUse": [command(["attention-hook", "--adapter", "claude", "--event",
                                "PreToolUse", "--deadline-ms", "3500"], 5)],
        "Stop": stop,
    }
    return {event: [{"matcher": "", "hooks": handlers}] for event, handlers in items.items()}


def _historical_kin_paths(data: dict) -> set[str]:
    """Find candidate old executable paths without treating them as ownership.

    An installation may have moved (or its settings may come from another
    machine). Decode shell quoting only; the complete command must still match
    a generated historical template before it can be removed.
    """
    paths = set()
    for entries in data.get("hooks", {}).values():
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for handler in entry["hooks"]:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str):
                    continue
                try:
                    parts = shlex.split(command)
                    if len(parts) == 3 and parts[:2] == ["/bin/bash", "-lc"]:
                        parts = shlex.split(parts[2])
                except ValueError:
                    continue
                paths.update(part for part in parts
                             if Path(part).is_absolute() and Path(part).name == "kin")
    return paths


def _known_commands(config, kin_path, historical_paths=()):
    from .setup import _kin_hook_command, _kin_stop_hook_command
    commands = {h["command"] for entries in legacy_manifest(config, kin_path).values()
                for entry in entries for h in entry["hooks"]}
    # Exact historical forms. Unknown wrappers remain visible for manual review;
    # a word such as 'dream' or 'kindex' is never proof of handler ownership.
    historical = ["prime --for hook", "prime --for hook --adapter claude", "prompt-check",
                  "attention-hook --adapter claude --event PreToolUse",
                  "attention-hook --adapter claude --event PreToolUse --deadline-ms 3500",
                  "compact-hook", "compact-hook --emit-context", "stop-guard",
                  "attention reinforce --enqueue", "dream --detach --lightweight",
                  'compact-hook --text "Session ended"']
    for args in historical:
        for binary in dict.fromkeys(["kin", kin_path, *historical_paths]):
            commands.add(f"{binary} {args}")
            commands.add(_kin_hook_command(binary, shlex.split(args)))
            commands.add(_kin_stop_hook_command(binary, shlex.split(args)))
    commands.update(command.replace("\n", "\\n") for command in list(commands))
    return commands


def unresolved_handlers(data: dict) -> list[str]:
    """Diagnostic only: unknown Kindex-looking wrappers are never auto-deleted."""
    import re
    found = []
    for entries in data.get("hooks", {}).values():
        for entry in entries:
            for handler in entry.get("hooks", []):
                command = handler.get("command", "")
                if re.search(r"(?:^|[\s/])(?:kin(?:[\s-]|$)|kindex[./])", command):
                    found.append(command)
    return found


def remove_owned(data: dict, commands: set[str]) -> int:
    """Remove only exact owned handlers, retaining foreign siblings and metadata."""
    removed = 0
    for event, entries in data.get("hooks", {}).items():
        kept = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            handlers = []
            for handler in entry["hooks"]:
                if isinstance(handler, dict) and handler.get("type") == "command" and handler.get("command") in commands:
                    removed += 1
                else:
                    handlers.append(handler)
            if handlers:
                kept.append({**entry, "hooks": handlers})
        data["hooks"][event] = kept
    return removed


def install(config, *, mode="legacy", dry_run=False, uninstall=False,
            retire_commands: list[str] | None = None) -> list[str]:
    from .setup import _find_kin_path
    if mode not in {"legacy", "modern"}:
        raise ValueError("Claude adapter must be legacy or modern")
    base = config.claude_path
    settings = base / "settings.json"
    record_path = base / "kindex-adapter.json"
    if settings.is_symlink() or record_path.is_symlink():
        raise ValueError("Refusing linked Claude integration settings")
    settings_before = settings.read_bytes() if settings.exists() else None
    data = json.loads(settings.read_text()) if settings.exists() else {}
    original_data = json.loads(json.dumps(data))
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    kin_path = _find_kin_path()
    commands = (_known_commands(config, kin_path, _historical_kin_paths(data))
                | set(record.get("commands", [])) | set(retire_commands or []))
    removed = remove_owned(data, commands)
    actions = [f"Removed {removed} exact Kindex legacy handlers (foreign handlers preserved)"]
    plugin = base / "skills" / PLUGIN_NAME
    staged_plugin = None
    enabled = data.setdefault("enabledPlugins", {})
    enabled[f"{PLUGIN_NAME}@skills-dir"] = not uninstall and mode == "modern"
    # Known packaged legacy plugin identities only; do not disable unrelated
    # plugin names which happen to contain 'kindex'.
    if mode == "modern" and not uninstall:
        unresolved = unresolved_handlers(data)
        if unresolved:
            raise ValueError("Unrecognized Kindex wrappers remain; inspect and explicitly retire with --retire-command: " + json.dumps(unresolved))
        # Verified on 2.1.263: persistent settings env activates modules even
        # when the launching shell does not export the early-access flag.
        data.setdefault("env", {})["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] = "1"
        for name in ("kindex@kindex", "kindex@skills-dir"):
            if name in enabled:
                enabled[name] = False
        claude = shutil.which("claude")
        if not claude:
            raise ValueError("Modern adapter requires Claude Code 2.1.263 on PATH")
        version = subprocess.run([claude, "--version"], capture_output=True, text=True,
                                 timeout=10, check=True).stdout.split()[0]
        if version != QUALIFIED_CLAUDE_VERSION:
            raise ValueError(f"Function hooks qualified on {QUALIFIED_CLAUDE_VERSION}, found {version}; use --mode legacy")
        assets = Path(__file__).parent / "claude_modern"
        manifest_path = plugin / ".claude-plugin" / "plugin.json"
        if plugin.exists() and (not manifest_path.exists() or
                               json.loads(manifest_path.read_text()).get("name") != PLUGIN_NAME):
            raise ValueError(f"Refusing to overwrite an unowned plugin directory: {plugin}")
        if plugin.is_symlink() or (plugin.exists() and any(path.is_symlink() for path in plugin.rglob("*"))):
            raise ValueError("Refusing linked files in installed Kindex plugin")
        actions.append(f"Install {PLUGIN_NAME} at {plugin}; legacy hooks are not loaded")
        actions.append("Enabled function hooks in Claude user settings; restart Claude (early-access host limits still apply)")
        if not dry_run:
            base.mkdir(parents=True, exist_ok=True)
            staged_plugin = Path(tempfile.mkdtemp(dir=base, prefix=".kindex-plugin-stage-"))
            # Build outside skills discovery and never update an enabled plugin
            # in place. A failed copy leaves its current files intact.
            shutil.copytree(assets, staged_plugin, dirs_exist_ok=True)
            # argv is installed data, not executable shell interpolation.
            from .setup import _kin_command_parts
            (staged_plugin / "hooks" / "runtime.ts").write_text("export default " + json.dumps({
                "argv": _kin_command_parts(kin_path),
                "signetExecutable": shutil.which("signet-eval") or "",
            }) + ";\n")
    elif not uninstall:
        manifest = legacy_manifest(config, kin_path)
        for event, entries in manifest.items():
            data.setdefault("hooks", {}).setdefault(event, []).extend(entries)
            actions.append(f"Installed {event} Kindex handlers")
        actions.append("Install canonical legacy adapter; modern plugin disabled")
    else:
        actions.append("Disable owned modern plugin; keep its files for recoverable rollback")
    installed = [] if uninstall or mode == "modern" else [
        h["command"] for entries in legacy_manifest(config, kin_path).values()
        for entry in entries for h in entry["hooks"]]
    record = {"schema": 1, "mode": "disabled" if uninstall else mode,
              "commands": installed, "qualified_claude": QUALIFIED_CLAUDE_VERSION}
    if data == original_data:
        actions.append("Selected Kindex adapter already installed")
    if not dry_run:
        base.mkdir(parents=True, exist_ok=True)
        if (settings.read_bytes() if settings.exists() else None) != settings_before:
            raise ValueError("Claude settings changed during installation; retry after reviewing them")
        if staged_plugin is not None:
            plugin.parent.mkdir(parents=True, exist_ok=True)
            previous_plugin = None
            if plugin.exists():
                backup_root = base / "kindex-adapter-backups"
                backup_root.mkdir(exist_ok=True)
                previous_plugin = backup_root / staged_plugin.name.removeprefix(".")
                os.replace(plugin, previous_plugin)
            try:
                os.replace(staged_plugin, plugin)
            except BaseException:
                if previous_plugin is not None:
                    os.replace(previous_plugin, plugin)
                raise
            if previous_plugin is not None:
                actions.append(f"Previous plugin retained at {previous_plugin}")
        if settings.exists() and data != original_data:
            digest = hashlib.sha256(settings.read_bytes()).hexdigest()[:12]
            backup = settings.with_name(f"settings.kindex-backup-{digest}.json")
            if not backup.exists():
                shutil.copy2(settings, backup)
        if data != original_data or not settings.exists():
            _atomic_json(settings, data)
        if not record_path.exists() or json.loads(record_path.read_text()) != record:
            _atomic_json(record_path, record)
    return actions
