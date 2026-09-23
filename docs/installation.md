# Installation

## Requirements

Python 3.10 or later, Django 5.2 or later, Celery 5.5 or later, and Redis 7.0
or later. The `redis_om` driver and the search index need Redis 8 or Redis
Stack, which include RediSearch.

## Install

```console
$ pip install django-celery-results-redis
```

The `redis_om` driver needs one extra dependency:

```console
$ pip install "django-celery-results-redis[redis-om]"
```

## Configure

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django.contrib.admin",
    "django_celery_results_redis",
]

DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}

CELERY_RESULT_BACKEND = "django-redis-db"
CELERY_RESULT_EXTENDED = True
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7
```

`django-redis-db` is registered in Celery's `celery.result_backends` entry
point group. The dotted path works too:

```python
CELERY_RESULT_BACKEND = "django_celery_results_redis.backends:RedisResultBackend"
```

The backend reads its connection from `DJANGO_CELERY_RESULTS_REDIS`. Anything
after the alias in the result backend URL is ignored.

`CELERY_RESULT_EXTENDED` stores the task name, arguments, worker and periodic
task name with each result, which the admin columns and filters use. Without it
those columns stay empty, exactly as with django-celery-results.

Every option is listed in [settings](settings.md).

## Migrate

```console
$ python manage.py migrate django_celery_results_redis
```

The migration creates no table. It registers the three models so their content
types and admin permissions exist, which is what the admin needs.

## Check the connection

```console
$ python manage.py check --database default
System check identified no issues (0 silenced).
```

The checks tagged `database` connect to Redis and report an unreachable server,
a missing RediSearch module, a server older than 7.0, or an eviction policy
that can drop results. They only run when `--database` is given. See
[settings](settings.md#system-checks).

## Try it

```console
$ celery -A proj worker -l info
```

```python
>>> from proj.tasks import add
>>> result = add.delay(2, 3)
>>> result.get(timeout=10)
5

>>> from django_celery_results_redis.models import TaskResult
>>> TaskResult.objects.get(task_id=result.id).status
'SUCCESS'
```

The results are now in the admin under "Celery Results (Redis)".

## Optional: the JSON status views

The four views of django-celery-results are ported unchanged:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("celery/", include("django_celery_results_redis.urls")),
]
```

```console
$ curl http://localhost:8000/celery/task/status/<task_id>/
{"task": {"id": "...", "status": "SUCCESS", "result": 5}}
```

## Optional: faster admin search

On a large result set the admin search reads every record. An index makes it
read only the matching ones:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
    "SEARCH": "redisearch",
}
```

```console
$ python manage.py celery_results_redis_rebuild_index
```

It needs RediSearch, and it makes writes more expensive. See
[the search index](drivers.md#search-index).

## Production notes

* Keep `CELERY_RESULT_EXPIRES` set and let Celery beat run
  `celery.backend_cleanup`, so the stored results and their indexes stay
  bounded.
* Set `maxmemory-policy` to `noeviction` or a `volatile-*` policy for the Redis
  instance holding results. An `allkeys-*` policy can evict result keys.
* Budget about 1.5 KB of Redis memory per result with the `indexed` driver, or
  2 KB with the search index. See [performance](performance.md#memory).
* Redis Sentinel is supported through a `sentinel://` URL. Redis Cluster is
  not; Celery's own Redis result backend does not support it either.
