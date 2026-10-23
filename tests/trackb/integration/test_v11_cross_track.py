"""Stage 09R: real Track B -> v1.1 -> Track A -> Track B integration against PostgreSQL.

Every test runs Track B's real remitter, switch and beneficiaries (HMAC-signed HTTP through the
in-process stack) on PostgreSQL, produces v1.1 messages from what Track B recorded, delivers
them to a real Track A ``ControllerCore`` (twins, Stage 06 estimator, Stage 07 look-ahead,
Stage 08 optimizer) over its FastAPI transport, and executes the decisions through Track B's
v1.1 dispatcher tick and execution gate. Numbers in comments refer to the 09R test list.

``FAST`` compresses the estimator's time constants (seconds-long windows instead of minutes) so
a positive decision can be reached in a test; it is declared here, not a production setting.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend.aorts.adapters.controller_client_v11 import V11ControllerClient
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.models import (
    EvidenceRecord,
    OutboxEvent,
    TelemetryWindow,
    Transaction,
    V11DeadLetter,
    V11DeliveryState,
)
from backend.aorts.contracts.requests import FaultConfig
from backend.aorts.integration.v11 import (
    LEDGER_SOURCE,
    Delivery,
    WindowProducer,
    export_event,
    export_window,
    telemetry_source,
    trackb_timing_policy,
)
from contracts.v1_1 import DecisionRequest, RouteDescriptor
from contracts.v1_1.adapters import Adapted
from contracts.v1_1.protocol import ControllerOutcome, encode
from intelligence.controller.api import SystemClock, create_app
from intelligence.controller.service import ControllerConfig, ControllerCore, RouteRegistration
from intelligence.ml.capacity_estimator import CapacityEstimatorConfig, EstimatorMode
from intelligence.twin.route_twin import RouteTwinConfig
from tests.trackb.conftest import OPENING, balance, pay
from tests.trackb.integration.test_team_contract import v1_pay

EXP = "exp-09r"
ROUTES = ("A1-B", "A1-C")
CONTROLLER = "aorts-controller"
FAST = CapacityEstimatorConfig(
    min_usable_windows=5,
    sustain_s=3.0,
    lag_allowance_s=0.2,
    conservation_block_s=3.0,
    conservation_lifetime_s=3.0,
    recovery_clean_s=3.0,
    evidence_horizon_s=120.0,
    point_horizon_s=60.0,
    baseline_horizon_s=120.0,
    ewma_tau_s=3.0,
    gap_horizon_s=8.0,
)


def track_a(settings, *, estimator=None):
    policy = trackb_timing_policy(settings)
    regs = tuple(
        RouteRegistration(
            descriptor=RouteDescriptor(
                schema_version="1.1",
                experiment_id=EXP,
                route_id=r,
                remitter_bank_id="A1",
                switch_id="SW",
                beneficiary_bank_id=r.split("-")[1],
            ),
            telemetry_sources=(telemetry_source(r),),
        )
        for r in ROUTES
    )
    extra = {"estimator": estimator} if estimator is not None else {}
    config = ControllerConfig(
        experiment_id=EXP,
        timing_policy=policy,
        routes=regs,
        event_sources=(LEDGER_SOURCE,),
        twin=RouteTwinConfig(
            model_version="rt-1",
            max_observation_age_s=30.0,
            max_future_skew_s=2.0,
            freshness_threshold_s=5.0,
        ),
        **extra,
    )
    token = secrets.token_urlsafe(24)
    core = ControllerCore(config, SystemClock())
    return core, TestClient(create_app(core, token)), token, policy


EPS = 0.03  # close just after the second boundary the switch's metrics roll on


def _next_second() -> int:
    return math.floor(time.time()) + 1


def _sleep_until(t: float) -> None:
    while (left := t - time.time()) > 0:
        time.sleep(min(left, 0.05))


class World:
    """One Track B stack + one Track A controller, wired with the v1.1 producers."""

    def __init__(self, stack_factory, estimator=None, **settings):
        base = dict(
            experiment_id=EXP, check_backoff_base_seconds=0.0, check_backoff_max_seconds=0.0
        )
        self.st = stack_factory(**{**base, **settings})
        self.core, self.client, self.token, self.policy = track_a(
            self.st.settings, estimator=estimator
        )
        self.delivery = Delivery(
            self.st.db,
            self.client,
            token=self.token,
            experiment_id=EXP,
            policy_id=self.policy.policy_id,
        )
        self.producer = WindowProducer(
            self.st.db,
            experiment_id=EXP,
            routes=["A1-B"],
            measured=lambda: self.st.switch_service.route_measurements(self._span),
        )
        self._span = 1
        self._boundary = _next_second()
        _sleep_until(self._boundary + EPS)
        self.producer.start = self.producer.db_now()

    def v11(self, policy=None, http=None):
        return V11ControllerClient(
            http or self.client,
            token=self.token,
            controller_id=CONTROLLER,
            experiment_id=EXP,
            timing_policy=policy or self.policy,
        )

    def dispatcher(self, v11=None, **kw):
        return Dispatcher(
            self.st.contexts["remitter"],
            self.st.remitter_service,
            twin=LocalVersionedStore(),
            queue=LocalQueue(2),
            v11=v11 or self.v11(),
            **kw,
        )

    def window(self, honest: int, lost: int = 0, report_override=None, seconds: int = 1):
        """One window aligned to whole wall-clock seconds, so the switch's independent
        per-second measurement covers exactly the same interval as the remitter's window."""

        for _ in range(honest):
            pay(self.st)
        for _ in range(lost):
            pay(self.st, simulate="lost-response")
        end = max(_next_second(), self._boundary + seconds)
        self._span, self._boundary = end - self._boundary, end
        _sleep_until(end + EPS)
        row = self.producer.close("A1-B", report_override=report_override)
        self.delivery.deliver_telemetry()
        self.delivery.deliver_events()
        return row

    def ready(self):
        """Demonstrate capacity (high load, <=1 lost confirmation per window), then calm."""

        for _ in range(14):
            self.window(12, lost=1)
        for i in range(16):  # 3/s over 2 s: enough samples for the switch to corroborate
            self.window(6, seconds=2)
            est = self.core._routes["A1-B"].latest_estimate
            # a window the validator flags (e.g. a genuine latency spike) is a sequence gap
            # for the estimator; keep running honest calm windows until it has aged out
            if i >= 9 and est.mode is EstimatorMode.TRUSTED and est.safe_headroom_rps >= 1:
                break
        assert est.mode is EstimatorMode.TRUSTED and est.safe_headroom_rps >= 1, (
            est,
            self.undelivered(),
        )
        return self

    def undelivered(self):
        with self.st.db.session() as s:
            rows = (
                s.execute(select(TelemetryWindow).where(TelemetryWindow.verdict != "TRUSTED"))
                .scalars()
                .all()
            )
            dead = s.execute(select(V11DeadLetter)).scalars().all()
            return (
                [(r.sequence, r.verdict, r.last_error, r.reasons) for r in rows],
                [(d.stream, d.error_code, d.detail[:120]) for d in dead],
            )

    def decision(self, route="A1-B"):
        req = DecisionRequest(
            schema_version="1.1", experiment_id=EXP, route_id=route, requested_at=datetime.now(UTC)
        )
        decision, refusal, record = self.core.decide(encode(req))
        assert refusal is None
        return decision, record

    def txn(self, txn_id):
        with self.st.db.session() as s:
            return s.get(Transaction, txn_id)

    def count(self, sql, **params):
        with self.st.db.session() as s:
            return s.execute(text(sql), params).scalar()


