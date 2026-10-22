# Track B security validation package: completion report

Branch `feature/backend` of `kolursamith/AORT-AWS`. Track B only: no Track A code (`intelligence/`),
generated artifact (`results/`) or shared `contracts/v1` model was modified, imported or run.
**No Track A controller behaviour is validated here.**

All measurements come from a **synthetic, local, isolated** environment: in-process Track B services
(real HTTP handlers, real HMAC signing, real PostgreSQL 16.15) on the repository's Compose PostgreSQL
container (`postgres:16-alpine`, digest `sha256:721873c3…`), Windows 11, Python 3.14.6. No real bank,
NPCI/UPI or public system was contacted. Raw data, run manifest, logs and reproduction commands:
[infra/trackb/evidence/security/](../../infra/trackb/evidence/security/README.md).

## A. Verified backend functionality

Five defects were found and fixed in Track B-owned code. Each has a regression test, and the first
two were reproduced on the unchanged commit before the fix.

| # | Defect | Effect | Fix |
|---|---|---|---|
| 1 | Under concurrent debits from one sender, the funds check read a **stale balance** (the account row was loaded before it was locked, and the locked re-read did not refresh it). | The PostgreSQL non-negative-balance constraint still blocked every overdraft, but the API returned **HTTP 500** instead of 422 `INSUFFICIENT_FUNDS`. Reproduced on `8b863da`: `phaseA/defect1_*`. | `populate_existing` on the `FOR UPDATE` re-read. Test `test_concurrent_debits_cannot_overdraw_the_sender` (10 concurrent quarter-balance payments: exactly 4 accepted, 6 × 422, balance never negative). |
| 2 | A status check for a transaction id the switch had never seen wrote a FAILED "not received" tombstone **owned by whichever bank asked**; a duplicate `/pay` returned another bank's record. | Bank A2 could fence bank A1's payment and lock A1 out of its own status checks (403), and could read A1's status. Funds stayed safe (fencing only blocks a credit). Found by the replay experiment: `phaseA/defect2_*`. | Transaction ids are `TXN<bank>-<16 hex>`; the switch rejects ids from another bank's namespace before any lookup (403, audited `FOREIGN_TRANSACTION_ID`) and checks ownership on duplicate `/pay`. 5 tests in `security/test_transaction_namespace.py`. |
| 3 | A **+30 % throughput lie** stays inside the per-sample cross-check tolerance (±50 %). | Not detected (the audit finding). | One-sided CUSUM per source/route/metric on the relative optimistic deviation (10 % allowance, threshold 0.6, capped so honest sources recover). |
| 4 | When the probe **goes silent**, the last TRUSTED verdict stayed in force indefinitely. | BudgetGuard never fell back, contrary to its documented behaviour. | TRUSTED telemetry older than `telemetry_max_age_seconds` (30 s) is STALE. |
| 5 | The test session **force-drops** the test database; a second concurrent pytest session killed the first one's connections (`phaseA/baseline_db_attempt2_*`). | Spurious failures, i.e. unreliable test evidence. | A server-wide advisory lock per test session; a second session now fails immediately with a clear message (`phaseA/session_lock_check.log`). |

Harness problems found while producing the evidence (fixed, attempts kept, not hidden): raw file
names exceeding the Windows 260-character path limit (first flooding attempt, 5 runs failed); a
counter-key bug that reported 0 pool overloads (second flooding attempt, superseded, re-run); Git
line-ending normalisation that would have broken the manifest hashes on checkout (evidence is now
stored byte-for-byte). See `flooding/SUPERSEDED.md`.

## B. PostgreSQL test results

| Run | Code | Tests | Passed | Skipped | Failed / errors | Log |
|---|---|---|---|---|---|---|
| Without a database (reproduces the audit) | `8b863da` | 184 | 85 | **99** | 0 | `phaseA/baseline_nodb.*` |
| Database required, attempt 1 | `8b863da` | 184 | 85 | 0 | 99 errors: Docker Desktop was not running | `phaseA/baseline_db_attempt1_docker_down.*` |
| Database required, attempt 2 | `8b863da` | 184 | 182 | 0 | 2: a concurrent test session collided (defect 5) | `phaseA/baseline_db_attempt2_concurrent_collision.*` |
| **Database required, baseline** | `8b863da` | **184** | **184** | 0 | 0 | `phaseA/baseline_db.*` |
| After fixes 1, 3, 4, 5 | `6807590` | 240 | 240 | 0 | 0 | `phaseA/after_fixes_db.*` |
| **Final** (`tests/trackb` + shared `tests/contracts`) | `dba5057` + report/figure commit | **256** | **255** | 1 (dashboard loaders: pandas, a dashboard extra, is not in the pinned CI environment; 9/9 pass in the dashboard environment, `phaseA/final_dashboard_tests.log`) | 0 | `phaseA/final_db.*` |

