import pytest
from celery.contrib.testing.worker import start_worker


@pytest.fixture
def celery_config():
    return {
        "broker_url": "memory://",
        "broker_transport_options": {"polling_interval": 0.05},
        "result_backend": "django_celery_results_redis.backends:RedisResultBackend",
        "result_extended": True,
        "task_track_started": True,
    }


@pytest.fixture
def tasks(celery_app):
    @celery_app.task(name="tests.add")
    def add(x, y):
        return x + y

    @celery_app.task(name="tests.fail")
    def fail():
        raise ValueError("boom")

    @celery_app.task(name="tests.total")
    def total(numbers):
        total.calls.append(numbers)
        return sum(numbers)

    total.calls = []
    return {"add": add, "fail": fail, "total": total}


@pytest.fixture
def worker(store_driver, celery_app, tasks):
    with start_worker(
        celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=10
    ) as worker:
        yield worker
