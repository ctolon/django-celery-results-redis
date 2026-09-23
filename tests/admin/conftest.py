from datetime import datetime, timezone

import pytest
from django.contrib import admin
from django.contrib.admin.templatetags.admin_list import date_hierarchy
from django.urls import reverse
from django_celery_results import models as upstream_models

from django_celery_results_redis import models

TASKS = [
    # task_id, status, task_name, worker, periodic_task_name, args, kwargs, date_done
    ("t00", "SUCCESS", "app.add", "w1", "nightly", "[1, 2]", '{"x": 1}',
     datetime(2023, 12, 31, 23, 30, tzinfo=timezone.utc)),
    ("t01", "FAILURE", "app.add", "w2", None, "[3, 4]", '{"y": "Foo Bar"}',
     datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)),
    ("t02", "SUCCESS", "app.mul", None, None, "[5]", "{}",
     datetime(2024, 1, 1, 4, 30, tzinfo=timezone.utc)),
    ("t03", "PENDING", None, "w1", "hourly", "[]", '{"needle": true}',
     datetime(2024, 2, 15, 12, 0, tzinfo=timezone.utc)),
    ("t04", "STARTED", "app.mul", "w2", "nightly", "[6, 7]", "{}",
     datetime(2024, 2, 15, 12, 0, tzinfo=timezone.utc)),
    ("t05", "SUCCESS", "app.div", "w1", None, '["foo bar"]', "{}",
     datetime(2024, 2, 15, 12, 0, tzinfo=timezone.utc)),
    ("t06", "RETRY", "app.div", None, "hourly", "[8]", '{"z": [1]}',
     datetime(2024, 3, 3, 8, 15, tzinfo=timezone.utc)),
    ("t07", "SUCCESS", "app.add", "w3", None, "[9, 10]", "{}",
     datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)),
]  # fmt: skip

GROUPS = [
    ("g00", datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)),
    ("g01", datetime(2024, 5, 2, 12, 0, tzinfo=timezone.utc)),
    ("g02", datetime(2025, 7, 3, 12, 0, tzinfo=timezone.utc)),
]


def naive(value, use_tz):
    return value if use_tz else value.replace(tzinfo=None)


def load_tasks(model, rows=TASKS, *, use_tz=True):
    for task_id, status, name, worker, periodic, args, kwargs, done in rows:
        model.objects.create(
            task_id=task_id,
            status=status,
            task_name=name,
            worker=worker,
            periodic_task_name=periodic,
            task_args=args,
            task_kwargs=kwargs,
            content_type="application/json",
            content_encoding="utf-8",
            result='"ok"',
        )
        model.objects.filter(task_id=task_id).update(
            date_done=naive(done, use_tz), date_created=naive(done, use_tz)
        )


def load_groups(model, rows=GROUPS):
    for group_id, done in rows:
        model.objects.create(
            group_id=group_id, content_type="application/json", content_encoding="utf-8"
        )
        model.objects.filter(group_id=group_id).update(
            date_done=done, date_created=done
        )


def changelist_url(app_label, model_name="taskresult"):
    return reverse(f"admin:{app_label}_{model_name}_changelist")


TASKS_URL = changelist_url("django_celery_results_redis")
GROUPS_URL = changelist_url("django_celery_results_redis", "groupresult")
UPSTREAM_TASKS_URL = changelist_url("django_celery_results")
UPSTREAM_GROUPS_URL = changelist_url("django_celery_results", "groupresult")


def row_ids(response, field="task_id"):
    return [getattr(obj, field) for obj in response.context["cl"].result_list]


def filter_choices(response):
    cl = response.context["cl"]
    return {
        spec.title: [
            (str(choice["display"]), choice["selected"], choice["query_string"])
            for choice in spec.choices(cl)
        ]
        for spec in cl.filter_specs
    }


def hierarchy(response):
    result = date_hierarchy(response.context["cl"])
    return {
        "back": result.get("back"),
        "choices": [dict(choice) for choice in result["choices"]],
    }


def column_index(response, name):
    return response.context["cl"].list_display.index(name)


@pytest.fixture
def tasks(store_driver):
    load_tasks(models.TaskResult)
    return TASKS


@pytest.fixture
def upstream_tasks(db):
    load_tasks(upstream_models.TaskResult)
    return TASKS


@pytest.fixture
def per_page(monkeypatch):
    def apply(size):
        for model in (models.TaskResult, upstream_models.TaskResult):
            monkeypatch.setattr(admin.site._registry[model], "list_per_page", size)

    return apply
