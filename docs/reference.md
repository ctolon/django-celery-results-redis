# API reference

The public surface, from the models most projects touch down to the drivers.
[architecture](architecture.md) explains how the layers fit together.

## Models

Unmanaged Django models whose records live in Redis. `task_id` and `group_id`
are the primary keys. Field names, verbose names, help texts and `Meta` options
match django-celery-results; see [parity](admin-parity.md).

::: django_celery_results_redis.models
    options:
      show_source: false
      show_root_heading: false
      members:
        - TaskResult
        - GroupResult
        - ChordCounter
      filters:
        - "!^_"
        - "!^redis_"
        - "!^Meta$"

## Managers

`TaskResult.objects`, `GroupResult.objects` and `ChordCounter.objects`. They
carry the methods the backend calls, plus `delete_expired()` for cleanup.

::: django_celery_results_redis.managers
    options:
      show_source: false
      show_root_heading: false
      members:
        - ResultManager
        - TaskResultManager
        - GroupResultManager
        - ChordCounterManager
        - transaction_retry
      filters:
        - "!^_"

## Backend

The Celery result backend. Registered as `django-redis-db` in the
`celery.result_backends` entry point group, so
`CELERY_RESULT_BACKEND = "django-redis-db"` is enough.

::: django_celery_results_redis.backends.redis
    options:
      show_source: false
      show_root_heading: false
      members:
        - RedisResultBackend
        - trigger_callback
      filters:
        - "!^_"

## QuerySet

What the managers return. It subclasses `django.db.models.QuerySet` and
implements the parts that can be answered from Redis; everything else raises
`NotSupportedError`. See [parity](admin-parity.md) for the supported lookups.

::: django_celery_results_redis.store.queryset
    options:
      show_source: false
      show_root_heading: false
      members:
        - RedisQuerySet
        - RedisQuery
        - RedisManager
      filters:
        - "!^_"

## Stores

One store per model, built from the settings by
`django_celery_results_redis.store.get_store()`. `Store` is the base and the
`raw` driver; the other two add query pushdown. A dotted path to a `Store`
subclass is a valid value for the `DRIVER` setting, which is how a project can
add its own.

::: django_celery_results_redis.store
    options:
      show_source: false
      show_root_heading: false
      members:
        - get_store
        - get_client
        - create_client
        - create_sentinel_client
        - check_format_version
        - reset
      filters:
        - "!^_"

::: django_celery_results_redis.store.drivers.base.Store
    options:
      show_source: false
      show_root_heading: true
      filters:
        - "!^_"

::: django_celery_results_redis.store.drivers.indexed.IndexedStore
    options:
      show_source: false
      show_root_heading: true
      members:
        - can_push
        - selection
        - repair
        - rebuild
        - distinct_values
      filters:
        - "!^_"

::: django_celery_results_redis.store.drivers.redis_om.RedisOMStore
    options:
      show_source: false
      show_root_heading: true
      members:
        - prepare
        - ensure_index
        - rebuild
        - can_push
        - selection
        - distinct_values
      filters:
        - "!^_"

## Search index

Optional. Built when `SEARCH` is set, used by every driver. See
[drivers](drivers.md#search-index).

::: django_celery_results_redis.store.search
    options:
      show_source: false
      show_root_heading: false
      members:
        - SearchIndex
        - tokens
      filters:
        - "!^_"

## Settings

`app_settings` reads `DJANGO_CELERY_RESULTS_REDIS` on every access, so
`override_settings` works in tests. [settings](settings.md) documents each key.

::: django_celery_results_redis.conf
    options:
      show_source: false
      show_root_heading: false
      members:
        - AppSettings
        - SETTING_NAME
        - DEFAULTS
      filters:
        - "!^_"
