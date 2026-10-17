"""Mock payment switch service. Run: ``uvicorn --factory backend.aorts.banking.switch.main:build --port 8010``."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.aorts.app.config import Settings, get_settings
from backend.aorts.app.dependencies import ServiceContext, build_context, install_error_handlers
from backend.aorts.app.logging_config import configure_logging
from backend.aorts.banking.switch.routes import router
from backend.aorts.banking.switch.service import SwitchService


def create_app(settings: Settings | None = None, ctx: ServiceContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = ctx or build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.audit_buffer.start()
        yield
        ctx.audit_buffer.stop()

    app = FastAPI(
        title="AORTS mock payment switch (SIMULATION ONLY)", version="1.0", lifespan=lifespan
    )
    app.state.ctx = ctx
    app.state.switch = SwitchService(ctx)
    install_error_handlers(app)
    app.include_router(router)
    return app


def build() -> FastAPI:
    """Entry point for ``uvicorn --factory backend.aorts.banking.switch.main:build``."""
    s = get_settings()
    configure_logging("switch", s.log_level)
    return create_app(s)
