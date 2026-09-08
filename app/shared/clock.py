"""Injectable clocks for deterministic tests and real UTC runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production default — wall-clock UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """Deterministic clock for tests. Always timezone-aware UTC."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(timezone.utc)

    def advance(self, **kwargs: float) -> datetime:
        """Advance by datetime.timedelta kwargs (days=, seconds=, …)."""
        from datetime import timedelta

        self._instant = self._instant + timedelta(**kwargs)
        return self._instant


UTC = SystemClock()
