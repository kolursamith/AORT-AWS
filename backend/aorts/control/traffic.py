"""Synthetic payment traffic: steady load, bursts and customer retries (same idempotency key).

All accounts are synthetic (``A1-CUST-0001`` ...); payments only go to the mock remitter.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from backend.aorts.app.metrics import percentile


@dataclass
class TrafficConfig:
    rps: float = 5.0
    duration_s: float = 30.0
    banks: tuple[str, ...] = ("A1", "A2")
    destinations: tuple[str, ...] = ("B", "C")
    customers_per_bank: int = 10
    min_amount_paise: int = 1_000
    max_amount_paise: int = 500_000
    retry_probability: float = 0.2  # customer re-submits an ambiguous payment with the SAME key
    burst_every_s: float = 0.0  # 0 = no bursts
    burst_multiplier: float = 3.0
    seed: int = 42
    concurrency: int = 32


@dataclass
class TrafficStats:
    sent: int = 0
    outcomes: Counter = field(default_factory=Counter)
    latencies_ms: list = field(default_factory=list)
    retries: int = 0
    replayed: int = 0
    by_destination: Counter = field(default_factory=Counter)

    def summary(self) -> dict:
        return {
            "sent": self.sent,
            "outcomes": dict(self.outcomes),
            "retries": self.retries,
            "idempotent_replays": self.replayed,
            "by_destination": dict(self.by_destination),
            "latency_p50_ms": _r(percentile(self.latencies_ms, 0.5)),
            "latency_p95_ms": _r(percentile(self.latencies_ms, 0.95)),
        }


def _r(x):
    return None if x is None else round(x, 2)


class TrafficGenerator:
    def __init__(self, client, cfg: TrafficConfig, correlation_id: str | None = None):
        self.client = client
        self.cfg = cfg
        self.correlation_id = correlation_id
        self.stats = TrafficStats()
        self._rng = random.Random(cfg.seed)
        # Accounts/amounts are seeded (reproducible workload) but idempotency keys must be unique
        # per run: reusing keys across runs would silently turn new payments into idempotent replays.
        self.run_nonce = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _one(self, body: dict, retry: bool = False) -> None:
        start = time.perf_counter()
        try:
            r = self.client.post(
                "remitter", "/api/v1/payments", body, as_identity="CHANNEL", timeout=10.0
            )
            ok = r.status_code == 200
            data = r.json() if r.content else {}
            state = (
                data["transaction"]["state"] if ok else data.get("error_code", str(r.status_code))
            )
            replayed = ok and data.get("replayed")
        except Exception as exc:  # noqa: BLE001
            state, replayed = type(exc).__name__, False
        with self._lock:
            self.stats.sent += 1
            self.stats.outcomes[state] += 1
            self.stats.latencies_ms.append((time.perf_counter() - start) * 1000)
            self.stats.replayed += int(bool(replayed))
            self.stats.retries += int(retry)
            do_retry = (
                not retry
                and state == "AMBIGUOUS"
                and self._rng.random() < self.cfg.retry_probability
            )
        if do_retry:
            self._one(body, retry=True)

    def _body(self) -> dict:
        c = self.cfg
        with self._lock:
            bank = self._rng.choice(c.banks)
            dest = self._rng.choice(c.destinations)
            body = {
                "sender_account_id": f"{bank}-CUST-{self._rng.randint(1, c.customers_per_bank):04d}",
                "beneficiary_account_id": f"{dest}-CUST-{self._rng.randint(1, c.customers_per_bank):04d}",
                "beneficiary_bank_id": dest,
                "amount_paise": self._rng.randint(c.min_amount_paise, c.max_amount_paise),
                "idempotency_key": f"load-{self.run_nonce}-{uuid.UUID(int=self._rng.getrandbits(128)).hex[:20]}",
            }
            self.stats.by_destination[dest] += 1
        if self.correlation_id:
            body["correlation_id"] = self.correlation_id
        return body

    def run(self) -> dict:
        c = self.cfg
        t0 = time.perf_counter()
        next_t = t0
        with ThreadPoolExecutor(max_workers=c.concurrency) as pool:
            while not self._stop.is_set() and time.perf_counter() - t0 < c.duration_s:
                elapsed = time.perf_counter() - t0
                rate = c.rps
                if c.burst_every_s and int(elapsed) % int(c.burst_every_s) == 0 and elapsed > 1:
                    rate *= c.burst_multiplier
                pool.submit(self._one, self._body())
                next_t += 1.0 / rate
                delay = next_t - time.perf_counter()
                if delay > 0:
                    self._stop.wait(delay)
        return {
            "config": asdict(c),
            **self.stats.summary(),
            "elapsed_s": round(time.perf_counter() - t0, 2),
        }

    def stop(self) -> None:
        self._stop.set()
