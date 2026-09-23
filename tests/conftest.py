import os
import uuid

import pytest
import redis
from celery.contrib.testing.app import Trap
from django.test import override_settings
from redis.exceptions import ResponseError

pytest_plugins = ("celery.contrib.pytest",)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DRIVERS = ("raw", "indexed", "redis_om")


@pytest.fixture(scope="session", autouse=True)
def setup_default_app_trap():
    from celery._state import set_default_app

    set_default_app(Trap())


@pytest.fixture
def app(celery_app):
    return celery_app


@pytest.fixture(scope="session")
def redis_client():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()
    yield client
    client.close()


@pytest.fixture(scope="session")
def has_search(redis_client):
    try:
        redis_client.execute_command("FT._LIST")
    except ResponseError:
        return False
    return True


def delete_prefix(client, prefix, *, search):
    if search:
        for index in client.execute_command("FT._LIST"):
            if index.startswith(f"{prefix}:"):
                client.ft(index).dropindex(delete_documents=False)
    keys = list(client.scan_iter(match=f"{prefix}:*", count=1000))
    if keys:
        client.unlink(*keys)


@pytest.fixture
def redis_settings(redis_client, has_search):
    """Return a function applying settings with a key prefix owned by the test."""
    prefixes = []
    overrides = []

    def apply(driver, **options):
        prefix = f"t{uuid.uuid4().hex[:12]}"
        prefixes.append(prefix)
        override = override_settings(
            DJANGO_CELERY_RESULTS_REDIS={
                "DRIVER": driver,
                "URL": REDIS_URL,
                "KEY_PREFIX": prefix,
                **options,
            }
        )
        override.enable()
        overrides.append(override)
        return prefix

    yield apply
    for override in reversed(overrides):
        override.disable()
    for prefix in prefixes:
        delete_prefix(redis_client, prefix, search=has_search)


@pytest.fixture(params=DRIVERS)
def store_driver(request, redis_settings, has_search):
    """Run the test once per storage driver, with an isolated key prefix."""
    if request.param == "redis_om" and not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings(request.param)
    return request.param


@pytest.fixture
def key_prefix(store_driver):
    from django_celery_results_redis.conf import app_settings

    return app_settings.KEY_PREFIX
