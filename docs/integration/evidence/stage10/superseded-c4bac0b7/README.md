# Superseded Stage 10 evidence (source digest c4bac0b7…, commit 00c51ec + uncommitted tree)

Kept for provenance; **not used in any Stage 10 result**.

- `replay/` (10 runs) and `resilience/` (5 runs) completed, but on an uncommitted tree whose
  harness could not resume. They are superseded by the re-run from committed source (`f068383`)
  so that all four Stage 10 campaigns share one source digest.
- `poisoning/manifest.jsonl` is an **interrupted** campaign: the host went to sleep during runs
  (wall-clock duration far above `duration_s`, which excludes sleep), one run failed with
  `401 Unauthorized` (signature timestamp outside ±300 s after a sleep), and the process ended
  mid-run. Its per-run rows were never written. These runs are invalid as measurements.
- Flooding had not started.

Raw per-tick files: `results/processed/stage10_superseded_c4bac0b7/` (not in Git) and a
checksummed copy in `CAD_project/AORTS_research_evidence_backup/stage10_preresume_2026-10-23/`.
