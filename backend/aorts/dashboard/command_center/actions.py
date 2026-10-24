"""Operator actions for the demo page: only through the existing authenticated backend APIs.

* Payments are submitted to the remitter API, signed with the CHANNEL identity. A "lost
  confirmation" uses the remitter's existing lab simulation header (admin token, fault controls
  enabled): the beneficiary commits and the switch response is lost.
* Congestion and recovery use the control API's admin fault endpoints (runtime config shared by
  all services).

Nothing here touches the database. Every result shown is what the backend returned.
"""

from __future__ import annotations

import threading
import time
import uuid

_TRAFFIC: dict = {"thread": None, "stop": threading.Event(), "sent": 0, "errors": 0, "plan": ""}


def _client():
    from backend.aorts.experiments.common import http_client

    return http_client()


def pay(dest: str, *, lost: bool, amount_paise: int = 1_000, tag: str = "ui") -> dict:
    client, settings = _client()
    headers = {}
    if lost:
        headers = {
            "X-AORTS-Simulate": "lost-response",
            "X-AORTS-Admin-Token": settings.admin_token.get_secret_value(),
        }
    account = int(time.time() * 1000) % 10 + 1
    body = {
        "sender_account_id": f"A1-CUST-{account:04d}",
        "beneficiary_account_id": f"{dest}-CUST-{account:04d}",
        "beneficiary_bank_id": dest,
        "amount_paise": amount_paise,
        "idempotency_key": f"{tag}-{uuid.uuid4().hex}",
    }
    r = client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL",
                    headers=headers, timeout=10.0)  # fmt: skip
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    return {"http": r.status_code, "response": data}


def _traffic_loop(profile: list[tuple[float, float]]) -> None:
    """``profile``: (payments/s per route, seconds) phases; honest payments to B and C."""

    from concurrent.futures import ThreadPoolExecutor

    stop = _TRAFFIC["stop"]
    with ThreadPoolExecutor(max_workers=24) as pool:
        for rps, seconds in profile:
            end, nxt = time.time() + seconds, time.time()
            while time.time() < end and not stop.is_set():
                for dest in ("B", "C"):
                    fut = pool.submit(pay, dest, lost=False, tag="ui-honest")
                    fut.add_done_callback(_count)
                nxt += 1.0 / rps
                time.sleep(max(0.0, nxt - time.time()))
            if stop.is_set():
                break


def _count(fut) -> None:
    try:
        ok = fut.result().get("http") == 200
    except Exception:  # noqa: BLE001
        ok = False
    _TRAFFIC["sent" if ok else "errors"] += 1


def start_traffic(profile: list[tuple[float, float]]) -> str:
    t = _TRAFFIC["thread"]
    if t is not None and t.is_alive():
        return "honest traffic is already running"
    _TRAFFIC.update(stop=threading.Event(), sent=0, errors=0,
                    plan=" then ".join(f"{r:g}/s per route for {s:g}s" for r, s in profile))  # fmt: skip
    _TRAFFIC["thread"] = threading.Thread(target=_traffic_loop, args=(profile,), daemon=True)
    _TRAFFIC["thread"].start()
    return f"started: {_TRAFFIC['plan']}"


def stop_traffic() -> None:
    _TRAFFIC["stop"].set()


def traffic_status() -> dict:
    t = _TRAFFIC["thread"]
    return {"running": bool(t and t.is_alive()), "sent": _TRAFFIC["sent"],
            "errors": _TRAFFIC["errors"], "plan": _TRAFFIC["plan"]}  # fmt: skip


def congest(target: str = "beneficiary:B", latency_ms: int = 600) -> dict:
    from backend.aorts.dashboard.command_center.sources import control_client

    return control_client().set_fault({"target": target, "latency_ms": latency_ms,
                                       "note": "dashboard demo: congested route"})  # fmt: skip


def recover() -> dict:
    from backend.aorts.dashboard.command_center.sources import control_client

    return control_client().reset_faults()
