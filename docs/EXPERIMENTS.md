# Experiment protocol

## Independent variables

Fault type/severity, offered payment load, background check load, recovery policy, malicious bank share, telemetry corruption, replay pattern, and random seed.

## Outcomes

- successful payment goodput;
- payment timeout/ambiguity rate;
- status-check accepted/rejected rate and queue depth;
- median and p95 ambiguous-resolution time;
- proportion resolved by the explicitly defined T+1 proxy deadline;
- predicted-versus-observed route capacity/delay error;
- duplicate/stale/unsupported financial operations accepted (target: zero).

RTO, RPO, and T+1 must be reported separately. Database RPO is not measured by the current simulator.

## Comparisons

Compare no recovery, immediate checks, fixed rate, exponential backoff, proportional/AIMD, digital-twin fallback, and trained controller under identical workloads and seeds. Include ablations for ranking, look-ahead, confidence gate, and each defense.

Store raw immutable run output under ignored `results/raw`, processed tables under `results/processed`, figures under `results/figures`, and commit compact reproduction metadata (commit SHA, seed, scenario, configuration, dependency versions). Use repeated seeds and report uncertainty; a single demo run is not evidence.
