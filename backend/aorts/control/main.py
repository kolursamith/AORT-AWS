"""Control service (ops + lab API used by the dashboard).
Run: ``uvicorn --factory backend.aorts.control.main:build --port 8130``."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.aorts.adapters.controller_client import make_controller
from backend.aorts.adapters.twin_state import make_store
from backend.aorts.app.config import Settings, get_settings
from backend.aorts.app.dependencies import ServiceContext, build_context, install_error_handlers
from backend.aorts.app.logging_config import configure_logging
from backend.aorts.banking.remitter.main import make_ledger
from backend.aorts.control.experiments import ExperimentRegistry
from backend.aorts.control.lab import Lab
from backend.aorts.control.metrics_service import MetricsService
from backend.aorts.control.routes import router
from backend.aorts.control.telemetry_service import HonestProbe, TelemetryService
from backend.aorts.events.publisher import OutboxRelay, make_sink
from backend.aorts.security.budget_guard import BudgetGuard


def create_app(
    settings: Settings | None = None, ctx: ServiceContext | None = None, *, background: bool = True
) -> FastAPI:
    settings = settings or get_settings()
    ctx = ctx or build_context(settings)
    ledger = make_ledger(settings, ctx)
    twin = make_store(settings, ctx.db)
    telemetry = TelemetryService(ctx, twin)
    probe = HonestProbe(
        telemetry, [f"{a}-{d}" for a in ("A1", "A2", "A3") for d in settings.beneficiary_url_map()]
    )
    relay = OutboxRelay(ctx.db, make_sink(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.audit_buffer.start()
        if background:
            relay.start()
            probe.start()
        yield
        probe.stop()
        relay.stop()
        if app.state.traffic:
            app.state.traffic.stop()
        ctx.audit_buffer.stop()

    app = FastAPI(
        title="AORTS control / lab API (SIMULATION ONLY)", version="1.0", lifespan=lifespan
    )
    app.state.ctx = ctx
    app.state.ledger = ledger
    app.state.twin = twin
    from backend.aorts.twin.engine import TwinEngine

    app.state.twin_engine = TwinEngine(ctx.db)
    app.state.telemetry = telemetry
    app.state.controller = make_controller(settings)
    app.state.guard = BudgetGuard(
        hard_cap=settings.max_budget_per_route, fallback_budget=settings.fallback_budget_per_route
    )
    app.state.metrics = MetricsService(ctx, ledger)
    app.state.registry = ExperimentRegistry(ctx.db, settings)
    app.state.lab = Lab(ctx, app.state.registry, telemetry, ledger)
    app.state.relay = relay
    app.state.traffic = None
    app.state.traffic_thread = None
    app.state.traffic_result = None

    def traffic_status() -> dict:
        running = bool(app.state.traffic_thread and app.state.traffic_thread.is_alive())
        live = app.state.traffic.stats.summary() if app.state.traffic else None
        return {"running": running, "live": live, "last_result": app.state.traffic_result}

    app.state.traffic_status = traffic_status
    install_error_handlers(app)
    app.include_router(router)
    return app


def build() -> FastAPI:
    s = get_settings()
    configure_logging("control", s.log_level)
    return create_app(s)
