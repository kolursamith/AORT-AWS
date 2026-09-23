"""Shared fixtures for the failure-injection tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contracts" / "i4-injection-event.provisional-0.1.schema.json"


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="inject real failures into the running stack",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="injects into a real stack; run with --live")
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


class FakeDocker:
    """Records docker commands and returns canned results."""

    def __init__(self, results=None, default=(0, "", ""), state=None):
        self.results = list(results or [])
        self.default = default
        self.state = state or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.results:
            return self.results.pop(0)
        if argv[:3] == ["docker", "inspect", "--format"]:
            return 0, self.state.get(argv[-1], "running"), ""
        return self.default

    @property
    def commands(self):
        """The docker sub-command of each call, e.g. ['stop', 'start']."""
        return [call[1] for call in self.calls]


@pytest.fixture
def fake_docker_cls():
    return FakeDocker
