# syntax=docker/dockerfile:1
# AORTS integrated system (Track A controller + Track B services). SIMULATION ONLY.
# One image, several entry points; dependencies are the exact pins tested in pyproject.toml.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY backend ./backend
COPY contracts ./contracts
COPY intelligence ./intelligence
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[dashboard]"

RUN useradd --create-home --uid 10001 aorts \
    && mkdir -p /app/results /run/aorts \
    && chown -R aorts /app/results /run/aorts
USER aorts

EXPOSE 8100 8110 8121 8122 8130 8140 8501
CMD ["python", "-m", "backend.aorts.integration.worker", "loop"]
