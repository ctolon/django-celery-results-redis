import re
from datetime import datetime, timedelta, timezone
from functools import reduce
from operator import and_, or_, xor

import pytest
from django.db.models import Count, Max, Min, Q
from django.utils import timezone as django_timezone
from django_celery_results.models import TaskResult as DatabaseTaskResult
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from django_celery_results_redis.models import TaskResult
from django_celery_results_redis.store import get_store
from django_celery_results_redis.store.query import evaluator
from django_celery_results_redis.store.query.compiler import compile_q

UTC = timezone.utc
MOMENTS = [
    datetime(2025, 12, 31, 23, 30, tzinfo=UTC),
    datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    datetime(2026, 1, 1, 4, 59, 59, 999999, tzinfo=UTC),
    datetime(2026, 3, 15, 12, tzinfo=UTC),
    datetime(2026, 3, 15, 12, tzinfo=UTC) + timedelta(microseconds=1),
    datetime(2026, 11, 30, 22, tzinfo=UTC),
]
TEXTS = {
    "status": ["SUCCESS", "success", "FAILURE", "PENDING", ""],
    "worker": [None, "w1", "W1", "w2", ""],
    "task_name": [None, "add", "Add x", "a|b", " x"],
    "periodic_task_name": [None, "nightly"],
    "task_args": [None, "[1]", "['x', 'Y']"],
}
TIME_ZONES = ["UTC", "America/New_York", "Asia/Tokyo"]

moments = st.sampled_from(MOMENTS)

records = st.lists(
    st.fixed_dictionaries(
        {
            **{name: st.sampled_from(values) for name, values in TEXTS.items()},
            "date_done": moments,
            "date_created": moments,
            "date_started": st.none() | moments,
        }
    ),
    max_size=10,
)


def text_leaf(field):
    values = [v for v in TEXTS[field] if v is not None]
    value = st.sampled_from(values)
    return st.one_of(
        st.builds(lambda v: Q(**{field: v}), value),
        st.builds(lambda v: Q(**{f"{field}__iexact": v}), value),
        st.builds(lambda v: Q(**{f"{field}__icontains": v[:2]}), value),
        st.builds(lambda v: Q(**{f"{field}__startswith": v[:1]}), value),
        st.builds(lambda v: Q(**{f"{field}__in": v}), st.lists(value, max_size=3)),
        st.builds(lambda v: Q(**{f"{field}__isnull": v}), st.booleans()),
        st.builds(lambda v: Q(**{f"{field}__gt": v}), value),
        st.builds(
            lambda v: Q(**{f"{field}__regex": f"^{re.escape(v[:2])}"}),
            value.filter(bool),
        ),
        st.builds(
            lambda v: Q(**{f"{field}__iregex": re.escape(v[-2:])}), value.filter(bool)
        ),
    )


PART_TRANSFORMS = {
    "iso_year": [2025, 2026],
    "quarter": [1, 2, 4],
    "month": [1, 3, 12],
    "week": [1, 11, 49],
    "week_day": [1, 5, 7],
    "iso_week_day": [1, 4, 7],
    "day": [1, 15, 30],
    "hour": [0, 4, 22],
    "minute": [0, 30, 59],
    "second": [0, 59],
}


def date_leaf(field):
    lookups = ["exact", "gt", "gte", "lt", "lte"]
    return st.one_of(
        st.builds(
            lambda lookup, v: Q(**{f"{field}__{lookup}": v}),
            st.sampled_from(lookups),
            moments,
        ),
        st.builds(
            lambda a, b: Q(**{f"{field}__range": (min(a, b), max(a, b))}),
            moments,
            moments,
        ),
        st.builds(
            lambda lookup, v: Q(**{f"{field}__date__{lookup}": v.date()}),
            st.sampled_from(lookups),
            moments,
        ),
        st.builds(
            lambda lookup, v: Q(**{f"{field}__year__{lookup}": v}),
            st.sampled_from(lookups),
            st.sampled_from([2025, 2026, 2027]),
        ),
        st.builds(
            lambda part, v: Q(**{f"{field}__{part}": v}),
            st.sampled_from(sorted(PART_TRANSFORMS)),
            st.integers(0, 59),
        ),
        st.builds(
            lambda v: Q(**{f"{field}__date__week_day": v}), st.sampled_from([1, 5, 7])
        ),
        st.builds(lambda v: Q(**{f"{field}__time__hour": v}), st.sampled_from([0, 22])),
        st.builds(lambda v: Q(**{f"{field}__time": v.time()}), moments),
        st.builds(lambda v: Q(**{f"{field}__isnull": v}), st.booleans()),
    )