@pytest.fixture
def world(stack_factory):
    return World(stack_factory)


@pytest.fixture
def ready(stack_factory):
    return World(stack_factory, estimator=FAST).ready()


def exported(w):
    with w.st.db.session() as s:
        return [
            export_event(s, r, experiment_id=EXP, policy_id=w.policy.policy_id)
            for r in s.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars()
        ]


# ============================================================ telemetry (1-3)


def test_01_valid_window_round_trips_into_the_route_twin(world):
    w = world
    for _ in range(3):
        row = w.window(6)
    adapted = export_window(row)
    assert isinstance(adapted, Adapted)
    t = adapted.value.telemetry
    assert t.observation_window_end > t.observation_window_start
    assert t.source_id == telemetry_source("A1-B") and t.sequence == row.sequence
    assert t.payment_rps == pytest.approx(row.measurement["sends"] / 1.0, rel=0.2)
    snap = w.core._route_twin.snapshot(EXP, "A1-B")
    assert {m.source_id: m.sequence for m in snap.latest_sequence_per_source}[
        telemetry_source("A1-B")
    ] == row.sequence
    with w.st.db.session() as s:
        seqs = [
            r.sequence
            for r in s.execute(select(TelemetryWindow).order_by(TelemetryWindow.id)).scalars()
        ]
    assert seqs == list(range(len(seqs)))  # genuine, gap-free per-source sequence


