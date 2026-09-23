from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone as django_timezone

from django_celery_results_redis.store.codec import datetime_to_micros
from django_celery_results_redis.store.query.ast import (
    MATCH_ALL,
    And,
    Condition,
    Not,
    Or,
    Xor,
)
from django_celery_results_redis.store.query.planner import (
    ScoreRange,
    conjunction_range,
    is_range_condition,
    score_ranges,
    split,
)

UTC = timezone.utc
PUSH = Condition("status", None, "exact", "A")
PUSH2 = Condition("worker", None, "exact", "w")
KEEP = Condition("task_args", None, "icontains", "x")


def can_push(condition):
    return condition.field != "task_args"


@pytest.mark.parametrize(
    ("node", "pushed", "residual"),
    [
        (PUSH, PUSH, MATCH_ALL),
        (KEEP, None, KEEP),
        (And((PUSH, KEEP, PUSH2)), And((PUSH, PUSH2)), KEEP),
        (And((PUSH, KEEP)), PUSH, KEEP),
        (And((KEEP, KEEP)), None, And((KEEP, KEEP))),
        (Or((PUSH, PUSH2)), Or((PUSH, PUSH2)), MATCH_ALL),
        (Or((PUSH, KEEP)), None, Or((PUSH, KEEP))),
        (Not(PUSH), Not(PUSH), MATCH_ALL),
        (Not(And((PUSH, KEEP))), None, Not(And((PUSH, KEEP)))),
        (Xor((PUSH, PUSH2)), None, Xor((PUSH, PUSH2))),
        (And((Or((PUSH, PUSH2)), Not(KEEP))), Or((PUSH, PUSH2)), Not(KEEP)),
        (MATCH_ALL, None, MATCH_ALL),
    ],
)
def test_split(node, pushed, residual):
    assert split(node, can_push) == (pushed, residual)


def test_split_rejects_unknown_nodes():
    with pytest.raises(TypeError):
        split(object(), can_push)


MOMENT = datetime(2026, 9, 22, 12, tzinfo=UTC)
M = datetime_to_micros(MOMENT)


@pytest.mark.parametrize(
    ("lookup", "value", "expected"),
    [
        ("exact", MOMENT, [ScoreRange(M, M)]),
        ("gt", MOMENT, [ScoreRange(M, None, low_inclusive=False)]),
        ("gte", MOMENT, [ScoreRange(M, None)]),
        ("lt", MOMENT, [ScoreRange(None, M, high_inclusive=False)]),
        ("lte", MOMENT, [ScoreRange(None, M)]),
        ("range", (MOMENT, MOMENT), [ScoreRange(M, M)]),
        ("in", frozenset({MOMENT}), [ScoreRange(M, M)]),
    ],
)
def test_score_ranges_without_transform(lookup, value, expected):
    assert score_ranges(Condition("date_done", None, lookup, value)) == expected


def micros(*args, tz=UTC):
    return datetime_to_micros(datetime(*args, tzinfo=tz))


@pytest.mark.parametrize(
    ("transform", "lookup", "value", "expected"),
    [
        (
            "year",
            "exact",
            2026,
            [ScoreRange(micros(2026, 1, 1), micros(2027, 1, 1), high_inclusive=False)],
        ),
        ("year", "gt", 2026, [ScoreRange(micros(2027, 1, 1), None)]),
        ("year", "gte", 2026, [ScoreRange(micros(2026, 1, 1), None)]),
        (
            "year",
            "lt",
            2026,
            [ScoreRange(None, micros(2026, 1, 1), high_inclusive=False)],
        ),
        (
            "year",
            "lte",
            2026,
            [ScoreRange(None, micros(2027, 1, 1), high_inclusive=False)],
        ),
        (
            "year",
            "range",
            (2025, 2026),
            [ScoreRange(micros(2025, 1, 1), micros(2027, 1, 1), high_inclusive=False)],
        ),
        (
            "date",
            "exact",
            date(2026, 9, 22),
            [
                ScoreRange(
                    micros(2026, 9, 22), micros(2026, 9, 23), high_inclusive=False
                )
            ],
        ),
        ("date", "gt", date(2026, 9, 22), [ScoreRange(micros(2026, 9, 23), None)]),
        (
            "date",
            "lte",
            date(2026, 9, 22),
            [ScoreRange(None, micros(2026, 9, 23), high_inclusive=False)],
        ),
        (
            "date",
            "in",
            frozenset({date(2026, 9, 22), date(2026, 9, 20)}),
            [
                ScoreRange(
                    micros(2026, 9, 20), micros(2026, 9, 21), high_inclusive=False
                ),
                ScoreRange(
                    micros(2026, 9, 22), micros(2026, 9, 23), high_inclusive=False
                ),
            ],
        ),
    ],
)
def test_score_ranges_with_transform(transform, lookup, value, expected):
    assert score_ranges(Condition("date_done", transform, lookup, value)) == expected


