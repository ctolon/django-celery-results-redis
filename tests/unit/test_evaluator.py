from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings
from django.utils import timezone as django_timezone

from django_celery_results_redis.store.query import evaluator
from django_celery_results_redis.store.query.ast import (
    MATCH_ALL,
    MATCH_NONE,
    And,
    Condition,
    Not,
    Or,
    Xor,
)
from django_celery_results_redis.store.query.evaluator import AggregateSpec

UTC = timezone.utc
RECORD = {
    "status": "Success",
    "worker": None,
    "date_done": datetime(2026, 1, 1, 22, 30, tzinfo=UTC),
}


def c(field, lookup, value, transform=None):
    return Condition(field, transform, lookup, value)


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (c("status", "exact", "Success"), True),
        (c("status", "exact", "success"), False),
        (c("status", "iexact", "SUCCESS"), True),
        (c("status", "contains", "cc"), True),
        (c("status", "contains", "CC"), False),
        (c("status", "icontains", "CC"), True),
        (c("status", "startswith", "Su"), True),
        (c("status", "istartswith", "su"), True),
        (c("status", "endswith", "ss"), True),
        (c("status", "iendswith", "SS"), True),
        (c("status", "gt", "S"), True),
        (c("status", "lt", "S"), False),
        (c("status", "in", frozenset({"Success", "X"})), True),
        (c("status", "range", ("A", "T")), True),
        (c("status", "isnull", False), True),
        (c("worker", "isnull", True), True),
        (c("worker", "exact", "w"), False),
        (c("worker", "gt", ""), False),
        (Not(c("worker", "exact", "w")), True),
        (c("date_done", "exact", 2026, "year"), True),
        (c("date_done", "exact", 22, "day"), False),
        (c("date_done", "exact", 2, "day"), False),
        (c("date_done", "exact", date(2026, 1, 1), "date"), True),
        (MATCH_ALL, True),
        (MATCH_NONE, False),
        (Xor((c("status", "isnull", False), c("worker", "isnull", True))), False),
        (Xor((c("status", "isnull", False), MATCH_NONE, MATCH_NONE)), True),
        (Or((MATCH_NONE, c("worker", "isnull", True))), True),
        (And((MATCH_ALL, c("worker", "isnull", False))), False),
    ],
)
def test_matches(condition, expected):
    assert evaluator.matches(condition, RECORD) is expected


def test_casefold_is_used_for_case_insensitive_lookups():
    assert evaluator.matches(c("status", "iexact", "STRASSE"), {"status": "straße"})


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (c("status", "regex", "^Suc"), True),
        (c("status", "regex", "suc"), False),
        (c("status", "regex", "ucce"), True),
        (c("status", "regex", "^success$"), False),
        (c("status", "iregex", "^success$"), True),
        (c("status", "iregex", "S.*S"), True),
        (c("status", "regex", "x"), False),
        (c("worker", "regex", "."), False),
    ],
)
def test_regex_lookups(condition, expected):
    assert evaluator.matches(condition, RECORD) is expected


MOMENT = datetime(2026, 1, 1, 22, 30, 45, 123456, tzinfo=UTC)


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("year", 2026),
        ("iso_year", 2026),
        ("quarter", 1),
        ("month", 1),
        ("week", 1),
        ("week_day", 5),
        ("iso_week_day", 4),
        ("day", 1),
        ("hour", 22),
        ("minute", 30),
        ("second", 45),
        ("date", date(2026, 1, 1)),
        ("time", time(22, 30, 45, 123456)),
    ],
)
def test_apply_transform(transform, expected):
    assert evaluator.apply_transform(MOMENT, transform) == expected


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("year", 2026),
        ("iso_year", 2026),
        ("month", 1),
        ("week", 1),
        ("week_day", 6),
        ("iso_week_day", 5),
        ("day", 2),
        ("hour", 7),
        ("date", date(2026, 1, 2)),
        ("time", time(7, 30, 45, 123456)),
    ],
)
def test_transforms_use_the_current_time_zone(transform, expected):
    with django_timezone.override(ZoneInfo("Asia/Tokyo")):
        assert evaluator.apply_transform(MOMENT, transform) == expected


