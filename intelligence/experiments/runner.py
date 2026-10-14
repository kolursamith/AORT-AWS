"""Initial experiment runner backed by the deterministic simulator."""

import json
from dataclasses import asdict
from pathlib import Path

from intelligence.simulation.engine import run


def run_experiment(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([asdict(tick) for tick in run()], indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    run_experiment(Path("results/raw/hackathon.json"))
