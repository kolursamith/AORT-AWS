"""CLI for recovery experiments.

    python -m aort_recovery run --scenario service_pause --component fineract

Injects the failure, recovers it, and prints the measured RTO, RPO,
availability and ledger integrity. Requires the banking and telemetry stacks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .experiment import RecoveryError, run_recovery_experiment
from .ledger import DockerLedger
from .probes import http_probe
from .strategies import RECOVERY_FOR_SCENARIO

# Absolute, so the command works from any working directory: a relative path
# fails silently as "ledger unreadable" rather than as a missing file.
DEFAULT_COMPOSE = str(Path(__file__).resolve().parents[3] / "banking" / "docker-compose.yml")
DEFAULT_HEALTH = "http://localhost:8080/fineract-provider/actuator/health"
DEFAULT_OUT = Path("infrastructure/recovery/runs")


def docker(argv):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


def newest_backup_epoch(compose_file: str) -> float | None:
    code, out, _ = docker(["docker", "compose", "-f", compose_file, "exec", "-T",
                           "db-backup", "cat", "/textfile/aort_backup.prom"])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("aort_backup_last_success_timestamp_seconds "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aort_recovery", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one failure-and-recovery experiment")
    run.add_argument("--scenario", required=True, choices=sorted(RECOVERY_FOR_SCENARIO))
    run.add_argument("--component", required=True, choices=["fineract", "postgres", "backup"])
    run.add_argument("--failure-seconds", type=float, default=30.0)
    run.add_argument("--probe-interval", type=float, default=2.0)
    run.add_argument("--recovery-timeout", type=float, default=180.0)
    run.add_argument("--health-url", default=DEFAULT_HEALTH)
    run.add_argument("--compose-file", default=DEFAULT_COMPOSE)
    run.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"experiment: {args.scenario} on {args.component}, "
          f"failure {args.failure_seconds:.0f}s")
    try:
        outcome = run_recovery_experiment(
            docker=docker,
            scenario=args.scenario,
            component_id=args.component,
            probe=http_probe(args.health_url, timeout=3),
            ledger=DockerLedger(compose_file=args.compose_file),
            backup_at=newest_backup_epoch(args.compose_file),
            failure_seconds=args.failure_seconds,
            probe_interval=args.probe_interval,
            recovery_timeout=args.recovery_timeout,
            outcomes_path=args.out_dir / "outcomes.jsonl",
            events_path=args.out_dir / "injections.jsonl",
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    rto = outcome["rto_seconds"]
    rpo = outcome["rpo_seconds"]
    print("--- measured ---")
    print(f"  recovered          : {outcome['recovered']}")
    print(f"  RTO (actual)       : {'not recovered' if rto is None else f'{rto:.1f} s'}")
    print(f"  RPO exposure       : {'unknown (no backup)' if rpo is None else f'{rpo:.1f} s'}")
    print(f"  availability       : {outcome['availability_ratio']:.2%} "
          f"({outcome['probes_ok']}/{outcome['probes_total']} probes)")
    print(f"  ledger integrity   : {outcome['transaction_integrity']}")
    print(f"  rows before/after  : {outcome['ledger_rows_before']} / {outcome['ledger_rows_after']}")
    print(f"  outcome written to : {args.out_dir / 'outcomes.jsonl'}")
    return 0 if outcome["recovered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
