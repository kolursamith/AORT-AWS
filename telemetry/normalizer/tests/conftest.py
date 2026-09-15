"""Shared fixtures for the Layer 1a normalizer tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# telemetry/normalizer/tests/conftest.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contracts" / "i1-observation.provisional-0.1.schema.json"


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests against the running banking and telemetry stacks",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs the running stacks; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def validator(schema):
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class FakeClient:
    """Stands in for PrometheusClient, returning canned samples per PromQL."""

    def __init__(self, responses=None, default=None, error=None):
        self.responses = responses or {}
        self.default = default
        self.error = error
        self.calls: list[tuple[str, float]] = []

    def query(self, promql, at):
        self.calls.append((promql, at))
        if self.error is not None:
            raise self.error
        if promql in self.responses:
            return self.responses[promql]
        if self.default is not None:
            return self.default(promql)
        return []


@pytest.fixture
def fake_client_cls():
    return FakeClient
