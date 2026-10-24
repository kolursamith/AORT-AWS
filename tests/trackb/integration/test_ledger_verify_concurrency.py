"""verify_ledger() must not report false violations while payments commit concurrently."""

from __future__ import annotations

import threading

from tests.trackb.conftest import pay


def test_verify_ledger_is_consistent_under_concurrent_payments(stack):
    stop = threading.Event()
    errors: list[str] = []

    def payer(i: int) -> None:
        n = 0
        while not stop.is_set() and n < 25:
            try:
                pay(stack, sender=f"A1-CUST-{i:04d}", beneficiary=f"B-CUST-{i:04d}",
                    amount=1_000 + n)  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(repr(exc))
            n += 1

    threads = [threading.Thread(target=payer, args=(i,)) for i in range(1, 7)]
    for t in threads:
        t.start()
    reports = []
    while any(t.is_alive() for t in threads):
        reports.append(stack.ledger.verify_ledger())
    stop.set()
    for t in threads:
        t.join()
    reports.append(stack.ledger.verify_ledger())
    assert not errors, errors[:3]
    assert len(reports) > 5
    bad = [r for r in reports if not r["ok"]]
    assert not bad, bad[:2]
