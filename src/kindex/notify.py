"""Notification channel abstraction and dispatch for Kindex reminders."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .privacy import redact, redact_text
from .privacy import redacting_print as print

if TYPE_CHECKING:
    from .config import Config


@dataclass
class NotifyResult:
    """Result of a notification attempt."""
    success: bool
    channel: str
    message: str = ""

    def __post_init__(self):
        self.message = redact_text(self.message)


@runtime_checkable
class NotificationChannel(Protocol):
    """Protocol for notification channels."""

    name: str

    def is_available(self, config: Config) -> bool: ...

    def send(self, reminder: dict, config: Config) -> NotifyResult: ...

    def supports_actions(self) -> bool: ...


# ── Built-in channels ──────────────────────────────────────────────


class SystemChannel:
    """macOS system notifications via terminal-notifier or osascript."""

    name = "system"

    def is_available(self, config: Config) -> bool:
        import platform
        return (
            config.reminders.channels.system.enabled
            and platform.system() == "Darwin"
        )

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        title = reminder.get("title", "Reminder")
        body = reminder.get("body", "")
        rid = reminder.get("id", "")
        priority = reminder.get("priority", "normal")
        sound = config.reminders.channels.system.sound

        # Try alerter first (persistent, clean notification — no focus steal)
        try:
            import shutil

            alerter = shutil.which("alerter")
            if not alerter:
                for candidate in ("/opt/homebrew/bin/alerter",
                                  "/usr/local/bin/alerter"):
                    if os.path.isfile(candidate):
                        alerter = candidate
                        break

            if alerter:
                alerter_cmd = [
                    alerter,
                    "--title", f"Kindex: {title}",
                    "--message", body or title,
                    "--group", f"kindex-{rid}",
                    "--timeout", "300",
                ]
                if priority == "urgent":
                    alerter_cmd.extend(["--sound", "Basso"])
                elif sound and sound != "none":
                    alerter_cmd.extend(["--sound", sound])

                subprocess.Popen(
                    alerter_cmd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return NotifyResult(True, "system", "alerter")
        except (FileNotFoundError, OSError):
            pass

        # Fallback: osascript notification banner (non-modal, no focus steal)
        try:
            msg = (body or title).replace('"', '\\"')
            ttl = title.replace('"', '\\"')
            # Include reminder ID so user can act on it
            full_msg = f'{msg}\\nkin remind done {rid} | kin remind snooze {rid}'
            script = f'display notification "{full_msg}" with title "Kindex: {ttl}"'
            if sound and sound != "none":
                script += f' sound name "{sound}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            return NotifyResult(True, "system", "osascript")
        except Exception as e:
            return NotifyResult(False, "system", str(e))

    def supports_actions(self) -> bool:
        return True


class SlackChannel:
    """Slack webhook notifications."""

    name = "slack"

    def is_available(self, config: Config) -> bool:
        sc = config.reminders.channels.slack
        return sc.enabled and bool(sc.webhook_url)

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        import json
        import urllib.request

        webhook_url = config.reminders.channels.slack.webhook_url
        priority = reminder.get("priority", "normal")
        emoji = {
            "urgent": ":rotating_light:", "high": ":warning:",
            "normal": ":bell:", "low": ":memo:",
        }.get(priority, ":bell:")

        title = reminder.get("title", "Reminder")
        body = reminder.get("body", "")
        payload = {
            "text": f"{emoji} *Kindex Reminder*: {title}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *{title}*\n{body}" if body else f"{emoji} *{title}*",
                    },
                },
            ],
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            return NotifyResult(True, "slack")
        except Exception as e:
            return NotifyResult(False, "slack", str(e))

    def supports_actions(self) -> bool:
        return False


class EmailChannel:
    """Email notifications via SMTP."""

    name = "email"

    def is_available(self, config: Config) -> bool:
        ec = config.reminders.channels.email
        return ec.enabled and bool(ec.smtp_host and ec.to_addr)

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        import smtplib
        from email.message import EmailMessage

        ec = config.reminders.channels.email
        title = reminder.get("title", "Reminder")
        body = reminder.get("body", "") or title
        priority = reminder.get("priority", "normal")

        msg = EmailMessage()
        msg["Subject"] = f"Kindex Reminder [{priority}]: {title}"
        msg["From"] = ec.from_addr or ec.smtp_user
        msg["To"] = ec.to_addr
        msg.set_content(body)

        password = ""
        if ec.smtp_pass_keychain:
            password = _keychain_get(ec.smtp_pass_keychain)

        try:
            with smtplib.SMTP(ec.smtp_host, ec.smtp_port, timeout=10) as server:
                server.starttls()
                if ec.smtp_user and password:
                    server.login(ec.smtp_user, password)
                server.send_message(msg)
            return NotifyResult(True, "email")
        except Exception as e:
            return NotifyResult(False, "email", str(e))

    def supports_actions(self) -> bool:
        return False


class ClaudeChannel:
    """Inject reminders into active Claude sessions via prime_context hook."""

    name = "claude"

    def is_available(self, config: Config) -> bool:
        return config.reminders.channels.claude.enabled

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        # Claude channel works by having prime_context() read due reminders.
        # This send() is a no-op marker — the actual injection happens in hooks.py.
        return NotifyResult(True, "claude", "queued for hook injection")

    def supports_actions(self) -> bool:
        return True


class TelegramChannel:
    """Telegram Bot API notifications."""

    name = "telegram"

    def is_available(self, config: Config) -> bool:
        tc = config.reminders.channels.telegram
        return tc.enabled and bool(tc.chat_id) and bool(tc.bot_token or tc.bot_token_keychain)

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        import json
        import urllib.request

        tc = config.reminders.channels.telegram
        token = tc.bot_token
        if not token and tc.bot_token_keychain:
            token = _keychain_get(tc.bot_token_keychain)
        if not token:
            return NotifyResult(False, "telegram", "No bot token configured")

        priority = reminder.get("priority", "normal")
        emoji = {
            "urgent": "\u26a0\ufe0f", "high": "\u2757",
            "normal": "\U0001f514", "low": "\U0001f4dd",
        }.get(priority, "\U0001f514")

        title = reminder.get("title", "Reminder")
        body = reminder.get("body", "")
        text = f"{emoji} *Kindex Reminder*: {title}"
        if body:
            text += f"\n{body}"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": tc.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            return NotifyResult(True, "telegram")
        except Exception as e:
            return NotifyResult(False, "telegram", str(e))

    def supports_actions(self) -> bool:
        return False


class TerminalChannel:
    """Terminal bell + stderr fallback (always available)."""

    name = "terminal"

    def is_available(self, config: Config) -> bool:
        return True

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        import sys
        priority = reminder.get("priority", "normal")
        prefix = "[URGENT] " if priority == "urgent" else ""
        title = reminder.get("title", "Reminder")
        print(f"\a{prefix}Kindex Reminder: {title}", file=sys.stderr)
        return NotifyResult(True, "terminal")

    def supports_actions(self) -> bool:
        return False


# ── Channel registry ────────────────────────────────────────────────


_CHANNELS: dict[str, NotificationChannel] = {}


def _register_builtins() -> None:
    for cls in (SystemChannel, SlackChannel, EmailChannel, ClaudeChannel,
                TelegramChannel, TerminalChannel):
        ch = cls()
        _CHANNELS[ch.name] = ch


_register_builtins()


def get_channel(name: str) -> NotificationChannel | None:
    return _CHANNELS.get(name)


def dispatch(
    reminder: dict,
    config: Config,
    channel_names: list[str] | None = None,
) -> list[NotifyResult]:
    """Dispatch a reminder notification to configured channels.

    Tries each channel in order. Stops at first success.
    Falls back to terminal if all others fail.
    """
    reminder = redact(reminder)
    names = channel_names or config.reminders.default_channels

    results = []
    any_success = False
    for name in names:
        ch = get_channel(name)
        if ch is None:
            results.append(NotifyResult(False, name, f"Unknown channel: {name}"))
            continue
        if not ch.is_available(config):
            results.append(NotifyResult(False, name, "Not available/configured"))
            continue
        result = ch.send(reminder, config)
        results.append(result)
        if result.success:
            any_success = True
            break

    # Fallback to terminal if all channels failed
    if not any_success and "terminal" not in names:
        terminal = get_channel("terminal")
        if terminal:
            results.append(terminal.send(reminder, config))

    return results


# ── Activity detection ──────────────────────────────────────────────


def get_idle_seconds() -> float:
    """Get user idle time in seconds on macOS via ioreg.

    Returns 0.0 on non-macOS or if detection fails.
    """
    import platform
    if platform.system() != "Darwin":
        return 0.0
    try:
        import re
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True, text=True, timeout=5,
        )
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
        if match:
            return int(match.group(1)) / 1_000_000_000
    except Exception:
        pass
    return 0.0


def is_user_idle(config: Config) -> bool:
    """Check if user has been idle longer than the suppress threshold."""
    idle = get_idle_seconds()
    return idle > config.reminders.idle_suppress_after


# ── Helpers ─────────────────────────────────────────────────────────


def _keychain_get(service_name: str) -> str:
    """Retrieve a password from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""
