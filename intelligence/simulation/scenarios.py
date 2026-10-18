"""Scenario configuration for synthetic AORTS research workloads (Stage 3A).

A ``ScenarioConfig`` fully determines a workload: route topology, exogenous arrival processes,
fault and capacity schedules, beneficiary behaviour, customer re-pay propensity, exogenous status
check demand and research timing. Generation lives in ``intelligence.data.generate_dataset``.

Everything here is synthetic. The ``reference_scenario`` presets reuse the Phase I report's
parameters (route load 700/s, capacity 1000/s, a 35 % capacity cut from minute 10 to 30, a 15 s
client timeout, 10 % re-pay after about 30 s, a 60-minute test deadline) as configurable research
parameters. They are not real UPI measurements and carry no expected results.

The legacy ``Scenario`` / ``HACKATHON_SCENARIO`` used by ``intelligence.simulation.engine`` is
kept unchanged at the end of this module.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from contracts.v1_1 import SCHEMA_VERSION, RouteDescriptor, TimingPolicy
from contracts.v1_1.models import (
    Identifier,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    Probability,
    UtcDatetime,
)

SYNTHETIC_LABEL = "SYNTHETIC RESEARCH WORKLOAD - not real UPI, NPCI or bank traffic"
REFERENCE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
REPORT_REFERENCE = (
    "Phase I report Table 12 parameters, used as configurable research parameters only"
)


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FaultKind(StrEnum):
    """Environment faults. Units: CAPACITY_CUT, RESPONSE_LOSS and REJECTION are fractions;
    LATENCY is added milliseconds; CONFIRMATION_DELAY is added seconds."""

    CAPACITY_CUT = "CAPACITY_CUT"
    LATENCY = "LATENCY"
    RESPONSE_LOSS = "RESPONSE_LOSS"
    CONFIRMATION_DELAY = "CONFIRMATION_DELAY"
    REJECTION = "REJECTION"


FAULT_UNITS: dict[FaultKind, str] = {
    FaultKind.CAPACITY_CUT: "fraction",
    FaultKind.LATENCY: "ms",
    FaultKind.RESPONSE_LOSS: "probability",
    FaultKind.CONFIRMATION_DELAY: "s",
    FaultKind.REJECTION: "probability",
}
_FRACTION_KINDS = frozenset({FaultKind.CAPACITY_CUT, FaultKind.RESPONSE_LOSS, FaultKind.REJECTION})


class RouteSpec(ConfigModel):
    route_id: Identifier
    remitter_bank_id: Identifier
    beneficiary_bank_id: Identifier
    switch_id: Identifier = "SW"
    payment_rps: NonNegativeFloat  # exogenous mean payment arrivals per second
    capacity_rps: PositiveFloat  # true processing capacity (ground truth, never visible)
    beneficiary_median_latency_ms: PositiveFloat = 120.0

    @model_validator(mode="after")
    def _distinct_banks(self) -> "RouteSpec":
        if self.remitter_bank_id == self.beneficiary_bank_id:
            raise ValueError("remitter_bank_id and beneficiary_bank_id must differ")
        return self

    def descriptor(self, experiment_id: str) -> RouteDescriptor:
        return RouteDescriptor(
            schema_version=SCHEMA_VERSION,
            experiment_id=experiment_id,
            route_id=self.route_id,
            remitter_bank_id=self.remitter_bank_id,
            switch_id=self.switch_id,
            beneficiary_bank_id=self.beneficiary_bank_id,
        )


class FaultSpec(ConfigModel):
    fault_id: Identifier
    route_id: Identifier
    kind: FaultKind
    start_s: NonNegativeFloat
    end_s: PositiveFloat
    magnitude: NonNegativeFloat

    @model_validator(mode="after")
    def _window(self) -> "FaultSpec":
        if self.end_s <= self.start_s:
            raise ValueError("fault end_s must be later than start_s")
        if self.kind in _FRACTION_KINDS and self.magnitude > 1:
            raise ValueError(f"{self.kind} magnitude is a fraction in [0, 1]")
        return self

    def active(self, offset_s: float) -> bool:
        return self.start_s <= offset_s < self.end_s


class FlappingSpec(ConfigModel):
    """Periodic capacity dips: ``dip_fraction`` lost for ``dip_duration_s`` every ``period_s``."""

    flapping_id: Identifier
    route_id: Identifier
    start_s: NonNegativeFloat
    end_s: PositiveFloat
    period_s: PositiveFloat
    dip_duration_s: PositiveFloat
    dip_fraction: Probability

    @model_validator(mode="after")
    def _shape(self) -> "FlappingSpec":
        if self.end_s <= self.start_s:
            raise ValueError("flapping end_s must be later than start_s")
        if self.dip_duration_s >= self.period_s:
            raise ValueError("dip_duration_s must be shorter than period_s")
        return self


class BurstSpec(ConfigModel):
    """Payment-rate multiplier for ``duration_s`` at the start of every ``every_s`` window."""

    every_s: PositiveFloat
    duration_s: PositiveFloat
    multiplier: float = Field(ge=1.0)
    route_ids: tuple[Identifier, ...] = ()  # empty: every route

    @model_validator(mode="after")
    def _shape(self) -> "BurstSpec":
        if self.duration_s >= self.every_s:
            raise ValueError("burst duration_s must be shorter than every_s")
        return self


class AmountDistribution(StrEnum):
    LOGNORMAL = "LOGNORMAL"
    UNIFORM = "UNIFORM"


class AmountSpec(ConfigModel):
    """Synthetic payment amounts in minor units (paise)."""

    distribution: AmountDistribution = AmountDistribution.LOGNORMAL
    median_minor: PositiveInt = 50_000  # Rs 500
    sigma: NonNegativeFloat = 1.0
    min_minor: PositiveInt = 100
    max_minor: PositiveInt = 10_000_000

    @model_validator(mode="after")
    def _bounds(self) -> "AmountSpec":
        if not self.min_minor <= self.median_minor <= self.max_minor:
            raise ValueError("amounts require min_minor <= median_minor <= max_minor")
        return self


class BeneficiarySpec(ConfigModel):
    base_rejection_prob: Probability = 0.02
    base_loss_prob: Probability = 0.0
    latency_sigma: NonNegativeFloat = 0.25


class RepaySpec(ConfigModel):
    """Customer propensity to pay again after seeing a pending payment (a NEW payment)."""

    probability: Probability = 0.10
    mean_delay_s: PositiveFloat = 30.0


class RepeatSpec(ConfigModel):
    """Customer re-submission of the SAME request (same idempotency key)."""

    probability: Probability = 0.0
    mean_delay_s: PositiveFloat = 5.0


class CheckDemandSpec(ConfigModel):
    """Exogenous status-check demand not controlled by AORTS (e.g. an aggressive bank)."""

    demand_id: Identifier
    bank_id: Identifier
    route_id: Identifier
    start_s: NonNegativeFloat
    end_s: PositiveFloat
    rate_rps: NonNegativeFloat

    @model_validator(mode="after")
    def _window(self) -> "CheckDemandSpec":
        if self.end_s <= self.start_s:
            raise ValueError("check demand end_s must be later than start_s")
        return self


class ScenarioConfig(ConfigModel):
    """Complete, serialisable definition of one synthetic workload."""

    format: Literal["aorts-scenario/1"] = "aorts-scenario/1"
    scenario_id: Identifier
    description: str = Field(min_length=1, max_length=500)
    reference: str = Field(min_length=1, max_length=500)
    experiment_id: Identifier
    seed: int = Field(ge=0, strict=True)
    epoch: UtcDatetime = REFERENCE_EPOCH
    duration_s: PositiveFloat
    currency: Literal["INR"] = "INR"
    routes: tuple[RouteSpec, ...] = Field(min_length=1)
    faults: tuple[FaultSpec, ...] = ()
    flapping: tuple[FlappingSpec, ...] = ()
    bursts: BurstSpec | None = None
    amount: AmountSpec = AmountSpec()
    beneficiary: BeneficiarySpec = BeneficiarySpec()
    repay: RepaySpec = RepaySpec()
    repeat: RepeatSpec = RepeatSpec()
    check_demand: tuple[CheckDemandSpec, ...] = ()
    timing_policy: TimingPolicy
    max_records: PositiveInt = 1_000_000

    @model_validator(mode="after")
    def _references(self) -> "ScenarioConfig":
        route_ids = [route.route_id for route in self.routes]
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("route_id values must be unique")
        known = set(route_ids)
        named = [
            *(f.route_id for f in self.faults),
            *(f.route_id for f in self.flapping),
            *(d.route_id for d in self.check_demand),
            *(self.bursts.route_ids if self.bursts else ()),
        ]
        unknown = sorted(set(named) - known)
        if unknown:
            raise ValueError(f"unknown route_id references: {unknown}")
        ids = [f.fault_id for f in self.faults] + [f.flapping_id for f in self.flapping]
        if len(set(ids)) != len(ids):
            raise ValueError("fault_id and flapping_id values must be unique")
        demand_ids = [d.demand_id for d in self.check_demand]
        if len(set(demand_ids)) != len(demand_ids):
            raise ValueError("demand_id values must be unique")
        for start in [f.start_s for f in self.faults] + [d.start_s for d in self.check_demand]:
            if start >= self.duration_s:
                raise ValueError("faults and check demand must start within duration_s")
        return self

    def route(self, route_id: str) -> RouteSpec:
        for route in self.routes:
            if route.route_id == route_id:
                return route
        raise KeyError(route_id)

    def descriptors(self) -> tuple[RouteDescriptor, ...]:
        return tuple(route.descriptor(self.experiment_id) for route in self.routes)


# --------------------------------------------------------------------- reference presets


def research_timing_policy() -> TimingPolicy:
    """Report Table 12 timing as RESEARCH_SCALED parameters (never TIER1_COMPLIANT).

    The 60-minute deadline is the report's scaled stand-in for T+1. The 600 s escalation window
    is a research assumption: the report does not define one.
    """

    return TimingPolicy(
        schema_version=SCHEMA_VERSION,
        policy_id="report-table12-research",
        regime="RESEARCH_SCALED",
        verification_status="RESEARCH_PARAMETER",
        first_check_delay_s=90.0,
        first_check_reference="INITIATED",
        min_check_spacing_s=90.0,
        max_counted_checks=3,
        check_window_s=7200.0,
        client_timeout_s=15.0,
        check_timeout_s=10.0,
        deadline_rule={"kind": "FIXED_DURATION", "duration_s": 3600.0},
        escalation_window_s=600.0,
        escalation_extra_checks=0,
    )


class ScenarioOptions(ConfigModel):
    """Optional variations layered on S1/S2/S3."""

    healthy_parallel_route: StrictBool = False  # adds A1-C, never faulted
    three_remitters: StrictBool = False  # A1-B, A2-B, A3-B share beneficiary B
    bursty: StrictBool = False
    extended_fault: StrictBool = False  # the degradation lasts 2 h
    flapping: StrictBool = False  # 30 % dips for 60 s every 5 min after the fault
    delayed_confirmations: StrictBool = False  # +20 s confirmation delay during the fault
    repeated_payments: StrictBool = False  # 20 % same-key re-submissions


_SCENARIO_BASE: dict[str, tuple[str, float | None, float | None]] = {
    # name: (description, fault start s, fault end s)
    "S1": ("Normal traffic, no fault", None, None),
    "S2": ("One degraded beneficiary route: capacity -35 % from minute 10 to 30", 600.0, 1800.0),
    "S3": ("Short fault: capacity -35 % for 2 minutes from minute 10", 600.0, 720.0),
}


def reference_scenario(
    name: Literal["S1", "S2", "S3"],
    *,
    seed: int,
    experiment_id: str,
    options: ScenarioOptions | None = None,
    rate_scale: float = 1.0,
    duration_s: float = 9000.0,
    epoch: datetime = REFERENCE_EPOCH,
) -> ScenarioConfig:
    """Build S1/S2/S3 from the report's reference parameters.

    ``rate_scale`` multiplies arrival rates and capacities together (as the report's 1/10-scale
    discrete-event check did), so load ratios are preserved. Full scale (700 payments/s for
    2.5 h) is about 6.3 million payments; tests use small scales.
    """

    if name not in _SCENARIO_BASE:
        raise ValueError(f"unknown reference scenario {name!r}")
    if not rate_scale > 0:
        raise ValueError("rate_scale must be positive")
    opts = options or ScenarioOptions()
    description, fault_start, fault_end = _SCENARIO_BASE[name]
    if opts.extended_fault and fault_start is not None:
        fault_end = fault_start + 7200.0
        description += "; extended to 2 h"

    total_rps, capacity = 700.0 * rate_scale, 1000.0 * rate_scale
    if opts.three_remitters:
        degraded = [("A1-B", "A1"), ("A2-B", "A2"), ("A3-B", "A3")]
        routes = [
            RouteSpec(
                route_id=route_id,
                remitter_bank_id=bank,
                beneficiary_bank_id="B",
                payment_rps=total_rps / 3,
                capacity_rps=capacity / 3,
            )
            for route_id, bank in degraded
        ]
    else:
        degraded = [("A1-B", "A1")]
        routes = [
            RouteSpec(
                route_id="A1-B",
                remitter_bank_id="A1",
                beneficiary_bank_id="B",
                payment_rps=total_rps,
                capacity_rps=capacity,
            )
        ]
    if opts.healthy_parallel_route:
        routes.append(
            RouteSpec(
                route_id="A1-C",
                remitter_bank_id="A1",
                beneficiary_bank_id="C",
                payment_rps=total_rps,
                capacity_rps=capacity,
            )
        )

    faults: list[FaultSpec] = []
    flapping: list[FlappingSpec] = []
    if fault_start is not None and fault_end is not None:
        for route_id, _ in degraded:
            faults.append(
                FaultSpec(
                    fault_id=f"cut-{route_id}",
                    route_id=route_id,
                    kind=FaultKind.CAPACITY_CUT,
                    start_s=fault_start,
                    end_s=fault_end,
                    magnitude=0.35,
                )
            )
            if opts.delayed_confirmations:
                faults.append(
                    FaultSpec(
                        fault_id=f"confirm-delay-{route_id}",
                        route_id=route_id,
                        kind=FaultKind.CONFIRMATION_DELAY,
                        start_s=fault_start,
                        end_s=fault_end,
                        magnitude=20.0,
                    )
                )
            if opts.flapping and fault_end < duration_s:
                flapping.append(
                    FlappingSpec(
                        flapping_id=f"flap-{route_id}",
                        route_id=route_id,
                        start_s=fault_end,
                        end_s=duration_s,
                        period_s=300.0,
                        dip_duration_s=60.0,
                        dip_fraction=0.30,
                    )
                )
    elif opts.delayed_confirmations or opts.flapping:
        raise ValueError(f"{name} has no fault window for delayed confirmations or flapping")

    return ScenarioConfig(
        scenario_id=name + "".join(f".{k}" for k, v in opts.model_dump().items() if v),
        description=description,
        reference=REPORT_REFERENCE,
        experiment_id=experiment_id,
        seed=seed,
        epoch=epoch,
        duration_s=duration_s,
        routes=tuple(routes),
        faults=tuple(faults),
        flapping=tuple(flapping),
        bursts=BurstSpec(every_s=300.0, duration_s=30.0, multiplier=3.0) if opts.bursty else None,
        repeat=RepeatSpec(probability=0.2) if opts.repeated_payments else RepeatSpec(),
        timing_policy=research_timing_policy(),
    )


# --------------------------------------------------------------------- Track B adapter


@dataclass(frozen=True)
class AdaptedScenario:
    """A Track B scenario translated into a ScenarioConfig, with what could not be mapped."""

    config: ScenarioConfig
    unmapped: tuple[str, ...]


def scenario_from_trackb(
    scenario: Mapping[str, Any],
    *,
    seed: int,
    experiment_id: str,
    capacity_rps: float,
    traffic: Mapping[str, Any] | None = None,
    flood_route_id: str | None = None,
) -> AdaptedScenario:
    """Translate the dictionary form of Track B's ``Scenario`` (``Scenario.as_dict()``) and
    optionally its ``TrafficConfig`` (``asdict``) into a Track A ``ScenarioConfig``.

    Pure data translation: no Track B code is imported or executed. Track B scenarios define no
    route capacity, so ``capacity_rps`` must be supplied explicitly rather than invented. Fields
    with no workload meaning (telemetry poisoning, dispatcher duplicate delivery) and floods
    without an explicit ``flood_route_id`` are reported in ``unmapped``, never guessed.
    """

    traffic = dict(traffic or {})
    banks = tuple(traffic.get("banks", ("A1", "A2")))
    destinations = tuple(traffic.get("destinations", ("B", "C")))
    total_rps = float(scenario.get("rps", traffic.get("rps", 5.0)))
    per_route = total_rps / (len(banks) * len(destinations))  # uniform bank and destination
    routes: tuple[RouteSpec, ...] = tuple(
        RouteSpec(
            route_id=f"{bank}-{dest}",
            remitter_bank_id=bank,
            beneficiary_bank_id=dest,
            payment_rps=per_route,
            capacity_rps=capacity_rps,
        )
        for bank in banks
        for dest in destinations
    )
    unmapped: list[str] = []
    faults: list[FaultSpec] = []
    demand: list[CheckDemandSpec] = []
    offset = 0.0
    kinds = (
        ("latency_ms", FaultKind.LATENCY),
        ("error_rate", FaultKind.REJECTION),
        ("drop_response_rate", FaultKind.RESPONSE_LOSS),
    )
    for phase in scenario.get("phases", ()):
        start, end = offset, offset + float(phase["duration_s"])
        offset = end
        for n, fault in enumerate(phase.get("faults", ())):
            target = str(fault.get("target", ""))
            if not target.startswith("beneficiary:"):
                unmapped.append(f"{phase['name']}.faults[{n}].target={target}")
                continue
            dest = target.split(":", 1)[1]
            for route in (r for r in routes if r.beneficiary_bank_id == dest):
                for key, kind in kinds:
                    if fault.get(key):
                        faults.append(
                            FaultSpec(
                                fault_id=f"{phase['name']}-{n}-{key}-{route.route_id}",
                                route_id=route.route_id,
                                kind=kind,
                                start_s=start,
                                end_s=end,
                                magnitude=float(fault[key]),
                            )
                        )
        if phase.get("flood_rps"):
            if flood_route_id is None:
                unmapped.append(f"{phase['name']}.flood_rps (no flood_route_id given)")
            else:
                demand.append(
                    CheckDemandSpec(
                        demand_id=f"flood-{phase['name']}",
                        bank_id="A3",
                        route_id=flood_route_id,
                        start_s=start,
                        end_s=end,
                        rate_rps=float(phase["flood_rps"]),
                    )
                )
        if phase.get("poison"):
            unmapped.append(f"{phase['name']}.poison (telemetry attack, not workload)")
    if scenario.get("duplicate_job_prob"):
        unmapped.append("duplicate_job_prob (dispatcher delivery, not workload)")
    if flood_route_id is not None and flood_route_id not in {r.route_id for r in routes}:
        routes = (
            *routes,
            RouteSpec(
                route_id=flood_route_id,
                remitter_bank_id="A3",
                beneficiary_bank_id=flood_route_id.split("-", 1)[-1],
                payment_rps=0.0,
                capacity_rps=capacity_rps,
            ),
        )

    every = float(scenario.get("burst_every_s", traffic.get("burst_every_s", 0.0)) or 0.0)
    multiplier = float(scenario.get("burst_multiplier", traffic.get("burst_multiplier", 1.0)))
    bursts = (
        # Track B bursts last one second at the start of each ``every_s`` window.
        BurstSpec(every_s=every, duration_s=1.0, multiplier=multiplier)
        if every > 1.0 and multiplier > 1.0
        else None
    )
    if bursts is not None:
        unmapped.append("burst phase approximated (Track B skips the burst in its first window)")
    low = int(traffic.get("min_amount_paise", 1_000))
    high = int(traffic.get("max_amount_paise", 500_000))
    amount = AmountSpec(
        distribution=AmountDistribution.UNIFORM,
        min_minor=low,
        max_minor=high,
        median_minor=(low + high) // 2,
        sigma=0.0,
    )
    retry = float(scenario.get("retry_probability", traffic.get("retry_probability", 0.0)))
    name = str(scenario.get("name", "trackb-scenario"))
    config = ScenarioConfig(
        scenario_id=f"trackb.{name}",
        description=str(scenario.get("description") or name)[:500],
        reference="Translated from a Track B Scenario dictionary (feature/backend 8b863da)",
        experiment_id=experiment_id,
        seed=seed,
        duration_s=max(offset, float(traffic.get("duration_s", 0.0)), 1.0),
        routes=routes,
        faults=tuple(faults),
        bursts=bursts,
        amount=amount,
        repay=RepaySpec(probability=0.0),
        repeat=RepeatSpec(probability=retry),
        check_demand=tuple(demand),
        timing_policy=research_timing_policy(),
    )
    return AdaptedScenario(config=config, unmapped=tuple(unmapped))


# --------------------------------------------------------------------- legacy (Stage 3B input)


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_seconds: int
    payment_rps: float
    route_capacity_rps: float
    fault_at_second: int | None


HACKATHON_SCENARIO = Scenario(
    name="switch-slowdown",
    duration_seconds=30,
    payment_rps=80.0,
    route_capacity_rps=100.0,
    fault_at_second=10,
)
