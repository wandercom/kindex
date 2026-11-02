"""Deterministic UTC-rollover accounting for the review allowance ledger.

Authority: normative (all cases pre-freeze). Oracle: the stated items below,
never an observed run (TESTING.md T1). Expected dictionaries are derived
mechanically from the contract's accounting rules and written out literally:
``remaining`` is ``max(0, limit - used)``, the conversation count is
cumulative across UTC dates, and a new UTC date starts its day count at 0.

Cites, for every assertion in this module unless a case cites more:

- contracts/review-ledger-calendar-contract.md
  @sha256:af69a4e2f1cb2280500126076612f210a9b45827d7eda70c5591fff996890003
  sections "Public ledger operations", "Shipped accounting semantics",
  "The utc_calendar fixture" and "Literal values for the rollover checks".
- Product Specification R1.4, R2.1-R2.4 (run kindex-clock-test-r1).
- Testing & Monitoring Strategy R1.4, R2.1-R2.3 planned cases; TESTING.md
  T1, T4-T6, T9, T35, T36.

Real ledger operations only: ``reserve_attempt`` and ``allowance_status``.
The persisted ledger file is never read or written here; its format is
private (Testing Strategy principles). The only substitution in play is
the ``utc_calendar`` fixture's single calendar attribute (PS D-2).
Each case builds its own ledger under its own ``tmp_path`` and reads only
the conversation it created, so cases pass alone and in any order (T35).
The controlled calendar starts at 2031-12-31, so ``advance_day()`` lands
on 2032-01-01, crossing month and year.
"""
from __future__ import annotations

import datetime

import pytest

from kindex import subscription_review as native
from kindex.config import Config


def _config(tmp_path, *, max_conversation_reviews, max_daily_reviews):
    # Contract "Public ledger operations": this exact Config shape; the
    # ledger is scoped to data_dir and survives a fresh Config on it.
    return Config(data_dir=str(tmp_path), sim={
        "enabled": True, "backend": "codex",
        "max_conversation_reviews": max_conversation_reviews,
        "max_daily_reviews": max_daily_reviews,
    })


def test_calendar_control_never_read_fails_the_check(tmp_path, utc_calendar):
    """R1.4: an unread calendar control raises, never silently falls back.

    Falsifier: ``assert_read()`` stops raising on zero reads (mutation M10)
    makes the ``pytest.raises`` below fail. Teardown enforcement on the
    real installed control is the outer fixture's own ``assert_read()``;
    this case earns its green teardown with one real ledger read.
    """
    unread = type(utc_calendar)(datetime.date(2031, 12, 31))
    assert unread.reads == 0, (
        "precondition: the probe instance installed nothing and was not read"
    )
    with pytest.raises(AssertionError):
        unread.assert_read()

    # One real read through the outer (installed) control. A fresh
    # conversation on the controlled start date owes the fixed literal
    # below, so the read reaching the ledger is observed, not assumed.
    cfg = _config(tmp_path, max_conversation_reviews=5, max_daily_reviews=10)
    status = native.allowance_status(cfg, "calendar-control-probe")
    assert status["day_id"] == "2031-12-31"
    assert status["conversation"] == {"used": 0, "limit": 5, "remaining": 5}
    assert status["day"] == {"used": 0, "limit": 10, "remaining": 10}
    assert status["provider_quota"] == "unknown"
    utc_calendar.assert_read()  # the installed control was read; must not raise


