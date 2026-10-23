"""Stage 09: the v1.1 recovery controller service, end to end over HTTP.

The app is the real FastAPI transport over a ``ControllerCore`` with a ``ManualClock``; every
message is a strict v1.1 contract object sent as JSON; decisions are checked with the real
execution gate. No test reads simulator truth.
"""

import json
import logging
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from contracts.v1_1 import (
    SCHEMA_VERSION,
    DecisionMode,
    DecisionReason,
    DecisionRequest,
    RecoveryDecision,
    RouteDescriptor,
    RouteTelemetry,
    SourceSequenceMark,
    StatusCheckResult,
)
from contracts.v1_1.protocol import (
    ControllerOutcome,
    DispatchCandidate,
    DispatchLimits,
    decode,
    encode,
    gate_decision,
)
from intelligence.common.clock import ManualClock
from intelligence.controller import service as service_module
from intelligence.controller.api import create_app
from intelligence.controller.service import ControllerConfig, ControllerCore, RouteRegistration
from intelligence.twin.route_twin import RouteTwinConfig
from tests.intelligence.test_capacity_estimator import envelope
from tests.intelligence.test_transaction_twin import (
    T0,
    dispatched,
    initiated,
    make_policy,
    timed_out,
)

EXP = "EXP-1"
TOKEN = "test-backend-token-not-a-secret"  # noqa: S105 - test value
AUTH = {"Authorization": f"Bearer {TOKEN}"}
POLICY = make_policy()


def descriptor(route="A1-B", remitter="A1", beneficiary="B"):
    return RouteDescriptor(schema_version=SCHEMA_VERSION, experiment_id=EXP, route_id=route,
                           remitter_bank_id=remitter, switch_id="SW",
                           beneficiary_bank_id=beneficiary)  # fmt: skip


def config(routes=("A1-B",)):
    regs = []
    for r in routes:
        bank = r.split("-")[1]
        regs.append(RouteRegistration(descriptor=descriptor(r, "A1", bank),
                                      telemetry_sources=(f"src-{r}",)))  # fmt: skip
    return ControllerConfig(
        experiment_id=EXP, timing_policy=POLICY, routes=tuple(regs),
        event_sources=("remitter-A1",),
        twin=RouteTwinConfig(model_version="rt-1", max_observation_age_s=30.0,
                             max_future_skew_s=1.0, freshness_threshold_s=5.0),
    )  # fmt: skip


def window(k, route="A1-B", rate=20.0, queue=3, seq=None, exp=EXP):
    start = T0 + timedelta(seconds=k)
    n = int(rate)
    return RouteTelemetry(
        schema_version=SCHEMA_VERSION, experiment_id=exp, route_id=route,
        source_id=f"src-{route}", observation_scope="REMITTER_OWN_TRAFFIC",
        sequence=k if seq is None else seq, observation_window_start=start,
        observation_window_end=start + timedelta(seconds=1),
        observation_timestamp=start + timedelta(seconds=1), payment_rps=rate, check_rps=0.0,
        observed_throughput_rps=rate, p50_latency_ms=150.0, p95_latency_ms=300.0,
        latency_sample_count=n, timeout_rate=0.0, timeout_count=0, timeout_denominator_count=n,
        queue_depth=queue,
    )  # fmt: skip


def rate_at(k):
    """Open-route profile: 30 rps, then 45 rps (demonstrated headroom), then 20 rps."""

    return 30.0 if k < 120 else 45.0 if k < 270 else 20.0


def status_result(txn, check, result="AUTHORITATIVE_SUCCESS", verified=True, route="A1-B", t=0.0):
    evidence = result in ("AUTHORITATIVE_SUCCESS", "AUTHORITATIVE_FAILURE", "PENDING")
    return StatusCheckResult(
        schema_version=SCHEMA_VERSION, experiment_id=EXP, check_id=check, transaction_id=txn,
        route_id=route, check_result=result,
        evidence_reference={"evidence_id": f"EV-{check}", "evidence_kind": "STATUS_CHECK_RESPONSE",
                            "issuer_id": "SW", "response_version": 1} if evidence else None,
        evidence_signature={"signature_alg": "ed25519", "key_id": "sw-key-1",
                            "signature": "c2lnbmF0dXJlLXBsYWNlaG9sZGVyLWZvci10ZXN0cw==",
                            "signed_fields": ["check_result"]} if evidence else None,
        evidence_verified=verified and evidence, counts_against_limit=True,
        counted_check_attempts=1, latency_ms=150.0,
        responded_at=T0 + timedelta(seconds=t),
    )  # fmt: skip


