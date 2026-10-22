"""Stage 04S: deterministic research-model selection."""

import inspect
from pathlib import Path

import pytest

pytest.importorskip("simpy")  # model_selection imports the DES schedule type (research extra)

from intelligence.simulation import model_selection as ms  # noqa: E402
from intelligence.simulation.des_validate import CheckRelease  # noqa: E402
from intelligence.simulation.model_validation import periodic_schedule  # noqa: E402
from intelligence.simulation.scenarios import (  # noqa: E402
    ScenarioOptions,
    reference_scenario,
)

REPO = Path(__file__).resolve().parents[2]
BURSTS = periodic_schedule(("A1-B",), 100, 2400, 10, 20)
SMALL = periodic_schedule(("A1-B",), 100, 2400, 10, 5)


def scenario(name, seed=7, **options):
    return reference_scenario(name, seed=seed, experiment_id="EXP-SEL", rate_scale=0.01,
                              duration_s=2400.0, options=ScenarioOptions(**options))  # fmt: skip


def entry(regime, status, dt, repay=True, bursts=True):
    return ms.ValidationEntry(ms.LoadRegime(regime), repay, bursts, dt, ms.Status(status), "test")


def test_regimes_come_from_the_design_only():
    assert ms.features(scenario("S1"), BURSTS).key == ("LOW", True, True, True)
    assert ms.features(scenario("S3"), BURSTS).key == ("TIMEOUT", True, True, True)
    assert ms.features(scenario("S2"), BURSTS).key == ("STALE", True, True, True)
    assert ms.features(scenario("S2"), SMALL).check_bursts is False
    # the seed changes the workload, never the declared regime
    assert ms.features(scenario("S2", seed=1), BURSTS) == ms.features(
        scenario("S2", seed=99), BURSTS
    )
    assert list(inspect.signature(ms.select_model).parameters) == [
        "config", "schedule", "purpose", "dt_s", "table", "timing_compliant",
    ]  # fmt: skip


def test_extended_fault_raises_the_regime():
    assert ms.features(scenario("S3", extended_fault=True), BURSTS).load_regime is (
        ms.LoadRegime.STALE
    )


TABLE = (
    entry("LOW", "GREEN", 1.0),
    entry("LOW", "GREEN", 0.25),
    entry("TIMEOUT", "AMBER", 1.0),
    entry("TIMEOUT", "GREEN", 0.25),
    entry("STALE", "RED", 1.0),
    entry("STALE", "AMBER", 0.1),
)


def test_green_regime_uses_the_fluid_model_at_the_coarsest_green_timestep():
    sel = ms.select_model(scenario("S1"), BURSTS, ms.Purpose.PRIMARY, table=TABLE)
    assert (sel.model, sel.status, sel.dt_s, sel.approximate) == (
        ms.Model.FLUID, ms.Status.GREEN, 1.0, False,
    )  # fmt: skip
    sel = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY, table=TABLE)
    assert (sel.model, sel.dt_s) == (ms.Model.FLUID, 0.25)


def test_timestep_restrictions_are_respected():
    sel = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY, dt_s=1.0, table=TABLE)
    assert sel.model is ms.Model.DES and sel.status is ms.Status.AMBER
    sel = ms.select_model(scenario("S1"), BURSTS, ms.Purpose.PRIMARY, dt_s=0.5, table=TABLE)
    assert sel.model is ms.Model.DES and sel.status is ms.Status.UNVALIDATED


def test_amber_is_exploratory_only_and_flagged_approximate():
    primary = ms.select_model(scenario("S2"), BURSTS, ms.Purpose.PRIMARY, table=TABLE)
    assert primary.model is ms.Model.DES
    exploratory = ms.select_model(scenario("S2"), BURSTS, ms.Purpose.EXPLORATORY, table=TABLE)
    assert (exploratory.model, exploratory.status, exploratory.approximate, exploratory.dt_s) == (
        ms.Model.FLUID, ms.Status.AMBER, True, 0.1,
    )  # fmt: skip


