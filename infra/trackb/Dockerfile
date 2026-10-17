# syntax=docker/dockerfile:1
# AORTS Track B services (SIMULATION ONLY). One image, several entry points.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
ARG REQUIREMENTS=requirements.txt
COPY infra/trackb/requirements.txt infra/trackb/requirements-dashboard.txt ./
# Cache mount keeps already-downloaded wheels if a slow network makes a build attempt fail.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r ${REQUIREMENTS}

COPY backend/__init__.py ./backend/__init__.py
COPY backend/aorts ./backend/aorts
COPY infra/trackb/scripts ./infra/trackb/scripts
# Repository-root layout: modules are imported as backend.aorts.*
ENV PYTHONPATH=/app

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 aorts && mkdir -p /app/results && chown -R aorts /app/results
USER aorts

EXPOSE 8100 8110 8121 8122 8130 8501
CMD ["uvicorn", "--factory", "backend.aorts.banking.switch.main:build", "--host", "0.0.0.0", "--port", "8110"]
