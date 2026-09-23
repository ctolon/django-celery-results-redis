import asyncio
import pickle

import django
import pytest
from django.db import IntegrityError, NotSupportedError
from django.db.models import Avg, Count, F, Max, Min, Q, QuerySet, StdDev, Sum
from django.db.models.signals import post_delete, pre_delete

from django_celery_results_redis.models import TaskResult
from django_celery_results_redis.store.queryset import (
    UNSUPPORTED_METHODS,
    RedisQuerySet,
)

from .conftest import BASE_TIME

INHERITED = {
    "all",
    "as_manager",
    "complex_filter",
    "contains",
    "create",
    "db",
    "distinct",
    "earliest",
    "exclude",
    "fetch_mode",
    "filter",
    "first",
    "get",
    "last",
    "latest",
    "none",
    "order_by",
    "ordered",
    "query",
    "reverse",
    "totally_ordered",
    "using",
}
ASYNC_WRAPPERS = {
    "aaggregate",
    "abulk_create",
    "abulk_update",
    "acontains",
    "acount",
    "acreate",
    "adelete",
    "aearliest",
    "aexists",
    "aexplain",
    "afirst",
    "aget",
    "aget_or_create",
    "ain_bulk",
    "aiterator",
    "alast",
    "alatest",
    "aupdate",
    "aupdate_or_create",
}


def pks(queryset):
    return [obj.pk for obj in queryset]


def test_every_public_queryset_method_is_accounted_for():
    public = {name for name in dir(QuerySet) if not name.startswith("_")}
    own = set(RedisQuerySet.__dict__)
    unknown = public - own - INHERITED - ASYNC_WRAPPERS
    assert not unknown, f"Vet these QuerySet methods for Redis support: {unknown}"
    assert set(UNSUPPORTED_METHODS) <= own


@pytest.mark.parametrize("name", UNSUPPORTED_METHODS)
def test_unsupported_methods_raise(store_driver, name):
    with pytest.raises(NotSupportedError, match=name):
        getattr(TaskResult.objects.all(), name)()


def test_is_a_django_queryset(tasks):
    assert isinstance(tasks, QuerySet)
    assert isinstance(TaskResult.objects.get_queryset(), RedisQuerySet)


def test_default_ordering_and_reverse(tasks):
    assert pks(tasks) == ["t4", "t3", "t2", "t1", "t0"]
    assert pks(tasks.reverse()) == ["t0", "t1", "t2", "t3", "t4"]
    assert pks(tasks.order_by("pk")) == ["t0", "t1", "t2", "t3", "t4"]
    assert pks(tasks.order_by(F("status").desc(), "-pk")) == [
        "t3",
        "t1",
        "t4",
        "t2",
        "t0",
    ]
    assert tasks.ordered
    assert pks(tasks.order_by()) == ["t0", "t1", "t2", "t3", "t4"]


@pytest.mark.skipif(django.VERSION < (6, 1), reason="QuerySet.totally_ordered")
def test_totally_ordered(tasks):
    assert tasks.order_by("-pk").totally_ordered
    assert not tasks.order_by("status").totally_ordered


def test_order_by_validation(tasks):
    with pytest.raises(Exception, match="Cannot resolve keyword"):
        tasks.order_by("nope")
    with pytest.raises(NotSupportedError):
        tasks.order_by("?")
    with pytest.raises(NotSupportedError):
        tasks.order_by(F("status").asc(nulls_last=True))


def test_slicing(tasks):
    assert pks(tasks[1:3]) == ["t3", "t2"]
    assert pks(tasks[1:][1:2]) == ["t2"]
    assert pks(tasks[::2]) == ["t4", "t2", "t0"]
    assert tasks[0].pk == "t4"
    assert pks(tasks[3:3]) == []
    with pytest.raises(IndexError):
        tasks.all()[10]
    with pytest.raises(ValueError, match="Negative"):
        tasks[-1]
    with pytest.raises(TypeError):
        tasks[1:3].filter(status="SUCCESS")
    with pytest.raises(TypeError):
        tasks[1:3].order_by("pk")
    assert tasks[1:3].count() == 2
    assert tasks[4:10].count() == 1
    assert tasks[4:10].exists()
    assert not tasks[5:].exists()


