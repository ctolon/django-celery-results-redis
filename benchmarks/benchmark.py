"""Measure the cost of storing results and of rendering the admin changelist.

    $ uv run python benchmarks/benchmark.py --records 50000 --drivers indexed raw

The Django settings come from ``tests.settings``; ``REDIS_URL`` selects the
server. Each driver runs under its own key prefix, which is deleted afterwards.
"""

import argparse
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import override_settings, setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402

from django_celery_results_redis.backends import RedisResultBackend  # noqa: E402
from django_celery_results_redis.models import TaskResult  # noqa: E402
from django_celery_results_redis.store import get_client, get_store  # noqa: E402

STATUSES = ["SUCCESS", "FAILURE", "STARTED", "RETRY", "PENDING"]
TASK_NAMES = [f"app.tasks.task_{index:02}" for index in range(20)]
WORKERS = [f"celery@worker-{index:02}" for index in range(8)]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def timed(function, repeat=5):
    """Return the median duration of ``function`` in milliseconds."""
    durations = []
    for _ in range(repeat):
        start = time.perf_counter()
        function()
        durations.append((time.perf_counter() - start) * 1000)
    return statistics.median(durations)


def memory_used(client, prefix):
    """Bytes held by the keys under ``prefix``, and how many there are."""
    total = keys = 0
    for key in client.scan_iter(match=f"{prefix}:*", count=1000):
        total += client.memory_usage(key) or 0
        keys += 1
    return total, keys


def populate(count, seed=1):
    random.seed(seed)
    store = get_store(TaskResult)
    start = time.perf_counter()
    for index in range(count):
        store.write(
            f"task-{index:08}",
            {
                "status": random.choice(STATUSES),
                "task_name": random.choice(TASK_NAMES),
                "worker": random.choice(WORKERS),
                "periodic_task_name": None,
                "result": f'{{"index": {index}}}',
                "task_args": f'"[{index}, {index * 2}]"',
                "task_kwargs": '"{\\"retries\\": 0}"',
                "content_type": "application/json",
                "content_encoding": "utf-8",
                "date_done": START + timedelta(seconds=index * 7),
                "date_created": START + timedelta(seconds=index * 7),
            },
        )
    return (time.perf_counter() - start) * 1000


def store_result_rate(app, samples=300):
    """Round trips per stored result, measured through the backend."""
    backend = RedisResultBackend(app=app)
    start = time.perf_counter()
    for index in range(samples):
        backend.store_result(f"bench-{index}", {"value": index}, "SUCCESS")
    elapsed = time.perf_counter() - start
    TaskResult.objects.filter(task_id__startswith="bench-").delete()
    return samples / elapsed


def admin_measurements(client, count):
    url = reverse("admin:django_celery_results_redis_taskresult_changelist")
    page = count // 2

    def get(params):
        response = client.get(f"{url}?{params}")
        assert response.status_code == 200, params
        return response

    return {
        "changelist (first page)": lambda: get(""),
        f"changelist (page {page // 100})": lambda: get(f"p={page // 100}"),
        "filter by status": lambda: get("status=SUCCESS"),
        "filter by status + worker": lambda: get(
            "status=SUCCESS&worker=celery%40worker-03"
        ),
        "sort by task_name": lambda: get("o=3"),
        "search (?q=)": lambda: get("q=task_07"),
        "date hierarchy (year)": lambda: get("date_done__year=2026"),
        "facets": lambda: get("_facets="),
    }


def run(driver, count, search, app):
    prefix = f"bench{driver}"
    settings = {
        "DRIVER": driver,
        "URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "KEY_PREFIX": prefix,
    }
    if search:
        settings["SEARCH"] = "redisearch"
    with override_settings(DJANGO_CELERY_RESULTS_REDIS=settings):
        client = Client()
        client.force_login(User.objects.get(username="bench"))
        print(f"\n== {driver}{' + search' if search else ''}, {count} records")
        print(f"{'populate':<28}{populate(count) / 1000:8.2f} s")
        print(f"{'store_result':<28}{store_result_rate(app):8.0f} results/s")
        used, keys = memory_used(get_client(), prefix)
        print(f"{'memory per result':<28}{used / count:8.0f} bytes")
        print(f"{'keys per result':<28}{keys / count:8.2f}")
        for name, measure in admin_measurements(client, count).items():
            print(f"{name:<28}{timed(measure):8.1f} ms")
        get_client().unlink(*get_client().keys(f"{prefix}:*"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20_000)
    parser.add_argument("--drivers", nargs="+", default=["raw", "indexed", "redis_om"])
    parser.add_argument("--search", action="store_true", help="enable the search index")
    options = parser.parse_args()

    setup_test_environment()
    from django.test.runner import DiscoverRunner

    runner = DiscoverRunner(verbosity=0, interactive=False)
    databases = runner.setup_databases()
    User.objects.create_superuser("bench", "bench@example.com", "bench")

    from celery import Celery

    app = Celery("bench")
    app.conf.result_backend = "django_celery_results_redis.backends:RedisResultBackend"
    app.conf.result_serializer = "json"
    try:
        for driver in options.drivers:
            run(driver, options.records, options.search, app)
    finally:
        runner.teardown_databases(databases)


if __name__ == "__main__":
    main()