leaves = st.one_of(
    *(text_leaf(field) for field in TEXTS),
    *(date_leaf(field) for field in ("date_done", "date_started")),
    st.builds(lambda v: Q(pk__in=v), st.lists(st.sampled_from(["t0", "t1", "t9"]))),
)
queries = st.recursive(
    leaves,
    lambda children: st.one_of(
        st.builds(
            lambda qs, op: reduce(op, qs),
            st.lists(children, min_size=2, max_size=3),
            st.sampled_from([and_, or_, xor]),
        ),
        st.builds(lambda q: ~q, children),
    ),
    max_leaves=6,
)
orderings = st.one_of(
    st.just(()),
    st.lists(
        st.sampled_from(
            [
                "date_done",
                "-date_done",
                "date_started",
                "-date_started",
                "status",
                "-status",
                "worker",
                "-worker",
                "task_name",
                "-task_name",
                "periodic_task_name",
                "pk",
                "-pk",
            ]
        ),
        max_size=2,
        unique=True,
    ).map(tuple),
)
slices = st.tuples(st.integers(0, 4), st.none() | st.integers(0, 8))
HYPOTHESIS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def load(store, data):
    TaskResult.objects.all().delete()
    for index, values in enumerate(data):
        store.write(f"t{index}", values)
    return store.get_many([f"t{index}" for index in range(len(data))])


def oracle(stored, queryset):
    query = queryset.query
    matching = [r for r in stored if evaluator.matches(query.where, r)]
    return evaluator.order_records(matching, query.get_ordering())


@HYPOTHESIS
@given(
    data=records,
    q=queries,
    ordering=orderings,
    bounds=slices,
    time_zone=st.sampled_from(TIME_ZONES),
)
def test_querysets_match_the_evaluator(
    store_driver, data, q, ordering, bounds, time_zone
):
    store = get_store(TaskResult)
    stored = load(store, data)
    with django_timezone.override(time_zone):
        queryset = TaskResult.objects.filter(q)
        if ordering:
            queryset = queryset.order_by(*ordering)
        expected = oracle(stored, queryset)
        pks = [r["task_id"] for r in expected]
        low, high = bounds

        assert [obj.pk for obj in queryset.all()] == pks
        assert list(queryset[low:high].values_list("pk", flat=True)) == pks[low:high]
        assert queryset.count() == len(pks)
        assert queryset[low:high].count() == len(pks[low:high])
        assert queryset.exists() is bool(pks)
        assert queryset.aggregate(
            n=Count("pk"),
            failures=Count("pk", filter=Q(status="FAILURE")),
            workers=Count("worker"),
            first=Min("date_done"),
            last=Max("date_started"),
        ) == {
            "n": len(expected),
            "failures": sum(r["status"] == "FAILURE" for r in expected),
            "workers": sum(r["worker"] is not None for r in expected),
            "first": min((r["date_done"] for r in expected), default=None),
            "last": max(
                (r["date_started"] for r in expected if r["date_started"]),
                default=None,
            ),
        }
        for field in ("status", "worker", "task_name"):
            values = queryset.distinct().order_by(field).values_list(field, flat=True)
            assert list(values) == sorted(
                {r[field] for r in expected}, key=evaluator.sort_key
            )
        for kind in ("year", "month", "day"):
            assert queryset.datetimes("date_done", kind) == sorted(
                {evaluator.truncate(r["date_done"], kind) for r in expected}
            )


@HYPOTHESIS
@given(data=records, q=queries)
def test_unfiltered_and_negated_queries_partition_records(store_driver, data, q):
    load(get_store(TaskResult), data)
    matching = set(TaskResult.objects.filter(q).values_list("pk", flat=True))
    excluded = set(TaskResult.objects.exclude(q).values_list("pk", flat=True))
    assert not matching & excluded
    assert matching | excluded == {f"t{i}" for i in range(len(data))}