def test_02_poisoned_window_never_becomes_trusted(world):
    w = world
    for _ in range(8):
        w.window(8)
    trusted_before = w.core._route_twin.snapshot(EXP, "A1-B").latest_trusted_observation
    # real traffic in the poisoned window; only the reported throughput lies
    poisoned = w.window(8, report_override={"throughput_tps": 200.0})
    assert poisoned.verdict in ("SUSPICIOUS", "REJECTED"), poisoned.reasons
    w.delivery.deliver_telemetry()
    snap = w.core._route_twin.snapshot(EXP, "A1-B")
    assert snap.latest_trusted_observation == trusted_before  # trusted view unchanged
    flagged = snap.latest_flagged_observation
    if poisoned.verdict == "SUSPICIOUS":  # delivered, but only into the flagged view
        assert flagged.telemetry.sequence == poisoned.sequence
    else:  # REJECTED windows are withheld from the controller entirely
        assert flagged is None or flagged.telemetry.sequence != poisoned.sequence


def test_03_stale_or_replayed_window_is_rejected(world):
    w = world
    for _ in range(3):
        row = w.window(5)
    replay = export_window(row).value
    r = w.client.post(
        "/v1.1/telemetry", content=encode(replay), headers={"Authorization": f"Bearer {w.token}"}
    )
    assert r.status_code == 200 and r.json()["error_code"] == "DUPLICATE_SEQUENCE"
    with w.st.db.session() as s:
        first = s.execute(select(TelemetryWindow).order_by(TelemetryWindow.id)).scalars().first()
    r2 = w.client.post(
        "/v1.1/telemetry",
        content=encode(export_window(first).value),
        headers={"Authorization": f"Bearer {w.token}"},
    )
    assert r2.status_code in (200, 409)  # duplicate or out-of-order: never re-applied
    snap = w.core._route_twin.snapshot(EXP, "A1-B")
    assert {m.source_id: m.sequence for m in snap.latest_sequence_per_source}[
        telemetry_source("A1-B")
    ] == row.sequence


# ============================================================ events (4-10)


def test_04_lost_response_payment_enters_the_transaction_twin_with_exact_versions(world):
    w = world
    txn_id = pay(w.st, simulate="lost-response").json()["transaction"]["transaction_id"]
    events = [e.value for e in exported(w)]
    assert [(e.event_type.value, e.state_version) for e in events] == [
        ("PAYMENT_INITIATED", 1),
        ("PAYMENT_TIMED_OUT", 2),
    ]
    assert all(e.financial_outcome.value == "UNKNOWN" for e in events)
    assert w.delivery.deliver_events().delivered == 2
    p = w.core._txn_twin.snapshot(EXP, txn_id)
    assert p.workflow_state.value == "AMBIGUOUS" and p.version == w.txn(txn_id).version


def test_05_verified_success_closes_the_twin_with_its_evidence(world):
    w = world
    txn_id = pay(w.st, simulate="lost-response").json()["transaction"]["transaction_id"]
    res = w.st.ledger.start_check(txn_id, w.txn(txn_id).version)
    out = w.st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    assert out["status"] == "SUCCESS"
    with w.st.db.session() as s:
        rec = s.get(EvidenceRecord, out["evidence_id"])
        assert rec.verified and rec.key_id and rec.signature_hex  # as received, verified
    report = w.delivery.deliver_events()
    assert report.dead_lettered == 0 and report.status_results == 1
    p = w.core._txn_twin.snapshot(EXP, txn_id)
    assert p.terminal and p.financial_outcome.value == "SUCCESS"
    assert p.last_evidence_reference.evidence_id == out["evidence_id"]


