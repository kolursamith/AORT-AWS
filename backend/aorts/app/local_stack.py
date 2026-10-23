"""In-process AORTS stack: remitter + switch + beneficiary B/C wired with TestClients.

Used by the automated tests and by reproducible experiments that do not need
Docker. Every hop is still a real HTTP request through FastAPI with HMAC
signing and verification; only the TCP socket is replaced.
"""

from __future__ import annotations

import secrets
from contextlib import ExitStack
from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.aorts.app.config import Settings
from backend.aorts.app.db import Database
from backend.aorts.app.dependencies import ServiceContext, build_context
from backend.aorts.app.http_client import SignedClient
from backend.aorts.app.seed import seed
from backend.aorts.banking.beneficiary.main import create_app as create_beneficiary
from backend.aorts.banking.remitter.main import create_app as create_remitter
from backend.aorts.banking.switch.main import create_app as create_switch
from backend.aorts.security.request_auth import KeyRegistry

IDENTITIES = [
    "A1",
    "A2",
    "A3",
    "SWITCH",
    "B",
    "C",
    "REMITTER",
    "CHANNEL",
    "DISPATCHER",
    "CONTROLLER",
    "twin-probe-1",
    "twin-probe-2",
    "LAB",
]


def generate_service_keys(identities=IDENTITIES) -> str:
    return ",".join(f"{i}-v1={i}:{secrets.token_hex(24)}" for i in identities)


TABLES = [
    "evidence_records",
    "v11_telemetry_windows",
    "v11_delivery_state",
    "v11_dead_letters",
    "twin_state",
    "ledger_entries",
    "financial_operations",
    "transaction_events",
    "outbox_events",
    "processed_events",
    "audit_events",
    "experiment_runs",
    "switch_transactions",
    "runtime_config",
    "telemetry_observations",
    "transactions",
    "accounts",
    "bank_identities",
]


def reset_database(db: Database, opening_balance: int) -> None:
    """Danger: wipes all AORTS tables. Only for tests/experiments on a dedicated database."""
    with db.session() as s:
        s.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    seed(db, opening_balance)


@dataclass
class LocalStack:
    settings: Settings
    db: Database
    remitter: TestClient
    switch: TestClient
    beneficiaries: dict[str, TestClient]
    contexts: dict[str, ServiceContext]
    client: SignedClient  # signs as any identity, routed to the in-process apps
    _stack: ExitStack = field(default_factory=ExitStack)
    control: TestClient | None = None

    @property
    def ledger(self):
        return self.remitter.app.state.remitter.ledger

    @property
    def remitter_service(self):
        return self.remitter.app.state.remitter

    @property
    def switch_service(self):
        return self.switch.app.state.switch

    def set_fault(self, cfg) -> None:
        """Set a fault and make every in-process service see it immediately (no 1 s cache lag)."""
        self.contexts["switch"].runtime.set_fault(cfg, "test")
        self.refresh_runtime()

    def reset_faults(self) -> None:
        self.contexts["switch"].runtime.reset_faults()
        self.refresh_runtime()

    def set_defenses(self, cfg) -> None:
        self.contexts["switch"].runtime.set_defenses(cfg, "test")
        self.refresh_runtime()

    def refresh_runtime(self) -> None:
        for c in self.contexts.values():
            c.runtime._refresh(force=True)

    def flush_audit(self) -> None:
        for c in self.contexts.values():
            c.audit_buffer.flush()

    def close(self) -> None:
        self._stack.close()


def build_local_stack(
    settings: Settings, db: Database, *, with_control: bool = False
) -> LocalStack:
    stack = ExitStack()
    contexts: dict[str, ServiceContext] = {}
    beneficiaries: dict[str, TestClient] = {}
    for bank in settings.beneficiary_url_map():
        bs = settings.model_copy(update={"bank_id": bank, "service_name": f"beneficiary-{bank}"})
        ctx = build_context(bs, db=db)
        contexts[f"beneficiary:{bank}"] = ctx
        beneficiaries[bank] = stack.enter_context(TestClient(create_beneficiary(bs, ctx)))
    sw_settings = settings.model_copy(update={"service_name": "switch"})
    sw_ctx = build_context(
        sw_settings,
        db=db,
        client_overrides={f"beneficiary:{b}": c for b, c in beneficiaries.items()},
    )
    contexts["switch"] = sw_ctx
    switch = stack.enter_context(TestClient(create_switch(sw_settings, sw_ctx)))
    rem_settings = settings.model_copy(update={"service_name": "remitter"})
    rem_ctx = build_context(rem_settings, db=db, client_overrides={"switch": switch})
    contexts["remitter"] = rem_ctx
    remitter = stack.enter_context(TestClient(create_remitter(rem_settings, rem_ctx)))
    overrides = {
        "remitter": remitter,
        "switch": switch,
        **{f"beneficiary:{b}": c for b, c in beneficiaries.items()},
    }
    control = None
    if with_control:
        from backend.aorts.control.main import create_app as create_control

        cs = settings.model_copy(update={"service_name": "control"})
        cctx = build_context(cs, db=db, client_overrides=overrides)
        contexts["control"] = cctx
        control = stack.enter_context(TestClient(create_control(cs, cctx, background=False)))
    registry = KeyRegistry.parse(
        settings.service_keys.get_secret_value(), settings.signing_key_map()
    )
    client = SignedClient(
        registry, {}, overrides={**overrides, **({"control": control} if control else {})}
    )
    return LocalStack(
        settings, db, remitter, switch, beneficiaries, contexts, client, stack, control
    )
