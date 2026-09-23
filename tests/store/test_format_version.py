import pytest
from django.core.exceptions import ImproperlyConfigured

from django_celery_results_redis.models import GroupResult, TaskResult
from django_celery_results_redis.store import FORMAT_VERSION, get_store


def meta(client, prefix):
    return client.hgetall(f"{prefix}:meta")


def test_a_fresh_prefix_claims_the_current_version(redis_settings, redis_client):
    prefix = redis_settings("raw")

    get_store(TaskResult)

    assert meta(redis_client, prefix) == {"format": str(FORMAT_VERSION)}


def test_the_version_is_claimed_once(redis_settings, redis_client):
    prefix = redis_settings("raw")
    redis_client.hset(f"{prefix}:meta", mapping={"format": FORMAT_VERSION, "kept": "1"})

    get_store(TaskResult)
    get_store(GroupResult)

    assert meta(redis_client, prefix) == {"format": str(FORMAT_VERSION), "kept": "1"}


def test_another_format_is_refused(redis_settings, redis_client):
    prefix = redis_settings("raw")
    redis_client.hset(f"{prefix}:meta", "format", FORMAT_VERSION + 1)

    with pytest.raises(ImproperlyConfigured, match=prefix):
        get_store(TaskResult)

    with pytest.raises(ImproperlyConfigured, match=f"format {FORMAT_VERSION}"):
        get_store(TaskResult)


def test_records_of_the_current_version_are_readable(redis_settings, store_driver):
    TaskResult.objects.store_result("application/json", "utf-8", "a", "1", "SUCCESS")
    assert TaskResult.objects.get(pk="a").status == "SUCCESS"


def test_every_prefix_is_checked(redis_settings, redis_client):
    redis_settings("raw")
    get_store(TaskResult)

    prefix = redis_settings("raw")
    redis_client.hset(f"{prefix}:meta", "format", FORMAT_VERSION + 1)

    with pytest.raises(ImproperlyConfigured):
        get_store(TaskResult)
