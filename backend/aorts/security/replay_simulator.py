"""Attack 3: transaction replay (duplicate, stale, concurrent, mis-linked, forged).

Each case returns a record with: case, event_id, transaction_id, expected_version,
actual_version (after), operation_id, decision (HTTP status + code), rejection
reason, ledger postings for the transaction, sender balance and final outcome.

The simulator signs "valid" status events with the SWITCH key to model an attacker
who captured a legitimately signed event and re-delivers it (or a buggy
at-least-once queue). Forgery cases use wrong keys, tampered bodies, stale
timestamps and repeated nonces.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from backend.aorts.app.http_client import dumps
from backend.aorts.contracts.events import new_id
from backend.aorts.security.request_auth import sign_request


class ReplaySimulator:
    def __init__(self, client, db, admin_token: str):
        self.client = client
        self.db = db
        self.admin_token = admin_token
        self.records: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, **p):
        with self.db.session() as s:
            return s.execute(text(sql), p).scalar()

    def _txn(self, txn_id: str) -> dict:
        with self.db.session() as s:
            row = (
                s.execute(text("SELECT * FROM transactions WHERE transaction_id=:t"), {"t": txn_id})
                .mappings()
                .one()
            )
            return dict(row)

    def _snapshot(self, txn_id: str) -> dict:
        t = self._txn(txn_id)
        return {
            "actual_version": t["version"],
            "state": t["state"],
            "final_outcome": t["financial_outcome"],
            "ledger_postings": self._q(
                "SELECT count(*) FROM financial_operations WHERE transaction_id=:t", t=txn_id
            ),
            "reversal_postings": self._q(
                "SELECT count(*) FROM financial_operations WHERE transaction_id=:t "
                "AND operation_type='REVERSAL'",
                t=txn_id,
            ),
            "sender_balance": self._q(
                "SELECT balance_paise FROM accounts WHERE account_id=:a", a=t["sender_account_id"]
            ),
        }

    def ambiguous_payment(self, sender: str = "A1-CUST-0002", amount: int = 25_000) -> dict:
        """A payment that never reached the switch: AMBIGUOUS at the remitter."""
        body = {
            "sender_account_id": sender,
            "beneficiary_account_id": "B-CUST-0002",
            "beneficiary_bank_id": "B",
            "amount_paise": amount,
            "idempotency_key": f"replay-{uuid.uuid4().hex}",
        }
        r = self.client.post(
            "remitter",
            "/api/v1/payments",
            body,
            as_identity="CHANNEL",
            headers={"X-AORTS-Simulate": "timeout", "X-AORTS-Admin-Token": self.admin_token},
        )
        r.raise_for_status()
        return r.json()["transaction"]

    @staticmethod
    def status_event(
        txn: dict,
        status: str,
        version: int,
        *,
        event_id: str | None = None,
        timestamp: datetime | None = None,
        **override,
    ) -> dict:
        ev = {
            "event_id": event_id or new_id("evt"),
            "schema_version": "1.0",
            "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
            "transaction_id": txn["transaction_id"],
            "route_id": txn["route_id"],
            "bank_id": txn["remitter_bank_id"],
            "amount_paise": txn["amount_paise"],
            "event_type": "STATUS_RESPONSE",
            "transaction_version": version,
            "payload": {"status": status, "switch_reference": f"SW-{txn['transaction_id']}"},
        }
        ev.update(override)
        return ev

    def send_event(self, ev: dict, as_identity: str = "SWITCH"):
        return self.client.post(
            "remitter", "/api/v1/events/transaction-state", ev, as_identity=as_identity
        )

    def _record(
        self,
        case: str,
        txn_id: str,
        resp,
        *,
        event_id=None,
        expected_version=None,
        operation_id=None,
        expectation: str = "",
    ) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        rec = {
            "case": case,
            "event_id": event_id,
            "transaction_id": txn_id,
            "expected_version": expected_version,
            "operation_id": operation_id,
            "http_status": resp.status_code,
            "decision": body.get("decision") or body.get("error_code"),
            "rejection_reason": body.get("message") if resp.status_code >= 400 else None,
            "expectation": expectation,
            **self._snapshot(txn_id),
        }
        self.records.append(rec)
        return rec

    # ------------------------------------------------------------------ cases
    def run_all(self) -> list[dict]:
        self.case_sequential_duplicate()
        self.case_stale_version()
        self.case_reversal_repeated()
        self.case_concurrent_duplicate_events()
        self.case_concurrent_reversals()
        self.case_duplicate_after_close()
        self.case_wrong_transaction()
        self.case_missing_idempotency_key()
        self.case_forged_requests()
        return self.records

    def case_sequential_duplicate(self):
        txn = self.ambiguous_payment()
        ev = self.status_event(txn, "FAILED", txn["version"])
        self._record(
            "1_valid_response_once",
            txn["transaction_id"],
            self.send_event(ev),
            event_id=ev["event_id"],
            expected_version=txn["version"],
            expectation="ACCEPTED; exactly one reversal",
        )
        self._record(
            "2_same_response_twice",
            txn["transaction_id"],
            self.send_event(ev),
            event_id=ev["event_id"],
            expected_version=txn["version"],
            expectation="DUPLICATE_EVENT; no new posting",
        )

    def case_stale_version(self):
        txn = self.ambiguous_payment()
        ev = self.status_event(txn, "SUCCESS", txn["version"] - 1)
        self._record(
            "3_stale_version",
            txn["transaction_id"],
            self.send_event(ev),
            event_id=ev["event_id"],
            expected_version=txn["version"] - 1,
            expectation="STALE_VERSION; state unchanged",
        )
        old = self.status_event(
            txn, "SUCCESS", txn["version"], timestamp=datetime.now(UTC) - timedelta(hours=2)
        )
        self._record(
            "3b_stale_timestamp",
            txn["transaction_id"],
            self.send_event(old),
            event_id=old["event_id"],
            expected_version=txn["version"],
            expectation="STALE_VERSION (old event); state unchanged",
        )

    def _confirmed_failed(self) -> tuple[dict, dict]:
        txn = self.ambiguous_payment()
        self.send_event(self.status_event(txn, "FAILED", txn["version"]))
        return txn, self._txn(txn["transaction_id"])

    def case_reversal_repeated(self):
        txn, now = self._confirmed_failed()
        op_id = f"{txn['transaction_id']}:REVERSAL"
        for i in range(3):
            body = {
                "operation_id": op_id,
                "expected_version": now["version"],
                "reason": "controller reversal",
            }
            r = self.client.post(
                "remitter",
                f"/api/v1/payments/{txn['transaction_id']}/reversal",
                body,
                as_identity="CONTROLLER",
            )
            self._record(
                f"4_same_reversal_delivery_{i + 1}",
                txn["transaction_id"],
                r,
                operation_id=op_id,
                expected_version=now["version"],
                expectation="one posting in total",
            )
        other = {
            "operation_id": f"{txn['transaction_id']}:REV-ALT",
            "expected_version": now["version"],
            "reason": "second reversal with a new id",
        }
        r = self.client.post(
            "remitter",
            f"/api/v1/payments/{txn['transaction_id']}/reversal",
            other,
            as_identity="CONTROLLER",
        )
        self._record(
            "4b_reversal_new_operation_id",
            txn["transaction_id"],
            r,
            operation_id=other["operation_id"],
            expected_version=now["version"],
            expectation="ALREADY_APPLIED/STALE; no second posting",
        )

    def case_concurrent_duplicate_events(self, n: int = 10):
        txn = self.ambiguous_payment()
        ev = self.status_event(txn, "FAILED", txn["version"])
        with ThreadPoolExecutor(n) as pool:
            responses = list(pool.map(lambda _: self.send_event(ev), range(n)))
        accepted = sum(1 for r in responses if r.status_code == 200)
        rec = self._record(
            "5_concurrent_duplicate_events",
            txn["transaction_id"],
            responses[0],
            event_id=ev["event_id"],
            expected_version=txn["version"],
            expectation=f"exactly 1 of {n} accepted",
        )
        rec["accepted_count"] = accepted
        rec["status_codes"] = sorted(r.status_code for r in responses)

    def case_concurrent_reversals(self, n: int = 8):
        txn, now = self._confirmed_failed()
        op_ids = [f"{txn['transaction_id']}:REVERSAL"] * (n // 2) + [
            f"{txn['transaction_id']}:R{i}" for i in range(n // 2)
        ]

        def go(op_id):
            body = {"operation_id": op_id, "expected_version": now["version"], "reason": "race"}
            return self.client.post(
                "remitter",
                f"/api/v1/payments/{txn['transaction_id']}/reversal",
                body,
                as_identity="CONTROLLER",
            )

        with ThreadPoolExecutor(n) as pool:
            responses = list(pool.map(go, op_ids))
        rec = self._record(
            "6_concurrent_reversals",
            txn["transaction_id"],
            responses[0],
            operation_id="(mixed)",
            expected_version=now["version"],
            expectation="exactly one reversal posting",
        )
        rec["status_codes"] = sorted(r.status_code for r in responses)

    def case_duplicate_after_close(self):
        txn = self.ambiguous_payment()
        self.send_event(self.status_event(txn, "FAILED", txn["version"]))
        closed = self._txn(txn["transaction_id"])
        ev = self.status_event(txn, "FAILED", closed["version"])
        self._record(
            "7_duplicate_after_close_agreeing",
            txn["transaction_id"],
            self.send_event(ev),
            event_id=ev["event_id"],
            expected_version=closed["version"],
            expectation="DUPLICATE/no-op; no new posting",
        )
        ev2 = self.status_event(txn, "SUCCESS", closed["version"])
        self._record(
            "7b_conflicting_after_close",
            txn["transaction_id"],
            self.send_event(ev2),
            event_id=ev2["event_id"],
            expected_version=closed["version"],
            expectation="ILLEGAL_TRANSITION; outcome unchanged",
        )

    def case_wrong_transaction(self):
        a = self.ambiguous_payment(amount=11_000)
        b = self.ambiguous_payment(sender="A2-CUST-0002", amount=22_000)
        ev = self.status_event(
            a,
            "FAILED",
            a["version"],
            route_id=b["route_id"],
            bank_id=b["remitter_bank_id"],
            amount_paise=b["amount_paise"],
        )
        self._record(
            "8_response_for_wrong_transaction",
            a["transaction_id"],
            self.send_event(ev),
            event_id=ev["event_id"],
            expected_version=a["version"],
            expectation="WRONG_TRANSACTION",
        )
        ev2 = self.status_event(a, "FAILED", a["version"])
        self._record(
            "8b_event_signed_by_non_switch",
            a["transaction_id"],
            self.send_event(ev2, as_identity="A1"),
            event_id=ev2["event_id"],
            expected_version=a["version"],
            expectation="AUTH_FORBIDDEN",
        )

    def case_missing_idempotency_key(self):
        txn = self.ambiguous_payment()
        body = {
            "sender_account_id": "A1-CUST-0002",
            "beneficiary_account_id": "B-CUST-0002",
            "beneficiary_bank_id": "B",
            "amount_paise": 1000,
        }
        r = self.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
        self._record(
            "9_missing_idempotency_key", txn["transaction_id"], r, expectation="VALIDATION_ERROR"
        )
        body["idempotency_key"] = "x"
        r = self.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
        self._record(
            "9b_invalid_idempotency_key", txn["transaction_id"], r, expectation="VALIDATION_ERROR"
        )

    def case_forged_requests(self):
        txn = self.ambiguous_payment()
        ev = self.status_event(txn, "FAILED", txn["version"])
        body = dumps(ev)
        path = "/api/v1/events/transaction-state"
        key = self.client.registry.signing_key_for("SWITCH")
        raw = self.client._client("remitter")
        # 10a: byte-identical replay of a captured signed request (same nonce).
        headers = {"Content-Type": "application/json", **sign_request(key, "POST", path, body)}
        first = raw.post(path, content=body, headers=headers)
        self._record(
            "10a_captured_request_first_use",
            txn["transaction_id"],
            first,
            event_id=ev["event_id"],
            expected_version=txn["version"],
            expectation="ACCEPTED",
        )
        again = raw.post(path, content=body, headers=headers)
        self._record(
            "10b_repeated_nonce",
            txn["transaction_id"],
            again,
            event_id=ev["event_id"],
            expected_version=txn["version"],
            expectation="AUTH_REPLAYED_NONCE",
        )
        txn2 = self.ambiguous_payment()
        ev2 = self.status_event(txn2, "SUCCESS", txn2["version"])
        body2 = dumps(ev2)
        expired = {
            "Content-Type": "application/json",
            **sign_request(
                key, "POST", path, body2, timestamp=int(datetime.now(UTC).timestamp()) - 3600
            ),
        }
        self._record(
            "10c_expired_timestamp",
            txn2["transaction_id"],
            raw.post(path, content=body2, headers=expired),
            event_id=ev2["event_id"],
            expected_version=txn2["version"],
            expectation="AUTH_EXPIRED_TIMESTAMP",
        )
        tampered = json.loads(body2)
        tampered["payload"]["status"] = "FAILED"
        valid_headers = {
            "Content-Type": "application/json",
            **sign_request(key, "POST", path, body2),
        }
        self._record(
            "10d_tampered_body",
            txn2["transaction_id"],
            raw.post(path, content=dumps(tampered), headers=valid_headers),
            event_id=ev2["event_id"],
            expected_version=txn2["version"],
            expectation="AUTH_INVALID_SIGNATURE",
        )
        unsigned = raw.post(path, content=body2, headers={"Content-Type": "application/json"})
        self._record(
            "10e_unsigned",
            txn2["transaction_id"],
            unsigned,
            event_id=ev2["event_id"],
            expected_version=txn2["version"],
            expectation="AUTH_MISSING",
        )
