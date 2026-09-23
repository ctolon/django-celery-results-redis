# FAQ

## Does this need a database?

Workers do not. Storing and reading a result touches Redis only, so a worker
process needs no database connection.

The admin does, because Django's admin needs sessions, users and the `LogEntry`
table. `migrate django_celery_results_redis` creates no table of its own; it
registers the models so that content types and permissions exist.

## Can I run it next to django-celery-results?

Yes. The app label, the models and the admin section are separate, so both can
be installed at the same time. That is how the [migration guide](migration.md)
moves a project over: install both, copy the rows, switch the backend, then
remove the old package.

Both packages register the same URL names in their `urls` module. Include only
one of them.

## What happens if Redis evicts keys?

Results disappear. With the `indexed` driver the index entries of an evicted
record are left behind; reads skip them and repair them, but counts are too high
until then. Run `celery_results_redis_rebuild_index` to fix the indexes in one
go.

Set `maxmemory-policy` to `noeviction` or a `volatile-*` policy for the database
that holds results. `manage.py check --database default` reports
`django_celery_results_redis.W101` when the policy can evict arbitrary keys.

## Why is my search slow?

Without the search index, a search reads every result that the other filters
left over and matches in Python. That is the only way to answer `icontains`
exactly with plain Redis.

Set `SEARCH = "redisearch"` and run
`manage.py celery_results_redis_rebuild_index`. See
[drivers](drivers.md#search-index) for what it costs, and
[performance](performance.md) for the numbers.

## Why is there no integer id?

`task_id` is the primary key. Redis is a key-value store, so the task id is
already the key; an extra integer would need a counter and a second lookup key
for every record, and would be a second source of truth.

Code that used the integer `id` has to use the task id, including admin URLs.
This is listed in [parity](admin-parity.md).

## Can I use Redis Cluster?

No. Celery's own Redis result backend does not support cluster mode either.
The Lua scripts and the index operations address several keys per call, which a
cluster would require to live in one slot.

Redis Sentinel is supported through a `sentinel://` URL; see
[settings](settings.md).

## What happens when results expire?

Nothing removes them on its own unless you ask for it. Two ways:

* `result_expires` plus Celery beat running `celery.backend_cleanup`, which is
  what the database backend does;
* `RESULT_TTL`, which lets Redis expire the keys, with the `raw` or `redis_om`
  driver.

`TaskResult.objects.delete_expired(timedelta(days=7))` does the same thing by
hand. See [examples](examples.md#expiring-results).

## How do I move between drivers?

Change `DRIVER` and run:

```console
$ python manage.py celery_results_redis_rebuild_index
```

All drivers read and write the same records, so nothing has to be converted. The
command builds the indexes the new driver needs. Until it finishes, queries can
miss results that were stored before the switch.

## How much memory does it use?

About 1.5 KB per result with the `indexed` driver, or 2 KB with the search
index, for results with short payloads. [performance](performance.md) has the
measured breakdown, and `benchmarks/benchmark.py` measures it for your own data.

## A query is slower than I expected

Check, in this order:

1. The driver. `raw` reads every record for every query. See
   [drivers](drivers.md).
2. Whether the filter can be pushed down. Lookups outside the list in
   [drivers](drivers.md) are evaluated in Python over the records the rest of
   the filter selected.
3. The ordering. Ordering by `date_done` or `date_created`, or by a tag field,
   pages inside Redis. Ordering by another field reads the matching records.
4. Search. Without the search index, `icontains` reads records.

`benchmarks/benchmark.py` reproduces the changelist queries against a dataset of
your size, and `benchmarks/soak.py` runs them while results are being written.

## Which Redis version do I need?

Redis 7.0 or later. The `redis_om` driver and the search index need RediSearch,
which means Redis 8 or Redis Stack, and database 0.