The 99 tests the audit saw as skipped all pass against PostgreSQL. The categories requested
(ledger invariants, concurrent debits, idempotent payments, duplicate reversals, concurrent replay,
outbox atomicity, recovery gates, bank rate limits, protected acceptance tests) are all covered;
outbox atomicity under an injected outbox failure and concurrent overdraft were added
(`integration/test_ledger_atomicity.py`). No test was removed or weakened. The one test whose
assertion changed asserted that the +30 % lie is *not* detected; it now asserts detection, plus two
new tests for the remaining limits.

## C. Flooding experiment (Phase B)

**Setup.** A1 and A2 send legitimate status checks (3 req/s each) and A1/A2 send payments
(4 req/s); A3 floods route B for 8 s after a 4 s baseline, then 6 s of recovery. Switch capacity was
calibrated to what the in-process harness can saturate (8 workers × 40 ms); the attack rate came from
a measured ramp (`flooding/calibration.json`): undefended legitimate checks first degrade at
**150 req/s** (p95 85 → 574 ms, 83 % answered). Conditions: no attack, flood defended/undefended,
flood with A1/A2 on the healthy parallel route C defended/undefended. Seeds 101–105, 25/25 runs
completed (experiment `secpkg-flooding-20261022T110257Z`, commit `ea8075b`).

| Mean over 5 seeds | normal | flood, undefended | flood, defended | parallel route, undefended | parallel route, defended |
|---|---|---|---|---|---|
| Legitimate checks answered during attack | 100 % | 88 % | **100 %** | 84 % | **100 %** |
| Legitimate p95 during attack (ms) | 63 | 570 | **64** | 464 | **64** |
| Legitimate p95 after attack (ms) | 72 | 253 | 61 | 162 | 60 |
| A3 requests admitted / rejected | 0 / 0 | 1200 / 0 | **99 / 1101** | 1200 / 0 | **99 / 1101** |
| Admitted A3 checks turned away by the saturated pool | 0 | 185 | 0 | 214 | 0 |
| Legitimate checks turned away by the saturated pool | 0 | 5.6 | 0 | 7.8 | 0 |
| Rejected requests that reached the worker pool | 0 | 0 | **0** | 0 | **0** |
| A3 rejection latency p50 (ms) | n/a | n/a | 2.6 | n/a | 2.8 |
| Detection (s after attack start) | n/a | never | 0.14 | never | 0.14 |
| Containment (s) | n/a | not during attack | 1.0 | not during attack | 1.0 |
| Recovery after attack (s) | 0 | 1.0 | 0 | 1.0 | 0 |
| Fairness, Jain index A1 vs A2 | 1.00 | 1.00 | 1.00 | 0.99 | 1.00 |
| A3 share of admitted checks during attack | 0 | 0.96 | 0.67 | 0.96 | 0.67 |
| Payment throughput (per s) / unresolved at end | 4.0 / 0 | 4.0 / 0 | 4.0 / 0 | 4.0 / 0 | 4.0 / 0 |

Paired bootstrap 95 % CIs, defended − undefended (same seeds): legitimate success **+0.117
[0.092, 0.154]**, legitimate p95 **−506 ms [−545, −470]**; parallel route: +0.163 [0.121, 0.204] and
−400 ms [−482, −312]. Defended vs no attack: legitimate p95 +0.9 ms [−6.2, 9.9], no clear difference.

