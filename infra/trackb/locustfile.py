"""Locust load profile for the LOCAL / isolated AORTS stack only.

    locust -f locustfile.py --host http://127.0.0.1:8100            # web UI on :8089
    locust -f locustfile.py --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8100
    AORTS_LOCUST_FLOOD=1 locust ...                                  # also spawn the A3 flooding user

Users: PaymentUser (customer channel -> remitter) and StatusCheckUser (banks A1/A2 -> switch).
FloodUser (bank A3, simulated compromise) hammers /check-status when AORTS_LOCUST_FLOOD=1.
Hosts other than localhost / 127.0.0.1 are refused.
"""

from __future__ import annotations

import os
import random
import uuid
from urllib.parse import urlsplit

from locust import HttpUser, between, constant_throughput, events, task

from backend.aorts.app.config import Settings
from backend.aorts.app.http_client import dumps
from backend.aorts.security.request_auth import KeyRegistry, sign_request

SETTINGS = Settings()
REGISTRY = KeyRegistry.parse(SETTINGS.service_keys.get_secret_value(), SETTINGS.signing_key_map())
SWITCH = SETTINGS.switch_url


@events.test_start.add_listener
def _guard(environment, **_):
    for url in (environment.host or "", SWITCH):
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
            raise SystemExit(f"refusing to load-test non-local host {url}")


def signed_post(client, url: str, path: str, body: dict, identity: str, name: str):
    raw = dumps(body)
    headers = {
        "Content-Type": "application/json",
        **sign_request(REGISTRY.signing_key_for(identity), "POST", path, raw),
    }
    return client.post(url + path, data=raw, headers=headers, name=name)


class PaymentUser(HttpUser):
    wait_time = between(0.2, 1.0)
    weight = 3

    @task
    def pay(self):
        bank, dest = random.choice(["A1", "A2"]), random.choice(["B", "C"])
        body = {
            "sender_account_id": f"{bank}-CUST-{random.randint(1, 10):04d}",
            "beneficiary_account_id": f"{dest}-CUST-{random.randint(1, 10):04d}",
            "beneficiary_bank_id": dest,
            "amount_paise": random.randint(1_000, 100_000),
            "idempotency_key": f"locust-{uuid.uuid4().hex}",
        }
        signed_post(self.client, "", "/api/v1/payments", body, "CHANNEL", "POST /payments")


class StatusCheckUser(HttpUser):
    wait_time = constant_throughput(2)
    weight = 1

    @task
    def check(self):
        bank = random.choice(["A1", "A2"])
        body = {"transaction_id": f"TXN-LOCUST-{bank}", "bank_id": bank, "route_id": f"{bank}-B"}
        signed_post(
            self.client, SWITCH, "/api/v1/check-status", body, bank, "POST /check-status (legit)"
        )


if os.environ.get("AORTS_LOCUST_FLOOD") == "1":

    class FloodUser(HttpUser):
        """Simulated compromised bank A3: open-loop status-check flood (isolated mock switch only)."""

        wait_time = constant_throughput(
            float(os.environ.get("AORTS_LOCUST_FLOOD_RPS_PER_USER", "20"))
        )
        weight = 1

        @task
        def flood(self):
            body = {"transaction_id": "TXN-LOCUST-A3", "bank_id": "A3", "route_id": "A3-B"}
            signed_post(
                self.client,
                SWITCH,
                "/api/v1/check-status",
                body,
                "A3",
                "POST /check-status (A3 flood)",
            )
