# Parity with django-celery-results

The target is django-celery-results **2.6.0**. This page lists what is identical
and every known difference.

## Identical

* **Backend**: storing results with `result_extended` properties
  (`task_name`, `task_args`, `task_kwargs`, `periodic_task_name`, `worker`,
  `traceback`), `meta` with `children` and `request.meta`, `date_started` on
  `STARTED`, binary content base64 encoding, `forget`, `cleanup`, group
  save/restore/delete, chord counting and callback triggering including error
  propagation, retry with `result_backend_always_retry`.
* **Models**: `TaskResult`, `GroupResult`, `ChordCounter` with the same field
  names, types, `verbose_name`, `help_text`, nullability, defaults,
  `editable`, `Meta.ordering`, `verbose_name(_plural)`, `as_dict()`,
  `__str__()` and `ChordCounter.group_result()`.
* **Managers**: `get_task`, `store_result` (same signature),
  `get_group`, `store_group_result`, `get_all_expired`, `delete_expired`.
* **Admin**: `TaskResultAdmin` and `GroupResultAdmin` use the same
  `list_display`, `list_filter`, `date_hierarchy`, `search_fields`,
  `fieldsets`, `readonly_fields` and `ALLOW_EDITS` behaviour. The changelist,
  filters (including facet counts), search, sorting, pagination, "show all",
  date hierarchy, change form, delete view, bulk delete action, and history are
  Django's own code running on a Redis-backed queryset. `ChordCounter` is not
  registered, as upstream.
* **Views and URLs**: the four JSON views, the `task_pattern` converter and URL
  names.
* **Translations**: upstream message ids are reused, and upstream catalogues are
  shipped.

## Differences

| Topic | django-celery-results | This package | Reason |
| --- | --- | --- | --- |
| Primary key | integer `id`, `task_id` unique | `task_id` / `group_id` | Natural Redis key; no counter or id mapping. Admin URLs use the task id. |
| App label | `django_celery_results` | `django_celery_results_redis` | Both packages can be installed side by side. |
| Admin section title | "Celery Results" | "Celery Results (Redis)" | Distinguishable when both are installed. |
| `ID_FIRST_URLS` default | `True` (deprecated URLs, warning on import) | `False` | The URLs are deprecated upstream. |
| Transactions | results are written inside the caller's transaction | Redis writes are immediate and never rolled back | Redis is not a Django database. |
| `using=` arguments | select a database alias | accepted and ignored | One Redis connection. |
| `date_started` on `STARTED` | database clock (`Now()`) | Python clock (`timezone.now()`) | No database; one clock also keeps `date_started <= date_done`. |
| String comparison | database collation | Unicode code points; case-insensitive lookups use `str.casefold()` | Deterministic across drivers. |
| `NULL` ordering | database dependent | last in ascending, first in descending order (PostgreSQL behaviour) | Deterministic across drivers. |
| Unsupported query features | full ORM | no joins, annotations, `extra()`, `raw()`, set operations, `select_for_update()`; `datetimes()`/`dates()` return lists | Not needed by the admin or the backend. Such calls raise `NotSupportedError`. |
| Lookups | every lookup of the database | `exact`, `iexact`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `regex`, `iregex`, `in`, `gt`, `gte`, `lt`, `lte`, `range`, `isnull`, and the datetime transforms `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, `date` and `time` (chainable as `date__year`, `time__hour`, ...) | Database-specific lookups such as `unaccent` or `trigram_similar` raise `FieldError`; typed into an admin URL they redirect with `?e=1`. |
| Regular expressions | the database dialect (POSIX on PostgreSQL) | Python `re.search()`, like Django on SQLite | No database to delegate to. |
| MySQL repeatable-read warning | emitted by `get_task` | not applicable | |
| `_delete_group` of a missing group | raises on the unsaved instance | no-op | Upstream bug. |

## How parity is tested

* Static tests import `django_celery_results` and compare admin options, model
  field metadata, `Meta` options and method output.
* Differential tests load identical data into both packages and compare query
  results and rendered admin changelists (row ids, filter choices, facet counts,
  date hierarchy links, result counts) for a table of URL parameters.
* The upstream backend and model tests are ported and run against every driver.
