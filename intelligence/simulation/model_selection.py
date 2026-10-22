"""Research-model selection: when may fluid-simulator evidence support a conclusion? (Stage 04S)

This is an experimenter-side policy. It reads only the declared experiment design (the
``ScenarioConfig`` and the external check schedule) — never run outcomes, never a workload's
ground-truth records — and maps it to a validated operating region established by the frozen
Stage 04S holdout. It must not be imported by controller code: the AORTS controller only ever
sees its permitted observations and its own forecast model.

Regimes are derived a priori from the design:

* load regime from the accumulated-delay index ``(rho - 1)+ x duration`` (seconds of queueing
  delay a fault or sustained overload can build, with rho = offered payment and background load
  over the degraded capacity): LOW below half the client timeout, TIMEOUT below the stale-drop
  age, STALE otherwise;
* whether customer re-payment feedback is enabled;
* whether the check schedule releases bursts (>= ``BURST_RELEASE_SIZE`` checks at once);
* whether checks obey the timing policy (the holdout covered compliant schedules only).

Only regimes listed in ``VALIDATION_TABLE`` (frozen holdout evidence) can use the fluid model.
Anything else, or any timestep not validated for that regime, selects the DES.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from intelligence.simulation.des_validate import CheckRelease
from intelligence.simulation.scenarios import ScenarioConfig

STALE_DROP_AGE_S = 30.0  # fluid engine and DES default
BURST_RELEASE_SIZE = 10


class LoadRegime(StrEnum):
    LOW = "LOW"
    TIMEOUT = "TIMEOUT"
    STALE = "STALE"


class Purpose(StrEnum):
    PRIMARY = "PRIMARY"  # evidence for research conclusions
    EXPLORATORY = "EXPLORATORY"  # screening, debugging, sensitivity sweeps


class Model(StrEnum):
    FLUID = "FLUID"
    DES = "DES"


class Status(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    UNVALIDATED = "UNVALIDATED"


@dataclass(frozen=True)
class ScenarioFeatures:
    load_regime: LoadRegime
    delay_index_s: float
    repay: bool
    check_bursts: bool
    timing_compliant: bool = True

    @property
    def key(self) -> tuple[str, bool, bool, bool]:
        return (self.load_regime.value, self.repay, self.check_bursts, self.timing_compliant)


@dataclass(frozen=True)
class ValidationEntry:
    load_regime: LoadRegime
    repay: bool
    check_bursts: bool
    dt_s: float
    status: Status
    evidence: str
    timing_compliant: bool = True

    @property
    def key(self) -> tuple[str, bool, bool, bool]:
        return (self.load_regime.value, self.repay, self.check_bursts, self.timing_compliant)


@dataclass(frozen=True)
class ModelSelection:
    model: Model
    status: Status
    dt_s: float | None
    approximate: bool
    reason: str


def _offered_rps(config: ScenarioConfig, route_id: str) -> float:
    route = config.route(route_id)
    rate = route.payment_rps
    bursts = config.bursts
    if bursts is not None and (not bursts.route_ids or route_id in bursts.route_ids):
        rate *= 1 + (bursts.multiplier - 1) * min(1.0, bursts.duration_s / bursts.every_s)
    rate += sum(d.rate_rps for d in config.check_demand if d.route_id == route_id)
    return rate


def features(
    config: ScenarioConfig,
    schedule: Sequence[CheckRelease],
    timing_compliant: bool = True,
) -> ScenarioFeatures:
    """A priori regime of an experiment design (no run outcome is consulted)."""

    index = 0.0
    for route in config.routes:
        offered = _offered_rps(config, route.route_id)
        windows = [(route.capacity_rps, config.duration_s)]
        for fault in config.faults:
            if fault.route_id == route.route_id and fault.kind == "CAPACITY_CUT":
                capacity = route.capacity_rps * (1 - fault.magnitude)
                windows.append((capacity, fault.end_s - fault.start_s))
        for flap in config.flapping:
            if flap.route_id == route.route_id:
                windows.append((route.capacity_rps * (1 - flap.dip_fraction), flap.dip_duration_s))
        for capacity, duration in windows:
            rho = float("inf") if capacity <= 0 else offered / capacity
            growth = duration if rho == float("inf") else max(0.0, rho - 1.0) * duration
            index = max(index, growth)
    timeout = config.timing_policy.client_timeout_s
    if index < 0.5 * timeout:
        regime = LoadRegime.LOW
    elif index < STALE_DROP_AGE_S:
        regime = LoadRegime.TIMEOUT
    else:
        regime = LoadRegime.STALE
    bursts = any(release.count >= BURST_RELEASE_SIZE for release in schedule)
    return ScenarioFeatures(regime, index, config.repay.probability > 0, bursts, timing_compliant)


# Filled from the frozen Stage 04S holdout (model_validation.HOLDOUT_V2_*, declaration sha256
# 621fa37c..., result sha256 3daf521d..., run once on 2026-10-22). Status = worst class over
# ten seeds and four budget candidates. Only these designs were tested: S1/S2/S3 reference
# routes, re-payment on, 20-check bursts, timing-policy compliant. In LOW and TIMEOUT runs no
# check became eligible, so check-service physics there is not exercised by this evidence.
_EVIDENCE = "Stage 04S holdout 3daf521d"
VALIDATION_TABLE: tuple[ValidationEntry, ...] = (
    ValidationEntry(LoadRegime.LOW, True, True, 1.0, Status.GREEN, f"S1 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.LOW, True, True, 0.25, Status.GREEN, f"S1 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.LOW, True, True, 0.1, Status.GREEN, f"S1 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.TIMEOUT, True, True, 1.0, Status.AMBER, f"S3 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.TIMEOUT, True, True, 0.25, Status.GREEN, f"S3 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.TIMEOUT, True, True, 0.1, Status.GREEN, f"S3 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.STALE, True, True, 1.0, Status.RED, f"S2 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.STALE, True, True, 0.25, Status.AMBER, f"S2 x10, {_EVIDENCE}"),
    ValidationEntry(LoadRegime.STALE, True, True, 0.1, Status.AMBER, f"S2 x10, {_EVIDENCE}"),
)
# Recommended research default timestep: 0.25 s (GREEN wherever any timestep is GREEN in the
# holdout, AMBER otherwise, about 2.3x faster than 0.1 s). 1.0 s is validated for LOW only.
RECOMMENDED_DT_S = 0.25


def select_model(
    config: ScenarioConfig,
    schedule: Sequence[CheckRelease],
    purpose: Purpose,
    dt_s: float | None = None,
    table: Sequence[ValidationEntry] = VALIDATION_TABLE,
    timing_compliant: bool = True,
) -> ModelSelection:
    """Deterministic model choice for one experiment design.

    PRIMARY: fluid only where the regime is GREEN at the requested (or recommended) timestep.
    EXPLORATORY: fluid where GREEN or AMBER (AMBER results are flagged approximate and need a
    DES cross-check before any sensitive conclusion). RED or unvalidated: DES.
    """

    f = features(config, schedule, timing_compliant)
    entries = [e for e in table if e.key == f.key and (dt_s is None or e.dt_s == dt_s)]
    allowed = {Status.GREEN} if purpose is Purpose.PRIMARY else {Status.GREEN, Status.AMBER}
    usable = [e for e in entries if e.status in allowed]
    if usable:
        # coarsest validated timestep (cheapest) among the best status
        best_status = (
            Status.GREEN if any(e.status is Status.GREEN for e in usable) else Status.AMBER
        )
        choice = max((e for e in usable if e.status is best_status), key=lambda e: e.dt_s)
        return ModelSelection(
            Model.FLUID,
            choice.status,
            choice.dt_s,
            choice.status is Status.AMBER,
            f"{f.key} validated {choice.status} at dt={choice.dt_s} s ({choice.evidence})",
        )
    if entries:
        status = min((e.status for e in entries), key=list(Status).index)
        reason = f"{f.key} is {status} for fluid at the requested timestep; DES required"
    else:
        status = Status.UNVALIDATED
        reason = f"{f.key} (delay index {f.delay_index_s:.1f} s) has no validation; DES required"
    return ModelSelection(Model.DES, status, None, False, reason)
