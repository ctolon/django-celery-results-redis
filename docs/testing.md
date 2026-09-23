# Testing

## Running the suite

```console
$ docker compose up -d --wait
$ uv run pytest -n auto
```

The tests need a Redis 8 server (it includes RediSearch). `REDIS_URL` selects
it, default `redis://localhost:6379/0`. Each test uses a fresh random key prefix
and deletes its keys and RediSearch indexes on teardown, so the suite does not
flush the database and can share a server with other data or run in parallel.

If the server has no RediSearch, `redis_om` tests are skipped.

## Layout

| Directory | Scope |
| --- | --- |
| `tests/unit/` | settings, key names, codec, Q compiler, evaluator, planner, checks |
| `tests/store/` | driver contract and cross-driver equivalence (Hypothesis) |
| `tests/orm/` | `RedisQuerySet` API, model save/delete semantics, managers |
| `tests/backend/` | the Celery backend, ported from upstream `test_database.py` |
| `tests/admin/` | admin parity: static options, changelist/change/delete/actions/history, differential rendering against upstream |
| `tests/integration/` | a real Celery worker thread, chords, groups |
| `tests/test_views.py` | JSON views |
| `tests/test_import_command.py` | importing django-celery-results rows |

Every test that touches storage runs once per driver through the `store_driver`
fixture.

## Benchmarks

`benchmarks/benchmark.py` measures the write path, the memory per result and
the admin changelist per driver. `benchmarks/soak.py` writes results while the
changelist is read and checks the pages that come back. Both are described in
[performance](performance.md).

## tox

```console
$ uvx --with tox-uv tox
```

runs the Python × Django × Celery matrix and `lint` (ruff). Integration tests with
a real worker thread are marked `integration`; deselect them with
`-m "not integration"`.
