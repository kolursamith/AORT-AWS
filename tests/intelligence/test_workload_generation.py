"""Stage 3A: deterministic synthetic workload generation.

Every test is deterministic (explicit seeds, fixed epoch, no wall clock). Statistical checks use
fixed seeds and generous tolerances; they verify the configured process, not report results.
"""

import json
import os
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from contracts.v1_1 import RouteTelemetry, TransactionEvent, ValidatedRouteTelemetry
from intelligence.data.generate_dataset import (
    BeneficiaryTruth,
    CapacityChange,
    CustomerBehaviour,
    FaultEvent,
    IntrinsicAmbiguity,
    PaymentArrival,
    Workload,
    WorkloadIntegrityError,
    WorkloadTooLargeError,
    generate_workload,
    read_workload,
    write_workload,
)
from intelligence.simulation.scenarios import (
    HACKATHON_SCENARIO,
    REFERENCE_EPOCH,
    SYNTHETIC_LABEL,
    BurstSpec,
    CheckDemandSpec,
    FaultKind,
    FaultSpec,
    RouteSpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
    research_timing_policy,
    scenario_from_trackb,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL = {"rate_scale": 0.002, "duration_s": 2400.0}  # 1.4 payments/s on the degraded route


def small(name="S2", seed=11, experiment_id="EXP-1", **options) -> ScenarioConfig:
    return reference_scenario(
        name,
        seed=seed,
        experiment_id=experiment_id,
        options=ScenarioOptions(**options),
        **SMALL,
    )


def custom(**overrides) -> ScenarioConfig:
    base = {
        "scenario_id": "custom",
        "description": "targeted test scenario",
        "reference": "unit test",
        "experiment_id": "EXP-T",
        "seed": 7,
        "duration_s": 600.0,
        "routes": (
            RouteSpec(
                route_id="A1-B",
                remitter_bank_id="A1",
                beneficiary_bank_id="B",
                payment_rps=5.0,
                capacity_rps=10.0,
            ),
        ),
        "timing_policy": research_timing_policy(),
    }
    return ScenarioConfig(**{**base, **overrides})


@pytest.fixture(scope="module")
def s2() -> Workload:
    return generate_workload(small())


def by_route(workload: Workload, route_id: str):
    payments = [p for p in workload.exogenous.payments if p.route_id == route_id]
    ids = {p.transaction_id for p in payments}
    behaviour = [b for b in workload.exogenous.customer_behaviour if b.transaction_id in ids]
    truth = [t for t in workload.ground_truth.beneficiary if t.transaction_id in ids]
    strip = {"experiment_id", "sequence"}
    return (
        [p.model_dump(exclude=strip) for p in payments],
        [b.model_dump(exclude=strip) for b in behaviour],
        [t.model_dump(exclude=strip) for t in truth],
    )


def all_datetimes(model: BaseModel):
    for value in model.__dict__.values():
        if isinstance(value, datetime):
            yield value
        elif isinstance(value, BaseModel):
            yield from all_datetimes(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, BaseModel):
                    yield from all_datetimes(item)


# ------------------------------------------------------------------ determinism


def test_identical_seeds_produce_identical_workloads(s2):
    again = generate_workload(small())
    assert again == s2
    assert again.manifest.content_hash == s2.manifest.content_hash
    assert again.to_jsonl_lines() == s2.to_jsonl_lines()


def test_different_seeds_produce_different_workloads(s2):
    other = generate_workload(small(seed=12))
    assert other.manifest.content_hash != s2.manifest.content_hash
    first = [p.offset_s for p in s2.exogenous.payments[:20]]
    assert first != [p.offset_s for p in other.exogenous.payments[:20]]


def test_regeneration_in_fresh_processes_is_identical(s2):
    code = (
        "from intelligence.data.generate_dataset import generate_workload\n"
        "from intelligence.simulation.scenarios import reference_scenario\n"
        "w = generate_workload(reference_scenario('S2', seed=11, experiment_id='EXP-1', "
        "rate_scale=0.002, duration_s=2400.0))\n"
        "print(w.manifest.content_hash)\n"
    )
    hashes = set()
    for hash_seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        hashes.add(completed.stdout.strip())
    assert hashes == {s2.manifest.content_hash}


def test_policy_runs_share_the_external_workload(s2):
    other_run = generate_workload(small(experiment_id="EXP-POLICY-B"))
    assert other_run.manifest.content_hash == s2.manifest.content_hash
    assert other_run.manifest.run_id == s2.manifest.run_id
    assert {r.experiment_id for r in other_run.records()} == {"EXP-POLICY-B"}
    assert {r.experiment_id for r in s2.records()} == {"EXP-1"}
    assert other_run.to_jsonl_lines() != s2.to_jsonl_lines()


def test_global_random_state_is_untouched():
    random.seed(99)
    before = random.getstate()
    generate_workload(small(seed=3))
    assert random.getstate() == before


# ------------------------------------------------------------------ identity and time


def test_route_identity_is_valid(s2):
    descriptors = {d.route_id: d for d in s2.manifest.config.descriptors()}
    for payment in s2.exogenous.payments:
        descriptor = descriptors[payment.route_id]
        assert payment.remitter_bank_id == descriptor.remitter_bank_id
        assert payment.beneficiary_bank_id == descriptor.beneficiary_bank_id
        assert descriptor.experiment_id == payment.experiment_id
    with pytest.raises(ValidationError, match="must differ"):
        RouteSpec(
            route_id="A1-A1",
            remitter_bank_id="A1",
            beneficiary_bank_id="A1",
            payment_rps=1.0,
            capacity_rps=1.0,
        )
    with pytest.raises(ValidationError, match="unknown route_id"):
        custom(faults=(FaultSpec(fault_id="f", route_id="Z9-Q", kind="LATENCY", start_s=0,
                                 end_s=1, magnitude=1),))  # fmt: skip


def test_transaction_ids_are_unique_and_route_scoped(s2):
    ids = [p.transaction_id for p in s2.exogenous.payments]
    assert len(ids) == len(set(ids))
    assert all(
        t.startswith(f"TXN-{p.route_id}-") for t, p in zip(ids, s2.exogenous.payments, strict=True)
    )


def test_all_timestamps_are_utc(s2):
    stamps = list(all_datetimes(s2))
    assert stamps and all(t.utcoffset() == timedelta(0) for t in stamps)


def test_epoch_is_normalised_to_utc_and_naive_is_rejected():
    ist = timezone(timedelta(hours=5, minutes=30))
    config = custom(epoch=datetime(2026, 1, 1, 5, 30, tzinfo=ist))
    assert config.epoch == REFERENCE_EPOCH
    with pytest.raises(ValidationError, match="timezone"):
        custom(epoch=datetime(2026, 1, 1))


def test_simulation_events_are_monotonic(s2):
    offsets = [entry.offset_s for entry in s2.timeline()]
    assert offsets == sorted(offsets)
    payments = s2.exogenous.payments
    assert [p.sequence for p in payments] == list(range(len(payments)))
    assert all(a.offset_s <= b.offset_s for a, b in zip(payments, payments[1:], strict=False))
    assert all(0 <= p.offset_s < s2.manifest.config.duration_s for p in payments)


@pytest.mark.parametrize("rate", [-1.0, float("nan"), float("inf")])
def test_arrival_rates_must_be_finite_and_nonnegative(rate):
    with pytest.raises(ValidationError):
        RouteSpec(
            route_id="A1-B",
            remitter_bank_id="A1",
            beneficiary_bank_id="B",
            payment_rps=rate,
            capacity_rps=1.0,
        )


def test_zero_rate_route_generates_no_payments():
    quiet = RouteSpec(
        route_id="A2-C", remitter_bank_id="A2", beneficiary_bank_id="C", payment_rps=0.0,
        capacity_rps=5.0,
    )  # fmt: skip
    workload = generate_workload(custom(routes=(*custom().routes, quiet)))
    assert not any(p.route_id == "A2-C" for p in workload.exogenous.payments)
    assert any(c.route_id == "A2-C" for c in workload.ground_truth.capacity)


# ------------------------------------------------------------------ arrival process and faults


def test_bursts_raise_the_arrival_rate_only_inside_burst_windows():
    config = custom(
        duration_s=4000.0,
        routes=(custom().routes[0].model_copy(update={"payment_rps": 2.0}),),
        bursts=BurstSpec(every_s=100.0, duration_s=20.0, multiplier=4.0),
    )
    payments = generate_workload(config).exogenous.payments
    inside = sum(1 for p in payments if p.offset_s % 100.0 < 20.0)
    outside = len(payments) - inside
    ratio = (inside / (0.2 * 4000.0)) / (outside / (0.8 * 4000.0))
    assert 3.4 < ratio < 4.6
    assert generate_workload(custom(duration_s=4000.0)).manifest.config.bursts is None


def test_capacity_faults_activate_and_clear_exactly_on_schedule(s2):
    changes = [c for c in s2.ground_truth.capacity if c.route_id == "A1-B"]
    assert [(c.offset_s, round(c.capacity_rps, 6), c.cause) for c in changes] == [
        (0.0, 2.0, "baseline"),
        (600.0, 1.3, "cut-A1-B"),
        (1800.0, 2.0, "restored"),
    ]
    (fault,) = s2.ground_truth.faults
    assert (fault.start_offset_s, fault.end_offset_s, fault.unit) == (600.0, 1800.0, "fraction")
    for payment, truth in zip(s2.exogenous.payments, s2.ground_truth.beneficiary, strict=True):
        assert (truth.active_fault_ids == ("cut-A1-B",)) == (600.0 <= payment.offset_s < 1800.0)


def test_latency_fault_adds_latency_only_while_active():
    config = custom(
        faults=(
            FaultSpec(
                fault_id="slow",
                route_id="A1-B",
                kind=FaultKind.LATENCY,
                start_s=300.0,
                end_s=400.0,
                magnitude=1000.0,
            ),
        )  # fmt: skip
    )
    workload = generate_workload(config)
    for payment, truth in zip(
        workload.exogenous.payments, workload.ground_truth.beneficiary, strict=True
    ):
        if 300.0 <= payment.offset_s < 400.0:
            assert truth.beneficiary_latency_ms >= 1000.0 and truth.active_fault_ids == ("slow",)
        else:
            assert truth.beneficiary_latency_ms < 1000.0 and truth.active_fault_ids == ()


def test_lost_responses_are_ground_truth_and_create_intrinsic_ambiguity():
    config = custom(
        faults=(
            FaultSpec(
                fault_id="drop",
                route_id="A1-B",
                kind=FaultKind.RESPONSE_LOSS,
                start_s=200.0,
                end_s=300.0,
                magnitude=1.0,
            ),
        )  # fmt: skip
    )
    workload = generate_workload(config)
    for payment, truth in zip(
        workload.exogenous.payments, workload.ground_truth.beneficiary, strict=True
    ):
        inside = 200.0 <= payment.offset_s < 300.0
        assert truth.response_lost is inside
        assert (truth.intrinsic_ambiguity is IntrinsicAmbiguity.RESPONSE_LOST) is inside


def test_delayed_confirmations_beyond_the_client_timeout_are_labelled():
    workload = generate_workload(small(delayed_confirmations=True))
    timeout = workload.manifest.config.timing_policy.client_timeout_s
    for payment, truth in zip(
        workload.exogenous.payments, workload.ground_truth.beneficiary, strict=True
    ):
        if 600.0 <= payment.offset_s < 1800.0:
            assert 18.0 <= truth.confirmation_delay_s <= 22.0
            assert truth.confirmation_delay_s >= timeout
            assert truth.intrinsic_ambiguity is IntrinsicAmbiguity.CONFIRMATION_LATE
        else:
            assert truth.confirmation_delay_s == 0.0 and truth.intrinsic_ambiguity is None


def test_customer_repay_behaviour_is_exogenous_and_pre_drawn(s2):
    behaviour = s2.exogenous.customer_behaviour
    assert [b.transaction_id for b in behaviour] == [
        p.transaction_id for p in s2.exogenous.payments
    ]
    share = sum(b.would_repay for b in behaviour) / len(behaviour)
    assert 0.08 < share < 0.12
    # The same customers face the same draws whatever the environment does.
    normal = generate_workload(small("S1"))
    assert normal.exogenous.payments == s2.exogenous.payments
    assert normal.exogenous.customer_behaviour == s2.exogenous.customer_behaviour
    assert normal.ground_truth.beneficiary != s2.ground_truth.beneficiary


def test_no_endogenous_repayments_are_materialised(s2):
    keys = [p.idempotency_key for p in s2.exogenous.payments]
    assert len(keys) == len(set(keys))  # re-pays and re-submissions are left to the simulator
    assert not any(b.would_resubmit_same_key for b in s2.exogenous.customer_behaviour)


def test_repeated_payments_option_sets_same_key_resubmission_intent():
    workload = generate_workload(small(repeated_payments=True))
    behaviour = workload.exogenous.customer_behaviour
    assert 0.17 < sum(b.would_resubmit_same_key for b in behaviour) / len(behaviour) < 0.23


def test_exogenous_check_demand_is_generated_on_its_own_stream():
    demand = CheckDemandSpec(
        demand_id="aggressive-A3", bank_id="A3", route_id="A1-B", start_s=100.0, end_s=200.0,
        rate_rps=10.0,
    )  # fmt: skip
    workload = generate_workload(custom(check_demand=(demand,)))
    checks = workload.exogenous.check_demands
    assert 850 < len(checks) < 1150
    assert all(100.0 <= c.offset_s < 200.0 and c.bank_id == "A3" for c in checks)
    assert workload.exogenous.payments == generate_workload(custom()).exogenous.payments


# ------------------------------------------------------------------ scenarios


def test_healthy_parallel_route_is_isolated(s2):
    with_parallel = generate_workload(small(healthy_parallel_route=True))
    healthy = [c for c in with_parallel.ground_truth.capacity if c.route_id == "A1-C"]
    assert len(healthy) == 1 and healthy[0].cause == "baseline"
    assert all(f.route_id == "A1-B" for f in with_parallel.ground_truth.faults)
    assert all(
        t.active_fault_ids == ()
        for t in with_parallel.ground_truth.beneficiary
        if t.route_id == "A1-C"
    )
    # Adding a route never perturbs another route's random draws.
    assert by_route(with_parallel, "A1-B") == by_route(s2, "A1-B")


def test_routes_draw_from_independent_streams():
    workload = generate_workload(small(healthy_parallel_route=True))
    degraded, healthy = (
        [p.offset_s for p in workload.exogenous.payments if p.route_id == route][:50]
        for route in ("A1-B", "A1-C")
    )
    assert degraded != healthy  # same rate, but not the same random sequence


def test_multi_route_payments_are_merged_in_time_order():
    payments = generate_workload(small(healthy_parallel_route=True)).exogenous.payments
    assert len({p.route_id for p in payments}) == 2
    assert all(a.offset_s <= b.offset_s for a, b in zip(payments, payments[1:], strict=False))
    assert [p.sequence for p in payments] == list(range(len(payments)))


def test_fault_windows_are_half_open():
    fault = FaultSpec(
        fault_id="f", route_id="A1-B", kind=FaultKind.LATENCY, start_s=10.0, end_s=20.0,
        magnitude=1.0,
    )  # fmt: skip
    assert [fault.active(t) for t in (9.999, 10.0, 19.999, 20.0)] == [False, True, True, False]


def test_three_remitter_banks_share_the_degraded_beneficiary():
    workload = generate_workload(small(three_remitters=True))
    routes = {p.route_id for p in workload.exogenous.payments}
    assert routes == {"A1-B", "A2-B", "A3-B"}
    assert {p.remitter_bank_id for p in workload.exogenous.payments} == {"A1", "A2", "A3"}
    assert {f.route_id for f in workload.ground_truth.faults} == routes
    caps = {c.route_id: c.capacity_rps for c in workload.ground_truth.capacity if c.offset_s == 0}
    assert all(abs(v - 2.0 / 3) < 1e-9 for v in caps.values())


def test_reference_presets_carry_report_parameters_as_configuration():
    s2 = reference_scenario("S2", seed=11, experiment_id="EXP-1")
    (route,) = s2.routes
    assert (route.payment_rps, route.capacity_rps) == (700.0, 1000.0)
    (cut,) = s2.faults
    assert (cut.kind, cut.start_s, cut.end_s, cut.magnitude) == ("CAPACITY_CUT", 600, 1800, 0.35)
    s3 = reference_scenario("S3", seed=11, experiment_id="EXP-1")
    assert s3.faults[0].end_s - s3.faults[0].start_s == 120.0
    assert reference_scenario("S1", seed=11, experiment_id="EXP-1").faults == ()
    policy = s2.timing_policy
    assert (policy.regime, policy.verification_status) == ("RESEARCH_SCALED", "RESEARCH_PARAMETER")
    assert (s2.repay.probability, s2.repay.mean_delay_s, policy.client_timeout_s) == (0.1, 30, 15)
    assert "research parameters" in s2.reference


def test_optional_scenario_variations():
    extended = reference_scenario("S2", seed=1, experiment_id="E", options=ScenarioOptions(
        extended_fault=True, flapping=True, bursty=True), duration_s=12000.0)  # fmt: skip
    assert extended.faults[0].end_s == 7800.0
    assert extended.flapping[0].start_s == 7800.0 and extended.bursts is not None
    with pytest.raises(ValueError, match="no fault window"):
        reference_scenario("S1", seed=1, experiment_id="E", options=ScenarioOptions(flapping=True))
    flapping = generate_workload(small(flapping=True))
    causes = [c.cause for c in flapping.ground_truth.capacity]
    assert any(cause.startswith("flap-A1-B.dip") for cause in causes)


def test_full_scale_workloads_are_refused_before_generation():
    with pytest.raises(WorkloadTooLargeError, match="records expected, above max_records"):
        generate_workload(reference_scenario("S2", seed=11, experiment_id="EXP-1"))


def test_legacy_simulator_scenario_is_unchanged():
    assert (HACKATHON_SCENARIO.duration_seconds, HACKATHON_SCENARIO.fault_at_second) == (30, 10)


# ------------------------------------------------------------------ controller boundary

TRUTH_FIELDS = {
    "credited",
    "capacity_rps",
    "response_lost",
    "beneficiary_latency_ms",
    "confirmation_delay_s",
    "intrinsic_ambiguity",
    "active_fault_ids",
    "would_repay",
    "would_resubmit_same_key",
    "magnitude",
    "fault_id",
}


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_ground_truth_never_enters_the_controller_view(s2):
    view = s2.controller_view()
    assert _keys(json.loads(view.model_dump_json())).isdisjoint(TRUTH_FIELDS)
    assert all(isinstance(p, PaymentArrival) for p in view.payments)
    assert view.synthetic_label == SYNTHETIC_LABEL
    banned = (BeneficiaryTruth, CapacityChange, FaultEvent, CustomerBehaviour)
    assert not any(isinstance(v, banned) for v in view.__dict__.values())


def test_future_payments_stay_hidden_from_the_controller(s2):
    view = s2.controller_view()
    cutoff = REFERENCE_EPOCH + timedelta(seconds=600)
    visible = view.payments_until(cutoff)
    assert visible and all(p.arrival_at <= cutoff for p in visible)
    assert len(visible) < len(view.payments)
    with pytest.raises(ValueError, match="timezone"):
        view.payments_until(datetime(2026, 1, 1))


def test_no_historical_observations_are_fabricated(s2):
    observation_types = (RouteTelemetry, ValidatedRouteTelemetry, TransactionEvent)
    assert not any(isinstance(r, observation_types) for r in s2.records())
    view = s2.controller_view()
    assert view.payments_until(REFERENCE_EPOCH - timedelta(seconds=1)) == ()
    assert all(r.offset_s >= 0 for r in s2.exogenous.payments)
    assert "telemetry" not in view.model_dump_json().lower()


def test_outputs_are_labelled_synthetic(s2, tmp_path):
    assert s2.manifest.synthetic is True and s2.manifest.label == SYNTHETIC_LABEL
    write_workload(s2, tmp_path)
    summary = json.loads((tmp_path / "manifest.json").read_text())
    assert summary["label"] == SYNTHETIC_LABEL


# ------------------------------------------------------------------ serialization


def test_serialization_round_trip(s2, tmp_path):
    path = write_workload(s2, tmp_path / "run")
    assert read_workload(path.parent) == s2
    assert Workload.from_jsonl_lines(s2.to_jsonl_lines()) == s2


def test_tampered_records_are_detected(s2, tmp_path):
    lines = s2.to_jsonl_lines()
    record = json.loads(lines[1])
    record["amount_minor"] += 1
    with pytest.raises(WorkloadIntegrityError, match="content_hash"):
        Workload.from_jsonl_lines([lines[0], json.dumps(record), *lines[2:]])
    path = write_workload(s2, tmp_path / "run")
    path.write_text(path.read_text().replace('"currency":"INR"', '"currency":"INR" ', 1))
    with pytest.raises(WorkloadIntegrityError, match="manifest.json"):
        read_workload(path.parent)


# ------------------------------------------------------------------ independence from systems


def test_no_wall_clock_dependency():
    past = datetime(2001, 3, 4, tzinfo=UTC)
    workload = generate_workload(custom(epoch=past))
    assert all(past <= p.arrival_at < past + timedelta(seconds=600) for p in workload.records()
               if isinstance(p, PaymentArrival))  # fmt: skip
    for module in ("intelligence/data/generate_dataset.py", "intelligence/simulation/scenarios.py"):
        source = (REPO_ROOT / module).read_text()
        for forbidden in ("datetime.now", "utcnow", "time.time", "uuid", "random.seed("):
            assert forbidden not in source, f"{module} uses {forbidden}"


def test_no_payment_execution_or_ledger_access():
    code = (
        "import sys, intelligence.data.generate_dataset\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'backend', 'sqlalchemy', 'psycopg', 'boto3', 'httpx', 'requests'})\n"
        "print(','.join(bad))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""


# ------------------------------------------------------------------ Track B adapter

TRACKB_S2 = {  # shape of Track B Scenario.as_dict() for "S2-degraded-B-flood-poison"
    "name": "S2-degraded-B-flood-poison",
    "description": "S1 plus an A3 status-check flood and false high-capacity telemetry",
    "phases": [
        {"name": "warmup", "duration_s": 3.0, "faults": [], "flood_rps": 0.0, "poison": False},
        {
            "name": "degraded",
            "duration_s": 10.0,
            "faults": [
                {
                    "target": "beneficiary:B",
                    "latency_ms": 200,
                    "error_rate": 0.5,
                    "drop_response_rate": 0.4,
                }
            ],
            "flood_rps": 80.0,
            "poison": True,
        },
        {"name": "recovered", "duration_s": 5.0, "faults": [], "flood_rps": 0.0, "poison": False},
    ],
    "rps": 6.0,
    "burst_every_s": 4.0,
    "burst_multiplier": 3.0,
    "retry_probability": 0.2,
    "duplicate_job_prob": 0.1,
    "held_out": False,
}


def test_trackb_scenario_translates_without_inventing_fields():
    adapted = scenario_from_trackb(TRACKB_S2, seed=1000, experiment_id="EXP-B", capacity_rps=40.0)
    config = adapted.config
    assert {r.route_id for r in config.routes} == {"A1-B", "A1-C", "A2-B", "A2-C"}
    assert all(r.payment_rps == 1.5 for r in config.routes)
    faulted = {(f.route_id, f.kind) for f in config.faults}
    assert faulted == {
        (route, kind)
        for route in ("A1-B", "A2-B")
        for kind in (FaultKind.LATENCY, FaultKind.REJECTION, FaultKind.RESPONSE_LOSS)
    }
    assert all((f.start_s, f.end_s) == (3.0, 13.0) for f in config.faults)
    assert config.repeat.probability == 0.2 and config.duration_s == 18.0
    assert any("poison" in u for u in adapted.unmapped)
    assert any("duplicate_job_prob" in u for u in adapted.unmapped)
    assert any("flood_rps" in u for u in adapted.unmapped)
    assert config.check_demand == ()


def test_trackb_flood_maps_only_with_an_explicit_route():
    adapted = scenario_from_trackb(
        TRACKB_S2, seed=1000, experiment_id="EXP-B", capacity_rps=40.0, flood_route_id="A3-B"
    )
    (demand,) = adapted.config.check_demand
    assert (demand.bank_id, demand.route_id, demand.rate_rps) == ("A3", "A3-B", 80.0)
    workload = generate_workload(adapted.config)
    assert workload.exogenous.check_demands
    with pytest.raises(TypeError):
        scenario_from_trackb(TRACKB_S2, seed=1, experiment_id="E")  # capacity is required
