"""Injectable time source for Track A components.

Twin and controller logic never read the system clock; they receive a ``Clock``. Tests and
simulations use ``ManualClock``, which only moves when told to and never moves backwards.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


def _require_aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class ManualClock:
    """Deterministic clock controlled explicitly by the caller."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware_utc(start, "start")

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | float) -> datetime:
        """Move forward by ``delta`` (a timedelta or seconds). Zero is allowed; negative is not."""

        if isinstance(delta, bool) or not isinstance(delta, timedelta | int | float):
            raise TypeError("delta must be a timedelta or a number of seconds")
        if not isinstance(delta, timedelta):
            if not math.isfinite(delta):
                raise ValueError("delta must be finite")
            delta = timedelta(seconds=delta)
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")
        self._now = self._now + delta
        return self._now

    def set_time(self, value: datetime) -> datetime:
        """Jump to ``value``, which must not be earlier than the current time."""

        target = _require_aware_utc(value, "value")
        if target < self._now:
            raise ValueError("ManualClock cannot move backwards")
        self._now = target
        return self._now