def test_get(tasks):
    assert tasks.get(pk="t1").worker == "w1"
    assert tasks.get(task_id="t1", status="SUCCESS").pk == "t1"
    with pytest.raises(TaskResult.DoesNotExist):
        tasks.get(pk="missing")
    with pytest.raises(TaskResult.MultipleObjectsReturned, match="returned 2"):
        tasks.get(status="SUCCESS")


def test_first_last_earliest_latest(tasks):
    assert tasks.first().pk == "t4"
    assert tasks.last().pk == "t0"
    assert tasks.filter(pk="missing").first() is None
    assert tasks.earliest("date_done").pk == "t0"
    assert tasks.latest("date_done").pk == "t4"
    assert tasks.order_by("pk").first().pk == "t0"


def test_filter_exclude_and_none(tasks):
    assert pks(tasks.filter(status="SUCCESS")) == ["t3", "t1"]
    assert pks(tasks.exclude(worker="w1")) == ["t4", "t2", "t0"]
    assert pks(tasks.filter(Q(worker__isnull=True) | Q(pk="t4"))) == ["t4", "t0"]
    assert pks(tasks.complex_filter({"status": "FAILURE"})) == ["t4", "t2", "t0"]
    assert pks(tasks.filter(date_done__gt=BASE_TIME).filter(date_done__month=3)) == []
    empty = tasks.none()
    assert pks(empty) == []
    assert empty.count() == 0
    assert not empty.exists()
    assert empty.aggregate(n=Count("pk")) == {"n": 0}
    assert pks(tasks.filter(status__in=[])) == []
    assert tasks.filter(status__in=[]).query.is_empty()


def test_values_and_values_list(tasks):
    assert list(tasks.filter(pk="t1").values("pk", "status")) == [
        {"pk": "t1", "status": "SUCCESS"}
    ]
    row = tasks.filter(pk="t1").values().get()
    assert row["task_id"] == "t1"
    assert row["date_done"] == tasks.get(pk="t1").date_done
    assert list(tasks.values_list("pk", flat=True)[:2]) == ["t4", "t3"]
    assert list(tasks.filter(pk="t1").values_list("pk", "worker")) == [("t1", "w1")]
    named = tasks.filter(pk="t1").values_list("pk", "worker", named=True).get()
    assert (named.pk, named.worker) == ("t1", "w1")
    assert list(
        tasks.order_by("worker").values_list("worker", flat=True).distinct()
    ) == [
        "w0",
        "w1",
        None,
    ]
    assert list(
        tasks.order_by("-status").values_list("status", flat=True).distinct()[:1]
    ) == ["SUCCESS"]
    assert tasks.values_list("status", flat=True).distinct().count() == 2
    with pytest.raises(TypeError):
        tasks.values_list("pk", "status", flat=True)
    with pytest.raises(TypeError):
        tasks.values_list("pk", flat=True, named=True)
    with pytest.raises(NotSupportedError):
        tasks.values(upper=F("status"))


def test_only_defer_and_refresh(tasks):
    obj = tasks.only("status").get(pk="t1")
    assert obj.get_deferred_fields() >= {"worker", "task_name"}
    assert obj.worker == "w1"
    obj = tasks.defer("worker").get(pk="t1")
    assert obj.get_deferred_fields() == {"worker"}
    assert obj.status == "SUCCESS"
    obj.status = "CHANGED"
    obj.worker = "local"
    TaskResult.objects.filter(pk="t1").update(worker="stored")
    obj.refresh_from_db(fields=["worker"])
    assert (obj.status, obj.worker) == ("CHANGED", "stored")
    obj.refresh_from_db()
    assert obj.status == "SUCCESS"
    with pytest.raises(TypeError):
        tasks.values("pk").only("status")


