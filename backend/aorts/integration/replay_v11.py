"""Stage 10 replay cases at the v1.1 boundary, against a real Track B stack on PostgreSQL.

Track B's :class:`~backend.aorts.security.replay_simulator.ReplaySimulator` already attacks the
banking APIs (duplicate and stale status events, reversal races, forged requests). These cases
attack what Stage 09R added: the evidence path from a signed switch response into the ledger and
the delivery path from the ledger into the Track A controller. Each case records what was sent,
what came back, the ledger and twin state before and after, and whether that matched the
expected safe behaviour. Nothing is asserted by construction; ``as_expected`` is computed.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import uuid

import httpx
from sqlalchemy import select, text

from backend.aorts.banking.ledger.models import EvidenceRecord, OutboxEvent, V11DeliveryState
from backend.aorts.integration.v11 import export_event, export_status_result
from backend.aorts.security.request_auth import (
    H_RESPONSE_KEY_ID,
    H_RESPONSE_SIGNATURE,
    H_RESPONSE_TIMESTAMP,
    _response_message,
)
from contracts.v1_1.adapters import Adapted
from contracts.v1_1.protocol import encode

CHECK_PATH = "/api/v1/check-status"


class _Case:
    def __init__(self, st, loop):
        self.st, self.loop = st, loop

    # ---------------------------------------------------------------- helpers
    def pay(self, simulate="lost-response", sender="A1-CUST-0001", dest="B") -> str:
        admin = self.st.settings.admin_token.get_secret_value()
        r = self.st.client.post(
            "remitter",
            "/api/v1/payments",
            {
                "sender_account_id": sender,
                "beneficiary_account_id": f"{dest}-CUST-0001",
                "beneficiary_bank_id": dest,
                "amount_paise": 5_000,
                "idempotency_key": f"rp-{uuid.uuid4().hex}",
            },
            as_identity="CHANNEL",
            headers={"X-AORTS-Simulate": simulate, "X-AORTS-Admin-Token": admin},
        )
        r.raise_for_status()
        return r.json()["transaction"]["transaction_id"]

    def txn(self, txn_id) -> dict:
        with self.st.db.session() as s:
            return dict(
                s.execute(text("SELECT * FROM transactions WHERE transaction_id=:t"), {"t": txn_id})
                .mappings()
                .one()
            )

    def postings(self, txn_id) -> dict:
        with self.st.db.session() as s:
            rows = s.execute(
                text(
                    "SELECT operation_type, count(*) FROM financial_operations "
                    "WHERE transaction_id=:t GROUP BY 1"
                ),
                {"t": txn_id},
            ).all()
        return {k: v for k, v in rows}

    def twin(self, txn_id):
        try:
            p = self.loop.core._txn_twin.snapshot(self.loop.exp, txn_id)
        except Exception:  # noqa: BLE001 - "no projection" is a valid observation
            return None
        return {
            "version": p.version,
            "outcome": p.financial_outcome.value,
            "terminal": p.terminal,
        }

    def check(self, txn_id) -> dict:
        res = self.st.ledger.start_check(txn_id, self.txn(txn_id)["version"])
        return self.st.remitter_service.check_status(txn_id, expected_version=res.new_version)

    def deliver(self):
        return self.loop.delivery.deliver_events()

    def post(self, path: str, message) -> httpx.Response:
        return self.loop.client.post(
            f"/v1.1/{path}",
            content=encode(message),
            headers={"Authorization": f"Bearer {self.loop.token}"},
        )

    def exported(self, txn_id=None) -> list:
        with self.st.db.session() as s:
            rows = s.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars().all()
            out = [
                export_event(
                    s, r, experiment_id=self.loop.exp, policy_id=self.loop.policy.policy_id
                )
                for r in rows
            ]
        events = [e.value for e in out if isinstance(e, Adapted)]
        return [e for e in events if txn_id is None or e.transaction_id == txn_id]

    def status_results(self, txn_id) -> list:
        with self.st.db.session() as s:
            rows = s.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars().all()
            out = [export_status_result(s, r, experiment_id=self.loop.exp) for r in rows]
        return [r.value for r in out if isinstance(r, Adapted) and r.value.transaction_id == txn_id]

    def with_switch_response(self, rewrite):
        """Run the next real status check with its signed switch response rewritten."""

        client = self.st.remitter_service.ctx.client
        real = client.post

        def post(target, path, *a, **kw):
            resp = real(target, path, *a, **kw)
            if path == CHECK_PATH:
                return rewrite(resp)
            return resp

        return client, real, post


def _sign(secret: bytes, key_id: str, status: int, path: str, ts: str, body: bytes) -> dict:
    sig = hmac.new(secret, _response_message(status, path, ts, body), hashlib.sha256).hexdigest()
    return {H_RESPONSE_KEY_ID: key_id, H_RESPONSE_TIMESTAMP: ts, H_RESPONSE_SIGNATURE: sig}


def _resigned(resp: httpx.Response, sig: dict) -> httpx.Response:
    headers = httpx.Headers(resp.headers)
    for k, v in sig.items():
        headers[k] = v  # case-insensitive replace, not a second value
    return httpx.Response(resp.status_code, content=resp.content, headers=headers)


def run_v11_replay_cases(st, loop, *, seed: int) -> list[dict]:
    c = _Case(st, loop)
    out: list[dict] = []

    def record(case, expected, observed, ok, **detail):
        out.append(
            {"case": case, "expected": expected, "observed": observed, "as_expected": bool(ok),
             "seed": seed, **detail}
        )  # fmt: skip

    # 1. duplicate payment submission (same idempotency key) -> one debit
    admin = st.settings.admin_token.get_secret_value()
    body = {
        "sender_account_id": "A1-CUST-0003",
        "beneficiary_account_id": "B-CUST-0003",
        "beneficiary_bank_id": "B",
        "amount_paise": 7_000,
        "idempotency_key": f"dup-{uuid.uuid4().hex}",
    }
    hdr = {"X-AORTS-Simulate": "lost-response", "X-AORTS-Admin-Token": admin}
    r1 = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL", headers=hdr)
    r2 = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL", headers=hdr)
    t1 = r1.json()["transaction"]["transaction_id"]
    same = r2.json()["transaction"]["transaction_id"] == t1
    p = c.postings(t1)
    record("duplicate_payment", "same transaction, one DEBIT", {"same": same, "postings": p},
           same and p.get("DEBIT") == 1 and r2.json().get("replayed"))  # fmt: skip

    # 2. duplicate event delivery (cursor reset after a crash) -> duplicates, no twin change
    first_pass = c.deliver()
    v_before = c.twin(t1)
    with st.db.session() as s:
        s.get(V11DeliveryState, loop.delivery.cursor_stream).last_id = 0
    rep = c.deliver()
    record("duplicate_event_delivery",
           "nothing newly applied (duplicates; unrepresentable rows dead-lettered again), twin unchanged",
           {"first_pass": first_pass.__dict__, "redelivery": rep.__dict__, "twin": c.twin(t1)},
           rep.delivered == 0 and not rep.retry_pending and c.twin(t1) == v_before)  # fmt: skip

    # 3. a verified status check, then its result delivered twice and 8 times concurrently
    out_check = c.check(t1)
    c.deliver()
    results = c.status_results(t1)
    twin_after = c.twin(t1)
    if results:
        res = results[-1]
        again = c.post("status-check-results", res)
        codes = []
        lock = threading.Lock()

        def hit():
            r = c.post("status-check-results", res)
            with lock:
                codes.append(r.status_code)

        ths = [threading.Thread(target=hit) for _ in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        record("duplicate_status_result", "200 DUPLICATE_EVENT, twin unchanged",
               {"status": again.status_code, "code": again.json().get("error_code")},
               again.status_code == 200 and c.twin(t1) == twin_after)  # fmt: skip
        record("concurrent_duplicate_result", "no 204 (already applied), twin unchanged",
               {"codes": sorted(codes)}, 204 not in codes and c.twin(t1) == twin_after)  # fmt: skip
        # 4. the same result under another transaction id / another route
        wrong_txn = res.model_copy(update={"transaction_id": "TXNA1-NOSUCHTXN000001",
                                           "check_id": f"chk-{uuid.uuid4().hex[:20]}"})  # fmt: skip
        r = c.post("status-check-results", wrong_txn)
        record("wrong_transaction_id", "refused UNKNOWN_TRANSACTION",
               {"status": r.status_code, "code": r.json().get("error_code")},
               r.status_code != 204 and r.json().get("error_code") == "UNKNOWN_TRANSACTION")  # fmt: skip
        wrong_route = res.model_copy(update={"route_id": "A1-C",
                                             "check_id": f"chk-{uuid.uuid4().hex[:20]}"})  # fmt: skip
        r = c.post("status-check-results", wrong_route)
        record("wrong_route", "refused ROUTE_IDENTITY_MISMATCH",
               {"status": r.status_code, "code": r.json().get("error_code")},
               r.status_code != 204 and r.json().get("error_code") == "ROUTE_IDENTITY_MISMATCH")  # fmt: skip
    else:
        record("duplicate_status_result", "a status result exists", {"check": out_check}, False)

    # 5. conflicting authoritative outcome for an already terminal transaction
    events = c.exported(t1)
    close = next(
        (e for e in events if e.state_version == max(x.state_version for x in events)), None
    )
    if close is not None:
        forged = close.model_copy(
            update={"event_id": f"evt-{uuid.uuid4().hex[:20]}", "financial_outcome": "FAILURE"}
        )
        r = c.post("transaction-events", forged)
        record("conflicting_authoritative_outcome", "refused 409, twin keeps SUCCESS",
               {"status": r.status_code, "twin": c.twin(t1)},
               r.status_code == 409 and (c.twin(t1) or {}).get("outcome") == "SUCCESS")  # fmt: skip
        # 6. an old transaction version re-sent with new content
        first = min(events, key=lambda e: e.state_version)
        snap = c.twin(t1)
        stale = first.model_copy(update={"event_id": f"evt-{uuid.uuid4().hex[:20]}"})
        r = c.post("transaction-events", stale)
        stale2 = first.model_copy(update={"event_id": f"evt-{uuid.uuid4().hex[:20]}",
                                          "counted_check_attempts": 4})  # fmt: skip
        r2 = c.post("transaction-events", stale2)
        record("old_transaction_version",
               "identical old version: duplicate; altered old version: refused; twin unchanged",
               {"identical": r.status_code, "altered": r2.status_code, "twin": c.twin(t1)},
               r.status_code == 200 and r2.status_code in (409, 422) and c.twin(t1) == snap)  # fmt: skip
        # 7. an event from an unregistered source
        rogue = close.model_copy(update={"event_id": f"evt-{uuid.uuid4().hex[:20]}",
                                         "source_id": "rogue-ledger"})  # fmt: skip
        r = c.post("transaction-events", rogue)
        record("unregistered_event_source", "refused UNKNOWN_SOURCE",
               {"status": r.status_code, "code": r.json().get("error_code")},
               r.json().get("error_code") == "UNKNOWN_SOURCE")  # fmt: skip

    # 8. invalid source signature: the switch's answer re-signed with an attacker key
    t2 = c.pay(sender="A1-CUST-0004")
    before = (c.txn(t2)["state"], c.postings(t2))

    def forge(resp):
        sig = _sign(b"attacker-secret-" + bytes(16), resp.headers[H_RESPONSE_KEY_ID], 200,
                    CHECK_PATH, resp.headers[H_RESPONSE_TIMESTAMP], resp.content)  # fmt: skip
        return _resigned(resp, sig)

    client, real, patched = c.with_switch_response(forge)
    client.post = patched
    try:
        o = c.check(t2)
    finally:
        client.post = real
    after = (c.txn(t2)["state"], c.postings(t2))
    with st.db.session() as s:
        ev = s.execute(select(EvidenceRecord).where(EvidenceRecord.transaction_id == t2,
                       EvidenceRecord.evidence_kind == "STATUS_CHECK_RESPONSE")).scalars().all()  # fmt: skip
        stored = [(e.verified, bool(e.signature_hex)) for e in ev]
    record("invalid_source_signature", "evidence stored unverified; no settlement; still unresolved",
           {"status": o.get("status"), "unverified_status": o.get("unverified_status"),
            "before": before, "after": after, "evidence": stored},
           o.get("status") is None and after[0] == "AMBIGUOUS" and after[1] == before[1]
           and stored and not any(v for v, _ in stored))  # fmt: skip

    # 9. expired evidence: genuine key, response timestamp 600 s old
    t3 = c.pay(sender="A1-CUST-0005")
    before3 = c.postings(t3)
    key = st.remitter_service.ctx.registry.get

    def expire(resp):
        k = key(resp.headers[H_RESPONSE_KEY_ID])
        ts = str(int(resp.headers[H_RESPONSE_TIMESTAMP]) - 600)
        sig = _sign(k.secret, k.key_id, 200, CHECK_PATH, ts, resp.content)
        return _resigned(resp, sig)

    client, real, patched = c.with_switch_response(expire)
    client.post = patched
    try:
        o = c.check(t3)
    finally:
        client.post = real
    record("expired_evidence", "stale signature rejected; no settlement",
           {"status": o.get("status"), "unverified_status": o.get("unverified_status"),
            "state": c.txn(t3)["state"], "postings": c.postings(t3)},
           o.get("status") is None and c.txn(t3)["state"] == "AMBIGUOUS"
           and c.postings(t3) == before3)  # fmt: skip

    # 10. a genuine, fresh answer about another transaction (captured and replayed)
    t4 = c.pay(sender="A1-CUST-0006")

    real_post = st.remitter_service.ctx.client.post
    captured = {  # a real, freshly signed answer about t1 (already closed)
        "resp": real_post(
            "switch", CHECK_PATH, {"transaction_id": t1, "bank_id": "A1", "route_id": "A1-B"},
            as_identity="A1",
        )
    }  # fmt: skip
    client, real, patched = c.with_switch_response(lambda resp: captured["resp"])
    client.post = patched
    try:
        o = c.check(t4)
    finally:
        client.post = real
    record("answer_for_other_transaction", "discarded (WRONG_TRANSACTION); t4 unresolved",
           {"status": o.get("status"), "state": c.txn(t4)["state"]},
           o.get("status") is None and c.txn(t4)["state"] == "AMBIGUOUS")  # fmt: skip

    # 11. duplicate reversal completion notification
    t5 = c.pay(simulate="timeout", sender="A1-CUST-0007")
    o = c.check(t5)
    if c.txn(t5)["state"] == "CONFIRMED_FAILED":  # auto-reverse off: the authorised reversal
        for _ in range(2):  # delivered twice (at-least-once caller)
            st.client.post(
                "remitter", f"/api/v1/payments/{t5}/reversal",
                {"operation_id": f"{t5}:REVERSAL", "expected_version": c.txn(t5)["version"],
                 "reason": "stage10 replay case"},
                as_identity="CONTROLLER",
            )  # fmt: skip
    c.deliver()
    rev = [e for e in c.exported(t5) if e.event_type.value == "REVERSAL_COMPLETED"]
    twin5 = c.twin(t5)
    codes = []
    for e in rev:
        codes.append(c.post("transaction-events", e).status_code)
    p5 = c.postings(t5)
    record("duplicate_reversal_completion", "duplicate (200); one REVERSAL posting",
           {"check": o.get("status"), "codes": codes, "postings": p5, "twin": c.twin(t5)},
           rev and all(x == 200 for x in codes) and p5.get("REVERSAL", 0) <= 1
           and c.twin(t5) == twin5)  # fmt: skip
    return out
