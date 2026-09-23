from datetime import timedelta

import pytest
import redis

from django_celery_results_redis import checks


def error_ids(messages):
    return [message.id for message in messages]


@pytest.mark.parametrize("driver", ["raw", "indexed", "redis_om"])
def test_valid_settings(redis_settings, driver):
    redis_settings(driver)
    assert checks.check_settings(None) == []


@pytest.mark.parametrize(
    "driver",
    [
        "unknown",
        "django_celery_results_redis.missing.Store",
        "django_celery_results_redis.models.TaskResult",
    ],
)
def test_unknown_driver(redis_settings, driver):
    redis_settings(driver)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E001"
    ]


def test_custom_driver_path(redis_settings):
    redis_settings("django_celery_results_redis.store.drivers.raw.RawStore")
    assert checks.check_settings(None) == []


def test_redis_om_not_installed(redis_settings, monkeypatch):
    redis_settings("redis_om")
    monkeypatch.setattr(checks.importlib.util, "find_spec", lambda name: None)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E002"
    ]


def test_redis_om_requires_database_zero(redis_settings):
    redis_settings("redis_om", URL="redis://localhost:6379/1")
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E003"
    ]


def test_redis_om_database_ignored_with_client_factory(redis_settings):
    redis_settings(
        "redis_om",
        URL="redis://localhost:6379/1",
        CLIENT_FACTORY="django_celery_results_redis.store.create_client",
    )
    assert checks.check_settings(None) == []


@pytest.mark.parametrize("search", [None, "redisearch"])
def test_valid_search_setting(redis_settings, search):
    redis_settings("indexed", SEARCH=search)
    assert checks.check_settings(None) == []


@pytest.mark.parametrize("search", ["yes", True, "redis_search"])
def test_invalid_search_setting(redis_settings, search):
    redis_settings("indexed", SEARCH=search)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E007"
    ]


def test_search_requires_database_zero(redis_settings):
    redis_settings("indexed", SEARCH="redisearch", URL="redis://localhost:6379/1")
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E003"
    ]


def test_search_database_ignored_with_client_factory(redis_settings):
    redis_settings(
        "indexed",
        SEARCH="redisearch",
        URL="redis://localhost:6379/1",
        CLIENT_FACTORY="django_celery_results_redis.store.create_client",
    )
    assert checks.check_settings(None) == []


@pytest.mark.parametrize("prefix", ["", "a:b", "a b", "a*", "a?", "a[b]", 3])
def test_invalid_key_prefix(redis_settings, prefix):
    redis_settings("raw", KEY_PREFIX=prefix)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E004"
    ]


def test_allow_edits_must_be_bool(redis_settings):
    redis_settings("raw", ALLOW_EDITS="yes")
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E005"
    ]


@pytest.mark.parametrize("ttl", [0, -1, "10", 1.5, True])
def test_invalid_result_ttl(redis_settings, ttl):
    redis_settings("raw", RESULT_TTL=ttl)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E006"
    ]


@pytest.mark.parametrize("ttl", [60, timedelta(hours=1)])
def test_valid_result_ttl(redis_settings, ttl):
    redis_settings("raw", RESULT_TTL=ttl)
    assert checks.check_settings(None) == []


def test_result_ttl_unsupported_by_indexed(redis_settings):
    redis_settings("indexed", RESULT_TTL=60)
    assert error_ids(checks.check_settings(None)) == [
        "django_celery_results_redis.E006"
    ]


def test_server_checks_skipped_without_databases(redis_settings):
    redis_settings("raw", URL="redis://localhost:1/0")
    assert checks.check_server(None) == []


def test_server_ok(redis_settings):
    redis_settings("redis_om")
    assert checks.check_server(None, databases=["default"]) == []


def test_server_unreachable(redis_settings):
    redis_settings("raw", URL="redis://localhost:1/0")
    assert error_ids(checks.check_server(None, databases=["default"])) == [
        "django_celery_results_redis.E101"
    ]


def test_server_without_search(redis_settings, monkeypatch):
    redis_settings("redis_om")
    monkeypatch.setattr(redis.Redis, "module_list", lambda self: [])
    assert error_ids(checks.check_server(None, databases=["default"])) == [
        "django_celery_results_redis.E102"
    ]


def test_search_index_without_search_module(redis_settings, monkeypatch):
    redis_settings("indexed", SEARCH="redisearch")
    monkeypatch.setattr(redis.Redis, "module_list", lambda self: [])
    messages = checks.check_server(None, databases=["default"])
    assert error_ids(messages) == ["django_celery_results_redis.E102"]
    assert "The search index requires RediSearch" in messages[0].msg


def test_search_index_with_search_module(redis_settings, has_search):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings("indexed", SEARCH="redisearch")
    assert checks.check_server(None, databases=["default"]) == []


def patch_info(monkeypatch, **overrides):
    info = redis.Redis.info

    def patched(self, section=None, *args, **kwargs):
        return {**info(self, section, *args, **kwargs), **overrides}

    monkeypatch.setattr(redis.Redis, "info", patched)


def test_server_too_old(redis_settings, monkeypatch):
    redis_settings("indexed")
    patch_info(monkeypatch, redis_version="6.0.16")
    assert error_ids(checks.check_server(None, databases=["default"])) == [
        "django_celery_results_redis.E103"
    ]


def test_server_evicting_keys(redis_settings, monkeypatch):
    redis_settings("indexed")
    patch_info(monkeypatch, maxmemory_policy="allkeys-lru")
    assert error_ids(checks.check_server(None, databases=["default"])) == [
        "django_celery_results_redis.W101"
    ]
