# Admin

The models are registered on the default admin site when the app is installed,
under the section "Celery Results (Redis)". The admin is Django's own: the
changelist, filters, search, date hierarchy, change form, actions and history
are the stock implementations running on a queryset backed by Redis.
[parity](admin-parity.md) compares it with django-celery-results field by field.

## Task results

Columns, in order: `task_id`, `periodic_task_name`, `task_name`, `date_done`,
`status`, `worker`. Rows are ordered by `date_done`, newest first. Every column
is sortable; clicking one sorts by it and breaks ties by task id.

The right hand sidebar holds five filters:

| Filter | Choices |
| --- | --- |
| `status` | every status that occurs in the stored results, plus "All" |
| `date_done` | Any date, Today, Past 7 days, This month, This year |
| `periodic_task_name` | the periodic task names that occur, plus "None" for results that have none |
| `task_name` | the task names that occur, plus "None" |
| `worker` | the workers that occur, plus "None" |

The choices come from the values actually stored, so a filter shrinks as old
results expire. With the `indexed` and `redis_om` drivers they are read from an
index rather than from the records.

`?_facets=` appends the number of matching results to each filter choice, as in
any Django admin. The counts respect the other active filters.

The search box searches `task_name`, `task_id`, `status`, `task_args` and
`task_kwargs`. Terms are matched case-insensitively anywhere in the value, and
several terms must all match. Quoted phrases are matched as one term. Searching
reads every result the other filters left over, unless the search index is
enabled; see [drivers](drivers.md#search-index).

The date hierarchy above the table drills down `date_done` by year, then month,
then day, in the active time zone.

## Group results

Columns: `group_id` and `date_done`, ordered by `date_done`, newest first.
Filterable by `date_done`, searchable by `group_id`. Chord counters are internal
and are not registered, which matches upstream.

## Editing

The change form is read only by default: every field is rendered as text and a
POST changes nothing. This is the upstream behaviour and the reason is that
results are written by workers.

```python
DJANGO_CELERY_RESULTS_REDIS = {"ALLOW_EDITS": True}
```

With `ALLOW_EDITS` on, everything except `date_created`, `date_started`,
`date_done`, `result` and `meta` becomes editable. The upstream setting
`DJANGO_CELERY_RESULTS["ALLOW_EDITS"]` is honoured as a fallback, so a project
moving over keeps its setting.

Changing `task_id` moves the record to the new id. Saving a `task_id` that
another result already uses is rejected with the usual uniqueness error.

## Permissions

Permissions are the standard model permissions, created by `migrate`:

* `django_celery_results_redis.view_taskresult`
* `django_celery_results_redis.change_taskresult`
* `django_celery_results_redis.delete_taskresult`
* `django_celery_results_redis.add_taskresult`
* the same four for `groupresult`

View permission alone gives a read-only changelist and change form. Staff
without any of them get a 403, and anonymous users are redirected to the login
page.

## History

Deletions and, with `ALLOW_EDITS`, changes made in the admin are written to
Django's `LogEntry` table, and the history view of an object lists them. History
records live in the database, not in Redis, so they survive the results they
describe. Results written by workers are not admin actions and produce no
history entries.

The `LogEntry` row is written inside the request's database transaction while
the Redis write is immediate. A rolled back request can therefore leave a change
applied in Redis without a history entry.

## A custom admin site

```python
# proj/admin.py
from django.contrib.admin import AdminSite

from django_celery_results_redis.admin import GroupResultAdmin, TaskResultAdmin
from django_celery_results_redis.models import GroupResult, TaskResult


class OperationsAdminSite(AdminSite):
    site_header = "Operations"


operations_site = OperationsAdminSite(name="operations")
operations_site.register(TaskResult, TaskResultAdmin)
operations_site.register(GroupResult, GroupResultAdmin)
```

```python
# proj/urls.py
from django.urls import path

from proj.admin import operations_site

urlpatterns = [
    path("operations/", operations_site.urls),
]
```

To keep the models off the default site as well, unregister them:

```python
from django.contrib import admin

admin.site.unregister(TaskResult)
admin.site.unregister(GroupResult)
```

[examples](examples.md#customizing-the-admin) shows how to subclass the admin
classes and add an action.
