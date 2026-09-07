"""Recording of workload results.

Each Fineract call the generator makes produces exactly one OperationResult,
built from the real HTTP response. These records are the raw material the later
AORT phases (telemetry validation, dataset construction) will draw on.

NOTE: this record schema is PROVISIONAL. Freezing it is a joint Owner A /
Owner B decision under the project's contract rules, not a Phase 2 call.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class OperationResult:
    """One banking operation attempt against Fineract."""

    run_id: str
    seq: int
    timestamp: str
    category: str          # client | savings | loan | transaction | accounting | bootstrap
    operation: str         # e.g. "savings.deposit"
    method: str
    path: str
    http_status: int | None
    ok: bool
    latency_ms: float
    resource_id: int | None = None
    error: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class RunReport:
    """Collects OperationResults and writes them out as JSONL plus a summary."""

    def __init__(self, run_id: str, output_dir: Path | None = None) -> None:
        self.run_id = run_id
        self.started_at = datetime.now(timezone.utc)
        self.results: list[OperationResult] = []
        self._seq = 0
        self.output_dir = output_dir

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, result: OperationResult) -> OperationResult:
        self.results.append(result)
        return result

    # --- derived views -------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return self.total - self.succeeded

    def by_operation(self) -> dict[str, dict[str, Any]]:
        """Per-operation counts and latency, computed only from observed calls."""
        buckets: dict[str, dict[str, Any]] = {}
        for r in self.results:
            b = buckets.setdefault(
                r.operation, {"attempts": 0, "ok": 0, "failed": 0, "latencies": []}
            )
            b["attempts"] += 1
            b["ok" if r.ok else "failed"] += 1
            b["latencies"].append(r.latency_ms)

        summary: dict[str, dict[str, Any]] = {}
        for op, b in sorted(buckets.items()):
            lat = sorted(b["latencies"])
            summary[op] = {
                "attempts": b["attempts"],
                "ok": b["ok"],
                "failed": b["failed"],
                "latency_ms_min": round(lat[0], 1),
                "latency_ms_median": round(lat[len(lat) // 2], 1),
                "latency_ms_max": round(lat[-1], 1),
            }
        return summary

    def failures(self) -> list[OperationResult]:
        return [r for r in self.results if not r.ok]

    # --- output --------------------------------------------------------

    def write(self) -> tuple[Path, Path] | None:
        """Persist results as JSONL and a JSON summary. Returns the paths."""
        if self.output_dir is None:
            return None
        self.output_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = self.output_dir / f"{self.run_id}.operations.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for r in self.results:
                fh.write(json.dumps(asdict(r)) + "\n")

        summary_path = self.output_dir / f"{self.run_id}.summary.json"
        summary = {
            "run_id": self.run_id,
            "schema_version": "provisional-0.1",
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "total_operations": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "by_operation": self.by_operation(),
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return jsonl_path, summary_path

    def print_summary(self) -> None:
        print("\n" + "=" * 72)
        print(f"AORT workload run {self.run_id}")
        print("=" * 72)
        rows = self.by_operation()
        if rows:
            print(f"{'operation':<28} {'ok':>5} {'fail':>5} {'p50 ms':>9} {'max ms':>9}")
            print("-" * 72)
            for op, s in rows.items():
                print(
                    f"{op:<28} {s['ok']:>5} {s['failed']:>5} "
                    f"{s['latency_ms_median']:>9} {s['latency_ms_max']:>9}"
                )
        print("-" * 72)
        print(f"{'TOTAL':<28} {self.succeeded:>5} {self.failed:>5}")

        if self.failed:
            print(f"\n{self.failed} failed operation(s):")
            for r in self.failures()[:15]:
                print(f"  [{r.operation}] HTTP {r.http_status}: {r.error}")
            if self.failed > 15:
                print(f"  ... and {self.failed - 15} more")
