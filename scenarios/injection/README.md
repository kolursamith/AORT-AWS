# scenarios/injection/

**Owner: Owner B · Phase 5 — Failure scenarios · Status: not started**

Failure-injection infrastructure: the mechanisms that actually perturb the
running system (stopping containers, applying resource pressure, introducing
database or network latency).

Owned separately from the rest of `scenarios/`, which is Owner A's — see
`.github/CODEOWNERS`. Scenario *semantics* and impact propagation are Owner A's;
the injection *mechanism* is Owner B's.
