# django-celery-results-redis

A Celery result backend for Django that stores task, group and chord results in
Redis, with the behaviour, models, manager API and admin of
[django-celery-results](https://github.com/celery/django-celery-results) 2.6.0.

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django_celery_results_redis",
]

DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}

CELERY_RESULT_BACKEND = "django-redis-db"
```

```python
from django_celery_results_redis.models import TaskResult

TaskResult.objects.filter(status="FAILURE", date_done__gte=yesterday)
```

## What it does

* **Keeps the admin.** The changelist, list filters, facet counts, search, date
  hierarchy, change form, delete and bulk actions and history are Django's own
  code running on a Redis-backed queryset. See [parity](admin-parity.md).
* **Keeps the models and managers.** `TaskResult`, `GroupResult` and
  `ChordCounter` have the same fields and methods as upstream, so most code
  only changes an import. See [migration](migration.md).
* **Keeps the backend behaviour.** `result_extended`, `result_expires`, groups,
  chords, `task_track_started` and retries work as they do with the database
  backend.
* **Takes the database out of the write path.** Workers talk to Redis only.
* **Offers three storage drivers.** `raw`, `indexed` and `redis_om`, plus an
  optional RediSearch index for the admin search. See [drivers](drivers.md).

## Requirements

| | Supported |
| --- | --- |
| Python | 3.10, 3.11, 3.12, 3.13, 3.14 |
| Django | 5.2, 6.0, 6.1 |
| Celery | 5.5, 5.6 |
| Redis | 7.0 and later; Redis 8 or Redis Stack for the `redis_om` driver and the search index |

## Where to go next

* [Installation](installation.md) sets the package up in a project.
* [Migrating from django-celery-results](migration.md) moves an existing one
  over, results included.
* [Examples](examples.md) shows the code you are likely to write around it.
* [Settings](settings.md) lists every option and system check.
* [Performance](performance.md) gives the numbers per driver and the memory
  each one costs.
