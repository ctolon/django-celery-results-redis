# Contributing

## Setting up

The project uses [uv](https://docs.astral.sh/uv/) and a Redis 8 container.

```console
$ git clone https://github.com/ctolon/django-celery-results-redis
$ cd django-celery-results-redis
$ uv sync
$ docker compose up -d --wait
```

`uv sync` installs the package with the `test`, `lint` and `docs` dependency
groups. The test suite needs a running Redis; `REDIS_URL` selects it and
defaults to `redis://localhost:6379/0`.

`uv.lock` is committed. It pins the development and documentation tools, not
what people installing the package get: their resolver uses the ranges in
`pyproject.toml`. After changing a dependency, run `uv lock` and commit the
result, or CI fails with a stale lockfile.

## Running the tests

```console
$ uv run pytest -n auto
$ uv run pytest -m "not integration"      # skip the worker tests
$ uv run pytest tests/admin -k changelist
```

Every test that touches storage runs once per driver, and each test works under
its own key prefix, so the suite never flushes the database and can run in
parallel. See [testing](testing.md) for the layout.

## Linting

```console
$ uv run ruff check .
$ uv run ruff format .
```

## The version matrix

```console
$ uvx --with tox-uv tox                       # everything
$ uvx --with tox-uv tox -e py313-django61     # one environment
```

## Documentation

```console
$ uv run mkdocs serve
$ uv run mkdocs build --strict
```

The site is built from `docs/` and published to GitHub Pages when `main`
changes.

## Benchmarks

```console
$ uv run python benchmarks/benchmark.py --records 20000
$ uv run python benchmarks/soak.py
```

The first measures the write path, the memory per result and the admin
changelist per driver. The second writes results while the changelist is read
and checks what comes back. Numbers from both are in
[performance](performance.md).

## Conventions

* Follow the style of the surrounding code; `ruff format` settles the rest.
* A change in behaviour comes with a test. Storage-level behaviour is tested
  against every driver, and anything the admin depends on is compared with
  django-celery-results in `tests/admin/test_differential.py`.
* Keep the parity table in [admin-parity](admin-parity.md) honest: a
  deliberate difference from upstream belongs there.
* Add a line to `CHANGELOG.md`.

## Releasing

Write the entries for the release under a `## x.y.z (unreleased)` heading in
`CHANGELOG.md`, then:

```console
$ uv run python scripts/release.py 0.2.0
```

The script sets `__version__`, dates the changelog heading, commits both files
and tags the commit with the changelog section as the tag message. It prints
the tag for review and pushes nothing:

```console
$ git push origin main
$ git push origin v0.2.0
```

The tag starts the `Release` workflow, which runs the matrix, builds the
distributions, publishes them to PyPI and creates the GitHub release with the
same changelog section as its notes. It refuses to build when the tag does not
match `__version__` or the changelog has no dated section for it.

## License of contributions

Contributions are accepted under the BSD 3-Clause license of the project. By
opening a pull request you agree that your changes are published under it.

## Reporting a problem

Include the driver, the Redis version, the Django and Celery versions, and the
output of `python manage.py check --database default`. If a query returns the
wrong rows, the fastest reproduction is a failing case for
`tests/store/test_equivalence.py`, which compares every driver with the
in-memory evaluator.
