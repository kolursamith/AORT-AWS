# scenarios/

**Owner: Owner A (semantics) · Owner B (injection) · Phase 5 — Failure scenarios · Status: not started**

Controlled failure and degradation scenarios, and how their impact propagates
through the twin.

**Split ownership** — see `.github/CODEOWNERS`:
- Owner A: scenario semantics, impact propagation, compound scenario design
- Owner B: failure-injection infrastructure, in `scenarios/injection/`

Planned controlled experiments: service failure, CPU/resource stress, database
degradation, network degradation, workload surge. Destructive or real-world
attacks are explicitly out of scope.

Relevant Phase 2 decision: neither banking container sets a Docker restart
policy, so an injected failure is not silently undone.
