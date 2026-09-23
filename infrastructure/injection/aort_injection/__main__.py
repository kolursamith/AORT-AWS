"""CLI for controlled failure injection.

    python -m aort_injection list
    python -m aort_injection inject --scenario service_pause --component fineract --duration 30
    python -m aort_injection inject --scenario cpu_throttle --component fineract --cpus 0.2

The stack must be running. Every injection is reverted before the command
returns, including on Ctrl-C.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .actions import SCENARIOS
from .session import InjectionError, run_injection
from .targets import CONTAINERS

DEFAULT_EVENTS = Path("infrastructure/injection/runs/injections.jsonl")


def docker(argv):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aort_injection", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="available scenarios and targets")

    inject = sub.add_parser("inject", help="inject one controlled failure")
    inject.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    inject.add_argument("--component", required=True, choices=sorted(CONTAINERS))
    inject.add_argument("--duration", type=float, default=30.0,
                        help="seconds to hold the failure (default: 30)")
    inject.add_argument("--cpus", default=None, help="cpu_throttle only, e.g. 0.2")
    inject.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        print("scenarios:")
        for name in sorted(SCENARIOS):
            print(f"  {name}")
        print("components:")
        for component, container in sorted(CONTAINERS.items()):
            print(f"  {component:<10} -> {container}")
        return 0

    parameters = {"cpus": args.cpus} if args.cpus is not None else {}
    print(f"injecting {args.scenario} into {args.component} for {args.duration:.0f}s ...")
    try:
        injection_id = run_injection(
            docker, args.scenario, args.component, parameters,
            hold_seconds=args.duration, events_path=args.events)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except InjectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted; the injection was reverted", file=sys.stderr)
        return 130

    print(f"done; reverted. injection_id={injection_id}")
    print(f"events appended to {args.events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