class Harness:
    """Drives one core through its HTTP app in time order."""

    def __init__(self, routes=("A1-B",)):
        self.clock = ManualClock(T0)
        self.core = ControllerCore(config(routes), self.clock)
        self.client = TestClient(create_app(self.core, TOKEN))
        self.k = 0

    def post(self, path, message, headers=AUTH):
        body = encode(message) if hasattr(message, "model_dump") else message
        return self.client.post(f"/v1.1/{path}", content=body, headers=headers)

    def run_until(self, k_end, events=(), route="A1-B"):
        events = sorted(events, key=lambda e: e.event_timestamp)
        i = 0
        while self.k < k_end:
            self.clock.set_time(T0 + timedelta(seconds=self.k + 1))
            r = self.post("telemetry", envelope(window(self.k, route, rate_at(self.k))))
            assert r.status_code == 204, r.text
            while i < len(events) and events[i].event_timestamp <= self.clock.now():
                r = self.post("transaction-events", events[i])
                assert r.status_code == 204, (events[i].event_id, r.text)
                i += 1
            self.k += 1

    def request(self, route="A1-B", **kw):
        return DecisionRequest(schema_version=SCHEMA_VERSION, experiment_id=EXP, route_id=route,
                               requested_at=self.clock.now(), **kw)  # fmt: skip

    def decide(self, route="A1-B", **kw):
        return self.post("recovery/decisions", self.request(route, **kw))


def ambiguous_events(n=40, start=100.0, prefix="T", route="A1-B"):
    out = []
    for i in range(n):
        t = start + i
        txn = f"{prefix}{i:03d}"
        out.append(initiated(txn=txn, exp=EXP, t=t, route=route,
                             beneficiary=route.split("-")[1]))  # fmt: skip
        out.append(timed_out(txn=txn, exp=EXP, t=t + 15.0, deadline=t + 15.0 + 3600.0,
                             route=route, beneficiary=route.split("-")[1]))  # fmt: skip
    return out


def probe_history(h, n=10):
    """Ten earlier checks on other transactions, answered with verified evidence."""

    events = []
    for i in range(n):
        txn = f"P{i:03d}"
        events += [initiated(txn=txn, exp=EXP, t=10.0 + i),
                   timed_out(txn=txn, exp=EXP, t=25.0 + i, deadline=3625.0 + i),
                   dispatched(3, txn=txn, exp=EXP, check=f"CK-P{i}", t=110.0 + i)]  # fmt: skip
    return events


def gate_for(h, decision, request):
    cands = {}
    for txn in decision.selected_transaction_ids:
        p = h.core._txn_twin.snapshot(EXP, txn)
        cands[txn] = DispatchCandidate(
            transaction_id=txn, experiment_id=EXP, route_id=p.route_id, state_version=p.version,
            terminal=p.terminal, check_in_flight=p.check_in_flight_id is not None,
            counted_check_attempts=p.checks_used,
            eligible_from=p.initiated_at + timedelta(seconds=POLICY.first_check_delay_s),
            eligible_until=None,
        )  # fmt: skip
    return gate_decision(
        encode(decision), request=request, controller_id="aorts", authorized_controllers=
        frozenset({"aorts"}), timing_policy=POLICY, candidates=cands,
        limits=DispatchLimits(rate_allowance=1000), now=h.clock.now(), dispatch_window_s=5.0,
        max_clock_skew_s=1.0,
    )  # fmt: skip


@pytest.fixture(scope="module")
def ready_harness():
    h = Harness()
    h.run_until(420, ambiguous_events() + probe_history(h))
    for i in range(10):
        r = h.post("status-check-results", status_result(f"P{i:03d}", f"CK-P{i}", t=111.0 + i))
        assert r.status_code == 204, r.text
    return h


# ===================================================================== end to end