def test_06_verified_failure_is_reversed_once_and_exported_as_failure(world):
    w = world
    txn_id = pay(w.st, simulate="timeout", amount=8_000).json()["transaction"]["transaction_id"]
    res = w.st.ledger.start_check(txn_id, w.txn(txn_id).version)
    out = w.st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    assert out["status"] == "FAILED" and w.txn(txn_id).state == "REVERSED"
    assert balance(w.st.db, "A1-CUST-0001") == OPENING  # reversed exactly once
    w.delivery.deliver_events()
    p = w.core._txn_twin.snapshot(EXP, txn_id)
    assert p.financial_outcome.value == "FAILURE" and p.ledger_state_observed.value == "REVERSED"
    kinds = [e.value.event_type.value for e in exported(w) if isinstance(e, Adapted)]
    assert "REVERSAL_COMPLETED" in kinds and kinds.count("REVERSAL_COMPLETED") == 1
    with w.st.db.session() as s:
        dead = [d.detail for d in s.execute(select(V11DeadLetter)).scalars()]
    assert len(dead) == 1 and "REVERSAL_INITIATED" in dead[0]  # documented, not representable


def test_07_unknown_and_pending_stay_unresolved(world, monkeypatch):
    w = world
    w.st.set_fault(FaultConfig(target="beneficiary:B", drop_response_rate=1.0))
    pending = pay(w.st, sender="A1-CUST-0004", beneficiary="B-CUST-0004").json()["transaction"]
    w.st.reset_faults()
    assert pending["state"] == "AMBIGUOUS"
    t2 = pay(w.st, simulate="lost-response", sender="A1-CUST-0002").json()["transaction"]
    res = w.st.ledger.start_check(t2["transaction_id"], w.txn(t2["transaction_id"]).version)
    client = w.st.remitter_service.ctx.client
    real_post = client.post

    def timing_out(target, path, *a, **kw):  # the check request times out on the wire
        if path == "/api/v1/check-status":
            raise httpx.ReadTimeout("injected check timeout")
        return real_post(target, path, *a, **kw)

    monkeypatch.setattr(client, "post", timing_out)
    out = w.st.remitter_service.check_status(t2["transaction_id"], expected_version=res.new_version)
    monkeypatch.undo()
    assert out["status"] is None and w.txn(t2["transaction_id"]).state == "AMBIGUOUS"
    w.delivery.deliver_events()
    by_type = {}
    for e in (x.value for x in exported(w) if isinstance(x, Adapted)):
        by_type.setdefault(e.event_type.value, []).append(e)
    pending_ev = next(
        e
        for e in by_type["PAYMENT_RESPONSE_RECEIVED"]
        if e.transaction_id == pending["transaction_id"]
    )
    assert pending_ev.financial_outcome.value == "PENDING" and pending_ev.evidence_reference
    timed_out = by_type["STATUS_CHECK_TIMED_OUT"][0]
    assert timed_out.financial_outcome.value == "UNKNOWN"  # transport failure is not FAILURE
    assert timed_out.result_code.value == "CHECK_TIMEOUT" and timed_out.counted_check_attempts == 1
    for txn in (pending["transaction_id"], t2["transaction_id"]):
        assert w.core._txn_twin.snapshot(EXP, txn).financial_outcome.value in ("UNKNOWN", "PENDING")


def test_08_duplicate_delivery_is_harmless(world):
    w = world
    txn_id = pay(w.st, simulate="lost-response").json()["transaction"]["transaction_id"]
    assert w.delivery.deliver_events().delivered == 2
    with w.st.db.session() as s:  # simulate a crash before the cursor advanced
        s.get(V11DeliveryState, w.delivery.cursor_stream).last_id = 0
    again = w.delivery.deliver_events()
    assert again.duplicates == 2 and again.delivered == 0 and again.dead_lettered == 0
    assert w.core._txn_twin.snapshot(EXP, txn_id).version == 2


