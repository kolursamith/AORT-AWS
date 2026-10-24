"""HTTP transport for the AORTS recovery controller (v1.1 surface plus the preserved /v1 starter).

v1.1 endpoints (``contracts.v1_1.protocol.ENDPOINTS``), served by a ``ControllerCore``:

* ``POST /v1.1/telemetry``, ``/v1.1/transaction-events``, ``/v1.1/status-check-results``: 204 on
  acceptance; idempotent duplicates answer 200 with the DUPLICATE_* ``ErrorResponse``; every
  refusal is a structured ``ErrorResponse`` with the protocol's HTTP status.
* ``POST /v1.1/recovery/decisions``: a strict v1.1 ``RecoveryDecision`` (200) or a refusal.
* ``GET /v1.1/health`` (200) and ``GET /v1.1/ready`` (200, or 503 with reasons).

Transport boundary: every v1.1 call must carry ``Authorization: Bearer <token>`` matching the
configured Track B credential (compared in constant time; the token is never logged). The
transport layer, not the body, establishes the caller. ``X-Correlation-ID`` is echoed (one is
generated here when absent; only this boundary uses random identifiers and the wall clock).
Without configuration the v1.1 surface answers 503 UPSTREAM_UNAVAILABLE and is not ready.

Configuration: ``AORTS_CONTROLLER_CONFIG`` (path to a ``ControllerConfig`` JSON file) and
``AORTS_BACKEND_TOKEN`` (secret, environment only).

Legacy: ``GET /health`` and ``POST /v1/recovery/decisions`` keep the starter /v1 contract for
Track B's existing ``HttpController``. That path is the labelled conservative fallback (budget
= capacity x min(confidence, 0.25) from caller-supplied numbers); it does not use the twins or
the optimizer and must never be reported as an AORTS optimizer result.
"""

import hmac
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from contracts.v1.models import RecoveryDecision as LegacyRecoveryDecision
from contracts.v1_1 import ErrorCode
from contracts.v1_1.errors import HTTP_STATUS
from contracts.v1_1.protocol import MEDIA_TYPE, PATH_PREFIX, encode, error_response
from intelligence.controller.fallback import safe_budget
from intelligence.controller.service import ControllerConfig, ControllerCore, IntakeOutcome

