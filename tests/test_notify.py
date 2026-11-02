"""Tests for the notification channel system."""

from __future__ import annotations

import os
import subprocess

import pytest

from kindex.config import Config
from kindex.notify import (
    NotifyResult,
    SystemChannel,
    SlackChannel,
    EmailChannel,
    ClaudeChannel,
    TelegramChannel,
    TerminalChannel,
    dispatch,
    get_channel,
    get_idle_seconds,
    is_user_idle,
)


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path))


@pytest.fixture
def reminder():
    return {
        "id": "test123",
        "title": "Test Reminder",
        "body": "This is a test",
        "priority": "normal",
    }


class TestSystemChannel:
    def test_is_available_check(self, config):
        ch = SystemChannel()
        # On macOS this should be True; on CI/Linux it may be False
        import platform
        expected = platform.system() == "Darwin"
        assert ch.is_available(config) == expected

    def test_is_available_respects_enabled_flag(self, config):
        ch = SystemChannel()
        config.reminders.channels.system.enabled = False

        assert not ch.is_available(config)

    def test_send_fallback(self, config, reminder, monkeypatch):
        ch = SystemChannel()
        # Mock subprocess to simulate alerter not found, osascript fallback
        call_count = {"run": 0, "popen": 0}

        def mock_run(*args, **kwargs):
            call_count["run"] += 1
            class FakeResult:
                returncode = 0
                stdout = ""
                stderr = ""
            return FakeResult()

        class MockPopen:
            def __init__(self, *args, **kwargs):
                call_count["popen"] += 1

        import shutil
        monkeypatch.setattr(shutil, "which", lambda x: None)
        # Also block direct path discovery so alerter is not found.
        monkeypatch.setattr("kindex.notify.os.path.isfile", lambda p: False)
        monkeypatch.setattr("kindex.notify.subprocess.run", mock_run)
        monkeypatch.setattr("kindex.notify.subprocess.Popen", MockPopen)

        result = ch.send(reminder, config)
        # Should have attempted osascript fallback via subprocess.run
        assert call_count["run"] == 1
        assert call_count["popen"] == 0
        assert result.success
        assert "display unconfirmed" in result.message

    @pytest.mark.parametrize("returncode", [1, -9])
    def test_osascript_nonzero_is_failure(self, config, reminder, monkeypatch, returncode):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("kindex.notify.os.path.isfile", lambda path: False)
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, returncode, stdout=b"", stderr=b"private script body")
        monkeypatch.setattr("kindex.notify.subprocess.run", run)
        result = SystemChannel().send(reminder, config)
        assert not result.success
        assert result.channel == "system"
        assert str(returncode) in result.message
        assert "unconfirmed" in result.message
        assert "private script body" not in result.message
        assert calls[0][0][:2] == ["osascript", "-e"]
        assert calls[0][1]["timeout"] == 5
        assert len(calls) == 1

    @pytest.mark.parametrize("error", [FileNotFoundError("private script body"),
        subprocess.TimeoutExpired(["osascript", "private script body"], 5)])
    def test_osascript_unavailable_or_timed_out_is_failure(self, config, reminder, monkeypatch, error):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("kindex.notify.os.path.isfile", lambda path: False)
        def run(*args, **kwargs):
            raise error
        monkeypatch.setattr("kindex.notify.subprocess.run", run)
        result = SystemChannel().send(reminder, config)
        assert not result.success
        assert "unconfirmed" in result.message
        assert "private script body" not in result.message


class TestSlackChannel:
    def test_not_available_without_webhook(self, config):
        ch = SlackChannel()
        assert not ch.is_available(config)

    def test_send_posts_webhook(self, config, reminder, monkeypatch):
        # Enable slack with a fake webhook
        config.reminders.channels.slack.enabled = True
        config.reminders.channels.slack.webhook_url = "https://hooks.slack.com/test"

        ch = SlackChannel()
        assert ch.is_available(config)

        # Mock urlopen
        import urllib.request
        requests_made = []

        def mock_urlopen(req, **kwargs):
            requests_made.append(req)

            class FakeResponse:
                status = 200

                def read(self):
                    return b"ok"
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

        result = ch.send(reminder, config)
        assert result.success
        assert len(requests_made) == 1