def test_score_ranges_use_the_current_time_zone():
    tz = ZoneInfo("Asia/Tokyo")
    with django_timezone.override(tz):
        ranges = score_ranges(
            Condition("date_done", "date", "exact", date(2026, 9, 22))
        )
    assert ranges == [
        ScoreRange(
            micros(2026, 9, 22, tz=tz), micros(2026, 9, 23, tz=tz), high_inclusive=False
        )
    ]


def date_condition(lookup, value, transform=None):
    return Condition("date_done", transform, lookup, value)


def pushes_dates(condition):
    return condition.field == "date_done" and (
        is_range_condition(condition) or condition.lookup == "isnull"
    )


LATER = MOMENT + timedelta(hours=1)
L = datetime_to_micros(LATER)


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        (date_condition("gte", MOMENT), ScoreRange(M, None)),
        (date_condition("lt", MOMENT), ScoreRange(None, M, high_inclusive=False)),
        (date_condition("exact", MOMENT), ScoreRange(M, M)),
        (
            And((date_condition("gte", MOMENT), date_condition("lt", LATER))),
            ScoreRange(M, L, high_inclusive=False),
        ),
        (
            And((date_condition("gt", MOMENT), date_condition("lte", LATER))),
            ScoreRange(M, L, low_inclusive=False),
        ),
        (
            And((date_condition("gte", MOMENT), date_condition("gt", MOMENT))),
            ScoreRange(M, None, low_inclusive=False),
        ),
        (
            And((date_condition("gte", MOMENT), date_condition("gte", LATER))),
            ScoreRange(L, None),
        ),
        (
            And((date_condition("lt", LATER), date_condition("lte", MOMENT))),
            ScoreRange(None, M),
        ),
        (
            date_condition("exact", 2026, "year"),
            ScoreRange(micros(2026, 1, 1), micros(2027, 1, 1), high_inclusive=False),
        ),
        (
            And(
                (
                    date_condition("gte", MOMENT),
                    date_condition("lt", datetime(2027, 1, 1, tzinfo=UTC)),
                )
            ),
            ScoreRange(M, micros(2027, 1, 1), high_inclusive=False),
        ),
        (MATCH_ALL, ScoreRange(None, None)),
    ],
)
def test_conjunction_range(node, expected):
    assert conjunction_range(node, "date_done", pushes_dates) == expected


@pytest.mark.parametrize(
    "node",
    [
        And((date_condition("gte", LATER), date_condition("lt", MOMENT))),
        And((date_condition("gt", LATER), date_condition("lt", MOMENT))),
        date_condition("isnull", True),
        date_condition("in", frozenset({MOMENT, LATER})),
        date_condition("exact", 3, "month"),
        Condition("date_created", None, "gte", MOMENT),
        And((date_condition("gte", MOMENT), Condition("status", None, "exact", "A"))),
        Or((date_condition("gte", MOMENT), date_condition("lt", LATER))),
        Not(date_condition("gte", MOMENT)),
        And((MATCH_ALL,)),
    ],
)
def test_conjunction_range_declines(node):
    assert conjunction_range(node, "date_done", pushes_dates) is None


def test_conjunction_range_respects_can_push():
    node = date_condition("gte", MOMENT)
    assert conjunction_range(node, "date_done", lambda condition: False) is None


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (Condition("date_done", None, "gt", MOMENT), True),
        (Condition("date_done", "year", "exact", 2026), True),
        (Condition("date_done", "year", "exact", 1), False),
        (Condition("date_done", "year", "range", (2000, 9999)), False),
        (Condition("date_done", "date", "exact", date.max), False),
        (Condition("date_done", "month", "exact", 3), False),
        (Condition("date_done", "day", "exact", 3), False),
        (Condition("date_done", None, "isnull", True), False),
    ],
)
def test_is_range_condition(condition, expected):
    assert is_range_condition(condition) is expected
