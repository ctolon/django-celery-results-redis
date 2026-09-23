import json

import pytest
from celery import states, uuid
from celery.app.task import Context
from celery.result import AsyncResult
from celery.result import GroupResult as CeleryGroupResult
from django.test.client import RequestFactory
from django.urls import reverse

from django_celery_results_redis.models import GroupResult, TaskResult
from django_celery_results_redis.views import (
    group_status,
    is_group_successful,
    is_task_successful,
    task_status,
)

pytestmark = pytest.mark.usefixtures("store_driver", "depends_on_current_app")


@pytest.fixture(autouse=True)
def setup_app(app):
    app.conf.result_serializer = "json"
    app.conf.result_backend = "django_celery_results_redis.backends:RedisResultBackend"


@pytest.fixture
def factory():
    return RequestFactory()


def create_task_result():
    taskmeta, _ = TaskResult.objects.get_or_create(task_id=uuid())
    return taskmeta


def create_group_result():
    """Return a GroupResult model instance with a single, successful result."""
    task_result = create_task_result()
    task_result.status = states.SUCCESS
    task_result.save()
    group = CeleryGroupResult(id=uuid(), results=[AsyncResult(id=task_result.task_id)])
    group.save()
    return GroupResult.objects.get(group_id=group.id)


def load(response):
    return json.loads(response.content.decode("utf-8"))


def test_is_task_successful(factory):
    taskmeta = create_task_result()
    request = factory.get(f"/done/{taskmeta.task_id}")
    assert (
        load(is_task_successful(request, taskmeta.task_id))["task"]["executed"] is False
    )

    TaskResult.objects.store_result(
        "application/json",
        "utf-8",
        taskmeta.task_id,
        json.dumps({"result": True}),
        status=states.SUCCESS,
    )

    response = is_task_successful(request, taskmeta.task_id)
    assert load(response)["task"]["executed"] is True


def test_task_status(factory):
    taskmeta = create_task_result()
    request = factory.get(f"/status/{taskmeta.task_id}")
    assert load(task_status(request, taskmeta.task_id))["task"]["status"] != (
        states.SUCCESS
    )

    TaskResult.objects.store_result(
        "application/json",
        "utf-8",
        taskmeta.task_id,
        json.dumps({"result": True}),
        status=states.SUCCESS,
    )

    result = load(task_status(request, taskmeta.task_id))
    assert result["task"]["status"] == states.SUCCESS


def test_task_status_failure(app, factory):
    app.conf.result_extended = True
    task_id = uuid()
    try:
        raise KeyError("foo")
    except KeyError as exc:
        app.backend.mark_as_failure(
            task_id,
            exc,
            traceback="Traceback: foo",
            request=Context(id=task_id, task="failing"),
        )

    result = load(task_status(factory.get("/"), task_id))["task"]
    assert result["status"] == states.FAILURE
    assert result["exc"] == "builtins.KeyError"
    assert result["traceback"] == "Traceback: foo"


def test_is_group_successful(factory):
    meta = create_group_result()
    request = factory.get(f"/group/done/{meta.group_id}")
    result = load(is_group_successful(request, meta.group_id))
    assert len(result["group"]["results"]) == 1
    assert result["group"]["results"][0]["executed"] is True


def test_is_group_successful_missing(factory):
    group_id = uuid()
    result = load(is_group_successful(factory.get("/"), group_id))
    assert result == {"group": {"id": group_id, "results": []}}


def test_group_status(factory):
    meta = create_group_result()
    request = factory.get(f"/group/status/{meta.group_id}")
    result = load(group_status(request, meta.group_id))
    assert len(result["group"]["results"]) == 1
    assert result["group"]["results"][0]["status"] == states.SUCCESS


def test_urls(client):
    taskmeta = create_task_result()
    meta = create_group_result()
    for name, value in [
        ("celery-is_task_successful", taskmeta.task_id),
        ("celery-task_status", taskmeta.task_id),
        ("celery-is_group_successful", meta.group_id),
        ("celery-group_status", meta.group_id),
    ]:
        response = client.get(reverse(name, args=[value]))
        assert response.status_code == 200
        assert value in response.content.decode()
