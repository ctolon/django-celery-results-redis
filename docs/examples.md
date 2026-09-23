# Examples

Short recipes for the things projects usually need. Every snippet runs against
the current API. See [settings](settings.md) for the full list of options.

## A minimal project

```python
# proj/settings.py
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_celery_results_redis",
]

DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
}

CELERY_BROKER_URL = "redis://localhost:6379/1"
CELERY_RESULT_BACKEND = "django-redis-db"
CELERY_RESULT_EXTENDED = True
CELERY_TASK_TRACK_STARTED = True
```

```python
# proj/celery.py
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")

app = Celery("proj")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# proj/__init__.py
from .celery import app as celery_app

__all__ = ("celery_app",)
```

```python
# billing/tasks.py
from celery import shared_task


@shared_task
def send_invoice(invoice_id):
    return {"invoice": invoice_id, "sent": True}
```

```console
$ python manage.py migrate django_celery_results_redis
$ celery -A proj worker -l info
```

The migration creates no table. It registers the models so that content types
and admin permissions exist. Workers do not open a database connection.

## Querying results

```python
from datetime import timedelta

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from django_celery_results_redis.models import TaskResult

since = timezone.now() - timedelta(days=1)

# The last failures.
failures = (
    TaskResult.objects.filter(status="FAILURE", date_done__gte=since)
    .order_by("-date_done")[:20]
)

# One task's history.
history = TaskResult.objects.filter(
    task_name="billing.tasks.send_invoice"
).order_by("-date_done")

# One result by id, with the dictionary the backend stores.
TaskResult.objects.get(task_id="6f0a...").as_dict()

# Several results by id.
TaskResult.objects.in_bulk(["6f0a...", "9b31..."])
```

`get_task()` returns an unsaved instance with status `PENDING` when the task id
is unknown, which is how Celery reads a result that was never stored:

```python
TaskResult.objects.get_task("unknown-id").status  # "PENDING"
```

Counting needs `aggregate()`, because `annotate()` and `values().annotate()` are
not available:

```python
statuses = (
    TaskResult.objects.values_list("status", flat=True).distinct().order_by("status")
)
counts = TaskResult.objects.aggregate(
    **{status: Count("pk", filter=Q(status=status)) for status in statuses}
)
# {"FAILURE": 3, "SUCCESS": 120}

TaskResult.objects.aggregate(
    total=Count("pk"), first=Min("date_done"), last=Max("date_done")
)
```

Datetime transforms and regular expressions work like Django's:

```python
TaskResult.objects.filter(date_done__year=2026, date_done__month=9)
TaskResult.objects.filter(date_done__date=timezone.localdate())
TaskResult.objects.filter(date_done__hour__gte=22)
TaskResult.objects.filter(date_done__week_day=2)  # Monday, Sunday is 1
TaskResult.objects.filter(task_name__regex=r"\.send_\w+$")
TaskResult.objects.filter(worker__isnull=True)
TaskResult.objects.filter(status__in=["FAILURE", "RETRY"])
TaskResult.objects.datetimes("date_done", "day")
```

[parity](admin-parity.md) lists every supported lookup. Joins, annotations,
`extra()`, `raw()` and set operations raise `NotSupportedError`.

Deleting old results by hand:

```python
TaskResult.objects.delete_expired(timedelta(days=7))
TaskResult.objects.filter(status="SUCCESS", date_done__lt=since).delete()
```

## Reacting to a result

```python
# billing/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver

from django_celery_results_redis.models import TaskResult


@receiver(post_save, sender=TaskResult)
def alert_on_failure(sender, instance, created, **kwargs):
    if instance.status == "FAILURE":
        notify(f"{instance.task_name} failed: {instance.task_id}")
```

Connect it from the app's `ready()`:

```python
# billing/apps.py
from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "billing"

    def ready(self):
        from . import signals  # noqa: F401
```

`pre_save` and `post_save` fire for every state change a worker stores. Storing
a result normally costs one round trip; when a listener is connected the manager
reads the record first so that the instance passed to the signal is complete,
which costs a second round trip per state change. Connect listeners in the
processes that need them.

## Expiring results

Celery's own cleanup task deletes results older than `result_expires`. It calls
`backend.cleanup()`, which removes both task and group results.

```python
# proj/settings.py
from celery.schedules import crontab

CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7  # seven days
CELERY_BEAT_SCHEDULE = {
    "celery.backend_cleanup": {
        "task": "celery.backend_cleanup",
        "schedule": crontab(hour=4, minute=0),
    },
}
```

```console
$ celery -A proj beat -l info
```