def test_09_contradictory_event_is_refused(world):
    w = world
    txn_id = pay(w.st, simulate="lost-response").json()["transaction"]["transaction_id"]
    w.delivery.deliver_events()
    good = exported(w)[1].value
    forged = good.model_copy(
        update={
            "event_id": "evt-forged-1",
            "financial_outcome": "UNKNOWN",
            "counted_check_attempts": 3,
        }
    )
    r = w.client.post(
        "/v1.1/transaction-events",
        content=encode(forged),
        headers={"Authorization": f"Bearer {w.token}"},
    )
    assert r.status_code == 409
    p = w.core._txn_twin.snapshot(EXP, txn_id)
    assert p.version == 2  # the forged version-2 content conflicts and changes nothing financial


def test_10_check_identity_and_counted_attempts_are_preserved(world):
    w = world
    txn_id = pay(w.st, simulate="lost-response").json()["transaction"]["transaction_id"]
    res = w.st.ledger.start_check(txn_id, w.txn(txn_id).version)
    check_id = w.txn(txn_id).check_in_flight_id
    w.st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    events = [e.value for e in exported(w) if isinstance(e, Adapted)]
    d = next(e for e in events if e.event_type.value == "STATUS_CHECK_DISPATCHED")
    a = next(e for e in events if e.event_type.value == "STATUS_CHECK_ANSWERED")
    assert d.check_id == a.check_id == check_id and a.counted_check_attempts == 1


def test_10b_credit_rejection_is_exported_as_rejected_and_not_counted(stack_factory):
    w = World(stack_factory, tier2_credit_cap=1, tier2_credit_refill_per_sec=0.001)
    ids = [
        pay(w.st, simulate="lost-response", sender=f"A1-CUST-000{i}").json()["transaction"][
            "transaction_id"
        ]
        for i in (1, 2)
    ]
    for txn_id in ids:
        res = w.st.ledger.start_check(txn_id, w.txn(txn_id).version)
        w.st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    events = [e.value for e in exported(w) if isinstance(e, Adapted)]
    rejected = [e for e in events if e.event_type.value == "STATUS_CHECK_REJECTED"]
    assert len(rejected) == 1 and rejected[0].result_code.value == "CREDIT_EXHAUSTED"
    assert w.txn(rejected[0].transaction_id).checks_used == 0


# ============================================================ controller and gate (11-24)


def test_11_to_15_real_decision_is_dispatched_and_the_result_returns(ready):
    w = ready
    decision, record = w.decision()
    assert record.source == "optimizer" and decision.decision_mode.value == "NORMAL"
    assert decision.reason_code.value == "SAFE_BUDGET_SELECTED", decision.reason_detail
    assert decision.predicted_new_ambiguous is not None  # Stage 07 forecast was used
    est = w.core._routes["A1-B"].latest_estimate  # Stage 06 estimate on real telemetry
    assert est.mode is EstimatorMode.TRUSTED and decision.budget_rps <= est.safe_headroom_rps
    for ref in decision.state_versions:  # the twin's versions are Track B's versions
        assert ref.version == w.txn(ref.transaction_id).version
    tick = w.dispatcher().tick(inline=True)
    route = tick["routes"]["A1-B"]
    assert route["outcome"] == "DECISION" and route["source"] == "controller"
    assert route["dispatched"] >= 1
    report = w.delivery.deliver_events()
    assert report.status_results >= 1 and report.dead_lettered == 0
    closed = [t for t in decision.selected_transaction_ids if w.txn(t).state == "CLOSED"]
    assert closed
    for txn_id in closed:  # the genuine result is visible to the controller's next decision
        assert w.core._txn_twin.snapshot(EXP, txn_id).terminal
    nxt, _ = w.decision()
    assert not set(nxt.selected_transaction_ids) & set(closed)


