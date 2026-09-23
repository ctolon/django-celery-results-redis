# django-celery-results-redis

A Celery result backend for Django that stores task, group and chord results in
Redis, with the behaviour, models, manager API and admin of
[django-celery-results](https://github.com/celery/django-celery-results).

* Drop-in backend semantics: `result_extended`, `result_expires`, groups,
  chords, `task_track_started`, retries.
* Django admin identical to django-celery-results: list filters, facets,
  search, sorting, date hierarchy, change form, delete and bulk actions,
  history.
* Three storage drivers: `raw`, `indexed` (own secondary indexes) and
  `redis_om` (RediSearch), plus an optional RediSearch index for the admin
  search.
* Redis Sentinel supported through a `sentinel://` URL.
* Workers never touch the SQL database.

## Requirements

Python 3.10+, Django 5.2+, Celery 5.5+, Redis 7.0+ (Redis 8 or Redis Stack for
the `redis_om` driver).

## Installation

```console
$ pip install django-celery-results-redis
$ pip install "django-celery-results-redis[redis-om]"   # for the redis_om driver
```

```python
INSTALLED_APPS = [
    ...,
    "django_celery_results_redis",
]

DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}

CELERY_RESULT_BACKEND = "django-redis-db"
CELERY_RESULT_EXTENDED = True
```

```console
$ python manage.py migrate django_celery_results_redis
```

The migration creates no table; it registers the models so that content types
and admin permissions exist.

Optional JSON status views:

```python
urlpatterns = [
    path("celery/", include("django_celery_results_redis.urls")),
]
```

## Migrating from django-celery-results

Both packages can be installed side by side. Point `CELERY_RESULT_BACKEND` at
`django-redis-db`, copy the rows you want to keep with
`manage.py celery_results_redis_import`, and restart the workers; see the
[migration guide](docs/migration.md).

## Documentation

Full documentation: <https://ctolon.github.io/django-celery-results-redis/>

* [Installation](https://ctolon.github.io/django-celery-results-redis/installation/)
* [Migrating from django-celery-results](https://ctolon.github.io/django-celery-results-redis/migration/)
* [Examples](https://ctolon.github.io/django-celery-results-redis/examples/)
* [Settings and system checks](https://ctolon.github.io/django-celery-results-redis/settings/)
* [Storage drivers](https://ctolon.github.io/django-celery-results-redis/drivers/)
* [Performance](https://ctolon.github.io/django-celery-results-redis/performance/)
* [Parity with django-celery-results](https://ctolon.github.io/django-celery-results-redis/admin-parity/)

## Contributing

```console
$ uv sync
$ docker compose up -d --wait
$ uv run pytest -n auto
```

See the [contributing guide](https://ctolon.github.io/django-celery-results-redis/contributing/).

## License

BSD 3-Clause, the same license as Django, Celery and django-celery-results.
Parts of the backend, models, managers and views are derived from
django-celery-results; the derived files and their copyright holders are listed
in [LICENSE](LICENSE).
