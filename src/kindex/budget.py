"""Budget tracking for LLM API usage with daily/weekly/monthly limits."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import BudgetConfig
from .privacy import redact


def _today() -> str:
    return date.today().isoformat()


def _this_week_start() -> str:
    d = date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def _this_month_start() -> str:
    return date.today().replace(day=1).isoformat()


class BudgetLedger:
    """Tracks LLM spend over time. Persisted as a simple YAML file.

    Format:
        entries:
          - date: "2026-02-24"
            amount: 0.003
            model: "claude-haiku-4-5-20251001"
            purpose: "classify"
            tokens_in: 150
            tokens_out: 50
    """

    def __init__(self, path: Path, limits: BudgetConfig):
        self.path = path
        self.limits = limits
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text()) or {}
            self.entries = data.get("entries", [])
        else:
            self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.dump(
            {"entries": self.entries},
            default_flow_style=False, sort_keys=False,
        ))

    def record(self, amount: float, model: str = "", purpose: str = "",
               tokens_in: int = 0, tokens_out: int = 0,
               cache_creation_tokens: int = 0,
               cache_read_tokens: int = 0,
               conversation_id: str = "",
               estimate: float | None = None,
               metadata: dict[str, Any] | None = None) -> None:
        entry = {
            "date": _today(),
            "amount": round(amount, 6),
            "model": model,
            "purpose": purpose,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
        if conversation_id:
            entry["conversation_id"] = conversation_id
        if estimate is not None:
            entry["estimate"] = round(estimate, 6)
        if metadata:
            entry["metadata"] = metadata
        if cache_creation_tokens:
            entry["cache_creation_tokens"] = cache_creation_tokens
        if cache_read_tokens:
            entry["cache_read_tokens"] = cache_read_tokens
        self.entries.append(redact(entry))
        self._save()

    def _spend_since(
        self,
        since: str,
        *,
        purpose: str | None = None,
        conversation_id: str | None = None,
    ) -> float:
        return sum(
            e.get("amount", 0) for e in self.entries
            if e.get("date", "") >= since
            and (purpose is None or e.get("purpose") == purpose)
            and (conversation_id is None or e.get("conversation_id") == conversation_id)
        )

    @property
    def today_spend(self) -> float:
        return self._spend_since(_today())

    @property
    def week_spend(self) -> float:
        return self._spend_since(_this_week_start())

    @property
    def month_spend(self) -> float:
        return self._spend_since(_this_month_start())

    def can_spend(self) -> bool:
        """Check if any budget remains under all limits."""
        return (self.today_spend < self.limits.daily
                and self.week_spend < self.limits.weekly
                and self.month_spend < self.limits.monthly)

    @property
    def remaining_today(self) -> float:
        return max(0, self.limits.daily - self.today_spend)

    def conversation_spend(
        self,
        conversation_id: str,
        *,
        since: str | None = None,
        purpose: str | None = None,
    ) -> float:
        """Spend for one conversation, optionally filtered by date/purpose."""
        start = since or "0000-00-00"
        return self._spend_since(
            start,
            purpose=purpose,
            conversation_id=conversation_id,
        )

    def summary(self, conversation_id: str | None = None) -> dict:
        s = {
            "today": {"spent": round(self.today_spend, 4),
                      "limit": self.limits.daily,
                      "remaining": round(self.remaining_today, 4)},
            "week": {"spent": round(self.week_spend, 4),
                     "limit": self.limits.weekly,
                     "remaining": round(max(0, self.limits.weekly - self.week_spend), 4)},
            "month": {"spent": round(self.month_spend, 4),
                      "limit": self.limits.monthly,
                      "remaining": round(max(0, self.limits.monthly - self.month_spend), 4)},
            "can_spend": self.can_spend(),
        }
        if conversation_id:
            s["conversation"] = {
                "id": conversation_id,
                "spent": round(self.conversation_spend(conversation_id), 4),
                "spent_today": round(
                    self.conversation_spend(conversation_id, since=_today()),
                    4,
                ),
            }
        cache = self.cache_efficiency()
        if cache["total_cacheable"] > 0:
            s["cache"] = cache
        return s

    def cache_efficiency(self) -> dict:
        """Cache hit rate and savings from today's entries."""
        today = _today()
        recent = [e for e in self.entries if e.get("date", "") >= today]
        cache_read = sum(e.get("cache_read_tokens", 0) for e in recent)
        cache_write = sum(e.get("cache_creation_tokens", 0) for e in recent)
        total = cache_read + cache_write
        hit_rate = cache_read / total if total > 0 else 0.0
        return {
            "cache_hit_rate": round(hit_rate, 3),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "total_cacheable": total,
        }
