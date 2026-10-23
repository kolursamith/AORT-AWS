"""E5 telemetry poisoning: clean vs poisoned-unprotected vs poisoned-protected (model-in-the-loop SIMULATION).

python -m backend.aorts.experiments.run_poisoning --repeats 5
"""

from __future__ import annotations

import time

from backend.aorts.experiments.common import env_info, experiment_id, parser, write_outputs
from backend.aorts.security.poisoning_simulator import CASES, PoisonConfig, run_poisoning


def main() -> None:
    args = parser("E5 telemetry poisoning experiment").parse_args()
    exp = experiment_id("poisoning")
    rows, ticks = [], []
    for rep in range(args.repeats):
        for case in CASES:
            for arm in ("clean", "poisoned_unprotected", "poisoned_protected"):
                r = run_poisoning(PoisonConfig(case=case, arm=arm, seed=args.seed + rep))
                rows.append({**r["summary"], "seed": args.seed + rep, "repeat": rep})
                ticks += [
                    {**t, "arm": arm, "poison_case": case, "seed": args.seed + rep}
                    for t in r["rows"]
                ]
    payload = {
        "experiment_id": exp,
        "scenario": "E5_telemetry_poisoning",
        "args": vars(args),
        "environment": env_info("model-in-the-loop"),
        "generated_at": time.time(),
        "note": "Fluid-queue route model is the ground truth; validator, twin update rule, mock controller "
        "and budget guard are the real Track B components.",
        "summaries": rows,
    }
    out = write_outputs(exp, payload, rows)
    out["ticks_csv"] = write_outputs(f"{exp}-ticks", {"rows": len(ticks)}, ticks).get("csv")
    print(out)


if __name__ == "__main__":
    main()
