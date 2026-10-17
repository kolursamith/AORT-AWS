"""F/G. Control plane: dashboard metrics, admin gating, lab experiments, outbox + idempotent consumer."""

from __future__ import annotations

import time

from backend.aorts.events.consumer import IdempotentConsumer
from backend.aorts.events.publisher import LocalSink, OutboxRelay
from tests.trackb.conftest import ADMIN_TOKEN, pay

ADMIN = {"X-AORTS-Admin-Token": ADMIN_TOKEN}


def wait_experiment(control, exp_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = control.get(f"/api/v1/admin/experiments/{exp_id}", headers=ADMIN).json()
        if body["status"] != "RUNNING":
            return body
        time.sleep(0.5)
    raise AssertionError("experiment did not finish")


def test_health_endpoints(stack_factory):
    st = stack_factory(with_control=True)
    for client in (st.remitter, st.switch, st.control, *st.beneficiaries.values()):
        assert client.get("/api/v1/health").json()["status"] == "ok"


def test_dashboard_summary_has_meaningful_metrics(stack_factory):
    st = stack_factory(with_control=True)
    pay(st)
    pay(st, beneficiary="B-CUST-9999")
    pay(st, simulate="lost-response")
    s = st.control.get("/api/v1/metrics/summary").json()
    assert s["payments"]["total"] == 3
    assert s["payments"]["successful"] == 1 and s["payments"]["failed_or_reversed"] == 1
    assert s["payments"]["ambiguous_backlog"] == 1
    assert s["ledger_integrity"]["ok"] is True
    k = s["kpis"]
    assert k["payments_created_60s"] == 3 and k["payment_goodput_per_s_60s"] > 0
    assert (
        k["deadline_population"] == 0 and k["deadline_compliance"] is None
    )  # 1 ambiguous, not yet due
    assert set(s["quotas"]) == {"A1", "A2", "A3"} and "credits" in s["quotas"]["A3"]
    ts = st.control.get("/api/v1/metrics/timeseries?seconds=60").json()
    assert sum(r["n"] for r in ts["payments_created"]) == 3


def test_admin_and_lab_endpoints_are_protected(stack_factory):
    st = stack_factory(with_control=True)
    assert (
        st.control.post("/api/v1/admin/faults", json={"target": "beneficiary:B"}).status_code == 403
    )
    assert st.control.get("/api/v1/audit").status_code == 403
    assert st.control.post("/api/v1/security/attacks/replay/run", json={}).status_code == 403
    assert (
        st.control.post(
            "/api/v1/security/attacks/replay/run", json={}, headers={"X-AORTS-Admin-Token": "wrong"}
        ).status_code
        == 403
    )
    r = st.control.post("/api/v1/admin/reset", json={"confirm": "yes"}, headers=ADMIN)
    assert r.status_code == 422  # wrong confirmation phrase: nothing erased


def test_lab_disabled_returns_404(stack_factory):
    st = stack_factory(with_control=True, enable_attack_controls=False, enable_fault_controls=False)
    assert (
        st.control.post("/api/v1/security/attacks/flooding/run", json={}, headers=ADMIN).status_code
        == 404
    )
    assert (
        st.control.post(
            "/api/v1/admin/faults", json={"target": "switch"}, headers=ADMIN
        ).status_code
        == 404
    )


def test_fault_injection_via_admin_api(stack_factory):
    st = stack_factory(with_control=True)
    r = st.control.post(
        "/api/v1/admin/faults", json={"target": "beneficiary:B", "unavailable": True}, headers=ADMIN
    )
    assert r.status_code == 200
    st.refresh_runtime()
    assert pay(st).json()["transaction"]["state"] == "AMBIGUOUS"
    assert pay(st, beneficiary="C-CUST-0001", bank="C").json()["transaction"]["state"] == "CLOSED"
    st.control.post("/api/v1/admin/faults/reset", headers=ADMIN)
    st.refresh_runtime()
    assert pay(st).json()["transaction"]["state"] == "CLOSED"


def test_replay_attack_via_lab(stack_factory):
    st = stack_factory(with_control=True)
    exp = st.control.post("/api/v1/security/attacks/replay/run", json={}, headers=ADMIN).json()[
        "experiment_id"
    ]
    result = wait_experiment(st.control, exp)
    assert result["status"] == "COMPLETED", result
    assert result["results"]["ledger_verification"]["ok"] is True


def test_poisoning_attack_via_lab_rejects_live_samples(stack_factory):
    st = stack_factory(with_control=True)
    exp = st.control.post(
        "/api/v1/security/attacks/poisoning/run", json={"target_route": "A1-B"}, headers=ADMIN
    ).json()["experiment_id"]
    result = wait_experiment(st.control, exp)
    assert result["status"] == "COMPLETED", result
    live = st.control.get("/api/v1/telemetry/recent").json()["items"]
    assert live and all(item["verdict"] != "TRUSTED" for item in live)


def test_flooding_attack_via_lab(stack_factory):
    st = stack_factory(with_control=True, bank_check_rate_per_sec=5, bank_check_burst=5)
    exp = st.control.post(
        "/api/v1/security/attacks/flooding/run",
        json={"duration_seconds": 1.5, "rate_per_second": 40, "defenses_enabled": True},
        headers=ADMIN,
    ).json()["experiment_id"]
    result = wait_experiment(st.control, exp)
    assert result["status"] == "COMPLETED", result
    a3 = result["results"]["arms"]["defenses_on"]["summary"]["per_bank"]["A3"]
    assert a3["outcomes"].get("RATE_LIMITED", 0) > 0


def test_telemetry_endpoint_requires_registered_probe(stack_factory):
    st = stack_factory(with_control=True)
    obs = {
        "event_id": "tel-x1",
        "source_id": "twin-probe-1",
        "route_id": "A1-B",
        "sequence": 1,
        "timestamp": "2026-10-14T10:00:00Z",
        "throughput_tps": 5.0,
    }
    assert (
        st.client.post("control", "/api/v1/telemetry/route", obs, as_identity="A3").status_code
        == 403
    )
    r = st.client.post("control", "/api/v1/telemetry/route", obs, as_identity="twin-probe-1")
    assert (
        r.status_code == 200
    )  # accepted for validation; the old timestamp makes the verdict REJECTED
    assert r.json()["verdict"] == "REJECTED"


def test_outbox_relay_and_idempotent_consumer(stack):
    pay(stack)
    sink = LocalSink()
    handled = []
    consumer = IdempotentConsumer(
        stack.db, "test-consumer", lambda s, e: handled.append(e["event_id"]) or {}
    )
    sink.subscribers.append(consumer)
    relay = OutboxRelay(stack.db, sink, batch_size=100)
    n = relay.run_once()
    assert (
        n == 3 and relay.run_once() == 0
    )  # DEBITED, CONFIRMED_SUCCESS, CLOSED; each published once
    for e in list(sink.recent):  # simulate at-least-once redelivery of every event
        assert consumer(e) is False
    assert len(handled) == n and consumer.duplicates == n


def test_reconcile_escalated_via_control(stack_factory):
    from tests.trackb.integration.test_reconciliation import escalate

    st = stack_factory(with_control=True, deadline_seconds=1)
    txn, _ = escalate(st, unavailable=True)
    r = st.control.post("/api/v1/admin/reconcile-escalated", headers=ADMIN).json()
    assert r["attempted"] == 1 and r["resolved"] == 1 and r["items"][0]["outcome"] == "REVERSED"
    assert st.control.post("/api/v1/admin/reconcile-escalated").status_code == 403


def test_controller_decision_dry_run(stack_factory):
    st = stack_factory(with_control=True)
    body = {
        "decision_request_id": "drq-1",
        "timestamp": "2026-10-14T10:00:00Z",
        "routes": [{"route_id": "A1-B", "backlog": 10}],
        "candidates": [],
    }
    r = st.client.post("control", "/api/v1/controller/decisions", body, as_identity="CONTROLLER")
    assert r.status_code == 200
    assert (
        r.json()["guard"]["A1-B"]["applied"] <= 2
    )  # no trusted telemetry -> conservative fallback


def test_twins_endpoint_and_rebuild_after_reset(stack_factory):
    st = stack_factory(with_control=True)
    pay(st, simulate="lost-response")
    body = st.control.get("/api/v1/twins").json()
    assert body["twins"]["unresolved_count"] == 1 and "A1-B" in body["twins"]["routes"]
    st.control.post(
        "/api/v1/admin/reset",
        json={"confirm": "RESET-SYNTHETIC-DATA", "snapshot": False},
        headers=ADMIN,
    )
    assert st.control.get("/api/v1/twins").json()["twins"]["transactions_tracked"] == 0
    assert st.control.get("/api/v1/experiments/strategy-summary").status_code in (200, 404)


def test_controller_switch_endpoint_validates_mode(stack_factory):
    st = stack_factory(with_control=True)
    assert (
        st.control.post(
            "/api/v1/admin/controller", json={"mode": "evil"}, headers=ADMIN
        ).status_code
        == 422
    )
    ok = st.control.post(
        "/api/v1/admin/controller", json={"mode": "aorts", "run_id": "r1"}, headers=ADMIN
    )
    assert ok.status_code == 200 and ok.json()["mode"] == "aorts"
    assert st.control.post("/api/v1/admin/controller", json={"mode": "aorts"}).status_code == 403
