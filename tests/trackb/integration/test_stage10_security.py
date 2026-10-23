"""Stage 10: security, resilience and the three attacks against the integrated AORTS loop.

PostgreSQL-backed (``AORTS_REQUIRE_DB=1`` fails instead of skipping). Every test drives Track B's
real services and, where a controller is involved, a real Track A ``ControllerCore`` through
:class:`backend.aorts.integration.loop.AortsLoop`. Attacks reuse Track B's own generators
(``run_flood``, the validator, the replay simulator). Shorter than the Stage 10 experiments;
the experiments (``backend.aorts.experiments.stage10_security``) carry the paired-seed results.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from sqlalchemy import select, text

from backend.aorts.banking.ledger.models import EvidenceRecord, OutboxEvent, TelemetryWindow
from backend.aorts.contracts.requests import DefenseConfig, FaultConfig
from backend.aorts.dashboard import v11_metrics
from backend.aorts.experiments.run_flooding import build_pool
from backend.aorts.experiments.stage10_security import LIES, safety, steps, warm_up
from backend.aorts.integration.loop import (
    AortsLoop,
    ControllerFaults,
    LoopConfig,
    Workload,
    ambiguous_backlog,
)
from backend.aorts.integration.replay_v11 import run_v11_replay_cases
from backend.aorts.integration.v11 import export_event, export_window, telemetry_source
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.security.replay_simulator import ReplaySimulator
from backend.aorts.security.request_auth import (
    H_RESPONSE_KEY_ID,
    H_RESPONSE_SIGNATURE,
    H_RESPONSE_TIMESTAMP,
)
from contracts.v1_1.adapters import Adapted
from contracts.v1_1.protocol import encode
from tests.trackb.conftest import OPENING, balance, pay

EXP = "exp-s10"


def loop_for(stack_factory, *, backlog: int = 20, validator=None, **settings) -> AortsLoop:
    st = stack_factory(experiment_id=EXP, **settings)
    if backlog:
        ambiguous_backlog(st, {"B": backlog, "C": backlog})
    return AortsLoop(st, LoopConfig(experiment_id=EXP, window_s=1), validator=validator)


def run(loop: AortsLoop, wl: Workload, seconds: float) -> list[dict]:
    """Step the loop for ``seconds``; ``wl`` keeps sending in its own thread."""

    del wl
    return steps(loop, seconds, "test")


_WORKLOADS: list[Workload] = []


@pytest.fixture(autouse=True)
def _close_workloads(stack_factory):
    """Stop every workload thread before its stack is torn down."""

    yield
    while _WORKLOADS:
        _WORKLOADS.pop().close()


def ready_loop(stack_factory, **kw):
    loop = loop_for(stack_factory, **kw)
    wl = Workload(loop.st, seed=101)
    _WORKLOADS.append(wl)
    warm = warm_up(loop, wl)
    assert warm["ready"], (warm, loop.ticks[-1])
    return loop, wl


def b(tick, route="A1-B"):
    return tick["routes"][route]


def no_financial_violation(st):
    s = safety(st)
    assert s == {**s, "ledger_ok": True, "duplicate_check_starts": 0, "multiple_reversals": 0,
                 "multiple_debits": 0, "unverified_evidence_applied": 0}, s  # fmt: skip


# ======================================================================= 1. flooding


def _flood(st, *, rate, seconds=3.0, defended=True, dest="B", legit_rps=3.0):
    st.set_defenses(DefenseConfig(rate_limiting=defended, tier2_credits=defended))
    pool = build_pool(st.client, st.settings.admin_token.get_secret_value(),
                      {"A3": "B", "A1": dest, "A2": dest})  # fmt: skip
    st.switch_service.admission.reset()
    cfg = FloodConfig(attack_rps=rate, duration_s=seconds, legit_rps_per_bank=legit_rps,
                      destination="B", seed=7, tail_s=1.0)  # fmt: skip
    return run_flood(st.client, pool, cfg).summary(bank_limit=st.settings.bank_check_rate_per_sec)


def test_flooding_is_contained_and_quota_enforced(stack_factory):
    st = stack_factory(experiment_id=EXP)
    s = _flood(st, rate=60.0)
    a3, a1, a2 = (s["per_bank"][k] for k in ("A3", "A1", "A2"))
    limit = st.settings.bank_check_rate_per_sec
    assert a3["outcomes"].get("RATE_LIMITED", 0) > 0  # per-bank quota fired
    assert a3["admitted_ok"] <= (limit * 1.5) * 4 + st.settings.bank_check_burst  # bounded
    assert s["attack_containment_s"] is not None
    assert a1["admitted_ratio"] >= 0.95 and a2["admitted_ratio"] >= 0.95  # legit protected
    no_financial_violation(st)


def test_multi_bank_fairness_under_flood(stack_factory):
    st = stack_factory(experiment_id=EXP)
    s = _flood(st, rate=60.0)
    ratios = [s["per_bank"][b_]["admitted_ratio"] for b_ in ("A1", "A2")]
    jain = sum(ratios) ** 2 / (len(ratios) * sum(r * r for r in ratios))
    assert jain >= 0.99, ratios  # the attacker's flood does not starve one honest bank
    assert s["legit_share_of_admitted"] > 0.25  # honest banks keep a real share of admissions


def test_flood_on_shared_route_keeps_parallel_route_and_valid_checks(stack_factory):
    loop, wl = ready_loop(stack_factory)
    res = {}
    fl = threading.Thread(target=lambda: res.update(s=_flood(loop.st, rate=60.0, seconds=6.0)))
    fl.start()
    ticks = []
    while fl.is_alive():
        ticks += run(loop, wl, 2.0)
    fl.join()
    c_dispatched = sum(b(t, "A1-C").get("dispatched") or 0 for t in ticks)
    assert c_dispatched >= 1  # the healthy route keeps genuine AORTS recovery
    assert all(b(t, "A1-C").get("source") in ("controller", "fail_closed_honoured", None)
               for t in ticks)  # fmt: skip
    closed_c = [r for r in wl.records if r["destination"] == "C"]
    assert closed_c and all(r["state"] == "CLOSED" for r in closed_c)  # payments unharmed
    with loop.st.db.session() as s:  # valid checks completed with verified evidence
        verified = s.execute(text(
            "SELECT count(*) FROM evidence_records WHERE evidence_kind='STATUS_CHECK_RESPONSE' "
            "AND verified")).scalar()  # fmt: skip
    assert verified >= 1
    no_financial_violation(loop.st)


def test_credit_exhaustion_is_rejected_and_not_counted(stack_factory):
    st = stack_factory(experiment_id=EXP, tier2_credit_cap=1, tier2_credit_refill_per_sec=0.001)
    loop = AortsLoop(st, LoopConfig(experiment_id=EXP, window_s=1, estimator=None))
    ids = [pay(st, simulate="lost-response", sender=f"A1-CUST-000{i}").json()["transaction"][
        "transaction_id"] for i in (1, 2)]  # fmt: skip
    for txn_id in ids:
        with st.db.session() as s:
            v = s.execute(text("SELECT version FROM transactions WHERE transaction_id=:t"),
                          {"t": txn_id}).scalar()  # fmt: skip
        res = st.ledger.start_check(txn_id, v)
        st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    with st.db.session() as s:
        summary = v11_metrics.checks_and_backlog(s, EXP)
    assert summary["rejected_checks_by_code"].get("CREDIT_EXHAUSTED") == 1
    with st.db.session() as s:
        used = dict(s.execute(text("SELECT transaction_id, checks_used FROM transactions "
                                   "WHERE transaction_id = ANY(:i)"), {"i": ids}).all())  # fmt: skip
    assert sorted(used.values()) == [0, 1]  # the rejected check is not counted
    del loop


# ======================================================================= 2. poisoning


def test_inflated_throughput_never_raises_the_budget(stack_factory):
    loop, wl = ready_loop(stack_factory)
    before = max((b(t).get("budget_rps") or 0) for t in loop.ticks[-3:])
    trusted_before = loop.core._route_twin.snapshot(EXP, "A1-B").latest_trusted_observation
    for f in (FaultConfig(target="route:B", capacity_workers=1),
              FaultConfig(target="beneficiary:B", latency_ms=400)):  # fmt: skip
        loop.st.set_fault(f)  # the real route degrades while the source lies
    loop.overrides["A1-B"] = LIES["inflate_100"]
    ticks = run(loop, wl, 10.0)
    loop.st.reset_faults()
    verdicts = [b(t).get("verdict") for t in ticks]
    assert sum(v != "TRUSTED" for v in verdicts if v) >= len([v for v in verdicts if v]) // 2
    assert max((b(t).get("budget_rps") or 0) for t in ticks) <= before
    trusted = loop.core._route_twin.snapshot(EXP, "A1-B").latest_trusted_observation
    if (
        trusted is not None
        and trusted_before is not None
        and trusted.sequence > trusted_before.sequence
    ):
        assert trusted.observed_throughput_rps < 50  # an inflated window never became trusted
    no_financial_violation(loop.st)


def test_deceptive_latency_is_flagged_or_capped(stack_factory):
    loop, wl = ready_loop(stack_factory)
    before = max((b(t).get("budget_rps") or 0) for t in loop.ticks[-3:])
    loop.st.set_fault(FaultConfig(target="beneficiary:B", latency_ms=600))
    loop.overrides["A1-B"] = LIES["latency_low"]
    ticks = run(loop, wl, 10.0)
    loop.st.reset_faults()
    flagged = [t for t in ticks if b(t).get("verdict") not in (None, "TRUSTED")]
    assert flagged  # the switch's independent latency contradicts the lie
    assert max((b(t).get("budget_rps") or 0) for t in ticks) <= before
    assert sum(b(t).get("dispatched") or 0 for t in ticks) <= len(ticks) * max(before, 1)


def test_stale_duplicate_wrong_source_and_other_experiment_telemetry_is_refused(stack_factory):
    loop, wl = ready_loop(stack_factory)
    with loop.st.db.session() as s:
        rows = s.execute(select(TelemetryWindow).where(TelemetryWindow.route_id == "A1-B",
                         TelemetryWindow.verdict == "TRUSTED").order_by(TelemetryWindow.sequence)
                         ).scalars().all()  # fmt: skip
        for r in rows:
            s.expunge(r)
    good = export_window(rows[-1]).value
    hdr = {"Authorization": f"Bearer {loop.token}"}
    seq = good.telemetry.sequence

    def post(msg) -> httpx.Response:
        body = msg if isinstance(msg, bytes) else encode(msg)
        return loop.client.post("/v1.1/telemetry", content=body, headers=hdr)

    assert post(good).json()["error_code"] == "DUPLICATE_SEQUENCE"  # replayed signed window
    assert post(export_window(rows[-3]).value).status_code in (200, 409)  # stale sequence
    wrong = good.model_copy(update={"telemetry": good.telemetry.model_copy(
        update={"source_id": telemetry_source("A1-C")})})  # fmt: skip
    assert post(wrong).json()["error_code"] == "UNKNOWN_SOURCE"  # another route's source
    rogue = good.model_copy(update={"telemetry": good.telemetry.model_copy(
        update={"source_id": "rogue-probe"})})  # fmt: skip
    assert post(rogue).json()["error_code"] == "UNKNOWN_SOURCE"
    body = json.loads(encode(good))
    body["experiment_id"] = body["telemetry"]["experiment_id"] = "other-exp"
    assert post(json.dumps(body).encode()).json()["error_code"] == "EXPERIMENT_MISMATCH"
    body = json.loads(encode(good))
    body["telemetry"].pop("observation_window_start")
    assert post(json.dumps(body).encode()).status_code == 422  # missing observation window
    snap = loop.core._route_twin.snapshot(EXP, "A1-B")
    assert {m.source_id: m.sequence for m in snap.latest_sequence_per_source}[
        telemetry_source("A1-B")] == seq  # fmt: skip
    del wl


def test_telemetry_reports_the_payment_path_only(stack_factory):
    """Regression (found in Stage 10): own status checks inflated the cross-checked report."""

    loop = loop_for(stack_factory, backlog=0)
    for _ in range(6):
        pay(loop.st)
    ids = [pay(loop.st, simulate="lost-response", sender="A1-CUST-0002").json()["transaction"][
        "transaction_id"] for _ in range(3)]  # fmt: skip
    for txn_id in ids:
        with loop.st.db.session() as s:
            v = s.execute(text("SELECT version FROM transactions WHERE transaction_id=:t"),
                          {"t": txn_id}).scalar()  # fmt: skip
        loop.st.remitter_service.check_status(
            txn_id, expected_version=loop.st.ledger.start_check(txn_id, v).new_version
        )
    row = loop.producer.close("A1-B")
    d = (row.window_end - row.window_start).total_seconds()
    m = row.measurement
    assert m["check_answers"] == 3
    assert m["reported"]["throughput_tps"] == pytest.approx(m["payment_responses"] / d)
    exported = export_window(row).value.telemetry
    assert exported.observed_throughput_rps == pytest.approx(
        (m["payment_responses"] + m["check_answers"]) / d
    )  # the v1.1 total keeps the checks
    assert exported.completed_check_rps == pytest.approx(3 / d)


# ======================================================================= 3. replay


def test_v11_replay_cases_on_postgresql(stack_factory):
    st = stack_factory(experiment_id=EXP)
    loop = AortsLoop(st, LoopConfig(experiment_id=EXP, window_s=1, estimator=None))
    cases = run_v11_replay_cases(st, loop, seed=1)
    bad = [(c["case"], c["observed"]) for c in cases if not c["as_expected"]]
    assert not bad, bad
    assert {c["case"] for c in cases} >= {
        "duplicate_payment", "duplicate_event_delivery", "duplicate_status_result",
        "concurrent_duplicate_result", "wrong_transaction_id", "wrong_route",
        "conflicting_authoritative_outcome", "old_transaction_version",
        "unregistered_event_source", "invalid_source_signature", "expired_evidence",
        "answer_for_other_transaction", "duplicate_reversal_completion",
    }  # fmt: skip
    no_financial_violation(st)


def test_trackb_concurrent_replay_is_idempotent(stack_factory):
    st = stack_factory(experiment_id=EXP)
    sim = ReplaySimulator(st.client, st.db, st.settings.admin_token.get_secret_value())
    sim.case_concurrent_duplicate_events()
    sim.case_concurrent_reversals()
    for r in sim.records:
        assert (r.get("reversal_postings") or 0) <= 1, r
        assert (r.get("ledger_postings") or 0) <= 3, r  # DEBIT + CREDIT/REVERSAL, never doubled
    assert st.ledger.verify_ledger()["ok"]


@pytest.mark.parametrize("auto_reverse", [True, False])
def test_reversal_completion_carries_the_failure_evidence(stack_factory, auto_reverse):
    """Regression (found in Stage 10): an authorised manual reversal lost its evidence id."""

    st = stack_factory(experiment_id=EXP, auto_reverse_on_confirmed_failure=auto_reverse)
    loop = AortsLoop(st, LoopConfig(experiment_id=EXP, window_s=1, estimator=None))
    txn_id = pay(st, simulate="timeout").json()["transaction"]["transaction_id"]
    with st.db.session() as s:
        v = s.execute(text("SELECT version FROM transactions WHERE transaction_id=:t"),
                      {"t": txn_id}).scalar()  # fmt: skip
    st.remitter_service.check_status(
        txn_id, expected_version=st.ledger.start_check(txn_id, v).new_version
    )
    if not auto_reverse:
        with st.db.session() as s:
            v = s.execute(text("SELECT version FROM transactions WHERE transaction_id=:t"),
                          {"t": txn_id}).scalar()  # fmt: skip
        r = st.client.post("remitter", f"/api/v1/payments/{txn_id}/reversal",
                           {"operation_id": f"{txn_id}:REVERSAL", "expected_version": v,
                            "reason": "test"}, as_identity="CONTROLLER")  # fmt: skip
        assert r.status_code == 200, r.text
    assert balance(st.db, "A1-CUST-0001") == OPENING
    with st.db.session() as s:
        rows = s.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars().all()
        out = [export_event(s, r, experiment_id=EXP, policy_id=loop.policy.policy_id) for r in rows]
    done = [e.value for e in out if isinstance(e, Adapted)
            and e.value.event_type.value == "REVERSAL_COMPLETED"]  # fmt: skip
    assert len(done) == 1 and done[0].evidence_reference is not None


def test_malformed_signature_headers_are_stored_unverified_not_crashing(stack_factory, monkeypatch):
    """Regression (found in Stage 10): duplicated signature headers crashed evidence storage."""

    st = stack_factory(experiment_id=EXP)
    txn_id = pay(st, simulate="lost-response").json()["transaction"]["transaction_id"]
    client = st.remitter_service.ctx.client
    real = client.post

    def doubled(target, path, *a, **kw):
        resp = real(target, path, *a, **kw)
        if path != "/api/v1/check-status":
            return resp
        items = list(resp.headers.multi_items())
        for k in (H_RESPONSE_KEY_ID, H_RESPONSE_TIMESTAMP, H_RESPONSE_SIGNATURE):
            items.append((k, resp.headers[k]))  # a second value: the joined header is malformed
        return httpx.Response(200, content=resp.content, headers=items)

    with st.db.session() as s:
        v = s.execute(text("SELECT version FROM transactions WHERE transaction_id=:t"),
                      {"t": txn_id}).scalar()  # fmt: skip
    res = st.ledger.start_check(txn_id, v)
    monkeypatch.setattr(client, "post", doubled)
    out = st.remitter_service.check_status(txn_id, expected_version=res.new_version)
    monkeypatch.undo()
    assert out["status"] is None and out["unverified_status"] == "SUCCESS"  # never reported
    with st.db.session() as s:
        rec = s.get(EvidenceRecord, out["evidence_id"])
        assert not rec.verified and rec.signature_hex is None and rec.key_id is None
        state = s.execute(text("SELECT state FROM transactions WHERE transaction_id=:t"),
                          {"t": txn_id}).scalar()  # fmt: skip
    assert state == "AMBIGUOUS"


# ======================================================================= 4. resilience


def test_outage_fallback_is_labelled_and_recovery_resumes(stack_factory):
    loop, wl = ready_loop(stack_factory, backlog=80)  # the fallback must not drain it
    proxy = ControllerFaults(lambda: loop.client)
    loop.dispatch_http = proxy.client
    loop.dispatcher.v11 = loop.v11_client(http=proxy.client)
    cap = loop.st.settings.fallback_budget_per_route
    for mode in ("down", "http_500", "malformed", "wrong_version", "wrong_route"):
        proxy.mode = mode
        for t in run(loop, wl, 2.0):
            r = b(t)
            assert r["source"] == "trackb_fallback" and r["decision_id"] is None, (mode, r)
            assert r["dispatched"] <= cap
    proxy.mode, proxy.delay_s = "slow", 3.0  # beyond the client timeout
    for t in run(loop, wl, 2.0):
        assert b(t)["outcome"] == "CONTROLLER_TIMEOUT" and b(t)["source"] == "trackb_fallback"
    proxy.mode = None
    after = run(loop, wl, 4.0)
    assert all(b(t)["outcome"] in ("DECISION", "EXPLICIT_FAIL_CLOSED", "WARMING_UP")
               for t in after)  # fmt: skip
    assert any(b(t)["source"] == "controller" for t in after)  # genuine decisions resume
    no_financial_violation(loop.st)


def test_explicit_fail_closed_dispatches_nothing_and_no_fallback(stack_factory):
    loop, wl = ready_loop(stack_factory)
    for r in ("A1-B", "A1-C"):
        loop.core._routes[r].estimator.latch_fail_closed()
    ticks = run(loop, wl, 4.0)
    for t in ticks:
        for rid in ("A1-B", "A1-C"):
            r = b(t, rid)
            assert r["outcome"] == "EXPLICIT_FAIL_CLOSED" and r["dispatched"] == 0
            assert r["source"] == "fail_closed_honoured"


def test_trust_restoration_after_the_source_goes_silent(stack_factory):
    loop, wl = ready_loop(stack_factory)
    loop.telemetry_enabled = False
    silent = run(loop, wl, 8.0)
    assert b(silent[-1])["outcome"] == "EXPLICIT_FAIL_CLOSED"
    assert b(silent[-1])["decision_reason"] == "TELEMETRY_STALE"
    assert all(b(t).get("source") != "trackb_fallback" for t in silent)
    loop.telemetry_enabled = True
    back = run(loop, wl, 16.0)
    assert any((b(t).get("budget_rps") or 0) > 0 for t in back)  # safe again on fresh evidence


def test_controller_restart_fails_closed_then_rebuilds_from_the_outbox(stack_factory):
    loop, wl = ready_loop(stack_factory)
    loop.restart_controller(replay_outbox=False)
    loop.telemetry_enabled = False  # the watermark says the controller is behind Track B
    t = run(loop, wl, 2.0)[-1]
    assert (
        b(t)["outcome"] == "EXPLICIT_FAIL_CLOSED" and b(t)["decision_reason"] == "TELEMETRY_STALE"
    )
    loop.telemetry_enabled = True
    loop.delivery.replay_events()  # the documented operator procedure
    rep = loop.delivery.deliver_events()
    assert rep.delivered > 0 and rep.dead_lettered >= 0
    assert loop.core._txn_twin.transaction_ids(EXP)  # projections rebuilt, idempotently
    no_financial_violation(loop.st)


def test_event_delivery_backlog_is_caught_by_the_execution_gate(stack_factory):
    loop, wl = ready_loop(stack_factory)
    loop.events_enabled = False  # the controller's transaction twin falls behind the ledger
    ticks = run(loop, wl, 6.0)
    refusals = {r for t in ticks for r in (b(t).get("gate_refused") or {}).values()}
    refusals |= {r for t in ticks for r in (b(t, "A1-C").get("gate_refused") or {}).values()}
    assert refusals <= {"STALE_VERSION", "CHECK_IN_FLIGHT", "TERMINAL", "OVER_BUDGET",
                        "RATE_LIMITED", "NOT_ELIGIBLE"}  # fmt: skip
    loop.events_enabled = True
    run(loop, wl, 2.0)
    no_financial_violation(loop.st)


# ======================================================================= 5. dashboard, isolation


def test_dashboard_metrics_match_the_recorded_sources(stack_factory):
    loop, wl = ready_loop(stack_factory)
    run(loop, wl, 4.0)
    st = loop.st
    counters = st.switch_service.ctx.metrics.snapshot()["counters"]
    with st.db.session() as s:  # one snapshot for the view and the ground truth
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        live = v11_metrics.live_summary(s, experiment_id=EXP, switch_counters=counters,
                                        ledger_ok=st.ledger.verify_ledger()["ok"])  # fmt: skip
        truth_windows = dict(s.execute(text(
            "SELECT route_id, count(*) FROM v11_telemetry_windows WHERE experiment_id=:e GROUP BY 1"
        ), {"e": EXP}).all())  # fmt: skip
        truth_unresolved = s.execute(text(
            "SELECT count(*) FROM transactions WHERE experiment_id=:e AND state IN "
            "('DEBITED','AMBIGUOUS','CHECK_IN_FLIGHT')"), {"e": EXP}).scalar()  # fmt: skip
    st.contexts["remitter"].audit_buffer.flush()
    with st.db.session() as s:
        live_dec = v11_metrics.decisions(s, EXP)
    for rid in ("A1-B", "A1-C"):
        assert live["telemetry"][rid]["windows"] == truth_windows[rid], (
            live["telemetry"],
            truth_windows,
        )
        ticks = [t for t in loop.ticks if rid in t["routes"] and t["routes"][rid].get("source")]
        assert sum(live_dec["routes"][rid]["ticks"].values()) == len(ticks)
        assert sum(live_dec["routes"][rid]["dispatched"].values()) == sum(
            t["routes"][rid]["dispatched"] for t in ticks)  # fmt: skip
    assert live["checks_and_backlog"]["unresolved"] == truth_unresolved
    a1 = live["per_bank_checks"]["A1"]
    assert a1["received"] == counters[next(k for k in counters if k == "check_received{bank=A1}")]
    assert live["financial_safety"]["ledger_ok"] is True


def test_dashboard_endpoint_serves_the_same_view(stack_factory):
    st = stack_factory(with_control=True, experiment_id=EXP)
    pay(st, simulate="lost-response")
    r = st.client.get("control", f"/api/v1/aorts/v11/summary?experiment_id={EXP}",
                      as_identity="LAB")  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checks_and_backlog"]["unresolved"] == 1
    assert body["financial_safety"]["ledger_ok"] is True


def test_source_and_experiment_isolation(stack_factory):
    st = stack_factory(experiment_id=EXP)
    loop = AortsLoop(st, LoopConfig(experiment_id=EXP, window_s=1, estimator=None))
    other = AortsLoop(st, LoopConfig(experiment_id="exp-other", window_s=1, estimator=None))
    pay(st, simulate="lost-response")
    rep = other.delivery.deliver_events()  # another experiment's delivery skips our rows
    assert rep.delivered == 0
    assert not other.core._txn_twin.transaction_ids("exp-other")
    assert loop.delivery.deliver_events().delivered == 2
    with st.db.session() as s:
        first = s.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars().first()
        msg = export_event(s, first, experiment_id=EXP, policy_id=loop.policy.policy_id).value
    r = other.client.post("/v1.1/transaction-events", content=encode(msg),
                          headers={"Authorization": f"Bearer {other.token}"})  # fmt: skip
    assert r.json()["error_code"] == "EXPERIMENT_MISMATCH"
    r = loop.client.post("/v1.1/transaction-events", content=encode(msg),
                         headers={"Authorization": f"Bearer {other.token}"})  # fmt: skip
    assert r.status_code in (401, 403)  # one experiment's token never authorises another's