def test_red_never_selects_the_fluid_model():
    red_only = (entry("STALE", "RED", 1.0), entry("STALE", "RED", 0.1))
    for purpose in ms.Purpose:
        sel = ms.select_model(scenario("S2"), BURSTS, purpose, table=red_only)
        assert sel.model is ms.Model.DES and sel.status is ms.Status.RED


def test_unvalidated_designs_default_to_des():
    sel = ms.select_model(scenario("S2"), SMALL, ms.Purpose.EXPLORATORY, table=TABLE)
    assert sel.model is ms.Model.DES and sel.status is ms.Status.UNVALIDATED
    assert ms.select_model(scenario("S1"), BURSTS, ms.Purpose.PRIMARY, table=()).model is (
        ms.Model.DES
    )


def test_selection_is_deterministic():
    a = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY, table=TABLE)
    b = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY, table=tuple(reversed(TABLE)))
    assert a == b


def test_frozen_table_never_permits_red_or_untested_regimes_for_primary_evidence():
    for e in ms.VALIDATION_TABLE:
        assert e.evidence
    for name in ("S1", "S2", "S3"):
        sel = ms.select_model(scenario(name), BURSTS, ms.Purpose.PRIMARY)
        key = ms.features(scenario(name), BURSTS).key
        statuses = {e.status for e in ms.VALIDATION_TABLE if e.key == key}
        if ms.Status.GREEN not in statuses:
            assert sel.model is ms.Model.DES
        else:
            assert sel.model is ms.Model.FLUID and sel.status is ms.Status.GREEN


def test_controller_code_cannot_reach_research_models_or_truth():
    for path in (REPO / "intelligence" / "controller").glob("*.py"):
        source = path.read_text()
        for forbidden in ("model_selection", "des_validate", "model_validation", "simulation"):
            assert forbidden not in source, (path.name, forbidden)
    assert "Workload" not in inspect.getsource(ms)  # design only, never ground-truth records


def test_check_release_type_is_shared_with_the_des_schedule():
    assert ms.features(scenario("S1"), (CheckRelease(100.0, "A1-B", 10),)).check_bursts


def test_frozen_holdout_selections():
    s1 = ms.select_model(scenario("S1"), BURSTS, ms.Purpose.PRIMARY)
    assert (s1.model, s1.status, s1.dt_s) == (ms.Model.FLUID, ms.Status.GREEN, 1.0)
    s3 = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY)
    assert (s3.model, s3.status, s3.dt_s) == (ms.Model.FLUID, ms.Status.GREEN, 0.25)
    coarse = ms.select_model(scenario("S3"), BURSTS, ms.Purpose.PRIMARY, dt_s=1.0)
    assert coarse.model is ms.Model.DES and coarse.status is ms.Status.AMBER
    s2 = ms.select_model(scenario("S2"), BURSTS, ms.Purpose.PRIMARY)
    assert (s2.model, s2.status) == (ms.Model.DES, ms.Status.AMBER)
    s2x = ms.select_model(scenario("S2"), BURSTS, ms.Purpose.EXPLORATORY)
    assert (s2x.model, s2x.approximate, s2x.dt_s) == (ms.Model.FLUID, True, 0.25)
    s2coarse = ms.select_model(scenario("S2"), BURSTS, ms.Purpose.EXPLORATORY, dt_s=1.0)
    assert (s2coarse.model, s2coarse.status) == (ms.Model.DES, ms.Status.RED)
    assert ms.RECOMMENDED_DT_S == 0.25


def test_noncompliant_check_schedules_are_unvalidated():
    sel = ms.select_model(scenario("S1"), BURSTS, ms.Purpose.EXPLORATORY, timing_compliant=False)
    assert sel.model is ms.Model.DES and sel.status is ms.Status.UNVALIDATED
