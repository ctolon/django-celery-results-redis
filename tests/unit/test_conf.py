from datetime import timedelta

import pytest
from django.test import override_settings

from django_celery_results_redis import store
from django_celery_results_redis.conf import DEFAULTS, app_settings
from django_celery_results_redis.models import TaskResult


@override_settings(DJANGO_CELERY_RESULTS_REDIS={})
def test_defaults():
    for name, value in DEFAULTS.items():
        assert getattr(app_settings, name) == value


@override_settings(DJANGO_CELERY_RESULTS_REDIS=None)
def test_unset_setting_uses_defaults():
    assert app_settings.DRIVER == "indexed"


def test_unknown_key_raises_attribute_error():
    with pytest.raises(AttributeError, match="NOPE"):
        app_settings.NOPE  # noqa: B018


@pytest.mark.parametrize(
    ("own", "upstream", "expected"),
    [
        (None, None, False),
        (None, {"ALLOW_EDITS": True}, True),
        (False, {"ALLOW_EDITS": True}, False),
        (True, {}, True),
    ],
)
def test_allow_edits_falls_back_to_django_celery_results(own, upstream, expected):
    settings = {"DJANGO_CELERY_RESULTS_REDIS": {"ALLOW_EDITS": own}}
    if upstream is not None:
        settings["DJANGO_CELERY_RESULTS"] = upstream
    with override_settings(**settings):
        assert app_settings.allow_edits is expected


def test_task_id_max_length_fallbacks():
    with override_settings(DJANGO_CELERY_RESULTS_REDIS={}):
        assert app_settings.task_id_max_length == 255
    with override_settings(
        DJANGO_CELERY_RESULTS_REDIS={}, DJANGO_CELERY_RESULTS_TASK_ID_MAX_LENGTH=191
    ):
        assert app_settings.task_id_max_length == 191
    with override_settings(
        DJANGO_CELERY_RESULTS_REDIS={"TASK_ID_MAX_LENGTH": 64},
        DJANGO_CELERY_RESULTS_TASK_ID_MAX_LENGTH=191,
    ):
        assert app_settings.task_id_max_length == 64


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), (60, 60), (timedelta(hours=1), 3600)]
)
def test_result_ttl(value, expected):
    with override_settings(DJANGO_CELERY_RESULTS_REDIS={"RESULT_TTL": value}):
        assert app_settings.result_ttl == expected


def test_changing_the_setting_drops_cached_stores(redis_settings):
    redis_settings("raw")
    raw_store = store.get_store(TaskResult)
    assert store.get_store(TaskResult) is raw_store
    prefix = redis_settings("indexed")
    indexed_store = store.get_store(TaskResult)
    assert indexed_store is not raw_store
    assert type(indexed_store).__name__ == "IndexedStore"
    assert indexed_store.keys.prefix == prefix
