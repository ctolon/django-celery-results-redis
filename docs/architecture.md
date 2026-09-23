# Architecture

`django_celery_results_redis` is a Celery result backend for Django projects that
keeps task, group and chord state in Redis. It mirrors
[django-celery-results](https://github.com/celery/django-celery-results) 2.6.0:
the same backend behaviour, the same model fields and manager API, and an admin
that renders and behaves like the original one.

## Layers

```
Celery worker / AsyncResult
    |
    v
backends.redis.RedisResultBackend     port of django_celery_results DatabaseBackend
    |
    v
models (managed=False) + managers     TaskResult, GroupResult, ChordCounter
    |
    v
store.queryset.RedisQuerySet          models.QuerySet subclass used by the admin
    |   Q objects / kwargs
    v
store.query.compiler                  Q into a lookup tree, values validated eagerly
store.query.evaluator                 evaluation, ordering, aggregates, truncation
    |
    v
store.drivers.{raw,indexed,redis_om}  persistence and query pushdown
store/lua/store.lua                   atomic writes, deletes, renames, chord counters
```

### Models

The models are unmanaged Django models. No table is ever created, but Django
still knows their fields, so `ModelAdmin`, `ModelForm`, content types,
permissions and `LogEntry` history work unchanged. Persistence is redirected by
overriding `Model._save_table()` and `Model.delete()`: `save()`/`save_base()`
keep their normal flow, including `pre_save`/`post_save` signals, `update_fields`
and the `created` flag.

`task_id` (and `group_id`) is the primary key. The original package uses an
integer `id` plus a unique `task_id`; an extra integer would need a counter and a
second lookup key in Redis for no functional gain.

### QuerySet

`RedisQuerySet` subclasses `django.db.models.QuerySet` because parts of the admin
check `isinstance(obj, QuerySet)`. Its `query` attribute is a small
`RedisQuery` that carries the lookup AST, ordering and slice bounds together with
the attributes the admin inspects (`order_by`, `select_related`,
`default_ordering`, ...). Every public `QuerySet` method is either implemented or
raises `NotSupportedError`; a test enforces that list against the installed
Django version.

Lookup values are prepared with `Field.get_prep_value()` when `filter()` is
called, so invalid admin URL parameters fail with `ValidationError`/`ValueError`
at the same point Django's SQL backend fails, and the admin shows its usual
`?e=1` redirect.

### Evaluator

`store.query.evaluator` is the reference implementation of lookup semantics. The
`raw` driver uses it for everything; the other drivers use it for whatever part
of a query they cannot express in Redis. The equivalence test suite compares
every driver with it.

## Storage layout

With the default `KEY_PREFIX` of `dcrr`:

| Key | Type | Content |
| --- | --- | --- |
| `dcrr:task:<task_id>` | hash | one `TaskResult` |
| `dcrr:group:<group_id>` | hash | one `GroupResult` |
| `dcrr:chord:<group_id>` | hash | one `ChordCounter` |
| `dcrr:idx:<kind>:...` | sorted sets, sets, hashes | secondary indexes and value counters |
| `dcrr:ft:<kind>` | RediSearch index | `redis_om` driver only |
| `dcrr:fts:<kind>` | RediSearch index | search index, when `SEARCH` is set |
| `dcrr:meta` | hash | the storage format version |

Hash fields are the Django field names. Values are text:

* datetimes are integer microseconds since the Unix epoch (UTC), so they
  round-trip exactly and can be used as sorted-set scores and numeric index
  values;
* `None` is represented by the absence of the field; an empty string is stored
  as an empty string;
* `null_fields` holds a comma separated list of the nullable fields that are
  `None`, which lets RediSearch answer `isnull` lookups.

Records can carry index fields beside their own: the trigrams of the searchable
fields when `SEARCH` is set (`g_<field>`), and an encoded copy of each tag value
under the `redis_om` driver (`t_<field>`). Reads ask for the record's own fields
by name, so those never travel with it.

All drivers read and write this same layout, so switching drivers only requires
building the new driver's indexes (`manage.py celery_results_redis_rebuild_index`).

## Writes and consistency

Every write goes through one Lua script (`store/lua/store.lua`). A save reads
the previous state, applies the new values, removes cleared fields, sets
`date_created` only when the record is new, updates secondary indexes and
returns the stored record, atomically. Chord counters are decremented by the same
script, which replaces the `SELECT ... FOR UPDATE` used by the database backend.

Redis writes do not take part in Django database transactions. When the admin
wraps a change in `transaction.atomic()`, a rollback affects the `LogEntry` row
but not the Redis write.

The `indexed` driver keeps its indexes consistent as long as records are only
changed through this package. A record removed behind its back (manual `DEL`,
eviction) leaves index entries that point nowhere; reads skip and repair those
entries, and the rebuild command recreates all indexes from the records.

## Expiry

As with the database backend, results are not expired by Redis by default.
`backend.cleanup()` (run by Celery beat's `celery.backend_cleanup` task) and
`TaskResult.objects.delete_expired()` remove results older than
`result_expires`. The `raw` and `redis_om` drivers can additionally set a key TTL
through `RESULT_TTL`.