def test_16_to_20_execution_gate_rejects_stale_terminal_expired_route_and_policy(ready):
    w = ready
    client = w.v11()
    request, response, failure = client.request("A1-B", now=datetime.now(UTC))
    assert failure is None and response.status_code == 200
    selected = response.json()["selected_transaction_ids"]
    assert len(selected) >= 2
    stale, terminal = selected[0], selected[1]
    # real concurrent changes to authoritative rows between decision and dispatch
    w.st.ledger.start_check(stale, w.txn(stale).version)  # another dispatcher took it
    res = w.st.ledger.start_check(terminal, w.txn(terminal).version)
    w.st.remitter_service.check_status(terminal, expected_version=res.new_version)
    rows = w.dispatcher()._rows(selected)
    plan = client.gate(request, response, rows, now=datetime.now(UTC), rate_allowance=100)
    refused = {t: r.value for t, r in plan.refused}
    assert (
        refused[stale] in ("STALE_VERSION", "CHECK_IN_FLIGHT") and refused[terminal] == "TERMINAL"
    )
    expired = client.gate(
        request, response, rows, now=datetime.now(UTC) + timedelta(seconds=30), rate_allowance=100
    )
    assert expired.outcome is ControllerOutcome.DECISION_EXPIRED and not expired.admitted
    _, other_resp, _ = client.request("A1-C", now=datetime.now(UTC))
    wrong = client.gate(request, other_resp, rows, now=datetime.now(UTC), rate_allowance=100)
    assert wrong.outcome is ControllerOutcome.IDENTITY_MISMATCH
    from tests.intelligence.test_transaction_twin import make_policy

    mismatch = w.v11(policy=make_policy(policy_id="trackb-other-policy"))
    plan2 = mismatch.gate(request, response, rows, now=datetime.now(UTC), rate_allowance=100)
    assert plan2.outcome is ControllerOutcome.TIMING_POLICY_MISMATCH and not plan2.admitted


def test_21_concurrent_dispatchers_never_dispatch_a_check_twice(ready):
    w = ready
    results = []
    dispatchers = [w.dispatcher(), w.dispatcher()]
    threads = [
        threading.Thread(target=lambda d=d: results.append(d.tick(inline=True)))
        for d in dispatchers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    started = w.count("SELECT count(*) FROM transaction_events WHERE event_type='CHECK_STARTED'")
    distinct = w.count(
        "SELECT count(DISTINCT (transaction_id, previous_version)) FROM "
        "transaction_events WHERE event_type='CHECK_STARTED'"
    )
    dispatched = sum(r["dispatched"] for r in results)
    assert started == distinct == dispatched >= 1  # every start won its own compare-and-set
    with w.st.db.session() as s:  # the compare-and-set itself: a second start on one version
        txn_id, version = s.execute(
            text("SELECT transaction_id, version FROM transactions WHERE state='AMBIGUOUS' LIMIT 1")
        ).one()
    w.st.ledger.start_check(txn_id, version)
    with pytest.raises(SafetyViolation):
        w.st.ledger.start_check(txn_id, version)


def test_22_quota_limits_bind_at_the_backend(ready):
    w = ready
    d = w.dispatcher()
    d.guard.hard_cap = 0  # Track B's per-route rate allowance this tick
    route = d.tick(inline=True)["routes"]["A1-B"]
    assert route["outcome"] == "DECISION" and route["dispatched"] == 0
    refusals = set(route["gate_refused"].values())
    assert refusals and refusals <= {"RATE_LIMITED", "OVER_BUDGET"}


def test_23_controller_outage_uses_the_labelled_trackb_fallback(ready):
    w = ready

    def down(_request):
        raise httpx.ConnectError("controller down")

    d = w.dispatcher(
        v11=w.v11(
            http=httpx.Client(transport=httpx.MockTransport(down), base_url="http://controller")
        )
    )
    route = d.tick(inline=True)["routes"]["A1-B"]
    assert route["outcome"] == "CONTROLLER_UNAVAILABLE" and route["source"] == "trackb_fallback"
    assert 1 <= route["dispatched"] <= w.st.settings.fallback_budget_per_route
    assert route["decision_id"] is None  # never attributed to the controller


def test_24_explicit_fail_closed_never_triggers_the_fixed_fallback(ready):
    w = ready
    assert w.count("SELECT count(*) FROM transactions WHERE state='AMBIGUOUS'") > 0
    w.core._routes["A1-B"].estimator.latch_fail_closed()
    route = w.dispatcher().tick(inline=True)["routes"]["A1-B"]
    assert route["outcome"] == "EXPLICIT_FAIL_CLOSED"
    assert route["source"] == "fail_closed_honoured" and route["dispatched"] == 0


def test_25_legacy_v1_client_still_works(world):
    r = v1_pay(world.st, key="v1-09r-000001", experiment_id="exp-v1")
    assert r.status_code == 202, r.text
