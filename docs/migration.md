# Migrating from django-celery-results

This package stores results in Redis instead of your database. The models,
managers, admin, views and backend behave like django-celery-results 2.6.0, so
most projects only change settings. The differences are listed in
[parity](admin-parity.md).

Both packages can stay installed while you move over, which is what the steps
below assume.

## 1. Install and configure

```console
$ pip install django-celery-results-redis
```

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django_celery_results",  # keep for now
    "django_celery_results_redis",
]

DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}

CELERY_RESULT_BACKEND = "django-redis-db"  # was "django-db"
```

```console
$ python manage.py migrate django_celery_results_redis
```

The migration creates no table. It registers the models so their content types
and admin permissions exist.

Settings that keep working unchanged: `CELERY_RESULT_EXTENDED`,
`CELERY_RESULT_EXPIRES`, `CELERY_RESULT_BACKEND_ALWAYS_RETRY`,
`CELERY_TASK_TRACK_STARTED`, and `DJANGO_CELERY_RESULTS["ALLOW_EDITS"]`.

## 2. Copy the results you want to keep

```console
$ python manage.py celery_results_redis_import --dry-run
3421 task results imported.
12 group results imported.

$ python manage.py celery_results_redis_import --since 2026-01-01
```

The command reads the django-celery-results tables and writes the rows to
Redis with their original `date_created` and `date_done`. Running it again
overwrites the same ids, so it is safe to repeat: import the bulk first, then
run it once more for the rows written in between.

Options: `--since` (only results completed on or after a date), `--batch-size`,
`--database` (read from another alias) and `--dry-run`.

Skip this step if old results do not matter. Results expire anyway.

## 3. Restart the workers

Workers pick up the new backend on restart. From then on, results are written
to Redis and never to the database, which also means workers no longer need a
database connection.

Both admins are visible while both apps are installed: "Celery Results" reads
the old rows, "Celery Results (Redis)" the new ones.

## 4. Remove the old package

Once you no longer need the old rows:

```python
INSTALLED_APPS = [
    ...,
    "django_celery_results_redis",
]
```

```console
$ python manage.py migrate django_celery_results zero   # drops the tables
$ pip uninstall django-celery-results
```

## Code that queries results

Imports change, the queries do not:

```python
# before
from django_celery_results.models import TaskResult

# after
from django_celery_results_redis.models import TaskResult

TaskResult.objects.filter(status="FAILURE", date_done__gte=yesterday)
TaskResult.objects.get(task_id=result.id).as_dict()
TaskResult.objects.delete_expired(timedelta(days=7))
```

Two things to check in existing code:

* **`task_id` is the primary key.** There is no integer `id`, so
  `TaskResult.objects.get(id=...)`, `order_by("id")` and admin URLs built from
  an integer id need to use the task id instead.
* **Only the lookups in [parity](admin-parity.md) exist**, and there are no
  joins, annotations or `select_related()`. Anything else raises
  `NotSupportedError` or `FieldError` rather than failing silently.

## Rolling back

Set `CELERY_RESULT_BACKEND = "django-db"` and restart the workers. Results
written to Redis in the meantime stay there; nothing in the database was
changed. The Redis keys can be dropped with the key prefix:

```console
$ redis-cli --scan --pattern 'dcrr:*' | xargs -r redis-cli del
```

## Choosing what to turn on next

* [Drivers](drivers.md): `indexed` is the default and needs nothing extra;
  `redis_om` uses RediSearch.
* [Search index](drivers.md#search-index): makes the admin search fast on large
  result sets, at the cost of slower writes.
* [Performance](performance.md): what a changelist costs per driver.

Both are opt-in and can be enabled later; after enabling either, run:

```console
$ python manage.py celery_results_redis_rebuild_index
```
