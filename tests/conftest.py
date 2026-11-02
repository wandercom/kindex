"""Shared test fixtures."""

import re
from datetime import date as _CalendarDate
from datetime import datetime as _RealDateTime
from datetime import timedelta as _DayDelta
from datetime import timezone as _CalendarZone
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.vault import Vault
from kindex.vectors import PROVIDER_DEFAULTS as _EMBED_PROVIDER_DEFAULTS

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_DATA = FIXTURES / "sample-vault"

# Provider API keys that, if inherited from the developer's real environment,
# would make extraction/summarization OR EMBEDDING hit live APIs — turning
# deterministic tests non-deterministic (and spending money). The embedding-
# provider keys are derived from vectors.PROVIDER_DEFAULTS so a new or default
# provider (e.g. Voyage, the default embedder, whose VOYAGE_API_KEY was
# previously missed) is covered automatically instead of silently drifting out.
_PROVIDER_KEY_ENVS = tuple(sorted({
    "ANTHROPIC_API_KEY",  # LLM: extraction / summarization / attention / sim
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    *(d["api_key_env"] for d in _EMBED_PROVIDER_DEFAULTS.values() if d.get("api_key_env")),
}))


@pytest.fixture(autouse=True)
def hermetic_state_dir(tmp_path, monkeypatch):
    """Point XDG_STATE_HOME at a per-test temp dir.

    Pre-merge DB snapshots (kindex.snapshots) default to
    ``$XDG_STATE_HOME/kindex/snapshots``; without this, any test exercising
    dream auto-merges or graph_merge would write the developer's real
    ``~/.local/state``.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def hermetic_user_config(request, tmp_path_factory, monkeypatch):
    """Hide the developer's own Kindex configuration and profile selection.

    The user-level config paths follow HOME, so a real
    ``~/.config/kindex/kin.yaml`` (profiles, default_profile, data_dir)
    otherwise leaks into every load_config() call and every CLI subprocess.
    Tests that need a user config write one under this temporary HOME, or set
    their own HOME and the config paths follow it.
    """
    # Outside tmp_path, which tests own and may list or create "home" in.
    name = re.sub(r"\W", "_", request.node.name)[:40]
    home = tmp_path_factory.mktemp(f"home-{name}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Ambient selection and routing from the developer's shell or host session.
    # KIN_HEALTH_DIR would also bypass the supervisor-health settings gate.
    for var in ("KIN_PROFILE", "KIN_PROJECT", "KIN_PROJECT_PATH", "KIN_AGENT_ID",
                "KIN_CLIENT", "KIN_SESSION_ID", "KIN_HEALTH_DIR", "KIN_LINEAR_ID",
                "KIN_SIGNET_EXECUTABLE"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def hermetic_scheduler(tmp_path, monkeypatch):
    """Keep tests away from the machine's real scheduler.

    Creating or completing a reminder repacks the schedule with the
    developer's own configuration, and a fresh test store has no pending
    reminders, so a test run unloaded (or rewrote) the real launchd job or
    crontab. Every repack here records what it would apply instead; CLI
    subprocesses inherit KIN_NO_SCHEDULER_WRITES and the per-test state
    directory. Tests of the platform writers call them directly with a
    faked subprocess.
    """
    from kindex import scheduling

    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    applied: list[int] = []

    def record(interval, config):
        applied.append(interval)
        return {"action": "updated"}

    monkeypatch.setattr(scheduling, "apply_schedule", record)
    monkeypatch.setattr(scheduling, "_scheduler_state_path",
                        lambda config: tmp_path / "scheduler" / "scheduler-state.json")
    # The repo-local graph registry lives in the same state directory.
    from kindex import project_store
    monkeypatch.setattr(project_store, "project_graph_registry_path",
                        lambda: tmp_path / "scheduler" / "project-graphs.json")
    return applied


@pytest.fixture(autouse=True)
def hermetic_provider_env(monkeypatch):
    """Keep the test suite hermetic.

    Strips ambient LLM provider keys so code paths that consult the environment
    (e.g. ``extract()`` -> ``llm_extract`` -> ``_get_client``) fall back to the
    deterministic keyword extractor instead of calling a live API. Tests that
    exercise the LLM path set their own (fake) key and mock the client, which
    runs after this fixture and therefore overrides it.
    """
    for var in _PROVIDER_KEY_ENVS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sample_config():
    """Config pointing at the sample fixture data."""
    return Config(data_dir=str(SAMPLE_DATA))


@pytest.fixture
def sample_vault(sample_config):
    """Loaded vault from fixture data."""
    return Vault(sample_config).load()


@pytest.fixture
def tmp_vault(tmp_path):
    """Empty vault in a temp directory for write tests."""
    cfg = Config(data_dir=str(tmp_path))
    v = Vault(cfg)
    v.ensure_dirs()
    return v.load()


# --- UTC calendar control (run kindex-clock-test-r1) -------------------------
# Authority: normative. Cites contracts/review-ledger-calendar-contract.md
# @sha256:af69a4e2f1cb2280500126076612f210a9b45827d7eda70c5591fff996890003
# sections "The calendar seam" and "The utc_calendar fixture", and Product
# Specification R1.1-R1.4 / D-2 / D-4. TESTING.md T1, T4, T21; the single
# attribute substitution below is the recorded exception to T16 in PS D-2.
# Only the one module attribute named there is replaced; nothing else is.


class _UtcCalendar:
    """Controller handed to a check by the ``utc_calendar`` fixture.

    Holds one UTC date at noon until ``advance_day()`` moves it, across any
    number of reads (the contract states the read site runs twice per
    successful reservation, so movement is never counted in reads).
    ``type(utc_calendar)(some_date)`` builds an unread instance that installs
    nothing, for checking ``assert_read()`` itself.
    """

    def __init__(self, start_date):
        self.reads = 0
        controller = self

        class _ControlledDateTime(_RealDateTime):
            @classmethod
            def now(cls, tz=None):
                controller.reads += 1
                current = controller._current
                if tz is None:
                    return current.replace(tzinfo=None)
                return current.astimezone(tz)

        self._current = _ControlledDateTime(
            start_date.year, start_date.month, start_date.day, 12, 0,
            tzinfo=_CalendarZone.utc,
        )
        self.datetime_class = _ControlledDateTime

    def advance_day(self):
        """Move the controlled date forward one day; the only movement."""
        self._current = self._current + _DayDelta(days=1)

    def assert_read(self):
        """Fail when the controlled calendar was never read (PS R1.4)."""
        if self.reads == 0:
            raise AssertionError(
                "utc_calendar control was never read; the check would "
                "silently run against the real UTC date"
            )


@pytest.fixture
def utc_calendar(monkeypatch):
    """Opt-in controlled UTC calendar, starting 2031-12-31 at noon UTC.

    Never autouse (PS D-4): a check gains control by naming this fixture.
    Teardown calls ``assert_read()`` so a dead control fails the check
    instead of falling back to the real date (PS R1.4).
    """
    from kindex import subscription_review as native

    calendar = _UtcCalendar(_CalendarDate(2031, 12, 31))
    monkeypatch.setattr(native, "datetime", calendar.datetime_class)
    yield calendar
    calendar.assert_read()