@pytest.mark.parametrize(
    ("transform", "expected"),
    [("year", 2025), ("iso_year", 2026), ("week", 1), ("iso_week_day", 1)],
)
def test_iso_transforms_at_a_year_boundary(transform, expected):
    value = datetime(2025, 12, 29, 3, tzinfo=UTC)
    assert evaluator.apply_transform(value, transform) == expected


@override_settings(USE_TZ=False, TIME_ZONE="Asia/Tokyo")
@pytest.mark.parametrize(
    ("transform", "expected"),
    [("day", 1), ("hour", 22), ("week_day", 5), ("date", date(2026, 1, 1))],
)
def test_transforms_on_naive_datetimes(transform, expected):
    naive = MOMENT.replace(tzinfo=None)
    assert evaluator.apply_transform(naive, transform) == expected


def test_order_records_puts_none_last_ascending_and_first_descending():
    records = [{"k": "b", "n": 1}, {"k": None, "n": 2}, {"k": "a", "n": 3}]
    ascending = evaluator.order_records(list(records), [("k", False)])
    assert [r["n"] for r in ascending] == [3, 1, 2]
    descending = evaluator.order_records(list(records), [("k", True)])
    assert [r["n"] for r in descending] == [2, 1, 3]


def test_order_records_with_several_keys():
    records = [{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 0, "b": 3}]
    ordered = evaluator.order_records(list(records), [("a", True), ("b", False)])
    assert ordered == [{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 0, "b": 3}]


def test_aggregate():
    records = [{"k": "a", "n": 1}, {"k": "a", "n": None}, {"k": "b", "n": 3}]
    specs = {
        "all": AggregateSpec("Count", None, False, None, None),
        "n": AggregateSpec("Count", "n", False, None, None),
        "distinct": AggregateSpec("Count", "k", True, None, None),
        "filtered": AggregateSpec("Count", None, False, c("k", "exact", "a"), None),
        "min": AggregateSpec("Min", "n", False, None, None),
        "max": AggregateSpec("Max", "n", False, None, None),
        "sum": AggregateSpec("Sum", "n", False, None, None),
        "avg": AggregateSpec("Avg", "n", False, None, None),
        "empty": AggregateSpec("Max", "n", False, c("k", "exact", "z"), 42),
    }
    assert evaluator.aggregate(records, specs) == {
        "all": 3,
        "n": 2,
        "distinct": 2,
        "filtered": 2,
        "min": 1,
        "max": 3,
        "sum": 4,
        "avg": 2,
        "empty": 42,
    }


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("year", datetime(2026, 1, 1)),
        ("quarter", datetime(2026, 4, 1)),
        ("month", datetime(2026, 5, 1)),
        ("week", datetime(2026, 5, 11)),
        ("day", datetime(2026, 5, 14)),
        ("hour", datetime(2026, 5, 14, 2)),
        ("minute", datetime(2026, 5, 14, 2, 30)),
        ("second", datetime(2026, 5, 14, 2, 30, 45)),
    ],
)
def test_truncate_in_the_current_time_zone(kind, expected):
    tz = ZoneInfo("Europe/Istanbul")
    value = datetime(2026, 5, 13, 23, 30, 45, 123, tzinfo=UTC)
    with django_timezone.override(tz):
        truncated = evaluator.truncate(value, kind)
    assert truncated == expected.replace(tzinfo=tz)
    assert truncated.tzinfo == tz


def test_truncate_with_explicit_tzinfo_and_to_date():
    value = datetime(2026, 5, 13, 23, 30, tzinfo=UTC)
    assert evaluator.truncate(value, "day", ZoneInfo("Asia/Tokyo")) == datetime(
        2026, 5, 14, tzinfo=ZoneInfo("Asia/Tokyo")
    )
    with django_timezone.override(ZoneInfo("Asia/Tokyo")):
        assert evaluator.truncate_to_date(value, "month") == date(2026, 5, 1)


@override_settings(USE_TZ=False)
def test_truncate_naive():
    assert evaluator.truncate(datetime(2026, 5, 13, 23, 30), "month") == datetime(
        2026, 5, 1
    )