**Reading.** Every A3 rejection came from the per-bank rate limit (`RATE_LIMITED`), in about 2.6 ms,
before the request touched the worker pool or any beneficiary; with defenses on, legitimate checks
were indistinguishable from the no-attack condition. A3 still received 67 % of admitted checks
during the attack because it is held *to* its per-bank quota (10/s), which is larger than A1+A2's
legitimate demand (6/s). Tier 2 credits never ran out (A3 ended with about 31 of 60): in this
configuration the rate limit, not the credits, was the binding control. Payments were unaffected in
every condition because of the switch's primary-payment reservation, which is independent of the
flood defenses. Payment p95 varied widely between runs (even the no-attack condition averaged
601 ms; conditions ran in a fixed order per seed), so no payment-latency effect is claimed.

Graphs: `figures/flooding_*.svg`. Per-request data: `flooding/raw/<file_key>-requests.csv`.

## D. Telemetry poisoning (Phase C)

### Model-in-the-loop (14 cases × 4 arms × 5 seeds, 280/280 runs)

A fluid-queue model of route B is the ground truth and provides the independent switch measurement;
the validator, twin update rule, mock controller and BudgetGuard are the real Track B components.
Bank B's capacity halves at tick 20 (a real degradation), poisoning starts at tick 10, 60 ticks.
Arms: *protected* (current code), *protected without the bias check* (the validator before this
package), *unprotected* (validator and guard off), *clean* (no lie).

| Case | Detection, protected | Detection, without bias check | Ticks > 1 s delay, protected |
|---|---|---|---|
| falsely low latency (×0.2) | 1.00 | 1.00 | 0 |
| falsely high throughput (×4) | 1.00 | 1.00 | 0 |
| false timeout rate 0 + low latency | 0.99 | 0.73 | 0 |
| conflicting timeout / backlog | 1.00 | 1.00 | 0 |
| **subtle +30 % throughput** | **0.95** (first flag ≈ 2.4 ticks) | **0.00** | 0 |
| subtle +8 % throughput (below the 10 % allowance) | 0.00 | 0.00 | 0 |
| false high timeout rate (pessimistic) | 0.00 | 0.00 | 0 |
| inconsistent backlog (pessimistic) | 0.00 | 0.00 | 0 |
| replayed old observation | 1.00 | 1.00 | 0 |
| stale timestamp (−120 s) | 1.00 | 1.00 | 0 |
| invalid values | 1.00 | 1.00 | 0 |
| signer ≠ claimed source | 1.00 | 1.00 | 0 |
| unregistered source | 1.00 | 1.00 | 0 |
| missing (probe silent) | n/a (nothing sent); guard STALE for the last 20 of 50 ticks | same | 0 |

**False positives: 0 flagged ticks in 4,200 clean-arm ticks**, through the real tick-20 degradation.
Not detected: optimistic bias below the 10 % allowance and pessimistic lies; neither caused a single
over-1-s tick with the guard on, because the guard caps the budget by the switch's own measured
headroom and suspicious telemetry can only freeze or lower it. In the unprotected arm every case
showed 33–40 ticks over 1 s; that arm disables the guard as well as the validator, so about 33 of
those ticks come from the unguarded controller during the real degradation, not from the lie itself.

### Live ingestion path

The same validator and guard inside the running Track B control service, with **real switch
measurements** from live payment traffic (A1 → B, 4 payments/s), samples injected through the
test-only `POST /api/v1/lab/telemetry/inject` endpoint (admin token + attack flag), 6 honest samples
then 12 attack samples per run (or 34 s of silence), and BudgetGuard read through the dry-run
decision endpoint. 9 cases × protected/unprotected × seeds 101–105: **90/90 runs completed**
(experiment `secpkg-poisoning-live-20261022T111521Z`, commit `8778efd`).

| Case | Flagged, protected | First flag (sample) | Guard during attack, protected | Flagged, unprotected |
|---|---|---|---|---|
| honest | 0 / 60 | n/a | TRUSTED | 0 |
| throughput ×4 (signed by probe 2) | 60 / 60 | 1st | SUSPICIOUS (fallback) | 0 |
| latency −80 % | 60 / 60 | 1st | SUSPICIOUS | 0 |
| timeout rate falsely 0 (+ low latency) | 60 / 60 | 1st | SUSPICIOUS | 0 |
| inconsistent backlog (pessimistic) | 0 / 60 | n/a | TRUSTED | 0 |
| **subtle +30 % throughput** | **35 / 60 (0.58)** | **6th (≈ 3 s)** | SUSPICIOUS from the 6th sample | 0 |
| stale timestamp (−120 s) | 60 / 60 | 1st | REJECTED | 0 |
| signer ≠ claimed source | 60 / 60 | 1st | REJECTED | 0 |
| missing (probe silent 34 s) | nothing sent | n/a | **STALE** from the 15th 2-s check (≈ 30 s) | guard off |

