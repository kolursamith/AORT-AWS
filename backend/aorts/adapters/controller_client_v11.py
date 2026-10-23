"""Additive v1.1 controller client for Track B (integration branch only; Stage 09).

This module adds a versioned path next to the existing ``HttpController`` (contracts/v1); it
changes no Track B financial semantics, schema or migration. It does three things:

1. sends a strict v1.1 ``DecisionRequest`` to ``POST /v1.1/recovery/decisions`` with the
   configured bearer credential (the controller authenticates Track B at its transport layer);
2. gates the response with the shared pure execution gate ``contracts.v1_1.protocol
   .gate_decision`` against Track B's own authoritative ``TransactionView`` rows read by the
   caller under its row lock (state version, terminal, in-flight, counted checks,
   ``next_eligible_at``) and Track B's remaining allowances;
3. maps transport failures to ``ControllerOutcome`` values so "controller unavailable"
   (fallback permitted) is never confused with "controller refused" (explicit FAIL_CLOSED: no
   fallback, no checks).

The returned ``DispatchPlan.admitted`` lists transactions that may be dispatched as status
checks only. Nothing here posts to the ledger; every financial mutation still goes through
``LedgerService`` and its execution-time safety gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx

from contracts.v1_1 import SCHEMA_VERSION, DecisionRequest, SourceSequenceMark, TimingPolicy
from contracts.v1_1.protocol import (
    PATH_PREFIX,
    ControllerOutcome,
    DispatchCandidate,
    DispatchLimits,
    DispatchPlan,
    TransportFailure,
    encode,
    gate_decision,
    outcome_for_transport_failure,
)

# Track B ledger states that end recovery (no status check may be dispatched).
TERMINAL_STATES = frozenset(
    {
        "CONFIRMED_SUCCESS",
        "CONFIRMED_FAILED",
        "REVERSAL_INITIATED",
        "REVERSED",
        "CLOSED",
        "RESOLVED_BY_RECONCILIATION",
        "QUARANTINED",
        "HANDOFF_UDIR",
    }
)


def dispatch_candidate(view, *, experiment_id: str) -> DispatchCandidate:
    """Track B ``TransactionView`` (authoritative, read under lock) -> gate candidate."""

    return DispatchCandidate(
        transaction_id=view.transaction_id,
        experiment_id=experiment_id,
        route_id=view.route_id,
        state_version=int(view.version),
        terminal=str(view.state) in TERMINAL_STATES,
        check_in_flight=str(view.state) == "CHECK_IN_FLIGHT",
        counted_check_attempts=int(view.checks_used),
        eligible_from=view.next_eligible_at,
        eligible_until=None,
    )


@dataclass(frozen=True)
class V11Exchange:
    plan: DispatchPlan
    http_status: int | None
    raw: bytes | None

    @property
    def backend_fallback_permitted(self) -> bool:
        return self.plan.backend_fallback_permitted


class V11ControllerClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        token: str,
        controller_id: str,
        experiment_id: str,
        timing_policy: TimingPolicy,
        timeout_s: float = 2.0,
        dispatch_window_s: float = 1.0,
        max_clock_skew_s: float = 1.0,
    ) -> None:
        self._client = client
        self._token = token
        self._controller_id = controller_id
        self._experiment_id = experiment_id
        self._policy = timing_policy
        self._timeout = timeout_s
        self._window = dispatch_window_s
        self._skew = max_clock_skew_s

    def decide(
        self,
        route_id: str,
        views: Iterable,
        *,
        now: datetime,
        rate_allowance: int,
        credit_allowance: int | None = None,
        watermark: tuple[SourceSequenceMark, ...] = (),
        gate_time: datetime | None = None,
    ) -> V11Exchange:
        request = DecisionRequest(
            schema_version=SCHEMA_VERSION,
            experiment_id=self._experiment_id,
            route_id=route_id,
            requested_at=now,
            min_telemetry_watermark=watermark,
        )
        try:
            response = self._client.post(
                f"{PATH_PREFIX}/recovery/decisions",
                content=encode(request),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return self._transport(TransportFailure.TIMEOUT)
        except httpx.TransportError:
            return self._transport(TransportFailure.CONNECT_ERROR)
        if response.status_code >= 500:
            return self._transport(TransportFailure.HTTP_5XX, response.status_code)
        if response.status_code != 200:
            # a structured refusal of the request (e.g. auth, stale request): treat as malformed
            plan = gate_decision(
                b"{}",
                request=request,
                controller_id=self._controller_id,
                authorized_controllers=frozenset({self._controller_id}),
                timing_policy=self._policy,
                candidates={},
                limits=DispatchLimits(rate_allowance=0),
                now=now,
                dispatch_window_s=self._window,
                max_clock_skew_s=self._skew,
            )
            return V11Exchange(plan, response.status_code, response.content)
        candidates: Mapping[str, DispatchCandidate] = {
            v.transaction_id: dispatch_candidate(v, experiment_id=self._experiment_id)
            for v in views
        }
        plan = gate_decision(
            response.content,
            request=request,
            controller_id=self._controller_id,
            authorized_controllers=frozenset({self._controller_id}),
            timing_policy=self._policy,
            candidates=candidates,
            limits=DispatchLimits(rate_allowance=rate_allowance, credit_allowance=credit_allowance),
            now=gate_time or now,
            dispatch_window_s=self._window,
            max_clock_skew_s=self._skew,
        )
        return V11Exchange(plan, 200, response.content)

    def _transport(self, failure: TransportFailure, status: int | None = None) -> V11Exchange:
        outcome = outcome_for_transport_failure(failure)
        return V11Exchange(DispatchPlan(outcome, None, None, (), ()), status, None)


__all__ = [
    "TERMINAL_STATES",
    "ControllerOutcome",
    "V11ControllerClient",
    "V11Exchange",
    "dispatch_candidate",
]
