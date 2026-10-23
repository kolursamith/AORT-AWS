"""The integrated AORTS v1.1 loop as one runnable unit (Stage 10).

One :class:`AortsLoop` couples a Track B stack (remitter, switch, beneficiaries, PostgreSQL
ledger) with a real Track A ``ControllerCore`` (route and transaction twins, Stage 06 estimator,
Stage 07 look-ahead, Stage 08 optimizer) served by its FastAPI app, through the v1.1 producers,
:class:`~backend.aorts.integration.v11.Delivery` and Track B's v1.1 dispatcher tick with the
execution gate. Every :meth:`AortsLoop.step` is one telemetry window aligned to whole wall-clock
seconds (so the switch's independent per-second measurement covers the same interval), then
delivery, then one dispatcher tick, then delivery of the results.

The controller is reached in process over ASGI (``fastapi.testclient.TestClient``); Stage 11
runs the same loop between containers. Synthetic data only.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.aorts.adapters.controller_client_v11 import V11ControllerClient
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.integration.v11 import (
    LEDGER_SOURCE,
    Delivery,
    DeliveryReport,
    WindowProducer,
    delivered_watermark,
    telemetry_source,
    trackb_timing_policy,
)
from contracts.v1_1 import RouteDescriptor
from intelligence.controller.api import SystemClock, create_app
from intelligence.controller.service import ControllerConfig, ControllerCore, RouteRegistration
from intelligence.ml.capacity_estimator import CapacityEstimatorConfig
from intelligence.twin.route_twin import RouteTwinConfig

CONTROLLER_ID = "aorts-controller"
EPS = 0.03  # close just after the second boundary the switch's metrics roll on
UNRESOLVED = ("DEBITED", "AMBIGUOUS", "CHECK_IN_FLIGHT")

# Estimator time constants compressed from minutes to seconds so that an experiment run reaches
# a positive decision in under a minute. Declared, never a production setting.
FAST_ESTIMATOR = CapacityEstimatorConfig(
    min_usable_windows=5,
    sustain_s=3.0,
    lag_allowance_s=0.2,
    conservation_block_s=3.0,
    conservation_lifetime_s=3.0,
    recovery_clean_s=3.0,
    evidence_horizon_s=120.0,
    point_horizon_s=60.0,
    baseline_horizon_s=120.0,
    ewma_tau_s=3.0,
    gap_horizon_s=8.0,
)


def next_second() -> int:
    return math.floor(time.time()) + 1


def sleep_until(t: float) -> None:
    while (left := t - time.time()) > 0:
        time.sleep(min(left, 0.05))


def controller_config(
    settings,
    experiment_id: str,
    routes: tuple[str, ...],
    *,
    estimator: CapacityEstimatorConfig | None = None,
    checks_per_bank_per_decision: int | None = 10,
) -> ControllerConfig:
    """The Track A controller configuration for Track B ``settings`` and ``routes``
    (``"A1-B"`` ...): Track B's timing policy expressed in v1.1, one own-traffic telemetry
    source per route, and the Track B ledger as the event source."""

    regs = tuple(
        RouteRegistration(
            descriptor=RouteDescriptor(
                schema_version="1.1",
                experiment_id=experiment_id,
                route_id=r,
                remitter_bank_id=r.split("-")[0],
                switch_id="SW",
                beneficiary_bank_id=r.split("-")[1],
            ),
            telemetry_sources=(telemetry_source(r),),
        )
        for r in routes
    )
    return ControllerConfig(
        experiment_id=experiment_id,
        timing_policy=trackb_timing_policy(settings),
        routes=regs,
        event_sources=(LEDGER_SOURCE,),
        twin=RouteTwinConfig(
            model_version="rt-1",
            max_observation_age_s=30.0,
            max_future_skew_s=2.0,
            freshness_threshold_s=5.0,
        ),
        estimator=estimator or CapacityEstimatorConfig(),
        checks_per_bank_per_decision=checks_per_bank_per_decision,
    )


def build_controller(
    settings,
    experiment_id: str,
    routes: tuple[str, ...],
    *,
    estimator: CapacityEstimatorConfig | None = None,
    checks_per_bank_per_decision: int | None = 10,
):
    """A Track A controller for ``routes`` (``"A1-B"`` ...) and its FastAPI app + bearer token."""

    config = controller_config(
        settings,
        experiment_id,
        routes,
        estimator=estimator,
        checks_per_bank_per_decision=checks_per_bank_per_decision,
    )
    token = secrets.token_urlsafe(24)
    core = ControllerCore(config, SystemClock())
    return core, TestClient(create_app(core, token)), token, config.timing_policy


@dataclass
class LoopConfig:
    experiment_id: str
    telemetry_routes: tuple[str, ...] = ("A1-B", "A1-C")  # routes with a telemetry producer
    registered_routes: tuple[str, ...] = ("A1-B", "A1-C", "A2-B", "A2-C", "A3-B", "A3-C")
    estimator: CapacityEstimatorConfig | None = field(default_factory=lambda: FAST_ESTIMATOR)
    checks_per_bank_per_decision: int | None = 10
    send_watermark: bool = True
    window_s: int = 1


class AortsLoop:
    def __init__(self, stack, cfg: LoopConfig, *, validator=None, http=None):
        self.st, self.cfg = stack, cfg
        self.exp = cfg.experiment_id
        self.core, self.client, self.token, self.policy = build_controller(
            stack.settings,
            cfg.experiment_id,
            cfg.registered_routes,
            estimator=cfg.estimator,
            checks_per_bank_per_decision=cfg.checks_per_bank_per_decision,
        )
        self.delivery = Delivery(
            stack.db,
            self.client,
            token=self.token,
            experiment_id=self.exp,
            policy_id=self.policy.policy_id,
        )
        self._span = cfg.window_s
        self.producer = WindowProducer(
            stack.db,
            experiment_id=self.exp,
            routes=list(cfg.telemetry_routes),
            validator=validator,
            measured=lambda: self.st.switch_service.route_measurements(self._span),
        )
        self.dispatch_enabled = True
        self.telemetry_enabled = True
        self.events_enabled = True
        self.overrides: dict[str, dict] = {}  # route -> reported values (attack hook)
        self.ticks: list[dict] = []
        self.dispatch_http = http  # the transport the dispatcher reaches the controller over
        self.dispatcher = self.make_dispatcher(http)
        self._boundary = next_second()
        sleep_until(self._boundary + EPS)
        self.producer.start = self.producer.db_now()
        self.t0 = time.time()

    # ------------------------------------------------------------------ wiring

    def v11_client(self, http=None, policy=None) -> V11ControllerClient:
        return V11ControllerClient(
            http or self.client,
            token=self.token,
            controller_id=CONTROLLER_ID,
            experiment_id=self.exp,
            timing_policy=policy or self.policy,
        )

    def make_dispatcher(self, http=None, **kw) -> Dispatcher:
        watermark = (
            (lambda rid: delivered_watermark(self.st.db, self.exp, rid))
            if self.cfg.send_watermark
            else None
        )
        return Dispatcher(
            self.st.contexts["remitter"],
            self.st.remitter_service,
            twin=LocalVersionedStore(),
            queue=LocalQueue(2),
            v11=self.v11_client(http),
            v11_watermark=watermark,
            **kw,
        )

    def restart_controller(self, *, replay_outbox: bool = True) -> None:
        """Replace the controller with a fresh process-equivalent (empty twins and estimators).

        ``replay_outbox`` applies the documented operator procedure (re-send the outbox so the
        new controller rebuilds its transaction twins); without it the new controller has no
        projections and selects nothing.
        """

        old = self.core
        self.core, self.client, self.token, self.policy = build_controller(
            self.st.settings,
            self.exp,
            self.cfg.registered_routes,
            estimator=self.cfg.estimator,
            checks_per_bank_per_decision=self.cfg.checks_per_bank_per_decision,
        )
        self.delivery.client, self.delivery.token = self.client, self.token
        self.dispatcher.v11 = self.v11_client(self.dispatch_http)
        if replay_outbox:
            self.delivery.replay_events()
        del old

    # ------------------------------------------------------------------ one step

    def unresolved(self) -> int:
        with self.st.db.session() as s:
            return s.execute(
                text("SELECT count(*) FROM transactions WHERE state = ANY(:s)"),
                {"s": list(UNRESOLVED)},
            ).scalar()

    def step(self) -> dict:
        end = max(next_second(), self._boundary + self.cfg.window_s)
        self._span, self._boundary = end - self._boundary, end
        sleep_until(end + EPS)
        windows = {}
        if self.telemetry_enabled:
            for rid in self.cfg.telemetry_routes:
                row = self.producer.close(rid, report_override=self.overrides.get(rid))
                if row is not None:
                    windows[rid] = row
        tel = self.delivery.deliver_telemetry()
        none = DeliveryReport()
        ev = self.delivery.deliver_events() if self.events_enabled else none
        tick = self.dispatcher.tick(inline=True) if self.dispatch_enabled else None
        ev2 = self.delivery.deliver_events() if self.events_enabled else none
        records = {r.decision_id: r for r in list(self.core.records)[-50:]}
        row = {
            "t": round(time.time() - self.t0, 3),
            "span_s": self._span,
            "telemetry_delivered": tel.delivered,
            "telemetry_refused": tel.dead_lettered,
            "events_delivered": ev.delivered + ev2.delivered,
            "events_dead_lettered": ev.dead_lettered + ev2.dead_lettered,
            "status_results": ev.status_results + ev2.status_results,
            "unresolved": self.unresolved(),
            "dispatched": tick["dispatched"] if tick else 0,
            "routes": {},
        }
        for rid in self.cfg.registered_routes:
            est = self.core._routes[rid].latest_estimate
            r = {
                "verdict": windows[rid].verdict if rid in windows else None,
                "estimator_mode": est.mode.value if est else None,
                "route_state": est.route_state.value if est else None,
                "headroom_rps": round(est.safe_headroom_rps, 3) if est else None,
                "capacity_lower_bound_rps": est.capacity_lower_bound_rps if est else None,
            }
            per = (tick or {}).get("routes", {}).get(rid)
            if per:
                rec = records.get(per.get("controller_decision_id") or per["decision_id"])
                r.update(
                    outcome=per["outcome"],
                    source=per["source"],
                    decision_id=per["decision_id"],
                    controller_decision_id=per.get("controller_decision_id"),
                    dispatched=per["dispatched"],
                    gate_refused=per["gate_refused"],
                    predispatch_refused=per["predispatch_refused"],
                    decision_mode=rec.mode if rec else None,
                    decision_reason=rec.reason if rec else None,
                    budget_rps=rec.budget_rps if rec else None,
                    selected=rec.selected if rec else None,
                )
            row["routes"][rid] = r
        self.ticks.append(row)
        return row

    def run(self, seconds: float, stop=None) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and not (stop and stop.is_set()):
            self.step()

    def now(self) -> datetime:
        return datetime.now(UTC)


class Workload:
    """Open-loop synthetic A-bank payments, seeded, running continuously in one thread.

    Phases change the rate (:meth:`set_rate`) instead of starting and stopping bursts, so every
    telemetry window sees steady traffic. A fraction can be sent with the switch's
    ``lost-response`` simulation (genuinely ambiguous payments).
    """

    def __init__(self, stack, *, seed: int, bank: str = "A1", concurrency: int = 16):
        import random
        import threading
        import uuid
        from concurrent.futures import ThreadPoolExecutor

        self.st, self.bank = stack, bank
        self.rng = random.Random(seed)
        self.nonce = uuid.uuid4().hex[:10]
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=concurrency)
        self.n = 0
        self.records: list[dict] = []  # t, destination, lost, latency_ms, state
        self.t0 = time.time()
        self.rps: dict[str, float] = {}
        self.lost_every = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._next: dict[str, float] = {}
        self._count: dict[str, int] = {}

    def _one(self, dest: str, lost: bool, i: int, account: int) -> None:
        body = {
            "sender_account_id": f"{self.bank}-CUST-{account:04d}",
            "beneficiary_account_id": f"{dest}-CUST-{account:04d}",
            "beneficiary_bank_id": dest,
            "amount_paise": 1_000 + (i % 97),
            "idempotency_key": f"wl-{self.nonce}-{i}",
        }
        headers = {}
        if lost:
            headers = {
                "X-AORTS-Simulate": "lost-response",
                "X-AORTS-Admin-Token": self.st.settings.admin_token.get_secret_value(),
            }
        sent_at, start = time.time(), time.perf_counter()
        try:
            r = self.st.client.post(
                "remitter",
                "/api/v1/payments",
                body,
                as_identity="CHANNEL",
                headers=headers,
                timeout=10.0,
            )
            state = r.json()["transaction"]["state"] if r.status_code == 200 else str(r.status_code)
        except Exception as exc:  # noqa: BLE001 - recorded as an outcome
            state = type(exc).__name__
        rec = {
            "t": round(sent_at - self.t0, 3),
            "destination": dest,
            "lost": lost,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "state": state,
        }
        with self.lock:
            self.records.append(rec)

    def set_rate(self, rps: dict[str, float], lost_every: int = 0) -> None:
        """Per-destination payments per second from now on; every ``lost_every``-th payment
        to a destination is a lost response (0 = none)."""

        with self.lock:
            now = time.time()
            for dest, rate in rps.items():
                if rate > 0 and (dest not in self._next or self.rps.get(dest, 0) <= 0):
                    self._next[dest] = now
            self.rps, self.lost_every = dict(rps), lost_every
        if not self._thread.is_alive() and not self._stop.is_set():
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                live = {d: t for d, t in self._next.items() if self.rps.get(d, 0) > 0}
            if not live:
                self._stop.wait(0.05)
                continue
            dest = min(live, key=live.get)
            sleep_until(live[dest])
            with self.lock:
                rate = self.rps.get(dest, 0)
                if rate <= 0 or self._stop.is_set():
                    continue
                self._next[dest] = max(self._next[dest] + 1.0 / rate, time.time() - 1.0)
                self._count[dest] = self._count.get(dest, 0) + 1
                self.n += 1
                i, account = self.n, self.rng.randint(1, 10)
                lost = bool(self.lost_every) and self._count[dest] % self.lost_every == 0
            self.pool.submit(self._one, dest, lost, i, account)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self.pool.shutdown(wait=True)


def ambiguous_backlog(stack, per_route: dict[str, int], *, bank: str = "A1") -> list[str]:
    """``n`` genuinely ambiguous payments per destination (switch completes, response lost),
    created before an experiment's telemetry starts so they do not distort its windows."""

    import uuid

    admin = stack.settings.admin_token.get_secret_value()
    ids = []
    for dest, n in per_route.items():
        for i in range(n):
            r = stack.client.post(
                "remitter",
                "/api/v1/payments",
                {
                    "sender_account_id": f"{bank}-CUST-{i % 10 + 1:04d}",
                    "beneficiary_account_id": f"{dest}-CUST-{i % 10 + 1:04d}",
                    "beneficiary_bank_id": dest,
                    "amount_paise": 2_000 + i,
                    "idempotency_key": f"bl-{uuid.uuid4().hex}",
                },
                as_identity="CHANNEL",
                headers={"X-AORTS-Simulate": "lost-response", "X-AORTS-Admin-Token": admin},
            )
            r.raise_for_status()
            ids.append(r.json()["transaction"]["transaction_id"])
    return ids


