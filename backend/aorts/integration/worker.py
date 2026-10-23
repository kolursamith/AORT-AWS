"""Stage 11: the integrated AORTS v1.1 loop as long-running services between containers.

Two entry points, both configured from Track B's ``Settings`` (environment) so that the
controller and the backend agree on the experiment, routes and timing policy:

``controller-config``
    Writes the Track A ``ControllerConfig`` JSON that ``intelligence.controller.api`` loads
    (``AORTS_CONTROLLER_CONFIG``). The controller container runs it before ``uvicorn``.

``loop``
    The Track B side of the v1.1 protocol, one step per telemetry window (aligned to whole
    wall-clock seconds, as :class:`~backend.aorts.integration.loop.AortsLoop`):

    1. close and validate one remitter-own telemetry window per route (Track B validator,
       cross-checked against the switch's independent ``/metrics/routes`` measurement);
    2. deliver telemetry, then outbox events and status results, to the controller over HTTP;
    3. one v1.1 dispatcher tick: decision request with the delivered-telemetry watermark, the
       execution gate against authoritative rows, pre-dispatch gate, check dispatch (or the
       labelled Track B fallback when the controller is unreachable; nothing on FAIL_CLOSED);
    4. deliver the resulting events.

    One JSON line per step goes to stdout (the audit trail is in PostgreSQL).

Environment (besides Track B's ``AORTS_*`` settings): ``AORTS_V11_CONTROLLER_URL``,
``AORTS_BACKEND_TOKEN`` (secret, shared with the controller), ``AORTS_V11_ROUTES``
(``A1-B,A1-C``), ``AORTS_V11_WINDOW_S`` (default 2) and ``AORTS_ESTIMATOR_PROFILE``
(``fast``: the declared compressed time constants used by the Stage 10 experiments, so a demo
reaches a decision within a minute; ``default``: the Stage 06 configuration).
Synthetic data only; nothing here contacts a real bank.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import httpx

ROUTES_DEFAULT = "A1-B,A1-C"
REGISTERED = ("A1-B", "A1-C", "A2-B", "A2-C", "A3-B", "A3-C")


def _routes() -> tuple[str, ...]:
    return tuple(r for r in os.getenv("AORTS_V11_ROUTES", ROUTES_DEFAULT).split(",") if r)


def _estimator():
    from backend.aorts.integration.loop import FAST_ESTIMATOR

    profile = os.getenv("AORTS_ESTIMATOR_PROFILE", "fast")
    if profile not in ("fast", "default"):
        raise SystemExit(f"AORTS_ESTIMATOR_PROFILE must be fast or default, not {profile!r}")
    return FAST_ESTIMATOR if profile == "fast" else None


def cmd_controller_config(args) -> None:
    from backend.aorts.app.config import get_settings
    from backend.aorts.integration.loop import controller_config

    settings = get_settings()
    config = controller_config(settings, settings.experiment_id, REGISTERED, estimator=_estimator())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    print(f"controller config for {settings.experiment_id} written to {out}", flush=True)


class Service:
    """The Track B side of the loop for one experiment."""

    def __init__(self, settings, ctx, remitter, controller: httpx.Client, switch: httpx.Client):
        from backend.aorts.adapters.controller_client_v11 import V11ControllerClient
        from backend.aorts.adapters.twin_state import LocalVersionedStore
        from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
        from backend.aorts.banking.dispatcher.service import Dispatcher
        from backend.aorts.integration.loop import CONTROLLER_ID
        from backend.aorts.integration.v11 import (
            Delivery,
            WindowProducer,
            delivered_watermark,
            trackb_timing_policy,
        )

        token = os.environ["AORTS_BACKEND_TOKEN"]
        self.exp = settings.experiment_id
        self.window_s = int(os.getenv("AORTS_V11_WINDOW_S", "2"))
        self.routes = _routes()
        self.switch = switch
        self._span = self.window_s
        policy = trackb_timing_policy(settings)
        self.delivery = Delivery(
            ctx.db, controller, token=token, experiment_id=self.exp, policy_id=policy.policy_id
        )
        self.producer = WindowProducer(
            ctx.db,
            experiment_id=self.exp,
            routes=list(self.routes),
            measured=self._measured,
        )
        self.dispatcher = Dispatcher(
            ctx,
            remitter,
            twin=LocalVersionedStore(),
            queue=LocalQueue(2),
            v11=V11ControllerClient(
                controller,
                token=token,
                controller_id=CONTROLLER_ID,
                experiment_id=self.exp,
                timing_policy=policy,
                timeout_s=settings.controller_timeout_seconds,
            ),
            v11_watermark=lambda rid: delivered_watermark(ctx.db, self.exp, rid),
        )
        self.stop = threading.Event()

    def _measured(self) -> dict:
        """The switch's independent per-route measurement over the window just closed."""

        try:
            r = self.switch.get("/api/v1/metrics/routes", params={"window_s": self._span})
            r.raise_for_status()
            return r.json()["routes"]
        except (httpx.HTTPError, KeyError, ValueError):
            return {}  # no corroboration: the validator records the cross-check as unavailable

    def step(self, boundary: float) -> dict:
        from backend.aorts.integration.loop import EPS, sleep_until

        sleep_until(boundary + EPS)
        verdicts = {}
        for rid in self.routes:
            row = self.producer.close(rid)
            if row is not None:
                verdicts[rid] = row.verdict
        tel = self.delivery.deliver_telemetry()
        ev = self.delivery.deliver_events()
        tick = self.dispatcher.tick(inline=True)
        ev2 = self.delivery.deliver_events()
        return {
            "t": round(time.time(), 3),
            "experiment_id": self.exp,
            "verdicts": verdicts,
            "telemetry_delivered": tel.delivered,
            "telemetry_refused": tel.dead_lettered,
            "events_delivered": ev.delivered + ev2.delivered,
            "events_dead_lettered": ev.dead_lettered + ev2.dead_lettered,
            "status_results": ev.status_results + ev2.status_results,
            "delivery_error": ev.last_error or ev2.last_error or tel.last_error,
            "dispatched": tick.get("dispatched", 0),
            "routes": {
                rid: {
                    k: per.get(k)
                    for k in ("outcome", "source", "decision_id", "dispatched", "gate_refused")
                }
                for rid, per in tick.get("routes", {}).items()
            },
        }

    def run(self) -> None:
        import math

        boundary = math.floor(time.time()) + 1
        self.producer.start = self.producer.db_now()
        while not self.stop.is_set():
            boundary += self.window_s
            self._span = self.window_s
            try:
                print(json.dumps(self.step(boundary), default=str), flush=True)
            except Exception as exc:  # noqa: BLE001 - one failed step must not end the service
                print(json.dumps({"t": time.time(), "step_error": repr(exc)[:300]}), flush=True)
            # a slow step (controller timeout) skips windows instead of producing short ones
            boundary = max(boundary, math.floor(time.time()))


def cmd_loop(args) -> None:
    from backend.aorts.app.config import get_settings
    from backend.aorts.app.dependencies import build_context
    from backend.aorts.app.logging_config import configure_logging
    from backend.aorts.banking.remitter.main import make_ledger
    from backend.aorts.banking.remitter.service import RemitterService

    settings = get_settings()
    configure_logging("aorts-loop", settings.log_level)
    ctx = build_context(settings)
    ctx.audit_buffer.start()
    controller = httpx.Client(
        base_url=os.environ["AORTS_V11_CONTROLLER_URL"],
        timeout=settings.controller_timeout_seconds,
    )
    switch = httpx.Client(base_url=settings.switch_url, timeout=2.0)
    service = Service(settings, ctx, RemitterService(ctx, make_ledger(settings, ctx)),
                      controller, switch)  # fmt: skip
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    try:
        service.run()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.audit_buffer.stop()
        controller.close()
        switch.close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="AORTS v1.1 integrated loop services (Stage 11)")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("controller-config")
    c.add_argument("--out", default="/run/aorts/controller.json")
    sub.add_parser("loop")
    args = p.parse_args(argv)
    {"controller-config": cmd_controller_config, "loop": cmd_loop}[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
