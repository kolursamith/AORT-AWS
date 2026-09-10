"""Command-line entry point for the AORT banking workload generator.

    python -m aort_workload --clients 5 --iterations 40 --seed 42

Exits non-zero if any Fineract call failed, so it can be used in CI or as a
gate before the telemetry phase starts collecting.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from .client import FineractClient, FineractError
from .config import BANKING_DIR, load_config
from .report import RunReport
from .runner import WorkloadOptions, run_workload

DEFAULT_OUTPUT_DIR = BANKING_DIR / "workload" / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aort_workload",
        description="Generate representative banking activity against Apache Fineract.",
    )
    parser.add_argument("--clients", type=int, default=5,
                        help="new clients to onboard this run (default: 5)")
    parser.add_argument("--iterations", type=int, default=40,
                        help="operations in the activity loop (default: 40)")
    parser.add_argument("--loans", type=int, default=2,
                        help="loans originated during onboarding (default: 2)")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed; the same seed replays the same sequence")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds to pause between operations (default: 0)")
    parser.add_argument("--business-date", type=str, default=None,
                        help="date for banking operations as YYYY-MM-DD (default: today)")
    parser.add_argument("--wait-timeout", type=float, default=900.0,
                        help="seconds to wait for Fineract readiness (default: 900)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="where to write the run JSONL and summary")
    parser.add_argument("--no-output", action="store_true",
                        help="print the summary but do not write run files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    business_date = (
        datetime.strptime(args.business_date, "%Y-%m-%d").date()
        if args.business_date
        else date.today()
    )

    run_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    report = RunReport(run_id, None if args.no_output else args.output_dir)

    config = load_config()
    client = FineractClient(config, report)

    print(f"AORT workload generator - run {run_id}")
    print(f"  target        : {config.base_url}")
    print(f"  tenant        : {config.tenant_id}")
    print(f"  business date : {business_date.isoformat()}")
    print(f"  seed          : {args.seed}")

    try:
        waited = client.wait_until_ready(timeout_seconds=args.wait_timeout)
        print(f"  Fineract ready after {waited:.1f}s\n")
    except TimeoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    options = WorkloadOptions(
        new_clients=args.clients,
        iterations=args.iterations,
        loans_per_run=args.loans,
        seed=args.seed,
        delay_seconds=args.delay,
    )

    try:
        setup, pools = run_workload(client, options, business_date)
    except FineractError as exc:
        print(f"ERROR: bootstrap could not complete: {exc}", file=sys.stderr)
        report.print_summary()
        report.write()
        return 3

    report.print_summary()
    print(
        f"\nCreated this run: {len(pools.client_ids)} clients, "
        f"{len(pools.savings_ids)} savings accounts, {len(pools.loans)} loans"
    )
    print(
        f"Products in use : savings id {setup.savings_product_id}, "
        f"loan id {setup.loan_product_id}"
    )

    written = report.write()
    if written:
        print(f"Run records     : {written[0]}")
        print(f"Run summary     : {written[1]}")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