def test_end_to_end_decision_uses_twins_estimator_lookahead_and_optimizer(ready_harness):
    h = ready_harness
    request = h.request()
    r = h.post("recovery/decisions", request)
    assert r.status_code == 200, r.text
    d = decode(RecoveryDecision, r.content)
    assert d.decision_mode is DecisionMode.NORMAL
    assert d.reason_code is DecisionReason.SAFE_BUDGET_SELECTED, d.reason_detail
    estimate = h.core._routes["A1-B"].latest_estimate
    assert 1 <= d.budget_rps <= int(estimate.safe_headroom_rps)
    assert set(d.selected_transaction_ids) <= {f"T{i:03d}" for i in range(40)}
    assert all(ref.version == 2 for ref in d.state_versions)
    assert d.predicted_new_ambiguous is not None and d.prediction_horizon_s == 60.0
    record = h.core.decision_log()[-1]
    assert record.source == "optimizer" and record.decision_id == d.decision_id
    assert {name for name, _ in record.timings_ms} >= {"validation", "pool", "total"}
    plan = gate_for(h, d, request)
    assert plan.outcome is ControllerOutcome.DECISION and plan.refused == ()


def test_decision_ids_are_unique_per_call(ready_harness):
    h = ready_harness
    ids = {decode(RecoveryDecision, h.decide().content).decision_id for _ in range(3)}
    assert len(ids) == 3


def test_check_outcomes_count_only_verified_authority(ready_harness):
    h = ready_harness
    counts = h.core._observables(h.core._routes["A1-B"], h.clock.now(), 40).check_outcomes
    assert counts.authoritative == 10
    unverified = status_result("T039", "CK-U1", verified=False, t=400.0)
    assert not unverified.is_authoritative
    assert h.post("status-check-results", unverified).status_code == 204
    counts = h.core._observables(h.core._routes["A1-B"], h.clock.now(), 40).check_outcomes
    assert (counts.authoritative, counts.pending) == (10, 1)


# ===================================================================== authentication


