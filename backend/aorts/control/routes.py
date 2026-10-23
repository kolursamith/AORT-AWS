"""Control / operations API: dashboard reads, admin + lab controls, telemetry and controller adapters."""

from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from backend.aorts.app.dependencies import (
    ctx_of,
    error_response,
    require_admin,
    require_flag,
    require_roles,
)
from backend.aorts.app.local_stack import reset_database
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.requests import (
    AttackRunRequest,
    DefenseConfig,
    DispatchResultObservation,
    FaultConfig,
    RecoveryDecisionRequest,
    RouteTelemetry,
)
from backend.aorts.control.experiments import save_json
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.security.audit import make_record, verify_chain
from backend.aorts.security.request_auth import VerifiedIdentity

router = APIRouter(prefix="/api/v1")
ADMIN = [Depends(require_admin)]
FAULTS = [Depends(require_flag("enable_fault_controls")), Depends(require_admin)]
ATTACKS = [Depends(require_flag("enable_attack_controls")), Depends(require_admin)]
RESET_PHRASE = "RESET-SYNTHETIC-DATA"


def st(request: Request):
    return request.app.state


# ---------------------------------------------------------------- health / dashboard reads
@router.get("/health")
def health(request: Request):
    return {
        "status": "ok",
        "service": "control",
        "controller": st(request).controller.health(),
        "lab_enabled": ctx_of(request).settings.enable_attack_controls,
        "faults_enabled": ctx_of(request).settings.enable_fault_controls,
    }


@router.get("/metrics/summary")
def summary(request: Request):
    data = st(request).metrics.summary()
    data["ledger_integrity"] = st(request).ledger.verify_ledger()
    data["telemetry_recent"] = st(request).telemetry.recent[-20:]
    data["twin"] = {k: {"version": v, **d} for k, (v, d) in st(request).twin.items().items()}
    data["traffic"] = st(request).traffic_status()
    return data


@router.get("/metrics/timeseries")
def timeseries(request: Request, seconds: int = Query(120, ge=10, le=600)):
    return st(request).metrics.timeseries(seconds)


@router.get("/decisions")
def decisions(request: Request, limit: int = Query(50, ge=1, le=500)):
    return {
        "items": st(request).metrics.decisions(limit),
        "label": "controller recommendations (predicted) and guard-applied budgets",
    }


@router.get("/audit", dependencies=ADMIN)
def audit(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    before_seq: int | None = None,
    action: str | None = None,
    transaction_id: str | None = None,
):
    return st(request).metrics.audit(
        limit=limit, before_seq=before_seq, action=action, transaction_id=transaction_id
    )


@router.get("/audit/verify", dependencies=ADMIN)
def audit_verify(request: Request):
    ctx_of(request).audit_buffer.flush()
    with ctx_of(request).db.session() as s:
        return verify_chain(s)


@router.get("/security/events")
def security_events(
    request: Request, limit: int = Query(100, ge=1, le=1000), before_seq: int | None = None
):
    return st(request).metrics.audit(limit=limit, before_seq=before_seq, security_only=True)


# ---------------------------------------------------------------- telemetry + twin
@router.post("/telemetry/route")
def ingest_telemetry(
    obs: RouteTelemetry,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("telemetry_source")),
):
    a = st(request).telemetry.ingest(obs, authenticated_source=ident.identity)
    return a.as_dict()


@router.post("/lab/telemetry/inject", dependencies=ATTACKS)
def inject_telemetry(
    obs: RouteTelemetry, request: Request, as_source: str | None = Query(default=None)
):
    """TEST-ONLY ingestion: lets the lab present an observation as coming from ``as_source`` (or nobody)."""
    return (
        st(request)
        .telemetry.ingest(obs, authenticated_source=as_source, experiment_id="lab-inject")
        .as_dict()
    )


@router.get("/telemetry/recent")
def telemetry_recent(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"items": st(request).telemetry.recent[-limit:]}


@router.get("/twin/routes")
def twin_routes(request: Request):
    return {k: {"version": v, **d} for k, (v, d) in st(request).twin.items().items()}


