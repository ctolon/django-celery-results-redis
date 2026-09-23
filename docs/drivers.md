# Storage drivers

The driver is chosen with `DJANGO_CELERY_RESULTS_REDIS["DRIVER"]`. All drivers
store records in the same format and return the same results for the same query;
they differ in how much work Redis does for them.

| | `raw` | `indexed` | `redis_om` |
| --- | --- | --- | --- |
| Extra dependency | none | none | `redis-om` (`pip install django-celery-results-redis[redis-om]`) |
| Redis server | any Redis ≥ 7.0 | Redis ≥ 7.0 | Redis 8, or Redis Stack with RediSearch |
| Extra memory | none | a sorted set entry per indexed field | RediSearch index |
| Result write | 1 script call | 1 script call (updates indexes) | 1 script call (RediSearch indexes the hash) |
| Result read by id | `HMGET` | `HMGET` | `HMGET` |
| Admin changelist | `SCAN` of every record | counting, sorting and paging in Redis | counting, sorting and paging with `FT.SEARCH` |
| Text search (`?q=`) | see [search index](#search-index) | see [search index](#search-index) | see [search index](#search-index) |
| `RESULT_TTL` | supported | not supported | supported |

The Celery side (storing and reading results, groups, chords) costs the same for
every driver. The driver choice matters for the admin and for code that queries
`TaskResult.objects`.

## `raw`

Records only, no secondary structures. Every query scans all records of the
model with `SCAN` and pipelined `HGETALL`, then filters, sorts and slices in
memory. A changelist page runs several such queries (count, page, filter
choices, date hierarchy). Suitable for development and for deployments that
keep a small number of results (roughly up to ten thousand).

## `indexed`

Maintains, per model, inside the write script:

| Key | Type | Members |
| --- | --- | --- |
| `idx:<kind>:z:<date field>` | sorted set | primary keys scored by the datetime |
| `idx:<kind>:s:<field>:<value>` | sorted set | primary keys with that value, ordered by primary key |
| `idx:<kind>:v:<field>` | set | distinct values of the field |
| `idx:<kind>:n:<field>` | sorted set | primary keys where the field is `None` |
| `idx:<kind>:all` | sorted set | every primary key |
| `idx:<kind>:c:<field>` | hash | records per distinct value, `redis_om` driver only |

Indexed fields: `status`, `task_name`, `periodic_task_name`, `worker` (tag
fields) and `date_created`, `date_done`, `date_started` (dates) for task results;
`date_created`, `date_done` for group results.

Pushed down to Redis:

* `exact` and `in` on tag fields and on the primary key;
* `isnull` on any indexed field;
* `exact`, `gt`, `gte`, `lt`, `lte`, `range` on date fields, plus `__year` and
  `__date` combined with those comparisons;
* `AND`, `OR` and `NOT` of the above.

Any other condition is evaluated in memory over the records selected by the
pushed-down part.

When the whole filter is pushed down, these run in Redis and their cost does
not grow with the number of stored results:

* paging ordered by `date_done` or `date_created`, the admin default;
* paging ordered by a tag field, which walks that field's values in order and
  reads only the groups the page overlaps;
* counting, including the facet counts of a changelist: a filter that is an
  intersection of index keys is counted with `ZCARD`/`ZINTERCARD`, without
  building a temporary key;
* filters that only restrict the ordering field, such as the date hierarchy and
  the date filters, which read the field's index between two scores;
* distinct values for list filters, from the `v:` sets.

## `redis_om`

Uses a [redis-om](https://github.com/redis/redis-om-python) `HashModel` per
Django model to declare a RediSearch index over the record hashes. Tag fields
are case-sensitive, sortable `TAG` fields; datetimes are `NUMERIC` fields holding
epoch microseconds. Writes do not go through `HashModel.save()`, which never
removes fields set to `None`; they use the shared write script, and RediSearch
indexes the hash on its own.

Tag fields are indexed through an encoded copy of their value (`t_<field>`, the
value in URL-safe base64), because RediSearch splits tag values on a separator
and trims them, which would make some values unmatchable. Every value is
therefore matched exactly.

The distinct values of a field (the choices of a list filter) come from a
counter kept per value by the write script, not from the search index:
RediSearch aggregations can miss a document that was just written.

Pushed down: the same lookups as `indexed`. Counting uses
`FT.SEARCH ... LIMIT 0 0`; paging ordered by a date field sorts in RediSearch,
and paging ordered by a tag field walks that field's values like the `indexed`
driver does.

Searches never ask RediSearch to load documents: results that span a paused
query (a cursor read, or documents loaded on a worker thread) are not a
consistent snapshot, and rows can go missing. Only document ids are returned,
and the records are read with `HMGET`.

RediSearch only indexes database 0, so the Redis URL must select database 0.

## Search index

`DJANGO_CELERY_RESULTS_REDIS["SEARCH"] = "redisearch"` adds a RediSearch index
that any driver can use to answer substring lookups (`icontains` and friends),
which is what the admin search box produces. Without it, a search reads every
record the other filters left over.

RediSearch cannot answer a substring match exactly: its wildcard matching is
capped by `MAXEXPANSIONS` (200 values by default) and would silently drop
results. The index therefore stores the trigrams of each searchable field. A
query asks for the records holding every trigram of the term, which is a
superset of the matches, and the lookup is then evaluated on those records. The
result is exactly what it would be without the index.

The searchable fields are the admin's `search_fields`: `task_id`, `task_name`,
`status`, `task_args` and `task_kwargs` for task results, `group_id` for group
results.

Costs and limits:

* writes carry the trigrams of the fields they change, which makes storing a
  result noticeably more expensive;
* terms shorter than three characters cannot use the index;
* values longer than `SEARCH_MAX_TEXT` are not tokenized, and searches on that
  field read those records;
* a term matching more than `SEARCH_MAX_CANDIDATES` records falls back to
  reading every record.

Run `manage.py celery_results_redis_rebuild_index` after enabling it to index
results stored earlier.

## Choosing

* `indexed` is the default and needs nothing beyond Redis.
* Pick `redis_om` if you already run Redis 8 / Redis Stack and use redis-om
  elsewhere.
* Pick `raw` when you want no extra keys at all and keep few results.

Switching drivers does not require migrating data. Run
`manage.py celery_results_redis_rebuild_index` after switching to `indexed` or
`redis_om` to index the existing records.
