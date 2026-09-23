import pytest
from django.db import IntegrityError
from django.db.models.functions import Now
from django.db.models.signals import post_save, pre_save

from django_celery_results_redis.models import GroupResult, TaskResult
from django_celery_results_redis.store import get_store

FIELDS = {
    "status": "SUCCESS",
    "result": "42",
    "content_type": "application/json",
    "content_encoding": "utf-8",
    "worker": "w1",
}


@pytest.fixture
def counted(store_driver, monkeypatch):
    """Count the store calls a manager makes."""
    store = get_store(TaskResult)
    calls = {"write": 0, "fetch": 0}

    def count(name):
        original = getattr(store, name)

        def wrapper(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper)

    count("write")
    count("fetch")
    return calls


@pytest.fixture
def listener():
    """Record the ``created`` flag of every save signal."""
    seen = {"pre": 0, "post": []}

    def on_pre_save(**kwargs):
        seen["pre"] += 1

    def on_post_save(created, **kwargs):
        seen["post"].append(created)

    pre_save.connect(on_pre_save, sender=TaskResult)
    post_save.connect(on_post_save, sender=TaskResult)
    yield seen
    pre_save.disconnect(on_pre_save, sender=TaskResult)
    post_save.disconnect(on_post_save, sender=TaskResult)


def test_store_writes_once_without_listeners(counted):
    instance = TaskResult.objects.store("t", dict(FIELDS))
    assert instance.status == "SUCCESS"
    assert counted == {"write": 1, "fetch": 0}
    TaskResult.objects.store("t", dict(FIELDS, status="FAILURE"))
    assert counted == {"write": 2, "fetch": 0}
    assert TaskResult.objects.get(pk="t").status == "FAILURE"


def test_store_reads_before_writing_when_signals_are_connected(counted, listener):
    TaskResult.objects.store("t", dict(FIELDS))
    TaskResult.objects.store("t", dict(FIELDS, status="FAILURE"))
    assert counted["fetch"] == 2
    assert listener["post"] == [True, False]
    assert listener["pre"] == 2
    assert TaskResult.objects.get(pk="t").status == "FAILURE"


def test_both_paths_store_the_same_record(store_driver):
    def receiver(**kwargs):
        pass

    post_save.connect(receiver, sender=TaskResult)
    try:
        with_signals = TaskResult.objects.store("signals", dict(FIELDS))
    finally:
        post_save.disconnect(receiver, sender=TaskResult)
    without = TaskResult.objects.store("plain", dict(FIELDS))
    volatile = {"task_id", "date_created", "date_done", "date_started"}
    names = [f.name for f in TaskResult._meta.concrete_fields if f.name not in volatile]
    assert {name: getattr(without, name) for name in names} == {
        name: getattr(with_signals, name) for name in names
    }
    stored = [TaskResult.objects.get(pk=pk).as_dict() for pk in ("plain", "signals")]
    assert {k: v for k, v in stored[0].items() if k not in volatile} == {
        k: v for k, v in stored[1].items() if k not in volatile
    }


def test_auto_timestamps_share_one_moment(store_driver):
    instance = TaskResult.objects.store("t", dict(FIELDS, date_started=Now()))
    assert instance.date_created == instance.date_started == instance.date_done


def test_updates_keep_date_created_and_advance_date_done(store_driver):
    created = TaskResult.objects.store("t", dict(FIELDS))
    updated = TaskResult.objects.store("t", dict(FIELDS, status="FAILURE"))
    assert updated.date_created == created.date_created
    assert updated.date_done > created.date_done
    assert updated.date_started is None


def test_none_in_a_not_null_field_is_rejected(store_driver):
    with pytest.raises(IntegrityError):
        TaskResult.objects.store("t", dict(FIELDS, status=None))
    assert not TaskResult.objects.filter(pk="t").exists()


def test_store_result_and_store_group_result_use_the_fast_path(counted):
    TaskResult.objects.store_result("application/json", "utf-8", "t", "1", "SUCCESS")
    assert counted == {"write": 1, "fetch": 0}
    group = GroupResult.objects.store_group_result(
        "application/json", "utf-8", "g", "[]"
    )
    assert group.result == "[]"
    assert GroupResult.objects.get(pk="g").content_type == "application/json"