def test_utc_rollover_conversation_cumulative_day_restarts(tmp_path, utc_calendar):
    """R2.1: 2 reservations on D, 1 on D+1 -> conversation 3, day 1.

    Limits per contract "Literal values": conversation 5, day 10.
    Falsifiers: day key fixed at first date (M1) breaks ``day_id``/day
    literals on D+1; day count summing all dates (M2) breaks day used 1;
    conversation resetting at a new date (M3) breaks conversation used 3.
    """
    cfg = _config(tmp_path, max_conversation_reviews=5, max_daily_reviews=10)
    conversation = "rollover-counts"

    first = native.reserve_attempt(cfg, conversation)
    assert first["status"] == "ok"
    assert first["allowance"]["day_id"] == "2031-12-31"
    assert first["allowance"]["conversation"] == {"used": 1, "limit": 5, "remaining": 4}
    assert first["allowance"]["day"] == {"used": 1, "limit": 10, "remaining": 9}

    second = native.reserve_attempt(cfg, conversation)
    assert second["status"] == "ok"
    assert second["allowance"]["day_id"] == "2031-12-31"
    assert second["allowance"]["conversation"] == {"used": 2, "limit": 5, "remaining": 3}
    assert second["allowance"]["day"] == {"used": 2, "limit": 10, "remaining": 8}

    on_d = native.allowance_status(cfg, conversation)
    assert on_d["day_id"] == "2031-12-31"
    assert on_d["conversation"] == {"used": 2, "limit": 5, "remaining": 3}
    assert on_d["day"] == {"used": 2, "limit": 10, "remaining": 8}
    assert on_d["provider_quota"] == "unknown"

    utc_calendar.advance_day()

    third = native.reserve_attempt(cfg, conversation)
    assert third["status"] == "ok"
    assert third["allowance"]["day_id"] == "2032-01-01"
    assert third["allowance"]["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert third["allowance"]["day"] == {"used": 1, "limit": 10, "remaining": 9}

    on_next = native.allowance_status(cfg, conversation)
    assert on_next["day_id"] == "2032-01-01"
    assert on_next["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert on_next["day"] == {"used": 1, "limit": 10, "remaining": 9}
    assert on_next["provider_quota"] == "unknown"


def test_utc_rollover_day_limit_reopens_next_day(tmp_path, utc_calendar):
    """R2.2: a day limit exhausted on D refuses on D and reopens on D+1.

    Day limit 2 with conversation limit 5 above it, so the same-day
    refusal can only be the day limit: at refusal the conversation still
    has remaining 3. Falsifiers: refusal ignoring the day (M4) breaks the
    same-day refusal; M1/M2 break the reopening literals on D+1. The
    refused attempt's allowance still shows day used 2, proving a refused
    attempt reserved nothing (contract "Public ledger operations").
    """
    cfg = _config(tmp_path, max_conversation_reviews=5, max_daily_reviews=2)
    conversation = "rollover-day-limit"

    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"
    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"

    refused = native.reserve_attempt(cfg, conversation)
    assert refused["status"] == "review_budget_exhausted"
    assert refused["allowance"]["day_id"] == "2031-12-31"
    assert refused["allowance"]["conversation"] == {"used": 2, "limit": 5, "remaining": 3}
    assert refused["allowance"]["day"] == {"used": 2, "limit": 2, "remaining": 0}

    utc_calendar.advance_day()

    reopened = native.reserve_attempt(cfg, conversation)
    assert reopened["status"] == "ok"
    assert reopened["allowance"]["day_id"] == "2032-01-01"
    assert reopened["allowance"]["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert reopened["allowance"]["day"] == {"used": 1, "limit": 2, "remaining": 1}

    status = native.allowance_status(cfg, conversation)
    assert status["day_id"] == "2032-01-01"
    assert status["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert status["day"] == {"used": 1, "limit": 2, "remaining": 1}


def test_utc_rollover_conversation_limit_survives_next_day(tmp_path, utc_calendar):
    """R2.2: a conversation limit exhausted on D still refuses on D+1.

    Conversation limit 2 with day limit 10 above it. On D+1 the new day
    has remaining 10, so the surviving refusal can only be the
    conversation limit. Falsifier: conversation resetting at a new date
    (M3) turns the D+1 attempt into ``ok`` and moves the counts.
    """
    cfg = _config(tmp_path, max_conversation_reviews=2, max_daily_reviews=10)
    conversation = "rollover-conversation-limit"

    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"
    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"

    on_d = native.allowance_status(cfg, conversation)
    assert on_d["day_id"] == "2031-12-31"
    assert on_d["conversation"] == {"used": 2, "limit": 2, "remaining": 0}
    assert on_d["day"] == {"used": 2, "limit": 10, "remaining": 8}

    utc_calendar.advance_day()

    refused = native.reserve_attempt(cfg, conversation)
    assert refused["status"] == "review_budget_exhausted"
    assert refused["allowance"]["day_id"] == "2032-01-01"
    assert refused["allowance"]["conversation"] == {"used": 2, "limit": 2, "remaining": 0}
    assert refused["allowance"]["day"] == {"used": 0, "limit": 10, "remaining": 10}
    assert refused["allowance"]["day"]["remaining"] > 0, (
        "reachability: the new day had room, so the conversation limit "
        "is the refusal that engaged"
    )

    status = native.allowance_status(cfg, conversation)
    assert status["day_id"] == "2032-01-01"
    assert status["conversation"] == {"used": 2, "limit": 2, "remaining": 0}
    assert status["day"] == {"used": 0, "limit": 10, "remaining": 10}


def test_utc_rollover_counts_survive_fresh_config(tmp_path, utc_calendar):
    """R2.3: the rollover answer is the durable ledger's, not process memory.

    Same reservation history shape as R2.1, then the D+1 read is repeated
    through a fresh ``Config`` on the same directory and asserted against
    the same literals (not merely against the first read). Falsifier:
    counts cached per ``Config`` instance (M7) reddens the fresh-Config
    read and only it.
    """
    cfg = _config(tmp_path, max_conversation_reviews=5, max_daily_reviews=10)
    conversation = "rollover-fresh-config"

    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"
    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"
    utc_calendar.advance_day()
    assert native.reserve_attempt(cfg, conversation)["status"] == "ok"

    via_original = native.allowance_status(cfg, conversation)
    assert via_original["day_id"] == "2032-01-01"
    assert via_original["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert via_original["day"] == {"used": 1, "limit": 10, "remaining": 9}

    fresh = _config(tmp_path, max_conversation_reviews=5, max_daily_reviews=10)
    via_fresh = native.allowance_status(fresh, conversation)
    assert via_fresh["day_id"] == "2032-01-01"
    assert via_fresh["conversation"] == {"used": 3, "limit": 5, "remaining": 2}
    assert via_fresh["day"] == {"used": 1, "limit": 10, "remaining": 9}
    assert via_fresh["provider_quota"] == "unknown"