`RESULT_TTL` is the alternative: Redis expires each key itself, with no beat
process involved.

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "raw",
    "URL": "redis://localhost:6379/0",
    "RESULT_TTL": 60 * 60 * 24,
}
```

Use it with the `raw` or `redis_om` driver, and when you do not run Celery beat.
The `indexed` driver does not support it: an expiring key would leave its index
entries behind. See [drivers](drivers.md).

## Customizing the admin

The models are registered on the default admin site. Replace the registration to
change it:

```python
# billing/admin.py
from django.contrib import admin

from django_celery_results_redis.admin import TaskResultAdmin
from django_celery_results_redis.models import TaskResult

from proj.celery import app as celery_app


class RetryableTaskResultAdmin(TaskResultAdmin):
    list_display = TaskResultAdmin.list_display + ("traceback_first_line",)
    actions = ["requeue"]

    @admin.display(description="error")
    def traceback_first_line(self, obj):
        return (obj.traceback or "").splitlines()[:1]

    @admin.action(description="Re-queue the selected failed tasks")
    def requeue(self, request, queryset):
        requeued = 0
        for result in queryset.filter(status="FAILURE"):
            if not result.task_name:
                continue
            celery_app.send_task(result.task_name, task_id=result.task_id)
            requeued += 1
        self.message_user(request, f"{requeued} tasks re-queued.")


admin.site.unregister(TaskResult)
admin.site.register(TaskResult, RetryableTaskResultAdmin)
```

Re-queuing needs the original arguments, which are only stored when
`CELERY_RESULT_EXTENDED` is on, and are stored in their `repr()` form. Send the
arguments your own code knows about rather than parsing `task_args`.

The change form is read only by default. To allow edits:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "ALLOW_EDITS": True,
}
```

Giving staff read-only access is a matter of permissions: grant
`django_celery_results_redis.view_taskresult` and nothing else.

```python
from django.contrib.auth.models import Group, Permission

group = Group.objects.create(name="Support")
group.permissions.add(
    Permission.objects.get(codename="view_taskresult"),
    Permission.objects.get(codename="view_groupresult"),
)
```

See [admin](admin.md) for what each part of the changelist does.

## Connecting to Redis

Sentinel, using the URL format Celery uses:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "sentinel://sentinel-a:26379;sentinel://sentinel-b:26379/0",
    "SENTINEL_MASTER_NAME": "results",
    "SENTINEL_KWARGS": {"password": "sentinel-password"},
    "CLIENT_KWARGS": {"password": "redis-password"},
}
```

TLS:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "URL": "rediss://cache.example.com:6380/0",
    "CLIENT_KWARGS": {"ssl_cert_reqs": "required"},
}
```

A factory gives full control over the client, including the connection pool:

```python
# proj/redis_client.py
import redis

POOL = redis.ConnectionPool.from_url(
    "redis://localhost:6379/0",
    decode_responses=True,
    max_connections=50,
    socket_timeout=5,
    socket_connect_timeout=2,
    health_check_interval=30,
)


def redis_client():
    return redis.Redis(connection_pool=POOL)
```

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "CLIENT_FACTORY": "proj.redis_client.redis_client",
}
```

The factory must return a client created with `decode_responses=True`. It takes
precedence over `URL`.

Separate environments that share one server by prefix:

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": os.environ["REDIS_URL"],
    "KEY_PREFIX": f"dcrr-{os.environ.get('ENVIRONMENT', 'dev')}",
}
```

Each prefix carries its own records, indexes and storage format version, so
staging cannot read or delete production results.

## Turning on the search index

```python
DJANGO_CELERY_RESULTS_REDIS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
    "SEARCH": "redisearch",
    "SEARCH_MAX_TEXT": 1024,
}
```

```console
$ python manage.py celery_results_redis_rebuild_index
Rebuilt the indexed indexes of django_celery_results_redis.TaskResult.
Rebuilt the indexed indexes of django_celery_results_redis.GroupResult.
```

The command writes the index fields of results that were stored before the
setting was added. New results are indexed as they are written. The server needs
RediSearch and database 0. See [drivers](drivers.md#search-index) for the cost.

## Health check

The system checks report whether Redis answers, whether the server is recent
enough and whether its eviction policy can drop results.

```python
# proj/views.py
from django.core.checks import run_checks
from django.http import JsonResponse


def result_store_health(request):
    messages = run_checks(tags=["database"], databases=["default"])
    problems = [
        {"id": message.id, "message": message.msg}
        for message in messages
        if message.id.startswith("django_celery_results_redis.")
    ]
    return JsonResponse(
        {"ok": not problems, "problems": problems},
        status=200 if not problems else 503,
    )
```

The same checks run on the command line:

```console
$ python manage.py check --database default
```

An unreachable server reports `django_celery_results_redis.E101`. The full list
is in [settings](settings.md#system-checks).
