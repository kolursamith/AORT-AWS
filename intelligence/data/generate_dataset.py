"""Deterministic synthetic workload generation for AORTS research (Stage 3A).

A ``ScenarioConfig`` and its seed fully determine a ``Workload``. The workload is split into:

1. ``ExogenousWorkload``: payment arrivals, exogenous status-check demand, and each payment's
   pre-drawn customer behaviour (would the customer re-pay or re-submit if they saw a pending
   payment, and after how long). These do not depend on any recovery policy.
2. ``GroundTruth``: the hidden environment - true route capacity over time, fault windows and
   each payment's beneficiary outcome (credited or not, intrinsic latency, lost response,
   delayed confirmation).
3. ``ControllerView``: what a Tier 1 controller may legitimately know - route descriptors, the
   timing policy and its own bank's payment requests, revealed only up to a given time. It holds
   no telemetry, no capacity and no outcome.

Ambiguity that a payment would suffer whatever the load (lost response, confirmation later than
the client timeout) is labelled from the ground-truth mechanics. Load-induced ambiguity, every
status check AORTS or a baseline sends, and whether a customer actually re-pays all depend on the
simulated system and are left to the Stage 3B simulator, which applies the pre-drawn customer
behaviour only when its own dynamics make a payment ambiguous.

Randomness: every stream is a ``random.Random`` seeded from SHA-256 of (seed, stream name), with
one stream per route and purpose and a fixed number of draws per payment. Policies therefore see
identical external workloads, and adding a route never perturbs another route's draws. Only
``random()`` is used (distributions are derived explicitly), because CPython guarantees that
method's sequence for a given seed. The wall clock and the global random state are never used.

Nothing here executes a payment or touches a ledger. All data is synthetic.
"""

import argparse
import hashlib
import json
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from contracts.v1_1 import RouteDescriptor, TimingPolicy
from contracts.v1_1.models import Identifier, NonNegativeFloat, PositiveInt, UtcDatetime
from intelligence.simulation.scenarios import (
    FAULT_UNITS,
    SYNTHETIC_LABEL,
    AmountDistribution,
    FaultKind,
    RouteSpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
)

GENERATOR_VERSION = "stage3a-1"
WORKLOAD_FORMAT = "aorts-synthetic-workload/1"


class WorkloadTooLargeError(ValueError):
    """The configuration would exceed ``max_records``; reduce the scale or the duration."""


class WorkloadIntegrityError(ValueError):
    """Serialized workload content does not match its manifest hashes."""


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# ------------------------------------------------------------------ records


class PaymentArrival(RecordModel):
    """A payment request as the remitter bank itself knows it. Contains no outcome."""

    record_type: Literal["payment_arrival"] = "payment_arrival"
    experiment_id: Identifier
    sequence: int = Field(ge=0, strict=True)
    transaction_id: Identifier
    idempotency_key: Identifier
    route_id: Identifier
    remitter_bank_id: Identifier
    beneficiary_bank_id: Identifier
    amount_minor: PositiveInt
    currency: Literal["INR"] = "INR"
    offset_s: NonNegativeFloat
    arrival_at: UtcDatetime


class CustomerBehaviour(RecordModel):
    """Exogenous pre-drawn customer intent; applied by the simulator only if a payment is seen
    as pending. Hidden from the controller."""

    record_type: Literal["customer_behaviour"] = "customer_behaviour"
    experiment_id: Identifier
    transaction_id: Identifier
    would_repay: StrictBool
    repay_delay_s: NonNegativeFloat
    would_resubmit_same_key: StrictBool
    resubmit_delay_s: NonNegativeFloat


class CheckDemandArrival(RecordModel):
    """One exogenous status check sent by a party AORTS does not control."""

    record_type: Literal["check_demand"] = "check_demand"
    experiment_id: Identifier
    sequence: int = Field(ge=0, strict=True)
    check_id: Identifier
    demand_id: Identifier
    bank_id: Identifier
    route_id: Identifier
    offset_s: NonNegativeFloat
    arrival_at: UtcDatetime


class IntrinsicAmbiguity(StrEnum):
    RESPONSE_LOST = "RESPONSE_LOST"
    CONFIRMATION_LATE = "CONFIRMATION_LATE"