DIFFERENTIAL_ROWS = [
    ("t0", "SUCCESS", "w1", "add", "[1]", MOMENTS[0]),
    ("t1", "success", None, "Add x", None, MOMENTS[1]),
    ("t2", "FAILURE", "w2", None, "['x']", MOMENTS[2]),
    ("t3", "PENDING", "", "mul", "[2, 3]", MOMENTS[3]),
    ("t4", "SUCCESS", "W1", "add", None, MOMENTS[5]),
    ("t5", "FAILURE", None, "", "[]", MOMENTS[3] + timedelta(days=40)),
]
DIFFERENTIAL_QUERIES = [
    Q(status="SUCCESS"),
    Q(status__iexact="success"),
    Q(status__in=["FAILURE", "PENDING"]),
    ~Q(worker="w1"),
    Q(worker__isnull=True),
    Q(worker="") | Q(task_name=""),
    ~Q(worker__isnull=True) & ~Q(status="SUCCESS"),
    Q(task_name__icontains="ADD"),
    Q(task_name__istartswith="a"),
    Q(task_args__contains="x"),
    Q(date_done__gte=MOMENTS[2]),
    Q(date_done__lt=MOMENTS[3]) ^ Q(status="SUCCESS"),
    Q(date_done__date=MOMENTS[1].date()),
    Q(date_done__year=2026),
    Q(date_done__month=3),
    Q(date_done__range=(MOMENTS[1], MOMENTS[3])),
    ~(Q(status="SUCCESS") | Q(worker__in=["w2", "W1"])),
    Q(status__regex="^[SF]"),
    Q(task_name__iregex="add"),
    Q(task_name__regex="^a"),
    Q(date_done__iso_year=2026),
    Q(date_done__quarter=1),
    Q(date_done__week=1),
    Q(date_done__week_day=5),
    Q(date_done__iso_week_day=4),
    Q(date_done__day=15),
    Q(date_done__hour__gte=12),
    Q(date_done__minute=30),
    Q(date_done__second=59),
    Q(date_done__date__year=2026),
    Q(date_done__time__hour=0),
]
DIFFERENTIAL_ORDERINGS = [
    ("-date_done", "-task_id"),
    ("date_done", "task_id"),
    ("status", "task_id"),
    ("-status", "-date_done", "task_id"),
]
NULLABLE_ORDERINGS = [
    ("worker", "task_id"),
    ("-worker", "task_id"),
    ("task_name", "-task_id"),
    ("-task_name", "-date_done", "task_id"),
]


@pytest.fixture
def both_packages(store_driver):
    for task_id, status, worker, task_name, task_args, date_done in DIFFERENTIAL_ROWS:
        values = {
            "status": status,
            "worker": worker,
            "task_name": task_name,
            "task_args": task_args,
        }
        TaskResult.objects.create(task_id=task_id, **values)
        TaskResult.objects.filter(pk=task_id).update(date_done=date_done)
        DatabaseTaskResult.objects.create(task_id=task_id, **values)
        DatabaseTaskResult.objects.filter(task_id=task_id).update(date_done=date_done)


@pytest.mark.django_db
@pytest.mark.usefixtures("both_packages")
@pytest.mark.parametrize("q", DIFFERENTIAL_QUERIES, ids=str)
@pytest.mark.parametrize("ordering", DIFFERENTIAL_ORDERINGS, ids=str)
def test_results_match_the_database_backend(q, ordering):
    ours = TaskResult.objects.filter(q).order_by(*ordering)
    theirs = DatabaseTaskResult.objects.filter(q).order_by(*ordering)
    assert list(ours.values_list("task_id", flat=True)) == list(
        theirs.values_list("task_id", flat=True)
    )
    assert ours.count() == theirs.count()
    excluded = TaskResult.objects.exclude(q).order_by("task_id")
    assert list(excluded.values_list("task_id", flat=True)) == list(
        DatabaseTaskResult.objects.exclude(q)
        .order_by("task_id")
        .values_list("task_id", flat=True)
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("both_packages")
@pytest.mark.parametrize("ordering", NULLABLE_ORDERINGS, ids=str)
def test_ordering_by_a_nullable_field_matches_apart_from_nulls(ordering):
    """NULL ordering is documented to differ, so compare the rows that have a value."""
    field = ordering[0].lstrip("-")
    ours = TaskResult.objects.order_by(*ordering).exclude(**{f"{field}__isnull": True})
    theirs = DatabaseTaskResult.objects.order_by(*ordering).exclude(
        **{f"{field}__isnull": True}
    )
    assert list(ours.values_list("task_id", flat=True)) == list(
        theirs.values_list("task_id", flat=True)
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("both_packages")
@pytest.mark.parametrize("ordering", NULLABLE_ORDERINGS, ids=str)
def test_ordering_by_a_nullable_field_puts_nulls_last_ascending(ordering):
    field = ordering[0].lstrip("-")
    values = list(TaskResult.objects.order_by(*ordering).values_list(field, flat=True))
    nulls = [index for index, value in enumerate(values) if value is None]
    others = [index for index, value in enumerate(values) if value is not None]
    if nulls and others:
        assert (min(nulls) > max(others)) is not ordering[0].startswith("-")


def test_compiled_tree_is_reused_by_the_oracle(store_driver):
    queryset = TaskResult.objects.filter(Q(status="A"))
    assert queryset.query.where == compile_q(TaskResult, Q(status="A"))
