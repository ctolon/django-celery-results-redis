"""Store results while the changelist is read, and check what comes back.

    $ uv run python benchmarks/soak.py

Reports the write rate, whether every write is readable afterwards, and whether
any page came back mis-ordered, duplicated or wrongly filtered.
"""

import os
import sys
import threading
import time
import uuid

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
django.setup()

from django.test.utils import override_settings  # noqa: E402

from django_celery_results_redis.models import TaskResult  # noqa: E402
from django_celery_results_redis.store import get_client  # noqa: E402

DURATION = 25
READERS = 3
PAGE = 50


def index_bytes(client, prefix):
    """Bytes held by the RediSearch indexes of ``prefix``."""
    total = 0
    for name in client.execute_command("FT._LIST"):
        if name.startswith(f"{prefix}:"):
            info = client.ft(name).info()
            total += float(info.get("total_index_memory_sz_mb") or 0) * 1024**2
    return total


def read_pages(errors, done):
    """Check what the changelist returns, until ``done`` is set."""
    while not done.is_set():
        try:
            page = list(TaskResult.objects.all()[:PAGE])
            ids = [row.task_id for row in page]
            if len(page) > PAGE:
                errors.append(f"page of {len(page)} rows")
            if len(set(ids)) != len(ids):
                errors.append("duplicate rows in one page")
            dates = [row.date_done for row in page]
            if dates != sorted(dates, reverse=True):
                errors.append("page not ordered by date_done")
            found = TaskResult.objects.filter(task_name__icontains="task_3")[:20]
            if any("task_3" not in row.task_name for row in found):
                errors.append("search returned a non-match")
            filtered = TaskResult.objects.filter(worker="w1")[:20]
            if any(row.worker != "w1" for row in filtered):
                errors.append("filter returned a non-match")
        except Exception as exc:
            errors.append(repr(exc))


def write_results(deadline):
    written = 0
    while time.monotonic() < deadline:
        for _ in range(50):
            TaskResult.objects.store_result(
                "application/json",
                "utf-8",
                f"t-{written:07}",
                '"x"',
                "SUCCESS",
                task_name=f"app.task_{written % 5}",
                worker=f"w{written % 3}",
            )
            written += 1
    return written


def clear(client, prefix):
    for key in client.scan_iter(match=f"{prefix}:*", count=1000):
        client.unlink(key)
    for name in client.execute_command("FT._LIST"):
        if name.startswith(f"{prefix}:"):
            client.ft(name).dropindex(delete_documents=False)


def soak(driver, search):
    """Write for ``DURATION`` seconds while readers page the changelist."""
    prefix = f"soak{uuid.uuid4().hex[:8]}"
    settings = {"DRIVER": driver, "KEY_PREFIX": prefix}
    if search:
        settings["SEARCH"] = "redisearch"
    with override_settings(DJANGO_CELERY_RESULTS_REDIS=settings):
        client = get_client()
        errors, done = [], threading.Event()
        readers = [
            threading.Thread(target=read_pages, args=(errors, done))
            for _ in range(READERS)
        ]
        for reader in readers:
            reader.start()
        written = write_results(time.monotonic() + DURATION)
        done.set()
        for reader in readers:
            reader.join()

        stored = TaskResult.objects.count()
        used = sum(
            client.memory_usage(key) or 0
            for key in client.scan_iter(match=f"{prefix}:*", count=1000)
        )
        name = driver + ("+search" if search else "")
        print(
            f"{name:<16}{written / DURATION:6.0f} writes/s  "
            f"lost writes {written - stored}  "
            f"inconsistent reads {len(errors)}  "
            f"{(used + index_bytes(client, prefix)) / max(stored, 1):5.0f} bytes/result"
        )
        for error in errors[:3]:
            print(f"    {error}")
        clear(client, prefix)


if __name__ == "__main__":
    for driver, search in (
        ("indexed", False),
        ("indexed", True),
        ("redis_om", False),
    ):
        soak(driver, search)
