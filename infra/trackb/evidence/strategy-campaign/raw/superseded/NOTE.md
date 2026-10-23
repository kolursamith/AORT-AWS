# Superseded results: flooding-20261014T181511

These files are kept for transparency, not deleted. **Do not use their payment-traffic columns**
(`payment_p95_ms`, `payments_*`).

A harness bug caused the problem. The concurrent payment generator derived idempotency keys from the
seed, so every run after the first in each repeat re-sent the same keys, and the remitter answered
with idempotent replays (28/28 `idempotent_replays`). That makes those runs' payment latencies
meaningless. The status-check columns (A3 admitted, A1/A2 admitted ratio, detection, containment,
legit check latency) were not affected. The experiment was re-run after the fix (unique per-run key
nonce + regression test `tests/unit/test_traffic_generator.py`).

# Superseded results: flooding-constrained-20261014T183411

These files are kept for transparency. A ledger deadlock bug (FK `KEY SHARE` lock vs `FOR UPDATE`
on the same sender account, plus a lock-order cycle with settlement) made 21 concurrent payments
fail with HTTP 500 across 5 runs (`OperationalError` in the traffic outcomes). It also distorted
the payment-latency columns. The status-check columns were not affected. The bug is fixed: there
is a global lock order and regression tests `test_concurrent_different_payments_from_same_sender_do_not_deadlock`
and `test_mixed_concurrent_debits_settlements_reversals_same_senders`. The run was repeated after
the fix.

The `policy-comparison-*` files here come from smoke runs and an aborted run that hit the same
deadlock bug. Do not use them.

# Superseded results: flooding-constrained-20261016T170149, flooding-e7-recovery-20261016T170137

These were invalidated by my own operational mistake. I stopped an experiment chain by killing
only its Python process, and the shell chain then started its next steps. Those steps ran
*concurrently* with the restarted chain on the same `aorts_experiments` database. Each run resets
that database at start, so the runs interfered with each other. Both experiments were repeated
afterwards, one at a time.