class BeneficiaryTruth(RecordModel):
    """Hidden truth about one payment at the beneficiary. Never visible to the controller."""

    record_type: Literal["beneficiary_truth"] = "beneficiary_truth"
    experiment_id: Identifier
    transaction_id: Identifier
    route_id: Identifier
    credited: StrictBool
    beneficiary_latency_ms: NonNegativeFloat
    response_lost: StrictBool
    confirmation_delay_s: NonNegativeFloat
    intrinsic_ambiguity: IntrinsicAmbiguity | None
    active_fault_ids: tuple[Identifier, ...]


class FaultEvent(RecordModel):
    record_type: Literal["fault_event"] = "fault_event"
    experiment_id: Identifier
    fault_id: Identifier
    route_id: Identifier
    kind: FaultKind
    magnitude: NonNegativeFloat
    unit: str
    start_offset_s: NonNegativeFloat
    end_offset_s: NonNegativeFloat
    start_at: UtcDatetime
    end_at: UtcDatetime


class CapacityChange(RecordModel):
    """True route capacity from ``offset_s`` until the next change (requests per second)."""

    record_type: Literal["capacity_change"] = "capacity_change"
    experiment_id: Identifier
    route_id: Identifier
    offset_s: NonNegativeFloat
    effective_at: UtcDatetime
    capacity_rps: NonNegativeFloat
    cause: Identifier


class ExogenousWorkload(RecordModel):
    payments: tuple[PaymentArrival, ...]
    customer_behaviour: tuple[CustomerBehaviour, ...]
    check_demands: tuple[CheckDemandArrival, ...]


class GroundTruth(RecordModel):
    beneficiary: tuple[BeneficiaryTruth, ...]
    faults: tuple[FaultEvent, ...]
    capacity: tuple[CapacityChange, ...]


class WorkloadManifest(RecordModel):
    record_type: Literal["manifest"] = "manifest"
    format: Literal["aorts-synthetic-workload/1"] = WORKLOAD_FORMAT
    synthetic: Literal[True] = True
    label: Literal["SYNTHETIC RESEARCH WORKLOAD - not real UPI, NPCI or bank traffic"] = (
        SYNTHETIC_LABEL
    )
    generator_version: str
    experiment_id: Identifier
    run_id: Identifier
    scenario_id: Identifier
    seed: int = Field(ge=0, strict=True)
    stream_names: tuple[str, ...]
    config: ScenarioConfig
    config_hash: str
    content_hash: str
    record_counts: tuple[tuple[str, int], ...]


class ControllerView(RecordModel):
    """Everything a Tier 1 controller may know before any simulation runs."""

    experiment_id: Identifier
    run_id: Identifier
    scenario_id: Identifier
    synthetic_label: str
    routes: tuple[RouteDescriptor, ...]
    timing_policy: TimingPolicy
    payments: tuple[PaymentArrival, ...]

    def payments_until(self, at: datetime) -> tuple[PaymentArrival, ...]:
        """Payments that have arrived by ``at``; future requests stay hidden."""

        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        return tuple(p for p in self.payments if p.arrival_at <= at)


@dataclass(frozen=True)
class TimelineEntry:
    offset_s: float
    at: datetime
    kind: str
    key: str
    record: RecordModel


_RECORD_TYPES: dict[str, type[RecordModel]] = {
    "payment_arrival": PaymentArrival,
    "customer_behaviour": CustomerBehaviour,
    "check_demand": CheckDemandArrival,
    "beneficiary_truth": BeneficiaryTruth,
    "fault_event": FaultEvent,
    "capacity_change": CapacityChange,
}


