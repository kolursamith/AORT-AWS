# Track B security validation: evidence package

Raw, unedited evidence for the Track B security validation package (PostgreSQL correctness,
status-check flooding, telemetry poisoning, transaction replay, secret scanning). The results and
their interpretation are in [docs/trackb/SECURITY_VALIDATION.md](../../../../docs/trackb/SECURITY_VALIDATION.md).

Everything here comes from a **synthetic, local, isolated** environment: in-process Track B services
(real HTTP handlers, real HMAC signing, real PostgreSQL) on the Compose PostgreSQL container. No real
bank, NPCI/UPI or public system was contacted. No Track A code was run or imported.

## Layout

| Path | Contents |
|---|---|
| `manifest.jsonl` | One `start` and one `end` line per run: experiment id, scenario id, condition, seed, run id, file key, git commit, configuration, start/end time, status (`completed` / `failed` + error), and every output file with its SHA-256. Failed and interrupted runs stay in it. |
| `environment-*.json` | Git commit, Python, platform, dependency versions, PostgreSQL server version, PostgreSQL image and digest, recorded at the start of each experiment command. |
| `phaseA/` | PostgreSQL test runs (logs and JUnit XML): baseline without DB, baseline with DB, after the fixes, defect reproductions, the test-session lock check. Failed attempts are kept. |
| `flooding/` | `calibration.json` (attack-rate ramp), `<experiment>-runs.csv` (one row per run), `<experiment>-summary.json` (mean, std, 95 % CI per condition; paired bootstrap CI defended vs undefended), `raw/<file_key>.json` and `raw/<file_key>-requests.csv` (every request: bank, time, latency, status, code). `SUPERSEDED.md` lists replaced attempts. |
| `poisoning/` | Model-in-the-loop telemetry poisoning: runs CSV, summary, `raw/<file_key>-ticks.csv` (every tick: clean vs reported values, verdict, reasons, budgets, delay). |
| `poisoning_live/` | Poisoning through the real ingestion path: runs CSV, summary, `raw/<file_key>-samples.csv` (every injected sample: verdict, reasons, guard decision). |
| `replay/` | Replay experiment: runs CSV, summary, `raw/<file_key>-cases.csv` (every case: HTTP status, expected status, ledger postings, balance, outcome) and `raw/<file_key>.json` (ledger verification, audit chain, audit actions). |
| `phaseG/` | gitleaks over the full history with and without `.gitleaksignore`. |
| `figures/` | SVG figures generated from the runs CSVs (the CSVs are the table view). |

Files under `infra/trackb/evidence/` are stored byte-for-byte (`.gitattributes: -text`) so the
SHA-256 values in the manifest match a fresh checkout on any operating system.

## Reproduce

From the repository root, Windows PowerShell (Linux/macOS: same commands with `/` paths):

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python infra/trackb/scripts/generate_env.py          # .env with fresh random secrets
docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d db

# Phase A: every Track B test, PostgreSQL required (fails instead of skipping without it)
$env:AORTS_REQUIRE_DB = "1"; .\.venv\Scripts\python -m pytest tests/trackb -v -rs

# Phases B-D (one experiment at a time; each wipes the aorts_experiments database)
$m = "backend.aorts.experiments.security_package"
.\.venv\Scripts\python -m $m calibrate
.\.venv\Scripts\python -m $m flooding       --seeds 101 102 103 104 105
.\.venv\Scripts\python -m $m poisoning      --seeds 101 102 103 104 105
.\.venv\Scripts\python -m $m poisoning-live --seeds 101 102 103 104 105
.\.venv\Scripts\python -m $m replay         --seeds 101 102 103 104 105
.\.venv\Scripts\python -m $m figures
.\.venv\Scripts\python -m $m verify          # recompute every SHA-256 in the manifest

# Phase G: secret scan of the full history, as in .github/workflows/security.yml
docker run --rm -v "${PWD}:/repo:ro" ghcr.io/gitleaks/gitleaks:v8.24.3 detect --source /repo --redact --log-opts="--all" -v
```

Add `--out <folder>` to write a fresh package somewhere else instead of appending to this one.
Absolute latencies depend on the machine. The comparisons between conditions are paired by seed.

## Dashboard

`streamlit run dashboard/trackb_app.py` (dashboard extras installed), then open the
**Security validation** tab. It reads this folder (override with `AORTS_EVIDENCE_DIR`) and shows
the newest experiment of each kind.