class ControllerFaults:
    """An ``httpx`` transport in front of the controller app that injects one fault at a time.

    Modes: ``None`` (pass through), ``"down"`` (connection refused), ``"slow"`` (``delay_s``
    before forwarding; at or above the client's timeout it raises ``ReadTimeout``, as the real
    client would), ``"malformed"`` (200 with a body that is not a decision), ``"wrong_version"``
    (``schema_version`` rewritten), ``"wrong_route"`` (``route_id`` rewritten) and ``"http_500"``.
    Only decision requests are affected; telemetry and event delivery go straight through.
    ``upstream`` is the controller's client, or a callable returning it (survives a restart).
    """

    def __init__(self, upstream, *, client_timeout_s: float = 2.0):
        import json as _json

        import httpx as _httpx

        self._json, self._httpx = _json, _httpx
        self.upstream = upstream
        self.client_timeout_s = client_timeout_s
        self.mode: str | None = None
        self.delay_s = 0.0
        self.injected = 0
        self.client = _httpx.Client(
            transport=_httpx.MockTransport(self._handle), base_url="http://controller"
        )

    def _forward(self, request):
        upstream = self.upstream() if callable(self.upstream) else self.upstream
        resp = upstream.request(
            request.method, request.url.path, content=request.content, headers=request.headers
        )
        return self._httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    def _handle(self, request):
        h = self._httpx
        if not request.url.path.endswith("/recovery/decisions") or self.mode is None:
            return self._forward(request)
        self.injected += 1
        if self.mode == "down":
            raise h.ConnectError("controller unavailable (injected)", request=request)
        if self.mode == "http_500":
            return h.Response(500, content=b'{"error":"injected"}')
        if self.mode == "slow":
            if self.delay_s >= self.client_timeout_s:
                time.sleep(self.client_timeout_s)
                raise h.ReadTimeout("controller too slow (injected)", request=request)
            time.sleep(self.delay_s)
            return self._forward(request)
        resp = self._forward(request)
        if self.mode == "malformed":
            return h.Response(200, content=b'{"decision": "check everything"}')
        body = self._json.loads(resp.content)
        if self.mode == "wrong_version":
            body["schema_version"] = "9.9"
        elif self.mode == "wrong_route":
            body["route_id"] = "A2-C" if body.get("route_id") != "A2-C" else "A1-B"
        return h.Response(resp.status_code, content=self._json.dumps(body).encode())
