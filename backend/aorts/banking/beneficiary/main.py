"""Beneficiary bank service. Run one process per bank with ``AORTS_BANK_ID=B`` / ``C``:
``uvicorn --factory backend.aorts.banking.beneficiary.main:build --port 8121``."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.aorts.app.config import Settings, get_settings
from backend.aorts.app.dependencies import ServiceContext, build_context, install_error_handlers
from backend.aorts.app.logging_config import configure_logging
from backend.aorts.banking.beneficiary.routes import router
from backend.aorts.banking.beneficiary.service import BeneficiaryService
from backend.aorts.banking.ledger.service import LedgerService


def create_app(settings: Settings | None = None, ctx: ServiceContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = ctx or build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.audit_buffer.start()
        yield
        ctx.audit_buffer.stop()

    app = FastAPI(
        title=f"AORTS beneficiary bank {settings.bank_id} (SIMULATION ONLY)",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.state.beneficiary = BeneficiaryService(ctx, LedgerService(ctx.db))
    install_error_handlers(app)
    app.include_router(router)
    return app


def build() -> FastAPI:
    s = get_settings()
    configure_logging(f"beneficiary-{s.bank_id}", s.log_level)
    return create_app(s)
