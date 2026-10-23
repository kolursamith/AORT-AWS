"""Remitter bank service. Run: ``uvicorn --factory backend.aorts.banking.remitter.main:build --port 8100``."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.aorts.app.config import Settings, get_settings
from backend.aorts.app.dependencies import ServiceContext, build_context, install_error_handlers
from backend.aorts.app.logging_config import configure_logging
from backend.aorts.banking.ledger.service import LedgerService
from backend.aorts.banking.remitter.routes import router
from backend.aorts.banking.remitter.service import RemitterService


def make_ledger(settings: Settings, ctx: ServiceContext) -> LedgerService:
    return LedgerService(
        ctx.db,
        deadline_seconds=settings.deadline_seconds,
        auto_reverse=settings.auto_reverse_on_confirmed_failure,
        backoff_base=settings.check_backoff_base_seconds,
        backoff_max=settings.check_backoff_max_seconds,
        max_checks=settings.max_checks_per_transaction,
        experiment_id=settings.experiment_id,
    )


def create_app(settings: Settings | None = None, ctx: ServiceContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = ctx or build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.audit_buffer.start()
        yield
        ctx.audit_buffer.stop()

    app = FastAPI(
        title="AORTS mock remitter bank (SIMULATION ONLY)", version="1.0", lifespan=lifespan
    )
    app.state.ctx = ctx
    app.state.remitter = RemitterService(ctx, make_ledger(settings, ctx))
    install_error_handlers(app)
    app.include_router(router)
    from backend.aorts.adapters.team_routes import remitter_v1

    app.include_router(remitter_v1)  # team contract (contracts/v1) surface
    return app


def build() -> FastAPI:
    s = get_settings()
    configure_logging("remitter", s.log_level)
    return create_app(s)
