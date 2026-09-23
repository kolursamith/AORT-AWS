"""Shared fixtures for the ledger-backup tests."""

from __future__ import annotations

import pytest

REPO_ROOT_MARKER = "banking"


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests against the running banking stack (real pg_dump/pg_restore)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs the running banking stack; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


class FakeRunner:
    """Stands in for subprocess execution of container commands."""

    def __init__(self, results=None, default=(0, "", "")):
        self.results = list(results or [])
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.results:
            return self.results.pop(0)
        return self.default


@pytest.fixture
def fake_runner_cls():
    return FakeRunner
