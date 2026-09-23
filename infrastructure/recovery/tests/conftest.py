"""Fixtures for the recovery-measurement tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contracts" / "i6-recovery-outcome.provisional-0.1.schema.json"


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run a real failure-and-recovery experiment against the stack",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="runs a real experiment; use --live")
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


class FakeClock:
    """Advances one second per read unless told otherwise."""

    def __init__(self, start=1000.0, step=1.0):
        self.value = start
        self.step = step

    def __call__(self):
        current = self.value
        self.value += self.step
        return current


class ScriptedProbe:
    """Returns a scripted sequence of health results, then repeats the last."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.sequence:
            ok = self.sequence.pop(0)
        else:
            ok = True
        return ok, "scripted"


class FakeLedger:
    """Banking row counts and ledger totals, scripted per call."""

    def __init__(self, counts=None, debits=1500.0, credits=1500.0):
        self.counts = list(counts or [{"m_client": 10}, {"m_client": 10}])
        self.debits = debits
        self.credits = credits
        self.calls = 0

    def row_counts(self):
        self.calls += 1
        return self.counts.pop(0) if self.counts else {"m_client": 10}

    def ledger_totals(self):
        return self.debits, self.credits


class FakeDocker:
    def __init__(self, results=None, default=(0, "", ""), state="running"):
        self.results = list(results or [])
        self.default = default
        self.state = state
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.results:
            return self.results.pop(0)
        if argv[:3] == ["docker", "inspect", "--format"]:
            return 0, self.state, ""
        return self.default

    @property
    def commands(self):
        return [call[1] for call in self.calls]


@pytest.fixture
def fakes():
    return {"clock": FakeClock, "probe": ScriptedProbe, "ledger": FakeLedger, "docker": FakeDocker}
