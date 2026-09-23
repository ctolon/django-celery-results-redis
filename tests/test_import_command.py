from datetime import datetime, timezone
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_celery_results import models as upstream

from django_celery_results_redis.models import GroupResult, TaskResult

pytestmark = pytest.mark.django_db

DONE = datetime(2024, 5, 4, 12, 30, tzinfo=timezone.utc)
CREATED = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def upstream_results():
    for index, status in enumerate(["SUCCESS", "FAILURE", "STARTED"]):
        upstream.TaskResult.objects.create(
            task_id=f"t{index}",
            status=status,
            task_name="app.add",
            worker=None if index else "w1",
            result=f'"{index}"',
            content_type="application/json",
            content_encoding="utf-8",
        )
        upstream.TaskResult.objects.filter(task_id=f"t{index}").update(
            date_done=DONE, date_created=CREATED
        )
    upstream.GroupResult.objects.create(
        group_id="g0", content_type="application/json", content_encoding="utf-8"
    )
    upstream.GroupResult.objects.filter(group_id="g0").update(
        date_done=DONE, date_created=CREATED
    )


def run(**options):
    output = StringIO()
    call_command("celery_results_redis_import", stdout=output, **options)
    return output.getvalue()


def test_imports_results_with_their_timestamps(store_driver, upstream_results):
    output = run()

    assert "3 task results imported." in output
    assert "1 group results imported." in output
    assert TaskResult.objects.count() == 3
    stored = TaskResult.objects.get(pk="t0")
    assert stored.status == "SUCCESS"
    assert stored.worker == "w1"
    assert TaskResult.objects.get(pk="t1").worker is None
    assert stored.result == '"0"'
    assert stored.date_done == DONE
    assert stored.date_created == CREATED
    assert GroupResult.objects.get(pk="g0").date_done == DONE


def test_imported_results_are_queryable(store_driver, upstream_results):
    run()

    assert list(
        TaskResult.objects.filter(status="SUCCESS").values_list("pk", flat=True)
    ) == ["t0"]
    assert TaskResult.objects.filter(worker__isnull=True).count() == 2
    assert TaskResult.objects.filter(date_done__year=2024).count() == 3


def test_since_and_dry_run(store_driver, upstream_results):
    assert "3 task results imported." in run(dry_run=True)
    assert TaskResult.objects.count() == 0

    upstream.TaskResult.objects.filter(task_id="t0").update(
        date_done=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    run(since="2025-01-01")
    assert list(TaskResult.objects.values_list("pk", flat=True)) == ["t0"]


def test_since_rejects_invalid_values(store_driver, upstream_results):
    with pytest.raises(CommandError, match="Cannot parse"):
        run(since="last tuesday")


def test_import_is_repeatable(store_driver, upstream_results):
    run()
    upstream.TaskResult.objects.filter(task_id="t0").update(status="RETRY")
    run()

    assert TaskResult.objects.count() == 3
    assert TaskResult.objects.get(pk="t0").status == "RETRY"