**0 false positives in 540 honest-phase samples.** Results were identical across the five seeds.
The subtle lie is caught later on the live path than in the model (6th sample vs about 2.4 ticks)
because the live route carried only about 1 transaction/s, below the validator's 2 tps absolute
floor, so each sample's relative deviation is computed against the floor and accumulates more
slowly. Every injected sample was stored as lab test data (`experiment_id = lab-inject`), and no
other telemetry rows were written; the switch's own measurements are never changed by injection.
At this load the mock controller recommended 1 check per tick throughout, so the live dry run shows
the guard's integrity decision but no budget change (see J).

## E. Replay protection (Phase D)

23 cases per run (duplicate status response, response for an older version, stale event timestamp,
repeated and concurrent reversals with the same and with new operation ids, concurrent duplicate
events, agreeing and conflicting responses after close, response for the wrong transaction, event
signed by a non-switch identity, missing/invalid idempotency key, captured request replayed (same
nonce), expired request timestamp, tampered body (invalid signature), unsigned request, status
check claiming another bank, status check for another bank's transaction), with and without
automatic reversal, seeds 101–105: **10/10 runs, 230/230 cases answered with the specified outcome,
0 unexpected financial changes, 0 duplicate reversal postings, ledger verification and audit hash
chain OK in every run** (25 audit rows recording rejections per run). Per-case detail:
`replay/raw/<file_key>-cases.csv`. The cases are deterministic; the repetitions show the outcome is
stable.

## F. Security dashboard

`streamlit run dashboard/trackb_app.py` → **Security validation** tab (reads
`infra/trackb/evidence/security/`, override with `AORTS_EVIDENCE_DIR`): run counts and failures,
requests per bank (admitted / rejected), A3 rejection reasons (rate limits, credit exhaustion),
legitimate latency and success during attacks, detection / containment / recovery / fairness table,
per-second request timeline per run (backlog recovery), poisoning detection per case and arm, false
positives, live-path verdicts, replay results, and the full run manifest. The existing **Security**
tab keeps the live counters (checks per bank, quotas and credits, telemetry verdicts, replay and
duplicate rejections, security events). Static graphs for the report are in `figures/` (SVG,
light and dark).

## G. Raw evidence manifest

`infra/trackb/evidence/security/manifest.jsonl`: **436 runs started, 430 completed, 5 failed, 1 interrupted** (stopped by hand during the first flooding attempt; kept). `python -m
backend.aorts.experiments.security_package verify` recomputes every output hash: **490 outputs verified, 0 hash mismatches, 0 missing files** (`verify.json`).
Environment per experiment (commit, dependency versions, PostgreSQL version and image digest):
`environment-*.json`. The earlier 449-run strategy campaign's raw files are now also committed
(`infra/trackb/evidence/strategy-campaign/`): 60/60 files match the hashes recorded when it ran;
its README reconciles 452 result lines with 449 unique runs.

## H. Security CI status

| Check | Result |
|---|---|
| Secret scanning (gitleaks v8.24.3, full history, as in `security.yml`) | The `main` failure was **one false positive**: `generic-api-key` on `tests/trackb/contract/test_contracts.py:45`, value `"idem-12345678"`, a synthetic idempotency key in a schema test (matches no configured secret, authenticates nothing). Narrowest exception: one exact fingerprint in `.gitleaksignore`. Reproduced without it (1 leak), no leaks with it: `phaseG/`. No credential rotation needed. |
| Secret scanning of this package's own commits | The first full-history scan after the evidence commits found **92 more false positives**, all the same: `"file_key": "s101-55e53c6d"` in `manifest.jsonl` (a run's raw-file name; the rule matches any `*_key` field with a random-looking value). Fix: `.gitleaks.toml` adds one **rule-level** allowlist to `generic-api-key` that requires both this one file *and* the exact `s<seed>-<8 hex>` shape. A test with planted fake keys shows a real-looking key in the manifest and the same shape in another file are both still reported (`phaseG/gitleaks_allowlist_narrowness_test.log`). The global `[allowlist]` form was tried first and rejected: gitleaks 8.24.3 ignores its `condition = "AND"`, which would have allowed the shape in every file. Full-history scan with this configuration (all 16 commits before the one that adds it): **no leaks** (`phaseG/gitleaks_final_history.log`). |
| Real secrets in this package's commits | 0. Two test logs captured the local database password in tracebacks; it was redacted before any commit, and every commit was scanned against the local `.env` values. |
| `ruff format --check` / `ruff check` | Clean (225 files formatted, all checks passed): `phaseG/lint_schema.log` |
| Contract schema drift | No drift after `python -m contracts.v1.generate_schemas` |
| Ownership admission (`check_ownership.py`) | Passed for all 694 files this branch changes against `developer` (author `girinandanv9845-ship-it`): `phaseG/ownership_check.log`. Nothing under `intelligence/` or `results/` was changed. |
| `pip-audit` | First run: 1 advisory, `certifi 2026.7.22` PYSEC-2026-4320 (transitive via httpx; fix 2026.10.19), from a stale local environment; a fresh install resolves the fixed version. After upgrading: **no known vulnerabilities** (`phaseG/pip_audit*.log`). The project itself is not on PyPI and is skipped, as expected. |
| Track B tests with PostgreSQL | see B |

