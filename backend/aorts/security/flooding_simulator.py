"""Attack 1: status-check flooding by a (simulated) compromised remitter bank.

Open-loop load: a scheduler fires requests at fixed rates regardless of how fast
the switch answers (that is what makes flooding dangerous). Bank A3 floods;
banks A1/A2 send ordinary status checks at a modest rate at the same time. Every
request is a real signed HTTP call to the (isolated, mock) switch.

Containment criterion (explicit): the first 1-second window, starting at or after
attack start, in which A3's *admitted* requests are <= ``containment_factor`` x
its configured per-bank rate limit, and every later window also satisfies it.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from backend.aorts.app.metrics import percentile
from backend.aorts.security.attack_config import clamp_rate


@dataclass
class FloodConfig:
    attacker: str = "A3"
    attack_rps: float = 50.0
    duration_s: float = 5.0
    legit_banks: tuple[str, ...] = ("A1", "A2")
    legit_rps_per_bank: float = 3.0
    destination: str = "B"
    concurrency: int = 64
    seed: int = 42
    spoof_bank_id: bool = False  # attacker claims to be A1 while signing as A3
    containment_factor: float = 1.5
    tail_s: float = 2.0  # keep legit traffic running after the attack stops (recovery window)
    lead_s: float = 0.0  # legit traffic alone before the attack starts (baseline window)


@dataclass
class RequestRecord:
    bank: str
    kind: str
    t: float
    latency_ms: float
    status: int
    code: str | None


@dataclass
class FloodResult:
    config: dict
    records: list[RequestRecord] = field(default_factory=list)

    def summary(self, bank_limit: float | None = None) -> dict:
        by_bank: dict[str, list[RequestRecord]] = defaultdict(list)
        for r in self.records:
            by_bank[r.bank].append(r)
        out = {}
        for bank, recs in sorted(by_bank.items()):
            ok = [r for r in recs if r.status == 200]
            codes = defaultdict(int)
            for r in recs:
                codes[r.code or str(r.status)] += 1
            lat = [r.latency_ms for r in ok]
            out[bank] = {
                "sent": len(recs),
                "admitted_ok": len(ok),
                "admitted_ratio": round(len(ok) / len(recs), 4) if recs else None,
                "outcomes": dict(codes),
                "latency_p50_ms": _r(percentile(lat, 0.5)),
                "latency_p95_ms": _r(percentile(lat, 0.95)),
            }
        attacker = self.config["attacker"]
        att = sorted((r for r in self.records if r.bank == attacker), key=lambda r: r.t)
        lead = self.config.get("lead_s", 0.0)  # attack times are reported relative to attack start
        detection = next((r.t - lead for r in att if r.status == 429), None)
        containment = None
        if bank_limit and att:
            windows: dict[int, int] = defaultdict(int)
            for r in att:
                if r.status == 200:
                    windows[int(r.t - lead)] += 1
            last_window = int(max(r.t - lead for r in att))
            threshold = bank_limit * self.config["containment_factor"]
            for w in range(0, last_window + 1):
                if all(windows.get(x, 0) <= threshold for x in range(w, last_window + 1)):
                    containment = float(w)
                    break
        legit = [r for r in self.records if r.bank != attacker]
        legit_ok = [r for r in legit if r.status == 200]
        recovery = self._recovery(legit)
        total_ok = len([r for r in self.records if r.status == 200])
        return {
            "per_bank": out,
            "attack_detection_s": _r(detection),
            "attack_containment_s": containment,
            "legit_success_ratio": round(len(legit_ok) / len(legit), 4) if legit else None,
            "legit_share_of_admitted": round(len(legit_ok) / total_ok, 4) if total_ok else None,
            **recovery,
        }

    def _recovery(self, legit: list[RequestRecord]) -> dict:
        """E7: time from attack stop until legit checks are healthy again and stay healthy.

        Baseline = median legit latency during the lead window (before the attack). A 1 s window is
        healthy when every legit request in it succeeded and its p95 <= max(2 x baseline, baseline + 50 ms).
        """
        lead, dur = self.config.get("lead_s", 0.0), self.config["duration_s"]
        base = [r.latency_ms for r in legit if r.t < lead and r.status == 200]
        if not base:
            return {"baseline_legit_ms": None, "recovery_after_attack_s": None}
        b = percentile(base, 0.5)
        limit = max(2 * b, b + 50.0)
        end = lead + dur
        windows: dict[int, list[RequestRecord]] = defaultdict(list)
        for r in legit:
            if r.t >= end:
                windows[int(r.t - end)].append(r)
        if not windows:
            return {"baseline_legit_ms": round(b, 2), "recovery_after_attack_s": None}
        last = max(windows)

        def healthy(w):
            rs = windows.get(w, [])
            return all(r.status == 200 for r in rs) and (
                not rs or percentile([r.latency_ms for r in rs], 0.95) <= limit
            )

        rec = next(
            (float(w) for w in range(last + 1) if all(healthy(x) for x in range(w, last + 1))), None
        )
        return {
            "baseline_legit_ms": round(b, 2),
            "recovery_after_attack_s": rec,
            "recovery_observed_window_s": last + 1,
        }


def _r(x):
    return None if x is None else round(x, 3)


def run_flood(client, txn_pool: dict[str, list[tuple[str, str]]], cfg: FloodConfig) -> FloodResult:
    """``client`` is a SignedClient; ``txn_pool`` maps bank -> [(transaction_id, route_id)]."""
    rps, duration = clamp_rate(cfg.attack_rps, cfg.duration_s)
    rng = random.Random(cfg.seed)
    result = FloodResult(config={**asdict(cfg), "attack_rps": rps, "duration_s": duration})
    lock = threading.Lock()
    t0 = time.perf_counter()

    def fire(bank: str, kind: str):
        txn_id, route_id = rng.choice(txn_pool[bank])
        claimed = "A1" if (cfg.spoof_bank_id and kind == "attack") else bank
        body = {
            "transaction_id": txn_id,
            "bank_id": claimed,
            "route_id": route_id if claimed == bank else f"{claimed}-{cfg.destination}",
        }
        start = time.perf_counter()
        try:
            resp = client.post(
                "switch", "/api/v1/check-status", body, as_identity=bank, timeout=5.0
            )
            status = resp.status_code
            code = (
                None if status == 200 else (resp.json().get("error_code") if resp.content else None)
            )
        except Exception as exc:  # noqa: BLE001 - client-side timeout counts as failure
            status, code = 599, type(exc).__name__
        rec = RequestRecord(
            bank, kind, start - t0, (time.perf_counter() - start) * 1000, status, code
        )
        with lock:
            result.records.append(rec)

    # attack_rps <= 0 is the no-attack control condition: only the legitimate banks send.
    streams = [(cfg.attacker, "attack", rps, duration, cfg.lead_s)] if cfg.attack_rps > 0 else []
    streams += [
        (b, "legit", cfg.legit_rps_per_bank, cfg.lead_s + duration + cfg.tail_s, 0.0)
        for b in cfg.legit_banks
    ]
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:

        def schedule(bank, kind, rate, dur, offset):
            interval = 1.0 / rate
            n = int(rate * dur)
            for i in range(n):
                target = t0 + offset + i * interval
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                pool.submit(fire, bank, kind)

        schedulers = [threading.Thread(target=schedule, args=s, daemon=True) for s in streams]
        for t in schedulers:
            t.start()
        for t in schedulers:
            t.join()
    return result
