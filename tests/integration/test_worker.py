import pytest
from celery import chord, group, states
from celery.result import GroupResult as CeleryGroupResult

from django_celery_results_redis.models import ChordCounter, GroupResult, TaskResult

pytestmark = pytest.mark.integration

TIMEOUT = 10


@pytest.mark.usefixtures("worker")
def test_success(tasks):
    result = tasks["add"].delay(2, 3)
    assert result.get(timeout=TIMEOUT) == 5

    stored = TaskResult.objects.get(task_id=result.id)
    assert stored.status == states.SUCCESS
    assert stored.result == "5"
    assert stored.task_name == "tests.add"
    assert stored.task_args == '"(2, 3)"'
    assert stored.task_kwargs == '"{}"'
    assert stored.worker
    assert stored.date_started is not None
    assert stored.date_created <= stored.date_started <= stored.date_done


@pytest.mark.usefixtures("worker")
def test_failure(tasks):
    result = tasks["fail"].delay()
    with pytest.raises(ValueError, match="boom"):
        result.get(timeout=TIMEOUT)

    stored = TaskResult.objects.get(task_id=result.id)
    assert stored.status == states.FAILURE
    assert "ValueError: boom" in stored.traceback
    assert result.traceback == stored.traceback


@pytest.mark.usefixtures("worker")
def test_chord(tasks):
    result = chord(tasks["add"].s(i, i) for i in range(4))(tasks["total"].s())
    assert result.get(timeout=TIMEOUT) == 12
    assert tasks["total"].calls == [[0, 2, 4, 6]]
    assert not ChordCounter.objects.exists()


@pytest.mark.usefixtures("worker")
def test_group_save_restore(tasks, celery_app):
    result = group(tasks["add"].s(i, 1) for i in range(3)).apply_async()
    assert result.get(timeout=TIMEOUT) == [1, 2, 3]
    result.save()

    assert GroupResult.objects.filter(group_id=result.id).exists()
    restored = CeleryGroupResult.restore(result.id, app=celery_app)
    assert [child.id for child in restored.results] == [
        child.id for child in result.results
    ]
    assert restored.get(timeout=TIMEOUT) == [1, 2, 3]


@pytest.mark.usefixtures("store_driver")
def test_eager_result_is_stored(celery_app, tasks):
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_store_eager_result = True

    result = tasks["add"].delay(1, 1)

    assert result.get() == 2
    stored = TaskResult.objects.get(task_id=result.id)
    assert stored.status == states.SUCCESS
    assert stored.task_name == "tests.add"
