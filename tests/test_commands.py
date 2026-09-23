from io import StringIO

import pytest
from celery import states, uuid
from django.core.management import call_command

from django_celery_results_redis.models import GroupResult, TaskResult
from django_celery_results_redis.store import get_store


def rebuild():
    out = StringIO()
    call_command("celery_results_redis_rebuild_index", stdout=out)
    return out.getvalue()


def store_results():
    for status in (states.SUCCESS, states.SUCCESS, states.FAILURE):
        TaskResult.objects.store_result(
            "application/json", "utf-8", uuid(), "1", status
        )
    GroupResult.objects.store_group_result("application/json", "utf-8", uuid(), "1")


def test_rebuild_indexed(redis_settings, redis_client):
    prefix = redis_settings("indexed")
    store_results()
    index_keys = list(redis_client.scan_iter(match=f"{prefix}:idx:*"))
    redis_client.unlink(*index_keys)
    assert TaskResult.objects.filter(status=states.SUCCESS).count() == 0

    output = rebuild()

    assert "Rebuilt the indexed indexes of django_celery_results_redis.TaskResult" in (
        output
    )
    assert TaskResult.objects.filter(status=states.SUCCESS).count() == 2
    assert TaskResult.objects.count() == 3
    assert GroupResult.objects.count() == 1
    assert sorted(redis_client.scan_iter(match=f"{prefix}:idx:*")) == sorted(index_keys)


def test_rebuild_indexed_removes_stale_entries(redis_settings, redis_client):
    prefix = redis_settings("indexed")
    store_results()
    stale = TaskResult.objects.filter(status=states.FAILURE).get()
    redis_client.delete(f"{prefix}:task:{stale.task_id}")

    rebuild()

    assert TaskResult.objects.filter(status=states.FAILURE).count() == 0
    assert not redis_client.exists(f"{prefix}:idx:task:s:status:{states.FAILURE}")


def test_rebuild_redis_om(redis_settings, redis_client, has_search):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings("redis_om")
    store_results()
    get_store(TaskResult).ft.dropindex(delete_documents=False)

    rebuild()

    assert TaskResult.objects.filter(status=states.SUCCESS).count() == 2
    assert GroupResult.objects.count() == 1


def test_rebuild_raw(redis_settings):
    redis_settings("raw")
    store_results()
    assert "raw" in rebuild()
    assert TaskResult.objects.count() == 3
