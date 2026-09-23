# Performance

## Measuring

```console
$ uv run python benchmarks/benchmark.py --records 100000 --drivers indexed --search
```

The script fills a key prefix with task results, then times the Celery write
path and a set of admin changelist requests through Django's test client. Each
figure is the median of five requests.

## Numbers

Redis 8 in Docker on a developer laptop, 20 000 stored results, admin page size
100. The host was running other workloads, so treat these as orders of
magnitude, not benchmarks of Redis.

| | `raw` | `indexed` | `redis_om` |
| --- | --- | --- | --- |
| Storing results (`store_result`) | ~2 800/s | ~2 800/s | ~2 400/s |
| Changelist, first page | 2 160 ms | 40 ms | 134 ms |
| Changelist, page 100 | 2 020 ms | 37 ms | 112 ms |
| Filter by status | 1 970 ms | 46 ms | 85 ms |
| Sort by task name | 2 030 ms | 36 ms | 110 ms |
| Date hierarchy (year) | 1 950 ms | 61 ms | 139 ms |
| Facet counts | 3 910 ms | 96 ms | 143 ms |
| Search (`?q=`), no search index | 1 870 ms | 870 ms | 1 130 ms |
| Search (`?q=`), `SEARCH="redisearch"` | not supported | 92 ms | 169 ms |

With the `indexed` driver the cost of a page stays flat as the data grows,
because counting, sorting and paging happen inside Redis. At 100 000 results the
same page takes about the same time as at 20 000; filtering by a value grows
with the size of that value's index, not with the whole dataset.

The `raw` driver reads every record for every query, which is what the numbers
above show; it is meant for development and small deployments.

## Memory

Bytes of Redis memory per stored result, including the driver's indexes,
measured over 10 000 results whose `task_args`, `task_kwargs` and `result` are
short JSON strings. Your results' payload sizes move the record part of this
up or down; the index part stays roughly constant.

| | `raw` | `indexed` | `redis_om` |
| --- | --- | --- | --- |
| Records only | 398 | 1 103 | 501 |
| Records and indexes | 398 | ~1 400 | ~1 400 |
| With `SEARCH="redisearch"` | n/a | ~2 100 | ~2 100 |

The `indexed` driver keeps a sorted-set entry per indexed field, which is what
makes it about three times the size of the records alone. The search index adds
the trigrams of the searchable fields, which is the largest single cost: enable
it when searching matters, and keep `SEARCH_MAX_TEXT` low if `task_args` or
`task_kwargs` are large.

Budget roughly 1.5 KB per result with `indexed`, or 2 KB with the search index,
and keep `result_expires` set so the total stays bounded.

## Under concurrent load

A 25-second soak per driver, one writer storing results through the backend
while three threads page, filter and search the changelist:

| | writes/s | lost writes | inconsistent reads |
| --- | --- | --- | --- |
| `indexed` | 328 | 0 | 0 |
| `indexed` + search | 438 | 0 | 0 |
| `redis_om` | 310 | 0 | 0 |

Every stored result was readable afterwards, and every page was correctly
ordered and filtered. The write rate here is far below the figures above
because the readers compete for the same connection pool and process.

Reads are not a snapshot: a count and the page that follows it are two
queries, so they can disagree while results are being written. That is also
true of the database backend outside a transaction.

## What costs what

* **Storing a result** is one Lua call: one round trip per state change. The
  search index makes writes noticeably more expensive, because the trigrams of
  the changed fields are written with them.
* **Reading a result by id** is one `HMGET`.
* **A changelist page** issues a handful of queries: two counts, the page
  itself, the choices of each list filter, and the date hierarchy. With the
  `indexed` driver those are `ZCARD`/`ZINTERCARD`, a range read on a sorted set,
  and a read of the distinct-value sets.
* **Searching** without the search index reads every record the other filters
  left over. With `SEARCH="redisearch"` it reads only the records whose
  trigrams match; see [drivers](drivers.md#search-index).
* **Facet counts** (`?_facets=`) are one count per filter choice. They are
  answered from the indexes when the filter is an intersection of index keys.

## Rules of thumb

* Use `indexed` for anything that the admin will be used on.
* Turn on the search index if people search the changelist, and accept slower
  writes.
* Keep `result_expires` set and let Celery's `backend_cleanup` task run, so the
  indexes stay proportional to the results you actually keep.
* Watch `maxmemory-policy`: evicting result keys leaves index entries behind
  (they are repaired on read, but counts are off until then).
