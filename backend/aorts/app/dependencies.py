"""Shared FastAPI plumbing: service context, authentication dependencies, error handlers."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.aorts.app.config import Settings
from backend.aorts.app.db import Database
from backend.aorts.app.http_client import SignedClient
from backend.aorts.app.logging_config import log_event
from backend.aorts.app.metrics import Metrics
from backend.aorts.banking.faults.fault_config import RuntimeConfigStore
from backend.aorts.banking.faults.fault_service import FaultInjector
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.responses import ErrorBody
from backend.aorts.security.audit import BufferedAuditWriter, make_record
from backend.aorts.security.nonce_store import NonceStore
from backend.aorts.security.request_auth import (
    AuthError,
    KeyRegistry,
    RequestVerifier,
    VerifiedIdentity,
    fingerprint,
)

logger = logging.getLogger("aorts.http")


@dataclass
class ServiceContext:
    settings: Settings
    db: Database
    registry: KeyRegistry
    verifier: RequestVerifier
    runtime: RuntimeConfigStore
    faults: FaultInjector
    metrics: Metrics
    client: SignedClient
    audit_buffer: BufferedAuditWriter
    extras: dict = field(default_factory=dict)


def build_context(
    settings: Settings,
    *,
    db: Database | None = None,
    client_overrides: dict[str, httpx.Client] | None = None,
) -> ServiceContext:
    db = db or Database(settings.database_url.get_secret_value(), settings.db_pool_size)
    registry = KeyRegistry.parse(
        settings.service_keys.get_secret_value(), settings.signing_key_map()
    )
    verifier = RequestVerifier(
        registry, NonceStore(settings.nonce_ttl_seconds), settings.max_clock_skew_seconds
    )
    runtime = RuntimeConfigStore(db)
    base_urls = {
        "switch": settings.switch_url,
        "remitter": settings.remitter_url,
        "control": settings.control_url,
        **{f"beneficiary:{k}": v for k, v in settings.beneficiary_url_map().items()},
    }
    return ServiceContext(
        settings=settings,
        db=db,
        registry=registry,
        verifier=verifier,
        runtime=runtime,
        faults=FaultInjector(runtime, settings.random_seed),
        metrics=Metrics(
            settings.service_name, emf=settings.emf_enabled, namespace=settings.emf_namespace
        ),
        client=SignedClient(registry, base_urls, client_overrides),
        audit_buffer=BufferedAuditWriter(db),
    )


def ctx_of(request: Request) -> ServiceContext:
    return request.app.state.ctx


def error_response(
    code: ErrorCode,
    message: str,
    status: int,
    *,
    retryable: bool = False,
    details: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    body = ErrorBody(error_code=code, message=message, retryable=retryable, details=details or {})
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def require_roles(*roles: str) -> Callable:
    """Dependency: verify the HMAC signature and require one of ``roles``."""

    async def _dep(request: Request) -> VerifiedIdentity:
        ctx = ctx_of(request)
        body = await request.body()
        if len(body) > ctx.settings.max_body_bytes:
            raise AuthError(ErrorCode.VALIDATION_ERROR, "request body too large", 413)
        try:
            ident = ctx.verifier.verify(
                request.method, request.url.path, request.url.query, request.headers, body
            )
        except AuthError as exc:
            ctx.metrics.inc("auth_failures", reason=exc.code.value)
            ctx.metrics.inc("auth_failures_total")
            ctx.audit_buffer.record(
                make_record(
                    "AUTH_FAILURE",
                    "REJECTED",
                    request.headers.get("X-AORTS-Key-Id") or "anonymous",
                    reason=exc.code.value,
                    metadata={
                        "path": request.url.path,
                        "sig_fp": fingerprint(request.headers.get("X-AORTS-Signature")),
                    },
                ),
                sample_key=("AUTH_FAILURE", exc.code.value),
            )
            raise
        if roles and not (ident.roles & set(roles)):
            ctx.metrics.inc("auth_failures", reason="FORBIDDEN")
            raise AuthError(
                ErrorCode.AUTH_FORBIDDEN, f"identity '{ident.identity}' lacks role {roles}", 403
            )
        request.state.identity = ident
        return ident

    return _dep


def require_admin(request: Request) -> str:
    """Separate administrative credential (never a service HMAC key)."""
    ctx = ctx_of(request)
    configured = ctx.settings.admin_token.get_secret_value()
    supplied = request.headers.get("X-AORTS-Admin-Token", "")
    if not configured or not hmac.compare_digest(configured.encode(), supplied.encode()):
        ctx.metrics.inc("auth_failures", reason="ADMIN")
        ctx.audit_buffer.record(
            make_record(
                "AUTH_FAILURE",
                "REJECTED",
                "admin?",
                reason="bad admin token",
                metadata={"path": request.url.path},
            ),
            sample_key=("ADMIN",),
        )
        raise AuthError(ErrorCode.AUTH_FORBIDDEN, "administrative authorization required", 403)
    return "ADMIN"


def require_flag(flag_name: str) -> Callable:
    """Hide lab/fault endpoints (404) unless explicitly enabled for this deployment."""

    def _dep(request: Request) -> None:
        if not getattr(ctx_of(request).settings, flag_name):
            raise AuthError(ErrorCode.FEATURE_DISABLED, "not found", 404)

    return _dep


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError):
        return error_response(exc.code, exc.message, exc.status)

    @app.exception_handler(SafetyViolation)
    async def _safety(_: Request, exc: SafetyViolation):
        return error_response(exc.code, exc.message, exc.http_status, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return error_response(
            ErrorCode.VALIDATION_ERROR, "request validation failed", 422, details={"errors": errors}
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log_event(
            logger,
            "unhandled error",
            logging.ERROR,
            path=request.url.path,
            error=type(exc).__name__,
        )
        logger.exception("unhandled", exc_info=exc)
        return error_response(ErrorCode.INTERNAL_ERROR, "internal error", 500, retryable=True)
