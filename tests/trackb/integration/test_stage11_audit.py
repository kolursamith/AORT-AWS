"""Stage 11: the v1.1 decision audit records what the controller claimed (audit only)."""

from __future__ import annotations

from sqlalchemy import text

from backend.aorts.integration.loop import ControllerFaults
from tests.trackb.integration.test_stage10_security import (
    _close_workloads,  # noqa: F401
    ready_loop,
    run,
)


def _audit(st) -> list[dict]:
    st.contexts["remitter"].audit_buffer.flush()
    with st.db.session() as s:
        return [
            dict(r._mapping)["metadata"]
            for r in s.execute(
                text("SELECT metadata FROM audit_events WHERE action = 'RECOVERY_DECISION_V11' "
                     "ORDER BY seq")
            )
        ]  # fmt: skip


def test_decision_audit_records_the_controllers_reason_mode_and_budget(stack_factory):
    loop, wl = ready_loop(stack_factory)
    run(loop, wl, 3.0)
    rows = [m for m in _audit(loop.st) if m["outcome"] == "DECISION"]
    assert rows
    for m in rows:
        assert m["controller_reason"] and m["controller_mode"]  # e.g. SAFE_BUDGET_SELECTED
        assert isinstance(m["controller_budget_rps"], int)
        # the claim is audit only: what was dispatched never exceeds what the gate admitted
        assert m["dispatched"] <= m["admitted"]


def test_fallback_rows_carry_no_controller_claim(stack_factory):
    loop, wl = ready_loop(stack_factory)
    proxy = ControllerFaults(lambda: loop.client)
    loop.dispatcher.v11 = loop.v11_client(http=proxy.client)
    proxy.mode = "down"
    before = len(_audit(loop.st))
    run(loop, wl, 2.0)
    rows = _audit(loop.st)[before:]
    assert rows and all(m["source"] == "trackb_fallback" for m in rows)
    assert all(m["controller_reason"] is None and m["controller_budget_rps"] is None for m in rows)