class Workload(RecordModel):
    manifest: WorkloadManifest
    exogenous: ExogenousWorkload
    ground_truth: GroundTruth

    def records(self) -> Iterator[RecordModel]:
        """All records in a fixed, deterministic order."""

        yield from self.exogenous.payments
        yield from self.exogenous.customer_behaviour
        yield from self.exogenous.check_demands
        yield from self.ground_truth.beneficiary
        yield from self.ground_truth.faults
        yield from self.ground_truth.capacity

    def controller_view(self) -> ControllerView:
        config = self.manifest.config
        return ControllerView(
            experiment_id=self.manifest.experiment_id,
            run_id=self.manifest.run_id,
            scenario_id=self.manifest.scenario_id,
            synthetic_label=SYNTHETIC_LABEL,
            routes=config.descriptors(),
            timing_policy=config.timing_policy,
            payments=self.exogenous.payments,
        )

    def timeline(self) -> tuple[TimelineEntry, ...]:
        """Simulation events ordered by time, then kind, then key."""

        rank = {"capacity_change": 0, "fault_start": 1, "fault_end": 1, "payment": 2, "check": 3}
        entries = [
            TimelineEntry(c.offset_s, c.effective_at, "capacity_change", c.route_id, c)
            for c in self.ground_truth.capacity
        ]
        for f in self.ground_truth.faults:
            entries.append(
                TimelineEntry(f.start_offset_s, f.start_at, "fault_start", f.fault_id, f)
            )
            entries.append(TimelineEntry(f.end_offset_s, f.end_at, "fault_end", f.fault_id, f))
        entries += [
            TimelineEntry(p.offset_s, p.arrival_at, "payment", p.transaction_id, p)
            for p in self.exogenous.payments
        ]
        entries += [
            TimelineEntry(c.offset_s, c.arrival_at, "check", c.check_id, c)
            for c in self.exogenous.check_demands
        ]
        return tuple(sorted(entries, key=lambda e: (e.offset_s, rank[e.kind], e.key)))

    def to_jsonl_lines(self) -> list[str]:
        return [self.manifest.model_dump_json()] + [r.model_dump_json() for r in self.records()]

    @classmethod
    def from_jsonl_lines(cls, lines: list[str]) -> "Workload":
        if not lines:
            raise WorkloadIntegrityError("empty workload")
        manifest = WorkloadManifest.model_validate_json(lines[0])
        buckets: dict[str, list[RecordModel]] = {name: [] for name in _RECORD_TYPES}
        for line in lines[1:]:
            record_type = json.loads(line).get("record_type")
            if record_type not in _RECORD_TYPES:
                raise WorkloadIntegrityError(f"unknown record_type {record_type!r}")
            buckets[record_type].append(_RECORD_TYPES[record_type].model_validate_json(line))
        workload = cls(
            manifest=manifest,
            exogenous=ExogenousWorkload(
                payments=tuple(buckets["payment_arrival"]),
                customer_behaviour=tuple(buckets["customer_behaviour"]),
                check_demands=tuple(buckets["check_demand"]),
            ),
            ground_truth=GroundTruth(
                beneficiary=tuple(buckets["beneficiary_truth"]),
                faults=tuple(buckets["fault_event"]),
                capacity=tuple(buckets["capacity_change"]),
            ),
        )
        if _content_hash(manifest.config, workload.records()) != manifest.content_hash:
            raise WorkloadIntegrityError("record content does not match manifest.content_hash")
        return workload


# ------------------------------------------------------------------ randomness


