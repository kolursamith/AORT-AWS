"""Verification for the AORT Phase 2 banking foundation.

Confirms, against a genuinely running stack, that:

  * Fineract and its PostgreSQL database are up and answering
  * the workload generator's bootstrap and operations actually succeed
  * every in-scope banking category is exercised
  * the activity is visible in Fineract independently of what the generator
    reported, by re-reading counts before and after the run
  * the Prometheus metrics surface Phase 3 depends on is exposed

Run it with the stack already started:

    python verify/verify_workload.py

Exits 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

# The generator lives in banking/workload; make it importable from banking/verify.
BANKING_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BANKING_DIR / "workload"))

import requests  # noqa: E402

from aort_workload.client import FineractClient  # noqa: E402
from aort_workload.config import load_config  # noqa: E402
from aort_workload.report import RunReport  # noqa: E402
from aort_workload.runner import WorkloadOptions, run_workload  # noqa: E402

# Categories the Phase 2 scope requires the workload to cover.
REQUIRED_CATEGORIES = {"client", "savings", "loan", "transaction", "accounting"}

VERIFY_CLIENTS = 3
VERIFY_ITERATIONS = 25
VERIFY_LOANS = 2


class Checks:
    """Collects named pass/fail checks and reports them."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append((name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        print(f"  [{marker}] {name}" + (f" - {detail}" if detail else ""))
        return passed

    @property
    def failed(self) -> int:
        return sum(1 for _, passed, _ in self.results if not passed)


def count_journal_entries(client: FineractClient) -> int | None:
    result, payload = client.get(
        "/journalentries",
        category="verify",
        operation="verify.journalentries.count",
        params={"offset": 0, "limit": 1},
    )
    if not result.ok or not isinstance(payload, dict):
        return None
    total = payload.get("totalFilteredRecords")
    return int(total) if total is not None else None


