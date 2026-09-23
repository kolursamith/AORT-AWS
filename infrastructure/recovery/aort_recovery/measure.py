"""Turning a probe timeline into RTO, availability and RPO.

These are the project's headline numbers, so each function has one job and
returns None - never a flattering default - when the honest answer is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# Probes and the injection are timed by the same clock, but not atomically.
# A probe a fraction of a second either side of the failure instant is the same
# moment, so recovery timing tolerates that much jitter.
CLOCK_TOLERANCE_SECONDS = 1.0

# Debits and credits are currency sums; this absorbs float representation noise
# without hiding a real imbalance.
LEDGER_TOLERANCE = 0.01


@dataclass(frozen=True)
class Probe:
    at: float
    ok: bool
    detail: str = ""


def availability_ratio(probes: Iterable[Probe]) -> float:
    """Share of probes that succeeded."""
    samples = list(probes)
    if not samples:
        # Reporting 1.0 here would claim perfect availability on no evidence.
        raise ValueError("availability needs at least one probe")
    return sum(1 for probe in samples if probe.ok) / len(samples)


def time_to_recover(
    probes: Iterable[Probe],
    failure_at: float,
    consecutive_ok: int = 2,
) -> float | None:
    """Seconds from the failure until the service was *sustainably* healthy.

    Requires `consecutive_ok` successive successful probes: a single success
    between failures is a flap, not a recovery. Returns None if that never
    happened, which must not be confused with recovering instantly.
    """
    relevant = sorted(
        (p for p in probes if p.at >= failure_at - CLOCK_TOLERANCE_SECONDS),
        key=lambda probe: probe.at,
    )
    streak = 0
    for index, probe in enumerate(relevant):
        if not probe.ok:
            streak = 0
            continue
        streak += 1
        if streak >= consecutive_ok:
            first_of_streak = relevant[index - consecutive_ok + 1]
            return max(0.0, first_of_streak.at - failure_at)
    return None


def recovery_moment(probes: Iterable[Probe], failure_at: float,
                    consecutive_ok: int = 2) -> float | None:
    """Absolute time of the sustained recovery, or None."""
    elapsed = time_to_recover(probes, failure_at, consecutive_ok)
    return None if elapsed is None else failure_at + elapsed


def rpo_seconds(failure_at: float, backup_at: float | None) -> float | None:
    """Data-loss exposure: activity between the newest backup and the failure.

    None when there is no backup: that is unbounded exposure, not zero.
    """
    if backup_at is None:
        return None
    return max(0.0, failure_at - backup_at)


def rows_lost(before: Mapping[str, int], after: Mapping[str, int]) -> int | None:
    """Banking rows that disappeared. None if either side could not be counted."""
    if not before or not after:
        return None
    total = 0
    for table, count in before.items():
        if table not in after:
            return None
        total += max(0, count - after[table])
    return total


def transaction_integrity(debits: float | None, credits: float | None) -> str:
    """Whether the ledger still balances after recovery."""
    if debits is None or credits is None:
        return "unknown"
    return "balanced" if abs(debits - credits) <= LEDGER_TOLERANCE else "unbalanced"