# ---------------------------------------------------------------- twins + AORTS + strategy comparison
@router.get("/twins")
def twins(request: Request, max_txns: int = Query(100, ge=1, le=1000)):
    """Route/Transaction Twin state (built here from recorded events) + the AORTS controller's last snapshot."""
    from sqlalchemy import func, select

    from backend.aorts.banking.ledger.models import OutboxEvent

    engine = st(request).twin_engine
    with ctx_of(request).db.session() as s:
        max_id = s.execute(select(func.max(OutboxEvent.id))).scalar() or 0
    if max_id < engine.outbox_cursor:  # the database was reset: rebuild from the (new) event log
        engine.reset()
    engine.poll()
    try:
        r = ctx_of(request).client.get(
            "switch", "/api/v1/metrics/routes", as_identity="CONTROLLER", timeout=1.0
        )
        engine.observe_measurements(r.json().get("routes", {}))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)
    snap = engine.snapshot(max_txns=max_txns)
    aorts = st(request).twin.get("aorts:snapshot")
    dstats = st(request).twin.get("dispatcher:stats")
    return {
        "twins": snap,
        "aorts": aorts[1] if aorts else None,
        "dispatcher": dstats[1] if dstats else None,
        "label": "twins are projections of recorded events; the ledger remains authoritative",
    }


@router.get("/experiments/strategy-summary")
def strategy_summary(request: Request):
    from backend.aorts.control.experiments import RESULTS_DIR

    path = RESULTS_DIR / "processed" / "strategy-summary.json"
    if not path.exists():
        return error_response(
            ErrorCode.TRANSACTION_NOT_FOUND, "no strategy comparison analysed yet", 404
        )
    import json

    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- controller adapter (Track A)
@router.get("/controller/health")
def controller_health(request: Request):
    return st(request).controller.health()


@router.post("/controller/decisions")
def controller_decision(
    req: RecoveryDecisionRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("controller", "dispatcher")),
):
    """Dry run: ask the controller and show what the budget guard WOULD apply. Nothing is dispatched."""
    decision = st(request).controller.decide(req)
    defenses = ctx_of(request).runtime.defenses()
    guarded = {}
    for route in req.routes:
        rid = route["route_id"]
        integrity = st(request).twin.get(f"integrity:{rid}")
        guarded[rid] = (
            st(request)
            .guard.apply(
                rid,
                decision.route_budgets.get(rid, 0),
                telemetry=integrity[1] if integrity else None,
                measured=route.get("measured"),
                enabled=defenses.telemetry_validation,
            )
            .as_dict()
        )
    return {"decision": decision.model_dump(mode="json"), "guard": guarded}


@router.post("/controller/observations")
def controller_observation(
    obs: DispatchResultObservation,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("dispatcher", "controller")),
):
    st(request).controller.observe(obs.model_dump(mode="json"))
    return {"accepted": True}


# ---------------------------------------------------------------- admin: faults + defenses
@router.get("/admin/faults", dependencies=ADMIN)
def get_faults(request: Request):
    return {
        "faults": ctx_of(request).runtime.faults(),
        "defenses": ctx_of(request).runtime.defenses().model_dump(),
    }


@router.post("/admin/faults", dependencies=FAULTS)
def set_fault(cfg: FaultConfig, request: Request):
    ctx = ctx_of(request)
    ctx.runtime.set_fault(cfg, "ADMIN")
    ctx.audit_buffer.record(
        make_record(
            "FAULT_INJECTION_STARTED",
            "ACCEPTED",
            "ADMIN",
            reason=cfg.target,
            metadata=cfg.model_dump(),
        )
    )
    return {"faults": ctx.runtime.faults()}


@router.post("/admin/faults/reset", dependencies=FAULTS)
def reset_faults(request: Request):
    ctx = ctx_of(request)
    removed = ctx.runtime.reset_faults()
    ctx.audit_buffer.record(
        make_record(
            "FAULT_INJECTION_STOPPED",
            "ACCEPTED",
            "ADMIN",
            reason=f"removed {len(removed)} fault(s)",
            metadata={"removed": removed},
        )
    )
    return {"removed": removed}