def count_clients(client: FineractClient) -> int | None:
    result, payload = client.get(
        "/clients",
        category="verify",
        operation="verify.clients.count",
        params={"offset": 0, "limit": 1},
    )
    if not result.ok or not isinstance(payload, dict):
        return None
    total = payload.get("totalFilteredRecords")
    return int(total) if total is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_workload",
        description="Verify the AORT Phase 2 banking foundation against a running stack.",
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=600.0,
        help="seconds to wait for Fineract readiness (default: 600)",
    )
    args = parser.parse_args(argv)
    wait_timeout = args.wait_timeout

    config = load_config()
    run_id = f"verify-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    report = RunReport(run_id, BANKING_DIR / "verify" / "runs")
    client = FineractClient(config, report)
    checks = Checks()

    print("AORT Phase 2 - banking foundation verification")
    print(f"  target : {config.base_url}\n")

    # 1. Stack reachability -------------------------------------------------
    # A cold start runs Liquibase migrations, so wait for readiness before
    # asserting anything - otherwise this fails purely because it asked early.
    print("Stack availability")
    print(f"  waiting for Fineract to become ready (up to {wait_timeout:.0f}s)...")
    try:
        waited = client.wait_until_ready(timeout_seconds=wait_timeout)
        checks.add("Authenticated Fineract API responds", True, f"ready in {waited:.1f}s")
    except TimeoutError as exc:
        checks.add("Authenticated Fineract API responds", False, str(exc))
        print(
            "\nStack did not become ready. Check it is started:\n"
            "  docker compose up -d && docker compose ps"
        )
        return 1

    try:
        health = requests.get(config.actuator_url, timeout=15)
        status = health.json().get("status") if health.ok else None
        checks.add(
            "Fineract actuator health reports UP", status == "UP", f"status={status}"
        )
    except requests.RequestException as exc:
        checks.add("Fineract actuator health reports UP", False, str(exc))

    metrics = requests.get(
        config.actuator_url.replace("/health", "/prometheus"), timeout=15
    )
    checks.add(
        "Prometheus metrics endpoint is exposed",
        metrics.ok and "jvm_" in metrics.text,
        f"HTTP {metrics.status_code}, {len(metrics.text)} bytes",
    )

    # 2. Baseline, taken from Fineract itself -------------------------------
    print("\nBaseline")
    clients_before = count_clients(client)
    entries_before = count_journal_entries(client)
    checks.add(
        "Baseline counts readable",
        clients_before is not None and entries_before is not None,
        f"clients={clients_before}, journal_entries={entries_before}",
    )

    # 3. Run the workload ---------------------------------------------------
    print("\nWorkload execution")
    options = WorkloadOptions(
        new_clients=VERIFY_CLIENTS,
        iterations=VERIFY_ITERATIONS,
        loans_per_run=VERIFY_LOANS,
        seed=20260907,
    )
    try:
        setup, pools = run_workload(client, options, date.today())
        ran = True
    except Exception as exc:  # noqa: BLE001 - report rather than crash the check run
        checks.add("Workload run completed", False, f"{type(exc).__name__}: {exc}")
        ran = False
        setup = pools = None

    if not ran:
        print(f"\n{checks.failed} check(s) failed.")
        report.write()
        return 1

    checks.add("Workload run completed", True)
    checks.add(
        "Bootstrap resolved products and accounts",
        setup.savings_product_id > 0
        and setup.loan_product_id > 0
        and len(setup.gl_accounts) >= 12
        and len(setup.payment_type_ids) >= 1,
        f"savings={setup.savings_product_id}, loan={setup.loan_product_id}, "
        f"gl_accounts={len(setup.gl_accounts)}",
    )
    checks.add(
        "Clients, savings accounts and loans were created",
        len(pools.client_ids) == VERIFY_CLIENTS
        and len(pools.savings_ids) == VERIFY_CLIENTS
        and len(pools.loans) >= VERIFY_LOANS,
        f"{len(pools.client_ids)} clients, {len(pools.savings_ids)} savings, "
        f"{len(pools.loans)} loans",
    )

    # 4. Every in-scope category exercised ----------------------------------
    print("\nScope coverage")
    seen = {r.category for r in report.results}
    missing = REQUIRED_CATEGORIES - seen
    checks.add(
        "All in-scope banking categories exercised",
        not missing,
        f"missing: {sorted(missing)}" if missing else f"covered: {sorted(REQUIRED_CATEGORIES)}",
    )

    operations = {r.operation for r in report.results if r.ok}
    for required_op in (
        "client.create",
        "savings.submit",
        "savings.deposit",
        "savings.withdrawal",
        "loan.apply",
        "loan.disburse",
        "loan.repayment",
        "accounting.journalentry.create",
    ):
        checks.add(f"Operation succeeded at least once: {required_op}",
                   required_op in operations)

    # 5. No failed calls ----------------------------------------------------
    print("\nCall outcomes")
    checks.add(
        "Every Fineract call succeeded",
        report.failed == 0,
        f"{report.succeeded}/{report.total} succeeded",
    )
    if report.failed:
        for failure in report.failures()[:10]:
            print(f"        {failure.operation}: HTTP {failure.http_status} {failure.error}")

    # 6. Independent confirmation from Fineract -----------------------------
    print("\nIndependent confirmation (re-read from Fineract)")
    clients_after = count_clients(client)
    entries_after = count_journal_entries(client)

    checks.add(
        "Client count increased by the number onboarded",
        clients_after is not None
        and clients_before is not None
        and clients_after - clients_before == VERIFY_CLIENTS,
        f"{clients_before} -> {clients_after}",
    )
    checks.add(
        "Fineract posted new journal entries for this activity",
        entries_after is not None
        and entries_before is not None
        and entries_after > entries_before,
        f"{entries_before} -> {entries_after}",
    )

    # --- outcome -----------------------------------------------------------
    written = report.write()
    print("\n" + "=" * 72)
    total = len(checks.results)
    if checks.failed:
        print(f"VERIFICATION FAILED - {checks.failed} of {total} checks failed")
    else:
        print(f"VERIFICATION PASSED - all {total} checks passed")
    print("=" * 72)
    if written:
        print(f"Recorded calls: {written[0]}")

    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
