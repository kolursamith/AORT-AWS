# AORTS project completion register

One entry per stage checkpoint, written from the recorded evidence of that checkpoint.

The baseline is the work verified before this handoff:

* Track A `feature/intelligence` `5024e6c`: Stage 09; 1,339 tests at that time.
* Track B `feature/backend` `9e736d9`.
* Stage 09R `00c51ec`: 1,621 passed on PostgreSQL. It was a local commit, and is now part of
  `feature/aorts-integration`.

## Stage 10: integrated security, resilience and three attacks

| item | value |
|---|---|
| branch | `feature/aorts-integration` |
| commits | `e0c7020` (WIP recovery checkpoint), `f068383` (resumable harness; experiment source), Stage 10 checkpoint commit (see `git log`) |
| files changed | listed in `docs/integration/STAGE10_SECURITY_INTEGRATION.md` §J |
| tests | full suite on PostgreSQL 16.15, `AORTS_REQUIRE_DB=1`, at f068383 + the Stage 10 docs and evidence: **1644 passed, 0 failed, 0 skipped** (JUnit-checked), 934 s |
| PostgreSQL | real server (disposable container `aorts09r-pg`, tmpfs) |
| static gates | ruff check and format clean; `compileall` OK; v1 schema regeneration unchanged; `git diff --check` clean; gitleaks clean in git mode |
| experiment evidence | `docs/integration/evidence/stage10/` (manifests, runs CSVs, summaries); raw files in `results/processed/stage10/` (not in Git) |
| evidence checksums | runs CSV and manifest SHA-256 in the Stage 10 report §L; 542-file checksummed backup at `CAD_project/AORTS_research_evidence_backup/stage10_final_2026-10-23/` |
| experiment runs | replay 10/10, resilience 5/5, poisoning 95/95, flooding 55/55: 0 failed; one source digest `d75babd2…` |
| GitHub | pushed normally to `origin/feature/aorts-integration` (SHA recorded in the Stage 10 report) |
| limitations | the validator's flags are not specific to lying (a capacity drop is flagged too); detection lag and inflated headroom with the validator disabled; trust restoration slower than 6 s phases after a latch release or restart; incomplete route isolation without defences (shared worker pool); controller budgets small; FAST estimator profile |
