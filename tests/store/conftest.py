from datetime import datetime, timedelta, timezone

import pytest

from django_celery_results_redis.models import TaskResult
from django_celery_results_redis.store import get_store

BASE_TIME = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def task_store(store_driver):
    return get_store(TaskResult)


def moment(minutes=0, micros=0):
    return BASE_TIME + timedelta(minutes=minutes, microseconds=micros)