## I. Git changes and branch status

Branch `feature/backend`, **11 local commits on top of `8b863da`, not pushed** (pushing and
merging need your explicit authorization):

| Commit | Content |
|---|---|
| `6807590` | Fixes 1, 3, 4, 5; outbox-atomicity and concurrent-overdraft tests; Phase A logs |
| `1f09c24` | Fix 2 (transaction-id namespace); experiment runner; replay case 11; `.gitleaksignore` |
| `b7b69ed` | Dashboard tab; flooding calibration; gitleaks evidence |
| `51451df` | Runner: short raw-file keys, full abort; SVG figures; unit tests |
| `ea8075b` | Runner: counter extraction fix; `verify`; superseded attempt kept |
| `b8b3a9f` | Flooding evidence; byte-exact evidence storage |
| `9c5595d` | Poisoning (model) evidence |
| `8778efd` | Replay evidence |
| `dba5057` | Strategy-campaign raw files; `SECURITY.md`, `API_CONTRACTS.md` |
| `7fb55d0` | Live poisoning evidence, figures, final test logs, this report, `STATUS.md` |
| (last commit) | `.gitleaks.toml` (rule-level allowlist for the manifest `file_key`), gitleaks evidence, report section H |

Every commit was scanned against the local `.env` secret values (0 hits) and passed ruff before it
was made.

## J. Unresolved limitations

- Single laptop, in-process services and a deliberately small calibrated switch; absolute latencies
  do not transfer to real systems. 5 seeds per condition: enough for the large effects reported,
  not for small ones.
- Flooding conditions ran in a fixed order per seed (not randomised).
- Telemetry poisoning below the 10 % bias allowance and pessimistic lies are not detected (bounded,
  not prevented); live detection of subtle lies is slower on a nearly idle route.
- The live poisoning dry run used the mock controller at low load, which recommended 1 check per
  tick throughout, so budget effects on the live path are not observable there; they are evidenced
  by the model-in-the-loop experiment.
- Nonces, token buckets and Tier 2 credits are in-memory and single-replica; HMAC uses shared keys
  (no mTLS).
- The Docker image was not rebuilt in this session (package download at about 16 kB/s); the tests
  and experiments ran in-process against the Compose PostgreSQL.
- The new CI PostgreSQL step and the `.gitleaksignore` take effect on GitHub only once the branch is
  pushed and a pull request runs.

## K. Independent deliverables ready for integration

- Defect fixes 1–5 with regression tests (Track B code only).
- `backend.aorts.experiments.security_package`: calibrate / flooding / poisoning / poisoning-live /
  replay / figures / verify, with the run manifest.
- The evidence package and the strategy-campaign raw files.
- Dashboard "Security validation" tab.
- `.gitleaksignore` with the single reviewed fingerprint.
- Updated `SECURITY.md` and `API_CONTRACTS.md` (transaction id format).

**TRACK B SECURITY PACKAGE VERIFIED — READY FOR FINAL INTEGRATION**
