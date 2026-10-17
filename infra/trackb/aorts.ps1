# AORTS Track B commands for Windows PowerShell (no make needed).
# Usage: .\aorts.ps1 setup | up | down | db | migrate | seed | test | test-security | demo | experiments | dashboard | clean
param([Parameter(Mandatory = $true)][string]$Target)
$ErrorActionPreference = "Stop"
$py = ".\.venv\Scripts\python.exe"
switch ($Target) {
    "setup" {
        python -m venv .venv
        & $py -m pip install -r infra/trackb/requirements-dev.txt
        if (-not (Test-Path .env)) { & $py infra\trackb\scripts\generate_env.py }
    }
    "up" { docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d --build; docker compose --env-file .env -f infra/trackb/docker-compose.yml ps }
    "down" { docker compose --env-file .env -f infra/trackb/docker-compose.yml down }
    "db" { docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d db }
    "migrate" { docker compose --env-file .env -f infra/trackb/docker-compose.yml run --rm migrate alembic -c backend/aorts/alembic.ini upgrade head }
    "seed" { docker compose --env-file .env -f infra/trackb/docker-compose.yml run --rm migrate python -m backend.aorts.app.seed }
    "test" { $env:AORTS_REQUIRE_DB = "1"; & $py -m pytest tests\trackb }
    "test-security" {
        $env:AORTS_REQUIRE_DB = "1"; & $py -m pytest tests\trackb\acceptance tests\trackb\test_rate_limits.py tests\trackb\test_replay_protection.py tests\trackb\test_telemetry_integrity.py tests\trackb\test_ledger_invariants.py tests\trackb\security
    }
    "demo" { & $py -m backend.aorts.experiments.run_end_to_end --mode http }
    "experiments" {
        & $py -m backend.aorts.experiments.run_replay --mode inprocess
        & $py -m backend.aorts.experiments.run_poisoning
        & $py -m backend.aorts.experiments.run_flooding --mode inprocess
        & $py -m backend.aorts.experiments.compare_results
    }
    "dashboard" { Start-Process "http://localhost:8501" }
    "clean" {
        $a = Read-Host "Delete ALL local synthetic data (docker volume)? [y/N]"
        if ($a -eq "y") { docker compose --env-file .env -f infra/trackb/docker-compose.yml down -v } else { "aborted" }
    }
    default { "Unknown target '$Target'" }
}
