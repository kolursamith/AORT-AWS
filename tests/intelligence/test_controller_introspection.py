"""Read-only controller diagnostics: complete, serialisable, authenticated, side-effect free."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from contracts.v1_1 import SCHEMA_VERSION, DecisionRequest
from contracts.v1_1.protocol import encode
from intelligence.common.clock import ManualClock
from intelligence.controller.api import create_app
from intelligence.experiments import controller_benchmark as cb

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def _warm_core():
    clock = ManualClock(T0)
    core, _ = cb._core(clock)
    for k in range(200):
        clock.set_time(T0 + timedelta(seconds=k + 1))
        assert core.ingest_telemetry(cb._window(T0, k, 30.0 if k < 120 else 20.0)).accepted
    for i in range(3):
        assert core.ingest_event(cb._event(T0, f"T{i}", "PAYMENT_INITIATED", 1, 100.0)).accepted
        ev = cb._event(T0, f"T{i}", "PAYMENT_TIMED_OUT", 2, 115.0, 3715.0)
        assert core.ingest_event(ev).accepted
    return core, clock


def _decide(core, clock):
    req = DecisionRequest(schema_version=SCHEMA_VERSION, experiment_id=cb.EXP,
                          route_id=cb.ROUTE, requested_at=clock.now())  # fmt: skip
    decision, refusal, _ = core.decide(encode(req))
    assert refusal is None
    return decision


def test_introspection_reports_estimate_optimisation_and_projections():
    core, clock = _warm_core()
    decision = _decide(core, clock)
    view = core.introspection()
    json.dumps(view)  # fully serialisable
    route = view["routes"][cb.ROUTE]
    assert route["snapshot"]["route_id"] == cb.ROUTE
    assert route["estimate"]["mode"] and "safe_headroom_rps" in route["estimate"]
    opt = route["optimisation"]
    assert opt["kind"] == "MODEL_PREDICTION"
    assert route["last_decision"]["decision_id"] == decision.decision_id
    assert opt["optimizer_decision"]["budget_rps"] == decision.budget_rps
    assert opt["selected_budget_rps"] == decision.budget_rps
    assert {p["transaction_id"] for p in view["transaction_projections"]} == {"T0", "T1", "T2"}
    assert view["decision_records"][-1]["decision_id"] == decision.decision_id


def test_introspection_has_no_side_effects():
    core, clock = _warm_core()
    _decide(core, clock)
    before = (core.status(), len(core.records), dict(core.counters))
    core.introspection()
    core.introspection()
    assert (core.status(), len(core.records), dict(core.counters)) == before


def test_introspection_endpoint_requires_the_bearer_token():
    core, clock = _warm_core()
    client = TestClient(create_app(core, "secret-token-for-tests"))
    assert client.get("/v1.1/introspection").status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.get("/v1.1/introspection", headers=bad).status_code == 401
    ok = client.get(
        "/v1.1/introspection", headers={"Authorization": "Bearer secret-token-for-tests"}
    )
    assert ok.status_code == 200 and ok.json()["experiment_id"] == cb.EXP
    assert "secret-token-for-tests" not in ok.text
