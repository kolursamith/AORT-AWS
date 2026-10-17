"""Seeded synthetic workload scenarios for strategy comparison (extends control/traffic.py).

A scenario is a timeline of fault phases plus a traffic profile and optional concurrent attacks:

* traffic bursts and customer retries (same idempotency key)       -> TrafficConfig;
* delayed confirmations / lost responses / route degradation        -> per-phase FaultConfig;
* one degraded route while another stays healthy                    -> faults on beneficiary:B only;
* duplicate events (at-least-once delivery)                         -> ``duplicate_job_prob``: dispatch
                                                                       jobs delivered twice;
* high-volume status-check flooding                                 -> A3 open-loop flood;
* false / manipulated capacity telemetry                            -> compromised probe twin-probe-2.

Every random choice derives from the run seed, so a seed reproduces the same workload for every
strategy (timing jitter of a real multi-threaded stack is the remaining, unavoidable noise).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Phase:
    name: str
    duration_s: float
    faults: list[dict] = field(default_factory=list)  # FaultConfig dicts; [] = healthy
    flood_rps: float = 0.0
    poison: bool = False


@dataclass
class Scenario:
    name: str
    description: str
    phases: list[Phase]
    rps: float = 6.0
    burst_every_s: float = 4.0
    burst_multiplier: float = 3.0
    retry_probability: float = 0.2
    duplicate_job_prob: float = 0.1
    held_out: bool = False  # never used while developing/tuning AORTS

    @property
    def traffic_s(self) -> float:
        return sum(p.duration_s for p in self.phases)

    def as_dict(self) -> dict:
        return asdict(self)


B_DEGRADED = {
    "target": "beneficiary:B",
    "latency_ms": 200,
    "error_rate": 0.5,
    "drop_response_rate": 0.4,
    "note": "Bank B degraded: slow, failing, lost/delayed confirmations",
}

SCENARIOS = {
    "S1-degraded-B": Scenario(
        "S1-degraded-B",
        "Bank B degraded for 10 s while Bank C stays healthy; bursts, retries, duplicate jobs",
        [Phase("warmup", 3.0), Phase("degraded", 10.0, [B_DEGRADED]), Phase("recovered", 5.0)],
    ),
    "S2-degraded-B-flood-poison": Scenario(
        "S2-degraded-B-flood-poison",
        "S1 plus an A3 status-check flood (80 req/s) and false high-capacity "
        "telemetry from a compromised probe during the degradation",
        [
            Phase("warmup", 3.0),
            Phase("degraded", 10.0, [B_DEGRADED], flood_rps=80.0, poison=True),
            Phase("recovered", 5.0),
        ],
    ),
    "S3-heldout-relapse": Scenario(
        "S3-heldout-relapse",
        "HELD-OUT combination: B degrades, recovers, relapses with a different fault "
        "mix; C mildly slow; poisoning throughout; heavier bursts",
        [
            Phase("warmup", 2.0),
            Phase(
                "degraded-1",
                6.0,
                [B_DEGRADED, {"target": "beneficiary:C", "latency_ms": 50}],
                poison=True,
            ),
            Phase("respite", 3.0, [{"target": "beneficiary:C", "latency_ms": 50}], poison=True),
            Phase(
                "relapse",
                6.0,
                [
                    {
                        "target": "beneficiary:B",
                        "latency_ms": 350,
                        "error_rate": 0.2,
                        "drop_response_rate": 0.6,
                    },
                    {"target": "beneficiary:C", "latency_ms": 50},
                ],
                flood_rps=40.0,
                poison=True,
            ),
            Phase("recovered", 3.0),
        ],
        rps=7.0,
        burst_every_s=3.0,
        burst_multiplier=4.0,
        held_out=True,
    ),
}
