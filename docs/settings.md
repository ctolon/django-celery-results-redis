# Settings

All settings live in one dictionary:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}
```

| Key | Default | Description |
| --- | --- | --- |
| `DRIVER` | `"indexed"` | `"raw"`, `"indexed"`, `"redis_om"`, or the dotted path of a `Store` subclass. See [drivers](drivers.md). |
| `URL` | `"redis://localhost:6379/0"` | Passed to `redis.Redis.from_url()`. A `sentinel://host:port;sentinel://host:port/0` URL (the format Celery uses) connects through Redis Sentinel and follows the current master. |
| `CLIENT_KWARGS` | `{}` | Extra keyword arguments for `redis.Redis.from_url()`. `decode_responses` is always `True`. |
| `CLIENT_FACTORY` | `None` | Dotted path of a callable returning a `redis.Redis` client created with `decode_responses=True`. Takes precedence over `URL` (Sentinel, custom pools, ...). |
| `KEY_PREFIX` | `"dcrr"` | Prefix of every key the package writes. Must not contain `:`, `*`, `?`, `[`, `]` or whitespace. |
| `ALLOW_EDITS` | `False` | Makes the admin change form editable. Falls back to `DJANGO_CELERY_RESULTS["ALLOW_EDITS"]`. |
| `TASK_ID_MAX_LENGTH` | `255` | `max_length` of `task_id`, `task_name` and `group_id`. Falls back to `DJANGO_CELERY_RESULTS_TASK_ID_MAX_LENGTH`. Read when models are loaded. |
| `SCAN_COUNT` | `1000` | `COUNT` hint for `SCAN`. |
| `BATCH_SIZE` | `500` | Number of records fetched, deleted or indexed per round trip. |
| `RESULT_TTL` | `None` | Seconds, or a `timedelta`. Sets a TTL on result and group keys on every write. `raw` and `redis_om` only. |
| `ID_FIRST_URLS` | `False` | Also register the deprecated `<task_id>/done/` style URLs. The original package defaults to `True`. |
| `SEARCH` | `None` | `"redisearch"` builds a trigram index so that the admin search and `icontains` lookups are answered by RediSearch instead of reading every record. Needs RediSearch and database 0. See [drivers](drivers.md#search-index). |
| `SEARCH_MAX_TEXT` | `4096` | Values longer than this are not tokenized; searches on them read the record instead. |
| `SEARCH_MAX_CANDIDATES` | `50000` | When a search term matches more records than this, the index is skipped and every record is read. |
| `SENTINEL_MASTER_NAME` | `"mymaster"` | Master name used with a `sentinel://` URL. |
| `SENTINEL_KWARGS` | `{}` | Extra keyword arguments for the sentinel connections (passwords, TLS, ...). |

Settings are read lazily, so `django.test.override_settings` works, and changing
the setting drops the cached client and store instances.

## Celery

```python
# settings.py
CELERY_RESULT_BACKEND = "django-redis-db"
```

`django-redis-db` is registered in the `celery.result_backends` entry point
group. The class path works too:
`"django_celery_results_redis.backends:RedisResultBackend"`. The backend reads
its connection from `DJANGO_CELERY_RESULTS_REDIS`; anything after the alias in
the result backend URL is ignored.

Celery options that the backend honours, exactly like the database backend:
`result_extended`, `result_expires`, `result_chord_join_timeout`,
`result_backend_always_retry` (retries on Redis connection and timeout errors),
`task_track_started`.

## System checks

| Id | Level | Condition |
| --- | --- | --- |
| `django_celery_results_redis.E001` | error | Unknown `DRIVER`, or a dotted path that is not a `Store` subclass. |
| `django_celery_results_redis.E002` | error | `DRIVER` is `redis_om` but `redis-om` is not installed. |
| `django_celery_results_redis.E003` | error | RediSearch is in use (`redis_om` driver or `SEARCH`) and `URL` selects a database other than 0. |
| `django_celery_results_redis.E004` | error | Invalid `KEY_PREFIX`. |
| `django_celery_results_redis.E005` | error | `ALLOW_EDITS` is not a boolean. |
| `django_celery_results_redis.E006` | error | `RESULT_TTL` is set with the `indexed` driver, or is not a positive number of seconds. |
| `django_celery_results_redis.E007` | error | `SEARCH` is neither `None` nor `"redisearch"`. |
| `django_celery_results_redis.E101` | error | Redis is unreachable. |
| `django_celery_results_redis.E102` | error | RediSearch is in use but the server does not have it. |
| `django_celery_results_redis.E103` | error | Redis server older than 7.0. |
| `django_celery_results_redis.W101` | warning | `maxmemory-policy` evicts arbitrary keys (`allkeys-*`), which can drop results and desynchronise indexes. |

`E1xx`/`W1xx` connect to Redis, so they are registered under the `database` tag
and only run with `manage.py check --database default`.

## Management commands

| Command | Purpose |
| --- | --- |
| `celery_results_redis_import` | Copy results from the django-celery-results tables into Redis. See the [migration guide](migration.md). |
| `celery_results_redis_rebuild_index` | Rebuild the driver's indexes and the search index from the stored records. Run it after switching drivers or enabling `SEARCH`. |

## Storage format

The records under a key prefix carry a format version, kept in `<prefix>:meta`.
A release that writes a different format refuses to read the data and says so,
rather than returning wrong results. To move to such a release, let the old
results expire, delete them, or point `KEY_PREFIX` somewhere else.

## Redis Cluster

Not supported. Celery's own Redis result backend does not support cluster mode
either. The Lua scripts and the index operations address several keys at once,
which a cluster would have to have in the same slot.
