"""E6 replay, E9 reversal race, E10 invalid authorization (sequential + concurrent).

python -m backend.aorts.experiments.run_replay --mode inprocess --repeats 5
"""

from __future__ import annotations

import time

from backend.aorts.experiments.common import (
    env_info,
    experiment_id,
    http_client,
    inprocess_stack,
    parser,
    write_outputs,
)
from backend.aorts.security.replay_simulator import ReplaySimulator


def main() -> None:
    args = parser("E6/E9/E10 replay experiment").parse_args()
    exp = experiment_id("replay")
    rows, verifications = [], []
    for rep in range(args.repeats):
        for auto in (True, False):
            if args.mode == "inprocess":
                with inprocess_stack(auto_reverse_on_confirmed_failure=auto) as st:
                    records = ReplaySimulator(
                        st.client, st.db, st.settings.admin_token.get_secret_value()
                    ).run_all()
                    v = st.ledger.verify_ledger()
            else:
                if not auto:
                    continue  # the running stack has a fixed auto-reverse setting
                client, settings = http_client()
                from backend.aorts.app.db import Database

                db = Database(settings.database_url.get_secret_value(), 5)
                records = ReplaySimulator(
                    client, db, settings.admin_token.get_secret_value()
                ).run_all()
                v = None
            verifications.append({"repeat": rep, "auto_reverse": auto, "ledger": v})
            rows += [
                {
                    **r,
                    "repeat": rep,
                    "auto_reverse": auto,
                    **{k: str(r[k]) for k in ("status_codes",) if k in r},
                }
                for r in records
            ]
            print(
                f"repeat {rep} auto_reverse={auto}: {len(records)} cases, ledger ok={v and v['ok']}",
                flush=True,
            )
    dup_postings = [r for r in rows if (r.get("reversal_postings") or 0) > 1]
    payload = {
        "experiment_id": exp,
        "scenario": "E6_E9_E10_replay",
        "args": vars(args),
        "environment": env_info(args.mode),
        "generated_at": time.time(),
        "ledger_verifications": verifications,
        "duplicate_reversal_postings_observed": len(dup_postings),
        "cases": rows,
    }
    print(write_outputs(exp, payload, rows))


if __name__ == "__main__":
    main()