class TestEmailChannel:
    def test_not_available_without_smtp(self, config):
        ch = EmailChannel()
        assert not ch.is_available(config)


class TestClaudeChannel:
    def test_is_available(self, config):
        ch = ClaudeChannel()
        assert ch.is_available(config)

    def test_send_returns_success(self, config, reminder):
        ch = ClaudeChannel()
        result = ch.send(reminder, config)
        assert result.success
        assert "queued" in result.message


class TestTerminalChannel:
    def test_always_available(self, config):
        ch = TerminalChannel()
        assert ch.is_available(config)

    def test_send_prints(self, config, reminder, capsys):
        ch = TerminalChannel()
        result = ch.send(reminder, config)
        assert result.success
        captured = capsys.readouterr()
        assert "Test Reminder" in captured.err


class TestDispatch:
    def test_uses_default_channels(self, config, reminder, monkeypatch):
        sent = []

        def mock_send(r, c):
            sent.append("system")
            return NotifyResult(True, "system")

        monkeypatch.setattr(get_channel("system"), "send", mock_send)
        monkeypatch.setattr(get_channel("system"), "is_available", lambda c: True)

        results = dispatch(reminder, config)
        # Default output discards quietly; explicit legacy channels remain supported.
        assert [(r.channel, r.success) for r in results] == [("output", True)]
        assert sent == []

        config.reminders.default_channels = ["system"]
        assert [r.channel for r in dispatch(reminder, config)] == ["system"]
        assert sent == ["system"]

    def test_fallback_to_terminal(self, config, reminder, monkeypatch):
        # Set channels to only a failing one
        config.reminders.default_channels = ["slack"]

        def mock_available(self, c):
            return False

        monkeypatch.setattr(SlackChannel, "is_available", mock_available)

        results = dispatch(reminder, config)
        # Terminal fallback should fire
        assert any(r.channel == "terminal" for r in results)

    def test_unknown_channel(self, config, reminder):
        results = dispatch(reminder, config, channel_names=["nonexistent"])
        assert any("Unknown channel" in r.message for r in results)


class TestTelegramChannel:
    def test_not_available_without_config(self, config):
        ch = TelegramChannel()
        assert not ch.is_available(config)

    def test_available_with_config(self, config):
        config.reminders.channels.telegram.enabled = True
        config.reminders.channels.telegram.bot_token = "123:ABC"
        config.reminders.channels.telegram.chat_id = "456"
        ch = TelegramChannel()
        assert ch.is_available(config)

    def test_send_posts_to_api(self, config, reminder, monkeypatch):
        config.reminders.channels.telegram.enabled = True
        config.reminders.channels.telegram.bot_token = "123:ABC"
        config.reminders.channels.telegram.chat_id = "456"

        ch = TelegramChannel()

        import urllib.request
        requests_made = []

        def mock_urlopen(req, **kwargs):
            requests_made.append(req)
            class FakeResponse:
                status = 200
                def read(self):
                    return b'{"ok":true}'
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

        result = ch.send(reminder, config)
        assert result.success
        assert len(requests_made) == 1
        assert "api.telegram.org" in requests_made[0].full_url

    def test_send_no_token_fails(self, config, reminder):
        config.reminders.channels.telegram.enabled = True
        config.reminders.channels.telegram.chat_id = "456"
        ch = TelegramChannel()
        result = ch.send(reminder, config)
        assert not result.success
        assert "No bot token" in result.message


class TestChannelRegistry:
    def test_get_channel(self):
        for name in ("system", "slack", "email", "claude", "telegram", "terminal"):
            assert get_channel(name) is not None

    def test_get_nonexistent(self):
        assert get_channel("nonexistent") is None


class TestActivityDetection:
    def test_get_idle_returns_float(self):
        idle = get_idle_seconds()
        assert isinstance(idle, float)
        assert idle >= 0.0

    def test_is_user_idle(self, config):
        # With a very high threshold, user should never be idle
        config.reminders.idle_suppress_after = 999999
        assert not is_user_idle(config)

    def test_is_user_idle_low_threshold(self, config, monkeypatch):
        monkeypatch.setattr("kindex.notify.get_idle_seconds", lambda: 1000.0)
        config.reminders.idle_suppress_after = 500
        assert is_user_idle(config)
