from datetime import datetime, timedelta
from unittest import mock

import pytest
from celery import states, uuid
from redis.exceptions import ConnectionError

from django_celery_results_redis.conf import app_settings
from django_celery_results_redis.models import ChordCounter, GroupResult, TaskResult
from django_celery_results_redis.store.drivers.base import Store
from django_celery_results_redis.utils import now

pytestmark = pytest.mark.usefixtures("store_driver", "depends_on_current_app")

CTYPE = "application/json"
CENC = "utf-8"


@pytest.fixture(autouse=True)
def setup_app(app):
    app.conf.result_serializer = "pickle"
    app.conf.result_backend = "django_celery_results_redis.backends:RedisResultBackend"


def create_task_result():
    taskmeta, _ = TaskResult.objects.get_or_create(task_id=uuid())
    return taskmeta


def create_group_result():
    groupmeta, _ = GroupResult.objects.get_or_create(group_id=uuid())
    return groupmeta


def failing_writes(count):
    write = Store.write
    failures = [ConnectionError()] * count

    def side_effect(self, *args, **kwargs):
        if failures:
            raise failures.pop()
        return write(self, *args, **kwargs)

    return mock.patch.object(Store, "write", autospec=True, side_effect=side_effect)


def test_taskmeta(app):
    m1 = create_task_result()
    m2 = create_task_result()
    m3 = create_task_result()
    assert str(m1).startswith("<Task:")
    assert m1.task_id
    assert isinstance(m1.date_done, datetime)

    task = TaskResult.objects.get_task(m1.task_id)
    assert task._meta.get_field("task_id").max_length == (
        app_settings.task_id_max_length
    )
    assert task.task_id == m1.task_id
    assert task.status != states.SUCCESS
    TaskResult.objects.store_result(
        CTYPE, CENC, m1.task_id, True, status=states.SUCCESS
    )
    TaskResult.objects.store_result(
        CTYPE, CENC, m2.task_id, True, status=states.SUCCESS
    )
    assert TaskResult.objects.get_task(m1.task_id).status == states.SUCCESS
    assert TaskResult.objects.get_task(m2.task_id).status == states.SUCCESS

    TaskResult.objects.filter(task_id=m1.task_id).update(
        date_done=now() - timedelta(days=10)
    )

    expired = TaskResult.objects.get_all_expired(app.conf.result_expires)
    assert m1 in expired
    assert m2 not in expired
    assert m3 not in expired

    TaskResult.objects.delete_expired(app.conf.result_expires)
    assert m1 not in TaskResult.objects.all()
    assert m2 in TaskResult.objects.all()


def test_store_result_ignores_using():
    m1 = create_task_result()
    TaskResult.objects.store_result(
        CTYPE, CENC, m1.task_id, True, status=states.SUCCESS, using="secondary"
    )
    assert TaskResult.objects.get_task(m1.task_id).status == states.SUCCESS


def test_store_result_creates_and_updates():
    task_id = uuid()
    created = TaskResult.objects.store_result(
        "application/json",
        "utf-8",
        task_id,
        "1",
        states.STARTED,
        task_name="add",
        worker="celery@host",
        date_started=now(),
    )
    date_created = created.date_created
    updated = TaskResult.objects.store_result(
        "application/json",
        "utf-8",
        task_id,
        "2",
        states.SUCCESS,
        traceback=None,
    )
    stored = TaskResult.objects.get(task_id=task_id)
    assert stored.status == states.SUCCESS
    assert stored.result == "2"
    assert stored.task_name is None
    assert stored.worker is None
    assert stored.date_created == date_created
    assert stored.date_started == created.date_started
    assert stored.date_done >= updated.date_created


def test_get_task_missing_returns_unsaved_instance():
    task_id = uuid()
    task = TaskResult.objects.get_task(task_id)
    assert task.task_id == task_id
    assert task.status == states.PENDING
    assert task._state.adding
    assert not TaskResult.objects.filter(task_id=task_id).exists()


def test_get_group_missing_returns_unsaved_instance():
    group_id = uuid()
    group = GroupResult.objects.get_group(group_id)
    assert group.group_id == group_id
    assert group._state.adding


def test_store_group_result():
    m1 = create_group_result()
    m2 = create_group_result()
    GroupResult.objects.store_group_result(CTYPE, CENC, m1.group_id, "result")
    GroupResult.objects.store_group_result(
        CTYPE, CENC, m2.group_id, "other", using="secondary"
    )
    assert GroupResult.objects.get_group(m1.group_id).result == "result"
    assert GroupResult.objects.get_group(m2.group_id).result == "other"
    new_id = uuid()
    stored = GroupResult.objects.store_group_result(CTYPE, CENC, new_id, "new")
    assert stored.content_type == CTYPE
    assert GroupResult.objects.get(group_id=new_id).content_encoding == CENC


def test_group_delete_expired():
    m1 = create_group_result()
    m2 = create_group_result()
    GroupResult.objects.filter(group_id=m1.group_id).update(
        date_done=now() - timedelta(days=10)
    )
    assert list(GroupResult.objects.get_all_expired(timedelta(days=1))) == [m1]
    GroupResult.objects.delete_expired(timedelta(days=1))
    assert list(GroupResult.objects.all()) == [m2]


def test_delete_expired_without_database_access():
    TaskResult.objects.delete_expired(expires=10)
    GroupResult.objects.delete_expired(expires=10)


def test_store_result_retries_connection_errors():
    task_id = uuid()
    with failing_writes(2):
        TaskResult.objects.store_result(
            "application/json", "utf-8", task_id, "1", states.SUCCESS
        )
    assert TaskResult.objects.get(task_id=task_id).status == states.SUCCESS


def test_store_result_gives_up_after_retries():
    with failing_writes(3), pytest.raises(ConnectionError):
        TaskResult.objects.store_result(
            "application/json", "utf-8", uuid(), "1", states.SUCCESS
        )


def test_store_result_exception_retry_count():
    with failing_writes(1), pytest.raises(ConnectionError):
        TaskResult.objects.store_result(
            "application/json",
            "utf-8",
            uuid(),
            "1",
            states.SUCCESS,
            exception_retry_count=0,
        )


def test_store_group_result_retries_connection_errors():
    group_id = uuid()
    with failing_writes(2):
        GroupResult.objects.store_group_result(
            "application/json", "utf-8", group_id, "1"
        )
    assert GroupResult.objects.filter(group_id=group_id).exists()


def test_chord_counter_decrement_missing():
    assert ChordCounter.objects.decrement(uuid()) is None


def test_chord_counter_decrement():
    group_id = uuid()
    ChordCounter.objects.create(group_id=group_id, sub_tasks="[]", count=2)

    counter = ChordCounter.objects.decrement(group_id)
    assert counter.count == 1
    assert counter.sub_tasks is None
    assert ChordCounter.objects.get(group_id=group_id).count == 1

    counter = ChordCounter.objects.decrement(group_id)
    assert counter.count == 0
    assert counter.sub_tasks == "[]"
    assert not ChordCounter.objects.filter(group_id=group_id).exists()
    assert ChordCounter.objects.decrement(group_id) is None
