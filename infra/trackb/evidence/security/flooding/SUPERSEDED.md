# Superseded flooding attempts

| Experiment id | Runs | Why superseded | Kept files |
|---|---|---|---|
| (first attempt, 2026-10-22 ~10:47 UTC) | 5 started, 5 failed | Raw file names embedded the full run id and exceeded the Windows 260-character path limit; nothing was written. | `manifest.jsonl` (failed end records), `flooding_attempt1_failed_path_length.log` |
| `secpkg-flooding-20261022T105127Z` | 25 completed | All runs completed, but two derived columns were wrong: `switch_overloaded_checks` was always 0 (a hand-built counter key missed the sorted label order `core_overloaded{bank=..,kind=check}`) and `*_entered_processing` counted service-latency samples instead of requests that reached the worker pool. The raw JSON per run (with the full switch counters) is correct and hash-locked in the manifest; the experiment was re-run with the fixed extraction instead of editing recorded data. | `secpkg-flooding-20261022T105127Z-runs.csv`, `secpkg-flooding-20261022T105127Z-summary.json`, its `raw/` files, `flooding_attempt2_superseded.log` |

The dashboard and figures use the newest experiment only.
