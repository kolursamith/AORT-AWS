"""CLI for ledger backup state and restore.

    python -m aort_backup status
    python -m aort_backup list
    python -m aort_backup restore --into aort_restore_check

Run from the repository root with the banking stack up.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

from .dumps import newest_dump
from .metrics import backup_age_seconds, parse_textfile
from .restore import RestoreError, compose_exec_argv, restore_into

DEFAULT_COMPOSE = str(Path(__file__).resolve().parents[2] / "docker-compose.yml")
SERVICE = "db-backup"


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    """Absent or corrupted values print as '-', never as 'nan'."""
    if value is None or not math.isfinite(value):
        return "-"
    return format(value, spec) + suffix


def run(argv, timeout: float = 300.0):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def in_container(argv, compose_file: str):
    return run(compose_exec_argv(SERVICE, argv, compose_file))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aort_backup", description=__doc__)
    parser.add_argument("--compose-file", default=DEFAULT_COMPOSE)
    parser.add_argument("--user", default="postgres")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="last backup, its age and size")
    sub.add_parser("list", help="dump files currently retained")
    restore = sub.add_parser("restore", help="restore the newest dump into a database")
    restore.add_argument("--into", required=True)
    restore.add_argument("--database", default="fineract_default",
                         help="which database's dump to restore (default: fineract_default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "status":
        code, out, err = in_container(["cat", "/textfile/aort_backup.prom"], args.compose_file)
        if code != 0:
            print(f"ERROR: could not read backup state: {err.strip()}", file=sys.stderr)
            return 2
        metrics = parse_textfile(out)
        age = backup_age_seconds(metrics, now=time.time())
        print(f"runs         : {_fmt(metrics.get('aort_backup_runs_total'), '.0f')}")
        print(f"failures     : {_fmt(metrics.get('aort_backup_failures_total'), '.0f')}")
        print(f"last size    : {_fmt(metrics.get('aort_backup_last_size_bytes'), '.0f', ' bytes')}")
        print(f"last duration: {_fmt(metrics.get('aort_backup_last_duration_seconds'), '.3f', ' s')}")
        print(f"age (RPO)    : {'no successful backup yet' if age is None else f'{age:.1f} s'}")
        return 0 if age is not None else 1

    code, out, err = in_container(["sh", "-c", "ls -1 /backups"], args.compose_file)
    if code != 0:
        print(f"ERROR: could not list dumps: {err.strip()}", file=sys.stderr)
        return 2
    names = [n for n in out.split() if n.endswith(".dump")]

    if args.command == "list":
        for name in sorted(names):
            print(f"  {name}")
        print(f"{len(names)} dump(s)")
        return 0

    newest = newest_dump(names, database=args.database)
    if not newest:
        print(f"ERROR: no dump found for {args.database}", file=sys.stderr)
        return 1
    print(f"restoring {newest} into {args.into} ...")
    try:
        restore_into(run, f"/backups/{newest}", args.into, user=args.user,
                     compose_file=args.compose_file, service=SERVICE)
    except (RestoreError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    print("restore complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
