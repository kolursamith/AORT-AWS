# Strategy-comparison campaign (2026-10-17): raw files

The raw and processed files of the 30-seed AORTS-vs-baselines campaign reported in
[docs/trackb/RESULTS.md](../../../../docs/trackb/RESULTS.md). The 2026-10-22 project audit noted
that these files "were not available for verification": they were produced locally and not committed,
because `results/` is owned by the other track. They are now kept here, in a Track B-owned folder,
**byte-for-byte as produced** (`.gitattributes: -text`).

## Integrity check

All **60** files listed with a SHA-256 in
[04-result-files-and-hashes.md](../../../../docs/track-b-technical-evidence/04-result-files-and-hashes.md)
(written on 2026-10-17, when the campaign ran) were copied here and re-hashed on 2026-10-22:
**60 / 60 match, 0 mismatches**. Two files are not in that list:
`raw/end-to-end-20261017T161057.json` and `processed/end-to-end-20261017T161057.csv` (an end-to-end
smoke run made after the list was written).

These runs predate the Git repository, so their provenance is a **source-tree SHA-256**
(`provenance.source_sha256` in each JSONL header line), not a commit id.

## Run count reconciliation (449 completed, 6 failed)

The five `raw/strategy-strategy-comparison-*.jsonl` files contain:

| Line type | Lines |
|---|---|
| provenance header (one per file) | 5 |
| run result | 452 |
| run error (`error`) | 5 |
| aborted marker (`aborted`, after 3 consecutive failures) | 1 |

452 result lines are **449 unique (scenario, strategy, seed) runs plus 3 duplicates**: S2 seed 1013
for `static-polling`, `exp-backoff-jitter` and `fixed-budget` ran in file `...-b245b7` before the
Docker engine hung, and the whole seed was re-run in file `...-2ec23f` after the restart.
`analyze_strategy_comparison.analyze()` keys runs by seed (`{seed: run}`), so every statistic used
exactly one run per seed (the later re-run). The 6 failed runs (5 errors + 1 aborted marker) are
excluded and reported. Note: `processed/strategy-summary-runs.csv` lists all 452 result rows
including the 3 superseded duplicates, without marking them.

## Superseded files

`raw/superseded/` and `processed/superseded/` hold runs replaced after harness bugs; each has a
`NOTE.md` that explains why. They are kept for transparency and are not used in the results.