@router.post("/admin/defenses", dependencies=FAULTS)
def set_defenses(cfg: DefenseConfig, request: Request):
    ctx = ctx_of(request)
    ctx.runtime.set_defenses(cfg, "ADMIN")
    ctx.audit_buffer.record(
        make_record(
            "ADMIN_CONFIG_CHANGE",
            "ACCEPTED",
            "ADMIN",
            reason="defenses updated",
            metadata=cfg.model_dump(),
        )
    )
    return cfg.model_dump()


# ---------------------------------------------------------------- admin: traffic, experiments, export, reset
@router.post("/admin/traffic/start", dependencies=FAULTS)
def traffic_start(request: Request, cfg: dict = Body(default_factory=dict)):
    state = st(request)
    if state.traffic and state.traffic_thread and state.traffic_thread.is_alive():
        return error_response(ErrorCode.VALIDATION_ERROR, "traffic already running", 409)
    tc = TrafficConfig(
        **{
            k: (tuple(v) if isinstance(v, list) else v)
            for k, v in cfg.items()
            if k in TrafficConfig.__dataclass_fields__
        }
    )
    tc.rps = min(tc.rps, 200.0)
    tc.duration_s = min(tc.duration_s, 600.0)
    gen = TrafficGenerator(
        ctx_of(request).client, tc, correlation_id=f"traffic-{datetime.now(UTC):%H%M%S}"
    )
    state.traffic = gen
    state.traffic_result = None

    def _run():
        state.traffic_result = gen.run()

    state.traffic_thread = threading.Thread(target=_run, daemon=True)
    state.traffic_thread.start()
    return {"started": True, "config": tc.__dict__}


@router.post("/admin/traffic/stop", dependencies=FAULTS)
def traffic_stop(request: Request):
    if st(request).traffic:
        st(request).traffic.stop()
    return {"stopped": True}


@router.post("/admin/controller", dependencies=FAULTS)
def set_controller(request: Request, body: dict = Body(...)):
    """Experiments: switch the dispatcher's recovery strategy at runtime. A new run_id = a fresh controller."""
    mode = str(body.get("mode", ""))
    allowed = {"mock", "aorts", "http"} | {
        f"baseline:{n}"
        for n in ("naive-polling", "npci-static", "exp-backoff-jitter", "fixed-budget", "aimd")
    }
    if mode not in allowed:
        return error_response(
            ErrorCode.VALIDATION_ERROR, f"mode must be one of {sorted(allowed)}", 422
        )
    cfg = {"mode": mode, "run_id": str(body.get("run_id") or datetime.now(UTC).isoformat())}
    ctx_of(request).runtime.put("controller", cfg, "ADMIN")
    ctx_of(request).audit_buffer.record(
        make_record(
            "ADMIN_CONFIG_CHANGE", "ACCEPTED", "ADMIN", reason=f"controller -> {mode}", metadata=cfg
        )
    )
    return cfg


@router.post("/admin/reconcile-escalated", dependencies=ADMIN)
def reconcile_escalated(request: Request, limit: int = Query(20, ge=1, le=200)):
    """Ask the remitter to reconcile escalated (HANDOFF_UDIR) transactions via its signed API."""
    from sqlalchemy import text

    ctx = ctx_of(request)
    with ctx.db.session() as s:
        ids = list(
            s.execute(
                text(
                    "SELECT transaction_id FROM transactions WHERE state='HANDOFF_UDIR' "
                    "ORDER BY updated_at LIMIT :n"
                ),
                {"n": limit},
            ).scalars()
        )
    results = []
    for txn_id in ids:
        r = ctx.client.post(
            "remitter", f"/api/v1/payments/{txn_id}/reconcile", {}, as_identity="DISPATCHER"
        )
        body = r.json() if r.content else {}
        results.append(
            {
                "transaction_id": txn_id,
                "http_status": r.status_code,
                "resolved": body.get("resolved"),
                "reason": body.get("reason") or body.get("error_code"),
                "outcome": (body.get("result") or {}).get("financial_outcome"),
            }
        )
    return {
        "attempted": len(ids),
        "resolved": sum(1 for x in results if x["resolved"]),
        "items": results,
    }