def _stream(seed: int, name: str) -> random.Random:
    digest = hashlib.sha256(f"aorts-workload|{seed}|{name}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 - simulation only


def _exponential(rng: random.Random, rate: float) -> float:
    return -math.log(1.0 - rng.random()) / rate


def _standard_normal(rng: random.Random) -> float:
    u1, u2 = rng.random(), rng.random()  # always two draws (Box-Muller)
    return math.sqrt(-2.0 * math.log(1.0 - u1)) * math.cos(2.0 * math.pi * u2)


# ------------------------------------------------------------------ generation


def _at(config: ScenarioConfig, offset_s: float) -> datetime:
    return config.epoch + timedelta(seconds=offset_s)


def _rate_segments(config: ScenarioConfig, route: RouteSpec) -> list[tuple[float, float, float]]:
    duration, base, bursts = config.duration_s, route.payment_rps, config.bursts
    if bursts is None or (bursts.route_ids and route.route_id not in bursts.route_ids):
        return [(0.0, duration, base)]
    segments, k = [], 0
    while k * bursts.every_s < duration:
        start = k * bursts.every_s
        burst_end = min(start + bursts.duration_s, duration)
        window_end = min(start + bursts.every_s, duration)
        segments.append((start, burst_end, base * bursts.multiplier))
        if burst_end < window_end:
            segments.append((burst_end, window_end, base))
        k += 1
    return segments


def _expected_records(config: ScenarioConfig) -> float:
    payments = sum(
        (end - start) * rate
        for route in config.routes
        for start, end, rate in _rate_segments(config, route)
    )
    checks = sum(
        (min(d.end_s, config.duration_s) - d.start_s) * d.rate_rps for d in config.check_demand
    )
    return 3 * payments + checks  # arrival, behaviour and truth per payment


def _amount(config: ScenarioConfig, rng: random.Random) -> int:
    spec = config.amount
    u1, u2 = rng.random(), rng.random()  # always two draws
    if spec.distribution is AmountDistribution.UNIFORM:
        value = spec.min_minor + math.floor(u1 * (spec.max_minor - spec.min_minor + 1))
    else:
        z = math.sqrt(-2.0 * math.log(1.0 - u1)) * math.cos(2.0 * math.pi * u2)
        value = round(spec.median_minor * math.exp(spec.sigma * z))
    return int(min(spec.max_minor, max(spec.min_minor, value)))


def _beneficiary(
    config: ScenarioConfig, route: RouteSpec, txn: str, offset_s: float, rng: random.Random
) -> BeneficiaryTruth:
    active = [f for f in config.faults if f.route_id == route.route_id and f.active(offset_s)]
    keep_reject, keep_loss, latency_add, confirm = 1.0, 1.0, 0.0, 0.0
    keep_reject *= 1.0 - config.beneficiary.base_rejection_prob
    keep_loss *= 1.0 - config.beneficiary.base_loss_prob
    for fault in active:
        if fault.kind is FaultKind.REJECTION:
            keep_reject *= 1.0 - fault.magnitude
        elif fault.kind is FaultKind.RESPONSE_LOSS:
            keep_loss *= 1.0 - fault.magnitude
        elif fault.kind is FaultKind.LATENCY:
            latency_add += fault.magnitude
        elif fault.kind is FaultKind.CONFIRMATION_DELAY:
            confirm = max(confirm, fault.magnitude)
    u_reject = rng.random()
    z = _standard_normal(rng)
    u_loss, u_jitter = rng.random(), rng.random()  # fixed five draws per payment

    latency_ms = route.beneficiary_median_latency_ms * math.exp(
        config.beneficiary.latency_sigma * z
    )
    latency_ms += latency_add
    response_lost = u_loss < 1.0 - keep_loss
    confirmation_delay = confirm * (0.9 + 0.2 * u_jitter) if confirm > 0 else 0.0
    if response_lost:
        intrinsic = IntrinsicAmbiguity.RESPONSE_LOST
    elif latency_ms / 1000.0 + confirmation_delay >= config.timing_policy.client_timeout_s:
        intrinsic = IntrinsicAmbiguity.CONFIRMATION_LATE
    else:
        intrinsic = None
    return BeneficiaryTruth(
        experiment_id=config.experiment_id,
        transaction_id=txn,
        route_id=route.route_id,
        credited=u_reject >= 1.0 - keep_reject,
        beneficiary_latency_ms=latency_ms,
        response_lost=response_lost,
        confirmation_delay_s=confirmation_delay,
        intrinsic_ambiguity=intrinsic,
        active_fault_ids=tuple(sorted(f.fault_id for f in active)),
    )


def _customer(config: ScenarioConfig, txn: str, rng: random.Random) -> CustomerBehaviour:
    u_repay, u_repay_delay = rng.random(), rng.random()
    u_repeat, u_repeat_delay = rng.random(), rng.random()  # fixed four draws per payment
    return CustomerBehaviour(
        experiment_id=config.experiment_id,
        transaction_id=txn,
        would_repay=u_repay < config.repay.probability,
        repay_delay_s=-math.log(1.0 - u_repay_delay) * config.repay.mean_delay_s,
        would_resubmit_same_key=u_repeat < config.repeat.probability,
        resubmit_delay_s=-math.log(1.0 - u_repeat_delay) * config.repeat.mean_delay_s,
    )


def _capacity_changes(config: ScenarioConfig, route: RouteSpec) -> list[CapacityChange]:
    cuts = [
        (f.start_s, f.end_s, f.magnitude, f.fault_id)
        for f in config.faults
        if f.route_id == route.route_id and f.kind is FaultKind.CAPACITY_CUT
    ]
    for flap in (f for f in config.flapping if f.route_id == route.route_id):
        k = 0
        while flap.start_s + k * flap.period_s < flap.end_s:
            start = flap.start_s + k * flap.period_s
            end = min(start + flap.dip_duration_s, flap.end_s)
            cuts.append((start, end, flap.dip_fraction, f"{flap.flapping_id}.dip{k}"))
            k += 1
    points = sorted({0.0, *(c[0] for c in cuts), *(c[1] for c in cuts)})
    changes: list[CapacityChange] = []
    previous: float | None = None
    for point in (p for p in points if p < config.duration_s):
        cut, cause = max(
            ((m, cid) for start, end, m, cid in cuts if start <= point < end),
            default=(0.0, "baseline" if previous is None else "restored"),
        )
        capacity = route.capacity_rps * (1.0 - cut)
        if capacity != previous:
            changes.append(
                CapacityChange(
                    experiment_id=config.experiment_id,
                    route_id=route.route_id,
                    offset_s=point,
                    effective_at=_at(config, point),
                    capacity_rps=capacity,
                    cause=cause,
                )
            )
        previous = capacity
    return changes


def _content_hash(config: ScenarioConfig, records) -> str:
    digest = hashlib.sha256(config.model_dump_json(exclude={"experiment_id"}).encode())
    for record in records:
        digest.update(b"\n")
        digest.update(record.model_dump_json(exclude={"experiment_id"}).encode())
    return digest.hexdigest()


def generate_workload(config: ScenarioConfig) -> Workload:
    """Generate the complete synthetic workload for ``config``. Pure and deterministic."""

    if not isinstance(config, ScenarioConfig):
        raise TypeError("config must be a ScenarioConfig")
    expected = _expected_records(config)
    if expected > config.max_records:
        raise WorkloadTooLargeError(
            f"about {expected:,.0f} records expected, above max_records={config.max_records:,}"
        )
    streams: list[str] = []

    def stream(name: str) -> random.Random:
        streams.append(name)
        return _stream(config.seed, name)

    staged: list[tuple[float, str, int, PaymentArrival, CustomerBehaviour, BeneficiaryTruth]] = []
    for route in config.routes:
        arrivals_rng = stream(f"arrivals:{route.route_id}")
        amounts_rng = stream(f"amounts:{route.route_id}")
        beneficiary_rng = stream(f"beneficiary:{route.route_id}")
        customer_rng = stream(f"customer:{route.route_id}")
        local = 0
        for start, end, rate in _rate_segments(config, route):
            if rate <= 0:
                continue
            offset = start
            while True:
                offset += _exponential(arrivals_rng, rate)
                if offset >= end:
                    break
                txn = f"TXN-{route.route_id}-{local:09d}"
                arrival = PaymentArrival(
                    experiment_id=config.experiment_id,
                    sequence=0,  # assigned after all routes are merged
                    transaction_id=txn,
                    idempotency_key=f"PAY-{route.route_id}-{local:09d}",
                    route_id=route.route_id,
                    remitter_bank_id=route.remitter_bank_id,
                    beneficiary_bank_id=route.beneficiary_bank_id,
                    amount_minor=_amount(config, amounts_rng),
                    currency=config.currency,
                    offset_s=offset,
                    arrival_at=_at(config, offset),
                )
                staged.append(
                    (
                        offset,
                        route.route_id,
                        local,
                        arrival,
                        _customer(config, txn, customer_rng),
                        _beneficiary(config, route, txn, offset, beneficiary_rng),
                    )
                )
                local += 1
                if 3 * len(staged) > config.max_records:
                    raise WorkloadTooLargeError("max_records exceeded during generation")
    staged.sort(key=lambda item: (item[0], item[1], item[2]))
    payments = tuple(item[3].model_copy(update={"sequence": n}) for n, item in enumerate(staged))

    checks_staged: list[tuple[float, str, int]] = []
    for demand in config.check_demand:
        rng = stream(f"checks:{demand.demand_id}")
        end = min(demand.end_s, config.duration_s)
        offset, n = demand.start_s, 0
        while demand.rate_rps > 0:
            offset += _exponential(rng, demand.rate_rps)
            if offset >= end:
                break
            checks_staged.append((offset, demand.demand_id, n))
            n += 1
    checks_staged.sort()
    by_id = {d.demand_id: d for d in config.check_demand}
    checks = tuple(
        CheckDemandArrival(
            experiment_id=config.experiment_id,
            sequence=seq,
            check_id=f"CHK-{demand_id}-{n:09d}",
            demand_id=demand_id,
            bank_id=by_id[demand_id].bank_id,
            route_id=by_id[demand_id].route_id,
            offset_s=offset,
            arrival_at=_at(config, offset),
        )
        for seq, (offset, demand_id, n) in enumerate(checks_staged)
    )

    faults = tuple(
        FaultEvent(
            experiment_id=config.experiment_id,
            fault_id=f.fault_id,
            route_id=f.route_id,
            kind=f.kind,
            magnitude=f.magnitude,
            unit=FAULT_UNITS[f.kind],
            start_offset_s=f.start_s,
            end_offset_s=f.end_s,
            start_at=_at(config, f.start_s),
            end_at=_at(config, f.end_s),
        )
        for f in sorted(config.faults, key=lambda f: (f.start_s, f.fault_id))
    )
    capacity = tuple(c for route in config.routes for c in _capacity_changes(config, route))
    exogenous = ExogenousWorkload(
        payments=payments,
        customer_behaviour=tuple(item[4] for item in staged),
        check_demands=checks,
    )
    truth = GroundTruth(
        beneficiary=tuple(item[5] for item in staged), faults=faults, capacity=capacity
    )
    placeholder = Workload.model_construct(exogenous=exogenous, ground_truth=truth)
    content_hash = _content_hash(config, Workload.records(placeholder))
    manifest = WorkloadManifest(
        generator_version=GENERATOR_VERSION,
        experiment_id=config.experiment_id,
        run_id=f"run.{content_hash[:16]}.seed{config.seed}",
        scenario_id=config.scenario_id,
        seed=config.seed,
        stream_names=tuple(streams),
        config=config,
        config_hash=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        content_hash=content_hash,
        record_counts=(
            ("payment_arrival", len(payments)),
            ("customer_behaviour", len(staged)),
            ("check_demand", len(checks)),
            ("beneficiary_truth", len(staged)),
            ("fault_event", len(faults)),
            ("capacity_change", len(capacity)),
        ),
    )
    return Workload(manifest=manifest, exogenous=exogenous, ground_truth=truth)


# ------------------------------------------------------------------ files


def write_workload(workload: Workload, directory: Path) -> Path:
    """Write ``workload.jsonl`` (manifest line, then records) and ``manifest.json``."""

    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join(workload.to_jsonl_lines()) + "\n"
    (directory / "workload.jsonl").write_text(body, encoding="utf-8")
    summary = {
        "label": SYNTHETIC_LABEL,
        "run_id": workload.manifest.run_id,
        "experiment_id": workload.manifest.experiment_id,
        "scenario_id": workload.manifest.scenario_id,
        "seed": workload.manifest.seed,
        "content_hash": workload.manifest.content_hash,
        "workload_jsonl_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "record_counts": dict(workload.manifest.record_counts),
    }
    (directory / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    return directory / "workload.jsonl"


def read_workload(directory: Path) -> Workload:
    body = (directory / "workload.jsonl").read_text(encoding="utf-8")
    summary = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(body.encode()).hexdigest() != summary["workload_jsonl_sha256"]:
        raise WorkloadIntegrityError("workload.jsonl does not match manifest.json")
    return Workload.from_jsonl_lines(body.splitlines())


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic AORTS research workload.")
    parser.add_argument("--scenario", choices=["S1", "S2", "S3"], default="S2")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--experiment-id", default="local-research")
    parser.add_argument("--rate-scale", type=float, default=0.01)
    parser.add_argument("--duration-s", type=float, default=2400.0)
    parser.add_argument(
        "--option", action="append", default=[], choices=list(ScenarioOptions.model_fields)
    )
    parser.add_argument("--output", type=Path, default=Path("results/raw/workloads"))
    args = parser.parse_args()
    config = reference_scenario(
        args.scenario,
        seed=args.seed,
        experiment_id=args.experiment_id,
        options=ScenarioOptions(**dict.fromkeys(args.option, True)),
        rate_scale=args.rate_scale,
        duration_s=args.duration_s,
    )
    workload = generate_workload(config)
    path = write_workload(workload, args.output / workload.manifest.run_id)
    print(
        json.dumps({"path": str(path), **json.loads(path.with_name("manifest.json").read_text())})
    )


if __name__ == "__main__":
    main()
