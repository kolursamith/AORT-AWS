# syntax=docker/dockerfile:1
# AORTS integrated system (Track A controller + Track B services + dashboard). SIMULATION ONLY.
# One image, several entry points; dependencies are the exact pins tested in pyproject.toml.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app
# 1. dependencies only (runtime + dashboard extra), read from pyproject.toml: this layer is
#    reused for every source-only change
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip python - <<'PY'
import subprocess, sys, tomllib
p = tomllib.load(open("pyproject.toml", "rb"))["project"]
reqs = p["dependencies"] + p["optional-dependencies"]["dashboard"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *reqs])
PY
# 2. the sources (imported from /app through PYTHONPATH; nothing else is installed)
COPY README.md LICENSE ./
COPY backend ./backend
COPY contracts ./contracts
COPY intelligence ./intelligence

RUN useradd --create-home --uid 10001 aorts \
    && mkdir -p /app/results /run/aorts \
    && chown -R aorts /app/results /run/aorts
USER aorts

EXPOSE 8100 8110 8121 8122 8130 8140 8501
CMD ["python", "-m", "backend.aorts.integration.worker", "loop"]