@router.post("/security/attacks/flooding/run", dependencies=ATTACKS)
def attack_flooding(req: AttackRunRequest, request: Request):
    return {"experiment_id": st(request).lab.flooding(req), "status": "RUNNING"}


@router.post("/security/attacks/poisoning/run", dependencies=ATTACKS)
def attack_poisoning(req: AttackRunRequest, request: Request):
    return {"experiment_id": st(request).lab.poisoning(req), "status": "RUNNING"}


@router.post("/security/attacks/replay/run", dependencies=ATTACKS)
def attack_replay(req: AttackRunRequest, request: Request):
    return {"experiment_id": st(request).lab.replay(req), "status": "RUNNING"}


@router.get("/admin/experiments", dependencies=ADMIN)
def list_experiments(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"items": st(request).registry.list(limit)}


@router.get("/admin/experiments/{experiment_id}", dependencies=ADMIN)
def get_experiment(experiment_id: str, request: Request):
    exp = st(request).registry.get(experiment_id)
    if exp is None:
        return error_response(ErrorCode.TRANSACTION_NOT_FOUND, "experiment not found", 404)
    return exp


@router.get("/admin/export", dependencies=ADMIN)
def export(request: Request, fmt: str = Query("json", pattern="^(json|csv)$")):
    from sqlalchemy import text

    with ctx_of(request).db.session() as s:
        rows = [
            dict(r)
            for r in s.execute(
                text(
                    "SELECT transaction_id, route_id, amount_paise, state, financial_outcome, version, checks_used, "
                    "created_at, ambiguous_since, resolved_at, deadline_at FROM transactions ORDER BY created_at"
                )
            )
            .mappings()
            .all()
        ]
    if fmt == "csv":
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    payload = {
        "exported_at": datetime.now(UTC).isoformat(),
        "summary": st(request).metrics.summary(),
        "transactions": rows,
    }
    path = save_json("processed", f"export-{datetime.now(UTC):%Y%m%dT%H%M%S}.json", payload)
    return {"saved_to": path, "transactions": len(rows), "summary": payload["summary"]}


@router.post("/admin/reset", dependencies=FAULTS)
def reset(request: Request, body: dict = Body(...)):
    """Wipe and re-seed SYNTHETIC data. Requires the exact confirmation phrase; snapshots first."""
    ctx = ctx_of(request)
    if body.get("confirm") != RESET_PHRASE or ctx.settings.environment == "aws":
        return error_response(
            ErrorCode.VALIDATION_ERROR, f"send {{'confirm': '{RESET_PHRASE}'}} (local only)", 422
        )
    ctx.audit_buffer.flush()
    path = None
    if body.get(
        "snapshot", True
    ):  # experiments may opt out explicitly (the phrase is still required)
        snapshot = {
            "summary": st(request).metrics.summary(),
            "ledger_integrity": st(request).ledger.verify_ledger(),
        }
        with ctx.db.session() as s:
            snapshot["audit_chain"] = verify_chain(s)
        path = save_json(
            "raw", f"pre-reset-snapshot-{datetime.now(UTC):%Y%m%dT%H%M%S}.json", snapshot
        )
    reset_database(ctx.db, ctx.settings.accounts_seed_balance_paise)
    # Fresh validator state too (sequence numbers, seen ids, EWMA baselines) so runs are independent.
    from backend.aorts.security.telemetry_validator import TelemetryValidator

    old = st(request).telemetry.validator
    st(request).telemetry.validator = TelemetryValidator(
        sources=old.sources,
        max_age_s=int(old.max_age.total_seconds()),
        max_future_s=int(old.max_future.total_seconds()),
        min_confidence=old.min_confidence,
        tolerance=old.tolerance,
        bias_check=old.bias_check,
    )
    st(request).telemetry.recent.clear()
    return {"reset": True, "snapshot_saved_to": path}
