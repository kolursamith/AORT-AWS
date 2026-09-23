"""Dump file naming, ordering and retention."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aort_backup.dumps import dump_name, newest_dump, parse_dump_name, prune_candidates

T1 = "fineract_default-20260923T101500Z.dump"
T2 = "fineract_default-20260923T101600Z.dump"
T3 = "fineract_default-20260923T101700Z.dump"
OTHER_DB = "fineract_tenants-20260923T101800Z.dump"


def test_round_trip_name_and_parse():
    moment = datetime(2026, 9, 23, 10, 15, 0, tzinfo=timezone.utc)
    assert dump_name("fineract_default", moment) == T1
    assert parse_dump_name(T1) == ("fineract_default", moment)


def test_parsed_time_is_timezone_aware_utc():
    _, moment = parse_dump_name(T1)
    assert moment.tzinfo is timezone.utc


@pytest.mark.parametrize("name", [
    "fineract_default.dump",
    "fineract_default-20260923.dump",
    "fineract_default-20260923T101500Z.sql",
    "20260923T101500Z.dump",
    "fineract_default-20261323T101500Z.dump",   # month 13
    "fineract_default-20260923T101500Z.dump.part",
    "",
])
def test_unparseable_names_raise(name):
    with pytest.raises(ValueError):
        parse_dump_name(name)


def test_newest_dump_picks_the_latest_timestamp_not_list_order():
    assert newest_dump([T2, T1, T3]) == T3


def test_newest_dump_can_filter_by_database():
    assert newest_dump([T3, OTHER_DB], database="fineract_default") == T3
    assert newest_dump([T3, OTHER_DB], database="fineract_tenants") == OTHER_DB


def test_newest_dump_ignores_unparseable_entries():
    assert newest_dump([T1, "notes.txt", "partial.dump.part", T3]) == T3


@pytest.mark.parametrize("names", [[], ["notes.txt"]])
def test_newest_dump_without_candidates_is_none(names):
    assert newest_dump(names) is None


def test_newest_dump_returns_none_when_database_has_no_dumps():
    assert newest_dump([T1, T3], database="other_db") is None


# --- retention -----------------------------------------------------------------

def test_prune_keeps_the_newest_n_and_returns_the_rest():
    assert prune_candidates([T1, T2, T3], keep=2) == [T1]


def test_prune_keeps_everything_when_under_the_limit():
    assert prune_candidates([T1, T2], keep=5) == []


def test_prune_never_touches_unparseable_files():
    assert prune_candidates([T1, T2, T3, "notes.txt"], keep=1) == [T1, T2]


def test_prune_with_keep_zero_returns_all_dumps():
    assert sorted(prune_candidates([T1, T2], keep=0)) == sorted([T1, T2])


def test_prune_rejects_negative_keep():
    with pytest.raises(ValueError):
        prune_candidates([T1], keep=-1)
