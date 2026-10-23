# AORTS Track B developer commands. Windows without make: use .\aorts.ps1 <target>
PY ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python
endif

.PHONY: setup env up down db migrate seed test test-security test-fast demo dashboard experiments clean

setup:            ## create venv, install deps, generate .env with random secrets
	python -m venv .venv
	$(PY) -m pip install -r infra/trackb/requirements-dev.txt
	$(PY) infra/trackb/scripts/generate_env.py || true

up:               ## build and start the whole stack (Docker)
	docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d --build
	docker compose --env-file .env -f infra/trackb/docker-compose.yml ps

down:             ## stop the stack (data volume kept)
	docker compose --env-file .env -f infra/trackb/docker-compose.yml down

db:               ## start only PostgreSQL (enough for the test suite)
	docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d db

migrate:          ## apply Alembic migrations to the Compose database
	docker compose --env-file .env -f infra/trackb/docker-compose.yml run --rm migrate alembic -c backend/aorts/alembic.ini upgrade head

seed:             ## (re)insert synthetic identities/accounts (idempotent)
	docker compose --env-file .env -f infra/trackb/docker-compose.yml run --rm migrate python -m backend.aorts.app.seed

test:             ## full automated test suite (needs `make db`)
	AORTS_REQUIRE_DB=1 $(PY) -m pytest tests/trackb

test-security:    ## security-focused tests only
	AORTS_REQUIRE_DB=1 $(PY) -m pytest tests/trackb/acceptance tests/trackb/test_rate_limits.py tests/trackb/test_replay_protection.py tests/trackb/test_telemetry_integrity.py tests/trackb/test_ledger_invariants.py tests/trackb/security

demo:             ## end-to-end demo against the running stack
	$(PY) -m backend.aorts.experiments.run_end_to_end --mode http

experiments:      ## reproducible experiment scripts -> results/
	$(PY) -m backend.aorts.experiments.run_replay --mode inprocess
	$(PY) -m backend.aorts.experiments.run_poisoning
	$(PY) -m backend.aorts.experiments.run_flooding --mode inprocess
	$(PY) -m backend.aorts.experiments.compare_results

dashboard:        ## open the dashboard (runs in Docker with `make up`)
	@echo "Dashboard: http://localhost:8501"

clean:            ## stop stack AND delete the local database volume (asks first)
	@read -p "Delete ALL local synthetic data (docker volume)? [y/N] " a; [ "$$a" = "y" ] && docker compose --env-file .env -f infra/trackb/docker-compose.yml down -v || echo aborted
