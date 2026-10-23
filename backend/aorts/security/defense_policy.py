"""Status-check admission control at the switch (Security Module 1).

Order of checks for an authenticated ``/check-status`` request:

1. identity binding  - claimed ``bank_id`` must equal the HMAC-authenticated identity
                       (prevents quota evasion by changing the claimed bank id);
2. global ceiling    - emergency cap on all status checks;
3. per-bank bucket   - short-term rate per remitter bank;
4. per-route bucket  - short-term rate per route (``A1-B``) so one route cannot starve others;
5. Tier 2 credits    - longer-horizon budget per bank (see ``tier2_credits.py``).

All buckets are examined first and debited together only if every check passes,
so a rejected request never consumes capacity it did not use.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.security.rate_limiter import TokenBucket
from backend.aorts.security.tier2_credits import Tier2CreditLedger


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    code: ErrorCode | None = None
    reason: str = ""
    retry_after: float = 0.0
    decision_ms: float = 0.0


class AdmissionController:
    def __init__(
        self,
        *,
        bank_rate: float,
        bank_burst: float,
        route_rate: float,
        route_burst: float,
        global_rate: float,
        global_burst: float,
        credit_cap: float,
        credit_refill: float,
        clock=time.monotonic,
    ):
        self.clock = clock
        self.bank_rate, self.bank_burst = bank_rate, bank_burst
        self.route_rate, self.route_burst = route_rate, route_burst
        self.global_bucket = TokenBucket(global_rate, global_burst, clock)
        self.bank_buckets: dict[str, TokenBucket] = {}
        self.route_buckets: dict[str, TokenBucket] = {}
        self.credits = Tier2CreditLedger(credit_cap, credit_refill, clock)
        self._lock = threading.Lock()

    def _bank(self, bank_id: str) -> TokenBucket:
        if bank_id not in self.bank_buckets:
            self.bank_buckets[bank_id] = TokenBucket(self.bank_rate, self.bank_burst, self.clock)
        return self.bank_buckets[bank_id]

    def _route(self, route_id: str) -> TokenBucket:
        if route_id not in self.route_buckets:
            self.route_buckets[route_id] = TokenBucket(
                self.route_rate, self.route_burst, self.clock
            )
        return self.route_buckets[route_id]

    def admit(
        self,
        authenticated_bank: str,
        claimed_bank: str,
        route_id: str,
        *,
        rate_limiting: bool = True,
        credits: bool = True,
    ) -> AdmissionDecision:
        start = time.perf_counter()

        def done(admitted, code=None, reason="", retry=0.0):
            return AdmissionDecision(
                admitted,
                code,
                reason,
                round(retry, 3),
                round((time.perf_counter() - start) * 1000, 4),
            )

        if authenticated_bank != claimed_bank or not route_id.startswith(f"{authenticated_bank}-"):
            return done(
                False,
                ErrorCode.IDENTITY_MISMATCH,
                f"authenticated as {authenticated_bank} but claimed {claimed_bank}/{route_id}",
            )
        with self._lock:
            g, b, r = self.global_bucket, self._bank(authenticated_bank), self._route(route_id)
            if rate_limiting:
                if not g.available():
                    return done(
                        False,
                        ErrorCode.GLOBAL_CAPACITY_EXCEEDED,
                        "global status-check ceiling",
                        g.retry_after(),
                    )
                if not b.available():
                    return done(
                        False,
                        ErrorCode.RATE_LIMITED,
                        f"bank {authenticated_bank} rate limit",
                        b.retry_after(),
                    )
                if not r.available():
                    return done(
                        False,
                        ErrorCode.ROUTE_RATE_LIMITED,
                        f"route {route_id} rate limit",
                        r.retry_after(),
                    )
            if credits and not self.credits.available(authenticated_bank):
                return done(
                    False,
                    ErrorCode.CREDIT_EXHAUSTED,
                    f"bank {authenticated_bank} Tier 2 credits exhausted",
                    self.credits.retry_after(authenticated_bank),
                )
            if rate_limiting:
                g.take()
                b.take()
                r.take()
            if credits:
                self.credits.consume(authenticated_bank)
            return done(True)

    def quota_view(self, bank_id: str) -> dict:
        with self._lock:
            b = self._bank(bank_id)
            b.refill()
            return {
                "bank_id": bank_id,
                "bank_tokens": round(b.tokens, 3),
                "bank_rate_per_sec": b.rate,
                **self.credits.view(bank_id),
            }

    def reset(self) -> None:
        with self._lock:
            self.bank_buckets.clear()
            self.route_buckets.clear()
            self.global_bucket.tokens = self.global_bucket.capacity
            self.credits.reset()
