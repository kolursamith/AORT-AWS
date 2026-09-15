"""The signal catalog must stay inside the contract's vocabulary."""

from __future__ import annotations

import re

from aort_normalizer.catalog import CATALOG

BOUNDED_TO_UNIT_INTERVAL = {"ratio", "boolean"}
NON_NEGATIVE_UNITS = {
    "boolean",
    "requests_per_second",
    "operations_per_second",
    "transactions_per_second",
    "ratio",
    "seconds",
    "bytes",
    "cores",
    "count",
    "load_average",
}


def keys():
    return [(s.component_id, s.signal) for s in CATALOG]


def test_catalog_is_substantial():
    assert len(CATALOG) >= 15


def test_catalog_uses_only_contract_vocabulary(schema):
    props = schema["properties"]
    components = set(props["component_id"]["enum"])
    categories = set(props["category"]["enum"])
    units = set(props["unit"]["enum"])
    for spec in CATALOG:
        assert spec.component_id in components, spec
        assert spec.category in categories, spec
        assert spec.unit in units, spec


def test_signal_names_match_contract_pattern(schema):
    pattern = re.compile(schema["properties"]["signal"]["pattern"])
    limit = schema["properties"]["signal"]["maxLength"]
    for spec in CATALOG:
        assert pattern.fullmatch(spec.signal), spec.signal
        assert len(spec.signal) <= limit


def test_component_signal_pairs_are_unique():
    assert len(keys()) == len(set(keys()))


def test_every_contract_component_is_observed(schema):
    observed = {s.component_id for s in CATALOG}
    assert observed == set(schema["properties"]["component_id"]["enum"])


def test_all_four_blueprint_data_categories_are_covered(schema):
    covered = {s.category for s in CATALOG}
    assert covered == set(schema["properties"]["category"]["enum"])


def test_scraped_banking_components_have_a_liveness_signal():
    for component in ("fineract", "postgres"):
        assert (component, "up") in keys()


def test_queries_are_non_empty_and_bracket_balanced():
    pairs = {")": "(", "]": "[", "}": "{"}
    for spec in CATALOG:
        assert spec.promql.strip(), spec
        stack = []
        for ch in spec.promql:
            if ch in "([{":
                stack.append(ch)
            elif ch in pairs:
                assert stack and stack.pop() == pairs[ch], spec.promql
        assert not stack, spec.promql


def test_group_by_labels_appear_in_the_query_by_clause():
    for spec in CATALOG:
        if not spec.group_by:
            continue
        match = re.search(r"\bby\s*\(([^)]*)\)", spec.promql)
        assert match, spec.promql
        labels = {label.strip() for label in match.group(1).split(",")}
        assert set(spec.group_by) <= labels, spec


def test_bounds_are_ordered():
    for spec in CATALOG:
        low, high = spec.bounds
        if low is not None and high is not None:
            assert low <= high, spec


def test_ratios_and_booleans_are_bounded_to_the_unit_interval():
    for spec in CATALOG:
        if spec.unit in BOUNDED_TO_UNIT_INTERVAL:
            assert spec.bounds == (0.0, 1.0), spec


def test_physical_quantities_cannot_be_negative():
    for spec in CATALOG:
        if spec.unit in NON_NEGATIVE_UNITS:
            assert spec.bounds[0] == 0.0, spec


def test_sources_identify_the_prometheus_job():
    for spec in CATALOG:
        assert spec.source.startswith("prometheus:job="), spec
