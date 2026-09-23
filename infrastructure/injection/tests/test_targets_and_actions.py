"""Targets are restricted, and every scenario can be applied and reverted."""

from __future__ import annotations

import pytest

from aort_injection.actions import SCENARIOS, build_apply_argv, build_revert_argv
from aort_injection.targets import CONTAINERS, container_for


# --- targets: a blast radius that cannot grow by accident -----------------------

def test_known_components_map_to_aort_containers():
    assert container_for("fineract") == "aort-fineract"
    assert container_for("postgres") == "aort-postgres"
    assert container_for("backup") == "aort-db-backup"


@pytest.mark.parametrize("unknown", ["", "prometheus", "grafana", "aort-fineract", None, "../x"])
def test_unknown_or_unowned_targets_are_refused(unknown):
    # Injection must never be able to touch an arbitrary container: telemetry
    # containers included, or the experiment destroys its own observation.
    with pytest.raises(ValueError):
        container_for(unknown)


def test_every_target_container_belongs_to_this_project():
    assert all(name.startswith("aort-") for name in CONTAINERS.values())


# --- scenarios ------------------------------------------------------------------

def test_the_four_blueprint_scenarios_are_available():
    assert set(SCENARIOS) == {"service_stop", "service_pause", "cpu_throttle", "backup_stop"}


@pytest.mark.parametrize("scenario", ["service_stop", "service_pause", "cpu_throttle", "backup_stop"])
def test_every_scenario_has_an_apply_and_a_revert(scenario):
    params = {"cpus": "0.2"} if scenario == "cpu_throttle" else {}
    assert build_apply_argv(scenario, "aort-fineract", params)
    assert build_revert_argv(scenario, "aort-fineract", params)


def test_service_stop_and_its_reversal():
    assert build_apply_argv("service_stop", "aort-fineract", {}) == [
        "docker", "stop", "--time", "10", "aort-fineract"]
    assert build_revert_argv("service_stop", "aort-fineract", {}) == [
        "docker", "start", "aort-fineract"]


def test_service_pause_freezes_without_data_loss():
    assert build_apply_argv("service_pause", "aort-postgres", {}) == [
        "docker", "pause", "aort-postgres"]
    assert build_revert_argv("service_pause", "aort-postgres", {}) == [
        "docker", "unpause", "aort-postgres"]


def test_cpu_throttle_sets_and_then_restores_an_unlimited_quota():
    assert build_apply_argv("cpu_throttle", "aort-fineract", {"cpus": "0.2"}) == [
        "docker", "update", "--cpus", "0.2", "aort-fineract"]
    assert build_revert_argv("cpu_throttle", "aort-fineract", {"cpus": "0.2"}) == [
        "docker", "update", "--cpus", "0", "aort-fineract"]


def test_backup_stop_targets_the_sidecar_only():
    assert build_apply_argv("backup_stop", "aort-db-backup", {}) == [
        "docker", "stop", "--time", "10", "aort-db-backup"]


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "1e9", "99"])
def test_cpu_throttle_rejects_implausible_quotas(bad):
    with pytest.raises(ValueError):
        build_apply_argv("cpu_throttle", "aort-fineract", {"cpus": bad})


def test_cpu_throttle_requires_its_parameter():
    with pytest.raises(ValueError):
        build_apply_argv("cpu_throttle", "aort-fineract", {})


@pytest.mark.parametrize("scenario", ["", "rm", "service_delete", "SERVICE_STOP"])
def test_unknown_scenarios_are_refused(scenario):
    with pytest.raises(ValueError):
        build_apply_argv(scenario, "aort-fineract", {})


def test_no_scenario_can_destroy_data_or_the_container():
    forbidden = {"rm", "kill", "rmi", "volume", "system", "exec", "down"}
    for scenario in SCENARIOS:
        params = {"cpus": "0.5"} if scenario == "cpu_throttle" else {}
        for argv in (build_apply_argv(scenario, "aort-fineract", params),
                     build_revert_argv(scenario, "aort-fineract", params)):
            assert argv[1] not in forbidden, argv
