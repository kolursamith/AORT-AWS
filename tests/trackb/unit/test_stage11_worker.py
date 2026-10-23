"""Stage 11: the containerised controller and loop derive one agreed configuration."""

from __future__ import annotations

import json

import pytest

from backend.aorts.app.config import Settings
from backend.aorts.integration import worker
from backend.aorts.integration.loop import FAST_ESTIMATOR, controller_config
from backend.aorts.integration.v11 import LEDGER_SOURCE, telemetry_source, trackb_timing_policy
from intelligence.controller.service import ControllerConfig


def _settings(**kw):
    return Settings(check_backoff_base_seconds=1.0, check_backoff_max_seconds=1.0,
                    experiment_id="aorts-s11-test", **kw)  # fmt: skip


def test_controller_config_matches_what_the_backend_enforces():
    s = _settings()
    cfg = controller_config(s, s.experiment_id, worker.REGISTERED, estimator=FAST_ESTIMATOR)
    assert cfg.timing_policy == trackb_timing_policy(s)  # the same v1.1 timing policy
    assert cfg.experiment_id == "aorts-s11-test"
    assert cfg.event_sources == (LEDGER_SOURCE,)
    for reg in cfg.routes:
        assert reg.telemetry_sources == (telemetry_source(reg.descriptor.route_id),)
    assert cfg.estimator == FAST_ESTIMATOR


def test_controller_config_command_writes_a_loadable_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AORTS_EXPERIMENT_ID", "aorts-s11-test")
    monkeypatch.setenv("AORTS_CHECK_BACKOFF_BASE_SECONDS", "1.0")
    monkeypatch.setenv("AORTS_CHECK_BACKOFF_MAX_SECONDS", "1.0")
    monkeypatch.setenv("AORTS_ESTIMATOR_PROFILE", "default")
    from backend.aorts.app import config

    config.get_settings.cache_clear()
    try:
        out = tmp_path / "controller.json"
        worker.main(["controller-config", "--out", str(out)])
        loaded = ControllerConfig.model_validate_json(out.read_text())
    finally:
        config.get_settings.cache_clear()
    assert loaded.experiment_id == "aorts-s11-test"
    assert {r.descriptor.route_id for r in loaded.routes} == set(worker.REGISTERED)
    assert "token" not in json.dumps(json.loads(out.read_text())).lower()  # no secret inside


def test_unknown_estimator_profile_is_refused(monkeypatch):
    monkeypatch.setenv("AORTS_ESTIMATOR_PROFILE", "optimistic")
    with pytest.raises(SystemExit):
        worker._estimator()


def test_routes_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AORTS_V11_ROUTES", "A1-B")
    assert worker._routes() == ("A1-B",)
