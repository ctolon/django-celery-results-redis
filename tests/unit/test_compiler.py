from datetime import date, datetime, time, timezone

import pytest
from django.core.exceptions import FieldError, ValidationError
from django.db import NotSupportedError
from django.db.models import F, Q

from django_celery_results_redis.models import TaskResult
from django_celery_results_redis.store.query.ast import (
    MATCH_ALL,
    MATCH_NONE,
    And,
    Condition,
    Not,
    Or,
    Xor,
)
from django_celery_results_redis.store.query.compiler import compile_filter, compile_q

UTC = timezone.utc


def compile_(*args, **kwargs):
    return compile_filter(TaskResult, args, kwargs)


@pytest.mark.parametrize(
    "lookup",
    [
        "exact",
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "gt",
        "gte",
        "lt",
        "lte",
    ],
)
def test_text_lookups(lookup):
    assert compile_(**{f"status__{lookup}": "A"}) == Condition(
        "status", None, lookup, "A"
    )


def test_implicit_exact_and_pk_alias():
    assert compile_(pk="a") == Condition("task_id", None, "exact", "a")
    assert compile_(task_id="a") == Condition("task_id", None, "exact", "a")


def test_in_and_range():
    assert compile_(status__in=["A", None, "B", "A"]) == Condition(
        "status", None, "in", frozenset({"A", "B"})
    )
    assert compile_(status__in=[]) == MATCH_NONE
    assert compile_(status__in=[None]) == MATCH_NONE
    assert compile_(status__range=("A", "C")) == Condition(
        "status", None, "range", ("A", "C")
    )
    with pytest.raises(TypeError):
        compile_(status__in="AB")


def test_datetime_values_are_prepared():
    condition = compile_(date_done__gte="2026-01-02 03:04:05+00:00")
    assert condition.value == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with pytest.raises(ValidationError):
        compile_(date_done__gte="not a date")


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("date_done__year", "2026", Condition("date_done", "year", "exact", 2026)),
        ("date_done__month__gte", 3, Condition("date_done", "month", "gte", 3)),
        (
            "date_done__day__in",
            [1, "2"],
            Condition("date_done", "day", "in", frozenset({1, 2})),
        ),
        (
            "date_done__date",
            "2026-09-22",
            Condition("date_done", "date", "exact", date(2026, 9, 22)),
        ),
        (
            "date_done__date__lt",
            date(2026, 9, 22),
            Condition("date_done", "date", "lt", date(2026, 9, 22)),
        ),
    ],
)
def test_datetime_transforms(key, value, expected):
    assert compile_(**{key: value}) == expected


def test_bad_transform_values():
    with pytest.raises(ValueError, match="abc"):
        compile_(date_done__year="abc")
    with pytest.raises(ValidationError):
        compile_(date_done__date="2026-13-40")


def test_transforms_only_apply_to_datetime_fields():
    with pytest.raises(FieldError):
        compile_(status__year=2026)


@pytest.mark.parametrize(
    "key",
    [
        "nope",
        "status__year",
        "status__exact__exact",
        "date_done__icontains",
        "date_done__year__icontains",
    ],
)
def test_field_errors(key):
    with pytest.raises(FieldError):
        compile_(**{key: "x"})


def test_regex_lookups():
    assert compile_(status__regex="^S.*S$") == Condition(
        "status", None, "regex", "^S.*S$"
    )
    assert compile_(status__iregex="s") == Condition("status", None, "iregex", "s")
    with pytest.raises(ValueError, match="Invalid regular expression"):
        compile_(status__regex="[")


@pytest.mark.parametrize(
    ("key", "transform"),
    [
        ("date_done__iso_year", "iso_year"),
        ("date_done__quarter", "quarter"),
        ("date_done__week", "week"),
        ("date_done__week_day", "week_day"),
        ("date_done__iso_week_day", "iso_week_day"),
        ("date_done__hour", "hour"),
        ("date_done__minute", "minute"),
        ("date_done__second", "second"),
        ("date_done__date__year", "year"),
        ("date_done__date__week_day", "week_day"),
        ("date_done__time__hour", "hour"),
    ],
)
def test_extended_datetime_transforms(key, transform):
    assert compile_(**{key: 3}) == Condition("date_done", transform, "exact", 3)


def test_time_transform():
    assert compile_(date_done__time="01:02:03") == Condition(
        "date_done", "time", "exact", time(1, 2, 3)
    )


def test_none_rules():
    assert compile_(worker=None) == Condition("worker", None, "isnull", True)
    assert compile_(worker__iexact=None) == Condition("worker", None, "isnull", True)
    with pytest.raises(ValueError, match="None"):
        compile_(worker__gt=None)
    with pytest.raises(ValueError, match="isnull"):
        compile_(worker__isnull="yes")
    with pytest.raises(ValueError, match="isnull"):
        compile_(worker__isnull=1)
    assert compile_(worker__isnull=False) == Condition("worker", None, "isnull", False)


def test_expressions_are_not_supported():
    with pytest.raises(NotSupportedError):
        compile_(status=F("worker"))


def test_connectors_and_negation():
    a = Condition("status", None, "exact", "A")
    b = Condition("worker", None, "exact", "w")
    assert compile_(Q(status="A") & Q(worker="w")) == And((a, b))
    assert compile_(Q(status="A") | Q(worker="w")) == Or((a, b))
    assert compile_(Q(status="A") ^ Q(worker="w")) == Xor((a, b))
    assert compile_(~Q(status="A")) == Not(a)
    assert compile_(~~Q(status="A")) == a


def test_constant_folding():
    a = Condition("status", None, "exact", "A")
    assert compile_(Q(status="A") & Q(status__in=[])) == MATCH_NONE
    assert compile_(Q(status="A") | Q(status__in=[])) == a
    assert compile_(~Q(status__in=[])) == MATCH_ALL
    assert compile_q(TaskResult, Q()) == MATCH_ALL
    assert compile_q(TaskResult, ~Q()) == MATCH_ALL
    assert compile_(Q(status="A") & (Q(worker="w") & Q(task_name="t"))) == And(
        (
            a,
            Condition("worker", None, "exact", "w"),
            Condition("task_name", None, "exact", "t"),
        )
    )


def test_queryset_values_in_lookup(store_driver):
    TaskResult.objects.create(task_id="a", status="A")
    TaskResult.objects.create(task_id="b", status="B")
    node = compile_(pk__in=TaskResult.objects.filter(status="A"))
    assert node == Condition("task_id", None, "in", frozenset({"a"}))
    node = compile_(status__in=TaskResult.objects.values_list("status", flat=True))
    assert node == Condition("status", None, "in", frozenset({"A", "B"}))