logger = logging.getLogger("aorts.controller")
_CORRELATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SystemClock:
    """Wall clock for the production transport boundary only (tests inject ManualClock)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class LegacyDecisionRequest(BaseModel):
    experiment_id: str
    route_id: str
    estimated_capacity_rps: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    pending_transaction_ids: list[str] = Field(default_factory=list, max_length=10_000)


def _json(status: int, body: bytes, correlation_id: str) -> Response:
    return Response(
        content=body,
        status_code=status,
        media_type=MEDIA_TYPE,
        headers={"X-Correlation-ID": correlation_id},
    )


def create_app(core: ControllerCore | None, backend_token: str | None) -> FastAPI:
    app = FastAPI(title="AORTS recovery controller", version="1.1.0")
    experiment_id = core.config.experiment_id if core else None

    def correlation(request: Request) -> str:
        given = request.headers.get("X-Correlation-ID", "")
        return given if _CORRELATION.match(given) else f"corr-{uuid4().hex}"

    def refuse(code: ErrorCode, message: str, corr: str) -> Response:
        counted = (
            False if code in (ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.INTERNAL_ERROR) else None
        )
        body = error_response(
            code,
            message,
            experiment_id=experiment_id,
            correlation_id=corr,
            counts_against_limit=counted,
        )
        return _json(HTTP_STATUS[code], encode(body), corr)

    def guard(request: Request, corr: str) -> Response | None:
        if core is None or not backend_token:
            return refuse(ErrorCode.UPSTREAM_UNAVAILABLE, "controller is not configured", corr)
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme != "Bearer" or not token:
            return refuse(ErrorCode.AUTHENTICATION_FAILED, "missing bearer credential", corr)
        if not hmac.compare_digest(token.encode(), backend_token.encode()):
            return refuse(ErrorCode.AUTHENTICATION_FAILED, "credential not recognised", corr)
        return None

    def outcome(result: IntakeOutcome, corr: str, endpoint: str) -> Response:
        logger.info(
            "intake endpoint=%s status=%s correlation_id=%s", endpoint, result.status_code, corr
        )
        if result.accepted:
            return Response(status_code=204, headers={"X-Correlation-ID": corr})
        error = result.error.model_copy(update={"correlation_id": corr})
        return _json(result.status_code, encode(error), corr)

    async def intake(request: Request, handler, endpoint: str) -> Response:
        corr = correlation(request)
        if (denied := guard(request, corr)) is not None:
            return denied
        try:
            result = handler(await request.body(), corr)
        except Exception as exc:  # noqa: BLE001 - never leak internals; fail as a refusal
            logger.error(
                "intake endpoint=%s internal error %s correlation_id=%s",
                endpoint,
                type(exc).__name__,
                corr,
            )
            return refuse(ErrorCode.INTERNAL_ERROR, "internal error during intake", corr)
        return outcome(result, corr, endpoint)

    @app.post(f"{PATH_PREFIX}/telemetry")
    async def telemetry(request: Request) -> Response:
        return await intake(request, core.ingest_telemetry if core else None, "telemetry")

    @app.post(f"{PATH_PREFIX}/transaction-events")
    async def events(request: Request) -> Response:
        return await intake(request, core.ingest_event if core else None, "transaction-events")

    @app.post(f"{PATH_PREFIX}/status-check-results")
    async def status_results(request: Request) -> Response:
        return await intake(
            request, core.ingest_status_result if core else None, "status-check-results"
        )

    @app.post(f"{PATH_PREFIX}/recovery/decisions")
    async def decisions(request: Request) -> Response:
        corr = correlation(request)
        if (denied := guard(request, corr)) is not None:
            return denied
        decision, refusal, record = core.decide(await request.body(), corr)
        if refusal is not None:
            return outcome(refusal, corr, "recovery/decisions")
        logger.info(
            "decision route=%s source=%s mode=%s reason=%s budget=%s selected=%s correlation_id=%s",
            record.route_id,
            record.source,
            record.mode,
            record.reason,
            record.budget_rps,
            record.selected,
            corr,
        )
        return _json(200, encode(decision), corr)

    @app.get(f"{PATH_PREFIX}/health")
    async def health_v11(request: Request) -> Response:
        corr = correlation(request)
        if (denied := guard(request, corr)) is not None:
            return denied
        return _json(200, encode(core.status()), corr)

    @app.get(f"{PATH_PREFIX}/introspection")
    async def introspection(request: Request) -> Response:
        """Read-only operator diagnostics (bearer-authenticated; not a contract message)."""

        corr = correlation(request)
        if (denied := guard(request, corr)) is not None:
            return denied
        return JSONResponse(status_code=200, content=core.introspection())

    @app.get(f"{PATH_PREFIX}/ready")
    async def ready(request: Request) -> Response:
        corr = correlation(request)
        if (denied := guard(request, corr)) is not None:
            return denied
        status = core.status()
        return _json(200 if status.ready else 503, encode(status), corr)

    # ---------------------------------------------------------------- legacy /v1 surface

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "controller",
            "mode": os.getenv("AORT_CONTROLLER_MODE", "fixed"),
            "v1_1": "configured" if core else "not configured",
        }

    @app.post("/v1/recovery/decisions", response_model=LegacyRecoveryDecision)
    def legacy_decide(request: LegacyDecisionRequest) -> LegacyRecoveryDecision:
        budget = safe_budget(request.estimated_capacity_rps, request.confidence)
        limit = int(budget)
        return LegacyRecoveryDecision(
            experiment_id=request.experiment_id,
            route_id=request.route_id,
            decision_id=f"decision-{uuid4().hex}",
            budget_rps=budget,
            selected_transaction_ids=request.pending_transaction_ids[:limit],
            predicted_delay_ms=0,
            predicted_new_ambiguous=0,
            decision_reason="conservative fallback; no financial outcome is inferred",
            expires_at=datetime.now(UTC) + timedelta(seconds=5),
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        corr = correlation(request)
        logger.error("unhandled %s correlation_id=%s", type(exc).__name__, corr)
        body = error_response(
            ErrorCode.INTERNAL_ERROR,
            "internal error",
            correlation_id=corr,
            counts_against_limit=False,
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    return app


def create_app_from_env() -> FastAPI:
    path = os.getenv("AORTS_CONTROLLER_CONFIG")
    token = os.getenv("AORTS_BACKEND_TOKEN")
    core = None
    if path:
        config = ControllerConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
        core = ControllerCore(config, SystemClock())
    return create_app(core, token)


app = create_app_from_env()