def test_in_bulk(tasks):
    assert set(tasks.in_bulk(["t1", "t2", "missing"])) == {"t1", "t2"}
    assert tasks.in_bulk([]) == {}
    assert len(tasks.in_bulk()) == 5
    with pytest.raises(ValueError, match="unique"):
        tasks.in_bulk(["x"], field_name="status")


def test_get_or_create_and_update_or_create(tasks):
    obj, created = TaskResult.objects.get_or_create(
        task_id="new", defaults={"status": "PENDING"}
    )
    assert created
    assert obj.status == "PENDING"
    obj, created = TaskResult.objects.get_or_create(task_id="new")
    assert not created
    obj, created = TaskResult.objects.update_or_create(
        task_id="new", defaults={"status": "SUCCESS"}
    )
    assert not created
    stored = TaskResult.objects.get(pk="new")
    assert stored.status == "SUCCESS"
    assert stored.date_done > BASE_TIME
    obj, created = TaskResult.objects.update_or_create(
        task_id="other", defaults={"status": "A"}, create_defaults={"status": "B"}
    )
    assert created
    assert obj.status == "B"


def test_get_or_create_handles_a_concurrent_create(tasks, monkeypatch):
    real_get = RedisQuerySet.get
    calls = []

    def racing_get(self, *args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            TaskResult.objects.create(task_id="race", status="OTHER")
            raise TaskResult.DoesNotExist
        return real_get(self, *args, **kwargs)

    monkeypatch.setattr(RedisQuerySet, "get", racing_get)
    obj, created = TaskResult.objects.get_or_create(
        task_id="race", defaults={"status": "MINE"}
    )
    assert not created
    assert obj.status == "OTHER"


def test_create_duplicate_raises_integrity_error(tasks):
    with pytest.raises(IntegrityError):
        TaskResult.objects.create(task_id="t1")


def test_update(tasks):
    before = tasks.get(pk="t2").date_done
    assert (
        tasks.filter(status="FAILURE").update(
            worker="x", date_started="2026-01-01T00:00Z"
        )
        == 3
    )
    obj = tasks.get(pk="t2")
    assert obj.worker == "x"
    assert obj.date_done == before
    assert obj.date_started.year == 2026
    assert tasks.filter(pk="missing").update(worker="x") == 0
    assert tasks.none().update(worker="x") == 0
    assert tasks.filter(pk="t2").update(worker=None) == 1
    assert tasks.get(pk="t2").worker is None
    with pytest.raises(IntegrityError):
        tasks.update(status=None)
    with pytest.raises(NotSupportedError):
        tasks.update(task_id="x")
    with pytest.raises(TypeError):
        tasks[:1].update(worker="x")


def test_delete(tasks):
    assert tasks.filter(status="SUCCESS").delete() == (
        2,
        {"django_celery_results_redis.TaskResult": 2},
    )
    assert tasks.filter(pk="missing").delete() == (0, {})
    assert tasks.none().delete() == (0, {})
    assert tasks.count() == 3
    with pytest.raises(TypeError):
        tasks[:1].delete()
    with pytest.raises(TypeError):
        tasks.values("pk").delete()


def test_delete_sends_signals_per_instance(tasks):
    received = []

    def receiver(signal, instance, **kwargs):
        received.append((signal, instance.pk))

    pre_delete.connect(receiver, sender=TaskResult)
    post_delete.connect(receiver, sender=TaskResult)
    try:
        assert tasks.filter(status="SUCCESS").delete()[0] == 2
    finally:
        pre_delete.disconnect(receiver, sender=TaskResult)
        post_delete.disconnect(receiver, sender=TaskResult)
    assert sorted(received, key=lambda item: (item[1], item[0] is post_delete)) == [
        (pre_delete, "t1"),
        (post_delete, "t1"),
        (pre_delete, "t3"),
        (post_delete, "t3"),
    ]
    assert tasks.count() == 3


def test_aggregate(tasks):
    assert tasks.aggregate(Count("pk"), Min("date_done")) == {
        "pk__count": 5,
        "date_done__min": BASE_TIME,
    }
    assert tasks.aggregate(
        n=Count("*"),
        workers=Count("worker"),
        distinct_workers=Count("worker", distinct=True),
        successes=Count("pk", filter=Q(status="SUCCESS")),
        latest=Max("date_done", filter=Q(status="FAILURE")),
        none=Max("date_started", default="nothing"),
    ) == {
        "n": 5,
        "workers": 4,
        "distinct_workers": 2,
        "successes": 2,
        "latest": tasks.get(pk="t4").date_done,
        "none": "nothing",
    }
    assert tasks[:2].aggregate(n=Count("pk")) == {"n": 2}
    with pytest.raises(NotSupportedError):
        tasks.aggregate(StdDev("date_done"))
    with pytest.raises(TypeError):
        tasks.aggregate(Count(F("pk") + 1))


def test_sum_and_avg(store_driver):
    from django_celery_results_redis.models import ChordCounter

    ChordCounter.objects.create(group_id="a", sub_tasks="[]", count=2)
    ChordCounter.objects.create(group_id="b", sub_tasks="[]", count=4)
    assert ChordCounter.objects.aggregate(Sum("count"), Avg("count")) == {
        "count__sum": 6,
        "count__avg": 3,
    }


def test_datetimes_and_dates(tasks):
    assert tasks.datetimes("date_done", "day") == [BASE_TIME.replace(hour=0)]
    assert tasks.datetimes("date_done", "hour", order="DESC")[0].hour == 16
    assert tasks.dates("date_done", "month") == [BASE_TIME.date().replace(day=1)]
    assert tasks[:1].datetimes("date_done", "hour") == [BASE_TIME.replace(hour=16)]
    assert tasks.none().dates("date_done", "day") == []
    with pytest.raises(ValueError, match="kind"):
        tasks.datetimes("date_done", "decade")
    with pytest.raises(ValueError, match="kind"):
        tasks.dates("date_done", "hour")
    with pytest.raises(ValueError, match="order"):
        tasks.dates("date_done", "day", order="UP")
    with pytest.raises(Exception, match="DateTimeField"):
        tasks.datetimes("status", "day")


def test_iteration_helpers(tasks):
    assert [obj.pk for obj in tasks.iterator()] == pks(tasks)
    assert tasks.contains(tasks.get(pk="t1"))
    assert "RedisQuerySet" in repr(tasks)
    assert str(tasks.query).startswith("RedisQuery(")
    assert len(tasks) == 5
    assert bool(tasks)
    restored = pickle.loads(pickle.dumps(tasks.filter(status="SUCCESS")))
    assert pks(restored) == ["t3", "t1"]


def test_select_related(tasks):
    assert pks(tasks.select_related()) == pks(tasks)
    with pytest.raises(Exception, match="Invalid field name"):
        tasks.select_related("worker")


def test_async_api(tasks):
    async def run():
        count = await tasks.acount()
        obj = await tasks.aget(pk="t1")
        listed = [o.pk async for o in tasks.filter(status="SUCCESS")]
        iterated = [o.pk async for o in tasks.filter(status="SUCCESS").aiterator()]
        created = await TaskResult.objects.acreate(task_id="async")
        return count, obj.pk, listed, iterated, created.pk

    assert asyncio.run(run()) == (5, "t1", ["t3", "t1"], ["t3", "t1"], "async")


def test_resolve_expression_is_rejected(tasks):
    from django_celery_results.models import TaskResult as DatabaseTaskResult

    with pytest.raises(NotSupportedError):
        DatabaseTaskResult.objects.filter(task_id__in=tasks.values("pk"))