def test_authentication_is_required_and_secrets_are_not_logged(ready_harness, caplog):
    h = ready_harness
    caplog.set_level(logging.INFO, logger="aorts.controller")
    assert h.client.get("/v1.1/health").status_code == 401
    bad = h.client.get("/v1.1/health", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.json()["error_code"] == "AUTHENTICATION_FAILED"
    basic = h.client.get("/v1.1/health", headers={"Authorization": f"Basic {TOKEN}"})
    assert basic.status_code == 401  # the right secret under the wrong scheme is refused
    ok = h.client.get("/v1.1/health", headers={**AUTH, "X-Correlation-ID": "corr-abc"})
    assert ok.status_code == 200 and ok.headers["X-Correlation-ID"] == "corr-abc"
    h.decide()
    assert TOKEN not in caplog.text


def test_readiness_reflects_telemetry():
    h = Harness()
    r = h.client.get("/v1.1/ready", headers=AUTH)
    assert r.status_code == 503 and r.json()["ready"] is False and r.json()["not_ready_reasons"]
    h.run_until(20)
    assert h.client.get("/v1.1/ready", headers=AUTH).status_code == 200


def test_unconfigured_service_is_unavailable_not_silent():
    client = TestClient(create_app(None, None))
    r = client.post("/v1.1/recovery/decisions", content=b"{}")
    assert r.status_code == 503 and r.json()["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert client.get("/health").json()["v1_1"] == "not configured"


# ===================================================================== intake rules


def test_telemetry_intake_rules():
    h = Harness()
    h.run_until(5)
    dup = h.post("telemetry", envelope(window(4, rate=rate_at(4))))
    assert dup.status_code == 200 and dup.json()["error_code"] == "DUPLICATE_SEQUENCE"
    other = h.post("telemetry", envelope(window(5, rate=30.0, exp="EXP-OTHER")))
    assert other.status_code == 409 and other.json()["error_code"] == "EXPERIMENT_MISMATCH"
    assert h.post("telemetry", b"{not json").json()["error_code"] == "SCHEMA_INVALID"
    data = json.loads(encode(envelope(window(5, rate=30.0))))
    data["schema_version"] = "9.9"
    assert h.post("telemetry", json.dumps(data).encode()).status_code == 422
    untrusted = envelope(window(5, rate=30.0), trust="UNAUTHENTICATED")
    assert h.post("telemetry", untrusted).json()["error_code"] == "UNTRUSTED_SOURCE"
    flagged = envelope(window(5, rate=500.0), flags=("STATISTICAL_ANOMALY",))
    assert h.post("telemetry", flagged).status_code == 204  # kept apart, never trusted
    estimate = h.core._routes["A1-B"].latest_estimate
    assert estimate.observed_throughput_rps == 30.0  # the flagged 500 rps never became trusted
    conflict = h.post("telemetry", envelope(window(4, rate=21.0)))  # same sequence, new body
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "CONFLICTING_SEQUENCE"


def test_event_intake_rules_and_idempotency():
    h = Harness()
    h.clock.set_time(T0 + timedelta(seconds=50))
    e1, e2 = initiated(txn="TX1", exp=EXP), timed_out(txn="TX1", exp=EXP)
    assert h.post("transaction-events", e1).status_code == 204
    assert h.post("transaction-events", e1).json()["error_code"] == "DUPLICATE_EVENT"
    assert h.post("transaction-events", e2).status_code == 204
    assert h.post("transaction-events", e1).status_code in (200, 409)  # never re-applied
    wrong_source = e2.model_copy(update={"source_id": "intruder", "event_id": "X"})
    r = h.post("transaction-events", json.dumps(wrong_source.model_dump(mode="json")).encode())
    assert r.json()["error_code"] == "UNKNOWN_SOURCE"
    conflict = timed_out(txn="TX1", exp=EXP, t=16.0, event_id="other-id")
    assert h.post("transaction-events", conflict).status_code == 409
    assert h.core._txn_twin.snapshot(EXP, "TX1").version == 2


def test_status_result_intake_rules():
    h = Harness()
    h.clock.set_time(T0 + timedelta(seconds=200))
    for e in (initiated(txn="TX1", exp=EXP), timed_out(txn="TX1", exp=EXP),
              dispatched(3, txn="TX1", exp=EXP)):  # fmt: skip
        assert h.post("transaction-events", e).status_code == 204
    unknown = h.post("status-check-results", status_result("NOPE", "CK-9"))
    assert unknown.status_code == 404 and unknown.json()["error_code"] == "UNKNOWN_TRANSACTION"
    good = status_result("TX1", "CHK-1", result="UNKNOWN")
    assert h.post("status-check-results", good).status_code == 204
    assert h.post("status-check-results", good).json()["error_code"] == "DUPLICATE_EVENT"
    changed = status_result("TX1", "CHK-1", result="PENDING")
    assert h.post("status-check-results", changed).json()["error_code"] == "DUPLICATE_CHECK_ID"
    elsewhere = status_result("TX1", "CHK-2", route="A1-C")
    assert h.post("status-check-results", elsewhere).status_code == 409
    from intelligence.controller.lookahead import CheckEvidence

    assert h.core._evidence["TX1"] is CheckEvidence.LAST_CHECK_UNKNOWN


# ===================================================================== decision rules


def test_decision_request_validation():
    h = Harness()
    h.run_until(30)
    starter = {"schema_version": "1.1", "experiment_id": EXP, "route_id": "A1-B",
               "requested_at": h.clock.now().isoformat(), "estimated_capacity_rps": 9}  # fmt: skip
    r = h.post("recovery/decisions", json.dumps(starter).encode())
    assert r.status_code == 422 and r.json()["error_code"] == "SCHEMA_INVALID"
    assert h.decide(route="A9-Z").json()["error_code"] == "ROUTE_IDENTITY_MISMATCH"
    old = h.request().model_copy(update={"requested_at": h.clock.now() - timedelta(seconds=60)})
    assert h.post("recovery/decisions", old).json()["error_code"] == "STALE_OBSERVATION"
    future = h.request().model_copy(update={"requested_at": h.clock.now() + timedelta(seconds=9)})
    assert h.post("recovery/decisions", future).json()["error_code"] == "FUTURE_TIMESTAMP"
    other = h.request().model_copy(update={"experiment_id": "EXP-OTHER"})
    assert h.post("recovery/decisions", other).json()["error_code"] == "EXPERIMENT_MISMATCH"


def test_lagging_watermark_fails_closed_and_is_attributed():
    h = Harness()
    h.run_until(30)
    mark = SourceSequenceMark(source_id="src-A1-B", sequence=999)
    d = decode(RecoveryDecision, h.decide(min_telemetry_watermark=(mark,)).content)
    assert d.decision_mode is DecisionMode.FAIL_CLOSED
    assert d.reason_code is DecisionReason.TELEMETRY_STALE and d.budget_rps == 0
    assert h.core.decision_log()[-1].source == "watermark_gate"


def test_warming_up_then_stale_telemetry_fail_closed():
    h = Harness()
    h.run_until(3)
    d = decode(RecoveryDecision, h.decide().content)
    assert d.decision_mode is DecisionMode.WARMING_UP and d.budget_rps == 0
    h.run_until(40)
    h.clock.advance(20.0)
    d = decode(RecoveryDecision, h.decide().content)
    assert (d.decision_mode, d.reason_code) == (DecisionMode.FAIL_CLOSED,
                                               DecisionReason.TELEMETRY_STALE)  # fmt: skip


def test_internal_error_fails_closed_with_its_true_cause(monkeypatch):
    h = Harness()
    h.run_until(30)

    def broken(*_a, **_k):
        raise RuntimeError("optimizer exploded")

    monkeypatch.setattr(service_module, "decide", broken)
    r = h.decide()
    d = decode(RecoveryDecision, r.content)
    assert d.decision_mode is DecisionMode.FAIL_CLOSED and d.budget_rps == 0
    record = h.core.decision_log()[-1]
    assert record.source == "internal_error" and "optimizer exploded" in record.cause
    assert record.source != "optimizer"  # never attributed to the optimizer


def test_fail_closed_decision_is_honoured_by_the_gate():
    h = Harness()
    h.run_until(30)
    request = h.request(min_telemetry_watermark=(SourceSequenceMark(source_id="src-A1-B",
                                                                    sequence=999),))  # fmt: skip
    d = decode(RecoveryDecision, h.post("recovery/decisions", request).content)
    plan = gate_for(h, d, request)
    assert plan.outcome is ControllerOutcome.EXPLICIT_FAIL_CLOSED and plan.admitted == ()
    assert not plan.backend_fallback_permitted


def test_route_isolation_between_two_routes():
    h = Harness(routes=("A1-B", "A1-C"))
    h.clock.set_time(T0 + timedelta(seconds=1))
    for k in range(420):
        h.clock.set_time(T0 + timedelta(seconds=k + 1))
        for route in ("A1-B", "A1-C"):
            assert h.post("telemetry", envelope(window(k, route, rate_at(k)))).status_code == 204
    # ambiguity only on A1-C: a decision for A1-B must never select it
    for e in sorted(ambiguous_events(5, start=250.0, prefix="C", route="A1-C"),
                    key=lambda e: e.event_timestamp):  # fmt: skip
        assert h.post("transaction-events", e).status_code == 204
    d = decode(RecoveryDecision, h.decide("A1-B").content)
    assert d.selected_transaction_ids == () and d.route_id == "A1-B"
    assert d.reason_code is DecisionReason.NO_ELIGIBLE_TRANSACTIONS


def test_concurrent_decisions_are_valid_and_distinct(ready_harness):
    h = ready_harness
    results, errors = [], []

    def worker():
        try:
            results.append(decode(RecoveryDecision, h.decide().content))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len({d.decision_id for d in results}) == 8


def test_deterministic_core_given_identical_inputs():
    def run():
        h = Harness()
        h.run_until(420, ambiguous_events())
        return decode(RecoveryDecision, h.decide().content)

    assert run() == run()


def test_legacy_v1_endpoint_is_preserved_and_labelled(ready_harness):
    r = ready_harness.client.post("/v1/recovery/decisions", json={
        "experiment_id": EXP, "route_id": "A1-B", "estimated_capacity_rps": 40.0,
        "confidence": 1.0, "pending_transaction_ids": ["a", "b", "c"]})  # fmt: skip
    assert r.status_code == 200 and "fallback" in r.json()["decision_reason"]


def test_benchmark_small_pool_is_within_its_declared_budget():
    from intelligence.experiments.controller_benchmark import BUDGETS_MS, run_size

    result = run_size(100, repeats=3)
    assert result["fallback_or_error_rate"] == 0.0
    assert result["phases"]["total"]["p95_ms"] <= BUDGETS_MS[100] * 4  # CI machines are slower
    assert result["response_bytes_max"] < 16_000
