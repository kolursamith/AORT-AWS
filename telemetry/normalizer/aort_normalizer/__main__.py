"""CLI: take I1 snapshots from Prometheus and write them as JSON Lines.

    python -m aort_normalizer --out observations.jsonl --count 6 --interval 10

Output is appended, so repeated runs build a time series. A snapshot is only
written once it has been collected completely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from .normalize import collect_snapshot
from .prometheus import PrometheusClient, TelemetrySourceError

DEFAULT_URL = "http://localhost:9090"


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aort_normalizer",
        description="Normalize AORT Prometheus telemetry into I1 observation records.",
    )
    parser.add_argument("--prometheus-url", default=None,
                        help=f"default: $AORT_PROMETHEUS_URL or {DEFAULT_URL}")
    parser.add_argument("--out", type=Path, default=None,
                        help="JSON Lines file to append to (default: stdout)")
    parser.add_argument("--count", type=_positive_int, default=1,
                        help="number of snapshots to take (default: 1)")
    parser.add_argument("--interval", type=_non_negative_float, default=10.0,
                        help="seconds between snapshots (default: 10)")
    parser.add_argument("--timeout", type=_positive_float, default=10.0,
                        help="per-query timeout in seconds (default: 10)")
    return parser


def main(
    argv: list[str] | None = None,
    client_factory: Callable[[str, float], object] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    url = args.prometheus_url or os.getenv("AORT_PROMETHEUS_URL") or DEFAULT_URL
    factory = client_factory or (lambda u, t: PrometheusClient(u, timeout=t))
    client = factory(url, args.timeout)

    for index in range(args.count):
        if index:
            time.sleep(args.interval)
        try:
            snapshot = collect_snapshot(client)
        except TelemetrySourceError as exc:
            print(f"ERROR: telemetry source unavailable: {exc}", file=sys.stderr)
            return 2

        lines = "".join(json.dumps(o.to_dict(), allow_nan=False) + "\n" for o in snapshot)
        if args.out is None:
            sys.stdout.write(lines)
            sys.stdout.flush()
        else:
            try:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("a", encoding="utf-8") as handle:
                    handle.write(lines)
            except OSError as exc:
                print(f"ERROR: cannot write to {args.out}: {exc}", file=sys.stderr)
                return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
