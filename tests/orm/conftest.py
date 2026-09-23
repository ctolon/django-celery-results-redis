from datetime import datetime, timedelta, timezone

import pytest

from django_celery_results_redis.models import TaskResult

BASE_TIME = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def tasks(store_driver):
    """Five results; t4 is the most recently done."""
    for index in range(5):
        TaskResult.objects.create(
            task_id=f"t{index}",
            status="SUCCESS" if index % 2 else "FAILURE",
            worker=None if index == 0 else f"w{index % 2}",
        )
        TaskResult.objects.filter(pk=f"t{index}").update(
            date_done=BASE_TIME + timedelta(hours=index)
        )
    return TaskResult.objects.all()
