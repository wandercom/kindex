"""Budget tracking for LLM API usage with daily/weekly/monthly limits."""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
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
        self._seen: tuple | None = None
        self._lock_depth = 0
        self._load()

    # Many processes share this file: cron drains, background attention and
    # sim workers, hooks, kin-mcp. Each used to load it once and rewrite it
    # whole from that snapshot, so a later writer erased an earlier one's
    # spend (two $0.40 calls left one entry and a $0.50 limit unmet), and a
    # torn rewrite left YAML nobody could read. Writes now re-read under a
    # lock and replace the file atomically; reads follow the file.

    def _stamp(self) -> tuple | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _load(self) -> None:
        stamp = self._stamp()
        if stamp is None:
            self.entries = []
            self._seen = None
            return
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
            entries = data.get("entries", []) if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise ValueError("budget ledger has no entry list")
        except (OSError, ValueError, yaml.YAMLError) as error:
            self.entries = self._recover(error)
            self._seen = self._stamp()
            return
        self.entries = entries
        self._seen = stamp

    def _refresh(self) -> None:
        if self._stamp() != self._seen:
            self._load()

    def _recover(self, error: BaseException) -> list[dict]:
        """An unreadable ledger is set aside, never overwritten, and spending
        stops for the rest of the day: the lost history might have used the
        allowance, and a fresh ledger that assumed nothing was spent could
        spend it again. Tomorrow's allowance is untouched."""
        from .config import record_degraded

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        kept = self.path.with_name(f"{self.path.name}.unreadable-{stamp}")
        entries = [{
            "date": _today(),
            "amount": round(float(self.limits.daily), 6),
            "model": "",
            "purpose": "ledger-recovered",
            "tokens_in": 0,
            "tokens_out": 0,
            "metadata": {"unreadable_ledger": kept.name},
        }]
        try:
            with self._locked():
                if self._stamp() is not None:
                    os.replace(self.path, kept)
                self._write(entries)
        except OSError:
            pass
        record_degraded("budget", error)
        return entries

    @contextmanager
    def _locked(self):
        # Re-entrant within this ledger: flock belongs to the open file, so a
        # second open taking it again would wait on itself.
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _write(self, entries: list[dict]) -> None:
        body = yaml.dump({"entries": entries}, default_flow_style=False, sort_keys=False)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent),
                                        prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        with self._locked():
            self._write(self.entries)
            self._seen = self._stamp()

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
        with self._locked():
            # Append to what is on disk now, not to this ledger's snapshot.
            self._load()
            self.entries.append(redact(entry))
            self._write(self.entries)
            self._seen = self._stamp()

    def _spend_since(
        self,
        since: str,
        *,
        purpose: str | None = None,
        conversation_id: str | None = None,
    ) -> float:
        self._refresh()
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
        self._refresh()
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
