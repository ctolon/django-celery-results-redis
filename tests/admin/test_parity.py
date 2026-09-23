"""Static comparison with the django-celery-results 2.6.0 admin and models."""

import json
from datetime import datetime, timezone

import pytest
from celery.result import AsyncResult
from django.contrib import admin
from django.test import RequestFactory
from django_celery_results import admin as upstream_admin
from django_celery_results import models as upstream_models

from django_celery_results_redis import admin as our_admin
from django_celery_results_redis import models

ADMIN_OPTIONS = (
    "list_display",
    "list_filter",
    "date_hierarchy",
    "search_fields",
    "fieldsets",
    "readonly_fields",
    "ordering",
    "actions",
    "list_per_page",
    "list_max_show_all",
    "show_full_result_count",
    "show_facets",
)
FIELD_ATTRIBUTES = (
    "verbose_name",
    "help_text",
    "null",
    "blank",
    "default",
    "editable",
    "max_length",
    "unique",
    "choices",
    "auto_now",
    "auto_now_add",
)
MODEL_PAIRS = [
    (models.TaskResult, upstream_models.TaskResult),
    (models.GroupResult, upstream_models.GroupResult),
    (models.ChordCounter, upstream_models.ChordCounter),
]
ADMIN_PAIRS = [
    (our_admin.TaskResultAdmin, upstream_admin.TaskResultAdmin),
    (our_admin.GroupResultAdmin, upstream_admin.GroupResultAdmin),
]
DONE = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=timezone.utc)


def registered(model):
    return admin.site._registry[model]


@pytest.mark.parametrize(("ours", "theirs"), ADMIN_PAIRS)
@pytest.mark.parametrize("option", ADMIN_OPTIONS)
def test_admin_options(ours, theirs, option):
    assert getattr(ours, option) == getattr(theirs, option)


@pytest.mark.parametrize(("ours", "theirs"), ADMIN_PAIRS)
def test_admins_are_registered_with_the_same_class_layout(ours, theirs):
    assert isinstance(registered(ours.model), ours)
    assert isinstance(registered(theirs.model), theirs)
    assert set(vars(ours)) - {"__module__", "__doc__"} == set(vars(theirs)) - {
        "__module__",
        "__doc__",
    }


def test_chord_counter_is_not_registered():
    assert models.ChordCounter not in admin.site._registry
    assert upstream_models.ChordCounter not in admin.site._registry


def readonly_fields(model):
    request = RequestFactory().get("/")
    return set(registered(model).get_readonly_fields(request)) - {"id"}


def test_readonly_fields_without_edits():
    ours = readonly_fields(models.TaskResult)
    assert ours == readonly_fields(upstream_models.TaskResult)
    assert ours == {f.name for f in models.TaskResult._meta.local_fields}


@pytest.mark.parametrize(
    "setting",
    [
        {"DJANGO_CELERY_RESULTS_REDIS": {"ALLOW_EDITS": True}},
        {"DJANGO_CELERY_RESULTS": {"ALLOW_EDITS": True}},
    ],
)
def test_readonly_fields_with_edits(setting, settings, monkeypatch):
    for name, value in setting.items():
        setattr(settings, name, value)
    monkeypatch.setattr(upstream_admin, "ALLOW_EDITS", True)
    assert readonly_fields(models.TaskResult) == readonly_fields(
        upstream_models.TaskResult
    )
    assert readonly_fields(models.TaskResult) == set(
        our_admin.TaskResultAdmin.readonly_fields
    )


def test_own_allow_edits_setting_wins(settings):
    settings.DJANGO_CELERY_RESULTS_REDIS = {"ALLOW_EDITS": False}
    settings.DJANGO_CELERY_RESULTS = {"ALLOW_EDITS": True}
    assert readonly_fields(models.TaskResult) == {
        f.name for f in models.TaskResult._meta.local_fields
    }


@pytest.mark.parametrize(("ours", "theirs"), MODEL_PAIRS)
def test_fields(ours, theirs):
    their_fields = [f for f in theirs._meta.concrete_fields if f.name != "id"]
    assert [f.name for f in ours._meta.concrete_fields] == [
        f.name for f in their_fields
    ]
    for our_field, their_field in zip(
        ours._meta.concrete_fields, their_fields, strict=True
    ):
        assert type(our_field) is type(their_field)
        for attribute in FIELD_ATTRIBUTES:
            assert getattr(our_field, attribute, None) == getattr(
                their_field, attribute, None
            ), (our_field.name, attribute)


@pytest.mark.parametrize(
    ("model", "pk"),
    [
        (models.TaskResult, "task_id"),
        (models.GroupResult, "group_id"),
        (models.ChordCounter, "group_id"),
    ],
)
def test_natural_primary_key(model, pk):
    assert model._meta.pk.name == pk


@pytest.mark.parametrize(("ours", "theirs"), MODEL_PAIRS)
@pytest.mark.parametrize(
    "option", ["ordering", "verbose_name", "verbose_name_plural", "get_latest_by"]
)
def test_meta_options(ours, theirs, option):
    assert str(getattr(ours._meta, option)) == str(getattr(theirs._meta, option))


def test_app_verbose_name():
    assert str(models.TaskResult._meta.app_config.verbose_name) == (
        "Celery Results (Redis)"
    )


TASK_VALUES = {
    "task_id": "abc",
    "task_name": "app.add",
    "task_args": "[1]",
    "task_kwargs": "{}",
    "status": "SUCCESS",
    "result": "2",
    "date_done": DONE,
    "traceback": "Traceback",
    "meta": '{"children": []}',
    "worker": "celery@host",
    "periodic_task_name": "nightly",
    "content_type": "application/json",
    "content_encoding": "utf-8",
}
GROUP_VALUES = {"group_id": "g", "result": "[]", "date_done": DONE}


@pytest.mark.parametrize(
    ("ours", "theirs", "values"),
    [
        (models.TaskResult, upstream_models.TaskResult, TASK_VALUES),
        (models.GroupResult, upstream_models.GroupResult, GROUP_VALUES),
    ],
)
def test_str_and_as_dict(ours, theirs, values):
    our_instance, their_instance = ours(**values), theirs(**values)
    assert str(our_instance) == str(their_instance)
    assert our_instance.as_dict() == their_instance.as_dict()


def test_default_status():
    assert models.TaskResult().status == upstream_models.TaskResult().status


@pytest.mark.usefixtures("depends_on_current_app")
def test_chord_counter_group_result(celery_app):
    sub_tasks = json.dumps([AsyncResult("a").as_tuple(), AsyncResult("b").as_tuple()])
    ours = models.ChordCounter(group_id="g", sub_tasks=sub_tasks, count=2)
    theirs = upstream_models.ChordCounter(group_id="g", sub_tasks=sub_tasks, count=2)
    assert ours.group_result(app=celery_app) == theirs.group_result(app=celery_app)


def test_task_state_choices():
    assert models.TASK_STATE_CHOICES == upstream_models.TASK_STATE_CHOICES
