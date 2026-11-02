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


def validate_destination(destination: str) -> str:
    """Accept explicit HTTP(S), absolute file/device paths, or inherited fd:N."""
    from pathlib import Path
    from urllib.parse import urlsplit

    if not isinstance(destination, str) or not destination or any(ord(c) < 32 for c in destination):
        raise ValueError("Notification destination must be nonempty and contain no controls")
    if destination in ("/dev/null", os.devnull):
        return destination
    if destination.startswith(("http://", "https://")):
        url = urlsplit(destination)
        if not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("Notification URL needs a host, without credentials or fragment")
        _ = url.port  # validate malformed ports
    elif destination.startswith("fd:"):
        if not destination[3:].isascii() or not destination[3:].isdigit() or int(destination[3:]) > 2147483647:
            raise ValueError("Descriptor destination must be fd:N")
    elif not Path(destination).is_absolute():
        raise ValueError("Notification destination must be HTTP(S), an absolute path, or fd:N")
    return destination


def notification_text(reminder: dict) -> str:
    """A notification describes the due item, never claims its action ran."""
    title = str(reminder.get("title") or "Untitled reminder").strip()
    body = str(reminder.get("body") or "").strip()
    lines = [f"Reminder due: {title}"]
    if body and body != title:
        lines.append(body)
    extra = reminder.get("extra") or {}
    if extra.get("wake_client"):
        target = str(extra["wake_client"])
        if extra.get("wake_session_id"):
            target += f" session {extra['wake_session_id']}"
        lines.append(f"Wake target: {target}. Delivery and execution are tracked separately.")
    elif extra.get("action_command") or extra.get("action_instructions"):
        lines.append("An action is scheduled; this notice does not confirm execution.")
    if reminder.get("id"):
        lines.append(f"Reminder ID: {reminder['id']}")
    return redact_text("\n".join(lines))


# Keep DNS, TLS and slow response headers inside a killable process. A socket
# inactivity timeout alone can be extended forever by a trickling endpoint.
_HTTP_POST_SCRIPT = r"""
import json
import sys
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

job = json.load(sys.stdin)
request = urllib.request.Request(job["destination"],
    data=json.dumps(job["payload"]).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=job["timeout"]) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
except Exception:
    sys.exit(1)
"""


def _post_notification(destination: str, payload: dict, timeout: float) -> NotifyResult:
    import json
    import sys
    from .actions import _run_process

    try:
        code, stdout, _ = _run_process([sys.executable, "-I", "-S", "-c", _HTTP_POST_SCRIPT],
            input_text=json.dumps({"destination": destination, "payload": redact(payload),
                                   "timeout": timeout}), timeout=timeout, max_bytes=128)
        if code == 0 and stdout.strip().isdigit():
            status = int(stdout.strip())
            if 200 <= status < 300:
                return NotifyResult(True, "output", "Notification accepted by HTTP endpoint")
            return NotifyResult(False, "output", f"HTTP status {status}; delivery unconfirmed")
    except OSError:
        pass
    return NotifyResult(False, "output", "Notification output failed or timed out; delivery unconfirmed")


class OutputChannel:
    """Write one redacted notification to the selected sink, without fallback."""

    name = "output"

    def is_available(self, config: Config) -> bool:
        return True

    def supports_actions(self) -> bool:
        return False

    def send(self, reminder: dict, config: Config) -> NotifyResult:
        import stat

        destination = (reminder.get("extra") or {}).get(
            "notification_destination", config.reminders.output_destination)
        try:
            validate_destination(destination)
            if destination == "/dev/null" or destination == os.devnull:
                return NotifyResult(True, self.name, "Notification discarded by null destination")
            text = notification_text(reminder)
            if destination.startswith(("http://", "https://")):
                payload = {"id": reminder.get("id", ""), "text": text,
                           "priority": reminder.get("priority", "normal"),
                           "due": reminder.get("next_due", "")}
                return _post_notification(destination, payload, config.reminders.output_timeout)
            # Duplicate caller-owned descriptors; never close or alter their flags.
            descriptor = destination[3:] if destination.startswith("fd:") else None
            fd = (os.dup(int(descriptor)) if descriptor is not None else
                  os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NONBLOCK", 0), 0o600))
            try:
                mode = os.fstat(fd).st_mode
                if not (stat.S_ISREG(mode) or
                        ((stat.S_ISCHR(mode) or stat.S_ISFIFO(mode)) and not getattr(os, "get_blocking", lambda _fd: True)(fd))):
                    raise ValueError("Notification output requires a regular file or nonblocking device/pipe")
                data = (text + "\n").encode("utf-8")
                while data:
                    written = os.write(fd, data)
                    if not written:
                        raise OSError("No output progress")
                    data = data[written:]
            finally:
                os.close(fd)
            return NotifyResult(True, self.name, "Notification written")
        except (OSError, ValueError):
            # URLs and OS errors can contain credentials or private path/query data.
            return NotifyResult(False, self.name, "Notification output failed; delivery unconfirmed")


class SystemChannel:
    """macOS notification submission via alerter or osascript, not display receipts."""

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
                return NotifyResult(True, "system", "alerter submitted; display unconfirmed")
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
            result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            if result.returncode != 0:
                return NotifyResult(False, "system",
                    f"osascript exited with status {result.returncode}; delivery unconfirmed")
            return NotifyResult(True, "system", "osascript accepted notification; display unconfirmed")
        except subprocess.TimeoutExpired:
            return NotifyResult(False, "system", "osascript timed out; delivery unconfirmed")
        except Exception:
            # Exceptions and stderr may contain the full private notification script.
            return NotifyResult(False, "system", "osascript failed; delivery unconfirmed")

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
    for cls in (OutputChannel, SystemChannel, SlackChannel, EmailChannel, ClaudeChannel,
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
    Legacy-only channel lists fall back to terminal; output never falls back.
    """
    original = reminder
    reminder = redact(reminder)
    names = channel_names or config.reminders.default_channels
    if (reminder.get("extra") or {}).get("notification_destination") is not None:
        names = ["output"]

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
        result = ch.send(original if name == "output" else reminder, config)
        results.append(result)
        if result.success:
            any_success = True
            break
        if name == "output":
            break  # An output failure never changes the destination.

    # Fallback to terminal if all channels failed
    if not any_success and "terminal" not in names and "output" not in names:
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
